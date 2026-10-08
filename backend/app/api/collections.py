"""收藏 API(TASK-2c,阶段2c):路线 / 目的地收藏的增、删、查(**幂等**)。

三条路由:

* ``POST /api/collections`` —— 新增一条收藏(``kind=route`` 路线 / ``kind=place`` 目的地)。
  唯一键 ``(kind, ref_key, mode)``:同一起终点 + 同一方式重复收藏**不报错也不产生第二行**,
  而是刷新快照摘要并返回原行(``created=false``);``id`` 与 ``created_at`` 保持
  "第一次收藏"的值,列表排序才稳定。
* ``GET /api/collections?kind=&cat=&limit=`` —— 收藏列表(**新的在前**),可按类型 / 分组过滤;
  响应附带 ``counts_by_kind`` 与分组清单,前端一个请求就能渲染整个收藏面板。
* ``DELETE /api/collections/{id}`` —— 删除一条收藏;``id`` 非正整数 **400**,不存在 **404**。

存的是**快照**:收藏那一刻的 ``mode``/``duration_min``/``cost_cny``/``distance_km``
(``/api/routes`` 返回什么就存什么,``geometry`` 主动丢掉不占库)。之后价格系数变了、
数据源降级了,列表仍显示用户当时看到的数字(M4 对比总账要的正是"当时口径");
要看最新数字请重新调 ``/api/routes``。

校验口径与 ``/api/places``、``/api/routes`` 一致:缺参 / 非法一律 **400 + 中文报错**。
所以请求体故意收成裸 JSON 对象(``Body(None)``)而不是 pydantic 模型 —— 否则字段类型不对
会被 FastAPI 拦成 422 英文报错,同一个"参数不对"就有两种状态码。
本模块**基本不触网**:只读写 SQLite(收藏是快照,不需要重新算路线);唯一例外是
TASK-15 的存量住宿收藏 ``summary.city`` **懒补齐**(逆地理降级链,每坐标一次、
进程内 TTL 缓存,失败留空不阻塞响应)。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from db import models
from db import repository as repo
from db.base import get_session
from services import place_loader
from services import routes as route_service

router = APIRouter()

MAX_PAGE = 200  # 单次列表上限:前端传更大的 limit 也按这个截断,免得一次把整库拉走
# 父级标注(功能4):写在 summary 里的键名 —— 收藏是快照,父级也是快照的一部分,
# 不新增物理列就不需要迁移,旧数据没有这个键时前端按"未归类"显示。
PARENT_KEY = "parent"

COLLECTIONS_NOTE = (
    "收藏存的是快照:收藏那一刻的方式/时长/费用/里程(即 /api/routes 的返回,geometry 不入库),"
    "之后价格系数变了、路线数据源降级了也不回填,要看最新数字请重新调 /api/routes。"
    "唯一键 (kind, ref_key, mode) 让重复收藏幂等:同一对起终点 + 同一方式只有一行,"
    "再次收藏刷新快照并返回原行(created=false),id 与 created_at 保持第一次收藏的值;"
    "place 收藏没有出行方式(mode 恒为空串),同一目的地收藏两次同样幂等。"
)
DELETE_NOTE = "只删这一条收藏,不影响目的地库(Place)、抓取水位与路线计算。"


def _optional_text(value: Any) -> Optional[str]:
    """可选文本参数归一:只认真正的 ``str``。

    直接调用端点函数(单测 / 脚本)时,FastAPI 的 ``Query(None)`` 默认值不会被解析,
    传进来是 ``FieldInfo`` 对象;这里统一把"没给"归一成 ``None``(同 ``app.api.routes``)。
    """
    return value if isinstance(value, str) else None


def _optional_int(name: str, value: Any) -> Optional[int]:
    """可选整数参数归一:没给 → ``None``;非整数 / 非正数 → **400**(中文报错)。"""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (str, int)):
        return None  # FieldInfo 等"没给"的情况
    text = str(value).strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError as exc:
        raise HTTPException(400, f"参数 {name} 必须是整数,收到:{value!r}") from exc
    if number <= 0:
        raise HTTPException(400, f"参数 {name} 必须是正整数,收到:{value!r}")
    return number


def _payload_dict(payload: Any) -> dict[str, Any]:
    """请求体归一:必须是 JSON 对象(``{...}``),否则 **400**(中文报错)。"""
    if payload is None:
        raise HTTPException(400, "缺少请求体:POST /api/collections 需要 JSON 对象")
    if not isinstance(payload, Mapping):
        raise HTTPException(
            400, f"请求体必须是 JSON 对象,收到:{type(payload).__name__}"
        )
    return dict(payload)


def _resolve_kind(raw_kind: Any) -> str:
    """收藏类型归一(``route`` / ``place``);缺失 / 非法 → **400**。"""
    try:
        return models.collection_kind(raw_kind)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _resolve_mode(kind: str, raw_mode: Any) -> str:
    """出行方式归一:``route`` 必填且要在 ``services.routes.MODES`` 里;``place`` 恒为空串。

    非法取值一律 **400**(而不是静默丢弃):前端把 ``/api/routes`` 的 ``mode`` 原样带上即可,
    拼错了当场报错比悄悄存成另一种方式安全。
    """
    wanted = (models.clean_text(raw_mode) or "").lower() or None
    if wanted and wanted not in route_service.MODES:
        raise HTTPException(
            400, f"未知出行方式:{raw_mode!r}(可选:{'、'.join(route_service.MODES)})"
        )
    if kind == models.KIND_PLACE:
        return models.NO_MODE
    if not wanted:
        raise HTTPException(
            400, f"缺少必要参数:mode(可选:{'、'.join(route_service.MODES)})"
        )
    return wanted


def _summary_of(payload: Mapping[str, Any], *, mode: str) -> dict[str, Any]:
    """快照摘要归一:只收 JSON 对象,规范键由 :func:`db.repository.collection_summary` 补齐。

    额外收一个 ``parent``(功能4「路线/住宿收藏挂到目的地下」):调用方给了就规范化后
    一起写进 ``summary``(**不新增物理列**,summary 本就是自由 JSON 快照,回读原样返回);
    没给且是路线收藏时,用目的地 OSM 身份/坐标**自动派生**一个父级(见 :func:`_auto_parent`)。
    """
    raw = payload.get("summary")
    if raw is None:
        summary = repo.collection_summary(None, mode=mode)
    elif not isinstance(raw, Mapping):
        raise HTTPException(
            400, f"参数 summary 必须是 JSON 对象,收到:{type(raw).__name__}"
        )
    else:
        summary = repo.collection_summary(raw, mode=mode)
    parent = _parent_of(payload) or _auto_parent(payload)
    if parent:
        summary[PARENT_KEY] = parent
    return summary


def _parent_of(payload: Mapping[str, Any]) -> Optional[dict[str, str]]:
    """显式父级(``payload["parent"]``)归一:必须是对象且带 ``ref_key``,否则 **400**。"""
    raw = payload.get(PARENT_KEY)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise HTTPException(400, f"参数 parent 必须是 JSON 对象,收到:{type(raw).__name__}")
    ref_key = models.clean_text(raw.get("ref_key"), limit=models.REF_KEY_LEN)
    if not ref_key:
        raise HTTPException(400, "参数 parent 缺少必要字段:ref_key")
    return {
        "kind": models.clean_text(raw.get("kind"), limit=16) or models.KIND_PLACE,
        "ref_key": ref_key,
        "name": models.clean_text(raw.get("name"), limit=models.POINT_NAME_LEN) or "",
    }


def _auto_parent(payload: Mapping[str, Any]) -> Optional[dict[str, str]]:
    """**路线**收藏的父级 = 目的地(用它的 OSM 身份,退化到坐标定点串;与 ref_key 同口径)。

    只对 ``kind=route`` 派生:目的地收藏本身就是那个节点(``parent`` 会等于自己,没意义);
    住宿收藏的父级由前端在收藏时显式给出(它知道自己在哪个目的地面板里被收藏的)。
    与目的地收藏的 ``ref_key`` 完全一致(``place:<osm_type>/<osm_id>`` 或 ``place:<lat,lng>``),
    前端把两级拼在一起时不需要额外映射表。缺目的地引用时返回 ``None``(不报错)。
    """
    if models.collection_kind(payload.get("kind")) != models.KIND_ROUTE:
        return None
    try:
        ref_key = models.collection_ref_key(
            kind=models.KIND_PLACE,
            osm_type=payload.get("osm_type"),
            osm_id=payload.get("osm_id"),
            to_lat=payload.get("to_lat"),
            to_lng=payload.get("to_lng"),
        )
    except ValueError:
        return None
    return {
        "kind": models.KIND_PLACE,
        "ref_key": ref_key,
        "name": models.clean_text(payload.get("to_name"), limit=models.POINT_NAME_LEN) or "",
    }


def _display_name(payload: Mapping[str, Any], *, kind: str, mode: str) -> Optional[str]:
    """收藏标题:调用方给了就用,没给按 ``起点 → 目的地 · 方式`` 生成(见 :mod:`db.models`)。"""
    given = models.clean_text(payload.get("name"), limit=models.NAME_LEN)
    if given:
        return given
    meta = route_service.MODE_META.get(mode)
    return models.default_collection_name(
        kind=kind,
        mode_label=(meta or {}).get("label"),
        from_name=payload.get("from_name"),
        to_name=payload.get("to_name"),
        from_lat=payload.get("from_lat"),
        from_lng=payload.get("from_lng"),
        to_lat=payload.get("to_lat"),
        to_lng=payload.get("to_lng"),
    )


def _cat_name(session: Session, cat_id: Optional[int]) -> Optional[str]:
    """单条收藏的分组名(列表走 :func:`db.repository.list_collections` 的批量查询,不 N+1)。"""
    if not cat_id:
        return None
    cat = repo.get_collection_cat(session, cat_id=cat_id)
    return cat.name if cat else None


# --------------------------------------------------------------------------- #
# TASK-13:GET /api/collections items 增 place_id(收藏 → 库内 Place 行匹配)
# --------------------------------------------------------------------------- #


def _match_place_id(session: Session, item: Mapping[str, Any]) -> Optional[int]:
    """按收藏快照的身份在 Place 表匹配 ``place_id``;匹配不到返回 ``None``(不猜)。

    只认 ``kind=place`` 且 ``ref_key`` 以 ``place:`` 开头的目的地收藏;身份口径与
    :func:`db.models.collection_ref_key` 完全一致:优先 OSM/amap 身份
    (``place:<osm_type>/<osm_id>``,重抓改名都不飘),退化到坐标定点串
    (``place:<lat,lng>``,7 位小数精确比对)。住宿收藏虽也是 kind=place,但它的
    身份来自 Stay 表,在 Place 表里天然匹配不到 → ``None``,前端走外链兜底。
    """
    if str(item.get("kind") or "") != models.KIND_PLACE:
        return None
    ref_key = str(item.get("ref_key") or "")
    if not ref_key.startswith(f"{models.KIND_PLACE}:"):
        return None
    target = ref_key.split(":", 1)[1]
    row = None
    if "/" in target:
        osm_type, _, osm_id = target.partition("/")
        try:
            numeric_id = int(osm_id)
        except ValueError:
            return None
        row = (
            session.query(models.Place)
            .filter(models.Place.osm_type == osm_type, models.Place.osm_id == numeric_id)
            .order_by(models.Place.id.asc())
            .first()
        )
    elif "," in target:
        lat_text, _, lng_text = target.partition(",")
        try:
            lat = round(float(lat_text), models.COORD_PRECISION)
            lng = round(float(lng_text), models.COORD_PRECISION)
        except ValueError:
            return None
        row = (
            session.query(models.Place)
            .filter(models.Place.lat == lat, models.Place.lng == lng)
            .order_by(models.Place.id.asc())
            .first()
        )
    return int(row.id) if row is not None else None


# --------------------------------------------------------------------------- #
# TASK-15:存量住宿收藏 summary.city 懒补齐(OTA 搜索词=城市+酒店名)
# --------------------------------------------------------------------------- #

#: 逆地理反查城市的进程内缓存:坐标定点串 → (city, 过期时刻)。仿 OriginCache 的
#: 7 天口径;反查失败缓存 1 小时(避免每次开面板都重打逆地理,但不永久判死)。
_CITY_CACHE_TTL_S = 7 * 24 * 3600
_CITY_CACHE_NEG_TTL_S = 3600
_city_cache: dict[str, tuple[Optional[str], float]] = {}
_city_cache_lock = threading.Lock()


def _is_stay_row(row: Mapping[str, Any]) -> bool:
    """住宿收藏判定(与前端 favGroupOf 同口径):kind=place + 住宿指纹。"""
    if str(row.get("kind") or "") != models.KIND_PLACE:
        return False
    summary = row.get("summary") or {}
    if not isinstance(summary, Mapping):
        summary = {}
    if summary.get("stay_kind") is not None or summary.get("price_estimate"):
        return True
    name = str(row.get("name") or "")
    return name.startswith("🛏️") or "🛏" in name


def city_of_coordinates(lat: Any, lng: Any) -> Optional[str]:
    """坐标 → 城市名(逆地理降级链:高德 → Photon → Nominatim);失败返回 ``None`` 不抛。

    结果进进程内 TTL 缓存(同坐标 7 天内只打一次逆地理);城市名去尾「市」
    (与 :func:`services.stays.normalize_city_name` 同口径,搜「昆山」比「苏州市」准的
    县级市情形由 adname 线索在前端快照里另行兜底,这里只保证不比现状差)。
    """
    try:
        latitude = round(float(lat), models.COORD_PRECISION)
        longitude = round(float(lng), models.COORD_PRECISION)
    except (TypeError, ValueError):
        return None
    key = f"{latitude},{longitude}"
    now = time.time()
    with _city_cache_lock:
        cached = _city_cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]
    city: Optional[str] = None
    try:
        geo, _source = place_loader.reverse_with_fallback(latitude, longitude)
        raw = place_loader.city_from_display_name(geo.get("display_name"))
        city = str(raw or "").strip().rstrip("市").strip() or None
    except Exception:  # noqa: BLE001 - 懒补齐失败只留空,绝不阻塞收藏列表
        city = None
    ttl = _CITY_CACHE_TTL_S if city else _CITY_CACHE_NEG_TTL_S
    with _city_cache_lock:
        _city_cache[key] = (city, now + ttl)
    return city


def backfill_stay_cities(session: Session) -> int:
    """存量住宿收藏懒补齐 ``summary.city``:反查得到才写回持久化,失败留空不阻塞。

    返回本次写回的行数。只碰 ``kind=place`` 的住宿行、只在 summary 缺 ``city`` 且有
    坐标时反查;新收藏由前端把 ``/api/stays`` 的 ``city`` 写进 summary,不走这里。
    """
    rows = (
        session.query(models.Collection)
        .filter(models.Collection.kind == models.KIND_PLACE)
        .all()
    )
    updated = 0
    for row in rows:
        summary = dict(row.summary or {})
        if not _is_stay_row({"kind": row.kind, "summary": summary, "name": row.name}):
            continue
        if str(summary.get("city") or "").strip():
            continue  # 已有城市(前端新收藏或上次补齐过),不重复触网
        city = city_of_coordinates(row.to_lat, row.to_lng)
        if not city:
            continue  # 反查失败留空:下次开面板再试(负缓存 1h 内不打网络)
        summary["city"] = city
        row.summary = summary  # 整 dict 重赋值,JSON 列的变更才会被 SQLAlchemy 追踪
        updated += 1
    if updated:
        session.commit()
    return updated


@router.post("/collections")
def create_collection(
    payload: Any = Body(None, description="收藏内容 JSON 对象:kind / mode / 起终点 / summary"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """新增一条收藏;**重复收藏幂等**(刷新快照、返回原行,``created=false``)。"""
    started = time.monotonic()
    body = _payload_dict(payload)
    kind = _resolve_kind(body.get("kind"))
    mode = _resolve_mode(kind, body.get("mode"))
    try:
        row, created = repo.upsert_collection(
            session,
            kind=kind,
            mode=mode,
            name=_display_name(body, kind=kind, mode=mode),
            osm_type=body.get("osm_type"),
            osm_id=body.get("osm_id"),
            from_lat=body.get("from_lat"),
            from_lng=body.get("from_lng"),
            from_name=body.get("from_name"),
            to_lat=body.get("to_lat"),
            to_lng=body.get("to_lng"),
            to_name=body.get("to_name"),
            summary=_summary_of(body, mode=mode),
            cat_id=body.get("cat_id"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return {
        "collection": repo.collection_to_dict(row, cat_name=_cat_name(session, row.cat_id)),
        "created": created,
        "idempotent": not created,
        "count": repo.count_collections(session),
        "counts_by_kind": repo.count_by_kind(session),
        "elapsed_s": round(time.monotonic() - started, 2),
        "note": COLLECTIONS_NOTE,
    }


@router.get("/collections")
def list_collections(
    kind: Optional[str] = Query(None, description="类型过滤:route / place,留空返回全部"),
    cat: Optional[str] = Query(None, description="分组 id 过滤(可选),留空返回全部"),
    limit: Optional[str] = Query(None, description=f"最多返回几条(正整数,上限 {MAX_PAGE}),留空不限"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """收藏列表(**新的在前**),可按类型 / 分组过滤;附带分类计数与分组清单。"""
    started = time.monotonic()
    wanted_kind = _optional_text(kind)
    if wanted_kind:
        wanted_kind = _resolve_kind(wanted_kind)
    resolved_cat = _optional_int("cat", cat)
    resolved_limit = _optional_int("limit", limit)
    if resolved_limit is not None:
        resolved_limit = min(resolved_limit, MAX_PAGE)
    try:
        items = repo.list_collections(
            session, kind=wanted_kind, cat_id=resolved_cat, limit=resolved_limit
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # TASK-15:存量住宿收藏懒补 summary.city(逆地理一次、进程内 TTL 缓存;
    # 失败留空不阻塞——补完后 items 里的 summary 即含新 city,持久化写回)
    if backfill_stay_cities(session):
        items = repo.list_collections(
            session, kind=wanted_kind, cat_id=resolved_cat, limit=resolved_limit
        )
    # TASK-13:**只加键不改既有键**(place_id,目的地收藏 → 库内 Place 行;匹配不到=None)
    for item in items:
        item["place_id"] = _match_place_id(session, item)
    return {
        "collections": items,
        "count": len(items),
        "kind": wanted_kind,
        "cat_id": resolved_cat,
        "total": repo.count_collections(session),
        "counts_by_kind": repo.count_by_kind(session),
        "cats": repo.list_collection_cats(session),
        "kinds": list(models.COLLECTION_KINDS),
        "modes": list(route_service.MODES),
        "elapsed_s": round(time.monotonic() - started, 2),
        "note": COLLECTIONS_NOTE,
    }


@router.delete("/collections/{collection_id}")
def delete_collection(
    collection_id: str,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """删除一条收藏;``id`` 非正整数 **400**,查不到 **404**(不静默成功)。"""
    resolved = _optional_int("收藏 id", collection_id)
    if resolved is None:
        raise HTTPException(400, f"缺少必要参数:收藏 id(收到:{collection_id!r})")
    try:
        deleted = repo.delete_collection(session, collection_id=resolved)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not deleted:
        raise HTTPException(404, f"收藏不存在:id={resolved}")
    session.commit()
    return {
        "deleted": True,
        "id": resolved,
        "count": repo.count_collections(session),
        "counts_by_kind": repo.count_by_kind(session),
        "note": DELETE_NOTE,
    }
