"""SQLAlchemy 2.0 数据模型:目的地(Place)+ 抓取水位(SegmentFetch)
+ 收藏(Collection / CollectionCat)。

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

去重口径(docs/STAGE1-PLAN.md 第 3 节):同一 OSM 实体在同一个城市库里只存一行,
唯一键 ``(osm_type, osm_id, origin_city)``。环形分段互斥,所以 band 不进唯一键;
重新抓取时按该键做 upsert(见 db.repository.upsert_places)。

来源标注(TASK-1c 种子数据):**不新增列**,复用 ``Place.tags`` 里的 ``source`` 键
(种子数据写 ``种子``,OSM 抓取不写这个键),存量库无需迁移;:func:`place_source`
再派生出统一的来源字符串给 API/前端用(见 db.repository.place_to_dict)。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
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
NAME_LEN = 255
CITY_LEN = 120
COORD_PRECISION = 7

# 来源标注(TASK-1c):OSM 国内滑雪/运动覆盖差,缺口由人工种子数据垫底(STAGE1-PLAN 第 3 节)
SOURCE_TAG = "source"
SEED_SOURCE = "种子"
OSM_SOURCE = "OSM"


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
    """从 ``tags`` 派生来源标注:``种子`` 或 ``OSM``。

    OSM 抓取来的行没有 ``source`` 键,或写的是 ``survey`` 之类的原始 tag 值,
    一律按 :data:`OSM_SOURCE` 处理 —— 存量库不改一行数据也能正确标注。
    """
    value = str(dict(tags or {}).get(SOURCE_TAG) or "").strip()
    return SEED_SOURCE if value == SEED_SOURCE else OSM_SOURCE


def is_seed(tags: Optional[Mapping[str, Any]]) -> bool:
    """该行是否是人工种子数据(``tags["source"] == "种子"``)。"""
    return place_source(tags) == SEED_SOURCE


class Place(Base):
    """一个目的地:OSM/Overpass 抓取,或人工种子数据(``tags["source"]="种子"``)。"""

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
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="overpass")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return f"<SegmentFetch {self.origin_city} {self.band} {self.place_count} 条 {self.source}>"


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
OSM_REF_TYPES: tuple[str, ...] = OSM_ELEMENT_TYPES + (FALLBACK_OSM_TYPE,)

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
    ``degraded``),之后价格系数变了、OSRM 降级了,收藏列表仍显示用户当时看到的数字
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
