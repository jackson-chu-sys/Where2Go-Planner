"""TASK-10a AI 行程规划服务层的单测:多轮编排 / 会话表 / 结构化解析 / 降级四态。

口径(契约 docs/TASK-10-CONTRACT.md §0 + §3 TASK-10a):

* 返回形状**键名逐字** ``{"session_key","title","reply","itinerary","degraded","reason",
  "turn_index","generated_at"}``;``itinerary`` 形状 ``{"days":[{day,base,stops,tip}],
  "days_count","summary","unused_collections"}``,stop 是 ``{name,reason,collection_id}``;
* **降级四态一律不抛**(no_key / timeout / error / parse_error),``reply`` 是含原因码的中文兜底;
* ``PLANNER_MAX_TOKENS=1200`` / ``PLANNER_TIMEOUT_S=180`` **必须真的按调用传给 LLM**
  (沿用 ``intro.LLMClient`` 的 120/20s 默认会静默截断 → parse_error,所以注入替身断言 kwargs);
* 历史只送最近 :data:`services.planner.HISTORY_LIMIT`(12)条;prompt 里写死两条硬约束
  (「未点名一律不得纳入」「不排交通、不排住宿」);
* ``parse_itinerary`` 是**纯函数**,四种脏输入(围栏 / 前后解释文字 / 非 JSON / days 缺字段)
  都只返回 ``(None, 原文前 200 字)``,不抛。

全套**纯 mock 不触网**(``no_network`` autouse 把 ``requests.Session.request`` 换成抛错)。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

import pytest
import requests
from sqlalchemy import select

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from data_sources import DataSourceError, TransientDataSourceError  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db import models  # noqa: E402
from services import planner  # noqa: E402
from services.intro import DEFAULT_MAX_TOKENS, DEFAULT_TIMEOUT_S  # noqa: E402

RESPONSE_KEYS = {
    "session_key",
    "title",
    "reply",
    "itinerary",
    "degraded",
    "reason",
    "turn_index",
    "generated_at",
}
ITINERARY_KEYS = {"days", "days_count", "summary", "unused_collections"}
DAY_KEYS = {"day", "base", "stops", "tip"}
STOP_KEYS = {"name", "reason", "collection_id"}

GOOD_PAYLOAD = {
    "days": [
        {
            "day": 1,
            "base": "杭州",
            "stops": [
                {"name": "西湖", "reason": "收藏里点过名,湖边步道适合亲子慢走", "collection_id": 3},
                {"name": "浙江省博物馆", "reason": "离西湖步行 10 分钟,雨天备选", "collection_id": None},
            ],
            "tip": "上午人少,建议 9 点前到断桥",
        },
        {
            "day": 2,
            "base": "杭州",
            "stops": [
                {"name": "灵隐寺", "reason": "山中古刹,与西湖同侧顺路", "collection_id": None},
                {"name": "龙井村", "reason": "从灵隐出来一路向西,茶园拍照", "collection_id": None},
            ],
            "tip": "门票待核实",
        },
    ],
    "days_count": 2,
    "summary": "两天都在西湖西侧,少折腾",
    "unused_collections": ["千岛湖"],
}
GOOD_JSON = json.dumps(GOOD_PAYLOAD, ensure_ascii=False)

BRIEFS = (
    {"id": 3, "kind": "place", "title": "西湖", "summary": "杭州西湖,环湖步道与断桥,亲子友好"},
    {"id": 7, "kind": "place", "title": "千岛湖", "summary": "远,单程 2 小时以上"},
)


# --------------------------------------------------------------------------- #
# fixtures 与替身
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
    engine = make_engine(f"sqlite:///{tmp_path / 'planner_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()


class FakeLLM:
    """``intro.LLMClient`` 替身:记下每次 chat 的 kwargs,按剧本返回文本或抛异常。"""

    def __init__(self, reply: str = GOOD_JSON, *, enabled: bool = True, error: Optional[Exception] = None) -> None:
        self.reply = reply
        self._enabled = enabled
        self.error = error
        self.calls: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def label(self) -> str:
        return "替身 · fake"

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


def turn(session, *, key="s-1", message="3 天,亲子,不要太累", client=None, **kwargs):
    """跑一轮的快捷方式(默认注入成功的替身)。"""
    return planner.plan_turn(
        session,
        session_key=key,
        message=message,
        client=client if client is not None else FakeLLM(),
        **kwargs,
    )


def session_rows(session) -> list[models.PlannerSession]:
    return list(session.scalars(select(models.PlannerSession)))


def message_rows(session) -> list[models.PlannerMessage]:
    return list(session.scalars(select(models.PlannerMessage).order_by(models.PlannerMessage.id)))


# --------------------------------------------------------------------------- #
# plan_turn:返回形状 / 落库 / 多轮
# --------------------------------------------------------------------------- #


def test_plan_turn_response_keys_are_verbatim(session):
    result = turn(session, collection_briefs=BRIEFS)
    assert set(result) == RESPONSE_KEYS
    assert result["session_key"] == "s-1"
    assert result["degraded"] is False
    assert result["reason"] is None
    assert result["turn_index"] == 1
    assert isinstance(result["reply"], str) and result["reply"].strip()
    assert isinstance(result["generated_at"], str) and result["generated_at"]


def test_plan_turn_itinerary_shape_is_verbatim(session):
    itinerary = turn(session, collection_briefs=BRIEFS)["itinerary"]
    assert set(itinerary) == ITINERARY_KEYS
    assert itinerary["days_count"] == 2 == len(itinerary["days"])
    for day in itinerary["days"]:
        assert set(day) == DAY_KEYS
        assert planner.STOPS_PER_DAY_MIN <= len(day["stops"]) <= planner.STOPS_PER_DAY_MAX
        for stop in day["stops"]:
            assert set(stop) == STOP_KEYS
    assert itinerary["unused_collections"] == ["千岛湖"]
    assert itinerary["days"][0]["stops"][0]["collection_id"] == 3
    assert itinerary["days"][0]["stops"][1]["collection_id"] is None


def test_plan_turn_reply_lists_days_and_stops(session):
    reply = turn(session)["reply"]
    assert "2 天" in reply
    assert "第1天" in reply and "西湖" in reply
    assert "不排" in reply or "阶段" in reply  # 阶段 A 口径:只排每天的目的地


def test_plan_turn_persists_user_and_assistant_messages(session):
    turn(session, message="3 天 亲子")
    rows = message_rows(session)
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[0].content == "3 天 亲子"
    assert rows[0].payload["collection_ids"] == []
    assert rows[1].payload["itinerary"]["days_count"] == 2
    assert rows[1].payload["degraded"] is False
    assert rows[1].payload["reason"] is None


def test_plan_turn_commits_so_a_new_session_sees_the_turn(session):
    turn(session, key="durable", message="3 天")
    other = session_factory(session.get_bind())()
    try:
        assert len(planner.list_messages(other, session_key="durable")) == 2
    finally:
        other.close()


def test_plan_turn_turn_index_increments_over_turns(session):
    first = turn(session, message="3 天 亲子")
    second = turn(session, message="第二天换成古镇")
    assert (first["turn_index"], second["turn_index"]) == (1, 2)


def test_plan_turn_reuses_session_key_without_new_row(session):
    turn(session, message="3 天 亲子")
    again = turn(session, message="第二天换成古镇")
    assert again["session_key"] == "s-1"
    assert len(session_rows(session)) == 1
    assert len(message_rows(session)) == 4


def test_plan_turn_generates_session_key_when_blank(session):
    result = turn(session, key="", message="3 天")
    assert len(result["session_key"]) == 32
    assert all(char in "0123456789abcdef" for char in result["session_key"])
    assert session_rows(session)[0].session_key == result["session_key"]


def test_plan_turn_title_from_first_message_and_not_overwritten(session):
    first = turn(session, message="3 天 亲子 不要太累", client=FakeLLM())
    second = turn(session, message="第二天换成古镇", client=FakeLLM())
    assert first["title"] == second["title"] == "3 天 亲子 不要太累"
    assert len(session_rows(session)[0].title) <= models.PLANNER_TITLE_LEN


def test_plan_turn_stores_nights_in_user_payload_but_not_in_prompt(session):
    fake = FakeLLM()
    turn(session, message="3 天", client=fake, nights=2)
    assert message_rows(session)[0].payload["nights"] == 2
    assert "晚" not in fake.calls[0]["prompt"]  # 阶段 A 不排住宿,晚数不进 prompt


def test_plan_turn_rejects_empty_message(session):
    with pytest.raises(ValueError) as exc:
        planner.plan_turn(session, session_key="s-1", message="   ", client=FakeLLM())
    assert "message" in str(exc.value)
    assert message_rows(session) == []


def test_plan_turn_rejects_overlong_message(session):
    with pytest.raises(ValueError) as exc:
        planner.plan_turn(session, session_key="s-1", message="字" * (planner.MAX_MESSAGE_LEN + 1), client=FakeLLM())
    assert str(planner.MAX_MESSAGE_LEN) in str(exc.value)


# --------------------------------------------------------------------------- #
# LLM 预算与 prompt 硬约束
# --------------------------------------------------------------------------- #


def test_budget_is_passed_per_call_not_intro_default(session):
    fake = FakeLLM()
    turn(session, client=fake)
    call = fake.calls[0]
    assert call["max_tokens"] == planner.PLANNER_MAX_TOKENS == 1200
    assert call["timeout"] == planner.PLANNER_TIMEOUT_S == 180
    assert call["max_tokens"] != DEFAULT_MAX_TOKENS and call["timeout"] != DEFAULT_TIMEOUT_S


def test_system_prompt_is_planners_own_and_carries_six_constraints(session):
    fake = FakeLLM()
    turn(session, client=fake)
    assert fake.calls[0]["system"] == planner.SYSTEM_PROMPT
    prompt = planner.SYSTEM_PROMPT
    assert "未点名一律不得纳入" in prompt                       # ①只纳入点名收藏
    assert "不输出交通方式与交通报价" in prompt and "不输出住宿名称与价格" in prompt  # ②阶段 A
    assert "待核实" in prompt and "禁止编造" in prompt          # ③事实字段
    assert "2~4" in prompt and "顺路" in prompt and "15" in prompt  # ④每天点数与天数上限
    assert "一句具体理由" in prompt                            # ⑤每个 stop 一句理由
    assert "只输出一个 JSON 对象" in prompt and "围栏" in prompt   # ⑥输出格式


def test_prompt_repeats_mention_only_and_no_transport_stay_constraints(session):
    fake = FakeLLM()
    turn(session, client=fake, collection_briefs=BRIEFS)
    prompt = fake.calls[0]["prompt"]
    assert "未点名一律不得纳入" in prompt
    assert "不排交通、不排住宿" in prompt
    assert "本轮用户消息" in prompt and "3 天,亲子,不要太累" in prompt


def test_prompt_lists_briefs_and_truncates_summary_to_120(session):
    long_brief = {"id": 9, "kind": "place", "title": "某地", "summary": "长" * 300}
    fake = FakeLLM()
    turn(session, client=fake, collection_briefs=[long_brief])
    prompt = fake.calls[0]["prompt"]
    assert "「某地」" in prompt and "#9" in prompt
    assert "长" * planner.BRIEF_SUMMARY_LEN in prompt
    assert "长" * (planner.BRIEF_SUMMARY_LEN + 1) not in prompt


def test_prompt_without_briefs_says_material_is_empty(session):
    fake = FakeLLM()
    turn(session, client=fake)
    assert "本轮没有收藏素材" in fake.calls[0]["prompt"]


def test_history_is_capped_at_last_twelve_messages(session):
    row, _ = planner.get_or_create_session(session, session_key="s-1", title="历史")
    for index in range(1, 21):
        planner.add_message(
            session,
            row,
            role="user" if index % 2 else "assistant",
            content=f"历史-{index:02d}",
        )
    session.commit()
    fake = FakeLLM()
    turn(session, message="第 21 轮", client=fake)
    prompt = fake.calls[0]["prompt"]
    assert "历史-20" in prompt and "历史-09" in prompt      # 最近 12 条 = 09..20
    assert "历史-08" not in prompt and "历史-01" not in prompt
    assert f"最近 {planner.HISTORY_LIMIT} 条" in prompt


def test_history_excludes_the_current_message(session):
    fake = FakeLLM()
    turn(session, message="第一轮", client=fake)
    turn(session, message="第二轮换成古镇", client=fake)
    prompt = fake.calls[1]["prompt"]
    assert prompt.count("第二轮换成古镇") == 1     # 只在「本轮用户消息」里出现一次
    assert "第一轮" in prompt                       # 上一轮进历史


# --------------------------------------------------------------------------- #
# 降级四态(一律不抛,reply 非空)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("client", "reason"),
    [
        (FakeLLM(enabled=False), "no_key"),
        (FakeLLM(error=TransientDataSourceError("LLM", "请求超时(>180s):https://x")), "timeout"),
        (FakeLLM(error=requests.exceptions.ReadTimeout("read timed out")), "timeout"),
        (FakeLLM(error=DataSourceError("LLM", "HTTP 状态码 500:忙")), "error"),
        (FakeLLM(error=RuntimeError("boom")), "error"),
        (FakeLLM(reply="抱歉,我不会排行程"), "parse_error"),
    ],
)
def test_degraded_states_never_raise_and_reply_mentions_reason(session, client, reason):
    result = turn(session, client=client)
    assert result["degraded"] is True
    assert result["reason"] == reason
    assert result["itinerary"] is None
    assert result["reply"].strip() and reason in result["reply"]
    assert "重试" in result["reply"] or "重发" in result["reply"] or "再问" in result["reply"]


def test_degraded_turn_still_persists_both_messages(session):
    result = turn(session, client=FakeLLM(enabled=False))
    rows = message_rows(session)
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[1].content == result["reply"]
    assert rows[1].payload["itinerary"] is None
    assert rows[1].payload["reason"] == "no_key"


def test_no_key_via_environ_without_key(session):
    result = planner.plan_turn(session, session_key="env-1", message="3 天", environ={})
    assert result["degraded"] is True and result["reason"] == "no_key"
    assert result["reply"].strip()


def test_parse_error_reply_carries_raw_snippet(session):
    raw = "这是一段散文式的行程建议,没有 JSON。" * 20
    result = turn(session, client=FakeLLM(reply=raw))
    assert result["reason"] == "parse_error"
    assert "原始片段" in result["reply"]
    assert len(result["reply"]) < len(raw)


# --------------------------------------------------------------------------- #
# parse_itinerary:纯函数 + 四种脏输入
# --------------------------------------------------------------------------- #


def test_parse_itinerary_strips_json_fence():
    text = f"```json\n{GOOD_JSON}\n```"
    itinerary, error = planner.parse_itinerary(text)
    assert error is None
    assert itinerary["days_count"] == 2
    assert itinerary["days"][1]["stops"][0]["name"] == "灵隐寺"


def test_parse_itinerary_tolerates_surrounding_prose():
    text = f"好的,这是给你的行程:\n{GOOD_JSON}\n祝玩得开心,记得提前订票。"
    itinerary, error = planner.parse_itinerary(text)
    assert error is None
    assert itinerary["summary"] == "两天都在西湖西侧,少折腾"


def test_parse_itinerary_rejects_non_json():
    text = "我觉得可以去西湖和灵隐寺,第二天去龙井村。"
    itinerary, error = planner.parse_itinerary(text)
    assert itinerary is None
    assert error == text[: planner.PARSE_SNIPPET_LEN]


def test_parse_itinerary_rejects_day_without_required_fields():
    broken = json.dumps({"days": [{"base": "杭州", "stops": [{"name": "西湖"}]}]}, ensure_ascii=False)
    itinerary, error = planner.parse_itinerary(broken)
    assert itinerary is None and error
    for bad in (
        json.dumps({"days": []}, ensure_ascii=False),
        json.dumps({"days": [{"day": 1, "stops": []}]}, ensure_ascii=False),
        json.dumps({"days": "第一天去西湖"}, ensure_ascii=False),
        json.dumps({"summary": "没有 days"}, ensure_ascii=False),
        json.dumps([{"day": 1, "stops": []}], ensure_ascii=False),
    ):
        assert planner.parse_itinerary(bad)[0] is None


def test_parse_itinerary_snippet_is_capped_at_200_chars():
    text = "非 JSON 的长文本" + "啊" * 400
    itinerary, error = planner.parse_itinerary(text)
    assert itinerary is None
    assert len(error) == planner.PARSE_SNIPPET_LEN


def test_parse_itinerary_recomputes_days_count_and_defaults():
    raw = json.dumps(
        {
            "days": [{"day": "一", "stops": ["西湖", {"name": "灵隐寺"}]}],
            "days_count": 99,
            "unused_collections": "西湖",
        },
        ensure_ascii=False,
    )
    itinerary, error = planner.parse_itinerary(raw)
    assert error is None
    assert itinerary["days_count"] == 1
    assert itinerary["days"][0]["day"] == 1          # "一" 转不出数字 → 按顺序补 1
    assert itinerary["days"][0]["base"] == ""
    assert itinerary["days"][0]["tip"] == ""
    assert itinerary["days"][0]["stops"][0] == {"name": "西湖", "reason": "", "collection_id": None}
    assert itinerary["unused_collections"] == []      # 不是数组 → 空数组
    assert itinerary["summary"] == ""


@pytest.mark.parametrize("bad", [None, "", "   ", 42, {"days": []}])
def test_parse_itinerary_never_raises_on_non_text(bad):
    itinerary, error = planner.parse_itinerary(bad)
    assert itinerary is None
    assert error is None or isinstance(error, str)


# --------------------------------------------------------------------------- #
# 会话读写:list_messages / clear_session
# --------------------------------------------------------------------------- #


def test_list_messages_returns_verbatim_items_in_chronological_order(session):
    turn(session, message="3 天 亲子")
    items = planner.list_messages(session, session_key="s-1")
    assert [item["role"] for item in items] == ["user", "assistant"]
    for item in items:
        assert set(item) == {"role", "content", "payload", "created_at"}
        assert isinstance(item["created_at"], str)
    assert items[1]["payload"]["itinerary"]["days_count"] == 2


def test_list_messages_unknown_session_returns_empty(session):
    assert planner.list_messages(session, session_key="不存在") == []


def test_list_messages_limit_keeps_the_latest(session):
    for index in range(4):
        turn(session, message=f"第 {index + 1} 轮")
    items = planner.list_messages(session, session_key="s-1", limit=2)
    assert len(items) == 2
    assert items[0]["content"] == "第 4 轮" or items[0]["role"] == "assistant"
    assert items[-1]["payload"]["itinerary"] is not None
    assert len(planner.list_messages(session, session_key="s-1", limit=0)) == 8
    assert len(planner.list_messages(session, session_key="s-1", limit=9999)) == 8


def test_clear_session_deletes_messages_but_keeps_the_row(session):
    turn(session, message="3 天 亲子")
    turn(session, message="第二天换成古镇")
    assert planner.clear_session(session, session_key="s-1") == 4
    assert planner.list_messages(session, session_key="s-1") == []
    assert len(session_rows(session)) == 1        # session_key 继续可用
    assert turn(session, message="重新来过")["turn_index"] == 1


def test_clear_session_unknown_key_is_zero(session):
    assert planner.clear_session(session, session_key="不存在") == 0
    assert planner.clear_session(session, session_key="不存在") == 0


def test_add_message_rejects_unknown_role(session):
    row, created = planner.get_or_create_session(session, session_key="s-1", title="x")
    assert created is True
    with pytest.raises(ValueError):
        planner.add_message(session, row, role="system", content="hi")


def test_normalize_briefs_drops_useless_entries_and_truncates(session):
    briefs = planner.normalize_briefs(
        [
            {"id": "3", "kind": "place", "title": "西湖", "summary": "  湖  " + "长" * 200},
            {"id": None, "kind": "", "title": "", "summary": ""},
            "不是字典",
            {"title": "只有名字"},
        ]
    )
    assert [brief["id"] for brief in briefs] == [3, None]
    assert briefs[0]["summary"] == ("  湖  " + "长" * 200).strip()[: planner.BRIEF_SUMMARY_LEN]
    assert len(briefs[0]["summary"]) == planner.BRIEF_SUMMARY_LEN
    assert briefs[1] == {"id": None, "kind": "", "title": "只有名字", "summary": ""}


def test_resolve_session_key_is_stable_and_generates_only_when_blank():
    assert planner.resolve_session_key("  abc  ") == "abc"
    assert planner.resolve_session_key(None) != planner.resolve_session_key("")
    assert len(planner.resolve_session_key("k" * 200)) == models.SESSION_KEY_LEN
