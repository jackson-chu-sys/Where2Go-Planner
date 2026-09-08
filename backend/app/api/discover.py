"""试用 API:发现周边目的地(环形距离分段,复用免费源)。"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import time
from typing import Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from data_sources import (
    DataSourceError,
    geocode as ds_geocode,
    haversine_km,
    nearby_places as ds_nearby,
    route as ds_route,
)

router = APIRouter()

CATEGORIES: dict[str, dict[str, Any]] = {
    "自然风光": {"tags": [{"natural": "peak"}, {"natural": "waterfall"}], "element_types": "node",
              "label": "山峰 / 瀑布"},
    "旅游景点": {"tags": [{"tourism": "attraction"}, {"tourism": "viewpoint"}], "element_types": "nwr",
              "label": "景点 / 观景点"},
}

# 环形分段(km):[low, high) —— 互斥,不含更低段。low 起自 50 以排除当前城市内。
# "upper" 是 Overpass 检索半径上限(需覆盖整个分段)。
DISTANCE_BANDS: list[dict[str, Any]] = [
    {"key": "50_100",  "label": "50-100 km",  "low": 50,  "high": 100},
    {"key": "100_200", "label": "100-200 km", "low": 100, "high": 200},
    {"key": "200_300", "label": "200-300 km", "low": 200, "high": 300},
    {"key": "300_500", "label": "300-500 km", "low": 300, "high": 500},
]
FETCH_LIMIT = 400   # Overpass 多取,留出过滤余量
SHOW_TOP = 8

# 简单进程内缓存:key = (城市, band_key, 类别) -> 过滤后的 POI 列表(带 distance_km)
_cache: dict[tuple, list[dict[str, Any]]] = {}
CACHE_TTL_S = 600


class DiscoverReq(BaseModel):
    city: str = Field(default="上海", min_length=1)
    band: str = Field(default="50_100")          # DISTANCE_BANDS 的 key
    category: str = Field(default="自然风光")


def _clean(text: str) -> str:
    return (text or "").strip()


@router.get("/categories")
def categories():
    return {"categories": [{"key": k, "label": v["label"]} for k, v in CATEGORIES.items()],
            "bands": DISTANCE_BANDS}


def _find_places(origin: dict, band: dict, cat: dict) -> list[dict[str, Any]]:
    """按分段检索并过滤:Overpass 用上限半径,本地按 haversine 过滤出 [low, high)。"""
    # 缓存检查
    ck = (_clean(origin.get("city", "")), band["key"], cat["label"])
    cached = _cache.get(ck)
    now = time.time()
    if cached and (now - cached[1]) < CACHE_TTL_S:
        return cached[0]

    upper_m = band["high"] * 1000.0
    raw = ds_nearby(
        origin["lat"], origin["lng"], upper_m,
        cat["tags"], limit=FETCH_LIMIT, require_name=True,
        element_types=cat["element_types"],
    )
    low, high = band["low"], band["high"]
    filtered = []
    for p in raw:
        d = haversine_km(origin["lat"], origin["lng"], p["lat"], p["lng"])
        if low <= d < high:
            item = {"name": p["name"], "lat": p["lat"], "lng": p["lng"],
                    "distance_km": round(d, 1)}
            filtered.append(item)
    # 近→远
    filtered.sort(key=lambda it: it["distance_km"])
    _cache[ck] = (filtered, now)
    return filtered


def _add_routes(origin: dict, places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """并行给前 SHOW_TOP 个目的地请求驾车路线。"""
    shown = places[:SHOW_TOP]
    result: list[dict[str, Any]] = []

    def one(p: dict) -> dict[str, Any]:
        out = dict(p)
        out["route"] = None
        try:
            leg = ds_route((origin["lng"], origin["lat"]), (p["lng"], p["lat"]))
            out["route"] = {"distance_km": round(leg["distance_km"], 1),
                            "duration_min": round(leg["duration_min"], 1)}
        except (DataSourceError, ValueError):
            pass
        return out

    with ThreadPoolExecutor(max_workers=SHOW_TOP) as ex:
        result = list(ex.map(one, shown))
    return result


@router.post("/discover")
def discover(req: DiscoverReq):
    cat = CATEGORIES.get(_clean(req.category))
    if not cat:
        raise HTTPException(400, f"未知类别:{req.category}")
    band = next((b for b in DISTANCE_BANDS if b["key"] == _clean(req.band)), None)
    if not band:
        raise HTTPException(400, f"未知距离分段:{req.band}")

    started = time.monotonic()
    try:
        geo = ds_geocode(_clean(req.city))
    except (DataSourceError, ValueError) as exc:
        raise HTTPException(502, f"无法解析城市 '{req.city}': {exc}") from exc
    origin = {"city": _clean(req.city), "name": geo["display_name"],
              "lat": geo["lat"], "lng": geo["lng"]}

    try:
        places = _find_places(origin, band, cat)
    except (DataSourceError, ValueError) as exc:
        raise HTTPException(502, f"目的地检索失败(公共数据源繁忙,请稍后重试): {exc}") from exc

    enriched = _add_routes(origin, places) if places else []

    return {
        "origin": origin,
        "category": req.category,
        "band_label": band["label"],
        "band_km": {"low": band["low"], "high": band["high"]},
        "results": enriched,
        "total_found": len(places),
        "elapsed_s": round(time.monotonic() - started, 1),
        "note": "驾车时间为 OSRM 免费估算(非实时路况);目的地来自 OpenStreetMap;距离为环形分段,不含城市内部。",
    }
