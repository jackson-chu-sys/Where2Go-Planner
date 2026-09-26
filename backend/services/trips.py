"""行程方案(TASK-5a,M4 第一步,**纯后端**):``TripPlan`` 组合 + "当时口径"总花费估算。

一份方案 = 把已经收藏好的三类东西拼成一次出行:

* ``place_collection_id`` —— 去哪儿(``Collection`` ``kind=place``,可空);
* ``route_collection_ids`` —— 怎么去(``kind=route``,可多段);
* ``stay_collection_ids`` —— 住哪儿(住宿收藏,价估算串在快照 ``summary["price_estimate"]``,
  没有就按 OSM 身份退回 :class:`~db.models.Stay` 行查 —— 仍然只读库)。

存的是**引用**而不是外键:用户整理收藏面板时删掉一条收藏,不该把方案连带删掉;
报价时缺行按"已删除"降级列进 ``missing``(见 :func:`quote_plan`),方案本身照旧能打开。

总花费**不重新调 ``/api/routes``**,只读 ``Collection.summary`` 的快照按"当时口径"相加
(收藏那一刻的 ``cost_cny`` 与住宿价估算),所以金额恒为**估算**:``kind="estimate"`` +
``note`` 双标注(架构文档口径:估算字段必须自带标注,不能长得像报价)。

* 交通 = 各路线收藏 ``summary["cost_cny"]`` 之和,**缺项跳过**(OSRM 降级时本来就是
  ``null``,不瞎造数字);
* 住宿 = 各处价**下限的均值** × 晚数;有上限的再按上限给一档 ``total_cny_high``
  (单值价按"上限=下限"算,所以上限档只在真有区间时才高于下限档);
* 价估算串解析见 :func:`parse_nightly_price`,复用 :data:`services.stays.PRICE_RANGE_RE`
  的同一套口径(``约¥250-450/晚``、``300-500元``、``1,200~1,800`` 都认);解析不出来一律
  ``(None, None)`` —— **宁可少算,不猜数**。

晚数(``nights``)是**报价参数**、不进表:同一份方案问"住 1 晚""住 3 晚"不该产生新行。
幂等键是方案名(``uq_trip_plan_name``):同名再提交 = 刷新引用与备注、返回原行,
与 :func:`db.repository.upsert_collection` / ``upsert_collection_cat`` 同一口径。
本模块**不触网**:只读写 SQLite。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (
    NAME_LEN,
    Collection,
    Stay,
    TripPlan,
    clean_text,
    iso_utc,
    utcnow,
)
from services.stays import PRICE_RANGE_RE

# 报价口径标注:金额一律是估算(与 services.stays 的 ``price_is_estimate`` 同一思路)
QUOTE_KIND = "estimate"
QUOTE_NOTE = "按收藏快照的当时口径估算 · 仅供参考"
MONEY_PRECISION = 2
# 快照摘要里读哪两个键:交通费与住宿价估算串(都是收藏那一刻写下的,不回算)
SUMMARY_COST_KEY = "cost_cny"
SUMMARY_PRICE_KEY = "price_estimate"
# 晚数上下限:0 晚不成立(当天往返不该按住宿算),60 晚以上已不是"周末去哪儿玩"的口径
MIN_NIGHTS = 1
MAX_NIGHTS = 60
DEFAULT_NIGHTS = 1


# --------------------------------------------------------------------------- #
# 归一工具
# --------------------------------------------------------------------------- #


def _number_or_none(value: Any) -> Optional[float]:
    """宽容转 float:``1,200`` / ``1，200`` / ``300.`` 都认;非数字 / NaN / inf → ``None``。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        # 千分位:半角逗号与全角逗号都吃掉("1,200" / "1，200" 是同一个数)
        text: Any = value.replace(",", "").replace("\uff0c", "").strip().strip(".")
        if not text:
            return None
    else:
        text = value
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _money(value: Optional[float]) -> Any:
    """金额归一:定点 :data:`MONEY_PRECISION` 位;整数就去掉 ``.0``(JSON 里更好看)。"""
    if value is None:
        return None
    number = round(float(value), MONEY_PRECISION)
    return int(number) if number == int(number) else number


def parse_nightly_price(price_estimate: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    """从价估算文案里解析**一晚**的 ``(下限, 上限)``;解析不出 → ``(None, None)``(不猜数)。

    认的写法(``services.stays.parse_price`` 产出的规范串与 LLM 的各种变体):
    ``约¥250-450/晚``、``¥300/晚``、``300-500元``、``约 ¥1,200~1,800 每晚``、``价位:200—320``;
    单值价返回 ``(值, None)`` —— 上限交给调用方按"上限=下限"处理。
    空串 / ``暂无报价`` / ``面议`` / ``¥0`` / 非字符串一律 ``(None, None)``;
    上下限写反了(``450-250``)就纠正回来,区间本身无序,不算猜数。
    """
    if isinstance(price_estimate, bool):
        return None, None
    if isinstance(price_estimate, (int, float)):
        number = _number_or_none(price_estimate)
        if number is None or number <= 0:
            return None, None
        return number, None
    if not isinstance(price_estimate, str):
        return None, None
    matched = PRICE_RANGE_RE.search(price_estimate)
    if matched is None:
        return None, None
    low = _number_or_none(matched.group("low"))
    high = _number_or_none(matched.group("high"))
    if low is None or low <= 0:
        return None, None
    if high is not None and high <= 0:
        high = None
    if high is not None and high < low:
        low, high = high, low
    return low, high


def resolve_nights(nights: Any = DEFAULT_NIGHTS) -> int:
    """晚数归一:没给按 :data:`DEFAULT_NIGHTS`;不在 ``1..60`` 内抛 :class:`ValueError`。"""
    if nights is None or (isinstance(nights, str) and not nights.strip()):
        return DEFAULT_NIGHTS
    hint = f"参数 nights 必须是 {MIN_NIGHTS}-{MAX_NIGHTS} 的整数"
    if isinstance(nights, bool):
        raise ValueError(f"{hint},收到:{nights!r}")
    try:
        resolved = int(str(nights).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{hint},收到:{nights!r}") from exc
    if resolved < MIN_NIGHTS or resolved > MAX_NIGHTS:
        raise ValueError(f"参数 nights 超出 {MIN_NIGHTS}-{MAX_NIGHTS} 范围,收到:{nights!r}")
    return resolved


def _optional_ref_id(name: str, value: Any) -> Optional[int]:
    """可空收藏引用归一:``None``/空串 → ``None``;非整数 / 非正数抛 :class:`ValueError`。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数,收到:{value!r}")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数,收到:{value!r}") from exc
    if number <= 0:
        raise ValueError(f"{name} 必须是正整数,收到:{value!r}")
    return number


def _ref_id_list(name: str, value: Any) -> list[int]:
    """收藏引用**数组**归一:去重保序;不是数组 / 元素不是正整数抛 :class:`ValueError`。"""
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} 必须是整数数组,收到:{type(value).__name__}")
    resolved: list[int] = []
    for item in value:
        number = _optional_ref_id(name, item)
        if number is None:
            raise ValueError(f"{name} 必须是整数数组,收到:{value!r}")
        if number not in resolved:
            resolved.append(number)
    return resolved


def _trip_plan_id(trip_plan_id: Any) -> int:
    """方案主键归一:空 / 非整数 / 非正数抛 :class:`ValueError`(API 层转 400)。"""
    resolved = _optional_ref_id("方案 id", trip_plan_id)
    if resolved is None:
        raise ValueError("缺少必要参数:方案 id")
    return resolved


def _collections_by_id(session: Session, ids: Sequence[Optional[int]]) -> dict[int, Collection]:
    """一次查回一批收藏行(报价时按引用逐个查会 N+1)。"""
    wanted = sorted({cid for cid in ids if cid is not None})
    if not wanted:
        return {}
    rows = session.scalars(select(Collection).where(Collection.id.in_(wanted)))
    return {row.id: row for row in rows}


def _missing_ids(session: Session, ids: Sequence[Optional[int]]) -> list[int]:
    """引用里查不到的收藏 id(**已删除**),按传入顺序返回、去重。"""
    wanted = list(dict.fromkeys(cid for cid in ids if cid is not None))
    if not wanted:
        return []
    found = set(session.scalars(select(Collection.id).where(Collection.id.in_(wanted))))
    return [cid for cid in wanted if cid not in found]


def stay_price_estimate(session: Session, collection: Collection) -> Optional[str]:
    """住宿收藏的价估算串:优先用**快照**里的,快照没留就按 OSM 身份退回 ``stays`` 表。

    退回查库只是为了少丢数字(收藏那一刻 ``summary`` 里没塞 ``price_estimate`` 的历史数据),
    仍然**只读 SQLite、不触网、不调 LLM**;两边都没有就返回 ``None``,报价按"没估价"跳过。
    """
    summary = collection.summary if isinstance(collection.summary, Mapping) else {}
    snapshot = clean_text(summary.get(SUMMARY_PRICE_KEY))
    if snapshot:
        return snapshot
    if collection.osm_type and collection.osm_id is not None:
        stay = session.scalar(
            select(Stay).where(
                Stay.osm_type == collection.osm_type, Stay.osm_id == collection.osm_id
            )
        )
        return clean_text(stay.price_estimate) if stay is not None else None
    return None


# --------------------------------------------------------------------------- #
# 报价:读快照算总账(不重新调 /api/routes)
# --------------------------------------------------------------------------- #


def quote_plan(
    session: Session,
    place_ref: Any,
    route_refs: Any,
    stay_refs: Any,
    *,
    nights: int = DEFAULT_NIGHTS,
) -> dict[str, Any]:
    """按收藏快照的"当时口径"给一份方案估总花费;返回的 dict 里金额恒为**估算**。

    * ``transport_cny`` = 各路线收藏 ``summary["cost_cny"]`` 之和,缺项(``None``/非数字)跳过;
    * 住宿 = 各处价**下限的均值** × ``nights``;有上限的按上限再算一档,单值价按
      "上限=下限"处理,所以谁都没给区间时 ``total_cny_high == total_cny_low``;
    * ``per_stay`` 逐处给出 ``{collection_id, name, price_estimate, low, high}``(前端能解释
      这个总数是怎么来的),``missing`` 是引用里**已被删除**的收藏 id;
    * ``kind``/``note`` 是估算标注(金额不是报价)。

    目的地收藏(``place_ref``)不产生费用,只参与 ``missing`` 判定。
    ``nights`` 超出 :data:`MIN_NIGHTS`..:data:`MAX_NIGHTS` 抛 :class:`ValueError`(API 层转 400);
    引用非法(不是正整数数组)同样抛 :class:`ValueError`。
    """
    resolved_nights = resolve_nights(nights)
    place_id = _optional_ref_id("place_collection_id", place_ref)
    route_ids = _ref_id_list("route_collection_ids", route_refs)
    stay_ids = _ref_id_list("stay_collection_ids", stay_refs)
    rows = _collections_by_id(session, [place_id, *route_ids, *stay_ids])

    missing = _missing_ids(session, [place_id, *route_ids, *stay_ids])
    transport = 0.0
    for collection_id in route_ids:
        row = rows.get(collection_id)
        if row is None:
            continue
        summary = row.summary if isinstance(row.summary, Mapping) else {}
        cost = _number_or_none(summary.get(SUMMARY_COST_KEY))
        if cost is not None:
            transport += cost

    per_stay: list[dict[str, Any]] = []
    lows: list[float] = []
    highs: list[float] = []
    for collection_id in stay_ids:
        row = rows.get(collection_id)
        if row is None:
            continue
        price = stay_price_estimate(session, row)
        low, high = parse_nightly_price(price)
        per_stay.append(
            {
                "collection_id": collection_id,
                "name": row.name or None,
                "price_estimate": price,
                "low": _money(low),
                "high": _money(high),
            }
        )
        if low is not None:
            lows.append(low)
            highs.append(low if high is None else high)

    stay_low = (sum(lows) / len(lows)) * resolved_nights if lows else 0.0
    stay_high = (sum(highs) / len(highs)) * resolved_nights if highs else stay_low
    return {
        "total_cny_low": _money(transport + stay_low),
        "total_cny_high": _money(transport + stay_high),
        "transport_cny": _money(transport),
        "stay_nights": resolved_nights,
        "per_stay": per_stay,
        "missing": missing,
        "kind": QUOTE_KIND,
        "note": QUOTE_NOTE,
    }


# --------------------------------------------------------------------------- #
# 入库 / 读库(repository 风格:幂等 upsert + 序列化)
# --------------------------------------------------------------------------- #


def trip_plan_to_dict(
    row: TripPlan, quote: Optional[Mapping[str, Any]] = None
) -> dict[str, Any]:
    """方案 ORM 行 → API/前端用的 dict(引用平铺 + ``counts`` + 可选 ``quote``)。

    ``counts`` 让列表一眼看出"这份方案拼了几段路线、几处住宿";``quote`` 是
    :func:`quote_plan` 的结果(没算就是 ``None``,不给假数字)。
    """
    routes = list(row.route_collection_ids or [])
    stays = list(row.stay_collection_ids or [])
    counts = {
        "place": 1 if row.place_collection_id else 0,
        "routes": len(routes),
        "stays": len(stays),
    }
    return {
        "id": row.id,
        "name": row.name,
        "note": row.note,
        "place_collection_id": row.place_collection_id,
        "route_collection_ids": routes,
        "stay_collection_ids": stays,
        "counts": counts,
        "quote": dict(quote) if quote else None,
        "created_at": iso_utc(row.created_at),
        "updated_at": iso_utc(row.updated_at),
    }


def upsert_trip_plan(
    session: Session,
    *,
    name: Any,
    note: Any = None,
    place_collection_id: Any = None,
    route_collection_ids: Any = None,
    stay_collection_ids: Any = None,
) -> tuple[TripPlan, bool]:
    """建 / 刷新一份行程方案,按唯一名字**幂等**;返回 ``(行, 是否新建)``。

    同名再提交 = 刷新:更新引用、顶 ``updated_at``,而 ``id`` 与 ``created_at`` 保持原样
    (与 :func:`db.repository.upsert_collection` 同口径,列表排序才稳定)。
    ``note`` 只在给了非空值时覆盖(与 ``upsert_collection_cat`` 保护备注同一口径)。
    写入时**引用必须存在**(不存在 → :class:`ValueError`,API 层转 400):拼方案时引用
    就查不到,基本是前端传错了 id;至于"存完之后收藏被删",由 :func:`quote_plan` 降级。
    只 ``flush`` 不 ``commit``:提交时机交给调用方。
    """
    resolved_name = clean_text(name, limit=NAME_LEN)
    if not resolved_name:
        raise ValueError("缺少必要参数:name(方案名不能为空)")
    resolved_place = _optional_ref_id("place_collection_id", place_collection_id)
    resolved_routes = _ref_id_list("route_collection_ids", route_collection_ids)
    resolved_stays = _ref_id_list("stay_collection_ids", stay_collection_ids)
    missing = _missing_ids(session, [resolved_place, *resolved_routes, *resolved_stays])
    if missing:
        raise ValueError(
            f"未知收藏:{'、'.join(str(item) for item in missing)}(引用不存在,先收藏再组合方案)"
        )

    row = session.scalar(select(TripPlan).where(TripPlan.name == resolved_name))
    created = row is None
    if created:
        row = TripPlan(name=resolved_name, created_at=utcnow())
        session.add(row)
    resolved_note = clean_text(note)
    if resolved_note:
        row.note = resolved_note
    row.place_collection_id = resolved_place
    row.route_collection_ids = resolved_routes
    row.stay_collection_ids = resolved_stays
    row.updated_at = utcnow()
    session.flush()
    return row, created


def get_trip_plan(session: Session, *, trip_plan_id: Any) -> Optional[TripPlan]:
    """按主键取方案 **ORM 行**;不存在返回 ``None``(API 层转 404)。id 非法抛 ValueError。"""
    resolved = _trip_plan_id(trip_plan_id)
    return session.scalar(select(TripPlan).where(TripPlan.id == resolved))


def select_trip_plans(session: Session, *, limit: Optional[int] = None) -> list[TripPlan]:
    """方案行(**新的在前**),给需要 ORM 行的调用方用(API 走 :func:`list_trip_plans`)。"""
    stmt = select(TripPlan).order_by(TripPlan.created_at.desc(), TripPlan.id.desc())
    if limit:
        stmt = stmt.limit(max(1, int(limit)))
    return list(session.scalars(stmt))


def list_trip_plans(
    session: Session, *, limit: Optional[int] = None, nights: int = DEFAULT_NIGHTS
) -> list[dict[str, Any]]:
    """方案列表(**新的在前**),每项带 ``counts`` 与按 ``nights`` 算的 ``quote``。"""
    resolved_nights = resolve_nights(nights)
    return [
        trip_plan_to_dict(
            row,
            quote=quote_plan(
                session,
                row.place_collection_id,
                row.route_collection_ids,
                row.stay_collection_ids,
                nights=resolved_nights,
            ),
        )
        for row in select_trip_plans(session, limit=limit)
    ]


def delete_trip_plan(session: Session, *, trip_plan_id: Any) -> bool:
    """删除一份方案;返回是否真删掉了(``False`` = 本来就不存在,重复删同样不报错)。

    只删方案行:引用的收藏(``Collection``)一律不动。
    """
    row = get_trip_plan(session, trip_plan_id=trip_plan_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def count_trip_plans(session: Session) -> int:
    """方案计数,给前端状态栏与删除后的余量提示用。"""
    return int(session.scalar(select(func.count(TripPlan.id))) or 0)
