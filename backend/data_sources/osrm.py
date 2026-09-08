"""OSRM 驾车路线规划(免费公共实例,无需 key)。

真实响应(2026-09-08 实测 ``https://router.project-osrm.org``)::

    {"code": "Ok",
     "routes": [{"distance": 122376.1, "duration": 5638.8,
                 "weight": 5638.8, "weight_name": "routability", "legs": [...]}],
     "waypoints": [...]}

要点(以真实 API 为准):

* ``distance`` 单位是**米**、``duration`` 单位是**秒**,本模块换算成 km / 分钟;
* 坐标顺序是 **经度,纬度**(与多数地图 API 相反);
* 失败时 ``code`` 不是 ``Ok``(例如 ``NotFound`` / ``NoRoute``),响应仍可能是 HTTP 200;
* 备选公共实例 ``https://routing.openstreetmap.de/routed-car`` 的路径前缀里已含
  profile,因此把 endpoint 整体替换即可,URL 结构一致。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any, Optional, Union

from ._common import (
    USER_AGENT,
    DataSourceError,
    build_session,
    http_json,
    normalize_timeout,
)

SOURCE_NAME = "OSRM"
DEFAULT_ENDPOINT = "https://router.project-osrm.org"
ALT_ENDPOINT = "https://routing.openstreetmap.de/routed-car"
DEFAULT_PROFILE = "driving"
ENV_ENDPOINT = "WHERE2GO_OSRM_ENDPOINT"

LngLat = Union[Sequence[float], str]


def format_lnglat(value: LngLat) -> str:
    """把 ``(lng, lat)`` 序列或 ``"lng,lat"`` 字符串规范成 OSRM 需要的 ``lng,lat``。"""
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    elif isinstance(value, Sequence):
        parts = [str(part).strip() for part in value]
    else:
        raise ValueError(f"坐标应为 (lng, lat) 序列或 'lng,lat' 字符串,收到:{value!r}")

    if len(parts) != 2:
        raise ValueError(f"坐标必须包含 2 个分量(lng, lat),收到:{value!r}")
    try:
        lng, lat = (float(part) for part in parts)
    except ValueError as exc:
        raise ValueError(f"坐标必须为数字,收到:{value!r}") from exc
    if not -180.0 <= lng <= 180.0:
        raise ValueError(f"经度超出 [-180, 180] 范围:{lng}")
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"纬度超出 [-90, 90] 范围:{lat}")
    return f"{lng:.6f},{lat:.6f}"


def parse_route(payload: Any) -> dict[str, float]:
    """解析 OSRM 响应,返回 ``{"distance_km": 公里, "duration_min": 分钟}``。"""
    if not isinstance(payload, dict):
        raise DataSourceError(SOURCE_NAME, f"响应格式异常(应为 JSON 对象):{type(payload).__name__}")

    code = payload.get("code")
    if code != "Ok":
        message = payload.get("message")
        raise DataSourceError(
            SOURCE_NAME,
            f"路线规划失败(code={code!r}, message={message!r});常见原因:坐标不在路网内或两点不连通",
        )

    routes = payload.get("routes") or []
    if not isinstance(routes, list) or not routes:
        raise DataSourceError(SOURCE_NAME, "响应中没有 routes,无法获取驾车路线")

    first = routes[0]
    if not isinstance(first, dict):
        raise DataSourceError(SOURCE_NAME, f"routes[0] 格式异常:{type(first).__name__}")
    distance_m = first.get("distance")
    duration_s = first.get("duration")
    if not isinstance(distance_m, (int, float)) or not isinstance(duration_s, (int, float)):
        raise DataSourceError(
            SOURCE_NAME,
            f"routes[0] 缺少数字型 distance/duration 字段(distance={distance_m!r},"
            f" duration={duration_s!r})",
        )
    if distance_m <= 0 or duration_s <= 0:
        raise DataSourceError(
            SOURCE_NAME, f"路线距离/耗时非正数(distance={distance_m} m, duration={duration_s} s)"
        )

    return {
        "distance_km": round(float(distance_m) / 1000.0, 3),
        "duration_min": round(float(duration_s) / 60.0, 1),
    }


class OsrmClient:
    """OSRM HTTP API 客户端;端点、超时、session 均可注入以便测试与切换实例。"""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        *,
        timeout: Optional[float] = None,
        user_agent: Optional[str] = None,
        session: Optional[Any] = None,
    ) -> None:
        resolved = endpoint or os.environ.get(ENV_ENDPOINT) or DEFAULT_ENDPOINT
        self.endpoint = resolved.rstrip("/")
        self.timeout = normalize_timeout(timeout)
        self.user_agent = user_agent or USER_AGENT
        self._session = session if session is not None else build_session(self.user_agent)

    def route(
        self,
        start_lnglat: LngLat,
        end_lnglat: LngLat,
        *,
        profile: str = DEFAULT_PROFILE,
    ) -> dict[str, float]:
        """驾车路线规划:返回 ``{"distance_km": float, "duration_min": float}``。"""
        coordinates = f"{format_lnglat(start_lnglat)};{format_lnglat(end_lnglat)}"
        url = f"{self.endpoint}/route/v1/{profile}/{coordinates}"
        params = {
            "overview": "false",
            "alternatives": "false",
            "steps": "false",
            "annotations": "false",
        }
        payload = http_json(
            self._session,
            url,
            source=SOURCE_NAME,
            params=params,
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
        )
        return parse_route(payload)


_default_client: Optional[OsrmClient] = None


def default_client() -> OsrmClient:
    """返回进程内共享的默认客户端(端点可用环境变量 ``WHERE2GO_OSRM_ENDPOINT`` 覆盖)。"""
    global _default_client
    if _default_client is None:
        _default_client = OsrmClient()
    return _default_client


def route(
    start_lnglat: LngLat,
    end_lnglat: LngLat,
    *,
    profile: str = DEFAULT_PROFILE,
    endpoint: Optional[str] = None,
    timeout: Optional[float] = None,
    session: Optional[Any] = None,
) -> dict[str, float]:
    """模块级便捷函数:两点间驾车路线 ``{"distance_km", "duration_min"}``。"""
    if endpoint is None and timeout is None and session is None:
        client = default_client()
    else:
        client = OsrmClient(endpoint, timeout=timeout, session=session)
    return client.route(start_lnglat, end_lnglat, profile=profile)
