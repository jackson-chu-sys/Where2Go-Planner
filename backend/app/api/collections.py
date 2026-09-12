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
OSRM 降级了,列表仍显示用户当时看到的数字(M4 对比总账要的正是"当时口径");
要看最新数字请重新调 ``/api/routes``。

校验口径与 ``/api/places``、``/api/routes`` 一致:缺参 / 非法一律 **400 + 中文报错**。
所以请求体故意收成裸 JSON 对象(``Body(None)``)而不是 pydantic 模型 —— 否则字段类型不对
会被 FastAPI 拦成 422 英文报错,同一个"参数不对"就有两种状态码。
本模块**不触网**:只读写 SQLite(收藏是快照,不需要重新算路线)。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from db import models
from db import repository as repo
from db.base import get_session
from services import routes as route_service

router = APIRouter()

MAX_PAGE = 200  # 单次列表上限:前端传更大的 limit 也按这个截断,免得一次把整库拉走

COLLECTIONS_NOTE = (
    "收藏存的是快照:收藏那一刻的方式/时长/费用/里程(即 /api/routes 的返回,geometry 不入库),"
    "之后价格系数变了、OSRM 降级了也不回填,要看最新数字请重新调 /api/routes。"
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
    """快照摘要归一:只收 JSON 对象,规范键由 :func:`db.repository.collection_summary` 补齐。"""
    raw = payload.get("summary")
    if raw is None:
        return repo.collection_summary(None, mode=mode)
    if not isinstance(raw, Mapping):
        raise HTTPException(
            400, f"参数 summary 必须是 JSON 对象,收到:{type(raw).__name__}"
        )
    return repo.collection_summary(raw, mode=mode)


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
