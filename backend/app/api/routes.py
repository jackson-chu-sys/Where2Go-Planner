"""路线 API(TASK-2a,阶段2 M2):多方式**时间 + 费用**对比 + 跳转链接。

一条路由:

* ``GET /api/routes?from_lat=&from_lng=&to_lat=&to_lng=&to_name=`` —— 起点/目的地坐标
  (TASK-9c 起全链 GCJ-02 高德口径)→ 驾车(高德 **真实**时长/里程/折线,费用估算)+ 铁路/飞机
  (**全估算**,阈值:铁路 ≥100km、飞机 ≥600km 才出现),每条带 ``kind=real|estimate``、
  ``note``(诚实标注「估算·非实时·以官方为准」)与 ``links``(高德/Google 导航、
  12306 查票、OTA 机票搜索的 deep-link)。
  可选 ``from_name``:12306/OTA 链接的出发地名(留空则只带目的地,由用户在官方页补全)。

**费用引擎 v2(TASK-6d)** 给三种方式都补上可解释口径(响应字段只增不删,
``cost_cny`` / ``kind`` 语义不变):驾车给 ``cost_breakdown{toll, fuel, mode}`` +
``vehicle_label`` / ``per_person_cny``(整车 ≤4 人与人均双标);铁路给 ``price_source``
(``seed`` 命中人工校录的热门城市对票价 / ``estimate`` 分档费率);机票给
``flight_low_cny`` / ``flight_high_cny``(民航公布价 × 典型折扣 ~ 公布价),
``cost_cny`` 取区间中值;直线 <400km 或任一端没有民航机场时**不给票价**(全为 ``null``),
只留 deep-link —— 机票动态定价、浮动大,实时价以跳转为准。

编排在 :mod:`services.routes`,这里只管参数校验与响应封装:缺参/非法坐标一律 **400**
(中文报错,风格同 ``/api/places`` 的 band/category 校验)。四个坐标故意声明成 ``str``
而不是 ``float``:否则范围越界/空值会被 FastAPI 拦成 422 英文报错,同一个"坐标不对"
就有两种状态码;交给 :func:`services.routes.require_coordinates` 统一判定,
缺失、空串、非数字、NaN、越界全是 **400**。高德不可用时驾车条目**降级**
(``degraded=true``、数字为 ``null``)而不是 500;响应里另附 ``mode_rules``(阈值/耗时系数)
与 ``cost_model``(费用系数与算式,v2 起含驾车区域费率表、铁路分档费率/种子对数、
机票公布价分档与折扣),前端标注「估算」时直接引用,不用把系数写死在页面里。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from services import routes as route_service

router = APIRouter()


def _optional_text(value: Any) -> Optional[str]:
    """可选文本参数归一:只认真正的 ``str``。

    直接调用本函数(单测/脚本)时,FastAPI 的 ``Query(None)`` 默认值不会被解析成字符串,
    传进服务层就是 ``FieldInfo`` 对象;这里统一把"没给"归一成 ``None``。
    """
    return value if isinstance(value, str) else None


ROUTES_NOTE = (
    "驾车:高德真实路网时长/里程/折线(非实时路况),费用为估算并给出构成明细 "
    "cost_breakdown{toll, fuel, mode}(油费 = 里程 × 0.08L/km × 油价;过路费 = 收费里程 × "
    "区域费率,收费里程取高德 toll_distance 真实值 → mode=amap_toll_distance,"
    "该字段缺失按 里程×0.55 → mode=heuristic;高德 tolls/cost 个人 key 恒为 0/null 故不用);"
    "口径为整车≤4人,同时给人均 per_person_cny;"
    "高德不可用时该条 degraded=true、数字为 null,不瞎估也不报错。"
    "铁路:票价 = 人工校录的热门城市对种子价(price_source=seed)或 运营里程(直线×1.15)× "
    "分档费率(双高铁枢纽 350km/h 线 0.46 元/km,其余 250km/h 线 0.31 元/km,price_source=estimate);"
    "耗时仍按 直线×1.2 / 均速 + 地面接驳 估算。"
    "机票:民航公布价锚定区间 flight_low_cny~flight_high_cny(公布价 × 典型折扣 ~ 公布价,"
    "主干商务线 0.45 / 支线 0.6),cost_cny 取区间中值;直线 <"
    f"{route_service.FLIGHT_PRICE_MIN_KM:g}km 或任一端城市无民航机场时不给票价(全为 null),"
    "只留 deep-link —— 动态定价·浮动大·实时价以跳转为准。"
    f"出现阈值:铁路 ≥{route_service.RAIL_MIN_KM:g}km、飞机 ≥{route_service.FLIGHT_MIN_KM:g}km;"
    "不抓 12306/OTA 实时价,只用 deep-link 跳转官方(ADR-004/ADR-007)。"
    f"所有价格:{route_service.ESTIMATE_DISCLAIMER}。"
)


@router.get("/routes")
def list_routes(
    from_lat: Optional[str] = Query(None, description="起点纬度(WGS84 数字,[-90, 90]),如 31.2304"),
    from_lng: Optional[str] = Query(None, description="起点经度(WGS84 数字,[-180, 180]),如 121.4737"),
    to_lat: Optional[str] = Query(None, description="目的地纬度(WGS84 数字,[-90, 90])"),
    to_lng: Optional[str] = Query(None, description="目的地经度(WGS84 数字,[-180, 180])"),
    to_name: Optional[str] = Query(None, description="目的地名称(deep-link 文案/URL 用,建议带上)"),
    from_name: Optional[str] = Query(None, description="起点地名(可选):12306/OTA 链接的出发地"),
) -> dict[str, Any]:
    """返回起终点之间的多方式路线数组(驾车始终有,铁路/飞机按距离阈值出现)。"""
    started = time.monotonic()
    try:
        plan = route_service.plan_routes(
            from_lat=from_lat,
            from_lng=from_lng,
            to_lat=to_lat,
            to_lng=to_lng,
            to_name=_optional_text(to_name),
            from_name=_optional_text(from_name),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return {
        "from": plan.origin,
        "to": plan.destination,
        "distance_km": plan.distance_km,
        "routes": plan.routes,
        "count": len(plan.routes),
        "mode_rules": route_service.mode_rules(),
        "cost_model": route_service.cost_coefficients(),
        "generated_at": plan.generated_at,
        "elapsed_s": round(time.monotonic() - started, 2),
        "note": ROUTES_NOTE,
    }
