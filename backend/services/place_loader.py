"""(城市, band) 目的地抓取入库编排:**命中库只读库,未命中才触网**。

流程(docs/STAGE1-PLAN.md 第 3/4 节"四分类检索 + 目的地库 cold-start"):

1. 查 ``SegmentFetch`` 水位 —— 有记录说明该 (城市, band) 已入库 →
   直接 :func:`db.repository.list_places` 读库返回,**不发任何网络请求**
   (读库路径也不会调 LLM,保证"二次查询秒出");
2. 没记录 → 解析起点(Nominatim)→ 按分段**上限半径**一次查**四分类 tag 并集**
   (:data:`SEARCH_GROUPS`,每组独立配额,见 :mod:`services.classify`)→
   haversine 收敛到环内 → 按 OSM ``(type, id)`` **去重** + 优先级**归类**
   (滑雪 > 运动 > 人文美食 > 自然,一地只入一类)→ upsert 入库 → 记水位;
3. 入库**之后**再补 LLM 一句话简介(:func:`services.intro.fill_missing_intros`):
   DB 即缓存,已有 ``intro`` 的 POI 不再调用;失败降级成空简介,**不阻塞入库**;
4. ``refresh=True`` 可强制重抓(仍按唯一键 upsert,不会产生重复行,也不覆盖已有简介)。

分类过滤只作用在**读取**阶段:一次抓取入库的数据覆盖全部分类,所以换分类查询
同样命中库、不触网。网络调用全部可注入(``fetcher`` / ``geocoder``),单测用替身即可。
"""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import Session

from data_sources import DataSourceError
from data_sources import geocode as ds_geocode
from data_sources import overpass
from db import init_db, make_engine, open_session
from db import repository as repo
from db.models import FALLBACK_OSM_TYPE, OSM_ELEMENT_TYPES, SegmentFetch
from services import classify
from services import intro as intro_service
from services.bands import band_keys, band_radius_m, filter_to_band, require_band
from services.classify import classify_places

# 四分类检索线索(docs/STAGE1-PLAN.md 第 3 节):按 band 上限半径**一次**查完并集。
# 分组各带配额,避免 ``sport=*``/餐厅这类高频 tag 把总量刷爆、山峰古镇一条不剩;
# 同一实体被多组命中时由 classify_places 按 OSM (type, id) 去重后只归一类。
SEARCH_GROUPS: list[dict[str, Any]] = classify.search_groups()
SEARCH_TAGS: list[classify.TagSelector] = classify.search_tags()
ELEMENT_TYPES = classify.DEFAULT_ELEMENT_TYPES
FETCH_LIMIT = overpass.MAX_FETCH
GROUP_QUERY_TIMEOUT = overpass.DEFAULT_GROUP_QUERY_TIMEOUT
GROUP_REQUEST_TIMEOUT = overpass.DEFAULT_GROUP_REQUEST_TIMEOUT_S
# 交互式抓取时一次最多补多少条简介(全量回填走 ``python -m services.intro``)
INTRO_BATCH_LIMIT = 40
SOURCE_OVERPASS = "overpass"
SOURCE_DB = "db"
FINGERPRINT_HEX_LEN = 12

FetchFn = Callable[[float, float, Mapping[str, Any]], list[dict[str, Any]]]
GeocodeFn = Callable[[str], Mapping[str, Any]]


@dataclass
class SegmentOutcome:
    """一次 (城市, band) 查询的结果:数据 + 来源(读库还是刚抓的)。"""

    origin: dict[str, Any]
    band: dict[str, Any]
    places: list[dict[str, Any]]
    source: str = SOURCE_DB
    network_used: bool = False
    fetched_at: Optional[str] = None
    written: int = 0
    counts_by_category: dict[str, int] = field(default_factory=dict)
    segment: Optional[dict[str, Any]] = None
    intro_stats: Optional[dict[str, Any]] = None


def default_geocoder(city: str) -> dict[str, Any]:
    """复用 POC 的 Nominatim 正向地理编码:城市名 → ``{city, name, lat, lng}``。"""
    geo = ds_geocode(city)
    return {"city": city, "name": geo["display_name"], "lat": geo["lat"], "lng": geo["lng"]}


def resolve_origin(
    city: str,
    *,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    geocoder: Optional[GeocodeFn] = None,
) -> dict[str, Any]:
    """解析起点:调用方给了坐标就直接用,否则查 Nominatim(会触网)。"""
    cleaned = (city or "").strip()
    if not cleaned:
        raise ValueError("起点城市不能为空")
    if (lat is None) != (lng is None):
        raise ValueError("lat/lng 必须成对给出")
    if lat is not None and lng is not None:
        return {"city": cleaned, "name": cleaned, "lat": float(lat), "lng": float(lng)}
    geo = dict((geocoder or default_geocoder)(cleaned))
    # 统一成 {city, name, lat, lng} 四个字段,API/前端只看这一种形状。
    return {
        "city": str(geo.get("city") or cleaned),
        "name": str(geo.get("name") or geo.get("display_name") or cleaned),
        "lat": float(geo["lat"]),
        "lng": float(geo["lng"]),
    }


def default_fetcher(
    lat: float,
    lng: float,
    band: Mapping[str, Any],
    *,
    client: Optional[overpass.OverpassClient] = None,
    groups: Optional[Iterable[Mapping[str, Any]]] = None,
    query_timeout: float = GROUP_QUERY_TIMEOUT,
    request_timeout: float = GROUP_REQUEST_TIMEOUT,
) -> list[dict[str, Any]]:
    """真实抓取:按分段**上限半径**一次查四分类 tag 并集(分组配额 + ``with_id`` 防重)。

    复用现有 overpass 环形分段机制:服务端只按上限半径 ``around`` 取数,环内收敛
    仍由 :func:`services.bands.filter_to_band` 用 haversine 在本地做。
    """
    overpass_client = client or overpass.default_client()
    return overpass_client.nearby_places_grouped(
        lat,
        lng,
        band_radius_m(band),
        list(groups) if groups is not None else SEARCH_GROUPS,
        require_name=True,
        query_timeout=query_timeout,
        request_timeout=request_timeout,
        with_id=True,
    )


def place_identity(place: Mapping[str, Any]) -> tuple[str, int]:
    """取 OSM 身份 ``(osm_type, osm_id)``;缺失时用"名字+坐标"指纹兜底(负数 id)。

    兜底分支给 TASK-1c 的种子数据用:没有 OSM id 的条目同样能防重、能 upsert,
    且负数 id 不会与真实 OSM id 撞车。
    """
    osm_type = str(place.get("osm_type") or "").strip().lower()
    osm_id = place.get("osm_id")
    if osm_type in OSM_ELEMENT_TYPES and isinstance(osm_id, int) and not isinstance(osm_id, bool):
        return osm_type, osm_id
    fingerprint = f"{place.get('name') or ''}|{place.get('lat')}|{place.get('lng')}"
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()  # 仅做指纹,非安全用途
    return FALLBACK_OSM_TYPE, -int(digest[:FINGERPRINT_HEX_LEN], 16)


def to_place_items(candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """环内候选 → 入库条目:先按 OSM ``(type, id)`` **去重**,再按优先级**归类**。

    去重与归类都由 :func:`services.classify.classify_places` 完成(一地只入一类,
    合并后的 tags 让优先级判定看到全部线索);这里只补入库身份与字段形状。
    ``intro`` 不在此生成 —— 入库后由 :mod:`services.intro` 按 POI 缓存补。
    """
    items: list[dict[str, Any]] = []
    for row in classify_places(candidates):
        osm_type, osm_id = place_identity(row)
        items.append(
            {
                "osm_type": osm_type,
                "osm_id": osm_id,
                "name": row.get("name") or "",
                "lat": row["lat"],
                "lng": row["lng"],
                "category": row["category"],
                "tags": dict(row.get("tags") or {}),
            }
        )
    return items


def load_segment(
    session: Session,
    *,
    city: str,
    band: str,
    category: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    fetcher: Optional[FetchFn] = None,
    geocoder: Optional[GeocodeFn] = None,
    refresh: bool = False,
    intros: bool = True,
    intro_limit: Optional[int] = INTRO_BATCH_LIMIT,
    intro_workers: int = intro_service.DEFAULT_WORKERS,
) -> SegmentOutcome:
    """读取某 (城市, band) 的目的地;未入库才抓取并落库。

    ``intros=True`` 时,**抓取入库之后**再给缺简介的 POI 补 LLM 一句话简介
    (只作用于本次抓取路径:命中库直接读库时不调 LLM,保持零网络秒回)。
    简介失败一律降级为空,不影响已入库的数据。

    失败语义:分段/城市非法抛 :class:`ValueError`;数据源不可用抛
    :class:`data_sources.DataSourceError`(由 API 层翻成中文 HTTP 错误)。
    """
    band_def = require_band(band)
    city_clean = (city or "").strip()
    if not city_clean:
        raise ValueError("起点城市不能为空")
    fetch_fn = fetcher or default_fetcher

    recorded = repo.get_segment(session, origin_city=city_clean, band=band_def["key"])
    if recorded is not None and not refresh:
        return _read_from_db(
            session,
            origin=stored_origin(recorded),
            band=band_def,
            category=category,
            source=SOURCE_DB,
            network_used=False,
            segment=repo.segment_to_dict(recorded),
        )

    origin = _reuse_origin(session, city_clean, recorded=recorded, lat=lat, lng=lng, geocoder=geocoder)
    candidates = filter_to_band(
        fetch_fn(origin["lat"], origin["lng"], band_def) or [],
        origin["lat"],
        origin["lng"],
        band_def,
    )
    written = repo.upsert_places(
        session,
        origin_city=city_clean,
        band=band_def["key"],
        items=to_place_items(candidates),
    )
    record = repo.record_segment(
        session,
        origin_city=city_clean,
        band=band_def["key"],
        origin=origin,
        place_count=len(candidates),
        source=SOURCE_OVERPASS,
    )
    session.commit()
    # 先落库再补简介:LLM 挂了/没 key 也只是简介为空,入库结果不受影响。
    intro_stats = None
    if intros:
        intro_stats = intro_service.fill_missing_intros(
            session,
            origin_city=city_clean,
            band=band_def["key"],
            limit=intro_limit,
            workers=intro_workers,
        )
    return _read_from_db(
        session,
        origin=origin,
        band=band_def,
        category=category,
        source=SOURCE_OVERPASS,
        network_used=True,
        segment=repo.segment_to_dict(record),
        written=written,
        intro_stats=intro_stats,
    )


def _reuse_origin(
    session: Session,
    city: str,
    *,
    recorded: Optional[SegmentFetch],
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    geocoder: Optional[GeocodeFn] = None,
) -> dict[str, Any]:
    """决定这次抓取用哪个起点:调用方坐标 > 库内已有起点 > Nominatim(触网)。"""
    if lat is not None or lng is not None:
        return resolve_origin(city, lat=lat, lng=lng, geocoder=geocoder)
    known = recorded or repo.latest_city_origin(session, origin_city=city)
    if known is not None:
        return stored_origin(known)
    return resolve_origin(city, geocoder=geocoder)


def stored_origin(record: SegmentFetch) -> dict[str, Any]:
    """抓取水位里的起点 → 统一的 origin dict(读库与重抓共用同一原点)。"""
    return {
        "city": record.origin_city,
        "name": record.origin_name,
        "lat": record.origin_lat,
        "lng": record.origin_lng,
    }


def _read_from_db(
    session: Session,
    *,
    origin: dict[str, Any],
    band: Mapping[str, Any],
    category: Optional[str],
    source: str,
    network_used: bool,
    segment: Optional[dict[str, Any]],
    written: int = 0,
    intro_stats: Optional[dict[str, Any]] = None,
) -> SegmentOutcome:
    """统一从库里取数,保证"读库"与"刚抓完"两条路径返回同一种形状。"""
    places = repo.list_places(
        session,
        origin_city=origin["city"],
        band=band["key"],
        category=category,
        origin_lat=origin["lat"],
        origin_lng=origin["lng"],
    )
    counts = repo.count_by_category(session, origin_city=origin["city"], band=band["key"])
    return SegmentOutcome(
        origin=dict(origin),
        band=dict(band),
        places=places,
        source=source,
        network_used=network_used,
        fetched_at=(segment or {}).get("fetched_at"),
        written=written,
        counts_by_category=counts,
        segment=segment,
        intro_stats=intro_stats,
    )


def main(argv: Optional[list[str]] = None) -> int:
    """CLI:预抓 (城市, band) 入库。``python -m services.place_loader 上海 50_100``"""
    parser = argparse.ArgumentParser(
        description="抓取并入库某城市某距离分段的目的地(已入库则直接读库,不触网)"
    )
    parser.add_argument("city", nargs="?", default="上海", help="起点城市名(默认:上海)")
    parser.add_argument("band", nargs="?", default="50_100", choices=band_keys(), help="距离分段 key")
    parser.add_argument("--refresh", action="store_true", help="强制重抓(仍按唯一键 upsert)")
    parser.add_argument("--no-intros", action="store_true", help="抓取后不调 LLM 补简介")
    parser.add_argument("--intro-limit", type=int, default=None,
                        help=f"本次最多补多少条简介(默认 {INTRO_BATCH_LIMIT};0 = 不限)")
    parser.add_argument("--db", default=None, help="数据库 URL(默认 WHERE2GO_DB_URL 或 backend/data/where2go.db)")
    parser.add_argument("--show", type=int, default=10, help="打印前 N 条(默认 10)")
    args = parser.parse_args(argv)

    intro_limit = INTRO_BATCH_LIMIT if args.intro_limit is None else (None if args.intro_limit <= 0 else args.intro_limit)
    engine = make_engine(args.db)
    init_db(engine)
    with open_session(engine) as session:
        try:
            outcome = load_segment(
                session,
                city=args.city,
                band=args.band,
                refresh=args.refresh,
                intros=not args.no_intros,
                intro_limit=intro_limit,
            )
        except (DataSourceError, ValueError) as exc:
            print(f"[失败] {exc}")
            return 1

    print(
        f"[完成] {outcome.origin['name']} · {outcome.band['label']} · "
        f"{len(outcome.places)} 条 · 来源={'数据库' if outcome.source == SOURCE_DB else 'Overpass 实时抓取'} · "
        f"本次写入 {outcome.written} 条"
    )
    print(f"[分类] {' · '.join(f'{name} {total}' for name, total in sorted(outcome.counts_by_category.items()))}")
    if outcome.intro_stats:
        stats = outcome.intro_stats
        print(f"[简介] 生成 {stats['filled']} 条 · 降级 {stats['failed']} 条 · 仍缺 {stats['pending']} 条 · {stats['provider']}")
    for row in outcome.places[: max(0, args.show)]:
        intro = f" — {row['intro']}" if row.get("intro") else ""
        print(f"  - {row['name']}({row['category']}) 距起点 {row['distance_km']} km{intro}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
