"""TASK-9a 单测:高德 POI 的四分类归类与检索组(:mod:`services.amap_categories`)。

纯逻辑、不触网也不碰库。三件事:

* :data:`AMAP_TYPE_GROUPS` 的形状与既有 :data:`services.classify.SEARCH_GROUPS` 对齐
  (``category``/``group``/``budget`` + ``types``/``keywords``,配额总量与各分类配额都不变),
  这样 TASK-9b 换检索侧时渐进配额口径零改动;
* :func:`classify_amap_poi` 的**锚点覆盖**(§1.4 实测 typecode/type 原文每类 ≥3 个)、
  **优先级**(滑雪 > 运动 > 人文美食 > 自然)与**人工设施归其他**;
* 一条「防漂移」元测试:规则表里出现的 typecode / type 字符串**必须**是实测过的锚点,
  禁凭记忆编造中类码(契约 §1.4 的硬约束)。

运行:``cd backend && ../.venv/bin/python -m pytest -q test_amap_categories.py``
"""

from __future__ import annotations

import os
import sys
from types import MappingProxyType
from typing import Any

import pytest

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from data_sources import amap  # noqa: E402
from db.models import UNCATEGORIZED  # noqa: E402
from services import amap_categories as cats  # noqa: E402
from services import classify  # noqa: E402

GROUP_KEYS = {"category", "group", "budget", "types", "keywords"}

#: §1.4 实测过的 typecode 锚点(**只允许**这些进规则表)
MEASURED_TYPECODES = {
    "080106", "080000", "080113", "080500",
    "110000", "110101", "110105",
    "050000", "050100", "050301", "050500",
    "100000", "100104", "100105",
}
#: §1.4 实测过的 type 字符串原文(锚点校验用「是这些串的子串」放宽到词根)
MEASURED_TYPE_TEXTS = (
    "体育休闲服务;运动场馆;滑雪场",
    "体育休闲服务场所", "运动场馆", "台球厅", "休闲场所",
    "风景名胜相关;旅游景点", "风景名胜", "旅游景点", "公园", "城市广场",
    "餐饮服务", "中餐厅", "肯德基", "咖啡厅",
    "住宿服务", "三星级宾馆", "经济型连锁酒店",
)


def poi(name: str = "", typecode: str = "", type_text: str = "", **extra: Any) -> dict[str, Any]:
    """造一条 :func:`data_sources.amap.parse_poi` 形状的归一化 POI。"""
    return {
        "id": extra.pop("id", "B0FFTEST01"),
        "name": name,
        "lat": 30.242451,
        "lng": 120.147913,
        "type": type_text,
        "typecode": typecode,
        "address": "",
        "cityname": "杭州市",
        "adname": "西湖区",
        "distance_m": None,
        **extra,
    }


# --------------------------------------------------------------------------- #
# AMAP_TYPE_GROUPS 形状与配额
# --------------------------------------------------------------------------- #


def test_groups_shape_matches_search_groups() -> None:
    groups = cats.AMAP_TYPE_GROUPS
    assert isinstance(groups, tuple) and len(groups) == 5
    for group in groups:
        assert set(group) == GROUP_KEYS
        assert isinstance(group["budget"], int) and group["budget"] > 0
        assert isinstance(group["types"], tuple)
        assert group["keywords"] is None or isinstance(group["keywords"], str)
        assert group["category"] in {
            cats.CATEGORY_SKI, cats.CATEGORY_SPORT, cats.CATEGORY_CULTURE, cats.CATEGORY_NATURE,
        }
        assert bool(group["types"]) != bool(group["keywords"]), "types 与 keywords 建议二选一(§6.3)"


def test_group_search_params_follow_contract() -> None:
    by_group = {group["group"]: group for group in cats.AMAP_TYPE_GROUPS}
    assert by_group["滑雪场"]["types"] == ("080106",)
    assert by_group["滑雪场"]["category"] == cats.CATEGORY_SKI
    assert by_group["运动场所"]["types"] == ("080000",)
    assert by_group["运动场所"]["category"] == cats.CATEGORY_SPORT
    assert by_group["人文美食"]["types"] == ("110000", "050000")
    assert by_group["人文美食"]["category"] == cats.CATEGORY_CULTURE
    # §6.4:村庄码 190106 实测 0 命中 → 小城古镇改走关键词
    assert by_group["小城古镇"]["keywords"] == "古镇|老街|古城"
    assert by_group["小城古镇"]["types"] == ()
    assert by_group["小城古镇"]["category"] == cats.CATEGORY_CULTURE
    assert by_group["自然风光"]["types"] == ("110000",)
    assert by_group["自然风光"]["category"] == cats.CATEGORY_NATURE
    assert "190106" not in {code for group in cats.AMAP_TYPE_GROUPS for code in group["types"]}


def test_budgets_match_existing_search_groups() -> None:
    """各分类配额与既有 :data:`classify.SEARCH_GROUPS` 完全一致(总量 540 不变)。"""
    def by_category(groups: Any) -> dict[str, int]:
        totals: dict[str, int] = {}
        for group in groups:
            totals[group["category"]] = totals.get(group["category"], 0) + int(group["budget"])
        return totals

    expected = by_category(classify.SEARCH_GROUPS)
    actual = by_category(cats.AMAP_TYPE_GROUPS)
    assert actual == expected
    assert sum(actual.values()) == 540
    assert actual[cats.CATEGORY_SKI] == 80
    assert actual[cats.CATEGORY_SPORT] == 100
    assert actual[cats.CATEGORY_CULTURE] == 220     # 人文美食 180 + 小城古镇 40
    assert actual[cats.CATEGORY_NATURE] == 140


def test_category_names_match_classify() -> None:
    assert cats.CATEGORY_SKI == classify.CATEGORY_SKI == "滑雪场"
    assert cats.CATEGORY_SPORT == classify.CATEGORY_SPORT == "运动"
    assert cats.CATEGORY_CULTURE == classify.CATEGORY_CULTURE == "小城人文美食"
    assert cats.CATEGORY_NATURE == classify.CATEGORY_NATURE == "自然风光"
    assert cats.UNCATEGORIZED == UNCATEGORIZED == "其他"


# --------------------------------------------------------------------------- #
# 四分类判定(每类 ≥3 个 §1.4 锚点)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("payload", [
    {"typecode": "080106", "type_text": "体育休闲服务;运动场馆;滑雪场", "name": "太舞滑雪场"},
    {"typecode": "", "type_text": "体育休闲服务;运动场馆;滑雪场", "name": "密苑云顶乐园(云顶滑雪场)"},
    {"typecode": "080106", "type_text": "", "name": "林语山谷"},
    {"typecode": "", "type_text": "", "name": "国家跳台滑雪中心(雪如意)"},
    {"typecode": "080106", "type_text": "体育休闲服务;运动场馆;滑雪场", "name": "万龙滑雪场"},
])
def test_classify_ski_anchors(payload: dict[str, Any]) -> None:
    assert cats.classify_amap_poi(poi(**payload)) == cats.CATEGORY_SKI


@pytest.mark.parametrize("payload", [
    {"typecode": "080113", "type_text": "体育休闲服务;运动场馆;台球厅", "name": "星牌台球俱乐部"},
    {"typecode": "080500", "type_text": "体育休闲服务;休闲场所", "name": "城西休闲场所"},
    {"typecode": "080000", "type_text": "体育休闲服务;体育休闲服务场所", "name": "黄龙体育中心"},
    {"typecode": "", "type_text": "体育休闲服务;运动场馆;篮球场", "name": "某中学体育馆"},
    {"typecode": "", "type_text": "", "name": "奥克斯健身会所"},
])
def test_classify_sport_anchors(payload: dict[str, Any]) -> None:
    assert cats.classify_amap_poi(poi(**payload)) == cats.CATEGORY_SPORT


@pytest.mark.parametrize("payload", [
    {"typecode": "050100", "type_text": "餐饮服务;中餐厅", "name": "外婆家(西湖店)"},
    {"typecode": "050301", "type_text": "餐饮服务;快餐厅;肯德基", "name": "肯德基(延安路店)"},
    {"typecode": "050500", "type_text": "餐饮服务;咖啡厅", "name": "湖畔咖啡厅"},
    {"typecode": "110101", "type_text": "风景名胜;公园广场;公园", "name": "太子湾公园"},
    {"typecode": "110105", "type_text": "风景名胜;公园广场;城市广场", "name": "城市阳台广场"},
    {"typecode": "110000", "type_text": "风景名胜相关;旅游景点", "name": "河坊街老街"},
    {"typecode": "110000", "type_text": "", "name": "浙江省博物馆"},
    {"typecode": "050000", "type_text": "餐饮服务", "name": "知味观"},
])
def test_classify_culture_anchors(payload: dict[str, Any]) -> None:
    assert cats.classify_amap_poi(poi(**payload)) == cats.CATEGORY_CULTURE


@pytest.mark.parametrize("payload", [
    {"typecode": "110000", "type_text": "风景名胜相关;旅游景点", "name": "西湖风景名胜区"},
    {"typecode": "110000", "type_text": "风景名胜", "name": "千岛湖"},
    {"typecode": "", "type_text": "", "name": "天目山"},
    {"typecode": "", "type_text": "", "name": "西溪国家湿地公园"},
    {"typecode": "", "type_text": "风景名胜相关;旅游景点", "name": "九溪十八涧瀑布"},
])
def test_classify_nature_anchors(payload: dict[str, Any]) -> None:
    assert cats.classify_amap_poi(poi(**payload)) == cats.CATEGORY_NATURE


# --------------------------------------------------------------------------- #
# 优先级:滑雪 > 运动 > 人文美食 > 自然
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("payload,expected", [
    # 滑雪场码即使被 080000 大类捞回来,也归滑雪(本地排除,§3 TASK-9a)
    ({"typecode": "080000", "type_text": "体育休闲服务;运动场馆;滑雪场", "name": "某滑雪场"}, cats.CATEGORY_SKI),
    ({"typecode": "080106", "type_text": "体育休闲服务;运动场馆;滑雪场", "name": "太舞滑雪场"}, cats.CATEGORY_SKI),
    # 运动 > 人文美食(名字里有「广场」也算运动)
    ({"typecode": "080500", "type_text": "体育休闲服务;休闲场所", "name": "体育休闲广场"}, cats.CATEGORY_SPORT),
    ({"typecode": "080113", "type_text": "体育休闲服务;运动场馆;台球厅", "name": "台球咖啡会所"}, cats.CATEGORY_SPORT),
    # 人文美食 > 自然(名字里既有「湖」又有「博物馆」)
    ({"typecode": "110000", "type_text": "风景名胜相关;旅游景点", "name": "西湖博物馆"}, cats.CATEGORY_CULTURE),
    ({"typecode": "110000", "type_text": "风景名胜", "name": "孤山公园"}, cats.CATEGORY_CULTURE),
    # 自然兜底:11 大类里没有人文关键词 → 自然
    ({"typecode": "110000", "type_text": "风景名胜相关;旅游景点", "name": "云栖竹径"}, cats.CATEGORY_NATURE),
])
def test_category_priority(payload: dict[str, Any], expected: str) -> None:
    assert cats.classify_amap_poi(poi(**payload)) == expected


def test_ski_typecode_excluded_from_sport_rule() -> None:
    assert cats.SPORT_EXCLUDED_TYPECODES == (cats.SKI_TYPECODE,) == ("080106",)
    assert cats.is_sport("太舞滑雪场", "080106", "体育休闲服务;运动场馆;滑雪场") is False
    assert cats.is_ski("太舞滑雪场", "080106", "体育休闲服务;运动场馆;滑雪场") is True
    assert cats.is_sport("某台球厅", "080113", "体育休闲服务;运动场馆;台球厅") is True


def test_scenic_big_class_split_between_culture_and_nature() -> None:
    """``110000`` 大类内二分:公园/广场/古迹/博物馆 → 人文;山/湖/森林/湿地 → 自然。"""
    assert cats.classify_amap_poi(poi(name="太子湾公园", typecode="110101")) == cats.CATEGORY_CULTURE
    assert cats.classify_amap_poi(poi(name="武林广场", typecode="110105")) == cats.CATEGORY_CULTURE
    assert cats.classify_amap_poi(poi(name="雷峰塔遗址", typecode="110000")) == cats.CATEGORY_CULTURE
    assert cats.classify_amap_poi(poi(name="大明山", typecode="110000")) == cats.CATEGORY_NATURE
    assert cats.classify_amap_poi(poi(name="青山湖", typecode="110000")) == cats.CATEGORY_NATURE


@pytest.mark.parametrize("name", [
    "西溪国家湿地公园", "半山森林公园", "天目山自然保护区", "千岛湖风景名胜区", "某地质公园",
])
def test_nature_reserve_names_beat_park_hint(name: str) -> None:
    """自然保护地专名(湿地/森林/国家公园…)即使带「公园」二字也归自然。"""
    assert cats.is_nature_reserve(name, "110000") is True
    assert cats.classify_amap_poi(poi(name=name, typecode="110000")) == cats.CATEGORY_NATURE
    assert cats.classify_amap_poi(poi(name=name, typecode="110101")) == cats.CATEGORY_NATURE
    # 缺 typecode 只能靠名字判时同样成立
    assert cats.classify_amap_poi(poi(name=name)) == cats.CATEGORY_NATURE


@pytest.mark.parametrize("name,typecode", [("森林公园餐厅", "050100"), ("湿地公园咖啡厅", "050500")])
def test_nature_reserve_rule_does_not_hijack_food(name: str, typecode: str) -> None:
    """餐饮大类里的「森林公园餐厅」仍归人文美食(自然保护地规则只在风景名胜大类生效)。"""
    assert cats.is_nature_reserve(name, typecode) is False
    assert cats.classify_amap_poi(poi(name=name, typecode=typecode)) == cats.CATEGORY_CULTURE


# --------------------------------------------------------------------------- #
# 人工设施 / 判不出来 → 其他
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("payload", [
    {"typecode": "080106", "type_text": "体育休闲服务;运动场馆;滑雪场", "name": "太舞滑雪场停车场"},
    {"typecode": "110000", "type_text": "风景名胜相关;旅游景点", "name": "西湖景区指示牌"},
    {"typecode": "110000", "type_text": "风景名胜", "name": "景区大门"},
    {"typecode": "050100", "type_text": "餐饮服务;中餐厅", "name": "美食街售票处"},
    {"typecode": "110101", "type_text": "风景名胜;公园广场;公园", "name": "公园公厕"},
    {"typecode": "080000", "type_text": "体育休闲服务", "name": "体育中心出入口"},
])
def test_facility_names_go_to_uncategorized(payload: dict[str, Any]) -> None:
    assert cats.classify_amap_poi(poi(**payload)) == UNCATEGORIZED
    assert cats.is_facility_name(payload["name"]) is True


def test_facility_rule_is_suffix_based() -> None:
    """后缀匹配,免得把「大门山景区」这类真地名误杀。"""
    assert cats.is_facility_name("大门山") is False
    assert cats.classify_amap_poi(poi(name="大门山景区", typecode="110000")) == cats.CATEGORY_NATURE
    assert cats.is_facility_name("停车场") is True
    assert cats.is_facility_name("太子湾公园停车场") is True
    assert cats.is_facility_name("") is False


@pytest.mark.parametrize("payload", [
    {"typecode": "999999", "type_text": "公司企业;公司", "name": "某某贸易有限公司"},
    {"typecode": "", "type_text": "", "name": "未知点位"},
    {"typecode": "100105", "type_text": "住宿服务;经济型连锁酒店", "name": "如家快捷酒店"},
    {"typecode": "", "type_text": "", "name": ""},
    {},
])
def test_unrecognized_pois_go_to_uncategorized(payload: dict[str, Any]) -> None:
    assert cats.classify_amap_poi(poi(**payload)) == UNCATEGORIZED


def test_classify_accepts_readonly_mapping_and_raw_amap_poi() -> None:
    assert cats.classify_amap_poi(MappingProxyType(poi(name="太舞滑雪场", typecode="080106"))) == cats.CATEGORY_SKI
    raw = {"id": "B023B17WWK", "name": "太舞滑雪场", "typecode": "080106",
           "type": "体育休闲服务;运动场馆;滑雪场", "location": "115.412345,40.987654"}
    assert cats.classify_amap_poi(raw) == cats.CATEGORY_SKI
    assert cats.classify_amap_poi({"name": [], "typecode": [], "type": []}) == UNCATEGORIZED


def test_classify_end_to_end_with_amap_parser() -> None:
    """``amap.parse_poi`` 的输出直接喂归类,是 TASK-9b 的调用顺序。"""
    parsed = amap.parse_poi({
        "id": "B023B17WWK", "name": "太舞滑雪场", "typecode": "080106",
        "type": "体育休闲服务;运动场馆;滑雪场", "location": "115.412345,40.987654",
        "distance": "1234", "cityname": "张家口市", "adname": "崇礼区",
    })
    assert parsed is not None
    assert cats.classify_amap_poi(parsed) == cats.CATEGORY_SKI
    assert cats.dedupe_key(parsed) == ("amap", "B023B17WWK")


# --------------------------------------------------------------------------- #
# dedupe_key
# --------------------------------------------------------------------------- #


def test_dedupe_key_shape() -> None:
    assert cats.dedupe_key(poi(id="B023B17WWK", name="太舞滑雪场")) == ("amap", "B023B17WWK")
    assert cats.dedupe_key({"id": "B0FFHCZ90B"}) == ("amap", "B0FFHCZ90B")
    assert cats.dedupe_key({}) == ("amap", "")
    assert cats.dedupe_key({"id": []}) == ("amap", "")


def test_dedupe_key_is_stable_across_categories() -> None:
    """高德 POI id 全局唯一:同实体被两个大类捞回来也只归一类。"""
    first = poi(id="B0FFHCZ90B", name="太子湾公园", typecode="110101")
    second = poi(id="B0FFHCZ90B", name="太子湾公园", typecode="110000")
    assert cats.dedupe_key(first) == cats.dedupe_key(second)
    assert len({cats.dedupe_key(first), cats.dedupe_key(second)}) == 1
    assert cats.dedupe_key(poi(id="B0OTHER001")) != cats.dedupe_key(first)


# --------------------------------------------------------------------------- #
# 防漂移:规则表只能用实测锚点(契约 §1.4 硬约束)
# --------------------------------------------------------------------------- #


def test_only_measured_typecodes_in_rules() -> None:
    used = {code for group in cats.AMAP_TYPE_GROUPS for code in group["types"]}
    used.update(cats.SPORT_TYPECODES)
    used.update(cats.FOOD_TYPECODES)
    used.update(cats.CULTURE_SCENIC_TYPECODES)
    used.update(cats.SPORT_EXCLUDED_TYPECODES)
    used.add(cats.SKI_TYPECODE)
    assert used <= MEASURED_TYPECODES, f"出现未实测的 typecode:{sorted(used - MEASURED_TYPECODES)}"


def test_only_measured_type_texts_in_rules() -> None:
    used = (
        cats.SKI_TYPE_TEXTS + cats.SPORT_TYPE_TEXTS
        + cats.CULTURE_TYPE_TEXTS + cats.NATURE_TYPE_TEXTS
    )
    for text in used:
        assert any(text in measured for measured in MEASURED_TYPE_TEXTS), (
            f"type 字符串 {text!r} 不在 §1.4 实测原文里(禁凭记忆编造)"
        )


def test_big_class_prefixes_are_measured() -> None:
    """粗筛用的大类前码必须来自实测大类码(08 体育休闲 / 05 餐饮 / 11 风景名胜)。"""
    assert cats.SPORT_TYPECODE_PREFIX == "08" and "080000" in MEASURED_TYPECODES
    assert cats.FOOD_TYPECODE_PREFIX == "05" and "050000" in MEASURED_TYPECODES
    assert cats.SCENIC_TYPECODE_PREFIX == "11" and "110000" in MEASURED_TYPECODES
