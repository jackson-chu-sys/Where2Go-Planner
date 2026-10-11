"""OTA 外链解析服务(TASK-17):住宿收藏 → 携程原生 hotelId/cityId。

神朱 2026-10-09 拍板:城市要传 OTA **原生城市字段**,不再拼进搜索词(实测经常
搜不到)。调研(docs/research-ota-wechat-2026-10-09.md §A1)确认携程 deep-link 最可靠
形态是 ``/hotels/detail/?hotelId=``,hotelId/cityId 走公开 suggest 接口解析
(:mod:`data_sources.ctrip`,免登录、任何失败降级 None 不抛)。

放服务层的原因:``app/api/collections.py`` 有架构护栏(单测断言收藏 API 不 import
data_sources/requests)——触网逻辑一律经 services,与 place_loader(逆地理懒补齐
city)同一口径。

缓存:进程内 TTL(成功 7 天 / 失败 1h,仿 OriginCache);解析结果本身会落收藏
``summary`` 持久化(ctrip_hotel_id/ctrip_city_id/ctrip_city_name),已有标识的行
零网络。每次懒补齐最多 :data:`CTRIP_BACKFILL_BATCH` 条,不拖慢面板响应。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Optional

from sqlalchemy.orm import Session

from data_sources import ctrip
from db import models

CTRIP_BACKFILL_BATCH = 5
_CACHE_TTL_S = 7 * 24 * 3600
_CACHE_NEG_TTL_S = 3600
_cache: dict[str, tuple[Optional[dict[str, Any]], float]] = {}
_cache_lock = threading.Lock()


def plain_stay_name(name: Any) -> str:
    """收藏名 → 酒店纯名(剥 🛏️ 前缀与「· 住宿」后缀;与前端 plainFavName 同口径)。

    ⚠️ emoji 前缀必须用**交替序列**而非字符类(JS/Python 同坑:无 u 标志的字符类
    按 UTF-16 单元拆代理对)。
    """
    text = str(name or "").strip()
    text = re.sub(r"^(?:🛏️|🛏|🏨)\s*", "", text)
    text = re.sub(r"\s*·\s*住宿\s*$", "", text)
    return text.strip()


def is_stay_row(row: Any) -> bool:
    """住宿收藏判定(与 app.api.collections._is_stay_row / 前端 favGroupOf 同口径)。"""
    if str(row.get("kind") or "") != models.KIND_PLACE:
        return False
    summary = row.get("summary") or {}
    if isinstance(summary, dict):
        if summary.get("stay_kind") is not None or summary.get("price_estimate"):
            return True
    name = str(row.get("name") or "")
    return name.startswith("🛏️") or "🛏" in name


def ctrip_ids_for_stay(name: str, city: str, lat: Any, lng: Any) -> Optional[dict[str, Any]]:
    """「城市+酒店纯名」→ 携程 ``{hotel_id, city_id, city_name}``;失败 ``None`` 不抛。

    关键词两级(2026-10-09 实测):①``城市+酒店名``(与前端旧搜索词同口径,城市前缀
    能消歧);②失败再试**纯酒店名**——实测城市名与酒店名重复时(「上海冰雪世界皇冠
    假日酒店」自带城市前缀 →「上海上海…」)suggest 反而解析不到,纯名可命中。
    """
    hotel = str(name or "").strip()
    if not hotel:
        return None
    city = str(city or "").strip()
    keywords = [f"{city}{hotel}"] if city and not hotel.startswith(city) else []
    keywords.append(hotel)
    for keyword in keywords:
        found = _suggest_cached(keyword, city, lat, lng)
        if found:
            return found
    return None


def ctrip_city_for_stay(city: str) -> Optional[dict[str, Any]]:
    """城市名 → 携程**境外城市** ``{city_id, city_name, country_name}``;失败 ``None`` 不抛。

    TASK-24(境外双轨 P5):境外小体量酒店常解析不到 Hotel 条目,城市级 cityId 可让
    前端退到 list 页(``cityId+searchWord``)不丢链接。**只认 IntlCity**——境内城市
    自然返回 None,境内口径零影响。
    """
    text = str(city or "").strip()
    if not text:
        return None
    now = time.time()
    key = f"city:{text}"
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]
    result: Optional[dict[str, Any]] = None
    try:
        found = ctrip.suggest_city(text)
        if found and found.get("city_id"):
            result = {
                "city_id": int(found["city_id"]),
                "city_name": str(found.get("city_name") or ""),
                "country_name": str(found.get("country_name") or ""),
            }
    except Exception:  # noqa: BLE001 - 尽力而为的增强源:任何失败只留空
        result = None
    ttl = _CACHE_TTL_S if result else _CACHE_NEG_TTL_S
    with _cache_lock:
        _cache[key] = (result, now + ttl)
    return result


def _suggest_cached(keyword: str, city: str, lat: Any, lng: Any) -> Optional[dict[str, Any]]:
    """suggest_hotel 的进程内 TTL 缓存壳(成功 7 天/失败 1h);失败 None 不抛。"""
    now = time.time()
    with _cache_lock:
        cached = _cache.get(keyword)
        if cached is not None and cached[1] > now:
            return cached[0]
    result: Optional[dict[str, Any]] = None
    try:
        found = ctrip.suggest_hotel(
            keyword,
            lat=lat if isinstance(lat, (int, float)) else None,
            lng=lng if isinstance(lng, (int, float)) else None,
            city_hint=city or None,
        )
        if found and found.get("hotel_id"):
            result = {
                "hotel_id": str(found["hotel_id"]),
                "city_id": int(found["city_id"]),
                "city_name": str(found.get("city_name") or ""),
            }
    except Exception:  # noqa: BLE001 - 尽力而为的增强源:任何失败只留空
        result = None
    ttl = _CACHE_TTL_S if result else _CACHE_NEG_TTL_S
    with _cache_lock:
        _cache[keyword] = (result, now + ttl)
    return result


def backfill_stay_ctrip_ids(session: Session) -> int:
    """住宿收藏懒补齐携程标识(写回 ``summary.ctrip_hotel_id``/``ctrip_city_id``/
    ``ctrip_city_name`` 持久化)。返回本次写回行数;每请求最多
    :data:`CTRIP_BACKFILL_BATCH` 条(其余下次开面板再补),解析失败留空不阻塞。
    已有 ctrip_hotel_id 的行零网络。"""
    rows = (
        session.query(models.Collection)
        .filter(models.Collection.kind == models.KIND_PLACE)
        .all()
    )
    updated = 0
    for row in rows:
        if updated >= CTRIP_BACKFILL_BATCH:
            break
        summary = dict(row.summary or {})
        if not is_stay_row({"kind": row.kind, "summary": summary, "name": row.name}):
            continue
        if str(summary.get("ctrip_hotel_id") or "").strip():
            continue  # 已解析过(永久缓存),零网络
        if summary.get("ctrip_city_id") and summary.get("ctrip_city_fallback"):
            continue  # TASK-24:城市级兜底已落(境外 IntlCity,永久缓存),零网络
        hotel = plain_stay_name(row.to_name or summary.get("name") or row.name)
        city = str(summary.get("city") or "").strip()
        found = ctrip_ids_for_stay(hotel, city, row.to_lat, row.to_lng)
        if not found:
            # TASK-24:酒店级解析不到 → 境外城市级兜底(IntlCity;境内城市自然
            # 返回 None,境内口径零影响),前端可退 list 页 cityId+searchWord。
            city_found = ctrip_city_for_stay(city) if city else None
            if not city_found:
                continue  # 都解析不到:负缓存 1h 内不重打,下次开面板再试
            summary["ctrip_city_id"] = city_found["city_id"]
            summary["ctrip_city_name"] = city_found.get("city_name") or city
            summary["ctrip_city_fallback"] = True
            if city_found.get("country_name"):
                summary["country"] = city_found["country_name"]
            row.summary = summary
            updated += 1
            continue
        summary["ctrip_hotel_id"] = found["hotel_id"]
        summary["ctrip_city_id"] = found["city_id"]
        if found.get("city_name"):
            summary["ctrip_city_name"] = found["city_name"]
        row.summary = summary  # 整 dict 重赋值,JSON 列的变更才会被 SQLAlchemy 追踪
        updated += 1
    if updated:
        session.commit()
    return updated
