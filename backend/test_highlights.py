"""TASK-8a2 类别专属要点的单测:字段表 / LLM 预算 / 降级 / 永久缓存 / API 批量。

口径(契约 docs/TASK-8-CONTRACT.md §3 TASK-8a2):

* 字段表**逐字**按分类走,分类名用**包含匹配**(库内是全称:自然风光/小城人文美食/滑雪场/运动);
* ``HIGHLIGHT_MAX_TOKENS=600`` / ``HIGHLIGHT_TIMEOUT_S=60`` **必须真的按调用传给 LLM**
  (沿用 ``intro.LLMClient`` 的 120/20s 默认会静默截断 → 空要点,所以这里注入替身断言 kwargs);
* 查不到 → ``value=null`` 且 note 标「待核实」;LLM 异常 / 坏 JSON → ``fields=[]`` + ``note="解析失败"``,**不抛**;
* 生成成功按 POI **永久缓存**在 ``PlaceHighlight``:命中即回、**零 LLM 调用**;失败**不写行**(可重试);
* ``GET /api/places/highlights``:batch ≤ 10、超限/缺参/非法 id 一律 **400 中文**,
  items **按入参顺序**,单条失败不影响其余。

全套**纯 mock 不触网**(``no_network`` autouse 把 ``requests.Session.request`` 换成抛错)。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Optional

import pytest
import requests
from fastapi import HTTPException
from sqlalchemy import select

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import places as places_api  # noqa: E402
from app.main import app  # noqa: E402
from data_sources import DataSourceError  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db import models  # noqa: E402
from db import repository as repo  # noqa: E402
from db.base import get_session  # noqa: E402
from services import highlights  # noqa: E402
from services import intro as intro_service  # noqa: E402

WEST_LAKE = (30.246, 120.149)


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
    engine = make_engine(f"sqlite:///{tmp_path / 'highlights_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


@pytest.fixture()
def api_client(session):
    """完整 HTTP 链用的客户端:把 ``get_session`` 依赖换成用例里的同一个临时库会话。"""
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield lambda method, path, query="": http_request(method, path, query=query)
    finally:
        app.dependency_overrides.pop(get_session, None)


def http_request(method: str, path: str, *, query: str = "") -> tuple[int, Any]:
    """直接驱动 ASGI app 走一遍**完整 HTTP 链**(仓库没装 httpx/TestClient,自己拼最小 scope)。"""
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                    (b"content-length", b"0")],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    payload = bytearray()
    status: dict[str, int] = {}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            payload.extend(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return status["code"], json.loads(payload.decode() or "null")


class FakeLLM:
    """替身 :class:`services.intro.LLMClient`:记下每次 ``chat`` 的 kwargs(断言预算),
    按队列回固定文本 / 抛异常;队列耗尽再被调用即失败(用来证明"命中缓存零 LLM")。"""

    def __init__(self, *replies: Any, enabled: bool = True) -> None:
        self.replies: list[Any] = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.enabled = enabled
        self.label = "测试 · fake-model"

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
        if not self.replies:
            raise AssertionError("替身 LLM 被多调用了一次(replies 已耗尽)")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return str(reply(prompt))
        return str(reply)


def stub_llm(monkeypatch: pytest.MonkeyPatch, fake: FakeLLM) -> FakeLLM:
    """把两个注入点(默认客户端 / 按 environ 构造)都换成同一个替身。"""
    monkeypatch.setattr(highlights, "default_llm_client", lambda: fake)
    monkeypatch.setattr(highlights, "LLMClient", lambda *args, **kwargs: fake)
    return fake


def make_place(session, *, name: str = "西湖", category: str = "自然风光", osm_id: int = 1001,
               lat: float = WEST_LAKE[0], lng: float = WEST_LAKE[1]) -> models.Place:
    """往临时库里塞一条 ``Place``(高德来源),返回 ORM 行。"""
    repo.upsert_places(session, origin_city="杭州", band="0_50", items=[{
        "osm_type": models.AMAP_OSM_TYPE, "osm_id": osm_id, "name": name,
        "lat": lat, "lng": lng, "category": category, "tags": {"amap_id": "B023B0AJQ5"},
    }])
    session.commit()
    return session.scalars(select(models.Place).where(models.Place.osm_id == osm_id)).one()


def reply_json(**values: Any) -> str:
    """字段表 → 严格 JSON 回复(值为 ``None`` 就是"查不到")。"""
    return json.dumps(values, ensure_ascii=False)


NATURE_OK = reply_json(**{
    "最佳季节": "春秋两季最舒服,夏夜可夜游。",
    "门票/开放信息": "环湖免费开放,个别景点单独收费。",
    "游玩建议": "留半天到一天,步行或骑行绕湖。",
})
SKI_OK = reply_json(**{
    "雪道数与分级": "初中级为主,具体条数需核实。",
    "开放期": "12 月至次年 3 月。",
    "适合人群": "初学者与家庭客。",
})
NATURE_PARTIAL = reply_json(**{
    "最佳季节": "春秋两季。",
    "门票/开放信息": None,
    "游玩建议": "半天即可。",
})


# --------------------------------------------------------------------------- #
# 1) 字段表(契约逐字)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("key", "labels"), [
    ("自然", ("最佳季节", "门票/开放信息", "游玩建议")),
    ("人文美食", ("人文背景", "必吃", "代表小店")),
    ("滑雪", ("雪道数与分级", "开放期", "适合人群")),
    ("运动", ("项目", "场地/装备", "适宜人群")),
    ("其他", ("亮点", "建议")),
])
def test_category_fields_are_verbatim(key: str, labels: tuple[str, ...]) -> None:
    """四分类 + 兜底的字段表必须**逐字**等于契约里的字段名与顺序。"""
    assert highlights.CATEGORY_FIELDS[key] == labels


@pytest.mark.parametrize(("category", "key"), [
    ("自然风光", "自然"),
    ("小城人文美食", "人文美食"),
    ("滑雪场", "滑雪"),
    ("运动", "运动"),
    ("其他", "其他"),
    ("", "其他"),
    (None, "其他"),
    ("自然", "自然"),          # 种子数据里的短名也要落到同一张表
    ("滑雪", "滑雪"),
])
def test_category_key_uses_substring_matching(category: Any, key: str) -> None:
    """库内分类是**全称**,所以用包含匹配(不硬编码全等);认不出的一律「其他」。"""
    assert highlights.category_key(category) == key
    assert highlights.fields_for_category(category) == highlights.CATEGORY_FIELDS[key]


def test_category_key_keeps_classify_priority() -> None:
    """含"滑雪"又含"运动"时按归类优先级取滑雪(与 services.classify 的优先级同口径)。"""
    assert highlights.category_key("滑雪运动公园") == "滑雪"
    assert highlights.category_key("水上运动中心") == "运动"


# --------------------------------------------------------------------------- #
# 2) LLM 预算(必须按调用放大,绝不用 120/20s 默认)
# --------------------------------------------------------------------------- #


def test_budget_constants_are_amplified_beyond_intro_defaults() -> None:
    """常量本身就比 ``intro`` 的默认宽(默认值是给"一句话简介"的,出 JSON 会被截断)。"""
    assert highlights.HIGHLIGHT_MAX_TOKENS == 600
    assert highlights.HIGHLIGHT_TIMEOUT_S == 60
    assert highlights.HIGHLIGHT_MAX_TOKENS > intro_service.DEFAULT_MAX_TOKENS
    assert highlights.HIGHLIGHT_TIMEOUT_S > intro_service.DEFAULT_TIMEOUT_S


def test_chat_call_receives_amplified_budget() -> None:
    """注入替身断言 ``max_tokens=600`` / ``timeout=60`` **确实传进了 chat()**。"""
    fake = FakeLLM(NATURE_OK)
    highlights.fetch_highlights({"id": 7, "name": "西湖", "category": "自然风光"}, client=fake)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["max_tokens"] == highlights.HIGHLIGHT_MAX_TOKENS == 600
    assert call["timeout"] == highlights.HIGHLIGHT_TIMEOUT_S == 60
    assert call["max_tokens"] != intro_service.DEFAULT_MAX_TOKENS
    assert call["timeout"] != intro_service.DEFAULT_TIMEOUT_S


def test_system_prompt_requires_strict_json_and_forbids_fabrication() -> None:
    """system prompt:严格 JSON + 查不到写 null + **禁止编造**票价/雪道数/店名。"""
    fake = FakeLLM(NATURE_OK)
    highlights.fetch_highlights({"id": 7, "name": "西湖", "category": "自然风光"}, client=fake)
    system = fake.calls[0]["system"]
    assert system == highlights.SYSTEM_PROMPT
    for fragment in ("JSON", "null", "编造", "门票价格", "雪道", "店名"):
        assert fragment in system, f"system prompt 应包含 {fragment!r}"


def test_user_prompt_lists_category_labels_in_order() -> None:
    """user prompt 里逐字带上该分类的字段表(顺序也照),模型才知道要出哪些键。"""
    fake = FakeLLM(SKI_OK)
    highlights.fetch_highlights({"id": 9, "name": "云栖滑雪场", "category": "滑雪场"}, client=fake)
    prompt = fake.calls[0]["prompt"]
    assert "云栖滑雪场" in prompt and "滑雪场" in prompt
    for label in highlights.CATEGORY_FIELDS["滑雪"]:
        assert label in prompt
    assert prompt.index("雪道数与分级") < prompt.index("开放期") < prompt.index("适合人群")
    assert "自然" not in prompt.replace("自然风光", "").replace("自然景观", "")  # 不串别的字段表


# --------------------------------------------------------------------------- #
# 3) 生成 / 待核实 / 降级
# --------------------------------------------------------------------------- #


def test_fetch_highlights_shape_and_field_order() -> None:
    """成功路径:响应形状恒定,``fields`` 按字段表顺序回 label/value,note 是正常口径。"""
    fake = FakeLLM(NATURE_OK)
    item = highlights.fetch_highlights(
        {"id": 3, "name": "西湖", "category": "自然风光", "distance_km": 12.5}, client=fake
    )
    assert set(item) >= {"place_id", "category", "fields", "note", "cached"}
    assert item["place_id"] == 3
    assert item["category"] == "自然风光"
    assert item["cached"] is False
    assert [field["label"] for field in item["fields"]] == list(highlights.CATEGORY_FIELDS["自然"])
    assert item["fields"][0]["value"] == "春秋两季最舒服,夏夜可夜游。"
    assert item["note"] == highlights.NOTE_BASE
    assert "待核实" not in item["note"]


def test_null_value_keeps_label_and_marks_note_pending() -> None:
    """模型回 null → 键保留、``value=None``,note **标「待核实」**(不许编造填充)。"""
    fake = FakeLLM(NATURE_PARTIAL)
    item = highlights.fetch_highlights({"id": 3, "name": "西湖", "category": "自然风光"}, client=fake)
    labels = [field["label"] for field in item["fields"]]
    assert labels == list(highlights.CATEGORY_FIELDS["自然"])
    assert item["fields"][1] == {"label": "门票/开放信息", "value": None}
    assert "待核实" in item["note"] and "1/3" in item["note"]


def test_missing_and_extra_keys_are_normalized_to_field_table() -> None:
    """模型少给键 → 补 ``value=None``(待核实);多给的键丢掉(响应形状永远等于字段表)。"""
    fake = FakeLLM(reply_json(**{"人文背景": "古镇沿河而建。", "乱入的键": "别显示我"}))
    item = highlights.fetch_highlights(
        {"id": 4, "name": "南浔古镇", "category": "小城人文美食"}, client=fake
    )
    assert item["fields"] == [
        {"label": "人文背景", "value": "古镇沿河而建。"},
        {"label": "必吃", "value": None},
        {"label": "代表小店", "value": None},
    ]
    assert "待核实" in item["note"] and "2/3" in item["note"]
    assert all("乱入的键" not in field["label"] for field in item["fields"])


@pytest.mark.parametrize("placeholder", ["未知", "暂无", "N/A", "null", "  ", "查不到"])
def test_placeholder_words_are_normalized_to_null(placeholder: str) -> None:
    """模型用「未知/暂无」这类占位话术搪塞 → 归一成 ``None``(前端按待核实弱化,不显示占位话术)。"""
    fake = FakeLLM(reply_json(**{"亮点": placeholder, "建议": "适合半日游。"}))
    item = highlights.fetch_highlights({"id": 5, "name": "某地", "category": "其他"}, client=fake)
    assert item["fields"][0]["value"] is None
    assert item["fields"][1]["value"] == "适合半日游。"
    assert "待核实" in item["note"]


@pytest.mark.parametrize("raw", [
    "",
    "对不起,我无法回答。",
    "{最佳季节: 春秋}",                       # 不是合法 JSON
    '{"最佳季节": "春秋", ',                    # 被截断(预算不够时的典型现象)
    "[]",
    "{}",
    "[1, 2, 3]",
])
def test_bad_json_degrades_to_empty_fields(raw: str) -> None:
    """坏 JSON / 空输出 → ``fields=[]`` + ``note="解析失败"``,**不抛**(API 不会 500)。"""
    item = highlights.fetch_highlights(
        {"id": 6, "name": "西湖", "category": "自然风光"}, client=FakeLLM(raw)
    )
    assert item["fields"] == []
    assert item["note"] == "解析失败"
    assert item["cached"] is False


def test_code_fenced_json_is_accepted() -> None:
    """模型爱包 ```json 代码块 / 前后加一句废话 → 照样解析出来(不算解析失败)。"""
    raw = "好的,以下是要点:\n```json\n" + NATURE_OK + "\n```\n希望有帮助。"
    item = highlights.fetch_highlights(
        {"id": 6, "name": "西湖", "category": "自然风光"}, client=FakeLLM(raw)
    )
    assert item["note"] != "解析失败"
    assert item["fields"][0]["value"] == "春秋两季最舒服,夏夜可夜游。"


def test_list_of_objects_json_is_accepted() -> None:
    """模型回 ``[{"label": ..., "value": ...}]`` 也认(按字段表重排、补 null)。"""
    raw = json.dumps([
        {"label": "项目", "value": "攀岩与抱石。"},
        {"label": "适宜人群", "value": None},
    ], ensure_ascii=False)
    item = highlights.fetch_highlights({"id": 8, "name": "岩馆", "category": "运动"}, client=FakeLLM(raw))
    assert item["fields"] == [
        {"label": "项目", "value": "攀岩与抱石。"},
        {"label": "场地/装备", "value": None},
        {"label": "适宜人群", "value": None},
    ]


@pytest.mark.parametrize("error", [
    DataSourceError("Highlight", "读超时"),
    RuntimeError("连接被重置"),
])
def test_llm_failure_degrades_without_raising(error: BaseException) -> None:
    """LLM 抛任何异常(数据源错 / 未预期的错)都降级成 ``fields=[]`` + 解析失败,**不冒出去**。"""
    item = highlights.fetch_highlights(
        {"id": 6, "name": "西湖", "category": "自然风光"}, client=FakeLLM(error)
    )
    assert item["fields"] == []
    assert item["note"] == highlights.NOTE_PARSE_FAILED == "解析失败"


def test_missing_key_degrades_without_calling_llm() -> None:
    """没配 key(``enabled=False``)→ 不发请求,``fields=[]`` + note 说明未配置。"""
    fake = FakeLLM(NATURE_OK, enabled=False)
    item = highlights.fetch_highlights(
        {"id": 6, "name": "西湖", "category": "自然风光"}, client=fake
    )
    assert fake.calls == []
    assert item["fields"] == []
    assert "未配置" in item["note"]


def test_resolve_client_without_key_environ_is_disabled() -> None:
    """``environ`` 传空环境 → 走 :func:`services.intro.resolve_provider`,拿不到 key 就降级。"""
    client = highlights.resolve_client(None, {})
    assert client.enabled is False
    item = highlights.fetch_highlights(
        {"id": 6, "name": "西湖", "category": "自然风光"}, environ={}
    )
    assert item["fields"] == [] and "未配置" in item["note"]


def test_place_without_id_raises_value_error() -> None:
    """没有正整数 id 就无法缓存 → 明确报错(而不是静默生成一条落不了库的要点)。"""
    with pytest.raises(ValueError):
        highlights.fetch_highlights({"name": "西湖"}, client=FakeLLM(NATURE_OK))


# --------------------------------------------------------------------------- #
# 4) 永久缓存(PlaceHighlight)
# --------------------------------------------------------------------------- #


def test_cache_hit_is_permanent_and_zero_llm(session) -> None:
    """第一次生成落库,第二次**命中永久缓存**:``cached=True`` 且 LLM 一次都不再调。"""
    place = make_place(session, name="西湖", category="自然风光")
    fake = FakeLLM(NATURE_OK)
    first = highlights.fetch_highlights(place, client=fake, session=session)
    session.commit()
    assert first["cached"] is False and len(fake.calls) == 1
    assert first["generated_at"]

    second = highlights.fetch_highlights(place, client=fake, session=session)
    assert second["cached"] is True
    assert len(fake.calls) == 1, "命中缓存必须零 LLM 调用"
    assert second["fields"] == first["fields"]
    assert second["note"] == first["note"]
    assert second["category"] == "自然风光"


def test_failed_generation_is_not_cached(session) -> None:
    """解析失败**不写行**(永久缓存里钉死一条失败就没法重试了)→ 下次仍会调 LLM 并成功。"""
    place = make_place(session, name="西湖", category="自然风光")
    fake = FakeLLM("这不是 JSON", NATURE_OK)
    broken = highlights.fetch_highlights(place, client=fake, session=session)
    session.commit()
    assert broken["fields"] == [] and broken["note"] == "解析失败"
    assert repo.get_place_highlight(session, place_id=place.id) is None

    retried = highlights.fetch_highlights(place, client=fake, session=session)
    session.commit()
    assert len(fake.calls) == 2
    assert retried["cached"] is False
    assert [field["label"] for field in retried["fields"]] == list(highlights.CATEGORY_FIELDS["自然"])
    assert repo.get_place_highlight(session, place_id=place.id) is not None


def test_all_null_fields_are_still_cached(session) -> None:
    """模型确有其答但全部字段查不到(全 null)→ 也算一次生成结果,**永久缓存**(不重复烧配额)。"""
    place = make_place(session, name="某野湖", category="自然风光")
    fake = FakeLLM(reply_json(**{"最佳季节": None, "门票/开放信息": None, "游玩建议": None}))
    item = highlights.fetch_highlights(place, client=fake, session=session)
    session.commit()
    assert all(field["value"] is None for field in item["fields"])
    assert "待核实" in item["note"]
    assert highlights.fetch_highlights(place, client=fake, session=session)["cached"] is True
    assert len(fake.calls) == 1


def test_repository_upsert_is_idempotent(session) -> None:
    """``upsert_place_highlight`` 按 ``place_id`` 唯一:重复写只刷新一行(与 PlaceDetail 同口径)。"""
    place = make_place(session, name="西湖", category="自然风光")
    fields = [{"label": "最佳季节", "value": "春秋"}, {"label": "门票/开放信息", "value": None}]
    first = repo.upsert_place_highlight(session, place_id=place.id, fields=fields,
                                        category="自然风光", note="n1")
    second = repo.upsert_place_highlight(session, place_id=place.id, fields=fields,
                                         category="自然风光", note="n2")
    session.commit()
    assert first.id == second.id
    rows = list(session.scalars(select(models.PlaceHighlight)))
    assert len(rows) == 1
    assert rows[0].note == "n2"
    assert repo.highlight_to_dict(rows[0]) == {
        "place_id": place.id,
        "category": "自然风光",
        "fields": [{"label": "最佳季节", "value": "春秋"},
                   {"label": "门票/开放信息", "value": None}],
        "note": "n2",
        "generated_at": repo.highlight_to_dict(rows[0])["generated_at"],
    }


def test_repository_rejects_bad_place_id(session) -> None:
    """``place_id`` 非正整数 → ``ValueError``(API 层转 400);``highlight_map`` 跳过非法 id。"""
    with pytest.raises(ValueError):
        repo.get_place_highlight(session, place_id="abc")
    with pytest.raises(ValueError):
        repo.upsert_place_highlight(session, place_id=0, fields=[])
    place = make_place(session, name="西湖", category="自然风光")
    repo.upsert_place_highlight(session, place_id=place.id,
                                fields=[{"label": "亮点", "value": "湖景"}], category="自然风光")
    session.commit()
    assert list(repo.highlight_map(session, [place.id, "abc", -1, None])) == [place.id]
    assert repo.highlight_map(session, []) == {}


def test_highlight_table_has_unique_place_id(session) -> None:
    """表结构:``place_id`` 唯一约束存在(旧库靠 create_all 自动建表)。"""
    table = models.PlaceHighlight.__table__
    assert {column.name for column in table.columns} >= {
        "id", "place_id", "category", "fields", "note", "generated_at"
    }
    assert any(
        constraint.columns.keys() == ["place_id"] and constraint.name == "uq_place_highlight"
        for constraint in table.constraints
    )


# --------------------------------------------------------------------------- #
# 5) API:GET /api/places/highlights
# --------------------------------------------------------------------------- #


def test_api_requires_place_ids(api_client) -> None:
    """缺参 → **400 中文**(不是 422:路由用裸 Query + 手工校验)。"""
    status, body = api_client("GET", "/api/places/highlights")
    assert status == 400
    assert "place_ids" in body["detail"]


@pytest.mark.parametrize(("query", "fragment"), [
    ("place_ids=", "解析后为空"),
    ("place_ids=abc", "正整数"),
    ("place_ids=1,,x", "正整数"),
    ("place_ids=0", "正整数"),
    ("place_ids=-3", "正整数"),
])
def test_api_rejects_bad_ids(api_client, query: str, fragment: str) -> None:
    status, body = api_client("GET", "/api/places/highlights", query=query)
    assert status == 400
    assert fragment in body["detail"]


def test_api_batch_limit_is_ten(api_client, session, monkeypatch) -> None:
    """batch ≤ 10:11 个 → 400 中文;10 个正好放行(超限报错里带上限与实收个数)。"""
    fake = stub_llm(monkeypatch, FakeLLM())
    ids = ",".join(str(index) for index in range(1, 12))
    status, body = api_client("GET", "/api/places/highlights", query=f"place_ids={ids}")
    assert status == 400
    assert "10" in body["detail"] and "11" in body["detail"]
    assert fake.calls == []

    ok_ids = ",".join(str(index) for index in range(1, 11))
    status, body = api_client("GET", "/api/places/highlights", query=f"place_ids={ok_ids}")
    assert status == 200
    assert body["count"] == 10 and len(body["items"]) == 10


def test_api_items_follow_input_order(session, api_client, monkeypatch) -> None:
    """``items`` 严格按入参顺序回(重复 id 只回一条),不是按库里的主键序。"""
    first = make_place(session, name="西湖", category="自然风光", osm_id=1001)
    second = make_place(session, name="南浔古镇", category="小城人文美食", osm_id=1002)
    stub_llm(monkeypatch, FakeLLM(SKI_OK, NATURE_OK))  # 回复内容不重要,只看顺序与 id
    status, body = api_client(
        "GET", "/api/places/highlights", query=f"place_ids={second.id},{first.id},{second.id}"
    )
    assert status == 200
    assert [item["place_id"] for item in body["items"]] == [second.id, first.id]
    assert body["count"] == 2
    assert body["items"][0]["category"] == "小城人文美食"
    assert body["items"][1]["category"] == "自然风光"


def test_api_cache_hit_is_zero_llm(session, api_client, monkeypatch) -> None:
    """库内已有要点 → API 直接回缓存(``cached=true``),LLM 一次都不调。"""
    place = make_place(session, name="西湖", category="自然风光")
    repo.upsert_place_highlight(
        session, place_id=place.id, category="自然风光", note=highlights.NOTE_BASE,
        fields=[{"label": "最佳季节", "value": "春秋"},
                {"label": "门票/开放信息", "value": None},
                {"label": "游玩建议", "value": "绕湖半天"}],
    )
    session.commit()
    fake = stub_llm(monkeypatch, FakeLLM())
    status, body = api_client("GET", "/api/places/highlights", query=f"place_ids={place.id}")
    assert status == 200
    assert fake.calls == []
    item = body["items"][0]
    assert item["cached"] is True
    assert item["fields"][1]["value"] is None
    assert item["note"] == highlights.NOTE_BASE


def test_api_generates_and_persists_for_miss(session, api_client, monkeypatch) -> None:
    """miss 才调 LLM:生成后**永久落库**(第二次请求就变成缓存命中、零 LLM)。"""
    place = make_place(session, name="西湖", category="自然风光")
    fake = stub_llm(monkeypatch, FakeLLM(NATURE_OK))
    status, body = api_client("GET", "/api/places/highlights", query=f"place_ids={place.id}")
    assert status == 200
    item = body["items"][0]
    assert item["cached"] is False and len(fake.calls) == 1
    assert [field["label"] for field in item["fields"]] == list(highlights.CATEGORY_FIELDS["自然"])
    assert item["generated_at"]
    assert repo.get_place_highlight(session, place_id=place.id) is not None

    again_status, again = api_client("GET", "/api/places/highlights", query=f"place_ids={place.id}")
    assert again_status == 200
    assert again["items"][0]["cached"] is True
    assert len(fake.calls) == 1


def test_api_single_failure_does_not_break_the_batch(session, api_client, monkeypatch) -> None:
    """批量里一条炸了(LLM 抛错)只影响该条:``fields=[]`` + note,其余照常、HTTP 仍 200。"""
    good = make_place(session, name="西湖", category="自然风光", osm_id=1001)
    bad = make_place(session, name="云栖滑雪场", category="滑雪场", osm_id=1002)
    stub_llm(monkeypatch, FakeLLM(
        DataSourceError("Highlight", "读超时"),   # 第一条:LLM 异常
        SKI_OK,                                    # 第二条:正常
    ))
    status, body = api_client(
        "GET", "/api/places/highlights", query=f"place_ids={good.id},{bad.id}"
    )
    assert status == 200
    first, second = body["items"]
    assert first["place_id"] == good.id and first["fields"] == [] and first["note"] == "解析失败"
    assert second["place_id"] == bad.id
    assert [field["label"] for field in second["fields"]] == list(highlights.CATEGORY_FIELDS["滑雪"])


def test_api_unknown_place_id_degrades_only_that_item(session, api_client, monkeypatch) -> None:
    """库内没有的 id → 该条 ``fields=[]`` + note「目的地不存在」,不 404、不影响其余。"""
    place = make_place(session, name="西湖", category="自然风光")
    stub_llm(monkeypatch, FakeLLM(NATURE_OK))
    status, body = api_client("GET", "/api/places/highlights", query=f"place_ids={place.id},99999")
    assert status == 200
    known, unknown = body["items"]
    assert known["place_id"] == place.id and known["fields"]
    assert unknown["place_id"] == 99999
    assert unknown["fields"] == []
    assert "不存在" in unknown["note"]


def test_api_no_key_returns_empty_fields_not_500(session, api_client, monkeypatch) -> None:
    """没配 LLM key:HTTP 仍 **200**,每条 ``fields=[]`` + note 说明未配置(不 500、不抛)。"""
    place = make_place(session, name="西湖", category="自然风光")
    stub_llm(monkeypatch, FakeLLM(NATURE_OK, enabled=False))
    status, body = api_client("GET", "/api/places/highlights", query=f"place_ids={place.id}")
    assert status == 200
    item = body["items"][0]
    assert item["fields"] == [] and "未配置" in item["note"]
    assert repo.get_place_highlight(session, place_id=place.id) is None  # 失败不落库


def test_api_response_shape_and_note(api_client, session, monkeypatch) -> None:
    """响应形状:``items``/``count``/``elapsed_s``/``note``,note 讲清永久缓存与待核实口径。"""
    place = make_place(session, name="西湖", category="自然风光")
    stub_llm(monkeypatch, FakeLLM(NATURE_PARTIAL))
    status, body = api_client("GET", "/api/places/highlights", query=f"place_ids={place.id}")
    assert status == 200
    assert set(body) >= {"items", "count", "elapsed_s", "note"}
    assert body["note"] == places_api.HIGHLIGHT_NOTE
    for fragment in ("永久缓存", "待核实", "按入参顺序", "null"):
        assert fragment in body["note"], f"note 应包含 {fragment!r}"
    item = body["items"][0]
    assert set(item) == {"place_id", "category", "fields", "note", "cached", "generated_at"}
    assert "待核实" in item["note"]


def test_service_batch_helper_is_order_stable_and_skips_bad_ids(session, monkeypatch) -> None:
    """服务层批量入口:非法 id 跳过、保序去重、库内没有的 id 给降级条目(不发 LLM)。"""
    place = make_place(session, name="西湖", category="自然风光")
    fake = stub_llm(monkeypatch, FakeLLM(NATURE_OK))
    items = highlights.fetch_highlights_for_places(session, [place.id, "abc", -1, place.id, 4242])
    session.commit()
    assert [item["place_id"] for item in items] == [place.id, 4242]
    assert items[1]["fields"] == [] and "不存在" in items[1]["note"]
    assert len(fake.calls) == 1
    assert highlights.fetch_highlights_for_places(session, []) == []


def test_direct_endpoint_call_without_http(session, monkeypatch) -> None:
    """单测直调端点函数也能用(``_optional_text`` 兜住 FieldInfo,缺参照样 400 中文)。"""
    stub_llm(monkeypatch, FakeLLM())
    with pytest.raises(HTTPException) as caught:
        places_api.places_highlights(place_ids=None, session=session)
    assert caught.value.status_code == 400 and "place_ids" in str(caught.value.detail)

    place = make_place(session, name="西湖", category="自然风光")
    stub_llm(monkeypatch, FakeLLM(NATURE_OK))
    payload = places_api.places_highlights(place_ids=str(place.id), session=session)
    assert payload["count"] == 1
    assert payload["items"][0]["place_id"] == place.id
