"""(城市, band) 目的地抓取入库编排:**命中库只读库,未命中才触网**。

流程(docs/STAGE1-PLAN.md 第 4 节"目的地库 cold-start"):

1. 查 ``SegmentFetch`` 水位 —— 有记录说明该 (城市, band) 已入库 →
   直接 :func:`db.repository.list_places` 读库返回,**不发任何网络请求**;
2. 没记录 → 解析起点(Nominatim)→ 按分段**上限半径**查 Overpass(多 tag 并集,
   一次查完)→ haversine 收敛到环内 → 简化归类 → upsert 入库 → 记水位;
3. ``refresh=True`` 可强制重抓(仍按唯一键 upsert,不会产生重复行)。

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
from services.bands import band_keys, band_radius_m, filter_to_band, require_band
from services.categories import categorize

# 阶段1a 沿用 POC 的检索线索;TASK-1b 会扩成四分类线索并集(滑雪/运动/人文/自然)。
SEARCH_TAGS: list[dict[str, str]] = [
    {"natural": "peak"},
    {"natural": "waterfall"},
    {"tourism": "attraction"},
    {"tourism": "viewpoint"},
]
ELEMENT_TYPES = "nwr"
FETCH_LIMIT = 400
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
    tags: Optional[Iterable[Mapping[str, str]]] = None,
    limit: int = FETCH_LIMIT,
    element_types: str = ELEMENT_TYPES,
) -> list[dict[str, Any]]:
    """真实抓取:按分段上限半径查 Overpass(多 tag 并集 + ``with_id`` 便于防重)。"""
    overpass_client = client or overpass.default_client()
    return overpass_client.nearby_places(
        lat,
        lng,
        band_radius_m(band),
        list(tags) if tags is not None else SEARCH_TAGS,
        limit=limit,
        require_name=True,
        element_types=element_types,
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
    """环内候选 → 入库条目(补 OSM 身份与简化分类;``intro`` 留给 TASK-1b)。"""
    items: list[dict[str, Any]] = []
    for candidate in candidates or []:
        osm_type, osm_id = place_identity(candidate)
        items.append(
            {
                "osm_type": osm_type,
                "osm_id": osm_id,
                "name": candidate.get("name") or "",
                "lat": candidate["lat"],
                "lng": candidate["lng"],
                "category": categorize(candidate.get("tags")),
                "tags": dict(candidate.get("tags") or {}),
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
) -> SegmentOutcome:
    """读取某 (城市, band) 的目的地;未入库才抓取并落库。

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
    return _read_from_db(
        session,
        origin=origin,
        band=band_def,
        category=category,
        source=SOURCE_OVERPASS,
        network_used=True,
        segment=repo.segment_to_dict(record),
        written=written,
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
    )


def main(argv: Optional[list[str]] = None) -> int:
    """CLI:预抓 (城市, band) 入库。``python -m services.place_loader 上海 50_100``"""
    parser = argparse.ArgumentParser(
        description="抓取并入库某城市某距离分段的目的地(已入库则直接读库,不触网)"
    )
    parser.add_argument("city", nargs="?", default="上海", help="起点城市名(默认:上海)")
    parser.add_argument("band", nargs="?", default="50_100", choices=band_keys(), help="距离分段 key")
    parser.add_argument("--refresh", action="store_true", help="强制重抓(仍按唯一键 upsert)")
    parser.add_argument("--db", default=None, help="数据库 URL(默认 WHERE2GO_DB_URL 或 backend/data/where2go.db)")
    parser.add_argument("--show", type=int, default=10, help="打印前 N 条(默认 10)")
    args = parser.parse_args(argv)

    engine = make_engine(args.db)
    init_db(engine)
    with open_session(engine) as session:
        try:
            outcome = load_segment(session, city=args.city, band=args.band, refresh=args.refresh)
        except (DataSourceError, ValueError) as exc:
            print(f"[失败] {exc}")
            return 1

    print(
        f"[完成] {outcome.origin['name']} · {outcome.band['label']} · "
        f"{len(outcome.places)} 条 · 来源={'数据库' if outcome.source == SOURCE_DB else 'Overpass 实时抓取'} · "
        f"本次写入 {outcome.written} 条"
    )
    for row in outcome.places[: max(0, args.show)]:
        print(f"  - {row['name']}({row['category']}) 距起点 {row['distance_km']} km")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
