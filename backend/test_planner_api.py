"""TASK-10b AI 行程规划 **API 层**单测:``/api/planner/messages``(POST/GET/DELETE)+ ``/api/planner/save``。

口径(契约 docs/TASK-10-CONTRACT.md §0 + §3 TASK-10b):

* 响应形状**键名逐字** —— POST 是 ``plan_turn`` 的原样透出
  ``{"session_key","title","reply","itinerary","degraded","reason","turn_index","generated_at"}``;
  GET 是 ``{"session_key","title","items":[{"role","content","payload","created_at"}]}``;
  DELETE 是 ``{"session_key","deleted"}``;save 是 ``{"trip_plan","matched","unmatched",…}``;
* **降级四态不 500**(no_key / timeout / error / parse_error)—— HTTP 200 + ``degraded=true`` +
  中文 ``reply``,消息照样落库可回放;只有"参数不对"才 **400 中文**;
* 素材清单两条路径:给了 ``collection_ids`` 只带这几条,没给带**全部收藏**(prompt 里的
  「未点名一律不得纳入」硬约束由 ``planner.SYSTEM_PROMPT`` 保证,这里断言素材块的内容);
* ``save`` 的名字匹配三级(精确 → 库内名包含它 → 它包含库内名,多命中取 ``id`` 最小)与
  **幂等**(同名重复保存不产生新 ``Place`` / ``Collection`` / ``TripPlan`` 行);
* ``/api/trip-plans`` 与 ``/api/collections`` 的**既有响应键逐键零变化**(10b 只新增路由)。

全套**纯 mock 不触网**(``no_network`` autouse 把 ``requests.Session.request`` 换成抛错;
LLM 用替身顶掉 ``planner.default_llm_client``),HTTP 链走手拼 ASGI scope(仓库没装 httpx)。

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

from app.api import planner as planner_api  # noqa: E402
from app.main import app  # noqa: E402
from data_sources import DataSourceError, TransientDataSourceError  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db import models  # noqa: E402
from db import repository as repo  # noqa: E402
from db.base import get_session  # noqa: E402
from services import planner  # noqa: E402
from services import trips as trip_service  # noqa: E402

# --------------------------------------------------------------------------- #
# 逐字键集(前端只读这些键,多一个少一个都算回归)
# --------------------------------------------------------------------------- #

MESSAGE_KEYS = {
    "session_key", "title", "reply", "itinerary",
    "degraded", "reason", "turn_index", "generated_at",
}
ITEM_KEYS = {"role", "content", "payload", "created_at"}
GET_KEYS = {"session_key", "title", "items"}
DELETE_KEYS = {"session_key", "deleted"}
SAVE_KEYS = {"trip_plan", "matched", "unmatched"}
MATCHED_KEYS = {"name", "place_id", "collection_id"}
ITINERARY_KEYS = {"days", "days_count", "summary", "unused_collections"}

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
COLLECTION_POST_KEYS = {
    "collection", "created", "idempotent", "count", "counts_by_kind", "elapsed_s", "note",
}
COLLECTION_GET_KEYS = {
    "collections", "count", "kind", "cat_id", "total", "counts_by_kind",
    "cats", "kinds", "modes", "elapsed_s", "note",
}
COLLECTION_DICT_KEYS = {
    "id", "kind", "mode", "ref_key", "name", "osm_type", "osm_id",
    "from_lat", "from_lng", "from_name", "to_lat", "to_lng", "to_name",
    "summary", "cat_id", "cat_name", "created_at", "updated_at",
}

NO_MATCH_DETAIL = "行程里的地点都不在库内,先在目的地列表里搜到它们再试"

GOOD_PAYLOAD = {
    "days": [
        {
            "day": 1,
            "base": "杭州",
            "stops": [
                {"name": "西湖", "reason": "点名收藏,湖边步道适合慢走", "collection_id": None},
                {"name": "浙江省博物馆", "reason": "离西湖步行 10 分钟", "collection_id": None},
            ],
            "tip": "上午人少",
        }
    ],
    "days_count": 1,
    "summary": "一天都在西湖西侧",
    "unused_collections": [],
}
GOOD_JSON = json.dumps(GOOD_PAYLOAD, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 替身 / 样本数据
# --------------------------------------------------------------------------- #


class FakeLLM:
    """``intro.LLMClient`` 替身:记下每次 chat 的入参,按剧本返回文本或抛异常。"""

    def __init__(
        self,
        reply: str = GOOD_JSON,
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


def seed_place(
    session,
    *,
    name: str,
    osm_id: int,
    lat: float = 30.2501234,
    lng: float = 120.1501234,
    city: str = "杭州",
    band: str = "0_25",
    category: str = "自然",
) -> Any:
    """往库内塞一个目的地(走既有 :func:`db.repository.upsert_places`,坐标自动定点 7 位)。"""
    repo.upsert_places(
        session,
        origin_city=city,
        band=band,
        items=[
            {
                "osm_type": models.AMAP_OSM_TYPE,
                "osm_id": osm_id,
                "name": name,
                "lat": lat,
                "lng": lng,
                "category": category,
                "tags": {"amap_id": f"B{osm_id:09d}"},
            }
        ],
    )
    session.commit()
    return session.scalar(select(models.Place).where(models.Place.name == name))


def seed_collection(
    session,
    *,
    kind: str = "place",
    name: str,
    osm_type: str = "way",
    osm_id: int,
    mode: Optional[str] = None,
    summary: Optional[dict[str, Any]] = None,
    cat_id: Any = None,
) -> Any:
    """一条收藏(素材清单 / 点名引用都用它)。"""
    row, _ = repo.upsert_collection(
        session,
        kind=kind,
        mode=mode,
        name=name,
        osm_type=osm_type,
        osm_id=osm_id,
        from_lat=31.2304 if kind == "route" else None,
        from_lng=121.4737 if kind == "route" else None,
        from_name="上海" if kind == "route" else None,
        to_lat=30.25,
        to_lng=120.15,
        to_name=name,
        summary=summary,
        cat_id=cat_id,
    )
    session.commit()
    return row


def counts(session) -> dict[str, int]:
    """三张表的行数(幂等断言用:重复保存不许长行)。"""
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
def session(tmp_path):
    """每个用例一个独立的临时 SQLite 库(**绝不碰 backend/data/where2go.db**)。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'planner_api_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


@pytest.fixture()
def llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """把 planner 的 LLM 入口换成替身(默认回一份合法行程 JSON)。"""
    fake = FakeLLM()
    monkeypatch.setattr(planner, "default_llm_client", lambda: fake)
    return fake


@pytest.fixture()
def http(session) -> Callable[..., tuple[int, Any]]:
    """完整 HTTP 链客户端:``get_session`` 依赖换成用例里的同一个临时库会话。"""
    app.dependency_overrides[get_session] = lambda: session
    yield lambda method, path, **kwargs: http_request(method, path, **kwargs)
    app.dependency_overrides.clear()


def post_message(http, body: Optional[dict[str, Any]]) -> tuple[int, Any]:
    return http("POST", "/api/planner/messages", body=body)


def save(http, body: Optional[dict[str, Any]]) -> tuple[int, Any]:
    return http("POST", "/api/planner/save", body=body)


# --------------------------------------------------------------------------- #
# 路由挂载(app/main.py 的 include_router)
# --------------------------------------------------------------------------- #


def test_planner_routes_are_mounted_under_api_prefix() -> None:
    """``app/main.py`` 的 ``include_router(planner.router, prefix="/api")`` 真的挂上了。"""
    paths = app.openapi()["paths"]
    assert set(paths["/api/planner/messages"]) >= {"post", "get", "delete"}
    assert set(paths["/api/planner/save"]) >= {"post"}


# --------------------------------------------------------------------------- #
# POST /api/planner/messages:参数校验(一律 400 中文,不 422 英文)
# --------------------------------------------------------------------------- #


def test_post_message_400_without_body(http, llm) -> None:
    status, body = post_message(http, None)
    assert status == 400
    assert "缺少请求体" in body["detail"] and "/api/planner/messages" in body["detail"]
    assert llm.calls == []


def test_post_message_400_when_body_is_not_object(http, llm) -> None:
    status, body = post_message(http, ["3 天"])
    assert status == 400
    assert "请求体必须是 JSON 对象" in body["detail"] and "list" in body["detail"]


def test_post_message_400_when_message_blank(http, llm) -> None:
    for blank in (None, "", "   "):
        status, body = post_message(http, {"session_key": "s-1", "message": blank})
        assert status == 400, blank
        assert "message" in body["detail"] and "不能为空" in body["detail"]
    assert llm.calls == []


def test_post_message_400_when_message_is_not_string(http, llm) -> None:
    status, body = post_message(http, {"message": 123})
    assert status == 400
    assert "message 必须是字符串" in body["detail"] and "int" in body["detail"]


def test_post_message_400_when_message_too_long_and_writes_nothing(http, session, llm) -> None:
    before = counts(session)
    status, body = post_message(http, {"message": "字" * (planner.MAX_MESSAGE_LEN + 1)})
    assert status == 400
    assert str(planner.MAX_MESSAGE_LEN) in body["detail"] and "过长" in body["detail"]
    assert llm.calls == []
    assert counts(session) == before  # 校验失败不落库


def test_post_message_400_when_collection_ids_not_array(http, llm) -> None:
    status, body = post_message(http, {"message": "3 天", "collection_ids": "3"})
    assert status == 400
    assert "collection_ids 必须是整数数组" in body["detail"]


def test_post_message_400_when_collection_ids_has_bad_item(http, llm) -> None:
    for bad in ([0], [-1], ["abc"], [None]):
        status, body = post_message(http, {"message": "3 天", "collection_ids": bad})
        assert status == 400, bad
        assert "collection_ids" in body["detail"]


# --------------------------------------------------------------------------- #
# POST /api/planner/messages:响应形状 / 会话
# --------------------------------------------------------------------------- #


def test_post_message_response_keys_are_verbatim(http, llm) -> None:
    status, body = post_message(http, {"session_key": "s-1", "message": "3 天,亲子,不要太累"})
    assert status == 200
    assert set(body) == MESSAGE_KEYS
    assert body["session_key"] == "s-1"
    assert body["degraded"] is False and body["reason"] is None
    assert body["turn_index"] == 1
    assert set(body["itinerary"]) == ITINERARY_KEYS
    assert isinstance(body["generated_at"], str) and body["generated_at"]


def test_post_message_generates_session_key_when_blank(http, session, llm) -> None:
    status, body = post_message(http, {"message": "3 天"})
    assert status == 200
    key = body["session_key"]
    assert len(key) == 32 and all(char in "0123456789abcdef" for char in key)
    row = session.scalar(select(models.PlannerSession).where(models.PlannerSession.session_key == key))
    assert row is not None


def test_post_message_reuses_session_key_and_increments_turn(http, session, llm) -> None:
    first = post_message(http, {"session_key": "s-1", "message": "3 天 亲子"})[1]
    second = post_message(http, {"session_key": "s-1", "message": "第二天换成古镇"})[1]
    assert (first["turn_index"], second["turn_index"]) == (1, 2)
    assert first["title"] == second["title"] == "3 天 亲子"
    assert len(list(session.scalars(select(models.PlannerSession)))) == 1
    assert counts(session)["planner_message"] == 4


def test_post_message_stores_nights_in_payload_but_not_in_prompt(http, session, llm) -> None:
    status, _ = post_message(http, {"session_key": "s-1", "message": "3 天", "nights": 2})
    assert status == 200
    user_row = session.scalars(
        select(models.PlannerMessage).where(models.PlannerMessage.role == "user")
    ).first()
    assert user_row.payload["nights"] == 2
    assert "晚" not in llm.calls[0]["prompt"]  # 阶段 A 不排住宿


def test_post_message_passes_planner_budget_per_call(http, llm) -> None:
    post_message(http, {"message": "3 天"})
    call = llm.calls[0]
    assert call["system"] == planner.SYSTEM_PROMPT
    assert call["max_tokens"] == planner.PLANNER_MAX_TOKENS == 1200
    assert call["timeout"] == planner.PLANNER_TIMEOUT_S == 180


# --------------------------------------------------------------------------- #
# 素材清单:点名 vs 取全部
# --------------------------------------------------------------------------- #


def test_post_message_collection_ids_limits_briefs_to_mentioned(http, session, llm) -> None:
    keep = seed_collection(session, name="西湖", osm_id=-1)
    skip = seed_collection(session, name="千岛湖", osm_id=-2)
    status, body = post_message(
        http, {"message": "用西湖那条", "collection_ids": [keep.id]}
    )
    assert status == 200 and body["degraded"] is False
    prompt = llm.calls[0]["prompt"]
    assert f"#{keep.id}" in prompt and "「西湖」" in prompt
    assert "「千岛湖」" not in prompt and f"#{skip.id}" not in prompt
    assert "未点名一律不得纳入" in prompt  # 硬约束照旧写死


def test_post_message_without_collection_ids_uses_all_collections(http, session, llm) -> None:
    first = seed_collection(session, name="西湖", osm_id=-1)
    second = seed_collection(
        session, kind="route", name="上海 → 西湖", osm_id=-2, mode="driving",
        summary={"duration_min": 120, "cost_cny": 320, "distance_km": 180.5},
    )
    status, _ = post_message(http, {"message": "3 天"})
    assert status == 200
    prompt = llm.calls[0]["prompt"]
    assert f"#{first.id}" in prompt and f"#{second.id}" in prompt
    assert "「西湖」" in prompt and "「上海 → 西湖」" in prompt
    assert "约 120 分钟" in prompt and "180.5 公里" in prompt
    assert "320" not in prompt  # 阶段 A 不出报价:费用不进 prompt


def test_post_message_empty_collection_ids_means_no_briefs(http, session, llm) -> None:
    seed_collection(session, name="西湖", osm_id=-1)
    status, _ = post_message(http, {"message": "随便排", "collection_ids": []})
    assert status == 200
    assert "「西湖」" not in llm.calls[0]["prompt"]
    assert "本轮没有收藏素材" in llm.calls[0]["prompt"]


def test_post_message_unknown_collection_id_is_skipped_not_400(http, session, llm) -> None:
    seed_collection(session, name="西湖", osm_id=-1)
    status, body = post_message(http, {"message": "3 天", "collection_ids": [99999]})
    assert status == 200 and body["degraded"] is False
    assert "「西湖」" not in llm.calls[0]["prompt"]


def test_briefs_shape_summary_truncated_and_price_free(session) -> None:
    long_summary = "景" * 300
    row = seed_collection(
        session, name="西湖", osm_id=-1,
        summary={"duration_min": 90, "cost_cny": 88, "price_estimate": "约¥250-450/晚",
                 "kind": "real", "note": long_summary},
    )
    briefs = planner_api._briefs_of(session, None)
    assert briefs == [
        {
            "id": row.id,
            "kind": "place",
            "title": "西湖",
            "summary": "目的地:西湖 · 约 90 分钟",
        }
    ]
    assert set(briefs[0]) == {"id", "kind", "title", "summary"}
    assert len(briefs[0]["summary"]) <= planner.BRIEF_SUMMARY_LEN == 120
    assert "88" not in briefs[0]["summary"] and "250" not in briefs[0]["summary"]


def test_briefs_summary_truncates_to_120_chars(session) -> None:
    """素材摘要**必须**截到 :data:`services.planner.BRIEF_SUMMARY_LEN`(120 字),长名字不许撑爆 prompt。"""
    seed_collection(session, name="西湖" + "景" * 400, osm_id=-1)
    briefs = planner_api._briefs_of(session, None)
    assert len(briefs[0]["summary"]) == planner.BRIEF_SUMMARY_LEN == 120
    assert briefs[0]["title"].startswith("西湖")


# --------------------------------------------------------------------------- #
# 降级四态:HTTP 200 + degraded/reason + 中文 reply(不 500、不静默)
# --------------------------------------------------------------------------- #


def test_post_message_degraded_no_key(http, session, llm) -> None:
    llm.enabled = False
    status, body = post_message(http, {"session_key": "s-1", "message": "3 天"})
    assert status == 200
    assert body["degraded"] is True and body["reason"] == "no_key"
    assert body["itinerary"] is None
    assert "no_key" in body["reply"] and "WHERE2GO_LLM_API_KEY" in body["reply"]
    assert llm.calls == []


def test_post_message_degraded_timeout(http, llm) -> None:
    llm.error = TransientDataSourceError("LLM", "请求超时(>180s):https://x")
    status, body = post_message(http, {"message": "3 天"})
    assert status == 200
    assert body["degraded"] is True and body["reason"] == "timeout"
    assert body["itinerary"] is None and "timeout" in body["reply"]


def test_post_message_degraded_error(http, llm) -> None:
    llm.error = DataSourceError("LLM", "HTTP 状态码 500:忙")
    status, body = post_message(http, {"message": "3 天"})
    assert status == 200
    assert body["degraded"] is True and body["reason"] == "error"
    assert "error" in body["reply"] and "500" in body["reply"]


def test_post_message_degraded_parse_error(http, llm) -> None:
    llm.reply = "抱歉,我不会排行程。"
    status, body = post_message(http, {"message": "3 天"})
    assert status == 200
    assert body["degraded"] is True and body["reason"] == "parse_error"
    assert body["itinerary"] is None
    assert "parse_error" in body["reply"] and "抱歉" in body["reply"]  # 原文片段附在后面


def test_degraded_turn_is_persisted_and_replayable(http, llm) -> None:
    llm.error = RuntimeError("boom")
    post_message(http, {"session_key": "s-1", "message": "3 天"})
    status, body = http("GET", "/api/planner/messages", query="session_key=s-1")
    assert status == 200
    assert [item["role"] for item in body["items"]] == ["user", "assistant"]
    assistant = body["items"][1]
    assert assistant["payload"]["degraded"] is True
    assert assistant["payload"]["reason"] == "error"
    assert assistant["payload"]["itinerary"] is None


# --------------------------------------------------------------------------- #
# GET /api/planner/messages
# --------------------------------------------------------------------------- #


def test_get_messages_item_keys_and_chronological_order(http, llm) -> None:
    post_message(http, {"session_key": "s-1", "message": "3 天 亲子"})
    status, body = http("GET", "/api/planner/messages", query="session_key=s-1")
    assert status == 200
    assert set(body) == GET_KEYS
    assert body["session_key"] == "s-1" and body["title"] == "3 天 亲子"
    assert [item["role"] for item in body["items"]] == ["user", "assistant"]
    for item in body["items"]:
        assert set(item) == ITEM_KEYS
        assert isinstance(item["created_at"], str) and item["created_at"]
    assert body["items"][1]["payload"]["itinerary"]["days_count"] == 1


def test_get_messages_unknown_session_returns_empty_items(http, llm) -> None:
    status, body = http("GET", "/api/planner/messages", query="session_key=nope")
    assert status == 200  # 不 404
    assert body == {"session_key": "nope", "title": "", "items": []}


def test_get_messages_400_without_session_key(http, llm) -> None:
    for query in ("", "session_key=", "session_key=%20%20"):
        status, body = http("GET", "/api/planner/messages", query=query)
        assert status == 400, query
        assert "session_key" in body["detail"]


def test_get_messages_limit_defaults_to_50_and_caps_at_200(http, session, llm, monkeypatch) -> None:
    post_message(http, {"session_key": "s-1", "message": "3 天"})
    seen: list[int] = []
    real = planner.list_messages

    def spy(sess, *, session_key, limit=planner.LIST_LIMIT_DEFAULT):
        seen.append(limit)
        return real(sess, session_key=session_key, limit=limit)

    monkeypatch.setattr(planner, "list_messages", spy)
    assert http("GET", "/api/planner/messages", query="session_key=s-1")[0] == 200
    assert http("GET", "/api/planner/messages", query="session_key=s-1&limit=500")[0] == 200
    assert http("GET", "/api/planner/messages", query="session_key=s-1&limit=1")[0] == 200
    assert seen == [planner.LIST_LIMIT_DEFAULT, planner.LIST_LIMIT_MAX, 1]
    assert (planner.LIST_LIMIT_DEFAULT, planner.LIST_LIMIT_MAX) == (50, 200)


def test_get_messages_limit_one_returns_latest_message_only(http, llm) -> None:
    post_message(http, {"session_key": "s-1", "message": "3 天"})
    status, body = http("GET", "/api/planner/messages", query="session_key=s-1&limit=1")
    assert status == 200
    assert [item["role"] for item in body["items"]] == ["assistant"]


def test_get_messages_400_when_limit_is_not_positive_int(http, llm) -> None:
    post_message(http, {"session_key": "s-1", "message": "3 天"})
    for bad in ("0", "-3", "abc"):
        status, body = http("GET", "/api/planner/messages", query=f"session_key=s-1&limit={bad}")
        assert status == 400, bad
        assert "limit" in body["detail"]


# --------------------------------------------------------------------------- #
# DELETE /api/planner/messages
# --------------------------------------------------------------------------- #


def test_delete_messages_clears_history_but_keeps_session(http, session, llm) -> None:
    post_message(http, {"session_key": "s-1", "message": "3 天"})
    status, body = http("DELETE", "/api/planner/messages", query="session_key=s-1")
    assert status == 200
    assert set(body) == DELETE_KEYS
    assert body == {"session_key": "s-1", "deleted": 2}
    assert session.scalar(
        select(models.PlannerSession).where(models.PlannerSession.session_key == "s-1")
    ) is not None  # 会话行保留,key 继续可用
    assert http("GET", "/api/planner/messages", query="session_key=s-1")[1]["items"] == []


def test_delete_messages_unknown_key_returns_zero_not_404(http, llm) -> None:
    status, body = http("DELETE", "/api/planner/messages", query="session_key=ghost")
    assert status == 200
    assert body == {"session_key": "ghost", "deleted": 0}


def test_delete_messages_400_without_session_key(http, llm) -> None:
    status, body = http("DELETE", "/api/planner/messages", query="")
    assert status == 400
    assert "session_key" in body["detail"]


def test_delete_messages_is_idempotent(http, llm) -> None:
    post_message(http, {"session_key": "s-1", "message": "3 天"})
    assert http("DELETE", "/api/planner/messages", query="session_key=s-1")[1]["deleted"] == 2
    assert http("DELETE", "/api/planner/messages", query="session_key=s-1")[1]["deleted"] == 0


# --------------------------------------------------------------------------- #
# POST /api/planner/save:名字匹配(精确 / 包含 / 反向包含 / 多命中 / 不命中)
# --------------------------------------------------------------------------- #


def test_save_matches_exact_name_and_creates_place_collection(http, session, llm) -> None:
    place = seed_place(session, name="西湖", osm_id=1001)
    status, body = save(http, {"name": "杭州一日", "stops": [{"name": "西湖"}]})
    assert status == 200
    assert SAVE_KEYS <= set(body)
    assert body["unmatched"] == []
    assert body["matched"] == [{"name": "西湖", "place_id": place.id, "collection_id": body["matched"][0]["collection_id"]}]
    assert set(body["matched"][0]) == MATCHED_KEYS
    row = repo.get_collection(session, collection_id=body["matched"][0]["collection_id"])
    assert row.kind == models.KIND_PLACE and row.mode == models.NO_MODE == ""
    assert row.ref_key == f"place:{models.AMAP_OSM_TYPE}/{place.osm_id}"
    assert row.osm_type == place.osm_type and row.osm_id == place.osm_id
    assert row.to_lat == round(place.lat, models.COORD_PRECISION)
    assert row.to_lng == round(place.lng, models.COORD_PRECISION)
    assert body["trip_plan"]["place_collection_id"] == row.id
    assert body["created"] is True and body["idempotent"] is False


def test_save_prefers_exact_match_over_contains(http, session, llm) -> None:
    exact = seed_place(session, name="西湖", osm_id=1001)
    seed_place(session, name="西湖文化广场", osm_id=1002)
    status, body = save(http, {"name": "杭州一日", "stops": [{"name": "西湖"}]})
    assert status == 200
    assert body["matched"][0]["place_id"] == exact.id


def test_save_contains_match_picks_smallest_id(http, session, llm) -> None:
    first = seed_place(session, name="西湖文化广场", osm_id=1001)
    seed_place(session, name="西湖博物馆", osm_id=1002)
    status, body = save(http, {"name": "杭州一日", "stops": [{"name": "西湖"}]})
    assert status == 200
    assert body["matched"][0]["place_id"] == first.id  # 多命中取入库最前一条
    assert first.id < session.scalar(
        select(models.Place.id).where(models.Place.name == "西湖博物馆")
    )


def test_save_reverse_contains_match(http, session, llm) -> None:
    place = seed_place(session, name="西湖", osm_id=1001)
    status, body = save(http, {"name": "杭州一日", "stops": [{"name": "杭州西湖风景区"}]})
    assert status == 200
    assert body["matched"][0]["place_id"] == place.id
    assert body["matched"][0]["name"] == "杭州西湖风景区"  # 站名用请求里的原文
    assert body["unmatched"] == []


def test_save_lists_unmatched_names_and_still_saves_the_rest(http, session, llm) -> None:
    place = seed_place(session, name="西湖", osm_id=1001)
    status, body = save(
        http,
        {"name": "杭州一日", "stops": [{"name": "西湖"}, {"name": "不存在的地方"}]},
    )
    assert status == 200
    assert body["unmatched"] == ["不存在的地方"]
    assert [item["place_id"] for item in body["matched"]] == [place.id]
    assert body["trip_plan"]["place_collection_id"] == body["matched"][0]["collection_id"]


def test_save_400_when_nothing_matched(http, session, llm) -> None:
    seed_place(session, name="西湖", osm_id=1001)
    before = counts(session)
    status, body = save(http, {"name": "空方案", "stops": [{"name": "火星基地"}]})
    assert status == 400
    assert body["detail"] == NO_MATCH_DETAIL
    assert counts(session) == before  # 一个都没匹配上 → 不留半拉子数据


def test_save_dedupes_repeated_stop_names(http, session, llm) -> None:
    place = seed_place(session, name="西湖", osm_id=1001)
    status, body = save(
        http, {"name": "杭州一日", "stops": [{"name": "西湖"}, {"name": "西湖"}]}
    )
    assert status == 200
    assert [item["collection_id"] for item in body["matched"]] == [body["matched"][0]["collection_id"]] * 2
    assert counts(session)["collection"] == 1
    row = session.scalar(
        select(models.Collection).where(models.Collection.osm_id == place.osm_id)
    )
    assert row.to_name == "西湖"


def test_save_explicit_collection_id_skips_name_match(http, session, llm) -> None:
    place = seed_place(session, name="西湖", osm_id=1001)
    row = seed_collection(
        session, name="西湖", osm_type=models.AMAP_OSM_TYPE, osm_id=place.osm_id
    )
    before = counts(session)
    status, body = save(
        http,
        {"name": "杭州一日", "stops": [{"name": "AI 写的别名", "collection_id": row.id}]},
    )
    assert status == 200
    assert body["matched"] == [{"name": "AI 写的别名", "place_id": place.id, "collection_id": row.id}]
    assert body["unmatched"] == []
    after = counts(session)
    assert after["place"] == before["place"]          # 点名收藏:不新建地点
    assert after["collection"] == before["collection"]  # 也不新建收藏
    assert after["trip_plan"] == before["trip_plan"] + 1  # 只多一份方案
    assert body["trip_plan"]["place_collection_id"] == row.id


def test_save_400_when_explicit_collection_is_missing(http, session, llm) -> None:
    seed_place(session, name="西湖", osm_id=1001)
    status, body = save(
        http, {"name": "杭州一日", "stops": [{"name": "西湖", "collection_id": 999}]}
    )
    assert status == 400
    assert "未知收藏" in body["detail"] and "999" in body["detail"]


# --------------------------------------------------------------------------- #
# POST /api/planner/save:幂等 + legs/stay/nights
# --------------------------------------------------------------------------- #


def test_save_is_idempotent_same_name_creates_no_new_rows(http, session, llm) -> None:
    place = seed_place(session, name="西湖", osm_id=1001)
    payload = {"name": "杭州一日", "stops": [{"name": "西湖"}]}
    first_status, first = save(http, payload)
    after_first = counts(session)
    second_status, second = save(http, payload)
    assert (first_status, second_status) == (200, 200)
    assert first["created"] is True and second["created"] is False
    assert second["idempotent"] is True
    assert first["trip_plan"]["id"] == second["trip_plan"]["id"]
    assert first["trip_plan"]["created_at"] == second["trip_plan"]["created_at"]
    assert counts(session) == after_first
    assert after_first == {**after_first, "place": 1, "collection": 1, "trip_plan": 1}
    assert second["matched"][0]["collection_id"] == first["matched"][0]["collection_id"]
    assert session.scalar(select(models.Place)).id == place.id


def test_save_uses_legs_stay_and_nights_in_quote(http, session, llm) -> None:
    seed_place(session, name="西湖", osm_id=1001)
    leg = seed_collection(
        session, kind="route", name="上海 → 西湖", osm_id=-7, mode="driving",
        summary={"duration_min": 120, "cost_cny": 88, "distance_km": 180.0},
    )
    stay = seed_collection(
        session, name="西湖民宿", osm_id=-8, summary={"price_estimate": "约¥250-450/晚"}
    )
    status, body = save(
        http,
        {
            "name": "杭州两日",
            "stops": [{"name": "西湖"}],
            "legs": [leg.id],
            "stay": [stay.id],
            "nights": 2,
        },
    )
    assert status == 200
    plan = body["trip_plan"]
    assert plan["route_collection_ids"] == [leg.id]
    assert plan["stay_collection_ids"] == [stay.id]
    assert plan["counts"] == {"place": 1, "routes": 1, "stays": 1}
    quote = plan["quote"]
    assert set(quote) == QUOTE_KEYS
    assert quote["transport_cny"] == 88.0
    assert quote["stay_nights"] == 2 == body["nights"]
    assert quote["total_cny_low"] == 588.0 and quote["total_cny_high"] == 988.0
    assert quote["kind"] == trip_service.QUOTE_KIND == "estimate"
    assert quote["note"] == trip_service.QUOTE_NOTE
    assert "估算" in body["note"]


def test_save_nights_defaults_to_one(http, session, llm) -> None:
    """没给 ``nights`` 按既有 :data:`services.trips.DEFAULT_NIGHTS`(=1)算报价。"""
    seed_place(session, name="西湖", osm_id=1001)
    status, body = save(http, {"name": "杭州一日", "stops": [{"name": "西湖"}]})
    assert status == 200
    assert body["nights"] == trip_service.DEFAULT_NIGHTS == 1
    assert body["trip_plan"]["quote"]["stay_nights"] == 1


def test_save_400_when_stops_missing_empty_or_malformed(http, llm) -> None:
    cases: list[tuple[dict[str, Any], str]] = [
        ({"name": "杭州一日"}, "stops"),
        ({"name": "杭州一日", "stops": []}, "至少"),
        ({"name": "杭州一日", "stops": "西湖"}, "数组"),
        ({"name": "杭州一日", "stops": ["西湖"]}, "JSON 对象"),
        ({"name": "杭州一日", "stops": [{"collection_id": 1}]}, "name"),
        ({"name": "杭州一日", "stops": [{"name": "西湖", "collection_id": "x"}]}, "collection_id"),
    ]
    for payload, fragment in cases:
        status, body = save(http, payload)
        assert status == 400, payload
        assert fragment in body["detail"], (payload, body["detail"])


def test_save_400_when_name_is_blank(http, session, llm) -> None:
    seed_place(session, name="西湖", osm_id=1001)
    for name in (None, "", "   "):
        status, body = save(http, {"name": name, "stops": [{"name": "西湖"}]})
        assert status == 400, name
        assert "name" in body["detail"]


def test_save_400_when_nights_out_of_range(http, llm) -> None:
    for nights in (0, 99, "abc"):
        status, body = save(http, {"name": "杭州一日", "stops": [{"name": "西湖"}], "nights": nights})
        assert status == 400, nights
        assert "nights" in body["detail"]


def test_save_400_when_legs_or_stay_not_int_array(http, session, llm) -> None:
    seed_place(session, name="西湖", osm_id=1001)
    for payload in (
        {"name": "杭州一日", "stops": [{"name": "西湖"}], "legs": "3"},
        {"name": "杭州一日", "stops": [{"name": "西湖"}], "legs": [0]},
        {"name": "杭州一日", "stops": [{"name": "西湖"}], "stay": {"id": 1}},
    ):
        status, body = save(http, payload)
        assert status == 400, payload
        assert "整数" in body["detail"]


def test_save_400_without_body(http, llm) -> None:
    status, body = save(http, None)
    assert status == 400
    assert "缺少请求体" in body["detail"] and "/api/planner/save" in body["detail"]


# --------------------------------------------------------------------------- #
# 既有端点零回归:/api/trip-plans 与 /api/collections 的响应键逐键不变
# --------------------------------------------------------------------------- #


def test_trip_plans_response_keys_unchanged(http, session, llm) -> None:
    row = seed_collection(session, name="西沙湿地", osm_id=-1234)
    status, body = http(
        "POST", "/api/trip-plans",
        body={"name": "崇明两日游", "place_collection_id": row.id, "nights": 2},
    )
    assert status == 200
    assert set(body) == TRIP_PLAN_POST_KEYS
    assert set(body["trip_plan"]) == TRIP_PLAN_DICT_KEYS
    assert set(body["quote"]) == QUOTE_KEYS
    assert body["quote"]["stay_nights"] == 2

    status, listing = http("GET", "/api/trip-plans")
    assert status == 200
    assert set(listing) == TRIP_PLAN_GET_KEYS
    assert set(listing["trip_plans"][0]) == TRIP_PLAN_DICT_KEYS
    assert listing["kind"] == "estimate"

    status, detail = http("GET", f"/api/trip-plans/{body['trip_plan']['id']}")
    assert status == 200
    assert set(detail) == {
        "trip_plan", "quote", "nights", "count", "elapsed_s", "note",
    }

    status, deleted = http("DELETE", f"/api/trip-plans/{body['trip_plan']['id']}")
    assert status == 200
    assert set(deleted) == {"deleted", "id", "count", "note"}


def test_collections_response_keys_unchanged(http, session, llm) -> None:
    status, body = http(
        "POST", "/api/collections",
        body={
            "kind": "place", "name": "西湖", "osm_type": "way", "osm_id": -1234,
            "to_lat": 30.25, "to_lng": 120.15, "to_name": "西湖",
        },
    )
    assert status == 200
    assert set(body) == COLLECTION_POST_KEYS
    assert set(body["collection"]) == COLLECTION_DICT_KEYS
    assert body["collection"]["mode"] == ""

    status, listing = http("GET", "/api/collections")
    assert status == 200
    assert set(listing) == COLLECTION_GET_KEYS
    assert set(listing["collections"][0]) == COLLECTION_DICT_KEYS
    assert listing["kinds"] == ["route", "place"] or set(listing["kinds"]) == {"route", "place"}


def test_saved_planner_place_shows_up_in_collections_with_same_keys(http, session, llm) -> None:
    """``save`` 建的收藏走的是既有 upsert 口径 → 收藏列表里键名/口径完全一致。"""
    place = seed_place(session, name="西湖", osm_id=1001)
    status, body = save(http, {"name": "杭州一日", "stops": [{"name": "西湖"}]})
    assert status == 200
    listed = http("GET", "/api/collections", query="kind=place")[1]["collections"]
    assert len(listed) == 1
    item = listed[0]
    assert set(item) == COLLECTION_DICT_KEYS
    assert item["id"] == body["matched"][0]["collection_id"]
    assert item["kind"] == "place" and item["mode"] == ""
    assert item["ref_key"] == f"place:{place.osm_type}/{place.osm_id}"
    assert item["summary"]["mode"] == ""
    assert set(item["summary"]) >= {"mode", "duration_min", "cost_cny", "distance_km"}
    # 同一地点再存一次:收藏列表仍然只有一行(幂等)
    save(http, {"name": "杭州一日", "stops": [{"name": "西湖"}]})
    assert len(http("GET", "/api/collections", query="kind=place")[1]["collections"]) == 1
