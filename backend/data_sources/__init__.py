"""Where2Go 数据源适配层(TASK-9 起主数据源为**高德 v3**)。

三个数据源(见 ADR-006 与 ``docs/TASK-9-CONTRACT.md``):

* :mod:`data_sources.amap` —— POI 检索 / 正逆地理编码 / 驾车路线(需 ``WHERE2GO_AMAP_KEY``);
* :mod:`data_sources.photon` —— 地理编码降级链第一腿(免费无 key,直连);
* :mod:`data_sources.nominatim` —— 地理编码降级链末腿(带 User-Agent 与 1 req/s 节流)。

Overpass 与 OSRM 已在 **TASK-9c 物理删除**(不留降级链、不留休眠文件);
:func:`haversine_km` 随之搬到 :mod:`data_sources._common`,导入面不变。

用法::

    from data_sources import amap, geocode, haversine_km

    rows = amap.geocode("杭州西湖")
    leg = amap.driving(30.2741, 120.1551, 31.2304, 121.4737)
    km = haversine_km(30.2741, 120.1551, 31.2304, 121.4737)

所有失败都会抛出 :class:`DataSourceError`(中文说明);可临时重试的再细分成
:class:`TransientDataSourceError`,方便上层做退避与降级。
"""

from . import amap
from ._common import (
    DEFAULT_TIMEOUT,
    EARTH_RADIUS_KM,
    MAX_TIMEOUT,
    USER_AGENT,
    DataSourceError,
    TransientDataSourceError,
    build_session,
    haversine_km,
    normalize_timeout,
)
from .nominatim import (
    DEFAULT_ENDPOINT as NOMINATIM_ENDPOINT,
    MIN_REQUEST_INTERVAL_S as NOMINATIM_MIN_INTERVAL_S,
    NominatimClient,
    geocode,
    reverse,
)

__all__ = [
    "USER_AGENT",
    "DEFAULT_TIMEOUT",
    "MAX_TIMEOUT",
    "EARTH_RADIUS_KM",
    "DataSourceError",
    "TransientDataSourceError",
    "build_session",
    "normalize_timeout",
    "haversine_km",
    "amap",
    "NOMINATIM_ENDPOINT",
    "NOMINATIM_MIN_INTERVAL_S",
    "NominatimClient",
    "geocode",
    "reverse",
]
