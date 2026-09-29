"""TASK-3a2 单测:``GET /api/stays`` 薄路由(校验口径 / 出参形状 / place_id / 缓存 / 降级)。

全程不触网、不调真 LLM:

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,任何偷偷联网当场失败;
* Overpass 换成假客户端(:func:`data_sources.overpass.default_client` 被 monkeypatch),
  LLM 换成假 client(``services.stays.default_llm_client`` 被 monkeypatch),
  所以一次请求会把 **路由 → 服务层 → 入库 → 序列化** 整条链跑完,仍然零网络;
* DB 用 ``tmp_path`` 下的临时 SQLite,``app.dependency_overrides`` 把 ``get_session``
  换成同一个会话;完整 HTTP 链用手拼的最小 ASGI scope 驱动(仓库没装 httpx/TestClient)。

重点覆盖:起点二选一(``lat``+``lng`` / ``place_id``)的校验(**400 + 中文报错**)、
``radius_km`` 上限与米换算、``refresh`` 透传、出参投影与 ``estimated`` 估算标注、
DB 即缓存(命中缓存时零次 Overpass、零次 LLM)与各路降级都不 500。

运行:``cd backend && ../.venv/bin/python -m pytest test_stays_api.py -q``
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import pytest
import requests
from sqlalchemy import select

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import stays as stays_api  # noqa: E402
from app.main import app  # noqa: E402
from data_sources import overpass as overpass_module  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db.base import get_session  # noqa: E402
from db.models import DEFAULT_CURRENCY, Place, Stay  # noqa: E402
from services import stays as stay_service  # noqa: E402

# --------------------------------------------------------------------------- #
# 样本数据:起点上海人民广场,半径 8 km 内四处住宿(含一处无名公寓)
# --------------------------------------------------------------------------- #

ORIGIN_LAT = 31.2304
ORIGIN_LNG = 121.4737
GOOD_COMPLETION = "价格: 约¥200-400/晚\n简介: 位于市中心的经济型酒店。"
# 由近及远:老船长(0.8km) < 无名公寓(1.7km) < 华尔道夫(1.9km) < 衡山路小筑(4.1km)
SAMPLE_ORDER = ["老船长青旅", "", "外滩华尔道夫酒店", "衡山路小筑"]
TOP_LEVEL_KEYS = {"lat", "lng", "radius_km", "count", "source", "note", "items"}
ITEM_KEYS = {
    "id", "osm_type", "osm_id", "name", "kind", "lat", "lng",
    "distance_km", "price_estimate", "price_kind", "currency", "intro", "estimated",
}
EXISTING_PATHS = {
    "/api/discover", "/api/categories", "/api/places", "/api/places/meta",
    "/api/places/intros", "/api/geocode", "/api/geocode/reverse", "/api/routes",
    "/api/collections", "/api/collections/{collection_id}",
}


def node(osm_id: int, lat: float, lng: float, tags: dict[str, Any]) -> dict[str, Any]:
    """一个 Overpass node element。"""
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lng, "tags": dict(tags)}


def way(osm_id: int, lat: float, lng: float, tags: dict[str, Any]) -> dict[str, Any]:
    """一个 way element:坐标只在 ``center`` 里(:func:`overpass.parse_element` 会取)。"""
    return {"type": "way", "id": osm_id, "center": {"lat": lat, "lon": lng}, "tags": dict(tags)}


SAMPLE_ELEMENTS: list[dict[str, Any]] = [
    node(1, 31.2400, 121.4900, {"tourism": "hotel", "name": "外滩华尔道夫酒店", "stars": "5"}),
    node(2, 31.2350, 121.4800, {"tourism": "hostel", "name": "老船长青旅"}),
    way(3, 31.2600, 121.5000, {"tourism": "guest_house", "name": "衡山路小筑"}),
    node(4, 31.2200, 121.4600, {"tourism": "apartment"}),
]


def sample_payload() -> dict[str, Any]:
    return {"elements": [dict(item) for item in SAMPLE_ELEMENTS]}


class FakeOverpass:
    """假 Overpass 客户端:记录查询与调用次数,可注入响应或异常。"""

    def __init__(self, payload: Optional[Any] = None, *, error: Optional[BaseException] = None):
        self.payload = {"elements": []} if payload is None else payload
        self.error = error
        self.queries: list[str] = []
        self.timeouts: list[Optional[float]] = []
        self.calls = 0

    def execute(
        self, query: str, *, timeout: Optional[float] = None, reject_runtime_errors: bool = False
    ) -> Any:
        self.calls += 1
        self.queries.append(query)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return self.payload


class FakeLLM:
    """假 LLM 客户端:只实现 :class:`services.intro.LLMClient` 用到的 ``enabled``/``chat``。"""

    def __init__(
        self,
        completion: Any = GOOD_COMPLETION,
        *,
        enabled: bool = True,
        error: Optional[BaseException] = None,
    ):
        self.completion = completion
        self.enabled = enabled
        self.error = error
        self.prompts: list[str] = []
        self.calls = 0

    def chat(self, prompt: str, *, system: str = "") -> Any:
        self.calls += 1
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.completion


def http_request(method: str, path: str, *, query: str = "") -> tuple[int, Any]:
    """直接驱动 ASGI app 走一遍**完整 HTTP 链**(含 FastAPI 解析),返回 ``(状态码, JSON)``。

    仓库没装 httpx/TestClient,所以自己拼最小 scope(同 :mod:`backend.test_collections`)。
    """
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", b"0"),
        ],
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


def add_place(session, *, lat: float = ORIGIN_LAT, lng: float = ORIGIN_LNG, name: str = "人民公园") -> Place:
    """入库一个目的地(``place_id`` 路径要用它的坐标当起点)。"""
    place = Place(
        osm_type="node", osm_id=9001, name=name, lat=lat, lng=lng,
        category="自然风光", origin_city="上海", band="50_100", tags={},
    )
    session.add(place)
    session.commit()
    return place


def seed_cached_stay(
    session,
    *,
    osm_id: int = 77,
    name: str = "已入库旅舍",
    lat: float = 31.2350,
    lng: float = 121.4800,
    price: str = "约¥150-260/晚",
) -> Stay:
    """直接入库一条**已有价格**的住宿(模拟夜间任务预抓 → 路由应走 DB 缓存)。"""
    stay_service.upsert_stays(
        session,
        [{
            "osm_type": "node", "osm_id": osm_id, "name": name, "kind": "hostel",
            "lat": lat, "lng": lng, "tags": {"tourism": "hostel"},
            "distance_km": 0.8, "price_estimate": price, "intro": "夜里预抓的简介。",
        }],
    )
    session.commit()
    return session.scalar(select(Stay).where(Stay.osm_id == osm_id))


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底:任何 requests 调用都视为测试失败(本套单测必须纯 mock)。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture(autouse=True)
def llm_key_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认"未配 key":需要 LLM 的用例再显式注入假 client。"""
    for key in (
        "WHERE2GO_LLM_API_KEY", "WHERE2GO_LLM_PROVIDER", "WHERE2GO_LLM_BASE_URL",
        "WHERE2GO_LLM_MODEL", "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def session(tmp_path):
    """每个用例一个独立的临时 SQLite 库。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'stays_api_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


@pytest.fixture()
def http(session):
    """完整 HTTP 链客户端:把 ``get_session`` 依赖换成用例里的同一个临时库会话。"""
    app.dependency_overrides[get_session] = lambda: session

    def call(path: str, **params: Any) -> tuple[int, Any]:
        return http_request("GET", path, query=urlencode(params) if params else "")

    try:
        yield call
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.fixture()
def fake_overpass(monkeypatch: pytest.MonkeyPatch) -> FakeOverpass:
    """替换 :func:`data_sources.overpass.default_client`(服务层缺省就是拿它检索)。"""
    fake = FakeOverpass(sample_payload())
    monkeypatch.setattr(overpass_module, "default_client", lambda: fake)
    return fake


@pytest.fixture()
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """替换服务层拿 LLM 客户端的入口(``services.stays.default_llm_client``)。"""
    fake = FakeLLM()
    monkeypatch.setattr(stay_service, "default_llm_client", lambda: fake)
    return fake


# --------------------------------------------------------------------------- #
# 1. 路由注册与既有路由零回归
# --------------------------------------------------------------------------- #


def test_stays_route_registered_with_expected_params() -> None:
    paths = app.openapi()["paths"]
    assert "/api/stays" in paths, "GET /api/stays 应已注册(TASK-3a2)"
    assert "get" in paths["/api/stays"], "住宿是只读检索,只该有 GET"
    params = {item["name"] for item in paths["/api/stays"]["get"]["parameters"]}
    assert {"lat", "lng", "place_id", "radius_km", "refresh"} <= params
    assert EXISTING_PATHS <= set(paths), f"既有路由不该被挤掉:{EXISTING_PATHS - set(paths)}"


def test_existing_routes_still_answer_after_mounting_stays(http) -> None:
    """零回归:挂上 /api/stays 之后,既有路由行为不变(收藏列表 + 分类元数据)。"""
    status, body = http("/api/collections")
    assert status == 200 and body["count"] == 0 and body["collections"] == []
    assert "note" in body, "既有路由的 note 口径不能被破坏"
    status, meta = http("/api/places/meta")
    assert status == 200 and meta["categories"], "POC/阶段1 路由应照常可用"


def test_route_module_does_not_touch_network_itself() -> None:
    """薄路由:模块里不出现 HTTP 客户端(触网的是服务层,可整体替换)。"""
    text = (Path(BACKEND_DIR) / "app" / "api" / "stays.py").read_text(encoding="utf-8")
    for forbidden in ("import requests", "from data_sources", "urlopen"):
        assert forbidden not in text, f"住宿路由不该{forbidden}"


# --------------------------------------------------------------------------- #
# 2. 正常查询:出参形状、排序、估算标注
# --------------------------------------------------------------------------- #


def test_list_stays_projects_items_with_estimate_label(http, fake_overpass, fake_llm) -> None:
    status, body = http("/api/stays", lat=ORIGIN_LAT, lng=ORIGIN_LNG)
    assert status == 200, body
    assert set(body) == TOP_LEVEL_KEYS, f"顶层形状不对:{set(body) ^ TOP_LEVEL_KEYS}"
    assert body["lat"] == ORIGIN_LAT and body["lng"] == ORIGIN_LNG
    assert body["radius_km"] == stays_api.DEFAULT_RADIUS_KM, "radius_km 缺省应是 8 km"
    assert body["count"] == 4 == len(body["items"])
    assert body["source"] == stay_service.SOURCE_FETCH, "首次查询应现场检索 Overpass"
    assert [item["name"] for item in body["items"]] == SAMPLE_ORDER, "应由近及远排序"

    first = body["items"][0]
    assert set(first) == ITEM_KEYS, f"单条形状不对:{set(first) ^ ITEM_KEYS}"
    assert first["estimated"] == "AI 预估 · 仅供参考 · 以 OTA 实时为准"
    # TASK-6g:老船长青旅命中 hostel 规则档 → 价格来自规则表,一次 LLM 都没调
    assert first["price_estimate"] == "约¥50-150/晚", "价格是服务层规范化的估算串"
    assert first["price_kind"] == "rule", "出处标记透出:规则表(0 token)"
    assert first["currency"] == DEFAULT_CURRENCY
    assert first["intro"] is None, "规则层不出简介(不为简介烧 token)"
    assert first["osm_type"] == "node" and first["osm_id"] == 2
    assert first["kind"] == "hostel"
    assert first["distance_km"] == pytest.approx(0.79, abs=0.05)
    assert all(item["estimated"] == first["estimated"] for item in body["items"])
    # 估算口径必须在 note 里写明(AI 预估参考价、非实时报价、以 OTA 为准)
    for fragment in ("估算", "OTA", "不是实时报价"):
        assert fragment in body["note"], f"note 应写明 {fragment}"
    assert "price_kind" in body["note"], "note 应写明 price_kind 的口径"
    # 无名公寓没有名称 → 服务层不调 LLM,价格留空但不影响 200
    assert body["items"][1]["price_estimate"] is None
    assert body["items"][1]["price_kind"] is None
    assert fake_llm.calls == 0, "三行有名称的都命中规则表,LLM 一次都不该调"


def test_radius_km_filters_items_and_is_echoed(http, fake_overpass, fake_llm) -> None:
    status, wide = http("/api/stays", lat=ORIGIN_LAT, lng=ORIGIN_LNG, radius_km=8)
    assert status == 200 and wide["radius_km"] == 8.0 and wide["count"] == 4
    status, narrow = http("/api/stays", lat=ORIGIN_LAT, lng=ORIGIN_LNG, radius_km=2)
    assert status == 200 and narrow["radius_km"] == 2.0, narrow
    assert [item["name"] for item in narrow["items"]] == SAMPLE_ORDER[:3], "2 km 外的应被滤掉"


def test_radius_km_converted_to_meters_and_refresh_passed_through(http, session, monkeypatch) -> None:
    """``radius_km`` → 服务层的**米**;``refresh`` 原样透传(路由不自己算距离)。"""
    seen: dict[str, Any] = {}

    def fake_load(sess, lat, lng, *, radius_m, refresh=False):
        seen.update(lat=lat, lng=lng, radius_m=radius_m, refresh=refresh)
        return []

    monkeypatch.setattr(stay_service, "load_or_fetch_stays", fake_load)
    status, body = http(
        "/api/stays", lat=ORIGIN_LAT, lng=ORIGIN_LNG, radius_km="2.5", refresh="true"
    )
    assert status == 200 and body["count"] == 0 and body["items"] == []
    assert seen["radius_m"] == 2500, "radius_km 必须换算成米再交给服务层"
    assert seen["refresh"] is True
    assert (seen["lat"], seen["lng"]) == (ORIGIN_LAT, ORIGIN_LNG)


def test_empty_search_returns_200_with_db_source(http, fake_overpass, fake_llm) -> None:
    """检索不到住宿(或服务层降级)→ 200 + 空列表,不是 500。"""
    fake_overpass.payload = {"elements": []}
    status, body = http("/api/stays", lat=ORIGIN_LAT, lng=ORIGIN_LNG)
    assert status == 200, body
    assert body["count"] == 0 and body["items"] == []
    assert body["source"] == stay_service.SOURCE_DB, "没有可归属的行,按 db 口径报"
    assert body["radius_km"] == stays_api.DEFAULT_RADIUS_KM
    assert fake_llm.calls == 0


def test_overpass_failure_degrades_to_empty_200(http, fake_overpass, fake_llm) -> None:
    fake_overpass.error = RuntimeError("端点全挂")
    status, body = http("/api/stays", lat=ORIGIN_LAT, lng=ORIGIN_LNG)
    assert status == 200 and body["count"] == 0, body


# --------------------------------------------------------------------------- #
# 3. place_id 路径:坐标从 Place 表取
# --------------------------------------------------------------------------- #


def test_place_id_uses_place_coordinates(http, session, fake_overpass, fake_llm) -> None:
    place = add_place(session, lat=31.2350, lng=121.4800)
    status, body = http("/api/stays", place_id=place.id)
    assert status == 200, body
    assert body["lat"] == 31.2350 and body["lng"] == 121.4800, "起点应是该 Place 的坐标"
    assert body["count"] == 4 and body["source"] == stay_service.SOURCE_FETCH
    assert "31.235" in fake_overpass.queries[0], "检索应以 Place 坐标为圆心"
    assert body["items"][0]["name"] in SAMPLE_ORDER


def test_unknown_place_id_returns_404(http, session, fake_overpass) -> None:
    status, body = http("/api/stays", place_id=4242)
    assert status == 404, body
    assert "4242" in body["detail"], "报错要带上出问题的 id"
    assert fake_overpass.calls == 0, "查不到目的地就不该触网"


# --------------------------------------------------------------------------- #
# 4. 校验口径:缺参 / 非法 / 超限一律 400 + 中文报错
# --------------------------------------------------------------------------- #


def test_missing_origin_returns_400_chinese(http, fake_overpass) -> None:
    status, body = http("/api/stays")
    assert status == 400, body
    assert "lat+lng" in body["detail"] and "place_id" in body["detail"], body["detail"]
    assert fake_overpass.calls == 0, "参数不对就不该触网"


@pytest.mark.parametrize(
    "params",
    [
        {"lat": ORIGIN_LAT},
        {"lng": ORIGIN_LNG},
        {"lat": ORIGIN_LAT, "lng": ORIGIN_LNG, "place_id": 1},
    ],
)
def test_half_or_conflicting_origin_returns_400(http, session, params) -> None:
    if "place_id" in params:
        add_place(session)
    status, body = http("/api/stays", **params)
    assert status == 400, body
    assert any("\u4e00" <= char <= "\u9fff" for char in body["detail"]), "报错应是中文"


@pytest.mark.parametrize("radius", [31, 30.5, 999])
def test_radius_over_limit_returns_400(http, radius) -> None:
    status, body = http("/api/stays", lat=ORIGIN_LAT, lng=ORIGIN_LNG, radius_km=radius)
    assert status == 400, body
    assert "上限" in body["detail"] and "30" in body["detail"], body["detail"]


@pytest.mark.parametrize("radius", [0, -3, "abc", "1e999"])
def test_invalid_radius_returns_400(http, radius) -> None:
    status, body = http("/api/stays", lat=ORIGIN_LAT, lng=ORIGIN_LNG, radius_km=radius)
    assert status == 400, body
    assert "radius_km" in body["detail"], body["detail"]


@pytest.mark.parametrize(
    "params",
    [
        {"lat": "abc", "lng": ORIGIN_LNG},
        {"lat": ORIGIN_LAT, "lng": "上海"},
        {"lat": 999, "lng": ORIGIN_LNG},
        {"lat": ORIGIN_LAT, "lng": -181},
        {"place_id": "abc"},
        {"place_id": 0},
        {"place_id": -2},
        {"lat": ORIGIN_LAT, "lng": ORIGIN_LNG, "refresh": "maybe"},
    ],
)
def test_invalid_params_return_400_not_422(http, params) -> None:
    """非法参数一律 **400 中文报错**(不用 pydantic/Query 类型,避免 422 英文报错)。"""
    status, body = http("/api/stays", **params)
    assert status == 400, f"{params} 应 400,实际 {status}:{body}"
    assert any("\u4e00" <= char <= "\u9fff" for char in body["detail"]), body["detail"]


def test_direct_call_with_unresolved_query_defaults_returns_400(session) -> None:
    """单测/脚本直调端点函数时默认值是 ``FieldInfo``:应当成"没给",而不是崩掉。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        stays_api.list_stays(session=session)
    assert caught.value.status_code == 400
    assert "place_id" in str(caught.value.detail)


# --------------------------------------------------------------------------- #
# 5. DB 即缓存 + LLM 降级
# --------------------------------------------------------------------------- #


def test_db_cache_hit_is_offline_and_llm_free(http, session, monkeypatch) -> None:
    """半径内已有行 → 直接读库:零次 Overpass、零次 LLM,``source=db``。"""
    seed_cached_stay(session)
    boom = FakeOverpass(error=AssertionError("命中缓存就不该触网"))
    monkeypatch.setattr(overpass_module, "default_client", lambda: boom)
    llm = FakeLLM()
    monkeypatch.setattr(stay_service, "default_llm_client", lambda: llm)

    status, body = http("/api/stays", lat=ORIGIN_LAT, lng=ORIGIN_LNG, radius_km=8)
    assert status == 200, body
    assert body["source"] == stay_service.SOURCE_DB
    assert body["count"] == 1
    item = body["items"][0]
    assert item["name"] == "已入库旅舍" and item["price_estimate"] == "约¥150-260/晚"
    assert item["estimated"] == stays_api.ESTIMATED_LABEL
    assert item["intro"] == "夜里预抓的简介。"
    assert boom.calls == 0 and llm.calls == 0


def test_refresh_refetches_without_re_estimating(http, fake_overpass, fake_llm) -> None:
    status, first = http("/api/stays", lat=ORIGIN_LAT, lng=ORIGIN_LNG)
    assert status == 200 and first["source"] == stay_service.SOURCE_FETCH
    assert fake_overpass.calls == 1 and fake_llm.calls == 0

    status, cached = http("/api/stays", lat=ORIGIN_LAT, lng=ORIGIN_LNG)
    assert status == 200 and cached["source"] == stay_service.SOURCE_DB
    assert fake_overpass.calls == 1, "未 refresh 时不该再检索"
    assert fake_llm.calls == 0

    status, refreshed = http("/api/stays", lat=ORIGIN_LAT, lng=ORIGIN_LNG, refresh="1")
    assert status == 200 and refreshed["source"] == stay_service.SOURCE_FETCH
    assert fake_overpass.calls == 2, "refresh=true 应强制重抓"
    assert fake_llm.calls == 0, "已有价格的行不该再调 LLM(不重复花 token)"
    assert refreshed["count"] == 4


@pytest.mark.parametrize("kwargs", [{"enabled": False}, {"error": RuntimeError("限流 429")}])
def test_llm_degradation_keeps_200_and_null_price(http, fake_overpass, monkeypatch, kwargs) -> None:
    """未配 key / 限流 → LLM 那一路降级成 null,接口照常 200(估算失败不影响事实字段)。

    TASK-6g:规则层是 0 token 的,没有 key 也照样出价,所以"降级"只降规则未命中的行。
    """
    llm = FakeLLM(**kwargs)
    monkeypatch.setattr(stay_service, "default_llm_client", lambda: llm)
    status, body = http("/api/stays", lat=ORIGIN_LAT, lng=ORIGIN_LNG)
    assert status == 200, body
    assert body["count"] == 4 and body["source"] == stay_service.SOURCE_FETCH
    for item in body["items"]:
        assert item["intro"] is None, "简介只有 LLM 那一路会给"
        assert item["estimated"] == stays_api.ESTIMATED_LABEL, "标注恒定,不因估不出而消失"
        assert item["name"] in SAMPLE_ORDER and item["distance_km"] is not None
    by_name = {item["name"]: item for item in body["items"]}
    assert by_name[""]["price_estimate"] is None and by_name[""]["price_kind"] is None
    assert by_name["老船长青旅"]["price_estimate"] == "约¥50-150/晚"
    assert by_name["老船长青旅"]["price_kind"] == "rule"
    assert by_name["外滩华尔道夫酒店"]["price_estimate"] == "约¥1200-3000/晚"
