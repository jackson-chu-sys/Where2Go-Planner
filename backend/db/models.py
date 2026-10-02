"""SQLAlchemy 2.0 数据模型:目的地(Place)+ 抓取水位(SegmentFetch)
+ 收藏(Collection / CollectionCat)+ 住宿(Stay)+ 行程方案(TripPlan)。

阶段 1a(TASK-1a)落这两张表,存储用 SQLite(见 02-项目计划与架构.md):

* :class:`Place` —— 一个目的地。字段按 docs/STAGE1-PLAN.md 第 4 节的模型定义;
  其中 ``intro``(LLM 一句话简介)与 ``category`` 的**精确四分类**由 TASK-1b
  负责,本阶段先留字段 + 简化归类(见 services.categories)。
* :class:`SegmentFetch` —— (城市, band) 的抓取水位。存在这一行即代表该段已入库,
  二次查询**直接读库、不再触网**(见 services.place_loader.load_segment)。
* :class:`Collection` / :class:`CollectionCat` —— 路线/目的地收藏与收藏分组(TASK-2c,
  为 M4「统一收藏面板」铺路)。收藏存的是**快照摘要**(当时的方式/时长/费用/里程),
  不随价格系数与路况变化;唯一键 ``(kind, ref_key, mode)`` 让重复收藏**幂等**
  (upsert 刷新快照并返回原行,不报错、不产生重复条目)。
* :class:`Stay` —— 一处住宿(TASK-3a1,阶段3a「住哪儿」)。与 ``Place`` 分开存:住宿是
  行程的**落脚点**、不进需求四分类,展示要的是价格区间估算而不是分类标签;唯一键
  ``(osm_type, osm_id)``,同一家酒店从不同起点搜到只存一行,``price_estimate`` / ``intro``
  由 LLM 生成后**不再被重抓覆盖**(见 services.stays)。
* :class:`StayQueryCache` —— 住宿检索的**负缓存**(TASK-6c,BUG-3/5):记"这个坐标这个半径
  查过了,结果是空/失败",6 小时内同坐标同半径直接回缓存态,不再重复打高德;
  空结果分 ``no_data`` / ``datasource_error`` / ``timeout`` 三档 reason 透传给前端文案。
* :class:`OriginCache` —— 城市名 → 起点坐标的**地理编码持久缓存**(TASK-7a):
  ``/api/geocode`` 每次实调 Photon(德国)实测 2.7~3.4s,同一城市重复搜索重复付费;
  城市中心坐标基本不变,所以落一行 ``city → name/lat/lng/geocoder``,
  ``WHERE2GO_ORIGIN_CACHE_TTL_S``(缺省 7 天)内直接回缓存、**零网络**。
* :class:`PlaceMedia` —— 一个 POI 的**图片缓存**(TASK-8a1,详情弹窗用):高德 POI 图为主 +
  维基/Commons 兜底,``place_id`` 唯一 → 重复写是 upsert;命中缓存 7 天、空结果/失败只缓存
  6 小时(**负缓存**,别把一次空结果永久钉死),过期判定在 :mod:`services.place_media`。
* :class:`TripPlan` —— 一份行程方案(TASK-5a,M4):按**名字**唯一(同名提交=刷新),
  把已收藏的目的地 / 路线 / 住宿(``collections.id`` 引用,**不建外键**)组合起来;
  报价只读收藏快照的"当时口径",不重新调 ``/api/routes``(见 services.trips)。

去重口径(docs/STAGE1-PLAN.md 第 3 节):同一 OSM 实体在同一个城市库里只存一行,
唯一键 ``(osm_type, osm_id, origin_city)``。环形分段互斥,所以 band 不进唯一键;
重新抓取时按该键做 upsert(见 db.repository.upsert_places)。

来源标注(TASK-1c 种子数据):**不新增列**,复用 ``Place.tags`` 里的 ``source`` 键
(种子数据写 ``种子``,OSM 抓取不写这个键),存量库无需迁移;:func:`place_source`
再派生出统一的来源字符串给 API/前端用(见 db.repository.place_to_dict)。
"""

from __future__ import annotations

import zlib
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

OSM_ELEMENT_TYPES: tuple[str, ...] = ("node", "way", "relation")
FALLBACK_OSM_TYPE = "point"
UNCATEGORIZED = "其他"
# 高德切源(TASK-9b):POI 身份写在既有 ``osm_type``/``osm_id`` 两列里 —— ``osm_type`` 固定
# ``"amap"``,``osm_id`` 是**高德 POI id 的 crc32**(列是 Integer,而高德 id 是 ``B023B17WWK``
# 这样的字符串,见 :func:`amap_osm_id`);原始 id 存 ``tags["amap_id"]``,表结构零改动。
AMAP_OSM_TYPE = "amap"
NAME_LEN = 255
CITY_LEN = 120
COORD_PRECISION = 7

# 来源标注(TASK-1c):OSM 国内滑雪/运动覆盖差,缺口由人工种子数据垫底(STAGE1-PLAN 第 3 节)
SOURCE_TAG = "source"
SEED_SOURCE = "种子"
OSM_SOURCE = "OSM"
# TASK-9b 起抓取行的 ``tags["source"]`` 写「高德」(:func:`place_source` 据此派生来源标注);
# 存量行的「种子」/「OSM」标注与优先级都不受影响。
AMAP_SOURCE = "高德"


def amap_osm_id(poi_id: Any) -> int:
    """高德 POI id(字符串)→ ``Place.osm_id`` / ``Stay.osm_id`` 的**无符号 32 位整数身份**。

    两张表的 ``osm_id`` 都是 ``Integer`` 列,而高德 id 是 ``B023B17WWK`` 这样的字符串
    → 用 :func:`zlib.crc32` 哈希成确定性整数(``& 0xFFFFFFFF`` 保证非负、可重入):
    同一个 POI 每次抓取都算出同一个 id,唯一键 ``(osm_type, osm_id, origin_city)`` 的
    幂等语义因此原样保留,**表结构零改动**。原文另存 ``tags["amap_id"]``
    (前端身份脚注与收藏引用都靠它自洽)。空 id 抛 :class:`ValueError`(调用方应跳过该行)。
    """
    text = str(poi_id if poi_id is not None else "").strip()
    if not text:
        raise ValueError("高德 POI id 不能为空(无法派生入库身份)")
    return zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF


class Base(DeclarativeBase):
    """所有表的声明式基类(SQLAlchemy 2.0 风格)。"""


def utcnow() -> datetime:
    """带时区的当前 UTC 时间(列默认值统一走这里,便于测试断言)。"""
    return datetime.now(timezone.utc)


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    """把时间格式化成 ISO8601;SQLite 读回的 naive 时间按 UTC 处理。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat(timespec="seconds")


def place_source(tags: Optional[Mapping[str, Any]]) -> str:
    """从 ``tags`` 派生来源标注:``种子`` / ``高德`` / ``OSM``。

    OSM 抓取来的行没有 ``source`` 键,或写的是 ``survey`` 之类的原始 tag 值,
    一律按 :data:`OSM_SOURCE` 处理 —— 存量库不改一行数据也能正确标注。
    TASK-9b 起新抓的行写 ``tags["source"]="高德"``(:data:`AMAP_SOURCE`)。
    """
    value = str(dict(tags or {}).get(SOURCE_TAG) or "").strip()
    if value == SEED_SOURCE:
        return SEED_SOURCE
    if value == AMAP_SOURCE:
        return AMAP_SOURCE
    return OSM_SOURCE


def is_seed(tags: Optional[Mapping[str, Any]]) -> bool:
    """该行是否是人工种子数据(``tags["source"] == "种子"``)。"""
    return place_source(tags) == SEED_SOURCE


class Place(Base):
    """一个目的地:高德 POI 抓取(TASK-9b 起;存量行可能是 OSM),或人工种子数据
    (``tags["source"]="种子"``)。"""

    __tablename__ = "places"
    __table_args__ = (
        UniqueConstraint("osm_type", "osm_id", "origin_city", name="uq_place_osm_city"),
        Index("ix_place_segment", "origin_city", "band", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    osm_type: Mapped[str] = mapped_column(String(16), nullable=False)
    osm_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, default="")
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default=UNCATEGORIZED, index=True
    )
    intro: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    origin_city: Mapped[str] = mapped_column(String(CITY_LEN), nullable=False, index=True)
    band: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return f"<Place {self.osm_type}/{self.osm_id} {self.name!r} {self.category} {self.band}>"


class SegmentFetch(Base):
    """(城市, band) 的抓取水位:有记录 = 该段已入库,后续查询只读库。"""

    __tablename__ = "segment_fetch"
    __table_args__ = (UniqueConstraint("origin_city", "band", name="uq_segment_city_band"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    origin_city: Mapped[str] = mapped_column(String(CITY_LEN), nullable=False, index=True)
    band: Mapped[str] = mapped_column(String(16), nullable=False)
    origin_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    origin_lat: Mapped[float] = mapped_column(Float, nullable=False)
    origin_lng: Mapped[float] = mapped_column(Float, nullable=False)
    place_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="amap")
    # 渐进抓取(TASK-6b):该 (城市, band) 已经完成过几轮抓取(TASK-9b 起是高德 v3)。
    # 冷启动只抓一小轮(配额缩到 30),前端"加载更多"每越界一次再抓一轮(30×(轮数+1)),
    # 下一轮的目标总量由这个计数推出来,所以它必须落库、且旧库要能补列(见 db.base.init_db)。
    fetch_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return (
            f"<SegmentFetch {self.origin_city} {self.band} {self.place_count} 条 "
            f"{self.source} 第 {self.fetch_rounds} 轮>"
        )


# --------------------------------------------------------------------------- #
# 路线收藏(TASK-2c,阶段2c):Collection / CollectionCat
# --------------------------------------------------------------------------- #

KIND_ROUTE = "route"
KIND_PLACE = "place"
COLLECTION_KINDS: tuple[str, ...] = (KIND_ROUTE, KIND_PLACE)
# place 收藏没有"出行方式"。这里用**空串**而不是 NULL:SQLite 唯一键里的 NULL 互不相等,
# 同一目的地收藏两次会得到两行,幂等就失效了。
NO_MODE = ""
CAT_MANUAL = "manual"
CAT_AUTO = "auto"
COLLECTION_CAT_SOURCES: tuple[str, ...] = (CAT_MANUAL, CAT_AUTO)
# 收藏引用可以指向种子/无名 POI 的指纹身份(point/-1234,见 services.place_loader.place_identity)
# TASK-9b:高德行的身份是 ``amap/<crc32>``,前端把 /api/places 的 osm_type 原样带回收藏,
# 所以白名单必须收 ``amap`` —— ref_key 口径(``place:<type>/<id>``)与唯一键都不变。
OSM_REF_TYPES: tuple[str, ...] = OSM_ELEMENT_TYPES + (FALLBACK_OSM_TYPE, AMAP_OSM_TYPE)

REF_KEY_LEN = 200
CAT_NAME_LEN = 60
POINT_NAME_LEN = 300
LAT_LIMIT = 90.0
LNG_LIMIT = 180.0
LABEL_PRECISION = 4  # 没有地名时,用坐标当展示名的精度(与 services.routes 同口径)
# 快照摘要的规范键:收藏列表就按这几项显示(方式/时长/费用/里程),恒在
SUMMARY_MODE_KEY = "mode"
SUMMARY_NUMBER_KEYS: tuple[str, ...] = ("duration_min", "cost_cny", "distance_km")
COLLECTION_ORIGIN_LABEL = "我的位置"
COLLECTION_TARGET_LABEL = "目的地"


def clean_text(value: Any, *, limit: Optional[int] = None) -> Optional[str]:
    """可选文本归一:只认真正的 ``str``,去首尾空白;空/非字符串 → ``None``。

    ``limit`` 用来在入库前按列宽截断(与 :func:`db.repository` 的写入口径一致),
    免得前端多带几个字就撞 ``String(n)`` 报错。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit] if limit else text


def collection_kind(value: Any) -> str:
    """收藏类型归一(``route`` / ``place``);非法抛 :class:`ValueError`(API 层转 400)。"""
    kind = str(value or "").strip().lower()
    if kind not in COLLECTION_KINDS:
        raise ValueError(f"未知收藏类型:{value!r}(可选:{'、'.join(COLLECTION_KINDS)})")
    return kind


def optional_coordinate(name: str, value: Any, *, limit: float) -> Optional[float]:
    """可选经纬度归一:没给 → ``None``;非数字/NaN/越界 → :class:`ValueError`(中文说明)。

    与 :func:`services.routes.require_coordinates` 的口径一致,只是这里坐标**可选**
    (收藏允许只有 OSM 身份、没有坐标),校验文案沿用同一套,前端提示不用分两处。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"参数 {name} 必须为数字,收到:{value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"参数 {name} 必须为数字,收到:{value!r}") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"参数 {name} 不是有效数字:{value!r}")
    if not -limit <= number <= limit:
        raise ValueError(f"参数 {name} 超出 [-{limit:g}, {limit:g}] 范围:{value!r}")
    return number


def osm_key(osm_type: Any, osm_id: Any) -> Optional[str]:
    """OSM 身份 → ``node/123``(收藏引用串用);两个都没给返回 ``None``,只给一个抛错。

    ``point``(见 :data:`FALLBACK_OSM_TYPE`)是种子/无名 POI 的**指纹身份**,同样接受 ——
    前端把 ``/api/places`` 回来的 ``osm_type``/``osm_id`` 原样带上即可收藏,不必判类型。
    """
    text = str(osm_type or "").strip().lower()
    raw_id = osm_id.strip() if isinstance(osm_id, str) else osm_id
    if not text and raw_id in (None, ""):
        return None
    if not text or raw_id in (None, ""):
        raise ValueError("OSM 身份要成对给:osm_type 与 osm_id")
    if text not in OSM_REF_TYPES:
        raise ValueError(f"未知 osm_type:{osm_type!r}(可选:{'、'.join(OSM_REF_TYPES)})")
    if isinstance(raw_id, bool):
        raise ValueError(f"osm_id 必须是整数,收到:{osm_id!r}")
    try:
        number = int(str(raw_id))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"osm_id 必须是整数,收到:{osm_id!r}") from exc
    if number == 0:
        raise ValueError(f"osm_id 不能为 0:{osm_id!r}")
    return f"{text}/{number}"


def point_key(lat: Any, lng: Any) -> Optional[str]:
    """坐标 → 定点串 ``31.2304000,121.4737000``(收藏引用串用);缺一返回 ``None``。

    先按 :data:`COORD_PRECISION` 定点再拼串:同一个点两次收藏必须得到**同一把钥匙**,
    不能让浮点尾巴(``31.230400000000004``)把幂等打掉。
    """
    resolved_lat = optional_coordinate("lat", lat, limit=LAT_LIMIT)
    resolved_lng = optional_coordinate("lng", lng, limit=LNG_LIMIT)
    if resolved_lat is None or resolved_lng is None:
        return None
    return f"{resolved_lat:.{COORD_PRECISION}f},{resolved_lng:.{COORD_PRECISION}f}"


def point_label(lat: Any, lng: Any) -> Optional[str]:
    """坐标 → 展示用短标签(``31.2304,121.4737``);没有地名时用它当收藏标题。"""
    resolved_lat = optional_coordinate("lat", lat, limit=LAT_LIMIT)
    resolved_lng = optional_coordinate("lng", lng, limit=LNG_LIMIT)
    if resolved_lat is None or resolved_lng is None:
        return None
    return f"{resolved_lat:.{LABEL_PRECISION}f},{resolved_lng:.{LABEL_PRECISION}f}"


def collection_ref_key(
    *,
    kind: Any,
    osm_type: Any = None,
    osm_id: Any = None,
    to_lat: Any = None,
    to_lng: Any = None,
    from_lat: Any = None,
    from_lng: Any = None,
) -> str:
    """算收藏的**引用串**(进唯一键):``place:{目的地}`` / ``route:{起点}->{目的地}``。

    目的地优先用 OSM 身份(``node/123``,重抓/改名都不飘),没有就退化成坐标串;
    起点没有 OSM 身份(通常是城市中心或"我的位置"),一律用坐标串。
    ``kind`` 也进串里,所以同一个点"收藏目的地"与"收藏到它的路线"互不冲突。
    引用不完整抛 :class:`ValueError`(中文说明,API 层转 400)。
    """
    normalized = collection_kind(kind)
    target = osm_key(osm_type, osm_id) or point_key(to_lat, to_lng)
    if not target:
        raise ValueError("收藏缺少目的地引用:给 osm_type + osm_id,或 to_lat + to_lng")
    if normalized == KIND_PLACE:
        return f"{KIND_PLACE}:{target}"
    origin = point_key(from_lat, from_lng)
    if not origin:
        raise ValueError("收藏路线缺少起点坐标:from_lat 与 from_lng 都要给")
    return f"{KIND_ROUTE}:{origin}->{target}"


def default_collection_name(
    *,
    kind: Any,
    mode_label: Any = None,
    from_name: Any = None,
    to_name: Any = None,
    from_lat: Any = None,
    from_lng: Any = None,
    to_lat: Any = None,
    to_lng: Any = None,
) -> str:
    """没给名字时的默认标题:``起点 → 目的地 · 方式``(``place`` 收藏只有目的地)。

    地名缺失就退化成坐标短标签(:func:`point_label`),再缺就用
    :data:`COLLECTION_ORIGIN_LABEL` / :data:`COLLECTION_TARGET_LABEL` 兜底,
    保证收藏列表里每一行都有可读文案(与前端路线面板的起终点口径一致)。
    """
    normalized = collection_kind(kind)
    label = clean_text(mode_label)
    destination = (
        clean_text(to_name) or point_label(to_lat, to_lng) or COLLECTION_TARGET_LABEL
    )
    if normalized == KIND_PLACE:
        title = f"{destination} · {label}" if label else destination
    else:
        origin = (
            clean_text(from_name) or point_label(from_lat, from_lng) or COLLECTION_ORIGIN_LABEL
        )
        title = f"{origin} → {destination}" + (f" · {label}" if label else "")
    return title[:NAME_LEN]


class CollectionCat(Base):
    """收藏分组(M4「统一收藏面板」用):名字唯一的分类,如"周末去"/"雪季计划"。

    分组是**可选**的 —— 收藏不挂分组照样能用(``Collection.cat_id`` 可空);
    删除分组只把旗下收藏**摘下来**(``cat_id`` 置空),不连带删收藏,
    避免用户整理标签时误删路线。``source`` 区分人工建的与程序自动建的
    (:data:`CAT_MANUAL` / :data:`CAT_AUTO`),自动分组可被脚本安全清理。
    """

    __tablename__ = "collection_cats"
    __table_args__ = (UniqueConstraint("name", name="uq_collection_cat_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(CAT_NAME_LEN), nullable=False, index=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CAT_MANUAL, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return f"<CollectionCat {self.id} {self.name!r} {self.source}>"


class Collection(Base):
    """一条收藏:路线(``kind="route"``)或目的地(``kind="place"``)+ 当时的快照摘要。

    收藏是**快照**不是外键:``summary`` 存下收藏那一刻的 ``mode``/``duration_min``/
    ``cost_cny``/``distance_km``(以及调用方想留的其他键,如 ``kind=real|estimate``、
    ``degraded``),之后价格系数变了、路线数据源降级了,收藏列表仍显示用户当时看到的数字
    (M4 对比总账要的正是"当时口径")。要看最新数字请重新调 ``/api/routes``。

    幂等:唯一键 ``(kind, ref_key, mode)``。同一对起终点、同一方式的路线只有一行,
    重复收藏走 upsert 刷新快照并返回原行(见 :func:`db.repository.upsert_collection`),
    既不报错也不产生重复条目;``mode`` 对 ``place`` 收藏恒为 :data:`NO_MODE`。
    """

    __tablename__ = "collections"
    __table_args__ = (
        UniqueConstraint("kind", "ref_key", "mode", name="uq_collection_kind_ref_mode"),
        Index("ix_collection_cat_created", "cat_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    ref_key: Mapped[str] = mapped_column(String(REF_KEY_LEN), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default=NO_MODE, index=True)
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, default="")
    osm_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    osm_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    from_lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    from_lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    from_name: Mapped[str] = mapped_column(
        String(POINT_NAME_LEN), nullable=False, default=""
    )
    to_lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    to_lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    to_name: Mapped[str] = mapped_column(String(POINT_NAME_LEN), nullable=False, default="")
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    cat_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("collection_cats.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return f"<Collection {self.id} {self.kind}/{self.mode or '-'} {self.name!r}>"


# --------------------------------------------------------------------------- #
# 住宿(TASK-3a1,阶段3a):Stay
# --------------------------------------------------------------------------- #

KIND_LEN = 32
PRICE_LEN = 64
CURRENCY_LEN = 8
# 价格估算串(price_estimate)恒为人民币口径,串里自带"约"字标注是估算而非报价
DEFAULT_CURRENCY = "CNY"
# 价格**出处**(TASK-6g):``rule`` = 品牌/星级/类型规则表算出来的(0 token),
# ``llm`` = 批量 LLM 估的。两者都是估算(price_is_estimate 恒 True),区分出处只为
# 前端能分文案与排查口径漂移;老行没有这个信息 → NULL(不猜)。
PRICE_KIND_LEN = 16
PRICE_KIND_RULE = "rule"
PRICE_KIND_LLM = "llm"
PRICE_KINDS: tuple[str, ...] = (PRICE_KIND_RULE, PRICE_KIND_LLM)


class Stay(Base):
    """一处住宿(酒店/民宿/青旅/公寓/小屋):高德 ``types=100000`` 抓取 + LLM 估价与简介。

    为什么不复用 :class:`Place`:住宿是行程编排的**落脚点**(M5「住哪儿」),不进需求
    四分类,展示字段也不同(要价格区间、要"约"字口径的估算标注),所以单独一张表 ——
    没有 ``origin_city``/``band``/``category``,唯一键只有 ``(osm_type, osm_id)``:
    同一家酒店无论从哪个起点搜到都只存一行,``distance_km`` 记的是**最近一次检索**时
    离起点的距离(可空,纯展示用,不作为身份的一部分)。

    缓存口径与 ``Place.intro`` 一致:``price_estimate``(形如 ``约¥300-500/晚``)与
    ``intro`` 由 LLM 生成后落库,**重新抓取不覆盖**(见 services.stays.upsert_stays);
    ``currency`` 默认 :data:`DEFAULT_CURRENCY`,与价格串里的 ``¥`` 对应。

    TASK-6g 起价格多了一个出处标记 ``price_kind``(:data:`PRICE_KINDS`):命中品牌/星级/
    类型规则表的行是 ``rule``(**永久缓存**,与 LLM 产物同口径,重抓不覆盖),走批量 LLM 的
    行是 ``llm``,估不出来的是 NULL。
    """

    __tablename__ = "stays"
    __table_args__ = (
        UniqueConstraint("osm_type", "osm_id", name="uq_stay_osm"),
        Index("ix_stay_location", "lat", "lng"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    osm_type: Mapped[str] = mapped_column(String(16), nullable=False)
    osm_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(KIND_LEN), nullable=False, default="", index=True)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    tags: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    distance_km: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_estimate: Mapped[Optional[str]] = mapped_column(String(PRICE_LEN), nullable=True)
    price_kind: Mapped[Optional[str]] = mapped_column(String(PRICE_KIND_LEN), nullable=True)
    currency: Mapped[str] = mapped_column(
        String(CURRENCY_LEN), nullable=False, default=DEFAULT_CURRENCY
    )
    intro: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return (
            f"<Stay {self.osm_type}/{self.osm_id} {self.name!r} {self.kind} "
            f"{self.price_estimate or '未估价'}>"
        )


# --------------------------------------------------------------------------- #
# 住宿检索的负缓存(TASK-6c,BUG-3/5):StayQueryCache
# --------------------------------------------------------------------------- #

STAY_REASON_LEN = 32
# 空结果的三档 reason(API 原样透传,前端据此分文案:真的没有 / 可重试 / 稍后再试)
REASON_NO_DATA = "no_data"
REASON_DATASOURCE_ERROR = "datasource_error"
REASON_TIMEOUT = "timeout"
STAY_REASONS: tuple[str, ...] = (REASON_NO_DATA, REASON_DATASOURCE_ERROR, REASON_TIMEOUT)
# 负缓存行的 kind:空结果记 ``empty_ok``(对应 reason=no_data),失败按档记;kind 进唯一键,
# 所以同一个 (坐标, 半径) 允许"上次超时、这次真的没有"两行并存,取**最新**的一行当缓存态。
STAY_CACHE_EMPTY = "empty_ok"
STAY_CACHE_KINDS: tuple[str, ...] = (STAY_CACHE_EMPTY, REASON_DATASOURCE_ERROR, REASON_TIMEOUT)


def stay_cache_kind(reason: Any) -> str:
    """三档 reason → 负缓存 kind;``no_data``/空值都归 :data:`STAY_CACHE_EMPTY`,未知值按失败收。"""
    text = str(reason or "").strip().lower()
    if not text or text == REASON_NO_DATA:
        return STAY_CACHE_EMPTY
    if text in STAY_CACHE_KINDS:
        return text
    return REASON_DATASOURCE_ERROR


def stay_cache_reason(kind: Any) -> str:
    """负缓存 kind → 三档 reason(:data:`STAY_CACHE_EMPTY` 还原成 ``no_data``)。"""
    text = str(kind or "").strip().lower()
    if not text or text == STAY_CACHE_EMPTY:
        return REASON_NO_DATA
    if text in STAY_REASONS:
        return text
    return REASON_DATASOURCE_ERROR


class StayQueryCache(Base):
    """一次住宿检索的**空结果/失败**记录(负缓存):有这行且未过期 = 不必再触网。

    为什么要有负缓存:住宿在郊区/小城镇经常真的搜不到,而"搜不到"这条路每次都要等
    半径阶梯(5/10/30km)一轮轮打完(实测十几秒),用户连点两下就是两次白等(BUG-3)。
    于是把"这个坐标 + 这个半径查过了,结论是空/报错/超时"落一行,
    :data:`services.stays.NEG_CACHE_TTL_S`(6 小时)内同键直接回缓存态。

    键口径:坐标按 :data:`COORD_PRECISION` **定点**后入库(与 :class:`Stay` 一致),
    否则浮点尾巴会让"同一个点"存成两行、缓存永远命不中;唯一键
    ``(lat, lng, radius_m, kind)`` 让重复写变成 upsert(刷新 ``fetched_at`` 即续期)。
    ``nearest_km`` 是空结果时"库里已知的最近一家在几公里外"(可空),给前端
    "最近的在 X km 外"提示用;它只是提示,不是身份的一部分。
    """

    __tablename__ = "stay_query_cache"
    __table_args__ = (
        UniqueConstraint("lat", "lng", "radius_m", "kind", name="uq_stay_cache_query"),
        Index("ix_stay_cache_lookup", "lat", "lng", "radius_m"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    radius_m: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(
        String(KIND_LEN), nullable=False, default=STAY_CACHE_EMPTY
    )
    reason: Mapped[str] = mapped_column(
        String(STAY_REASON_LEN), nullable=False, default=REASON_NO_DATA
    )
    nearest_km: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return (
            f"<StayQueryCache {self.lat:.7f},{self.lng:.7f} r={self.radius_m} "
            f"{self.kind}/{self.reason} nearest={self.nearest_km}>"
        )


# --------------------------------------------------------------------------- #
# 行程方案(TASK-5a,M4 第一步):TripPlan
# --------------------------------------------------------------------------- #


class TripPlan(Base):
    """一份行程方案:把已收藏的「目的地 + 路线 + 住宿」组合起来,给出大致总花费。

    三个引用列存的都是 :class:`Collection` 的主键,**故意不建外键**:收藏是快照、方案是
    编排,用户在收藏面板里删掉一条收藏不该把方案连带删掉;报价时缺行按"已删除"降级处理
    (见 :func:`services.trips.quote_plan` 的 ``missing``)。

    总花费**不重新调 ``/api/routes``**,只按 ``Collection.summary`` 里"当时口径"的
    ``cost_cny`` 与住宿价估算串相加,所以金额恒为**估算**(响应里 ``kind="estimate"`` +
    ``note`` 双标注)。晚数(``nights``)是**报价时**的参数,不进表:同一份方案问"住 1 晚
    多少钱""住 3 晚多少钱"都不该产生新行。

    幂等:唯一键 ``name``(``uq_trip_plan_name``)—— 同名再次提交视为"刷新方案"
    (更新引用与备注、顶 ``updated_at``),``id`` 与 ``created_at`` 保持第一次的值,
    与 :class:`Collection` / :class:`CollectionCat` 的 upsert 口径一致。
    """

    __tablename__ = "trip_plans"
    __table_args__ = (UniqueConstraint("name", name="uq_trip_plan_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, index=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    place_collection_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    route_collection_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    stay_collection_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return (
            f"<TripPlan {self.id} {self.name!r} "
            f"路线{len(self.route_collection_ids or [])}段/住宿{len(self.stay_collection_ids or [])}处>"
        )


# --------------------------------------------------------------------------- #
# AI 推荐(神朱 2026-09-27 功能2)与目的地长介绍(功能3)
# --------------------------------------------------------------------------- #

SIGNATURE_LEN = 40
PROVIDER_LEN = 64
BASIS_LEN = 32
ALL_CATEGORIES = ""  # 推荐/介绍的 category 维度:空串 = 不限分类(与 API 的 category 留空同口径)


class PlaceRecommendation(Base):
    """某 (起点城市, band, 分类) 下 AI 推荐的 3~5 个"最值得去"目的地。

    为什么单独一张表:推荐是**整段的派生结果**(不是某个 Place 的属性),而且要把
    "这一结果基于哪一批候选算出来的"一起存下来 —— ``signature`` 是候选集合的指纹
    (:func:`services.recommend.candidate_signature`),分段重新抓取 / 候选变化后指纹
    跟着变,自然算出新的一行(旧行留着可追溯),同指纹重复请求直接命中缓存、不再烧 token。

    ``items`` 是 JSON 列表 ``[{"place_id", "rank", "reason"}, ...]``(按 rank 升序);
    ``provider`` 记 LLM 供应商标识(未配置 key 降级时为空),``degraded=True`` 表示这次
    是**没有 AI 参与**的兜底结果(按距离取前 N 条),前端据此标注"AI 推荐"还是"按距离推荐"。
    """

    __tablename__ = "place_recommendations"
    __table_args__ = (
        UniqueConstraint("origin_city", "band", "category", "signature", name="uq_reco_segment_sig"),
        Index("ix_reco_lookup", "origin_city", "band", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    origin_city: Mapped[str] = mapped_column(String(CITY_LEN), nullable=False, index=True)
    band: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default=ALL_CATEGORIES, index=True)
    signature: Mapped[str] = mapped_column(String(SIGNATURE_LEN), nullable=False)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    provider: Mapped[str] = mapped_column(String(PROVIDER_LEN), nullable=False, default="")
    basis: Mapped[str] = mapped_column(String(BASIS_LEN), nullable=False, default="")
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return f"<PlaceRecommendation {self.origin_city}/{self.band}/{self.category or '全部'} {len(self.items or [])}条>"


class PlaceDetail(Base):
    """目的地的「2~3 句重点介绍」:列表模式用的长文案,与 :attr:`Place.intro` 并存。

    分工:``Place.intro`` 是弹窗里的一句话简介(短、恒在);``PlaceDetail.text`` 是列表里
    的 2~3 句重点介绍(长、按需生成)。两者都**按 POI 缓存**(DB 即缓存),生成失败
    降级为不写行、下次可重试 —— 不阻塞列表渲染。
    """

    __tablename__ = "place_details"
    __table_args__ = (UniqueConstraint("place_id", name="uq_place_detail"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    place_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("places.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(PROVIDER_LEN), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return f"<PlaceDetail place={self.place_id} {len(self.text or '')}字>"


# --------------------------------------------------------------------------- #
# 地理编码持久缓存(TASK-7a):OriginCache
# --------------------------------------------------------------------------- #

GEOCODER_LEN = 16


class OriginCache(Base):
    """城市名 → 起点坐标的**地理编码持久缓存**:有这行且未过期 = 不必再问地理编码源。

    ``/api/geocode`` 每次都实调地理编码源(TASK-9c 起主链是高德,降级腿 Photon 在德国
    实测 2.7~3.4s);而城市中心坐标基本不变,同一个城市被反复搜索就是反复白等白耗配额。
    于是把结果落一行,
    ``WHERE2GO_ORIGIN_CACHE_TTL_S``(缺省 7 天)内命中直接返回、零网络
    (读写口径见 :func:`db.repository.get_origin_cache` 与 app.api.places.geocode_city)。

    键口径:``city`` 就是调用方传来的城市名(去空白后),String **主键** → 天然幂等 upsert
    (重复写只刷新坐标与 ``updated_at``)。``geocoder`` 原样存 ``amap`` / ``photon`` / ``nominatim``,
    命中时按原值回报,响应形状与不走缓存时逐字段一致;**只缓存真的问到了地理编码源的
    结果** —— 调用方直接给坐标(``geocoder="none"``)的退化路径不写行。坐标按
    :data:`COORD_PRECISION` 定点入库,与其他表同一口径。
    """

    __tablename__ = "origin_cache"

    city: Mapped[str] = mapped_column(String(CITY_LEN), primary_key=True)
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False, default="")
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    geocoder: Mapped[str] = mapped_column(String(GEOCODER_LEN), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return f"<OriginCache {self.city} {self.lat:.7f},{self.lng:.7f} by {self.geocoder}>"


# --------------------------------------------------------------------------- #
# 目的地图片缓存(TASK-8a1):PlaceMedia
# --------------------------------------------------------------------------- #

MEDIA_SOURCE_LEN = 16
MEDIA_REASON_LEN = 32
MEDIA_PAGE_URL_LEN = 500
# 图源四态:前端据此显示「图源:高德」/「维基百科」/「高德 + 维基百科」或「暂无图片」
MEDIA_SOURCE_AMAP = "amap"
MEDIA_SOURCE_WIKIMEDIA = "wikimedia"
MEDIA_SOURCE_MIXED = "mixed"
MEDIA_SOURCE_NONE = "none"
MEDIA_SOURCES: tuple[str, ...] = (
    MEDIA_SOURCE_AMAP, MEDIA_SOURCE_WIKIMEDIA, MEDIA_SOURCE_MIXED, MEDIA_SOURCE_NONE,
)
# 没拿到图时的三档 reason(可空):缺 key(配好即可重试)/ 真的没图 / 数据源报错(可重试)
MEDIA_REASON_NO_KEY = "no_key"
MEDIA_REASON_NO_DATA = "no_data"
MEDIA_REASON_ERROR = "error"
MEDIA_REASONS: tuple[str, ...] = (MEDIA_REASON_NO_KEY, MEDIA_REASON_NO_DATA, MEDIA_REASON_ERROR)


def media_source(value: Any) -> str:
    """图源归一到 :data:`MEDIA_SOURCES`;未知值一律按 :data:`MEDIA_SOURCE_NONE` 收。"""
    text = str(value or "").strip().lower()
    return text if text in MEDIA_SOURCES else MEDIA_SOURCE_NONE


def media_reason(value: Any) -> Optional[str]:
    """reason 归一到 :data:`MEDIA_REASONS`;空值 → ``None``(有图时 reason 恒为空)。"""
    text = str(value or "").strip().lower()
    if not text:
        return None
    return text if text in MEDIA_REASONS else MEDIA_REASON_ERROR


class PlaceMedia(Base):
    """一个 POI 的图片(**DB 即缓存**):高德 POI 图为主 + 维基/Commons 兜底。

    为什么单独一张表:图片是详情弹窗(TASK-8b)才要的**派生数据**,不进 ``Place``
    (契约 §5:只新增表、不改既有列);而且一次要问两个源(高德 ``/place/text`` 0.1~0.2s
    + 维基 ~1.5s),必须缓存,否则每开一次弹窗就白等一遍。

    键口径:``place_id`` **唯一**(与 :class:`PlaceDetail` 同风格)→ 重复写是 upsert,
    只刷新 ``source``/``images``/``page_url``/``reason``/``fetched_at``。
    ``images`` 存 ``[{"url", "title", "source"}]``,``source`` 标每张图的出处
    (``amap`` / ``wikimedia``),前端据此拼图源文案;**只存真的从数据源拿到的 url,
    没图就是空数组**(绝不编造图源)。``page_url`` 是维基页外链(可空,给「资料来源」用)。

    TTL 不在这里判:命中 :data:`services.place_media.MEDIA_TTL_DEFAULT_S`(7 天)、
    空结果/失败只缓存 :data:`services.place_media.MEDIA_MISS_TTL_DEFAULT_S`(6 小时**负缓存**
    —— 别把一次空结果永久钉死,同 TASK-6c 的教训),读写口径见
    :func:`db.repository.get_place_media` / :func:`db.repository.upsert_place_media`。
    旧库靠 ``create_all`` 自动建表,**不迁移存量**。
    """

    __tablename__ = "place_media"
    __table_args__ = (UniqueConstraint("place_id", name="uq_place_media"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    place_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("places.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(
        String(MEDIA_SOURCE_LEN), nullable=False, default=MEDIA_SOURCE_NONE
    )
    images: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    page_url: Mapped[Optional[str]] = mapped_column(String(MEDIA_PAGE_URL_LEN), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(MEDIA_REASON_LEN), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return (
            f"<PlaceMedia place={self.place_id} {self.source} "
            f"{len(self.images or [])}张 reason={self.reason}>"
        )
