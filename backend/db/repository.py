"""``Place`` / ``SegmentFetch`` / ``Collection`` / ``CollectionCat`` 的读写封装。

只碰 DB,不碰网络(抓取编排在 :mod:`services.place_loader`):

* :func:`upsert_places` —— 按唯一键 ``(osm_type, osm_id, origin_city)`` 防重写入,
  同批内重复也只落一行;已有 ``intro`` 不被覆盖(TASK-1b 的 LLM 简介是缓存,重抓不重算);
* :func:`list_places` —— 按 (城市, band, 分类) 查询,并按大圆距离升序返回
  (距离复用 :func:`data_sources.haversine_km`,与抓取时的环形过滤同一口径);
* :func:`select_places` / :func:`count_places` —— 返回 ORM 行的查询,给
  TASK-1b 的重归类与 LLM 简介回填用(``missing_intro=True`` 只取还没简介的 POI,
  **DB 就是简介缓存**:已有 ``intro`` 的行不会被再送去调 LLM);
* :func:`get_segment` / :func:`record_segment` —— (城市, band) 抓取水位的读与记;
* :func:`upsert_collection` —— 收藏写入(阶段2c):唯一键 ``(kind, ref_key, mode)``,
  重复收藏**幂等**(刷新快照摘要、返回原行,不报错也不产生第二行);
* :func:`list_collections` / :func:`count_by_kind` / :func:`delete_collection` ——
  收藏列表(可按 kind / mode / 分组过滤,新的在前)、分类计数与删除;
* :func:`upsert_collection_cat` / :func:`delete_collection_cat` —— 收藏分组(M4 铺路):
  分组名唯一,删分组只把旗下收藏**摘下来**(``cat_id`` 置空),不连带删收藏。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from data_sources import haversine_km

from .models import (
    CAT_MANUAL,
    CAT_NAME_LEN,
    COLLECTION_CAT_SOURCES,
    COORD_PRECISION,
    KIND_PLACE,
    KIND_ROUTE,
    LAT_LIMIT,
    LNG_LIMIT,
    NAME_LEN,
    NO_MODE,
    OSM_SOURCE,
    POINT_NAME_LEN,
    REF_KEY_LEN,
    SEED_SOURCE,
    SUMMARY_MODE_KEY,
    SUMMARY_NUMBER_KEYS,
    UNCATEGORIZED,
    Collection,
    CollectionCat,
    Place,
    SegmentFetch,
    clean_text,
    collection_kind,
    collection_ref_key,
    default_collection_name,
    iso_utc,
    optional_coordinate,
    osm_key,
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


# --------------------------------------------------------------------------- #
# 路线收藏(阶段2c,TASK-2c):Collection / CollectionCat 的读写
# --------------------------------------------------------------------------- #

# 收藏是"快照摘要",不是整条路线:上千点的 OSRM 折线主动丢掉,要看线请重新调 /api/routes
SUMMARY_DROP_KEYS: tuple[str, ...] = ("geometry",)
SUMMARY_PRECISION = 2


def _optional_int(name: str, value: Any) -> Optional[int]:
    """可空整数归一(``None``/空串 → ``None``);非整数抛 :class:`ValueError`(中文说明)。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数,收到:{value!r}")
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数,收到:{value!r}") from exc


def _collection_id(collection_id: Any) -> int:
    """收藏主键归一:空 / 非整数 / 非正数一律抛 :class:`ValueError`(API 层转 400)。"""
    resolved = _optional_int("收藏 id", collection_id)
    if resolved is None:
        raise ValueError("缺少必要参数:收藏 id")
    if resolved <= 0:
        raise ValueError(f"收藏 id 必须是正整数,收到:{collection_id!r}")
    return resolved


def _resolve_cat_id(session: Session, cat_id: Any) -> Optional[int]:
    """分组外键归一:``None`` = 不挂分组;分组不存在抛 :class:`ValueError`(API 层转 400)。"""
    resolved = _optional_int("cat_id", cat_id)
    if resolved is None:
        return None
    if session.scalar(select(CollectionCat.id).where(CollectionCat.id == resolved)) is None:
        raise ValueError(f"未知收藏分组:cat_id={resolved}")
    return resolved


def _resolve_osm(osm_type: Any, osm_id: Any) -> tuple[Optional[str], Optional[int]]:
    """OSM 身份成对校验(复用 :func:`db.models.osm_key`),再拆回列上的两个字段。"""
    key = osm_key(osm_type, osm_id)
    if not key:
        return None, None
    resolved_type, raw_id = key.split("/", 1)
    return resolved_type, int(raw_id)


def _number_or_none(value: Any) -> Any:
    """快照里的数字字段归一:没给 / 空串 / 非数字 / NaN / inf → ``None``(**不瞎造数字**)。

    OSRM 降级时 ``duration_min``/``cost_cny`` 本来就是 ``null``(见 :mod:`services.routes`),
    收藏照实存 ``None``;整数值保持 ``int``,JSON 来回一趟仍是 ``50`` 而不是 ``50.0``。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number.is_integer():
        return int(number)
    return round(number, SUMMARY_PRECISION)


def collection_summary(
    summary: Optional[Mapping[str, Any]] = None,
    *,
    mode: Optional[str] = None,
) -> dict[str, Any]:
    """快照摘要归一:``mode`` 与三个数字键(``duration_min``/``cost_cny``/``distance_km``)**恒在**。

    收藏存的是"当时看到的数字":之后价格系数改了、OSRM 降级了,列表仍显示收藏那一刻的口径
    (M4 对比总账要的正是这个)。缺的键写成 ``None``(前端显示"—"),其余键
    (``kind=real|estimate``、``degraded``、``links`` 等)原样保留;只有 ``geometry`` 主动丢掉。
    显式给了 ``mode`` 就以它为准(``place`` 收藏恒为空串),没给则沿用 ``summary`` 里的。
    """
    raw = dict(summary or {})
    if mode is None:
        resolved_mode = clean_text(raw.get(SUMMARY_MODE_KEY)) or NO_MODE
    else:
        resolved_mode = clean_text(mode) or NO_MODE
    normalized: dict[str, Any] = {SUMMARY_MODE_KEY: resolved_mode}
    for key in SUMMARY_NUMBER_KEYS:
        normalized[key] = _number_or_none(raw.get(key))
    for key, value in raw.items():
        if key not in normalized and key not in SUMMARY_DROP_KEYS:
            normalized[key] = value
    return normalized


def collection_to_dict(row: Collection, *, cat_name: Optional[str] = None) -> dict[str, Any]:
    """收藏 ORM 行 → API/前端用的 dict(起终点平铺,含分组名与 ISO8601 时间戳)。"""
    return {
        "id": row.id,
        "kind": row.kind,
        "mode": row.mode,
        "ref_key": row.ref_key,
        "name": row.name,
        "osm_type": row.osm_type,
        "osm_id": row.osm_id,
        "from_lat": row.from_lat,
        "from_lng": row.from_lng,
        "from_name": row.from_name or None,
        "to_lat": row.to_lat,
        "to_lng": row.to_lng,
        "to_name": row.to_name or None,
        "summary": dict(row.summary or {}),
        "cat_id": row.cat_id,
        "cat_name": cat_name,
        "created_at": iso_utc(row.created_at),
        "updated_at": iso_utc(row.updated_at),
    }


def upsert_collection(
    session: Session,
    *,
    kind: Any,
    mode: Any = None,
    name: Optional[str] = None,
    ref_key: Optional[str] = None,
    osm_type: Any = None,
    osm_id: Any = None,
    from_lat: Any = None,
    from_lng: Any = None,
    from_name: Optional[str] = None,
    to_lat: Any = None,
    to_lng: Any = None,
    to_name: Optional[str] = None,
    summary: Optional[Mapping[str, Any]] = None,
    cat_id: Any = None,
) -> tuple[Collection, bool]:
    """写一条收藏,按唯一键 ``(kind, ref_key, mode)`` **幂等**;返回 ``(行, 是否新建)``。

    重复收藏既不报错也不产生第二行:刷新名称 / 坐标 / 快照摘要 / 分组并顶 ``updated_at``,
    而 ``id`` 与 ``created_at`` 保持原样 —— 收藏时间是"第一次收藏"的时间,M4 排序才稳定。
    ``kind=place`` 的 ``mode`` 恒为 :data:`~db.models.NO_MODE`(空串),否则同一个目的地
    会按方式裂成多行、幂等失效;路线的 ``mode`` 取值由 API 层按 ``services.routes.MODES`` 校验。
    引用串缺省时按 :func:`db.models.collection_ref_key` 算(OSM 身份优先,退化到坐标定点串);
    ``ref_key`` 只留给脚本/M4 显式指定,常规调用不要传。
    参数非法(类型未知、坐标越界、引用不完整、分组不存在)抛 :class:`ValueError`(API 层转 400)。
    """
    resolved_kind = collection_kind(kind)
    resolved_mode = NO_MODE if resolved_kind == KIND_PLACE else (clean_text(mode) or NO_MODE)
    resolved_from_lat = optional_coordinate("from_lat", from_lat, limit=LAT_LIMIT)
    resolved_from_lng = optional_coordinate("from_lng", from_lng, limit=LNG_LIMIT)
    resolved_to_lat = optional_coordinate("to_lat", to_lat, limit=LAT_LIMIT)
    resolved_to_lng = optional_coordinate("to_lng", to_lng, limit=LNG_LIMIT)
    resolved_osm_type, resolved_osm_id = _resolve_osm(osm_type, osm_id)
    resolved_ref_key = clean_text(ref_key, limit=REF_KEY_LEN) or collection_ref_key(
        kind=resolved_kind,
        osm_type=resolved_osm_type,
        osm_id=resolved_osm_id,
        to_lat=resolved_to_lat,
        to_lng=resolved_to_lng,
        from_lat=resolved_from_lat,
        from_lng=resolved_from_lng,
    )
    resolved_from_name = clean_text(from_name, limit=POINT_NAME_LEN) or ""
    resolved_to_name = clean_text(to_name, limit=POINT_NAME_LEN) or ""
    resolved_name = clean_text(name, limit=NAME_LEN) or default_collection_name(
        kind=resolved_kind,
        from_name=resolved_from_name,
        to_name=resolved_to_name,
        from_lat=resolved_from_lat,
        from_lng=resolved_from_lng,
        to_lat=resolved_to_lat,
        to_lng=resolved_to_lng,
    )
    resolved_cat_id = _resolve_cat_id(session, cat_id)
    resolved_summary = collection_summary(summary, mode=resolved_mode)

    row = session.scalar(
        select(Collection).where(
            Collection.kind == resolved_kind,
            Collection.ref_key == resolved_ref_key,
            Collection.mode == resolved_mode,
        )
    )
    created = row is None
    if created:
        row = Collection(
            kind=resolved_kind,
            ref_key=resolved_ref_key,
            mode=resolved_mode,
            created_at=utcnow(),
        )
        session.add(row)
    row.name = resolved_name
    row.osm_type = resolved_osm_type
    row.osm_id = resolved_osm_id
    row.from_lat = resolved_from_lat
    row.from_lng = resolved_from_lng
    row.from_name = resolved_from_name
    row.to_lat = resolved_to_lat
    row.to_lng = resolved_to_lng
    row.to_name = resolved_to_name
    row.summary = resolved_summary
    row.cat_id = resolved_cat_id
    row.updated_at = utcnow()
    session.flush()
    return row, created


def get_collection(session: Session, *, collection_id: Any) -> Optional[Collection]:
    """按主键取收藏 **ORM 行**;不存在返回 ``None``(API 层转 404)。id 非法抛 ValueError。"""
    resolved = _collection_id(collection_id)
    return session.scalar(select(Collection).where(Collection.id == resolved))


def _cat_names(session: Session, ids: Iterable[Optional[int]]) -> dict[int, str]:
    """一次查回一批分组名(避免列表渲染时 N+1 查询)。"""
    wanted = {int(item) for item in ids if item is not None}
    if not wanted:
        return {}
    rows = session.scalars(select(CollectionCat).where(CollectionCat.id.in_(sorted(wanted))))
    return {row.id: row.name for row in rows}


def list_collections(
    session: Session,
    *,
    kind: Any = None,
    mode: Any = None,
    cat_id: Any = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """收藏列表(**新的在前**),可按 ``kind`` / ``mode`` / 分组过滤;返回 dict 数组。

    ``kind`` 非法抛 :class:`ValueError`(API 层转 400);分组名随每条一起返回(``cat_name``)。
    """
    stmt = select(Collection)
    wanted_kind = clean_text(kind)
    if wanted_kind:
        stmt = stmt.where(Collection.kind == collection_kind(wanted_kind))
    wanted_mode = clean_text(mode)
    if wanted_mode:
        stmt = stmt.where(Collection.mode == wanted_mode)
    resolved_cat_id = _resolve_cat_id(session, cat_id)
    if resolved_cat_id is not None:
        stmt = stmt.where(Collection.cat_id == resolved_cat_id)
    stmt = stmt.order_by(Collection.created_at.desc(), Collection.id.desc())
    if limit:
        stmt = stmt.limit(max(1, int(limit)))
    rows = list(session.scalars(stmt))
    names = _cat_names(session, (row.cat_id for row in rows))
    return [collection_to_dict(row, cat_name=names.get(row.cat_id)) for row in rows]


def delete_collection(session: Session, *, collection_id: Any) -> bool:
    """删除一条收藏;返回是否真删掉了(``False`` = 本来就不存在,重复删除同样不报错)。"""
    row = get_collection(session, collection_id=collection_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def count_collections(
    session: Session, *, kind: Any = None, cat_id: Any = None
) -> int:
    """收藏计数(可按类型 / 分组过滤),给前端状态栏与删除后的余量提示用。"""
    stmt = select(func.count(Collection.id))
    wanted_kind = clean_text(kind)
    if wanted_kind:
        stmt = stmt.where(Collection.kind == collection_kind(wanted_kind))
    resolved_cat_id = _resolve_cat_id(session, cat_id)
    if resolved_cat_id is not None:
        stmt = stmt.where(Collection.cat_id == resolved_cat_id)
    return int(session.scalar(stmt) or 0)


def count_by_kind(session: Session) -> dict[str, int]:
    """收藏按类型(``route`` / ``place``)计数;两个键恒在,前端不必判空。"""
    counts = {KIND_ROUTE: 0, KIND_PLACE: 0}
    stmt = select(Collection.kind, func.count(Collection.id)).group_by(Collection.kind)
    for kind, total in session.execute(stmt):
        counts[str(kind)] = int(total)
    return counts


def collection_cat_to_dict(
    cat: CollectionCat, *, collection_count: Optional[int] = None
) -> dict[str, Any]:
    """分组 ORM 行 → dict;给了 ``collection_count`` 就带上旗下收藏数。"""
    return {
        "id": cat.id,
        "name": cat.name,
        "note": cat.note,
        "source": cat.source,
        "sort_order": cat.sort_order,
        "collection_count": collection_count,
        "created_at": iso_utc(cat.created_at),
        "updated_at": iso_utc(cat.updated_at),
    }


def get_collection_cat(
    session: Session, *, cat_id: Any = None, name: Any = None
) -> Optional[CollectionCat]:
    """按 id 或名字取分组(名字唯一);``cat_id`` 优先,两者都没给返回 ``None``。"""
    resolved_id = _optional_int("cat_id", cat_id)
    if resolved_id is not None:
        return session.scalar(select(CollectionCat).where(CollectionCat.id == resolved_id))
    wanted = clean_text(name, limit=CAT_NAME_LEN)
    if wanted:
        return session.scalar(select(CollectionCat).where(CollectionCat.name == wanted))
    return None


def upsert_collection_cat(
    session: Session,
    *,
    name: Any,
    note: Any = None,
    source: Any = CAT_MANUAL,
    sort_order: Any = 0,
) -> tuple[CollectionCat, bool]:
    """建 / 更新收藏分组,按唯一名字**幂等**;返回 ``(行, 是否新建)``。

    ``note`` 只在给了非空值时覆盖(与 :func:`upsert_places` 保护 ``intro`` 同一口径),
    免得前端只传名字就把已有备注抹掉。``source`` 区分人工建的与脚本自动建的
    (:data:`~db.models.CAT_MANUAL` / :data:`~db.models.CAT_AUTO`),自动分组可被安全清理。
    """
    resolved_name = clean_text(name, limit=CAT_NAME_LEN)
    if not resolved_name:
        raise ValueError("分组名不能为空")
    resolved_source = clean_text(source) or CAT_MANUAL
    if resolved_source not in COLLECTION_CAT_SOURCES:
        raise ValueError(
            f"未知分组来源:{source!r}(可选:{'、'.join(COLLECTION_CAT_SOURCES)})"
        )
    cat = session.scalar(select(CollectionCat).where(CollectionCat.name == resolved_name))
    created = cat is None
    if created:
        cat = CollectionCat(name=resolved_name, created_at=utcnow())
        session.add(cat)
    resolved_note = clean_text(note)
    if resolved_note:
        cat.note = resolved_note
    cat.source = resolved_source
    cat.sort_order = _optional_int("sort_order", sort_order) or 0
    cat.updated_at = utcnow()
    session.flush()
    return cat, created


def list_collection_cats(session: Session) -> list[dict[str, Any]]:
    """全部分组(按 ``sort_order`` 再按名字),每条带旗下收藏数(一次查完,不 N+1)。"""
    counts = dict(
        session.execute(
            select(Collection.cat_id, func.count(Collection.id))
            .where(Collection.cat_id.is_not(None))
            .group_by(Collection.cat_id)
        ).all()
    )
    cats = session.scalars(
        select(CollectionCat).order_by(
            CollectionCat.sort_order, CollectionCat.name, CollectionCat.id
        )
    )
    return [
        collection_cat_to_dict(cat, collection_count=int(counts.get(cat.id, 0))) for cat in cats
    ]


def delete_collection_cat(session: Session, *, cat_id: Any) -> bool:
    """删除分组;旗下收藏**摘下来**(``cat_id`` 置空)而不是连带删除。返回是否真删掉了。

    这里显式置空而不是只靠外键的 ``ON DELETE SET NULL``:SQLite 默认不开
    ``PRAGMA foreign_keys``,靠它兜底会静默失效,整理标签时误删路线就找不回来了。
    """
    resolved = _optional_int("cat_id", cat_id)
    cat = get_collection_cat(session, cat_id=resolved) if resolved is not None else None
    if cat is None:
        return False
    for row in session.scalars(select(Collection).where(Collection.cat_id == cat.id)):
        row.cat_id = None
    session.delete(cat)
    session.flush()
    return True
