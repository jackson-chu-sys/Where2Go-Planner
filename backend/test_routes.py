"""TASK-2a 单测:路线编排 + 费用估算 + ``/api/routes`` + deep-link 纯函数。

全程不触网(参照 :mod:`backend.test_classify` 的 ``no_network`` 风格):

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,任何偷偷联网都会当场失败;
* OSRM 两层都有替身:服务层注入 ``router=``(:class:`FakeRouter`),数据源层用
  :class:`FakeSession` 断言**请求参数**与 geometry 解析;
* API 直接调路由函数(同 :mod:`backend.test_places`),用 :func:`expect_http_error`
  断言状态码与中文 detail;另有 :func:`http_get` 自己拼 ASGI scope 走一遍**完整 HTTP 链**
  (仓库没装 httpx/TestClient),校验参数解析与 400 契约;
* 时间用 ``now=`` 注入,断言不依赖当前时钟。

运行:``python -m pytest backend/ -q``
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fastapi import HTTPException

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import discover as discover_api  # noqa: E402
from app.api import routes as routes_api  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from data_sources import DataSourceError  # noqa: E402
from data_sources import osrm  # noqa: E402
from data_sources.overpass import EARTH_RADIUS_KM  # noqa: E402
from services import routes as route_service  # noqa: E402

# --------------------------------------------------------------------------- #
# 样本:上海为起点,沿正北按公里摆目的地(用 haversine 同源的度/公里换算,边界才准)
# --------------------------------------------------------------------------- #

SHANGHAI = {"lat": 31.2304, "lng": 121.4737}
DEG_PER_KM = 180.0 / (math.pi * EARTH_RADIUS_KM)
FAR_DEST = {"lat": 39.9042, "lng": 116.4074, "name": "北京"}

# 每条路线必须有的字段(spec:{mode,label,duration_min,cost_cny,distance_km,geometry?,kind,note})
SPEC_ROUTE_KEYS = {
    "mode", "label", "duration_min", "cost_cny", "distance_km", "geometry", "kind", "note",
}
ROUTE_KEYS = SPEC_ROUTE_KEYS | {"emoji", "degraded", "source", "links"}
LINK_KEYS = {"provider", "label", "url", "note"}

DEFAULT_LEG = {
    "distance_km": 122.4,
    "duration_min": 94.2,
    "geometry": [[31.2304, 121.4737], [31.8, 121.2], [32.5, 120.6]],
}

OSRM_WITH_GEOMETRY = {
    "code": "Ok",
    "routes": [{
        "distance": 122376.1,
        "duration": 5638.8,
        "geometry": {
            "type": "LineString",
            "coordinates": [[121.4737, 31.2304], [121.2, 31.8], [116.4074, 39.9042]],
        },
    }],
}


def point_north(km: float, *, name: Optional[str] = None) -> dict[str, Any]:
    """构造距上海 ``km`` 公里(正北)的目的地,直线距离由 haversine 复核即 ``km``。"""
    return {
        "lat": SHANGHAI["lat"] + km * DEG_PER_KM,
        "lng": SHANGHAI["lng"],
        "name": name,
    }


def plan_for(km: float, *, router: Optional[Callable] = None, to_name: Optional[str] = "目的地",
             from_name: Optional[str] = "上海", **kwargs: Any):
    """快捷方式:规划上海 → 正北 ``km`` 公里处的路线。"""
    dest = point_north(km)
    return route_service.plan_routes(
        from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"],
        to_lat=dest["lat"], to_lng=dest["lng"],
        to_name=to_name, from_name=from_name, router=router, **kwargs
    )


# --------------------------------------------------------------------------- #
# 测试替身与断言辅助
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class Call:
    method: str
    url: str
    params: Optional[dict[str, Any]]


class FakeResponse:
    """最小 response 替身:只需要 ``status_code`` 与 ``text``。"""

    def __init__(self, payload: Any, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)


class FakeSession:
    """记录请求参数并返回预设响应的 session 替身(不触网)。"""

    def __init__(self, payload: Any) -> None:
        self.responses = [FakeResponse(payload)]
        self.calls: list[Call] = []

    def request(self, method: str, url: str, params: Optional[dict[str, Any]] = None,
                **kwargs: Any) -> FakeResponse:
        self.calls.append(Call(method, url, params))
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]

    @property
    def last(self) -> Call:
        return self.calls[-1]


class FakeRouter:
    """OSRM 替身:记录 ``(start, end)`` 调用,返回预设 leg 或抛预设异常。"""

    def __init__(self, leg: Optional[dict[str, Any]] = None,
                 error: Optional[BaseException] = None) -> None:
        self.leg = dict(DEFAULT_LEG if leg is None else leg)
        self.error = error
        self.calls: list[tuple[tuple[float, float], tuple[float, float]]] = []

    def __call__(self, start_lnglat: Any, end_lnglat: Any) -> dict[str, Any]:
        self.calls.append((tuple(start_lnglat), tuple(end_lnglat)))
        if self.error is not None:
            raise self.error
        return dict(self.leg)


def expect_http_error(func: Callable[[], Any], status_code: int, *fragments: str) -> HTTPException:
    """断言 ``func()`` 抛出指定状态码的 HTTPException,且 detail 含全部片段。"""
    with pytest.raises(HTTPException) as caught:
        func()
    exc = caught.value
    assert exc.status_code == status_code, f"应为 HTTP {status_code},实际 {exc.status_code}"
    for fragment in fragments:
        assert fragment in str(exc.detail), f"detail 应包含 {fragment!r},实际:{exc.detail}"
    return exc


def expect_error(func: Callable[[], Any], exc_type: type, *fragments: str) -> Exception:
    """断言 ``func()`` 抛出 ``exc_type``,且错误信息包含全部 ``fragments``。"""
    with pytest.raises(exc_type) as caught:
        func()
    for fragment in fragments:
        assert fragment in str(caught.value), f"应包含 {fragment!r},实际:{caught.value}"
    return caught.value


def query_of(url: str) -> dict[str, list[str]]:
    """把 URL 的 query 解析回字典(顺带验证百分号编码能被正确解回中文)。"""
    return parse_qs(urlsplit(url).query)


def http_get(query: str) -> tuple[int, Any]:
    """直接驱动 ASGI app 走一遍**完整 HTTP 链**(含 FastAPI 参数校验),返回 ``(状态码, JSON)``。

    仓库没装 httpx/TestClient,所以自己拼最小 scope;不触网、不建 DB 会话。
    """
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/api/routes", "raw_path": b"/api/routes", "query_string": query.encode(),
        "root_path": "", "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    body = bytearray()
    status: dict[str, int] = {}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    asyncio.run(fastapi_app(scope, receive, send))
    return status["code"], json.loads(body.decode() or "null")


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底:任何 requests 调用都视为测试失败(本套单测必须纯 mock)。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture()
def stub_osrm(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeRouter]:
    """把服务层的默认 OSRM 调用换成替身;返回工厂,便于按用例定制 leg / error。"""

    def install(leg: Optional[dict[str, Any]] = None,
                error: Optional[BaseException] = None) -> FakeRouter:
        router = FakeRouter(leg=leg, error=error)
        monkeypatch.setattr(route_service, "default_router", router)
        return router

    return install


# --------------------------------------------------------------------------- #
# OSRM 数据源:geometry(默认形状不变,只在 with_geometry 时多要折线)
# --------------------------------------------------------------------------- #


def test_osrm_default_call_keeps_stage0_shape() -> None:
    session = FakeSession(OSRM_WITH_GEOMETRY)
    result = osrm.OsrmClient(session=session).route((121.4737, 31.2304), (116.4074, 39.9042))

    assert result == {"distance_km": 122.376, "duration_min": 94.0}, "默认不能多带 geometry 键"
    assert session.last.params == {
        "overview": "false", "alternatives": "false", "steps": "false", "annotations": "false",
    }, "默认请求参数不能变(POC /api/discover 与既有单测依赖)"


def test_osrm_with_geometry_asks_full_overview_and_swaps_axis_order() -> None:
    session = FakeSession(OSRM_WITH_GEOMETRY)
    result = osrm.OsrmClient(session=session).route(
        (121.4737, 31.2304), (116.4074, 39.9042), with_geometry=True
    )

    assert session.last.params == {
        "overview": "full", "geometries": "geojson",
        "alternatives": "false", "steps": "false", "annotations": "false",
    }
    assert result["distance_km"] == 122.376 and result["duration_min"] == 94.0
    # GeoJSON 是 [lng, lat],前端 Leaflet 要 [lat, lng],数据源层就换好
    assert result["geometry"] == [[31.2304, 121.4737], [31.8, 121.2], [39.9042, 116.4074]]


def test_osrm_geometry_is_optional_and_malformed_is_ignored() -> None:
    """折线缺失/畸形不该拖垮整条驾车路线:geometry 降级为 None,时长里程照给。"""
    bad_geometries: list[Any] = [
        None, {}, "LineString", {"type": "LineString"}, {"coordinates": []},
        {"coordinates": [[121.4737]]}, {"coordinates": [["a", "b"]]}, {"coordinates": [[999.0, 31.0]]},
    ]
    for geometry in bad_geometries:
        route: dict[str, Any] = {"distance": 1000.0, "duration": 600.0}
        if geometry is not None:
            route["geometry"] = geometry
        session = FakeSession({"code": "Ok", "routes": [route]})
        result = osrm.OsrmClient(session=session).route((121.0, 31.0), (121.1, 31.1),
                                                        with_geometry=True)
        assert result["geometry"] is None, f"geometry={geometry!r} 应降级为 None"
        assert result["distance_km"] == 1.0 and result["duration_min"] == 10.0


def test_service_default_router_asks_osrm_for_geometry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, Any, dict[str, Any]]] = []

    def fake_ds_route(start: Any, end: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((start, end, kwargs))
        return {"distance_km": 10.0, "duration_min": 12.0, "geometry": None}

    monkeypatch.setattr(route_service, "ds_route", fake_ds_route)
    leg = route_service.default_router((121.4737, 31.2304), (121.5, 31.3))

    assert calls == [((121.4737, 31.2304), (121.5, 31.3), {"with_geometry": True})]
    assert leg["distance_km"] == 10.0


# --------------------------------------------------------------------------- #
# 费用估算(系数集中在 services.routes,算式见 docs/STAGE2-PLAN.md 第 2 节)
# --------------------------------------------------------------------------- #


def test_cost_formulas_follow_stage2_coefficients() -> None:
    # 驾车 100km:油费 100×8/100×7.5 = 60,过路 0.7×100×0.5 = 35 → 95 元
    assert route_service.driving_cost_cny(100) == pytest.approx(95.0)
    assert route_service.round_cost(route_service.driving_cost_cny(122.4)) == 116
    # 铁路 100km:计费里程 100×1.2 = 120 → 120×0.45 = 54 元
    assert route_service.rail_cost_cny(100) == pytest.approx(54.0)
    # 起步价下限:10km → 12×0.45 = 5.4 元,低于 20 元下限 → 20 元
    assert route_service.rail_cost_cny(10) == pytest.approx(20.0)
    # 飞机 300km:330×0.6 + 100 = 298 元
    assert route_service.flight_cost_cny(300) == pytest.approx(298.0)
    # 负里程按 0 处理,不会算出负费用
    assert route_service.driving_cost_cny(-50) == 0.0
    assert route_service.cost_for("rail", straight_km=100) == 54
    assert route_service.cost_for("driving", straight_km=100, driving_km=None) is None
    expect_error(lambda: route_service.cost_for("bike", straight_km=100), ValueError, "未知出行方式")


def test_cost_model_and_mode_rules_are_exposed_for_labels() -> None:
    model = route_service.cost_coefficients()
    assert set(model) == {"driving", "rail", "flight", "disclaimer"}
    assert model["disclaimer"] == "估算·非实时·以官方为准"
    assert model["driving"]["fuel_l_per_100km"] == 8.0
    assert model["driving"]["fuel_price_cny_per_l"] == 7.5
    assert model["driving"]["highway_ratio"] == 0.7 and model["driving"]["toll_cny_per_km"] == 0.5
    assert model["rail"]["cny_per_km"] == 0.45 and model["rail"]["min_fare_cny"] == 20.0
    assert model["flight"]["cny_per_km"] == 0.6 and model["flight"]["base_cny"] == 100.0

    rules = route_service.mode_rules()
    assert rules["rail_min_km"] == 100.0 and rules["flight_min_km"] == 300.0
    assert rules["rail"]["speed_kmh"] == 220.0 and rules["flight"]["ground_min"] == 210.0


# --------------------------------------------------------------------------- #
# 方式阈值(POC 口径)与三方式结构
# --------------------------------------------------------------------------- #


def test_modes_appear_by_distance_threshold(stub_osrm) -> None:
    stub_osrm()
    cases = [
        (99.0, ["driving"]),                       # 99km:不到铁路阈值
        (100.5, ["driving", "rail"]),
        (299.0, ["driving", "rail"]),              # 299km:不到飞机阈值
        (300.5, ["driving", "rail", "flight"]),
    ]
    for km, expected in cases:
        plan = plan_for(km)
        assert [route["mode"] for route in plan.routes] == expected, f"{km}km 的方式组合不对"
        assert plan.distance_km == pytest.approx(km, abs=0.05)

    assert "rail" not in [r["mode"] for r in plan_for(99.0).routes]
    assert "flight" not in [r["mode"] for r in plan_for(299.0).routes]


def test_thresholds_and_durations_match_poc_est_mode() -> None:
    """耗时估算与出现阈值必须和 POC ``app/api/discover.py`` 一个口径。"""
    assert (route_service.RAIL_MIN_KM, route_service.FLIGHT_MIN_KM) == (
        discover_api.RAIL_MIN_KM, discover_api.FLIGHT_MIN_KM
    )
    for km in (100.0, 150.0, 299.0, 300.5, 480.0):
        assert route_service.estimate_duration_min("rail", km) == discover_api._est_mode("rail", km)["duration_min"]
        assert route_service.estimate_duration_min("flight", km) == discover_api._est_mode("flight", km)["duration_min"]
    expect_error(lambda: route_service.estimate_duration_min("driving", 100.0), ValueError, "仅铁路/飞机")


def test_three_mode_payload_shape_and_honest_labels(stub_osrm) -> None:
    stub_osrm()
    plan = plan_for(350.5, to_name="天目湖")
    driving, rail, flight = plan.routes

    for route in plan.routes:
        assert set(route) == ROUTE_KEYS, f"{route['mode']} 字段应与约定形状一致"
        assert SPEC_ROUTE_KEYS <= set(route)
        assert route["degraded"] is False
        assert route["links"], "每条路线都要带跳转链接"

    assert (driving["mode"], driving["label"], driving["kind"], driving["source"]) == (
        "driving", "驾车", "real", "OSRM"
    )
    assert driving["duration_min"] == 94 and driving["distance_km"] == 122.4
    assert driving["cost_cny"] == 116, "驾车费用 = 油费 + 高速过路费(估算)"
    assert driving["geometry"] == DEFAULT_LEG["geometry"]
    assert "OSRM" in driving["note"] and "费用为估算" in driving["note"]
    assert route_service.ESTIMATE_DISCLAIMER in driving["note"]

    assert (rail["mode"], rail["kind"], rail["source"]) == ("rail", "estimate", "estimate")
    assert rail["label"] == "铁路(估算)" and rail["emoji"] == "🚄"
    assert rail["distance_km"] == pytest.approx(350.5, abs=0.05)
    assert rail["cost_cny"] == 189, "350.5km × 1.2 × 0.45 ≈ 189 元"
    assert rail["duration_min"] == 225 and rail["geometry"] is None
    assert route_service.ESTIMATE_DISCLAIMER in rail["note"] and "无实时班次" in rail["note"]

    assert (flight["mode"], flight["kind"]) == ("flight", "estimate")
    assert flight["label"] == "飞机(估算)" and flight["emoji"] == "✈️"
    assert flight["cost_cny"] == 331, "350.5km × 1.1 × 0.6 + 100 ≈ 331 元"
    assert flight["duration_min"] == 240
    assert route_service.ESTIMATE_DISCLAIMER in flight["note"] and "无实时航班" in flight["note"]


def test_driving_route_calls_osrm_with_lnglat_pairs() -> None:
    router = FakeRouter()
    dest = point_north(120.0)
    route_service.plan_routes(
        from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"],
        to_lat=dest["lat"], to_lng=dest["lng"], to_name="莫干山", router=router,
    )
    # OSRM 坐标顺序是 (lng, lat),别和 Leaflet 的 [lat, lng] 搞混
    assert router.calls == [
        ((SHANGHAI["lng"], SHANGHAI["lat"]), (dest["lng"], dest["lat"]))
    ]


def test_geometry_is_thinned_but_keeps_endpoints() -> None:
    points = [[31.0 + index * 0.001, 121.0] for index in range(5000)]
    router = FakeRouter(leg={"distance_km": 400.0, "duration_min": 300.0, "geometry": points})
    plan = plan_for(350.5, router=router)

    geometry = plan.routes[0]["geometry"]
    assert geometry is not None and len(geometry) == route_service.GEOMETRY_MAX_POINTS
    assert geometry[0] == points[0] and geometry[-1] == points[-1], "抽稀要留住首尾点"

    full = plan_for(350.5, router=FakeRouter(leg={"distance_km": 400.0, "duration_min": 300.0,
                                                 "geometry": points}),
                    geometry_max_points=None)
    assert len(full.routes[0]["geometry"]) == 5000, "max_points=None 时不抽稀"
    assert route_service.thin_geometry(points[:10]) == points[:10]
    assert route_service.thin_geometry([]) is None and route_service.thin_geometry(None) is None
    assert len(route_service.thin_geometry(points, 5)) == 5


# --------------------------------------------------------------------------- #
# deep-link 纯函数(高德 / Google / 12306 / OTA 各一条)
# --------------------------------------------------------------------------- #


def test_amap_navigation_url_orders_lng_lat_and_encodes_chinese() -> None:
    url = route_service.amap_navigation_url(
        from_lng=121.4737, from_lat=31.2304, from_name="上海",
        to_lng=116.4074, to_lat=39.9042, to_name="北京滑雪场",
    )
    assert url.startswith("https://uri.amap.com/navigation?")
    assert "%E5%8C%97%E4%BA%AC" in url, "中文地名要按 UTF-8 百分号编码"
    query = query_of(url)
    assert query["from"] == ["121.473700,31.230400,上海"], "高德是 lng,lat,name(经度在前)"
    assert query["to"] == ["116.407400,39.904200,北京滑雪场"]
    assert query["mode"] == ["car"] and query["policy"] == ["1"]
    assert query["src"] == ["where2go"] and query["callnative"] == ["1"]


def test_google_maps_directions_url_uses_lat_lng_and_travelmode() -> None:
    url = route_service.google_maps_directions_url(
        from_lat=31.2304, from_lng=121.4737, to_lat=39.9042, to_lng=116.4074
    )
    assert url == (
        "https://www.google.com/maps/dir/?api=1"
        "&origin=31.230400,121.473700&destination=39.904200,116.407400&travelmode=driving"
    ), "Google 是 lat,lng(纬度在前),且必须带 api=1"


def test_rail_12306_url_carries_station_names_and_date() -> None:
    url = route_service.rail_12306_url(from_name="上海", to_name="北京", date="2026-09-20")
    assert url == (
        "https://kyfw.12306.cn/otn/leftTicket/init?linktypeid=dc"
        "&fs=%E4%B8%8A%E6%B5%B7&ts=%E5%8C%97%E4%BA%AC&date=2026-09-20&flag=N,N,Y"
    )
    assert query_of(url)["ts"] == ["北京"], "编码后的中文站名要能解回来"


def test_flight_ota_url_carries_city_names_and_date() -> None:
    url = route_service.flight_ota_url(from_name="上海", to_name="北京", date="2026-09-20")
    assert url.startswith("https://flight.qunar.com/site/oneway_list.htm?")
    assert "%E4%B8%8A%E6%B5%B7" in url and "%E5%8C%97%E4%BA%AC" in url
    query = query_of(url)
    assert query["searchDepartureAirport"] == ["上海"]
    assert query["searchArrivalAirport"] == ["北京"]
    assert query["searchDepartureTime"] == ["2026-09-20"]


def test_deep_links_omit_unknown_names_instead_of_inventing_them() -> None:
    amap = route_service.amap_navigation_url(to_lng=116.4074, to_lat=39.9042)
    assert "from=" not in amap, "起点未知就省略,高德会自动用当前位置"
    assert query_of(amap)["to"] == ["116.407400,39.904200"]

    rail = route_service.rail_12306_url(to_name=None, from_name="  ", date="2026-09-20")
    assert "fs=" not in rail and "ts=" not in rail and "date=2026-09-20" in rail

    ota = route_service.flight_ota_url(to_name=None, from_name=None, date="2026-09-20")
    assert "searchDepartureAirport" not in ota and "searchArrivalAirport" not in ota

    google = route_service.google_maps_directions_url(to_lat=39.9042, to_lng=116.4074)
    assert "origin=" not in google and "destination=39.904200,116.407400" in google


def test_default_departure_date_is_today_in_china_time() -> None:
    cst = timezone(timedelta(hours=8))
    assert route_service.default_departure_date() == datetime.now(cst).date().isoformat()
    assert route_service.default_departure_date(date(2026, 10, 1)) == "2026-10-01"
    assert route_service.default_departure_date(datetime(2026, 10, 1, 8, 30, tzinfo=cst)) == "2026-10-01"
    assert "date=2026-10-01" in route_service.rail_12306_url(to_name="北京", date="2026-10-01")
    today = datetime.now(cst).date().isoformat()
    assert f"searchDepartureTime={today}" in route_service.flight_ota_url(to_name="北京")


def test_route_links_are_attached_per_mode(stub_osrm) -> None:
    stub_osrm()
    driving, rail, flight = plan_for(350.5, to_name="天目湖", from_name="上海").routes

    assert [link["provider"] for link in driving["links"]] == ["amap", "google"]
    assert [link["provider"] for link in rail["links"]] == ["12306"]
    assert [link["provider"] for link in flight["links"]] == [route_service.LINK_PROVIDER_OTA]

    for link in driving["links"] + rail["links"] + flight["links"]:
        assert set(link) == LINK_KEYS
        assert link["url"].startswith("https://") and link["label"] and link["note"]

    assert "天目湖" in query_of(rail["links"][0]["url"])["ts"][0]
    assert "天目湖" in query_of(flight["links"][0]["url"])["searchArrivalAirport"][0]
    assert query_of(driving["links"][0]["url"])["to"][0].endswith(",天目湖")
    expect_error(
        lambda: route_service.route_links("bike", from_lat=31.0, from_lng=121.0,
                                          to_lat=32.0, to_lng=122.0),
        ValueError, "未知出行方式",
    )


def test_missing_place_names_fall_back_to_coordinates(stub_osrm) -> None:
    stub_osrm()
    plan = plan_for(150.0, to_name=None, from_name=None)

    assert plan.origin["name"] == "我的位置(31.2304,121.4737)"
    assert plan.destination["name"].startswith("目的地(")
    rail_link = plan.routes[1]["links"][0]["url"]
    assert "ts=" not in rail_link, "没有地名就别编站名,留给用户在 12306 页面上选"
    amap_query = query_of(plan.routes[0]["links"][0]["url"])
    assert amap_query["to"] == [f"{SHANGHAI['lng']:.6f},{plan.destination['lat']:.6f}"]


# --------------------------------------------------------------------------- #
# OSRM 失败:驾车条目降级,不 500、不瞎估
# --------------------------------------------------------------------------- #


def test_osrm_failure_degrades_driving_entry() -> None:
    router = FakeRouter(error=DataSourceError("OSRM", "请求超时(>15s)"))
    plan = plan_for(350.5, to_name="天目湖", router=router)
    driving, rail, flight = plan.routes

    assert driving["degraded"] is True and driving["kind"] == "estimate"
    assert driving["source"] == route_service.SOURCE_UNAVAILABLE
    assert driving["duration_min"] is None and driving["cost_cny"] is None
    assert driving["distance_km"] is None and driving["geometry"] is None
    assert "OSRM" in driving["note"] and route_service.ESTIMATE_DISCLAIMER in driving["note"]
    assert [link["provider"] for link in driving["links"]] == ["amap", "google"], "降级也要能跳转导航"
    assert rail["cost_cny"] == 189 and flight["cost_cny"] == 331, "估算方式不受 OSRM 影响"


def test_osrm_invalid_coordinates_degrade_instead_of_raising() -> None:
    """router 抛 ValueError(坐标不在路网/格式不对)时同样只降级。"""
    router = FakeRouter(error=ValueError("坐标必须包含 2 个分量(lng, lat)"))
    driving = plan_for(150.0, router=router).routes[0]
    assert driving["degraded"] is True and driving["duration_min"] is None


# --------------------------------------------------------------------------- #
# /api/routes
# --------------------------------------------------------------------------- #


def test_api_routes_payload_shape(stub_osrm) -> None:
    router = stub_osrm()
    dest = point_north(350.5, name="天目湖")
    payload = routes_api.list_routes(
        from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"],
        to_lat=dest["lat"], to_lng=dest["lng"], to_name="天目湖", from_name="上海",
    )

    assert set(payload) == {
        "from", "to", "distance_km", "routes", "count",
        "mode_rules", "cost_model", "generated_at", "elapsed_s", "note",
    }
    assert payload["from"] == {"lat": SHANGHAI["lat"], "lng": SHANGHAI["lng"], "name": "上海"}
    assert payload["to"]["name"] == "天目湖" and payload["to"]["lat"] == pytest.approx(dest["lat"])
    assert payload["distance_km"] == pytest.approx(350.5, abs=0.05)
    assert payload["count"] == 3 == len(payload["routes"])
    assert [route["mode"] for route in payload["routes"]] == ["driving", "rail", "flight"]
    assert payload["routes"][0]["kind"] == "real" and payload["routes"][1]["kind"] == "estimate"
    assert payload["mode_rules"]["rail_min_km"] == 100.0
    assert payload["cost_model"]["disclaimer"] == route_service.ESTIMATE_DISCLAIMER
    assert payload["generated_at"] and payload["elapsed_s"] >= 0
    assert "估算" in payload["note"] and "OSRM" in payload["note"]
    assert router.calls == [((SHANGHAI["lng"], SHANGHAI["lat"]), (dest["lng"], dest["lat"]))]


def test_api_routes_rejects_bad_params(stub_osrm) -> None:
    router = stub_osrm()
    ok = {"from_lat": 31.2304, "from_lng": 121.4737, "to_lat": 32.0, "to_lng": 121.0}

    for missing in ("from_lat", "from_lng", "to_lat", "to_lng"):
        expect_http_error(
            lambda name=missing: routes_api.list_routes(**{**ok, name: None}),
            400, "缺少必要参数", missing,
        )
    expect_http_error(lambda: routes_api.list_routes(**{**ok, "from_lat": "   "}),
                      400, "缺少必要参数", "from_lat")
    expect_http_error(lambda: routes_api.list_routes(**{**ok, "from_lat": 91.0}),
                      400, "from_lat", "[-90, 90]")
    expect_http_error(lambda: routes_api.list_routes(**{**ok, "to_lng": -181.0}),
                      400, "to_lng", "[-180, 180]")
    expect_http_error(lambda: routes_api.list_routes(**{**ok, "to_lat": "abc"}),
                      400, "to_lat", "必须为数字")
    expect_http_error(lambda: routes_api.list_routes(**{**ok, "to_lat": float("nan")}),
                      400, "不是有效数字")
    assert router.calls == [], "参数非法时应快速失败,不触网"


def test_api_routes_degrades_when_osrm_fails(stub_osrm) -> None:
    stub_osrm(error=DataSourceError("OSRM", "所有端点均不可用"))
    dest = point_north(150.0, name="莫干山")
    payload = routes_api.list_routes(
        from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"],
        to_lat=dest["lat"], to_lng=dest["lng"], to_name="莫干山", from_name="上海",
    )

    assert payload["count"] == 2, "驾车降级但条目仍在,铁路照常给"
    driving, rail = payload["routes"]
    assert driving["mode"] == "driving" and driving["degraded"] is True
    assert driving["duration_min"] is None and driving["cost_cny"] is None
    assert "OSRM" in driving["note"]
    assert len(driving["links"]) == 2
    assert rail["mode"] == "rail" and rail["cost_cny"] == 81 and rail["kind"] == "estimate"


def test_api_routes_accepts_string_coordinates(stub_osrm) -> None:
    """query 参数在 HTTP 层是字符串;服务层要能吃 ``"31.2304"`` 这种写法。"""
    stub_osrm()
    dest = point_north(120.0, name="莫干山")
    payload = routes_api.list_routes(
        from_lat=str(SHANGHAI["lat"]), from_lng=str(SHANGHAI["lng"]),
        to_lat=f"{dest['lat']:.6f}", to_lng=f"{dest['lng']:.6f}", to_name=" 莫干山 ",
    )
    assert payload["count"] == 2
    assert payload["to"]["name"] == "莫干山", "地名要去空白"
    assert payload["from"]["name"] == "我的位置(31.2304,121.4737)"


def test_http_layer_rejects_bad_coordinates_with_400(stub_osrm) -> None:
    """走完整 FastAPI 校验链:缺参/空串/非数字/越界一律 **400 + 中文说明**(不是 422)。"""
    router = stub_osrm()
    tail = "from_lng=121.4737&to_lat=32.0&to_lng=121.0"
    cases = [
        ("", "缺少必要参数:from_lat"),
        (f"from_lat=&{tail}", "缺少必要参数:from_lat"),
        (f"from_lat=abc&{tail}", "必须为数字"),
        (f"from_lat=91&{tail}", "[-90, 90]"),
        ("from_lat=31.2304&from_lng=181&to_lat=32.0&to_lng=121.0", "[-180, 180]"),
        ("from_lat=31.2304&from_lng=121.4737&to_lat=32.0", "缺少必要参数:to_lng"),
    ]
    for query, fragment in cases:
        status, payload = http_get(query)
        assert status == 400, f"{query!r} 应为 400,实际 {status}:{payload}"
        assert fragment in payload["detail"], f"{query!r} 的 detail 应含 {fragment!r}"
    assert router.calls == [], "参数非法时应快速失败,不触网"


def test_http_layer_returns_full_payload_for_string_query(stub_osrm) -> None:
    """HTTP 层 query 全是字符串:``"31.2304"`` 要能规划出三方式,响应可直接 JSON 序列化。"""
    stub_osrm()
    dest = point_north(350.5)
    status, payload = http_get(
        f"from_lat={SHANGHAI['lat']}&from_lng={SHANGHAI['lng']}"
        f"&to_lat={dest['lat']:.6f}&to_lng={dest['lng']:.6f}"
        "&to_name=%E5%A4%A9%E7%9B%AE%E6%B9%96&from_name=%E4%B8%8A%E6%B5%B7"
    )

    assert status == 200
    assert payload["count"] == 3 == len(payload["routes"])
    assert payload["to"]["name"] == "天目湖" and payload["from"]["name"] == "上海"
    assert payload["distance_km"] == pytest.approx(350.5, abs=0.05)
    assert payload["routes"][0]["kind"] == "real" and payload["routes"][0]["geometry"]
    assert [route["mode"] for route in payload["routes"]] == ["driving", "rail", "flight"]
    assert payload["cost_model"]["rail"]["cny_per_km"] == 0.45


def test_plan_routes_generated_at_is_injectable() -> None:
    router = FakeRouter()
    now = datetime(2026, 9, 11, 6, 30, tzinfo=timezone.utc)
    plan = plan_for(120.0, router=router, now=now)
    assert plan.generated_at == "2026-09-11T06:30:00+00:00"
    assert isinstance(plan.origin, dict) and isinstance(plan.routes, list)
