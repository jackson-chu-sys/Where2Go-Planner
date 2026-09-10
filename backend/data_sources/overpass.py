"""Overpass API:按 tag 检索周边 POI(目的地库冷启动)。

真实响应(2026-09-08 实测)::

    {"version": 0.6, "generator": "Overpass API 0.7.62.11 ...",
     "elements": [
       {"type": "node", "id": 277142345, "lat": 39.9454069, "lon": 116.1562855,
        "tags": {"ele": "309", "name": "福寿岭", "natural": "peak"}},
       {"type": "way", "id": 123, "center": {"lat": 39.92, "lon": 116.39},
        "tags": {"leisure": "park", "name": "..."}}
     ]}

要点(以真实 API 为准):

* node 直接带 ``lat``/``lon``,way/relation 只有在 ``out center`` 下才给 ``center``,
  解析时两者都要兼容;
* 部分 POI(如无名山峰)没有 ``name`` tag,因此名字按 ``name`` → ``name:zh`` →
  ``name:en`` 依次兜底,并可用 ``require_name`` 过滤;
* ``around`` 的结果**不是**按距离排序(实测按 quadtile/id),所以客户端按大圆距离重排;
* 阶段1a 起 ``parse_*``/``nearby_places`` 支持 ``with_id=True``,额外返回
  ``osm_type``/``osm_id``(入库按 OSM 身份 ``(type, id)`` 防重需要);
  默认关闭,保持阶段0 POC 的返回形状 ``{"lat", "lng", "name", "tags"}`` 不变;
* 阶段1b 起支持**分组查询** :func:`build_grouped_query` /
  :meth:`OverpassClient.nearby_places_grouped`:一次 HTTP 请求里放多段
  ``( 选择器并集 ); out center N;``,每组一个独立配额。四分类并集检索必须这样做,
  否则 ``sport=*`` / 餐厅这类高频 tag 会把总量配额刷爆,山峰/古镇一条都取不到;
  同一实体被多组命中时由 :func:`services.classify.dedupe_places` 按 ``(type, id)`` 去重;
* TASK-1d 起分组查询支持**环形差集**(:func:`build_grouped_ring_query` /
  :meth:`OverpassClient.nearby_places_ring` 的 ``inner_radius_m``):每组语句变成
  ``( 上限圆并集; - 下限圆并集; ); out center N;``,**配额只花在环内地物上**。
  在此之前远距离分段(200-300 / 300-500 km)只按 band **上限半径** ``around`` 取数,
  每组配额被 0~下限 的近处 POI 占满,本地 haversine 收敛到环内后只剩个位数
  (实测北京 200-300 入库 1 条、上海 5 条,而 50-100 有 135 条);
  差集要在公共实例上同时扫两个圆,实测比单圆慢一倍以上(上海 200-300 单组
  小城古镇 24-33s、运动 57s、人文 86s、美食 71s),因此服务端/客户端超时另给一档
  (:data:`DEFAULT_RING_QUERY_TIMEOUT` / :data:`DEFAULT_RING_REQUEST_TIMEOUT_S`);
  ``inner_radius_m`` 缺省或为 0 时退化成普通 ``around`` 查询,输出与阶段1b 逐字一致;
* 环形差集**不能**像单圆那样六组塞进一次请求:差集要在服务端同时物化上限圆与下限圆
  两个集合,六组合并实测直接撞公共实例的单查询内存上限(maps.mail.ru 回
  ``runtime error: Query run out of memory using about 2048 MB of RAM`` 且 elements 为空,
  overpass-api.de 则 504 拒收),所以 :meth:`OverpassClient.nearby_places_ring`
  改成**每组一次请求**(各自走端点链 + 重试);
* 单组仍可能太重(实测**滑雪场**组四个选择器里有两个是正则,300 km 外圈在
  overpass-api.de 与 maps.mail.ru 都撞 2048 MB 上限,各花 88-145s 才回 OOM),
  这时把该组**按选择器拆开**逐个重发同样的差集(每条实测 27-58s 跑通),
  拆分后仍受该组配额约束(合并 → ``(type, id)`` 去重 → 由近及远取前 ``budget`` 条);
  OOM 之类的**致命** remark 直接抛 :class:`OverpassRuntimeError` 而不换端点
  (三个公共实例的单查询内存上限都是 2048 MB,换端点只会再等一遍),
  超时 remark 仍按"只回部分分组"收下,口径不变;
* 分组并集是**冷启动批量抓取**(一个 (城市, band) 只跑一次,之后读 SQLite),实测
  20-110s,远超交互请求的 20s 上限,因此 :meth:`OverpassClient.execute` 支持按次
  覆盖 timeout,分组路径用 :data:`DEFAULT_GROUP_REQUEST_TIMEOUT_S`;
  ``nearby_places``(交互/POC 路径)仍走 ``normalize_timeout`` 的 20s 上限,行为不变;
* 公共实例经常返回 ``504 + HTML``("The server is probably too busy"),实测
  ``overpass-api.de`` 繁忙时 ``z.overpass-api.de`` / ``maps.mail.ru`` 仍可用,
  因此这里做**端点链 + 重试**降级;``overpass.osm.ch`` 实测无数据、
  ``overpass.private.coffee`` 数据陈旧数月,均不纳入默认链。
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Iterable, Optional, Union

from ._common import (
    USER_AGENT,
    DataSourceError,
    TransientDataSourceError,
    build_session,
    http_json,
    normalize_timeout,
)

SOURCE_NAME = "Overpass"
DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"
FALLBACK_ENDPOINTS: tuple[str, ...] = (
    "https://z.overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
ENV_ENDPOINT = "WHERE2GO_OVERPASS_ENDPOINT"

ELEMENT_TYPES = ("node", "way", "relation")
ELEMENT_TYPES_ALIASES = {"nwr": ELEMENT_TYPES, "nw": ("node", "way")}
DEFAULT_QUERY_TIMEOUT = 18
# 分组并集查询的**服务端** Overpass QL 超时:比客户端 HTTP 超时略小,
# 服务端先到点就带着已完成分组的部分结果返回(remark 记超时),而不是让整个请求失败。
DEFAULT_GROUP_QUERY_TIMEOUT = 120
# 分组并集查询的**客户端** HTTP 超时:冷启动批量抓取专用,不受交互 20s 上限约束
# (实测上海 100km 六组并集 20-110s);服务端 Overpass QL 超时仍由 query_timeout 控制。
DEFAULT_GROUP_REQUEST_TIMEOUT_S = 150.0
# 环形差集(上限圆 - 下限圆)要在服务端扫两个圆,实测比单圆并集慢一倍以上,
# 因此超时各放宽一档;仍不超过 MAX_GROUP_REQUEST_TIMEOUT_S。
DEFAULT_RING_QUERY_TIMEOUT = 240
DEFAULT_RING_REQUEST_TIMEOUT_S = 270.0
MAX_GROUP_REQUEST_TIMEOUT_S = 300.0
DEFAULT_LIMIT = 20
FETCH_FACTOR = 12
MIN_FETCH = 60
MAX_FETCH = 400
EARTH_RADIUS_KM = 6371.0088
DETAIL_LEN = 200
DETAIL_KEEP = 3
# Overpass 用响应里的 ``remark`` 汇报服务端运行期错误。超时(只回部分分组)按现有口径
# 收下;其余(OOM 等)意味着**一条都没取到**,必须把查询拆小重发,而不是当成"环内真的
# 没有 POI"(见 :class:`OverpassRuntimeError`)。
RUNTIME_ERROR_REMARK = "runtime error"
TIMEOUT_REMARK = "query timed out"
COORD_PRECISION = 7

Tags = Union[Mapping[str, Any], str, Sequence[Union[Mapping[str, Any], str]]]


class OverpassRuntimeError(DataSourceError):
    """服务端回了**致命** ``remark``(OOM 之类):同一条查询换端点也会再撞一遍。

    实测 overpass-api.de 与 maps.mail.ru 的单查询内存上限都是 2048 MB,同一句
    ``runtime error: Query run out of memory using about 2048 MB of RAM``;换端点只是
    白等一两分钟。:meth:`OverpassClient.nearby_places_ring` 收到它就把该组
    **按选择器拆开**重发(每条查询小得多),拆无可拆才向调用方抛错。
    """


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两点间大圆距离(公里),用于把 POI 按离起点远近排序。"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def _as_int(value: Any) -> Optional[int]:
    """Overpass 的 ``id`` 归一成 int;缺失或非法返回 None(由上层决定兜底身份)。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _escape(value: Any) -> str:
    text = str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _selector(mapping: Mapping[str, Any]) -> str:
    parts = []
    for key, value in mapping.items():
        if value is None:
            parts.append(f'["{_escape(key)}"]')
        else:
            parts.append(f'["{_escape(key)}"="{_escape(value)}"]')
    if not parts:
        raise ValueError("Overpass tag 选择器不能为空字典")
    return "".join(parts)


def tags_to_selectors(tags: Tags) -> list[str]:
    """把 tags 归一成 Overpass 选择器列表(多组之间是并集 OR)。

    支持三种写法::

        {"tourism": "attraction"}                              # 单组 tag
        [{"natural": "peak"}, {"natural": "waterfall"}]        # 多组 tag(并集)
        '["natural"="peak"]["name"]'                           # 原始选择器字符串
    """
    if isinstance(tags, Mapping):
        return [_selector(tags)]
    if isinstance(tags, str):
        text = tags.strip()
        if not text.startswith("["):
            raise ValueError(f"原始 Overpass 选择器应以 '[' 开头,收到:{tags!r}")
        return [text]
    if isinstance(tags, Sequence):
        selectors = [tags_to_selectors(item)[0] for item in tags]
        if not selectors:
            raise ValueError("Overpass tags 不能为空")
        return selectors
    raise ValueError(f"不支持的 tags 类型:{type(tags).__name__}")


def normalize_element_types(element_types: Union[str, Iterable[str]]) -> tuple[str, ...]:
    """归一 Overpass 元素类型:``"nwr"`` / ``"node"`` / ``["node", "way"]``。"""
    if isinstance(element_types, str):
        key = element_types.strip().lower()
        if key in ELEMENT_TYPES_ALIASES:
            return ELEMENT_TYPES_ALIASES[key]
        candidates = [key]
    else:
        candidates = [str(item).strip().lower() for item in element_types]
    unknown = [item for item in candidates if item not in ELEMENT_TYPES]
    if unknown or not candidates:
        raise ValueError(f"element_types 只能是 node/way/relation/nwr/nw,收到:{element_types!r}")
    return tuple(candidates)


def resolve_fetch_limit(limit: Optional[int]) -> int:
    """服务端 ``out`` 的取数条数:因为结果不按距离排序,需要多取再在本地排序截断。"""
    if limit is None:
        return MAX_FETCH
    return max(MIN_FETCH, min(MAX_FETCH, int(limit) * FETCH_FACTOR))


def build_query(
    lat: float,
    lng: float,
    radius_m: float,
    tags: Tags,
    *,
    limit: Optional[int] = DEFAULT_LIMIT,
    element_types: Union[str, Iterable[str]] = "nwr",
    query_timeout: float = DEFAULT_QUERY_TIMEOUT,
) -> str:
    """构造 Overpass QL 查询语句(``out center`` 以便 way/relation 也有坐标)。"""
    radius = int(radius_m)
    if radius <= 0:
        raise ValueError(f"radius_m 必须为正数(米),收到:{radius_m!r}")
    selectors = tags_to_selectors(tags)
    types = normalize_element_types(element_types)
    around = f"around:{radius},{float(lat):.6f},{float(lng):.6f}"
    statements = "\n".join(f"  {etype}{selector}({around});" for selector in selectors for etype in types)
    return (
        f"[out:json][timeout:{int(query_timeout)}];\n"
        f"(\n{statements}\n);\n"
        f"out center {resolve_fetch_limit(limit)};\n"
    )


def resolve_group_limit(limit: Optional[int]) -> int:
    """分组查询里每组的服务端取数条数(配额):收敛到 [1, 400]。

    与 :func:`resolve_fetch_limit` 的区别:分组配额是**最终要多少条**,不再乘
    ``FETCH_FACTOR``(本地不做二次截断,``parse_places(limit=None)`` 全收)。
    """
    if limit is None:
        return MAX_FETCH
    return max(1, min(MAX_FETCH, int(limit)))


def build_grouped_query(
    lat: float,
    lng: float,
    radius_m: float,
    groups: Sequence[Mapping[str, Any]],
    *,
    require_name: bool = True,
    query_timeout: float = DEFAULT_GROUP_QUERY_TIMEOUT,
) -> str:
    """构造**多组并集**的 Overpass QL:一次请求查完多类 tag,每组独立 ``out`` 配额。

    ``groups`` 形如 ``[{"tags": [...], "element_types": "nwr", "budget": 120}, ...]``
    (``limit`` 是 ``budget`` 的别名)。``require_name=True`` 时给每个选择器再挂
    ``["name"]``,让服务端就滤掉无名地物(比取回 400 条再本地过滤有效得多)。

    这是**单圆**查询(0 ~ ``radius_m``),只适合下限为 0 的分段;远距离分段(下限 > 0)
    请用 :func:`build_grouped_ring_query`,否则每组配额都会被 0~下限 的近处 POI 吃光。

    生成的语句形如::

        [out:json][timeout:20];
        (
          nwr["piste:type"]["name"](around:100000,31.230400,121.473700);
        );
        out center 80;
        (
          nwr["sport"]["name"](around:100000,31.230400,121.473700);
        );
        out center 100;
    """
    return _join_grouped_blocks(
        _group_blocks(lat, lng, radius_m, groups, require_name=require_name), query_timeout
    )


def build_grouped_ring_query(
    lat: float,
    lng: float,
    outer_radius_m: float,
    inner_radius_m: float,
    groups: Sequence[Mapping[str, Any]],
    *,
    require_name: bool = True,
    query_timeout: float = DEFAULT_RING_QUERY_TIMEOUT,
) -> str:
    """**环形差集**版分组并集:每组只取 ``[inner, outer]`` 环内的地物(TASK-1d)。

    远距离分段(200-300 / 300-500 km)必须走这条:单圆 ``around:上限`` 的每组配额会被
    0~下限 的近处 POI 占满,本地再按环过滤就只剩个位数。差集把近处那一圈在**服务端**
    减掉,配额全部落在环内;分组并集、去重键 ``(type, id)``、归类优先级口径都不变。

    ``inner_radius_m`` 为 0 / None 时退化成普通 ``around`` 查询,输出与
    :func:`build_grouped_query` 逐字一致;``query_timeout`` 默认放宽一档
    (:data:`DEFAULT_RING_QUERY_TIMEOUT`),因为服务端要同时扫两个圆。
    """
    return _join_grouped_blocks(
        _group_blocks(
            lat,
            lng,
            outer_radius_m,
            groups,
            inner_radius_m=inner_radius_m,
            require_name=require_name,
        ),
        query_timeout,
    )


def _join_grouped_blocks(blocks: Sequence[str], query_timeout: float) -> str:
    """把各组语句拼成**一次**请求(整条查询只有一个 ``[out:json]`` 头)。"""
    return f"[out:json][timeout:{int(query_timeout)}];\n" + "\n".join(blocks) + "\n"


def _around(radius_m: int, lat: float, lng: float) -> str:
    """``around`` 的半径 + 圆心片段(坐标固定 6 位小数,便于构造出可复现的语句)。"""
    return f"around:{radius_m},{float(lat):.6f},{float(lng):.6f}"


def _group_statements(
    selectors: Sequence[str], types: Sequence[str], around: str, *, indent: str = "  "
) -> str:
    """选择器 × 元素类型 展开成多行 ``nwr[tag](around:...);`` 语句。"""
    return "\n".join(f"{indent}{etype}{selector}({around});" for selector in selectors for etype in types)


def _group_blocks(
    lat: float,
    lng: float,
    radius_m: float,
    groups: Sequence[Mapping[str, Any]],
    *,
    inner_radius_m: Optional[float] = None,
    require_name: bool = True,
) -> list[str]:
    """每个分组一段 Overpass QL;``inner_radius_m > 0`` 时该段是**环形差集**。

    单圆(TASK-1b 口径)::

        (
          nwr["piste:type"]["name"](around:100000,31.230400,121.473700);
        );
        out center 80;

    环形差集(TASK-1d,配额只花在环内)::

        (
          (
            nwr["piste:type"]["name"](around:300000,31.230400,121.473700);
          );
          -
          (
            nwr["piste:type"]["name"](around:200000,31.230400,121.473700);
          );
        );
        out center 80;

    ``inner_radius_m`` 缺省 / 为 0 → 单圆;负数或 ``>= radius_m`` → :class:`ValueError`。
    """
    outer = int(radius_m)
    if outer <= 0:
        raise ValueError(f"radius_m 必须为正数(米),收到:{radius_m!r}")
    if not groups:
        raise ValueError("groups 不能为空")
    inner = int(inner_radius_m or 0)
    if inner < 0:
        raise ValueError(f"inner_radius_m 不能为负数(米),收到:{inner_radius_m!r}")
    if inner >= outer:
        raise ValueError(
            f"inner_radius_m 必须小于 radius_m(否则环形差集为空集),"
            f"收到:inner={inner_radius_m!r} / radius={radius_m!r}"
        )
    outer_around = _around(outer, lat, lng)
    inner_around = _around(inner, lat, lng) if inner > 0 else None

    blocks: list[str] = []
    for group in groups:
        selectors = tags_to_selectors(group.get("tags"))
        if require_name:
            selectors = [f'{selector}["name"]' for selector in selectors]
        types = normalize_element_types(group.get("element_types") or "nwr")
        budget = resolve_group_limit(group.get("budget", group.get("limit")))
        if inner_around is None:
            statements = _group_statements(selectors, types, outer_around)
            blocks.append(f"(\n{statements}\n);\nout center {budget};")
            continue
        blocks.append(
            f"(\n"
            f"  (\n{_group_statements(selectors, types, outer_around, indent='    ')}\n  );\n"
            f"  -\n"
            f"  (\n{_group_statements(selectors, types, inner_around, indent='    ')}\n  );\n"
            f");\nout center {budget};"
        )
    return blocks


def runtime_error_remark(payload: Any) -> str:
    """取出响应里的**致命** ``remark``(OOM 之类);超时与正常响应返回空串。

    实测样本(maps.mail.ru,六组环形差集一次请求)::

        {"remark": "runtime error: Query run out of memory using about 2048 MB of RAM.",
         "elements": []}

    这类响应是 HTTP 200 + 合法 JSON,光看状态码会误判成"环内没有 POI";
    而 ``runtime error: Query timed out ...`` 是服务端到点中止、**仍带已完成分组的结果**,
    按既有口径收下即可,不算致命。
    """
    if not isinstance(payload, dict):
        return ""
    remark = str(payload.get("remark") or "").strip()
    lowered = remark.lower()
    if RUNTIME_ERROR_REMARK not in lowered or TIMEOUT_REMARK in lowered:
        return ""
    return remark


def _merge_split_rows(
    rows: Iterable[dict[str, Any]], budget: int, lat: float, lng: float
) -> list[dict[str, Any]]:
    """把**按选择器拆开**取回的一组结果合并回去重 + 配额限制后的样子。

    整组一条查询时由服务端的 ``out center budget`` 限制该组条数;拆成一个选择器一条查询
    后每条都各自带 ``budget``,直接合并会超配额,所以在本地补上同一道限制:先按
    ``(osm_type, osm_id)`` 去重(同一地物常被组内多个选择器命中,键与
    :func:`services.classify.dedupe_places` 一致;没有 OSM 身份时退化成坐标 + 名称),
    再由近及远取前 ``budget`` 条。归类优先级与跨组去重仍由 services 层负责,口径不变。
    """
    unique: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for row in rows:
        identity: Any = (row.get("osm_type"), row.get("osm_id"))
        if identity[1] is None:
            identity = (
                round(float(row["lat"]), COORD_PRECISION),
                round(float(row["lng"]), COORD_PRECISION),
                row.get("name"),
            )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(row)
    unique.sort(key=lambda item: haversine_km(lat, lng, item["lat"], item["lng"]))
    return unique[: max(1, int(budget))]


def parse_element(element: Any, *, with_id: bool = False) -> Optional[dict[str, Any]]:
    """把单个 Overpass element 解析成 ``{"lat", "lng", "name", "tags"}``;无坐标则返回 None。

    ``with_id=True`` 时额外带 ``osm_type``/``osm_id``(入库防重要用 OSM 身份)。
    """
    if not isinstance(element, dict):
        return None
    lat, lng = element.get("lat"), element.get("lon")
    if lat is None or lng is None:
        center = element.get("center")
        if isinstance(center, dict):
            lat, lng = center.get("lat"), center.get("lon")
    if lat is None or lng is None:
        return None
    try:
        latitude = float(lat)
        longitude = float(lng)
    except (TypeError, ValueError):
        return None

    tags = element.get("tags")
    if not isinstance(tags, dict):
        tags = {}
    name = tags.get("name") or tags.get("name:zh") or tags.get("name:en") or ""
    place: dict[str, Any] = {
        "lat": round(latitude, COORD_PRECISION),
        "lng": round(longitude, COORD_PRECISION),
        "name": str(name),
        "tags": tags,
    }
    if with_id:
        place["osm_type"] = str(element.get("type") or "")
        place["osm_id"] = _as_int(element.get("id"))
    return place


def parse_places(
    payload: Any,
    origin_lat: float,
    origin_lng: float,
    *,
    limit: Optional[int] = DEFAULT_LIMIT,
    require_name: bool = False,
    with_id: bool = False,
) -> list[dict[str, Any]]:
    """解析 Overpass 响应,按离 ``origin`` 由近及远排序后截断到 ``limit`` 条。

    ``with_id=True`` 时每条额外带 ``osm_type``/``osm_id``。
    """
    elements = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(elements, list):
        raise DataSourceError(
            SOURCE_NAME, f"响应中没有 elements 列表(实际类型:{type(payload).__name__})"
        )

    places: list[dict[str, Any]] = []
    for element in elements:
        place = parse_element(element, with_id=with_id)
        if place is None:
            continue
        if require_name and not place["name"]:
            continue
        places.append(place)

    places.sort(key=lambda item: haversine_km(origin_lat, origin_lng, item["lat"], item["lng"]))
    return places[:limit] if limit else places


class OverpassClient:
    """Overpass 客户端:端点链降级 + 重试,应对公共实例频繁 504 的实际情况。"""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        *,
        timeout: Optional[float] = None,
        user_agent: Optional[str] = None,
        session: Optional[Any] = None,
        fallback_endpoints: Sequence[str] = FALLBACK_ENDPOINTS,
        retries: int = 2,
        retry_backoff_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        resolved = endpoint or os.environ.get(ENV_ENDPOINT) or DEFAULT_ENDPOINT
        self.endpoint = resolved.rstrip("/")
        self.fallback_endpoints = tuple(item.rstrip("/") for item in fallback_endpoints)
        self.timeout = normalize_timeout(timeout)
        self.user_agent = user_agent or USER_AGENT
        self.retries = max(1, int(retries))
        self.retry_backoff_s = max(0.0, float(retry_backoff_s))
        self.used_endpoint: Optional[str] = None
        self._session = session if session is not None else build_session(self.user_agent)
        self._sleep = sleep

    @property
    def endpoints(self) -> tuple[str, ...]:
        """实际尝试顺序:主端点 + 备用镜像(去重、保持顺序)。"""
        ordered = [self.endpoint, *self.fallback_endpoints]
        unique: list[str] = []
        for item in ordered:
            if item not in unique:
                unique.append(item)
        return tuple(unique)

    def nearby_places(
        self,
        lat: float,
        lng: float,
        radius_m: float,
        tags: Tags,
        *,
        limit: Optional[int] = DEFAULT_LIMIT,
        require_name: bool = False,
        element_types: Union[str, Iterable[str]] = "nwr",
        query_timeout: float = DEFAULT_QUERY_TIMEOUT,
        with_id: bool = False,
    ) -> list[dict[str, Any]]:
        """检索 ``(lat, lng)`` 半径 ``radius_m`` 内匹配 ``tags`` 的 POI 列表。

        返回 ``[{"lat", "lng", "name", "tags"}]``,按离中心点由近及远排序;
        ``with_id=True`` 时每条额外带 ``osm_type``/``osm_id``。
        """
        latitude = float(lat)
        longitude = float(lng)
        query = build_query(
            latitude,
            longitude,
            radius_m,
            tags,
            limit=limit,
            element_types=element_types,
            query_timeout=query_timeout,
        )
        payload = self.execute(query)
        return parse_places(
            payload,
            latitude,
            longitude,
            limit=limit,
            require_name=require_name,
            with_id=with_id,
        )

    def nearby_places_grouped(
        self,
        lat: float,
        lng: float,
        radius_m: float,
        groups: Sequence[Mapping[str, Any]],
        *,
        require_name: bool = True,
        query_timeout: float = DEFAULT_GROUP_QUERY_TIMEOUT,
        request_timeout: float = DEFAULT_GROUP_REQUEST_TIMEOUT_S,
        with_id: bool = True,
    ) -> list[dict[str, Any]]:
        """分组并集检索:一次 HTTP 请求查多类 tag,每组独立配额(见 :func:`build_grouped_query`)。

        返回按离中心点由近及远排序的 ``[{"lat", "lng", "name", "tags"(, osm_type/osm_id)}]``;
        各组之间可能有重复实体(同一地物命中多组 tag),**不在这里去重**,
        由 :func:`services.classify.dedupe_places` 按 ``(type, id)`` 合并。

        这是**单圆**路径(0 ~ ``radius_m``);下限 > 0 的分段请用 :meth:`nearby_places_ring`,
        否则每组配额都会被近处 POI 吃光。

        ``request_timeout`` 是客户端 HTTP 超时(冷启动批量抓取,可远超交互 20s 上限);
        公共实例繁忙时服务端可能在 ``query_timeout`` 处中止并只回**部分分组**的结果,
        这时不报错、按拿到的入库(下次 ``refresh=true`` 可补齐)。
        """
        latitude = float(lat)
        longitude = float(lng)
        query = build_grouped_query(
            latitude,
            longitude,
            radius_m,
            groups,
            require_name=require_name,
            query_timeout=query_timeout,
        )
        payload = self.execute(query, timeout=request_timeout)
        return parse_places(
            payload,
            latitude,
            longitude,
            limit=None,
            require_name=require_name,
            with_id=with_id,
        )

    def nearby_places_ring(
        self,
        lat: float,
        lng: float,
        outer_radius_m: float,
        inner_radius_m: Optional[float],
        groups: Sequence[Mapping[str, Any]],
        *,
        require_name: bool = True,
        query_timeout: Optional[float] = None,
        request_timeout: Optional[float] = None,
        with_id: bool = True,
    ) -> list[dict[str, Any]]:
        """环形差集检索:只取 ``[inner_radius_m, outer_radius_m]`` 环内的地物(TASK-1d)。

        与 :meth:`nearby_places_grouped` 的差别有两点:

        1. **每组一次请求**。差集要在服务端同时物化上限圆与下限圆两个集合,六组塞进一次
           请求实测直接撞公共实例的单查询内存上限(maps.mail.ru 回 OOM remark、
           overpass-api.de 504 拒收);拆开后单组实测 24-130s 可跑通。每组各自走
           :meth:`execute` 的端点链 + 重试退避。
        2. **单组仍太重时按选择器再拆**(见 :meth:`_ring_group_rows`):实测滑雪场组
           在 overpass-api.de / maps.mail.ru 都 OOM,拆成一个选择器一条差集后
           每条 27-58s 跑通;拆分后仍受该组配额约束。
        3. **失败要响**。某组拆到选择器粒度仍拿不到,就抛 :class:`DataSourceError`(带组名),
           不把残缺结果当成功返回 —— 否则调用方会把这个 (城市, band) 记成"已抓取",
           远环又只剩几条,正好回到本任务要修的老问题。

        ``inner_radius_m`` 为 0 / None 时没有内圈可减,直接委托
        :meth:`nearby_places_grouped`(单圆、一次请求、单圆超时),行为与 TASK-1b 一致。
        分组并集、每组配额、去重键 ``(type, id)`` 与归类优先级口径都不变。
        """
        latitude = float(lat)
        longitude = float(lng)
        inner = float(inner_radius_m or 0.0)
        ring = inner > 0
        if query_timeout is None:
            query_timeout = DEFAULT_RING_QUERY_TIMEOUT if ring else DEFAULT_GROUP_QUERY_TIMEOUT
        if request_timeout is None:
            request_timeout = DEFAULT_RING_REQUEST_TIMEOUT_S if ring else DEFAULT_GROUP_REQUEST_TIMEOUT_S
        if not ring:
            return self.nearby_places_grouped(
                latitude,
                longitude,
                outer_radius_m,
                groups,
                require_name=require_name,
                query_timeout=query_timeout,
                request_timeout=request_timeout,
                with_id=with_id,
            )
        if not groups:
            raise ValueError("groups 不能为空")

        rows: list[dict[str, Any]] = []
        for group in groups:
            rows.extend(
                self._ring_group_rows(
                    group,
                    latitude,
                    longitude,
                    outer_radius_m,
                    inner,
                    require_name=require_name,
                    query_timeout=query_timeout,
                    request_timeout=request_timeout,
                    with_id=with_id,
                )
            )
        rows.sort(key=lambda item: haversine_km(latitude, longitude, item["lat"], item["lng"]))
        return rows

    def _ring_group_rows(
        self,
        group: Mapping[str, Any],
        lat: float,
        lng: float,
        outer_radius_m: float,
        inner_radius_m: float,
        *,
        require_name: bool,
        query_timeout: float,
        request_timeout: float,
        with_id: bool,
    ) -> list[dict[str, Any]]:
        """一个分组的环内结果:先**整组一次请求**,失败再**按选择器拆开**逐个请求。

        整组差集要把"该组所有选择器 × 上下限圆"一次物化,重的组会直接 OOM/504
        (实测滑雪场组:4 个选择器里 ``sport~`` / ``ski~`` 两个是正则,300 km 外圈在
        overpass-api.de 花 145s、maps.mail.ru 花 88s 后都回 2048 MB OOM)。拆成
        "一个选择器一条差集查询"后每条都小得多(同组四个选择器实测 27/29/44/58s 全部跑通),
        是公共实例上的常规降级手段。

        拆分后仍受**该组配额**约束:合并 → ``(type, id)`` 去重 → 由近及远取前 ``budget`` 条
        (:func:`_merge_split_rows`),与整组查询 ``out center budget`` 的口径一致。
        拆到选择器粒度仍全部失败才抛错(带组名),免得把残缺结果记成"已抓取"的水位。
        """
        label = str(group.get("group") or group.get("category") or "未命名分组")
        budget = resolve_group_limit(group.get("budget", group.get("limit")))

        def fetch(targets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
            return self._ring_request(
                targets,
                lat,
                lng,
                outer_radius_m,
                inner_radius_m,
                require_name=require_name,
                query_timeout=query_timeout,
                request_timeout=request_timeout,
                with_id=with_id,
            )

        try:
            return fetch([group])
        except DataSourceError as exc:
            whole = exc

        selectors = tags_to_selectors(group.get("tags"))
        if len(selectors) < 2:
            raise self._ring_failure(label, whole) from whole

        rows: list[dict[str, Any]] = []
        failures: list[str] = []
        for selector in selectors:
            split = dict(group)
            split["tags"] = [selector]
            try:
                rows.extend(fetch([split]))
            except DataSourceError as exc:
                failures.append(f"{selector}:{exc.message[:DETAIL_LEN]}")
        if failures:
            raise self._ring_failure(label, whole, failures) from whole
        return _merge_split_rows(rows, budget, lat, lng)

    def _ring_request(
        self,
        groups: Sequence[Mapping[str, Any]],
        lat: float,
        lng: float,
        outer_radius_m: float,
        inner_radius_m: float,
        *,
        require_name: bool,
        query_timeout: float,
        request_timeout: float,
        with_id: bool,
    ) -> list[dict[str, Any]]:
        """发一条环形差集查询并解析(端点链 + 重试由 :meth:`execute` 负责)。"""
        query = build_grouped_ring_query(
            lat,
            lng,
            outer_radius_m,
            inner_radius_m,
            groups,
            require_name=require_name,
            query_timeout=query_timeout,
        )
        payload = self.execute(query, timeout=request_timeout, reject_runtime_errors=True)
        return parse_places(
            payload, lat, lng, limit=None, require_name=require_name, with_id=with_id
        )

    @staticmethod
    def _ring_failure(
        label: str, cause: DataSourceError, failures: Optional[Sequence[str]] = None
    ) -> DataSourceError:
        """「某组取不到数」的统一错误:带组名与失败明细,便于定位是哪类 tag 太重。"""
        detail = ";".join(failures[-DETAIL_KEEP:]) if failures else cause.message
        return DataSourceError(
            SOURCE_NAME,
            f"环形差集分组「{label}」取数失败,本次抓取按失败处理"
            f"(不写残缺水位,可稍后 refresh 重试):{detail}",
        )

    def execute(
        self, query: str, *, timeout: Optional[float] = None, reject_runtime_errors: bool = False
    ) -> Any:
        """执行一段 Overpass QL:依次尝试各端点,临时失败(504/超时)自动重试与降级。

        ``timeout`` 只对本次调用生效(批量冷启动用),缺省沿用实例的 20s 上限值。
        ``reject_runtime_errors=True`` 时,HTTP 200 但带**致命** ``remark``(OOM 之类,
        见 :func:`runtime_error_remark`)的响应直接抛 :class:`OverpassRuntimeError`:
        这类错误源于查询本身太重,而三个公共实例的单查询内存上限实测都是 2048 MB,
        换端点只会再等一遍;该由调用方把查询拆小(见 :meth:`_ring_group_rows`)。
        超时 remark 不在此列,照旧收下部分结果。
        """
        request_timeout = self.timeout if timeout is None else max(1.0, float(timeout))
        attempts_log: list[str] = []
        for index, endpoint in enumerate(self.endpoints):
            for attempt in range(1, self.retries + 1):
                try:
                    payload = http_json(
                        self._session,
                        endpoint,
                        source=SOURCE_NAME,
                        method="POST",
                        data={"data": query},
                        timeout=request_timeout,
                        headers={"User-Agent": self.user_agent},
                    )
                except TransientDataSourceError as exc:
                    attempts_log.append(f"{endpoint} 第{attempt}次:{exc.message[:DETAIL_LEN]}")
                    self._backoff(index, attempt)
                    continue
                if not isinstance(payload, dict):
                    raise DataSourceError(
                        SOURCE_NAME, f"响应格式异常(应为 JSON 对象):{type(payload).__name__}"
                    )
                remark = runtime_error_remark(payload) if reject_runtime_errors else ""
                if remark:
                    raise OverpassRuntimeError(
                        SOURCE_NAME, f"{endpoint} 服务端致命错误:{remark[:DETAIL_LEN]}"
                    )
                self.used_endpoint = endpoint
                return payload

        tried = "、".join(self.endpoints)
        detail = ";".join(attempts_log[-DETAIL_KEEP:])
        raise DataSourceError(
            SOURCE_NAME,
            f"所有 Overpass 端点均不可用(公共实例常见为繁忙/超时,可稍后重试或换端点;"
            f"已尝试:{tried};每端点 {self.retries} 次)。明细:{detail}",
        )

    def _backoff(self, endpoint_index: int, attempt: int) -> None:
        if self.retry_backoff_s <= 0:
            return
        self._sleep(self.retry_backoff_s * attempt * (endpoint_index + 1))


_default_client: Optional[OverpassClient] = None


def default_client() -> OverpassClient:
    """返回进程内共享的默认客户端(端点可用 ``WHERE2GO_OVERPASS_ENDPOINT`` 覆盖)。"""
    global _default_client
    if _default_client is None:
        _default_client = OverpassClient()
    return _default_client


def nearby_places(
    lat: float,
    lng: float,
    radius_m: float,
    tags: Tags,
    *,
    limit: Optional[int] = DEFAULT_LIMIT,
    require_name: bool = False,
    element_types: Union[str, Iterable[str]] = "nwr",
    endpoint: Optional[str] = None,
    timeout: Optional[float] = None,
    session: Optional[Any] = None,
    retries: int = 2,
    with_id: bool = False,
) -> list[dict[str, Any]]:
    """模块级便捷函数:周边 POI 检索。"""
    if endpoint is None and timeout is None and session is None and retries == 2:
        client = default_client()
    else:
        client = OverpassClient(endpoint, timeout=timeout, session=session, retries=retries)
    return client.nearby_places(
        lat,
        lng,
        radius_m,
        tags,
        limit=limit,
        require_name=require_name,
        element_types=element_types,
        with_id=with_id,
    )
