"""AI 行程规划 API(TASK-10b,**阶段 A**):``/api/planner/*`` 的对话三件套 + 「存为行程方案」。

四条路由(编排全在 :mod:`services.planner`,本模块只做**参数归一 / 素材清单 / 落库**):

* ``POST /api/planner/messages`` —— 发一轮对话。``session_key`` 空则后端补发 ``uuid4().hex``;
  ``collection_ids`` **给了就只把这几条收藏当素材**,没给(键缺省 / ``null``)则取当前**全部**
  收藏 —— 两种情况都受 ``planner.SYSTEM_PROMPT`` 的硬约束「未点名一律不得纳入」管着,
  素材清单只是"可选素材"。响应是 :func:`services.planner.plan_turn` 的形状**原样透出**
  (键名逐字,前端只读这些键,所以这里**不加** ``note``/``elapsed_s``)。
* ``GET /api/planner/messages?session_key=&limit=`` —— 回放历史(``limit`` 默认 50、上限 200);
  **未知 session → ``items: []``**(不 404:刷新页面时前端的 ``localStorage`` key 可能已失效)。
* ``DELETE /api/planner/messages?session_key=`` —— 「🗑 清空对话」:只删消息、**会话行保留**
  (``session_key`` 继续可用),未知 key → ``deleted: 0``,同样不 404。
* ``POST /api/planner/save`` —— 「⭐ 存为行程方案」:把 AI 排的地点落成既有
  :class:`~db.models.TripPlan`(复用它的报价与列表),见下。

``save`` 的口径(契约 §3 TASK-10b 逐字):

1. 没有 ``collection_id`` 的 stop → 在库内 :class:`~db.models.Place` 按**名字**匹配:
   精确同名优先 → 库内名包含它 → 它包含库内名;多命中取**入库最前**的一条(``id`` 最小,
   请求体里没有起点坐标,所以用不了"距离最近");命中不到 → 进 ``unmatched``。
2. 命中的地点**幂等建收藏**:``kind="place"``、``mode`` 用空串(:data:`~db.models.NO_MODE`,
   不用 NULL)、``ref_key`` 走既有 ``osm_key(type,id)`` 口径、坐标定点
   :data:`~db.models.COORD_PRECISION`(7 位)—— 全部交给
   :func:`db.repository.upsert_collection`,所以同名重复保存**不产生新行**。
3. 这些收藏 id + 传入的 ``legs``/``stay``/``nights`` 调**既有**
   :func:`services.trips.upsert_trip_plan`(唯一键 = 方案名,同名 = 刷新幂等)。
   ⚠️ ``TripPlan.place_collection_id`` 只有**一个**目的地引用列(不改表结构),所以方案里
   挂的是**第一站**;其余命中的地点照样建成收藏,在收藏面板里可见。
4. 一个地点都没匹配上 → **400 中文**(提示先去目的地列表里搜到它们)。

校验口径与 ``/api/trip-plans``、``/api/collections`` 一致:缺参 / 非法一律 **400 + 中文报错**,
所以请求体故意收成裸 JSON 对象(``Body(None)``)而不是 pydantic 模型 —— 否则字段类型不对会被
FastAPI 拦成 422 英文报错,同一个"参数不对"就有两种状态码。
本模块**不触网**(除了 ``plan_turn`` 里那一次 LLM chat),其余只读写 SQLite。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import literal, select
from sqlalchemy.orm import Session

from db import models
from db import repository as repo
from db.base import get_session
from services import planner as planner_service
from services import trips as trip_service

router = APIRouter()

DEFAULT_PAGE = planner_service.LIST_LIMIT_DEFAULT  # 50:GET 历史消息的默认条数
MAX_PAGE = planner_service.LIST_LIMIT_MAX          # 200:前端传更大也按这个截断(与 trips 同口径)
BRIEF_SUMMARY_LEN = planner_service.BRIEF_SUMMARY_LEN  # 120:素材摘要截断长度
LIKE_ESCAPE = "\\"

NO_MATCH_DETAIL = "行程里的地点都不在库内,先在目的地列表里搜到它们再试"
SAVE_NOTE = (
    "存为方案 = 把 AI 排的地点落成既有 TripPlan:方案名是唯一键,同名再存 = 刷新引用(created=false)。"
    "TripPlan 只有一个目的地引用列(place_collection_id),所以方案里挂的是第一站;"
    "其余命中的地点照样幂等建成 kind=place 的收藏(mode 空串、ref_key 走 OSM 身份、坐标定点 7 位),"
    "在收藏面板里可见,重存不产生新行。"
    "legs/stay 原样进 route_collection_ids/stay_collection_ids;"
    "总花费按收藏快照的当时口径估算(quote.kind 恒为 estimate,仅供参考,不重新调 /api/routes)。"
    "unmatched 是库内找不到同名地点的站名:先在目的地列表里搜到它们(入库)再存,或直接给 collection_id。"
)


def _optional_text(value: Any) -> Optional[str]:
    """可选文本参数归一:只认真正的 ``str``。

    直接调用端点函数(单测 / 脚本)时,FastAPI 的 ``Query(None)`` 默认值不会被解析,
    传进来是 ``FieldInfo`` 对象;这里统一把"没给"归一成 ``None``(同 ``app.api.trips``)。
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


def _payload_dict(payload: Any, *, endpoint: str) -> dict[str, Any]:
    """请求体归一:必须是 JSON 对象(``{...}``),否则 **400**(中文报错)。"""
    if payload is None:
        raise HTTPException(400, f"缺少请求体:{endpoint} 需要 JSON 对象")
    if not isinstance(payload, Mapping):
        raise HTTPException(
            400, f"请求体必须是 JSON 对象,收到:{type(payload).__name__}"
        )
    return dict(payload)


def _required_session_key(value: Any) -> str:
    """``session_key`` 归一(GET / DELETE 用):缺 / 空白 → **400**(中文报错)。

    这两个端点**不补发** key:补发会让前端拿着一个空会话继续问,历史反而对不上。
    """
    key = models.clean_text(value, limit=models.SESSION_KEY_LEN)
    if not key:
        raise HTTPException(
            400, "缺少必要参数:session_key(先 POST /api/planner/messages 拿一个)"
        )
    return key


def _message_of(body: Mapping[str, Any]) -> str:
    """对话内容归一:空 / 非字符串 / 超 :data:`services.planner.MAX_MESSAGE_LEN` → **400**。"""
    raw = body.get("message")
    if raw is not None and not isinstance(raw, str):
        raise HTTPException(400, f"参数 message 必须是字符串,收到:{type(raw).__name__}")
    text = models.clean_text(raw)
    if not text:
        raise HTTPException(400, "缺少必要参数:message(对话内容不能为空)")
    if len(text) > planner_service.MAX_MESSAGE_LEN:
        raise HTTPException(
            400,
            f"message 过长:{len(text)} 字,上限 {planner_service.MAX_MESSAGE_LEN} 字(请分几轮说)",
        )
    return text


def _collection_ids_of(raw: Any) -> Optional[list[int]]:
    """点名收藏 id 归一:``None`` = **没点名**(素材取当前全部收藏);数组 → 正整数列表(保序去重)。

    空数组 ``[]`` 与 ``None`` **不是一回事**:空数组 = 用户主动不点名任何收藏(素材清单为空),
    ``None`` = 前端没带这个字段(按契约取全部收藏当"可选素材")。
    不是数组 / 元素不是正整数 → **400 中文**(与 ``/api/trip-plans`` 的引用数组同口径)。
    """
    if raw is None:
        return None
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise HTTPException(
            400, f"参数 collection_ids 必须是整数数组,收到:{type(raw).__name__}"
        )
    resolved: list[int] = []
    for item in raw:
        number = _optional_int("collection_ids", item)
        if number is None:
            raise HTTPException(400, f"参数 collection_ids 必须是正整数数组,收到:{raw!r}")
        if number not in resolved:
            resolved.append(number)
    return resolved


def _brief_summary(item: Mapping[str, Any]) -> str:
    """一条收藏 → **一行中文素材摘要**(只说"是什么 / 在哪 / 多久",不塞费用)。

    阶段 A 不出交通报价与住宿价(``SYSTEM_PROMPT`` 硬约束 2),所以快照里的 ``cost_cny`` /
    ``price_estimate`` **不进 prompt** —— 免得模型顺手把价格写进行程里;时长与里程是
    "顺不顺路"的判断依据,留着。字段口径与 ``/api/collections`` 的响应完全一致。
    """
    summary = item.get("summary")
    snapshot = summary if isinstance(summary, Mapping) else {}
    if item.get("kind") == models.KIND_ROUTE:
        origin = item.get("from_name") or models.COLLECTION_ORIGIN_LABEL
        target = item.get("to_name") or item.get("name") or ""
        head = f"路线:{origin} → {target}"
        if item.get("mode"):
            head = f"{head}({item['mode']})"
    else:
        head = f"目的地:{item.get('to_name') or item.get('name') or ''}"
    pieces = [head]
    if snapshot.get("duration_min") is not None:
        pieces.append(f"约 {snapshot['duration_min']} 分钟")
    if snapshot.get("distance_km") is not None:
        pieces.append(f"{snapshot['distance_km']} 公里")
    if item.get("cat_name"):
        pieces.append(f"分组「{item['cat_name']}」")
    return " · ".join(piece for piece in pieces if piece)


def _briefs_of(session: Session, wanted: Optional[list[int]]) -> list[dict[str, Any]]:
    """素材清单(``{"id","kind","title","summary"}``):点名了只取这几条,没点名取**全部收藏**。

    走 :func:`db.repository.list_collections`(分组名批量查回,不 N+1),再按点名顺序挑;
    库里查不到的 id **静默跳过**(收藏可能刚被删掉,不该为一轮对话报 400)。
    """
    items = repo.list_collections(session)
    if wanted is None:
        chosen = items
    else:
        by_id = {item.get("id"): item for item in items}
        chosen = [by_id[collection_id] for collection_id in wanted if collection_id in by_id]
    return [
        {
            "id": item.get("id"),
            "kind": item.get("kind") or "",
            "title": item.get("name") or "",
            "summary": _brief_summary(item)[:BRIEF_SUMMARY_LEN],
        }
        for item in chosen
    ]


def _like_pattern(name: str) -> str:
    """包含匹配的 LIKE 模式:转义 ``%`` / ``_`` / ``\\``,免得地名里的下划线变成通配符。"""
    escaped = (
        name.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


def _first_place(session: Session, stmt: Any) -> Optional[models.Place]:
    """执行一个地点查询,取**入库最前**的一条(``id`` 最小 → 结果稳定可复现)。"""
    return session.scalars(stmt.order_by(models.Place.id).limit(1)).first()


def _match_place(session: Session, name: str) -> Optional[models.Place]:
    """按名字在库内找地点:**精确同名 → 库内名包含它 → 它包含库内名**;都没有 → ``None``。

    三级降级是因为 AI 给的站名常常不完全等于库内名(「西湖」vs「杭州西湖风景名胜区」);
    第三级(站名包含库内名)用 ``instr`` 语义的 LIKE 反查,库内名里的 ``%``/``_`` 极少,
    退化成通配也不影响"取 id 最小"的确定性。
    """
    wanted = models.clean_text(name, limit=models.NAME_LEN)
    if not wanted:
        return None
    place = models.Place
    exact = _first_place(session, select(place).where(place.name == wanted))
    if exact is not None:
        return exact
    contains = _first_place(
        session,
        select(place).where(
            place.name.like(_like_pattern(wanted), escape=LIKE_ESCAPE)
        ),
    )
    if contains is not None:
        return contains
    return _first_place(
        session, select(place).where(literal(wanted).like("%" + place.name + "%"))
    )


def _place_collection(session: Session, place: models.Place) -> models.Collection:
    """把一个库内地点**幂等**建成目的地收藏(``kind=place``、``mode`` 空串),返回收藏行。

    ``ref_key`` 由 :func:`db.repository.upsert_collection` 按既有
    :func:`db.models.collection_ref_key` 算(``place:{osm_type}/{osm_id}``,OSM 身份优先、
    退化到坐标定点串),所以同一个地点存两次拿到的是**同一行**(唯一键 ``(kind, ref_key, mode)``)。
    坐标再定点 :data:`~db.models.COORD_PRECISION` 一次:入库时已经定过,这里显式复述口径,
    免得日后有人直接塞未定点的浮点进来把幂等打掉。
    """
    row, _created = repo.upsert_collection(
        session,
        kind=models.KIND_PLACE,
        mode=models.NO_MODE,
        name=place.name,
        osm_type=place.osm_type,
        osm_id=place.osm_id,
        to_lat=round(float(place.lat), models.COORD_PRECISION),
        to_lng=round(float(place.lng), models.COORD_PRECISION),
        to_name=place.name,
    )
    return row


def _stops_of(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """行程停靠点归一:``[{"name","collection_id"|null}]``;缺 / 空 / 非法 → **400 中文**。"""
    raw = body.get("stops")
    if raw is None:
        raise HTTPException(400, "缺少必要参数:stops(行程里的地点数组)")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise HTTPException(400, f"参数 stops 必须是 JSON 数组,收到:{type(raw).__name__}")
    stops: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise HTTPException(
                400, f"参数 stops 的每个元素必须是 JSON 对象,收到:{type(item).__name__}"
            )
        name = models.clean_text(item.get("name"), limit=models.NAME_LEN)
        if not name:
            raise HTTPException(400, "参数 stops 里每个地点都要有 name(地点名不能为空)")
        stops.append({"name": name, "collection_id": _stop_collection_id(item.get("collection_id"))})
    if not stops:
        raise HTTPException(400, "缺少必要参数:stops(行程里至少要有一个地点)")
    return stops


def _stop_collection_id(raw: Any) -> Optional[int]:
    """stop 里**点名**的收藏 id:``None``/空串 = 没点名(走库内名字匹配);非法 → **400**。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    resolved = _optional_int("stops[].collection_id", raw)
    if resolved is None:
        raise HTTPException(400, f"参数 stops[].collection_id 必须是正整数,收到:{raw!r}")
    return resolved


def _place_id_of_collection(session: Session, row: Optional[models.Collection]) -> Optional[int]:
    """收藏 → 它对应的库内地点 id(按 OSM 身份反查);查不到 → ``None``。

    用户点名引用的收藏(行程里带 ``collection_id`` 的 stop)不是按名字匹配出来的,
    但收藏里存着 ``osm_type``/``osm_id``,能反查回 :class:`~db.models.Place` 就把
    ``matched[].place_id`` 补上,前端不必区分"AI 匹配的"与"我点名的"。
    """
    if row is None or not row.osm_type or row.osm_id is None:
        return None
    place = models.Place
    return _first_place(
        session,
        select(place.id).where(place.osm_type == row.osm_type, place.osm_id == row.osm_id),
    )


def _resolve_stops(
    session: Session, stops: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[str], list[int]]:
    """把 AI 排的站点落成收藏引用:返回 ``(matched, unmatched, 收藏 id 列表)``。

    ``matched`` 逐站给 ``{"name","place_id","collection_id"}``(``name`` 是**请求里的站名**,
    库内同名地点可能叫法略有出入);``unmatched`` 是库内匹配不到的站名;
    收藏 id 列表保序去重,第一个进 ``TripPlan.place_collection_id``。
    """
    matched: list[dict[str, Any]] = []
    unmatched: list[str] = []
    collection_ids: list[int] = []
    for stop in stops:
        name = str(stop["name"])
        collection_id = stop["collection_id"]
        place_id: Optional[int] = None
        if collection_id is None:
            place = _match_place(session, name)
            if place is None:
                unmatched.append(name)
                continue
            place_id = int(place.id)
            collection_id = _place_collection(session, place).id
        else:
            named = repo.get_collection(session, collection_id=collection_id)
            place_id = _place_id_of_collection(session, named)
        if collection_id not in collection_ids:
            collection_ids.append(int(collection_id))
        matched.append({"name": name, "place_id": place_id, "collection_id": int(collection_id)})
    return matched, unmatched, collection_ids


def _nights_of(body: Mapping[str, Any]) -> int:
    """晚数归一:没给按 :data:`services.trips.DEFAULT_NIGHTS`;越界 / 非整数 → **400**。"""
    try:
        return trip_service.resolve_nights(body.get("nights"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _quote_of(session: Session, row: Any, *, nights: int) -> dict[str, Any]:
    """给方案行算报价;晚数非法转 **400**(其余异常照 :mod:`services.trips` 的口径抛)。"""
    try:
        return trip_service.quote_plan(
            session,
            row.place_collection_id,
            row.route_collection_ids,
            row.stay_collection_ids,
            nights=nights,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/planner/messages")
def post_message(
    payload: Any = Body(
        None,
        description="对话 JSON 对象:session_key / message / collection_ids / nights",
    ),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """发一轮 AI 行程对话;响应是 :func:`services.planner.plan_turn` 的形状**原样透出**。

    降级四态(``no_key`` / ``timeout`` / ``error`` / ``parse_error``)由 planner **不抛**地
    写成 ``degraded=True`` + ``reason`` + 中文 ``reply``,所以这里只有"参数不对"才会 400。
    """
    body = _payload_dict(payload, endpoint="POST /api/planner/messages")
    message = _message_of(body)
    briefs = _briefs_of(session, _collection_ids_of(body.get("collection_ids")))
    try:
        return planner_service.plan_turn(
            session,
            session_key=body.get("session_key"),
            message=message,
            collection_briefs=briefs,
            nights=body.get("nights"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/planner/messages")
def get_messages(
    session_key: Optional[str] = Query(None, description="会话 key(POST /api/planner/messages 返回的)"),
    limit: Optional[str] = Query(None, description=f"最多返回几条(正整数,上限 {MAX_PAGE}),默认 {DEFAULT_PAGE}"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """回放一个会话的历史(**时间正序**);未知 session → ``items: []``(不 404)。"""
    key = _required_session_key(session_key)
    resolved_limit = _optional_int("limit", limit)
    resolved_limit = DEFAULT_PAGE if resolved_limit is None else min(resolved_limit, MAX_PAGE)
    row = planner_service.get_session(session, session_key=key)
    return {
        "session_key": key,
        "title": (row.title if row is not None else "") or "",
        "items": planner_service.list_messages(session, session_key=key, limit=resolved_limit),
    }


@router.delete("/planner/messages")
def delete_messages(
    session_key: Optional[str] = Query(None, description="要清空的会话 key"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """「🗑 清空对话」:只删消息、**会话行保留**;未知 key → ``deleted: 0``(不 404)。"""
    key = _required_session_key(session_key)
    return {"session_key": key, "deleted": planner_service.clear_session(session, session_key=key)}


@router.post("/planner/save")
def save_plan(
    payload: Any = Body(
        None,
        description="存为方案 JSON 对象:session_key / name / stops / legs / stay / nights",
    ),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """把 AI 排的行程落成既有 :class:`~db.models.TripPlan`(**同名 = 刷新幂等**)。

    地点按名字匹配库内 ``Place`` → 幂等建 ``kind=place`` 收藏 → 交给
    :func:`services.trips.upsert_trip_plan`;一个地点都没匹配上 → **400 中文**。
    """
    started = time.monotonic()
    body = _payload_dict(payload, endpoint="POST /api/planner/save")
    stops = _stops_of(body)
    nights = _nights_of(body)
    matched, unmatched, collection_ids = _resolve_stops(session, stops)
    if not collection_ids:
        raise HTTPException(400, NO_MATCH_DETAIL)
    try:
        row, created = trip_service.upsert_trip_plan(
            session,
            name=body.get("name"),
            place_collection_id=collection_ids[0],
            route_collection_ids=body.get("legs"),
            stay_collection_ids=body.get("stay"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    quote = _quote_of(session, row, nights=nights)
    return {
        "trip_plan": trip_service.trip_plan_to_dict(row, quote=quote),
        "matched": matched,
        "unmatched": unmatched,
        "created": created,
        "idempotent": not created,
        "nights": nights,
        "count": trip_service.count_trip_plans(session),
        "elapsed_s": round(time.monotonic() - started, 2),
        "note": SAVE_NOTE,
    }
