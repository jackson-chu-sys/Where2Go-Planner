"""住宿 API(TASK-3a2,阶段3a **路由层**):``GET /api/stays`` —— 某坐标半径内的落脚点。

刻意做成**薄路由**:检索 / 入库 / LLM 估价 / DB 即缓存全在 :mod:`services.stays`(TASK-3a1),
本模块只干三件事 ——

1. 参数校验:缺参 / 非法一律 **400 + 中文报错**(与 ``/api/places``、``/api/collections``
   同口径)。所以数字与布尔参数一律收成 ``Optional[str]`` 再自己解析,**不用** ``float``/``bool``
   的 Query 类型 —— 否则 FastAPI 会把"参数不对"拦成 422 英文报错,同一个错误就有两种状态码。
2. 单位换算:对外 ``radius_km``(前端/用户友好),对内换成服务层要的**米**。
3. 出参投影:把服务层的行裁成前端要的形状(丢掉 ``tags``/``fetched_at`` 等内部字段),
   并给每行补上估算标注 ``estimated``。

起点**二选一**:``lat`` + ``lng``(地图中心 / 浏览器定位),或 ``place_id``
(已入库目的地 :class:`db.models.Place` 的 id,坐标从库里取 —— 前端点了卡片就能直接查周边住宿)。
两者同时给 → **400**:口径必须唯一,否则"到底以谁为准"会变成前后端各自的猜测;都不给 → **400**。

价格是 **AI 估算**,不是报价:每行带 ``estimated`` 标注,顶层 ``note`` 写明参考价口径
(架构文档"AI 幻觉"对策:事实字段绑结构化来源,估算字段必须自带标注)。
本模块自己不触网,触网的是服务层(**高德** ``place/around`` 检索 + LLM 估价,TASK-9b),
测试里可整体替换。**响应键名与形状零变化**(``source`` 的值随数据源改成 ``amap``)。

TASK-6c 的三件透传(**只在空结果/降级/后台估价中/阶梯扩档时出现**,正常结果的顶层形状
保持原样,前端老代码零改动):

* ``reason`` —— 三档空态:``no_data``(真的没有)/ ``datasource_error``(高德报错,
  可重试)/ ``timeout``(超时,稍后再试);正常结果为 ``null``。
* ``nearest_km`` —— 最近一家的直线距离(km,1 位);空结果时是**库里已知**的最近一家,
  给"最近的在 X km 外"文案用。
* ``estimating`` —— 价格正在后台批量回填(``price_estimate`` 此刻可能是 null),重查即得。

TASK-6g:每行多透出一个 ``price_kind`` —— ``"rule"`` = 价格来自品牌/星级/类型规则表
(0 token、离线算的),``"llm"`` = 批量 LLM 估的,``null`` = 还没估出来。两者**都是估算**
(``estimated`` 标注照旧),前端据此可以分文案("参考价规则" vs "AI 预估")。

半径口径:调用方**没给** ``radius_km`` 时传 ``radius_m=None`` 给服务层,由它按
5→10→30 km 阶梯自动扩(空结果才扩);给了就只查那一档。``radius_km`` 出参恒回显
"调用方要的/默认的"公里数,不因阶梯扩档而变(前端的半径选择器与出参一一对应)。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from db import models
from db.base import get_session
from services import stays as stay_service

router = APIRouter()

DEFAULT_RADIUS_KM = 8.0  # 周末出行的落脚点半径:默认 8 km(与 services.stays 的 8000 m 同口径)
MAX_RADIUS_KM = 30.0  # 上限:住宿是"到了目的地再找",超过 30 km 已经不是同一个落脚点市场
METERS_PER_KM = 1000.0
TRUE_WORDS = frozenset({"1", "true", "t", "yes", "y", "on"})
FALSE_WORDS = frozenset({"0", "false", "f", "no", "n", "off"})

# 每行的估算标注:价格与简介都是 LLM 产物,标注必须跟着数据走(前端不必再自己拼免责声明)
ESTIMATED_LABEL = "AI 预估 · 仅供参考 · 以 OTA 实时为准"
# 出参投影白名单:服务层的行里 ``tags``/``price_is_estimate``/``fetched_at``/``source``
# 是给夜间任务与排查用的,住宿卡片不需要,裁掉免得前端误当成事实字段
ITEM_KEYS: tuple[str, ...] = (
    "id", "osm_type", "osm_id", "name", "kind", "lat", "lng",
    "distance_km", "price_estimate", "price_kind", "currency", "intro",
)
STAYS_NOTE = (
    "价格与简介是 AI 依据名称/住宿类型/星级等标签给出的**参考价估算**"
    "(形如 约¥A-B/晚,人民币、一晚),不是实时报价,下单前请以携程/Booking/Agoda 等 OTA 实时价格为准;"
    "估不出来的行 price_estimate 为 null(不编数字)。"
    "price_kind 标明价格出处:rule = 命中品牌/星级/类型规则表(离线算的,不花 token),"
    "llm = AI 估的,null = 还没估出来;两者都是估算口径。"
    "distance_km 是距起点的大圆直线距离,不是步行/驾车里程。"
    "DB 即缓存:该坐标半径内已入库就直接读库(source=db,零次网络与 LLM),"
    "否则现场检索高德 place/around(types=100000 住宿服务大类)并入库估价(source=amap,"
    "osm_type=amap、osm_id 为高德 POI id 的 crc32、原文在 tags.amap_id);"
    "refresh=true 强制重抓,但已有价格的行不会再调 LLM(不重复花 token)。"
)
# 空结果/降级时追加的口径说明(TASK-6c):三档 reason 怎么读、nearest_km 是什么、
# estimating 为什么要等一会儿。正常结果不带这段,免得卡片下方多一坨没人看的字。
STAYS_DEGRADED_NOTE = (
    "本次为空结果或降级返回,附带 reason/nearest_km/estimating 三个判别字段:"
    "reason=no_data 表示该半径内确实没有住宿(nearest_km 是库里已知的最近一家距离,单位 km,"
    "为 null 表示阶梯最大档内也没有);reason=datasource_error 表示高德检索报错,可重试;"
    "reason=timeout 表示检索超时,请稍后再试。"
    "空结果与失败都会写负缓存,6 小时内同坐标同半径直接回缓存态、不再重复触网。"
    "estimating=true 表示价格正在后台批量回填(每批 5 家一次 LLM 调用,不阻塞本请求),"
    "此刻 price_estimate 可能为 null,稍后重查同一坐标即可拿到已回填的估算价。"
)


def _optional_text(value: Any) -> Optional[str]:
    """可选文本参数归一:只认真正的 ``str``(同 :mod:`app.api.collections`)。

    直接调用端点函数(单测 / 脚本)时,FastAPI 的 ``Query(None)`` 默认值不会被解析,
    传进来是 ``FieldInfo`` 对象;这里统一把"没给"归一成 ``None``。
    """
    return value if isinstance(value, str) else None


def _optional_float(name: str, value: Any) -> Optional[float]:
    """可选数字参数归一:没给 → ``None``;非数字 → **400**(中文报错)。"""
    text = _optional_text(value)
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise HTTPException(400, f"参数 {name} 必须是数字,收到:{value!r}") from exc


def _optional_id(name: str, value: Any) -> Optional[int]:
    """可选正整数 id 归一:没给 → ``None``;非整数 / 非正数 → **400**(中文报错)。"""
    number = _optional_float(name, value)
    if number is None:
        return None
    if number != int(number) or number <= 0:
        raise HTTPException(400, f"参数 {name} 必须是正整数,收到:{value!r}")
    return int(number)


def _resolve_radius_km(value: Any) -> Optional[float]:
    """半径归一(公里):没给 → ``None``(服务层走 5→10→30 km 阶梯);≤0 或超上限 → **400**。"""
    radius = _optional_float("radius_km", value)
    if radius is None:
        return None
    if radius != radius or radius in (float("inf"), float("-inf")):  # NaN / inf
        raise HTTPException(400, f"参数 radius_km 非法,收到:{value!r}")
    if radius <= 0:
        raise HTTPException(400, f"参数 radius_km 必须大于 0,收到:{value!r}")
    if radius > MAX_RADIUS_KM:
        raise HTTPException(
            400, f"参数 radius_km 超出上限 {MAX_RADIUS_KM:g} km,收到:{value!r}"
        )
    return radius


def _resolve_flag(name: str, value: Any) -> bool:
    """布尔参数归一:没给 → ``False``;认 ``1/true/yes/on``(大小写不限),其余非假值 → **400**。"""
    text = _optional_text(value)
    if text is None:
        return False
    text = text.strip().lower()
    if not text or text in FALSE_WORDS:
        return False
    if text in TRUE_WORDS:
        return True
    raise HTTPException(
        400, f"参数 {name} 必须是布尔值(true/false),收到:{value!r}"
    )


def _resolve_origin(
    session: Session,
    *,
    lat: Optional[float],
    lng: Optional[float],
    place_id: Optional[int],
) -> tuple[float, float]:
    """起点二选一:``place_id`` 从 :class:`db.models.Place` 取坐标,否则用 ``lat`` + ``lng``。

    都缺 / 都给 / 只给半个 / 越界 → **400**;``place_id`` 查不到 → **404**(不静默成功)。
    """
    if place_id is not None and (lat is not None or lng is not None):
        raise HTTPException(
            400, "起点只能二选一:要么给 lat+lng,要么给 place_id,不能同时给"
        )
    if place_id is not None:
        place = session.get(models.Place, place_id)
        if place is None:
            raise HTTPException(404, f"目的地不存在:place_id={place_id}")
        pair = stay_service.coordinate_pair(place.lat, place.lng)
        if pair is None:
            raise HTTPException(400, f"目的地坐标非法,不能当起点:place_id={place_id}")
        return pair
    if lat is None and lng is None:
        raise HTTPException(400, "缺少必要参数:lat+lng 或 place_id(二者至少给一组)")
    if lat is None or lng is None:
        raise HTTPException(400, "参数 lat 与 lng 必须同时给出")
    pair = stay_service.coordinate_pair(lat, lng)
    if pair is None:
        raise HTTPException(
            400, f"坐标非法:lat={lat!r}, lng={lng!r}(应在 -90~90 / -180~180 之间)"
        )
    return pair


def _item(row: Mapping[str, Any]) -> dict[str, Any]:
    """服务层的一行 → 出参形状(白名单投影 + 估算标注)。"""
    item: dict[str, Any] = {key: row.get(key) for key in ITEM_KEYS}
    item["estimated"] = ESTIMATED_LABEL
    return item


@router.get("/stays")
def list_stays(
    lat: Optional[str] = Query(None, description="起点纬度(-90~90);与 place_id 二选一"),
    lng: Optional[str] = Query(None, description="起点经度(-180~180);与 place_id 二选一"),
    place_id: Optional[str] = Query(None, description="已入库目的地 id(用它的坐标当起点);与 lat/lng 二选一"),
    radius_km: Optional[str] = Query(
        None,
        description=(
            f"检索半径(公里,上限 {MAX_RADIUS_KM:g});不给则由服务层按 5→10→30 km 阶梯自动扩,"
            f"出参 radius_km 回显 {DEFAULT_RADIUS_KM:g}"
        ),
    ),
    refresh: Optional[str] = Query(None, description="true = 忽略库缓存强制重抓(会触网)"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """起点半径内的住宿列表(**由近及远**),含 AI 价格区间估算与一句话简介。"""
    origin_lat, origin_lng = _resolve_origin(
        session,
        lat=_optional_float("lat", lat),
        lng=_optional_float("lng", lng),
        place_id=_optional_id("place_id", place_id),
    )
    radius = _resolve_radius_km(radius_km)
    # 出参恒回显"调用方要的/默认的"半径;没给半径时服务层按阶梯自己扩,回显值不跟着变
    echo_radius_km = DEFAULT_RADIUS_KM if radius is None else radius
    radius_m = None if radius is None else int(round(radius * METERS_PER_KM))
    force_refresh = _resolve_flag("refresh", refresh)
    rows = stay_service.load_or_fetch_stays(
        session,
        origin_lat,
        origin_lng,
        radius_m=radius_m,
        refresh=force_refresh,
    )
    items = [_item(row) for row in rows]
    # 空结果没有可归属的行,按 db 口径报(既没抓到也没读到,不谎报 amap)
    source = str(rows[0].get("source") or stay_service.SOURCE_DB) if rows else stay_service.SOURCE_DB
    reason = getattr(rows, "reason", None)
    nearest_km = getattr(rows, "nearest_km", None)
    estimating = bool(getattr(rows, "estimating", False))
    expanded = bool(getattr(rows, "expanded", False))
    body: dict[str, Any] = {
        "lat": origin_lat,
        "lng": origin_lng,
        "radius_km": echo_radius_km,
        "count": len(items),
        "source": source,
        "note": STAYS_NOTE,
        "items": items,
    }
    # 三档空态/后台估价中/阶梯扩过档 → 多给三个判别字段(正常结果的顶层形状保持不变)
    if reason is not None or estimating or expanded or not items:
        note = STAYS_NOTE + STAYS_DEGRADED_NOTE
        effective_m = getattr(rows, "radius_m", None)
        if expanded and effective_m:
            note += f"本次未给半径,已按 5→10→30 km 阶梯自动扩到 {effective_m / METERS_PER_KM:g} km 检索。"
        body["reason"] = reason
        body["nearest_km"] = nearest_km
        body["estimating"] = estimating
        body["note"] = note
    return body
