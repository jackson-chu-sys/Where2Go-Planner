"""地图模式 API:目的地检索(`/api/places`)+ 元信息 + 起点地理编码(`/api/geocode`)。

与 POC 的 `/api/discover`、`/api/categories` 并存,四条路由:

* ``GET /api/places?origin=&band=&category=`` —— 返回该 (城市, band) 内**已入库**的目的地
  (``category`` 为四分类优先级归类的结果,一地只属一类);未入库时按需抓一次 Overpass
  四分类 tag 并集落库,之后同一 (城市, band) 直接读 SQLite、不触网。
  可选 ``lat``/``lng``(前端已地理编码过就带上,省一次 Nominatim)、``refresh=true``(强制重抓)
  与 ``intros=false``(抓取后不调 LLM 补简介)。
* ``GET /api/places/meta`` —— 分段、四分类(含 pin 颜色/图标)、归类优先级、检索分组与
  LLM 简介配置的元信息(前端下拉/图例/状态栏的唯一出处,**不含任何 key**)。
* ``GET /api/places/intros?origin=&band=`` —— 给已入库但还没有简介的 POI 补 LLM 一句话简介
  (DB 即缓存,已有简介的不再调用;失败降级为空简介)。
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
from services import intro as intro_service
from services import place_loader
from services.bands import DISTANCE_BANDS, band_keys, find_band
from services.classify import (
    CATEGORIES,
    CATEGORY_PRIORITY,
    category_keys,
    is_known_category,
    search_budget,
    search_groups,
)

router = APIRouter()

PLACES_NOTE = (
    "已入库的 (城市, band) 直接读 SQLite、不再触网;distance_km 为距起点的大圆直线距离"
    "(环形分段,不含城区)。分类为四分类优先级归类(滑雪 > 运动 > 人文美食 > 自然),"
    "按 OSM (type, id) 去重,一地只属一类;intro 为 LLM 一句话简介,按 POI 缓存。"
)
META_NOTE = (
    "分段:环形互斥,检索按 band 上限半径一次查四分类 tag 并集(每组独立配额);"
    "分类:四分类优先级归类的可选值(color/emoji 供前端 pin 使用);"
    "简介:LLM 生成后缓存在 Place.intro,已有简介不再调用,失败降级为空。"
)
INTROS_NOTE = (
    "只给 intro 为空的 POI 调 LLM(DB 即缓存);网络/额度失败降级为空简介,下次可重试。"
)


def _clean(text: Optional[str]) -> Optional[str]:
    value = (text or "").strip()
    return value or None


def resolve_intro_limit(limit: Optional[int]) -> Optional[int]:
    """本次抓取最多补多少条简介:留空用默认批量,``0`` = 不限(冷启动全量)。"""
    if limit is None:
        return place_loader.INTRO_BATCH_LIMIT
    return None if int(limit) <= 0 else int(limit)


@router.get("/places/meta")
def places_meta() -> dict[str, Any]:
    """分段 + 四分类 + 归类优先级 + 检索分组 + LLM 配置(前端图例/状态栏的唯一出处)。"""
    return {
        "bands": [dict(band) for band in DISTANCE_BANDS],
        "categories": [dict(item) for item in CATEGORIES],
        "category_priority": list(CATEGORY_PRIORITY),
        "search_tags": place_loader.SEARCH_TAGS,
        "search_groups": [
            {"group": group["group"], "category": group["category"], "budget": group["budget"],
             "selectors": len(group["tags"])}
            for group in search_groups()
        ],
        "search_budget": search_budget(),
        "fetch_limit": place_loader.FETCH_LIMIT,
        "llm": intro_service.describe_llm(),
        "note": META_NOTE,
    }


@router.get("/places/intros")
def fill_intros(
    origin: str = Query(..., min_length=1, description="起点城市名,如:上海"),
    band: Optional[str] = Query(None, description="距离分段 key,留空 = 该城市全部分段"),
    category: Optional[str] = Query(None, description="只补某个分类"),
    limit: Optional[int] = Query(None, ge=0, description="最多补多少条(0/留空 = 不限)"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """给已入库但缺简介的 POI 补 LLM 一句话简介(已有简介的不重复调用)。"""
    city = _clean(origin)
    if not city:
        raise HTTPException(400, "起点城市不能为空")
    wanted_band = _clean(band)
    if wanted_band and not find_band(wanted_band):
        raise HTTPException(400, f"未知距离分段:{band}(可选:{'、'.join(band_keys())})")
    wanted_category = _clean(category)
    if wanted_category and not is_known_category(wanted_category):
        raise HTTPException(400, f"未知分类:{category}(可选:{'、'.join(category_keys())})")

    started = time.monotonic()
    stats = intro_service.fill_missing_intros(
        session,
        origin_city=city,
        band=wanted_band,
        category=wanted_category,
        limit=(limit or None),
    )
    return {
        "origin_city": city,
        "band": wanted_band,
        "category": wanted_category,
        **stats,
        "elapsed_s": round(time.monotonic() - started, 2),
        "note": INTROS_NOTE,
    }


@router.get("/places")
def list_places(
    origin: str = Query(..., min_length=1, description="起点城市名,如:上海"),
    band: str = Query(..., description="距离分段 key:50_100 / 100_200 / 200_300 / 300_500"),
    category: Optional[str] = Query(None, description="分类过滤,留空返回该段全部"),
    lat: Optional[float] = Query(None, ge=-90, le=90, description="起点纬度(可选,免二次地理编码)"),
    lng: Optional[float] = Query(None, ge=-180, le=180, description="起点经度(可选)"),
    refresh: bool = Query(False, description="true = 强制重新抓取(会触网)"),
    intros: bool = True,
    intro_limit: Optional[int] = None,
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
            intros=intros,
            intro_limit=resolve_intro_limit(intro_limit),
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
        "intro_stats": outcome.intro_stats,
        "intro_pending": repo.count_places(
            session, origin_city=city, band=band_def["key"], missing_intro=True
        ),
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
