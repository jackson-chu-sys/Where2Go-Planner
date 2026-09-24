"""住宿检索 + 价格估算(TASK-3a1,阶段3a **服务层**;路由与前端由 TASK-3a2 接)。

为什么单独一张表而不复用 ``Place``:住宿是行程的**落脚点**,不进需求四分类,展示要的
是价格区间估算而不是分类标签,所以落 :class:`db.models.Stay`,唯一键 ``(osm_type, osm_id)``
—— 同一家酒店从不同起点搜到只存一行(详见该表 docstring)。

三段式(照 :mod:`services.place_loader` 的结构,只是范围缩到"一个坐标 + 半径"):

* :func:`search_stays` —— Overpass **单组**并集检索:``tourism=hotel/guest_house/hostel/
  apartment/chalet`` 五个选择器塞进同一个分组(一次 HTTP 请求、共用一个配额,住宿在
  8 km 内密度低,不必像四分类那样按组拆配额),用 :func:`data_sources.overpass.parse_places`
  解析(``limit=None`` 全收、``require_name=False`` —— 民宿/公寓常没有 ``name`` tag,
  不能在服务端就滤掉),返回**已按离起点由近及远排序**、带 ``osm_type``/``osm_id`` 的行。
  任何失败(端点全挂、查询非法、响应格式不对)一律降级成**空列表**,不抛给调用方。
* :func:`estimate_price` —— LLM 估价 + 一句话简介,**一次调用出两行**
  (``价格: 约¥A-B/晚`` / ``简介: <40字内>``):比拆两次调用省一半 token 与限流额度。
  复用 :class:`services.intro.LLMClient`(Provider 可切换、key 只读环境变量),
  未配 key / 超时 / 限流 / 格式不对 → ``("", "")``,**绝不抛异常、绝不阻塞入库**
  (与 :func:`services.intro.generate_intro` 同口径)。价格是**估算**:规范串恒带"约",
  ``currency`` 默认 ``CNY``,序列化时另给 ``price_is_estimate`` 标注(架构文档"AI 幻觉"
  对策:事实字段绑结构化来源,估算字段必须自带标注)。
* :func:`load_or_fetch_stays` —— **DB 即缓存**:该坐标半径内已有 ≥
  :data:`MIN_CACHED_ROWS` 行就直接读库返回(``source="db"``),否则检索 → upsert →
  只给**缺价格**的行调 LLM → 落库(``source="overpass"``);``refresh=True`` 强制重抓。
  已有 ``price_estimate`` 的行**永不再调 LLM**,与 ``Place.intro`` 的缓存口径一致。

CLI(给夜间预抓/排查用,联网)::

    python -m services.stays 31.2304 121.4737 --radius 8000 --limit 10
"""

from __future__ import annotations

import argparse
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from data_sources import DataSourceError
from data_sources import overpass
from db.models import (
    COORD_PRECISION,
    CURRENCY_LEN,
    DEFAULT_CURRENCY,
    KIND_LEN,
    NAME_LEN,
    PRICE_LEN,
    Stay,
    clean_text,
    iso_utc,
    utcnow,
)
from services.intro import INTRO_TARGET_CHARS, LLMClient, clean_intro
from services.intro import default_client as default_llm_client

# ``tourism=*`` 的住宿类型:**值即 kind**(顺序 = Overpass 选择器顺序)
STAY_TAGS: tuple[str, ...] = ("hotel", "guest_house", "hostel", "apartment", "chalet")
DEFAULT_RADIUS_M = 8000
LAT_LIMIT = 90.0
LNG_LIMIT = 180.0
# 单组五选择器的服务端配额:住宿密度低,200 条足够,又不至于把公共实例拖慢
GROUP_BUDGET = 200
# 客户端 HTTP 超时:8 km 单组并集是**交互路径**,不走 place_loader 那档 150s 冷启动超时
STAY_REQUEST_TIMEOUT_S = 30.0
DISTANCE_PRECISION = 2
# DB 即缓存:半径内已有这么多行就不再触网(refresh=True 可强制重抓)
MIN_CACHED_ROWS = 1
SOURCE_DB = "db"
SOURCE_FETCH = "overpass"
METERS_PER_DEGREE = 111_320.0
# 高纬度兜底:cos(lat) 太小时经度包围盒会炸开,夹一个下限保证 SQL 粗筛仍收敛
MIN_COS_LAT = 0.05

# 进 prompt 的住宿标签白名单(房价线索优先;上限 STAY_FACT_LIMIT 个,不塞整包 tag)
STAY_FACT_TAGS: tuple[str, ...] = (
    "tourism", "stars", "rooms", "beds", "brand", "operator", "internet_access",
    "wheelchair", "addr:city", "addr:street", "opening_hours", "website",
)
STAY_FACT_LIMIT = 8
STAY_SYSTEM_PROMPT = (
    "你是 Where2Go(周末去哪儿玩)的住宿信息助手,为行程落脚点写**价格区间估算**与一句话简介。"
    "严格按用户要求的两行输出,不要编号、标题、解释或多余文字:"
    "第一行 `价格: 约¥A-B/晚`(人民币、一晚的大致区间,必须带“约”字;不确定就给宽一点的区间);"
    "第二行 `简介: <40字内一句话>`。"
    "价格只能依据名称、住宿类型、星级/房量等标签与所在城市做常识性估算,"
    "不得编造具体房型、电话、地址、促销或任何精确数字;"
    "简介只依据给出的名称/类型/标签,信息不足就写该类型的通用描述,"
    "不要提及 OSM、标签、数据源或模型。"
)
OUTPUT_FORMAT_LINES: tuple[str, ...] = (
    "请严格按下面两行输出,不要多余文字:",
    "价格: 约¥A-B/晚",
    "简介: <40字内一句话>",
)

PRICE_LINE_RE = re.compile(r"^(?:预估|估算|参考)?(?:价格|房价|价位|均价)\s*[:：]\s*(?P<value>.+)$")
INTRO_LINE_RE = re.compile(r"^(?:一句话)?简介\s*[:：]\s*(?P<value>.+)$")
PRICE_RANGE_RE = re.compile(
    r"(?P<low>\d[\d,，.]*)\s*(?:元|[¥￥])?\s*"
    r"(?:[-~－—–至到]\s*(?:[¥￥]?\s*)?(?P<high>\d[\d,，.]*))?"
)
QUOTE_CHARS = "\"'“”‘’「」『』 "


# --------------------------------------------------------------------------- #
# 归一工具
# --------------------------------------------------------------------------- #


def _get(stay: Any, key: str) -> Any:
    """从 Mapping 或 ORM 行里取字段(两种入参都收,调用方不必先转 dict)。"""
    if isinstance(stay, Mapping):
        return stay.get(key)
    return getattr(stay, key, None)


def _as_float(value: Any) -> Optional[float]:
    """宽容转 float:非数字 / NaN / inf → ``None``(降级,不抛)。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def coordinate(value: Any) -> Optional[float]:
    """坐标归一:定点 :data:`~db.models.COORD_PRECISION` 位小数;非法 → ``None``。"""
    number = _as_float(value)
    return None if number is None else round(number, COORD_PRECISION)


def coordinate_pair(lat: Any, lng: Any) -> Optional[tuple[float, float]]:
    """经纬度归一;缺一或越界 → ``None``。

    调用方据此降级:检索侧当成"没搜到"返回空列表,入库侧直接跳过该行。
    """
    latitude = coordinate(lat)
    longitude = coordinate(lng)
    if latitude is None or longitude is None:
        return None
    if not -LAT_LIMIT <= latitude <= LAT_LIMIT or not -LNG_LIMIT <= longitude <= LNG_LIMIT:
        return None
    return latitude, longitude


def stay_kind(tags: Optional[Mapping[str, Any]]) -> str:
    """住宿类型归一:``tourism`` 值命中 :data:`STAY_TAGS` 即为 kind。

    命中不了就退回 ``tourism`` 原值(``motel``/``resort`` 这类同族写法照收,便于前端分组);
    连 ``tourism`` 都没有时看别的 tag 值里有没有住宿类型(``building=hotel`` 的少数写法),
    都没有 → 空串(不猜)。
    """
    normalized = {
        str(key).strip().lower(): str(value).strip().lower()
        for key, value in dict(tags or {}).items()
    }
    tourism = normalized.get("tourism", "")
    if tourism in STAY_TAGS:
        return tourism
    for tag in STAY_TAGS:
        if tag in normalized.values():
            return tag
    return tourism[:KIND_LEN]


def normalize_kind(row: Any) -> str:
    """一行的 kind:显式给了就用(小写归一),否则从 ``tags`` 推。"""
    explicit = clean_text(_get(row, "kind"), limit=KIND_LEN)
    if explicit:
        return explicit.lower()
    tags = _get(row, "tags")
    return stay_kind(tags if isinstance(tags, Mapping) else None)


def stay_identity(row: Any) -> Optional[tuple[str, int]]:
    """OSM 身份 ``(osm_type, osm_id)``;缺一个就返回 ``None``(无法幂等 upsert,该行跳过)。

    与 :data:`db.models.Stay` 的唯一键同口径;``osm_id=0`` 不是合法 OSM 身份,同样跳过。
    """
    osm_type = str(_get(row, "osm_type") or "").strip().lower()
    raw_id = _get(row, "osm_id")
    if not osm_type or raw_id is None or isinstance(raw_id, bool):
        return None
    try:
        osm_id = int(str(raw_id).strip())
    except (TypeError, ValueError):
        return None
    if osm_id == 0:
        return None
    return osm_type[:16], osm_id


def distance_km(origin_lat: Any, origin_lng: Any, lat: Any, lng: Any) -> Optional[float]:
    """haversine 距离(公里,保留 :data:`DISTANCE_PRECISION` 位);坐标不全 → ``None``。"""
    origin = coordinate_pair(origin_lat, origin_lng)
    point = coordinate_pair(lat, lng)
    if origin is None or point is None:
        return None
    return round(
        overpass.haversine_km(origin[0], origin[1], point[0], point[1]), DISTANCE_PRECISION
    )


def _raw_distance(origin_lat: float, origin_lng: float, lat: Any, lng: Any) -> Optional[float]:
    """未取整的 haversine 距离(SQL 粗筛后精确复核 + 排序用)。"""
    latitude = _as_float(lat)
    longitude = _as_float(lng)
    if latitude is None or longitude is None:
        return None
    return overpass.haversine_km(origin_lat, origin_lng, latitude, longitude)


# --------------------------------------------------------------------------- #
# 检索(Overpass)
# --------------------------------------------------------------------------- #


def stay_groups(*, budget: int = GROUP_BUDGET) -> list[dict[str, Any]]:
    """住宿检索分组:**单组**,组内每个 ``{"tourism": tag}`` 一个选择器(并集、共用配额)。"""
    return [
        {
            "tags": [{"tourism": tag} for tag in STAY_TAGS],
            "element_types": "nwr",
            "budget": budget,
        }
    ]


def build_stay_query(
    lat: float, lng: float, radius_m: float = DEFAULT_RADIUS_M, *, budget: int = GROUP_BUDGET
) -> str:
    """构造住宿检索的 Overpass QL(:func:`data_sources.overpass.build_grouped_query` 单组)。

    ``require_name=False``:民宿/公寓/小屋经常没有 ``name`` tag,服务端加 ``["name"]``
    会把它们整片滤掉,宁可取回来在展示层兜底成"(无名)"。
    """
    return overpass.build_grouped_query(
        lat, lng, radius_m, stay_groups(budget=budget), require_name=False
    )


def stay_row(place: Mapping[str, Any], origin_lat: float, origin_lng: float) -> dict[str, Any]:
    """``parse_places`` 的一条结果 → 住宿行(补 ``kind`` 与 ``distance_km``)。"""
    tags = dict(place.get("tags") or {})
    return {
        "osm_type": str(place.get("osm_type") or ""),
        "osm_id": place.get("osm_id"),
        "name": str(place.get("name") or "").strip(),
        "kind": stay_kind(tags),
        "lat": place.get("lat"),
        "lng": place.get("lng"),
        "tags": tags,
        "distance_km": distance_km(origin_lat, origin_lng, place.get("lat"), place.get("lng")),
    }


def search_stays(
    lat: float,
    lng: float,
    radius_m: int = DEFAULT_RADIUS_M,
    *,
    client: Optional[overpass.OverpassClient] = None,
) -> list[dict[str, Any]]:
    """检索起点半径内的住宿,按由近及远排序;**任何失败都返回空列表**(降级,不抛)。

    ``client`` 可注入(测试替换成假客户端);缺省用
    :func:`data_sources.overpass.default_client`(端点链 + 重试)。
    """
    origin = coordinate_pair(lat, lng)
    if origin is None:
        return []
    origin_lat, origin_lng = origin
    http = client if client is not None else overpass.default_client()
    try:
        query = build_stay_query(origin_lat, origin_lng, radius_m)
        payload = http.execute(query, timeout=STAY_REQUEST_TIMEOUT_S)
        places = overpass.parse_places(
            payload, origin_lat, origin_lng, limit=None, require_name=False, with_id=True
        )
    except DataSourceError:
        return []
    except Exception:  # noqa: BLE001 - 检索是增强项:查询非法/响应异常都当"没搜到"
        return []

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for place in places:
        row = stay_row(place, origin_lat, origin_lng)
        identity = stay_identity(row)
        if identity is None or identity in seen:
            continue
        seen.add(identity)
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# 估价 + 简介(LLM)
# --------------------------------------------------------------------------- #


def resolve_llm(environ: Optional[Mapping[str, str]] = None) -> LLMClient:
    """LLM 客户端:给了 ``environ`` 就按它解析 Provider(测试传 ``{}`` 模拟"未配 key")。"""
    if environ is None:
        return default_llm_client()
    return LLMClient(environ=environ)


def stay_facts(tags: Optional[Mapping[str, Any]], *, limit: int = STAY_FACT_LIMIT) -> str:
    """住宿标签摘要 ``k=v; k=v``(白名单 + 上限),给 LLM 当房价线索。"""
    normalized = {
        str(key).strip().lower(): str(value).strip() for key, value in dict(tags or {}).items()
    }
    facts: list[str] = []
    for key in STAY_FACT_TAGS:
        value = normalized.get(key)
        if value and value.lower() not in ("no", "none", "unknown"):
            facts.append(f"{key}={value}")
        if len(facts) >= limit:
            break
    return "; ".join(facts)


def _where_line(stay: Any) -> str:
    """prompt 里的位置行:坐标(定点)+ 距起点公里数(有才带)。"""
    parts: list[str] = []
    point = coordinate_pair(_get(stay, "lat"), _get(stay, "lng"))
    if point is not None:
        parts.append(f"坐标:{point[0]:.7f},{point[1]:.7f}")
    distance = _as_float(_get(stay, "distance_km"))
    if distance is not None:
        parts.append(f"距起点:{distance:.1f} km")
    return " · ".join(parts)


def build_price_prompt(stay: Any) -> str:
    """按住宿拼 prompt:名称 + 类型 + 位置 + 标签摘要 + **严格两行**的输出格式要求。"""
    name = str(_get(stay, "name") or "").strip() or "(无名)"
    kind = normalize_kind(stay)
    lines = [f"名称:{name}", f"类型:{kind or '住宿'}"]
    where = _where_line(stay)
    if where:
        lines.append(where)
    facts = stay_facts(_get(stay, "tags") if isinstance(_get(stay, "tags"), Mapping) else None)
    if facts:
        lines.append(f"OSM 标签:{facts}")
    lines.extend(OUTPUT_FORMAT_LINES)
    return "\n".join(lines)


def _clean_number(text: Optional[str]) -> str:
    """``1,200.50`` → ``1200.5``;空 / 非数字 / ≤0 → ``""``。"""
    digits = str(text or "").replace(",", "").replace(",", "").strip().strip(".")
    number = _as_float(digits)
    if number is None or number <= 0:
        return ""
    return str(int(number)) if number == int(number) else f"{number:g}"


def parse_price(completion: Optional[str]) -> str:
    """取价格行 → 规范串 ``约¥A-B/晚``(单值则 ``约¥A/晚``);解析不出 → ``""``。

    规范化的意义:LLM 会写 ``300-500元``/``约 ¥300~500 每晚``/``1,200-1,800`` 等一堆变体,
    落库统一成一种带"约"的口径,前端不必再猜格式;"约"字就是估算标注。
    """
    for line in str(completion or "").splitlines():
        matched = PRICE_LINE_RE.match(line.strip())
        if matched is None:
            continue
        value = matched.group("value").strip(QUOTE_CHARS)
        numbers = PRICE_RANGE_RE.search(value)
        if numbers is None:
            return ""
        low = _clean_number(numbers.group("low"))
        high = _clean_number(numbers.group("high"))
        if not low:
            return ""
        if high and high != low:
            return f"约¥{low}-{high}/晚"[:PRICE_LEN]
        return f"约¥{low}/晚"[:PRICE_LEN]
    return ""


def parse_intro_line(completion: Optional[str], *, max_chars: int = INTRO_TARGET_CHARS) -> str:
    """取 ``简介:`` 行,复用 :func:`services.intro.clean_intro` 清洗(压一行/去引号/截断)。"""
    for line in str(completion or "").splitlines():
        matched = INTRO_LINE_RE.match(line.strip())
        if matched is None:
            continue
        return clean_intro(matched.group("value"), max_chars=max_chars)
    return ""


def estimate_price(
    stay: Any,
    *,
    client: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> tuple[str, str]:
    """给一处住宿估**价格区间 + 一句话简介**,返回 ``(price_estimate, intro)``。

    降级口径(与 :func:`services.intro.generate_intro` 一致):未配 key、没有名称、
    网络/限流异常、返回格式不对 —— 一律 ``("", "")``,**绝不抛异常**。
    已有 ``price_estimate`` 的行**原样返回、不调用 LLM**(DB 即缓存)。
    """
    existing = clean_text(_get(stay, "price_estimate"), limit=PRICE_LEN)
    if existing:
        return existing, str(_get(stay, "intro") or "").strip()

    llm = client if client is not None else resolve_llm(environ)
    if not llm.enabled:
        return "", ""
    if not str(_get(stay, "name") or "").strip():
        return "", ""
    try:
        completion = llm.chat(build_price_prompt(stay), system=STAY_SYSTEM_PROMPT)
    except DataSourceError:
        return "", ""
    except Exception:  # noqa: BLE001 - 估价是增强项,任何异常都不得阻塞入库
        return "", ""
    price = parse_price(completion)
    if not price:
        return "", ""
    return price, parse_intro_line(completion)


def estimate_missing(
    stays: Sequence[Any],
    *,
    client: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> int:
    """给**缺价格**的行就地补 ``(price_estimate, intro)``,返回补上的条数(不 commit)。"""
    estimated = 0
    for stay in stays:
        if clean_text(_get(stay, "price_estimate"), limit=PRICE_LEN):
            continue
        price, intro = estimate_price(stay, client=client, environ=environ)
        if not price:
            continue
        stay.price_estimate = price
        if intro and not str(_get(stay, "intro") or "").strip():
            stay.intro = intro
        estimated += 1
    return estimated


# --------------------------------------------------------------------------- #
# 入库 / 读库
# --------------------------------------------------------------------------- #


def select_stays(
    session: Session, lat: float, lng: float, radius_m: float = DEFAULT_RADIUS_M
) -> list[Stay]:
    """半径内的住宿行:SQL 先按经纬度**包围盒**粗筛,再用 haversine 精确复核 + 由近及远排序。

    住宿表按 OSM 身份存,不带起点城市/分段,所以"某坐标半径内"只能这样按坐标筛;
    包围盒让 SQLite 走 ``ix_stay_location`` 索引,不必全表算 haversine。
    """
    origin = coordinate_pair(lat, lng)
    if origin is None:
        return []
    origin_lat, origin_lng = origin
    radius = _as_float(radius_m)
    if radius is None or radius <= 0:
        return []
    lat_delta = radius / METERS_PER_DEGREE
    lng_delta = radius / (METERS_PER_DEGREE * max(MIN_COS_LAT, abs(math.cos(math.radians(origin_lat)))))
    rows = session.scalars(
        select(Stay).where(
            Stay.lat.between(origin_lat - lat_delta, origin_lat + lat_delta),
            Stay.lng.between(origin_lng - lng_delta, origin_lng + lng_delta),
        )
    ).all()
    matched: list[tuple[float, Stay]] = []
    for row in rows:
        raw = _raw_distance(origin_lat, origin_lng, row.lat, row.lng)
        if raw is None or raw > radius / 1000.0:
            continue
        matched.append((raw, row))
    matched.sort(key=lambda item: (item[0], item[1].id))
    return [row for _, row in matched]


def upsert_stays(session: Session, rows: Sequence[Mapping[str, Any]]) -> int:
    """按 ``(osm_type, osm_id)`` 幂等入库,返回写入行数;**不覆盖已生成的价格与简介**。

    刷新的是"抓取事实"(名称/类型/坐标/标签/距离/``fetched_at``);LLM 产物
    (``price_estimate``/``intro``)只在**该行还空着**时才写,重抓不冲掉已花的 token。
    只 ``flush`` 不 ``commit``:提交时机交给调用方(编排函数/脚本)。
    """
    written = 0
    for row in rows or ():
        identity = stay_identity(row)
        if identity is None:
            continue
        point = coordinate_pair(_get(row, "lat"), _get(row, "lng"))
        if point is None:
            continue
        lat, lng = point
        osm_type, osm_id = identity
        stay = session.scalar(select(Stay).where(Stay.osm_type == osm_type, Stay.osm_id == osm_id))
        created = stay is None
        if created:
            stay = Stay(osm_type=osm_type, osm_id=osm_id)
            session.add(stay)

        tags = _get(row, "tags")
        stay.name = clean_text(_get(row, "name"), limit=NAME_LEN) or ""
        stay.kind = normalize_kind(row)
        stay.lat = lat
        stay.lng = lng
        stay.tags = dict(tags) if isinstance(tags, Mapping) else {}
        distance = _as_float(_get(row, "distance_km"))
        if created or distance is not None:
            stay.distance_km = None if distance is None else round(distance, DISTANCE_PRECISION)
        if created or not str(stay.price_estimate or "").strip():
            stay.price_estimate = clean_text(_get(row, "price_estimate"), limit=PRICE_LEN)
        if created or not str(stay.intro or "").strip():
            stay.intro = clean_text(_get(row, "intro"))
        currency = clean_text(_get(row, "currency"), limit=CURRENCY_LEN)
        if currency:
            stay.currency = currency.upper()
        elif created:
            stay.currency = DEFAULT_CURRENCY
        stay.fetched_at = utcnow()
        written += 1

    if written:
        session.flush()
    return written


def stay_to_dict(
    stay: Stay,
    *,
    origin_lat: Optional[float] = None,
    origin_lng: Optional[float] = None,
    source: str = SOURCE_DB,
) -> dict[str, Any]:
    """序列化(:func:`db.repository.place_to_dict` 同风格):坐标定点、时间 ISO、带来源与估算标注。

    给了起点就按起点**现算** ``distance_km``(库里的值只是上次检索的快照);
    ``price_is_estimate`` 是估算标注字段 —— 价格是 LLM 的常识性区间,不是报价。
    """
    computed = (
        distance_km(origin_lat, origin_lng, stay.lat, stay.lng)
        if origin_lat is not None and origin_lng is not None
        else None
    )
    distance = computed if computed is not None else _as_float(stay.distance_km)
    return {
        "id": stay.id,
        "osm_type": stay.osm_type,
        "osm_id": stay.osm_id,
        "name": stay.name,
        "kind": stay.kind,
        "lat": coordinate(stay.lat),
        "lng": coordinate(stay.lng),
        "tags": dict(stay.tags or {}),
        "distance_km": None if distance is None else round(distance, DISTANCE_PRECISION),
        "price_estimate": stay.price_estimate or None,
        "price_is_estimate": bool(stay.price_estimate),
        "currency": stay.currency or DEFAULT_CURRENCY,
        "intro": stay.intro or None,
        "fetched_at": iso_utc(stay.fetched_at),
        "source": source,
    }


def load_or_fetch_stays(
    session: Session,
    lat: float,
    lng: float,
    *,
    radius_m: int = DEFAULT_RADIUS_M,
    refresh: bool = False,
    client: Optional[overpass.OverpassClient] = None,
    llm: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> list[dict[str, Any]]:
    """读库优先的住宿列表(**DB 即缓存**),每项带 ``distance_km``(haversine,2 位)。

    半径内已有 ≥ :data:`MIN_CACHED_ROWS` 行且未 ``refresh`` → 直接读库,
    ``source="db"``,**零次网络、零次 LLM**;否则检索 → upsert → 只给缺价格的行估价 →
    commit,``source="overpass"``(检索失败但库里有货时仍按 ``"db"`` 返回,不谎报来源)。
    起点非法或两边都拿不到数据 → 空列表(降级,不抛)。
    """
    origin = coordinate_pair(lat, lng)
    if origin is None:
        return []
    origin_lat, origin_lng = origin

    cached = select_stays(session, origin_lat, origin_lng, radius_m)
    if not refresh and len(cached) >= MIN_CACHED_ROWS:
        return [
            stay_to_dict(row, origin_lat=origin_lat, origin_lng=origin_lng, source=SOURCE_DB)
            for row in cached
        ]

    rows = search_stays(origin_lat, origin_lng, radius_m, client=client)
    if rows:
        upsert_stays(session, rows)
    stays = select_stays(session, origin_lat, origin_lng, radius_m)
    if not stays:
        return []
    estimated = estimate_missing(stays, client=llm, environ=environ)
    if rows or estimated:
        session.commit()
    source = SOURCE_FETCH if rows else SOURCE_DB
    return [
        stay_to_dict(row, origin_lat=origin_lat, origin_lng=origin_lng, source=source)
        for row in stays
    ]


def main(argv: Optional[list[str]] = None) -> int:
    """CLI:抓一个坐标半径内的住宿并入库估价。``python -m services.stays 31.2304 121.4737``"""
    parser = argparse.ArgumentParser(description="检索/入库周边住宿(带 LLM 价格估算与简介)")
    parser.add_argument("lat", type=float, help="起点纬度")
    parser.add_argument("lng", type=float, help="起点经度")
    parser.add_argument("--radius", type=int, default=DEFAULT_RADIUS_M, help=f"半径(米,默认 {DEFAULT_RADIUS_M})")
    parser.add_argument("--refresh", action="store_true", help="忽略库缓存,强制重新检索")
    parser.add_argument("--limit", type=int, default=None, help="最多显示多少条(默认:全部)")
    parser.add_argument("--db", default=None, help="数据库 URL(默认 WHERE2GO_DB_URL 或 backend/data/where2go.db)")
    args = parser.parse_args(argv)

    from db import init_db, make_engine, open_session

    engine = make_engine(args.db)
    init_db(engine)
    with open_session(engine) as session:
        stays = load_or_fetch_stays(
            session, args.lat, args.lng, radius_m=args.radius, refresh=args.refresh
        )
    shown = stays if args.limit is None else stays[: max(0, int(args.limit))]
    for item in shown:
        distance = item["distance_km"]
        price = item["price_estimate"] or "未估价"
        print(
            f"[{item['kind'] or '住宿'}] {item['name'] or '(无名)'} · "
            f"{'-' if distance is None else f'{distance:.1f} km'} · {price}(估算) · {item['source']}"
        )
    print(f"[完成] {len(shown)} / {len(stays)} 条 · 半径 {args.radius} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
