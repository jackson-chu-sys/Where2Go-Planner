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
4. ``refresh=True`` 可强制重抓(仍按唯一键 upsert,不会产生重复行,也不覆盖已有简介);
5. **种子数据垫底**(TASK-1c):OSM 国内滑雪/运动覆盖差,抓取路径与读库路径都会合并
   :mod:`services.seed_data` 的人工种子 —— 去重键 = 名字 + 坐标,OSM 已经抓到就不重复补;
   读库路径的补种是**幂等且纯本地**的,存量库不重抓也能拿到种子。整体开关是环境变量
   ``WHERE2GO_SEEDS``(默认开,单测在 ``backend/conftest.py`` 里默认关)。

起点除了城市名,还能来自浏览器定位:前端拿到 GPS 坐标后调 ``GET /api/geocode/reverse``,
由 :func:`resolve_reverse_origin` 用 Nominatim **逆**地理编码反查城市;反查失败不报错,
降级成"我的位置(纬度,经度)"这样的坐标起点,地图照样能用。

分类过滤只作用在**读取**阶段:一次抓取入库的数据覆盖全部分类,所以换分类查询
同样命中库、不触网。网络调用全部可注入(``fetcher`` / ``geocoder`` / ``reverse_geocoder``),
单测用替身即可。
"""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import Session

from data_sources import DataSourceError
from data_sources import geocode as ds_geocode
from data_sources import overpass
from data_sources import reverse as ds_reverse
from db import init_db, make_engine, open_session
from db import repository as repo
from db.models import FALLBACK_OSM_TYPE, OSM_ELEMENT_TYPES, UNCATEGORIZED, SegmentFetch
from services import classify
from services import intro as intro_service
from services import seed_data
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
SOURCE_SEED = "seed"
FINGERPRINT_HEX_LEN = 12
# 逆地理编码的 zoom:10 ≈ 区县级,反查出来的 display_name 里稳定带城市名
REVERSE_ZOOM = 10
# 反查不出城市名时的坐标起点命名(保留 2 位小数,免得 GPS 抖动造出一堆新"城市")
UNNAMED_COORD_PRECISION = 2
# display_name 里判定城市的后缀(Nominatim 中文结果形如 "浦东新区, 上海市, 中国")
CITY_SUFFIXES = ("市", "州", "地区", "盟")
COUNTRY_TOKENS = frozenset({"中国", "中华人民共和国", "china"})

FetchFn = Callable[[float, float, Mapping[str, Any]], list[dict[str, Any]]]
GeocodeFn = Callable[[str], Mapping[str, Any]]
ReverseGeocodeFn = Callable[[float, float], Mapping[str, Any]]


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
    seeded: int = 0
    counts_by_category: dict[str, int] = field(default_factory=dict)
    counts_by_source: dict[str, int] = field(default_factory=dict)
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


def default_reverse_geocoder(lat: float, lng: float, zoom: int = REVERSE_ZOOM) -> dict[str, Any]:
    """复用 POC 的 Nominatim **逆**地理编码:坐标 → ``{lat, lng, display_name}``。"""
    return ds_reverse(float(lat), float(lng), zoom=int(zoom))


def unnamed_origin(lat: float, lng: float) -> str:
    """反查不到城市名时的起点名:``我的位置(31.23,121.47)``。

    坐标只保留 2 位小数(约 1 km 精度):既够画范围圈,又不会让 GPS 抖动
    每次都造出一个新的 ``origin_city``,把库切得七零八落。
    """
    return f"我的位置({float(lat):.{UNNAMED_COORD_PRECISION}f},{float(lng):.{UNNAMED_COORD_PRECISION}f})"


def city_from_display_name(display_name: Any) -> str:
    """从 Nominatim 的 ``display_name`` 里挑出**城市级**地名。

    中文逆地理编码结果形如 ``"浦东新区, 上海市, 中国"`` 或
    ``"某某院, 东城区, 北京市, 100010, 中国"``,由细到粗用逗号分隔。规则:
    去掉国家名与纯数字邮编后,取第一个以 市/州/地区/盟 结尾的片段
    (``len > 后缀长``,避免只返回一个"市"字);都不像城市时退回最后一段(最粗的行政区)。
    """
    tokens = [part.strip() for part in str(display_name or "").split(",") if part.strip()]
    useful = [
        token for token in tokens
        if token.lower() not in COUNTRY_TOKENS and not token.isdigit()
    ]
    for token in useful:
        if any(token.endswith(suffix) and len(token) > len(suffix) for suffix in CITY_SUFFIXES):
            return token
    return useful[-1] if useful else ""


def resolve_reverse_origin(
    lat: float,
    lng: float,
    *,
    reverse_geocoder: Optional[ReverseGeocodeFn] = None,
    zoom: int = REVERSE_ZOOM,
) -> dict[str, Any]:
    """浏览器"我的位置" → 起点:**反查失败不报错**,降级成坐标起点。

    与 :func:`resolve_origin` 的区别:坐标是已知的(浏览器 GPS 给的),只需要反查
    城市名,所以 ``lat``/``lng`` 一律沿用**传入的 GPS 坐标**(范围圈要以用户真实
    位置为圆心,而不是 Nominatim 返回的行政区中心)。

    Nominatim 挂了、限流、或返回的 ``display_name`` 里挑不出城市时,返回
    ``resolved=False`` + :func:`unnamed_origin` 兜底名 —— API 层照常 200,
    前端地图照样能画环、能查库,只是起点名不好看。
    """
    latitude = float(lat)
    longitude = float(lng)
    fallback = {
        "city": unnamed_origin(latitude, longitude),
        "name": unnamed_origin(latitude, longitude),
        "lat": latitude,
        "lng": longitude,
        "resolved": False,
    }
    # 注入的替身保持 (lat, lng) 两参;真实实现才需要 zoom(区县级反查更稳)
    lookup = reverse_geocoder or (lambda point_lat, point_lng: default_reverse_geocoder(point_lat, point_lng, zoom=zoom))
    try:
        geo = dict(lookup(latitude, longitude) or {})
    except (DataSourceError, ValueError, TypeError):
        return fallback
    display_name = str(geo.get("display_name") or geo.get("name") or "").strip()
    city = city_from_display_name(display_name)
    if not city:
        return fallback
    return {
        "city": city,
        "name": display_name or city,
        "lat": latitude,
        "lng": longitude,
        "resolved": True,
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


def to_seed_items(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """种子数据 → 入库条目:与 :func:`to_place_items` 同形状,**但带静态 ``intro``**。

    种子没有 OSM 身份,``place_identity`` 会用"名字 + 坐标"的 sha1 指纹兜底成
    ``("point", 负数)``:既不会与真实 OSM id 撞车,重复写入也照样被唯一键挡住。
    ``category`` 直接沿用种子里声明的值(:func:`services.seed_data.validate` 保证它与
    ``categorize(tags)`` 一致),``intro`` 是人工静态文案 —— 有了它,
    :func:`services.intro.fill_missing_intros` 就不会再把这些行送去调 LLM。
    """
    items: list[dict[str, Any]] = []
    for row in rows or []:
        osm_type, osm_id = place_identity(row)
        items.append(
            {
                "osm_type": osm_type,
                "osm_id": osm_id,
                "name": row.get("name") or "",
                "lat": row["lat"],
                "lng": row["lng"],
                "category": row.get("category") or UNCATEGORIZED,
                "tags": dict(row.get("tags") or {}),
                "intro": str(row.get("intro") or "").strip(),
            }
        )
    return items


def ensure_seeded(
    session: Session,
    *,
    origin: Mapping[str, Any],
    band: Mapping[str, Any],
    seeds: Optional[Iterable[Mapping[str, Any]]] = None,
) -> int:
    """给某个 (起点, band) 幂等补种:只写库里还没有的种子,返回**新增**条数。

    纯本地操作(不触网、不调 LLM),所以已入库的分段二次查询时也能顺手补种,
    存量库不必重抓 Overpass。去重口径与 :func:`services.seed_data.attach_seeds` 一致:
    库里已有同名(相等或互相包含)且距离 ≤ 2 km 的行,就认为已覆盖、不再补。
    """
    seed_rows = list(seeds) if seeds is not None else seed_data.load_seeds()
    if not seed_rows:
        return 0
    in_band = seed_data.seeds_in_band(seed_rows, origin["lat"], origin["lng"], band)
    if not in_band:
        return 0
    stored = repo.list_places(
        session,
        origin_city=origin["city"],
        band=band["key"],
        origin_lat=origin["lat"],
        origin_lng=origin["lng"],
    )
    fresh = seed_data.attach_seeds(stored, in_band)
    if not fresh:
        return 0
    return repo.upsert_places(
        session, origin_city=origin["city"], band=band["key"], items=to_seed_items(fresh)
    )


def record_seed_segment(
    session: Session, *, city: str, band: Mapping[str, Any], origin: Mapping[str, Any]
) -> SegmentFetch:
    """给"只有种子、还没抓过 Overpass"的分段建一条水位(``source="seed"``)。"""
    return repo.record_segment(
        session,
        origin_city=city,
        band=band["key"],
        origin=origin,
        place_count=0,
        source=SOURCE_SEED,
    )


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
    seeds: Optional[Iterable[Mapping[str, Any]]] = None,
    refresh: bool = False,
    intros: bool = True,
    intro_limit: Optional[int] = INTRO_BATCH_LIMIT,
    intro_workers: int = intro_service.DEFAULT_WORKERS,
) -> SegmentOutcome:
    """读取某 (城市, band) 的目的地;未入库才抓取并落库。

    ``intros=True`` 时,**抓取入库之后**再给缺简介的 POI 补 LLM 一句话简介
    (只作用于本次抓取路径:命中库直接读库时不调 LLM,保持零网络秒回)。
    简介失败一律降级为空,不影响已入库的数据。

    ``seeds`` 留空就用 :func:`services.seed_data.load_seeds`(受 ``WHERE2GO_SEEDS``
    开关控制);传 ``[]`` 可显式关掉本次的种子合并。抓取与读库两条路径都合并种子,
    所以已入库的分段也能拿到种子,不必重抓 Overpass。

    失败语义:分段/城市非法抛 :class:`ValueError`;数据源不可用抛
    :class:`data_sources.DataSourceError`(由 API 层翻成中文 HTTP 错误)。
    """
    band_def = require_band(band)
    city_clean = (city or "").strip()
    if not city_clean:
        raise ValueError("起点城市不能为空")
    fetch_fn = fetcher or default_fetcher
    seed_rows = list(seeds) if seeds is not None else seed_data.load_seeds()

    recorded = repo.get_segment(session, origin_city=city_clean, band=band_def["key"])
    if recorded is not None and not refresh:
        origin = stored_origin(recorded)
        # 读库路径也补种:纯本地、幂等(第二次必然补 0 条),存量库不必重抓 Overpass
        seeded = ensure_seeded(session, origin=origin, band=band_def, seeds=seed_rows)
        if seeded:
            recorded.place_count = int(recorded.place_count or 0) + seeded
            session.commit()
        return _read_from_db(
            session,
            origin=origin,
            band=band_def,
            category=category,
            source=SOURCE_DB,
            network_used=False,
            segment=repo.segment_to_dict(recorded),
            seeded=seeded,
        )

    origin = _reuse_origin(session, city_clean, recorded=recorded, lat=lat, lng=lng, geocoder=geocoder)
    candidates = filter_to_band(
        fetch_fn(origin["lat"], origin["lng"], band_def) or [],
        origin["lat"],
        origin["lng"],
        band_def,
    )
    fresh_seeds = _fresh_seeds(candidates, seed_rows, origin, band_def)
    seeded_items = to_seed_items(fresh_seeds)
    written = repo.upsert_places(
        session,
        origin_city=city_clean,
        band=band_def["key"],
        items=to_place_items(candidates) + seeded_items,
    )
    record = repo.record_segment(
        session,
        origin_city=city_clean,
        band=band_def["key"],
        origin=origin,
        place_count=len(candidates) + len(seeded_items),
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
        seeded=len(seeded_items),
        intro_stats=intro_stats,
    )


def _fresh_seeds(
    candidates: Iterable[Mapping[str, Any]],
    seed_rows: Sequence[Mapping[str, Any]],
    origin: Mapping[str, Any],
    band: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """抓取路径的种子合并:先按环筛种子,再与本次 OSM 结果按名字 + 坐标去重。"""
    if not seed_rows:
        return []
    in_band = seed_data.seeds_in_band(seed_rows, origin["lat"], origin["lng"], band)
    return seed_data.attach_seeds(candidates, in_band)


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
    seeded: int = 0,
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
        seeded=seeded,
        counts_by_category=counts,
        counts_by_source=repo.count_by_source(
            session, origin_city=origin["city"], band=band["key"]
        ),
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
