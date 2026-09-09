"""试用 API:发现周边目的地(环形距离分段 + 多交通方式估算)。"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import time
from typing import Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from data_sources import (
    DataSourceError,
    geocode as ds_geocode,
    haversine_km,
    nearby_places as ds_nearby,
    route as ds_route,
)
from services.bands import DISTANCE_BANDS as BANDS

router = APIRouter()

CATEGORIES: dict[str, dict[str, Any]] = {
    "自然风光": {"tags": [{"natural": "peak"}, {"natural": "waterfall"}], "element_types": "node",
              "label": "山峰 / 瀑布"},
    "旅游景点": {"tags": [{"tourism": "attraction"}, {"tourism": "viewpoint"}], "element_types": "nwr",
              "label": "景点 / 观景点"},
}

# 分段定义只出一份(services.bands),POC 路由与入库/地图 API 共用同一口径。
DISTANCE_BANDS: list[dict[str, Any]] = BANDS
FETCH_LIMIT = 400
SHOW_TOP = 8

# 交通方式估算阈值(km,按 POI 直线距离)与经验系数 —— 无真实班次源,纯估算
RAIL_MIN_KM = 100.0   # >= 触发铁路估算
FLIGHT_MIN_KM = 300.0  # >= 触发飞机估算
RAIL_DETOUR = 1.20     # 铁路路径绕行系数(相对直线)
RAIL_SPEED_KMH = 220.0  # 城际均速(含中间停靠)
RAIL_GROUND_MIN = 110.0  # 候车 + 起/终点站与景点接驳
FLIGHT_DETOUR = 1.10
FLIGHT_SPEED_KMH = 780.0
FLIGHT_GROUND_MIN = 210.0  # 值机安检 + 两端机场接驳

_cache: dict[tuple, list[dict[str, Any]]] = {}
CACHE_TTL_S = 600


class DiscoverReq(BaseModel):
    city: str = Field(default="上海", min_length=1)
    band: str = Field(default="50_100")
    category: str = Field(default="自然风光")


def _clean(text: str) -> str:
    return (text or "").strip()


def _est_mode(mode: str, straight_km: float) -> dict[str, Any]:
    """铁路/飞机耗时估算(分钟)。纯几何 + 经验系数,无真实班次。"""
    if mode == "rail":
        km = straight_km * RAIL_DETOUR
        dur = km / RAIL_SPEED_KMH * 60.0 + RAIL_GROUND_MIN
        return {"mode": "rail", "label": "铁路(估算)",
                "duration_min": round(dur), "distance_km": round(straight_km),
                "note": f"按直线{round(straight_km)}km、均速{RAIL_SPEED_KMH:g}km/h + 接驳估算,无实时班次"}
    # flight
    km = straight_km * FLIGHT_DETOUR
    dur = km / FLIGHT_SPEED_KMH * 60.0 + FLIGHT_GROUND_MIN
    return {"mode": "flight", "label": "飞机(估算)",
            "duration_min": round(dur), "distance_km": round(straight_km),
            "note": f"按直线{round(straight_km)}km、巡航{FLIGHT_SPEED_KMH:g}km/h + 值机/接驳估算,无实时班次"}


@router.get("/categories")
def categories():
    return {"categories": [{"key": k, "label": v["label"]} for k, v in CATEGORIES.items()],
            "bands": DISTANCE_BANDS}


def _find_places(origin: dict, band: dict, cat: dict) -> list[dict[str, Any]]:
    ck = (origin.get("city", ""), band["key"], cat["label"])
    cached = _cache.get(ck)
    now = time.time()
    if cached and (now - cached[1]) < CACHE_TTL_S:
        return cached[0]
    upper_m = band["high"] * 1000.0
    raw = ds_nearby(origin["lat"], origin["lng"], upper_m,
                    cat["tags"], limit=FETCH_LIMIT, require_name=True,
                    element_types=cat["element_types"])
    low, high = band["low"], band["high"]
    filtered = []
    for p in raw:
        d = haversine_km(origin["lat"], origin["lng"], p["lat"], p["lng"])
        if low <= d < high:
            filtered.append({"name": p["name"], "lat": p["lat"], "lng": p["lng"],
                             "distance_km": round(d, 1)})
    filtered.sort(key=lambda it: it["distance_km"])
    _cache[ck] = (filtered, now)
    return filtered


def _driving_route(origin: dict, p: dict) -> dict[str, Any]:
    try:
        leg = ds_route((origin["lng"], origin["lat"]), (p["lng"], p["lat"]))
        return {"mode": "driving", "label": "驾车",
                "duration_min": round(leg["duration_min"]), "distance_km": round(leg["distance_km"]),
                "note": "OSRM 免费估算(非实时路况)"}
    except (DataSourceError, ValueError):
        return {"mode": "driving", "label": "驾车", "duration_min": None,
                "distance_km": None, "note": "驾车路线获取失败"}


def _enrich_modes(origin: dict, places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给每个目的地生成交通方式列表:驾车(始终,并行 OSRM) + 铁路/飞机(估算,按阈值)。"""
    shown = places[:SHOW_TOP]
    result: list[dict[str, Any]] = []

    def one(p: dict) -> dict[str, Any]:
        out = {"name": p["name"], "lat": p["lat"], "lng": p["lng"],
               "distance_km": p["distance_km"], "modes": []}
        # 驾车走并行池外,先占位
        out["modes"].append("__DRIVING__")
        d = p["distance_km"]
        if d >= RAIL_MIN_KM:
            out["modes"].append(_est_mode("rail", d))
        if d >= FLIGHT_MIN_KM:
            out["modes"].append(_est_mode("flight", d))
        return out

    base = [one(p) for p in shown]
    # 并行补驾车
    driving = []
    with ThreadPoolExecutor(max_workers=max(1, SHOW_TOP)) as ex:
        driving = list(ex.map(lambda p: _driving_route(origin, p), shown))
    for item, drv in zip(base, driving):
        item["modes"][0] = drv
    return base


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
    enriched = _enrich_modes(origin, places) if places else []
    return {
        "origin": origin, "category": req.category,
        "band_label": band["label"], "band_km": {"low": band["low"], "high": band["high"]},
        "results": enriched, "total_found": len(places),
        "elapsed_s": round(time.monotonic() - started, 1),
        "mode_rules": {"rail_min_km": RAIL_MIN_KM, "flight_min_km": FLIGHT_MIN_KM},
        "note": "驾车为 OSRM 估算;铁路/飞机为经验估算(无实时班次);距离为环形分段,不含城市内部。",
    }
