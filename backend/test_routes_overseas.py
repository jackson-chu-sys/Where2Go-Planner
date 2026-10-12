"""TASK-22 单测(契约 §2 的 1~4、7 组):境外轨驾车 —— OSRM 复活 + ``region`` 分支。

全程不触网:

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,偷偷联网当场失败;
* OSRM 数据源层用 :class:`FakeSession` 替身(照 ``test_osm_overseas.py`` 的样板)跑
  **真实响应形状**;服务层用 monkeypatch 顶掉 :func:`data_sources.osrm.route` 与
  :func:`data_sources.amap.driving`(两轨互为"零调用"断言:走境外就不许碰高德,反之亦然);
* API 既直调路由函数,也用 :func:`http_get` 拼最小 ASGI scope 走一遍**完整 HTTP 链**
  (仓库没装 httpx/TestClient),验 ``region`` 的 Query 接线与 400 中文契约。

覆盖:osrm.py 恢复完整性 / :func:`services.routes.osrm_leg` 归一形状 /
``default_router_osrm`` 坐标序与失败上抛 / ``driving_route(region=overseas)`` 的
heuristic 过路费与境外 note 文案 / 境内高德轨**逐字零变化** / ``plan_routes`` 只把 region
透传给驾车(铁路/飞机估算腿不分轨)/ ``/api/routes`` 只多一个 ``region`` 键 /
前端 ``loadRoutes`` 的开关接线。

运行:``cd backend && ../.venv/bin/python -m pytest test_routes_overseas.py -q``
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
from typing import Any, Callable, Optional

import pytest
import requests
from fastapi import HTTPException

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import data_sources as ds  # noqa: E402
from app.api import routes as routes_api  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from data_sources import osrm  # noqa: E402
from data_sources import haversine_km  # noqa: E402
from data_sources._common import (  # noqa: E402
    DEFAULT_SOURCE_PROXY,
    PROXY_OFF,
    DataSourceError,
    apply_proxy_policy,
)
from services import routes as route_service  # noqa: E402
from services.region_gate import REGION_DOMESTIC, REGION_OVERSEAS  # noqa: E402

# --------------------------------------------------------------------------- #
# 样本:因特拉肯 → 格林德瓦(契约 §0 的实测锚点:19.7 km / 24 min,WGS-84)
# --------------------------------------------------------------------------- #

INTERLAKEN = {"lat": 46.6863, "lng": 7.8632, "name": "因特拉肯"}
GRINDELWALD = {"lat": 46.6240, "lng": 8.0360, "name": "格林德瓦"}
ROME = {"lat": 41.9028, "lng": 12.4964, "name": "罗马"}

#: OSRM demo 服务器的真实响应形状(2026-10-11 实测裁剪):legs[].steps 通常为空、无 tolls
OSRM_PAYLOAD: dict[str, Any] = {
    "code": "Ok",
    "routes": [{
        "distance": 19712.4,
        "duration": 1452.8,
        "weight": 1452.8,
        "weight_name": "routability",
        "geometry": {
            "type": "LineString",
            "coordinates": [[7.8632, 46.6863], [7.9500, 46.6500], [8.0360, 46.6240]],
        },
        "legs": [{"distance": 19712.4, "duration": 1452.8, "steps": []}],
    }],
    "waypoints": [{"distance": 0.0, "name": ""}, {"distance": 19712.4, "name": ""}],
}

OSRM_LEG = {"distance_km": 19.712, "duration_min": 24.2, "geometry": [[46.6863, 7.8632], [46.624, 8.036]]}

#: 高德驾车腿的形状(:data:`data_sources.amap.DRIVING_KEYS`),境内轨零变化断言用
AMAP_LEG = {
    "distance_m": 122_400.0,
    "duration_s": 5_640.0,
    "toll_distance_m": 98_000.0,
    "traffic_lights": 12,
    "steps_n": 34,
    "polyline": [[31.2304, 121.4737], [31.8, 121.2]],
}


@dataclasses.dataclass
class Call:
    """一次 HTTP 调用的关键参数快照。"""

    method: str
    url: str
    params: Optional[dict[str, Any]]
    timeout: Optional[float]
    headers: dict[str, str]


class FakeResponse:
    """最小 response 替身:只需要 ``status_code`` 与 ``text``。"""

    def __init__(self, payload: Any = None, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = "" if payload is None else json.dumps(payload, ensure_ascii=False)


class FakeSession:
    """记录调用参数并按顺序返回预设响应的 session 替身(不触网)。"""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses) or [FakeResponse({})]
        self.calls: list[Call] = []

    def request(self, method: str, url: str, params: Optional[dict[str, Any]] = None,
                data: Optional[dict[str, Any]] = None, timeout: Optional[float] = None,
                headers: Optional[dict[str, str]] = None, **kwargs: Any) -> FakeResponse:
        self.calls.append(Call(method, url, params, timeout, dict(headers or {})))
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(response, BaseException):
            raise response
        return response


class RecordingOsrm:
    """:func:`data_sources.osrm.route` 替身:记下坐标序与关键字参数,回预设 leg 或抛错。"""

    def __init__(self, leg: Optional[dict[str, Any]] = None,
                 error: Optional[BaseException] = None) -> None:
        self.leg = dict(OSRM_LEG if leg is None else leg)
        self.error = error
        self.calls: list[tuple[tuple[float, float], tuple[float, float], dict[str, Any]]] = []

    def __call__(self, start_lnglat: Any, end_lnglat: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((tuple(start_lnglat), tuple(end_lnglat), dict(kwargs)))
        if self.error is not None:
            raise self.error
        return dict(self.leg)


def boom_amap_driving(*args: Any, **kwargs: Any) -> Any:
    """境外轨用例的高德替身:被调到就说明"两轨串了"。"""
    raise AssertionError("region=overseas 不该调高德驾车")


def boom_osrm_route(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("境内轨不该调 OSRM")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底:任何 requests 调用都视为测试失败(本套单测必须纯 mock)。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture()
def fake_osrm(monkeypatch: pytest.MonkeyPatch) -> Callable[..., RecordingOsrm]:
    """把服务层看到的 :func:`data_sources.osrm.route` 换成替身(返回工厂)。"""

    def install(leg: Optional[dict[str, Any]] = None,
                error: Optional[BaseException] = None) -> RecordingOsrm:
        recorder = RecordingOsrm(leg=leg, error=error)
        monkeypatch.setattr(route_service.osrm, "route", recorder)
        return recorder

    return install


@pytest.fixture()
def fake_amap(monkeypatch: pytest.MonkeyPatch) -> Callable[..., dict[str, Any]]:
    """把 :func:`data_sources.amap.driving` 换成回预设 leg 的替身(境内轨零变化断言用)。"""

    def install(leg: Optional[dict[str, Any]] = None) -> dict[str, list[Any]]:
        calls: dict[str, list[Any]] = {"args": []}
        body = dict(AMAP_LEG if leg is None else leg)

        def driving(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls["args"].append((args, kwargs))
            return dict(body)

        monkeypatch.setattr(route_service.amap, "driving", driving)
        return calls

    return install


def driving_kwargs(**overrides: Any) -> dict[str, Any]:
    """因特拉肯 → 格林德瓦的 :func:`services.routes.driving_route` 入参。"""
    kwargs: dict[str, Any] = {
        "from_lat": INTERLAKEN["lat"], "from_lng": INTERLAKEN["lng"],
        "to_lat": GRINDELWALD["lat"], "to_lng": GRINDELWALD["lng"],
        "to_name": GRINDELWALD["name"], "from_name": INTERLAKEN["name"],
    }
    kwargs.update(overrides)
    return kwargs


def http_get(query: str) -> tuple[int, Any]:
    """直接驱动 ASGI app 走一遍完整 HTTP 链(含 FastAPI 参数校验),返回 ``(状态码, JSON)``。"""
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
            status["code"] = int(message["status"])
        elif message["type"] == "http.response.body":
            body.extend(message.get("body") or b"")

    asyncio.run(fastapi_app(scope, receive, send))
    return status["code"], json.loads(body.decode() or "null")


# --------------------------------------------------------------------------- #
# 1. osrm.py 恢复完整性(契约 §1.1 / §2.1)
# --------------------------------------------------------------------------- #


def test_osrm_module_is_restored_with_historical_surface() -> None:
    """零改动恢复:源名、端点、公开符号与 ``_common`` 依赖都在(契约 §1.1)。"""
    assert osrm.SOURCE_NAME == "OSRM"
    assert osrm.DEFAULT_ENDPOINT == "https://router.project-osrm.org"
    assert osrm.ALT_ENDPOINT == "https://routing.openstreetmap.de/routed-car"
    for name in ("format_lnglat", "parse_geometry", "parse_steps", "parse_route",
                 "OsrmClient", "default_client", "route"):
        assert hasattr(osrm, name), f"osrm.{name} 应随恢复一起回来"
    assert osrm.format_lnglat((7.8632, 46.6863)) == "7.863200,46.686300"


def test_osrm_parse_route_and_geometry_from_real_payload() -> None:
    """真实响应形状跑得通:米/秒 → km/分钟、GeoJSON ``[lng,lat]`` → ``[lat,lng]``。"""
    plain = osrm.parse_route(OSRM_PAYLOAD)
    assert plain == {"distance_km": 19.712, "duration_min": 24.2}, "默认形状与阶段0 一致(无 geometry/steps)"
    full = osrm.parse_route(OSRM_PAYLOAD, with_geometry=True, with_steps=True)
    assert full["geometry"] == [[46.6863, 7.8632], [46.65, 7.95], [46.624, 8.036]]
    assert full["steps"] == [], "demo 服务器没有分段明细 → 空列表(费用引擎据此退 heuristic)"
    assert osrm.parse_geometry({"type": "LineString", "coordinates": [[8.036, 46.624]]}) == [[46.624, 8.036]]
    assert osrm.parse_geometry(None) is None


def test_osrm_parse_route_raises_datasource_error_on_bad_payload() -> None:
    """``code != Ok`` / 没有 routes / 距离非正 → :class:`DataSourceError`(中文说明)。"""
    with pytest.raises(DataSourceError) as caught:
        osrm.parse_route({"code": "NotFound", "message": "Impossible route"})
    assert "NotFound" in str(caught.value) and "路线规划失败" in str(caught.value)
    with pytest.raises(DataSourceError):
        osrm.parse_route({"code": "Ok", "routes": []})
    with pytest.raises(DataSourceError):
        osrm.parse_route({"code": "Ok", "routes": [{"distance": 0, "duration": 10}]})
    with pytest.raises(DataSourceError):
        osrm.parse_route(["not", "a", "dict"])


def test_osrm_client_builds_driving_url_with_lnglat_order() -> None:
    """客户端 URL/参数口径没被改坏:``/route/v1/driving/lng,lat;lng,lat`` + overview/steps。"""
    session = FakeSession(FakeResponse(OSRM_PAYLOAD))
    client = osrm.OsrmClient(session=session)
    leg = client.route((7.8632, 46.6863), (8.0360, 46.6240), with_geometry=True)
    assert leg["distance_km"] == 19.712 and leg["duration_min"] == 24.2
    call = session.calls[0]
    assert call.method == "GET"
    assert call.url.endswith("/route/v1/driving/7.863200,46.686300;8.036000,46.624000")
    assert call.params["overview"] == "full" and call.params["geometries"] == "geojson"
    assert call.params["steps"] == "false" and call.params["alternatives"] == "false"
    assert leg["geometry"] == [[46.6863, 7.8632], [46.65, 7.95], [46.624, 8.036]]


def test_osrm_proxy_policy_is_direct_connect_and_out_of_export_surface() -> None:
    """§1.1:代理表新增 ``"osrm": PROXY_OFF``;包导出面仍不含 OSRM 符号(退役断言成立)。"""
    assert DEFAULT_SOURCE_PROXY["osrm"] == PROXY_OFF
    session = FakeSession()
    session.trust_env = True  # type: ignore[attr-defined]
    session.proxies = {"http": "http://127.0.0.1:7892"}  # type: ignore[attr-defined]
    assert apply_proxy_policy(session, "osrm") == {}, "off = 直连(proxies 清空)"
    for retired in ("route", "OsrmClient", "OSRM_ENDPOINT", "OSRM_ALT_ENDPOINT"):
        assert retired not in ds.__all__, f"{retired} 不该回到 data_sources.__all__"
        assert not hasattr(ds, retired), f"{retired} 不该被包级导出"
    assert "osrm" not in ds.__all__, "境外轨模块与 osm_overseas 同口径:不进包导出面"


def test_osrm_leg_normalizes_to_amap_leg_shape() -> None:
    """§1.2:归一成 :func:`amap_leg` 同形状,``toll_distance_km`` 恒 ``None``。"""
    leg = route_service.osrm_leg(OSRM_LEG)
    amap_shape = route_service.amap_leg(AMAP_LEG)
    assert set(leg) == set(amap_shape), "两轨的 leg 形状必须逐键一致(下游只认这一种)"
    assert leg["distance_km"] == pytest.approx(19.712)
    assert leg["duration_min"] == pytest.approx(24.2)
    assert leg["toll_distance_km"] is None, "OSRM demo 无收费数据 → 费用引擎自动退 heuristic"
    assert leg["traffic_lights"] is None
    assert leg["geometry"] == [[46.6863, 7.8632], [46.624, 8.036]]
    assert leg["steps_n"] is None


def test_osrm_leg_accepts_raw_meters_seconds_and_steps() -> None:
    """契约 §1.2 的 ``{distance_m, duration_s, geometry, steps}`` 口径也吃得下。"""
    leg = route_service.osrm_leg({
        "distance_m": 19_712.4, "duration_s": 1_452.8,
        "geometry": [[46.6863, 7.8632]], "steps": [{"distance_km": 1.2, "ref": "A8"}],
    })
    assert leg["distance_km"] == pytest.approx(19.7124)
    assert leg["duration_min"] == pytest.approx(24.2133, abs=1e-3)
    assert leg["steps_n"] == 1 and leg["toll_distance_km"] is None


def test_default_router_osrm_keeps_lnglat_order_and_asks_geometry(fake_osrm) -> None:
    """境外默认路由:WGS-84 的 ``(lng, lat)`` **不换序**,并要折线。"""
    recorder = fake_osrm()
    leg = route_service.default_router_osrm((7.8632, 46.6863), (8.0360, 46.6240))
    assert recorder.calls == [((7.8632, 46.6863), (8.0360, 46.6240), {"with_geometry": True})]
    assert leg["distance_km"] == pytest.approx(19.712) and leg["toll_distance_km"] is None


def test_default_router_osrm_propagates_datasource_error(fake_osrm) -> None:
    """OSRM 失败**上抛**(:class:`DataSourceError`)—— 降级是 ``driving_route`` 的事。"""
    fake_osrm(error=DataSourceError("OSRM", "路线规划失败(code='NoRoute')"))
    with pytest.raises(DataSourceError):
        route_service.default_router_osrm((7.8632, 46.6863), (8.0360, 46.6240))
    fake_osrm(error=ValueError("坐标必须为数字"))
    with pytest.raises(ValueError):
        route_service.default_router_osrm((7.8632, 46.6863), (8.0360, 46.6240))


# --------------------------------------------------------------------------- #
# 2. driving_route(region=overseas)(契约 §1.2 / §2.2)
# --------------------------------------------------------------------------- #


def test_driving_route_overseas_defaults_to_osrm_and_never_touches_amap(
    fake_osrm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """境外轨默认路由 = OSRM;高德**零调用**;``source`` 照实报 ``osrm``。"""
    monkeypatch.setattr(route_service.amap, "driving", boom_amap_driving)
    recorder = fake_osrm()
    route = route_service.driving_route(**driving_kwargs(region=REGION_OVERSEAS))
    assert len(recorder.calls) == 1, "境外轨只打一次 OSRM"
    assert route["kind"] == "real" and route["degraded"] is False
    assert route["source"] == route_service.SOURCE_OSRM == "osrm"
    assert route["distance_km"] == 19.7 and route["duration_min"] == 24
    assert route["geometry"] and route["geometry"][0] == [46.6863, 7.8632]
    assert route["mode"] == "driving" and route["links"], "deep-link 两轨都给"


def test_driving_route_overseas_toll_is_heuristic_with_overseas_note(fake_osrm) -> None:
    """过路费只能启发式(里程×0.55),note 里明示"境外费率按中国西部档估算,仅供参考"。"""
    fake_osrm()
    route = route_service.driving_route(**driving_kwargs(region=REGION_OVERSEAS))
    assert route["cost_breakdown"]["mode"] == route_service.TOLL_MODE_HEURISTIC
    breakdown = route_service.driving_cost_breakdown(
        route["distance_km"], toll_distance_km=None, region=route_service.REGION_WEST
    )
    assert route["cost_breakdown"]["toll"] == route_service.round_cost(breakdown["toll_cny"])
    assert route["cost_cny"] == route["cost_breakdown"]["toll"] + route["cost_breakdown"]["fuel"]
    assert route["per_person_cny"] == route_service.round_cost(route["cost_cny"] / 4)
    for fragment in ("境外费率按中国西部档估算,仅供参考", "OSRM", route_service.TOLL_MODE_HEURISTIC):
        assert fragment in route["note"], f"境外 note 应包含 {fragment!r}:{route['note']}"


def test_driving_route_overseas_degrades_without_raising(fake_osrm) -> None:
    """OSRM 挂了只降级(不 500、不瞎估):数字全 null、note 换成境外降级文案、链接照给。"""
    fake_osrm(error=DataSourceError("OSRM", "服务端错误(HTTP 503)"))
    route = route_service.driving_route(**driving_kwargs(region=REGION_OVERSEAS))
    assert route["kind"] == "estimate" and route["degraded"] is True
    assert route["source"] == route_service.SOURCE_UNAVAILABLE
    assert route["note"] == route_service.OVERSEAS_DRIVING_DEGRADED_NOTE
    for key in ("duration_min", "cost_cny", "cost_breakdown", "per_person_cny",
                "distance_km", "geometry"):
        assert route[key] is None, f"降级时 {key} 必须是 null"
    assert len(route["links"]) == 2 and route["links"][1]["provider"] == route_service.LINK_PROVIDER_GOOGLE


def test_driving_route_domestic_track_is_unchanged(fake_amap, monkeypatch: pytest.MonkeyPatch) -> None:
    """境内轨(缺省 / ``domestic``)逐字零变化:高德被调、OSRM 零调用、toll 走真实值。"""
    monkeypatch.setattr(route_service.osrm, "route", boom_osrm_route)
    calls = fake_amap()
    for region in (None, REGION_DOMESTIC, " DOMESTIC "):
        route = route_service.driving_route(
            from_lat=31.2304, from_lng=121.4737, to_lat=32.5, to_lng=120.6,
            to_name="目的地", from_name="上海", region=region,
        )
        assert route["source"] == route_service.SOURCE_AMAP and route["kind"] == "real"
        assert route["cost_breakdown"]["mode"] == route_service.TOLL_MODE_AMAP_TOLL_DISTANCE
        assert route["distance_km"] == 122.4 and route["duration_min"] == 94
        assert route_service.OVERSEAS_DRIVING_NOTE not in route["note"]
        assert "OSRM" not in route["note"]
    assert len(calls["args"]) == 3, "三种写法都走高德"


def test_driving_route_injected_router_beats_region(fake_osrm) -> None:
    """``router=`` 显式注入优先于 region 分支(既有单测口径不变)。"""
    recorder = fake_osrm()
    seen: list[Any] = []

    def injected(start_lnglat: Any, end_lnglat: Any) -> dict[str, Any]:
        seen.append((tuple(start_lnglat), tuple(end_lnglat)))
        return {"distance_km": 21.0, "duration_min": 30.0, "toll_distance_km": None,
                "traffic_lights": None, "steps_n": None, "geometry": []}

    route = route_service.driving_route(**driving_kwargs(region=REGION_OVERSEAS, router=injected))
    assert seen == [((INTERLAKEN["lng"], INTERLAKEN["lat"]), (GRINDELWALD["lng"], GRINDELWALD["lat"]))]
    assert recorder.calls == [], "注入了 router 就不该再打 OSRM"
    assert route["distance_km"] == 21.0 and route["source"] == route_service.SOURCE_OSRM
    assert route_service.OVERSEAS_DRIVING_NOTE in route["note"], "note 仍按境外口径拼"


def test_driving_track_normalizes_region() -> None:
    """``driving_track`` 只认 overseas,其余(含垃圾值)按境内 —— 服务层不抛。"""
    assert route_service.driving_track(REGION_OVERSEAS) == REGION_OVERSEAS
    assert route_service.driving_track(" Overseas ") == REGION_OVERSEAS
    for value in (None, "", REGION_DOMESTIC, "mars", 42, {"a": 1}):
        assert route_service.driving_track(value) == REGION_DOMESTIC, f"{value!r} 应按境内"


# --------------------------------------------------------------------------- #
# 3. plan_routes 透传(契约 §1.2 / §2.3)
# --------------------------------------------------------------------------- #


def test_plan_routes_passes_region_to_driving_only(fake_osrm, monkeypatch: pytest.MonkeyPatch) -> None:
    """短途境外对:只有驾车一条,且走的是 OSRM(高德零调用)。"""
    monkeypatch.setattr(route_service.amap, "driving", boom_amap_driving)
    recorder = fake_osrm()
    plan = route_service.plan_routes(
        from_lat=INTERLAKEN["lat"], from_lng=INTERLAKEN["lng"],
        to_lat=GRINDELWALD["lat"], to_lng=GRINDELWALD["lng"],
        to_name=GRINDELWALD["name"], from_name=INTERLAKEN["name"], region=REGION_OVERSEAS,
    )
    assert [route["mode"] for route in plan.routes] == ["driving"], "这个距离不该出铁路/飞机"
    assert plan.routes[0]["source"] == "osrm" and len(recorder.calls) == 1
    # 顶层 distance_km 是**直线**距离(haversine),驾车条目的 19.7 km 才是 OSRM 的真实路网里程
    straight = haversine_km(INTERLAKEN["lat"], INTERLAKEN["lng"], GRINDELWALD["lat"], GRINDELWALD["lng"])
    assert plan.distance_km == pytest.approx(straight, abs=0.05)
    assert plan.routes[0]["distance_km"] == 19.7 > plan.distance_km, "路网里程 > 直线里程"


def test_plan_routes_overseas_rail_and_flight_legs_are_not_region_aware(
    fake_osrm, fake_amap
) -> None:
    """境外长途对(因特拉肯→罗马 ≈ 690km):铁路/飞机估算腿与境内口径**逐项一致**。"""
    fake_osrm()
    fake_amap()  # 对照的那条 plan 走境内轨:高德也用替身(全程不触网)
    kwargs = dict(
        from_lat=INTERLAKEN["lat"], from_lng=INTERLAKEN["lng"],
        to_lat=ROME["lat"], to_lng=ROME["lng"], to_name=ROME["name"], from_name=INTERLAKEN["name"],
    )
    overseas = route_service.plan_routes(region=REGION_OVERSEAS, **kwargs)
    domestic = route_service.plan_routes(region=None, **kwargs)
    assert [route["mode"] for route in overseas.routes] == ["driving", "rail", "flight"]
    by_mode = {route["mode"]: route for route in overseas.routes}
    assert by_mode["driving"]["source"] == "osrm"
    # 铁路:境外城市对命中不了种子表 → 自然走分档费率估算(既有降级路径)
    rail = by_mode["rail"]
    assert rail["price_source"] == route_service.PRICE_SOURCE_ESTIMATE
    assert rail["kind"] == "estimate" and rail["cost_cny"] and rail["duration_min"] > 0
    # 飞机:两端都不在民航机场表里 → 不给票价(全 null),只留 deep-link
    flight = by_mode["flight"]
    assert flight["flight_low_cny"] is None and flight["flight_high_cny"] is None
    assert flight["cost_cny"] is None and flight["links"], "没票价也要给跳转"
    # 估算腿不分轨:去掉驾车那条,两轨结果逐键相同
    strip = lambda plan: [  # noqa: E731 - 局部小工具,只为对齐两条 plan 的估算腿
        {key: value for key, value in route.items()}
        for route in plan.routes if route["mode"] != "driving"
    ]
    assert strip(overseas)[0] == strip(domestic)[0], "铁路腿不该受 region 影响"
    assert {k: v for k, v in strip(overseas)[1].items() if k != "links"} == \
           {k: v for k, v in strip(domestic)[1].items() if k != "links"}, "飞机腿不该受 region 影响"


# --------------------------------------------------------------------------- #
# 4. /api/routes 的 region 键(契约 §1.3 / §2.4)
# --------------------------------------------------------------------------- #

LEGACY_ROUTE_KEYS = {
    "from", "to", "distance_km", "routes", "count",
    "mode_rules", "cost_model", "generated_at", "elapsed_s", "note",
}


def test_api_routes_overseas_adds_only_the_region_key(fake_osrm, monkeypatch: pytest.MonkeyPatch) -> None:
    """响应**只多一个** ``region`` 键;其余键逐键零变化,驾车条目报 osrm。"""
    monkeypatch.setattr(route_service.amap, "driving", boom_amap_driving)
    fake_osrm()
    body = routes_api.list_routes(
        from_lat="46.6863", from_lng="7.8632", to_lat="46.6240", to_lng="8.0360",
        to_name="格林德瓦", from_name="因特拉肯", region="overseas",
    )
    assert set(body) == LEGACY_ROUTE_KEYS | {"region"}
    assert body["region"] == "overseas"
    assert body["routes"][0]["source"] == "osrm"
    assert route_service.OVERSEAS_DRIVING_NOTE in body["routes"][0]["note"]
    assert body["note"] == routes_api.ROUTES_NOTE, "顶层 note 常量不动(境外文案在条目 note 里)"
    assert body["mode_rules"] == route_service.mode_rules()
    assert body["cost_model"] == route_service.cost_coefficients(), "费用系数/费率表零改动"


def test_api_routes_defaults_to_domestic_region(fake_amap, monkeypatch: pytest.MonkeyPatch) -> None:
    """没给 ``region`` → 生效值 ``domestic``,走现状高德轨(OSRM 零调用)。"""
    monkeypatch.setattr(route_service.osrm, "route", boom_osrm_route)
    fake_amap()
    body = routes_api.list_routes(
        from_lat="31.2304", from_lng="121.4737", to_lat="32.5", to_lng="120.6",
        to_name="目的地", from_name="上海",
    )
    assert body["region"] == REGION_DOMESTIC
    assert body["routes"][0]["source"] == route_service.SOURCE_AMAP
    assert body["routes"][0]["cost_breakdown"]["mode"] == route_service.TOLL_MODE_AMAP_TOLL_DISTANCE


def test_api_routes_invalid_region_returns_400_chinese(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法 ``region`` → **400 中文**,且一次网都不触(校验在编排之前)。"""
    monkeypatch.setattr(route_service.osrm, "route", boom_osrm_route)
    monkeypatch.setattr(route_service.amap, "driving", boom_amap_driving)
    for bad in ("mars", "OVERSEAS_X", "境内"):
        with pytest.raises(HTTPException) as caught:
            routes_api.list_routes(
                from_lat="46.6863", from_lng="7.8632", to_lat="46.6240", to_lng="8.0360",
                region=bad,
            )
        assert caught.value.status_code == 400
        assert str(caught.value.detail) == routes_api.REGION_INVALID_MSG == "region 只支持 domestic 或 overseas"


def test_api_routes_full_http_chain_carries_region(fake_osrm, monkeypatch: pytest.MonkeyPatch) -> None:
    """完整 HTTP 链(Query 接线):``&region=overseas`` 生效,非法值 400 中文。"""
    monkeypatch.setattr(route_service.amap, "driving", boom_amap_driving)
    recorder = fake_osrm()
    query = "from_lat=46.6863&from_lng=7.8632&to_lat=46.6240&to_lng=8.0360&region=overseas"
    status, body = http_get(query)
    assert status == 200 and body["region"] == "overseas" and len(recorder.calls) == 1
    assert body["routes"][0]["source"] == "osrm"
    status, detail = http_get(query.replace("overseas", "moon"))
    assert status == 400 and "region 只支持 domestic 或 overseas" in json.dumps(detail, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 7. 前端接线(契约 §1.6 / §2.7)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def html() -> str:
    path = os.path.join(BACKEND_DIR, "app", "static", "index.html")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def js_function(body: str, start_marker: str, end_marker: str) -> str:
    start = body.index(start_marker)
    return body[start:body.index(end_marker, start)]


def test_frontend_route_url_carries_region_only_in_overseas_mode(html: str) -> None:
    """``loadRoutes`` 的 URL 拼装:境外轨(开关或后端自动判定)才追加 ``region=overseas``。

    TASK-23 QA 修正:``/api/routes`` 后端**不做**自动判定(契约 §1.3 只认显式参数),
    只认开关会让「搜索境外城市但没勾开关」的境外驾车错走高德(实测降级 unavailable)。
    修正后口径 = ``isOverseasTrack()``(regionOverride || regionSeen==overseas)。
    """
    section = js_function(html, "async function loadRoutes(", "async function openRoutePanel(")
    assert 'if(isOverseasTrack()) params.set("region","overseas");' in section, \
        "/api/routes 要透传境外轨判定(后端据此走 OSRM 境外轨)"
    assert '"/api/routes?"+params.toString()' in section, "URL 仍由 URLSearchParams 拼(自动编码)"
    assert '"domestic"' not in section, "境内不带 region(默认就是境内高德轨)"
    for field in ("from_lat", "from_lng", "to_lat", "to_lng"):
        assert field in section, f"既有坐标参数 {field} 照旧拼"


def test_frontend_overseas_track_helper_reads_both_triggers(html: str) -> None:
    """``isOverseasTrack``:手动开关或后端自动判定(regionSeen)任一为境外都算境外轨。

    routes/stays 后端只认显式 region 参数(无 OriginCache 兜底),所以两个触发源
    都必须让前端显式带上——这是 TASK-23 真机 QA 抓到并修掉的口径缺口。
    """
    helper = js_function(html, "function isOverseasTrack(", "\n")
    assert "regionOverride" in helper and "regionSeen" in helper, "两个触发源都要认"
    assert 'toLowerCase()==="overseas"' in helper, "regionSeen 判定大小写宽容"
    # routes 与 stays 两条 URL 都走同一个 helper(口径唯一)
    routes = js_function(html, "async function loadRoutes(", "async function openRoutePanel(")
    stays = js_function(html, "async function loadStays(", "function openStaySection(")
    assert 'isOverseasTrack()) params.set("region","overseas")' in routes
    assert 'isOverseasTrack()) params.set("region","overseas")' in stays


def test_frontend_route_card_note_is_backend_driven(html: str) -> None:
    """境外口径文案由后端拼在条目 ``note`` 里,前端只透出(§1.6:卡片零改动)。"""
    assert "境外费率按中国西部档估算" not in html, "前端不该硬编码境外费率文案"
    assert "region=overseas" in html, "境外开关的传参口径仍在(TASK-21 的 /api/places 同款)"
    card = js_function(html, "function routeCardHtml(", "function renderRouteCards(")
    assert 'route.note' in card and 'esc(route.note)' in card, "路线卡片仍照旧透出后端 note"
    assert "routeMoneyHtml(route)" in card and "fmtKm(route.distance_km)" in card, \
        "费用/里程的展示口径不变(境外只是 note 文案与 source 值不同)"
