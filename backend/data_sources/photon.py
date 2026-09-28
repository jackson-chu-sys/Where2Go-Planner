"""Photon(komoot)地理编码:正向地名→坐标、逆向坐标→地名。

为什么要有它(TASK-6a,2026-09-28 实测):产品环境没有 mihomo 代理,而 Nominatim
**直连不通**(容器实测 15s 超时);Photon 公共实例直连 1.1s 可用,中文城市/乡村/
区划命中正确坐标,逆地理编码也能用。所以地理编码改成 **Photon 主路径 + Nominatim
降级**(降级链在 :mod:`services.place_loader`,本模块只负责 Photon 自己)。

真实响应(2026-09-28 实测 ``https://photon.komoot.io``,都是 GeoJSON FeatureCollection)::

    # GET /api?q=北京&limit=5 → 城市级结果的 properties 里**没有 state**
    {"type": "FeatureCollection", "features": [
      {"type": "Feature",
       "geometry": {"type": "Point", "coordinates": [116.3912972, 39.9057136]},
       "properties": {"osm_type": "R", "osm_id": 912940, "osm_key": "place",
                      "osm_value": "city", "type": "city", "name": "北京市",
                      "country": "中国", "countrycode": "CN",
                      "extra": {"admin_level": "4"}, "extent": [...]}}]}

    # GET /reverse?lat=39.9042&lon=116.4074 → 门牌级结果(反查不到时 features 为 [])
    {"type": "FeatureCollection", "features": [
      {"type": "Feature",
       "properties": {"osm_type": "N", "osm_id": 13860479407, "type": "house",
                      "name": "台基厂头条14号院-10号院", "street": "台基厂头条",
                      "district": "东城区", "city": "北京市", "country": "中国",
                      "postcode": "100010", "countrycode": "CN"},
       "geometry": {"type": "Point", "coordinates": [116.4075123, 39.9042695]}}]}

    # → display_name 分别是 "北京市, 中国" 与 "台基厂头条14号院-10号院, 北京市, 中国",
    #   services.place_loader.city_from_display_name 从后者里照样能挑出 "北京市"。

与 Nominatim 的三处关键差异(以真实 API 为准):

* 坐标在 ``geometry.coordinates`` 里,顺序是 **[lon, lat]**(GeoJSON 口径,经度在前!),
  而且是**数字**不是字符串;
* 没有 ``display_name``,地名要自己从 ``properties.{name, city, state, country}`` 拼
  (实测中国结果基本只有 name / city / country 三段有值);
* **不支持** ``lang`` 参数(传了会被忽略),用默认本地语言即可 —— 中国地名自带中文。

输出形状与 :mod:`data_sources.nominatim` **完全一致**(``{"lat", "lng", "display_name"}``,
坐标定点 7 位小数),上层降级链可以无脑互换。礼貌请求同样内置 **1 req/s** 节流
(:meth:`PhotonClient._throttle`),网络/格式错误一律抛 :class:`DataSourceError`(中文说明)。
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

SOURCE_NAME = "Photon"
DEFAULT_ENDPOINT = "https://photon.komoot.io"
ENV_ENDPOINT = "WHERE2GO_PHOTON_ENDPOINT"
MIN_REQUEST_INTERVAL_S = 1.0
#: Photon 的路径与 Nominatim 不同:正向是 /api,逆向是 /reverse(不是 /api/reverse,实测 404)
SEARCH_PATH = "/api"
REVERSE_PATH = "/reverse"
#: 正向检索默认取几条(交给上层挑最相关的第一条,其余留作候选)
DEFAULT_LIMIT = 5
COORD_PRECISION = 7
#: display_name 的拼接顺序与参与字段(跳过空段;Photon 直辖市的 name/state 常同名,重复段只留一次)
DISPLAY_NAME_FIELDS = ("name", "city", "state", "country")
DISPLAY_NAME_SEP = ", "


def build_display_name(properties: Any) -> str:
    """按「name, city, state, country」拼 display_name:跳过空段,重复段只留第一次。"""
    if not isinstance(properties, dict):
        return ""
    parts: list[str] = []
    for key in DISPLAY_NAME_FIELDS:
        value = str(properties.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return DISPLAY_NAME_SEP.join(parts)


def parse_feature(raw: Any, *, action: str = "地理编码") -> dict[str, Any]:
    """把 Photon 的一个 GeoJSON feature 解析成 ``{"lat", "lng", "display_name"}``。

    注意 ``geometry.coordinates`` 是 **[lon, lat]**(经度在前),别抄成纬度在前。
    """
    if not isinstance(raw, dict):
        raise DataSourceError(SOURCE_NAME, f"{action}响应格式异常(feature 应为 JSON 对象):{type(raw).__name__}")

    geometry = raw.get("geometry")
    coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        raise DataSourceError(
            SOURCE_NAME, f"{action}响应缺少可解析的 geometry.coordinates:[lon, lat]:{raw!r}"
        )
    try:
        # GeoJSON 口径:coordinates = [经度, 纬度](可能还带第三个高程值)
        lng = float(coordinates[0])
        lat = float(coordinates[1])
    except (TypeError, ValueError) as exc:
        raise DataSourceError(SOURCE_NAME, f"{action}响应的 coordinates 不是数字:{coordinates!r}") from exc

    display_name = build_display_name(raw.get("properties"))
    if not display_name:
        raise DataSourceError(
            SOURCE_NAME, f"{action}结果的 properties 里没有任何地名(name/city/state/country 全为空)"
        )

    return {
        "lat": round(lat, COORD_PRECISION),
        "lng": round(lng, COORD_PRECISION),
        "display_name": display_name,
    }


def parse_features(payload: Any, *, action: str = "地理编码") -> list[dict[str, Any]]:
    """解析 FeatureCollection → ``[{"lat", "lng", "display_name"}, ...]``(保持原顺序)。"""
    if not isinstance(payload, dict):
        raise DataSourceError(
            SOURCE_NAME, f"{action}响应格式异常(应为 GeoJSON 对象):{type(payload).__name__}"
        )
    features = payload.get("features")
    if not isinstance(features, list):
        raise DataSourceError(
            SOURCE_NAME, f"{action}响应缺少 features 数组:{type(features).__name__}"
        )
    return [parse_feature(feature, action=action) for feature in features]


class PhotonClient:
    """Photon 客户端:统一 User-Agent、1 req/s 节流与中文错误提示。"""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        *,
        timeout: Optional[float] = None,
        user_agent: Optional[str] = USER_AGENT,
        session: Optional[Any] = None,
        min_interval: float = MIN_REQUEST_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        resolved = endpoint or os.environ.get(ENV_ENDPOINT) or DEFAULT_ENDPOINT
        self.endpoint = resolved.rstrip("/")
        self.timeout = normalize_timeout(timeout)
        self.user_agent = user_agent
        self.min_interval = max(0.0, float(min_interval))
        self._session = session if session is not None else build_session(self.user_agent or USER_AGENT, source="photon")
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
        # 刻意**不传 lang**:Photon 公共实例不支持该参数,中国地名本来就是中文。
        self._throttle()
        return http_json(
            self._session,
            f"{self.endpoint}{path}",
            source=SOURCE_NAME,
            params=params,
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent} if self.user_agent else None,
        )

    def geocode(self, query: str, *, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
        """正向地理编码:城市/地名 → ``[{"lat", "lng", "display_name"}, ...]``(按相关度)。

        与 Nominatim 的同名方法不同,这里返回**列表**(调用方自己挑第一条);
        检索不到时返回**空列表**而不是抛错,方便上层直接判定"该降级了"。
        """
        text = (query or "").strip()
        if not text:
            raise ValueError("geocode 的 query 不能为空")
        payload = self._request(SEARCH_PATH, {"q": text, "limit": max(1, int(limit))})
        return parse_features(payload, action=f"正向地理编码 {text!r}")

    def reverse(self, lat: float, lng: float) -> dict[str, Any]:
        """逆地理编码:坐标 → ``{"lat", "lng", "display_name"}``(用于"当前位置")。

        Photon 的 reverse 没有 Nominatim 的 ``zoom`` 概念(固定返回最近的地点),
        所以这里也不收 zoom 参数。
        """
        try:
            latitude = float(lat)
            longitude = float(lng)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"坐标必须为数字,收到:lat={lat!r}, lng={lng!r}") from exc
        if not -90.0 <= latitude <= 90.0:
            raise ValueError(f"纬度超出 [-90, 90] 范围:{latitude}")
        if not -180.0 <= longitude <= 180.0:
            raise ValueError(f"经度超出 [-180, 180] 范围:{longitude}")

        payload = self._request(
            REVERSE_PATH, {"lat": f"{latitude:.6f}", "lon": f"{longitude:.6f}"}
        )
        places = parse_features(payload, action=f"逆地理编码 ({latitude:.6f},{longitude:.6f})")
        if not places:
            raise DataSourceError(
                SOURCE_NAME, f"逆地理编码未找到结果:({latitude:.6f},{longitude:.6f})"
            )
        return places[0]


_default_client: Optional[PhotonClient] = None


def default_client() -> PhotonClient:
    """返回进程内共享的默认客户端(节流状态在客户端内,便于遵守 1 req/s)。"""
    global _default_client
    if _default_client is None:
        _default_client = PhotonClient()
    return _default_client


def geocode(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    endpoint: Optional[str] = None,
    timeout: Optional[float] = None,
    session: Optional[Any] = None,
    min_interval: float = MIN_REQUEST_INTERVAL_S,
) -> list[dict[str, Any]]:
    """模块级便捷函数:正向地理编码。"""
    client = _resolve_client(endpoint, timeout, session, min_interval)
    return client.geocode(query, limit=limit)


def reverse(
    lat: float,
    lng: float,
    *,
    endpoint: Optional[str] = None,
    timeout: Optional[float] = None,
    session: Optional[Any] = None,
    min_interval: float = MIN_REQUEST_INTERVAL_S,
) -> dict[str, Any]:
    """模块级便捷函数:逆地理编码。"""
    client = _resolve_client(endpoint, timeout, session, min_interval)
    return client.reverse(lat, lng)


def _resolve_client(
    endpoint: Optional[str],
    timeout: Optional[float],
    session: Optional[Any],
    min_interval: float,
) -> PhotonClient:
    if (
        endpoint is None
        and timeout is None
        and session is None
        and min_interval == MIN_REQUEST_INTERVAL_S
    ):
        return default_client()
    return PhotonClient(endpoint, timeout=timeout, session=session, min_interval=min_interval)
