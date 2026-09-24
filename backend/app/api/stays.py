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
本模块自己不触网,触网的是服务层(Overpass 检索 + LLM 估价),测试里可整体替换。
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
    "distance_km", "price_estimate", "currency", "intro",
)
STAYS_NOTE = (
    "价格与简介是 AI 依据名称/住宿类型/星级等 OSM 标签给出的**参考价估算**"
    "(形如 约¥A-B/晚,人民币、一晚),不是实时报价,下单前请以携程/Booking/Agoda 等 OTA 实时价格为准;"
    "估不出来的行 price_estimate 为 null(不编数字)。"
    "distance_km 是距起点的大圆直线距离,不是步行/驾车里程。"
    "DB 即缓存:该坐标半径内已入库就直接读库(source=db,零次网络与 LLM),"
    "否则现场检索 Overpass 并入库估价(source=overpass);"
    "refresh=true 强制重抓,但已有价格的行不会再调 LLM(不重复花 token)。"
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


def _resolve_radius_km(value: Any) -> float:
    """半径归一(公里):没给 → :data:`DEFAULT_RADIUS_KM`;≤0 或超上限 → **400**。"""
    radius = _optional_float("radius_km", value)
    if radius is None:
        return DEFAULT_RADIUS_KM
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
        None, description=f"检索半径(公里,默认 {DEFAULT_RADIUS_KM:g},上限 {MAX_RADIUS_KM:g})"
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
    force_refresh = _resolve_flag("refresh", refresh)
    rows = stay_service.load_or_fetch_stays(
        session,
        origin_lat,
        origin_lng,
        radius_m=int(round(radius * METERS_PER_KM)),
        refresh=force_refresh,
    )
    items = [_item(row) for row in rows]
    # 空结果没有可归属的行,按 db 口径报(既没抓到也没读到,不谎报 overpass)
    source = str(rows[0].get("source") or stay_service.SOURCE_DB) if rows else stay_service.SOURCE_DB
    return {
        "lat": origin_lat,
        "lng": origin_lng,
        "radius_km": radius,
        "count": len(items),
        "source": source,
        "note": STAYS_NOTE,
        "items": items,
    }
