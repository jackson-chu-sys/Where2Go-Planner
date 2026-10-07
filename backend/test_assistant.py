"""TASK-11a 全局 AI 助手单测:``services.assistant`` + ``/api/assistant/messages``(POST/GET/DELETE)。

口径(契约 TASK-11a 逐字):

* 响应形状**键名逐字** —— POST 是 ``chat_turn`` 的原样透出
  ``{"session_key","reply","degraded","reason","message_id"}``;GET 是
  ``{"session_key","items","note"}``;DELETE 是 ``{"session_key","deleted","note"}``;
* **预算按调用放大** —— ``max_tokens=800`` / ``timeout=120`` 必须真的传给 ``llm.chat``
  (沿用 intro 的 120/20s 默认值会把回答静默截断,这是本项目的老坑);
* **降级四态不 500**(no_key / timeout / error / parse_error)—— ``degraded=True`` + 正确
  ``reason`` + **非空中文** ``reply``,消息照样落库可回放;只有"参数不对"才 **400 中文**;
* ``context`` 截 500 字进 prompt、``None``/空白**整块不拼**;``session_key`` 空则补发
  ``chat-`` 前缀,给了就**原样存取**(服务层不校验前缀);
* **复用** planner 的两张会话表(表结构零改动),所以顺带断言 ``/api/planner/*`` 与
  ``/api/trip-plans`` 的既有响应键**逐键零变化**。

全套**纯 mock 不触网**(``no_network`` autouse 把 ``requests.Session.request`` 换成抛错;
LLM 用替身顶掉 ``assistant.default_llm_client`` / ``planner.default_llm_client``),
HTTP 链走手拼 ASGI scope(仓库没装 httpx)。

运行:``cd backend && ../.venv/bin/python -m pytest -q``
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Callable, Optional

import pytest
import requests
from sqlalchemy import select

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.main import app  # noqa: E402
from data_sources import DataSourceError, TransientDataSourceError  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db import models  # noqa: E402
from db import repository as repo  # noqa: E402
from db.base import get_session  # noqa: E402
from services import assistant  # noqa: E402
from services import intro  # noqa: E402
from services import planner  # noqa: E402

# --------------------------------------------------------------------------- #
# 逐字键集(前端只读这些键,多一个少一个都算回归)
# --------------------------------------------------------------------------- #

CHAT_KEYS = {"session_key", "reply", "degraded", "reason", "message_id"}
GET_KEYS = {"session_key", "items", "note"}
DELETE_KEYS = {"session_key", "deleted", "note"}
ITEM_KEYS = {"role", "content", "payload", "created_at"}

PLANNER_MESSAGE_KEYS = {
    "session_key", "title", "reply", "itinerary",
    "degraded", "reason", "turn_index", "generated_at",
}
PLANNER_GET_KEYS = {"session_key", "title", "items"}
PLANNER_DELETE_KEYS = {"session_key", "deleted"}
PLANNER_ITINERARY_KEYS = {"days", "days_count", "summary", "unused_collections"}

TRIP_PLAN_POST_KEYS = {
    "trip_plan", "quote", "created", "idempotent", "nights", "count", "elapsed_s", "note",
}
TRIP_PLAN_GET_KEYS = {
    "trip_plans", "count", "limit", "total", "nights", "kind", "elapsed_s", "note",
}
TRIP_PLAN_DICT_KEYS = {
    "id", "name", "note", "place_collection_id", "route_collection_ids",
    "stay_collection_ids", "counts", "quote", "created_at", "updated_at",
}
QUOTE_KEYS = {
    "total_cny_low", "total_cny_high", "transport_cny", "stay_nights",
    "per_stay", "missing", "kind", "note",
}

GOOD_REPLY = "西湖边适合慢走:白堤 → 孤山 → 曲院风荷,半天足够,门票免费但节假日人多。"
PLANNER_JSON = json.dumps(
    {
        "days": [
            {
                "day": 1,
                "base": "杭州",
                "stops": [
                    {"name": "西湖", "reason": "湖边步道适合慢走", "collection_id": None},
                    {"name": "浙江省博物馆", "reason": "离西湖步行 10 分钟", "collection_id": None},
                ],
                "tip": "上午人少",
            }
        ],
        "days_count": 1,
        "summary": "一天都在西湖西侧",
        "unused_collections": [],
    },
    ensure_ascii=False,
)


# --------------------------------------------------------------------------- #
# 替身 / 样本数据
# --------------------------------------------------------------------------- #


class FakeLLM:
    """``intro.LLMClient`` 替身:记下每次 chat 的入参,按剧本返回文本或抛异常。"""

    def __init__(
        self,
        reply: str = GOOD_REPLY,
        *,
        enabled: bool = True,
        error: Optional[Exception] = None,
    ) -> None:
        self.reply = reply
        self.enabled = enabled
        self.error = error
        self.label = "替身 · fake"
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> str:
        self.calls.append(
            {"prompt": prompt, "system": system, "max_tokens": max_tokens, "timeout": timeout}
        )
        if self.error is not None:
            raise self.error
        return self.reply


def http_request(method: str, path: str, *, body: Any = None, query: str = "") -> tuple[int, Any]:
    """直接驱动 ASGI app 走一遍**完整 HTTP 链**(含 FastAPI 解析),返回 ``(状态码, JSON)``。"""
    raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
        ],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    chunks = [raw]
    payload = bytearray()
    status: dict[str, int] = {}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": chunks.pop(0) if chunks else b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            payload.extend(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return status["code"], json.loads(payload.decode() or "null")


def seed_collection(session, *, name: str = "西湖", osm_id: int = -1234) -> Any:
    """一条收藏(零回归断言里 ``/api/trip-plans`` 要用它当目的地引用)。"""
    row, _ = repo.upsert_collection(
        session,
        kind="place",
        mode=None,
        name=name,
        osm_type="way",
        osm_id=osm_id,
        to_lat=30.25,
        to_lng=120.15,
        to_name=name,
    )
    session.commit()
    return row


def seed_history(session, key: str, *, count: int) -> None:
    """直接往会话里塞 ``count`` 条历史消息(测 :data:`assistant.ASSISTANT_HISTORY_LIMIT` 用)。"""
    row, _ = planner.get_or_create_session(session, session_key=key, title="历史")
    for index in range(count):
        planner.add_message(
            session,
            row,
            role="user" if index % 2 == 0 else "assistant",
            content=f"历史第{index}条",
        )
    session.commit()


def counts(session) -> dict[str, int]:
    """几张表的行数(助手绝不落 Place / TripPlan,会话表复用 planner 的两张)。"""
    return {
        "place": len(list(session.scalars(select(models.Place)))),
        "collection": len(list(session.scalars(select(models.Collection)))),
        "trip_plan": len(list(session.scalars(select(models.TripPlan)))),
        "planner_session": len(list(session.scalars(select(models.PlannerSession)))),
        "planner_message": len(list(session.scalars(select(models.PlannerMessage)))),
    }


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底:任何 requests 调用都视为测试失败(本套单测必须纯 mock)。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture()
def engine(tmp_path):
    made = make_engine(f"sqlite:///{tmp_path / 'assistant_test.db'}")
    init_db(made)
    try:
        yield made
    finally:
        made.dispose()


@pytest.fixture()
def session(engine):
    """每个用例一个独立的临时 SQLite 库(**绝不碰 backend/data/where2go.db**)。"""
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()


@pytest.fixture()
def llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """把 assistant 的 LLM 入口换成替身(默认回一段中文纯文本)。"""
    fake = FakeLLM()
    monkeypatch.setattr(assistant, "default_llm_client", lambda: fake)
    return fake


@pytest.fixture()
def planner_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """把 planner 的 LLM 入口换成替身(默认回一份合法行程 JSON)—— 零回归断言用。"""
    fake = FakeLLM(PLANNER_JSON)
    monkeypatch.setattr(planner, "default_llm_client", lambda: fake)
    return fake


@pytest.fixture()
def http(session) -> Callable[..., tuple[int, Any]]:
    """完整 HTTP 链客户端:``get_session`` 依赖换成用例里的同一个临时库会话。"""
    app.dependency_overrides[get_session] = lambda: session
    yield lambda method, path, **kwargs: http_request(method, path, **kwargs)
    app.dependency_overrides.clear()


def post_message(http, body: Optional[dict[str, Any]]) -> tuple[int, Any]:
    return http("POST", "/api/assistant/messages", body=body)


# --------------------------------------------------------------------------- #
# 路由挂载(app/main.py 的 include_router)
# --------------------------------------------------------------------------- #


def test_assistant_routes_are_mounted_under_api_prefix() -> None:
    """``app/main.py`` 的 ``include_router(assistant.router, prefix="/api")`` 真的挂上了。"""
    paths = app.openapi()["paths"]
    assert set(paths["/api/assistant/messages"]) >= {"post", "get", "delete"}


# --------------------------------------------------------------------------- #
# 常量与预算(必须按调用放大,不能沿用 intro 的 120 tokens / 20s)
# --------------------------------------------------------------------------- #


def test_budget_constants_are_the_contract_values() -> None:
    assert assistant.ASSISTANT_MAX_TOKENS == 800
    assert assistant.ASSISTANT_TIMEOUT_S == 120
    assert assistant.ASSISTANT_HISTORY_LIMIT == 12
    assert assistant.CONTEXT_MAX_CHARS == 500
    assert assistant.MAX_MESSAGE_LEN == planner.MAX_MESSAGE_LEN == 2000


def test_chat_turn_passes_budget_per_call(session, llm) -> None:
    """``max_tokens`` / ``timeout`` 是**按调用**传进 ``llm.chat`` 的(不是 intro 的默认值)。"""
    result = assistant.chat_turn("chat-1", "西湖怎么玩?", session=session)
    assert result["degraded"] is False
    call = llm.calls[0]
    assert call["max_tokens"] == 800 == assistant.ASSISTANT_MAX_TOKENS
    assert call["timeout"] == 120 == assistant.ASSISTANT_TIMEOUT_S
    assert call["max_tokens"] != intro.DEFAULT_MAX_TOKENS
    assert call["timeout"] != intro.DEFAULT_TIMEOUT_S
    assert call["system"] == assistant.SYSTEM_PROMPT


def test_system_prompt_is_general_travel_assistant(session, llm) -> None:
    """系统提示 = 通用旅行助手:答旅行问题 / 拿不到的事实明说 / 中文纯文本 ≤400 字。"""
    assistant.chat_turn("chat-1", "西湖怎么玩?", session=session)
    system = llm.calls[0]["system"]
    assert "旅行助手" in system
    assert "禁止编造" in system
    assert "400" in system and "纯文本" in system
    assert "JSON" in system  # 明确禁止输出 JSON
    assert "行程" in system  # 排完整行程仍归「🤖 AI 行程」那个入口


def test_chat_turn_returns_exact_keys_and_message_id(session, llm) -> None:
    result = assistant.chat_turn("chat-1", "西湖怎么玩?", session=session)
    assert set(result) == CHAT_KEYS
    assert result["session_key"] == "chat-1"
    assert result["reply"] == GOOD_REPLY
    assert result["degraded"] is False and result["reason"] is None
    assistant_row = session.scalars(
        select(models.PlannerMessage).where(models.PlannerMessage.role == "assistant")
    ).one()
    assert result["message_id"] == assistant_row.id


# --------------------------------------------------------------------------- #
# 降级四态(一律不抛:degraded=True + reason 正确 + reply 非空中文,消息照样落库)
# --------------------------------------------------------------------------- #


def test_degraded_no_key(session, monkeypatch) -> None:
    fake = FakeLLM(enabled=False)
    monkeypatch.setattr(assistant, "default_llm_client", lambda: fake)
    result = assistant.chat_turn("chat-nokey", "西湖怎么玩?", session=session)
    assert fake.calls == []  # 没 key 就不发请求
    assert result["degraded"] is True and result["reason"] == "no_key"
    assert result["reply"] and "no_key" in result["reply"] and "AI 助手" in result["reply"]
    assert set(result) == CHAT_KEYS


def test_degraded_no_key_with_real_client_and_empty_environ(session, monkeypatch) -> None:
    """不靠替身:真 ``LLMClient`` + 空环境变量 → ``enabled=False`` → 同样降级不抛。"""
    monkeypatch.setattr(assistant, "default_llm_client", lambda: assistant.LLMClient(environ={}))
    assert assistant.resolve_provider({}) is None  # 空环境 = 没有可用 key
    result = assistant.chat_turn("chat-nokey", "西湖怎么玩?", session=session)
    assert result["degraded"] is True and result["reason"] == "no_key"
    assert "no_key" in result["reply"]


@pytest.mark.parametrize(
    "error",
    [
        TransientDataSourceError("llm", "读超时:120s 内没返回"),
        DataSourceError("llm", "ReadTimeout"),
        TimeoutError("timed out"),
    ],
)
def test_degraded_timeout(session, monkeypatch, error) -> None:
    monkeypatch.setattr(assistant, "default_llm_client", lambda: FakeLLM(error=error))
    result = assistant.chat_turn("chat-timeout", "西湖怎么玩?", session=session)
    assert result["degraded"] is True and result["reason"] == "timeout"
    assert result["reply"] and "timeout" in result["reply"]


@pytest.mark.parametrize(
    "error",
    [
        DataSourceError("llm", "上游 500"),
        RuntimeError("boom"),
    ],
)
def test_degraded_error(session, monkeypatch, error) -> None:
    monkeypatch.setattr(assistant, "default_llm_client", lambda: FakeLLM(error=error))
    result = assistant.chat_turn("chat-error", "西湖怎么玩?", session=session)
    assert result["degraded"] is True and result["reason"] == "error"
    assert result["reply"] and "error" in result["reply"]


@pytest.mark.parametrize("reply", ["", "   ", None, '{"answer":"西湖"}', "```json\n{\"a\":1}\n```"])
def test_degraded_parse_error(session, monkeypatch, reply) -> None:
    """空回复 / 回了 JSON(系统提示要求纯文本)→ ``parse_error``,绝不把 JSON 甩给用户。"""
    monkeypatch.setattr(assistant, "default_llm_client", lambda: FakeLLM(reply))
    result = assistant.chat_turn("chat-parse", "西湖怎么玩?", session=session)
    assert result["degraded"] is True and result["reason"] == "parse_error"
    assert result["reply"] and "parse_error" in result["reply"]
    assert not result["reply"].startswith("{")


def test_degraded_turns_are_still_persisted(session, monkeypatch) -> None:
    """四态降级都**照样落库**(前端刷新后能回放失败原因,不静默)。"""
    cases = [
        (FakeLLM(enabled=False), "no_key"),
        (FakeLLM(error=TransientDataSourceError("llm", "读超时")), "timeout"),
        (FakeLLM(error=DataSourceError("llm", "上游 500")), "error"),
        (FakeLLM(""), "parse_error"),
    ]
    for index, (fake, reason) in enumerate(cases):
        monkeypatch.setattr(assistant, "default_llm_client", lambda fake=fake: fake)
        key = f"chat-degraded-{index}"
        result = assistant.chat_turn(key, "西湖怎么玩?", session=session)
        assert result["reason"] == reason
        items = assistant.list_messages(key, session=session)
        assert [item["role"] for item in items] == ["user", "assistant"]
        payload = items[1]["payload"]
        assert payload["degraded"] is True and payload["reason"] == reason
        assert payload["turn_index"] == 1 and payload["generated_at"]
        assert items[1]["content"] == result["reply"]


# --------------------------------------------------------------------------- #
# context:截 500 字进 prompt;None / 空白整块不拼
# --------------------------------------------------------------------------- #


def test_context_is_truncated_to_500_chars(session, llm) -> None:
    context = "城" * 800
    assistant.chat_turn("chat-ctx", "西湖怎么玩?", context=context, session=session)
    prompt = llm.calls[0]["prompt"]
    assert assistant.CONTEXT_HEADER in prompt
    assert "城" * 500 in prompt
    assert "城" * 501 not in prompt
    stored = assistant.list_messages("chat-ctx", session=session)[0]["payload"]["context"]
    assert stored == "城" * 500 and len(stored) == 500


def test_short_context_is_kept_verbatim(session, llm) -> None:
    assistant.chat_turn("chat-ctx2", "近吗?", context="杭州 · 25_50 环带 · 收藏:西湖", session=session)
    prompt = llm.calls[0]["prompt"]
    assert "杭州 · 25_50 环带 · 收藏:西湖" in prompt
    assert assistant.list_messages("chat-ctx2", session=session)[0]["payload"]["context"] == (
        "杭州 · 25_50 环带 · 收藏:西湖"
    )


@pytest.mark.parametrize("context", [None, "", "   ", ["杭州"], {"city": "杭州"}])
def test_blank_context_is_not_in_prompt(session, llm, context) -> None:
    """``None`` / 空白 / 非文本 → **整块不拼**进 prompt(``payload.context`` 存 ``None``)。"""
    assistant.chat_turn("chat-noctx", "西湖怎么玩?", context=context, session=session)
    prompt = llm.calls[0]["prompt"]
    assert assistant.CONTEXT_HEADER not in prompt
    assert "当前页面上下文" not in prompt
    assert assistant.list_messages("chat-noctx", session=session)[0]["payload"]["context"] is None


# --------------------------------------------------------------------------- #
# session_key:空则补发 chat- 前缀;给了就原样存取(服务层不校验前缀)
# --------------------------------------------------------------------------- #


def test_blank_session_key_gets_chat_prefix(session, llm) -> None:
    for blank in ("", "   ", None):
        key = assistant.chat_turn(blank, "西湖怎么玩?", session=session)["session_key"]
        assert key.startswith("chat-")
        assert len(key) == len("chat-") + 32  # uuid4().hex
    assert assistant.new_session_key().startswith(assistant.SESSION_PREFIX)
    # 三个空 key 各自新建会话,不会串在一起
    assert counts(session)["planner_session"] == 3


def test_given_session_key_is_stored_verbatim(session, llm) -> None:
    """前缀只是约定:给 ``trip-`` 甚至无前缀的 key 也原样存取(服务层不校验)。"""
    for key in ("trip-abc", "no-prefix-key", "chat-x" * 5):
        result = assistant.chat_turn(key, "西湖怎么玩?", session=session)
        assert result["session_key"] == key
        assert assistant.list_messages(key, session=session)
    rows = {row.session_key for row in session.scalars(select(models.PlannerSession))}
    assert rows == {"trip-abc", "no-prefix-key", "chat-x" * 5}


def test_same_session_key_reuses_same_row(session, llm) -> None:
    first = assistant.chat_turn("chat-reuse", "第一轮", session=session)
    second = assistant.chat_turn("chat-reuse", "第二轮", session=session)
    assert first["session_key"] == second["session_key"] == "chat-reuse"
    assert counts(session)["planner_session"] == 1
    assert counts(session)["planner_message"] == 4
    assert second["message_id"] > first["message_id"]


# --------------------------------------------------------------------------- #
# 落库 / 回放 / 清空
# --------------------------------------------------------------------------- #


def test_messages_are_persisted_and_replayable(session, llm) -> None:
    result = assistant.chat_turn("chat-1", "西湖怎么玩?", context="杭州", session=session)
    items = assistant.list_messages("chat-1", session=session)
    assert [item["role"] for item in items] == ["user", "assistant"]
    for item in items:
        assert set(item) == ITEM_KEYS
    assert items[0]["content"] == "西湖怎么玩?"
    assert items[1]["content"] == result["reply"] == GOOD_REPLY
    assert items[1]["payload"]["degraded"] is False and items[1]["payload"]["reason"] is None
    # 助手**只写会话两张表**:绝不落 Place / Collection / TripPlan
    assert counts(session) == {
        "place": 0, "collection": 0, "trip_plan": 0,
        "planner_session": 1, "planner_message": 2,
    }


def test_list_messages_unknown_session_returns_empty(session, llm) -> None:
    assert assistant.list_messages("chat-does-not-exist", session=session) == []
    assert assistant.list_messages("", session=session) == []
    assert assistant.list_messages(None, session=session) == []


def test_list_messages_limit_is_clamped_and_time_ordered(session, llm) -> None:
    seed_history(session, "chat-many", count=210)
    assert len(assistant.list_messages("chat-many", limit=5, session=session)) == 5
    assert len(assistant.list_messages("chat-many", limit=9999, session=session)) == 200
    latest = assistant.list_messages("chat-many", limit=3, session=session)
    assert [item["content"] for item in latest] == ["历史第207条", "历史第208条", "历史第209条"]
    for bad in (0, -3, "abc", None):
        assert len(assistant.list_messages("chat-many", limit=bad, session=session)) == 50


def test_clear_session_deletes_messages_but_keeps_row(session, llm) -> None:
    assistant.chat_turn("chat-clear", "西湖怎么玩?", session=session)
    assert assistant.clear_session("chat-clear", session=session) == 2
    assert assistant.list_messages("chat-clear", session=session) == []
    # 会话行保留 → 同一个 key 还能继续问(前端不必换 key)
    assert counts(session)["planner_session"] == 1
    assert counts(session)["planner_message"] == 0
    assert assistant.chat_turn("chat-clear", "再来一轮", session=session)["reply"] == GOOD_REPLY
    assert len(assistant.list_messages("chat-clear", session=session)) == 2
    assert assistant.clear_session("chat-unknown", session=session) == 0  # 未知 key 不抛


def test_chat_turn_opens_and_closes_its_own_session_when_none(
    engine, session, llm, monkeypatch
) -> None:
    """``session=None``(脚本/CLI 直调)时自己开一个会话,用完**关掉**。"""
    opened: list[Any] = []
    closed: list[Any] = []

    def fake_open_session() -> Any:
        own = session_factory(engine)()
        original_close = own.close

        def tracking_close() -> None:
            closed.append(own)
            original_close()

        own.close = tracking_close
        opened.append(own)
        return own

    monkeypatch.setattr(assistant, "open_session", fake_open_session)
    result = assistant.chat_turn("chat-own", "西湖怎么玩?")
    assert result["reply"] == GOOD_REPLY and result["degraded"] is False
    assert len(opened) == 1 and closed == opened  # 自己开的会话自己关
    session.rollback()
    assert len(assistant.list_messages("chat-own", session=session)) == 2


def test_chat_turn_validates_message(session, llm) -> None:
    for blank in ("", "   ", None, ["西湖"], {"message": "西湖"}):
        with pytest.raises(ValueError) as exc:
            assistant.chat_turn("chat-1", blank, session=session)
        assert "message" in str(exc.value)
    with pytest.raises(ValueError) as exc:
        assistant.chat_turn("chat-1", "问" * 2001, session=session)
    assert "过长" in str(exc.value) and "2000" in str(exc.value)
    assert llm.calls == [] and counts(session)["planner_message"] == 0


# --------------------------------------------------------------------------- #
# 多轮历史:第二次调用的 prompt 要带上上一轮
# --------------------------------------------------------------------------- #


def test_second_turn_prompt_contains_previous_turn(session, llm) -> None:
    assistant.chat_turn("chat-multi", "西湖怎么玩?", session=session)
    assistant.chat_turn("chat-multi", "那第二天去哪儿?", session=session)
    assert len(llm.calls) == 2
    first_prompt, second_prompt = llm.calls[0]["prompt"], llm.calls[1]["prompt"]
    assert assistant.EMPTY_HISTORY in first_prompt
    assert "西湖怎么玩?" not in first_prompt.split("【本轮用户消息】")[0]  # 本轮消息不重复进历史
    assert "用户:西湖怎么玩?" in second_prompt
    assert f"助手:{GOOD_REPLY}" in second_prompt
    assert "用户:那第二天去哪儿?" not in second_prompt.split("【历史对话")[1].split("【本轮用户消息】")[0]
    assert "那第二天去哪儿?" in second_prompt.split("【本轮用户消息】")[1]


def test_history_is_capped_at_twelve_messages(session, llm) -> None:
    seed_history(session, "chat-cap", count=30)
    assistant.chat_turn("chat-cap", "现在的问题", session=session)
    prompt = llm.calls[0]["prompt"]
    history = prompt.split("【本轮用户消息】")[0]
    assert "历史第29条" in history and "历史第18条" in history
    assert "历史第17条" not in history and "历史第1条" not in history
    assert assistant.ASSISTANT_HISTORY_LIMIT == 12


# --------------------------------------------------------------------------- #
# API:POST /api/assistant/messages
# --------------------------------------------------------------------------- #


def test_api_post_returns_chat_turn_shape_verbatim(http, session, llm) -> None:
    status, body = post_message(http, {"message": "西湖怎么玩?", "context": "杭州 · 0_25"})
    assert status == 200
    assert set(body) == CHAT_KEYS
    assert body["session_key"].startswith("chat-")
    assert body["reply"] == GOOD_REPLY
    assert body["degraded"] is False and body["reason"] is None
    assert isinstance(body["message_id"], int) and body["message_id"] > 0
    call = llm.calls[0]
    assert call["max_tokens"] == 800 and call["timeout"] == 120
    assert "杭州 · 0_25" in call["prompt"]
    # 落库了,并且能用返回的 key 回放
    status, listing = http("GET", "/api/assistant/messages", query=f"session_key={body['session_key']}")
    assert status == 200 and set(listing) == GET_KEYS
    assert [item["role"] for item in listing["items"]] == ["user", "assistant"]
    assert set(listing["items"][0]) == ITEM_KEYS
    assert listing["items"][0]["payload"]["context"] == "杭州 · 0_25"


def test_api_post_reuses_given_session_key(http, session, llm) -> None:
    first = post_message(http, {"session_key": "chat-fixed", "message": "第一轮"})[1]
    second = post_message(http, {"session_key": "chat-fixed", "message": "第二轮"})[1]
    assert first["session_key"] == second["session_key"] == "chat-fixed"
    assert counts(session)["planner_session"] == 1
    assert counts(session)["planner_message"] == 4
    listing = http("GET", "/api/assistant/messages", query="session_key=chat-fixed")[1]
    assert [item["content"] for item in listing["items"]] == [
        "第一轮", GOOD_REPLY, "第二轮", GOOD_REPLY,
    ]


def test_api_post_degraded_is_200_not_500(http, session, monkeypatch) -> None:
    monkeypatch.setattr(assistant, "default_llm_client", lambda: FakeLLM(enabled=False))
    status, body = post_message(http, {"message": "西湖怎么玩?"})
    assert status == 200
    assert set(body) == CHAT_KEYS
    assert body["degraded"] is True and body["reason"] == "no_key" and body["reply"]


@pytest.mark.parametrize(
    "body,fragment",
    [
        (None, "缺少请求体"),
        ([], "JSON 对象"),
        ("text", "JSON 对象"),
        ({}, "message"),
        ({"message": ""}, "message"),
        ({"message": "   "}, "message"),
        ({"message": None}, "message"),
        ({"message": 123}, "必须是字符串"),
        ({"message": "问" * 2001}, "过长"),
        ({"message": "西湖?", "context": 123}, "context"),
    ],
)
def test_api_post_400_in_chinese(http, llm, body, fragment) -> None:
    status, payload = post_message(http, body)
    assert status == 400, body
    assert fragment in payload["detail"], (body, payload["detail"])
    assert llm.calls == []


def test_api_post_400_message_too_long_mentions_limit(http, llm) -> None:
    status, body = post_message(http, {"message": "问" * 2001})
    assert status == 400
    assert "2000" in body["detail"] and "过长" in body["detail"]


def test_api_post_missing_body_mentions_endpoint(http, llm) -> None:
    status, body = post_message(http, None)
    assert status == 400
    assert "/api/assistant/messages" in body["detail"]


# --------------------------------------------------------------------------- #
# API:GET / DELETE /api/assistant/messages
# --------------------------------------------------------------------------- #


def test_api_get_unknown_session_returns_empty_items(http, session, llm) -> None:
    status, body = http("GET", "/api/assistant/messages", query="session_key=chat-ghost")
    assert status == 200  # 不 404
    assert set(body) == GET_KEYS
    assert body["session_key"] == "chat-ghost"
    assert body["items"] == []
    assert "助手" in body["note"]


def test_api_get_requires_session_key(http, llm) -> None:
    for query in ("", "session_key=", "session_key=%20%20", "limit=5"):
        status, body = http("GET", "/api/assistant/messages", query=query)
        assert status == 400, query
        assert "session_key" in body["detail"]


def test_api_get_limit_validation_and_clamping(http, session, llm) -> None:
    seed_history(session, "chat-limit", count=210)
    for bad in ("abc", "0", "-3", "1.5"):
        status, body = http("GET", "/api/assistant/messages", query=f"session_key=chat-limit&limit={bad}")
        assert status == 400, bad
        assert "limit" in body["detail"]
    status, body = http("GET", "/api/assistant/messages", query="session_key=chat-limit&limit=9999")
    assert status == 200 and len(body["items"]) == 200
    status, body = http("GET", "/api/assistant/messages", query="session_key=chat-limit&limit=3")
    assert status == 200 and len(body["items"]) == 3
    status, body = http("GET", "/api/assistant/messages", query="session_key=chat-limit")
    assert status == 200 and len(body["items"]) == 50  # 默认 50


def test_api_delete_clears_messages_and_keeps_session(http, session, llm) -> None:
    post_message(http, {"session_key": "chat-del", "message": "西湖怎么玩?"})
    status, body = http("DELETE", "/api/assistant/messages", body={"session_key": "chat-del"})
    assert status == 200
    assert set(body) == DELETE_KEYS
    assert body["session_key"] == "chat-del" and body["deleted"] == 2
    assert counts(session)["planner_session"] == 1 and counts(session)["planner_message"] == 0
    again = http("DELETE", "/api/assistant/messages", body={"session_key": "chat-del"})
    assert again[0] == 200 and again[1]["deleted"] == 0
    unknown = http("DELETE", "/api/assistant/messages", body={"session_key": "chat-ghost"})
    assert unknown[0] == 200 and unknown[1]["deleted"] == 0


def test_api_delete_400_without_session_key(http, llm) -> None:
    for body in (None, [], {}, {"session_key": ""}, {"session_key": 123}):
        status, payload = http("DELETE", "/api/assistant/messages", body=body)
        assert status == 400, body
        assert "session_key" in payload["detail"] or "请求体" in payload["detail"]


# --------------------------------------------------------------------------- #
# 既有端点零回归:/api/planner/* 与 /api/trip-plans 的响应键逐键不变
# --------------------------------------------------------------------------- #


def test_planner_messages_response_keys_unchanged(http, session, planner_llm) -> None:
    status, body = http("POST", "/api/planner/messages", body={"message": "帮我排 2 天杭州"})
    assert status == 200
    assert set(body) == PLANNER_MESSAGE_KEYS
    assert set(body["itinerary"]) == PLANNER_ITINERARY_KEYS
    assert body["degraded"] is False and body["reason"] is None and body["turn_index"] == 1
    assert not body["session_key"].startswith("chat-")  # 行程会话仍是裸 uuid4().hex
    key = body["session_key"]

    status, listing = http("GET", "/api/planner/messages", query=f"session_key={key}")
    assert status == 200
    assert set(listing) == PLANNER_GET_KEYS
    assert set(listing["items"][0]) == ITEM_KEYS
    assert [item["role"] for item in listing["items"]] == ["user", "assistant"]

    status, deleted = http("DELETE", "/api/planner/messages", query=f"session_key={key}")
    assert status == 200
    assert set(deleted) == PLANNER_DELETE_KEYS
    assert deleted["deleted"] == 2


def test_planner_and_assistant_sessions_coexist_in_same_tables(http, session, llm, planner_llm) -> None:
    """助手复用 planner 的两张表,但两种会话互不干扰(各自的 key 各自的历史)。"""
    trip_key = http("POST", "/api/planner/messages", body={"message": "帮我排 2 天杭州"})[1]["session_key"]
    chat_key = post_message(http, {"message": "西湖怎么玩?"})[1]["session_key"]
    assert chat_key.startswith("chat-") and not trip_key.startswith("chat-")
    assert counts(session)["planner_session"] == 2
    trip_items = http("GET", "/api/planner/messages", query=f"session_key={trip_key}")[1]["items"]
    chat_items = http("GET", "/api/assistant/messages", query=f"session_key={chat_key}")[1]["items"]
    assert [item["content"] for item in trip_items][0] == "帮我排 2 天杭州"
    assert [item["content"] for item in chat_items][0] == "西湖怎么玩?"
    assert len(trip_items) == len(chat_items) == 2
    # 助手轮不会污染行程会话的历史(行程 prompt 里没有助手那句回答)
    post_message(http, {"session_key": chat_key, "message": "再来一轮"})
    http("POST", "/api/planner/messages", body={"session_key": trip_key, "message": "第二天换成古镇"})
    trip_prompt = planner_llm.calls[-1]["prompt"]
    assert GOOD_REPLY not in trip_prompt
    assert "第二天换成古镇" in trip_prompt


def test_trip_plans_response_keys_unchanged(http, session, llm) -> None:
    row = seed_collection(session)
    status, body = http(
        "POST", "/api/trip-plans",
        body={"name": "杭州一日", "place_collection_id": row.id, "nights": 2},
    )
    assert status == 200
    assert set(body) == TRIP_PLAN_POST_KEYS
    assert set(body["trip_plan"]) == TRIP_PLAN_DICT_KEYS
    assert set(body["quote"]) == QUOTE_KEYS
    assert body["quote"]["stay_nights"] == 2
    assert body["created"] is True and body["idempotent"] is False

    status, listing = http("GET", "/api/trip-plans")
    assert status == 200
    assert set(listing) == TRIP_PLAN_GET_KEYS
    assert set(listing["trip_plans"][0]) == TRIP_PLAN_DICT_KEYS
    assert listing["kind"] == "estimate"

    plan_id = body["trip_plan"]["id"]
    status, detail = http("GET", f"/api/trip-plans/{plan_id}")
    assert status == 200
    assert set(detail) == {"trip_plan", "quote", "nights", "count", "elapsed_s", "note"}

    status, deleted = http("DELETE", f"/api/trip-plans/{plan_id}")
    assert status == 200
    assert set(deleted) == {"deleted", "id", "count", "note"}
