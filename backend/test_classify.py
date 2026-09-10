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
from data_sources import overpass  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db import repository as repo  # noqa: E402
from db.models import UNCATEGORIZED, Place  # noqa: E402
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
# 构造样本(不触网):Overpass 风格 elements / 环内候选点
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


class FakeOverpassClient:
    """Overpass 客户端替身:记录批量抓取调用的半径/内圈/分组(验证真实抓取路径)。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def nearby_places_grouped(self, lat: float, lng: float, radius_m: float,
                              groups: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"lat": lat, "lng": lng, "radius_m": radius_m, "inner_radius_m": 0.0,
                           "groups": list(groups), **kwargs})
        return []

    def nearby_places_ring(self, lat: float, lng: float, outer_radius_m: float,
                           inner_radius_m: Any, groups: Any,
                           **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"lat": lat, "lng": lng, "radius_m": outer_radius_m,
                           "inner_radius_m": inner_radius_m, "groups": list(groups), **kwargs})
        return []


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
        # Overpass 走表单 ``data={"data": query}``;LLM 走 JSON 字节串 —— 两种都原样记下。
        self.calls.append({"method": method, "url": url, "data": data,
                           "query": data.get("data") if isinstance(data, Mapping) else None,
                           "timeout": timeout, "headers": dict(headers or {})})
        return FakeResponse(self.payload)


class QueuedSession(FakeSession):
    """按调用顺序返回预设 payload(用于"整组差集 OOM → 按选择器拆开后成功"这类降级)。"""

    def __init__(self, *payloads: Any) -> None:
        super().__init__(payloads[-1] if payloads else {})
        self.payloads = list(payloads) or [{}]

    def request(self, method: str, url: str, params: Any = None, data: Any = None,
                timeout: Any = None, headers: Any = None) -> FakeResponse:
        self.payload = self.payloads[min(len(self.calls), len(self.payloads) - 1)]
        return super().request(method, url, params, data, timeout, headers)


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
# 4. 检索:band 上限半径 + 四分类 tag 并集(一次请求,每组独立配额)
# --------------------------------------------------------------------------- #


def test_search_groups_cover_four_categories_with_independent_budgets() -> None:
    groups = classify.search_groups()
    assert {group["category"] for group in groups} == set(CATEGORY_PRIORITY)
    assert all(group["budget"] > 0 and group["tags"] for group in groups)
    assert classify.search_budget() == sum(group["budget"] for group in groups)
    ski = classify.search_groups([CATEGORY_SKI])
    assert ski and all(group["category"] == CATEGORY_SKI for group in ski)
    assert classify.search_budget([CATEGORY_SKI]) < classify.search_budget()


def test_search_tags_is_a_deduped_union_of_all_category_clues() -> None:
    tags = classify.search_tags()
    markers = [str(tag) for tag in tags]
    assert len(markers) == len(set(markers)), "并集内不应有重复选择器"
    assert tags == classify.search_tags(), "并集顺序稳定(便于构造出同样的查询语句)"
    joined = " ".join(markers)
    for clue in ("piste:type", "sport", "historic", "amenity", "natural", "place"):
        assert clue in joined, f"四分类线索 {clue} 应进检索并集"


def test_grouped_query_puts_every_category_in_one_request() -> None:
    query = overpass.build_grouped_query(
        SHANGHAI["lat"], SHANGHAI["lng"], 100_000, classify.search_groups(), query_timeout=120
    )
    assert query.startswith("[out:json][timeout:120];")
    assert query.count("out center ") == len(classify.SEARCH_GROUPS), "每组一个独立配额"
    for group in classify.SEARCH_GROUPS:
        assert f"out center {group['budget']};" in query
    assert "around:100000,31.230400,121.473700" in query, "按 band 上限半径检索"
    assert '["piste:type"]["name"]' in query and '["natural"~' in query
    assert query.count("[out:json]") == 1, "必须是**一次**请求"


def test_nearby_places_grouped_sends_a_single_post_and_keeps_osm_ids() -> None:
    payload = {"elements": [
        {"type": "node", "id": 1, "lat": 31.5, "lon": 121.5, "tags": {"name": "雪场", "piste:type": "downhill"}},
        {"type": "way", "id": 2, "center": {"lat": 31.6, "lon": 121.6}, "tags": {"name": "古镇", "historic": "town"}},
        {"type": "way", "id": 2, "center": {"lat": 31.6, "lon": 121.6}, "tags": {"name": "古镇", "tourism": "attraction"}},
    ]}
    fake = FakeSession(payload)
    client = overpass.OverpassClient(session=fake, retries=1, retry_backoff_s=0)
    rows = client.nearby_places_grouped(
        SHANGHAI["lat"], SHANGHAI["lng"], 100_000, classify.search_groups()
    )

    assert len(fake.calls) == 1, "四分类并集只发一次 HTTP 请求"
    assert fake.calls[0]["method"] == "POST"
    assert fake.calls[0]["timeout"] > 20, "冷启动批量抓取不受交互 20s 上限约束"
    assert len(rows) == 3, "抓取层不去重(同一实体被多组命中)"
    assert {(row["osm_type"], row["osm_id"]) for row in rows} == {("node", 1), ("way", 2)}
    # 去重 + 归类在 services 层完成
    classified = classify_places(rows)
    assert [(row["name"], row["category"]) for row in classified] == [("雪场", CATEGORY_SKI), ("古镇", CATEGORY_CULTURE)]


def test_grouped_ring_query_keeps_every_group_budget_inside_the_ring() -> None:
    """TASK-1d:远环改用集合差,六组的配额口径与单圆并集**完全一致**。"""
    query = overpass.build_grouped_ring_query(
        SHANGHAI["lat"], SHANGHAI["lng"], 300_000, 200_000, classify.search_groups(), query_timeout=240
    )
    assert query.startswith("[out:json][timeout:240];")
    assert query.count("[out:json]") == 1, "环形差集仍然是**一次**请求"
    assert query.count("out center ") == len(classify.SEARCH_GROUPS), "每组一个独立配额"
    for group in classify.SEARCH_GROUPS:
        assert f"out center {group['budget']};" in query, f"{group['group']} 配额不变"
    # 每组都要同时扫上限圆与下限圆,并用一个差集运算符把内圈减掉
    outer = query.count("around:300000,31.230400,121.473700")
    inner = query.count("around:200000,31.230400,121.473700")
    assert outer == inner > 0, "上下限圆的选择器数量必须对称"
    assert query.count("\n  -\n") == len(classify.SEARCH_GROUPS), "每组一个集合差"
    assert '["piste:type"]["name"](around:300000' in query, "require_name 口径不变"


def test_nearby_places_ring_sends_one_difference_request_per_group() -> None:
    """TASK-1d:环形差集**每组一次请求**,配额/去重口径与单圆并集完全一致。

    六组塞进一次请求会撞公共实例的单查询内存上限(实测 OOM / 504),拆开后单组可跑通。
    """
    payload = {"elements": [
        {"type": "node", "id": 1, "lat": 33.03, "lon": 121.47, "tags": {"name": "环内古镇", "historic": "town"}},
        {"type": "way", "id": 2, "center": {"lat": 32.9, "lon": 120.1}, "tags": {"name": "环内山峰", "natural": "peak"}},
    ]}
    fake = FakeSession(payload)
    client = overpass.OverpassClient(session=fake, retries=1, retry_backoff_s=0)
    groups = classify.search_groups()
    rows = client.nearby_places_ring(SHANGHAI["lat"], SHANGHAI["lng"], 300_000, 200_000, groups)

    assert len(fake.calls) == len(groups), "每组一次请求"
    for call, group in zip(fake.calls, groups):
        sent = call["query"]
        assert sent.count("[out:json]") == 1
        assert sent.count("out center ") == 1, "一次请求只查一组"
        assert f"out center {group['budget']};" in sent, f"{group['group']} 配额不变"
        assert f"[timeout:{overpass.DEFAULT_RING_QUERY_TIMEOUT}]" in sent, "环形查询放宽服务端超时"
        assert call["timeout"] == overpass.DEFAULT_RING_REQUEST_TIMEOUT_S, "客户端超时同步放宽"
        assert "around:300000" in sent and "around:200000" in sent and "\n  -\n" in sent
    # 抓取层仍不去重(同一实体被多组命中),但跨组合并后按由近及远排序(古镇 200 km < 山峰 226 km)
    assert len(rows) == 2 * len(groups)
    assert [row["name"] for row in rows] == ["环内古镇"] * len(groups) + ["环内山峰"] * len(groups)
    assert {(row["osm_type"], row["osm_id"]) for row in rows} == {("node", 1), ("way", 2)}
    classified = classify_places(rows)
    assert [(row["name"], row["category"]) for row in classified] == [
        ("环内古镇", CATEGORY_CULTURE), ("环内山峰", CATEGORY_NATURE)
    ], "去重键 (type, id) 与归类优先级口径不变"


OOM_REMARK = "runtime error: Query run out of memory using about 2048 MB of RAM."


def test_ring_falls_back_to_per_selector_difference_for_the_heavy_ski_group() -> None:
    """实测**滑雪场**组整条差集在公共实例上 OOM(2048 MB),必须能按选择器拆开重发。

    拆分只改"发几条查询",不改口径:每条仍是环形差集、仍带**该组**配额,合并后按
    ``(type, id)`` 去重并截到配额,归类优先级照旧由 :func:`classify_places` 决定。
    """
    ski = next(group for group in classify.search_groups() if group["category"] == CATEGORY_SKI)
    selectors = overpass.tags_to_selectors(ski["tags"])
    assert len(selectors) > 1, "滑雪场组本来就是多选择器并集,才需要拆分降级"
    payload = {"elements": [
        {"type": "node", "id": 1, "lat": 33.03, "lon": 121.47,
         "tags": {"name": "环内雪场", "piste:type": "downhill", "sport": "skiing"}},
        {"type": "way", "id": 2, "center": {"lat": 32.9, "lon": 120.1},
         "tags": {"name": "环内雪道", "piste:type": "downhill"}},
    ]}
    session = QueuedSession({"remark": OOM_REMARK, "elements": []}, *([payload] * len(selectors)))
    client = overpass.OverpassClient(session=session, retries=1, retry_backoff_s=0)
    rows = client.nearby_places_ring(SHANGHAI["lat"], SHANGHAI["lng"], 300_000, 200_000, [ski])

    assert len(session.calls) == 1 + len(selectors), "整组 1 次 + 每个选择器各 1 次"
    for call, selector in zip(session.calls, [None, *selectors]):
        sent = call["query"]
        assert sent.count("out center ") == 1, "一次请求只查一组/一个选择器"
        assert f"out center {ski['budget']};" in sent, f"{ski['group']} 配额不变"
        assert "\n  -\n" in sent and "around:300000" in sent and "around:200000" in sent
        assert call["timeout"] == overpass.DEFAULT_RING_REQUEST_TIMEOUT_S
        if selector is not None:
            assert f'{selector}["name"]' in sent, "每个选择器单独一条差集查询"
    # 同两个地物被每个选择器各命中一次 → 按 (type, id) 去重后只剩 2 条(未超配额)
    assert {(row["osm_type"], row["osm_id"]) for row in rows} == {("node", 1), ("way", 2)}
    assert len(rows) == 2
    assert [(row["name"], row["category"]) for row in classify_places(rows)] == [
        ("环内雪场", CATEGORY_SKI), ("环内雪道", CATEGORY_SKI)
    ], "归类优先级口径不变"


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


def test_loader_fetches_every_band_as_a_ring_difference() -> None:
    assert place_loader.SEARCH_GROUPS == classify.search_groups()
    assert {group["category"] for group in place_loader.SEARCH_GROUPS} == set(CATEGORY_PRIORITY)
    assert place_loader.SEARCH_TAGS == classify.search_tags()
    assert place_loader.GROUP_REQUEST_TIMEOUT > 20, "冷启动批量抓取不受交互 20s 上限约束"
    assert place_loader.RING_REQUEST_TIMEOUT > place_loader.GROUP_REQUEST_TIMEOUT, "差集更慢,超时放宽一档"

    client = FakeOverpassClient()
    band = DISTANCE_BANDS[1]
    place_loader.default_fetcher(SHANGHAI["lat"], SHANGHAI["lng"], band, client=client)
    assert len(client.calls) == 1, "四分类并集只发一次 Overpass 请求"
    call = client.calls[0]
    assert call["radius_m"] == band_radius_m(band), "外圈仍是分段**上限半径**"
    assert call["inner_radius_m"] == band_inner_radius_m(band) == 100_000, "内圈 = 分段下限(配额只花在环内)"
    assert call["groups"] == classify.search_groups(), "一次查完四分类 tag 并集"
    assert call["with_id"] is True, "带 OSM 身份才能按 (type, id) 去重"
    assert call["query_timeout"] == place_loader.RING_QUERY_TIMEOUT
    assert call["request_timeout"] == place_loader.RING_REQUEST_TIMEOUT


def test_loader_ring_covers_every_band_with_positive_lower_bound() -> None:
    """四个分段下限都 > 0,所以全部都走环形差集(远环稀少 bug 的根因就在这里)。"""
    client = FakeOverpassClient()
    for band in DISTANCE_BANDS:
        place_loader.default_fetcher(SHANGHAI["lat"], SHANGHAI["lng"], band, client=client)
    assert [call["inner_radius_m"] for call in client.calls] == [
        band_inner_radius_m(band) for band in DISTANCE_BANDS
    ]
    assert all(call["inner_radius_m"] > 0 for call in client.calls)
    assert [call["radius_m"] for call in client.calls] == [band_radius_m(band) for band in DISTANCE_BANDS]


def test_loader_degrades_to_single_circle_when_band_low_is_zero() -> None:
    """下限为 0 的分段没有内圈可减 → 退化成单圆查询与单圆超时(行为同 TASK-1b)。"""
    client = FakeOverpassClient()
    band = {"key": "0_50", "label": "0-50 km", "low": 0, "high": 50}
    assert band_inner_radius_m(band) == 0.0
    place_loader.default_fetcher(SHANGHAI["lat"], SHANGHAI["lng"], band, client=client)
    call = client.calls[0]
    assert call["inner_radius_m"] == 0.0
    assert call["radius_m"] == 50_000
    assert call["query_timeout"] == place_loader.GROUP_QUERY_TIMEOUT
    assert call["request_timeout"] == place_loader.GROUP_REQUEST_TIMEOUT




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
    band = DISTANCE_BANDS[0]
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
        places_api.fill_intros(origin="上海", band="0_50", category=None, limit=None, session=session)
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
