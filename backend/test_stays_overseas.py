"""TASK-22 单测(契约 §2 的 5~7 组):境外轨住宿 —— OSM(Overpass)住宿腿 + region 分支。

全程不触网、不调真 LLM(照 :mod:`test_stays_v2` 的套路):

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,偷偷联网当场失败;
* ``llm_key_absent`` 清掉所有 LLM key 环境变量(规则层 0 token 出价的路径照样能测);
* ``inline_background`` 把后台执行器换成同步的,不起真线程;
* Overpass 用 :class:`FakeSession` 替身跑**真实响应形状**(照 ``test_osm_overseas.py``),
  服务层用假 ``fetch_fn`` / monkeypatch ``fetch_stay_pois``;
* Nominatim 逆地理编码(城市懒补)monkeypatch ``stay_service.ds_nominatim_reverse``;
* DB 用 ``tmp_path`` 下的临时 SQLite,不碰 ``backend/data/``。

覆盖:OSM 住宿腿的查询语句与行归一(kind/身份/坐标)、``region`` 只换数据源腿、
负缓存与半径阶梯**共用**(键不含 region)、``stay_city`` 的境外 Nominatim 腿(取值/失败 None/
memo/不为境内行白打)、``/api/stays`` 只多一个 ``region`` 键、前端 ``loadStays`` 接线。

运行:``cd backend && ../.venv/bin/python -m pytest test_stays_overseas.py -q``
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from typing import Any, Optional

import pytest
import requests
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import stays as stays_api  # noqa: E402
from data_sources import DataSourceError, haversine_km  # noqa: E402
from data_sources import nominatim  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db.models import (  # noqa: E402
    AMAP_OSM_TYPE,
    COORD_PRECISION,
    PRICE_KIND_RULE,
    REASON_NO_DATA,
    OSM_SOURCE,
    SOURCE_TAG,
    Stay,
    StayQueryCache,
    amap_osm_id,
)
from services import stays as stay_service  # noqa: E402
from services.region_gate import REGION_DOMESTIC, REGION_OVERSEAS  # noqa: E402

# --------------------------------------------------------------------------- #
# 样本:因特拉肯(2026-10-11 实测起点,WGS-84);住宿按"正北 N 公里"摆放
# --------------------------------------------------------------------------- #

ORIGIN_LAT = 46.6863
ORIGIN_LNG = 7.8632
KM_PER_DEGREE = 111.32

TOP_LEVEL_KEYS = {"lat", "lng", "radius_km", "count", "source", "note", "items"}
ITEM_KEYS = set(stays_api.ITEM_KEYS) | {"city", "estimated"}
LLM_KEY_ENVS = (
    "WHERE2GO_LLM_API_KEY", "ALIBABA_TOKEN_PLAN_API_KEY", "DASHSCOPE_API_KEY",
    "QWEN_API_KEY", "DEEPSEEK_API_KEY",
)


def point_at(km: float) -> tuple[float, float]:
    """起点正北 ``km`` 公里处的 ``(lat, lng)``(7 位定点,与入库口径一致)。"""
    return round(ORIGIN_LAT + km / KM_PER_DEGREE, COORD_PRECISION), ORIGIN_LNG


def node(osm_id: int, km: float, tags: dict[str, Any]) -> dict[str, Any]:
    lat, lng = point_at(km)
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lng, "tags": tags}


def way(osm_id: int, km: float, tags: dict[str, Any]) -> dict[str, Any]:
    """way/relation 只有 ``out center`` 才带 center(解析要兼容)。"""
    lat, lng = point_at(km)
    return {"type": "way", "id": osm_id, "center": {"lat": lat, "lon": lng}, "tags": tags}


def payload(*elements: dict[str, Any]) -> dict[str, Any]:
    return {"version": 0.6, "generator": "Overpass API 0.7.62.11", "elements": list(elements)}


HOTEL = node(1001, 0.6, {
    "tourism": "hotel", "name": "Hotel Interlaken", "stars": "3", "addr:city": "Interlaken",
})
GUEST_HOUSE = way(2002, 1.4, {"tourism": "guest_house", "name": "Pension Alpenblick"})
HOSTEL = node(3003, 2.2, {"tourism": "hostel", "name": "Backpackers Central", "rooms": "8"})
MOTEL = node(4004, 3.0, {"tourism": "motel", "name": "Motel Brunig"})
UNNAMED_FLAT = way(5005, 4.0, {"tourism": "apartment"})
OVERPASS_PAYLOAD = payload(HOTEL, GUEST_HOUSE, HOSTEL, MOTEL, UNNAMED_FLAT)


def osm_row(osm_id: int, km: float, *, name: str = "示例住宿", tourism: str = "hotel",
            osm_type: str = "node", tags: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """一条**境外轨 fetch 行**(:func:`stays.osm_stay_poi` 的输出形状,注入 ``fetch_fn`` 用)。"""
    lat, lng = point_at(km)
    body: dict[str, Any] = {
        "osm_type": osm_type,
        "osm_id": osm_id,
        "name": name,
        "kind": tourism,
        "lat": lat,
        "lng": lng,
        "tags": {**(tags or {}), "tourism": tourism, SOURCE_TAG: OSM_SOURCE,
                 "coord_system": stay_service.OSM_COORD_SYSTEM},
    }
    return body


def amap_row(number: int, km: float, *, name: str = "示例酒店") -> dict[str, Any]:
    """一条**归一化高德 POI**(境内轨 fetch 行的形状,对照用)。"""
    lat, lng = point_at(km)
    return {
        "id": f"B0FFH{number:05d}", "name": name, "lat": lat, "lng": lng,
        "type": "住宿服务;宾馆酒店;星级酒店", "typecode": stay_service.STAY_TYPES,
        "address": "", "cityname": "", "adname": "", "distance_m": int(km * 1000),
    }


@dataclasses.dataclass
class Call:
    """一次 HTTP 调用的关键参数快照。"""

    method: str
    url: str
    params: Optional[dict[str, Any]]
    data: Optional[dict[str, Any]]
    timeout: Optional[float]

    @property
    def query(self) -> str:
        """Overpass 用 POST ``data={"data": <QL>}`` 发查询,这里取出语句本体。"""
        return str((self.data or {}).get("data") or "")


class FakeResponse:
    """最小 response 替身:只需要 ``status_code`` 与 ``text``。"""

    def __init__(self, payload: Any = None, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = "" if payload is None else json.dumps(payload, ensure_ascii=False)


class FakeSession:
    """记录调用参数并按顺序返回预设响应的 session 替身(不触网)。"""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses) or [FakeResponse({})]
        self.calls: list[Call] = []

    def request(self, method: str, url: str, params: Optional[dict[str, Any]] = None,
                data: Optional[dict[str, Any]] = None, timeout: Optional[float] = None,
                headers: Optional[dict[str, str]] = None, **kwargs: Any) -> FakeResponse:
        self.calls.append(Call(method, url, params, data, timeout))
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(response, BaseException):
            raise response
        return response


class FakeFetch:
    """假检索腿(:data:`services.stays.StayFetchFn` 签名):**按半径过滤**,记调用与 kwargs。

    按半径过滤才测得出阶梯扩档("5 km 内没有、10 km 才有");``error`` 让所有请求失败。
    """

    def __init__(self, rows: Optional[list[dict[str, Any]]] = None,
                 *, error: Optional[BaseException] = None):
        self.rows = list(rows or [])
        self.error = error
        self.calls: list[tuple[float, float, Any, dict[str, Any]]] = []

    def __call__(self, lat: float, lng: float, radius_m: Any = None,
                 **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append((lat, lng, radius_m, dict(kwargs)))
        if self.error is not None:
            raise self.error
        if radius_m is None:
            return [dict(item) for item in self.rows]
        limit_km = float(radius_m) / 1000.0
        return [
            dict(item) for item in self.rows
            if haversine_km(lat, lng, float(item["lat"]), float(item["lng"])) <= limit_km
        ]


class ExplodingFetch:
    """已被缓存挡住还调它 = 又触网了,直接失败。"""

    def __call__(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("命中缓存(正/负)就不该再触网")


class RecordingNominatim:
    """:func:`data_sources.nominatim.reverse` 替身:记参数,回预设地址段或抛错。"""

    def __init__(self, address: Optional[dict[str, Any]] = None,
                 *, error: Optional[BaseException] = None):
        self.address = dict(address or {})
        self.error = error
        self.calls: list[tuple[float, float, dict[str, Any]]] = []

    def __call__(self, lat: float, lng: float, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((lat, lng, dict(kwargs)))
        if self.error is not None:
            raise self.error
        return {
            "lat": lat, "lng": lng, "display_name": " somewhere, Schweiz",
            "country_code": "CH", "country": "Schweiz", "address": dict(self.address),
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


@pytest.fixture(autouse=True)
def llm_key_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认"没配 LLM key":规则层(0 token)照旧出价,LLM 腿一次都不调。"""
    for key in LLM_KEY_ENVS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def inline_background() -> Any:
    """后台执行器换成同步的:不起真线程,回填在断言前就跑完。"""
    stay_service.set_background_executor(stay_service.INLINE_EXECUTOR)
    try:
        yield stay_service.INLINE_EXECUTOR
    finally:
        stay_service.set_background_executor(None)


@pytest.fixture(autouse=True)
def city_cache_reset() -> Any:
    """清空 :func:`stays.overseas_city` 的进程内 memo(用例之间不串)。"""
    stay_service.reset_city_lookup_cache()
    yield
    stay_service.reset_city_lookup_cache()


@pytest.fixture()
def session(tmp_path):
    """每个用例一个独立的临时 SQLite 库。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'stays_overseas.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


def all_stays(session: Session) -> list[Stay]:
    return list(session.scalars(select(Stay).order_by(Stay.id)).all())


def all_cache(session: Session) -> list[StayQueryCache]:
    return list(session.scalars(select(StayQueryCache).order_by(StayQueryCache.id)).all())


# --------------------------------------------------------------------------- #
# 5a. OSM 住宿腿:查询语句 + 行归一(契约 §1.4 / §2.5)
# --------------------------------------------------------------------------- #


def test_fetch_stay_pois_osm_queries_single_circle_with_tourism_union() -> None:
    """境外腿 = Overpass **单圆并集** ``tourism ∈ STAY_TAGS``、``require_name=False``、交互超时。"""
    session = FakeSession(FakeResponse(OVERPASS_PAYLOAD))
    rows = stay_service.fetch_stay_pois(
        ORIGIN_LAT, ORIGIN_LNG, 8000, region=REGION_OVERSEAS, session=session
    )
    assert len(session.calls) == 1, "住宿密度低,单组一次请求就够(不像目的地那样拆四组)"
    call = session.calls[0]
    assert call.method == "POST" and call.url.startswith("https://")
    assert call.timeout == stay_service.STAY_REQUEST_TIMEOUT_S, "交互路径超时,不是冷启动那档 150s"
    query = call.query
    assert "[out:json]" in query and f"around:8000,{ORIGIN_LAT:.6f},{ORIGIN_LNG:.6f}" in query
    for tag in stay_service.STAY_TAGS:
        for element in ("node", "way", "relation"):
            assert f'{element}["tourism"="{tag}"]' in query, f"{element}/{tag} 选择器应在并集里"
    assert '["name"]' not in query, "require_name=False:民宿/公寓常没 name,不能被服务端滤掉"
    assert "out center" in query, "way/relation 要靠 out center 才有坐标"
    # 行归一:原生 OSM 身份 + kind(tourism 值)+ 来源/坐标系标注
    by_id = {row["osm_id"]: row for row in rows}
    assert set(by_id) == {1001, 2002, 3003, 4004, 5005}, "无名公寓也要收(require_name=False)"
    assert by_id[1001]["osm_type"] == "node" and by_id[2002]["osm_type"] == "way"
    assert by_id[1001]["kind"] == "hotel" and by_id[2002]["kind"] == "guest_house"
    assert by_id[3003]["kind"] == "hostel" and by_id[5005]["kind"] == "apartment"
    assert by_id[4004]["kind"] == "motel", "tourism 同族写法照收(不归一到 hotel)"
    assert by_id[1001]["tags"][SOURCE_TAG] == OSM_SOURCE
    assert by_id[1001]["tags"]["coord_system"] == "WGS-84"
    assert by_id[1001]["tags"]["addr:city"] == "Interlaken", "OSM 原始 tag 原样保留"
    assert by_id[1001]["name"] == "Hotel Interlaken" and by_id[5005]["name"] == ""
    # 由近及远:parse_places 已按距离排序,服务层不重排(0.6 / 1.4 / 2.2 / 3.0 / 4.0 km)
    assert [row["osm_id"] for row in rows] == [1001, 2002, 3003, 4004, 5005]


def test_fetch_stay_pois_region_branch_switches_only_the_data_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``region`` 只决定数据源腿:境外打 Overpass、境内打高德,互不串门。"""
    overpass_session = FakeSession(FakeResponse(OVERPASS_PAYLOAD))

    def boom_amap(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("region=overseas 不该调高德 place/around")

    monkeypatch.setattr(stay_service.amap, "search_around", boom_amap)
    rows = stay_service.fetch_stay_pois(
        ORIGIN_LAT, ORIGIN_LNG, 8000, region=REGION_OVERSEAS, session=overpass_session
    )
    assert rows and len(overpass_session.calls) == 1
    assert all(row["osm_type"] in ("node", "way") for row in rows), "境外是原生 OSM 身份"

    # 境内:Overpass 一次都不该碰,高德腿照旧(crc32 身份由 stay_row 在编排层补)
    def boom_overpass(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("境内轨不该碰 Overpass")

    monkeypatch.setattr(stay_service.osm_overseas, "OverpassClient", boom_overpass)
    seen: list[dict[str, Any]] = []

    def fake_amap(lat: float, lng: float, **kwargs: Any) -> list[dict[str, Any]]:
        seen.append({"lat": lat, "lng": lng, **kwargs})
        return [amap_row(1, 0.5)]

    monkeypatch.setattr(stay_service.amap, "search_around", fake_amap)
    for region in (None, REGION_DOMESTIC, "junk"):
        domestic = stay_service.fetch_stay_pois(ORIGIN_LAT, ORIGIN_LNG, 8000, region=region)
        assert domestic and domestic[0]["id"] == "B0FFH00001"
    assert len(seen) == 3 and all(item["types"] == stay_service.STAY_TYPES for item in seen)


def test_osm_stay_row_keeps_native_identity_and_computes_distance() -> None:
    """境外行身份**不 crc32**:``(node/way/relation, 原生 id)``;``distance_km`` 按起点现算。"""
    row = stay_service.osm_stay_row(osm_row(1001, 0.6, name="Hotel Interlaken"), ORIGIN_LAT, ORIGIN_LNG)
    assert (row["osm_type"], row["osm_id"]) == ("node", 1001)
    assert row["osm_id"] != amap_osm_id("1001"), "境外轨不走 crc32 哈希身份"
    assert row["kind"] == "hotel" and row["name"] == "Hotel Interlaken"
    assert row["distance_km"] == pytest.approx(0.6, abs=0.02)
    assert stay_service.stay_identity(row) == ("node", 1001)
    # kind 缺失时从 tags 兜(OSM 分支),且不受 relation 的 type=* 干扰
    bare = stay_service.osm_stay_row(
        {"osm_type": "relation", "osm_id": 7, "name": "Chalet", "lat": ORIGIN_LAT, "lng": ORIGIN_LNG,
         "tags": {"tourism": "chalet", "type": "multipolygon"}},
        ORIGIN_LAT, ORIGIN_LNG,
    )
    assert bare["kind"] == "chalet" and bare["osm_type"] == "relation"
    assert stay_service.osm_stay_kind({"tourism": "motel", "type": "multipolygon"}) == "motel"
    assert stay_service.stay_kind({"typecode": "100105", "type": "住宿服务;经济型连锁酒店"}) == "hotel", \
        "境内高德行的 kind 归一逐字不变"


def test_search_stays_overseas_dedupes_and_never_raises() -> None:
    """境外检索:按 ``(osm_type, osm_id)`` 去重;检索腿抛错 → 空列表 + 三档 reason。"""
    duplicated = [osm_row(1001, 0.6), osm_row(1001, 0.6), osm_row(2002, 1.4, osm_type="way",
                                                                  tourism="guest_house")]
    rows, reason = stay_service.search_stays_detailed(
        ORIGIN_LAT, ORIGIN_LNG, 8000, fetch_fn=FakeFetch(duplicated), region=REGION_OVERSEAS
    )
    assert reason is None and [row["osm_id"] for row in rows] == [1001, 2002]
    assert rows[0]["distance_km"] == pytest.approx(0.6, abs=0.02)
    empty, empty_reason = stay_service.search_stays_detailed(
        ORIGIN_LAT, ORIGIN_LNG, 8000, fetch_fn=FakeFetch([]), region=REGION_OVERSEAS
    )
    assert empty == [] and empty_reason == REASON_NO_DATA
    broken, broken_reason = stay_service.search_stays_detailed(
        ORIGIN_LAT, ORIGIN_LNG, 8000,
        fetch_fn=FakeFetch(error=DataSourceError("Overpass", "端点链全挂")), region=REGION_OVERSEAS,
    )
    assert broken == [] and broken_reason == "datasource_error", "失败只降级,绝不抛给调用方"


# --------------------------------------------------------------------------- #
# 5b. 编排层:入库身份 / source / 负缓存与阶梯共用(契约 §1.4 / §2.5)
# --------------------------------------------------------------------------- #


def test_load_stays_overseas_persists_native_rows_and_reports_osm_source(session: Session) -> None:
    """境外抓取入库:原生身份、7 位定点坐标、``source=osm``,规则层照旧 0 token 出价。"""
    fetch = FakeFetch([osm_row(1001, 0.6, name="Hotel Interlaken", tags={"stars": "3"}),
                       osm_row(3003, 2.2, name="Backpackers Central", tourism="hostel")])
    result = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000, fetch_fn=fetch, region=REGION_OVERSEAS
    )
    assert result.reason is None and result.source == stay_service.SOURCE_OSM == "osm"
    assert len(fetch.calls) == 1 and fetch.calls[0][3] == {}, "fetch_fn 注入优先于 region 分支"
    stored = all_stays(session)
    assert [(row.osm_type, row.osm_id) for row in stored] == [("node", 1001), ("node", 3003)]
    assert all(row.lat == round(row.lat, COORD_PRECISION) for row in stored), "坐标定点 7 位"
    assert stored[0].tags[SOURCE_TAG] == OSM_SOURCE and stored[0].tags["stars"] == "3"
    items = {item["name"]: item for item in result.items}
    assert items["Hotel Interlaken"]["price_kind"] == PRICE_KIND_RULE, "星级规则表不分轨"
    assert items["Hotel Interlaken"]["price_estimate"], "规则层 0 token 出价(不必调 LLM)"
    assert items["Backpackers Central"]["kind"] == "hostel"
    assert all(item["source"] == "osm" for item in result.items)


def test_load_stays_negative_cache_is_shared_across_tracks(session: Session) -> None:
    """负缓存**不分轨**:境外写下的空结果,境内同坐标同半径直接命中(零网络)。"""
    empty = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000,
        fetch_fn=FakeFetch([]), region=REGION_OVERSEAS,
    )
    assert empty.items == [] and empty.reason == REASON_NO_DATA
    cached = all_cache(session)
    assert len(cached) == 1, "空结果写一行负缓存"
    assert not any("region" in str(key) for key in cached[0].__table__.columns.keys()), \
        "StayQueryCache 的键不含 region(同一坐标不会既是境内又是境外)"
    # 境内轨(默认)再来一次:命中同一条负缓存,一次网都不触
    domestic = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000, fetch_fn=ExplodingFetch()
    )
    assert domestic.items == [] and domestic.reason == REASON_NO_DATA
    assert domestic.from_cache is True and domestic.source == stay_service.SOURCE_DB
    # 境外轨同样命中(第二遍也零网络)
    again = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000,
        fetch_fn=ExplodingFetch(), region=REGION_OVERSEAS,
    )
    assert again.items == [] and again.from_cache is True and len(all_cache(session)) == 1


def test_load_stays_overseas_radius_ladder_and_db_cache_unchanged(session: Session) -> None:
    """半径阶梯与正缓存共用:没给半径按 5→10→30 km 扩;入库后二查零网络。"""
    far = [osm_row(7007, 7.0, name="Chalet Ausserhalb")]
    fetch = FakeFetch([row for row in far])
    first = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, fetch_fn=fetch, region=REGION_OVERSEAS
    )
    assert [call[2] for call in fetch.calls] == [5000, 10000], "5 km 空 → 扩到 10 km 才有货"
    assert first.radius_m == 10000 and first.expanded is True
    assert first.items[0]["name"] == "Chalet Ausserhalb" and first.source == "osm"
    second = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=10000,
        fetch_fn=ExplodingFetch(), region=REGION_OVERSEAS,
    )
    assert second.from_cache is True and second.source == stay_service.SOURCE_DB
    assert len(second.items) == 1, "正缓存命中:直接读库,零网络零 LLM"


def test_load_stays_domestic_track_is_unchanged(session: Session) -> None:
    """境内轨(缺省 region)零变化:高德行 → ``osm_type=amap`` + crc32 身份 + ``source=amap``。"""
    fetch = FakeFetch([amap_row(1, 0.5, name="汉庭酒店")])
    result = stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000, fetch_fn=fetch)
    assert result.source == stay_service.SOURCE_FETCH == "amap"
    stored = all_stays(session)
    assert [(row.osm_type, row.osm_id) for row in stored] == [
        (AMAP_OSM_TYPE, amap_osm_id("B0FFH00001"))
    ]
    assert stored[0].tags["amap_id"] == "B0FFH00001"
    assert stay_service.resolve_stay_region(None) == REGION_DOMESTIC
    assert stay_service.resolve_stay_region(" OverSeas ") == REGION_OVERSEAS
    assert stay_service.resolve_stay_region("mars") == REGION_DOMESTIC, "认不出按境内,不猜"


# --------------------------------------------------------------------------- #
# 5c. stay_city 的境外腿:Nominatim 懒补(契约 §1.4 / §2.5)
# --------------------------------------------------------------------------- #


def test_stay_city_overseas_falls_back_to_nominatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """境外行缺城市线索 → Nominatim 逆地理编码取 ``address.city||town||village``。"""
    fake = RecordingNominatim({"town": "Grindelwald", "country_code": "ch"})
    monkeypatch.setattr(stay_service, "ds_nominatim_reverse", fake)
    row = stay_service.osm_stay_row(osm_row(2002, 1.4, name="Pension Alpenblick",
                                           tourism="guest_house"), ORIGIN_LAT, ORIGIN_LNG)
    assert stay_service.stay_city(row) == "Grindelwald"
    assert len(fake.calls) == 1
    lat, lng, kwargs = fake.calls[0]
    assert (lat, lng) == (row["lat"], row["lng"]), "按行自己的坐标查,不是起点"
    assert kwargs["include_address"] is True and kwargs["include_country"] is True
    assert kwargs["zoom"] == stay_service.NOMINATIM_CITY_ZOOM
    # city 优先于 town、town 优先于 village
    monkeypatch.setattr(stay_service, "ds_nominatim_reverse",
                        RecordingNominatim({"village": "Saxeten", "town": "Wilderswil",
                                            "city": "Interlaken"}))
    stay_service.reset_city_lookup_cache()
    assert stay_service.overseas_city(46.5, 7.9) == "Interlaken"
    stay_service.reset_city_lookup_cache()
    monkeypatch.setattr(stay_service, "ds_nominatim_reverse",
                        RecordingNominatim({"village": "Saxeten", "town": "Wilderswil"}))
    assert stay_service.overseas_city(46.5, 7.9) == "Wilderswil"


def test_stay_city_overseas_failure_returns_none_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nominatim 挂/限流/查不到 → ``None``(不阻塞、不抛),且失败也 memo(不反复重试)。"""
    for error in (DataSourceError("Nominatim", "逆地理编码失败:Unable to geocode"),
                  AssertionError("单测不允许触网"), TimeoutError("timed out")):
        stay_service.reset_city_lookup_cache()
        fake = RecordingNominatim(error=error)
        monkeypatch.setattr(stay_service, "ds_nominatim_reverse", fake)
        row = stay_service.osm_stay_row(osm_row(2002, 1.4, tourism="guest_house"),
                                        ORIGIN_LAT, ORIGIN_LNG)
        assert stay_service.stay_city(row) is None
        assert stay_service.stay_city(row) is None, "第二次仍 None"
        assert len(fake.calls) == 1, "失败结果也 memo:同一网格不重复打(1 req/s 吃不消)"
        assert stay_service.city_price_factor(row) == stay_service.CITY_TIER_UNKNOWN_FACTOR, \
            "查不到城市按中性系数,价格规则不受影响"
    # 空地址段 / 坐标非法 → 同样 None,连腿都不打
    stay_service.reset_city_lookup_cache()
    empty = RecordingNominatim({})
    monkeypatch.setattr(stay_service, "ds_nominatim_reverse", empty)
    assert stay_service.overseas_city(46.5, 7.9) is None and len(empty.calls) == 1
    assert stay_service.overseas_city(None, 7.9) is None and len(empty.calls) == 1


def test_stay_city_prefers_tags_and_never_calls_nominatim_for_amap_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``addr:city`` 命中就不打 Nominatim;境内高德行/存量 OSM 行**一次都不打**。"""
    fake = RecordingNominatim({"city": "Interlaken"})
    monkeypatch.setattr(stay_service, "ds_nominatim_reverse", fake)
    tagged = stay_service.osm_stay_row(
        osm_row(1001, 0.6, tags={"addr:city": "Interlaken"}), ORIGIN_LAT, ORIGIN_LNG
    )
    assert stay_service.stay_city(tagged) == "Interlaken" and fake.calls == []
    # 境内高德行:tags 里没有 "source": "OSM" 标记 → 不走境外腿
    amap = stay_service.stay_row(amap_row(1, 0.5, name="汉庭酒店"), ORIGIN_LAT, ORIGIN_LNG)
    assert stay_service.is_osm_stay(amap) is False
    assert stay_service.stay_city(amap) is None and fake.calls == []
    # 阶段1 的存量 OSM 行(没有来源标注)也不打:免得为老行白等 1 req/s 的节流
    legacy = {"osm_type": "node", "osm_id": 9, "name": "老数据民宿",
              "lat": ORIGIN_LAT, "lng": ORIGIN_LNG, "tags": {"tourism": "guest_house"}}
    assert stay_service.is_osm_stay(legacy) is False
    assert stay_service.stay_city(legacy) is None and fake.calls == []


def test_overseas_city_memoizes_by_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程内 memo 按 ≈1.1 km 网格:同片区的行只打一次,远处的另算一次。"""
    fake = RecordingNominatim({"town": "Interlaken"})
    monkeypatch.setattr(stay_service, "ds_nominatim_reverse", fake)
    near_a = stay_service.osm_stay_row(osm_row(1, 0.3), ORIGIN_LAT, ORIGIN_LNG)
    near_b = stay_service.osm_stay_row(osm_row(2, 0.9), ORIGIN_LAT, ORIGIN_LNG)
    assert stay_service.stay_city(near_a) == "Interlaken"
    assert stay_service.stay_city(near_b) == "Interlaken"
    assert len(fake.calls) == 1, "同一网格(≈1.1 km)共用一次逆地理编码"
    far_lat, far_lng = point_at(12.0)
    assert stay_service.overseas_city(far_lat, far_lng) == "Interlaken"
    assert len(fake.calls) == 2, "跨网格才再打一次"


def test_nominatim_include_address_is_opt_in() -> None:
    """数据源层的 opt-in(TASK-22):默认三键零变化,``include_address=True`` 才多 ``address``。"""
    raw = {
        "lat": "46.6863", "lon": "7.8632", "display_name": "Interlaken, Schweiz",
        "address": {"town": "Interlaken", "country_code": "ch", "country": "Schweiz"},
    }
    assert set(nominatim.parse_place(raw)) == {"lat", "lng", "display_name"}
    with_country = nominatim.parse_place(raw, include_country=True)
    assert with_country["country_code"] == "CH" and "address" not in with_country
    full = nominatim.parse_place(raw, include_country=True, include_address=True)
    assert full["address"] == {"town": "Interlaken", "country_code": "ch", "country": "Schweiz"}
    assert nominatim.parse_place({"lat": "1", "lon": "2", "display_name": "x"},
                                 include_address=True)["address"] == {}
    assert nominatim.CITY_ADDRESS_KEYS[:3] == stay_service.NOMINATIM_CITY_KEYS


# --------------------------------------------------------------------------- #
# 6. /api/stays 的 region 键(契约 §1.5 / §2.6)
# --------------------------------------------------------------------------- #


def call_api(session: Session, monkeypatch: pytest.MonkeyPatch, **params: Any) -> dict[str, Any]:
    """直调端点函数(仓库没装 httpx/TestClient):参数按**字符串**传,与 HTTP 查询串一致。"""
    fetch = params.pop("fetch", FakeFetch([osm_row(1001, 0.6, name="Hotel Interlaken")]))
    monkeypatch.setattr(stay_service, "fetch_stay_pois", fetch)
    kwargs: dict[str, Any] = {"session": session}
    kwargs.update({key: (value if value is None else str(value)) for key, value in params.items()})
    kwargs.setdefault("lat", str(ORIGIN_LAT))
    kwargs.setdefault("lng", str(ORIGIN_LNG))
    kwargs.setdefault("radius_km", "8")
    return stays_api.list_stays(**kwargs)


def test_api_stays_overseas_adds_only_the_region_key(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """响应**只多一个** ``region`` 键;items 既有键零变化,note 追加境外口径一段。"""
    fetch = FakeFetch([osm_row(1001, 0.6, name="Hotel Interlaken", tags={"stars": "3"}),
                       osm_row(2002, 1.4, name="Pension Alpenblick", tourism="guest_house")])
    body = call_api(session, monkeypatch, fetch=fetch, region="overseas")
    assert set(body) == TOP_LEVEL_KEYS | {"region"}
    assert body["region"] == REGION_OVERSEAS and body["source"] == stay_service.SOURCE_OSM
    assert body["count"] == 2 and fetch.calls[0][3] == {"region": "overseas"}
    assert body["note"].startswith(stays_api.STAYS_NOTE), "境内文案原样在前"
    assert stays_api.STAYS_OVERSEAS_NOTE in body["note"] and "Overpass" in body["note"]
    for item in body["items"]:
        assert set(item) == ITEM_KEYS, f"items 既有键零变化:{set(item) ^ ITEM_KEYS}"
        assert item["osm_type"] in ("node", "way", "relation") and item["estimated"]
    assert body["items"][0]["kind"] == "hotel" and body["items"][0]["price_kind"] == PRICE_KIND_RULE


def test_api_stays_defaults_to_domestic_and_rejects_bad_region(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没给 region → 生效值 ``domestic``(境内文案);非法值 → **400 中文**、一次网都不触。"""
    body = call_api(session, monkeypatch, fetch=FakeFetch([amap_row(1, 0.5, name="汉庭酒店")]))
    assert body["region"] == REGION_DOMESTIC and body["source"] == stay_service.SOURCE_FETCH
    assert stays_api.STAYS_OVERSEAS_NOTE not in body["note"], "境内 note 逐字不变"
    assert all(item["osm_type"] == AMAP_OSM_TYPE for item in body["items"])
    assert call_api(session, monkeypatch, fetch=FakeFetch([]), region=" overseas ")["region"] == \
        REGION_OVERSEAS, "大小写与首尾空白按 /api/places 同口径归一"
    for bad in ("mars", "OVERSEA", "overseas2", "境内"):
        with pytest.raises(HTTPException) as caught:
            call_api(session, monkeypatch, fetch=ExplodingFetch(), region=bad)
        assert caught.value.status_code == 400
        assert str(caught.value.detail) == stays_api.REGION_INVALID_MSG == "region 只支持 domestic 或 overseas"


# --------------------------------------------------------------------------- #
# 7. 前端接线(契约 §1.6 / §2.7)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def html() -> str:
    path = os.path.join(BACKEND_DIR, "app", "static", "index.html")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def js_function(body: str, start_marker: str, end_marker: str) -> str:
    start = body.index(start_marker)
    return body[start:body.index(end_marker, start)]


def test_frontend_stay_url_carries_region_only_in_overseas_mode(html: str) -> None:
    """``loadStays`` 的 URL 拼装:勾了「境外模式」才追加 ``region=overseas``。"""
    section = js_function(html, "async function loadStays(", "function openStaySection(")
    assert 'if(regionOverride) params.set("region","overseas");' in section, \
        "/api/stays 要透传境外开关(后端据此走 OSM 住宿腿)"
    assert '"/api/stays?"+params.toString()' in section, "URL 仍由 URLSearchParams 拼(自动编码)"
    assert '"domestic"' not in section, "不勾时不带 region(默认就是境内高德轨)"
    for field in ("lat", "lng", "radius_km"):
        assert field in section, f"既有参数 {field} 照旧拼"


def test_frontend_stay_cards_are_unchanged_for_overseas_rows(html: str) -> None:
    """住宿卡片零改动(§1.6):OSM 行走的是同一套渲染,只吃既有字段。"""
    card = js_function(html, "function stayCardHtml(", "function stayPriceHtml(")
    for field in ("item.kind", "item.distance_km", "item.name", "item.estimated", "item.currency"):
        assert field in card, f"住宿卡片仍消费既有字段 {field}"
    for identity in ("osm_type", "osm_id", "region"):
        assert identity not in card, f"卡片不依赖身份/口径细节({identity}),境外行照样渲染"
    price = js_function(html, "function stayPriceHtml(", "function renderStayCards(")
    assert "item.price_estimate" in price and "estimating" in price, "价格区口径不变"
    listing = js_function(html, "function renderStayCards(", "// 三档空态")
    assert "result.items" in listing, "列表仍按 items 渲染"
