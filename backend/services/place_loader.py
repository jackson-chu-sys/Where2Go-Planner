"""(城市, band) 目的地抓取入库编排:**命中库只读库,未命中才触网**。

流程(docs/STAGE1-PLAN.md 第 3/4 节"四分类检索 + 目的地库 cold-start"):

1. 查 ``SegmentFetch`` 水位 —— 有记录说明该 (城市, band) 已入库 →
   直接 :func:`db.repository.list_places` 读库返回,**不发任何网络请求**
   (读库路径也不会调 LLM,保证"二次查询秒出");
2. 没记录 → 解析起点(高德主 + Photon/Nominatim 降级)→ 按分段
   **分组打高德 v3 检索**(:data:`SEARCH_GROUPS` = 高德 typecode/keywords 组,每组独立配额,
   见 :mod:`services.amap_categories`):分段下限 = 0 走 ``place/around`` 单圆;下限 > 0 走
   「外半径包围盒 :func:`data_sources.amap.grid_polygons` 分格 + 逐格 ``place/polygon``」,
   再本地 haversine 收敛到环内(高德**单查询只有 200 条**、``radius`` 被截断在 50km)
   → 按高德 POI id ``("amap", id)`` **去重** + 优先级**归类**
   (滑雪 > 运动 > 人文美食 > 自然,一地只入一类)→ upsert 入库 → 记水位;
   入库身份:``osm_type="amap"``、``osm_id = crc32(POI id)``(:func:`db.models.amap_osm_id`),
   原始 id 存 ``tags["amap_id"]`` —— **表结构零改动**,唯一键幂等语义原样保留;
3. 入库**之后**再补 LLM 一句话简介(:func:`services.intro.fill_missing_intros`):
   DB 即缓存,已有 ``intro`` 的 POI 不再调用;失败降级成空简介,**不阻塞入库**;
4. ``refresh=True`` 可强制重抓(仍按唯一键 upsert,不会产生重复行,也不覆盖已有简介);
5. **种子数据垫底**(TASK-1c):OSM 国内滑雪/运动覆盖差,抓取路径与读库路径都会合并
   :mod:`services.seed_data` 的人工种子 —— 去重键 = 名字 + 坐标,OSM 已经抓到就不重复补;
   读库路径的补种是**幂等且纯本地**的,存量库不重抓也能拿到种子。整体开关是环境变量
   ``WHERE2GO_SEEDS``(默认开,单测在 ``backend/conftest.py`` 里默认关)。
6. **渐进抓取**(TASK-6b,BUG-1 主修复):``target_total`` 把"一次抓满 540 配额"缩成
   "首查 30、显示 15、加载更多每次 +30"。轮数记在 ``SegmentFetch.fetch_rounds``,
   下一轮目标总量 = ``30 × (轮数 + 1)``(:func:`progressive_target_total`);
   分段下拉、四分类归类与读库口径全都不变,变的只是一轮抓多少。

起点除了城市名,还能来自浏览器定位:前端拿到 GPS 坐标后调 ``GET /api/geocode/reverse``,
由 :func:`resolve_reverse_origin` 用**逆**地理编码反查城市;反查失败不报错,
降级成"我的位置(纬度,经度)"这样的坐标起点,地图照样能用。

地理编码是**三腿降级链**(TASK-9c 起):**高德**作主路径(与 POI/瓦片同一坐标系 GCJ-02,
国内直连 0.1~0.2s);Photon 公共实例直连可用且快(实测 1.1s),作第一降级(高德没配 key /
超配额时顶上);Nominatim 在没有代理的产品环境直连不通(实测 15s 超时),只作末腿兜底。
两条链见 :func:`geocode_with_fallback` 与 :func:`reverse_with_fallback`,谁答的会以
``geocoder``(``amap`` / ``photon`` / ``nominatim``)字段一路报到 API 响应,方便排查
"这次是哪个源在兜底";:class:`db.models.OriginCache` 会把结果缓存 7 天省配额。

分类过滤只作用在**读取**阶段:一次抓取入库的数据覆盖全部分类,所以换分类查询
同样命中库、不触网。网络调用全部可注入(``fetcher`` / ``geocoder`` / ``reverse_geocoder``),
单测用替身即可。
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import Session

from data_sources import DataSourceError
from data_sources import amap
from data_sources import geocode as ds_geocode
from data_sources import haversine_km
from data_sources import reverse as ds_reverse
from data_sources.amap import SOURCE_NAME as AMAP_DS_SOURCE
from data_sources.amap import geocode as ds_amap_geocode
from data_sources.amap import reverse_geocode as ds_amap_reverse
from data_sources.nominatim import SOURCE_NAME as NOMINATIM_SOURCE
from data_sources.photon import SOURCE_NAME as PHOTON_SOURCE
from data_sources.photon import geocode as ds_photon_geocode
from data_sources.photon import reverse as ds_photon_reverse
from db import init_db, make_engine, open_session
from db import repository as repo
from db.models import (
    AMAP_OSM_TYPE,
    AMAP_SOURCE,
    FALLBACK_OSM_TYPE,
    OSM_ELEMENT_TYPES,
    UNCATEGORIZED,
    SegmentFetch,
    amap_osm_id,
)
from services import amap_categories
from services import classify
from services import intro as intro_service
from services import seed_data
from services.bands import (
    band_inner_radius_m,
    band_keys,
    band_radius_m,
    filter_to_band,
    in_band,
    require_band,
)
from services.classify import classify_amap_places

# 四分类检索线索(TASK-9b 起走**高德** typecode/keywords 组,docs/TASK-9-CONTRACT.md §3):
# 分组各带配额,避免餐饮这类高频大类把总量刷爆、滑雪场/古镇一条不剩;
# 同一实体被多组命中时按高德 POI id 去重(:func:`services.classify.classify_amap_places`)后只归一类。
SEARCH_GROUPS: list[dict[str, Any]] = classify.search_groups()
# ``/api/places/meta`` 的 ``search_tags`` 键仍要有值(**响应键名零变化**):
# 这里给的是遗留 OSM 选择器并集,只作口径展示,检索链路已不使用。
SEARCH_TAGS: list[classify.TagSelector] = classify.search_tags()
ELEMENT_TYPES = classify.DEFAULT_ELEMENT_TYPES
# 高德**单查询**取数上限(实测 page≥9 恒空,8×25=200 条;``radius`` 也被截断在 50km)
FETCH_LIMIT = amap.MAX_ROWS_PER_QUERY
# 渐进抓取(TASK-6b,BUG-1 主修复):冷启动一次抓满 :data:`SEARCH_GROUPS` 的合计配额
# (540)会让首屏等上几分钟,所以首查只抓一小轮 —— 把各组配额**按比例**缩到目标总量
# (首轮 30),之后每"加载更多"越界一次再抓一轮(30 × 轮数)。缩放后每组至少留
# :data:`MIN_GROUP_BUDGET` 条,免得小分组(古镇 40)被缩成 0、整类彻底消失。
FULL_SEARCH_BUDGET: int = classify.search_budget()
MIN_GROUP_BUDGET = 2
PROGRESSIVE_STEP = 30
# --- 分格抓取(TASK-9b):band 下限 > 0 时高德没有「环形差集」,只能包围盒切格逐格捞 --- #
# 每格目标边长(km):格子小到「单查询 200 条」装得下,又不至于格数爆炸。
GRID_CELL_KM = 25.0
GRID_MIN_SIDE = 2
GRID_MAX_SIDE = 12
# 本轮最多抓几格 = GRID_CELLS_PER_ROUND × (fetch_rounds + 1):与 :func:`progressive_target_total`
# 同口径的**递增扩格** —— 配额没抓满且还有未抓格子时,下一轮「加载更多」自动多抓几格。
GRID_CELLS_PER_ROUND = 4
# 环带收敛会把格子里「太近/太远」的行丢掉,所以分格路径比配额多翻一页兜住损耗。
RING_PAGE_SLACK = 1
# 经纬度换算(与 :mod:`services.stays` 同口径):1 度纬度 ≈ 111.32 km;高纬度时
# cos(lat) 太小会让经度包围盒炸开,夹一个下限保证格子数仍收敛。
METERS_PER_DEGREE = 111_320.0
MIN_COS_LAT = 0.05
# 交互式抓取时一次最多补多少条简介(全量回填走 ``python -m services.intro``)
INTRO_BATCH_LIMIT = 40
# ``SegmentFetch.source`` / API 的 ``source`` 字段:TASK-9b 起抓取来源是 "amap"(§6.7)
SOURCE_AMAP = "amap"
SOURCE_DB = "db"
SOURCE_SEED = "seed"
FINGERPRINT_HEX_LEN = 12
# 逆地理编码的 zoom:10 ≈ 区县级,反查出来的 display_name 里稳定带城市名
REVERSE_ZOOM = 10
# 反查不出城市名时的坐标起点命名(保留 2 位小数,免得 GPS 抖动造出一堆新"城市")
UNNAMED_COORD_PRECISION = 2
# 正向检索时向 Photon 要几条候选(只用最相关的第一条,其余留作日志排查)
PHOTON_LIMIT = 5
# ``geocoder`` 字段的取值:谁答的就是谁;给了坐标/反查失败时没有地理编码源参与 = "none"
GEOCODER_AMAP = "amap"
GEOCODER_PHOTON = "photon"
GEOCODER_NOMINATIM = "nominatim"
GEOCODER_NONE = "none"
# 全链失败时报错的数据源名(三个源都写进消息,便于运维判断是全站断网还是单源挂了)
GEOCODER_CHAIN_SOURCE = f"{AMAP_DS_SOURCE}/{PHOTON_SOURCE}/{NOMINATIM_SOURCE}"
# 高德地理编码结果拼 ``display_name`` 用的行政区字段(**由细到粗**,与 Photon/Nominatim
# 的 "区, 市, 省" 顺序对齐):高德的 ``formatted_address`` 是不带逗号的一整串,
# :func:`city_from_display_name` 挑不出城市名,所以逆地理必须用这几个字段自己拼。
AMAP_DISPLAY_KEYS: tuple[str, ...] = ("township", "district", "city", "province")
# display_name 里判定城市的后缀(Nominatim 中文结果形如 "浦东新区, 上海市, 中国")
CITY_SUFFIXES = ("市", "州", "地区", "盟")
COUNTRY_TOKENS = frozenset({"中国", "中华人民共和国", "china"})

FetchFn = Callable[[float, float, Mapping[str, Any]], list[dict[str, Any]]]
GeocodeFn = Callable[[str], Mapping[str, Any]]
ReverseGeocodeFn = Callable[[float, float], Mapping[str, Any]]


@dataclass
class SegmentOutcome:
    """一次 (城市, band) 查询的结果:数据 + 来源(读库还是刚抓的)。"""

    origin: dict[str, Any]
    band: dict[str, Any]
    places: list[dict[str, Any]]
    source: str = SOURCE_DB
    network_used: bool = False
    fetched_at: Optional[str] = None
    written: int = 0
    seeded: int = 0
    counts_by_category: dict[str, int] = field(default_factory=dict)
    counts_by_source: dict[str, int] = field(default_factory=dict)
    segment: Optional[dict[str, Any]] = None
    intro_stats: Optional[dict[str, Any]] = None


def _failure_text(exc: BaseException) -> str:
    """把降级链里的失败原因压成单行,双失败时一次报清两个源。

    :class:`DataSourceError` 的 ``str()`` 自带 ``[源名]`` 前缀,而外层消息已经写了源名,
    所以优先取 ``message``,免得报成 "Photon=[Photon] 网络连接失败" 这种重复。
    """
    text = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return " ".join(str(text).split())


def amap_display_name(row: Mapping[str, Any], *, reverse: bool = False) -> str:
    """高德地理编码结果 → 与 Photon/Nominatim 同形状的 ``display_name``。

    * 正向(``reverse=False``)优先用 ``formatted_address``(信息最全:"浙江省杭州市西湖区
      西湖风景名胜区"),行政区字段只作兜底;
    * 逆向(``reverse=True``)按 :data:`AMAP_DISPLAY_KEYS` **由细到粗**拼成逗号分隔串
      ("灵隐街道, 西湖区, 杭州市, 浙江省")——:func:`city_from_display_name` 要靠逗号切分
      才能挑出"杭州市",直接用不带逗号的 ``formatted_address`` 会把整条地址当成城市名。

    空段跳过(直辖市的 ``city`` 高德返回空)、重复段只留一次(省 == 市 的直辖市口径)。
    """
    parts: list[str] = []
    for key in AMAP_DISPLAY_KEYS:
        token = str(row.get(key) or "").strip()
        if token and token not in parts:
            parts.append(token)
    formatted = str(row.get("formatted_address") or "").strip()
    if not parts:
        return formatted
    return ", ".join(parts) if reverse else (formatted or ", ".join(parts))


def amap_geocode(city: str) -> dict[str, Any]:
    """高德正向地理编码:取最相关的一条,形状对齐 Photon/Nominatim 的 ``{lat,lng,display_name}``。

    **空结果也当失败**(抛 :class:`DataSourceError`),好让降级链接管;未配
    ``WHERE2GO_AMAP_KEY`` 时 :func:`data_sources.amap.geocode` 自己就抛
    :class:`DataSourceError`,同样落到 Photon。坐标是 GCJ-02 —— 与高德 POI/瓦片自洽
    (§1.6,全链不做坐标转换)。
    """
    rows = ds_amap_geocode(city)
    if not rows:
        raise DataSourceError(AMAP_DS_SOURCE, f"未找到与 {city!r} 匹配的地名(结果为空)")
    first = dict(rows[0])
    return {
        "lat": float(first["lat"]),
        "lng": float(first["lng"]),
        "display_name": amap_display_name(first),
    }


def amap_reverse(lat: float, lng: float) -> dict[str, Any]:
    """高德逆地理编码 → ``{lat, lng, display_name}``;无结果(``{}``)同样当失败降级。"""
    row = ds_amap_reverse(float(lat), float(lng))
    if not row:
        raise DataSourceError(
            AMAP_DS_SOURCE, f"未反查到 ({float(lat):.6f},{float(lng):.6f}) 的地名(结果为空)"
        )
    parsed = dict(row)
    return {
        "lat": float(parsed.get("lat") if parsed.get("lat") is not None else lat),
        "lng": float(parsed.get("lng") if parsed.get("lng") is not None else lng),
        "display_name": amap_display_name(parsed, reverse=True),
    }


def photon_geocode(city: str) -> dict[str, Any]:
    """Photon 正向地理编码:取最相关的一条;**空结果也当失败**,好让降级链接管。"""
    places = ds_photon_geocode(city, limit=PHOTON_LIMIT)
    if not places:
        raise DataSourceError(PHOTON_SOURCE, f"未找到与 {city!r} 匹配的地名(结果为空)")
    return dict(places[0])


def geocode_with_fallback(city: str) -> tuple[dict[str, Any], str]:
    """正向地理编码降级链:**高德主 → Photon → Nominatim**(TASK-9c)。

    返回 ``({"lat", "lng", "display_name"}, "amap"|"photon"|"nominatim")``。任一腿抛
    :class:`DataSourceError`(网络/格式/未配 key)或**结果为空**都算失败,原样降到下一腿;
    三腿全失败时抛 :class:`DataSourceError`,消息里同时给出三边的中文原因
    (API 层转成 HTTP 400)。

    前两腿刻意用宽 ``except Exception``:它们只是"尽力而为的主路径",任何异常
    (包括单测 ``no_network`` 兜底抛的 ``AssertionError``)都只该让它让位给下一腿,
    而不是把整条起点解析打挂 —— 真正的失败判定交给末腿 Nominatim。
    """
    cleaned = (city or "").strip()
    if not cleaned:
        raise ValueError("起点城市不能为空")
    reasons: list[str] = []
    try:
        return amap_geocode(cleaned), GEOCODER_AMAP
    except Exception as exc:  # noqa: BLE001 - 主路径失败只降级,不上抛(见 docstring)
        reasons.append(f"{AMAP_DS_SOURCE}={_failure_text(exc)}")
    try:
        return photon_geocode(cleaned), GEOCODER_PHOTON
    except Exception as exc:  # noqa: BLE001 - 同上:降级链中间腿只让位,不上抛
        reasons.append(f"{PHOTON_SOURCE}={_failure_text(exc)}")
    try:
        return dict(ds_geocode(cleaned)), GEOCODER_NOMINATIM
    except DataSourceError as exc:
        reasons.append(f"{NOMINATIM_SOURCE}={_failure_text(exc)}")
        raise DataSourceError(
            GEOCODER_CHAIN_SOURCE,
            f"三个地理编码源都失败,无法解析 {cleaned!r}:" + ";".join(reasons),
        ) from exc


def default_geocoder(city: str) -> dict[str, Any]:
    """起点解析的默认实现:城市名 → ``{city, name, lat, lng, geocoder}``。

    走 :func:`geocode_with_fallback`(高德主 + Photon/Nominatim 降级);``geocoder`` 只是
    给 API 层标注"谁答的",:func:`resolve_origin` 会把它摘掉,origin 形状不变。
    """
    geo, geocoder = geocode_with_fallback(city)
    return {
        "city": city,
        "name": geo["display_name"],
        "lat": geo["lat"],
        "lng": geo["lng"],
        "geocoder": geocoder,
    }


def resolve_origin(
    city: str,
    *,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    geocoder: Optional[GeocodeFn] = None,
) -> dict[str, Any]:
    """解析起点:调用方给了坐标就直接用,否则查地理编码降级链(会触网)。"""
    origin, _ = resolve_origin_with_source(city, lat=lat, lng=lng, geocoder=geocoder)
    return origin


def resolve_origin_with_source(
    city: str,
    *,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    geocoder: Optional[GeocodeFn] = None,
) -> tuple[dict[str, Any], str]:
    """同 :func:`resolve_origin`,另外回报这次是哪个地理编码源答的。

    ``geocoder``(``"amap"|"photon"|"nominatim"|"none"``)只作为**第二个返回值**给 API 用,
    不进 origin 字典 —— origin 的 city/name/lat/lng 四字段是前端与既有单测认定的唯一形状。
    调用方直接给了坐标时没有源参与,报 ``"none"``。
    """
    cleaned = (city or "").strip()
    if not cleaned:
        raise ValueError("起点城市不能为空")
    if (lat is None) != (lng is None):
        raise ValueError("lat/lng 必须成对给出")
    if lat is not None and lng is not None:
        return (
            {"city": cleaned, "name": cleaned, "lat": float(lat), "lng": float(lng)},
            GEOCODER_NONE,
        )
    geo = dict((geocoder or default_geocoder)(cleaned))
    # 统一成 {city, name, lat, lng} 四个字段,API/前端只看这一种形状。
    origin = {
        "city": str(geo.get("city") or cleaned),
        "name": str(geo.get("name") or geo.get("display_name") or cleaned),
        "lat": float(geo["lat"]),
        "lng": float(geo["lng"]),
    }
    # 注入的替身不带 geocoder 字段,它们顶的是 Nominatim 那一腿(既有单测口径)。
    return origin, str(geo.get("geocoder") or GEOCODER_NOMINATIM)


def reverse_with_fallback(
    lat: float,
    lng: float,
    *,
    zoom: int = REVERSE_ZOOM,
) -> tuple[dict[str, Any], str]:
    """逆地理编码降级链:**高德主 → Photon → Nominatim**(TASK-9c)。

    返回 ``({"lat", "lng", "display_name"}, "amap"|"photon"|"nominatim")``。``zoom`` 只有
    Nominatim 用得上(高德与 Photon 的 reverse 固定返回最近地点,不收 zoom);宽 ``except``
    的理由同 :func:`geocode_with_fallback`。三腿全失败时抛 :class:`DataSourceError`,
    由 :func:`resolve_reverse_origin` 兜成坐标起点(**不是** HTTP 错误)。
    """
    reasons: list[str] = []
    try:
        return amap_reverse(float(lat), float(lng)), GEOCODER_AMAP
    except Exception as exc:  # noqa: BLE001 - 主路径失败只降级,不上抛(见 geocode_with_fallback)
        reasons.append(f"{AMAP_DS_SOURCE}={_failure_text(exc)}")
    try:
        return dict(ds_photon_reverse(float(lat), float(lng))), GEOCODER_PHOTON
    except Exception as exc:  # noqa: BLE001 - 同上:降级链中间腿只让位,不上抛
        reasons.append(f"{PHOTON_SOURCE}={_failure_text(exc)}")
    try:
        return dict(ds_reverse(float(lat), float(lng), zoom=int(zoom))), GEOCODER_NOMINATIM
    except DataSourceError as exc:
        reasons.append(f"{NOMINATIM_SOURCE}={_failure_text(exc)}")
        raise DataSourceError(
            GEOCODER_CHAIN_SOURCE,
            f"三个逆地理编码源都失败,无法反查 ({float(lat):.6f},{float(lng):.6f}):"
            + ";".join(reasons),
        ) from exc


def default_reverse_geocoder(lat: float, lng: float, zoom: int = REVERSE_ZOOM) -> dict[str, Any]:
    """默认的**逆**地理编码:坐标 → ``{lat, lng, display_name, geocoder}``(高德主 + Photon/Nominatim 降级)。"""
    geo, geocoder = reverse_with_fallback(lat, lng, zoom=zoom)
    return {**geo, "geocoder": geocoder}


def unnamed_origin(lat: float, lng: float) -> str:
    """反查不到城市名时的起点名:``我的位置(31.23,121.47)``。

    坐标只保留 2 位小数(约 1 km 精度):既够画范围圈,又不会让 GPS 抖动
    每次都造出一个新的 ``origin_city``,把库切得七零八落。
    """
    return f"我的位置({float(lat):.{UNNAMED_COORD_PRECISION}f},{float(lng):.{UNNAMED_COORD_PRECISION}f})"


def city_from_display_name(display_name: Any) -> str:
    """从地理编码结果的 ``display_name`` 里挑出**城市级**地名(Photon / Nominatim 同一形状)。

    中文逆地理编码结果形如 ``"浦东新区, 上海市, 中国"`` 或
    ``"某某院, 东城区, 北京市, 100010, 中国"``,由细到粗用逗号分隔。规则:
    去掉国家名与纯数字邮编后,取第一个以 市/州/地区/盟 结尾的片段
    (``len > 后缀长``,避免只返回一个"市"字);都不像城市时退回最后一段(最粗的行政区)。
    """
    tokens = [part.strip() for part in str(display_name or "").split(",") if part.strip()]
    useful = [
        token for token in tokens
        if token.lower() not in COUNTRY_TOKENS and not token.isdigit()
    ]
    for token in useful:
        if any(token.endswith(suffix) and len(token) > len(suffix) for suffix in CITY_SUFFIXES):
            return token
    return useful[-1] if useful else ""


def resolve_reverse_origin(
    lat: float,
    lng: float,
    *,
    reverse_geocoder: Optional[ReverseGeocodeFn] = None,
    zoom: int = REVERSE_ZOOM,
) -> dict[str, Any]:
    """浏览器"我的位置" → 起点(**反查失败不报错**,降级成坐标起点)。

    细节(坐标口径、降级条件、``geocoder`` 标注)见 :func:`resolve_reverse_origin_with_source`。
    """
    origin, _ = resolve_reverse_origin_with_source(
        lat, lng, reverse_geocoder=reverse_geocoder, zoom=zoom
    )
    return origin


def resolve_reverse_origin_with_source(
    lat: float,
    lng: float,
    *,
    reverse_geocoder: Optional[ReverseGeocodeFn] = None,
    zoom: int = REVERSE_ZOOM,
) -> tuple[dict[str, Any], str]:
    """同 :func:`resolve_reverse_origin`,另外回报这次是哪个地理编码源答的。

    降级成坐标起点(``resolved=False``)时没有源答上来,报 ``"none"``;origin 字典的
    五个字段(city/name/lat/lng/resolved)保持不变,``geocoder`` 只作第二个返回值。

    与 :func:`resolve_origin` 的区别:坐标是已知的(浏览器 GPS 给的),只需要反查
    城市名,所以 ``lat``/``lng`` 一律沿用**传入的 GPS 坐标**(范围圈要以用户真实
    位置为圆心,而不是地理编码源返回的行政区中心)。

    高德、Photon 与 Nominatim 都挂了/被限流、或返回的 ``display_name`` 里挑不出城市时,
    返回 ``resolved=False`` + :func:`unnamed_origin` 兜底名 —— API 层照常 200,
    前端地图照样能画环、能查库,只是起点名不好看。
    """
    latitude = float(lat)
    longitude = float(lng)
    fallback = {
        "city": unnamed_origin(latitude, longitude),
        "name": unnamed_origin(latitude, longitude),
        "lat": latitude,
        "lng": longitude,
        "resolved": False,
    }
    # 注入的替身保持 (lat, lng) 两参;真实实现才需要 zoom(区县级反查更稳)
    lookup = reverse_geocoder or (lambda point_lat, point_lng: default_reverse_geocoder(point_lat, point_lng, zoom=zoom))
    try:
        geo = dict(lookup(latitude, longitude) or {})
    except (DataSourceError, ValueError, TypeError):
        return fallback, GEOCODER_NONE
    display_name = str(geo.get("display_name") or geo.get("name") or "").strip()
    city = city_from_display_name(display_name)
    if not city:
        return fallback, GEOCODER_NONE
    origin = {
        "city": city,
        "name": display_name or city,
        "lat": latitude,
        "lng": longitude,
        "resolved": True,
    }
    # 注入的替身不带 geocoder 字段,它们顶的是 Nominatim 那一腿(既有单测口径)。
    return origin, str(geo.get("geocoder") or GEOCODER_NOMINATIM)


def band_bbox(lat: float, lng: float, radius_m: float) -> tuple[float, float, float, float]:
    """外半径的经纬度包围盒 ``(min_lat, min_lng, max_lat, max_lng)``(:func:`amap.grid_polygons` 的入参)。

    经度方向按 ``cos(lat)`` 修正,高纬度夹 :data:`MIN_COS_LAT` 下限(与
    :func:`services.stays.select_stays` 的包围盒粗筛同口径),免得格子数在极地附近炸开。
    """
    latitude = float(lat)
    longitude = float(lng)
    radius = max(0.0, float(radius_m))
    lat_delta = radius / METERS_PER_DEGREE
    cos_lat = max(MIN_COS_LAT, abs(math.cos(math.radians(latitude))))
    lng_delta = radius / (METERS_PER_DEGREE * cos_lat)
    min_lat = max(-89.999, latitude - lat_delta)
    max_lat = min(89.999, latitude + lat_delta)
    min_lng = max(-179.999, longitude - lng_delta)
    max_lng = min(179.999, longitude + lng_delta)
    if min_lat >= max_lat or min_lng >= max_lng:
        raise ValueError(
            f"起点太靠近极点/日期线,无法为半径 {radius:.0f}m 构造包围盒:{latitude},{longitude}"
        )
    return min_lat, min_lng, max_lat, max_lng


def grid_side(radius_m: float) -> int:
    """包围盒切几行几列:让每格边长 ≈ :data:`GRID_CELL_KM`,夹在 [:data:`GRID_MIN_SIDE`, :data:`GRID_MAX_SIDE`]。

    格子边长要小到「单查询 200 条」装得下(高德硬上限,见 :data:`amap.MAX_ROWS_PER_QUERY`),
    又不能格数爆炸(每格每页都是一次 0.6s 节流的请求)—— 远环 POI 稀疏,格子大些无妨。
    """
    diameter_km = 2.0 * max(0.0, float(radius_m)) / 1000.0
    if diameter_km <= 0:
        return GRID_MIN_SIDE
    side = math.ceil(diameter_km / GRID_CELL_KM)
    return max(GRID_MIN_SIDE, min(GRID_MAX_SIDE, int(side)))


def cell_distance_range(
    cell: Sequence[Sequence[float]], lat: float, lng: float
) -> tuple[float, float]:
    """一个矩形格到起点的 ``(最近, 最远)`` 大圆距离(km)——用来筛掉整格都不在环内的格子。

    最近点 = 把起点坐标夹进矩形(起点在格内则为 0);最远点必在四个角上。
    """
    lngs = [float(point[0]) for point in cell]
    lats = [float(point[1]) for point in cell]
    near_lat = min(max(float(lat), min(lats)), max(lats))
    near_lng = min(max(float(lng), min(lngs)), max(lngs))
    nearest = haversine_km(float(lat), float(lng), near_lat, near_lng)
    farthest = max(
        haversine_km(float(lat), float(lng), corner_lat, corner_lng)
        for corner_lat in (min(lats), max(lats))
        for corner_lng in (min(lngs), max(lngs))
    )
    return nearest, farthest


def ring_cells(
    lat: float,
    lng: float,
    band: Mapping[str, Any],
    *,
    side: Optional[int] = None,
) -> list[list[tuple[float, float]]]:
    """band(下限 > 0)的**分格计划**:切格 → 只留与环带相交的 → 按最近距离升序。

    高德没有 Overpass 那种「环形差集」查询,只能包围盒切格逐格捞,再本地 haversine 收敛。
    排序按「格子到起点的最近距离」升序:整格都落在 ``[low, high)`` 之外的直接丢掉
    (太近 = 上一段的地盘,太远 = 下一段),于是**前几格总是环带里离起点最近的部分**,
    配合 :func:`grid_cell_limit` 就是「本轮抓几格、下一轮扩几格」的递增扩格。
    """
    radius_m = band_radius_m(band)
    inner_km = band_inner_radius_m(band) / 1000.0
    outer_km = radius_m / 1000.0
    rows = max(1, int(side)) if side is not None else grid_side(radius_m)
    min_lat, min_lng, max_lat, max_lng = band_bbox(lat, lng, radius_m)
    cells = amap.grid_polygons(min_lat, min_lng, max_lat, max_lng, rows, rows)
    usable: list[tuple[float, int, list[tuple[float, float]]]] = []
    for index, cell in enumerate(cells):
        nearest, farthest = cell_distance_range(cell, lat, lng)
        if farthest < inner_km or nearest >= outer_km:
            continue
        usable.append((nearest, index, cell))
    usable.sort(key=lambda item: (item[0], item[1]))
    return [cell for _, _, cell in usable]


def grid_cell_limit(fetch_rounds: Optional[int] = None) -> int:
    """本轮最多抓几格::data:`GRID_CELLS_PER_ROUND` × (已完成轮数 + 1) → 4 / 8 / 12 ...

    与 :func:`progressive_target_total`(``30 × (轮数 + 1)``)同口径的**递增扩格**:
    上一轮配额没抓满、又还有未抓的格子时,下一轮「加载更多」自动多抓几格。
    """
    return GRID_CELLS_PER_ROUND * (max(0, int(fetch_rounds or 0)) + 1)


def page_limit(budget: Optional[int], *, slack: int = 0) -> int:
    """一组配额最多翻几页:``ceil(budget / 25) + slack``,夹在 ``[1, amap.MAX_PAGE]``。

    v3 固定 25 条/页、``page >= 9`` 服务端恒空(:data:`amap.MAX_ROWS_PER_QUERY` = 200),
    所以既不能硬翻,也别为 8 条配额翻满 8 页。``slack`` 给分格路径用:环带收敛会丢掉
    「太近/太远」的行,多翻一页兜住这部分损耗。
    """
    wanted = max(1, int(budget or 0))
    pages = math.ceil(wanted / amap.PAGE_SIZE) + max(0, int(slack))
    return max(1, min(amap.MAX_PAGE, int(pages)))


def _paged(
    search_fn: Callable[..., list[dict[str, Any]]],
    *,
    budget: int,
    page_cap: int,
    keep: Optional[Callable[[Mapping[str, Any]], bool]] = None,
) -> list[dict[str, Any]]:
    """翻页取一组:每页 :data:`amap.PAGE_SIZE` 条,**配额抓满 / 某页没拿满 / 到页数上限**就停。

    ``keep`` 是本地过滤(分格路径用它把环外的行丢掉):被过滤掉的行**不占配额**,
    否则近处的密集 POI 会把整组配额吃光 —— 这正是 Overpass 时代「远环只剩个位数」的根因。
    组内按 :func:`services.amap_categories.dedupe_key`(``("amap", POI id)``)去重。
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for page in range(1, max(1, int(page_cap)) + 1):
        batch = list(search_fn(page=page, offset=amap.PAGE_SIZE) or [])
        for poi in batch:
            if keep is not None and not keep(poi):
                continue
            key = amap_categories.dedupe_key(poi)
            if key in seen:
                continue
            seen.add(key)
            rows.append(dict(poi))
            if budget and len(rows) >= budget:
                return rows
        if len(batch) < amap.PAGE_SIZE:
            return rows
    return rows


def _group_params(group: Mapping[str, Any]) -> tuple[Optional[list[str]], Optional[str]]:
    """一组的检索参数 ``(types, keywords)``;§6.3:两者**二选一**,空值一律传 ``None``。"""
    types = [str(code) for code in (group.get("types") or ()) if str(code).strip()]
    keywords = str(group.get("keywords") or "").strip()
    return (types or None), (keywords or None)


def fetch_group_around(
    lat: float,
    lng: float,
    radius_m: float,
    group: Mapping[str, Any],
    *,
    environ: Optional[Mapping[str, str]] = None,
    session: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """band 下限 = 0:一组一次 ``place/around`` 圆形检索(半径 > 50km 由 amap 内部钳制)。"""
    budget = max(0, int(group.get("budget") or 0))
    if not budget:
        return []
    types, keywords = _group_params(group)
    return _paged(
        lambda **params: amap.search_around(
            lat, lng, radius_m=radius_m, types=types, keywords=keywords,
            environ=environ, session=session, **params,
        ),
        budget=budget,
        page_cap=page_limit(budget),
    )


def fetch_group_polygon(
    cells: Sequence[Sequence[Sequence[float]]],
    lat: float,
    lng: float,
    band: Mapping[str, Any],
    group: Mapping[str, Any],
    *,
    environ: Optional[Mapping[str, str]] = None,
    session: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """band 下限 > 0:一组**逐格** ``place/polygon`` + 本地 haversine 收敛到 ``[low, high)``。

    配额是整组共享的:抓满就**停止扩格**(剩下的格子留给下一轮 :data:`SegmentFetch.fetch_rounds`),
    环外的行不占配额(见 :func:`_paged` 的 ``keep``);相邻格子共用边界,贴着格线的 POI
    会被两格各返回一次,所以组内**跨格**再按 :func:`amap_categories.dedupe_key` 去一遍重,
    免得同一条 POI 白占两个配额(:func:`_paged` 只在单格单组内去重)。
    """
    budget = max(0, int(group.get("budget") or 0))
    if not budget or not cells:
        return []
    types, keywords = _group_params(group)

    def keep(poi: Mapping[str, Any]) -> bool:
        distance = haversine_km(lat, lng, float(poi["lat"]), float(poi["lng"]))
        return in_band(distance, band)

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for cell in cells:
        remaining = budget - len(rows)
        if remaining <= 0:
            break
        for poi in _paged(
            lambda cell=cell, **params: amap.search_polygon(
                cell, types=types, keywords=keywords,
                environ=environ, session=session, **params,
            ),
            budget=remaining,
            page_cap=page_limit(remaining, slack=RING_PAGE_SLACK),
            keep=keep,
        ):
            key = amap_categories.dedupe_key(poi)
            if key in seen:
                continue
            seen.add(key)
            rows.append(poi)
            if len(rows) >= budget:
                break
    return rows[:budget]


def default_fetcher(
    lat: float,
    lng: float,
    band: Mapping[str, Any],
    *,
    groups: Optional[Iterable[Mapping[str, Any]]] = None,
    fetch_rounds: Optional[int] = None,
    cell_limit: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
    session: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """真实抓取(TASK-9b 起走**高德 v3**):分组检索 → 合并 → 按 POI id 去重。

    * band 下限 ``== 0``(:data:`services.bands` 的 ``0_50``)→ 每组一次
      :func:`amap.search_around`(半径 = band 上限;超过 50km 由 amap 钳到 50000);
    * band 下限 ``> 0`` → 外半径**包围盒** :func:`ring_cells` 切格,逐组逐格
      :func:`amap.search_polygon`,本地 haversine 收敛到 ``[low, high)``;
      本轮只抓前 :func:`grid_cell_limit` 格(``fetch_rounds`` 越大抓得越多 = **递增扩格**),
      配额抓满即停,不再打剩下的格子;
    * 每组翻页上限 :func:`page_limit`(单查询 200 条硬上限,拿不满不硬翻);
    * 多组结果合并后按 ``("amap", POI id)`` 去重(同一实体被多组命中只留一行),
      归类交给 :func:`services.classify.classify_amap_places`(优先级 滑雪>运动>人文美食>自然)。

    ``groups`` 是 :func:`scale_search_groups` 缩放后的分组(渐进配额);
    ``environ``/``session`` 透传给 :mod:`data_sources.amap`,测试可注入假 session。
    """
    wanted = list(groups) if groups is not None else list(SEARCH_GROUPS)
    radius_m = band_radius_m(band)
    if band_inner_radius_m(band) <= 0:
        batches = [
            fetch_group_around(lat, lng, radius_m, group, environ=environ, session=session)
            for group in wanted
        ]
    else:
        cells = ring_cells(lat, lng, band)
        limit = grid_cell_limit(fetch_rounds) if cell_limit is None else max(0, int(cell_limit))
        batches = [
            fetch_group_polygon(cells[:limit], lat, lng, band, group, environ=environ, session=session)
            for group in wanted
        ]
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for batch in batches:
        for poi in batch:
            key = amap_categories.dedupe_key(poi)
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(poi))
    return merged


def scale_search_groups(target_total: Optional[int]) -> Optional[list[dict[str, Any]]]:
    """把 :data:`SEARCH_GROUPS` 各组配额按比例缩到总量 ≈ ``target_total``(渐进抓取用)。

    每组 ``max(MIN_GROUP_BUDGET, round(组配额 × target_total / 540))``:按比例缩放才保得住
    四分类的相对权重(自然风光 140 vs 古镇 40),下限 :data:`MIN_GROUP_BUDGET` 保证小分组
    不会被缩成 0 而整类消失。``target_total`` 为 ``None`` 时返回 ``None`` = 用满配额(旧口径)。
    **一次查完分组并集**的口径不变,变的只是服务端取数上限,所以首屏从分钟级降到秒级。
    """
    if target_total is None:
        return None
    wanted = max(1, int(target_total))
    scaled: list[dict[str, Any]] = []
    for group in SEARCH_GROUPS:
        row = dict(group)
        row["budget"] = max(
            MIN_GROUP_BUDGET, round(int(group["budget"]) * wanted / FULL_SEARCH_BUDGET)
        )
        scaled.append(row)
    return scaled


def progressive_target_total(fetch_rounds: Optional[int] = None) -> int:
    """下一轮扩抓的目标总量:``30 × (已完成轮数 + 1)`` → 30 / 60 / 90 ..."""
    return PROGRESSIVE_STEP * (max(0, int(fetch_rounds or 0)) + 1)


def _accepts_kwarg(fetch_fn: FetchFn, name: str) -> bool:
    """抓取实现是否认某个关键字参数。

    既有单测的替身是 ``(lat, lng, band)`` 三参签名,不能因为加了配额缩放(``groups``)
    或递增扩格(``fetch_rounds``)就报错;真链路 :func:`default_fetcher` 两个都认。
    """
    try:
        parameters = list(inspect.signature(fetch_fn).parameters.values())
    except (TypeError, ValueError):  # pragma: no cover - 拿不到签名的内建对象
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == name
        for parameter in parameters
    )


def _accepts_groups(fetch_fn: FetchFn) -> bool:
    """抓取实现是否认 ``groups`` 关键字(:func:`_accepts_kwarg` 的兼容别名)。"""
    return _accepts_kwarg(fetch_fn, "groups")


def _fetcher_kwargs(
    fetch_fn: FetchFn, *, groups: Optional[list[dict[str, Any]]], fetch_rounds: int
) -> dict[str, Any]:
    """按替身签名挑它**认**的关键字:渐进配额(``groups``)与扩格轮数(``fetch_rounds``)。"""
    kwargs: dict[str, Any] = {}
    if groups is not None and _accepts_kwarg(fetch_fn, "groups"):
        kwargs["groups"] = groups
    if _accepts_kwarg(fetch_fn, "fetch_rounds"):
        kwargs["fetch_rounds"] = fetch_rounds
    return kwargs


def stored_fetch_rounds(recorded: Optional[SegmentFetch]) -> int:
    """水位里已完成的抓取轮数(首抓 / 旧库缺列时按 0)—— 递增扩格与目标配额都靠它推。"""
    if recorded is None:
        return 0
    return int(getattr(recorded, "fetch_rounds", 0) or 0)


def place_identity(place: Mapping[str, Any]) -> tuple[str, int]:
    """取 OSM 身份 ``(osm_type, osm_id)``;缺失时用"名字+坐标"指纹兜底(负数 id)。

    兜底分支给 TASK-1c 的种子数据用:没有 OSM id 的条目同样能防重、能 upsert,
    且负数 id 不会与真实 OSM id 撞车。
    """
    osm_type = str(place.get("osm_type") or "").strip().lower()
    osm_id = place.get("osm_id")
    if osm_type in OSM_ELEMENT_TYPES and isinstance(osm_id, int) and not isinstance(osm_id, bool):
        return osm_type, osm_id
    fingerprint = f"{place.get('name') or ''}|{place.get('lat')}|{place.get('lng')}"
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()  # 仅做指纹,非安全用途
    return FALLBACK_OSM_TYPE, -int(digest[:FINGERPRINT_HEX_LEN], 16)


def amap_tags(row: Mapping[str, Any]) -> dict[str, Any]:
    """高德行的入库 ``tags``:``{"source":"高德","amap_id":…,"typecode":…,"type":…}``。

    原始 POI id **必须**留在这里:``osm_id`` 是 crc32 哈希(不可逆),前端身份脚注、
    排查与「同实体跨 band 认亲」都靠 ``tags["amap_id"]`` 还原原文;``type``/``typecode``
    是高德的分类原文,给 :mod:`services.intro` / :mod:`services.details` 的 prompt 当事实线索。
    ``source="高德"`` 让 :func:`db.models.place_source` 派生出「高德」来源标注
    (存量「种子」/「OSM」行的标注不受影响)。
    """
    return {
        "source": AMAP_SOURCE,
        "amap_id": classify.amap_poi_id(row),
        "typecode": str(row.get("typecode") or "").strip(),
        "type": str(row.get("type") or "").strip(),
    }


def to_place_items(candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """环内候选 → 入库条目:先按高德 POI id **去重**,再按优先级**归类**,最后派生入库身份。

    去重与归类由 :func:`services.classify.classify_amap_places` 完成(一地只入一类);
    身份是 §3 TASK-9b 的硬约束 —— ``Place.osm_id`` 是 Integer 列而高德 POI id 是字符串,
    所以 ``osm_type="amap"``、``osm_id = crc32(id)``(:func:`db.models.amap_osm_id`),
    原文进 ``tags["amap_id"]``(**表结构零改动**,唯一键与收藏 ref_key 口径原样保留)。
    没有高德身份的行(人工种子 / 存量 OSM 形状)退回 :func:`place_identity` 的旧口径。
    ``intro`` 不在此生成 —— 入库后由 :mod:`services.intro` 按 POI 缓存补。
    """
    items: list[dict[str, Any]] = []
    for row in classify_amap_places(candidates):
        poi_id = classify.amap_poi_id(row)
        if poi_id:
            osm_type, osm_id = AMAP_OSM_TYPE, amap_osm_id(poi_id)
            tags = amap_tags(row)
        else:
            osm_type, osm_id = place_identity(row)
            tags = dict(row.get("tags") or {})
        items.append(
            {
                "osm_type": osm_type,
                "osm_id": osm_id,
                "name": row.get("name") or "",
                "lat": row["lat"],
                "lng": row["lng"],
                "category": row["category"],
                "tags": tags,
            }
        )
    return items


def to_seed_items(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """种子数据 → 入库条目:与 :func:`to_place_items` 同形状,**但带静态 ``intro``**。

    种子没有 OSM 身份,``place_identity`` 会用"名字 + 坐标"的 sha1 指纹兜底成
    ``("point", 负数)``:既不会与真实 OSM id 撞车,重复写入也照样被唯一键挡住。
    ``category`` 直接沿用种子里声明的值(:func:`services.seed_data.validate` 保证它与
    ``categorize(tags)`` 一致),``intro`` 是人工静态文案 —— 有了它,
    :func:`services.intro.fill_missing_intros` 就不会再把这些行送去调 LLM。
    """
    items: list[dict[str, Any]] = []
    for row in rows or []:
        osm_type, osm_id = place_identity(row)
        items.append(
            {
                "osm_type": osm_type,
                "osm_id": osm_id,
                "name": row.get("name") or "",
                "lat": row["lat"],
                "lng": row["lng"],
                "category": row.get("category") or UNCATEGORIZED,
                "tags": dict(row.get("tags") or {}),
                "intro": str(row.get("intro") or "").strip(),
            }
        )
    return items


def ensure_seeded(
    session: Session,
    *,
    origin: Mapping[str, Any],
    band: Mapping[str, Any],
    seeds: Optional[Iterable[Mapping[str, Any]]] = None,
) -> int:
    """给某个 (起点, band) 幂等补种:只写库里还没有的种子,返回**新增**条数。

    纯本地操作(不触网、不调 LLM),所以已入库的分段二次查询时也能顺手补种,
    存量库不必重抓高德。去重口径与 :func:`services.seed_data.attach_seeds` 一致:
    库里已有同名(相等或互相包含)且距离 ≤ 2 km 的行,就认为已覆盖、不再补。
    """
    seed_rows = list(seeds) if seeds is not None else seed_data.load_seeds()
    if not seed_rows:
        return 0
    in_band = seed_data.seeds_in_band(seed_rows, origin["lat"], origin["lng"], band)
    if not in_band:
        return 0
    stored = repo.list_places(
        session,
        origin_city=origin["city"],
        band=band["key"],
        origin_lat=origin["lat"],
        origin_lng=origin["lng"],
    )
    fresh = seed_data.attach_seeds(stored, in_band)
    if not fresh:
        return 0
    return repo.upsert_places(
        session, origin_city=origin["city"], band=band["key"], items=to_seed_items(fresh)
    )


def record_seed_segment(
    session: Session, *, city: str, band: Mapping[str, Any], origin: Mapping[str, Any]
) -> SegmentFetch:
    """给"只有种子、还没抓过高德"的分段建一条水位(``source="seed"``)。"""
    return repo.record_segment(
        session,
        origin_city=city,
        band=band["key"],
        origin=origin,
        place_count=0,
        source=SOURCE_SEED,
    )


def load_segment(
    session: Session,
    *,
    city: str,
    band: str,
    category: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    fetcher: Optional[FetchFn] = None,
    geocoder: Optional[GeocodeFn] = None,
    seeds: Optional[Iterable[Mapping[str, Any]]] = None,
    refresh: bool = False,
    target_total: Optional[int] = None,
    intros: bool = True,
    intro_limit: Optional[int] = INTRO_BATCH_LIMIT,
    intro_workers: int = intro_service.DEFAULT_WORKERS,
) -> SegmentOutcome:
    """读取某 (城市, band) 的目的地;未入库才抓取并落库。

    ``intros=True`` 时,**抓取入库之后**再给缺简介的 POI 补 LLM 一句话简介
    (只作用于本次抓取路径:命中库直接读库时不调 LLM,保持零网络秒回)。
    简介失败一律降级为空,不影响已入库的数据。

    ``seeds`` 留空就用 :func:`services.seed_data.load_seeds`(受 ``WHERE2GO_SEEDS``
    开关控制);传 ``[]`` 可显式关掉本次的种子合并。抓取与读库两条路径都合并种子,
    所以已入库的分段也能拿到种子,不必重抓高德。

    ``target_total``(TASK-6b 渐进抓取)只在**真的要抓**时生效:给定了就把
    :data:`SEARCH_GROUPS` 各组配额按比例缩到总量 ≈ ``target_total``
    (见 :func:`scale_search_groups`);留空 = 用满配额(540)的旧口径。
    归类/去重/环内收敛/upsert 口径完全不变,所以扩抓轮重复命中同一实体也不会
    产生重复行、不会覆盖已有 ``intro``。每完成一轮抓取,
    :attr:`db.models.SegmentFetch.fetch_rounds` +1(:func:`db.repository.bump_fetch_rounds`),
    下一轮的**目标总量**(``30 × (轮数+1)``)与**抓几格**(:func:`grid_cell_limit`,
    递增扩格)都由它推出来;``refresh=True`` 时沿用库内轮数,不会把扩格进度归零。

    失败语义:分段/城市非法抛 :class:`ValueError`;数据源不可用抛
    :class:`data_sources.DataSourceError`(由 API 层翻成中文 HTTP 错误)。
    """
    band_def = require_band(band)
    city_clean = (city or "").strip()
    if not city_clean:
        raise ValueError("起点城市不能为空")
    fetch_fn = fetcher or default_fetcher
    seed_rows = list(seeds) if seeds is not None else seed_data.load_seeds()

    recorded = repo.get_segment(session, origin_city=city_clean, band=band_def["key"])
    if recorded is not None and not refresh:
        origin = stored_origin(recorded)
        # 读库路径也补种:纯本地、幂等(第二次必然补 0 条),存量库不必重抓高德
        seeded = ensure_seeded(session, origin=origin, band=band_def, seeds=seed_rows)
        if seeded:
            recorded.place_count = int(recorded.place_count or 0) + seeded
            session.commit()
        return _read_from_db(
            session,
            origin=origin,
            band=band_def,
            category=category,
            source=SOURCE_DB,
            network_used=False,
            segment=repo.segment_to_dict(recorded),
            seeded=seeded,
        )

    origin = _reuse_origin(session, city_clean, recorded=recorded, lat=lat, lng=lng, geocoder=geocoder)
    groups = scale_search_groups(target_total)
    # 扩格轮数跟着水位走:这一轮的分组配额与「抓几格」都由它推出来(递增扩格)
    payload = fetch_fn(
        origin["lat"],
        origin["lng"],
        band_def,
        **_fetcher_kwargs(
            fetch_fn, groups=groups, fetch_rounds=stored_fetch_rounds(recorded)
        ),
    )
    candidates = filter_to_band(payload or [], origin["lat"], origin["lng"], band_def)
    fresh_seeds = _fresh_seeds(candidates, seed_rows, origin, band_def)
    seeded_items = to_seed_items(fresh_seeds)
    written = repo.upsert_places(
        session,
        origin_city=city_clean,
        band=band_def["key"],
        items=to_place_items(candidates) + seeded_items,
    )
    record = repo.record_segment(
        session,
        origin_city=city_clean,
        band=band_def["key"],
        origin=origin,
        # 扩抓轮是**追加**在已有行之上,水位要记库内真实条数,否则 /api/geocode 的
        # segments 会把"这一轮抓到多少"当成"这个分段一共有多少"。冷启动/refresh 的
        # 口径保持原样(本轮候选数 + 本轮补种数)。
        place_count=(
            repo.count_places(session, origin_city=city_clean, band=band_def["key"])
            if target_total is not None
            else len(candidates) + len(seeded_items)
        ),
        source=SOURCE_AMAP,
    )
    repo.bump_fetch_rounds(session, record)
    session.commit()
    # 先落库再补简介:LLM 挂了/没 key 也只是简介为空,入库结果不受影响。
    intro_stats = None
    if intros:
        intro_stats = intro_service.fill_missing_intros(
            session,
            origin_city=city_clean,
            band=band_def["key"],
            limit=intro_limit,
            workers=intro_workers,
        )
    return _read_from_db(
        session,
        origin=origin,
        band=band_def,
        category=category,
        source=SOURCE_AMAP,
        network_used=True,
        segment=repo.segment_to_dict(record),
        written=written,
        seeded=len(seeded_items),
        intro_stats=intro_stats,
    )


def _fresh_seeds(
    candidates: Iterable[Mapping[str, Any]],
    seed_rows: Sequence[Mapping[str, Any]],
    origin: Mapping[str, Any],
    band: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """抓取路径的种子合并:先按环筛种子,再与本次 OSM 结果按名字 + 坐标去重。"""
    if not seed_rows:
        return []
    in_band = seed_data.seeds_in_band(seed_rows, origin["lat"], origin["lng"], band)
    return seed_data.attach_seeds(candidates, in_band)


def _reuse_origin(
    session: Session,
    city: str,
    *,
    recorded: Optional[SegmentFetch],
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    geocoder: Optional[GeocodeFn] = None,
) -> dict[str, Any]:
    """决定这次抓取用哪个起点:调用方坐标 > 库内已有起点 > Nominatim(触网)。"""
    if lat is not None or lng is not None:
        return resolve_origin(city, lat=lat, lng=lng, geocoder=geocoder)
    known = recorded or repo.latest_city_origin(session, origin_city=city)
    if known is not None:
        return stored_origin(known)
    return resolve_origin(city, geocoder=geocoder)


def stored_origin(record: SegmentFetch) -> dict[str, Any]:
    """抓取水位里的起点 → 统一的 origin dict(读库与重抓共用同一原点)。"""
    return {
        "city": record.origin_city,
        "name": record.origin_name,
        "lat": record.origin_lat,
        "lng": record.origin_lng,
    }


def _read_from_db(
    session: Session,
    *,
    origin: dict[str, Any],
    band: Mapping[str, Any],
    category: Optional[str],
    source: str,
    network_used: bool,
    segment: Optional[dict[str, Any]],
    written: int = 0,
    seeded: int = 0,
    intro_stats: Optional[dict[str, Any]] = None,
) -> SegmentOutcome:
    """统一从库里取数,保证"读库"与"刚抓完"两条路径返回同一种形状。"""
    places = repo.list_places(
        session,
        origin_city=origin["city"],
        band=band["key"],
        category=category,
        origin_lat=origin["lat"],
        origin_lng=origin["lng"],
    )
    counts = repo.count_by_category(session, origin_city=origin["city"], band=band["key"])
    return SegmentOutcome(
        origin=dict(origin),
        band=dict(band),
        places=places,
        source=source,
        network_used=network_used,
        fetched_at=(segment or {}).get("fetched_at"),
        written=written,
        seeded=seeded,
        counts_by_category=counts,
        counts_by_source=repo.count_by_source(
            session, origin_city=origin["city"], band=band["key"]
        ),
        segment=segment,
        intro_stats=intro_stats,
    )


def main(argv: Optional[list[str]] = None) -> int:
    """CLI:预抓 (城市, band) 入库。``python -m services.place_loader 上海 50_100``"""
    parser = argparse.ArgumentParser(
        description="抓取并入库某城市某距离分段的目的地(已入库则直接读库,不触网)"
    )
    parser.add_argument("city", nargs="?", default="上海", help="起点城市名(默认:上海)")
    parser.add_argument("band", nargs="?", default="50_100", choices=band_keys(), help="距离分段 key")
    parser.add_argument("--refresh", action="store_true", help="强制重抓(仍按唯一键 upsert)")
    parser.add_argument("--no-intros", action="store_true", help="抓取后不调 LLM 补简介")
    parser.add_argument("--intro-limit", type=int, default=None,
                        help=f"本次最多补多少条简介(默认 {INTRO_BATCH_LIMIT};0 = 不限)")
    parser.add_argument("--db", default=None, help="数据库 URL(默认 WHERE2GO_DB_URL 或 backend/data/where2go.db)")
    parser.add_argument("--show", type=int, default=10, help="打印前 N 条(默认 10)")
    args = parser.parse_args(argv)

    intro_limit = INTRO_BATCH_LIMIT if args.intro_limit is None else (None if args.intro_limit <= 0 else args.intro_limit)
    engine = make_engine(args.db)
    init_db(engine)
    with open_session(engine) as session:
        try:
            outcome = load_segment(
                session,
                city=args.city,
                band=args.band,
                refresh=args.refresh,
                intros=not args.no_intros,
                intro_limit=intro_limit,
            )
        except (DataSourceError, ValueError) as exc:
            print(f"[失败] {exc}")
            return 1

    print(
        f"[完成] {outcome.origin['name']} · {outcome.band['label']} · "
        f"{len(outcome.places)} 条 · 来源={'数据库' if outcome.source == SOURCE_DB else '高德实时抓取'} · "
        f"本次写入 {outcome.written} 条"
    )
    print(f"[分类] {' · '.join(f'{name} {total}' for name, total in sorted(outcome.counts_by_category.items()))}")
    if outcome.intro_stats:
        stats = outcome.intro_stats
        print(f"[简介] 生成 {stats['filled']} 条 · 降级 {stats['failed']} 条 · 仍缺 {stats['pending']} 条 · {stats['provider']}")
    for row in outcome.places[: max(0, args.show)]:
        intro = f" — {row['intro']}" if row.get("intro") else ""
        print(f"  - {row['name']}({row['category']}) 距起点 {row['distance_km']} km{intro}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
