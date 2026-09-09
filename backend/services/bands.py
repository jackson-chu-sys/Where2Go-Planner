"""环形距离分段:50-100 / 100-200 / 200-300 / 300-500 km(互斥、不含城区)。

抓取口径(docs/STAGE1-PLAN.md 第 3 节):按当前分段的**上限半径**一次性查 Overpass,
再用大圆距离(haversine)把结果收敛到 ``[low, high)`` 环内。POC 的
``app/api/discover.py`` 与本阶段入库走的是同一套定义,所以分段只在这里出一份。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Optional

from data_sources import haversine_km

DISTANCE_BANDS: list[dict[str, Any]] = [
    {"key": "50_100", "label": "50-100 km", "low": 50, "high": 100},
    {"key": "100_200", "label": "100-200 km", "low": 100, "high": 200},
    {"key": "200_300", "label": "200-300 km", "low": 200, "high": 300},
    {"key": "300_500", "label": "300-500 km", "low": 300, "high": 500},
]
DISTANCE_PRECISION = 1


def band_keys() -> list[str]:
    """全部分段 key(按由近及远)。"""
    return [str(band["key"]) for band in DISTANCE_BANDS]


def find_band(key: Optional[str]) -> Optional[dict[str, Any]]:
    """按 key 找分段;找不到返回 None。"""
    wanted = (key or "").strip()
    return next((band for band in DISTANCE_BANDS if band["key"] == wanted), None)


def require_band(key: Optional[str]) -> dict[str, Any]:
    """按 key 找分段;找不到抛 :class:`ValueError`(中文说明 + 可选值)。"""
    band = find_band(key)
    if band is None:
        raise ValueError(f"未知距离分段:{key!r}(可选:{'、'.join(band_keys())})")
    return band


def band_radius_m(band: Mapping[str, Any]) -> float:
    """该分段的 Overpass 检索半径(米)= 上限半径。"""
    return float(band["high"]) * 1000.0


def in_band(distance_km: float, band: Mapping[str, Any]) -> bool:
    """距离是否落在环内(``low <= d < high``,互斥、不含城区)。"""
    return float(band["low"]) <= float(distance_km) < float(band["high"])


def filter_to_band(
    items: Iterable[Mapping[str, Any]],
    origin_lat: float,
    origin_lng: float,
    band: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """把"上限半径内"的候选收敛到环内:补 ``distance_km`` 并按由近及远排序。"""
    kept: list[dict[str, Any]] = []
    for item in items or []:
        distance = haversine_km(float(origin_lat), float(origin_lng), float(item["lat"]), float(item["lng"]))
        if not in_band(distance, band):
            continue
        row = dict(item)
        row["distance_km"] = round(distance, DISTANCE_PRECISION)
        kept.append(row)
    kept.sort(key=lambda row: (row["distance_km"], str(row.get("name") or "")))
    return kept
