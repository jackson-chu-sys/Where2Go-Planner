"""人工种子数据:补 OSM 国内覆盖不足的**滑雪场 / 运动**目的地(TASK-1c)。

背景(docs/STAGE1-PLAN.md 第 3 节"数据支撑现实" + TASK-1b 实测):免费源 Overpass
在中国境内的 ``piste:*`` / ``sport=*`` 覆盖非常稀疏 —— 上海两个分段实测滑雪场仅 1 条、
运动 6 条,四分类里这两类基本"空转"。神朱已拍板:**接受现状,种子数据垫底**,
功能稳定后再考虑切高德/百度 key。

本模块只做数据与纯函数,**不碰网络、不碰 DB**:

1. :data:`SEEDS` —— 人工补录的国内知名滑雪场与运动目的地。坐标逐条用 Nominatim
   正向地理编码取回、再用逆地理编码核对落在哪个区县(2026-09-10);简介为静态文案
   (:func:`services.intro.fill_missing_intros` 只补 ``intro`` 为空的行,所以种子
   **不会**触发 LLM 调用,也就没有额度成本);
2. :func:`load_seeds` / :func:`seeds_in_band` —— 归一化(打上 ``source=种子`` 标签、
   校验分类)并按环形分段筛出该带内的种子;
3. :func:`attach_seeds` —— 与 OSM 结果**合并去重**:去重键 = 名字 + 坐标
   (名字相同或互相包含,且大圆距离 ≤ :data:`DEDUPE_RADIUS_KM`),OSM 已经抓到了
   就不重复补 —— OSM 数据优先,种子只垫缺口。

写库与幂等补种的编排在 :mod:`services.place_loader`(:func:`~services.place_loader.ensure_seeded`);
本模块**不 import 它**,避免 services 层循环依赖(CLI 里是函数内延迟导入)。

开关:环境变量 ``WHERE2GO_SEEDS``,**默认开**;设为 ``0/false/off/no`` 关闭。
``backend/conftest.py`` 里整套单测默认关掉它,既有 87 个用例的条数断言因此不受影响,
种子相关的用例再显式打开或传入自造种子列表。

用法::

    python -m services.seed_data --validate        # 校验数据(分类/坐标/简介/重名)
    python -m services.seed_data --list            # 按分类打印全部种子
    python -m services.seed_data --city 上海       # 给该城市已入库的分段补种(只读本地库)
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Optional

from data_sources import haversine_km
from db.models import SEED_SOURCE, SOURCE_TAG
from services.bands import DISTANCE_PRECISION, band_keys, in_band, require_band
from services.classify import CATEGORY_SKI, CATEGORY_SPORT, categorize, is_known_category

ENV_SEEDS = "WHERE2GO_SEEDS"
# 这些值视为"关闭";留空/未设置视为"开启"(线上默认带种子,单测由 conftest 关掉)
DISABLED_VALUES = frozenset({"0", "false", "no", "off", "关", "关闭"})
# 与 OSM 结果去重的半径:同名且距离在这个范围内就算同一个地方(雪场大门/中心点常有偏移)
DEDUPE_RADIUS_KM = 2.0
# 名字包含判定的最短长度:"南山滑雪场" ⊂ "北京南山滑雪场" 算同一个,"湖" 不算
MIN_NAME_KEY_LEN = 3
MAX_SEED_INTRO_CHARS = 60
LAT_RANGE = (-90.0, 90.0)
LNG_RANGE = (-180.0, 180.0)
# 名字归一时忽略的装饰字符(中英括号、连接号、空白与标点)
NAME_TRIM_CHARS = " \t·•・-—―_/\\,，.。、()（）[]【】{}《》\"'“”‘’"

# --------------------------------------------------------------------------- #
# tag 预设:种子数据没有 OSM id,分类完全靠这组 tag 走 services.classify.categorize
# 判定,所以 tag 必须能让 ``categorize`` 得出与声明一致的分类(validate 会逐条核对)。
# 运动类刻意避开 ski/snowboard/piste/winter_sports 这些滑雪线索词。
# --------------------------------------------------------------------------- #

SKI_DOWNHILL: dict[str, str] = {"piste:type": "downhill", "sport": "skiing", "leisure": "sports_centre"}
SKI_INDOOR: dict[str, str] = {**SKI_DOWNHILL, "indoor": "yes"}
SKI_JUMP: dict[str, str] = {"piste:type": "jump", "sport": "ski_jumping", "leisure": "stadium"}

SPORT_STADIUM: dict[str, str] = {"leisure": "stadium", "sport": "multi"}
SPORT_SWIMMING: dict[str, str] = {"leisure": "swimming_pool", "sport": "swimming"}
SPORT_SPEED_SKATING: dict[str, str] = {"leisure": "sports_centre", "sport": "speed_skating"}
SPORT_MOTOR: dict[str, str] = {"leisure": "track", "sport": "motor"}
SPORT_SAILING: dict[str, str] = {"leisure": "marina", "sport": "sailing"}
SPORT_GOLF: dict[str, str] = {"leisure": "golf_course", "sport": "golf"}
SPORT_DIVING: dict[str, str] = {"leisure": "sports_centre", "sport": "scuba_diving"}
SPORT_CLIMBING: dict[str, str] = {"leisure": "climbing", "sport": "climbing"}
SPORT_RAFTING: dict[str, str] = {"leisure": "sports_centre", "sport": "rafting"}
SPORT_CYCLING: dict[str, str] = {"leisure": "track", "sport": "cycling"}
SPORT_BEACH: dict[str, str] = {"leisure": "beach", "sport": "swimming"}


def _seed(
    name: str,
    lat: float,
    lng: float,
    category: str,
    tags: Mapping[str, str],
    intro: str,
) -> dict[str, Any]:
    """造一条种子(内部辅助):字段固定,``tags`` 拷一份避免调用方改到常量。"""
    return {
        "name": name,
        "lat": float(lat),
        "lng": float(lng),
        "category": category,
        "tags": dict(tags),
        "intro": intro,
    }


# --------------------------------------------------------------------------- #
# 滑雪场(28 条):按 华北 → 东北 → 西北 → 华中/西南 → 华东 排列
# 坐标来源:Nominatim 正向地理编码(2026-09-10),并用逆地理编码核对所在区县
# --------------------------------------------------------------------------- #

SKI_SEEDS: tuple[dict[str, Any], ...] = (
    _seed("万龙滑雪场", 40.9532447, 115.3382136, CATEGORY_SKI, SKI_DOWNHILL,
          "崇礼开发最早的雪场之一,雪道多、落差大,适合进阶练技术。"),
    _seed("云顶滑雪公园", 40.9483320, 115.4185984, CATEGORY_SKI, SKI_DOWNHILL,
          "北京冬奥会自由式滑雪与单板赛场,赛后向公众开放。"),
    _seed("太舞滑雪场", 40.8833809, 115.4366885, CATEGORY_SKI, SKI_DOWNHILL,
          "崇礼大型度假雪场,雪道宽长,住宿与餐饮配套齐全。"),
    _seed("富龙滑雪场", 40.9686617, 115.3039945, CATEGORY_SKI, SKI_DOWNHILL,
          "紧邻崇礼城区,开夜场,适合当天往返的短途滑雪。"),
    _seed("长城岭滑雪场", 41.0059457, 115.4549048, CATEGORY_SKI, SKI_DOWNHILL,
          "崇礼海拔较高的雪场,雪期长,森林道氛围安静。"),
    _seed("南山滑雪场", 40.3299573, 116.8558060, CATEGORY_SKI, SKI_DOWNHILL,
          "北京密云人气雪场,雪道分级清楚,单板公园与初级道都友好。"),
    _seed("军都山滑雪场", 40.2380534, 116.3244870, CATEGORY_SKI, SKI_DOWNHILL,
          "昌平近郊雪场,离城近、有夜场,适合入门体验。"),
    _seed("怀北国际滑雪场", 40.4466959, 116.6482219, CATEGORY_SKI, SKI_DOWNHILL,
          "怀柔山间雪场,雪道视野开阔,市区自驾可达。"),
    _seed("盘山滑雪场", 40.0673450, 117.2877206, CATEGORY_SKI, SKI_DOWNHILL,
          "天津蓟州盘山脚下,京津冀一日滑雪的常见选择。"),
    _seed("北大湖滑雪度假区", 43.4112690, 126.6184853, CATEGORY_SKI, SKI_DOWNHILL,
          "吉林大型度假区,落差与雪质在国内名列前茅,雪季长。"),
    _seed("松花湖滑雪场", 43.6751561, 126.6146261, CATEGORY_SKI, SKI_DOWNHILL,
          "吉林市郊度假区,雪道数量多,冬季可顺路看雾凇。"),
    _seed("长春莲花山滑雪场", 43.8578504, 125.7312094, CATEGORY_SKI, SKI_DOWNHILL,
          "长春近郊雪场,初级道平缓,适合家庭与新手。"),
    _seed("亚布力滑雪旅游度假区", 44.7752383, 128.4542495, CATEGORY_SKI, SKI_DOWNHILL,
          "国内起步最早的大型滑雪度假区,雪道群规模大。"),
    _seed("帽儿山滑雪场", 45.2503463, 127.4470388, CATEGORY_SKI, SKI_DOWNHILL,
          "哈尔滨近郊山地雪场,雪道选择较多,交通方便。"),
    _seed("万达长白山国际度假区滑雪场", 42.1038429, 127.4968593, CATEGORY_SKI, SKI_DOWNHILL,
          "长白山脚下度假雪场,雪期长、雪质松软,配套完善。"),
    _seed("沈阳东北亚滑雪场", 42.0384391, 123.7203660, CATEGORY_SKI, SKI_DOWNHILL,
          "沈阳棋盘山一带雪场,离城近,适合周末短途。"),
    _seed("将军山滑雪场", 47.8259879, 88.1572304, CATEGORY_SKI, SKI_DOWNHILL,
          "阿勒泰市区旁的雪场,地处公认的人类滑雪起源地一带。"),
    _seed("可可托海国际滑雪场", 47.1886232, 90.0567909, CATEGORY_SKI, SKI_DOWNHILL,
          "新疆雪期最长的雪场之一,落差大,野雪资源丰富。"),
    _seed("丝绸之路国际滑雪场", 43.4431133, 87.4123485, CATEGORY_SKI, SKI_DOWNHILL,
          "乌鲁木齐近郊天山北坡雪场,雪道分级齐全。"),
    _seed("广州融创雪世界", 23.4296248, 113.2258857, CATEGORY_SKI, SKI_INDOOR,
          "华南大型室内雪场,全年恒温,南方也能体验滑雪。"),
    _seed("哈尔滨融创雪世界", 45.8016435, 126.5024264, CATEGORY_SKI, SKI_INDOOR,
          "室内雪场,四季可滑,适合夏季维持手感与亲子体验。"),
    _seed("神农架国际滑雪场", 31.5525174, 110.3814544, CATEGORY_SKI, SKI_DOWNHILL,
          "华中高山雪场,林区雪景独特,冬季自驾可达。"),
    _seed("西岭雪山滑雪场", 30.7011132, 103.1867545, CATEGORY_SKI, SKI_DOWNHILL,
          "成都周边规模较大的高山雪场,雪山与雾凇景观相伴。"),
    _seed("上海耀雪冰雪世界", 30.9217497, 121.9020694, CATEGORY_SKI, SKI_INDOOR,
          "临港大型室内滑雪场,市区出发当天可来回。"),
    _seed("太仓阿尔卑斯雪世界", 31.4072808, 121.1443788, CATEGORY_SKI, SKI_INDOOR,
          "长三角室内雪场,离上海近,适合周末短途滑雪。"),
    _seed("绍兴乔波冰雪世界", 30.0569283, 120.4721799, CATEGORY_SKI, SKI_INDOOR,
          "柯桥室内滑雪馆,常年开放,适合新手练基本动作。"),
    _seed("大明山滑雪场", 30.0260698, 118.9919185, CATEGORY_SKI, SKI_DOWNHILL,
          "杭州临安高山雪场,山势陡,滑雪与观景兼顾。"),
    _seed("首钢滑雪大跳台", 39.9097054, 116.1455043, CATEGORY_SKI, SKI_JUMP,
          "北京冬奥会单板大跳台赛场,由工业遗址改建的地标。"),
)

# --------------------------------------------------------------------------- #
# 运动(21 条):场馆 / 水上 / 山地户外 / 骑行,尽量覆盖不同项目与地域
# --------------------------------------------------------------------------- #

SPORT_SEEDS: tuple[dict[str, Any], ...] = (
    _seed("国家体育场(鸟巢)", 39.9884514, 116.3941336, CATEGORY_SPORT, SPORT_STADIUM,
          "北京奥运主场馆,可参观场地与看台,常有赛事演出。"),
    _seed("国家游泳中心(水立方)", 39.9915785, 116.3841726, CATEGORY_SPORT, SPORT_SWIMMING,
          "奥运游泳馆改造的公共泳池与水上乐园,常年开放。"),
    _seed("国家速滑馆(冰丝带)", 40.0160281, 116.3713841, CATEGORY_SPORT, SPORT_SPEED_SKATING,
          "冬奥速滑馆,赛后向大众开放冰上运动体验。"),
    _seed("上海国际赛车场", 31.3399793, 121.2195976, CATEGORY_SPORT, SPORT_MOTOR,
          "F1 中国大奖赛赛道,可参加赛道日与卡丁车体验。"),
    _seed("珠海国际赛车场", 22.3674667, 113.5558771, CATEGORY_SPORT, SPORT_MOTOR,
          "国内较早的专业赛道,常年举办房车与摩托赛事。"),
    _seed("青岛奥林匹克帆船中心", 36.0557034, 120.3911399, CATEGORY_SPORT, SPORT_SAILING,
          "奥运帆船赛场,现为公共码头,可体验帆船与出海。"),
    _seed("青岛金沙滩", 35.9585074, 120.2402245, CATEGORY_SPORT, SPORT_BEACH,
          "西海岸长沙滩,适合游泳、沙滩球类与水上项目。"),
    _seed("观澜湖高尔夫球会", 22.7419046, 114.0703972, CATEGORY_SPORT, SPORT_GOLF,
          "深圳龙华的大型高尔夫度假区,球场数量多。"),
    _seed("蜈支洲岛", 18.3113515, 109.7618490, CATEGORY_SPORT, SPORT_DIVING,
          "三亚近海海岛,水质清澈,潜水与海上项目集中。"),
    _seed("北戴河", 39.8605725, 119.4354373, CATEGORY_SPORT, SPORT_BEACH,
          "渤海湾传统海滨浴场,游泳与沙滩运动历史悠久。"),
    _seed("白城沙滩", 24.4332909, 118.0996332, CATEGORY_SPORT, SPORT_BEACH,
          "厦门环岛路旁的城市沙滩,游泳与沙滩排球都方便。"),
    _seed("阳朔月亮山", 24.7245479, 110.4723972, CATEGORY_SPORT, SPORT_CLIMBING,
          "喀斯特岩壁经典攀岩地,自然线路难度分布广。"),
    _seed("林州太行大峡谷", 36.1460857, 113.8847719, CATEGORY_SPORT, SPORT_CLIMBING,
          "太行山峡谷群,徒步、攀岩与溯溪线路丰富。"),
    _seed("古龙峡", 23.7826343, 112.9531040, CATEGORY_SPORT, SPORT_RAFTING,
          "清远峡谷漂流河道,落差大,夏季亲水热门地。"),
    _seed("十渡", 39.6460483, 115.5833880, CATEGORY_SPORT, SPORT_RAFTING,
          "北京房山拒马河沿岸,漂流与山水徒步的郊游地。"),
    _seed("野三坡", 39.6706326, 115.4481434, CATEGORY_SPORT, SPORT_RAFTING,
          "河北涞水峡谷地带,漂流、骑行与徒步集中在一处。"),
    _seed("平谷金海湖", 40.1727123, 117.3027763, CATEGORY_SPORT, SPORT_SAILING,
          "京郊大型湖区,帆船与皮划艇等水上项目集中。"),
    _seed("妙峰山", 40.0725951, 116.0141011, CATEGORY_SPORT, SPORT_CYCLING,
          "北京西山盘山公路,骑行与徒步的经典拉练路线。"),
    _seed("草原天路(东线)", 41.5498689, 115.9045572, CATEGORY_SPORT, SPORT_CYCLING,
          "张家口坝上公路,长距离骑行与自驾观景路线。"),
    _seed("阳澄湖半岛旅游度假区", 31.4176149, 120.7728928, CATEGORY_SPORT, SPORT_CYCLING,
          "苏州环湖绿道,骑行与跑步路线平缓好走。"),
    _seed("滴水湖", 30.9093062, 121.9257641, CATEGORY_SPORT, SPORT_CYCLING,
          "临港环湖步道与骑行道,水面开阔适合长距离拉练。"),
)

#: 全部种子(滑雪场在前、运动在后);只读常量,取数据请走 :func:`load_seeds`。
SEEDS: tuple[dict[str, Any], ...] = SKI_SEEDS + SPORT_SEEDS


def enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """种子数据开关:环境变量 :data:`ENV_SEEDS` 未设置/留空 = 开,显式否定值 = 关。"""
    env = os.environ if environ is None else environ
    raw = str(env.get(ENV_SEEDS, "") or "").strip().lower()
    if not raw:
        return True
    return raw not in DISABLED_VALUES


def name_key(name: Any) -> str:
    """名字归一(去装饰字符 + 小写),作为"名字 + 坐标"去重键的名字部分。"""
    text = str(name or "").strip().lower()
    return "".join(char for char in text if char not in NAME_TRIM_CHARS)


def _names_match(key_a: str, key_b: str) -> bool:
    """两个归一化名字是否指同一个地方:相等,或短名被长名包含(短名不能太短)。"""
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True
    shorter, longer = sorted((key_a, key_b), key=len)
    return len(shorter) >= MIN_NAME_KEY_LEN and shorter in longer


def same_place(
    name_a: Any,
    lat_a: float,
    lng_a: float,
    name_b: Any,
    lat_b: float,
    lng_b: float,
    *,
    radius_km: float = DEDUPE_RADIUS_KM,
) -> bool:
    """"名字 + 坐标"去重判定:名字对得上**且**大圆距离在 ``radius_km`` 内才算同一地。"""
    if not _names_match(name_key(name_a), name_key(name_b)):
        return False
    return haversine_km(float(lat_a), float(lng_a), float(lat_b), float(lng_b)) <= float(radius_km)


def load_seeds(
    *,
    categories: Optional[Iterable[str]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> list[dict[str, Any]]:
    """取归一化后的种子列表;关掉开关时返回空列表。

    归一化做两件事:``tags`` 补上 ``source=种子``(来源标注就落在这个键上,
    见 :func:`db.models.place_source`),以及返回**拷贝**(调用方改动不污染常量)。
    """
    if not enabled(environ):
        return []
    wanted = {str(item).strip() for item in categories} if categories else None
    rows: list[dict[str, Any]] = []
    for seed in SEEDS:
        category = str(seed["category"])
        if wanted is not None and category not in wanted:
            continue
        rows.append(
            {
                "name": str(seed["name"]),
                "lat": float(seed["lat"]),
                "lng": float(seed["lng"]),
                "category": category,
                "tags": {**dict(seed["tags"]), SOURCE_TAG: SEED_SOURCE},
                "intro": str(seed["intro"]),
            }
        )
    return rows


def seeds_in_band(
    seeds: Optional[Iterable[Mapping[str, Any]]],
    lat: float,
    lng: float,
    band: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """筛出落在环形分段内的种子,补 ``distance_km`` 并按由近及远排序。

    与 :func:`services.bands.filter_to_band` 同一口径(``low <= d < high``,haversine),
    所以种子和 OSM 结果混在一起时距离与排序是一致的。
    """
    kept: list[dict[str, Any]] = []
    for seed in seeds or []:
        distance = haversine_km(float(lat), float(lng), float(seed["lat"]), float(seed["lng"]))
        if not in_band(distance, band):
            continue
        row = dict(seed)
        row["distance_km"] = round(distance, DISTANCE_PRECISION)
        kept.append(row)
    kept.sort(key=lambda row: (row["distance_km"], str(row.get("name") or "")))
    return kept


def attach_seeds(
    rows: Optional[Iterable[Mapping[str, Any]]],
    seeds: Optional[Iterable[Mapping[str, Any]]] = None,
    *,
    radius_km: float = DEDUPE_RADIUS_KM,
) -> list[dict[str, Any]]:
    """把种子合并进已有(OSM)结果,**只返回需要补的那些**(不修改入参)。

    去重键 = 名字 + 坐标:已有结果里存在同名(相等或互相包含)且距离 ≤ ``radius_km``
    的地物,就认为 OSM 已经覆盖,种子**不再重复补**(OSM 数据优先,它有真实 OSM id、
    tag 更全)。已被接受的种子同样参与去重,所以种子列表内部重复也只会补一条。
    """
    known: list[tuple[str, float, float]] = [
        (name_key(row.get("name")), float(row["lat"]), float(row["lng"]))
        for row in (rows or [])
        if row.get("lat") is not None and row.get("lng") is not None
    ]
    fresh: list[dict[str, Any]] = []
    for seed in seeds if seeds is not None else load_seeds():
        key = name_key(seed.get("name"))
        lat = float(seed["lat"])
        lng = float(seed["lng"])
        if any(
            _names_match(key, known_key)
            and haversine_km(lat, lng, known_lat, known_lng) <= float(radius_km)
            for known_key, known_lat, known_lng in known
        ):
            continue
        known.append((key, lat, lng))
        fresh.append(dict(seed))
    return fresh


def seed_stats(seeds: Optional[Sequence[Mapping[str, Any]]] = None) -> dict[str, Any]:
    """种子概览(条数 + 分类分布),给 ``/api/places/meta`` 与 CLI 用。"""
    rows = list(seeds) if seeds is not None else load_seeds()
    by_category: dict[str, int] = {}
    for row in rows:
        category = str(row.get("category") or "")
        by_category[category] = by_category.get(category, 0) + 1
    return {
        "enabled": enabled(),
        "total": len(rows),
        "by_category": dict(sorted(by_category.items())),
        "source": SEED_SOURCE,
        "env": ENV_SEEDS,
        "total_defined": len(SEEDS),
    }


def validate(seeds: Optional[Sequence[Mapping[str, Any]]] = None) -> list[str]:
    """自检:返回问题清单(空列表 = 全部通过)。单测与 ``--validate`` 都走这里。

    逐条核对:名称/简介非空且简介不超长、坐标在合法范围且落在中国境内、
    分类是已知四分类、**声明的分类与 ``categorize(tags)`` 的结果一致**
    (种子没有 OSM id,分类完全由 tag 决定,这条最关键)、名字不重复。
    """
    rows = list(seeds) if seeds is not None else list(SEEDS)
    problems: list[str] = []
    seen: dict[str, str] = {}
    for index, seed in enumerate(rows):
        name = str(seed.get("name") or "").strip()
        label = name or f"第 {index + 1} 条"
        if not name:
            problems.append(f"{label}:名称为空")

        intro = str(seed.get("intro") or "").strip()
        if not intro:
            problems.append(f"{label}:简介为空(种子必须自带静态文案,不依赖 LLM)")
        elif len(intro) > MAX_SEED_INTRO_CHARS:
            problems.append(f"{label}:简介 {len(intro)} 字,超过 {MAX_SEED_INTRO_CHARS} 字上限")

        try:
            lat = float(seed["lat"])
            lng = float(seed["lng"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"{label}:坐标缺失或不是数字(lat={seed.get('lat')!r}, lng={seed.get('lng')!r})")
            continue
        if not LAT_RANGE[0] <= lat <= LAT_RANGE[1] or not LNG_RANGE[0] <= lng <= LNG_RANGE[1]:
            problems.append(f"{label}:坐标超出合法范围({lat}, {lng})")
        elif not (18.0 <= lat <= 54.0 and 73.0 <= lng <= 135.5):
            problems.append(f"{label}:坐标 ({lat}, {lng}) 不在中国境内,疑似录错")

        category = str(seed.get("category") or "").strip()
        if not is_known_category(category):
            problems.append(f"{label}:分类 {category!r} 不是已知四分类")
        else:
            derived = categorize(seed.get("tags"))
            if derived != category:
                problems.append(
                    f"{label}:声明分类 {category} 与 tag 归类结果 {derived} 不一致(tags={seed.get('tags')!r})"
                )

        key = name_key(name)
        if key:
            if key in seen:
                problems.append(f"{label}:与 {seen[key]} 重名(名字 + 坐标 去重键冲突)")
            else:
                seen[key] = name
    return problems


def main(argv: Optional[list[str]] = None) -> int:
    """CLI:校验 / 打印 / 给已入库分段补种。``python -m services.seed_data --list``"""
    # 延迟导入:seed_data 是纯数据层,不该在 import 时把 DB 编排层拉进来(会成环)
    from db import init_db, make_engine, open_session
    from db import repository as repo
    from services import place_loader

    parser = argparse.ArgumentParser(
        description="人工种子数据(滑雪场/运动):校验、打印,或给已入库的 (城市, band) 补种"
    )
    parser.add_argument("--list", action="store_true", help="按分类打印全部种子")
    parser.add_argument("--validate", action="store_true", help="自检数据(分类/坐标/简介/重名)")
    parser.add_argument("--city", default=None,
                        help="给这个城市**已入库**的分段补种(只读本地库,不触网)")
    parser.add_argument("--band", default=None, choices=band_keys(),
                        help="只补某个分段;与 --city 连用,该段未入库时用 Nominatim 解析起点(触网)")
    parser.add_argument("--db", default=None,
                        help="数据库 URL(默认 WHERE2GO_DB_URL 或 backend/data/where2go.db)")
    parser.add_argument("--show", type=int, default=0, help="补种后打印前 N 条种子(默认 0 = 不打印)")
    args = parser.parse_args(argv)

    if not (args.list or args.validate or args.city):
        parser.print_help()
        return 1

    seeds = load_seeds()
    if not seeds:
        print(f"[跳过] 种子数据已被环境变量 {ENV_SEEDS} 关闭;模块内共定义 {len(SEEDS)} 条。")
        return 0

    exit_code = 0
    if args.validate:
        problems = validate()
        stats = seed_stats(seeds)
        detail = " · ".join(f"{name} {total}" for name, total in stats["by_category"].items())
        if problems:
            exit_code = 1
            print(f"[不通过] {stats['total']} 条种子里有 {len(problems)} 个问题:")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print(f"[通过] {stats['total']} 条种子全部合法:{detail}")

    if args.list:
        for category in (CATEGORY_SKI, CATEGORY_SPORT):
            rows = [row for row in seeds if row["category"] == category]
            print(f"\n== {category}({len(rows)} 条)==")
            for row in rows:
                print(f"  - {row['name']}  {row['lat']:.6f},{row['lng']:.6f}  {row['intro']}")

    if not args.city:
        return exit_code

    engine = make_engine(args.db)
    init_db(engine)
    bands = [args.band] if args.band else band_keys()
    total = 0
    with open_session(engine) as session:
        for band_key in bands:
            band = require_band(band_key)
            recorded = repo.get_segment(session, origin_city=args.city, band=band_key)
            if recorded is not None:
                origin = place_loader.stored_origin(recorded)
            elif args.band:
                # 显式指定了分段但还没入库:补种也得先有原点,只能问 Nominatim(触网)
                origin = place_loader.resolve_origin(args.city)
                recorded = place_loader.record_seed_segment(session, city=args.city, band=band,
                                                           origin=origin)
            else:
                continue
            candidates = seeds_in_band(seeds, origin["lat"], origin["lng"], band)
            seeded = place_loader.ensure_seeded(session, origin=origin, band=band, seeds=candidates)
            if seeded and recorded is not None:
                recorded.place_count = int(recorded.place_count or 0) + seeded
            total += seeded
            print(f"[{args.city} · {band['label']}] 带内种子 {len(candidates)} 条 · 本次补种 {seeded} 条")
            if args.show:
                rows = repo.list_places(session, origin_city=args.city, band=band_key,
                                       origin_lat=origin["lat"], origin_lng=origin["lng"])
                for row in [r for r in rows if r.get("source") == SEED_SOURCE][: args.show]:
                    print(f"    - {row['name']}({row['category']}) 距起点 {row['distance_km']} km")
        session.commit()
    print(f"[完成] {args.city} 共补种 {total} 条(种子总量 {len(seeds)} 条,已存在的不会重复补)")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
