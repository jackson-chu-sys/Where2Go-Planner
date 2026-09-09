"""阶段1a 地图模式 API:目的地检索(`/api/places`)+ 起点地理编码(`/api/geocode`)。

与 POC 的 `/api/discover`、`/api/categories` 并存(不改其行为),三条新路由:

* ``GET /api/places?origin=&band=&category=`` —— 返回该 (城市, band) 内**已入库**的目的地;
  未入库时按需抓一次 Overpass 落库,之后同一 (城市, band) 直接读 SQLite、不触网。
  可选 ``lat``/``lng``(前端已地理编码过就带上,省一次 Nominatim)与 ``refresh=true``(强制重抓)。
* ``GET /api/places/meta`` —— 分段与分类元信息(前端下拉/图例的唯一出处)。
* ``GET /api/geocode?city=`` —— 起点城市搜索,复用 POC 的 Nominatim 接口,
  并回报该城市哪些分段已入库(前端可提示"即时读库"还是"首次抓取")。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from data_sources import DataSourceError
from db import repository as repo
from db.base import get_session
from services import place_loader
from services.bands import DISTANCE_BANDS
from services.categories import CATEGORIES, category_keys, is_known_category

router = APIRouter()

PLACES_NOTE = (
    "已入库的 (城市, band) 直接读 SQLite、不再触网;distance_km 为距起点的大圆直线距离"
    "(环形分段,不含城区)。分类为阶段1a 简化归类,TASK-1b 换成四分类优先级去重 + LLM 简介。"
)
META_NOTE = (
    "分段:环形互斥,检索按上限半径;分类:阶段1a 简化归类的可选值(color/emoji 供前端 pin 使用)。"
)


def _clean(text: Optional[str]) -> Optional[str]:
    value = (text or "").strip()
    return value or None


@router.get("/places/meta")
def places_meta() -> dict[str, Any]:
    """分段 + 分类元信息(前端下拉、图例、pin 颜色的唯一出处)。"""
    return {
        "bands": [dict(band) for band in DISTANCE_BANDS],
        "categories": [dict(item) for item in CATEGORIES],
        "search_tags": place_loader.SEARCH_TAGS,
        "fetch_limit": place_loader.FETCH_LIMIT,
        "note": META_NOTE,
    }


@router.get("/places")
def list_places(
    origin: str = Query(..., min_length=1, description="起点城市名,如:上海"),
    band: str = Query(..., description="距离分段 key:50_100 / 100_200 / 200_300 / 300_500"),
    category: Optional[str] = Query(None, description="分类过滤,留空返回该段全部"),
    lat: Optional[float] = Query(None, ge=-90, le=90, description="起点纬度(可选,免二次地理编码)"),
    lng: Optional[float] = Query(None, ge=-180, le=180, description="起点经度(可选)"),
    refresh: bool = Query(False, description="true = 强制重新抓取(会触网)"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """返回该 (城市, band) 内已入库的目的地(可按分类过滤)。"""
    city = _clean(origin)
    if not city:
        raise HTTPException(400, "起点城市不能为空")
    wanted_category = _clean(category)
    if wanted_category and not is_known_category(wanted_category):
        raise HTTPException(
            400, f"未知分类:{category}(可选:{'、'.join(category_keys())})"
        )

    started = time.monotonic()
    try:
        outcome = place_loader.load_segment(
            session,
            city=city,
            band=_clean(band) or "",
            category=wanted_category,
            lat=lat,
            lng=lng,
            refresh=refresh,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except DataSourceError as exc:
        raise HTTPException(502, f"目的地检索失败(公共数据源繁忙,请稍后重试):{exc}") from exc

    band_def = outcome.band
    return {
        "origin": outcome.origin,
        "band": {
            "key": band_def["key"],
            "label": band_def["label"],
            "low_km": band_def["low"],
            "high_km": band_def["high"],
        },
        "category": wanted_category,
        "places": outcome.places,
        "count": len(outcome.places),
        "counts_by_category": outcome.counts_by_category,
        "source": outcome.source,
        "network_used": outcome.network_used,
        "fetched_at": outcome.fetched_at,
        "written": outcome.written,
        "elapsed_s": round(time.monotonic() - started, 2),
        "note": PLACES_NOTE,
    }


@router.get("/geocode")
def geocode_city(
    city: str = Query(..., min_length=1, description="城市名,如:北京"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """起点城市搜索(Nominatim),并回报该城市已入库的分段。"""
    cleaned = _clean(city)
    if not cleaned:
        raise HTTPException(400, "城市名不能为空")
    try:
        origin = place_loader.resolve_origin(cleaned)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except DataSourceError as exc:
        raise HTTPException(502, f"无法解析城市 '{cleaned}':{exc}") from exc
    return {
        "origin": origin,
        "bands": [dict(band) for band in DISTANCE_BANDS],
        "segments": repo.segment_overview(session, origin_city=cleaned),
    }
