"""``Place`` / ``SegmentFetch`` 的读写封装。

只碰 DB,不碰网络(抓取编排在 :mod:`services.place_loader`):

* :func:`upsert_places` —— 按唯一键 ``(osm_type, osm_id, origin_city)`` 防重写入,
  同批内重复也只落一行;已有 ``intro`` 不被覆盖(TASK-1b 的 LLM 简介是缓存,重抓不重算);
* :func:`list_places` —— 按 (城市, band, 分类) 查询,并按大圆距离升序返回
  (距离复用 :func:`data_sources.haversine_km`,与抓取时的环形过滤同一口径);
* :func:`select_places` / :func:`count_places` —— 返回 ORM 行的查询,给
  TASK-1b 的重归类与 LLM 简介回填用(``missing_intro=True`` 只取还没简介的 POI,
  **DB 就是简介缓存**:已有 ``intro`` 的行不会被再送去调 LLM);
* :func:`get_segment` / :func:`record_segment` —— (城市, band) 抓取水位的读与记。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from data_sources import haversine_km

from .models import (
    COORD_PRECISION,
    OSM_SOURCE,
    SEED_SOURCE,
    UNCATEGORIZED,
    Place,
    SegmentFetch,
    iso_utc,
    place_source,
    utcnow,
)

DISTANCE_PRECISION = 1
REQUIRED_ITEM_KEYS = ("osm_type", "osm_id", "lat", "lng")


def _clean_city(origin_city: str) -> str:
    city = (origin_city or "").strip()
    if not city:
        raise ValueError("origin_city 不能为空")
    return city


def place_to_dict(
    place: Place,
    *,
    origin_lat: Optional[float] = None,
    origin_lng: Optional[float] = None,
) -> dict[str, Any]:
    """ORM 行 → API/前端用的 dict;给了起点坐标就顺带算 ``distance_km``。

    ``source`` 是派生的来源标注(``种子`` / ``OSM``,见 :func:`db.models.place_source`),
    前端 popup 用它给人工补录的种子数据打上"来源=种子"的徽章。
    """
    distance_km: Optional[float] = None
    if origin_lat is not None and origin_lng is not None:
        distance_km = round(
            haversine_km(float(origin_lat), float(origin_lng), place.lat, place.lng),
            DISTANCE_PRECISION,
        )
    return {
        "id": place.id,
        "osm_type": place.osm_type,
        "osm_id": place.osm_id,
        "name": place.name,
        "lat": place.lat,
        "lng": place.lng,
        "category": place.category,
        "intro": place.intro,
        "tags": dict(place.tags or {}),
        "source": place_source(place.tags),
        "origin_city": place.origin_city,
        "band": place.band,
        "distance_km": distance_km,
    }


def upsert_places(
    session: Session,
    *,
    origin_city: str,
    band: str,
    items: Iterable[Mapping[str, Any]],
) -> int:
    """批量写入目的地,按唯一键防重;返回受影响行数(新增 + 更新)。"""
    city = _clean_city(origin_city)
    rows = [dict(item) for item in (items or [])]
    if not rows:
        session.flush()
        return 0
    for row in rows:
        missing = [key for key in REQUIRED_ITEM_KEYS if row.get(key) is None]
        if missing:
            raise ValueError(f"目的地缺少必填字段 {missing}:{row!r}")

    ids = {int(row["osm_id"]) for row in rows}
    types = {str(row["osm_type"]) for row in rows}
    existing: dict[tuple[str, int], Place] = {
        (place.osm_type, place.osm_id): place
        for place in session.scalars(
            select(Place).where(
                Place.origin_city == city,
                Place.osm_type.in_(sorted(types)),
                Place.osm_id.in_(sorted(ids)),
            )
        )
    }

    touched = 0
    for row in rows:
        key = (str(row["osm_type"]), int(row["osm_id"]))
        place = existing.get(key)
        if place is None:
            place = Place(osm_type=key[0], osm_id=key[1], origin_city=city, created_at=utcnow())
            existing[key] = place
            session.add(place)
        place.name = str(row.get("name") or "").strip()[:255]
        place.lat = round(float(row["lat"]), COORD_PRECISION)
        place.lng = round(float(row["lng"]), COORD_PRECISION)
        place.category = str(row.get("category") or UNCATEGORIZED).strip() or UNCATEGORIZED
        place.tags = dict(row.get("tags") or {})
        place.band = str(band)
        intro = str(row.get("intro") or "").strip()
        if intro:
            place.intro = intro
        place.updated_at = utcnow()
        touched += 1

    session.flush()
    return touched


def list_places(
    session: Session,
    *,
    origin_city: str,
    band: Optional[str] = None,
    category: Optional[str] = None,
    origin_lat: Optional[float] = None,
    origin_lng: Optional[float] = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """按 (城市, band, 分类) 查询已入库目的地,按距起点由近及远排序。"""
    city = _clean_city(origin_city)
    stmt = select(Place).where(Place.origin_city == city)
    if band:
        stmt = stmt.where(Place.band == str(band))
    if category:
        stmt = stmt.where(Place.category == str(category))
    places = list(session.scalars(stmt))
    rows = [
        place_to_dict(place, origin_lat=origin_lat, origin_lng=origin_lng) for place in places
    ]
    rows.sort(key=lambda row: (row["distance_km"] is None, row["distance_km"] or 0.0, row["name"]))
    return rows[:limit] if limit else rows


def _missing_intro_clause():
    """``intro`` 为空的判定:NULL 或只有空白字符(空串不算已缓存)。"""
    return or_(Place.intro.is_(None), func.trim(Place.intro) == "")


def select_places(
    session: Session,
    *,
    origin_city: Optional[str] = None,
    band: Optional[str] = None,
    category: Optional[str] = None,
    missing_intro: bool = False,
    limit: Optional[int] = None,
) -> list[Place]:
    """按条件取 ``Place`` **ORM 行**(需要就地改字段时用这个,不是 :func:`list_places`)。

    ``missing_intro=True`` 只返回还没简介的行(LLM 简介按 POI 缓存的读侧)。
    排序按 (城市, band, id),保证批量回填的顺序稳定、可重复。
    """
    stmt = select(Place)
    if origin_city:
        stmt = stmt.where(Place.origin_city == _clean_city(origin_city))
    if band:
        stmt = stmt.where(Place.band == str(band))
    if category:
        stmt = stmt.where(Place.category == str(category))
    if missing_intro:
        stmt = stmt.where(_missing_intro_clause())
    stmt = stmt.order_by(Place.origin_city, Place.band, Place.id)
    if limit:
        stmt = stmt.limit(max(1, int(limit)))
    return list(session.scalars(stmt))


def count_places(
    session: Session,
    *,
    origin_city: Optional[str] = None,
    band: Optional[str] = None,
    category: Optional[str] = None,
    missing_intro: Optional[bool] = None,
) -> int:
    """计数版 :func:`select_places`(前端"还有 N 条待生成简介"用)。"""
    stmt = select(func.count(Place.id))
    if origin_city:
        stmt = stmt.where(Place.origin_city == _clean_city(origin_city))
    if band:
        stmt = stmt.where(Place.band == str(band))
    if category:
        stmt = stmt.where(Place.category == str(category))
    if missing_intro is True:
        stmt = stmt.where(_missing_intro_clause())
    elif missing_intro is False:
        stmt = stmt.where(~_missing_intro_clause())
    return int(session.scalar(stmt) or 0)


def count_by_category(
    session: Session, *, origin_city: str, band: Optional[str] = None
) -> dict[str, int]:
    """某城市(可限定 band)已入库目的地的分类计数,给前端图例用。"""
    city = _clean_city(origin_city)
    stmt = (
        select(Place.category, func.count(Place.id))
        .where(Place.origin_city == city)
        .group_by(Place.category)
    )
    if band:
        stmt = stmt.where(Place.band == str(band))
    return {str(category): int(total) for category, total in session.execute(stmt)}


def count_by_source(
    session: Session,
    *,
    origin_city: Optional[str] = None,
    band: Optional[str] = None,
    category: Optional[str] = None,
) -> dict[str, int]:
    """按来源(``OSM`` / ``种子``)计数,给前端状态栏与图例用。

    ``tags`` 是 JSON 列,这里不做方言相关的 JSON 索引查询,直接取回 ``tags``
    在 Python 里判定(单个 (城市, band) 的行数量级只有几百条);两个键恒在,
    没有种子数据时就是 ``0``,前端不必判空。
    """
    stmt = select(Place.tags)
    if origin_city:
        stmt = stmt.where(Place.origin_city == _clean_city(origin_city))
    if band:
        stmt = stmt.where(Place.band == str(band))
    if category:
        stmt = stmt.where(Place.category == str(category))
    counts = {OSM_SOURCE: 0, SEED_SOURCE: 0}
    for (tags,) in session.execute(stmt):
        counts[place_source(tags)] += 1
    return counts


def get_segment(session: Session, *, origin_city: str, band: str) -> Optional[SegmentFetch]:
    """读取 (城市, band) 的抓取水位;返回 None 表示该段还没入库。"""
    city = _clean_city(origin_city)
    return session.scalar(
        select(SegmentFetch).where(SegmentFetch.origin_city == city, SegmentFetch.band == str(band))
    )


def latest_city_origin(session: Session, *, origin_city: str) -> Optional[SegmentFetch]:
    """该城市**任意已入库分段**的起点(取最近一次),用于换分段时复用同一原点。

    Nominatim 官方政策是 ≤1 次/秒,能复用就不重查;同时保证同一城市各分段的
    范围圈是**同心**的(前端地图不会跳)。
    """
    city = _clean_city(origin_city)
    return session.scalar(
        select(SegmentFetch)
        .where(SegmentFetch.origin_city == city)
        .order_by(SegmentFetch.fetched_at.desc(), SegmentFetch.id.desc())
        .limit(1)
    )


def record_segment(
    session: Session,
    *,
    origin_city: str,
    band: str,
    origin: Mapping[str, Any],
    place_count: int,
    source: str = "overpass",
) -> SegmentFetch:
    """记录/刷新 (城市, band) 的抓取水位(含起点坐标,读库时按同一原点算距离)。"""
    city = _clean_city(origin_city)
    record = get_segment(session, origin_city=city, band=band)
    if record is None:
        record = SegmentFetch(origin_city=city, band=str(band))
        session.add(record)
    record.origin_name = str(origin.get("name") or city)
    record.origin_lat = round(float(origin["lat"]), COORD_PRECISION)
    record.origin_lng = round(float(origin["lng"]), COORD_PRECISION)
    record.place_count = int(place_count)
    record.source = str(source or "overpass")
    record.fetched_at = utcnow()
    session.flush()
    return record


def segment_to_dict(record: SegmentFetch) -> dict[str, Any]:
    """抓取水位 → dict(API 的 ``fetched_at`` / ``source`` 字段来源)。"""
    return {
        "origin_city": record.origin_city,
        "band": record.band,
        "origin_name": record.origin_name,
        "origin_lat": record.origin_lat,
        "origin_lng": record.origin_lng,
        "place_count": record.place_count,
        "source": record.source,
        "fetched_at": iso_utc(record.fetched_at),
    }


def segment_overview(
    session: Session,
    *,
    origin_city: Optional[str] = None,
    band: Optional[str] = None,
) -> list[dict[str, Any]]:
    """已入库分段的概览(按城市与 band 排序),可按城市/band 过滤。"""
    stmt = select(SegmentFetch).order_by(SegmentFetch.origin_city, SegmentFetch.band)
    if origin_city:
        stmt = stmt.where(SegmentFetch.origin_city == _clean_city(origin_city))
    if band:
        stmt = stmt.where(SegmentFetch.band == str(band))
    return [segment_to_dict(record) for record in session.scalars(stmt)]
