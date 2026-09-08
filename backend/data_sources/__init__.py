"""Where2Go 免费数据源适配层(阶段0 POC)。

三个**免费、无需 key** 的公共数据源(见 ADR-006):

* :mod:`data_sources.osrm` —— 驾车路线(``distance_km`` / ``duration_min``);
* :mod:`data_sources.nominatim` —— 正向/逆向地理编码(带 User-Agent 与 1 req/s 节流);
* :mod:`data_sources.overpass` —— 按 tag 检索周边 POI(目的地库冷启动)。

用法::

    from data_sources import geocode, nearby_places, route

    origin = geocode("北京")
    places = nearby_places(origin["lat"], origin["lng"], 50_000, {"tourism": "attraction"})
    leg = route((origin["lng"], origin["lat"]), (places[0]["lng"], places[0]["lat"]))

所有失败都会抛出 :class:`DataSourceError`(中文说明);真实网络端到端验证见
``data_sources/verify_poc.py``。
"""

from ._common import (
    DEFAULT_TIMEOUT,
    MAX_TIMEOUT,
    USER_AGENT,
    DataSourceError,
    TransientDataSourceError,
    build_session,
    normalize_timeout,
)
from .nominatim import (
    DEFAULT_ENDPOINT as NOMINATIM_ENDPOINT,
    MIN_REQUEST_INTERVAL_S as NOMINATIM_MIN_INTERVAL_S,
    NominatimClient,
    geocode,
    reverse,
)
from .osrm import (
    ALT_ENDPOINT as OSRM_ALT_ENDPOINT,
    DEFAULT_ENDPOINT as OSRM_ENDPOINT,
    OsrmClient,
    route,
)
from .overpass import (
    DEFAULT_ENDPOINT as OVERPASS_ENDPOINT,
    FALLBACK_ENDPOINTS as OVERPASS_FALLBACK_ENDPOINTS,
    OverpassClient,
    haversine_km,
    nearby_places,
)

__all__ = [
    "USER_AGENT",
    "DEFAULT_TIMEOUT",
    "MAX_TIMEOUT",
    "DataSourceError",
    "TransientDataSourceError",
    "build_session",
    "normalize_timeout",
    "OSRM_ENDPOINT",
    "OSRM_ALT_ENDPOINT",
    "NOMINATIM_ENDPOINT",
    "NOMINATIM_MIN_INTERVAL_S",
    "OVERPASS_ENDPOINT",
    "OVERPASS_FALLBACK_ENDPOINTS",
    "OsrmClient",
    "NominatimClient",
    "OverpassClient",
    "route",
    "geocode",
    "reverse",
    "nearby_places",
    "haversine_km",
]
