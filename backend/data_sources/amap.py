"""高德(AutoNavi / AMap)v3 Web 服务数据源层(TASK-9a,2026-10-01 起的主数据源)。

为什么要换(见 ``docs/TASK-9-CONTRACT.md``):Overpass 抓一个城市要 125~383s 且中国
POI 覆盖差(崇礼滑雪场几乎为 0),高德 v3 REST 国内直连**秒级**返回、带 POI id /
typecode / 距离,坐标系(GCJ-02)与前端底图自洽。本模块只做**数据源适配**:检索、
地理编码、驾车路线与两个纯函数工具;归类在 :mod:`services.amap_categories`,
入库/渐进配额在 :mod:`services.place_loader`(TASK-9b)。

实测口径(2026-09-30 ~ 10-01 容器内实跑,**勿改**):

* **必须走 v3**(``https://restapi.amap.com/v3``):v5 的 ``page=2`` 与 ``page=1``
  返回完全相同的一页(翻页坏),所以这里所有检索都固定在 v3。
* **单查询最多 200 条**(``offset=25`` × ``page=1..8``);``page>=9`` 直接返回 0 条,
  响应里的 ``count`` 字段仍显示 1000/600(**不可信**,别拿它当可取条数)。
  → 拿不满就按 typecode 拆细 + 用 :func:`grid_polygons` 分格 polygon 检索(TASK-9b)。
* **``radius`` 被截断在 50000**(``radius=60000/100000`` 返回与 50000 一致)
  → 环带下限 ≥50km 必须走 :func:`search_polygon`。
* **QPS 限流**:密集连打第 3 个请求就 ``infocode=10021`` → 模块内单进程节流
  :data:`MIN_REQUEST_INTERVAL_S` = 0.6s(§6.1 定稿,不是 0.4s)。
* **HTTP 码恒 200**,成败要看响应体的 ``status``/``infocode``(见 :func:`check_status`):
  ``10021``/``10004`` 等限流与繁忙是**瞬时**错误(退避重试 2s/5s、最多 3 次),
  ``10001``(key 无效)/``10009``(JS key 调 REST,平台不符)等是**永久**错误。
* **驾车两个反直觉坑**(§1.5):``tolls`` 恒 ``0``、``cost`` 恒 ``null``(个人 key 不出
  过路费数值)→ 本模块**不透出**这两个字段,只透出 ``toll_distance_m``(收费路段米),
  过路费由上层按 ``toll_distance × 区域费率`` 估算;``steps[].polyline`` 是
  ``"lng,lat;lng,lat;…"`` **明文**(不是 Google 式编码折线),用 :func:`decode_polyline`
  解析成 ``[[lat, lng], …]`` 并按 :data:`GEOMETRY_MAX_POINTS` 抽稀。
* ``strategy`` 参数**只在签名里保留、请求时不传**(§6.5:传了可能返回多条 ``paths``);
  万一仍返回多条,一律取 ``paths[0]``。

坐标系:高德全链 **GCJ-02**,内部自洽不做转换(存量 WGS-84 行由白天 refresh 消化)。
所有公开函数都接受 ``environ=`` / ``session=`` 关键字注入,便于单测全程 mock 不触网;
网络/格式错误一律抛 :class:`DataSourceError`(中文说明),瞬时的抛
:class:`TransientDataSourceError`,方便上层回落 Photon / Nominatim 降级链。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping as AbcMapping
from collections.abc import Sequence as AbcSequence
from typing import Any, Callable, Mapping, Optional, Sequence, Union

from ._common import (
    USER_AGENT,
    DataSourceError,
    TransientDataSourceError,
    build_session,
    http_json,
    normalize_timeout,
)

SOURCE_NAME = "amap"
DEFAULT_ENDPOINT = "https://restapi.amap.com/v3"
ENV_ENDPOINT = "WHERE2GO_AMAP_ENDPOINT"
ENV_AMAP_KEY = "WHERE2GO_AMAP_KEY"
ENV_MIN_INTERVAL = "WHERE2GO_AMAP_MIN_INTERVAL_S"

#: 实测 ``radius`` 超过 50000 会被服务端截断 → 客户端先钳住,免得以为查到了更大范围
AUTO_MAX_RADIUS_M = 50000
#: 实测单个查询最多取到 200 条(``page>=9`` 返回 0 条)
MAX_ROWS_PER_QUERY = 200
#: v3 每页固定 25 条
PAGE_SIZE = 25
#: 25 × 8 = 200,再往后就是空页
MAX_PAGE = 8
#: 单进程两次请求的最小间隔(§6.1:实测连发第 3 个请求即 10021 QPS 超限)
MIN_REQUEST_INTERVAL_S = 0.6
#: 瞬时错误退避重试:第 1 次失败等 2s、第 2 次等 5s,最多 3 次尝试
MAX_ATTEMPTS = 3
RETRY_BACKOFF_S: tuple[float, ...] = (2.0, 5.0)

COORD_PRECISION = 7          # 与 db.models.COORD_PRECISION 同一口径(定点入库)
GEOMETRY_COORD_PRECISION = 6  # 折线坐标 6 位小数(≈0.1m,画线足够)
GEOMETRY_MAX_POINTS = 1200    # 抽稀口径与 services.routes.GEOMETRY_MAX_POINTS 等值(两边各自定义)

GEOCODE_PATH = "/geocode/geo"
REGEO_PATH = "/geocode/regeo"
SEARCH_AROUND_PATH = "/place/around"
SEARCH_POLYGON_PATH = "/place/polygon"
DRIVING_PATH = "/direction/driving"

EXTENSIONS_BASE = "base"
EXTENSIONS_ALL = "all"
STATUS_OK = "1"
#: 多值参数(``types`` / ``keywords``)的分隔符:实测 ``types=080106|110101`` 混排可用
MULTI_VALUE_SEPARATOR = "|"
#: polygon 各顶点之间的分隔符;对角线简写用 ``~``(实测可用)
POLYGON_POINT_SEPARATOR = ";"
POLYGON_DIAGONAL_SEPARATOR = "~"
#: 多边形至少要 4 个点(高德 polygon 接口的矩形下限)
POLYGON_MIN_POINTS = 4
#: 驾车 ``strategy`` 默认值(仅为签名兼容,**请求时不传**,见 §6.5)
DEFAULT_DRIVING_STRATEGY = 11

# --------------------------------------------------------------------------- #
# infocode 分派(§6.2 细分表,比契约 §3 更全,以此为准)
# --------------------------------------------------------------------------- #
#: 瞬时可重试:分钟/QPS/日限流、服务器繁忙
TRANSIENT_INFOCODES: frozenset[str] = frozenset({
    "10004", "10014", "10015", "10016", "10019", "10020", "10021", "10029", "10044",
})
#: 永久失败:重试也没用,上层应回落降级链或给可见文案
PERMANENT_INFOCODES: frozenset[str] = frozenset({
    "10001", "10002", "10005", "10009", "10012", "10013", "10041",
    "20000", "20001", "40000", "40002",
})
INFOCODE_TEXT: dict[str, str] = {
    "10000": "请求正常",
    "10001": "key 不正确或已过期",
    "10002": "该 key 未开通对应服务权限",
    "10004": "分钟级请求量超限(限流)",
    "10005": "IP 白名单校验未通过",
    "10009": "key 与平台类型不符(JS key 不能调 Web 服务 REST 接口)",
    "10012": "权限不足(该 key 未获授权)",
    "10013": "key 已被删除",
    "10014": "QPS 超限",
    "10015": "服务器繁忙",
    "10016": "服务器繁忙(返回结果异常)",
    "10019": "QPS 超限",
    "10020": "QPS 超限",
    "10021": "QPS 超限(CUQPS_HAS_EXCEEDED_THE_LIMIT,客户端必须节流)",
    "10029": "日调用量超限(限流)",
    "10041": "账号权限受限(未开通或欠费)",
    "10044": "请求量超限(限流)",
    "20000": "请求参数非法",
    "20001": "缺少必填参数",
    "40000": "配额已耗尽",
    "40002": "服务已到期或欠费停用",
}
MISSING_KEY_MESSAGE = "未配置 WHERE2GO_AMAP_KEY(高德 Web 服务 key)"

#: 归一化 POI 的键名(TASK-9b 入库与 :mod:`services.amap_categories` 都按这套取字段)
POI_KEYS: tuple[str, ...] = (
    "id", "name", "lat", "lng", "type", "typecode",
    "address", "cityname", "adname", "distance_m",
)
#: 地理编码结果的键名(正向列表 / 逆向单条同构)
GEOCODE_KEYS: tuple[str, ...] = (
    "formatted_address", "province", "city", "district", "adcode", "township", "lng", "lat",
)
#: 驾车路线结果的键名
DRIVING_KEYS: tuple[str, ...] = (
    "distance_m", "duration_s", "toll_distance_m", "traffic_lights", "steps_n", "polyline",
)

Number = Union[int, float, str, None]
PolygonInput = Union[str, Sequence[Any]]


# --------------------------------------------------------------------------- #
# 基础解析小工具
# --------------------------------------------------------------------------- #


def _text(value: Any) -> str:
    """把高德字段收敛成字符串:``None`` / 空数组(高德对空值常返回 ``[]``)→ ``""``。"""
    if value is None or isinstance(value, (list, dict, tuple)):
        return ""
    return str(value).strip()


def _number(value: Number) -> Optional[Union[int, float]]:
    """把高德字段(``"175897"`` 这类字符串数字)转成 int/float;拿不到 → ``None``。"""
    text = _text(value)
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if number == int(number):
        return int(number)
    return number


def parse_location(value: Any) -> Optional[tuple[float, float]]:
    """高德坐标串 ``"lng,lat"``(**经度在前**)→ ``(lng, lat)`` float;解析不了返回 ``None``。"""
    if isinstance(value, str):
        parts: Sequence[Any] = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        parts = value
    else:
        return None
    if len(parts) != 2:
        return None
    try:
        lng = float(parts[0])
        lat = float(parts[1])
    except (TypeError, ValueError):
        return None
    if not -180.0 <= lng <= 180.0:
        return None
    if not -90.0 <= lat <= 90.0:
        return None
    return lng, lat


def require_coordinates(lat: Any, lng: Any) -> tuple[float, float]:
    """校验一对坐标(纬度在前,与函数签名一致);非法抛 :class:`ValueError`(中文说明)。"""
    try:
        latitude = float(lat)
        longitude = float(lng)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"坐标必须为数字,收到:lat={lat!r}, lng={lng!r}") from exc
    if not -90.0 <= latitude <= 90.0:
        raise ValueError(f"纬度超出 [-90, 90] 范围:{latitude}")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(f"经度超出 [-180, 180] 范围:{longitude}")
    return latitude, longitude


def format_lnglat(lng: float, lat: float) -> str:
    """拼高德要的 ``"lng,lat"``(**经度在前**,6 位小数)。"""
    latitude, longitude = require_coordinates(lat, lng)
    return f"{longitude:.6f},{latitude:.6f}"


def normalize_multi(value: Union[str, Sequence[Any], None]) -> str:
    """把 ``types`` / ``keywords`` 收敛成高德的多值串(``"a|b"``,去重保序)。"""
    if value is None:
        return ""
    items = [value] if isinstance(value, str) else list(value)
    cleaned: list[str] = []
    for item in items:
        text = _text(item)
        if not text or text in cleaned:
            continue
        cleaned.append(text)
    return MULTI_VALUE_SEPARATOR.join(cleaned)


def normalize_types(types: Union[str, Sequence[Any], None]) -> str:
    """``types`` 参数归一化:``("080106","110000")`` → ``"080106|110000"``。"""
    return normalize_multi(types)


def normalize_keywords(keywords: Union[str, Sequence[Any], None]) -> str:
    """``keywords`` 参数归一化:``("古镇","老街")`` → ``"古镇|老街"``。

    §6.3:``types`` 与 ``keywords`` **建议二选一**(同时给会按关键词排序偏移),
    所以这里只做拼接,不做取舍——由调用方(TASK-9b)决定给哪个。
    """
    return normalize_multi(keywords)


def rectangle_points(min_lng: float, min_lat: float, max_lng: float, max_lat: float) -> list[tuple[float, float]]:
    """矩形 → 4 个 ``(lng, lat)`` 顶点,顺序固定:**左下 / 右下 / 右上 / 左上**(高德 polygon 口径)。"""
    lo_lng = round(float(min_lng), COORD_PRECISION)
    hi_lng = round(float(max_lng), COORD_PRECISION)
    lo_lat = round(float(min_lat), COORD_PRECISION)
    hi_lat = round(float(max_lat), COORD_PRECISION)
    return [(lo_lng, lo_lat), (hi_lng, lo_lat), (hi_lng, hi_lat), (lo_lng, hi_lat)]


def _polygon_pair(text: str) -> tuple[float, float]:
    pair = parse_location(text)
    if pair is None:
        raise ValueError(f"polygon 顶点必须是合法的 'lng,lat'(经度在前),收到:{text!r}")
    return pair


def _polygon_points(polygon: PolygonInput) -> list[tuple[float, float]]:
    """把 polygon 入参收敛成 ``(lng, lat)`` 顶点列表(不校验点数)。"""
    if isinstance(polygon, str):
        text = polygon.strip()
        if not text:
            raise ValueError("polygon 不能为空")
        if POLYGON_DIAGONAL_SEPARATOR in text:
            corners = [part.strip() for part in text.split(POLYGON_DIAGONAL_SEPARATOR) if part.strip()]
            if len(corners) != 2:
                raise ValueError(
                    f"polygon 对角线简写应为 'lng,lat~lng,lat' 两个角点,收到:{polygon!r}"
                )
            first = _polygon_pair(corners[0])
            second = _polygon_pair(corners[1])
            return rectangle_points(
                min(first[0], second[0]), min(first[1], second[1]),
                max(first[0], second[0]), max(first[1], second[1]),
            )
        if POLYGON_POINT_SEPARATOR in text:
            parts = [part.strip() for part in text.split(POLYGON_POINT_SEPARATOR) if part.strip()]
            return [_polygon_pair(part) for part in parts]
        return [_polygon_pair(text)]

    if isinstance(polygon, AbcMapping) or not isinstance(polygon, AbcSequence):
        raise ValueError(f"polygon 应为顶点序列或字符串,收到:{type(polygon).__name__}")

    points: list[tuple[float, float]] = []
    for item in polygon:
        if isinstance(item, str):
            points.append(_polygon_pair(item))
            continue
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"polygon 顶点应为 (lng, lat) 二元组,收到:{item!r}")
        pair = parse_location(tuple(item))
        if pair is None:
            raise ValueError(f"polygon 顶点必须是合法的 'lng,lat'(经度在前),收到:{item!r}")
        points.append(pair)
    return points


def normalize_polygon(polygon: PolygonInput) -> list[tuple[float, float]]:
    """polygon 入参归一化:顶点序列(``lng,lat`` 顺序,≥4 点)或对角线简写字符串。

    对角线简写 ``"119.6,29.8~120.7,30.8"``(实测可用)会展开成 4 个顶点的矩形,
    保证拼出来的查询串统一是 ``;`` 分隔的 4 点(见 :func:`format_polygon`)。
    """
    points = _polygon_points(polygon)
    if len(points) < POLYGON_MIN_POINTS:
        raise ValueError(
            f"polygon 至少需要 {POLYGON_MIN_POINTS} 个顶点(lng,lat),收到 {len(points)} 个:{polygon!r}"
        )
    return points


def format_polygon(points: Sequence[tuple[float, float]]) -> str:
    """顶点列表 → 高德 ``polygon`` 参数串(``"lng,lat;lng,lat;…"``)。"""
    return POLYGON_POINT_SEPARATOR.join(f"{lng:.6f},{lat:.6f}" for lng, lat in points)


def clamp_radius(radius_m: Any) -> int:
    """检索半径(米)钳制:``>50000`` 一律按 :data:`AUTO_MAX_RADIUS_M`(实测被服务端截断)。"""
    try:
        value = float(radius_m)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"radius_m 必须为数字(米),收到:{radius_m!r}") from exc
    return int(max(1, min(round(value), AUTO_MAX_RADIUS_M)))


def clamp_page(page: Any) -> int:
    """页码钳到 ``>=1``(是否超过 :data:`MAX_PAGE` 由调用方短路,不发无效请求)。"""
    try:
        value = int(page)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"page 必须为整数,收到:{page!r}") from exc
    return max(1, value)


def clamp_offset(offset: Any) -> int:
    """每页条数钳到 ``[1, 25]``(v3 实测每页固定 25 条)。"""
    try:
        value = int(offset)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"offset 必须为整数,收到:{offset!r}") from exc
    return int(max(1, min(value, PAGE_SIZE)))


def retry_backoff_s(attempt: int) -> float:
    """第 ``attempt`` 次尝试失败后的退避秒数:2s / 5s(超出取最后一档)。"""
    index = min(max(1, int(attempt)) - 1, len(RETRY_BACKOFF_S) - 1)
    return RETRY_BACKOFF_S[index]


def decode_polyline(text: Any) -> list[list[float]]:
    """高德明文折线 ``"lng,lat;lng,lat;…"`` → ``[[lat, lng], …]``(前端画线要纬度在前)。

    ⚠️ 高德 v3 的 ``steps[].polyline`` **不是** Google 式编码折线,就是分号分隔的明文
    坐标串(经度在前);解析不了的片段直接跳过,不让一段脏数据毁掉整条路线。
    """
    if not isinstance(text, str) or not text.strip():
        return []
    points: list[list[float]] = []
    for chunk in text.split(POLYGON_POINT_SEPARATOR):
        pair = parse_location(chunk.strip())
        if pair is None:
            continue
        lng, lat = pair
        points.append([round(lat, GEOMETRY_COORD_PRECISION), round(lng, GEOMETRY_COORD_PRECISION)])
    return points


def thin_points(
    points: Optional[Sequence[Sequence[float]]],
    max_points: Optional[int] = GEOMETRY_MAX_POINTS,
) -> list[list[float]]:
    """等间隔抽稀折线,**首尾点必留**(画线不缩水);空/``None`` → ``[]``。

    口径与 :func:`services.routes.thin_geometry` 一致(``max_points`` < 2 时原样返回)。
    """
    if not points:
        return []
    coords = [[float(pair[0]), float(pair[1])] for pair in points]
    if max_points is None or max_points < 2 or len(coords) <= max_points:
        return coords
    step = (len(coords) - 1) / (max_points - 1)
    picked = [coords[round(index * step)] for index in range(max_points - 1)]
    picked.append(coords[-1])
    return picked


def grid_polygons(
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    rows: int,
    cols: int,
) -> list[list[tuple[float, float]]]:
    """把包围盒切成 ``rows × cols`` 个矩形(每个 4 点:左下/右下/右上/左上,``lng,lat``)。

    给 TASK-9b 的分格抓取用:单查询最多 200 条 + ``radius`` 被截断在 50km,
    所以环带下限 ≥50km 时改用「包围盒分格 + :func:`search_polygon`」逐格捞、
    再在本地用 haversine 收敛到环带内。

    格子顺序:**行优先、自南向北、自西向东**(``cells[r * cols + c]``),
    便于按 ``fetch_rounds`` 递增扩格;边界格子直接贴包围盒边,不留缝。
    """
    try:
        south = float(min_lat)
        west = float(min_lng)
        north = float(max_lat)
        east = float(max_lng)
        row_count = int(rows)
        col_count = int(cols)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"grid_polygons 参数必须为数字,收到:{min_lat!r}/{min_lng!r}/{max_lat!r}/{max_lng!r}/{rows!r}/{cols!r}") from exc
    if row_count < 1 or col_count < 1:
        raise ValueError(f"rows/cols 必须 ≥1,收到:rows={rows!r}, cols={cols!r}")
    if not -90.0 <= south < north <= 90.0:
        raise ValueError(f"纬度范围非法(需 -90 ≤ min_lat < max_lat ≤ 90):{south} ~ {north}")
    if not -180.0 <= west < east <= 180.0:
        raise ValueError(f"经度范围非法(需 -180 ≤ min_lng < max_lng ≤ 180):{west} ~ {east}")

    lat_step = (north - south) / row_count
    lng_step = (east - west) / col_count
    cells: list[list[tuple[float, float]]] = []
    for row in range(row_count):
        cell_south = south + lat_step * row
        cell_north = north if row == row_count - 1 else south + lat_step * (row + 1)
        for col in range(col_count):
            cell_west = west + lng_step * col
            cell_east = east if col == col_count - 1 else west + lng_step * (col + 1)
            cells.append(rectangle_points(cell_west, cell_south, cell_east, cell_north))
    return cells


# --------------------------------------------------------------------------- #
# 节流 / key / 端点(照 photon.PhotonClient._throttle 的写法,但状态是**进程级**)
# --------------------------------------------------------------------------- #
#: 时钟与睡眠做成模块级可替换属性,单测 monkeypatch 掉就不会真的等 0.6s
_CLOCK: Callable[[], float] = time.monotonic
_SLEEP: Callable[[float], None] = time.sleep

_throttle_lock = threading.Lock()
_last_request_at = float("-inf")

_session_lock = threading.Lock()
_default_session: Optional[Any] = None


def min_interval_s(environ: Optional[Mapping[str, str]] = None) -> float:
    """单进程最小请求间隔(秒):``WHERE2GO_AMAP_MIN_INTERVAL_S`` 可覆盖,非法值回退默认。"""
    env = os.environ if environ is None else environ
    raw = str(env.get(ENV_MIN_INTERVAL) or "").strip()
    if not raw:
        return MIN_REQUEST_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        return MIN_REQUEST_INTERVAL_S
    return max(0.0, value)


def reset_throttle() -> None:
    """复位节流时间戳(单测 / 长时间空闲后立刻发第一个请求时用)。"""
    global _last_request_at
    with _throttle_lock:
        _last_request_at = float("-inf")


def _throttle(environ: Optional[Mapping[str, str]] = None) -> None:
    """限速:距上次请求不足 :func:`min_interval_s` 就先睡够(实测第 3 个连发即 10021)。"""
    interval = min_interval_s(environ)
    if interval <= 0:
        return
    global _last_request_at
    with _throttle_lock:
        now = _CLOCK()
        wait = _last_request_at + interval - now
        _last_request_at = now + max(wait, 0.0)
    if wait > 0:
        _SLEEP(wait)


def resolve_endpoint(environ: Optional[Mapping[str, str]] = None) -> str:
    """服务前缀:``WHERE2GO_AMAP_ENDPOINT`` 覆盖 > :data:`DEFAULT_ENDPOINT`(v3,勿改 v5)。"""
    env = os.environ if environ is None else environ
    return str(env.get(ENV_ENDPOINT) or DEFAULT_ENDPOINT).strip().rstrip("/")


def resolve_key(environ: Optional[Mapping[str, str]] = None) -> str:
    """高德 **Web 服务** key;没配就抛中文 :class:`DataSourceError`(上层据此回落降级链)。

    ⚠️ JS API 的 key(``WHERE2GO_AMAP_JS_KEY``,前端地图用)调 REST 会返回
    ``infocode=10009`` 平台不符 —— 两者**不能混用**。
    """
    env = os.environ if environ is None else environ
    key = str(env.get(ENV_AMAP_KEY) or "").strip()
    if not key:
        raise DataSourceError(SOURCE_NAME, MISSING_KEY_MESSAGE)
    return key


def _resolve_session(session: Optional[Any]) -> Any:
    """注入的 session 优先;否则用进程内共享的默认 session(代理口径见 ``_common``)。"""
    global _default_session
    if session is not None:
        return session
    with _session_lock:
        if _default_session is None:
            # "amap" 已在 _common.DEFAULT_SOURCE_PROXY 里注册为直连(国内可达,走代理反而慢)
            _default_session = build_session(USER_AGENT, source=SOURCE_NAME)
        return _default_session


def check_status(payload: Any, *, action: str = "高德请求") -> Any:
    """按 ``status``/``infocode`` 判成败(**HTTP 码恒 200,不能按 HTTP 判错**)。

    * ``status == "1"`` → 原样返回 payload;
    * 限流/繁忙(:data:`TRANSIENT_INFOCODES`)→ :class:`TransientDataSourceError`(可退避重试);
    * key/权限/参数/配额(:data:`PERMANENT_INFOCODES`)→ :class:`DataSourceError`(重试无用);
    * 其余未知 ``infocode`` → :class:`DataSourceError`,并在文案里带上 infocode 原文。
    """
    if not isinstance(payload, dict):
        raise DataSourceError(
            SOURCE_NAME, f"{action}响应格式异常(应为 JSON 对象):{type(payload).__name__}"
        )
    status = _text(payload.get("status"))
    if status == STATUS_OK:
        return payload

    infocode = _text(payload.get("infocode"))
    info = _text(payload.get("info"))
    known = INFOCODE_TEXT.get(infocode, "")
    reason = f"{known}(info={info})" if known and info else (known or info or "未说明原因")
    head = (
        f"{action}失败:高德返回 status={status or '空'} · infocode={infocode or '空'}"
    )
    if infocode in TRANSIENT_INFOCODES:
        raise TransientDataSourceError(SOURCE_NAME, f"{head} · {reason}(瞬时,可退避重试)")
    raise DataSourceError(SOURCE_NAME, f"{head} · {reason}")


def _request(
    path: str,
    params: Mapping[str, Any],
    *,
    environ: Optional[Mapping[str, str]] = None,
    session: Optional[Any] = None,
    timeout: Optional[float] = None,
    action: str = "高德请求",
) -> Any:
    """发一次 v3 请求:节流 → 带 key 拼参 → 瞬时错误退避重试(2s/5s,最多 3 次)。"""
    env = dict(os.environ if environ is None else environ)
    key = resolve_key(env)
    url = f"{resolve_endpoint(env)}{path}"
    query: dict[str, Any] = {"key": key}
    query.update({name: value for name, value in params.items() if value is not None and value != ""})
    resolved_session = _resolve_session(session)
    resolved_timeout = normalize_timeout(timeout)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        _throttle(env)
        try:
            payload = http_json(
                resolved_session,
                url,
                source=SOURCE_NAME,
                params=query,
                timeout=resolved_timeout,
            )
            return check_status(payload, action=action)
        except TransientDataSourceError:
            if attempt >= MAX_ATTEMPTS:
                raise
            _SLEEP(retry_backoff_s(attempt))
    raise DataSourceError(SOURCE_NAME, f"{action}失败:重试次数用尽仍无结果")  # pragma: no cover


# --------------------------------------------------------------------------- #
# 响应解析
# --------------------------------------------------------------------------- #


def parse_poi(raw: Any) -> Optional[dict[str, Any]]:
    """一条高德 POI → 归一化 dict(:data:`POI_KEYS`);脏数据(缺 id/name/坐标)返回 ``None``。

    ``distance`` 只有 ``place/around`` 才给(``place/polygon`` 常返回 ``[]``)→ 无则 ``None``。
    """
    if not isinstance(raw, dict):
        return None
    poi_id = _text(raw.get("id"))
    name = _text(raw.get("name"))
    pair = parse_location(raw.get("location"))
    if not poi_id or not name or pair is None:
        return None
    lng, lat = pair
    return {
        "id": poi_id,
        "name": name,
        "lat": round(lat, COORD_PRECISION),
        "lng": round(lng, COORD_PRECISION),
        "type": _text(raw.get("type")),
        "typecode": _text(raw.get("typecode")),
        "address": _text(raw.get("address")),
        "cityname": _text(raw.get("cityname")),
        "adname": _text(raw.get("adname")),
        "distance_m": _number(raw.get("distance")),
    }


def parse_pois(payload: Any, *, action: str = "POI 检索") -> list[dict[str, Any]]:
    """``pois`` 数组 → 归一化 POI 列表(**保持服务端返回顺序**,即距离/相关度序)。"""
    if not isinstance(payload, dict):
        raise DataSourceError(
            SOURCE_NAME, f"{action}响应格式异常(应为 JSON 对象):{type(payload).__name__}"
        )
    raw = payload.get("pois")
    if not isinstance(raw, list):
        return []
    places: list[dict[str, Any]] = []
    for item in raw:
        poi = parse_poi(item)
        if poi is not None:
            places.append(poi)
    return places


def _geocode_item(raw: Any, *, action: str) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    pair = parse_location(raw.get("location"))
    if pair is None:
        raise DataSourceError(
            SOURCE_NAME,
            f"{action}响应的 location 无法解析(应为 'lng,lat' 字符串):{raw.get('location')!r}",
        )
    lng, lat = pair
    return {
        "formatted_address": _text(raw.get("formatted_address")),
        "province": _text(raw.get("province")),
        "city": _text(raw.get("city")),
        "district": _text(raw.get("district")),
        "adcode": _text(raw.get("adcode")),
        "township": _text(raw.get("township")),
        "lng": round(lng, COORD_PRECISION),
        "lat": round(lat, COORD_PRECISION),
    }


def parse_geocodes(payload: Any, *, action: str = "地理编码") -> list[dict[str, Any]]:
    """``geocodes`` 数组 → :data:`GEOCODE_KEYS` 列表;无结果 ``[]``。

    直辖市的 ``city`` 字段高德会返回 ``[]``(空数组),这里统一收敛成 ``""``。
    """
    if not isinstance(payload, dict):
        raise DataSourceError(
            SOURCE_NAME, f"{action}响应格式异常(应为 JSON 对象):{type(payload).__name__}"
        )
    raw = payload.get("geocodes")
    if not isinstance(raw, list):
        return []
    results: list[dict[str, Any]] = []
    for item in raw:
        parsed = _geocode_item(item, action=action)
        if parsed is not None:
            results.append(parsed)
    return results


def parse_regeocode(payload: Any, *, lat: float, lng: float, action: str = "逆地理编码") -> dict[str, Any]:
    """``regeocode`` 对象 → 单条 :data:`GEOCODE_KEYS`(坐标沿用请求值);无结果 ``{}``。

    高德的 ``regeocode`` 在查不到时返回 ``[]``(空数组)而不是对象,所以要判类型。
    """
    if not isinstance(payload, dict):
        raise DataSourceError(
            SOURCE_NAME, f"{action}响应格式异常(应为 JSON 对象):{type(payload).__name__}"
        )
    raw = payload.get("regeocode")
    if not isinstance(raw, dict):
        return {}
    component = raw.get("addressComponent")
    if not isinstance(component, dict):
        component = {}
    latitude, longitude = require_coordinates(lat, lng)
    return {
        "formatted_address": _text(raw.get("formatted_address")),
        "province": _text(component.get("province")),
        "city": _text(component.get("city")),
        "district": _text(component.get("district")),
        "adcode": _text(component.get("adcode")),
        "township": _text(component.get("township")),
        "lng": round(longitude, COORD_PRECISION),
        "lat": round(latitude, COORD_PRECISION),
    }


def parse_driving(payload: Any, *, with_geometry: bool = True, action: str = "驾车路线") -> dict[str, Any]:
    """``route.paths[0]`` → :data:`DRIVING_KEYS`(多条 paths 一律取第一条,§6.5)。

    ``tolls`` 恒 ``0``、``cost`` 恒 ``null``(个人 key 不出数值)→ **刻意不透出**;
    过路费口径由上层用 ``toll_distance_m × 区域费率`` 估算(§1.5)。
    """
    if not isinstance(payload, dict):
        raise DataSourceError(
            SOURCE_NAME, f"{action}响应格式异常(应为 JSON 对象):{type(payload).__name__}"
        )
    route = payload.get("route")
    if not isinstance(route, dict):
        raise DataSourceError(
            SOURCE_NAME, f"{action}响应缺少 route 对象:{type(route).__name__}"
        )
    paths = route.get("paths")
    if not isinstance(paths, list) or not paths:
        raise DataSourceError(SOURCE_NAME, f"{action}响应没有可用路径(route.paths 为空),起终点可能不可达")
    first = paths[0]
    if not isinstance(first, dict):
        raise DataSourceError(
            SOURCE_NAME, f"{action}响应的 paths[0] 格式异常(应为 JSON 对象):{type(first).__name__}"
        )

    raw_steps = first.get("steps")
    steps = [step for step in raw_steps if isinstance(step, dict)] if isinstance(raw_steps, list) else []
    polyline: list[list[float]] = []
    if with_geometry:
        collected: list[list[float]] = []
        for step in steps:
            collected.extend(decode_polyline(step.get("polyline")))
        polyline = thin_points(collected)

    return {
        "distance_m": _number(first.get("distance")),
        "duration_s": _number(first.get("duration")),
        "toll_distance_m": _number(first.get("toll_distance")),
        "traffic_lights": _number(first.get("traffic_lights")),
        "steps_n": len(steps),
        "polyline": polyline,
    }


# --------------------------------------------------------------------------- #
# 公开 API(全部支持 environ= / session= 注入)
# --------------------------------------------------------------------------- #


def geocode(
    address: str,
    *,
    city: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    session: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """正向地理编码:``GET /geocode/geo`` → :data:`GEOCODE_KEYS` 列表(按相关度,无结果 ``[]``)。

    ``city`` 可选(给中文城市名或 adcode 能提高重名地名的命中率)。
    """
    text = str(address or "").strip()
    if not text:
        raise ValueError("geocode 的 address 不能为空")
    params = {"address": text, "city": _text(city)}
    payload = _request(
        GEOCODE_PATH, params, environ=environ, session=session, action=f"地理编码 {text!r}"
    )
    return parse_geocodes(payload, action=f"地理编码 {text!r}")


def reverse_geocode(
    lat: float,
    lng: float,
    *,
    environ: Optional[Mapping[str, str]] = None,
    session: Optional[Any] = None,
) -> dict[str, Any]:
    """逆地理编码:``GET /geocode/regeo`` → 单条 :data:`GEOCODE_KEYS`(无结果 ``{}``)。"""
    latitude, longitude = require_coordinates(lat, lng)
    params = {
        "location": format_lnglat(longitude, latitude),
        "extensions": EXTENSIONS_BASE,
    }
    payload = _request(
        REGEO_PATH, params, environ=environ, session=session,
        action=f"逆地理编码 ({latitude:.6f},{longitude:.6f})",
    )
    return parse_regeocode(payload, lat=latitude, lng=longitude, action="逆地理编码")


def search_around(
    lat: float,
    lng: float,
    *,
    radius_m: Any = AUTO_MAX_RADIUS_M,
    types: Union[str, Sequence[Any], None] = None,
    keywords: Union[str, Sequence[Any], None] = None,
    page: int = 1,
    offset: int = PAGE_SIZE,
    environ: Optional[Mapping[str, str]] = None,
    session: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """圆形范围检索:``GET /place/around`` → 归一化 POI 列表(**已按距离排序**)。

    * ``radius_m`` 超过 :data:`AUTO_MAX_RADIUS_M` 会被钳到 50000(实测服务端截断);
    * ``page`` 超过 :data:`MAX_PAGE`(8)直接返回 ``[]`` —— 实测 ``page>=9`` 恒空,
      不发这次无效请求(也省一次配额);
    * 单查询上限 :data:`MAX_ROWS_PER_QUERY`(200)条,拿不满就分格走 :func:`search_polygon`。
    """
    latitude, longitude = require_coordinates(lat, lng)
    resolved_page = clamp_page(page)
    if resolved_page > MAX_PAGE:
        return []
    params = {
        "location": format_lnglat(longitude, latitude),
        "radius": clamp_radius(radius_m),
        "types": normalize_types(types),
        "keywords": normalize_keywords(keywords),
        "page": resolved_page,
        "offset": clamp_offset(offset),
        "extensions": EXTENSIONS_BASE,
    }
    payload = _request(
        SEARCH_AROUND_PATH, params, environ=environ, session=session,
        action=f"周边检索 ({latitude:.6f},{longitude:.6f}) r={params['radius']}m",
    )
    return parse_pois(payload, action="周边检索")


def search_polygon(
    polygon: PolygonInput,
    *,
    types: Union[str, Sequence[Any], None] = None,
    keywords: Union[str, Sequence[Any], None] = None,
    page: int = 1,
    offset: int = PAGE_SIZE,
    environ: Optional[Mapping[str, str]] = None,
    session: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """多边形范围检索:``GET /place/polygon`` → 归一化 POI 列表。

    ``polygon`` 收 ``(lng, lat)`` 顶点序列(**经度在前**,≥4 点)或对角线简写字符串
    ``"119.6,29.8~120.7,30.8"``;查询串用 ``";"`` 分隔顶点。band ≥50km 或需要绕开
    ``radius`` 截断时用它(配 :func:`grid_polygons` 分格)。
    """
    points = normalize_polygon(polygon)
    resolved_page = clamp_page(page)
    if resolved_page > MAX_PAGE:
        return []
    params = {
        "polygon": format_polygon(points),
        "types": normalize_types(types),
        "keywords": normalize_keywords(keywords),
        "page": resolved_page,
        "offset": clamp_offset(offset),
        "extensions": EXTENSIONS_BASE,
    }
    payload = _request(
        SEARCH_POLYGON_PATH, params, environ=environ, session=session,
        action=f"多边形检索 ({len(points)} 顶点)",
    )
    return parse_pois(payload, action="多边形检索")


def driving(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    *,
    strategy: int = DEFAULT_DRIVING_STRATEGY,
    with_geometry: bool = True,
    environ: Optional[Mapping[str, str]] = None,
    session: Optional[Any] = None,
) -> dict[str, Any]:
    """驾车路线:``GET /direction/driving`` → :data:`DRIVING_KEYS`。

    ⚠️ ``strategy`` **只为签名兼容而保留(默认 11),请求时刻意不传** —— §6.5 实测
    传了可能返回多条 ``paths``;万一仍返回多条,一律取 ``paths[0]``。
    ``with_geometry=False`` 时只取 ``extensions=base``(省流量),``polyline`` 为 ``[]``。
    """
    o_lat, o_lng = require_coordinates(origin_lat, origin_lng)
    d_lat, d_lng = require_coordinates(dest_lat, dest_lng)
    params = {
        "origin": format_lnglat(o_lng, o_lat),
        "destination": format_lnglat(d_lng, d_lat),
        "extensions": EXTENSIONS_ALL if with_geometry else EXTENSIONS_BASE,
    }
    payload = _request(
        DRIVING_PATH, params, environ=environ, session=session, action="驾车路线"
    )
    return parse_driving(payload, with_geometry=with_geometry, action="驾车路线")
