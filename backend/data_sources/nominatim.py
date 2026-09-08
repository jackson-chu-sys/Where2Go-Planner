"""Nominatim(OSM)地理编码:正向地名→坐标、逆向坐标→地名。

真实响应(2026-09-08 实测 ``https://nominatim.openstreetmap.org``,``format=jsonv2``)::

    # /search?q=北京 → JSON **数组**
    [{"osm_type": "relation", "lat": "39.9057136", "lon": "116.3912972",
      "display_name": "北京市, 中国", "addresstype": "city", ...}]

    # /reverse?lat=39.9042&lon=116.4074 → JSON **对象**
    {"lat": "39.9042695", "lon": "116.4075123",
     "display_name": "台基厂头条14号院-10号院, ..., 东城区, 北京市, 100010, 中国", ...}

    # /reverse 找不到结果时返回 {"error": "Unable to geocode"}

要点(以真实 API 为准):

* ``lat`` / ``lon`` 是**字符串**,需要转 float;
* 必须带可识别的 ``User-Agent``,否则会被拒绝;
* 官方使用政策要求 **≤ 1 次/秒**,本模块内置节流(:meth:`NominatimClient._throttle`)。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Optional

from ._common import (
    USER_AGENT,
    DataSourceError,
    build_session,
    http_json,
    normalize_timeout,
)

SOURCE_NAME = "Nominatim"
DEFAULT_ENDPOINT = "https://nominatim.openstreetmap.org"
ENV_ENDPOINT = "WHERE2GO_NOMINATIM_ENDPOINT"
MIN_REQUEST_INTERVAL_S = 1.0
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"
COORD_PRECISION = 7


def parse_place(raw: Any, *, action: str = "地理编码") -> dict[str, Any]:
    """把 Nominatim 的单条结果解析成 ``{"lat", "lng", "display_name"}``。"""
    if not isinstance(raw, dict):
        raise DataSourceError(SOURCE_NAME, f"{action}响应格式异常(应为 JSON 对象):{type(raw).__name__}")

    try:
        # 真实 API 返回的是字符串坐标,这里统一转 float。
        lat = float(raw["lat"])
        lng = float(raw["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DataSourceError(SOURCE_NAME, f"{action}响应缺少可解析的 lat/lon:{raw!r}") from exc

    display_name = str(raw.get("display_name") or raw.get("name") or "").strip()
    if not display_name:
        raise DataSourceError(SOURCE_NAME, f"{action}结果没有 display_name,无法核对地名")

    return {
        "lat": round(lat, COORD_PRECISION),
        "lng": round(lng, COORD_PRECISION),
        "display_name": display_name,
    }


class NominatimClient:
    """Nominatim 客户端:带 User-Agent、1 req/s 节流与中文错误提示。"""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        *,
        timeout: Optional[float] = None,
        user_agent: Optional[str] = USER_AGENT,
        session: Optional[Any] = None,
        min_interval: float = MIN_REQUEST_INTERVAL_S,
        accept_language: Optional[str] = ACCEPT_LANGUAGE,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not (user_agent or "").strip():
            raise ValueError("Nominatim 要求必须提供非空 User-Agent")
        resolved = endpoint or os.environ.get(ENV_ENDPOINT) or DEFAULT_ENDPOINT
        self.endpoint = resolved.rstrip("/")
        self.timeout = normalize_timeout(timeout)
        self.user_agent = user_agent
        self.min_interval = max(0.0, float(min_interval))
        self.accept_language = accept_language
        self._session = session if session is not None else build_session(self.user_agent)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_request_at = float("-inf")

    def _throttle(self) -> None:
        """礼貌请求:两次请求之间至少间隔 ``min_interval`` 秒。"""
        if self.min_interval <= 0:
            return
        with self._lock:
            now = self._clock()
            wait = self._last_request_at + self.min_interval - now
            self._last_request_at = now + max(wait, 0.0)
        if wait > 0:
            self._sleep(wait)

    def _request(self, path: str, params: dict[str, Any]) -> Any:
        params.setdefault("format", "jsonv2")
        params.setdefault("addressdetails", 0)
        if self.accept_language:
            params.setdefault("accept-language", self.accept_language)
        self._throttle()
        return http_json(
            self._session,
            f"{self.endpoint}{path}",
            source=SOURCE_NAME,
            params=params,
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
        )

    def geocode(self, query: str, *, limit: int = 1) -> dict[str, Any]:
        """正向地理编码:城市/地名 → ``{"lat", "lng", "display_name"}``(取最相关的一条)。"""
        text = (query or "").strip()
        if not text:
            raise ValueError("geocode 的 query 不能为空")
        payload = self._request("/search", {"q": text, "limit": max(1, int(limit))})
        if not isinstance(payload, list):
            raise DataSourceError(
                SOURCE_NAME, f"正向地理编码响应格式异常(应为 JSON 数组):{type(payload).__name__}"
            )
        if not payload:
            raise DataSourceError(SOURCE_NAME, f"未找到与 {text!r} 匹配的地名(结果为空)")
        return parse_place(payload[0], action=f"正向地理编码 {text!r}")

    def reverse(self, lat: float, lng: float, *, zoom: int = 10) -> dict[str, Any]:
        """逆地理编码:坐标 → ``{"lat", "lng", "display_name"}``(用于"当前位置")。"""
        try:
            latitude = float(lat)
            longitude = float(lng)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"坐标必须为数字,收到:lat={lat!r}, lng={lng!r}") from exc
        if not -90.0 <= latitude <= 90.0:
            raise ValueError(f"纬度超出 [-90, 90] 范围:{latitude}")
        if not -180.0 <= longitude <= 180.0:
            raise ValueError(f"经度超出 [-180, 180] 范围:{longitude}")

        params = {
            "lat": f"{latitude:.6f}",
            "lon": f"{longitude:.6f}",
            "zoom": int(zoom),
        }
        payload = self._request("/reverse", params)
        if isinstance(payload, dict) and payload.get("error"):
            raise DataSourceError(SOURCE_NAME, f"逆地理编码失败:{payload['error']}")
        return parse_place(payload, action=f"逆地理编码 ({latitude:.6f},{longitude:.6f})")


_default_client: Optional[NominatimClient] = None


def default_client() -> NominatimClient:
    """返回进程内共享的默认客户端(节流状态在客户端内,便于遵守 1 req/s)。"""
    global _default_client
    if _default_client is None:
        _default_client = NominatimClient()
    return _default_client


def geocode(
    query: str,
    *,
    limit: int = 1,
    endpoint: Optional[str] = None,
    timeout: Optional[float] = None,
    session: Optional[Any] = None,
    min_interval: float = MIN_REQUEST_INTERVAL_S,
) -> dict[str, Any]:
    """模块级便捷函数:正向地理编码。"""
    client = _resolve_client(endpoint, timeout, session, min_interval)
    return client.geocode(query, limit=limit)


def reverse(
    lat: float,
    lng: float,
    *,
    zoom: int = 10,
    endpoint: Optional[str] = None,
    timeout: Optional[float] = None,
    session: Optional[Any] = None,
    min_interval: float = MIN_REQUEST_INTERVAL_S,
) -> dict[str, Any]:
    """模块级便捷函数:逆地理编码。"""
    client = _resolve_client(endpoint, timeout, session, min_interval)
    return client.reverse(lat, lng, zoom=zoom)


def _resolve_client(
    endpoint: Optional[str],
    timeout: Optional[float],
    session: Optional[Any],
    min_interval: float,
) -> NominatimClient:
    if (
        endpoint is None
        and timeout is None
        and session is None
        and min_interval == MIN_REQUEST_INTERVAL_S
    ):
        return default_client()
    return NominatimClient(endpoint, timeout=timeout, session=session, min_interval=min_interval)
