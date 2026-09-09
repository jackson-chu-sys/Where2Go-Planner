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
DEFAULT_LIMIT = 20
FETCH_FACTOR = 12
MIN_FETCH = 60
MAX_FETCH = 400
EARTH_RADIUS_KM = 6371.0088
DETAIL_LEN = 200
DETAIL_KEEP = 3
COORD_PRECISION = 7

Tags = Union[Mapping[str, Any], str, Sequence[Union[Mapping[str, Any], str]]]


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

    def execute(self, query: str) -> Any:
        """执行一段 Overpass QL:依次尝试各端点,临时失败(504/超时)自动重试与降级。"""
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
                        timeout=self.timeout,
                        headers={"User-Agent": self.user_agent},
                    )
                except TransientDataSourceError as exc:
                    attempts_log.append(f"{endpoint} 第{attempt}次:{exc.message[:DETAIL_LEN]}")
                    self._backoff(index, attempt)
                    continue
                self.used_endpoint = endpoint
                if not isinstance(payload, dict):
                    raise DataSourceError(
                        SOURCE_NAME, f"响应格式异常(应为 JSON 对象):{type(payload).__name__}"
                    )
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
