"""数据源公共层与 Nominatim(地理编码降级链末腿)的纯 mock 单测。

不触网:用 :class:`FakeSession` 替身断言**请求 URL、参数、请求头、超时**与
**真实响应形状下的解析逻辑**(真实响应样本取自 2026-09-08 实测)。

TASK-9c:Overpass 与 OSRM 已随全仓切高德**物理删除**,它们那两节用例一并移除
(高德数据源层的用例见 :mod:`backend.test_amap`,驾车/地理编码切链见
:mod:`backend.test_amap_geocode_driving`);``data_sources/verify_poc.py`` 同步删除,
真实网络的可达性验证改由 ``docs/TASK-9-CONTRACT.md`` §4 的真机冒烟口径负责。

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
from data_sources import nominatim  # noqa: E402
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
# 真实响应样本(2026-09-08 实测,已裁剪;Overpass/OSRM 样本随模块一并退役)
# --------------------------------------------------------------------------- #

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
# 公共层与包导出
# --------------------------------------------------------------------------- #


def test_haversine_matches_known_distance() -> None:
    """大圆距离:TASK-9c 起住 :mod:`data_sources._common`(原 overpass.py),口径不变。"""
    # 与实测样本一致:福寿岭(39.9454069,116.1562855) 距北京市中心约 21.9 km
    assert abs(ds.haversine_km(39.9042, 116.4074, 39.9454069, 116.1562855) - 21.9) < 0.1
    assert ds.haversine_km(39.9, 116.4, 39.9, 116.4) == 0.0
    assert ds.EARTH_RADIUS_KM == 6371.0088


def test_package_exports_amap_and_geocoders() -> None:
    """包导出面(TASK-9c):高德 + Nominatim + 公共层;Overpass/OSRM 的名字一律退役。"""
    assert ds.USER_AGENT == "Where2Go-POC/0.1 (dev)"
    for name in (
        "amap",
        "geocode",
        "reverse",
        "haversine_km",
        "NominatimClient",
        "DataSourceError",
        "TransientDataSourceError",
    ):
        assert name in ds.__all__, f"{name} 未在 __all__ 中导出"
        assert hasattr(ds, name), f"{name} 未导出"
    assert ds.NOMINATIM_ENDPOINT in ds.NominatimClient().endpoint
    for retired in (
        "route", "nearby_places", "OsrmClient", "OverpassClient",
        "OSRM_ENDPOINT", "OSRM_ALT_ENDPOINT",
        "OVERPASS_ENDPOINT", "OVERPASS_FALLBACK_ENDPOINTS",
    ):
        assert retired not in ds.__all__, f"{retired} 应随 Overpass/OSRM 一并退役"
        assert not hasattr(ds, retired), f"{retired} 不该再被导出"


def test_module_level_functions_accept_injected_session() -> None:
    assert ds.geocode("北京", session=FakeSession(FakeResponse(NOMINATIM_SEARCH)), min_interval=0)[
        "display_name"
    ] == "北京市, 中国"
    assert ds.reverse(39.9042, 116.4074, session=FakeSession(FakeResponse(NOMINATIM_REVERSE)), min_interval=0)[
        "lng"
    ] == 116.4075123


def test_endpoint_can_be_overridden_per_call() -> None:
    session = FakeSession(FakeResponse(NOMINATIM_SEARCH))
    ds.geocode("北京", endpoint="https://nominatim.internal.example.com", session=session, min_interval=0)
    assert session.last.url.startswith("https://nominatim.internal.example.com/")



def test_normalize_timeout_caps_at_20s() -> None:
    assert normalize_timeout(None) == 15.0
    assert normalize_timeout(60) == 20.0
    assert normalize_timeout(3) == 3.0
    expect_error(lambda: normalize_timeout(0), ValueError, "timeout")
    expect_error(lambda: normalize_timeout(-1), ValueError, "timeout")


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
