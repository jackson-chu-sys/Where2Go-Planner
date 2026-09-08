"""三个免费数据源(OSRM / Nominatim / Overpass)的纯 mock 单测。

不触网:用 :class:`FakeSession` 替身断言**请求 URL、参数、请求头、超时**与
**真实响应形状下的解析逻辑**(真实响应样本取自 2026-09-08 实测)。
真实网络的可达性验证由 ``data_sources/verify_poc.py`` 负责。

运行方式(二选一)::

    python -m pytest backend/ -q
    python backend/test_data_sources.py
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from typing import Any, Callable, Optional

import requests

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import data_sources as ds  # noqa: E402
from data_sources import nominatim, osrm, overpass  # noqa: E402
from data_sources._common import normalize_timeout  # noqa: E402

# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class Call:
    """一次 HTTP 调用的关键参数快照。"""

    method: str
    url: str
    params: Optional[dict[str, Any]]
    data: Optional[dict[str, Any]]
    timeout: Optional[float]
    headers: dict[str, str]


class FakeResponse:
    """最小 response 替身:只需要 ``status_code`` 与 ``text``。"""

    def __init__(self, payload: Any = None, *, status_code: int = 200, text: Optional[str] = None) -> None:
        self.status_code = status_code
        if text is None:
            text = "" if payload is None else json.dumps(payload, ensure_ascii=False)
        self.text = text


class FakeSession:
    """记录调用参数并按顺序返回预设响应的 session 替身(不触网)。"""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses) or [FakeResponse({})]
        self.calls: list[Call] = []

    def request(
        self,
        method: str,
        url: str,
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        headers: Optional[dict[str, str]] = None,
        **kwargs: Any,
    ) -> FakeResponse:
        self.calls.append(Call(method, url, params, data, timeout, dict(headers or {})))
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def last(self) -> Call:
        return self.calls[-1]


class FakeClock:
    """可控单调时钟:每次读取返回当前值,然后前进 ``step`` 秒。"""

    def __init__(self, step: float = 0.1) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


def expect_error(func: Callable[[], Any], exc_type: type, *fragments: str) -> Exception:
    """断言 ``func()`` 抛出 ``exc_type``,且错误信息包含全部 ``fragments``。"""
    try:
        func()
    except exc_type as exc:
        text = str(exc)
        for fragment in fragments:
            assert fragment in text, f"错误信息应包含 {fragment!r},实际:{text}"
        return exc
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"应抛出 {exc_type.__name__},实际抛出 {type(exc).__name__}:{exc}") from exc
    raise AssertionError(f"应抛出 {exc_type.__name__},但没有任何异常")


# --------------------------------------------------------------------------- #
# 真实响应样本(2026-09-08 实测,已裁剪)
# --------------------------------------------------------------------------- #

OSRM_OK = {
    "code": "Ok",
    "routes": [
        {
            "legs": [{"duration": 5638.8, "distance": 122376.1, "summary": "", "steps": []}],
            "weight_name": "routability",
            "weight": 5638.8,
            "duration": 5638.8,
            "distance": 122376.1,
        }
    ],
    "waypoints": [{"name": "台基厂头条", "location": [116.407381, 39.904421], "distance": 24.59}],
}

NOMINATIM_SEARCH = [
    {
        "place_id": 216697945,
        "osm_type": "relation",
        "osm_id": 912940,
        "lat": "39.9057136",  # 真实 API 返回字符串坐标
        "lon": "116.3912972",
        "addresstype": "city",
        "name": "北京市",
        "display_name": "北京市, 中国",
        "boundingbox": ["39.1707096", "41.0592360", "115.4168490", "117.7371243"],
    }
]

NOMINATIM_REVERSE = {
    "place_id": 418421109,
    "osm_type": "node",
    "lat": "39.9042695",
    "lon": "116.4075123",
    "addresstype": "landuse",
    "name": "台基厂头条14号院-10号院",
    "display_name": "台基厂头条14号院-10号院, 台基厂头条, 东城区, 北京市, 100010, 中国",
    "address": {"road": "台基厂头条", "city": "东城区", "country": "中国", "country_code": "cn"},
}

# Overpass 实测特征:node 带 lat/lon;way/relation 只有 `out center` 时才有 center;
# 部分山峰没有 name;结果顺序不按距离。
OVERPASS_PAYLOAD = {
    "version": 0.6,
    "generator": "Overpass API 0.7.62.11 87bfad18",
    "elements": [
        {
            "type": "node",
            "id": 1,
            "lat": 39.99,
            "lon": 116.50,
            "tags": {"natural": "peak", "ele": "1200", "name": "远山"},
        },
        {
            "type": "way",
            "id": 2,
            "center": {"lat": 39.92, "lon": 116.42},  # way/relation 仅在 `out center` 下带 center
            "tags": {"tourism": "attraction", "name:zh": "近景点"},
        },
        {"type": "node", "id": 3, "lat": 39.93, "lon": 116.43, "tags": {"natural": "peak"}},
        {"type": "relation", "id": 4, "tags": {"name": "无坐标的元素"}},
        {"type": "node", "id": 5, "lat": 39.95, "lon": 116.45, "tags": {"natural": "peak", "name:en": "Middle Hill"}},
    ],
}

OVERPASS_BUSY_HTML = (
    '<?xml version="1.0" encoding="UTF-8"?><html><body><p><strong>Error</strong>: runtime error: '
    "open64: 0 Success /osm3s_osm_base Dispatcher_Client::request_read_and_idx::timeout. "
    "The server is probably too busy to handle your request.</p></body></html>"
)

BEIJING_START = (116.4074, 39.9042)  # (lng, lat)
BEIJING_END = (116.4815, 39.9907)


# --------------------------------------------------------------------------- #
# OSRM
# --------------------------------------------------------------------------- #


def test_osrm_builds_url_params_and_converts_units() -> None:
    session = FakeSession(FakeResponse(OSRM_OK))
    result = osrm.OsrmClient(osrm.DEFAULT_ENDPOINT, session=session).route(BEIJING_START, BEIJING_END)

    call = session.last
    assert call.method == "GET"
    # OSRM 坐标顺序是 lng,lat;两点用 ";" 连接
    assert call.url == (
        "https://router.project-osrm.org/route/v1/driving/"
        "116.407400,39.904200;116.481500,39.990700"
    )
    assert call.params == {
        "overview": "false",
        "alternatives": "false",
        "steps": "false",
        "annotations": "false",
    }
    assert call.headers["User-Agent"] == "Where2Go-POC/0.1 (dev)"
    assert 0 < call.timeout <= 20
    # 米→公里、秒→分钟
    assert result == {"distance_km": 122.376, "duration_min": 94.0}


def test_osrm_supports_alt_endpoint_and_string_coordinates() -> None:
    session = FakeSession(FakeResponse(OSRM_OK))
    osrm.OsrmClient(osrm.ALT_ENDPOINT, session=session).route("116.4074,39.9042", [116.4815, 39.9907])
    assert session.last.url.startswith("https://routing.openstreetmap.de/routed-car/route/v1/driving/")
    assert session.last.url.endswith("116.407400,39.904200;116.481500,39.990700")


def test_osrm_rejects_invalid_coordinates() -> None:
    for bad in [(200.0, 39.9), (116.4, 99.0), (116.4,), "116.4", "abc,def", 42]:
        expect_error(lambda value=bad: osrm.format_lnglat(value), ValueError)
    expect_error(lambda: osrm.OsrmClient(session=FakeSession()).route((116.4,), BEIJING_END), ValueError)


def test_osrm_non_ok_code_raises_chinese_error() -> None:
    payload = {"code": "NotFound", "message": "Not found"}
    session = FakeSession(FakeResponse(payload))
    exc = expect_error(
        lambda: osrm.OsrmClient(session=session).route(BEIJING_START, BEIJING_END),
        ds.DataSourceError,
        "[OSRM]",
        "路线规划失败",
        "NotFound",
    )
    assert isinstance(exc, ds.DataSourceError) and exc.source == "OSRM"


def test_osrm_rejects_empty_routes_and_bad_numbers() -> None:
    cases = [
        ({"code": "Ok", "routes": []}, "routes"),
        ({"code": "Ok", "routes": [{"distance": None, "duration": 100.0}]}, "distance/duration"),
        ({"code": "Ok", "routes": [{"distance": 0.0, "duration": 0.0}]}, "非正数"),
        ({"code": "Ok"}, "routes"),
        ([], "应为 JSON 对象"),
    ]
    for payload, fragment in cases:
        session = FakeSession(FakeResponse(payload))
        expect_error(
            lambda s=session: osrm.OsrmClient(session=s).route(BEIJING_START, BEIJING_END),
            ds.DataSourceError,
            fragment,
        )


def test_osrm_timeout_is_transient_and_retryable() -> None:
    session = FakeSession(requests.exceptions.ReadTimeout("read timed out"))
    expect_error(
        lambda: osrm.OsrmClient(session=session).route(BEIJING_START, BEIJING_END),
        ds.TransientDataSourceError,
        "请求超时",
    )


# --------------------------------------------------------------------------- #
# Nominatim
# --------------------------------------------------------------------------- #


def test_nominatim_geocode_url_params_and_parsing() -> None:
    session = FakeSession(FakeResponse(NOMINATIM_SEARCH))
    place = nominatim.NominatimClient(session=session, min_interval=0).geocode("北京")

    call = session.last
    assert call.method == "GET"
    assert call.url == "https://nominatim.openstreetmap.org/search"
    assert call.params["q"] == "北京"
    assert call.params["format"] == "jsonv2"
    assert call.params["limit"] == 1
    assert call.params["accept-language"] == nominatim.ACCEPT_LANGUAGE
    # Nominatim 强制要求可识别 User-Agent
    assert call.headers["User-Agent"] == "Where2Go-POC/0.1 (dev)"
    assert 0 < call.timeout <= 20
    # 真实响应里 lat/lon 是字符串,需要转成 float
    assert place == {"lat": 39.9057136, "lng": 116.3912972, "display_name": "北京市, 中国"}


def test_nominatim_geocode_empty_result_raises() -> None:
    session = FakeSession(FakeResponse([]))
    expect_error(
        lambda: nominatim.NominatimClient(session=session, min_interval=0).geocode("某不存在的地点"),
        ds.DataSourceError,
        "[Nominatim]",
        "未找到",
    )


def test_nominatim_reverse_url_params_and_parsing() -> None:
    session = FakeSession(FakeResponse(NOMINATIM_REVERSE))
    place = nominatim.NominatimClient(session=session, min_interval=0).reverse(39.9042, 116.4074, zoom=12)

    call = session.last
    assert call.url == "https://nominatim.openstreetmap.org/reverse"
    assert call.params["lat"] == "39.904200"
    assert call.params["lon"] == "116.407400"
    assert call.params["zoom"] == 12
    assert call.params["format"] == "jsonv2"
    assert place["display_name"].startswith("台基厂头条14号院-10号院")
    assert place["lat"] == 39.9042695 and place["lng"] == 116.4075123


def test_nominatim_reverse_error_field_and_bad_input() -> None:
    session = FakeSession(FakeResponse({"error": "Unable to geocode"}))
    expect_error(
        lambda: nominatim.NominatimClient(session=session, min_interval=0).reverse(0.0, 0.0),
        ds.DataSourceError,
        "逆地理编码失败",
        "Unable to geocode",
    )
    for lat, lng in [(91.0, 116.4), (39.9, 181.0), ("abc", 116.4)]:
        expect_error(
            lambda a=lat, b=lng: nominatim.NominatimClient(session=FakeSession(), min_interval=0).reverse(a, b),
            ValueError,
        )
    expect_error(
        lambda: nominatim.NominatimClient(session=FakeSession(), min_interval=0).geocode("   "),
        ValueError,
    )


def test_nominatim_requires_user_agent_and_throttles_to_1rps() -> None:
    expect_error(lambda: nominatim.NominatimClient(user_agent=""), ValueError, "User-Agent")

    slept: list[float] = []
    session = FakeSession(FakeResponse(NOMINATIM_SEARCH))
    client = nominatim.NominatimClient(
        session=session, min_interval=1.0, clock=FakeClock(0.1), sleep=slept.append
    )
    client.geocode("北京")
    assert slept == [], "首次请求不应等待"
    client.geocode("北京")
    assert len(slept) == 1 and 0.8 <= slept[0] <= 1.0, f"第二次请求应节流约 1s,实际:{slept}"
    assert len(session.calls) == 2


def test_nominatim_connection_error_is_transient() -> None:
    session = FakeSession(requests.exceptions.ConnectionError("name resolution failed"))
    expect_error(
        lambda: nominatim.NominatimClient(session=session, min_interval=0).geocode("北京"),
        ds.TransientDataSourceError,
        "网络连接失败",
    )


# --------------------------------------------------------------------------- #
# Overpass
# --------------------------------------------------------------------------- #


def test_overpass_build_query_shape() -> None:
    query = overpass.build_query(
        39.9042, 116.4074, 50_000, {"tourism": "attraction"}, limit=5, element_types="node", query_timeout=18
    )
    assert query.startswith("[out:json][timeout:18];")
    assert 'node["tourism"="attraction"](around:50000,39.904200,116.407400);' in query
    # around 结果不按距离排序,所以服务端多取、本地排序后截断
    assert "out center 60;" in query


def test_overpass_tag_union_and_escaping() -> None:
    query = overpass.build_query(
        39.9, 116.4, 1_000, [{"natural": "peak"}, {"name": 'say "hi"'}, {"leisure": None}], limit=20
    )
    assert query.count("(around:1000,39.900000,116.400000);") == 9  # 3 组 tag × nwr
    assert 'node["natural"="peak"]' in query and 'relation["natural"="peak"]' in query
    assert 'way["name"="say \\"hi\\""]' in query
    assert 'way["leisure"]' in query  # value 为 None → 只判断 tag 是否存在


def test_overpass_rejects_bad_inputs() -> None:
    expect_error(lambda: overpass.build_query(39.9, 116.4, 0, {"natural": "peak"}), ValueError, "radius_m")
    expect_error(lambda: overpass.tags_to_selectors({}), ValueError, "不能为空")
    expect_error(lambda: overpass.tags_to_selectors([]), ValueError, "不能为空")
    expect_error(lambda: overpass.tags_to_selectors("natural=peak"), ValueError, "'['")
    expect_error(lambda: overpass.normalize_element_types("point"), ValueError, "element_types")


def test_overpass_parse_places_sorts_and_falls_back_names() -> None:
    places = overpass.parse_places(OVERPASS_PAYLOAD, 39.9042, 116.4074, limit=None)
    # 无坐标的 relation 被丢弃;剩下的按大圆距离升序
    assert [place["name"] for place in places] == ["近景点", "", "Middle Hill", "远山"]
    assert set(places[0]) == {"lat", "lng", "name", "tags"}
    assert places[0]["tags"]["name:zh"] == "近景点"

    named = overpass.parse_places(OVERPASS_PAYLOAD, 39.9042, 116.4074, limit=None, require_name=True)
    assert [place["name"] for place in named] == ["近景点", "Middle Hill", "远山"]
    assert len(overpass.parse_places(OVERPASS_PAYLOAD, 39.9042, 116.4074, limit=2)) == 2


def test_overpass_nearby_places_posts_query_and_parses() -> None:
    session = FakeSession(FakeResponse(OVERPASS_PAYLOAD))
    client = overpass.OverpassClient(session=session, retries=1, retry_backoff_s=0)
    places = client.nearby_places(
        39.9042, 116.4074, 50_000, {"tourism": "attraction"}, limit=3, require_name=True
    )

    call = session.last
    assert call.method == "POST"
    assert call.url == "https://overpass-api.de/api/interpreter"
    assert call.data["data"].startswith("[out:json][timeout:")
    assert "around:50000,39.904200,116.407400" in call.data["data"]
    assert call.headers["User-Agent"] == "Where2Go-POC/0.1 (dev)"
    assert 0 < call.timeout <= 20
    assert client.used_endpoint == overpass.DEFAULT_ENDPOINT
    assert [place["name"] for place in places] == ["近景点", "Middle Hill", "远山"]


def test_overpass_falls_back_to_mirror_when_primary_busy() -> None:
    busy = FakeResponse(None, status_code=504, text=OVERPASS_BUSY_HTML)
    session = FakeSession(busy, FakeResponse(OVERPASS_PAYLOAD))
    slept: list[float] = []
    client = overpass.OverpassClient(session=session, retries=1, retry_backoff_s=0.5, sleep=slept.append)

    places = client.nearby_places(39.9042, 116.4074, 50_000, {"tourism": "attraction"}, limit=2, require_name=True)
    assert len(session.calls) == 2
    assert client.used_endpoint == "https://z.overpass-api.de/api/interpreter"
    assert [place["name"] for place in places] == ["近景点", "Middle Hill"]
    assert slept and all(item > 0 for item in slept), f"降级前应有退避等待,实际:{slept}"


def test_overpass_reports_error_when_all_endpoints_fail() -> None:
    session = FakeSession(FakeResponse(None, status_code=504, text=OVERPASS_BUSY_HTML))
    client = overpass.OverpassClient(session=session, retries=2, retry_backoff_s=0, sleep=lambda _: None)
    expect_error(
        lambda: client.nearby_places(39.9042, 116.4074, 50_000, {"tourism": "attraction"}),
        ds.DataSourceError,
        "所有 Overpass 端点均不可用",
        "overpass-api.de",
        "maps.mail.ru",
        "too busy",
    )
    assert len(session.calls) == len(client.endpoints) * 2


def test_overpass_bad_query_fails_fast_without_fallback() -> None:
    session = FakeSession(FakeResponse(None, status_code=400, text='{"remark": "unknown query type"}'))
    client = overpass.OverpassClient(session=session, retries=3, retry_backoff_s=0, sleep=lambda _: None)
    expect_error(
        lambda: client.nearby_places(39.9042, 116.4074, 50_000, {"tourism": "attraction"}),
        ds.DataSourceError,
        "HTTP 状态码 400",
    )
    assert len(session.calls) == 1, "查询本身有误时不应重试或换端点"


def test_overpass_rejects_payload_without_elements() -> None:
    session = FakeSession(FakeResponse({"version": 0.6, "generator": "Overpass API"}))
    client = overpass.OverpassClient(session=session, retries=1, retry_backoff_s=0)
    expect_error(
        lambda: client.nearby_places(39.9042, 116.4074, 50_000, {"tourism": "attraction"}),
        ds.DataSourceError,
        "elements",
    )


def test_overpass_haversine_matches_known_distance() -> None:
    # 与实测样本一致:福寿岭(39.9454069,116.1562855) 距北京市中心约 21.9 km
    assert abs(overpass.haversine_km(39.9042, 116.4074, 39.9454069, 116.1562855) - 21.9) < 0.1
    assert overpass.haversine_km(39.9, 116.4, 39.9, 116.4) == 0.0


# --------------------------------------------------------------------------- #
# 公共层与包导出
# --------------------------------------------------------------------------- #


def test_normalize_timeout_caps_at_20s() -> None:
    assert normalize_timeout(None) == 15.0
    assert normalize_timeout(60) == 20.0
    assert normalize_timeout(3) == 3.0
    expect_error(lambda: normalize_timeout(0), ValueError, "timeout")
    expect_error(lambda: normalize_timeout(-1), ValueError, "timeout")


def test_package_exports_all_sources() -> None:
    assert ds.USER_AGENT == "Where2Go-POC/0.1 (dev)"
    for name in (
        "route",
        "geocode",
        "reverse",
        "nearby_places",
        "haversine_km",
        "OsrmClient",
        "NominatimClient",
        "OverpassClient",
        "DataSourceError",
        "TransientDataSourceError",
    ):
        assert name in ds.__all__, f"{name} 未在 __all__ 中导出"
        assert hasattr(ds, name), f"{name} 未导出"
    assert ds.OSRM_ENDPOINT == "https://router.project-osrm.org"
    assert ds.OSRM_ALT_ENDPOINT == "https://routing.openstreetmap.de/routed-car"
    assert ds.OVERPASS_ENDPOINT in ds.OverpassClient().endpoints[0]


def test_module_level_functions_accept_injected_session() -> None:
    assert ds.route(BEIJING_START, BEIJING_END, session=FakeSession(FakeResponse(OSRM_OK))) == {
        "distance_km": 122.376,
        "duration_min": 94.0,
    }
    assert ds.geocode("北京", session=FakeSession(FakeResponse(NOMINATIM_SEARCH)), min_interval=0)[
        "display_name"
    ] == "北京市, 中国"
    assert ds.reverse(39.9042, 116.4074, session=FakeSession(FakeResponse(NOMINATIM_REVERSE)), min_interval=0)[
        "lng"
    ] == 116.4075123
    places = ds.nearby_places(
        39.9042,
        116.4074,
        50_000,
        {"tourism": "attraction"},
        session=FakeSession(FakeResponse(OVERPASS_PAYLOAD)),
        retries=1,
        require_name=True,
        limit=2,
    )
    assert [place["name"] for place in places] == ["近景点", "Middle Hill"]


def test_endpoint_can_be_overridden_per_call() -> None:
    session = FakeSession(FakeResponse(OSRM_OK))
    ds.route(BEIJING_START, BEIJING_END, endpoint="https://routing.openstreetmap.de/routed-car", session=session)
    assert session.last.url.startswith("https://routing.openstreetmap.de/routed-car/")

    session = FakeSession(FakeResponse(OVERPASS_PAYLOAD))
    ds.nearby_places(
        39.9042,
        116.4074,
        1_000,
        {"natural": "peak"},
        endpoint="https://z.overpass-api.de/api/interpreter",
        session=session,
        retries=1,
    )
    assert session.last.url == "https://z.overpass-api.de/api/interpreter"


# --------------------------------------------------------------------------- #
# 不依赖 pytest 的直接运行入口
# --------------------------------------------------------------------------- #


def _run_all() -> int:
    tests = sorted(
        (name, obj) for name, obj in globals().items() if name.startswith("test_") and callable(obj)
    )
    failures: list[tuple[str, BaseException]] = []
    for name, func in tests:
        try:
            func()
        except BaseException as exc:  # noqa: BLE001
            failures.append((name, exc))
            print(f"[失败] {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"[通过] {name}")
    print(f"\n共 {len(tests)} 个单测:通过 {len(tests) - len(failures)},失败 {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
