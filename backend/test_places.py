"""TASK-1a 单测:Place 入库 + (城市, band) 读库不触网 + 检索 API 过滤(TASK-9b 起替身是**高德**)。

全程不触网:

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,任何偷偷联网都会当场失败;
* 抓取/地理编码用替身注入 :mod:`services.place_loader`(``fetcher`` / ``geocoder``),
  抓取替身返回 :func:`data_sources.amap.parse_poi` 形状的**归一化高德 POI**
  (入库身份 ``osm_type="amap"`` + ``osm_id=crc32(POI id)``);
* DB 用 ``tmp_path`` 下的临时 SQLite 文件,不碰 ``backend/data/``。

运行:``python -m pytest backend/ -q``
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import Any, Optional

import pytest
import requests
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import places as places_api  # noqa: E402
from app.main import app  # noqa: E402
from data_sources import amap  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db.models import UNCATEGORIZED, Place, SegmentFetch, amap_osm_id  # noqa: E402
from db import repository as repo  # noqa: E402
from services import place_loader  # noqa: E402
from services.bands import DISTANCE_BANDS, band_radius_m, filter_to_band, in_band  # noqa: E402
from services.categories import CATEGORIES, categorize  # noqa: E402

# --------------------------------------------------------------------------- #
# 样本数据:上海为起点,沿正北方向按公里摆点(1 度纬度 ≈ 111.32 km)。
# 形状 = :func:`data_sources.amap.parse_poi` 的归一化高德 POI(TASK-9b 抓取替身)。
# --------------------------------------------------------------------------- #

SHANGHAI = {"lat": 31.2304, "lng": 121.4737}
KM_PER_DEGREE = 111.32
REQUIRED_PLACE_COLUMNS = {
    "osm_type", "osm_id", "name", "lat", "lng", "category", "intro", "tags", "origin_city", "band",
}


def poi_at(
    km: float,
    *,
    name: str,
    poi_id: str,
    typecode: str = "",
    type_text: str = "",
) -> dict[str, Any]:
    """构造距上海 ``km`` 公里(正北)的归一化高德 POI(坐标 GCJ-02,7 位定点)。"""
    return {
        "id": poi_id,
        "name": name,
        "lat": round(SHANGHAI["lat"] + km / KM_PER_DEGREE, 7),
        "lng": SHANGHAI["lng"],
        "type": type_text,
        "typecode": typecode,
        "address": "",
        "cityname": "上海市",
        "adname": "",
        "distance_m": int(km * 1000),
    }


# typecode/type 只用 docs/TASK-9-CONTRACT.md §1.4 的实测锚点(110000 风景名胜、
# 110101 公园、080106 滑雪场),归类结果由 services.amap_categories.classify_amap_poi 决定。
MOUNTAIN = poi_at(60, name="远山", poi_id="B0FFHMOUNT",
                  typecode="110000", type_text="风景名胜;旅游景点")
OLD_TOWN = poi_at(75, name="古镇", poi_id="B0FFHTOWN",
                  typecode="110000", type_text="风景名胜;旅游景点")
SAMPLE_CANDIDATES = [
    poi_at(20, name="城里公园", poi_id="B0FFHPARK",
           typecode="110101", type_text="风景名胜;公园;公园"),          # 环外:太近
    MOUNTAIN,
    OLD_TOWN,
    poi_at(150, name="下一段雪场", poi_id="B0FFHSKI0",
           typecode="080106", type_text="体育休闲服务;运动场馆;滑雪场"),   # 环外:太远
]


class FakeFetcher:
    """高德抓取替身:记录调用参数(含上限半径),返回预设的归一化 POI。"""

    def __init__(self, payload: Optional[list[dict[str, Any]]] = None) -> None:
        self.payload = SAMPLE_CANDIDATES if payload is None else payload
        self.calls: list[dict[str, Any]] = []

    def __call__(self, lat: float, lng: float, band: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append({"lat": lat, "lng": lng, "band": band["key"], "radius_m": band_radius_m(band)})
        return [dict(row) for row in self.payload]

    @property
    def count(self) -> int:
        return len(self.calls)


class ExplodingFetcher:
    """已入库分段再调它就说明"又触网了",直接失败。"""

    def __call__(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("已入库的 (城市, band) 不应再次抓取")


def exploding_geocoder(city: str) -> dict[str, Any]:
    raise AssertionError(f"不应触发 Nominatim 地理编码:{city}")


class RecordingGeocoder:
    """Nominatim 替身:固定返回上海坐标,并记录被调用的城市。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, city: str) -> dict[str, Any]:
        self.calls.append(city)
        return {"lat": SHANGHAI["lat"], "lng": SHANGHAI["lng"], "display_name": f"{city}市, 中国"}


def expect_http_error(func, status_code: int, *fragments: str) -> HTTPException:
    """断言 ``func()`` 抛出指定状态码的 HTTPException,且 detail 含全部片段。"""
    with pytest.raises(HTTPException) as caught:
        func()
    exc = caught.value
    assert exc.status_code == status_code, f"应为 HTTP {status_code},实际 {exc.status_code}"
    for fragment in fragments:
        assert fragment in str(exc.detail), f"detail 应包含 {fragment!r},实际:{exc.detail}"
    return exc


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
    """每个用例一个独立的临时 SQLite 库。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'places_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


@pytest.fixture()
def offline_sources(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """API 层不接收 fetcher 参数,这里替换模块默认实现(仍是替身,不触网)。"""
    fetcher = FakeFetcher()
    geocoder = RecordingGeocoder()
    monkeypatch.setattr(place_loader, "default_fetcher", lambda lat, lng, band: fetcher(lat, lng, band))
    monkeypatch.setattr(place_loader, "default_geocoder", geocoder)
    return SimpleNamespace(fetcher=fetcher, geocoder=geocoder)


def load(session, *, city="上海", band="50_100", category=None, fetcher=None, geocoder=None, **kwargs):
    """便捷封装:默认注入替身,避免用例里重复写参数。"""
    return place_loader.load_segment(
        session,
        city=city,
        band=band,
        category=category,
        fetcher=fetcher or FakeFetcher(),
        geocoder=geocoder or RecordingGeocoder(),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 表结构与防重
# --------------------------------------------------------------------------- #


def test_place_table_has_required_columns_and_unique_key(session) -> None:
    columns = {column.name for column in Place.__table__.columns}
    assert REQUIRED_PLACE_COLUMNS <= columns, f"Place 缺字段:{REQUIRED_PLACE_COLUMNS - columns}"
    unique = {
        frozenset(column.name for column in constraint.columns)
        for constraint in Place.__table__.constraints
        if type(constraint).__name__ == "UniqueConstraint"
    }
    assert frozenset({"osm_type", "osm_id", "origin_city"}) in unique, \
        f"唯一键应覆盖 (osm_type, osm_id, origin_city),实际:{unique}"

    # DB 层防重:同 (osm_type, osm_id, origin_city) 直插两行必须被唯一键挡住
    session.add(Place(osm_type="node", osm_id=1, name="甲", lat=31.0, lng=121.0,
                      category="自然风光", tags={}, origin_city="上海", band="50_100"))
    session.flush()
    session.add(Place(osm_type="node", osm_id=1, name="乙", lat=31.5, lng=121.5,
                      category="自然风光", tags={}, origin_city="上海", band="50_100"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# --------------------------------------------------------------------------- #
# 抓取入库
# --------------------------------------------------------------------------- #


def test_load_segment_fetches_by_upper_radius_and_filters_ring(session) -> None:
    fetcher = FakeFetcher()
    geocoder = RecordingGeocoder()
    outcome = place_loader.load_segment(
        session, city="上海", band="50_100", fetcher=fetcher, geocoder=geocoder
    )

    assert geocoder.calls == ["上海"], "未入库时应先用 Nominatim 解析起点"
    assert fetcher.count == 1
    assert fetcher.calls[0]["radius_m"] == 100_000.0, "应按分段上限半径检索"
    assert fetcher.calls[0]["band"] == "50_100"

    # 环外(20 km 太近 / 150 km 太远)被 haversine 过滤掉,只剩环内两条,按距离升序
    assert [place["name"] for place in outcome.places] == ["远山", "古镇"]
    assert all(50 <= place["distance_km"] < 100 for place in outcome.places)
    assert outcome.source == "amap" and outcome.network_used is True
    assert outcome.written == 2
    assert outcome.counts_by_source == {"高德": 2, "OSM": 0, "种子": 0}, \
        "来源标注随数据源改成「高德」(三个键恒在,前端不必判空)"

    rows = list(session.scalars(select(Place)))
    # 入库身份(§3 TASK-9b 硬约束):osm_type="amap" + osm_id=crc32(高德 POI id)
    assert {(row.osm_type, row.osm_id) for row in rows} == {
        ("amap", amap_osm_id(MOUNTAIN["id"])), ("amap", amap_osm_id(OLD_TOWN["id"])),
    }
    assert all(row.origin_city == "上海" and row.band == "50_100" for row in rows)
    by_name = {row.name: row for row in rows}
    assert by_name["远山"].category == "自然风光"
    assert by_name["远山"].tags == {
        "source": "高德", "amap_id": "B0FFHMOUNT",
        "typecode": "110000", "type": "风景名胜;旅游景点",
    }, "tags 只写来源/POI id 原文/高德分类原文(表结构零改动)"
    assert by_name["远山"].intro is None, "简介留给 LLM 事后补"
    assert by_name["古镇"].category == "小城人文美食"

    record = session.scalar(select(SegmentFetch))
    assert record is not None and record.place_count == 2 and record.source == "amap"
    assert (record.origin_lat, record.origin_lng) == (SHANGHAI["lat"], SHANGHAI["lng"])


def test_second_query_reads_db_without_touching_network(session) -> None:
    fetcher = FakeFetcher()
    first = place_loader.load_segment(
        session, city="上海", band="50_100", fetcher=fetcher, geocoder=RecordingGeocoder()
    )
    second = place_loader.load_segment(
        session, city="上海", band="50_100", fetcher=ExplodingFetcher(), geocoder=exploding_geocoder
    )

    assert first.source == "amap" and first.network_used is True
    assert second.source == "db" and second.network_used is False
    assert fetcher.count == 1, "二次查询不应再抓一次"
    assert [place["name"] for place in second.places] == [place["name"] for place in first.places]
    assert second.fetched_at == first.fetched_at
    assert second.origin == first.origin, "读库时应沿用入库时的起点坐标"


def test_refresh_forces_refetch_and_upserts_without_duplicates(session) -> None:
    fetcher = FakeFetcher()
    place_loader.load_segment(session, city="上海", band="50_100", fetcher=fetcher, geocoder=RecordingGeocoder())
    outcome = place_loader.load_segment(
        session, city="上海", band="50_100", fetcher=fetcher, geocoder=exploding_geocoder, refresh=True
    )

    assert fetcher.count == 2 and outcome.source == "amap"
    assert len(list(session.scalars(select(Place)))) == 2, "重抓应按唯一键 upsert,不产生重复行"
    assert len(list(session.scalars(select(SegmentFetch)))) == 1


def test_refetch_keeps_generated_intro(session) -> None:
    fetcher = FakeFetcher()
    place_loader.load_segment(session, city="上海", band="50_100", fetcher=fetcher, geocoder=RecordingGeocoder())
    row = session.scalar(select(Place).where(Place.name == "远山"))
    row.intro = "一句话简介(TASK-1b 由 LLM 生成并缓存)"
    session.commit()

    place_loader.load_segment(
        session, city="上海", band="50_100", fetcher=fetcher, geocoder=exploding_geocoder, refresh=True
    )
    assert session.scalar(select(Place).where(Place.name == "远山")).intro.startswith("一句话简介")


def test_each_band_is_fetched_and_stored_separately(session) -> None:
    fetcher = FakeFetcher()
    near = place_loader.load_segment(
        session, city="上海", band="50_100", fetcher=fetcher, geocoder=RecordingGeocoder()
    )
    far = place_loader.load_segment(
        session, city="上海", band="100_200", fetcher=fetcher, geocoder=exploding_geocoder
    )

    assert [row["radius_m"] for row in fetcher.calls] == [100_000.0, 200_000.0]
    # 换分段复用库里已有的起点(Nominatim ≤1 req/s,且各段范围圈同心)
    assert [place["name"] for place in near.places] == ["远山", "古镇"]
    assert [place["name"] for place in far.places] == ["下一段雪场"]
    assert far.places[0]["category"] == "滑雪场"
    bands_in_db = {(row.origin_city, row.band) for row in session.scalars(select(SegmentFetch))}
    assert bands_in_db == {("上海", "50_100"), ("上海", "100_200")}


def test_given_coordinates_skip_geocoding(session) -> None:
    fetcher = FakeFetcher()
    outcome = place_loader.load_segment(
        session,
        city="上海",
        band="50_100",
        lat=SHANGHAI["lat"],
        lng=SHANGHAI["lng"],
        fetcher=fetcher,
        geocoder=exploding_geocoder,
    )
    assert outcome.origin["lat"] == SHANGHAI["lat"] and outcome.network_used is True
    assert fetcher.count == 1
    assert [place["name"] for place in outcome.places] == ["远山", "古镇"]


def test_seed_place_without_osm_id_gets_stable_identity(session) -> None:
    seeds = [{"name": "室内滑雪场", "lat": 31.8, "lng": 121.5, "tags": {"piste:type": "downhill"}}]
    fetcher = FakeFetcher(seeds)
    place_loader.load_segment(
        session, city="上海", band="50_100", lat=SHANGHAI["lat"], lng=SHANGHAI["lng"], fetcher=fetcher
    )
    place_loader.load_segment(
        session, city="上海", band="50_100", lat=SHANGHAI["lat"], lng=SHANGHAI["lng"],
        fetcher=fetcher, refresh=True,
    )
    rows = list(session.scalars(select(Place)))
    assert len(rows) == 1, "无 OSM id 的种子数据也要能防重"
    assert rows[0].osm_type == "point" and rows[0].osm_id < 0
    assert rows[0].category == "滑雪场"

    first, second = (place_loader.place_identity(seed) for seed in seeds + seeds[:1])
    assert first == second, "指纹必须确定性(不能随进程变化)"


def test_loader_rejects_bad_band_and_city(session) -> None:
    with pytest.raises(ValueError, match="未知距离分段"):
        place_loader.load_segment(session, city="上海", band="0_10", fetcher=ExplodingFetcher())
    with pytest.raises(ValueError, match="起点城市不能为空"):
        place_loader.load_segment(session, city="  ", band="50_100", fetcher=ExplodingFetcher())
    with pytest.raises(ValueError, match="成对"):
        place_loader.resolve_origin("上海", lat=31.0)


# --------------------------------------------------------------------------- #
# 环形过滤 / 归类(纯函数)
# --------------------------------------------------------------------------- #


def test_filter_to_band_keeps_only_ring_and_sorts_by_distance() -> None:
    band = next(item for item in DISTANCE_BANDS if item["low"] > 0)
    kept = filter_to_band(SAMPLE_CANDIDATES, SHANGHAI["lat"], SHANGHAI["lng"], band)
    assert [row["name"] for row in kept] == ["远山", "古镇"]
    assert all(in_band(row["distance_km"], band) for row in kept)
    assert band_radius_m(band) == band["high"] * 1000
    assert in_band(50, band) and not in_band(100, band), "环为 [low, high),互斥"


def test_categorize_simple_rules() -> None:
    assert categorize({"natural": "peak", "tourism": "attraction"}) == "自然风光", "自然线索优先,避免 POC 的重复"
    assert categorize({"tourism": "viewpoint"}) == "自然风光"
    assert categorize({"tourism": "attraction", "historic": "yes"}) == "小城人文美食"
    assert categorize({"amenity": "restaurant", "cuisine": "noodle"}) == "小城人文美食"
    assert categorize({"piste:type": "downhill"}) == "滑雪场"
    assert categorize({"leisure": "sports_centre", "sport": "skiing"}) == "滑雪场"
    assert categorize({"sport": "climbing"}) == "运动"
    assert categorize({"leisure": "stadium"}) == "运动"
    assert categorize({"shop": "bakery"}) == UNCATEGORIZED
    assert categorize(None) == UNCATEGORIZED
    assert {item["key"] for item in CATEGORIES} >= {"自然风光", "小城人文美食", "滑雪场", "运动"}


def test_amap_poi_shape_is_what_the_loader_consumes() -> None:
    """place_loader 吃的就是 :func:`amap.parse_poi` 的归一化形状(带 id 才能派生入库身份)。"""
    payload = {"status": "1", "infocode": "10000", "pois": [
        {"id": "B0FFHTOWN", "name": "古镇", "location": "121.4737000,31.9032000",
         "type": "风景名胜;旅游景点", "typecode": "110000", "address": "某路 1 号",
         "cityname": "上海市", "adname": "青浦区", "distance": "75000"},
    ]}
    pois = amap.parse_pois(payload)
    assert set(pois[0]) == {
        "id", "name", "lat", "lng", "type", "typecode", "address", "cityname", "adname", "distance_m",
    }, "归一化形状不能变(place_loader/to_place_items 依赖它)"
    items = place_loader.to_place_items(pois)
    assert (items[0]["osm_type"], items[0]["osm_id"]) == ("amap", amap_osm_id("B0FFHTOWN"))
    assert items[0]["tags"]["amap_id"] == "B0FFHTOWN", "原始 POI id 必须留在 tags 里"


# --------------------------------------------------------------------------- #
# 检索 API
# --------------------------------------------------------------------------- #


def test_api_places_payload_shape(session, offline_sources) -> None:
    payload = places_api.list_places(origin="上海", band="50_100", category=None, lat=None, lng=None,
                                    refresh=False, session=session)
    assert offline_sources.geocoder.calls == ["上海"]
    assert payload["count"] == 2 and payload["source"] == "amap" and payload["network_used"] is True
    assert payload["origin"] == {"city": "上海", "name": "上海市, 中国", **SHANGHAI}
    assert payload["band"] == {"key": "50_100", "label": "50-100 km", "low_km": 50, "high_km": 100}
    assert payload["counts_by_category"] == {"自然风光": 1, "小城人文美食": 1}
    place = payload["places"][0]
    assert REQUIRED_PLACE_COLUMNS <= set(place) and "distance_km" in place
    assert place["name"] == "远山" and place["distance_km"] == pytest.approx(60.1, abs=0.5)

    cached = places_api.list_places(origin="上海", band="50_100", category=None, lat=None, lng=None,
                                    refresh=False, session=session)
    assert cached["source"] == "db" and cached["network_used"] is False
    assert offline_sources.fetcher.count == 1 and offline_sources.geocoder.calls == ["上海"]
    assert [row["name"] for row in cached["places"]] == ["远山", "古镇"]


def test_api_places_filters_by_category_and_band(session, offline_sources) -> None:
    places_api.list_places(origin="上海", band="50_100", category=None, lat=None, lng=None,
                           refresh=False, session=session)
    places_api.list_places(origin="上海", band="100_200", category=None, lat=None, lng=None,
                           refresh=False, session=session)

    nature = places_api.list_places(origin="上海", band="50_100", category="自然风光", lat=None, lng=None,
                                    refresh=False, session=session)
    assert [row["name"] for row in nature["places"]] == ["远山"]
    assert nature["category"] == "自然风光"
    assert nature["source"] == "db" and nature["network_used"] is False, "换分类查询也应读库"
    assert nature["counts_by_category"] == {"自然风光": 1, "小城人文美食": 1}, "分类计数不受过滤影响"

    ski = places_api.list_places(origin="上海", band="100_200", category="滑雪场", lat=None, lng=None,
                                 refresh=False, session=session)
    assert [row["name"] for row in ski["places"]] == ["下一段雪场"]
    assert ski["band"]["low_km"] == 100 and ski["band"]["high_km"] == 200

    empty = places_api.list_places(origin="上海", band="50_100", category="运动", lat=None, lng=None,
                                   refresh=False, session=session)
    assert empty["count"] == 0 and empty["places"] == []
    assert offline_sources.fetcher.count == 2, "只有两个分段各抓一次"


def test_api_places_rejects_bad_input(session, offline_sources) -> None:
    expect_http_error(
        lambda: places_api.list_places(origin="上海", band="0_10", category=None, lat=None, lng=None,
                                       refresh=False, session=session),
        400, "未知距离分段", "50_100",
    )
    expect_http_error(
        lambda: places_api.list_places(origin="上海", band="50_100", category="美食", lat=None, lng=None,
                                       refresh=False, session=session),
        400, "未知分类", "自然风光",
    )
    expect_http_error(
        lambda: places_api.list_places(origin="   ", band="50_100", category=None, lat=None, lng=None,
                                       refresh=False, session=session),
        400, "起点城市不能为空",
    )
    assert offline_sources.fetcher.count == 0, "参数非法时应快速失败,不触网"


def test_api_places_reports_upstream_failure_in_chinese(session, monkeypatch) -> None:
    from data_sources import DataSourceError

    def broken_fetcher(lat: float, lng: float, band: dict[str, Any]) -> list[dict[str, Any]]:
        raise DataSourceError("amap", "未配置 WHERE2GO_AMAP_KEY(高德 Web 服务 key)")

    monkeypatch.setattr(place_loader, "default_fetcher", broken_fetcher)
    monkeypatch.setattr(place_loader, "default_geocoder", RecordingGeocoder())
    expect_http_error(
        lambda: places_api.list_places(origin="上海", band="50_100", category=None, lat=None, lng=None,
                                       refresh=False, session=session),
        502, "目的地检索失败", "高德",
    )


def test_api_geocode_returns_origin_and_cached_segments(session, offline_sources) -> None:
    payload = places_api.geocode_city(city="上海", session=session)
    assert payload["origin"] == {"city": "上海", "name": "上海市, 中国", **SHANGHAI}
    assert [band["key"] for band in payload["bands"]] == ["0_50", "50_100", "100_200", "200_300", "300_500"]
    assert payload["segments"] == []

    places_api.list_places(origin="上海", band="50_100", category=None, lat=None, lng=None,
                           refresh=False, session=session)
    payload = places_api.geocode_city(city="上海", session=session)
    assert [(row["band"], row["place_count"]) for row in payload["segments"]] == [("50_100", 2)]

    expect_http_error(lambda: places_api.geocode_city(city="  ", session=session), 400, "城市名不能为空")


def test_api_places_meta_exposes_bands_and_categories() -> None:
    meta = places_api.places_meta()
    assert [band["key"] for band in meta["bands"]] == ["0_50", "50_100", "100_200", "200_300", "300_500"]
    assert all({"low", "high", "label", "key"} <= set(band) for band in meta["bands"])
    keys = [item["key"] for item in meta["categories"]]
    assert keys[:4] == ["自然风光", "小城人文美食", "滑雪场", "运动"]
    assert all(item["color"].startswith("#") and item["emoji"] for item in meta["categories"])
    assert meta["search_tags"] == place_loader.SEARCH_TAGS


def test_app_keeps_poc_routes_and_mounts_places_routes() -> None:
    paths = set(app.openapi()["paths"])
    assert {"/api/discover", "/api/categories"} <= paths, "POC 路由不能被破坏"
    assert {"/api/places", "/api/places/meta", "/api/geocode"} <= paths
    params = {item["name"] for item in app.openapi()["paths"]["/api/places"]["get"]["parameters"]}
    assert {"origin", "band", "category"} <= params, "检索 API 必须支持 origin/band/category"


def test_repository_counts_and_overview(session) -> None:
    load(session)
    assert repo.count_by_category(session, origin_city="上海", band="50_100") == {
        "自然风光": 1, "小城人文美食": 1,
    }
    overview = repo.segment_overview(session, origin_city="上海")
    assert [(row["band"], row["place_count"], row["source"]) for row in overview] == [("50_100", 2, "amap")]
    assert overview[0]["fetched_at"], "水位应带抓取时间(ISO8601)"
    rows = repo.list_places(session, origin_city="上海", band="50_100",
                            origin_lat=SHANGHAI["lat"], origin_lng=SHANGHAI["lng"], limit=1)
    assert [row["name"] for row in rows] == ["远山"]
