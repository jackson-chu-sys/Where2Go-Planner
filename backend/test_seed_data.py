"""TASK-1c 单测:人工种子数据(滑雪/运动)+ 与 OSM 合并去重 + 来源标注 + 起点逆地理编码。

全程不触网:

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,任何偷偷联网都会当场失败;
* 种子数据是 :mod:`services.seed_data` 里的模块常量,校验/筛选/去重都是纯函数;
* Overpass、Nominatim(正向 + **逆向**)、LLM 一律用替身注入;
* DB 用 ``tmp_path`` 下的临时 SQLite 文件,不碰 ``backend/data/``。

注意:整套单测的种子开关默认是**关**的(``backend/conftest.py``),这样 TASK-1a/1b 那 87 个
用例里对条数的精确断言不受影响;本文件需要种子时,要么显式 ``monkeypatch.setenv`` 打开
(:fixture:`seeds_on`),要么给 :func:`services.place_loader.load_segment` 传自造种子列表。

运行:``python -m pytest backend/ -q``
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import Any, Optional

import pytest
import requests

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import places as places_api  # noqa: E402
from app.main import app  # noqa: E402
from data_sources import DataSourceError  # noqa: E402
from db import init_db, make_engine, open_session, session_factory  # noqa: E402
from db import repository as repo  # noqa: E402
from db.models import OSM_SOURCE, SEED_SOURCE, SOURCE_TAG, is_seed, place_source  # noqa: E402
from services import intro as intro_service, place_loader, seed_data  # noqa: E402
from services.bands import band_keys, in_band, require_band  # noqa: E402
from services.classify import (  # noqa: E402
    CATEGORY_NATURE,
    CATEGORY_SKI,
    CATEGORY_SPORT,
    categorize,
    is_known_category,
)

# --------------------------------------------------------------------------- #
# 样本:上海为起点,沿正北按公里摆点(1 度纬度 ≈ 111.32 km)
# --------------------------------------------------------------------------- #

SHANGHAI = {"lat": 31.2304, "lng": 121.4737}
KM_PER_DEGREE = 111.32
BAND = "50_100"
#: 需求点名要有种子的知名雪场(TASK-1c 验收项)
REQUIRED_RESORTS = ("万龙", "云顶", "南山", "军都山", "北大湖", "亚布力")
SKI_HINT_WORDS = ("ski", "snowboard", "piste", "winter_sports")


def point_at(
    km: float,
    *,
    name: str,
    tags: Optional[dict[str, Any]] = None,
    osm_type: str = "node",
    osm_id: int = 1,
) -> dict[str, Any]:
    """构造距上海 ``km`` 公里(正北)的 Overpass 风格候选点。"""
    return {
        "osm_type": osm_type,
        "osm_id": osm_id,
        "name": name,
        "lat": round(SHANGHAI["lat"] + km / KM_PER_DEGREE, 6),
        "lng": SHANGHAI["lng"],
        "tags": dict(tags or {}),
    }


def seed_at(
    km: float,
    *,
    name: str,
    category: str = CATEGORY_SKI,
    tags: Optional[dict[str, Any]] = None,
    intro: str = "人工补录的测试雪场,雪道分级清楚。",
) -> dict[str, Any]:
    """构造距上海 ``km`` 公里(正北)的种子条目(带 ``source=种子`` 标签)。"""
    if tags is None:
        tags = seed_data.SKI_DOWNHILL if category == CATEGORY_SKI else seed_data.SPORT_CLIMBING
    return {
        "name": name,
        "lat": round(SHANGHAI["lat"] + km / KM_PER_DEGREE, 6),
        "lng": SHANGHAI["lng"],
        "category": category,
        "tags": {**dict(tags), SOURCE_TAG: SEED_SOURCE},
        "intro": intro,
    }


class FakeFetcher:
    """Overpass 替身:记录调用次数,返回预设候选。"""

    def __init__(self, payload: Optional[list[dict[str, Any]]] = None) -> None:
        self.payload = [] if payload is None else payload
        self.calls = 0

    def __call__(self, lat: float, lng: float, band: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls += 1
        return [dict(row) for row in self.payload]


class ExplodingFetcher:
    """已入库分段再调它就说明"又触网了",直接失败。"""

    def __call__(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("已入库的 (城市, band) 不应再次抓取")


def recording_geocoder(city: str) -> dict[str, Any]:
    return {"lat": SHANGHAI["lat"], "lng": SHANGHAI["lng"], "display_name": f"{city}市, 中国"}


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
    engine = make_engine(f"sqlite:///{tmp_path / 'seeds_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


@pytest.fixture()
def seeds_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """打开种子开关(conftest 里整套默认是关的),用**真实的 49 条种子**。"""
    monkeypatch.setenv(seed_data.ENV_SEEDS, "1")


@pytest.fixture()
def offline_sources(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """API 层不接收 fetcher 参数,这里替换模块默认实现(仍是替身,不触网)。"""
    fetcher = FakeFetcher([point_at(60, name="云州峰", tags={"natural": "peak"}, osm_id=1)])
    monkeypatch.setattr(place_loader, "default_fetcher", lambda lat, lng, band: fetcher(lat, lng, band))
    monkeypatch.setattr(place_loader, "default_geocoder", recording_geocoder)
    return SimpleNamespace(fetcher=fetcher)


# --------------------------------------------------------------------------- #
# 1. 种子数据本身:校验通过、覆盖知名雪场、坐标/简介/分类自洽
# --------------------------------------------------------------------------- #


def test_all_seeds_pass_validation() -> None:
    assert seed_data.validate() == [], "种子数据自检必须零问题"
    assert len(seed_data.SEEDS) >= 40, "滑雪/运动缺口应有一批种子垫底"


def test_seed_catalogue_covers_well_known_resorts() -> None:
    names = "".join(seed["name"] for seed in seed_data.SEEDS)
    missing = [keyword for keyword in REQUIRED_RESORTS if keyword not in names]
    assert not missing, f"需求点名的知名雪场缺失:{missing}"


def test_seed_counts_by_category() -> None:
    stats = seed_data.seed_stats(list(seed_data.SEEDS))
    assert stats["by_category"] == {CATEGORY_SKI: len(seed_data.SKI_SEEDS),
                                   CATEGORY_SPORT: len(seed_data.SPORT_SEEDS)}
    assert stats["total"] == len(seed_data.SEEDS) == stats["total_defined"]
    assert len(seed_data.SKI_SEEDS) >= 10 and len(seed_data.SPORT_SEEDS) >= 10, \
        "滑雪与运动两类都要有若干条可展示的目的地(验收项)"


def test_seed_declared_category_matches_tag_classification() -> None:
    """种子没有 OSM id,分类完全由 tags 决定 —— 声明值必须与归类引擎一致。"""
    for seed in seed_data.SEEDS:
        assert is_known_category(seed["category"]), seed["name"]
        assert categorize(seed["tags"]) == seed["category"], \
            f"{seed['name']}:tags={seed['tags']} 归类结果与声明的 {seed['category']} 不一致"


def test_sport_seeds_do_not_carry_ski_hints() -> None:
    """运动类种子若带滑雪线索词,会被优先级抢去"滑雪场"类,归错类。"""
    for seed in seed_data.SPORT_SEEDS:
        joined = " ".join(f"{key}={value}" for key, value in seed["tags"].items()).lower()
        hits = [word for word in SKI_HINT_WORDS if word in joined]
        assert not hits, f"{seed['name']} 的运动 tag 带滑雪线索 {hits}"


def test_seed_intros_are_static_and_within_limit() -> None:
    for seed in seed_data.SEEDS:
        intro = str(seed["intro"]).strip()
        assert intro, f"{seed['name']} 缺静态简介(种子不应依赖 LLM)"
        assert len(intro) <= seed_data.MAX_SEED_INTRO_CHARS, f"{seed['name']} 简介过长"


def test_seed_coordinates_are_in_china() -> None:
    for seed in seed_data.SEEDS:
        assert 18.0 <= float(seed["lat"]) <= 54.0, f"{seed['name']} 纬度异常"
        assert 73.0 <= float(seed["lng"]) <= 135.5, f"{seed['name']} 经度异常"


def test_real_seed_names_and_coordinates_are_unique() -> None:
    keys = {seed_data.name_key(seed["name"]) for seed in seed_data.SEEDS}
    assert len(keys) == len(seed_data.SEEDS), "种子名字(归一后)不能重复,否则去重键冲突"
    coords = {(round(float(s["lat"]), 5), round(float(s["lng"]), 5)) for s in seed_data.SEEDS}
    assert len(coords) == len(seed_data.SEEDS), "不同种子不应共用同一坐标(疑似复制粘贴录错)"


# --------------------------------------------------------------------------- #
# 2. 开关与归一化:WHERE2GO_SEEDS、source 标签、按分类取数
# --------------------------------------------------------------------------- #


def test_env_switch_defaults_on_and_honours_disable_values() -> None:
    assert seed_data.enabled({}) is True, "线上默认开(未设置环境变量)"
    assert seed_data.enabled({seed_data.ENV_SEEDS: ""}) is True
    for value in ("0", "false", "no", "off", "关", "关闭", "OFF", " Off "):
        assert seed_data.enabled({seed_data.ENV_SEEDS: value}) is False, value
        assert seed_data.load_seeds(environ={seed_data.ENV_SEEDS: value}) == []
    assert seed_data.enabled({seed_data.ENV_SEEDS: "1"}) is True


def test_whole_test_suite_runs_with_seeds_off() -> None:
    """记录 conftest 的约定:整套单测默认关种子,既有条数断言因此不受影响。"""
    assert os.environ.get(seed_data.ENV_SEEDS) == "off"
    assert seed_data.load_seeds() == []
    assert seed_data.seed_stats()["enabled"] is False
    assert seed_data.seed_stats()["total_defined"] == len(seed_data.SEEDS), \
        "关闭时仍要报告定义了多少条(前端图例用)"


def test_load_seeds_marks_source_tag_and_returns_copies() -> None:
    seeds = seed_data.load_seeds(environ={seed_data.ENV_SEEDS: "1"})
    assert len(seeds) == len(seed_data.SEEDS)
    assert all(seed["tags"][SOURCE_TAG] == SEED_SOURCE for seed in seeds), "来源标注必须打在 tags.source 上"
    seeds[0]["tags"]["被改了"] = True
    seeds[0]["name"] = "被改了"
    assert "被改了" not in seed_data.SEEDS[0]["tags"], "load_seeds 必须返回拷贝,不能污染常量"
    assert seed_data.SEEDS[0]["name"] != "被改了"


def test_load_seeds_filters_by_categories() -> None:
    env = {seed_data.ENV_SEEDS: "1"}
    ski = seed_data.load_seeds(categories=[CATEGORY_SKI], environ=env)
    assert ski and all(seed["category"] == CATEGORY_SKI for seed in ski)
    assert len(ski) == len(seed_data.SKI_SEEDS)
    assert seed_data.load_seeds(categories=[CATEGORY_NATURE], environ=env) == [], "自然风光不靠种子补"


# --------------------------------------------------------------------------- #
# 3. 去重纯函数:名字 + 坐标
# --------------------------------------------------------------------------- #


def test_name_key_ignores_decoration_and_case() -> None:
    assert seed_data.name_key(" 万龙滑雪场 ") == "万龙滑雪场"
    assert seed_data.name_key("Wanlong Ski Resort") == "wanlongskiresort"
    assert seed_data.name_key("南山滑雪场(密云)") == "南山滑雪场密云"
    assert seed_data.name_key(None) == ""


def test_same_place_needs_both_name_and_distance() -> None:
    near = SHANGHAI["lat"] + 1 / KM_PER_DEGREE
    assert seed_data.same_place("军都山滑雪场", SHANGHAI["lat"], SHANGHAI["lng"],
                               "军都山滑雪场", SHANGHAI["lat"], SHANGHAI["lng"])
    assert seed_data.same_place("南山滑雪场", SHANGHAI["lat"], SHANGHAI["lng"],
                               "北京南山滑雪场", near, SHANGHAI["lng"]), "短名被长名包含即同一地"
    far_lat = SHANGHAI["lat"] + (seed_data.DEDUPE_RADIUS_KM + 1) / KM_PER_DEGREE
    assert not seed_data.same_place("南山滑雪场", SHANGHAI["lat"], SHANGHAI["lng"],
                                   "南山滑雪场", far_lat, SHANGHAI["lng"]), "超出半径就算另一个地方"
    assert not seed_data.same_place("万龙滑雪场", SHANGHAI["lat"], SHANGHAI["lng"],
                                   "云顶滑雪公园", SHANGHAI["lat"], SHANGHAI["lng"])
    assert not seed_data.same_place("湖", SHANGHAI["lat"], SHANGHAI["lng"],
                                   "滴水湖", SHANGHAI["lat"], SHANGHAI["lng"]), "过短的名字不参与包含判定"


def test_attach_seeds_skips_place_osm_already_covers() -> None:
    osm_rows = [point_at(60, name="云州峰滑雪场", tags={"piste:type": "downhill"}, osm_id=7)]
    seeds = [seed_at(60, name="云州峰滑雪场")]
    assert seed_data.attach_seeds(osm_rows, seeds) == [], "OSM 已抓到就不重复补(OSM 优先)"


def test_attach_seeds_matches_contained_name_within_radius() -> None:
    osm_rows = [point_at(60, name="北京南山滑雪场", tags={"piste:type": "downhill"}, osm_id=7)]
    seeds = [seed_at(60.5, name="南山滑雪场")]
    assert seed_data.attach_seeds(osm_rows, seeds) == []


def test_attach_seeds_keeps_same_name_outside_radius() -> None:
    osm_rows = [point_at(60, name="云顶滑雪公园", osm_id=7)]
    far = seed_at(70, name="云顶滑雪公园")
    kept = seed_data.attach_seeds(osm_rows, [far])
    assert [row["name"] for row in kept] == ["云顶滑雪公园"], "同名但相隔 10 km 是两个地方"


def test_attach_seeds_dedupes_within_seed_list_itself() -> None:
    seeds = [seed_at(60, name="太舞滑雪场"), seed_at(60, name="太舞滑雪场")]
    assert [row["name"] for row in seed_data.attach_seeds([], seeds)] == ["太舞滑雪场"]


def test_attach_seeds_does_not_mutate_inputs() -> None:
    osm_rows = [point_at(60, name="云州峰", osm_id=7)]
    seeds = [seed_at(70, name="富龙滑雪场")]
    snapshot_rows = [dict(row) for row in osm_rows]
    snapshot_seeds = [dict(row) for row in seeds]
    kept = seed_data.attach_seeds(osm_rows, seeds)
    kept[0]["name"] = "改了"
    assert osm_rows == snapshot_rows
    assert seeds == snapshot_seeds, "attach_seeds 返回的必须是拷贝"


def test_attach_seeds_radius_is_configurable() -> None:
    osm_rows = [point_at(60, name="万龙滑雪场", osm_id=7)]
    seeds = [seed_at(61.5, name="万龙滑雪场")]
    assert seed_data.attach_seeds(osm_rows, seeds, radius_km=0.5), "半径收紧后不再算同一地"
    assert seed_data.attach_seeds(osm_rows, seeds, radius_km=5.0) == []


def test_seeds_in_band_filters_ring_and_sorts_by_distance() -> None:
    band = require_band(BAND)
    seeds = [
        seed_at(20, name="城里雪场"),            # 环外:太近
        seed_at(90, name="远处雪场"),
        seed_at(60, name="近处雪场"),
        seed_at(150, name="下一段雪场"),          # 环外:太远
    ]
    kept = seed_data.seeds_in_band(seeds, SHANGHAI["lat"], SHANGHAI["lng"], band)
    assert [row["name"] for row in kept] == ["近处雪场", "远处雪场"], "只留环内,并由近及远排序"
    assert all(in_band(row["distance_km"], band) for row in kept), "与 filter_to_band 同一口径(low<=d<high)"
    assert [row["distance_km"] for row in kept] == sorted(row["distance_km"] for row in kept)
    assert seed_data.seeds_in_band([], SHANGHAI["lat"], SHANGHAI["lng"], band) == []


# --------------------------------------------------------------------------- #
# 4. 来源标注:models 派生 + repository 计数/出参
# --------------------------------------------------------------------------- #


def test_place_source_derives_seed_and_osm() -> None:
    assert place_source({SOURCE_TAG: SEED_SOURCE}) == SEED_SOURCE
    assert is_seed({SOURCE_TAG: SEED_SOURCE})
    assert place_source({}) == OSM_SOURCE, "存量 OSM 行没有 source 键,一律按 OSM 标注"
    assert place_source(None) == OSM_SOURCE
    assert place_source({SOURCE_TAG: "survey"}) == OSM_SOURCE, "OSM 原始 tag 值不能被误判成种子"
    assert not is_seed({"natural": "peak"})


def test_place_to_dict_exposes_source(session) -> None:
    repo.upsert_places(session, origin_city="上海", band=BAND, items=place_loader.to_seed_items(
        [seed_at(60, name="种子雪场")]))
    repo.upsert_places(session, origin_city="上海", band=BAND,
                       items=place_loader.to_place_items([point_at(70, name="云州峰",
                                                                   tags={"natural": "peak"}, osm_id=3)]))
    rows = {row["name"]: row for row in repo.list_places(
        session, origin_city="上海", band=BAND,
        origin_lat=SHANGHAI["lat"], origin_lng=SHANGHAI["lng"])}
    assert rows["种子雪场"]["source"] == SEED_SOURCE
    assert rows["云州峰"]["source"] == OSM_SOURCE
    assert rows["种子雪场"]["intro"], "种子自带静态简介,入库即有"


def test_count_by_source_counts_seeds_and_osm(session) -> None:
    repo.upsert_places(session, origin_city="上海", band=BAND, items=place_loader.to_seed_items(
        [seed_at(60, name="种子雪场甲"), seed_at(70, name="种子雪场乙", category=CATEGORY_SPORT,
                                              tags=seed_data.SPORT_CYCLING, intro="人工补录的骑行路线。")]))
    repo.upsert_places(session, origin_city="上海", band=BAND,
                       items=place_loader.to_place_items([point_at(80, name="云州峰",
                                                                   tags={"natural": "peak"}, osm_id=3)]))
    counts = repo.count_by_source(session, origin_city="上海", band=BAND)
    assert counts == {OSM_SOURCE: 1, SEED_SOURCE: 2}
    assert repo.count_by_source(session, origin_city="上海", band=BAND, category=CATEGORY_SKI) == {
        OSM_SOURCE: 0, SEED_SOURCE: 1}
    assert repo.count_by_source(session, origin_city="北京", band=BAND) == {OSM_SOURCE: 0, SEED_SOURCE: 0}, \
        "两个键恒在,前端不必判空"


# --------------------------------------------------------------------------- #
# 5. 入库条目形状与身份(名字 + 坐标 指纹兜底)
# --------------------------------------------------------------------------- #


def test_to_seed_items_shape_and_identity() -> None:
    items = place_loader.to_seed_items([seed_at(60, name="万龙滑雪场", intro="崇礼老牌雪场。")])
    assert len(items) == 1
    item = items[0]
    assert item["name"] == "万龙滑雪场"
    assert item["category"] == CATEGORY_SKI
    assert item["intro"] == "崇礼老牌雪场。"
    assert item["tags"][SOURCE_TAG] == SEED_SOURCE
    assert item["osm_type"] == "point", "没有 OSM id 的种子走指纹兜底类型"
    assert item["osm_id"] < 0, "负数 id 不会与真实 OSM id 撞车"
    assert "intro" not in place_loader.to_place_items(
        [point_at(60, name="云州峰", tags={"natural": "peak"})])[0], \
        "OSM 条目的简介仍由 LLM 事后补,to_place_items 不带 intro"


def test_seed_identity_is_stable_and_distinct() -> None:
    first = place_loader.place_identity(seed_at(60, name="万龙滑雪场"))
    again = place_loader.place_identity(seed_at(60, name="万龙滑雪场"))
    other = place_loader.place_identity(seed_at(60, name="云顶滑雪公园"))
    assert first == again, "同名字同坐标 → 同身份,重复写入被唯一键挡住"
    assert first != other
    assert place_loader.place_identity(point_at(60, name="云州峰", osm_id=9)) == ("node", 9)


# --------------------------------------------------------------------------- #
# 6. 编排:抓取路径与读库路径都合并种子,补种幂等、不重抓、不调 LLM
# --------------------------------------------------------------------------- #


def test_fetch_path_merges_seeds_and_dedupes_against_osm(session) -> None:
    fetcher = FakeFetcher([
        point_at(20, name="城里公园", tags={"leisure": "park"}, osm_id=3),      # 环外
        point_at(60, name="云州峰滑雪场", tags={"piste:type": "downhill"}, osm_id=1),
    ])
    seeds = [
        seed_at(60, name="云州峰滑雪场"),                    # 与 OSM 同一地 → 不补
        seed_at(70, name="测试雪场"),                        # 缺口 → 补
        seed_at(150, name="下一段雪场"),                     # 环外 → 不补
    ]
    outcome = place_loader.load_segment(
        session, city="上海", band=BAND, fetcher=fetcher, geocoder=recording_geocoder,
        seeds=seeds, intros=False)
    assert outcome.seeded == 1
    assert outcome.written == 2
    assert outcome.segment["place_count"] == 2
    assert outcome.counts_by_source == {OSM_SOURCE: 1, SEED_SOURCE: 1}
    assert outcome.counts_by_category == {CATEGORY_SKI: 2}
    assert sorted(row["name"] for row in outcome.places) == ["云州峰滑雪场", "测试雪场"]
    sources = {row["name"]: row["source"] for row in outcome.places}
    assert sources == {"云州峰滑雪场": OSM_SOURCE, "测试雪场": SEED_SOURCE}
    seeded_row = next(row for row in outcome.places if row["name"] == "测试雪场")
    assert seeded_row["intro"], "种子入库即带静态简介"


def test_explicit_empty_seeds_disable_merging(session) -> None:
    fetcher = FakeFetcher([point_at(60, name="云州峰", tags={"natural": "peak"}, osm_id=1)])
    outcome = place_loader.load_segment(
        session, city="上海", band=BAND, fetcher=fetcher, geocoder=recording_geocoder,
        seeds=[], intros=False)
    assert outcome.seeded == 0
    assert outcome.counts_by_source == {OSM_SOURCE: 1, SEED_SOURCE: 0}


def test_read_path_seeding_is_idempotent_and_never_refetches(session) -> None:
    seeds = [seed_at(70, name="测试雪场")]
    place_loader.load_segment(session, city="上海", band=BAND, fetcher=FakeFetcher(
        [point_at(60, name="云州峰", tags={"natural": "peak"}, osm_id=1)]),
        geocoder=recording_geocoder, seeds=seeds, intros=False)

    first = place_loader.load_segment(session, city="上海", band=BAND, fetcher=ExplodingFetcher(),
                                     geocoder=recording_geocoder, seeds=seeds, intros=False)
    assert first.source == place_loader.SOURCE_DB and first.network_used is False
    assert first.seeded == 0, "已经补过的种子不能再补一遍"
    assert first.segment["place_count"] == 2

    second = place_loader.load_segment(session, city="上海", band=BAND, fetcher=ExplodingFetcher(),
                                      geocoder=recording_geocoder, seeds=seeds, intros=False)
    assert second.seeded == 0 and second.segment["place_count"] == 2
    assert second.counts_by_source == {OSM_SOURCE: 1, SEED_SOURCE: 1}


def test_existing_segment_gets_seeded_without_refetch(session) -> None:
    """存量库(TASK-1b 抓的、没有种子)二次查询就能拿到种子,不必重抓 Overpass。"""
    place_loader.load_segment(session, city="上海", band=BAND, fetcher=FakeFetcher(
        [point_at(60, name="云州峰", tags={"natural": "peak"}, osm_id=1)]),
        geocoder=recording_geocoder, seeds=[], intros=False)
    before = repo.get_segment(session, origin_city="上海", band=BAND)
    assert before.place_count == 1 and before.source == place_loader.SOURCE_OVERPASS
    fetched_at = before.fetched_at

    outcome = place_loader.load_segment(session, city="上海", band=BAND, fetcher=ExplodingFetcher(),
                                       geocoder=recording_geocoder,
                                       seeds=[seed_at(70, name="测试雪场")], intros=False)
    assert outcome.seeded == 1 and outcome.network_used is False
    assert outcome.segment["place_count"] == 2
    after = repo.get_segment(session, origin_city="上海", band=BAND)
    assert after.fetched_at == fetched_at, "补种是本地操作,不能改动抓取水位时间"
    assert after.source == place_loader.SOURCE_OVERPASS


def test_ensure_seeded_returns_zero_when_band_has_no_seeds(session) -> None:
    origin = {"city": "上海", "name": "上海", **SHANGHAI}
    band = require_band(BAND)
    assert place_loader.ensure_seeded(session, origin=origin, band=band, seeds=[]) == 0
    assert place_loader.ensure_seeded(session, origin=origin, band=band,
                                     seeds=[seed_at(20, name="环外雪场")]) == 0
    assert repo.count_places(session, origin_city="上海", band=BAND) == 0


def test_ensure_seeded_skips_row_already_in_db(session) -> None:
    origin = {"city": "上海", "name": "上海", **SHANGHAI}
    band = require_band(BAND)
    seeds = [seed_at(70, name="测试雪场")]
    assert place_loader.ensure_seeded(session, origin=origin, band=band, seeds=seeds) == 1
    assert place_loader.ensure_seeded(session, origin=origin, band=band, seeds=seeds) == 0
    assert repo.count_by_source(session, origin_city="上海", band=BAND) == {OSM_SOURCE: 0, SEED_SOURCE: 1}


def test_seeds_never_reach_the_llm(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """种子自带静态简介 → ``fill_missing_intros`` 不该把它们送去调 LLM(省额度)。"""
    scanned: list[list[str]] = []

    def spy(session_arg, *, origin_city=None, band=None, category=None, limit=None,
            workers=0, commit=True) -> dict[str, Any]:
        rows = repo.select_places(session_arg, origin_city=origin_city, band=band,
                                 category=category, missing_intro=True)
        scanned.append([row.name for row in rows])
        return {"scanned": len(rows), "filled": 0, "failed": 0, "pending": len(rows), "provider": "spy"}

    monkeypatch.setattr(intro_service, "fill_missing_intros", spy)
    outcome = place_loader.load_segment(
        session, city="上海", band=BAND,
        fetcher=FakeFetcher([point_at(60, name="云州峰", tags={"natural": "peak"}, osm_id=1)]),
        geocoder=recording_geocoder, seeds=[seed_at(70, name="测试雪场")], intros=True)
    assert scanned == [["云州峰"]], f"只有缺简介的 OSM 行该进 LLM 队列,实际:{scanned}"
    assert outcome.intro_stats["scanned"] == 1


def test_record_seed_segment_marks_seed_only_watermark(session) -> None:
    band = require_band(BAND)
    origin = {"city": "上海", "name": "上海", **SHANGHAI}
    record = place_loader.record_seed_segment(session, city="上海", band=band, origin=origin)
    session.commit()
    assert record.source == place_loader.SOURCE_SEED == "seed"
    assert record.place_count == 0
    overview = repo.segment_overview(session, origin_city="上海")
    assert [(row["band"], row["source"]) for row in overview] == [(BAND, "seed")]
    assert set(band_keys()) == {"50_100", "100_200", "200_300", "300_500"}


# --------------------------------------------------------------------------- #
# 7. 真实种子接进 load_segment(开关打开时)
# --------------------------------------------------------------------------- #


def test_real_seeds_merge_into_real_city_band(session, seeds_on) -> None:
    outcome = place_loader.load_segment(
        session, city="上海", band=BAND,
        fetcher=FakeFetcher([point_at(60, name="云州峰", tags={"natural": "peak"}, osm_id=1)]),
        geocoder=recording_geocoder, intros=False)
    assert outcome.seeded >= 1, "上海 50-100 km 环内应有人工种子(如滴水湖/阳澄湖)"
    assert outcome.counts_by_source[SEED_SOURCE] == outcome.seeded
    seeded_rows = [row for row in outcome.places if row["source"] == SEED_SOURCE]
    assert seeded_rows and all(row["intro"] for row in seeded_rows)
    assert all(row["category"] in (CATEGORY_SKI, CATEGORY_SPORT) for row in seeded_rows)

    again = place_loader.load_segment(session, city="上海", band=BAND, fetcher=ExplodingFetcher(),
                                     geocoder=recording_geocoder, intros=False)
    assert again.seeded == 0 and again.counts_by_source == outcome.counts_by_source


def test_real_ski_seeds_show_up_around_beijing(session, seeds_on) -> None:
    beijing = {"lat": 39.9042, "lng": 116.4074}
    outcome = place_loader.load_segment(
        session, city="北京", band=BAND,
        fetcher=FakeFetcher([]),
        geocoder=lambda city: {**beijing, "display_name": f"{city}市, 中国"}, intros=False)
    names = [row["name"] for row in outcome.places]
    assert "南山滑雪场" in names, f"需求点名的京郊雪场应被种子补上:{names}"
    assert outcome.source == place_loader.SOURCE_OVERPASS
    assert all(row["source"] == SEED_SOURCE for row in outcome.places)


# --------------------------------------------------------------------------- #
# 8. 起点定位:浏览器 GPS → Nominatim 逆地理编码(失败不报错)
# --------------------------------------------------------------------------- #


def test_city_from_display_name_picks_city_token() -> None:
    assert place_loader.city_from_display_name("浦东新区, 上海市, 200120, 中国") == "上海市"
    assert place_loader.city_from_display_name("某某院, 东城区, 北京市, 100010, 中国") == "北京市"
    assert place_loader.city_from_display_name("崇礼区, 张家口市, 河北省, 中国") == "张家口市"
    assert place_loader.city_from_display_name("呼伦贝尔地区, 内蒙古自治区, 中国") == "呼伦贝尔地区"
    assert place_loader.city_from_display_name("中国") == ""
    assert place_loader.city_from_display_name(None) == ""
    assert place_loader.city_from_display_name("Some Village, 中国") == "Some Village", \
        "都不像城市时退回最粗的一段"


def test_unnamed_origin_rounds_coordinates() -> None:
    assert place_loader.unnamed_origin(31.2304, 121.4737) == "我的位置(31.23,121.47)"
    assert place_loader.unnamed_origin(40.172712, 116.410148) == "我的位置(40.17,116.41)"


def test_resolve_reverse_origin_keeps_gps_coordinates() -> None:
    seen: list[tuple[float, float]] = []

    def fake(lat: float, lng: float) -> dict[str, Any]:
        seen.append((lat, lng))
        return {"lat": 31.5, "lng": 121.9, "display_name": "浦东新区, 上海市, 200120, 中国"}

    origin = place_loader.resolve_reverse_origin(31.2304, 121.4737, reverse_geocoder=fake)
    assert origin == {"city": "上海市", "name": "浦东新区, 上海市, 200120, 中国",
                     "lat": 31.2304, "lng": 121.4737, "resolved": True}
    assert seen == [(31.2304, 121.4737)]


@pytest.mark.parametrize("boom", [
    DataSourceError("nominatim", "限流"),
    ValueError("坐标必须为数字"),
    TypeError("bad"),
    None,                       # 返回空结果
    {"display_name": "  "},     # 有响应但挑不出城市名
    {"lat": 1, "lng": 2},
])
def test_resolve_reverse_origin_never_raises(boom: Any) -> None:
    def fake(lat: float, lng: float) -> Any:
        if isinstance(boom, BaseException):
            raise boom
        return boom

    origin = place_loader.resolve_reverse_origin(31.2304, 121.4737, reverse_geocoder=fake)
    assert origin["resolved"] is False
    assert origin["city"] == origin["name"] == "我的位置(31.23,121.47)"
    assert (origin["lat"], origin["lng"]) == (31.2304, 121.4737)


def test_default_reverse_geocoder_passes_zoom(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[float, float, int]] = []
    monkeypatch.setattr(place_loader, "ds_reverse",
                       lambda lat, lng, zoom=10: calls.append((lat, lng, zoom)) or {
                           "lat": lat, "lng": lng, "display_name": "朝阳区, 北京市, 中国"})
    place_loader.resolve_reverse_origin(39.9, 116.4)
    place_loader.resolve_reverse_origin(39.9, 116.4, zoom=12)
    assert calls == [(39.9, 116.4, place_loader.REVERSE_ZOOM), (39.9, 116.4, 12)]


def test_api_reverse_geocode_returns_origin_bands_and_segments(session, monkeypatch) -> None:
    monkeypatch.setattr(place_loader, "default_reverse_geocoder",
                       lambda lat, lng, zoom=10: {"lat": lat, "lng": lng,
                                                 "display_name": "浦东新区, 上海市, 200120, 中国"})
    payload = places_api.reverse_geocode(lat=31.2304, lng=121.4737, zoom=None, session=session)
    assert payload["resolved"] is True
    assert payload["origin"]["city"] == "上海市"
    assert (payload["origin"]["lat"], payload["origin"]["lng"]) == (31.2304, 121.4737)
    assert [band["key"] for band in payload["bands"]] == band_keys()
    assert payload["segments"] == []
    assert payload["note"]


def test_api_reverse_geocode_fallback_is_not_an_error(session, monkeypatch) -> None:
    """Nominatim 挂了也要 200:降级成坐标起点,前端只给提示。"""
    def boom(lat: float, lng: float, zoom: int = 10) -> dict[str, Any]:
        raise DataSourceError("nominatim", "服务繁忙")

    monkeypatch.setattr(place_loader, "default_reverse_geocoder", boom)
    payload = places_api.reverse_geocode(lat=31.2304, lng=121.4737, zoom=None, session=session)
    assert payload["resolved"] is False
    assert payload["origin"]["city"] == "我的位置(31.23,121.47)"


def test_reverse_route_is_registered_with_coordinate_bounds() -> None:
    paths = app.openapi()["paths"]
    assert "/api/geocode/reverse" in paths, "前端「我的位置」要有后端路由"
    assert {"/api/places", "/api/places/meta", "/api/geocode"} <= set(paths), "既有路由不能被破坏"
    params = {item["name"]: item for item in paths["/api/geocode/reverse"]["get"]["parameters"]}
    assert {"lat", "lng"} <= set(params)
    assert params["lat"]["schema"]["minimum"] == -90 and params["lat"]["schema"]["maximum"] == 90
    assert params["lng"]["schema"]["minimum"] == -180 and params["lng"]["schema"]["maximum"] == 180
    assert params["lat"]["required"] is True and params["lng"]["required"] is True


# --------------------------------------------------------------------------- #
# 9. API 出参:来源计数 + 种子概览
# --------------------------------------------------------------------------- #


def test_api_places_reports_seed_counts(session, offline_sources, seeds_on) -> None:
    payload = places_api.list_places(origin="上海", band=BAND, category=None, lat=None, lng=None,
                                    refresh=False, intros=False, session=session)
    assert payload["seeded"] >= 1
    assert payload["counts_by_source"][SEED_SOURCE] == payload["seeded"]
    assert payload["counts_by_source"][OSM_SOURCE] == 1
    assert {row["source"] for row in payload["places"]} == {OSM_SOURCE, SEED_SOURCE}
    assert sum(payload["counts_by_source"].values()) == sum(payload["counts_by_category"].values())


def test_api_places_category_filter_can_return_seed_only(session, offline_sources, seeds_on) -> None:
    payload = places_api.list_places(origin="上海", band=BAND, category=CATEGORY_SPORT, lat=None,
                                    lng=None, refresh=False, intros=False, session=session)
    assert payload["count"] == len(payload["places"])
    assert all(row["source"] == SEED_SOURCE for row in payload["places"])
    assert all(row["category"] == CATEGORY_SPORT for row in payload["places"])


def test_api_meta_reports_seed_stats(seeds_on) -> None:
    meta = places_api.places_meta()
    assert meta["seeds"]["enabled"] is True
    assert meta["seeds"]["total"] == len(seed_data.SEEDS)
    assert meta["seeds"]["by_category"][CATEGORY_SKI] == len(seed_data.SKI_SEEDS)
    assert meta["seeds"]["env"] == seed_data.ENV_SEEDS
    assert meta["seeds"]["source"] == SEED_SOURCE


def test_api_meta_reports_seeds_disabled_by_default() -> None:
    meta = places_api.places_meta()
    assert meta["seeds"] == {"enabled": False, "total": 0, "by_category": {},
                            "source": SEED_SOURCE, "env": seed_data.ENV_SEEDS,
                            "total_defined": len(seed_data.SEEDS)}


# --------------------------------------------------------------------------- #
# 10. CLI:校验 / 打印 / 给已入库分段补种(只读本地库)
# --------------------------------------------------------------------------- #


def test_cli_validate_and_list(seeds_on, capsys) -> None:
    assert seed_data.main(["--validate"]) == 0
    assert seed_data.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "通过" in out and str(len(seed_data.SEEDS)) in out
    assert CATEGORY_SKI in out and CATEGORY_SPORT in out
    assert seed_data.main([]) == 1, "没给任何动作时打印帮助并返回非 0"


def test_cli_reports_when_seeds_disabled(capsys) -> None:
    assert seed_data.main(["--validate"]) == 0
    assert "跳过" in capsys.readouterr().out


def test_cli_backfills_seeds_for_recorded_city(tmp_path, seeds_on, capsys) -> None:
    url = f"sqlite:///{tmp_path / 'cli_seeds.db'}"
    engine = make_engine(url)
    init_db(engine)
    current = open_session(engine)
    try:
        repo.record_segment(current, origin_city="上海", band=BAND,
                            origin={"name": "上海市, 中国", **SHANGHAI},
                            place_count=1, source="overpass")
        repo.upsert_places(current, origin_city="上海", band=BAND,
                           items=place_loader.to_place_items(
                               [point_at(60, name="云州峰", tags={"natural": "peak"}, osm_id=1)]))
        current.commit()
    finally:
        current.close()
        engine.dispose()

    assert seed_data.main(["--city", "上海", "--db", url]) == 0
    out = capsys.readouterr().out
    assert "共补种" in out

    engine = make_engine(url)
    current = open_session(engine)
    try:
        counts = repo.count_by_source(current, origin_city="上海", band=BAND)
        segment = repo.get_segment(current, origin_city="上海", band=BAND)
    finally:
        current.close()
        engine.dispose()
    assert counts[SEED_SOURCE] >= 1, f"CLI 应把环内种子补进库:{counts}"
    assert segment.place_count == counts[OSM_SOURCE] + counts[SEED_SOURCE]
