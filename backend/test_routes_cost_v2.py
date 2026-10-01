"""TASK-6d 单测:**费用引擎 v2**(驾车构成明细 + 整车/人均、铁路分档 + 种子价、机票公布价区间)。

全程离线、全 mock(与 :mod:`backend.test_routes` 一个套路):``no_network`` 把
:meth:`requests.Session.request` 换成直接抛错,任何偷偷联网都会当场失败;驾车在服务层
注入 ``router=`` 替身(TASK-9c 起真实实现是 :func:`data_sources.amap.driving`,
其请求/解析用例见 :mod:`backend.test_amap` 与 :mod:`backend.test_amap_geocode_driving`)。

覆盖任务契约逐条:

* 杭州 → 崇儒乡 驾车给出 ``per_person_cny`` 与 ``cost_breakdown``,且 ``toll_mode`` 的
  两条路径(``amap_toll_distance`` 拿到高德 ``toll_distance``、``heuristic`` 拿不到)都测;
* 上海 → 北京 铁路命中种子价 **553**、``price_source="seed"``;未命中走分档费率
  (双高铁枢纽 0.46、其余 0.31 元/km,运营里程 = 直线 × 1.15)并标 ``"estimate"``;
* 直线 <400km 的城市对**不出机票价**;任一端没有民航机场的城市对也不出机票价
  (条目仍带 deep-link);飞机候选阈值 300km → **600km**;
* ``PUBLISHED_FARE_TIERS`` 三段边界(<812 / 812~1600 / >1600)、主干商务线 0.45 与
  支线 0.6 的折扣区分、``cost_cny`` = 区间中值(旧字段兼容)。

运行:``cd backend && ../.venv/bin/python -m pytest test_routes_cost_v2.py -q``
"""

from __future__ import annotations

import json
import math
import os
import sys
from typing import Any, Callable, Optional

import pytest
import requests

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import routes as routes_api  # noqa: E402
from data_sources import EARTH_RADIUS_KM  # noqa: E402
from services import routes as route_service  # noqa: E402

# --------------------------------------------------------------------------- #
# 样本坐标与驾车替身数据
# --------------------------------------------------------------------------- #

SHANGHAI = {"lat": 31.2304, "lng": 121.4737, "name": "上海"}
HANGZHOU = {"lat": 30.2741, "lng": 120.1551, "name": "杭州"}
CHONGRU = {"lat": 26.9536, "lng": 119.9021, "name": "崇儒乡"}   # 霞浦县崇儒畲族乡:无机场、非枢纽
DEG_PER_KM = 180.0 / (math.pi * EARTH_RADIUS_KM)

# 杭州 → 崇儒乡 的高德 leg:``toll_distance`` 427.2km(G15 + S203 收费路段),
# 里程/时长是高德真实值(米/秒在 :func:`services.routes.amap_leg` 里换成 km/分钟)。
CHONGRU_TOLL_KM = 427.2
CHONGRU_LEG = {
    "distance_km": 452.6,
    "duration_min": 331.4,
    "geometry": [[30.2741, 120.1551], [28.6, 120.0], [26.9536, 119.9021]],
    "toll_distance_km": CHONGRU_TOLL_KM,
}
PLAIN_LEG = {  # 没有 toll_distance 的 leg(替身/字段缺失)→ 过路费只能走启发式
    "distance_km": 452.6,
    "duration_min": 331.4,
    "geometry": [[30.2741, 120.1551], [26.9536, 119.9021]],
}


def point_north(km: float, *, lat: float = SHANGHAI["lat"], lng: float = SHANGHAI["lng"],
                name: Optional[str] = None) -> dict[str, Any]:
    """构造距 ``(lat, lng)`` 正北 ``km`` 公里的点(直线距离由 haversine 复核即 ``km``)。"""
    return {"lat": lat + km * DEG_PER_KM, "lng": lng, "name": name}


def plan_between(origin: dict[str, Any], dest: dict[str, Any], *,
                 leg: Optional[dict[str, Any]] = None, **kwargs: Any):
    """两点之间规划路线(驾车用替身 leg,不触网)。"""
    return route_service.plan_routes(
        from_lat=origin["lat"], from_lng=origin["lng"],
        to_lat=dest["lat"], to_lng=dest["lng"],
        to_name=dest.get("name"), from_name=origin.get("name"),
        router=FakeRouter(leg), **kwargs,
    )


def plan_north(km: float, *, to_name: Optional[str], from_name: Optional[str] = "上海",
               leg: Optional[dict[str, Any]] = None, origin: Optional[dict[str, Any]] = None,
               **kwargs: Any):
    """快捷方式:从 ``origin``(默认上海)往正北 ``km`` 公里处的 ``to_name`` 规划。"""
    start = origin or SHANGHAI
    dest = point_north(km, lat=start["lat"], lng=start["lng"], name=to_name)
    return route_service.plan_routes(
        from_lat=start["lat"], from_lng=start["lng"],
        to_lat=dest["lat"], to_lng=dest["lng"],
        to_name=to_name, from_name=from_name, router=FakeRouter(leg), **kwargs,
    )


def only(plan: Any, mode: str) -> dict[str, Any]:
    """从规划结果里取出某条路线;不存在就报错(断言更直观)。"""
    found = [route for route in plan.routes if route["mode"] == mode]
    assert found, f"{plan.distance_km}km 的规划里没有 {mode} 条目:{[r['mode'] for r in plan.routes]}"
    return found[0]


# --------------------------------------------------------------------------- #
# 测试替身与 fixtures
# --------------------------------------------------------------------------- #


class FakeRouter:
    """驾车路由替身:返回预设 leg(可带 ``toll_distance_km``)或抛预设异常。"""

    def __init__(self, leg: Optional[dict[str, Any]] = None,
                 error: Optional[BaseException] = None) -> None:
        self.leg = dict(PLAIN_LEG if leg is None else leg)
        self.error = error
        self.calls: list[tuple[tuple[float, float], tuple[float, float]]] = []

    def __call__(self, start_lnglat: Any, end_lnglat: Any) -> dict[str, Any]:
        self.calls.append((tuple(start_lnglat), tuple(end_lnglat)))
        if self.error is not None:
            raise self.error
        return dict(self.leg)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底:任何 requests 调用都视为测试失败(本套单测必须纯 mock)。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture(autouse=True)
def default_fuel_price(monkeypatch: pytest.MonkeyPatch) -> None:
    """油价默认走常量 8.0 元/L;要测 env 的用例自己再 setenv。"""
    monkeypatch.delenv(route_service.ENV_FUEL_PRICE, raising=False)


# --------------------------------------------------------------------------- #
# 1. 驾车:收费里程两条路径(amap_toll_distance / heuristic)+ 区域费率 + 油价
# --------------------------------------------------------------------------- #


def test_driving_toll_uses_amap_toll_distance_when_present() -> None:
    """拿到高德 ``toll_distance`` → 过路费按真实收费里程算,mode=amap_toll_distance。"""
    money = route_service.driving_money(452.6, toll_distance_km=CHONGRU_TOLL_KM, region="east")

    assert money["cost_breakdown"]["mode"] == route_service.TOLL_MODE_AMAP_TOLL_DISTANCE
    assert money["toll_km"] == pytest.approx(427.2), "收费里程就是高德给的 toll_distance"
    assert money["cost_breakdown"]["toll"] == 192, "427.2km × 0.45 = 192.24 → 192"
    assert money["cost_breakdown"]["fuel"] == 290, "452.6km × 0.08L/km × 8 元/L = 289.66 → 290"
    assert money["cost_cny"] == 482 == 192 + 290
    assert money["per_person_cny"] == 121, "482 ÷ 4 = 120.5 → 四舍五入 121(半进位)"


def test_driving_toll_falls_back_to_heuristic_without_toll_distance() -> None:
    """拿不到 ``toll_distance`` → 收费里程按 总里程 × 0.55 估,mode=heuristic。"""
    money = route_service.driving_money(452.6, region="east")

    assert money["cost_breakdown"]["mode"] == route_service.TOLL_MODE_HEURISTIC
    assert money["toll_km"] == pytest.approx(452.6 * 0.55)
    assert money["cost_breakdown"]["toll"] == 112, "248.93km × 0.45 = 112.02 → 112"
    assert money["cost_breakdown"]["fuel"] == 290
    assert money["cost_cny"] == 402 and money["per_person_cny"] == 101


def test_driving_toll_zero_toll_distance_means_no_toll_road() -> None:
    """高德给了 ``toll_distance=0``(全程无收费路段)→ 过路费 0,但**仍标真实口径**。

    与"字段缺失"必须区分开:缺失才退化启发式,否则市区短途会被凭空估出 55% 高速费。
    """
    money = route_service.driving_money(50.0, toll_distance_km=0.0, region="east")

    assert money["cost_breakdown"] == {"toll": 0, "fuel": 32, "mode": "amap_toll_distance"}
    assert money["toll_km"] == 0.0 and money["cost_cny"] == 32


@pytest.mark.parametrize("junk", [None, -1.0, "427.2", {}, [], True])
def test_driving_toll_ignores_bad_toll_distance(junk: Any) -> None:
    """``toll_distance`` 非法(负数/字符串/布尔/容器)→ 当"拿不到",退化启发式而不是崩。"""
    money = route_service.driving_money(50.0, toll_distance_km=junk, region="east")

    assert money["cost_breakdown"]["mode"] == route_service.TOLL_MODE_HEURISTIC
    assert money["toll_km"] == pytest.approx(27.5)


def test_driving_toll_clamps_toll_distance_to_total_km() -> None:
    """收费里程不该超过总里程(高德偶有重复计数)→ 夹到总里程。"""
    money = route_service.driving_money(50.0, toll_distance_km=999.0, region="east")

    assert money["toll_km"] == pytest.approx(50.0)
    assert money["cost_breakdown"]["mode"] == route_service.TOLL_MODE_AMAP_TOLL_DISTANCE
    assert money["cost_breakdown"]["toll"] == 23, "50km × 0.45 = 22.5 → 半进位 23"


def test_resolve_toll_km_reports_mode_alongside_value() -> None:
    assert route_service.resolve_toll_km(100.0, 60.0) == (60.0, "amap_toll_distance")
    km, mode = route_service.resolve_toll_km(100.0, None)
    assert km == pytest.approx(55.0) and mode == "heuristic"
    assert route_service.resolve_toll_km(-100.0, None) == (0.0, "heuristic"), "负里程按 0 处理"


def test_driving_region_rates_are_east_central_west() -> None:
    """区域费率:东部 0.45 / 中部 0.40 / 西部 0.35 元/km(同样的高速里程,费用递减)。"""
    assert route_service.TOLL_CNY_PER_KM_BY_REGION == {"east": 0.45, "central": 0.40, "west": 0.35}
    tolls = {region: route_service.driving_money(100.0, region=region)["cost_breakdown"]["toll"]
             for region in ("east", "central", "west")}
    assert tolls == {"east": 25, "central": 22, "west": 19}, "55km 高速 × 各档费率"
    assert tolls["east"] > tolls["central"] > tolls["west"]


def test_driving_region_prefers_names_then_midpoint_longitude() -> None:
    assert route_service.driving_region(from_name="杭州", to_name="崇儒乡", mid_lng=120.0) == "east"
    assert route_service.driving_region(to_name="浙江省杭州市", mid_lng=0.0) == "east", "含省名即命中"
    assert route_service.driving_region(from_name="武汉", to_name="黄山风景区") == "central"
    assert route_service.driving_region(to_name="成都", from_name="上海") == "west", "目的地名优先"
    # 名字都认不出来 → 按中点经度分带
    assert route_service.driving_region(to_name="某某乡", from_name=None, mid_lng=121.0) == "east"
    assert route_service.driving_region(mid_lng=110.0) == "central"
    assert route_service.driving_region(mid_lng=95.0) == "west"
    assert route_service.resolve_region_by_name("  ") is None
    assert route_service.resolve_region_by_name("崇儒乡") is None


def test_driving_fuel_price_reads_env_and_ignores_junk(monkeypatch: pytest.MonkeyPatch) -> None:
    assert route_service.fuel_price_cny_per_l() == 8.0, "env 未设 → 默认 8.0 元/L"
    monkeypatch.setenv(route_service.ENV_FUEL_PRICE, "9.5")
    assert route_service.fuel_price_cny_per_l() == 9.5
    money = route_service.driving_money(100.0, region="east")
    assert money["cost_breakdown"]["fuel"] == 76, "100 × 0.08 × 9.5 = 76"

    for junk in ("", "   ", "abc", "0", "-3", "nan", "inf"):
        monkeypatch.setenv(route_service.ENV_FUEL_PRICE, junk)
        assert route_service.fuel_price_cny_per_l() == 8.0, f"油价 {junk!r} 应降级成默认值"
    assert route_service.fuel_price_cny_per_l({route_service.ENV_FUEL_PRICE: "7.2"}) == 7.2, \
        "可注入 environ,便于单测"


def test_hangzhou_to_chongru_driving_reports_breakdown_and_per_person() -> None:
    """契约样例:杭州 → 崇儒乡 驾车给出 cost_breakdown(amap_toll_distance 路径)+ 人均。"""
    plan = plan_between(HANGZHOU, CHONGRU, leg=CHONGRU_LEG)
    driving = only(plan, "driving")

    assert driving["kind"] == "real" and driving["degraded"] is False
    assert driving["vehicle_label"] == "整车≤4人"
    assert set(driving["cost_breakdown"]) == {"toll", "fuel", "mode"}
    assert driving["cost_breakdown"]["mode"] == "amap_toll_distance"
    assert driving["cost_cny"] == 482
    assert driving["cost_breakdown"]["toll"] + driving["cost_breakdown"]["fuel"] == driving["cost_cny"]
    assert driving["per_person_cny"] == 121
    assert driving["duration_min"] == 331 and driving["distance_km"] == 452.6
    assert "cost_breakdown.mode=amap_toll_distance" in driving["note"], "note 要写清本次走的是哪条口径"
    assert "人均" in driving["note"] and route_service.ESTIMATE_DISCLAIMER in driving["note"]


def test_hangzhou_to_chongru_driving_heuristic_path() -> None:
    """同一条线路拿不到 toll_distance → 退化启发式,金额变化但字段/口径标注照样齐。"""
    plan = plan_between(HANGZHOU, CHONGRU, leg=PLAIN_LEG)
    driving = only(plan, "driving")

    assert driving["cost_breakdown"] == {"toll": 112, "fuel": 290, "mode": "heuristic"}
    assert driving["cost_cny"] == 402 and driving["per_person_cny"] == 101
    assert driving["vehicle_label"] == "整车≤4人"
    assert "heuristic" in driving["note"] and "收费里程" in driving["note"]


def test_driving_degraded_gives_no_money_but_keeps_label() -> None:
    from data_sources import DataSourceError

    plan = plan_between(HANGZHOU, CHONGRU,
                        leg=None)  # 占位,下面用抛错的替身覆盖
    del plan
    router = FakeRouter(error=DataSourceError("amap", "未配置 WHERE2GO_AMAP_KEY(高德 Web 服务 key)"))
    degraded = route_service.plan_routes(
        from_lat=HANGZHOU["lat"], from_lng=HANGZHOU["lng"],
        to_lat=CHONGRU["lat"], to_lng=CHONGRU["lng"],
        to_name=CHONGRU["name"], from_name=HANGZHOU["name"], router=router,
    )
    driving = only(degraded, "driving")

    assert driving["degraded"] is True and driving["kind"] == "estimate"
    assert driving["cost_cny"] is None and driving["cost_breakdown"] is None
    assert driving["per_person_cny"] is None
    assert driving["vehicle_label"] == "整车≤4人"
    assert driving["links"], "降级也要能跳转导航"


# --------------------------------------------------------------------------- #
# 2. 铁路:运营里程 × 分档费率 + 热门城市对种子价
# --------------------------------------------------------------------------- #


def test_rail_seed_hit_shanghai_beijing_553() -> None:
    """契约样例:上海 → 北京 命中种子价 553 元、``price_source="seed"``。"""
    plan = plan_north(1067.0, to_name="北京")
    rail = only(plan, "rail")

    assert rail["cost_cny"] == 553
    assert rail["price_source"] == "seed"
    assert rail["kind"] == "estimate" and rail["degraded"] is False
    assert "种子" in rail["note"] and "上海—北京" in rail["note"]
    assert route_service.rail_fare(1067.0, from_name="上海", to_name="北京")["seed_pair"] == ("上海", "北京")


def test_rail_seed_hit_is_symmetric_and_tolerates_station_names() -> None:
    """种子键无序,且"北京南站/上海虹桥站"这类带后缀的名字也能归一命中。"""
    assert route_service.rail_seed_fare("北京", "上海") == ("上海", "北京", 553.0)
    assert route_service.rail_seed_fare("上海", "北京") == ("上海", "北京", 553.0)
    assert route_service.rail_seed_fare("北京南站", "上海虹桥站") is not None
    assert route_service.rail_fare(1067.0, from_name="杭州市", to_name="上海")["total_cny"] == 73.0
    assert route_service.rail_seed_fare("上海", "上海") is None, "同城不算城市对"
    assert route_service.rail_seed_fare("上海", "崇儒乡") is None
    assert route_service.rail_seed_fare(None, "北京") is None


def test_rail_seed_table_covers_at_least_eight_real_pairs() -> None:
    seeds = route_service.RAIL_SEED_FARES
    assert len(seeds) >= 8, "契约要求 ≥8 对热门城市对种子票价"
    assert len(route_service.RAIL_SEED_INDEX) == len(seeds), "城市对不能重复"
    pairs = {frozenset(pair[:2]) for pair in seeds}
    assert len(pairs) == len(seeds)
    for city_a, city_b, fare in seeds:
        assert city_a in route_service.RAIL_HUB_CITIES, f"{city_a} 应是枢纽城市"
        assert city_b in route_service.RAIL_HUB_CITIES, f"{city_b} 应是枢纽城市"
        assert 20.0 <= fare <= 2000.0, f"{city_a}—{city_b} 票价 {fare} 看着不像二等座常态价"
    assert frozenset({"上海", "北京"}) in pairs and frozenset({"杭州", "上海"}) in pairs


def test_rail_estimate_uses_350_tier_for_double_hub() -> None:
    """两端都是高铁枢纽(且未命中种子)→ 350km/h 线 0.46 元/km。"""
    assert route_service.is_trunk_pair("上海", "长沙") is True
    assert route_service.rail_speed_tier_kmh("上海", "长沙") == 350
    assert route_service.rail_rate_cny_per_km("上海", "长沙") == 0.46
    fare = route_service.rail_fare(900.0, from_name="上海", to_name="长沙")
    assert fare["price_source"] == "estimate"
    assert fare["operating_km"] == pytest.approx(1035.0)
    assert fare["total_cny"] == pytest.approx(476.1), "900 × 1.15 × 0.46"
    assert route_service.round_cost(fare["total_cny"]) == 476


def test_rail_estimate_uses_250_tier_when_not_double_hub() -> None:
    """任一端不是枢纽 → 250km/h 线 0.31 元/km(崇儒乡这种乡镇也算非枢纽)。"""
    assert route_service.is_trunk_pair("上海", "某某县") is False
    for pair in (("上海", "莫干山"), ("杭州", "崇儒乡"), ("上海", None)):
        assert route_service.rail_speed_tier_kmh(*pair) == 250, f"{pair} 应走 250 档"
    fare = route_service.rail_fare(900.0, from_name="上海", to_name="莫干山")
    assert fare["rate_cny_per_km"] == 0.31 and fare["total_cny"] == pytest.approx(320.85), "900 × 1.15 × 0.31"
    assert fare["price_source"] == "estimate" and fare["seed_pair"] is None


def test_rail_operating_km_is_straight_times_115_and_has_min_fare() -> None:
    assert route_service.RAIL_FARE_DETOUR == 1.15
    assert route_service.rail_operating_km(100.0) == pytest.approx(115.0)
    assert route_service.rail_operating_km(-30) == 0.0, "负里程按 0 处理"
    # 起步价下限:10km → 11.5 × 0.31 = 3.57 → 20 元
    assert route_service.rail_cost_cny(10.0, from_name="上海", to_name="莫干山") == pytest.approx(20.0)


def test_rail_note_separates_fare_detour_from_duration_detour() -> None:
    """票价用运营里程 1.15,耗时仍用 POC 的 1.20 —— note 里两个口径都要写清。"""
    fare = route_service.rail_fare(350.5, from_name="上海", to_name="天目湖")
    note = route_service._rail_note(350.5, fare)
    assert "运营里程 403.1km(直线 350.5km × 1.15)" in note
    assert "绕行 1.2" in note and "无实时班次" in note
    assert route_service.ESTIMATE_DISCLAIMER in note
    assert route_service.estimate_duration_min("rail", 350.5) == 225, "耗时口径不变"


def test_rail_price_source_estimate_for_unseeded_pair_in_plan() -> None:
    plan = plan_north(369.7, to_name="崇儒乡", from_name="杭州", origin=HANGZHOU)
    rail = only(plan, "rail")
    assert rail["price_source"] == "estimate"
    assert rail["cost_cny"] == route_service.round_cost(rail["distance_km"] * 1.15 * 0.31)
    assert "0.31元/km" in rail["note"] and "250km/h 线" in rail["note"]


# --------------------------------------------------------------------------- #
# 3. 机票:民航公布价锚定区间(纯规则)
# --------------------------------------------------------------------------- #


def test_published_fare_tiers_boundaries() -> None:
    """三段边界:<812km → 1.6;812~1600km(含两端)→ 0.95;>1600km → 0.8 元/km。"""
    assert route_service.PUBLISHED_FARE_TIERS == ((812.0, 1.6), (1600.0, 0.95), (None, 0.8))
    assert route_service.published_fare_tier(811.9)[1] == 1.6
    assert route_service.published_fare_tier(812.0)[1] == 0.95, "812km 属于第二档"
    assert route_service.published_fare_tier(1600.0)[1] == 0.95, "1600km 仍属第二档"
    assert route_service.published_fare_tier(1600.1)[1] == 0.8
    assert route_service.published_fare_cny(800.0) == pytest.approx(1280.0)
    assert route_service.published_fare_cny(812.0) == pytest.approx(771.4)
    assert route_service.published_fare_cny(1600.0) == pytest.approx(1520.0)
    assert route_service.published_fare_cny(1601.0) == pytest.approx(1280.8)
    assert route_service.published_fare_tier_label(500.0) == "<812km 档 1.6元/km"
    assert route_service.published_fare_tier_label(1000.0) == "812~1600km 档 0.95元/km"
    assert route_service.published_fare_tier_label(2000.0) == ">1600km 档 0.8元/km"


def test_flight_range_is_discount_to_published_and_cost_is_midpoint() -> None:
    """上海 → 北京(1067km,主干商务线):区间 [公布价×0.45, 公布价],cost_cny 取中值。"""
    quote = route_service.flight_fare(1067.0, from_name="上海", to_name="北京")
    assert quote["available"] is True and quote["trunk"] is True
    assert quote["segment_km"] == pytest.approx(1173.7), "航段里程 = 大圆 × 1.1"
    assert quote["published_cny"] == pytest.approx(1115.015)
    assert (quote["low_cny"], quote["high_cny"]) == (502, 1115)
    assert quote["mid_cny"] == 809 == route_service.round_cost((502 + 1115) / 2)
    assert quote["low_cny"] < quote["high_cny"]

    plan = plan_north(1067.0, to_name="北京")
    flight = only(plan, "flight")
    assert (flight["flight_low_cny"], flight["flight_high_cny"], flight["cost_cny"]) == (502, 1115, 809)
    assert flight["kind"] == "estimate"


def test_flight_discount_separates_trunk_and_branch() -> None:
    """主干商务线(双枢纽)0.45、支线 0.6:同一里程下支线区间更窄、下限更高。"""
    assert route_service.flight_discount("上海", "北京") == 0.45
    assert route_service.flight_discount("上海", "丽江") == 0.6, "丽江有机场但不是高铁枢纽"
    trunk = route_service.flight_fare(1200.0, from_name="上海", to_name="北京")
    branch = route_service.flight_fare(1200.0, from_name="上海", to_name="丽江")
    assert trunk["published_cny"] == pytest.approx(branch["published_cny"])
    assert trunk["low_cny"] < branch["low_cny"], "主干线折扣更狠 → 区间下限更低"
    assert (trunk["low_cny"], trunk["high_cny"]) == (564, 1254)
    assert (branch["low_cny"], branch["high_cny"], branch["mid_cny"]) == (752, 1254, 1003)
    assert "主干商务线" in route_service._flight_note(1200.0, trunk)
    assert "支线" in route_service._flight_note(1200.0, branch)


def test_flight_price_suppressed_below_400km() -> None:
    """契约样例:<400km 的城市对不给机票价(阈值 :data:`FLIGHT_PRICE_MIN_KM`)。"""
    assert route_service.FLIGHT_PRICE_MIN_KM == 400.0
    quote = route_service.flight_fare(350.0, from_name="上海", to_name="杭州")
    assert quote["available"] is False
    assert (quote["low_cny"], quote["high_cny"], quote["mid_cny"]) == (None, None, None)
    assert "< 400km" in quote["reason"] or "400km" in quote["reason"]
    assert route_service.flight_cost_cny(350.0, from_name="上海", to_name="杭州") is None
    # 规划层面:399km 连飞机条目都不出(阈值 600km),自然也就没有机票价
    plan = plan_north(399.0, to_name="杭州")
    assert "flight" not in [route["mode"] for route in plan.routes]
    assert route_service.flight_fare(400.0, from_name="上海", to_name="杭州")["available"] is True


def test_flight_entry_threshold_is_600km() -> None:
    """飞机候选阈值 300km → 600km:599km 不出条目,600.5km 出条目。"""
    assert route_service.FLIGHT_MIN_KM == 600.0
    assert [r["mode"] for r in plan_north(599.0, to_name="北京").routes] == ["driving", "rail"]
    modes = [r["mode"] for r in plan_north(600.5, to_name="北京").routes]
    assert modes == ["driving", "rail", "flight"]
    assert route_service.mode_rules()["flight_min_km"] == 600.0


def test_flight_price_suppressed_without_airport_but_links_kept() -> None:
    """契约样例:任一端城市没有民航机场 → 不给票价,条目与 deep-link 仍在。"""
    plan = plan_north(700.0, to_name="黄冈")     # 黄冈无民航机场(用武汉天河)
    flight = only(plan, "flight")

    assert flight["flight_low_cny"] is None and flight["flight_high_cny"] is None
    assert flight["cost_cny"] is None, "不给票价时旧字段也必须是 null,不能瞎填"
    assert flight["duration_min"] is not None, "耗时仍可估"
    assert [link["provider"] for link in flight["links"]] == ["qunar"], "只保留 deep-link 跳转"
    assert "没有匹配到民航机场" in flight["note"] and "黄冈" in flight["note"]
    assert route_service.FLIGHT_DYNAMIC_NOTE in flight["note"]
    assert route_service.resolve_airport_city("上海") == "上海"
    assert route_service.resolve_airport_city("黄冈") is None


def test_flight_price_suppressed_when_city_names_missing() -> None:
    """没给地名就判不出机场 → 宁可不估:不给票价,只留 deep-link。"""
    assert route_service.missing_airport_endpoint(None, "北京") == "出发地(未给地名)"
    quote = route_service.flight_fare(1067.0, from_name=None, to_name=None)
    assert quote["available"] is False and quote["mid_cny"] is None
    plan = plan_north(1067.0, to_name=None, from_name=None)
    flight = only(plan, "flight")
    assert flight["cost_cny"] is None and flight["flight_low_cny"] is None
    assert flight["links"], "没有票价也要能跳转 OTA 查实时价"


def test_airport_table_covers_at_least_40_cities() -> None:
    cities = route_service.AIRPORT_CITIES
    assert len(cities) >= 40, "契约要求 ≥40 城机场表"
    for hub in ("北京", "上海", "广州", "深圳", "成都", "西安", "昆明", "乌鲁木齐"):
        assert hub in cities, f"{hub} 有机场,应在表里"
    for no_airport in ("苏州", "黄冈", "崇儒乡", "东莞", "六安"):
        assert no_airport not in cities, f"{no_airport} 没有民航机场,不该在表里"
    assert route_service.resolve_airport_city("上海虹桥国际机场") == "上海"
    assert route_service.missing_airport_endpoint("上海", "北京") is None
    assert "目的地" in route_service.missing_airport_endpoint("上海", "崇儒乡")


def test_flight_note_states_dynamic_pricing_and_range() -> None:
    quote = route_service.flight_fare(1067.0, from_name="上海", to_name="北京")
    note = route_service._flight_note(1067.0, quote)
    assert "动态定价·浮动大·实时价以跳转为准" in note
    assert "502~1115 元" in note and "区间中值 809 元" in note
    assert "公布价" in note and "无实时航班" in note
    assert route_service.ESTIMATE_DISCLAIMER in note

    suppressed = route_service.flight_fare(700.0, from_name="上海", to_name="黄冈")
    assert "不给机票价" in route_service._flight_note(700.0, suppressed)


# --------------------------------------------------------------------------- #
# 4. 地名归一 / 枢纽判定(铁路分档与机票折扣共用)
# --------------------------------------------------------------------------- #


def test_normalize_city_uses_longest_match_and_keeps_unknown_names() -> None:
    assert route_service.normalize_city("上海市") == "上海"
    assert route_service.normalize_city("上海虹桥站") == "上海"
    assert route_service.normalize_city(" 杭州西湖 ") == "杭州"
    assert route_service.normalize_city("北京南站") == "北京"
    assert route_service.normalize_city("崇儒乡") == "崇儒乡", "认不出城市就原样返回"
    assert route_service.normalize_city(None) is None and route_service.normalize_city("  ") is None
    assert route_service.is_hub_city("杭州东站") is True
    assert route_service.is_hub_city("崇儒乡") is False


# --------------------------------------------------------------------------- #
# 5. /api/routes:响应字段、note 口径与 JSON 可序列化
# --------------------------------------------------------------------------- #


def test_api_payload_carries_v2_fields_and_stays_serializable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(route_service, "default_router", FakeRouter(CHONGRU_LEG))
    payload = routes_api.list_routes(
        from_lat=HANGZHOU["lat"], from_lng=HANGZHOU["lng"],
        to_lat=CHONGRU["lat"], to_lng=CHONGRU["lng"],
        to_name=CHONGRU["name"], from_name=HANGZHOU["name"],
    )
    driving = payload["routes"][0]

    assert driving["cost_breakdown"] == {"toll": 192, "fuel": 290, "mode": "amap_toll_distance"}
    assert driving["vehicle_label"] == "整车≤4人" and driving["per_person_cny"] == 121
    assert payload["routes"][1]["price_source"] == "estimate"
    assert payload["mode_rules"]["flight_min_km"] == 600.0
    assert payload["cost_model"]["driving"]["toll_cny_per_km_by_region"]["east"] == 0.45
    assert payload["cost_model"]["rail"]["rate_cny_per_km"] == {"350": 0.46, "250": 0.31}
    assert payload["cost_model"]["flight"]["discount"] == {"trunk": 0.45, "branch": 0.6}
    # 旧字段一个没删、语义不变
    for route in payload["routes"]:
        assert {"mode", "label", "duration_min", "cost_cny", "distance_km", "geometry",
                "kind", "degraded", "source", "note", "links"} <= set(route)
    assert json.loads(json.dumps(payload, ensure_ascii=False))["count"] == payload["count"]


def test_api_note_documents_v2_cost_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(route_service, "default_router", FakeRouter(PLAIN_LEG))
    payload = routes_api.list_routes(
        from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"],
        to_lat=point_north(1067.0)["lat"], to_lng=SHANGHAI["lng"],
        to_name="北京", from_name="上海",
    )
    note = payload["note"]
    for fragment in ("cost_breakdown", "amap_toll_distance", "heuristic", "整车≤4人", "per_person_cny",
                     "price_source", "seed", "flight_low_cny", "flight_high_cny",
                     "动态定价·浮动大·实时价以跳转为准", "600km", "400km",
                     route_service.ESTIMATE_DISCLAIMER):
        assert fragment in note, f"响应 note 应写清 v2 口径,缺:{fragment}"
    assert payload["routes"][2]["flight_low_cny"] == 502
