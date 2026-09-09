"""SQLAlchemy 2.0 数据模型:目的地(Place)+ 抓取水位(SegmentFetch)。

阶段 1a(TASK-1a)落这两张表,存储用 SQLite(见 02-项目计划与架构.md):

* :class:`Place` —— 一个目的地。字段按 docs/STAGE1-PLAN.md 第 4 节的模型定义;
  其中 ``intro``(LLM 一句话简介)与 ``category`` 的**精确四分类**由 TASK-1b
  负责,本阶段先留字段 + 简化归类(见 services.categories)。
* :class:`SegmentFetch` —— (城市, band) 的抓取水位。存在这一行即代表该段已入库,
  二次查询**直接读库、不再触网**(见 services.place_loader.load_segment)。

去重口径(docs/STAGE1-PLAN.md 第 3 节):同一 OSM 实体在同一个城市库里只存一行,
唯一键 ``(osm_type, osm_id, origin_city)``。环形分段互斥,所以 band 不进唯一键;
重新抓取时按该键做 upsert(见 db.repository.upsert_places)。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

OSM_ELEMENT_TYPES: tuple[str, ...] = ("node", "way", "relation")
FALLBACK_OSM_TYPE = "point"
UNCATEGORIZED = "其他"
NAME_LEN = 255
CITY_LEN = 120
COORD_PRECISION = 7


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


class Place(Base):
    """一个目的地(当前来源为 OSM/Overpass,TASK-1c 会补种子数据)。"""

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
