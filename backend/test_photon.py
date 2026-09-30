"""TASK-6a 单测:Photon 地理编码主路径 + Nominatim 降级链。

全程不触网:

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,任何偷偷联网都会当场失败;
* Photon 客户端用 :class:`FakeSession` 替身,断言 **URL、参数(不含 lang)、请求头、超时**
  与 **GeoJSON 真实形状下的解析**(``coordinates=[lon, lat]``,经度在前);
* 降级链(:mod:`services.place_loader`)用 ``monkeypatch`` 替换两条腿
  (``ds_photon_geocode`` / ``ds_photon_reverse`` / ``ds_geocode`` / ``ds_reverse``);
* DB 用 ``tmp_path`` 下的临时 SQLite 文件,不碰 ``backend/data/``。

覆盖四件事:解析口径(lon-lat 顺序、display_name 组装跳过空段)、降级触发条件
(Photon 抛错 / 空结果)、双失败口径(正向 400 中文、逆向仍 200 + ``resolved=false``)、
以及 API 响应里的 ``geocoder`` 标注。

TASK-7a 起 ``/api/geocode`` 还带**地理编码持久缓存**(:class:`db.models.OriginCache`,
TTL 缺省 7 天):同一城市第二次搜索**零网络**、响应逐字段一致,过期/TTL=0 才重新问源。

运行:``cd backend && ../.venv/bin/python -m pytest -q``
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from datetime import timedelta
from typing import Any, Callable, Optional

import pytest
import requests
from fastapi import HTTPException

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import places as places_api  # noqa: E402
from data_sources import DataSourceError, TransientDataSourceError  # noqa: E402
from data_sources import _common, photon  # noqa: E402
from db import init_db, make_engine, repository as repo, session_factory  # noqa: E402
from db.models import utcnow  # noqa: E402
from services import place_loader  # noqa: E402
from services.bands import band_keys  # noqa: E402

# --------------------------------------------------------------------------- #
# 测试替身(与 backend/test_data_sources.py 同一套写法)
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


def expect_http_error(func: Callable[[], Any], status_code: int, *fragments: str) -> HTTPException:
    """断言 ``func()`` 抛出指定状态码的 HTTPException,且 detail 含全部片段。"""
    with pytest.raises(HTTPException) as caught:
        func()
    exc = caught.value
    assert exc.status_code == status_code, f"应为 HTTP {status_code},实际 {exc.status_code}"
    for fragment in fragments:
        assert fragment in str(exc.detail), f"detail 应包含 {fragment!r},实际:{exc.detail}"
    return exc


# --------------------------------------------------------------------------- #
# 真实响应样本(Photon 公共实例的 GeoJSON 形状;coordinates = [lon, lat])
# --------------------------------------------------------------------------- #

BEIJING = {"lat": 39.9057136, "lng": 116.3912972}
DONGCHENG = {"lat": 39.9042695, "lng": 116.4075123}

PHOTON_SEARCH = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [116.3912972, 39.9057136]},
            "properties": {
                "osm_id": 2219849,
                "osm_type": "R",
                "type": "relation",
                "name": "北京市",
                "state": "北京市",      # 直辖市:name 与 state 同名,拼接时只留一次
                "country": "中国",
            },
        },
        {
            "type": "Feature",
            # 坐标多给一位小数 + 第三个高程值:解析要按 7 位定点收敛、并容忍多余分量
            "geometry": {"type": "Point", "coordinates": [116.40751234, 39.90426951, 43.0]},
            "properties": {
                "name": "东城区",
                "city": "北京市",
                "state": "北京市",
                "country": "中国",
            },
        },
    ],
}

# 2026-09-28 实测 /reverse 的真实形状:门牌级结果,properties 里 name/district/city/country 齐全
PHOTON_REVERSE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "osm_type": "N",
                "osm_id": 13860479407,
                "type": "house",
                "name": "台基厂头条14号院-10号院",
                "street": "台基厂头条",
                "district": "东城区",
                "city": "北京市",
                "country": "中国",
                "postcode": "100010",
                "countrycode": "CN",
            },
            "geometry": {"type": "Point", "coordinates": [116.4075123, 39.9042695]},
        }
    ],
}

PHOTON_EMPTY = {"type": "FeatureCollection", "features": []}


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
def session(tmp_path):
    """每个用例一个独立的临时 SQLite 库。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'photon_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


@pytest.fixture()
def photon_ok(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """替换 place_loader 的 Photon 正向腿,并记录调用参数;Nominatim 腿一碰就失败。"""
    calls: list[dict[str, Any]] = []

    def fake(query: str, *, limit: int = photon.DEFAULT_LIMIT) -> list[dict[str, Any]]:
        calls.append({"query": query, "limit": limit})
        return [
            {"lat": BEIJING["lat"], "lng": BEIJING["lng"], "display_name": "北京市, 中国"},
        ]

    def boom(city: str) -> dict[str, Any]:
        raise AssertionError(f"Photon 命中时不应降级到 Nominatim:{city}")

    monkeypatch.setattr(place_loader, "ds_photon_geocode", fake)
    monkeypatch.setattr(place_loader, "ds_geocode", boom)
    return calls


@pytest.fixture()
def nominatim_leg(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """让 Photon 正向腿失败(抛错 / 空结果由用例改),Nominatim 腿正常答并记录调用。"""
    calls: list[str] = []

    def fake(query: str, *, limit: int = 1) -> dict[str, Any]:
        calls.append(query)
        return {"lat": BEIJING["lat"], "lng": BEIJING["lng"], "display_name": "北京市, 中国"}

    monkeypatch.setattr(place_loader, "ds_geocode", fake)
    return calls


# --------------------------------------------------------------------------- #
# 1. Photon 客户端:请求口径(URL / 参数 / 头 / 超时 / 不传 lang)
# --------------------------------------------------------------------------- #


def test_photon_geocode_request_shape_and_lon_lat_order() -> None:
    session = FakeSession(FakeResponse(PHOTON_SEARCH))
    places = photon.PhotonClient(session=session, min_interval=0).geocode("北京")

    call = session.last
    assert call.method == "GET"
    assert call.url == "https://photon.komoot.io/api", "Photon 正向检索路径是 /api,不是 /search"
    assert call.params["q"] == "北京"
    assert call.params["limit"] == photon.DEFAULT_LIMIT
    assert "lang" not in call.params, "Photon 不支持 lang 参数,不许传"
    assert "format" not in call.params, "Photon 只出 GeoJSON,不需要 format"
    assert call.headers["User-Agent"] == "Where2Go-POC/0.1 (dev)"
    assert 0 < call.timeout <= 20
    # GeoJSON 的 coordinates 是 [lon, lat] —— 解析必须换回来
    assert places[0] == {**BEIJING, "display_name": "北京市, 中国"}


def test_photon_geocode_keeps_every_feature_in_order_and_rounds_to_7() -> None:
    session = FakeSession(FakeResponse(PHOTON_SEARCH))
    places = photon.PhotonClient(session=session, min_interval=0).geocode("北京", limit=5)

    assert len(places) == 2, "limit=5 时应把候选整份交给上层,不自己截断"
    assert [item["display_name"] for item in places] == ["北京市, 中国", "东城区, 北京市, 中国"]
    # 8 位小数收敛到入库口径的 7 位;第三个高程分量被忽略
    assert places[1]["lat"] == DONGCHENG["lat"] and places[1]["lng"] == DONGCHENG["lng"]


def test_phon_geocode_limit_is_at_least_one() -> None:
    session = FakeSession(FakeResponse(PHOTON_SEARCH))
    photon.PhotonClient(session=session, min_interval=0).geocode("北京", limit=0)
    assert session.last.params["limit"] == 1


# --------------------------------------------------------------------------- #
# 2. display_name 组装:name, city, state, country(跳过空段)
# --------------------------------------------------------------------------- #


def test_photon_display_name_skips_empty_segments() -> None:
    assert photon.build_display_name({"name": "西塘镇", "city": "", "state": None, "country": "中国"}) == "西塘镇, 中国"
    assert photon.build_display_name({"name": "  ", "city": "嘉兴市", "country": "中国"}) == "嘉兴市, 中国"
    assert photon.build_display_name({"osm_id": 1}) == ""
    assert photon.build_display_name(None) == ""


def test_photon_display_name_follows_name_city_state_country_order() -> None:
    properties = {"country": "中国", "state": "浙江省", "city": "嘉兴市", "name": "西塘镇"}
    assert photon.build_display_name(properties) == "西塘镇, 嘉兴市, 浙江省, 中国"


def test_photon_display_name_dedupes_municipality_segments() -> None:
    """直辖市 name/state 同名(北京市),重复段只留一次,免得前端显示"北京市, 北京市"。"""
    assert photon.build_display_name({"name": "北京市", "state": "北京市", "country": "中国"}) == "北京市, 中国"


def test_photon_feature_without_any_name_raises() -> None:
    feature = {"geometry": {"coordinates": [116.4, 39.9]}, "properties": {"osm_id": 1}}
    expect_error(
        lambda: photon.parse_feature(feature),
        DataSourceError,
        "[Photon]",
        "没有任何地名",
    )


# --------------------------------------------------------------------------- #
# 3. 格式错误 / 空结果 / HTTP 错误
# --------------------------------------------------------------------------- #


def test_photon_geocode_empty_features_returns_empty_list() -> None:
    session = FakeSession(FakeResponse(PHOTON_EMPTY))
    assert photon.PhotonClient(session=session, min_interval=0).geocode("某不存在的地点") == []


def test_photon_bad_payload_raises_data_source_error() -> None:
    expect_error(
        lambda: photon.PhotonClient(session=FakeSession(FakeResponse([{"lat": 1}])), min_interval=0).geocode("北京"),
        DataSourceError,
        "[Photon]",
        "GeoJSON",
    )
    expect_error(
        lambda: photon.PhotonClient(session=FakeSession(FakeResponse({"type": "FeatureCollection"})), min_interval=0).geocode("北京"),
        DataSourceError,
        "features",
    )
    expect_error(
        lambda: photon.PhotonClient(session=FakeSession(FakeResponse({"features": "nope"})), min_interval=0).geocode("北京"),
        DataSourceError,
        "features 数组",
    )


@pytest.mark.parametrize("feature,fragment", [
    ({"geometry": {"coordinates": [116.4]}, "properties": {"name": "北京市"}}, "coordinates"),
    ({"geometry": {"coordinates": ["a", "b"]}, "properties": {"name": "北京市"}}, "不是数字"),
    ({"geometry": {}, "properties": {"name": "北京市"}}, "coordinates"),
    ({"properties": {"name": "北京市"}}, "coordinates"),
    ("not-a-dict", "格式异常"),
])
def test_photon_bad_geometry_raises_data_source_error(feature: Any, fragment: str) -> None:
    expect_error(lambda: photon.parse_feature(feature), DataSourceError, "[Photon]", fragment)


def test_photon_http_and_connection_errors_are_data_source_errors() -> None:
    expect_error(
        lambda: photon.PhotonClient(
            session=FakeSession(FakeResponse(status_code=400, text='{"message":"invalid"}')), min_interval=0
        ).geocode("北京"),
        DataSourceError,
        "[Photon]",
        "HTTP 状态码 400",
    )
    expect_error(
        lambda: photon.PhotonClient(session=FakeSession(FakeResponse(status_code=503, text="busy")), min_interval=0).geocode("北京"),
        TransientDataSourceError,
        "服务端错误",
    )
    expect_error(
        lambda: photon.PhotonClient(
            session=FakeSession(requests.exceptions.ConnectionError("name resolution failed")), min_interval=0
        ).geocode("北京"),
        TransientDataSourceError,
        "网络连接失败",
    )
    expect_error(
        lambda: photon.PhotonClient(
            session=FakeSession(requests.exceptions.ReadTimeout("read timed out")), min_interval=0
        ).geocode("北京"),
        TransientDataSourceError,
        "请求超时",
    )


def test_photon_blank_query_raises_value_error() -> None:
    expect_error(
        lambda: photon.PhotonClient(session=FakeSession(), min_interval=0).geocode("   "),
        ValueError,
        "query",
    )


# --------------------------------------------------------------------------- #
# 4. 逆地理编码:/api/reverse、返回 dict、无 zoom/lang
# --------------------------------------------------------------------------- #


def test_photon_reverse_request_shape_and_parsing() -> None:
    session = FakeSession(FakeResponse(PHOTON_REVERSE))
    place = photon.PhotonClient(session=session, min_interval=0).reverse(39.9042, 116.4074)

    call = session.last
    # 实测口径:正向 /api、逆向 /reverse(/api/reverse 会 404)
    assert call.url == "https://photon.komoot.io/reverse"
    assert call.params == {"lat": "39.904200", "lon": "116.407400"}, "只传 lat/lon:不收 zoom,也不传 lang"
    assert place == {"lat": 39.9042695, "lng": 116.4075123,
                     "display_name": "台基厂头条14号院-10号院, 北京市, 中国"}


def test_photon_reverse_empty_features_raises() -> None:
    session = FakeSession(FakeResponse(PHOTON_EMPTY))
    expect_error(
        lambda: photon.PhotonClient(session=session, min_interval=0).reverse(0.0, 0.0),
        DataSourceError,
        "[Photon]",
        "逆地理编码未找到结果",
    )


@pytest.mark.parametrize("lat,lng", [(91.0, 116.4), (39.9, 181.0), ("abc", 116.4), (39.9, None)])
def test_photon_reverse_validates_coordinates(lat: Any, lng: Any) -> None:
    expect_error(
        lambda: photon.PhotonClient(session=FakeSession(), min_interval=0).reverse(lat, lng),
        ValueError,
    )


# --------------------------------------------------------------------------- #
# 5. 节流(1 req/s)、端点覆盖、代理口径(直连)
# --------------------------------------------------------------------------- #


def test_photon_throttles_to_1rps() -> None:
    slept: list[float] = []
    session = FakeSession(FakeResponse(PHOTON_SEARCH))
    client = photon.PhotonClient(session=session, min_interval=1.0, clock=FakeClock(0.1), sleep=slept.append)
    client.geocode("北京")
    assert slept == [], "首次请求不应等待"
    client.geocode("北京")
    assert len(slept) == 1 and 0.8 <= slept[0] <= 1.0, f"第二次请求应节流约 1s,实际:{slept}"
    assert len(session.calls) == 2


def test_photon_endpoint_env_override_and_module_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(photon.ENV_ENDPOINT, "https://photon.internal.example.com/")
    assert photon.PhotonClient().endpoint == "https://photon.internal.example.com"
    assert photon.PhotonClient(endpoint="https://a.example.com").endpoint == "https://a.example.com"
    monkeypatch.delenv(photon.ENV_ENDPOINT)
    assert photon.PhotonClient().endpoint == photon.DEFAULT_ENDPOINT == "https://photon.komoot.io"

    places = photon.geocode("北京", session=FakeSession(FakeResponse(PHOTON_SEARCH)), min_interval=0)
    assert places[0]["display_name"] == "北京市, 中国"
    place = photon.reverse(39.9042, 116.4074, session=FakeSession(FakeResponse(PHOTON_REVERSE)), min_interval=0)
    assert place["lng"] == 116.4075123 and place["lat"] == 39.9042695


def test_photon_uses_direct_connection_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """实测 Photon 直连 1.1s、走代理 5s 挂 → 默认口径必须是 off(强制直连)。"""
    monkeypatch.delenv("WHERE2GO_PROXY_PHOTON", raising=False)
    assert _common.DEFAULT_SOURCE_PROXY["photon"] == _common.PROXY_OFF
    assert _common.proxy_mode("photon") == _common.PROXY_OFF
    built = _common.build_session(source="photon")
    assert built.trust_env is False and built.proxies == {}
    assert photon.PhotonClient()._session.proxies == {}, "Photon 客户端默认必须直连"
    assert photon.PhotonClient()._session.trust_env is False

    monkeypatch.setenv("WHERE2GO_PROXY_PHOTON", "env")
    assert _common.proxy_mode("photon") == _common.PROXY_ENV


# --------------------------------------------------------------------------- #
# 6. 降级链:Photon 主 → Nominatim 备(place_loader)
# --------------------------------------------------------------------------- #


def test_chain_prefers_photon_and_never_touches_nominatim(photon_ok) -> None:
    geo = place_loader.default_geocoder("北京")
    assert geo == {
        "city": "北京",
        "name": "北京市, 中国",
        "lat": BEIJING["lat"],
        "lng": BEIJING["lng"],
        "geocoder": "photon",
    }
    assert photon_ok == [{"query": "北京", "limit": place_loader.PHOTON_LIMIT}]


def test_chain_falls_back_to_nominatim_when_photon_raises(monkeypatch: pytest.MonkeyPatch, nominatim_leg) -> None:
    def boom(query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        raise DataSourceError("Photon", "网络连接失败")

    monkeypatch.setattr(place_loader, "ds_photon_geocode", boom)
    geo, geocoder = place_loader.geocode_with_fallback("北京")
    assert geocoder == "nominatim"
    assert geo["display_name"] == "北京市, 中国"
    assert nominatim_leg == ["北京"]


def test_chain_falls_back_to_nominatim_when_photon_returns_empty(monkeypatch: pytest.MonkeyPatch, nominatim_leg) -> None:
    monkeypatch.setattr(place_loader, "ds_photon_geocode", lambda query, *, limit=5: [])
    _, geocoder = place_loader.geocode_with_fallback("北京")
    assert geocoder == "nominatim", "空结果也算 Photon 失败,要降级"
    assert nominatim_leg == ["北京"]


def test_chain_double_failure_raises_chinese_error_with_both_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    def photon_boom(query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        raise DataSourceError("Photon", "网络连接失败")

    def nominatim_boom(query: str, *, limit: int = 1) -> dict[str, Any]:
        raise DataSourceError("Nominatim", "被限流(HTTP 429)")

    monkeypatch.setattr(place_loader, "ds_photon_geocode", photon_boom)
    monkeypatch.setattr(place_loader, "ds_geocode", nominatim_boom)
    exc = expect_error(
        lambda: place_loader.geocode_with_fallback("北京"),
        DataSourceError,
        "两个地理编码源都失败",
        "Photon=网络连接失败",
        "Nominatim=被限流",
    )
    assert exc.source == "Photon/Nominatim"


def test_chain_blank_city_raises_value_error() -> None:
    expect_error(lambda: place_loader.geocode_with_fallback("   "), ValueError, "起点城市不能为空")


def test_reverse_chain_prefers_photon_without_zoom(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[float, float]] = []

    def fake(lat: float, lng: float) -> dict[str, Any]:
        calls.append((lat, lng))
        return {"lat": 31.5, "lng": 121.9, "display_name": "浦东新区, 上海市, 中国"}

    def boom(lat: float, lng: float, zoom: int = 10) -> dict[str, Any]:
        raise AssertionError("Photon 命中时不应降级到 Nominatim")

    monkeypatch.setattr(place_loader, "ds_photon_reverse", fake)
    monkeypatch.setattr(place_loader, "ds_reverse", boom)
    geo = place_loader.default_reverse_geocoder(31.2304, 121.4737, zoom=12)
    assert geo == {"lat": 31.5, "lng": 121.9, "display_name": "浦东新区, 上海市, 中国", "geocoder": "photon"}
    assert calls == [(31.2304, 121.4737)], "Photon 的 reverse 不收 zoom"


def test_reverse_chain_falls_back_to_nominatim_with_zoom(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[float, float, int]] = []

    def photon_boom(lat: float, lng: float) -> dict[str, Any]:
        raise DataSourceError("Photon", "请求超时(>15s)")

    def nominatim(lat: float, lng: float, zoom: int = 10) -> dict[str, Any]:
        calls.append((lat, lng, zoom))
        return {"lat": lat, "lng": lng, "display_name": "浦东新区, 上海市, 200120, 中国"}

    monkeypatch.setattr(place_loader, "ds_photon_reverse", photon_boom)
    monkeypatch.setattr(place_loader, "ds_reverse", nominatim)
    geo, geocoder = place_loader.reverse_with_fallback(31.2304, 121.4737, zoom=12)
    assert geocoder == "nominatim"
    assert geo["display_name"] == "浦东新区, 上海市, 200120, 中国"
    assert calls == [(31.2304, 121.4737, 12)], "降级时 zoom 要原样传给 Nominatim"


def test_reverse_chain_double_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def photon_boom(lat: float, lng: float) -> dict[str, Any]:
        raise DataSourceError("Photon", "网络连接失败")

    def nominatim_boom(lat: float, lng: float, zoom: int = 10) -> dict[str, Any]:
        raise DataSourceError("Nominatim", "服务繁忙")

    monkeypatch.setattr(place_loader, "ds_photon_reverse", photon_boom)
    monkeypatch.setattr(place_loader, "ds_reverse", nominatim_boom)
    expect_error(
        lambda: place_loader.reverse_with_fallback(31.2304, 121.4737),
        DataSourceError,
        "两个逆地理编码源都失败",
        "Photon=网络连接失败",
        "Nominatim=服务繁忙",
    )


def test_resolve_origin_keeps_four_field_shape_and_reports_source(photon_ok) -> None:
    origin, geocoder = place_loader.resolve_origin_with_source("北京")
    assert geocoder == "photon"
    assert origin == {"city": "北京", "name": "北京市, 中国", **BEIJING}, "geocoder 不许混进 origin"
    assert place_loader.resolve_origin("北京") == origin


def test_resolve_origin_with_coordinates_skips_both_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("给了坐标就不该再查地理编码")

    monkeypatch.setattr(place_loader, "ds_photon_geocode", boom)
    monkeypatch.setattr(place_loader, "ds_geocode", boom)
    origin, geocoder = place_loader.resolve_origin_with_source("北京", lat=39.9, lng=116.4)
    assert origin == {"city": "北京", "name": "北京", "lat": 39.9, "lng": 116.4}
    assert geocoder == place_loader.GEOCODER_NONE


def test_resolve_reverse_origin_reports_none_when_both_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def photon_boom(lat: float, lng: float) -> dict[str, Any]:
        raise DataSourceError("Photon", "网络连接失败")

    def nominatim_boom(lat: float, lng: float, zoom: int = 10) -> dict[str, Any]:
        raise DataSourceError("Nominatim", "服务繁忙")

    monkeypatch.setattr(place_loader, "ds_photon_reverse", photon_boom)
    monkeypatch.setattr(place_loader, "ds_reverse", nominatim_boom)
    origin, geocoder = place_loader.resolve_reverse_origin_with_source(31.2304, 121.4737)
    assert geocoder == place_loader.GEOCODER_NONE
    assert origin["resolved"] is False
    assert origin["city"] == origin["name"] == "我的位置(31.23,121.47)"
    assert (origin["lat"], origin["lng"]) == (31.2304, 121.4737), "范围圈仍以 GPS 坐标为圆心"


# --------------------------------------------------------------------------- #
# 7. API 层:/api/geocode 与 /api/geocode/reverse 的 geocoder 标注与错误口径
# --------------------------------------------------------------------------- #


def test_api_geocode_reports_photon(session, photon_ok) -> None:
    payload = places_api.geocode_city(city="北京", session=session)
    assert payload["geocoder"] == "photon"
    assert payload["origin"] == {"city": "北京", "name": "北京市, 中国", **BEIJING}
    assert [band["key"] for band in payload["bands"]] == band_keys()
    assert payload["segments"] == []


def test_api_geocode_reports_nominatim_on_fallback(session, monkeypatch: pytest.MonkeyPatch, nominatim_leg) -> None:
    monkeypatch.setattr(place_loader, "ds_photon_geocode", lambda query, *, limit=5: [])
    payload = places_api.geocode_city(city="北京", session=session)
    assert payload["geocoder"] == "nominatim"
    assert payload["origin"]["name"] == "北京市, 中国"
    assert nominatim_leg == ["北京"]


def test_api_geocode_double_failure_is_400_in_chinese(session, monkeypatch: pytest.MonkeyPatch) -> None:
    def photon_boom(query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        raise DataSourceError("Photon", "网络连接失败")

    def nominatim_boom(query: str, *, limit: int = 1) -> dict[str, Any]:
        raise DataSourceError("Nominatim", "直连超时")

    monkeypatch.setattr(place_loader, "ds_photon_geocode", photon_boom)
    monkeypatch.setattr(place_loader, "ds_geocode", nominatim_boom)
    expect_http_error(
        lambda: places_api.geocode_city(city="北京", session=session),
        400,
        "无法解析城市",
        "两个地理编码源都失败",
        "Photon",
        "Nominatim",
    )


def test_api_geocode_blank_city_is_still_400(session) -> None:
    expect_http_error(lambda: places_api.geocode_city(city="   ", session=session), 400, "城市名不能为空")


def test_geocode_cache_hit(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """TASK-7a:同一城市第二次 ``/api/geocode`` **零网络**,响应与第一次逐字段一致。

    实调 Photon(德国)一次 2.7~3.4s,而城市中心坐标基本不变,所以按城市名落一行
    :class:`~db.models.OriginCache`,TTL(``WHERE2GO_ORIGIN_CACHE_TTL_S``,缺省 7 天)内
    命中直接拼响应;TTL=0 或 ``updated_at`` 超出 TTL 才重新走网络并刷新缓存行。
    """
    calls: list[str] = []

    def fake_photon(query: str, *, limit: int = photon.DEFAULT_LIMIT) -> list[dict[str, Any]]:
        calls.append(query)
        return [{"lat": BEIJING["lat"], "lng": BEIJING["lng"], "display_name": "北京市, 中国"}]

    def no_fallback(city: str) -> dict[str, Any]:
        raise AssertionError(f"Photon 命中时不应降级到 Nominatim:{city}")

    monkeypatch.setattr(place_loader, "ds_photon_geocode", fake_photon)
    monkeypatch.setattr(place_loader, "ds_geocode", no_fallback)

    first = places_api.geocode_city(city="北京", session=session)
    assert calls == ["北京"], "第一次实调一次地理编码源"
    assert first["geocoder"] == "photon"
    assert first["origin"] == {"city": "北京", "name": "北京市, 中国", **BEIJING}

    row = repo.get_origin_cache(session, city="北京")
    assert row is not None, "photon 答的要写进持久缓存"
    assert row.geocoder == "photon"
    assert (row.lat, row.lng) == (BEIJING["lat"], BEIJING["lng"])
    assert row.name == "北京市, 中国"

    second = places_api.geocode_city(city="北京", session=session)
    assert calls == ["北京"], "命中缓存不应再打网络"
    assert second == first, "命中与否响应形状逐字段一致"

    monkeypatch.setenv(places_api.ENV_ORIGIN_CACHE_TTL, "0")
    third = places_api.geocode_city(city="北京", session=session)
    assert calls == ["北京", "北京"], "TTL=0 = 缓存永不当命中"
    assert third == first

    monkeypatch.setenv(places_api.ENV_ORIGIN_CACHE_TTL, "60")
    stale = repo.get_origin_cache(session, city="北京")
    stale.updated_at = utcnow() - timedelta(seconds=61)
    session.flush()
    expired_at = stale.updated_at
    fourth = places_api.geocode_city(city="北京", session=session)
    assert calls == ["北京", "北京", "北京"], "updated_at 超出 TTL 视为过期"
    assert fourth == first
    assert repo.get_origin_cache(session, city="北京").updated_at > expired_at, "过期后刷新缓存行"


def test_geocode_cache_hit_reports_the_original_geocoder(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """命中缓存时 ``geocoder`` 按**原值**回报:谁答的就还写谁(nominatim 不被冒充成 photon)。"""
    calls: list[str] = []

    monkeypatch.setattr(place_loader, "ds_photon_geocode", lambda query, *, limit=5: [])
    monkeypatch.setattr(place_loader, "ds_geocode", lambda city: calls.append(city) or {
        "lat": BEIJING["lat"], "lng": BEIJING["lng"], "display_name": "北京市, 中国",
    })

    first = places_api.geocode_city(city="北京", session=session)
    assert first["geocoder"] == "nominatim" and calls == ["北京"]
    assert repo.get_origin_cache(session, city="北京").geocoder == "nominatim"

    def boom(query: str, *, limit: int = 5) -> Any:
        raise AssertionError("命中缓存不应再触网")

    monkeypatch.setattr(place_loader, "ds_photon_geocode", boom)
    monkeypatch.setattr(place_loader, "ds_geocode", boom)
    second = places_api.geocode_city(city="北京", session=session)
    assert second == first, "0 网络且响应一致"
    assert calls == ["北京"]


def test_geocode_cache_skips_rows_no_source_answered(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """``geocoder="none"``(没有地理编码源参与的退化结果)**不写缓存**,免得把兜底坐标钉死 7 天。"""
    monkeypatch.setattr(
        place_loader, "default_geocoder",
        lambda city: {"city": city, "name": city, "lat": 1.5, "lng": 2.5,
                      "geocoder": place_loader.GEOCODER_NONE},
    )
    payload = places_api.geocode_city(city="北京", session=session)
    assert payload["geocoder"] == place_loader.GEOCODER_NONE
    assert repo.get_origin_cache(session, city="北京") is None



def test_api_reverse_geocode_reports_photon(session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        place_loader, "ds_photon_reverse",
        lambda lat, lng: {"lat": 31.5, "lng": 121.9, "display_name": "浦东新区, 上海市, 中国"},
    )
    monkeypatch.setattr(
        place_loader, "ds_reverse",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Photon 命中时不应降级")),
    )
    payload = places_api.reverse_geocode(lat=31.2304, lng=121.4737, zoom=None, session=session)
    assert payload["geocoder"] == "photon"
    assert payload["resolved"] is True
    assert payload["origin"]["city"] == "上海市"
    assert (payload["origin"]["lat"], payload["origin"]["lng"]) == (31.2304, 121.4737)
    assert payload["note"]


def test_api_reverse_geocode_reports_nominatim_on_fallback(session, monkeypatch: pytest.MonkeyPatch) -> None:
    def photon_boom(lat: float, lng: float) -> dict[str, Any]:
        raise DataSourceError("Photon", "被限流(HTTP 429)")

    monkeypatch.setattr(place_loader, "ds_photon_reverse", photon_boom)
    monkeypatch.setattr(
        place_loader, "ds_reverse",
        lambda lat, lng, zoom=10: {"lat": lat, "lng": lng, "display_name": "浦东新区, 上海市, 200120, 中国"},
    )
    payload = places_api.reverse_geocode(lat=31.2304, lng=121.4737, zoom=None, session=session)
    assert payload["geocoder"] == "nominatim"
    assert payload["resolved"] is True and payload["origin"]["city"] == "上海市"


def test_api_reverse_geocode_double_failure_is_still_200(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """逆向双失败**不是** 400:降级成坐标起点,前端照常画环(TASK-1c 口径不变)。"""

    def photon_boom(lat: float, lng: float) -> dict[str, Any]:
        raise DataSourceError("Photon", "网络连接失败")

    def nominatim_boom(lat: float, lng: float, zoom: int = 10) -> dict[str, Any]:
        raise DataSourceError("Nominatim", "服务繁忙")

    monkeypatch.setattr(place_loader, "ds_photon_reverse", photon_boom)
    monkeypatch.setattr(place_loader, "ds_reverse", nominatim_boom)
    payload = places_api.reverse_geocode(lat=31.2304, lng=121.4737, zoom=None, session=session)
    assert payload["resolved"] is False
    assert payload["geocoder"] == "none"
    assert payload["origin"]["city"] == "我的位置(31.23,121.47)"
    assert payload["segments"] == []


def test_geocode_routes_are_still_registered() -> None:
    from app.main import app

    paths = app.openapi()["paths"]
    assert {"/api/geocode", "/api/geocode/reverse"} <= set(paths), "两条地理编码路由不能被破坏"
    params = {item["name"] for item in paths["/api/geocode"]["get"]["parameters"]}
    assert "city" in params
