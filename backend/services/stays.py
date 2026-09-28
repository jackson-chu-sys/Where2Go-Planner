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

TASK-6c 在这个骨架上加了三件事(BUG-3/5):

* **负缓存**:空结果与检索失败都落一行 :class:`db.models.StayQueryCache`,
  :data:`NEG_CACHE_TTL_S`(6 小时)内同坐标同半径**直接回缓存态、不再触网**;
  过期即重查。缓存态带三档 ``reason`` —— ``no_data``(真的没有)/
  ``datasource_error``(Overpass 报错)/ ``timeout``(超时),API 原样透传给前端分文案。
* **半径阶梯** :data:`STAY_RADIUS_LADDER_M`(5→10→30 km):调用方**没显式给半径**时,
  小半径空结果就逐级扩大再查,扩到有结果即停,并给 ``nearest_km``(最近一家的 haversine
  距离,1 位小数);显式给了半径就**只查那一档**(不擅自扩,尊重调用方口径)。
  检索**失败**不扩档 —— 端点已经挂了,再打两遍只是白等。
* **估价异步回填**:检索入库后就能返回列表(``price_estimate`` 可为 null)。待估价的行数
  超过一批(:data:`PRICE_BATCH_SIZE` = 5 家)时不再阻塞请求,而是丢给后台线程**批量**
  回填(5 家一个 prompt、固定 ``qwen3.8-max`` 的 token-plan 注册项、单批重试 ≤ 1 次、
  解析不出就留 null 不抛),响应带 ``estimating=True``;一批以内仍就地算完,首屏即有价格。
  线程池口径照 :func:`services.intro._run_batch`,执行器可注入(测试/CLI 用同步执行器)。

CLI(给夜间预抓/排查用,联网)::

    python -m services.stays 31.2304 121.4737 --radius 8000 --limit 10
"""

from __future__ import annotations

import argparse
import math
import os
import re
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timezone
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
    REASON_DATASOURCE_ERROR,
    REASON_NO_DATA,
    REASON_TIMEOUT,
    STAY_CACHE_EMPTY,
    STAY_REASON_LEN,
    Stay,
    StayQueryCache,
    clean_text,
    iso_utc,
    stay_cache_kind,
    stay_cache_reason,
    utcnow,
)
from services.intro import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MODEL,
    INTRO_TARGET_CHARS,
    LLMClient,
    ResolvedLLM,
    clean_intro,
    find_provider,
)
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

# --- TASK-6c:负缓存 / 半径阶梯 / 后台批量估价 --- #
# 负缓存有效期:6 小时内同坐标同半径不再触网(空结果与失败都记)
NEG_CACHE_TTL_S = 6 * 3600
# 半径阶梯:调用方未显式给半径时,5 km 空 → 10 km → 30 km,扩到有结果即停
STAY_RADIUS_LADDER_M: tuple[int, ...] = (5000, 10000, 30000)
# ``nearest_km`` 的口径:最近一家的 haversine 距离,保留 1 位(给"最近的在 X km 外"提示)
NEAREST_KM_PRECISION = 1
# LLM 批量估价:每批 5 家一个 prompt(神朱 2026-09-28 定),单批重试不超过 1 次
PRICE_BATCH_SIZE = 5
PRICE_BATCH_RETRIES = 1
# 批量输出比单家长得多,token 预算与超时按调用放宽(照 intro.chat 的口径,不放开会被截断)
PRICE_BATCH_MAX_TOKENS = 1200
PRICE_BATCH_TIMEOUT_S = 90.0
# 后台回填线程数:估价是限流敏感路径,2 条足够,不与 place 简介抢额度
PRICE_BACKGROUND_WORKERS = 2
# 待估价行数 ≤ 这个值(= 一批)就地算完再返回;超过才转后台(首屏价格 vs. 请求不被拖死的折中)
SYNC_ESTIMATE_MAX_ROWS = PRICE_BATCH_SIZE
# 批量估价固定用注册表里的 qwen(token-plan 入口,qwen3.8-max):神朱定,**不做双模型**
PRICE_PROVIDER_NAME = "qwen"
PRICE_MODEL = "qwen3.8-max"
# 估价口径开关(``estimate=`` 参数):auto = 按待估行数自动选
ESTIMATE_AUTO = "auto"
ESTIMATE_SYNC = "sync"
ESTIMATE_ASYNC = "async"
ESTIMATE_OFF = "off"
ESTIMATE_MODES: tuple[str, ...] = (ESTIMATE_AUTO, ESTIMATE_SYNC, ESTIMATE_ASYNC, ESTIMATE_OFF)
# 超时判定的文案线索:``data_sources._common`` 把 requests.Timeout 包成"请求超时(>20s)"
TIMEOUT_HINTS: tuple[str, ...] = ("请求超时", "超时(>", "timed out", "timeout")

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
# 批量估价输出的行首序号(``1|…`` / ``1. …`` / ``1) …`` 都收)与字段分隔符
BATCH_INDEX_RE = re.compile(r"^(?P<index>\d{1,3})\s*[|｜.、)）:：\-]\s*(?P<rest>.*)$")
BATCH_SEPARATOR_RE = re.compile(r"[|｜]")
# 批量行里"简介"挤在价格后面(没有 ``|`` 分隔)时的兜底:不锚行首,只抓到下一个分隔符前
INTRO_INLINE_RE = re.compile(r"简介\s*[:：]\s*(?P<value>[^|｜\n]+)")


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
    需要知道"为什么空"的调用方(编排层/负缓存)用 :func:`search_stays_detailed`。
    """
    return search_stays_detailed(lat, lng, radius_m, client=client)[0]


def classify_failure(exc: BaseException) -> str:
    """检索异常 → 三档 reason 里的失败档:``timeout`` 或 ``datasource_error``。

    超时单独分档是因为前端文案不同(超时="稍后再试",其他="可重试/换端点")。
    ``data_sources._common`` 把 :class:`requests.Timeout` 包成 ``请求超时(>20s)`` 的
    :class:`~data_sources.TransientDataSourceError`,Overpass 端点链全挂时又把每次尝试的
    明细拼进最终消息,所以按**文案线索**判超时最稳;其余异常(限流/5xx/响应格式不对/
    查询非法)一律 ``datasource_error``。
    """
    if isinstance(exc, TimeoutError):  # socket.timeout 在 3.10+ 就是 TimeoutError
        return REASON_TIMEOUT
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(hint.lower() in text for hint in TIMEOUT_HINTS):
        return REASON_TIMEOUT
    return REASON_DATASOURCE_ERROR


def search_stays_detailed(
    lat: float,
    lng: float,
    radius_m: int = DEFAULT_RADIUS_M,
    *,
    client: Optional[overpass.OverpassClient] = None,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """同 :func:`search_stays`,但额外报**为什么空**:返回 ``(rows, reason)``。

    ``reason`` 三档(TASK-6c):``None`` = 拿到结果;:data:`REASON_NO_DATA` = 检索成功但
    半径内真没有;:data:`REASON_DATASOURCE_ERROR` / :data:`REASON_TIMEOUT` = 检索失败
    (按 :func:`classify_failure` 分档)。仍然**绝不抛异常**。
    """
    origin = coordinate_pair(lat, lng)
    if origin is None:
        return [], REASON_NO_DATA
    origin_lat, origin_lng = origin
    http = client if client is not None else overpass.default_client()
    try:
        query = build_stay_query(origin_lat, origin_lng, radius_m)
        payload = http.execute(query, timeout=STAY_REQUEST_TIMEOUT_S)
        places = overpass.parse_places(
            payload, origin_lat, origin_lng, limit=None, require_name=False, with_id=True
        )
    except DataSourceError as exc:
        return [], classify_failure(exc)
    except Exception as exc:  # noqa: BLE001 - 检索是增强项:查询非法/响应异常都当"没搜到"
        return [], classify_failure(exc)

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for place in places:
        row = stay_row(place, origin_lat, origin_lng)
        identity = stay_identity(row)
        if identity is None or identity in seen:
            continue
        seen.add(identity)
        rows.append(row)
    return (rows, None) if rows else ([], REASON_NO_DATA)


# --------------------------------------------------------------------------- #
# 负缓存 + 半径阶梯(TASK-6c)
# --------------------------------------------------------------------------- #


def radius_ladder(radius_m: Optional[float]) -> tuple[int, ...]:
    """本次要查的半径序列:**未显式给半径** → 走阶梯(5→10→30 km);给了 → 只查那一档。

    非法半径(0/负数/非数字)→ 空序列,调用方据此直接返回空结果(不触网)。
    """
    if radius_m is None:
        return STAY_RADIUS_LADDER_M
    resolved = _as_float(radius_m)
    if resolved is None or resolved <= 0:
        return ()
    return (int(round(resolved)),)


def cache_age_seconds(row: Any, *, now: Optional[Any] = None) -> Optional[float]:
    """负缓存行的年龄(秒);没有 ``fetched_at`` → ``None``(视为不可用,不当命中)。"""
    fetched = getattr(row, "fetched_at", None)
    if fetched is None:
        return None
    if fetched.tzinfo is None:  # SQLite 读回的是 naive 时间,按 UTC 处理
        fetched = fetched.replace(tzinfo=timezone.utc)
    moment = now if now is not None else utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - fetched).total_seconds()


def remember_negative(
    session: Session,
    lat: float,
    lng: float,
    radius_m: float,
    *,
    reason: str,
    nearest_km: Optional[float] = None,
) -> Optional[StayQueryCache]:
    """写/刷新一条负缓存(按 ``(定点坐标, 半径, kind)`` upsert),返回该行;入参非法 → ``None``。

    坐标先按 :data:`~db.models.COORD_PRECISION` 定点再入库,复刻唯一键口径 —— 否则
    ``31.230400000000004`` 与 ``31.2304`` 会存成两行,缓存永远命不中。
    只 ``flush`` 不 ``commit``:提交时机交给编排函数(与 :func:`upsert_stays` 一致)。
    """
    origin = coordinate_pair(lat, lng)
    if origin is None:
        return None
    radius = _as_float(radius_m)
    if radius is None or radius <= 0:
        return None
    kind = stay_cache_kind(reason)
    origin_lat, origin_lng = origin
    resolved_radius = int(round(radius))
    row = session.scalar(
        select(StayQueryCache).where(
            StayQueryCache.lat == origin_lat,
            StayQueryCache.lng == origin_lng,
            StayQueryCache.radius_m == resolved_radius,
            StayQueryCache.kind == kind,
        )
    )
    if row is None:
        row = StayQueryCache(
            lat=origin_lat, lng=origin_lng, radius_m=resolved_radius, kind=kind
        )
        session.add(row)
    distance = _as_float(nearest_km)
    row.reason = stay_cache_reason(kind)[:STAY_REASON_LEN]
    row.nearest_km = None if distance is None else round(distance, NEAREST_KM_PRECISION)
    row.fetched_at = utcnow()
    session.flush()
    return row


def lookup_negative(
    session: Session,
    lat: float,
    lng: float,
    radius_m: float,
    *,
    ttl_s: float = NEG_CACHE_TTL_S,
    now: Optional[Any] = None,
) -> Optional[StayQueryCache]:
    """命中未过期的负缓存就返回该行(**最新的一行优先**),否则 ``None`` = 该重新查了。"""
    origin = coordinate_pair(lat, lng)
    if origin is None:
        return None
    radius = _as_float(radius_m)
    if radius is None or radius <= 0:
        return None
    rows = session.scalars(
        select(StayQueryCache)
        .where(
            StayQueryCache.lat == origin[0],
            StayQueryCache.lng == origin[1],
            StayQueryCache.radius_m == int(round(radius)),
        )
        .order_by(StayQueryCache.fetched_at.desc(), StayQueryCache.id.desc())
    ).all()
    for row in rows:
        age = cache_age_seconds(row, now=now)
        if age is not None and age <= ttl_s:
            return row
    return None


def negative_state(row: Optional[StayQueryCache]) -> dict[str, Any]:
    """负缓存行 → 可直接透传 API 的缓存态(``reason`` / ``nearest_km`` / ``kind`` / 时间)。"""
    if row is None:
        return {"reason": None, "nearest_km": None, "kind": None, "fetched_at": None}
    distance = _as_float(row.nearest_km)
    return {
        "reason": stay_cache_reason(row.kind),
        "nearest_km": None if distance is None else round(distance, NEAREST_KM_PRECISION),
        "kind": row.kind or STAY_CACHE_EMPTY,
        "fetched_at": iso_utc(row.fetched_at),
    }


def nearest_known_km(
    session: Session,
    lat: float,
    lng: float,
    *,
    radius_m: Optional[float] = None,
) -> Optional[float]:
    """库里已知的最近一家住宿有多远(km,1 位);范围外/库里没有 → ``None``。

    空结果时用它给前端"最近的在 X km 外"提示(BUG-3):**只读库、不触网**,
    范围默认取阶梯最大档(30 km)—— 再远的住宿对"今晚住哪儿"没有参考价值。
    """
    limit = _as_float(radius_m)
    if limit is None or limit <= 0:
        limit = float(max(STAY_RADIUS_LADDER_M))
    rows = select_stays(session, lat, lng, limit)
    if not rows:
        return None
    origin = coordinate_pair(lat, lng)
    if origin is None:
        return None
    raw = _raw_distance(origin[0], origin[1], rows[0].lat, rows[0].lng)
    return None if raw is None else round(raw, NEAREST_KM_PRECISION)


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
        return normalize_price_range(matched.group("value").strip(QUOTE_CHARS))
    return ""


def normalize_price_range(value: Optional[str]) -> str:
    """一段文本里的第一个数字区间 → 规范串 ``约¥A-B/晚``(单值 ``约¥A/晚``);抓不出 → ``""``。

    从 :func:`parse_price` 里抽出来给**批量**输出复用:批量行是
    ``1|价格: 约¥300-500/晚|简介: …``,按 ``|`` 切开之后每段仍走同一套归一,
    口径不会两处漂("约"字这个估算标注恒在)。
    """
    numbers = PRICE_RANGE_RE.search(str(value or ""))
    if numbers is None:
        return ""
    low = _clean_number(numbers.group("low"))
    high = _clean_number(numbers.group("high"))
    if not low:
        return ""
    if high and high != low:
        return f"约¥{low}-{high}/晚"[:PRICE_LEN]
    return f"约¥{low}/晚"[:PRICE_LEN]


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
# 批量估价 + 后台异步回填(TASK-6c:5 家/prompt、固定 qwen3.8-max、失败留 null)
# --------------------------------------------------------------------------- #

BATCH_SYSTEM_PROMPT = (
    "你是 Where2Go(周末去哪儿玩)的住宿信息助手,为行程落脚点**批量**写价格区间估算与一句话简介。"
    "用户一次给出若干家住宿,请**每家输出一行**,行数与序号必须与输入一一对应,"
    "每行严格写成 `序号|价格: 约¥A-B/晚|简介: <40字内一句话>`,"
    "不要标题、解释、空行或任何多余文字。"
    "价格只能依据名称、住宿类型、星级/房量等标签与所在城市做常识性估算,"
    "必须带“约”字,不得编造具体房型、电话、地址、促销或任何精确数字;"
    "简介只依据给出的名称/类型/标签,信息不足就写该类型的通用描述,"
    "不要提及 OSM、标签、数据源或模型。价格是**估算**,不是报价。"
)


def resolve_price_llm(environ: Optional[Mapping[str, str]] = None) -> LLMClient:
    """后台批量估价用的 LLM 客户端:**固定** qwen(token-plan 入口,``qwen3.8-max``)。

    口径照 :func:`services.intro.resolve_provider`(``WHERE2GO_LLM_*`` 可覆盖 base_url/model,
    key 只读环境变量),但**不退回别的供应商**(神朱 2026-09-28 定:不做双模型)——
    拿不到该 provider 的 key 就返回一个 ``enabled=False`` 的客户端,上层据此把价格留 null,
    既不烧 token 也不猜数字。
    """
    provider = find_provider(PRICE_PROVIDER_NAME)
    env = dict(os.environ if environ is None else environ)
    if provider is None:
        return LLMClient(environ={})
    api_key = ""
    key_env = ""
    for candidate in (ENV_API_KEY,) + tuple(provider.env_api_keys):
        value = str(env.get(candidate) or "").strip()
        if value:
            api_key, key_env = value, candidate
            break
    if not api_key:
        return LLMClient(environ={})
    return LLMClient(
        resolved=ResolvedLLM(
            provider=provider.name,
            label=provider.label,
            base_url=str(env.get(ENV_BASE_URL) or provider.base_url).strip().rstrip("/"),
            model=str(env.get(ENV_MODEL) or PRICE_MODEL or provider.model).strip(),
            api_key=api_key,
            key_env=key_env,
        )
    )


def stays_needing_price(stays: Sequence[Any]) -> list[Any]:
    """挑出**还能估价**的行:没有 ``price_estimate`` 且有名称。

    没有名称的行 LLM 只能瞎猜(:func:`estimate_price` 也直接降级),所以不排队、不占 token;
    已有价格的行**永不再估**(DB 即缓存,与 ``Place.intro`` 同口径)。
    """
    pending: list[Any] = []
    for stay in stays or ():
        if clean_text(_get(stay, "price_estimate"), limit=PRICE_LEN):
            continue
        if not str(_get(stay, "name") or "").strip():
            continue
        pending.append(stay)
    return pending


def price_batches(stays: Sequence[Any], *, batch_size: int = PRICE_BATCH_SIZE) -> list[list[Any]]:
    """按 :data:`PRICE_BATCH_SIZE`(5 家)切批;``batch_size`` 非法时退回默认。"""
    size = int(batch_size) if batch_size and int(batch_size) > 0 else PRICE_BATCH_SIZE
    rows = list(stays or ())
    return [rows[index:index + size] for index in range(0, len(rows), size)]


def batch_output_format_lines(count: int) -> tuple[str, ...]:
    """批量 prompt 的输出格式要求(逐行给出序号,模型照着填最稳)。"""
    sample = "\n".join(
        f"{index}|价格: 约¥A-B/晚|简介: <40字内一句话>" for index in range(1, max(1, count) + 1)
    )
    return (
        f"请严格按下面 {max(1, count)} 行输出,每行一家,序号与上面一一对应,不要多余文字:",
        sample,
    )


def build_batch_price_prompt(batch: Sequence[Any]) -> str:
    """一批住宿拼一个 prompt:逐家给名称/类型/位置/标签摘要,末尾附**严格逐行**的输出格式。"""
    rows = list(batch or ())
    lines: list[str] = [f"下面是 {len(rows)} 家住宿,请为每一家给出价格区间估算与一句话简介。", ""]
    for index, stay in enumerate(rows, start=1):
        name = str(_get(stay, "name") or "").strip() or "(无名)"
        lines.append(f"第{index}家")
        lines.append(f"名称:{name}")
        lines.append(f"类型:{normalize_kind(stay) or '住宿'}")
        where = _where_line(stay)
        if where:
            lines.append(where)
        facts = stay_facts(_get(stay, "tags") if isinstance(_get(stay, "tags"), Mapping) else None)
        if facts:
            lines.append(f"OSM 标签:{facts}")
        lines.append("")
    lines.extend(batch_output_format_lines(len(rows)))
    return "\n".join(lines)


def parse_batch_line(text: Optional[str]) -> tuple[str, str]:
    """批量输出的一行(去掉序号后)→ ``(price, intro)``;解析不出就是 ``("", "")``(留 null)。

    先按 ``|`` 切段,每段复用单条口径的 :func:`parse_price` / :func:`parse_intro_line`;
    首段额外允许"没有 ``价格:`` 前缀"的写法(``1|约¥300-500/晚|…``)—— 只有首段兜底,
    免得简介里的数字被当成房价。
    """
    raw = str(text or "").strip()
    if not raw:
        return "", ""
    parts = [part.strip() for part in BATCH_SEPARATOR_RE.split(raw) if part.strip()] or [raw]
    price = ""
    intro = ""
    for position, part in enumerate(parts):
        if not price:
            price = parse_price(part) or (
                normalize_price_range(part.strip(QUOTE_CHARS)) if position == 0 else ""
            )
        if not intro:
            intro = parse_intro_line(part)
    if not intro:  # 模型把两段挤在一起(``价格: … 简介: …``)时的兜底
        matched = INTRO_INLINE_RE.search(raw)
        if matched is not None:
            intro = clean_intro(matched.group("value"))
    return price, intro


def parse_batch_completion(completion: Optional[str], count: int) -> list[tuple[str, str]]:
    """一次批量输出 → 按**序号**归位的 ``count`` 条 ``(price, intro)``。

    缺行 / 序号越界 / 解析不出 → 该家留 ``("", "")``(价格 null,**不猜、不抛**);
    同一序号出现多行时后到的只补空位,不覆盖已解析出的值。
    """
    total = max(0, int(count))
    results: list[tuple[str, str]] = [("", "")] * total
    for line in str(completion or "").splitlines():
        matched = BATCH_INDEX_RE.match(line.strip())
        if matched is None:
            continue
        index = int(matched.group("index"))
        if not 1 <= index <= total:
            continue
        price, intro = parse_batch_line(matched.group("rest"))
        if not price and not intro:
            continue
        known_price, known_intro = results[index - 1]
        results[index - 1] = (price or known_price, intro or known_intro)
    return results


def estimate_batch(
    batch: Sequence[Any],
    *,
    client: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
    retries: int = PRICE_BATCH_RETRIES,
) -> list[tuple[str, str]]:
    """给一批(≤ :data:`PRICE_BATCH_SIZE` 家)估价:**一次 prompt** 出全部行。

    降级口径与 :func:`estimate_price` 一致:未配 key / 超时 / 限流 / 整批解析不出 →
    全留空,绝不抛异常;单批**重试不超过 :data:`PRICE_BATCH_RETRIES` 次**(1 次),
    重试只针对"调用失败或整批一个价格都没解析出来",部分解析成功就收下、缺的留 null。
    """
    rows = list(batch or ())
    if not rows:
        return []
    llm = client if client is not None else resolve_price_llm(environ)
    if not llm.enabled:
        return [("", "") for _ in rows]
    prompt = build_batch_price_prompt(rows)
    attempts = max(1, int(retries) + 1)
    for _ in range(attempts):
        try:
            completion = llm.chat(
                prompt,
                system=BATCH_SYSTEM_PROMPT,
                max_tokens=PRICE_BATCH_MAX_TOKENS,
                timeout=PRICE_BATCH_TIMEOUT_S,
            )
        except DataSourceError:
            continue
        except Exception:  # noqa: BLE001 - 估价是增强项,任何异常都不得冒到调用方
            continue
        parsed = parse_batch_completion(completion, len(rows))
        if any(price for price, _ in parsed):
            return parsed
    return [("", "") for _ in rows]


def fill_prices_batched(
    stays: Sequence[Any],
    *,
    client: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
    batch_size: int = PRICE_BATCH_SIZE,
) -> int:
    """按批给**缺价格**的行就地补 ``(price_estimate, intro)``,返回补上的条数(不 commit)。

    与 :func:`estimate_missing`(逐家一次调用)的区别只在**批量**:5 家一个 prompt,
    token 与限流额度都省到 1/5;写入口径完全一致(已有价格/简介不覆盖)。
    """
    pending = stays_needing_price(stays)
    if not pending:
        return 0
    filled = 0
    for batch in price_batches(pending, batch_size=batch_size):
        for stay, (price, intro) in zip(batch, estimate_batch(batch, client=client, environ=environ)):
            if not price:
                continue
            stay.price_estimate = price
            if intro and not str(_get(stay, "intro") or "").strip():
                stay.intro = intro
            filled += 1
    return filled


class InlineExecutor:
    """同步执行器:``submit`` 就在调用线程里跑完(测试断言与 CLI"等回填完再打印"用)。

    只实现 :class:`concurrent.futures.Executor` 用到的 ``submit``,返回值仍是
    :class:`~concurrent.futures.Future`,所以与真线程池可互换(依赖倒置,测试不必等线程)。
    """

    def submit(self, fn, *args: Any, **kwargs: Any) -> Future:
        future: Future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - 与线程池一致:异常进 Future,不冒出来
            future.set_exception(exc)
        return future


INLINE_EXECUTOR = InlineExecutor()

_background_executor: Optional[Executor] = None
_background_lock = threading.Lock()


def background_executor() -> Executor:
    """进程内共享的后台线程池(懒建;照 :func:`services.intro._run_batch` 的口径)。

    后台线程**不阻塞请求**:请求线程只 ``submit`` 一次就返回,估价在池子里慢慢跑;
    池子跑完的活儿写回同一个 SQLite 库(见 :func:`price_fill_job`,它自己开 Session)。
    """
    global _background_executor
    with _background_lock:
        if _background_executor is None:
            _background_executor = ThreadPoolExecutor(
                max_workers=PRICE_BACKGROUND_WORKERS, thread_name_prefix="stay-price"
            )
        return _background_executor


def set_background_executor(executor: Optional[Executor]) -> None:
    """替换/清空后台执行器(测试注入 :data:`INLINE_EXECUTOR` 或假执行器;传 ``None`` 复原)。"""
    global _background_executor
    with _background_lock:
        _background_executor = executor


def price_fill_job(
    engine: Any,
    stay_ids: Sequence[int],
    *,
    client: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
    batch_size: int = PRICE_BATCH_SIZE,
) -> dict[str, Any]:
    """后台线程体:按 id 重新取行 → 批量估价 → 写回 → commit,返回统计;**任何异常都吞掉**。

    自己开 Session:SQLAlchemy 的 Session 非线程安全,请求线程的 Session 绝不能跨线程用;
    而且调用方在 ``submit`` 之前已经 commit 过事实行(否则新连接看不见未提交数据)。
    """
    stats: dict[str, Any] = {"scanned": 0, "filled": 0, "batches": 0, "pending": 0, "provider": "未配置"}
    ids = [int(item) for item in (stay_ids or ()) if item is not None]
    if not ids or engine is None:
        return stats
    from db.base import session_factory  # 延迟导入:后台线程才需要,避免服务层 import 副作用

    session = session_factory(engine)()
    try:
        rows = list(session.scalars(select(Stay).where(Stay.id.in_(ids))).all())
        stats["scanned"] = len(rows)
        pending = stays_needing_price(rows)
        stats["pending"] = len(pending)
        if not pending:
            return stats
        llm = client if client is not None else resolve_price_llm(environ)
        # ``getattr``:注入的假客户端(单测)可以没有 label,不能因此把整个回填任务打死
        stats["provider"] = str(getattr(llm, "label", "") or "") if llm.enabled else "未配置"
        if not llm.enabled:
            return stats
        stats["batches"] = len(price_batches(pending, batch_size=batch_size))
        stats["filled"] = fill_prices_batched(
            pending, client=llm, environ=environ, batch_size=batch_size
        )
        if stats["filled"]:
            session.commit()
    except Exception:  # noqa: BLE001 - 后台回填失败不得影响已经返回的响应
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 - 回滚失败也不该把线程打死
            pass
    finally:
        session.close()
    return stats


def schedule_price_fill(
    session: Session,
    stays: Sequence[Any],
    *,
    executor: Optional[Executor] = None,
    llm: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
    batch_size: int = PRICE_BATCH_SIZE,
) -> bool:
    """把缺价格的行交给后台**批量**回填;返回是否真的排上了(``estimating`` 就用它)。

    排不上的三种情况都返回 ``False``(前端不显示"估价中"):没有待估行、
    没配 LLM key(排了也白排)、执行器拒收。
    """
    pending = stays_needing_price(stays)
    ids = [int(_get(stay, "id")) for stay in pending if _get(stay, "id") is not None]
    if not ids:
        return False
    if llm is None and not resolve_price_llm(environ).enabled:
        return False
    engine = session.get_bind()
    runner = executor if executor is not None else background_executor()
    try:
        runner.submit(
            price_fill_job, engine, ids, client=llm, environ=environ, batch_size=batch_size
        )
    except Exception:  # noqa: BLE001 - 排不上就算了,价格留 null,下次请求再试
        return False
    return True


def estimate_mode(
    requested: Optional[str],
    *,
    pending_count: int,
) -> str:
    """决定这次的估价口径:``sync``(就地逐家算)/ ``async``(后台批量)/ ``off``(不算)。

    * 显式 ``estimate=sync|async|off`` 照办(没有待估行时一律 ``off``);
    * ``auto``(默认):**≤ 一批(5 家)就地算完再返回** —— 首屏就有价格,交互延迟可接受;
      **超过一批转后台批量回填** —— 一次 30 km 检索可能几十家,逐家串行能把请求拖到超时(BUG-3);
    * ``auto`` 的同步档沿用 TASK-3a1 的**逐家**口径(:func:`estimate_missing`),既有单测与
      既有 API 行为零改动;只有转后台时才换成 5 家/prompt 的批量口径。
    """
    mode = str(requested or ESTIMATE_AUTO).strip().lower()
    if mode not in ESTIMATE_MODES:
        mode = ESTIMATE_AUTO
    if pending_count <= 0:
        return ESTIMATE_OFF
    if mode in (ESTIMATE_SYNC, ESTIMATE_ASYNC, ESTIMATE_OFF):
        return mode
    if pending_count <= SYNC_ESTIMATE_MAX_ROWS:
        return ESTIMATE_SYNC
    return ESTIMATE_ASYNC


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


@dataclass(frozen=True)
class StaySearchResult:
    """一次住宿检索的**完整结论**:列表 + 判别信息(TASK-6c)。

    ``items`` 的形状与 :func:`stay_to_dict` 一致;``reason`` / ``nearest_km`` /
    ``estimating`` 放在**外层**而不是塞进每一行 —— 空结果时根本没有行可挂,
    而前端三档文案(no_data / datasource_error / timeout)恰恰只在空结果时要紧(BUG-3)。

    * ``reason``:``None`` = 正常拿到结果;否则是三档之一(见 :data:`db.models.STAY_REASONS`)。
    * ``nearest_km``:最近一家的距离(km,1 位);空结果时是**库里已知**的最近一家。
    * ``estimating``:后台正在批量回填价格(``price_estimate`` 此刻可能还是 null)。
    * ``radius_m``:实际生效的半径(阶梯可能扩过档);``requested_radius_m``:调用方显式给的。
    * ``expanded``:是否扩过档;``from_cache``:本次结论是否来自缓存(DB 正缓存 / 负缓存)。
    """

    items: list[dict[str, Any]]
    reason: Optional[str] = None
    nearest_km: Optional[float] = None
    estimating: bool = False
    source: str = SOURCE_DB
    radius_m: Optional[int] = None
    requested_radius_m: Optional[int] = None
    expanded: bool = False
    from_cache: bool = False


class StayItems(list):
    """:func:`load_or_fetch_stays` 的返回值:**仍然是 list**(既有调用方与断言零改动),
    只是额外挂了本次检索的判别信息(``reason`` / ``nearest_km`` / ``estimating`` / ``source`` …)。

    API 侧用 ``getattr`` 取,拿到普通 ``list``(例如单测里替换成的假返回值)也能降级成
    "没有判别信息",不会 KeyError。
    """

    reason: Optional[str] = None
    nearest_km: Optional[float] = None
    estimating: bool = False
    source: str = SOURCE_DB
    radius_m: Optional[int] = None
    requested_radius_m: Optional[int] = None
    expanded: bool = False
    from_cache: bool = False

    @classmethod
    def from_result(cls, result: StaySearchResult) -> "StayItems":
        """把 :class:`StaySearchResult` 摊成"带属性的列表"。"""
        items = cls(list(result.items))
        items.reason = result.reason
        items.nearest_km = result.nearest_km
        items.estimating = bool(result.estimating)
        items.source = result.source
        items.radius_m = result.radius_m
        items.requested_radius_m = result.requested_radius_m
        items.expanded = bool(result.expanded)
        items.from_cache = bool(result.from_cache)
        return items


def _nearest_of(rows: Sequence[Any], origin_lat: float, origin_lng: float) -> Optional[float]:
    """已由近及远排好序的行 → 最近一家的距离(km,1 位);空 → ``None``。"""
    if not rows:
        return None
    raw = _raw_distance(origin_lat, origin_lng, _get(rows[0], "lat"), _get(rows[0], "lng"))
    return None if raw is None else round(raw, NEAREST_KM_PRECISION)


def _finish_rows(
    session: Session,
    rows: Sequence[Stay],
    origin: tuple[float, float],
    radius_m: int,
    *,
    source: str,
    llm: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
    executor: Optional[Executor] = None,
    estimate: Optional[str] = None,
    reason: Optional[str] = None,
    expanded: bool = False,
    from_cache: bool = False,
    requested_radius_m: Optional[int] = None,
) -> StaySearchResult:
    """把库里的行按 :func:`estimate_mode` 的口径估完价(或排到后台),再序列化返回。"""
    origin_lat, origin_lng = origin
    pending = stays_needing_price(rows)
    mode = estimate_mode(estimate, pending_count=len(pending))
    estimating = False
    if mode == ESTIMATE_SYNC:
        if estimate_missing(rows, client=llm, environ=environ):
            session.commit()
    elif mode == ESTIMATE_ASYNC:
        estimating = schedule_price_fill(
            session, pending, executor=executor, llm=llm, environ=environ
        )
    items = [
        stay_to_dict(row, origin_lat=origin_lat, origin_lng=origin_lng, source=source)
        for row in rows
    ]
    return StaySearchResult(
        items=items,
        reason=reason,
        nearest_km=_nearest_of(rows, origin_lat, origin_lng),
        estimating=estimating,
        source=source,
        radius_m=int(radius_m),
        requested_radius_m=requested_radius_m,
        expanded=expanded,
        from_cache=from_cache,
    )


def load_stays(
    session: Session,
    lat: float,
    lng: float,
    *,
    radius_m: Optional[int] = None,
    refresh: bool = False,
    client: Optional[overpass.OverpassClient] = None,
    llm: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
    executor: Optional[Executor] = None,
    estimate: Optional[str] = None,
) -> StaySearchResult:
    """住宿检索的**主编排**(TASK-6c 口径),返回带判别信息的 :class:`StaySearchResult`。

    每一档半径按这个顺序走(**绝不抛异常**):

    1. 正缓存:库里有 ≥ :data:`MIN_CACHED_ROWS` 行 → 直接读库返回(``source="db"``,零网络);
    2. 负缓存::data:`NEG_CACHE_TTL_S`(6h)内查过且是空/失败 → 直接回缓存态(零网络);
    3. 检索:成功有货 → upsert + commit(先落库,后台线程才看得见)→ 估价 → 返回;
       成功但空 → 写负缓存(``no_data``),**未显式给半径时**按阶梯扩到下一档;
       失败 → 写负缓存(``datasource_error`` / ``timeout``)并**停止扩档**(端点已挂,
       再打两遍只是白等),库里有货仍按 ``"db"`` 返回,不谎报来源。

    ``refresh=True`` 跳过 1、2 两层缓存强制重查;``estimate`` 见 :func:`estimate_mode`;
    ``executor`` 可注入(测试/CLI 用 :data:`INLINE_EXECUTOR` 把后台回填变成同步)。
    """
    ladder = radius_ladder(radius_m)
    requested = None if radius_m is None else (ladder[0] if ladder else None)
    origin = coordinate_pair(lat, lng)
    if origin is None or not ladder:
        return StaySearchResult(items=[], requested_radius_m=requested)
    origin_lat, origin_lng = origin

    for index, rung in enumerate(ladder):
        expanded = index > 0
        if not refresh:
            db_rows = select_stays(session, origin_lat, origin_lng, rung)
            if len(db_rows) >= MIN_CACHED_ROWS:
                return _finish_rows(
                    session, db_rows, origin, rung,
                    source=SOURCE_DB, llm=llm, environ=environ, executor=executor,
                    estimate=estimate, expanded=expanded, from_cache=True,
                    requested_radius_m=requested,
                )
            cached = lookup_negative(session, origin_lat, origin_lng, rung)
            if cached is not None:
                state = negative_state(cached)
                return StaySearchResult(
                    items=[],
                    reason=state["reason"],
                    nearest_km=state["nearest_km"],
                    source=SOURCE_DB,
                    radius_m=rung,
                    requested_radius_m=requested,
                    expanded=expanded,
                    from_cache=True,
                )

        rows, reason = search_stays_detailed(origin_lat, origin_lng, rung, client=client)
        if reason in (REASON_DATASOURCE_ERROR, REASON_TIMEOUT):
            db_rows = select_stays(session, origin_lat, origin_lng, rung)
            nearest = (
                _nearest_of(db_rows, origin_lat, origin_lng)
                if db_rows
                else nearest_known_km(session, origin_lat, origin_lng)
            )
            if not db_rows:
                remember_negative(
                    session, origin_lat, origin_lng, rung, reason=reason, nearest_km=nearest
                )
                session.commit()
                return StaySearchResult(
                    items=[], reason=reason, nearest_km=nearest, source=SOURCE_DB,
                    radius_m=rung, requested_radius_m=requested, expanded=expanded,
                )
            return _finish_rows(
                session, db_rows, origin, rung,
                source=SOURCE_DB, llm=llm, environ=environ, executor=executor,
                estimate=estimate, reason=reason, expanded=expanded,
                requested_radius_m=requested,
            )

        if rows:
            upsert_stays(session, rows)
            db_rows = select_stays(session, origin_lat, origin_lng, rung)
            # 先提交事实行:后台回填线程用的是**另一个连接**,看不见未提交的数据
            session.commit()
            return _finish_rows(
                session, db_rows, origin, rung,
                source=SOURCE_FETCH, llm=llm, environ=environ, executor=executor,
                estimate=estimate, expanded=expanded, requested_radius_m=requested,
            )

        # 检索成功但半径内真没有:refresh 时库里的老货照旧返回(不因为一次空检索就清空展示)
        db_rows = select_stays(session, origin_lat, origin_lng, rung) if refresh else []
        if db_rows:
            return _finish_rows(
                session, db_rows, origin, rung,
                source=SOURCE_DB, llm=llm, environ=environ, executor=executor,
                estimate=estimate, expanded=expanded, requested_radius_m=requested,
            )
        remember_negative(
            session, origin_lat, origin_lng, rung,
            reason=REASON_NO_DATA,
            nearest_km=nearest_known_km(session, origin_lat, origin_lng),
        )
        session.commit()

    nearest = nearest_known_km(session, origin_lat, origin_lng)
    return StaySearchResult(
        items=[],
        reason=REASON_NO_DATA,
        nearest_km=nearest,
        source=SOURCE_DB,
        radius_m=ladder[-1],
        requested_radius_m=requested,
        expanded=len(ladder) > 1,
    )


def load_or_fetch_stays(
    session: Session,
    lat: float,
    lng: float,
    *,
    radius_m: Optional[int] = None,
    refresh: bool = False,
    client: Optional[overpass.OverpassClient] = None,
    llm: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
    executor: Optional[Executor] = None,
    estimate: Optional[str] = None,
) -> StayItems:
    """读库优先的住宿列表(**DB 即缓存**),每项带 ``distance_km``(haversine,2 位)。

    既有入口,签名与返回类型向后兼容:返回的还是"行的列表"(空结果就是 ``[]``),
    只是换成了 :class:`StayItems` —— 额外挂着 ``reason`` / ``nearest_km`` / ``estimating``
    供 API 透传。要拿完整结论(:class:`StaySearchResult`)就用 :func:`load_stays`。

    ``radius_m=None``(调用方没给半径)走 :data:`STAY_RADIUS_LADDER_M` 阶梯;
    给了就只查那一档。起点非法或两边都拿不到数据 → 空列表(降级,不抛)。
    """
    return StayItems.from_result(
        load_stays(
            session,
            lat,
            lng,
            radius_m=radius_m,
            refresh=refresh,
            client=client,
            llm=llm,
            environ=environ,
            executor=executor,
            estimate=estimate,
        )
    )


def main(argv: Optional[list[str]] = None) -> int:
    """CLI:抓一个坐标半径内的住宿并入库估价。``python -m services.stays 31.2304 121.4737``"""
    parser = argparse.ArgumentParser(description="检索/入库周边住宿(带 LLM 价格估算与简介)")
    parser.add_argument("lat", type=float, help="起点纬度")
    parser.add_argument("lng", type=float, help="起点经度")
    parser.add_argument("--radius", type=int, default=DEFAULT_RADIUS_M, help=f"半径(米,默认 {DEFAULT_RADIUS_M})")
    parser.add_argument(
        "--ladder", action="store_true",
        help="不给死半径,按 5→10→30 km 阶梯自动扩(只在空结果时扩,扩到即停)",
    )
    parser.add_argument("--refresh", action="store_true", help="忽略库缓存,强制重新检索")
    parser.add_argument(
        "--estimate", default=ESTIMATE_AUTO, choices=list(ESTIMATE_MODES),
        help="估价口径:auto(默认,>5 家转后台批量)/ sync / async / off",
    )
    parser.add_argument(
        "--no-wait", action="store_true",
        help="后台批量估价不等待(CLI 默认用同步执行器,回填完再打印)",
    )
    parser.add_argument("--limit", type=int, default=None, help="最多显示多少条(默认:全部)")
    parser.add_argument("--db", default=None, help="数据库 URL(默认 WHERE2GO_DB_URL 或 backend/data/where2go.db)")
    args = parser.parse_args(argv)

    from db import init_db, make_engine, open_session

    engine = make_engine(args.db)
    init_db(engine)
    with open_session(engine) as session:
        result = load_stays(
            session,
            args.lat,
            args.lng,
            radius_m=None if args.ladder else args.radius,
            refresh=args.refresh,
            estimate=args.estimate,
            executor=None if args.no_wait else INLINE_EXECUTOR,
        )
    stays = result.items
    shown = stays if args.limit is None else stays[: max(0, int(args.limit))]
    for item in shown:
        distance = item["distance_km"]
        price = item["price_estimate"] or "未估价"
        print(
            f"[{item['kind'] or '住宿'}] {item['name'] or '(无名)'} · "
            f"{'-' if distance is None else f'{distance:.1f} km'} · {price}(估算) · {item['source']}"
        )
    tail = f"[完成] {len(shown)} / {len(stays)} 条 · 半径 {result.radius_m or args.radius} m"
    if result.reason:
        tail += f" · reason={result.reason}"
    if result.nearest_km is not None:
        tail += f" · 最近一家 {result.nearest_km} km"
    if result.estimating:
        tail += " · 估价后台回填中"
    print(tail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
