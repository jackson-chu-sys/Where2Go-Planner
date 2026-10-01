"""路线编排 + 费用估算 + deep-link 跳转链接(TASK-2a,阶段2 M2 的后端)。

一个入口 :func:`plan_routes`:给起点/目的地坐标,返回**驾车 / 铁路 / 飞机**三种方式的
时间 + 费用对比,每条路线形状统一(docs/STAGE2-PLAN.md 第 2/4 节)::

    {"mode": "driving|rail|flight", "label": "驾车", "duration_min": 94,
     "cost_cny": 884, "distance_km": 122.4, "geometry": [[lat, lng], ...] | None,
     "kind": "real|estimate", "degraded": False, "source": "amap",
     "note": "...", "links": [{"provider", "label", "url", "note"}, ...]}

口径与**诚实标注**(ADR-007:公共交通/OTA 不抓实时数据,只做估算 + deep-link):

* **驾车**:走**高德 v3** 驾车路线(:func:`data_sources.amap.driving`,TASK-9c 起替代 OSRM),
  时长/里程/折线都是真实路网 → ``kind="real"``;**费用仍是估算**(油耗 + 收费里程费率),
  note 里写明。高德失败(没配 key、超配额、两点不连通)时**不抛错**:该条降级成
  ``kind="estimate"`` + ``degraded=True``、时长/费用/geometry 为 ``None``,
  note 说明原因,deep-link 照给(跳转导航不依赖路线接口)。
* **铁路 / 飞机**:耗时沿用 POC ``app/api/discover.py`` 的 ``_est_mode`` 口径
  (直线距离 × 绕行系数 / 均速 + 地面接驳),费用按里程 × 单价 → 全是估算,
  ``kind="estimate"``,note 一律带「估算·非实时·以官方为准」。
* **出现阈值**:直线距离 ≥ :data:`RAIL_MIN_KM` 才出铁路(沿用 POC)、
  ≥ :data:`FLIGHT_MIN_KM` 才出飞机(TASK-6d 由 300km 提到 **600km**:短途机票
  既不划算也常被高铁替代);驾车始终出现。POC ``/api/discover`` 的 300km 口径**不动**。

**费用引擎 v2(TASK-6d)**:三种方式都从"一个笼统数字"升级成可解释的口径,
响应字段只增不删(``cost_cny`` / ``kind`` 语义不变):

* 驾车给出**构成明细** ``cost_breakdown{toll, fuel, mode}`` + **整车/人均双标**
  (``vehicle_label`` / ``per_person_cny``):油费 = 里程 × :data:`FUEL_L_PER_KM` × 油价
  (env ``WHERE2GO_FUEL_PRICE_CNY_L``);过路费 = **收费里程** × 区域费率
  (东/中/西 = 0.45/0.40/0.35 元/km),收费里程取高德 ``toll_distance``(**真实值**,
  ``mode="amap_toll_distance"``;高德的 ``tolls`` 恒 0、``cost`` 恒 null,**不能用**,
  见契约 §1.5),该字段缺失才退化成 里程 × 0.55(``mode="heuristic"``)。
* 铁路按**运营里程**(直线 × :data:`RAIL_FARE_DETOUR`)× **分档费率**(350km/h 线
  0.46、250km/h 线 0.31 元/km,按两端是否"双高铁枢纽"判档);命中
  :data:`RAIL_SEED_FARES`(人工校录的热门城市对真实票价)时直接用种子价并标
  ``price_source="seed"``,否则 ``"estimate"``。
* 机票用**民航公布价**锚定区间(纯规则,无 LLM、不抓 OTA):公布价 ≈ 航段里程 ×
  :data:`PUBLISHED_FARE_TIERS` 分段费率,区间 = [公布价 × 典型折扣, 公布价]
  (主干商务线 0.45 / 支线 0.6),``cost_cny`` 取区间中值以兼容旧字段;直线
  < :data:`FLIGHT_PRICE_MIN_KM` 或任一端城市没有民航机场(:data:`AIRPORT_CITIES`)时
  **不给票价**(``flight_low_cny`` / ``flight_high_cny`` / ``cost_cny`` 全为 ``None``),
  只留 deep-link —— 宁可不估,不瞎估。

所有费用系数、城市表与种子价集中在下面「费用估算系数 v2」一段常量里,
日后换成真实票价/实时路况只改那里,:func:`cost_coefficients` 会把当前系数原样吐给前端做标注。

deep-link(:func:`amap_navigation_url` / :func:`google_maps_directions_url` /
:func:`rail_12306_url` / :func:`flight_ota_url`)都是**纯函数**:不触网、不查库、
中文地名按 UTF-8 百分号编码,给定同样的入参永远得到同样的 URL,可直接单测。
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

from data_sources import DataSourceError
from data_sources import amap
from data_sources import haversine_km
from db.models import iso_utc, utcnow

# --------------------------------------------------------------------------- #
# 出行方式、出现阈值与耗时系数(沿用 POC app/api/discover.py 的 _est_mode 口径)
# --------------------------------------------------------------------------- #

MODE_DRIVING = "driving"
MODE_RAIL = "rail"
MODE_FLIGHT = "flight"
MODES: tuple[str, ...] = (MODE_DRIVING, MODE_RAIL, MODE_FLIGHT)

# 卡片文案与图标(emoji 与 services.classify 的四分类 pin 一个路子,前端可直接用)
MODE_META: dict[str, dict[str, str]] = {
    MODE_DRIVING: {"label": "驾车", "emoji": "🚗"},
    MODE_RAIL: {"label": "铁路(估算)", "emoji": "🚄"},
    MODE_FLIGHT: {"label": "飞机(估算)", "emoji": "✈️"},
}

RAIL_MIN_KM = 100.0     # 直线距离 >= 才出现铁路
# 飞机出现阈值:TASK-6d 由 300km 提到 600km(短途机票性价比低、且多被高铁替代)。
# POC ``app/api/discover.py`` 自带常量仍是 300km,不受影响(AGENTS.md:不改 POC 路由行为)。
FLIGHT_MIN_KM = 600.0   # 直线距离 >= 才出现飞机
FLIGHT_PRICE_MIN_KM = 400.0  # 直线距离 >= 才**给机票价**;不到就只留 deep-link
RAIL_DETOUR = 1.20      # 铁路路径绕行系数(相对直线)
RAIL_SPEED_KMH = 220.0  # 城际均速(含中间停靠)
RAIL_GROUND_MIN = 110.0  # 候车 + 起/终点站与景点接驳
FLIGHT_DETOUR = 1.10
FLIGHT_SPEED_KMH = 780.0
FLIGHT_GROUND_MIN = 210.0  # 值机安检 + 两端机场接驳

# --------------------------------------------------------------------------- #
# 费用估算系数 v2(TASK-6d)—— 全部集中在此,换真实票价/实时路况只改这一段
# --------------------------------------------------------------------------- #

# --- 驾车:油费 + 高速过路费 = 整车费用,再摊成人均 --- #
FUEL_L_PER_KM = 0.08                     # 油耗(L/km)
FUEL_L_PER_100KM = FUEL_L_PER_KM * 100.0  # 展示口径(8L/100km)
ENV_FUEL_PRICE = "WHERE2GO_FUEL_PRICE_CNY_L"  # 油价环境变量名
FUEL_PRICE_CNY_PER_L = 8.0               # 油价默认值(元/L);env 缺失/非法时用它
HEURISTIC_HIGHWAY_RATIO = 0.55           # 高德没给 toll_distance 时:收费里程 ≈ 总里程 × 0.55
# cost_breakdown.mode:收费里程来自高德 ``toll_distance``(真实值) / 系数估的
TOLL_MODE_AMAP_TOLL_DISTANCE = "amap_toll_distance"
TOLL_MODE_HEURISTIC = "heuristic"

REGION_EAST = "east"
REGION_CENTRAL = "central"
REGION_WEST = "west"
REGIONS: tuple[str, ...] = (REGION_EAST, REGION_CENTRAL, REGION_WEST)
REGION_LABELS: dict[str, str] = {REGION_EAST: "东部", REGION_CENTRAL: "中部", REGION_WEST: "西部"}
# 高速通行费(元/km):按三大经济地带分档(东部路网密/造价高,西部有政策优惠)
TOLL_CNY_PER_KM_BY_REGION: dict[str, float] = {
    REGION_EAST: 0.45, REGION_CENTRAL: 0.40, REGION_WEST: 0.35,
}
DEFAULT_REGION = REGION_EAST             # 区域判不出来时的兜底(取最高档,宁可高估)
REGION_LNG_EAST_MIN = 112.5              # 名字表命中不了时按中点经度分带:≥112.5 → 东部
REGION_LNG_CENTRAL_MIN = 102.0           # 102 ~ 112.5 → 中部;<102 → 西部
# 省级名 → 区域(地名里含省名即命中:"浙江省"/"浙江"/"杭州浙江"都算)
REGION_BY_PROVINCE: dict[str, tuple[str, ...]] = {
    REGION_EAST: ("北京", "天津", "河北", "辽宁", "上海", "江苏", "浙江",
                  "福建", "山东", "广东", "海南"),
    REGION_CENTRAL: ("山西", "吉林", "黑龙江", "安徽", "江西", "河南", "湖北", "湖南"),
    REGION_WEST: ("内蒙古", "广西", "重庆", "四川", "贵州", "云南", "西藏",
                  "陕西", "甘肃", "青海", "宁夏", "新疆"),
}
# 城市名 → 区域(名字里不含省名的常见出发地/目的地)
REGION_BY_CITY: dict[str, str] = {
    "深圳": REGION_EAST, "大连": REGION_EAST, "青岛": REGION_EAST, "宁波": REGION_EAST,
    "杭州": REGION_EAST, "南京": REGION_EAST, "苏州": REGION_EAST, "无锡": REGION_EAST,
    "温州": REGION_EAST, "厦门": REGION_EAST, "福州": REGION_EAST, "广州": REGION_EAST,
    "珠海": REGION_EAST, "惠州": REGION_EAST, "汕头": REGION_EAST, "济南": REGION_EAST,
    "海口": REGION_EAST, "三亚": REGION_EAST, "烟台": REGION_EAST, "绍兴": REGION_EAST,
    "武汉": REGION_CENTRAL, "长沙": REGION_CENTRAL, "郑州": REGION_CENTRAL,
    "洛阳": REGION_CENTRAL, "南昌": REGION_CENTRAL, "合肥": REGION_CENTRAL,
    "太原": REGION_CENTRAL, "宜昌": REGION_CENTRAL, "襄阳": REGION_CENTRAL,
    "哈尔滨": REGION_CENTRAL, "长春": REGION_CENTRAL, "大同": REGION_CENTRAL,
    "黄石": REGION_CENTRAL, "岳阳": REGION_CENTRAL, "九江": REGION_CENTRAL,
    "成都": REGION_WEST, "西安": REGION_WEST, "昆明": REGION_WEST, "贵阳": REGION_WEST,
    "兰州": REGION_WEST, "西宁": REGION_WEST, "银川": REGION_WEST, "南宁": REGION_WEST,
    "桂林": REGION_WEST, "丽江": REGION_WEST, "大理": REGION_WEST, "延安": REGION_WEST,
    "榆林": REGION_WEST, "敦煌": REGION_WEST, "拉萨": REGION_WEST, "绵阳": REGION_WEST,
    "宜宾": REGION_WEST, "遵义": REGION_WEST, "呼伦贝尔": REGION_WEST,
    "鄂尔多斯": REGION_WEST, "乌鲁木齐": REGION_WEST, "呼和浩特": REGION_WEST,
}
DRIVING_SEATS = 4                        # 人均口径:整车按 4 人摊
VEHICLE_LABEL = "整车≤4人"               # 驾车费用的口径标注(前端可直接展示)

# --- 铁路:运营里程 × 分档费率;命中热门城市对种子价就直接用种子 --- #
# 计费(运营)里程系数 1.15;**耗时**仍沿用 POC 的 RAIL_DETOUR=1.20,两者口径分开。
RAIL_FARE_DETOUR = 1.15
RAIL_TIER_FAST_KMH = 350                 # 两端都是高铁枢纽 → 按 350km/h 线计费
RAIL_TIER_SLOW_KMH = 250                 # 其余 → 按 250km/h 线计费
RAIL_RATE_CNY_PER_KM: dict[int, float] = {RAIL_TIER_FAST_KMH: 0.46, RAIL_TIER_SLOW_KMH: 0.31}
RAIL_MIN_FARE_CNY = 20.0                 # 起步价下限(元)
PRICE_SOURCE_SEED = "seed"               # 票价来源:人工种子
PRICE_SOURCE_ESTIMATE = "estimate"       # 票价来源:分档费率估算
# 高铁枢纽城市表(判"双高铁枢纽"→ 350km/h 档;机票主干商务线折扣也复用它)
RAIL_HUB_CITIES: frozenset[str] = frozenset({
    "北京", "上海", "天津", "广州", "深圳", "杭州", "南京", "苏州", "无锡", "常州",
    "宁波", "温州", "嘉兴", "金华", "合肥", "福州", "厦门", "南昌", "济南", "青岛",
    "徐州", "郑州", "洛阳", "武汉", "长沙", "石家庄", "保定", "太原", "西安", "成都",
    "重庆", "昆明", "贵阳", "南宁", "桂林", "沈阳", "大连", "长春", "哈尔滨", "兰州",
    "西宁", "银川", "乌鲁木齐", "惠州", "珠海", "绍兴", "台州", "烟台", "潍坊",
})
# 人工校录的热门城市对**二等座**公布票价(元;12306 常态价,非促销、非浮动)。
# 命中即用种子价并标 price_source="seed";键无序(上海—北京 == 北京—上海)。
RAIL_SEED_FARES: tuple[tuple[str, str, float], ...] = (
    ("上海", "北京", 553.0),
    ("杭州", "上海", 73.0),
    ("上海", "南京", 134.5),
    ("上海", "苏州", 39.5),
    ("杭州", "南京", 117.5),
    ("北京", "天津", 54.5),
    ("北京", "郑州", 309.0),
    ("北京", "西安", 515.5),
    ("上海", "武汉", 301.5),
    ("广州", "深圳", 74.5),
    ("广州", "长沙", 314.0),
    ("成都", "重庆", 154.0),
)
RAIL_SEED_SOURCE_NOTE = "人工校录的热门城市对二等座公布票价(12306 常态价,非促销)"

# --- 机票:民航公布价锚定区间(纯规则,无 LLM、不抓 OTA) --- #
# 公布价分段费率(元/km):里程 <812km → 1.6;812~1600km(含两端)→ 0.95;>1600km → 0.8。
# 第三档的 ``None`` 表示"以上"。判档口径见 :func:`published_fare_tier`。
PUBLISHED_FARE_TIERS: tuple[tuple[Optional[float], float], ...] = (
    (812.0, 1.6),
    (1600.0, 0.95),
    (None, 0.8),
)
FLIGHT_DISCOUNT_TRUNK = 0.45             # 主干商务线典型折扣(两端都是枢纽)
FLIGHT_DISCOUNT_BRANCH = 0.6             # 支线典型折扣
FLIGHT_DYNAMIC_NOTE = "动态定价·浮动大·实时价以跳转为准"
# 有民航机场的城市表(≥40 城):**任一端不在表里就不给机票价**,只留 deep-link。
# 刻意不含 苏州 / 黄冈 / 东莞 / 中山 / 六安 / 马鞍山 等无民航机场的城市。
AIRPORT_CITIES: frozenset[str] = frozenset({
    "北京", "上海", "天津", "广州", "深圳", "杭州", "南京", "成都", "重庆", "武汉",
    "西安", "长沙", "郑州", "青岛", "大连", "沈阳", "哈尔滨", "长春", "昆明", "贵阳",
    "南宁", "海口", "三亚", "厦门", "福州", "南昌", "合肥", "济南", "石家庄", "太原",
    "兰州", "西宁", "银川", "乌鲁木齐", "拉萨", "呼和浩特", "温州", "宁波", "无锡",
    "常州", "珠海", "桂林", "丽江", "大理", "张家界", "敦煌", "喀什", "伊宁", "景洪",
    "绵阳", "宜宾", "泸州", "遵义", "襄阳", "宜昌", "黄山", "武夷山", "泉州", "烟台",
    "威海", "义乌", "盐城", "徐州", "唐山", "大同", "鄂尔多斯", "呼伦贝尔", "丹东",
    "齐齐哈尔", "大庆", "佳木斯", "延安", "榆林",
})

COST_PRECISION = 0       # 费用取整到元
DISTANCE_PRECISION = 1   # 里程保留 1 位小数
COORD_LABEL_PRECISION = 4  # 没有地名时,用坐标当展示名的精度
# geometry 抽稀上限:长路线的 steps[].polyline 动辄上万点,服务端先抽到画线够用的量级,
# 前端要更平滑可自行再插值(见 STAGE2-PLAN 第 6 节风险 3)。
# :mod:`data_sources.amap` 解析 polyline 时已按同值抽稀一次,这里是二次保险(替身/旧数据)。
GEOMETRY_MAX_POINTS = 1200

# 绕行系数(**耗时**口径,与 POC ``_est_mode`` 同源):铁路 1.20、机票航段 1.10;
# 驾车直接用高德的真实里程,不用系数。
# 注意:铁路**票价**另用 :data:`RAIL_FARE_DETOUR`(1.15,运营里程口径),两者故意分开。
BILLABLE_DETOUR = {MODE_RAIL: RAIL_DETOUR, MODE_FLIGHT: FLIGHT_DETOUR}

# 地名归一用的已知城市集合(枢纽表 ∪ 机场表 ∪ 区域表 ∪ 种子价城市):
# "上海虹桥站"/"杭州西湖" 这类带后缀的名字靠它归一到城市,归一不了就原样保留。
KNOWN_CITIES: frozenset[str] = frozenset(
    RAIL_HUB_CITIES | AIRPORT_CITIES | set(REGION_BY_CITY)
    | {city for pair in RAIL_SEED_FARES for city in pair[:2]}
)
# 种子票价索引:键是**无序**城市对(frozenset),上海—北京 == 北京—上海。
RAIL_SEED_INDEX: dict[frozenset[str], tuple[str, str, float]] = {
    frozenset((city_a, city_b)): (city_a, city_b, fare)
    for city_a, city_b, fare in RAIL_SEED_FARES
}

KIND_REAL = "real"
KIND_ESTIMATE = "estimate"
SOURCE_AMAP = "amap"
SOURCE_ESTIMATE = "estimate"
SOURCE_UNAVAILABLE = "unavailable"
ESTIMATE_DISCLAIMER = "估算·非实时·以官方为准"

DRIVING_NOTE = (
    "时长/里程为高德真实路网(非实时路况,不含拥堵与休息);费用为估算,"
    f"口径 {VEHICLE_LABEL}(人均 = 整车 ÷ {DRIVING_SEATS} 人):"
    f"油费 = 里程 × {FUEL_L_PER_KM:g}L/km × 油价(env {ENV_FUEL_PRICE},"
    f"默认 {FUEL_PRICE_CNY_PER_L:g}元/L);过路费 = 收费里程 × 区域费率"
    f"(东部 {TOLL_CNY_PER_KM_BY_REGION[REGION_EAST]:g}/"
    f"中部 {TOLL_CNY_PER_KM_BY_REGION[REGION_CENTRAL]:g}/"
    f"西部 {TOLL_CNY_PER_KM_BY_REGION[REGION_WEST]:g} 元/km);收费里程取高德"
    f" toll_distance(真实收费路段里程,cost_breakdown.mode={TOLL_MODE_AMAP_TOLL_DISTANCE};"
    f"高德的 tolls/cost 字段个人 key 恒为 0/null,不用),该字段缺失则按 里程 ×"
    f" {HEURISTIC_HIGHWAY_RATIO:g} 估算(mode={TOLL_MODE_HEURISTIC})。{ESTIMATE_DISCLAIMER}"
)
DRIVING_DEGRADED_NOTE = (
    "高德驾车路线暂不可用(未配置 key、配额/限流,或两点不在同一路网/坐标离路网太远):"
    f"时长与费用本次给不出,不做瞎估;可直接用下方导航链接跳转官方。{ESTIMATE_DISCLAIMER}"
)

ORIGIN_FALLBACK_LABEL = "我的位置"
DESTINATION_FALLBACK_LABEL = "目的地"

RouterFn = Callable[[Sequence[float], Sequence[float]], Mapping[str, Any]]


# --------------------------------------------------------------------------- #
# 费用估算(纯函数)
# --------------------------------------------------------------------------- #


def _km(distance_km: float) -> float:
    """里程归一:非负 float(负数按 0 处理,免得算出负费用)。"""
    return max(float(distance_km), 0.0)


def round_cost(value: float) -> int:
    """费用取整到元(:data:`COST_PRECISION`,**四舍五入半进位**:74.5 → 75)。

    不用内置 ``round``:它是银行家舍入(``round(74.5) == 74``),而铁路票面价常以 ``.5``
    结尾(54.5 / 134.5),用它会凭空少一块钱。
    """
    return int(math.floor(float(value) + 0.5))


# --------------------------------------------------------------------------- #
# 驾车:油费 + 高速过路费 → 整车 / 人均双标(TASK-6d)
# --------------------------------------------------------------------------- #


def fuel_price_cny_per_l(environ: Optional[Mapping[str, str]] = None) -> float:
    """油价(元/L):读 env :data:`ENV_FUEL_PRICE`;缺失/非法/非正数 → 默认值。

    只认 ``> 0`` 的有限数字,写错环境变量不会把费用算成 0 或负数(降级成默认 8.0 元/L)。
    """
    env = os.environ if environ is None else environ
    raw = env.get(ENV_FUEL_PRICE)
    if raw is None or not str(raw).strip():
        return FUEL_PRICE_CNY_PER_L
    try:
        price = float(str(raw).strip())
    except (TypeError, ValueError):
        return FUEL_PRICE_CNY_PER_L
    if price != price or price in (float("inf"), float("-inf")) or price <= 0.0:
        return FUEL_PRICE_CNY_PER_L
    return price


def region_by_longitude(lng: float) -> str:
    """经度 → 费率区域(地名认不出来时的兜底):东/中/西三带按经度粗分。"""
    value = float(lng)
    if value >= REGION_LNG_EAST_MIN:
        return REGION_EAST
    if value >= REGION_LNG_CENTRAL_MIN:
        return REGION_CENTRAL
    return REGION_WEST


def resolve_region_by_name(name: Optional[str]) -> Optional[str]:
    """地名 → 费率区域:先按省名、再按城市名做包含匹配;都不认识返回 ``None``。"""
    text = clean_name(name)
    if not text:
        return None
    core = text.rstrip("省市") or text
    for region, provinces in REGION_BY_PROVINCE.items():
        if any(province in core for province in provinces):
            return region
    for city, city_region in REGION_BY_CITY.items():
        if city in core:
            return city_region
    return None


def driving_region(*, from_name: Optional[str] = None, to_name: Optional[str] = None,
                   mid_lng: Optional[float] = None) -> str:
    """本次驾车按哪档区域费率算:目的地名 > 起点名 > 中点经度分带(都是估算口径)。"""
    for name in (to_name, from_name):
        resolved = resolve_region_by_name(name)
        if resolved:
            return resolved
    return region_by_longitude(0.0 if mid_lng is None else mid_lng)


def resolve_toll_km(
    distance_km: float,
    toll_distance_km: Optional[float],
) -> tuple[float, str]:
    """收费里程(km)+ 它的口径标签(``cost_breakdown.mode`` 的值)。

    高德 v3 驾车的 ``tolls`` 恒 ``0``、``cost`` 恒 ``null``(个人 key 不出过路费数值,
    契约 §1.5),但 ``toll_distance``(收费路段米数)是**真实值** → 过路费按
    收费里程 × 区域费率算,标 :data:`TOLL_MODE_AMAP_TOLL_DISTANCE`。

    ``toll_distance`` 缺失/非法(``None``、负数、非数字)时退回
    :data:`HEURISTIC_HIGHWAY_RATIO` 启发式并标 :data:`TOLL_MODE_HEURISTIC` ——
    而不是把过路费直接算成 0(全程无高速才是 0,而高德会给 ``toll_distance=0``)。
    收费里程夹在 ``[0, 总里程]`` 内(高德偶有重复计数)。
    """
    km = _km(distance_km)
    value = toll_distance_km
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return min(km * HEURISTIC_HIGHWAY_RATIO, km), TOLL_MODE_HEURISTIC
    return min(float(value), km), TOLL_MODE_AMAP_TOLL_DISTANCE


def driving_cost_breakdown(
    distance_km: float,
    *,
    toll_distance_km: Optional[float] = None,
    region: Optional[str] = None,
    fuel_price: Optional[float] = None,
) -> dict[str, Any]:
    """驾车费用构成(**未取整**):油费 + 过路费,外加算这两项用到的口径。

    * ``fuel_cny`` = 里程 × :data:`FUEL_L_PER_KM` × 油价(env 可覆盖);
    * ``toll_cny`` = 收费里程 × 区域费率;收费里程来自高德 ``toll_distance`` 时
      ``toll_mode="amap_toll_distance"``,拿不到就按 里程 × 0.55 估并标 ``"heuristic"``
      (判定见 :func:`resolve_toll_km`);
    * ``region`` 非法/缺失 → :data:`DEFAULT_REGION`(取最高档,宁可高估不少估)。
    """
    resolved_region = region if region in TOLL_CNY_PER_KM_BY_REGION else DEFAULT_REGION
    km = _km(distance_km)
    price = fuel_price_cny_per_l() if fuel_price is None else float(fuel_price)
    fuel = km * FUEL_L_PER_KM * price
    toll_km, toll_mode = resolve_toll_km(km, toll_distance_km)
    rate = TOLL_CNY_PER_KM_BY_REGION[resolved_region]
    toll = toll_km * rate
    return {
        "total_cny": toll + fuel,
        "toll_cny": toll,
        "fuel_cny": fuel,
        "toll_mode": toll_mode,
        "toll_km": toll_km,
        "toll_rate_cny_per_km": rate,
        "region": resolved_region,
        "fuel_price_cny_per_l": price,
    }


def driving_cost_cny(distance_km: float, **kwargs: Any) -> float:
    """驾车整车费用(元,未取整)= 油费 + 过路费;里程用高德的真实公里数。"""
    return driving_cost_breakdown(distance_km, **kwargs)["total_cny"]


def driving_money(distance_km: float, **kwargs: Any) -> dict[str, Any]:
    """取整后的驾车金额(**先各项四舍五入再相加**,所以 ``toll + fuel == cost`` 恒成立)。

    返回响应里那三个 v2 字段(``cost_cny`` / ``per_person_cny`` / ``cost_breakdown``),
    外加 note 里要复述的口径明细(``region``、``toll_km``、费率、油价)。
    """
    breakdown = driving_cost_breakdown(distance_km, **kwargs)
    toll = round_cost(breakdown["toll_cny"])
    fuel = round_cost(breakdown["fuel_cny"])
    total = toll + fuel
    return {
        "cost_cny": total,
        "per_person_cny": round_cost(total / DRIVING_SEATS),
        "cost_breakdown": {"toll": toll, "fuel": fuel, "mode": breakdown["toll_mode"]},
        "toll_cny": toll,
        "fuel_cny": fuel,
        "toll_km": breakdown["toll_km"],
        "toll_rate_cny_per_km": breakdown["toll_rate_cny_per_km"],
        "region": breakdown["region"],
        "fuel_price_cny_per_l": breakdown["fuel_price_cny_per_l"],
    }


def billable_km(mode: str, straight_km: float) -> float:
    """铁路/飞机的绕行里程 = 直线距离 × 绕行系数(与耗时估算同源)。

    铁路票价**不用**它(v2 改用 :func:`rail_operating_km` 的 1.15 运营里程口径),
    这里只服务耗时估算与机票航段里程。
    """
    if mode not in BILLABLE_DETOUR:
        raise ValueError(f"该方式不走绕行系数计费:{mode!r}(可选:{'、'.join(BILLABLE_DETOUR)})")
    return _km(straight_km) * BILLABLE_DETOUR[mode]


# --------------------------------------------------------------------------- #
# 地名归一 + 枢纽/机场判定(铁路分档与机票折扣、机场可达性都靠它)
# --------------------------------------------------------------------------- #


def normalize_city(name: Optional[str]) -> Optional[str]:
    """地名 → 城市名:去空白与结尾"市",再用 :data:`KNOWN_CITIES` 做**最长包含匹配**。

    "上海市" / "上海虹桥站" / "杭州西湖" 都能归一到城市;"崇儒乡"这种没有城市线索的
    原样返回(调用方据此判定"不是枢纽 / 没有机场"),``None``/空串 → ``None``。
    """
    text = clean_name(name)
    if not text:
        return None
    core = text.rstrip("市") or text
    best: Optional[str] = None
    for city in KNOWN_CITIES:
        if city in core and (best is None or len(city) > len(best)):
            best = city
    return best or core


def is_hub_city(name: Optional[str]) -> bool:
    """地名是否落在高铁枢纽城市表 :data:`RAIL_HUB_CITIES` 里。"""
    city = normalize_city(name)
    return city is not None and city in RAIL_HUB_CITIES


def is_trunk_pair(from_name: Optional[str], to_name: Optional[str]) -> bool:
    """两端都是高铁枢纽 → 主干线(铁路按 350km/h 档计费、机票按商务线折扣)。"""
    return is_hub_city(from_name) and is_hub_city(to_name)


def resolve_airport_city(name: Optional[str]) -> Optional[str]:
    """地名 → 民航机场城市:命中 :data:`AIRPORT_CITIES` 才返回,否则 ``None``。"""
    city = normalize_city(name)
    return city if city is not None and city in AIRPORT_CITIES else None


def missing_airport_endpoint(from_name: Optional[str], to_name: Optional[str]) -> Optional[str]:
    """没有民航机场的那一端(返回 ``目的地「崇儒乡」`` 这类文案);两端都有 → ``None``。"""
    for label, name in (("出发地", from_name), ("目的地", to_name)):
        if resolve_airport_city(name) is None:
            city = clean_name(name)
            return f"{label}「{city}」" if city else f"{label}(未给地名)"
    return None


# --------------------------------------------------------------------------- #
# 铁路:运营里程 × 分档费率,命中热门城市对种子价就直接用种子(TASK-6d)
# --------------------------------------------------------------------------- #


def rail_operating_km(straight_km: float) -> float:
    """铁路运营里程(计费口径)≈ 直线距离 × :data:`RAIL_FARE_DETOUR`(1.15)。"""
    return _km(straight_km) * RAIL_FARE_DETOUR


def rail_speed_tier_kmh(from_name: Optional[str] = None,
                        to_name: Optional[str] = None) -> int:
    """费率档位:双高铁枢纽 → 350km/h 线,其余 → 250km/h 线。"""
    return RAIL_TIER_FAST_KMH if is_trunk_pair(from_name, to_name) else RAIL_TIER_SLOW_KMH


def rail_rate_cny_per_km(from_name: Optional[str] = None,
                         to_name: Optional[str] = None) -> float:
    """该城市对适用的铁路费率(元/km,二等座)。"""
    return RAIL_RATE_CNY_PER_KM[rail_speed_tier_kmh(from_name, to_name)]


def rail_seed_fare(from_name: Optional[str] = None,
                   to_name: Optional[str] = None) -> Optional[tuple[str, str, float]]:
    """命中热门城市对种子价 → ``(城市A, 城市B, 二等座票价)``;没命中返回 ``None``。"""
    city_a = normalize_city(from_name)
    city_b = normalize_city(to_name)
    if not city_a or not city_b or city_a == city_b:
        return None
    return RAIL_SEED_INDEX.get(frozenset((city_a, city_b)))


def rail_fare(straight_km: float, *, from_name: Optional[str] = None,
              to_name: Optional[str] = None) -> dict[str, Any]:
    """铁路二等座票价:种子价优先(``price_source="seed"``),否则运营里程 × 分档费率。

    估算价不低于起步价 :data:`RAIL_MIN_FARE_CNY`;返回里带上算式用到的每一项,
    note 与系数表都从这里取数,免得两处口径打架。
    """
    operating_km = rail_operating_km(straight_km)
    seed = rail_seed_fare(from_name, to_name)
    if seed is not None:
        city_a, city_b, fare = seed
        return {
            "total_cny": fare,
            "price_source": PRICE_SOURCE_SEED,
            "operating_km": operating_km,
            "tier_kmh": None,
            "rate_cny_per_km": None,
            "seed_pair": (city_a, city_b),
        }
    tier = rail_speed_tier_kmh(from_name, to_name)
    rate = RAIL_RATE_CNY_PER_KM[tier]
    return {
        "total_cny": max(RAIL_MIN_FARE_CNY, operating_km * rate),
        "price_source": PRICE_SOURCE_ESTIMATE,
        "operating_km": operating_km,
        "tier_kmh": tier,
        "rate_cny_per_km": rate,
        "seed_pair": None,
    }


def rail_cost_cny(straight_km: float, **kwargs: Any) -> float:
    """铁路二等座票价(元,未取整)—— :func:`rail_fare` 的总额简写。"""
    return rail_fare(straight_km, **kwargs)["total_cny"]


# --------------------------------------------------------------------------- #
# 机票:民航公布价锚定区间(纯规则,无 LLM、不抓 OTA;TASK-6d)
# --------------------------------------------------------------------------- #


def flight_segment_km(straight_km: float) -> float:
    """航段里程 = 大圆距离 × :data:`FLIGHT_DETOUR`(与耗时估算同源)。"""
    return billable_km(MODE_FLIGHT, straight_km)


def published_fare_tier(segment_km: float) -> tuple[Optional[float], float]:
    """公布价分档(元/km):``<812km``、``812~1600km``(含两端)、``>1600km``。"""
    km = _km(segment_km)
    short_limit, _ = PUBLISHED_FARE_TIERS[0]
    mid_limit, _ = PUBLISHED_FARE_TIERS[1]
    if short_limit is not None and km < short_limit:
        return PUBLISHED_FARE_TIERS[0]
    if mid_limit is not None and km <= mid_limit:
        return PUBLISHED_FARE_TIERS[1]
    return PUBLISHED_FARE_TIERS[2]


def published_fare_tier_label(segment_km: float) -> str:
    """分档的人话版(note 里用):如 ``<812km 档 1.6元/km``。"""
    tier = published_fare_tier(segment_km)
    limit, rate = tier
    if tier is PUBLISHED_FARE_TIERS[0] and limit is not None:
        return f"<{limit:g}km 档 {rate:g}元/km"
    if limit is None:
        return f">{PUBLISHED_FARE_TIERS[1][0]:g}km 档 {rate:g}元/km"
    return f"{PUBLISHED_FARE_TIERS[0][0]:g}~{limit:g}km 档 {rate:g}元/km"


def published_fare_cny(segment_km: float) -> float:
    """民航公布价近似(元)= 航段里程 × 分段费率 :data:`PUBLISHED_FARE_TIERS`。"""
    return _km(segment_km) * published_fare_tier(segment_km)[1]


def flight_discount(from_name: Optional[str] = None, to_name: Optional[str] = None) -> float:
    """典型折扣:主干商务线(双枢纽):data:`FLIGHT_DISCOUNT_TRUNK`,支线 :data:`FLIGHT_DISCOUNT_BRANCH`。"""
    return FLIGHT_DISCOUNT_TRUNK if is_trunk_pair(from_name, to_name) else FLIGHT_DISCOUNT_BRANCH


def flight_fare(straight_km: float, *, from_name: Optional[str] = None,
                to_name: Optional[str] = None) -> dict[str, Any]:
    """机票价区间:``[公布价 × 典型折扣, 公布价]``,``cost_cny`` 取区间中值。

    两种情况**不给价**(``available=False``,三个金额都是 ``None``,只留 deep-link):

    * 直线距离 < :data:`FLIGHT_PRICE_MIN_KM`(400km)—— 短途票价没参考意义;
    * 任一端城市不在 :data:`AIRPORT_CITIES`(或压根没给地名)—— 没机场就飞不了。

    宁可不估、不瞎估:机票是动态定价,区间只是"典型折扣 ~ 公布价"的锚,实时价以跳转为准。
    """
    km = _km(straight_km)
    segment_km = flight_segment_km(km)
    base: dict[str, Any] = {
        "available": False,
        "reason": None,
        "low_cny": None,
        "high_cny": None,
        "mid_cny": None,
        "published_cny": None,
        "discount": None,
        "trunk": False,
        "segment_km": segment_km,
        "tier": None,
    }
    if km < FLIGHT_PRICE_MIN_KM:
        return {**base, "reason": f"直线 {round(km, DISTANCE_PRECISION):g}km < {FLIGHT_PRICE_MIN_KM:g}km"}
    missing = missing_airport_endpoint(from_name, to_name)
    if missing is not None:
        return {**base,
                "reason": f"{missing}没有匹配到民航机场(内置 {len(AIRPORT_CITIES)} 城机场表)"}
    published = published_fare_cny(segment_km)
    discount = flight_discount(from_name, to_name)
    low = round_cost(published * discount)
    high = round_cost(published)
    limit, rate = published_fare_tier(segment_km)
    return {
        **base,
        "available": True,
        "low_cny": low,
        "high_cny": high,
        "mid_cny": round_cost((low + high) / 2.0),
        "published_cny": published,
        "discount": discount,
        "trunk": is_trunk_pair(from_name, to_name),
        "tier": {"limit_km": limit, "rate_cny_per_km": rate},
    }


def flight_cost_cny(straight_km: float, **kwargs: Any) -> Optional[int]:
    """机票 ``cost_cny``(元)= 公布价区间中值;区间给不出时 ``None``(兼容旧字段语义)。"""
    return flight_fare(straight_km, **kwargs)["mid_cny"]


def cost_for(
    mode: str,
    *,
    straight_km: float,
    driving_km: Optional[float] = None,
    toll_distance_km: Optional[float] = None,
    region: Optional[str] = None,
    from_name: Optional[str] = None,
    to_name: Optional[str] = None,
) -> Optional[int]:
    """按方式取整后的费用(元)—— v2 口径:

    * 驾车:缺高德里程 → ``None``(不瞎估);否则 = 过路费 + 油费(各自四舍五入再相加);
    * 铁路:种子价或运营里程 × 分档费率;
    * 飞机:公布价区间中值;区间给不出(距离不足 / 没有机场)→ ``None``。
    """
    if mode == MODE_DRIVING:
        if driving_km is None:
            return None
        return driving_money(driving_km, toll_distance_km=toll_distance_km, region=region)["cost_cny"]
    if mode == MODE_RAIL:
        return round_cost(rail_cost_cny(straight_km, from_name=from_name, to_name=to_name))
    if mode == MODE_FLIGHT:
        return flight_cost_cny(straight_km, from_name=from_name, to_name=to_name)
    raise ValueError(f"未知出行方式:{mode!r}(可选:{'、'.join(MODES)})")


def estimate_duration_min(mode: str, straight_km: float) -> int:
    """铁路/飞机耗时估算(分钟):直线 × 绕行 / 均速 + 地面接驳。口径同 POC ``_est_mode``。"""
    if mode == MODE_RAIL:
        speed, ground = RAIL_SPEED_KMH, RAIL_GROUND_MIN
    elif mode == MODE_FLIGHT:
        speed, ground = FLIGHT_SPEED_KMH, FLIGHT_GROUND_MIN
    else:
        raise ValueError(f"仅铁路/飞机有经验估算耗时:{mode!r}(驾车请用高德真实时长)")
    return int(round(billable_km(mode, straight_km) / speed * 60.0 + ground))


def cost_coefficients() -> dict[str, Any]:
    """当前费用系数与算式(v2;前端标注「估算」时展示,也是日后替换真实数据的对照表)。

    顶层键与 ``disclaimer`` 保持 v1 形状(前端 ``cost_model.disclaimer`` 在用),
    各方式内部换成 v2 的可解释口径:驾车构成明细、铁路分档 + 种子、机票公布价分档。
    """
    return {
        MODE_DRIVING: {
            "fuel_l_per_km": FUEL_L_PER_KM,
            "fuel_l_per_100km": FUEL_L_PER_100KM,
            "fuel_price_cny_per_l": fuel_price_cny_per_l(),
            "fuel_price_env": ENV_FUEL_PRICE,
            "toll_cny_per_km_by_region": dict(TOLL_CNY_PER_KM_BY_REGION),
            "region_labels": dict(REGION_LABELS),
            "toll_distance_field": "amap.toll_distance",
            "heuristic_highway_ratio": HEURISTIC_HIGHWAY_RATIO,
            "toll_modes": [TOLL_MODE_AMAP_TOLL_DISTANCE, TOLL_MODE_HEURISTIC],
            "vehicle_label": VEHICLE_LABEL,
            "seats": DRIVING_SEATS,
            "formula": (
                "油费 = 里程 × 0.08L/km × 油价(env WHERE2GO_FUEL_PRICE_CNY_L,默认 8 元/L);"
                "过路费 = 收费里程 × 区域费率(东部0.45/中部0.40/西部0.35 元/km);"
                "收费里程取高德 toll_distance 真实值(amap_toll_distance;"
                "高德 tolls/cost 个人 key 恒为 0/null,不用),"
                "该字段缺失按 里程 × 0.55 估算(heuristic);人均 = 整车 ÷ 4 人"
            ),
        },
        MODE_RAIL: {
            "operating_detour": RAIL_FARE_DETOUR,
            "rate_cny_per_km": {str(tier): rate for tier, rate in RAIL_RATE_CNY_PER_KM.items()},
            "tier_rule": "两端都是高铁枢纽 → 350km/h 线 0.46 元/km,否则 250km/h 线 0.31 元/km",
            "hub_cities": len(RAIL_HUB_CITIES),
            "min_fare_cny": RAIL_MIN_FARE_CNY,
            "seed_pairs": len(RAIL_SEED_FARES),
            "seed_source": RAIL_SEED_SOURCE_NOTE,
            "price_sources": [PRICE_SOURCE_SEED, PRICE_SOURCE_ESTIMATE],
            "detour": RAIL_DETOUR,
            "formula": (
                "命中热门城市对种子 → 直接用种子二等座价(price_source=seed);"
                "否则 运营里程(直线 × 1.15)× 分档费率,起步价 20 元下限(price_source=estimate);"
                "耗时仍按 直线 × 1.2 / 220km/h + 接驳(POC 口径)"
            ),
        },
        MODE_FLIGHT: {
            "segment_detour": FLIGHT_DETOUR,
            "published_fare_tiers": [[limit, rate] for limit, rate in PUBLISHED_FARE_TIERS],
            "tier_rule": "航段里程 <812km → 1.6 元/km;812~1600km → 0.95;>1600km → 0.8",
            "discount": {"trunk": FLIGHT_DISCOUNT_TRUNK, "branch": FLIGHT_DISCOUNT_BRANCH},
            "price_min_km": FLIGHT_PRICE_MIN_KM,
            "airport_cities": len(AIRPORT_CITIES),
            "range_rule": "区间 = [公布价 × 典型折扣, 公布价];cost_cny 取区间中值",
            "dynamic_note": FLIGHT_DYNAMIC_NOTE,
            "detour": FLIGHT_DETOUR,
            "formula": (
                "公布价 ≈ 航段里程(直线 × 1.1)× 分段费率;"
                "区间下限 = 公布价 × 典型折扣(主干商务线 0.45 / 支线 0.6);"
                "直线 <400km 或任一端城市无民航机场 → 不给票价,只留 OTA deep-link"
            ),
        },
        "disclaimer": ESTIMATE_DISCLAIMER,
    }


def mode_rules() -> dict[str, Any]:
    """方式出现阈值与耗时估算系数。

    **耗时**系数与 POC ``/api/discover`` 同源;阈值在 v2 里改了:飞机由 300km 提到
    :data:`FLIGHT_MIN_KM`(600km),另加 :data:`FLIGHT_PRICE_MIN_KM`(400km)——
    不到 600km 不出飞机条目,600km 以上但没有机场/太短则只出条目不给票价。
    """
    return {
        "rail_min_km": RAIL_MIN_KM,
        "flight_min_km": FLIGHT_MIN_KM,
        "flight_price_min_km": FLIGHT_PRICE_MIN_KM,
        "rail": {"detour": RAIL_DETOUR, "speed_kmh": RAIL_SPEED_KMH, "ground_min": RAIL_GROUND_MIN,
                 "fare_detour": RAIL_FARE_DETOUR},
        "flight": {"detour": FLIGHT_DETOUR, "speed_kmh": FLIGHT_SPEED_KMH,
                   "ground_min": FLIGHT_GROUND_MIN},
    }


def _driving_note(money: Mapping[str, Any]) -> str:
    """驾车说明:通用口径(:data:`DRIVING_NOTE`)+ **本次实算**(区域/费率/高速里程/金额)。"""
    breakdown = money["cost_breakdown"]
    return (
        f"{DRIVING_NOTE} 本次:{REGION_LABELS[money['region']]}费率 "
        f"{money['toll_rate_cny_per_km']:g}元/km × 收费里程 {money['toll_km']:.1f}km"
        f"(cost_breakdown.mode={breakdown['mode']}) → 过路费 {breakdown['toll']} 元"
        f" + 油费 {breakdown['fuel']} 元(油价 {money['fuel_price_cny_per_l']:g}元/L)"
        f" = {VEHICLE_LABEL} {money['cost_cny']} 元、人均 {money['per_person_cny']} 元。"
    )


def _rail_note(straight_km: float, fare: Mapping[str, Any]) -> str:
    """铁路说明:票价来源(种子/分档估算)写在脸上,耗时口径仍与 POC 一致。"""
    km = round(_km(straight_km), DISTANCE_PRECISION)
    operating = round(float(fare["operating_km"]), DISTANCE_PRECISION)
    duration_km = round(billable_km(MODE_RAIL, km), DISTANCE_PRECISION)
    if fare["price_source"] == PRICE_SOURCE_SEED:
        city_a, city_b = fare["seed_pair"] or ("", "")
        price = (
            f"票价命中热门城市对种子:{city_a}—{city_b} 二等座 "
            f"{round_cost(float(fare['total_cny']))} 元 —— {RAIL_SEED_SOURCE_NOTE}"
        )
    else:
        price = (
            f"票价按运营里程 {operating:g}km(直线 {km:g}km × {RAIL_FARE_DETOUR:g})× "
            f"{float(fare['rate_cny_per_km']):g}元/km({fare['tier_kmh']}km/h 线,二等座,"
            f"{RAIL_MIN_FARE_CNY:g}元起步价下限)估算"
        )
    return (
        f"{price};耗时按直线 {km:g}km × 绕行 {RAIL_DETOUR:g}(运行里程 {duration_km:g}km)、"
        f"均速 {RAIL_SPEED_KMH:g}km/h + 候车与两端接驳 {RAIL_GROUND_MIN:g}min 估算,"
        f"无实时班次。{ESTIMATE_DISCLAIMER}"
    )


def _flight_note(straight_km: float, quote: Mapping[str, Any]) -> str:
    """机票说明:给价时写清公布价 → 折扣 → 区间;不给价时写清**为什么不给**。"""
    km = round(_km(straight_km), DISTANCE_PRECISION)
    segment = round(float(quote["segment_km"]), DISTANCE_PRECISION)
    duration = (
        f"耗时按大圆 {km:g}km × 绕行 {FLIGHT_DETOUR:g}(航段里程 {segment:g}km)、"
        f"巡航 {FLIGHT_SPEED_KMH:g}km/h + 值机安检与两端机场接驳 {FLIGHT_GROUND_MIN:g}min 估算,"
        f"无实时航班。"
    )
    if not quote["available"]:
        return (
            f"{quote['reason']},本次不给机票价(宁可不估、不瞎估),"
            f"只用下方 OTA deep-link 查实时价。{duration}{FLIGHT_DYNAMIC_NOTE}。"
            f"{ESTIMATE_DISCLAIMER}"
        )
    trunk_label = "主干商务线" if quote["trunk"] else "支线"
    return (
        f"民航公布价 ≈ 航段里程 {segment:g}km × "
        f"{published_fare_tier_label(segment)} = {round_cost(float(quote['published_cny']))} 元;"
        f"区间 = 公布价 × 典型折扣 {float(quote['discount']):g}({trunk_label})~ 公布价 → "
        f"{quote['low_cny']}~{quote['high_cny']} 元,cost_cny 取区间中值 "
        f"{quote['mid_cny']} 元。{duration}{FLIGHT_DYNAMIC_NOTE}。{ESTIMATE_DISCLAIMER}"
    )


# --------------------------------------------------------------------------- #
# deep-link 跳转链接(纯函数:不触网、不查库,中文按 UTF-8 百分号编码)
# --------------------------------------------------------------------------- #

LINK_SRC = "where2go"
# 12306/OTA 的出发日默认今天(中国标准时间,无夏令时;固定 UTC+8 免得依赖系统 tzdata)
CST = timezone(timedelta(hours=8))

AMAP_NAVIGATION_ENDPOINT = "https://uri.amap.com/navigation"
GOOGLE_DIRECTIONS_ENDPOINT = "https://www.google.com/maps/dir/"
RAIL_12306_ENDPOINT = "https://kyfw.12306.cn/otn/leftTicket/init"
FLIGHT_OTA_ENDPOINT = "https://flight.qunar.com/site/oneway_list.htm"
FLIGHT_OTA_PROVIDER = "qunar"
FLIGHT_OTA_LABEL = "OTA 机票搜索(去哪儿)"

LINK_PROVIDER_AMAP = "amap"
LINK_PROVIDER_GOOGLE = "google"
LINK_PROVIDER_12306 = "12306"
LINK_PROVIDER_OTA = FLIGHT_OTA_PROVIDER

AMAP_NOTE = (
    "高德 URI API 路径规划;坐标为 WGS84(OSM/GPS),高德按 GCJ-02 解析,"
    "国内可能有百米级偏移 —— 带上地名时高德可按名字纠偏"
)
GOOGLE_NOTE = "Google 地图导航;中国大陆不可访问,仅供海外或可访问环境使用"
RAIL_12306_NOTE = (
    "打开 12306 官方查询页(只带站名,由 12306 自行匹配车站);"
    f"余票与票价以官方为准 —— 不抓价、不代订(ADR-004/ADR-007)。{ESTIMATE_DISCLAIMER}"
)
FLIGHT_OTA_NOTE = (
    "打开 OTA 单程机票搜索页;实时票价以官方为准 —— 不抓价、不代订"
    f"(ADR-004/ADR-007)。{ESTIMATE_DISCLAIMER}"
)


def clean_name(name: Optional[str]) -> Optional[str]:
    """地名清洗:去空白;空串 → ``None``(deep-link 里就省略该参数)。"""
    cleaned = (name or "").strip()
    return cleaned or None


def display_name(name: Optional[str], lat: float, lng: float, *, fallback: str) -> str:
    """展示名:有地名用地名,没有就降级成 ``我的位置(31.2304,121.4737)`` 这种坐标名。"""
    cleaned = clean_name(name)
    if cleaned:
        return cleaned
    return (f"{fallback}({float(lat):.{COORD_LABEL_PRECISION}f},"
            f"{float(lng):.{COORD_LABEL_PRECISION}f})")


def default_departure_date(today: Optional[Any] = None) -> str:
    """12306/OTA 链接的默认出发日:今天(UTC+8)的 ``YYYY-MM-DD``。"""
    if today is None:
        return datetime.now(CST).date().isoformat()
    if isinstance(today, datetime):
        return today.date().isoformat()
    return str(today)


def _query(params: Mapping[str, Any]) -> str:
    """拼 query string:跳过 ``None``/空值,值按 UTF-8 百分号编码(逗号保留原样)。"""
    parts: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        parts.append(f"{quote(str(key), safe='')}={quote(text, safe=',')}")
    return "&".join(parts)


def _amap_point(lng: Optional[float], lat: Optional[float], name: Optional[str]) -> Optional[str]:
    """高德的 ``from``/``to`` 值:``lng,lat[,name]``(**经度在前**,官方口径)。"""
    if lng is None or lat is None:
        return None
    point = f"{float(lng):.6f},{float(lat):.6f}"
    cleaned = clean_name(name)
    return f"{point},{cleaned}" if cleaned else point


def amap_navigation_url(
    *,
    to_lng: float,
    to_lat: float,
    to_name: Optional[str] = None,
    from_lng: Optional[float] = None,
    from_lat: Optional[float] = None,
    from_name: Optional[str] = None,
    mode: str = "car",
    policy: int = 1,
    callnative: int = 1,
) -> str:
    """高德导航 deep-link(``https://uri.amap.com/navigation``)。

    官方参数口径(2026-09-11 复核 lbs.amap.com URI API 文档):``from``/``to`` 为
    ``lng,lat[,name]``;``mode`` 取 ``car``/``bus``/``walk``/``ride``;``policy=1`` 避免拥堵;
    ``callnative=1`` 移动端尝试调起 App。**起点坐标缺失时省略 ``from``**,
    高德会自动用用户当前位置(官方支持,移动端生效)。
    """
    params = {
        "from": _amap_point(from_lng, from_lat, from_name),
        "to": _amap_point(to_lng, to_lat, to_name),
        "mode": mode,
        "policy": policy,
        "src": LINK_SRC,
        "callnative": callnative,
    }
    return f"{AMAP_NAVIGATION_ENDPOINT}?{_query(params)}"


def google_maps_directions_url(
    *,
    to_lat: float,
    to_lng: float,
    from_lat: Optional[float] = None,
    from_lng: Optional[float] = None,
    travelmode: str = "driving",
) -> str:
    """Google 地图导航 deep-link(``https://www.google.com/maps/dir/?api=1``)。

    ``origin``/``destination`` 为 ``lat,lng``(**纬度在前**,与高德相反);
    起点缺失时省略 ``origin``,Google 同样会用当前位置。
    """
    origin = None if from_lat is None or from_lng is None else f"{float(from_lat):.6f},{float(from_lng):.6f}"
    params = {
        "api": 1,
        "origin": origin,
        "destination": f"{float(to_lat):.6f},{float(to_lng):.6f}",
        "travelmode": travelmode,
    }
    return f"{GOOGLE_DIRECTIONS_ENDPOINT}?{_query(params)}"


def rail_12306_url(*, to_name: Optional[str], from_name: Optional[str] = None,
                   date: Optional[str] = None) -> str:
    """12306 车票查询 deep-link(``fs`` 出发地 / ``ts`` 到达地 / ``date`` 出发日)。

    官方分享链接形如 ``fs=北京,BJP``(站名 + 电报码);本产品只有地名、没有站码映射,
    只传站名 —— 12306 查询页会按名字匹配车站(2026-09-11 实测 HTTP 200 正常打开)。
    地名缺失时省略对应参数,由用户在官方页面自行补全。
    """
    params = {
        "linktypeid": "dc",
        "fs": clean_name(from_name),
        "ts": clean_name(to_name),
        "date": date or default_departure_date(),
        "flag": "N,N,Y",
    }
    return f"{RAIL_12306_ENDPOINT}?{_query(params)}"


def flight_ota_url(*, to_name: Optional[str], from_name: Optional[str] = None,
                   date: Optional[str] = None) -> str:
    """OTA 机票搜索 deep-link(去哪儿单程列表页,接受**中文城市名**)。

    为什么不是携程:携程的 ``flights.ctrip.com/online/list/oneway-{from}-{to}`` 只认
    **三字码**,传中文城市名会被重定向到机票首页、查询条件丢失(2026-09-11 实测);
    去哪儿的查询页接受中文城市名并原样带进列表页(``depCity``/``arrCity``),
    所以默认用它。日后若接入城市码映射,把这个函数换成携程即可,调用方与系数都不用动。
    """
    params = {
        "searchDepartureAirport": clean_name(from_name),
        "searchArrivalAirport": clean_name(to_name),
        "searchDepartureTime": date or default_departure_date(),
        "nextNDays": 0,
        "startSearch": "true",
        "from": "flight_index",
    }
    return f"{FLIGHT_OTA_ENDPOINT}?{_query(params)}"


def route_links(
    mode: str,
    *,
    from_lat: float,
    from_lng: float,
    to_lat: float,
    to_lng: float,
    to_name: Optional[str] = None,
    from_name: Optional[str] = None,
    date: Optional[str] = None,
) -> list[dict[str, Any]]:
    """某条路线的跳转按钮:驾车 → 高德 + Google 导航;铁路 → 12306;飞机 → OTA 机票搜索。"""
    if mode == MODE_DRIVING:
        return [
            {
                "provider": LINK_PROVIDER_AMAP,
                "label": "高德导航",
                "url": amap_navigation_url(
                    from_lng=from_lng, from_lat=from_lat, from_name=from_name,
                    to_lng=to_lng, to_lat=to_lat, to_name=to_name,
                ),
                "note": AMAP_NOTE,
            },
            {
                "provider": LINK_PROVIDER_GOOGLE,
                "label": "Google 地图导航",
                "url": google_maps_directions_url(
                    from_lat=from_lat, from_lng=from_lng, to_lat=to_lat, to_lng=to_lng,
                ),
                "note": GOOGLE_NOTE,
            },
        ]
    if mode == MODE_RAIL:
        return [{
            "provider": LINK_PROVIDER_12306,
            "label": "12306 查票",
            "url": rail_12306_url(to_name=to_name, from_name=from_name, date=date),
            "note": RAIL_12306_NOTE,
        }]
    if mode == MODE_FLIGHT:
        return [{
            "provider": LINK_PROVIDER_OTA,
            "label": FLIGHT_OTA_LABEL,
            "url": flight_ota_url(to_name=to_name, from_name=from_name, date=date),
            "note": FLIGHT_OTA_NOTE,
        }]
    raise ValueError(f"未知出行方式:{mode!r}(可选:{'、'.join(MODES)})")


# --------------------------------------------------------------------------- #
# geometry 抽稀 + 坐标校验
# --------------------------------------------------------------------------- #


def thin_geometry(points: Optional[Sequence[Sequence[float]]],
                  max_points: Optional[int] = GEOMETRY_MAX_POINTS) -> Optional[list[list[float]]]:
    """等间隔抽稀折线,**首尾点必留**(画线不缩水);空/``None`` → ``None``。

    ``max_points`` 为 ``None`` 或 < 2 时不抽稀(原样返回副本)。
    """
    if not points:
        return None
    coords = [[float(pair[0]), float(pair[1])] for pair in points]
    if max_points is None or max_points < 2 or len(coords) <= max_points:
        return coords
    step = (len(coords) - 1) / (max_points - 1)
    picked = [coords[round(index * step)] for index in range(max_points - 1)]
    picked.append(coords[-1])
    return picked


def require_coordinates(
    *,
    from_lat: Any,
    from_lng: Any,
    to_lat: Any,
    to_lng: Any,
) -> tuple[float, float, float, float]:
    """四个坐标缺一不可、必须是合法经纬度;非法抛 :class:`ValueError`(中文说明)。

    API 层把它映射成 HTTP 400,校验风格与 ``/api/places`` 一致(缺参/非法都是 400)。
    """
    resolved: dict[str, float] = {}
    for name, value, limit in (
        ("from_lat", from_lat, 90.0),
        ("from_lng", from_lng, 180.0),
        ("to_lat", to_lat, 90.0),
        ("to_lng", to_lng, 180.0),
    ):
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"缺少必要参数:{name}(起点/目的地经纬度都要给)")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"参数 {name} 必须为数字,收到:{value!r}") from exc
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError(f"参数 {name} 不是有效数字:{value!r}")
        if not -limit <= number <= limit:
            raise ValueError(f"参数 {name} 超出 [-{limit:g}, {limit:g}] 范围:{value!r}")
        resolved[name] = number
    return resolved["from_lat"], resolved["from_lng"], resolved["to_lat"], resolved["to_lng"]


# --------------------------------------------------------------------------- #
# 单条路线构造
# --------------------------------------------------------------------------- #


def amap_leg(leg: Mapping[str, Any]) -> dict[str, Any]:
    """高德驾车结果(:data:`data_sources.amap.DRIVING_KEYS`)→ 本模块的 leg 口径。

    高德给的是**米/秒**与 ``toll_distance``(收费路段米),这里换成
    ``distance_km`` / ``duration_min`` / ``toll_distance_km``,并把已解码抽稀的
    ``polyline``(``[[lat, lng], …]``)挪到 ``geometry`` 键 —— 下游
    (:func:`driving_route`)只认这一种形状,注入替身也因此不必知道高德。
    """
    distance_m = leg.get("distance_m")
    duration_s = leg.get("duration_s")
    toll_m = leg.get("toll_distance_m")
    return {
        "distance_km": float(distance_m) / 1000.0,
        "duration_min": float(duration_s) / 60.0,
        "toll_distance_km": None if toll_m is None else float(toll_m) / 1000.0,
        "traffic_lights": leg.get("traffic_lights"),
        "steps_n": leg.get("steps_n"),
        "geometry": [list(point) for point in (leg.get("polyline") or [])],
    }


def default_router(start_lnglat: Sequence[float], end_lnglat: Sequence[float]) -> Mapping[str, Any]:
    """默认取**高德**驾车路线(v3 ``direction/driving``,含 polyline 与 ``toll_distance``)。

    入参是 ``(lng, lat)`` 二元组(与既有 :data:`RouterFn` 口径一致),高德要的是
    纬度在前,这里换序。单测注入替身即可不触网;替身返回的 leg 里没有
    ``toll_distance_km`` 键时,费用引擎自动退化成 ``toll_mode="heuristic"``,
    不影响时长/里程/折线。
    """
    start_lng, start_lat = float(start_lnglat[0]), float(start_lnglat[1])
    end_lng, end_lat = float(end_lnglat[0]), float(end_lnglat[1])
    return amap_leg(amap.driving(start_lat, start_lng, end_lat, end_lng))


def driving_route(
    *,
    from_lat: float,
    from_lng: float,
    to_lat: float,
    to_lng: float,
    to_name: Optional[str] = None,
    from_name: Optional[str] = None,
    date: Optional[str] = None,
    router: Optional[RouterFn] = None,
    geometry_max_points: Optional[int] = GEOMETRY_MAX_POINTS,
) -> dict[str, Any]:
    """驾车路线:高德真实时长/里程/折线 + 估算费用(构成明细 + 整车/人均双标)。

    **高德失败只降级、不抛错**:降级时 ``duration_min``/``cost_cny``/``cost_breakdown``/
    ``per_person_cny``/``geometry`` 全是 ``None``,deep-link 照给。
    """
    meta = MODE_META[MODE_DRIVING]
    links = route_links(
        MODE_DRIVING, from_lat=from_lat, from_lng=from_lng, to_lat=to_lat, to_lng=to_lng,
        to_name=to_name, from_name=from_name, date=date,
    )
    try:
        leg = (router or default_router)((from_lng, from_lat), (to_lng, to_lat))
    except (DataSourceError, ValueError):
        leg = None

    if leg is None:  # 高德不可用:降级成"没有数字",不瞎估、也不 500
        duration_min: Optional[int] = None
        cost_cny: Optional[int] = None
        cost_breakdown: Optional[dict[str, Any]] = None
        per_person_cny: Optional[int] = None
        distance_km: Optional[float] = None
        geometry: Optional[list[list[float]]] = None
        kind, degraded, source, note = (
            KIND_ESTIMATE, True, SOURCE_UNAVAILABLE, DRIVING_DEGRADED_NOTE
        )
    else:
        distance_km = round(float(leg["distance_km"]), DISTANCE_PRECISION)
        duration_min = int(round(float(leg["duration_min"])))
        money = driving_money(
            distance_km,
            toll_distance_km=leg.get("toll_distance_km"),
            region=driving_region(
                from_name=from_name, to_name=to_name, mid_lng=(float(from_lng) + float(to_lng)) / 2.0
            ),
        )
        cost_cny = money["cost_cny"]
        cost_breakdown = money["cost_breakdown"]
        per_person_cny = money["per_person_cny"]
        geometry = thin_geometry(leg.get("geometry"), geometry_max_points)
        kind, degraded, source, note = KIND_REAL, False, SOURCE_AMAP, _driving_note(money)

    return {
        "mode": MODE_DRIVING,
        "label": meta["label"],
        "emoji": meta["emoji"],
        "duration_min": duration_min,
        "cost_cny": cost_cny,
        "cost_breakdown": cost_breakdown,
        "vehicle_label": VEHICLE_LABEL,
        "per_person_cny": per_person_cny,
        "distance_km": distance_km,
        "geometry": geometry,
        "kind": kind,
        "degraded": degraded,
        "source": source,
        "note": note,
        "links": links,
    }


def estimated_route(
    mode: str,
    *,
    straight_km: float,
    to_name: Optional[str] = None,
    from_name: Optional[str] = None,
    from_lat: float,
    from_lng: float,
    to_lat: float,
    to_lng: float,
    date: Optional[str] = None,
) -> dict[str, Any]:
    """铁路/飞机估算路线:耗时 + 费用都是经验估算,``kind="estimate"``,不带 geometry。

    v2 字段:铁路多一个 ``price_source``(``seed``/``estimate``);飞机多
    ``flight_low_cny`` / ``flight_high_cny``(公布价锚定区间;给不出票价时全为 ``None``,
    ``cost_cny`` 同样为 ``None``,只留 deep-link)。
    """
    if mode not in (MODE_RAIL, MODE_FLIGHT):
        raise ValueError(f"该方式不走估算:{mode!r}(可选:{MODE_RAIL}、{MODE_FLIGHT})")
    meta = MODE_META[mode]
    if mode == MODE_RAIL:
        fare = rail_fare(straight_km, from_name=from_name, to_name=to_name)
        priced: dict[str, Any] = {
            "cost_cny": round_cost(float(fare["total_cny"])),
            "price_source": fare["price_source"],
        }
        note = _rail_note(straight_km, fare)
    else:
        quote = flight_fare(straight_km, from_name=from_name, to_name=to_name)
        priced = {
            "cost_cny": quote["mid_cny"],
            "flight_low_cny": quote["low_cny"],
            "flight_high_cny": quote["high_cny"],
        }
        note = _flight_note(straight_km, quote)
    return {
        "mode": mode,
        "label": meta["label"],
        "emoji": meta["emoji"],
        "duration_min": estimate_duration_min(mode, straight_km),
        "distance_km": round(_km(straight_km), DISTANCE_PRECISION),
        "geometry": None,
        "kind": KIND_ESTIMATE,
        "degraded": False,
        "source": SOURCE_ESTIMATE,
        "note": note,
        "links": route_links(
            mode, from_lat=from_lat, from_lng=from_lng, to_lat=to_lat, to_lng=to_lng,
            to_name=to_name, from_name=from_name, date=date,
        ),
        **priced,
    }


# --------------------------------------------------------------------------- #
# 编排入口
# --------------------------------------------------------------------------- #


@dataclass
class RoutePlan:
    """一次路线规划的结果:起终点 + 直线距离 + 各方式路线 + 生成时间。"""

    origin: dict[str, Any]
    destination: dict[str, Any]
    distance_km: float
    routes: list[dict[str, Any]]
    generated_at: str


def plan_routes(
    *,
    from_lat: Any,
    from_lng: Any,
    to_lat: Any,
    to_lng: Any,
    to_name: Optional[str] = None,
    from_name: Optional[str] = None,
    router: Optional[RouterFn] = None,
    date: Optional[str] = None,
    now: Optional[datetime] = None,
    geometry_max_points: Optional[int] = GEOMETRY_MAX_POINTS,
) -> RoutePlan:
    """编排三种方式:驾车始终出现(高德),铁路 ≥100km、飞机 ≥600km 才出现(估算)。

    坐标非法抛 :class:`ValueError`(API 层转 400);高德失败**不抛**,驾车条目降级。
    铁路票价优先用 :data:`RAIL_SEED_FARES` 种子价;机票按民航公布价锚定区间,
    直线 <400km 或任一端没有民航机场时不给票价(条目仍带 deep-link)。
    """
    lat1, lng1, lat2, lng2 = require_coordinates(
        from_lat=from_lat, from_lng=from_lng, to_lat=to_lat, to_lng=to_lng
    )
    origin_name = clean_name(from_name)
    destination_name = clean_name(to_name)
    straight_km = haversine_km(lat1, lng1, lat2, lng2)

    routes: list[dict[str, Any]] = [
        driving_route(
            from_lat=lat1, from_lng=lng1, to_lat=lat2, to_lng=lng2,
            to_name=destination_name, from_name=origin_name, date=date,
            router=router, geometry_max_points=geometry_max_points,
        )
    ]
    if straight_km >= RAIL_MIN_KM:
        routes.append(estimated_route(
            MODE_RAIL, straight_km=straight_km, to_name=destination_name, from_name=origin_name,
            from_lat=lat1, from_lng=lng1, to_lat=lat2, to_lng=lng2, date=date,
        ))
    if straight_km >= FLIGHT_MIN_KM:
        routes.append(estimated_route(
            MODE_FLIGHT, straight_km=straight_km, to_name=destination_name, from_name=origin_name,
            from_lat=lat1, from_lng=lng1, to_lat=lat2, to_lng=lng2, date=date,
        ))

    return RoutePlan(
        origin={
            "lat": lat1,
            "lng": lng1,
            "name": display_name(origin_name, lat1, lng1, fallback=ORIGIN_FALLBACK_LABEL),
        },
        destination={
            "lat": lat2,
            "lng": lng2,
            "name": display_name(destination_name, lat2, lng2, fallback=DESTINATION_FALLBACK_LABEL),
        },
        distance_km=round(straight_km, DISTANCE_PRECISION),
        routes=routes,
        generated_at=iso_utc(now or utcnow()) or "",
    )
