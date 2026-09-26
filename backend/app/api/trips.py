"""行程方案 API(TASK-5a,M4 第一步):``/api/trip-plans`` 的建、查、删(**幂等**)。

四条路由:

* ``POST /api/trip-plans`` —— 新建 / 刷新方案。请求体是裸 JSON
  ``{name, note?, place_collection_id?, route_collection_ids?, stay_collection_ids?, nights?}``;
  唯一键是方案名,所以**重名 = 刷新**(更新引用、返回原行、``created=false``),
  与 ``POST /api/collections`` 的幂等口径一致。响应带 ``quote``(总花费估算)。
* ``GET /api/trip-plans?limit=`` —— 方案列表(**新的在前**),每项带 ``counts`` 与 ``quote``。
* ``GET /api/trip-plans/{id}`` —— 单份方案详情(含 ``quote``);id 非正整数 **400**,不存在 **404**。
* ``DELETE /api/trip-plans/{id}`` —— 删除方案;只删方案行,引用的收藏不动。

金额口径:总花费**不重新调 ``/api/routes``**,只按 ``Collection`` 快照的"当时口径"相加
(交通 = 路线收藏的 ``cost_cny`` 之和;住宿 = 价下限均值 × 晚数),所以响应里的
``quote.kind`` 恒为 ``estimate`` 且带 ``note`` —— 估算不是报价(见 :mod:`services.trips`)。
引用的收藏被删掉时**不连带删方案**,报价把缺行按"已删除"列进 ``quote.missing``。

校验口径与 ``/api/collections`` 一致:缺参 / 非法一律 **400 + 中文报错**,所以请求体故意
收成裸 JSON 对象(``Body(None)``)而不是 pydantic 模型 —— 否则字段类型不对会被 FastAPI
拦成 422 英文报错,同一个"参数不对"就有两种状态码。本模块**不触网**:只读写 SQLite。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from db.base import get_session
from services import trips as trip_service

router = APIRouter()

MAX_PAGE = 200  # 单次列表上限:前端传更大的 limit 也按这个截断(与 collections 同口径)

TRIP_PLANS_NOTE = (
    "方案是收藏的组合:引用 collections.id 但不建外键,删收藏不连带删方案,"
    "报价时缺行按“已删除”列进 quote.missing。"
    "总花费按收藏快照的当时口径估算(交通 = 各路线收藏 cost_cny 之和,缺项跳过;"
    "住宿 = 各处价下限均值 × 晚数,有区间再给上限档),不重新调 /api/routes,"
    "quote.kind 恒为 estimate,仅供参考。"
    "唯一键是方案名:同名再次提交 = 刷新引用并返回原行(created=false),幂等。"
)
DELETE_NOTE = "只删这份方案,不影响收藏(Collection)、目的地库(Place)与住宿库(Stay)。"


def _optional_text(value: Any) -> Optional[str]:
    """可选文本参数归一:只认真正的 ``str``。

    直接调用端点函数(单测 / 脚本)时,FastAPI 的 ``Query(None)`` 默认值不会被解析,
    传进来是 ``FieldInfo`` 对象;这里统一把"没给"归一成 ``None``(同 ``app.api.collections``)。
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
        raise HTTPException(400, "缺少请求体:POST /api/trip-plans 需要 JSON 对象")
    if not isinstance(payload, Mapping):
        raise HTTPException(
            400, f"请求体必须是 JSON 对象,收到:{type(payload).__name__}"
        )
    return dict(payload)


def _nights_of(body: Mapping[str, Any]) -> int:
    """晚数归一:没给按 :data:`services.trips.DEFAULT_NIGHTS`;越界 / 非整数 → **400**。"""
    try:
        return trip_service.resolve_nights(body.get("nights"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _plan_id(raw_id: str) -> int:
    """路径里的方案 id 归一:缺 / 非整数 / 非正数 → **400**(中文报错)。"""
    resolved = _optional_int("方案 id", raw_id)
    if resolved is None:
        raise HTTPException(400, f"缺少必要参数:方案 id(收到:{raw_id!r})")
    return resolved


def _quote_of(session: Session, row: Any, *, nights: Optional[int] = None) -> dict[str, Any]:
    """给一行方案算报价;晚数非法转 **400**(其余异常照 :mod:`services.trips` 的口径抛)。"""
    try:
        return trip_service.quote_plan(
            session,
            row.place_collection_id,
            row.route_collection_ids,
            row.stay_collection_ids,
            nights=trip_service.DEFAULT_NIGHTS if nights is None else nights,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/trip-plans")
def create_trip_plan(
    payload: Any = Body(
        None,
        description="方案 JSON 对象:name / note / place_collection_id / "
        "route_collection_ids / stay_collection_ids / nights",
    ),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """新建 / 刷新一份行程方案;**重名幂等**(刷新引用、返回原行,``created=false``)。"""
    started = time.monotonic()
    body = _payload_dict(payload)
    nights = _nights_of(body)
    try:
        row, created = trip_service.upsert_trip_plan(
            session,
            name=body.get("name"),
            note=_optional_text(body.get("note")),
            place_collection_id=body.get("place_collection_id"),
            route_collection_ids=body.get("route_collection_ids"),
            stay_collection_ids=body.get("stay_collection_ids"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    quote = _quote_of(session, row, nights=nights)
    return {
        "trip_plan": trip_service.trip_plan_to_dict(row, quote=quote),
        "quote": quote,
        "created": created,
        "idempotent": not created,
        "nights": nights,
        "count": trip_service.count_trip_plans(session),
        "elapsed_s": round(time.monotonic() - started, 2),
        "note": TRIP_PLANS_NOTE,
    }


@router.get("/trip-plans")
def list_trip_plans(
    limit: Optional[str] = Query(None, description=f"最多返回几条(正整数,上限 {MAX_PAGE}),留空不限"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """方案列表(**新的在前**),每项带 ``counts`` 与按 1 晚算的 ``quote``。"""
    started = time.monotonic()
    resolved_limit = _optional_int("limit", limit)
    if resolved_limit is not None:
        resolved_limit = min(resolved_limit, MAX_PAGE)
    items = trip_service.list_trip_plans(session, limit=resolved_limit)
    return {
        "trip_plans": items,
        "count": len(items),
        "limit": resolved_limit,
        "total": trip_service.count_trip_plans(session),
        "nights": trip_service.DEFAULT_NIGHTS,
        "kind": trip_service.QUOTE_KIND,
        "elapsed_s": round(time.monotonic() - started, 2),
        "note": TRIP_PLANS_NOTE,
    }


@router.get("/trip-plans/{trip_plan_id}")
def get_trip_plan(
    trip_plan_id: str,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """单份方案详情(含 ``quote``);id 非正整数 **400**,查不到 **404**(不静默成功)。"""
    started = time.monotonic()
    resolved = _plan_id(trip_plan_id)
    row = trip_service.get_trip_plan(session, trip_plan_id=resolved)
    if row is None:
        raise HTTPException(404, f"行程方案不存在:id={resolved}")
    quote = _quote_of(session, row)
    return {
        "trip_plan": trip_service.trip_plan_to_dict(row, quote=quote),
        "quote": quote,
        "nights": quote["stay_nights"],
        "count": trip_service.count_trip_plans(session),
        "elapsed_s": round(time.monotonic() - started, 2),
        "note": TRIP_PLANS_NOTE,
    }


@router.delete("/trip-plans/{trip_plan_id}")
def delete_trip_plan(
    trip_plan_id: str,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """删除一份方案;id 非正整数 **400**,查不到 **404**;引用的收藏不动。"""
    resolved = _plan_id(trip_plan_id)
    deleted = trip_service.delete_trip_plan(session, trip_plan_id=resolved)
    if not deleted:
        raise HTTPException(404, f"行程方案不存在:id={resolved}")
    session.commit()
    return {
        "deleted": True,
        "id": resolved,
        "count": trip_service.count_trip_plans(session),
        "note": DELETE_NOTE,
    }
