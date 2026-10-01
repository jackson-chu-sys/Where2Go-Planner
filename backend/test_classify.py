"""TASK-1b 单测:四分类优先级归类 + 跨 tag 去重 + 检索并集 + LLM 简介(mock)。

全程不触网:

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,任何偷偷联网都会当场失败;
* 归类/去重/检索语句构造都是纯函数,喂**构造好的 elements** 即可;
* LLM 用替身(:class:`FakeLLM`)注入,既不联网也不依赖环境里有没有 key;
* DB 用 ``tmp_path`` 下的临时 SQLite,不碰 ``backend/data/``。

运行:``python -m pytest backend/ -q``
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from typing import Any, Optional

import pytest
import requests
from fastapi import HTTPException
from sqlalchemy import select

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import discover as discover_api  # noqa: E402
from app.api import places as places_api  # noqa: E402
from data_sources import DataSourceError  # noqa: E402
from data_sources import amap, haversine_km  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db import repository as repo  # noqa: E402
from db.models import (  # noqa: E402
    AMAP_OSM_TYPE,
    AMAP_SOURCE,
    UNCATEGORIZED,
    Place,
    amap_osm_id,
)
from services import amap_categories  # noqa: E402
from services import classify, intro as intro_service, place_loader, reclassify  # noqa: E402
from services.bands import DISTANCE_BANDS, band_inner_radius_m, band_radius_m  # noqa: E402
from services.classify import (  # noqa: E402
    CATEGORY_CULTURE,
    CATEGORY_NATURE,
    CATEGORY_PRIORITY,
    CATEGORY_SKI,
    CATEGORY_SPORT,
    categorize,
    classify_places,
    dedupe_places,
)

# --------------------------------------------------------------------------- #
# 构造样本(不触网):归一化高德 POI / 遗留 OSM 风格 elements(种子与存量行仍走 tag 口径)
# --------------------------------------------------------------------------- #

SHANGHAI = {"lat": 31.2304, "lng": 121.4737}
KM_PER_DEGREE = 111.32
LLM_KEY_ENVS = (
    "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "QWEN_API_KEY",
    "ALIBABA_TOKEN_PLAN_API_KEY", intro_service.ENV_API_KEY,
)


def element(osm_type: str, osm_id: int, name: str, **tags: Any) -> dict[str, Any]:
    """构造一条"已被多组 tag 命中"的 Overpass 风格地物。"""
    return {
        "osm_type": osm_type, "osm_id": osm_id, "name": name,
        "lat": 31.5, "lng": 121.5, "tags": dict(tags),
    }


def point_at(km: float, *, name: str, tags: Optional[dict[str, Any]] = None,
             osm_type: str = "node", osm_id: int = 1) -> dict[str, Any]:
    """构造距上海 ``km`` 公里(正北)的环内候选点。"""
    return {
        "osm_type": osm_type, "osm_id": osm_id, "name": name,
        "lat": round(SHANGHAI["lat"] + km / KM_PER_DEGREE, 6), "lng": SHANGHAI["lng"],
        "tags": dict(tags or {}),
    }


RING_CANDIDATES = [
    point_at(60, name="远山", tags={"natural": "peak", "ele": "309"}, osm_id=1),
    point_at(75, name="古镇", tags={"tourism": "attraction", "historic": "yes"}, osm_type="way", osm_id=2),
]


class FakeFetcher:
    """Overpass 替身:记录调用(含上限半径),返回预设候选。"""

    def __init__(self, payload: Optional[list[dict[str, Any]]] = None) -> None:
        self.payload = RING_CANDIDATES if payload is None else payload
        self.calls: list[dict[str, Any]] = []

    def __call__(self, lat: float, lng: float, band: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append({"radius_m": band_radius_m(band), "band": band["key"]})
        return [dict(row) for row in self.payload]


class RecordingGeocoder:
    """Nominatim 替身:固定返回上海坐标。"""

    def __call__(self, city: str) -> dict[str, Any]:
        return {"lat": SHANGHAI["lat"], "lng": SHANGHAI["lng"], "display_name": f"{city}市, 中国"}


def poi_at_km(
    km: float,
    name: str,
    id_text: str,
    *,
    typecode: str = "110000",
    type_text: str = "风景名胜;旅游景点",
) -> dict[str, Any]:
    """距上海 ``km`` 公里(正北)的**归一化高德 POI**(:func:`data_sources.amap.parse_poi` 形状)。

    typecode/type 只用 ``docs/TASK-9-CONTRACT.md`` §1.4 的实测锚点(禁凭记忆编造中类码)。
    """
    return {
        "id": id_text,
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


def raw_poi(
    id_text: str,
    name: str,
    lng: float,
    lat: float,
    type_text: str,
    typecode: str,
    **extra: Any,
) -> dict[str, Any]:
    """高德 v3 响应里 ``pois[]`` 的**原始**形状(``location`` 是 "lng,lat" 字符串)。"""
    body: dict[str, Any] = {
        "id": id_text, "name": name, "location": f"{lng},{lat}",
        "type": type_text, "typecode": typecode, "address": "", "adname": "",
    }
    body.update(extra)
    return body


def v3_payload(pois: list[dict[str, Any]]) -> dict[str, Any]:
    """高德 v3 成功响应外壳(**HTTP 码恒 200**,成败看 status/infocode)。"""
    return {"status": "1", "infocode": "10000", "info": "OK",
            "count": str(len(pois)), "pois": list(pois)}


class FakeAmapSearch:
    """高德检索替身:签名与 :func:`amap.search_around` / :func:`amap.search_polygon` 一致。

    * 圆形检索按 ``radius_m`` 过滤、多边形检索按格子包围盒过滤(高德只在范围内返回);
    * 每次调用只回 ``offset`` 条(按页切 ``rows``),所以配额没满时
      :func:`place_loader._paged` 会翻页 —— 正好验「配额抓满即停」与页数上限;
    * ``error`` 让所有请求都抛错(验降级)。
    """

    def __init__(self, rows: Optional[list[dict[str, Any]]] = None,
                 *, error: Optional[BaseException] = None) -> None:
        self.rows = list(rows or [])
        self.error = error
        self.around_calls: list[dict[str, Any]] = []
        self.polygon_calls: list[dict[str, Any]] = []

    @property
    def calls(self) -> int:
        return len(self.around_calls) + len(self.polygon_calls)

    def _page(self, page: int, offset: int) -> list[dict[str, Any]]:
        if self.error is not None:
            raise self.error
        start = (int(page) - 1) * int(offset)
        return [dict(row) for row in self.rows[start:start + int(offset)]]

    def search_around(self, lat: float, lng: float, *, radius_m: Any = amap.AUTO_MAX_RADIUS_M,
                      types: Any = None, keywords: Any = None, page: int = 1,
                      offset: int = amap.PAGE_SIZE, environ: Any = None,
                      session: Any = None) -> list[dict[str, Any]]:
        self.around_calls.append({
            "lat": lat, "lng": lng, "radius_m": radius_m, "types": types, "keywords": keywords,
            "page": page, "offset": offset, "environ": environ, "session": session,
        })
        limit_km = min(float(radius_m), float(amap.AUTO_MAX_RADIUS_M)) / 1000.0
        return [
            row for row in self._page(page, offset)
            if haversine_km(float(lat), float(lng), row["lat"], row["lng"]) <= limit_km
        ]

    def search_polygon(self, polygon: Any, *, types: Any = None, keywords: Any = None,
                       page: int = 1, offset: int = amap.PAGE_SIZE, environ: Any = None,
                       session: Any = None) -> list[dict[str, Any]]:
        points = [(float(point[0]), float(point[1])) for point in polygon]
        self.polygon_calls.append({
            "polygon": points, "types": types, "keywords": keywords,
            "page": page, "offset": offset, "environ": environ, "session": session,
        })
        lngs = [point[0] for point in points]
        lats = [point[1] for point in points]
        return [
            row for row in self._page(page, offset)
            if min(lngs) <= row["lng"] <= max(lngs) and min(lats) <= row["lat"] <= max(lats)
        ]


class FakeLLM:
    """LLM 替身:记录 prompt;可切换成抛错,用来验证"失败降级为空简介"。"""

    def __init__(self, reply: str = "适合周末半日登高远眺。", error: Optional[Exception] = None) -> None:
        self.reply = reply
        self.error = error
        self.prompts: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    @property
    def label(self) -> str:
        return "假 LLM · fake-model"

    def chat(self, prompt: str, *, system: str = "") -> str:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.reply


class ExplodingLLM(FakeLLM):
    """被调用即失败:用来证明"命中库直接读库"时不会再调 LLM。"""

    def chat(self, prompt: str, *, system: str = "") -> str:
        raise AssertionError("读库路径不应调用 LLM")


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)


class FakeSession:
    """requests.Session 替身:记录每次调用(用于断言"一次请求查完并集")。"""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, params: Any = None, data: Any = None,
                timeout: Any = None, headers: Any = None) -> FakeResponse:
        # 高德 v3 走 GET 查询串 ``params``;LLM 走 JSON 字节串 —— 两种都原样记下。
        self.calls.append({"method": method, "url": url, "params": dict(params or {}),
                           "data": data,
                           "query": data.get("data") if isinstance(data, Mapping) else None,
                           "timeout": timeout, "headers": dict(headers or {})})
        return FakeResponse(self.payload)


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
    engine = make_engine(f"sqlite:///{tmp_path / 'classify_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


@pytest.fixture()
def fake_amap(monkeypatch: pytest.MonkeyPatch) -> FakeAmapSearch:
    """把 :mod:`data_sources.amap` 的两个检索函数换成替身(纯本地,零 HTTP)。"""
    fake = FakeAmapSearch()
    monkeypatch.setattr(amap, "search_around", fake.search_around)
    monkeypatch.setattr(amap, "search_polygon", fake.search_polygon)
    return fake


@pytest.fixture()
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """把 :func:`services.intro.default_client` 换成替身(不依赖环境里的 key)。"""
    client = FakeLLM()
    monkeypatch.setattr(intro_service, "default_client", lambda: client)
    return client


def seed(session, *, category: str, tags: dict[str, Any], osm_id: int, name: str,
         intro: Optional[str] = None, city: str = "上海", band: str = "50_100") -> Place:
    row = Place(osm_type="node", osm_id=osm_id, name=name, lat=31.5, lng=121.5,
                category=category, tags=tags, intro=intro, origin_city=city, band=band)
    session.add(row)
    session.commit()
    return row


# --------------------------------------------------------------------------- #
# 1. 四分类识别(STAGE1-PLAN 第 3 节的 OSM tag 线索)
# --------------------------------------------------------------------------- #


def test_categorize_recognizes_nature_clues() -> None:
    for tags in ({"natural": "peak"}, {"natural": "waterfall"}, {"natural": "water"},
                 {"leisure": "nature_reserve"}, {"leisure": "park"}, {"waterway": "waterfall"},
                 {"tourism": "viewpoint"}, {"place": "island"}):
        assert categorize(tags) == CATEGORY_NATURE, tags


def test_categorize_recognizes_culture_and_food_clues() -> None:
    for tags in ({"historic": "castle"}, {"historic": "yes", "tourism": "attraction"},
                 {"tourism": "museum"}, {"amenity": "restaurant", "cuisine": "noodle"},
                 {"amenity": "cafe"}, {"place": "town"}, {"place": "village"}):
        assert categorize(tags) == CATEGORY_CULTURE, tags


def test_categorize_recognizes_ski_clues() -> None:
    for tags in ({"piste:type": "downhill"}, {"piste:type": "nordic", "piste:difficulty": "easy"},
                 {"sport": "skiing"}, {"ski": "yes"}, {"landuse": "winter_sports"},
                 {"leisure": "sports_centre", "sport": "skiing"}, {"sport": "snowboard"}):
        assert categorize(tags) == CATEGORY_SKI, tags


def test_categorize_recognizes_sport_clues() -> None:
    for tags in ({"sport": "climbing"}, {"sport": "cycling"}, {"leisure": "stadium"},
                 {"leisure": "pitch"}, {"leisure": "sports_centre"}, {"leisure": "golf_course"},
                 {"leisure": "swimming_pool"}):
        assert categorize(tags) == CATEGORY_SPORT, tags


def test_categorize_returns_other_when_no_clue() -> None:
    assert categorize(None) == UNCATEGORIZED
    assert categorize({}) == UNCATEGORIZED
    assert categorize({"shop": "bakery"}) == UNCATEGORIZED
    assert categorize({"highway": "residential"}) == UNCATEGORIZED


def test_categorize_ignores_negative_values_and_is_case_tolerant() -> None:
    assert categorize({"ski": "no", "natural": "peak"}) == CATEGORY_NATURE, "ski=no 不算滑雪线索"
    assert categorize({"sport": "no"}) == UNCATEGORIZED
    assert categorize({"Natural": " Peak "}) == CATEGORY_NATURE
    assert categorize({"HISTORIC": "Castle"}) == CATEGORY_CULTURE


# --------------------------------------------------------------------------- #
# 2. 归类优先级:滑雪 > 运动 > 人文美食 > 自然(一地只入一类)
# --------------------------------------------------------------------------- #


def test_category_priority_order_is_ski_sport_culture_nature() -> None:
    assert list(CATEGORY_PRIORITY) == [CATEGORY_SKI, CATEGORY_SPORT, CATEGORY_CULTURE, CATEGORY_NATURE]
    assert [rule[0] for rule in classify.CATEGORY_RULES] == list(CATEGORY_PRIORITY)


def test_categorize_applies_priority_when_several_categories_match() -> None:
    # 雪场同时带 sport/leisure → 归滑雪,不再进"运动"
    assert categorize({"piste:type": "downhill", "sport": "climbing", "leisure": "stadium"}) == CATEGORY_SKI
    assert categorize({"sport": "skiing", "leisure": "sports_centre"}) == CATEGORY_SKI
    # 运动场所同时带餐饮/古迹 → 归运动
    assert categorize({"sport": "climbing", "historic": "ruins", "amenity": "cafe"}) == CATEGORY_SPORT
    assert categorize({"leisure": "stadium", "cuisine": "noodle"}) == CATEGORY_SPORT
    # 人文美食同时带自然线索 → 归人文(有明确 historic/food 线索)
    assert categorize({"historic": "castle", "natural": "wood"}) == CATEGORY_CULTURE
    assert categorize({"natural": "peak", "amenity": "restaurant"}) == CATEGORY_CULTURE
    # 只剩自然线索 → 归自然
    assert categorize({"natural": "peak", "leisure": "park"}) == CATEGORY_NATURE


def test_categorize_fixes_poc_nature_attraction_duplication() -> None:
    """POC 修复:``natural=peak`` + ``tourism=attraction`` 只能落一类(自然),不再两类都出现。"""
    both = {"natural": "peak", "tourism": "attraction"}
    assert categorize(both) == CATEGORY_NATURE
    assert categorize({"tourism": "attraction"}) == CATEGORY_CULTURE, "泛景点无自然线索 → 人文美食(自然需 attraction ∩ 自然线索)"
    assert categorize({"tourism": "attraction", "historic": "memorial"}) == CATEGORY_CULTURE
    # 同一实体在"并集检索"里被两组命中,合并 tags 后仍只有一个分类
    rows = classify_places([
        element("node", 5, "双子峰", natural="peak"),
        element("node", 5, "双子峰", tourism="attraction"),
    ])
    assert len(rows) == 1 and rows[0]["category"] == CATEGORY_NATURE


def test_every_category_has_color_and_label_for_frontend_pins() -> None:
    by_key = {item["key"]: item for item in classify.CATEGORIES}
    assert set(by_key) == set(CATEGORY_PRIORITY) | {UNCATEGORIZED}
    # STAGE1-PLAN 第 2 节:自然=绿、人文=橙、滑雪=蓝、运动=红
    assert by_key[CATEGORY_NATURE]["color"] == "#16a34a"
    assert by_key[CATEGORY_CULTURE]["color"] == "#ea580c"
    assert by_key[CATEGORY_SKI]["color"] == "#2563eb"
    assert by_key[CATEGORY_SPORT]["color"] == "#dc2626"
    assert all(item["emoji"] and item["label"] and item["blurb"] for item in classify.CATEGORIES)
    assert classify.is_known_category(CATEGORY_SKI) and not classify.is_known_category("美食")


# --------------------------------------------------------------------------- #
# 3. 跨 tag 去重:键 = OSM type + id
# --------------------------------------------------------------------------- #


def test_dedupe_merges_same_osm_identity_and_unions_tags() -> None:
    rows = dedupe_places([
        element("way", 7, "古镇", tourism="attraction"),
        element("way", 7, "古镇", historic="town", cuisine="local"),
        element("node", 8, "雪场", piste="downhill"),
    ])
    assert [(row["osm_type"], row["osm_id"]) for row in rows] == [("way", 7), ("node", 8)]
    assert rows[0]["tags"] == {"tourism": "attraction", "historic": "town", "cuisine": "local"}
    assert len(rows[0]["tags"]) == 3, "同一实体的多组 tag 应合并,归类才看得到全部线索"


def test_dedupe_keeps_same_id_of_different_type_apart() -> None:
    rows = dedupe_places([element("node", 7, "甲"), element("way", 7, "乙"), element("relation", 7, "丙")])
    assert len(rows) == 3, "去重键是 type + id,node/way/relation 的同号 id 是不同实体"


def test_dedupe_preserves_first_seen_order_and_fills_missing_fields() -> None:
    rows = dedupe_places([
        element("node", 1, "", natural="peak"),
        element("node", 2, "乙", natural="water"),
        {"osm_type": "node", "osm_id": 1, "name": "甲", "lat": None, "lng": None, "tags": {"ele": "309"}},
    ])
    assert [row["name"] for row in rows] == ["甲", "乙"], "首次出现的位置保留,名字后来补上"
    assert rows[0]["tags"] == {"natural": "peak", "ele": "309"}


def test_dedupe_falls_back_to_name_and_coordinates_without_osm_id() -> None:
    seeds = [
        {"name": "室内滑雪场", "lat": 31.8, "lng": 121.5, "tags": {"piste:type": "downhill"}},
        {"name": "室内滑雪场", "lat": 31.8, "lng": 121.5, "tags": {"sport": "skiing"}},
        {"name": "另一处", "lat": 31.9, "lng": 121.6, "tags": {}},
    ]
    rows = dedupe_places(seeds)
    assert len(rows) == 2, "没有 OSM id 的种子数据按 名字+坐标 兜底去重"
    assert classify_places(seeds)[0]["category"] == CATEGORY_SKI


def test_classify_places_gives_exactly_one_category_per_feature() -> None:
    rows = classify_places([
        element("node", 1, "雪场", piste="downhill", sport="skiing"),
        element("node", 1, "雪场", leisure="sports_centre"),
        element("way", 2, "古镇", historic="town", tourism="attraction", natural="wood"),
        element("node", 3, "攀岩馆", sport="climbing", amenity="cafe"),
        element("node", 4, "湖", natural="water", leisure="park"),
    ])
    assert [(row["name"], row["category"]) for row in rows] == [
        ("雪场", CATEGORY_SKI), ("古镇", CATEGORY_CULTURE), ("攀岩馆", CATEGORY_SPORT), ("湖", CATEGORY_NATURE),
    ]
    assert len({(row["osm_type"], row["osm_id"]) for row in rows}) == len(rows)


# --------------------------------------------------------------------------- #
# 4. 检索(TASK-9b 起走**高德**):分组 typecode/keywords + 圆形/分格多边形 + 配额翻页
# --------------------------------------------------------------------------- #


def test_search_groups_cover_four_categories_with_independent_budgets() -> None:
    """四分类检索组(TASK-9b 起是高德 typecode/keywords 组)与配额口径不变。"""
    groups = classify.search_groups()
    assert {group["category"] for group in groups} == set(CATEGORY_PRIORITY)
    assert all(group["budget"] > 0 for group in groups)
    assert all(group["types"] or group["keywords"] for group in groups), \
        "每组要么给 typecode 粗筛,要么给关键词(§6.3:两者二选一)"
    assert classify.search_budget() == sum(group["budget"] for group in groups) == 540
    assert [group["budget"] for group in groups] == [80, 100, 180, 40, 140], "各组配额沿用旧数值"
    ski = classify.search_groups([CATEGORY_SKI])
    assert ski and all(group["category"] == CATEGORY_SKI for group in ski)
    assert ski[0]["types"] == [amap_categories.SKI_TYPECODE], "滑雪场 = 080106(§6.4)"
    assert classify.search_budget([CATEGORY_SKI]) < classify.search_budget()


def test_search_tags_is_a_deduped_union_of_all_category_clues() -> None:
    tags = classify.search_tags()
    markers = [str(tag) for tag in tags]
    assert len(markers) == len(set(markers)), "并集内不应有重复选择器"
    assert tags == classify.search_tags(), "并集顺序稳定(便于构造出同样的查询语句)"
    joined = " ".join(markers)
    for clue in ("piste:type", "sport", "historic", "amenity", "natural", "place"):
        assert clue in joined, f"四分类线索 {clue} 应进检索并集"


def test_first_band_queries_every_group_once_with_its_own_amap_params(fake_amap) -> None:
    """band 下限 = 0:每组一次 ``place/around``(半径 = band 上限),types/keywords 按组给。"""
    fake_amap.rows = [
        poi_at_km(20, "城里公园", "B0FFHPARK0", typecode="110101", type_text="风景名胜;公园;公园"),
        poi_at_km(30, "云州古镇", "B0FFHTOWN0"),
    ]
    band = {"key": "0_50", "label": "0-50 km", "low": 0, "high": 50}
    rows = place_loader.default_fetcher(SHANGHAI["lat"], SHANGHAI["lng"], band)

    groups = classify.search_groups()
    assert len(fake_amap.around_calls) == len(groups), "每组一次圆形检索(不多不少)"
    assert fake_amap.polygon_calls == [], "下限为 0 的 band 不该走分格多边形"
    for call, group in zip(fake_amap.around_calls, groups):
        assert call["radius_m"] == band_radius_m(band), "半径 = band 上限"
        assert call["types"] == (list(group["types"]) or None)
        assert call["keywords"] == group["keywords"]
        assert call["page"] == 1 and call["offset"] == amap.PAGE_SIZE, "v3 固定 25 条/页"
    # 同一条 POI 被五组各命中一次 → 跨组按 ("amap", POI id) 去重后只剩两行
    assert [row["id"] for row in rows] == ["B0FFHPARK0", "B0FFHTOWN0"]


def test_loader_sends_one_v3_request_per_group_and_dedupes_by_amap_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真链路(:mod:`data_sources.amap` + 假 HTTP session):每组一次 v3 GET,身份是 crc32。"""
    payload = v3_payload([
        raw_poi("B023B17WWK", "太舞滑雪场", 115.4, 40.9,
                "体育休闲服务;运动场馆;滑雪场", "080106", cityname="张家口市"),
        raw_poi("B0FFHTOWN1", "云州古镇", 121.5, 31.6, "风景名胜;旅游景点", "110000"),
        raw_poi("B0FFHTOWN1", "云州古镇", 121.5, 31.6, "风景名胜;旅游景点", "110000"),
    ])
    fake = FakeSession(payload)
    environ = {amap.ENV_AMAP_KEY: "test-key", amap.ENV_MIN_INTERVAL: "0"}
    band = {"key": "0_50", "label": "0-50 km", "low": 0, "high": 50}
    rows = place_loader.default_fetcher(
        SHANGHAI["lat"], SHANGHAI["lng"], band, environ=environ, session=fake
    )

    groups = classify.search_groups()
    assert len(fake.calls) == len(groups), "每组一次请求;第一页没拿满就不再翻页"
    assert all(call["method"] == "GET" for call in fake.calls)
    assert all(call["url"].endswith(amap.SEARCH_AROUND_PATH) for call in fake.calls), "必须走 v3 圆形检索"
    assert {call["params"]["key"] for call in fake.calls} == {"test-key"}
    assert {call["params"]["radius"] for call in fake.calls} == {50000}
    assert {call["params"]["offset"] for call in fake.calls} == {amap.PAGE_SIZE}
    ski = next(call for call in fake.calls if call["params"].get("types") == "080106")
    assert "keywords" not in ski["params"], "§6.3:types 与 keywords 二选一"
    town = next(call for call in fake.calls if call["params"].get("keywords") == "古镇|老街|古城")
    assert "types" not in town["params"], "小城古镇走关键词(§6.4:村庄码 0 命中)"

    assert [row["id"] for row in rows] == ["B023B17WWK", "B0FFHTOWN1"], "同实体跨组只留一行"
    classified = classify.classify_amap_places(rows)
    assert [(row["name"], row["category"]) for row in classified] == [
        ("太舞滑雪场", CATEGORY_SKI), ("云州古镇", CATEGORY_CULTURE)
    ], "去重键 = 高德 POI id,归类优先级不变"
    items = place_loader.to_place_items(rows)
    assert [(item["osm_type"], item["osm_id"]) for item in items] == [
        (AMAP_OSM_TYPE, amap_osm_id("B023B17WWK")), (AMAP_OSM_TYPE, amap_osm_id("B0FFHTOWN1"))
    ], "入库身份:osm_type=amap、osm_id=crc32(POI id)"
    assert items[0]["tags"] == {
        "source": AMAP_SOURCE, "amap_id": "B023B17WWK",
        "typecode": "080106", "type": "体育休闲服务;运动场馆;滑雪场",
    }


def test_ring_cells_only_keep_cells_intersecting_the_band() -> None:
    """band 下限 > 0 的分格计划:切格 → 只留与环带相交的 → 按最近距离升序。"""
    band = next(item for item in DISTANCE_BANDS if item["key"] == "50_100")
    assert place_loader.grid_side(band_radius_m(band)) == 8, "200 km 直径 / 25 km 格边长 = 8"
    cells = place_loader.ring_cells(SHANGHAI["lat"], SHANGHAI["lng"], band)
    assert 0 < len(cells) < 8 * 8, "整格都在环外的(太近/太远)被丢掉"
    ranges = [place_loader.cell_distance_range(cell, SHANGHAI["lat"], SHANGHAI["lng"]) for cell in cells]
    assert [near for near, _ in ranges] == sorted(near for near, _ in ranges), \
        "按最近距离升序 → 前几格总是环带里离起点最近的部分"
    for cell, (near, far) in zip(cells, ranges):
        assert len(cell) == 4 and all(len(point) == 2 for point in cell), "格子是 4 顶点矩形(lng,lat)"
        assert far >= band_inner_radius_m(band) / 1000.0, "整格都在内圈里的不该进计划"
        assert near < band_radius_m(band) / 1000.0, "整格都在外圈外的不该进计划"
    min_lat, min_lng, max_lat, max_lng = place_loader.band_bbox(
        SHANGHAI["lat"], SHANGHAI["lng"], band_radius_m(band)
    )
    for cell in cells:  # 顶点按 7 位小数定点(amap.COORD_PRECISION),留 1e-6 容差
        assert all(min_lng - 1e-6 <= x <= max_lng + 1e-6 for x, _ in cell)
        assert all(min_lat - 1e-6 <= y <= max_lat + 1e-6 for _, y in cell), "格子都在外半径包围盒内"


def test_grid_cell_limit_grows_with_fetch_rounds() -> None:
    """递增扩格:本轮抓几格 = 4 ×(已完成轮数 + 1),与配额 30 ×(轮数 + 1)同口径。"""
    assert place_loader.GRID_CELLS_PER_ROUND == 4
    assert place_loader.grid_cell_limit(None) == 4
    assert [place_loader.grid_cell_limit(rounds) for rounds in (0, 1, 2, 3)] == [4, 8, 12, 16]
    assert place_loader.grid_cell_limit(-5) == 4, "负轮数按 0 起算"
    assert [place_loader.progressive_target_total(rounds) for rounds in (0, 1, 2)] == [30, 60, 90]


def test_ring_fetch_queries_every_group_per_cell(fake_amap) -> None:
    """分格路径:**每组 × 每格**一次 ``place/polygon``,参数就是该组的 types/keywords。"""
    band = next(item for item in DISTANCE_BANDS if item["key"] == "50_100")
    fake_amap.rows = []  # 空结果:配额永远抓不满 → 每格都得问一遍
    cells = place_loader.ring_cells(SHANGHAI["lat"], SHANGHAI["lng"], band)
    groups = classify.search_groups()
    rows = place_loader.default_fetcher(
        SHANGHAI["lat"], SHANGHAI["lng"], band, cell_limit=len(cells)
    )

    assert rows == []
    assert fake_amap.around_calls == [], "下限 > 0 不走圆形检索"
    assert len(fake_amap.polygon_calls) == len(groups) * len(cells), "每组每格一次请求"
    expected = [
        (list(group["types"]) or None, group["keywords"])
        for group in groups
        for _ in cells
    ]
    assert [(call["types"], call["keywords"]) for call in fake_amap.polygon_calls] == expected
    assert all(call["page"] == 1 and call["offset"] == amap.PAGE_SIZE for call in fake_amap.polygon_calls)


def test_default_fetcher_expands_cells_with_fetch_rounds(fake_amap) -> None:
    """扩格随 ``fetch_rounds`` 递增:上一轮没抓满 → 下一轮「加载更多」多抓几格。"""
    band = next(item for item in DISTANCE_BANDS if item["key"] == "50_100")
    cells = place_loader.ring_cells(SHANGHAI["lat"], SHANGHAI["lng"], band)
    assert len(cells) > place_loader.grid_cell_limit(1), "本用例需要足够多的格子才验得出扩格"
    group = {"category": CATEGORY_NATURE, "group": "自然风光", "budget": 140,
             "types": ("110000",), "keywords": None}
    for rounds in (None, 0, 1, 2):
        fake_amap.polygon_calls.clear()
        place_loader.default_fetcher(
            SHANGHAI["lat"], SHANGHAI["lng"], band, groups=[group], fetch_rounds=rounds
        )
        assert len(fake_amap.polygon_calls) == place_loader.grid_cell_limit(rounds), \
            f"fetch_rounds={rounds} 应抓 {place_loader.grid_cell_limit(rounds)} 格"
    # cell_limit 显式给定时优先于 fetch_rounds(测试/回填脚本用)
    fake_amap.polygon_calls.clear()
    place_loader.default_fetcher(
        SHANGHAI["lat"], SHANGHAI["lng"], band, groups=[group], fetch_rounds=0, cell_limit=3
    )
    assert len(fake_amap.polygon_calls) == 3


def test_group_paging_respects_amap_page_cap_and_budget(fake_amap) -> None:
    """单查询 200 条硬上限:页数夹在 ``[1, MAX_PAGE]``,配额抓满就停止翻页。"""
    assert place_loader.FETCH_LIMIT == amap.MAX_ROWS_PER_QUERY == 200
    assert place_loader.page_limit(30) == 2, "30 条配额 = 2 页(25 条/页)"
    assert place_loader.page_limit(30, slack=place_loader.RING_PAGE_SLACK) == 3, "分格路径多翻一页兜损耗"
    assert place_loader.page_limit(540) == amap.MAX_PAGE == 8, "page>=9 服务端恒空,不硬翻"
    assert place_loader.page_limit(0) == 1

    fake_amap.rows = [
        poi_at_km(20 + index * 0.1, f"环内点{index + 1:03d}", f"B0FFH{index + 1:05d}")
        for index in range(60)
    ]
    group = {"category": CATEGORY_NATURE, "group": "自然风光", "budget": 30,
             "types": ("110000",), "keywords": None}
    rows = place_loader.fetch_group_around(SHANGHAI["lat"], SHANGHAI["lng"], 50_000, group)
    assert len(rows) == 30, "配额抓满即停"
    assert [call["page"] for call in fake_amap.around_calls] == [1, 2], "30 条只翻 2 页"
    assert [row["id"] for row in rows] == [f"B0FFH{index + 1:05d}" for index in range(30)]


def test_ring_fetch_converges_to_band_and_keeps_group_budget(fake_amap) -> None:
    """分格 + 本地 haversine 收敛:环外的行不占配额,配额只花在 ``[low, high)`` 内。"""
    band = next(item for item in DISTANCE_BANDS if item["key"] == "50_100")
    fake_amap.rows = [
        poi_at_km(20, "城里公园", "B0FFHPARK0", typecode="110101", type_text="风景名胜;公园;公园"),
        poi_at_km(60, "远山", "B0FFHMOUNT0"),
        poi_at_km(75, "云州古镇", "B0FFHTOWN0"),
        poi_at_km(150, "下一段雪场", "B0FFHSKI00",
                  typecode="080106", type_text="体育休闲服务;运动场馆;滑雪场"),
    ]
    cells = place_loader.ring_cells(SHANGHAI["lat"], SHANGHAI["lng"], band)
    group = {"category": CATEGORY_NATURE, "group": "自然风光", "budget": 140,
             "types": ("110000",), "keywords": None}
    rows = place_loader.fetch_group_polygon(cells, SHANGHAI["lat"], SHANGHAI["lng"], band, group)
    assert [row["id"] for row in rows] == ["B0FFHMOUNT0", "B0FFHTOWN0"], "环内两条按由近及远"
    for row in rows:
        distance = haversine_km(SHANGHAI["lat"], SHANGHAI["lng"], row["lat"], row["lng"])
        assert 50.0 <= distance < 100.0, f"收敛到 [low, high):{row['name']} {distance}"

    tight = dict(group, budget=1)
    assert [row["id"] for row in
            place_loader.fetch_group_polygon(cells, SHANGHAI["lat"], SHANGHAI["lng"], band, tight)] \
        == ["B0FFHMOUNT0"], "配额 1 条就只留最近的一条"
    assert place_loader.fetch_group_polygon([], SHANGHAI["lat"], SHANGHAI["lng"], band, group) == []
    assert place_loader.fetch_group_polygon(
        cells, SHANGHAI["lat"], SHANGHAI["lng"], band, dict(group, budget=0)
    ) == [], "配额 0 不检索"


# --------------------------------------------------------------------------- #
# 5. 入库链路:去重归类写 Place.category
# --------------------------------------------------------------------------- #


def test_to_place_items_dedupes_and_classifies_before_insert() -> None:
    items = place_loader.to_place_items([
        element("way", 7, "古镇", tourism="attraction"),
        element("way", 7, "古镇", historic="town"),
        element("node", 8, "雪场", piste="downhill", sport="skiing"),
        element("node", 9, "攀岩馆", sport="climbing"),
    ])
    assert [(item["osm_type"], item["osm_id"]) for item in items] == [("way", 7), ("node", 8), ("node", 9)]
    assert [item["category"] for item in items] == [CATEGORY_CULTURE, CATEGORY_SKI, CATEGORY_SPORT]
    assert items[0]["tags"] == {"tourism": "attraction", "historic": "town"}
    assert all("intro" not in item for item in items), "简介在入库后由 services.intro 补,不在归类阶段生成"


def test_loader_pins_amap_groups_and_limits() -> None:
    """loader 侧的口径常量:分组 = classify 的高德组、200 条上限、水位来源写 amap。"""
    assert place_loader.SEARCH_GROUPS == classify.search_groups()
    assert {group["category"] for group in place_loader.SEARCH_GROUPS} == set(CATEGORY_PRIORITY)
    assert place_loader.SEARCH_TAGS == classify.search_tags(), \
        "/api/places/meta 的 search_tags 键仍要有值(遗留 OSM 选择器并集,只作口径展示)"
    assert place_loader.FULL_SEARCH_BUDGET == 540 and place_loader.PROGRESSIVE_STEP == 30
    assert place_loader.FETCH_LIMIT == amap.MAX_ROWS_PER_QUERY
    assert place_loader.SOURCE_AMAP == "amap", "SegmentFetch.source / API source 写 amap(§6.7)"
    assert not hasattr(place_loader, "SOURCE_OVERPASS"), "Overpass 时代的来源常量应已退役"


def test_loader_ring_covers_every_band_with_positive_lower_bound(fake_amap) -> None:
    """下限 > 0 的分段全部走**包围盒分格**(远环稀少 bug 的根因就在这里);0_50 例外(见下一例)。"""
    for band in DISTANCE_BANDS:
        fake_amap.around_calls.clear()
        fake_amap.polygon_calls.clear()
        place_loader.default_fetcher(
            SHANGHAI["lat"], SHANGHAI["lng"], band,
            cell_limit=place_loader.GRID_MAX_SIDE ** 2,
        )
        if band["low"] == 0:
            assert fake_amap.polygon_calls == [] and fake_amap.around_calls, band["key"]
            continue
        assert fake_amap.around_calls == [], f"{band['key']} 该走分格多边形"
        cells = place_loader.ring_cells(SHANGHAI["lat"], SHANGHAI["lng"], band)
        queried = [call["polygon"] for call in fake_amap.polygon_calls]
        groups = classify.search_groups()
        assert len(queried) == len(cells) * len(groups), f"{band['key']} 每格每组都问到"
        assert queried[:len(cells)] == [list(cell) for cell in cells], "格子顺序 = 由近及远"
        min_lat, min_lng, max_lat, max_lng = place_loader.band_bbox(
            SHANGHAI["lat"], SHANGHAI["lng"], band_radius_m(band)
        )
        for cell in queried:  # 顶点 7 位小数定点,留 1e-6 容差
            assert all(min_lng - 1e-6 <= x <= max_lng + 1e-6 for x, _ in cell)
            assert all(min_lat - 1e-6 <= y <= max_lat + 1e-6 for _, y in cell), \
                f"{band['key']} 的格子必须落在**上限半径**的包围盒里"


def test_loader_degrades_to_single_circle_when_band_low_is_zero(fake_amap) -> None:
    """下限为 0 的分段没有内圈可减 → 退化成每组一次圆形检索(半径 = band 上限)。"""
    band = {"key": "0_50", "label": "0-50 km", "low": 0, "high": 50}
    assert band_inner_radius_m(band) == 0.0
    place_loader.default_fetcher(SHANGHAI["lat"], SHANGHAI["lng"], band)
    assert fake_amap.polygon_calls == []
    groups = classify.search_groups()
    assert len(fake_amap.around_calls) == len(groups)
    assert all(call["radius_m"] == 50_000 for call in fake_amap.around_calls)
    assert all(call["page"] == 1 for call in fake_amap.around_calls)
    assert (fake_amap.around_calls[0]["lat"], fake_amap.around_calls[0]["lng"]) == (
        SHANGHAI["lat"], SHANGHAI["lng"]
    )




def test_load_segment_stores_one_row_per_feature_with_priority_category(session) -> None:
    candidates = [
        point_at(60, name="雪场", tags={"piste:type": "downhill", "sport": "skiing"}, osm_id=1),
        point_at(60, name="雪场", tags={"leisure": "sports_centre"}, osm_id=1),          # 同实体,跨 tag
        point_at(75, name="古镇", tags={"historic": "town", "natural": "wood"}, osm_type="way", osm_id=2),
        point_at(80, name="湖", tags={"natural": "water", "tourism": "attraction"}, osm_id=3),
        point_at(150, name="环外", tags={"natural": "peak"}, osm_id=4),                   # 环外
    ]
    outcome = place_loader.load_segment(
        session, city="上海", band="50_100", fetcher=FakeFetcher(candidates),
        geocoder=RecordingGeocoder(), intros=False,
    )
    rows = {row.name: row for row in session.scalars(select(Place))}
    assert set(rows) == {"雪场", "古镇", "湖"}, "环外被过滤、同实体去重"
    assert rows["雪场"].category == CATEGORY_SKI
    assert rows["古镇"].category == CATEGORY_CULTURE
    assert rows["湖"].category == CATEGORY_NATURE
    assert rows["湖"].tags == {"natural": "water", "tourism": "attraction"}
    assert outcome.counts_by_category == {CATEGORY_SKI: 1, CATEGORY_CULTURE: 1, CATEGORY_NATURE: 1}
    assert [place["name"] for place in outcome.places] == ["雪场", "古镇", "湖"]


# --------------------------------------------------------------------------- #
# 6. LLM 一句话简介:mock + 按 POI 缓存 + 失败降级
# --------------------------------------------------------------------------- #


def test_clean_intro_flattens_and_trims_llm_output() -> None:
    assert intro_service.clean_intro("  适合周末登高远眺  ") == "适合周末登高远眺。"
    assert intro_service.clean_intro('"湖边的老镇,烟火气足。"') == "湖边的老镇,烟火气足。"
    assert intro_service.clean_intro("简介:\n  山间步道适合徒步。 ") == "山间步道适合徒步。"
    assert intro_service.clean_intro("") == "" and intro_service.clean_intro(None) == ""
    long_text = intro_service.clean_intro("很" * 200)
    assert len(long_text) <= intro_service.MAX_INTRO_CHARS + 1 and long_text.endswith("。")


def test_build_intro_prompt_carries_name_category_and_tag_facts() -> None:
    prompt = intro_service.build_intro_prompt({
        "name": "秦望山", "category": CATEGORY_NATURE, "origin_city": "上海", "band": "50_100",
        "tags": {"natural": "peak", "ele": "32", "wikipedia": "zh:秦望山", "source": "survey"},
    })
    assert "秦望山" in prompt and CATEGORY_NATURE in prompt
    assert "natural=peak" in prompt and "ele=32" in prompt
    assert "wikipedia" not in prompt and "source=survey" not in prompt, "只带白名单标签,不塞整包 tag"
    assert "50-100 km" in prompt


def test_generate_intro_returns_text_and_swallows_failures() -> None:
    place = {"name": "远山", "category": CATEGORY_NATURE, "tags": {"natural": "peak"}}
    ok = FakeLLM(reply="山顶视野开阔,适合看日落。")
    assert intro_service.generate_intro(place, client=ok) == "山顶视野开阔,适合看日落。"
    assert ok.prompts and "远山" in ok.prompts[0]

    for error in (DataSourceError("LLM", "被限流(HTTP 429)"), RuntimeError("boom")):
        broken = FakeLLM(error=error)
        assert intro_service.generate_intro(place, client=broken) == "", f"失败应降级为空简介:{error}"
    assert intro_service.generate_intro({"name": "", "category": CATEGORY_NATURE}, client=FakeLLM()) == ""


def test_fill_missing_intros_caches_per_poi_and_never_refills(session) -> None:
    seed(session, category=CATEGORY_NATURE, tags={"natural": "peak"}, osm_id=1, name="远山")
    seed(session, category=CATEGORY_CULTURE, tags={"historic": "town"}, osm_id=2, name="古镇")
    seed(session, category=CATEGORY_NATURE, tags={"natural": "water"}, osm_id=3, name="已有简介",
         intro="之前生成过的一句话简介。")

    first = FakeLLM(reply="适合周末半日游。")
    stats = intro_service.fill_missing_intros(session, client=first, workers=1)
    assert stats["scanned"] == 2 and stats["filled"] == 2 and stats["failed"] == 0
    assert stats["pending"] == 0 and stats["provider"] == first.label
    assert len(first.prompts) == 2, "已有简介的 POI 不调用 LLM"
    assert all("已有简介" not in prompt for prompt in first.prompts)
    rows = {row.name: row for row in session.scalars(select(Place))}
    assert rows["远山"].intro == "适合周末半日游。"
    assert rows["已有简介"].intro == "之前生成过的一句话简介。", "已缓存的简介不被覆盖"

    second = FakeLLM(reply="不该被调用")
    again = intro_service.fill_missing_intros(session, client=second, workers=1)
    assert again == {"scanned": 0, "filled": 0, "failed": 0, "pending": 0, "provider": second.label}
    assert second.prompts == [], "第二次不应再调 LLM(DB 即缓存)"


def test_fill_missing_intros_degrades_without_blocking_ingest(session) -> None:
    seed(session, category=CATEGORY_NATURE, tags={"natural": "peak"}, osm_id=1, name="远山")
    broken = FakeLLM(error=DataSourceError("LLM", "网络连接失败"))
    stats = intro_service.fill_missing_intros(session, client=broken, workers=1)
    assert stats["filled"] == 0 and stats["failed"] == 1 and stats["pending"] == 1
    row = session.scalar(select(Place))
    assert row.intro in (None, ""), "降级后简介为空,但行仍在库里"
    assert row.category == CATEGORY_NATURE


def test_fill_missing_intros_respects_filters_and_limit(session) -> None:
    seed(session, category=CATEGORY_NATURE, tags={"natural": "peak"}, osm_id=1, name="远山", band="50_100")
    seed(session, category=CATEGORY_SKI, tags={"piste:type": "downhill"}, osm_id=2, name="雪场", band="50_100")
    seed(session, category=CATEGORY_NATURE, tags={"natural": "water"}, osm_id=3, name="湖", band="100_200")

    client = FakeLLM(reply="一句话简介。")
    stats = intro_service.fill_missing_intros(session, band="50_100", category=CATEGORY_SKI, client=client, workers=1)
    assert stats["scanned"] == 1 and client.prompts and "雪场" in client.prompts[0]

    limited = FakeLLM(reply="一句话简介。")
    stats = intro_service.fill_missing_intros(session, limit=1, client=limited, workers=1)
    assert stats["scanned"] == 1 and stats["pending"] == 1, "limit 控制单次调用量,其余下轮再补"
    assert repo.count_places(session, missing_intro=True) == 1


def test_load_segment_generates_intros_after_ingest(session, fake_llm) -> None:
    outcome = place_loader.load_segment(
        session, city="上海", band="50_100", fetcher=FakeFetcher(),
        geocoder=RecordingGeocoder(), intro_workers=1,
    )
    assert outcome.intro_stats["filled"] == 2 and outcome.intro_stats["failed"] == 0
    rows = {row.name: row for row in session.scalars(select(Place))}
    assert rows["远山"].intro and rows["古镇"].intro
    assert any("远山" in prompt for prompt in fake_llm.prompts)
    assert any(CATEGORY_NATURE in prompt for prompt in fake_llm.prompts), "prompt 应带上归类结果"


def test_load_segment_intro_failure_does_not_block_ingest(session, monkeypatch) -> None:
    monkeypatch.setattr(intro_service, "default_client", lambda: FakeLLM(error=DataSourceError("LLM", "超时")))
    outcome = place_loader.load_segment(
        session, city="上海", band="50_100", fetcher=FakeFetcher(),
        geocoder=RecordingGeocoder(), intro_workers=1,
    )
    assert outcome.written == 2 and len(outcome.places) == 2, "简介失败不影响入库"
    assert outcome.intro_stats["filled"] == 0 and outcome.intro_stats["failed"] == 2
    assert all(row.intro in (None, "") for row in session.scalars(select(Place)))


def test_cached_segment_neither_fetches_nor_calls_llm(session, fake_llm, monkeypatch) -> None:
    place_loader.load_segment(session, city="上海", band="50_100", fetcher=FakeFetcher(),
                              geocoder=RecordingGeocoder(), intro_workers=1)
    fake_llm.prompts.clear()
    monkeypatch.setattr(intro_service, "default_client", lambda: ExplodingLLM())

    second = place_loader.load_segment(session, city="上海", band="50_100", fetcher=FakeFetcher(),
                                       geocoder=RecordingGeocoder())
    assert second.source == "db" and second.network_used is False
    assert second.intro_stats is None, "读库路径不触发 LLM(保持零网络秒回)"
    assert [place["intro"] for place in second.places] == ["适合周末半日登高远眺。"] * 2, "简介从库里读回"


def test_refetch_keeps_cached_intro(session, fake_llm) -> None:
    place_loader.load_segment(session, city="上海", band="50_100", fetcher=FakeFetcher(),
                              geocoder=RecordingGeocoder(), intro_workers=1)
    fake_llm.prompts.clear()
    outcome = place_loader.load_segment(session, city="上海", band="50_100", fetcher=FakeFetcher(),
                                        geocoder=RecordingGeocoder(), refresh=True, intro_workers=1)
    assert outcome.written == 2
    assert fake_llm.prompts == [], "重抓不该给已有简介的 POI 再调 LLM"
    assert outcome.intro_stats["scanned"] == 0


def test_describe_llm_never_leaks_the_api_key() -> None:
    payload = json.dumps(intro_service.describe_llm(), ensure_ascii=False)
    for env_name in LLM_KEY_ENVS:
        secret = (os.environ.get(env_name) or "").strip()
        if secret:
            assert secret not in payload, f"{env_name} 的值绝不能出现在接口/日志里"
    described = intro_service.describe_llm({"DEEPSEEK_API_KEY": "sk-test-123"})
    assert described["enabled"] is True and described["provider"] == "deepseek"
    assert described["base_url"] == "https://api.deepseek.com" and described["key_env"] == "DEEPSEEK_API_KEY"
    assert "sk-test-123" not in json.dumps(described, ensure_ascii=False)
    assert intro_service.describe_llm({})["enabled"] is False, "没有 key 时明确报未配置(简介留空)"


def test_llm_client_posts_openai_compatible_chat_request() -> None:
    resolved = intro_service.resolve_provider({"DEEPSEEK_API_KEY": "sk-test-123"})
    assert resolved is not None and resolved.chat_url == "https://api.deepseek.com/chat/completions"
    fake = FakeSession({"choices": [{"message": {"content": "湖边适合散步。"}}]})
    client = intro_service.LLMClient(resolved, session=fake)
    assert client.enabled and client.chat("写一句话简介") == "湖边适合散步。"

    call = fake.calls[0]
    assert call["method"] == "POST" and call["url"] == resolved.chat_url
    assert call["headers"]["Authorization"] == "Bearer sk-test-123"
    body = json.loads(call["data"].decode("utf-8")) if isinstance(call["data"], bytes) else None
    assert body and body["model"] == "deepseek-chat" and body["stream"] is False
    assert body["messages"][0]["role"] == "system" and body["messages"][1]["role"] == "user"


# --------------------------------------------------------------------------- #
# 7. 存量库重归类(旧值/空值)
# --------------------------------------------------------------------------- #


def test_reclassify_updates_legacy_and_empty_categories(session) -> None:
    seed(session, category="旅游景点", tags={"piste:type": "downhill"}, osm_id=11, name="旧值雪场")
    seed(session, category="", tags={"sport": "climbing"}, osm_id=12, name="空值攀岩馆")
    seed(session, category=CATEGORY_NATURE, tags={"natural": "peak"}, osm_id=13, name="本来就对")

    dry = reclassify.reclassify_places(session, commit=False)
    assert dry["scanned"] == 3 and dry["changed"] == 2 and dry["legacy"] == 2
    assert session.scalar(select(Place).where(Place.osm_id == 11)).category == "旅游景点", "dry-run 不写库"

    stats = reclassify.reclassify_places(session)
    assert stats["changed"] == 2 and stats["committed"] is True
    rows = {row.name: row.category for row in session.scalars(select(Place))}
    assert rows == {"旧值雪场": CATEGORY_SKI, "空值攀岩馆": CATEGORY_SPORT, "本来就对": CATEGORY_NATURE}
    assert dict(stats["transitions"]) == {"旅游景点 → 滑雪场": 1, "(空) → 运动": 1}
    assert stats["by_category"] == {CATEGORY_SKI: 1, CATEGORY_SPORT: 1, CATEGORY_NATURE: 1}

    again = reclassify.reclassify_places(session)
    assert again["changed"] == 0 and again["legacy"] == 0, "重归类是幂等的"


def test_reclassify_can_be_scoped_to_legacy_rows_only(session) -> None:
    seed(session, category="旅游景点", tags={"historic": "castle"}, osm_id=21, name="旧值")
    seed(session, category=CATEGORY_NATURE, tags={"historic": "castle", "natural": "wood"}, osm_id=22, name="新规则会变")

    stats = reclassify.reclassify_places(session, only_legacy=True)
    assert stats["scanned"] == 1 and stats["changed"] == 1
    rows = {row.name: row.category for row in session.scalars(select(Place))}
    assert rows["旧值"] == CATEGORY_CULTURE and rows["新规则会变"] == CATEGORY_NATURE, "only_legacy 不动已知分类"


def test_reclassify_dedupes_poc_duplicates_by_identity(session) -> None:
    """POC 的"自然/景点重复"最终形态:同一 OSM 实体在库里只有一行、只属一类。"""
    seed(session, category="旅游景点", tags={"natural": "peak", "tourism": "attraction"}, osm_id=31, name="双子峰")
    reclassify.reclassify_places(session)
    rows = list(session.scalars(select(Place).where(Place.osm_id == 31)))
    assert len(rows) == 1 and rows[0].category == CATEGORY_NATURE
    assert repo.count_by_category(session, origin_city="上海", band="50_100") == {CATEGORY_NATURE: 1}


# --------------------------------------------------------------------------- #
# 8. POC 路由与 API
# --------------------------------------------------------------------------- #


def test_poc_discover_no_longer_lists_one_place_under_two_categories(monkeypatch) -> None:
    monkeypatch.setattr(discover_api, "_cache", {})
    twin = {"lat": round(SHANGHAI["lat"] + 60 / KM_PER_DEGREE, 6), "lng": SHANGHAI["lng"],
            "name": "双子峰", "tags": {"natural": "peak", "tourism": "attraction"}}
    monkeypatch.setattr(discover_api, "ds_nearby", lambda *args, **kwargs: [dict(twin)])

    origin = {"city": "上海", "name": "上海市", **SHANGHAI}
    band = DISTANCE_BANDS[1]
    nature = discover_api._find_places(origin, band, discover_api.CATEGORIES["自然风光"])
    attraction = discover_api._find_places(origin, band, discover_api.CATEGORIES["旅游景点"])
    assert [row["name"] for row in nature] == ["双子峰"]
    assert attraction == [], "同一地物不再在第二类里重复出现"


def test_api_meta_exposes_priority_groups_and_llm_without_keys() -> None:
    meta = places_api.places_meta()
    assert meta["category_priority"] == list(CATEGORY_PRIORITY)
    assert [item["key"] for item in meta["categories"]][:4] == list(
        (CATEGORY_NATURE, CATEGORY_CULTURE, CATEGORY_SKI, CATEGORY_SPORT)
    )
    assert {group["category"] for group in meta["search_groups"]} == set(CATEGORY_PRIORITY)
    assert meta["search_tags"] == place_loader.SEARCH_TAGS
    assert meta["search_budget"] == classify.search_budget()
    assert {"enabled", "provider", "label", "base_url", "model", "key_env", "note"} <= set(meta["llm"])

    dumped = json.dumps(meta, ensure_ascii=False)
    for env_name in LLM_KEY_ENVS:
        secret = (os.environ.get(env_name) or "").strip()
        if secret:
            assert secret not in dumped, "meta 绝不能泄露 LLM key"


def test_api_fill_intros_endpoint_uses_cache(session, fake_llm) -> None:
    seed(session, category=CATEGORY_NATURE, tags={"natural": "peak"}, osm_id=41, name="远山")
    seed(session, category=CATEGORY_NATURE, tags={"natural": "water"}, osm_id=42, name="湖",
         intro="已缓存的简介。")

    payload = places_api.fill_intros(origin="上海", band="50_100", category=None, limit=0, session=session)
    assert payload["scanned"] == 1 and payload["filled"] == 1 and payload["pending"] == 0
    assert payload["provider"] == fake_llm.label and len(fake_llm.prompts) == 1

    again = places_api.fill_intros(origin="上海", band="50_100", category=None, limit=0, session=session)
    assert again["scanned"] == 0 and len(fake_llm.prompts) == 1, "已有 intro 不再调 LLM"

    with pytest.raises(HTTPException) as caught:
        places_api.fill_intros(origin="  ", band=None, category=None, limit=None, session=session)
    assert caught.value.status_code == 400 and "起点城市不能为空" in str(caught.value.detail)
    with pytest.raises(HTTPException) as caught:
        places_api.fill_intros(origin="上海", band="10_20", category=None, limit=None, session=session)
    assert caught.value.status_code == 400 and "未知距离分段" in str(caught.value.detail)
    with pytest.raises(HTTPException) as caught:
        places_api.fill_intros(origin="上海", band=None, category="美食", limit=None, session=session)
    assert caught.value.status_code == 400 and "未知分类" in str(caught.value.detail)


def test_api_places_reports_intro_progress(session, fake_llm) -> None:
    fetcher = FakeFetcher()
    original_fetcher = place_loader.default_fetcher
    original_geocoder = place_loader.default_geocoder
    place_loader.default_fetcher = lambda lat, lng, band: fetcher(lat, lng, band)
    place_loader.default_geocoder = RecordingGeocoder()
    try:
        payload = places_api.list_places(origin="上海", band="50_100", category=None, lat=None, lng=None,
                                         refresh=False, intros=True, intro_limit=None, session=session)
    finally:
        place_loader.default_fetcher = original_fetcher
        place_loader.default_geocoder = original_geocoder

    assert payload["count"] == 2
    assert payload["intro_stats"]["filled"] == 2
    assert payload["intro_pending"] == 0
    assert all(place["intro"] for place in payload["places"]), "popup 需要简介字段"
    assert "四分类" in payload["note"] and "去重" in payload["note"]
