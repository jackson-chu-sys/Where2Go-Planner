"""路线编排 + 费用估算 + deep-link 跳转链接(TASK-2a,阶段2 M2 的后端)。

一个入口 :func:`plan_routes`:给起点/目的地坐标,返回**驾车 / 铁路 / 飞机**三种方式的
时间 + 费用对比,每条路线形状统一(docs/STAGE2-PLAN.md 第 2/4 节)::

    {"mode": "driving|rail|flight", "label": "驾车", "duration_min": 94,
     "cost_cny": 884, "distance_km": 122.4, "geometry": [[lat, lng], ...] | None,
     "kind": "real|estimate", "degraded": False, "source": "OSRM",
     "note": "...", "links": [{"provider", "label", "url", "note"}, ...]}

口径与**诚实标注**(ADR-007:公共交通/OTA 不抓实时数据,只做估算 + deep-link):

* **驾车**:走现有 OSRM 数据源(:func:`data_sources.route` 带 ``with_geometry=True``),
  时长/里程/折线都是真实路网 → ``kind="real"``;**费用仍是估算**(油耗 + 高速过路费系数),
  note 里写明。OSRM 失败(公共实例繁忙、两点不连通)时**不抛错**:该条降级成
  ``kind="estimate"`` + ``degraded=True``、时长/费用/geometry 为 ``None``,
  note 说明原因,deep-link 照给(跳转导航不依赖 OSRM)。
* **铁路 / 飞机**:耗时沿用 POC ``app/api/discover.py`` 的 ``_est_mode`` 口径
  (直线距离 × 绕行系数 / 均速 + 地面接驳),费用按里程 × 单价 → 全是估算,
  ``kind="estimate"``,note 一律带「估算·非实时·以官方为准」。
* **出现阈值**(同样沿用 POC):直线距离 ≥ :data:`RAIL_MIN_KM` 才出铁路、
  ≥ :data:`FLIGHT_MIN_KM` 才出飞机;驾车始终出现。

所有费用系数集中在下面「费用估算系数」一段常量里,日后换成真实票价/实时路况只改那里,
:func:`cost_coefficients` 会把当前系数原样吐给前端做标注。

deep-link(:func:`amap_navigation_url` / :func:`google_maps_directions_url` /
:func:`rail_12306_url` / :func:`flight_ota_url`)都是**纯函数**:不触网、不查库、
中文地名按 UTF-8 百分号编码,给定同样的入参永远得到同样的 URL,可直接单测。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

from data_sources import DataSourceError
from data_sources import haversine_km
from data_sources import route as ds_route
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
FLIGHT_MIN_KM = 300.0   # 直线距离 >= 才出现飞机
RAIL_DETOUR = 1.20      # 铁路路径绕行系数(相对直线)
RAIL_SPEED_KMH = 220.0  # 城际均速(含中间停靠)
RAIL_GROUND_MIN = 110.0  # 候车 + 起/终点站与景点接驳
FLIGHT_DETOUR = 1.10
FLIGHT_SPEED_KMH = 780.0
FLIGHT_GROUND_MIN = 210.0  # 值机安检 + 两端机场接驳

# --------------------------------------------------------------------------- #
# 费用估算系数(docs/STAGE2-PLAN.md 第 2 节)—— 全部集中在此,换真实数据只改这一段
# --------------------------------------------------------------------------- #

FUEL_L_PER_100KM = 8.0      # 百公里油耗(L)
FUEL_PRICE_CNY_PER_L = 7.5  # 油价(元/L)
HIGHWAY_RATIO = 0.7         # 高速里程占比(默认 0.7)
TOLL_CNY_PER_KM = 0.5       # 高速通行费(元/km)
RAIL_CNY_PER_KM = 0.45      # 铁路二等座(元/km)
RAIL_MIN_FARE_CNY = 20.0    # 铁路起步价下限(元)
FLIGHT_CNY_PER_KM = 0.6     # 经济舱(元/km)
FLIGHT_BASE_CNY = 100.0     # 机建 + 燃油(元/程)

COST_PRECISION = 0       # 费用取整到元
DISTANCE_PRECISION = 1   # 里程保留 1 位小数
COORD_LABEL_PRECISION = 4  # 没有地名时,用坐标当展示名的精度
# geometry 抽稀上限:OSRM overview=full 长路线动辄上万点,服务端先抽到画线够用的量级,
# 前端要更平滑可自行再插值(见 STAGE2-PLAN 第 6 节风险 3)
GEOMETRY_MAX_POINTS = 1200

# 计费里程:铁路按线路里程(≈ 直线 × 绕行),机票按航段里程(≈ 大圆 × 绕行);
# 驾车直接用 OSRM 的真实里程,不用系数。
BILLABLE_DETOUR = {MODE_RAIL: RAIL_DETOUR, MODE_FLIGHT: FLIGHT_DETOUR}

KIND_REAL = "real"
KIND_ESTIMATE = "estimate"
SOURCE_OSRM = "OSRM"
SOURCE_ESTIMATE = "estimate"
SOURCE_UNAVAILABLE = "unavailable"
ESTIMATE_DISCLAIMER = "估算·非实时·以官方为准"

DRIVING_NOTE = (
    f"时长/里程为 OSRM 真实路网(非实时路况,不含拥堵与休息);费用为估算:"
    f"油费 {FUEL_L_PER_100KM:g}L/100km × {FUEL_PRICE_CNY_PER_L:g}元/L"
    f" + 高速占比 {HIGHWAY_RATIO:g} × 里程 × {TOLL_CNY_PER_KM:g}元/km。{ESTIMATE_DISCLAIMER}"
)
DRIVING_DEGRADED_NOTE = (
    "OSRM 驾车路线暂不可用(公共实例繁忙,或两点不在同一路网/坐标离路网太远):"
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
    """费用取整到元(:data:`COST_PRECISION`)。"""
    return int(round(float(value), COST_PRECISION))


def driving_cost_cny(distance_km: float) -> float:
    """驾车费用(元)= 油费 + 高速过路费;里程用 OSRM 的真实公里数。"""
    km = _km(distance_km)
    fuel = km * FUEL_L_PER_100KM / 100.0 * FUEL_PRICE_CNY_PER_L
    toll = HIGHWAY_RATIO * km * TOLL_CNY_PER_KM
    return fuel + toll


def billable_km(mode: str, straight_km: float) -> float:
    """铁路/飞机的计费里程 = 直线距离 × 绕行系数(与耗时估算同源)。"""
    if mode not in BILLABLE_DETOUR:
        raise ValueError(f"该方式不走绕行系数计费:{mode!r}(可选:{'、'.join(BILLABLE_DETOUR)})")
    return _km(straight_km) * BILLABLE_DETOUR[mode]


def rail_cost_cny(straight_km: float) -> float:
    """铁路二等座票价(元)= 计费里程 × 单价,不低于起步价 :data:`RAIL_MIN_FARE_CNY`。"""
    return max(RAIL_MIN_FARE_CNY, billable_km(MODE_RAIL, straight_km) * RAIL_CNY_PER_KM)


def flight_cost_cny(straight_km: float) -> float:
    """经济舱票价(元)= 计费里程 × 单价 + 机建燃油 :data:`FLIGHT_BASE_CNY`。"""
    return billable_km(MODE_FLIGHT, straight_km) * FLIGHT_CNY_PER_KM + FLIGHT_BASE_CNY


def cost_for(mode: str, *, straight_km: float, driving_km: Optional[float] = None) -> Optional[int]:
    """按方式取整后的估算费用(元);驾车缺 OSRM 里程时返回 ``None``(不瞎估)。"""
    if mode == MODE_DRIVING:
        return None if driving_km is None else round_cost(driving_cost_cny(driving_km))
    if mode == MODE_RAIL:
        return round_cost(rail_cost_cny(straight_km))
    if mode == MODE_FLIGHT:
        return round_cost(flight_cost_cny(straight_km))
    raise ValueError(f"未知出行方式:{mode!r}(可选:{'、'.join(MODES)})")


def estimate_duration_min(mode: str, straight_km: float) -> int:
    """铁路/飞机耗时估算(分钟):直线 × 绕行 / 均速 + 地面接驳。口径同 POC ``_est_mode``。"""
    if mode == MODE_RAIL:
        speed, ground = RAIL_SPEED_KMH, RAIL_GROUND_MIN
    elif mode == MODE_FLIGHT:
        speed, ground = FLIGHT_SPEED_KMH, FLIGHT_GROUND_MIN
    else:
        raise ValueError(f"仅铁路/飞机有经验估算耗时:{mode!r}(驾车请用 OSRM 真实时长)")
    return int(round(billable_km(mode, straight_km) / speed * 60.0 + ground))


def cost_coefficients() -> dict[str, Any]:
    """当前费用系数与算式(前端标注「估算」时展示,也是日后替换真实数据的对照表)。"""
    return {
        MODE_DRIVING: {
            "fuel_l_per_100km": FUEL_L_PER_100KM,
            "fuel_price_cny_per_l": FUEL_PRICE_CNY_PER_L,
            "highway_ratio": HIGHWAY_RATIO,
            "toll_cny_per_km": TOLL_CNY_PER_KM,
            "formula": "里程 × 8L/100km × 7.5元/L + 0.7 × 里程 × 0.5元/km(里程取 OSRM 真实值)",
        },
        MODE_RAIL: {
            "cny_per_km": RAIL_CNY_PER_KM,
            "min_fare_cny": RAIL_MIN_FARE_CNY,
            "detour": RAIL_DETOUR,
            "formula": "计费里程(直线 × 1.2)× 0.45元/km,起步价 20 元下限(二等座)",
        },
        MODE_FLIGHT: {
            "cny_per_km": FLIGHT_CNY_PER_KM,
            "base_cny": FLIGHT_BASE_CNY,
            "detour": FLIGHT_DETOUR,
            "formula": "计费里程(直线 × 1.1)× 0.6元/km + 100元(机建 + 燃油,经济舱)",
        },
        "disclaimer": ESTIMATE_DISCLAIMER,
    }


def mode_rules() -> dict[str, Any]:
    """方式出现阈值与耗时估算系数(与 POC ``/api/discover`` 的 ``mode_rules`` 同源口径)。"""
    return {
        "rail_min_km": RAIL_MIN_KM,
        "flight_min_km": FLIGHT_MIN_KM,
        "rail": {"detour": RAIL_DETOUR, "speed_kmh": RAIL_SPEED_KMH, "ground_min": RAIL_GROUND_MIN},
        "flight": {"detour": FLIGHT_DETOUR, "speed_kmh": FLIGHT_SPEED_KMH,
                   "ground_min": FLIGHT_GROUND_MIN},
    }


def _note(mode: str, straight_km: float) -> str:
    """铁路/飞机的一行说明:把估算过程写在脸上(距离、绕行、均速、接驳、票价系数)。"""
    km = round(_km(straight_km), DISTANCE_PRECISION)
    billable = round(billable_km(mode, km), DISTANCE_PRECISION)
    if mode == MODE_RAIL:
        return (
            f"按直线 {km:g}km × 绕行 {RAIL_DETOUR:g}(计费/运行里程 {billable:g}km)、"
            f"均速 {RAIL_SPEED_KMH:g}km/h + 候车与两端接驳 {RAIL_GROUND_MIN:g}min 估算;"
            f"票价按 {RAIL_CNY_PER_KM:g}元/km(二等座,{RAIL_MIN_FARE_CNY:g}元起步价下限),"
            f"无实时班次。{ESTIMATE_DISCLAIMER}"
        )
    return (
        f"按大圆 {km:g}km × 绕行 {FLIGHT_DETOUR:g}(航段里程 {billable:g}km)、"
        f"巡航 {FLIGHT_SPEED_KMH:g}km/h + 值机安检与两端机场接驳 {FLIGHT_GROUND_MIN:g}min 估算;"
        f"票价按 {FLIGHT_CNY_PER_KM:g}元/km + {FLIGHT_BASE_CNY:g}元机建燃油,"
        f"无实时航班。{ESTIMATE_DISCLAIMER}"
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


def default_router(start_lnglat: Sequence[float], end_lnglat: Sequence[float]) -> Mapping[str, Any]:
    """默认取 OSRM 驾车路线(``with_geometry=True``,画线用);单测注入替身即可不触网。"""
    return ds_route(start_lnglat, end_lnglat, with_geometry=True)


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
    """驾车路线:OSRM 真实时长/里程/折线 + 估算费用;**OSRM 失败只降级、不抛错**。"""
    meta = MODE_META[MODE_DRIVING]
    links = route_links(
        MODE_DRIVING, from_lat=from_lat, from_lng=from_lng, to_lat=to_lat, to_lng=to_lng,
        to_name=to_name, from_name=from_name, date=date,
    )
    try:
        leg = (router or default_router)((from_lng, from_lat), (to_lng, to_lat))
    except (DataSourceError, ValueError):
        leg = None

    if leg is None:  # OSRM 不可用:降级成"没有数字",不瞎估、也不 500
        duration_min: Optional[int] = None
        cost_cny: Optional[int] = None
        distance_km: Optional[float] = None
        geometry: Optional[list[list[float]]] = None
        kind, degraded, source, note = (
            KIND_ESTIMATE, True, SOURCE_UNAVAILABLE, DRIVING_DEGRADED_NOTE
        )
    else:
        distance_km = round(float(leg["distance_km"]), DISTANCE_PRECISION)
        duration_min = int(round(float(leg["duration_min"])))
        cost_cny = round_cost(driving_cost_cny(distance_km))
        geometry = thin_geometry(leg.get("geometry"), geometry_max_points)
        kind, degraded, source, note = KIND_REAL, False, SOURCE_OSRM, DRIVING_NOTE

    return {
        "mode": MODE_DRIVING,
        "label": meta["label"],
        "emoji": meta["emoji"],
        "duration_min": duration_min,
        "cost_cny": cost_cny,
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
    """铁路/飞机估算路线:耗时 + 费用都是经验估算,``kind="estimate"``,不带 geometry。"""
    if mode not in (MODE_RAIL, MODE_FLIGHT):
        raise ValueError(f"该方式不走估算:{mode!r}(可选:{MODE_RAIL}、{MODE_FLIGHT})")
    meta = MODE_META[mode]
    cost = cost_for(mode, straight_km=straight_km)
    return {
        "mode": mode,
        "label": meta["label"],
        "emoji": meta["emoji"],
        "duration_min": estimate_duration_min(mode, straight_km),
        "cost_cny": cost,
        "distance_km": round(_km(straight_km), DISTANCE_PRECISION),
        "geometry": None,
        "kind": KIND_ESTIMATE,
        "degraded": False,
        "source": SOURCE_ESTIMATE,
        "note": _note(mode, straight_km),
        "links": route_links(
            mode, from_lat=from_lat, from_lng=from_lng, to_lat=to_lat, to_lng=to_lng,
            to_name=to_name, from_name=from_name, date=date,
        ),
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
    """编排三种方式:驾车始终出现(OSRM),铁路 ≥100km、飞机 ≥300km 才出现(估算)。

    坐标非法抛 :class:`ValueError`(API 层转 400);OSRM 失败**不抛**,驾车条目降级。
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
