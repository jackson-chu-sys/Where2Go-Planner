"""TASK-9a 单测:高德 v3 数据源层(:mod:`data_sources.amap`)。

全程不触网:

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,任何偷偷联网当场失败;
* HTTP 用 :class:`FakeSession` 替身,断言 **URL 路径、查询参数(lng,lat 顺序、radius 钳制、
  types/keywords 拼接、polygon 的 ``;`` 分隔)、超时** 与 **真实响应形状下的解析**;
* 节流与退避用 :class:`FakeClock` + 记录 ``sleep`` 的替身(monkeypatch ``amap._CLOCK`` /
  ``amap._SLEEP``),不会真的等 0.6s;
* key / 端点 / 节流间隔全部走 ``environ=`` 注入(另有一条用例验 ``os.environ`` 回退)。

覆盖契约要点:HTTP 码恒 200 时按 ``status``/``infocode`` 分派(瞬时 vs 永久)、
瞬时错误退避 2s/5s 最多 3 次、缺 key 的中文报错、``radius>50000`` 钳制、
``page>8`` 不发请求直接空、驾车 ``tolls=0``/``cost=null`` 不透出而 ``toll_distance`` 透出、
``strategy`` 不进请求参数、``decode_polyline`` 明文折线、``grid_polygons`` 分格坐标顺序。

运行:``cd backend && ../.venv/bin/python -m pytest -q test_amap.py``
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from typing import Any, Callable, Optional

import pytest
import requests

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from data_sources import _common, amap  # noqa: E402
from data_sources._common import DataSourceError, TransientDataSourceError  # noqa: E402

# --------------------------------------------------------------------------- #
# 测试替身(与 backend/test_photon.py 同一套写法)
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
        # 允许直接塞 payload(dict)或异常,省得每条用例都手写 FakeResponse
        given = list(responses) or [{}]
        self.responses = [
            item if isinstance(item, (FakeResponse, BaseException)) else FakeResponse(item)
            for item in given
        ]
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

    def __init__(self, step: float = 0.0, start: float = 0.0) -> None:
        self.now = start
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
# 真实响应样本(2026-10-01 容器内实测形状;坐标 "lng,lat"、数字多为字符串)
# --------------------------------------------------------------------------- #

ENVS: dict[str, str] = {amap.ENV_AMAP_KEY: "test-amap-key"}
HANGZHOU = (30.274084, 120.155070)   # (lat, lng)
SHANGHAI = (31.230416, 121.473701)

POI_SKI = {
    "id": "B023B17WWK",
    "name": "太舞滑雪场",
    "type": "体育休闲服务;运动场馆;滑雪场",
    "typecode": "080106",
    "address": "崇礼区太舞小镇",
    "location": "115.412345,40.987654",
    "distance": "1234",
    "cityname": "张家口市",
    "adname": "崇礼区",
}
POI_PARK = {
    "id": "B0FFHCZ90B",
    "name": "太子湾公园",
    "type": "风景名胜;公园广场;公园",
    "typecode": "110101",
    "address": "西湖区南山路",
    "location": "120.135000,30.231000",
    "distance": [],            # polygon 检索常返回空数组 → distance_m 应为 None
    "cityname": [],             # 高德对空值常给 []
    "adname": "西湖区",
}
POI_DIRTY_NO_NAME = {"id": "B000000001", "name": [], "location": "120.1,30.2", "typecode": "110000"}
POI_DIRTY_NO_LOCATION = {"id": "B000000002", "name": "某某景点", "location": [], "typecode": "110000"}
POI_DIRTY_NO_ID = {"id": "", "name": "无名景点", "location": "120.2,30.3", "typecode": "110000"}


def ok(payload: dict[str, Any]) -> dict[str, Any]:
    """高德成功响应的公共外壳(**HTTP 恒 200**,成败看 status/infocode)。"""
    return {"status": "1", "info": "OK", "infocode": "10000", "count": "1000", **payload}


def fail(infocode: str, info: str = "ERROR") -> dict[str, Any]:
    return {"status": "0", "info": info, "infocode": infocode}


AROUND_OK = ok({"pois": [POI_SKI, POI_PARK]})
AROUND_EMPTY = ok({"pois": []})
AROUND_DIRTY = ok({"pois": [POI_DIRTY_NO_NAME, POI_SKI, POI_DIRTY_NO_LOCATION, POI_DIRTY_NO_ID]})

GEOCODE_OK = ok({
    "geocodes": [{
        "formatted_address": "浙江省杭州市西湖区西湖",
        "province": "浙江省",
        "city": "杭州市",
        "district": "西湖区",
        "adcode": "330106",
        "township": [],
        "location": "120.147913,30.242451",
        "level": "景点",
    }]
})
GEOCODE_MUNICIPAL = ok({
    "geocodes": [{
        "formatted_address": "北京市",
        "province": "北京市",
        "city": [],           # 直辖市:city 是空数组
        "district": [],
        "adcode": "110000",
        "township": [],
        "location": "116.407526,39.904030",
    }]
})
GEOCODE_EMPTY = ok({"geocodes": []})

REGEO_OK = ok({
    "regeocode": {
        "formatted_address": "浙江省杭州市西湖区灵隐街道曙光社区(浙大路)曙光新村",
        "addressComponent": {
            "province": "浙江省",
            "city": "杭州市",
            "district": "西湖区",
            "adcode": "330106",
            "township": "灵隐街道",
            "citycode": "0571",
        },
    }
})
REGEO_EMPTY = ok({"regeocode": []})

DRIVING_STEP_A = {
    "instruction": "沿延安路向北行驶",
    "distance": "1200",
    "duration": "180",
    "polyline": "120.155070,30.274084;120.160000,30.280000",
}
DRIVING_STEP_B = {
    "instruction": "进入 G60 沪昆高速",
    "distance": "174000",
    "duration": "10400",
    "polyline": "120.160000,30.280000;121.473701,31.230416",
}
DRIVING_PATH = {
    "distance": "175897",
    "duration": "10589",
    "tolls": "0",            # ⚠️ 恒 0(个人 key 不出过路费)→ 不透出
    "toll_distance": "135514",
    "cost": None,            # ⚠️ 恒 null → 不透出
    "traffic_lights": "14",
    "restriction": "0",
    "steps": [DRIVING_STEP_A, DRIVING_STEP_B],
}
DRIVING_OK = ok({"route": {
    "origin": "120.155070,30.274084",
    "destination": "121.473701,31.230416",
    "taxi_cost": "512",
    "paths": [DRIVING_PATH],
}})


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底:任何 requests 调用都视为测试失败(本套单测必须纯 mock)。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """默认把时钟拨快(每次读 +10s → 节流永不触发),并记录所有 ``sleep`` 秒数。

    节流/退避的专项用例自己再 monkeypatch 一次 ``amap._CLOCK`` 即可。
    """
    amap.reset_throttle()
    sleeps: list[float] = []
    monkeypatch.setattr(amap, "_CLOCK", FakeClock(step=10.0))
    monkeypatch.setattr(amap, "_SLEEP", sleeps.append)
    yield sleeps
    amap.reset_throttle()


def call(func: Callable[..., Any], session: FakeSession, *args: Any, **kwargs: Any) -> Any:
    """统一注入 environ / session 调一次公开函数。"""
    return func(*args, environ=dict(ENVS), session=session, **kwargs)


# --------------------------------------------------------------------------- #
# 常量 / key / 端点 / 代理
# --------------------------------------------------------------------------- #


def test_module_constants_match_contract() -> None:
    assert amap.SOURCE_NAME == "amap"
    assert amap.DEFAULT_ENDPOINT == "https://restapi.amap.com/v3"
    assert amap.ENV_AMAP_KEY == "WHERE2GO_AMAP_KEY"
    assert amap.AUTO_MAX_RADIUS_M == 50000
    assert amap.PAGE_SIZE == 25
    assert amap.MAX_PAGE == 8
    assert amap.MAX_ROWS_PER_QUERY == amap.PAGE_SIZE * amap.MAX_PAGE == 200
    assert amap.MIN_REQUEST_INTERVAL_S == 0.6
    assert amap.MAX_ATTEMPTS == 3
    assert amap.RETRY_BACKOFF_S == (2.0, 5.0)


def test_request_url_key_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认端点(v3)+ key 走 query 参数 + 超时 15s。"""
    monkeypatch.delenv(amap.ENV_ENDPOINT, raising=False)
    session = FakeSession(GEOCODE_OK)
    call(amap.geocode, session, "杭州西湖")
    assert session.last.method == "GET"
    assert session.last.url == "https://restapi.amap.com/v3/geocode/geo"
    assert session.last.params["key"] == "test-amap-key"
    assert session.last.params["address"] == "杭州西湖"
    assert session.last.timeout == 15.0


def test_endpoint_env_override_strips_slash() -> None:
    session = FakeSession(GEOCODE_OK)
    environ = {**ENVS, amap.ENV_ENDPOINT: "https://amap.example.test/v3/"}
    amap.geocode("杭州", environ=environ, session=session)
    assert session.last.url == "https://amap.example.test/v3/geocode/geo"


def test_key_from_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """不传 ``environ=`` 时回落 :data:`os.environ`(线上真实调用路径)。"""
    monkeypatch.setenv(amap.ENV_AMAP_KEY, "env-key-123")
    monkeypatch.delenv(amap.ENV_ENDPOINT, raising=False)
    session = FakeSession(GEOCODE_OK)
    amap.geocode("杭州", session=session)
    assert session.last.params["key"] == "env-key-123"


@pytest.mark.parametrize("func,args", [
    (amap.geocode, ("杭州",)),
    (amap.reverse_geocode, (30.274084, 120.155070)),
    (amap.search_around, (30.274084, 120.155070)),
    (amap.search_polygon, ("119.6,29.8~120.7,30.8",)),
    (amap.driving, (30.274084, 120.155070, 31.230416, 121.473701)),
])
def test_missing_key_raises_chinese_error(func: Callable[..., Any], args: tuple[Any, ...]) -> None:
    """没配 key → 中文 :class:`DataSourceError`,且**一次 HTTP 都不发**。"""
    session = FakeSession(AROUND_OK)
    exc = expect_error(lambda: func(*args, environ={}, session=session), DataSourceError,
                       "未配置 WHERE2GO_AMAP_KEY", "高德 Web 服务 key")
    assert exc.source == "amap"
    assert not isinstance(exc, TransientDataSourceError)
    assert session.calls == []


def test_default_session_uses_amap_proxy_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """``amap`` 注册为国内直连(``PROXY_OFF``),``WHERE2GO_PROXY_AMAP`` 可覆盖。"""
    monkeypatch.delenv("WHERE2GO_PROXY_AMAP", raising=False)
    assert _common.DEFAULT_SOURCE_PROXY["amap"] == _common.PROXY_OFF
    assert _common.proxy_mode("amap") == _common.PROXY_OFF
    assert _common.proxy_mode("AMAP") == _common.PROXY_OFF
    session = _common.build_session(source="amap")
    assert session.trust_env is False
    assert session.proxies == {}
    assert amap._resolve_session(None) is amap._resolve_session(None)

    monkeypatch.setenv("WHERE2GO_PROXY_AMAP", "http://127.0.0.1:7892")
    assert _common.proxy_mode("amap") == "http://127.0.0.1:7892"
    proxied = _common.build_session(source="amap")
    assert proxied.proxies == {"http": "http://127.0.0.1:7892", "https": "http://127.0.0.1:7892"}


# --------------------------------------------------------------------------- #
# 地理编码 / 逆地理编码
# --------------------------------------------------------------------------- #


def test_geocode_parses_keys_and_lnglat_order() -> None:
    session = FakeSession(GEOCODE_OK)
    places = call(amap.geocode, session, "杭州西湖")
    assert len(places) == 1
    assert set(places[0]) == set(amap.GEOCODE_KEYS)
    assert places[0] == {
        "formatted_address": "浙江省杭州市西湖区西湖",
        "province": "浙江省",
        "city": "杭州市",
        "district": "西湖区",
        "adcode": "330106",
        "township": "",
        "lng": 120.147913,     # location 是 "lng,lat" —— 经度在前
        "lat": 30.242451,
    }
    assert isinstance(places[0]["lat"], float) and isinstance(places[0]["lng"], float)


def test_geocode_city_param_and_municipality_empty_city() -> None:
    session = FakeSession(GEOCODE_MUNICIPAL)
    places = call(amap.geocode, session, "北京市", city="北京")
    assert session.last.params["city"] == "北京"
    assert places[0]["city"] == ""          # 直辖市高德返回 [] → 收敛成 ""
    assert places[0]["province"] == "北京市"
    assert places[0]["lng"] == pytest.approx(116.407526)
    assert places[0]["lat"] == pytest.approx(39.904030)


def test_geocode_omits_empty_city_param() -> None:
    session = FakeSession(GEOCODE_OK)
    call(amap.geocode, session, "杭州西湖")
    assert "city" not in session.last.params


def test_geocode_empty_results() -> None:
    session = FakeSession(GEOCODE_EMPTY)
    assert call(amap.geocode, session, "不存在的地名") == []


@pytest.mark.parametrize("payload", [{"status": "1", "infocode": "10000"}, {"status": "1", "geocodes": {}}])
def test_geocode_missing_or_bad_geocodes_is_empty(payload: dict[str, Any]) -> None:
    session = FakeSession(FakeResponse(payload))
    assert call(amap.geocode, session, "杭州") == []


def test_geocode_blank_address_raises_value_error() -> None:
    with pytest.raises(ValueError, match="address"):
        amap.geocode("   ", environ=ENVS, session=FakeSession(GEOCODE_OK))


def test_geocode_unparsable_location_raises() -> None:
    payload = ok({"geocodes": [{"formatted_address": "x", "location": "abc"}]})
    session = FakeSession(FakeResponse(payload))
    expect_error(lambda: call(amap.geocode, session, "杭州"), DataSourceError, "location", "无法解析")


def test_reverse_geocode_params_and_parse() -> None:
    session = FakeSession(REGEO_OK)
    place = call(amap.reverse_geocode, session, 30.2424512, 120.1479134)
    assert session.last.url.endswith("/geocode/regeo")
    assert session.last.params["location"] == "120.147913,30.242451"   # lng,lat
    assert session.last.params["extensions"] == "base"
    assert set(place) == set(amap.GEOCODE_KEYS)
    assert place["formatted_address"].startswith("浙江省杭州市西湖区")
    assert place["township"] == "灵隐街道"
    assert place["adcode"] == "330106"
    assert place["lat"] == pytest.approx(30.2424512)
    assert place["lng"] == pytest.approx(120.1479134)


def test_reverse_geocode_no_result_is_empty_dict() -> None:
    session = FakeSession(REGEO_EMPTY)
    assert call(amap.reverse_geocode, session, 30.2424512, 120.1479134) == {}


def test_reverse_geocode_missing_address_component() -> None:
    payload = ok({"regeocode": {"formatted_address": "浙江省杭州市"}})
    session = FakeSession(FakeResponse(payload))
    place = call(amap.reverse_geocode, session, 30.2, 120.1)
    assert place["formatted_address"] == "浙江省杭州市"
    assert place["province"] == "" and place["city"] == "" and place["district"] == ""
    assert place["township"] == "" and place["adcode"] == ""


def test_invalid_coordinates_raise_value_error() -> None:
    session = FakeSession(AROUND_OK)
    with pytest.raises(ValueError, match="纬度"):
        call(amap.reverse_geocode, session, 95.0, 120.0)
    with pytest.raises(ValueError, match="经度"):
        call(amap.search_around, session, 30.0, 190.0)
    with pytest.raises(ValueError, match="坐标必须为数字"):
        call(amap.search_around, session, "abc", 120.0)


# --------------------------------------------------------------------------- #
# place/around
# --------------------------------------------------------------------------- #


def test_search_around_params() -> None:
    session = FakeSession(AROUND_OK)
    call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1],
         radius_m=5000, types=("080106", "110000"), keywords=None, page=2, offset=20)
    params = session.last.params
    assert session.last.url == "https://restapi.amap.com/v3/place/around"
    assert params["location"] == "120.155070,30.274084"   # lng,lat(经度在前)
    assert params["radius"] == 5000
    assert params["types"] == "080106|110000"
    assert params["page"] == 2
    assert params["offset"] == 20
    assert params["extensions"] == "base"
    assert "keywords" not in params


def test_search_around_normalized_pois() -> None:
    session = FakeSession(AROUND_OK)
    places = call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1], types=("080106",))
    assert [place["name"] for place in places] == ["太舞滑雪场", "太子湾公园"]  # 保持服务端顺序
    first = places[0]
    assert tuple(first) == amap.POI_KEYS
    assert first == {
        "id": "B023B17WWK",
        "name": "太舞滑雪场",
        "lat": 40.987654,
        "lng": 115.412345,
        "type": "体育休闲服务;运动场馆;滑雪场",
        "typecode": "080106",
        "address": "崇礼区太舞小镇",
        "cityname": "张家口市",
        "adname": "崇礼区",
        "distance_m": 1234,
    }
    assert isinstance(first["lat"], float) and isinstance(first["lng"], float)
    assert isinstance(first["distance_m"], int)
    second = places[1]
    assert second["distance_m"] is None    # polygon/无距离时高德给 []
    assert second["cityname"] == ""
    assert second["adname"] == "西湖区"


def test_search_around_skips_dirty_pois() -> None:
    session = FakeSession(AROUND_DIRTY)
    places = call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1])
    assert [place["id"] for place in places] == ["B023B17WWK"]


def test_search_around_empty_and_missing_pois() -> None:
    session = FakeSession(AROUND_EMPTY)
    assert call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1]) == []
    session = FakeSession(FakeResponse(ok({})))
    assert call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1]) == []


@pytest.mark.parametrize("given,expected", [
    (50000, 50000),
    (60000, 50000),      # 实测 radius 被服务端截断在 50000 → 客户端先钳住
    (100000, 50000),
    (999999999, 50000),
    (0, 1),
    (-5, 1),
    ("3000", 3000),
    (1234.6, 1235),
])
def test_search_around_radius_clamped(given: Any, expected: int) -> None:
    session = FakeSession(AROUND_EMPTY)
    call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1], radius_m=given)
    assert session.last.params["radius"] == expected
    assert amap.clamp_radius(given) == expected


def test_search_around_page_beyond_max_short_circuits() -> None:
    """``page>=9`` 实测恒返回 0 条 → 直接空列表,**不发这次无效请求**;page=8 照常发。"""
    session = FakeSession(AROUND_OK)
    assert call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1], page=9) == []
    assert call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1], page=99) == []
    assert session.calls == []
    places = call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1], page=8)
    assert len(places) == 2
    assert session.last.params["page"] == 8


@pytest.mark.parametrize("given,expected", [(0, 1), (-1, 1), (1, 1), (25, 25), (26, 25), (100, 25)])
def test_offset_clamped(given: Any, expected: int) -> None:
    session = FakeSession(AROUND_EMPTY)
    call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1], offset=given)
    assert session.last.params["offset"] == expected


def test_page_zero_clamped_to_one() -> None:
    session = FakeSession(AROUND_EMPTY)
    call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1], page=0)
    assert session.last.params["page"] == 1


def test_keywords_normalized() -> None:
    session = FakeSession(AROUND_EMPTY)
    call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1], keywords=("古镇", "老街", "古城"))
    assert session.last.params["keywords"] == "古镇|老街|古城"
    call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1], keywords="古镇|老街|古城")
    assert session.last.params["keywords"] == "古镇|老街|古城"
    call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1], types="080106")
    assert session.last.params["types"] == "080106"


def test_normalize_multi_dedupes_and_drops_empty() -> None:
    assert amap.normalize_types(("080106", "080106", "", None, "110000")) == "080106|110000"
    assert amap.normalize_keywords(None) == ""
    assert amap.normalize_keywords([]) == ""


def test_distance_float_and_none() -> None:
    payload = ok({"pois": [
        {**POI_SKI, "id": "B1", "distance": "1234.5"},
        {**POI_SKI, "id": "B2", "distance": []},
        {**POI_SKI, "id": "B3", "distance": "abc"},
    ]})
    session = FakeSession(FakeResponse(payload))
    places = call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1])
    assert places[0]["distance_m"] == 1234.5
    assert places[1]["distance_m"] is None
    assert places[2]["distance_m"] is None


# --------------------------------------------------------------------------- #
# place/polygon
# --------------------------------------------------------------------------- #

RING = ((119.6, 29.8), (120.7, 29.8), (120.7, 30.8), (119.6, 30.8))
RING_PARAM = "119.600000,29.800000;120.700000,29.800000;120.700000,30.800000;119.600000,30.800000"


def test_search_polygon_params_and_separator() -> None:
    session = FakeSession(AROUND_OK)
    places = call(amap.search_polygon, session, RING, types=("110000",), page=3)
    params = session.last.params
    assert session.last.url == "https://restapi.amap.com/v3/place/polygon"
    assert params["polygon"] == RING_PARAM          # 顶点用 ";" 分隔、lng,lat 顺序
    assert params["types"] == "110000"
    assert params["page"] == 3
    assert params["offset"] == amap.PAGE_SIZE
    assert params["extensions"] == "base"
    assert "radius" not in params
    assert len(places) == 2


def test_search_polygon_diagonal_shorthand() -> None:
    """对角线简写 ``"lng,lat~lng,lat"`` 展开成 4 顶点矩形(左下/右下/右上/左上)。"""
    session = FakeSession(AROUND_EMPTY)
    call(amap.search_polygon, session, "119.6,29.8~120.7,30.8")
    assert session.last.params["polygon"] == RING_PARAM
    call(amap.search_polygon, session, "120.7,30.8~119.6,29.8")   # 角点顺序反了也一样
    assert session.last.params["polygon"] == RING_PARAM
    assert amap.normalize_polygon("119.6,29.8~120.7,30.8") == [
        (119.6, 29.8), (120.7, 29.8), (120.7, 30.8), (119.6, 30.8)
    ]


def test_search_polygon_semicolon_string_passthrough() -> None:
    session = FakeSession(AROUND_EMPTY)
    text = "119.6,29.8;120.7,29.8;120.7,30.8;119.6,30.8;119.6,29.8"
    call(amap.search_polygon, session, text)
    assert session.last.params["polygon"] == (
        "119.600000,29.800000;120.700000,29.800000;120.700000,30.800000;"
        "119.600000,30.800000;119.600000,29.800000"
    )


def test_search_polygon_string_point_list() -> None:
    session = FakeSession(AROUND_EMPTY)
    call(amap.search_polygon, session, ["119.6,29.8", "120.7,29.8", "120.7,30.8", "119.6,30.8"])
    assert session.last.params["polygon"] == RING_PARAM


@pytest.mark.parametrize("polygon", [
    ((119.6, 29.8), (120.7, 29.8), (120.7, 30.8)),          # 只有 3 个点
    ((119.6, 29.8), (120.7, 29.8)),
    "119.6,29.8",                                             # 单点
    "119.6,29.8;120.7,30.8;120.7,29.8",                       # 3 点串
    [],
    (),
    "",
    "   ",
])
def test_search_polygon_too_few_points(polygon: Any) -> None:
    session = FakeSession(AROUND_OK)
    with pytest.raises(ValueError, match="polygon"):
        call(amap.search_polygon, session, polygon)
    assert session.calls == []


@pytest.mark.parametrize("polygon", [
    ((200.0, 29.8), (120.7, 29.8), (120.7, 30.8), (119.6, 30.8)),   # 经度越界
    ((119.6, 99.8), (120.7, 29.8), (120.7, 30.8), (119.6, 30.8)),   # 纬度越界
    (("abc", 29.8), (120.7, 29.8), (120.7, 30.8), (119.6, 30.8)),   # 非数字
    "abc~120.7,30.8",
    ((119.6, 29.8, 3.0), (120.7, 29.8)),                            # 分量数不对
    {"a": 1},
])
def test_search_polygon_invalid_points(polygon: Any) -> None:
    with pytest.raises(ValueError, match="polygon"):
        amap.normalize_polygon(polygon)


def test_search_polygon_page_beyond_max_short_circuits() -> None:
    session = FakeSession(AROUND_OK)
    assert call(amap.search_polygon, session, RING, page=9) == []
    assert session.calls == []


def test_grid_cell_feeds_search_polygon() -> None:
    """``grid_polygons`` 的格子可以直接喂给 ``search_polygon``(TASK-9b 分格抓取)。"""
    cells = amap.grid_polygons(29.0, 119.0, 30.0, 121.0, 1, 1)
    session = FakeSession(AROUND_EMPTY)
    call(amap.search_polygon, session, cells[0], types=("110000",))
    assert session.last.params["polygon"] == (
        "119.000000,29.000000;121.000000,29.000000;121.000000,30.000000;119.000000,30.000000"
    )


# --------------------------------------------------------------------------- #
# decode_polyline / thin_points / grid_polygons(纯函数)
# --------------------------------------------------------------------------- #


def test_decode_polyline_swaps_to_latlng() -> None:
    assert amap.decode_polyline("120.155070,30.274084;121.473701,31.230416") == [
        [30.274084, 120.155070],
        [31.230416, 121.473701],
    ]


@pytest.mark.parametrize("text", ["", "   ", None, [], 123])
def test_decode_polyline_empty(text: Any) -> None:
    assert amap.decode_polyline(text) == []


def test_decode_polyline_skips_bad_chunks() -> None:
    decoded = amap.decode_polyline("abc;116.4,39.9;;200.0,39.9;116.5,39.95;116.5")
    assert decoded == [[39.9, 116.4], [39.95, 116.5]]


def test_thin_points_keeps_ends() -> None:
    points = [[float(index), float(index) * 2] for index in range(3000)]
    thinned = amap.thin_points(points, max_points=amap.GEOMETRY_MAX_POINTS)
    assert len(thinned) == amap.GEOMETRY_MAX_POINTS == 1200
    assert thinned[0] == points[0]
    assert thinned[-1] == points[-1]
    assert amap.thin_points(points[:5], max_points=1200) == points[:5]
    assert amap.thin_points([], max_points=1200) == []
    assert amap.thin_points(None) == []


def test_grid_polygons_count_and_corner_order() -> None:
    cells = amap.grid_polygons(29.0, 119.0, 30.0, 121.0, 2, 2)
    assert len(cells) == 4
    assert all(len(cell) == 4 for cell in cells)
    # 行优先、自南向北、自西向东;每格 4 点 = 左下/右下/右上/左上
    assert cells[0] == [(119.0, 29.0), (120.0, 29.0), (120.0, 29.5), (119.0, 29.5)]
    assert cells[1] == [(120.0, 29.0), (121.0, 29.0), (121.0, 29.5), (120.0, 29.5)]
    assert cells[2] == [(119.0, 29.5), (120.0, 29.5), (120.0, 30.0), (119.0, 30.0)]
    assert cells[3] == [(120.0, 29.5), (121.0, 29.5), (121.0, 30.0), (120.0, 30.0)]


def test_grid_polygons_covers_bbox_without_gaps() -> None:
    cells = amap.grid_polygons(29.0, 119.0, 30.0, 121.0, 3, 4)
    assert len(cells) == 12
    lngs = [point[0] for cell in cells for point in cell]
    lats = [point[1] for cell in cells for point in cell]
    assert min(lngs) == pytest.approx(119.0) and max(lngs) == pytest.approx(121.0)
    assert min(lats) == pytest.approx(29.0) and max(lats) == pytest.approx(30.0)
    # 相邻格子共边:每个内部经度/纬度线都出现两次(左右格各一次)
    assert len(cells[0]) == 4 and cells[0][0] == (119.0, 29.0)


def test_grid_polygons_invalid_arguments() -> None:
    with pytest.raises(ValueError, match="rows"):
        amap.grid_polygons(29.0, 119.0, 30.0, 121.0, 0, 2)
    with pytest.raises(ValueError, match="rows"):
        amap.grid_polygons(29.0, 119.0, 30.0, 121.0, 2, -1)
    with pytest.raises(ValueError, match="纬度"):
        amap.grid_polygons(30.0, 119.0, 29.0, 121.0, 2, 2)
    with pytest.raises(ValueError, match="经度"):
        amap.grid_polygons(29.0, 121.0, 30.0, 119.0, 2, 2)
    with pytest.raises(ValueError, match="数字"):
        amap.grid_polygons("abc", 119.0, 30.0, 121.0, 2, 2)


# --------------------------------------------------------------------------- #
# direction/driving
# --------------------------------------------------------------------------- #


def test_driving_parses_path_and_ignores_tolls_cost() -> None:
    session = FakeSession(DRIVING_OK)
    route = call(amap.driving, session, HANGZHOU[0], HANGZHOU[1], SHANGHAI[0], SHANGHAI[1])
    assert tuple(route) == amap.DRIVING_KEYS
    assert route["distance_m"] == 175897
    assert route["duration_s"] == 10589
    assert route["toll_distance_m"] == 135514      # tolls=0 / cost=null 也要能拿到收费里程
    assert route["traffic_lights"] == 14
    assert route["steps_n"] == 2
    assert "tolls" not in route and "cost" not in route
    assert route["polyline"][0] == [30.274084, 120.155070]     # [lat, lng]
    assert route["polyline"][-1] == [31.230416, 121.473701]
    assert len(route["polyline"]) == 4


def test_driving_params_omit_strategy() -> None:
    """§6.5:``strategy`` 留在签名里(默认 11)但**不进请求参数**。"""
    session = FakeSession(DRIVING_OK)
    call(amap.driving, session, HANGZHOU[0], HANGZHOU[1], SHANGHAI[0], SHANGHAI[1])
    params = session.last.params
    assert session.last.url == "https://restapi.amap.com/v3/direction/driving"
    assert params["origin"] == "120.155070,30.274084"
    assert params["destination"] == "121.473701,31.230416"
    assert params["extensions"] == "all"
    assert "strategy" not in params
    assert amap.DEFAULT_DRIVING_STRATEGY == 11
    # 显式传 strategy 也一样不带上
    call(amap.driving, session, HANGZHOU[0], HANGZHOU[1], SHANGHAI[0], SHANGHAI[1], strategy=2)
    assert "strategy" not in session.last.params


def test_driving_without_geometry() -> None:
    session = FakeSession(DRIVING_OK)
    route = call(amap.driving, session, HANGZHOU[0], HANGZHOU[1], SHANGHAI[0], SHANGHAI[1],
                 with_geometry=False)
    assert session.last.params["extensions"] == "base"
    assert route["polyline"] == []
    assert route["steps_n"] == 2 and route["distance_m"] == 175897


def test_driving_takes_first_path_when_multiple() -> None:
    second = {**DRIVING_PATH, "distance": "999999", "duration": "9", "toll_distance": "1"}
    payload = ok({"route": {"paths": [DRIVING_PATH, second]}})
    session = FakeSession(FakeResponse(payload))
    route = call(amap.driving, session, HANGZHOU[0], HANGZHOU[1], SHANGHAI[0], SHANGHAI[1])
    assert route["distance_m"] == 175897
    assert route["toll_distance_m"] == 135514


def test_driving_missing_fields_and_steps() -> None:
    payload = ok({"route": {"paths": [{"distance": "1000"}]}})
    session = FakeSession(FakeResponse(payload))
    route = call(amap.driving, session, HANGZHOU[0], HANGZHOU[1], SHANGHAI[0], SHANGHAI[1])
    assert route["distance_m"] == 1000
    assert route["duration_s"] is None
    assert route["toll_distance_m"] is None
    assert route["traffic_lights"] is None
    assert route["steps_n"] == 0
    assert route["polyline"] == []


@pytest.mark.parametrize("payload", [
    ok({"route": {"paths": []}}),
    ok({"route": {}}),
    ok({}),
    ok({"route": {"paths": [None]}}),
])
def test_driving_no_route_raises(payload: dict[str, Any]) -> None:
    session = FakeSession(FakeResponse(payload))
    expect_error(
        lambda: call(amap.driving, session, HANGZHOU[0], HANGZHOU[1], SHANGHAI[0], SHANGHAI[1]),
        DataSourceError,
    )


def test_driving_thins_long_polyline() -> None:
    points = ";".join(f"{120.0 + index * 0.001:.6f},{30.0 + index * 0.001:.6f}" for index in range(3000))
    payload = ok({"route": {"paths": [{
        "distance": "9000", "duration": "900", "toll_distance": "0", "traffic_lights": "0",
        "steps": [{"polyline": points}],
    }]}})
    session = FakeSession(FakeResponse(payload))
    route = call(amap.driving, session, HANGZHOU[0], HANGZHOU[1], SHANGHAI[0], SHANGHAI[1])
    assert route["steps_n"] == 1
    assert len(route["polyline"]) == amap.GEOMETRY_MAX_POINTS
    assert route["polyline"][0] == [30.0, 120.0]
    assert route["polyline"][-1] == [pytest.approx(30.0 + 2999 * 0.001), pytest.approx(120.0 + 2999 * 0.001)]


def test_driving_invalid_coordinates() -> None:
    with pytest.raises(ValueError, match="纬度超出"):
        amap.driving(30.0, 120.0, 999.0, 121.0, environ=ENVS, session=FakeSession(DRIVING_OK))
    with pytest.raises(ValueError, match="坐标必须为数字"):
        amap.driving(30.0, "x", 31.0, 121.0, environ=ENVS, session=FakeSession(DRIVING_OK))


# --------------------------------------------------------------------------- #
# status / infocode 分派(§6.2 细分表)
# --------------------------------------------------------------------------- #

TRANSIENT_CODES = ("10004", "10014", "10015", "10016", "10019", "10020", "10021", "10029", "10044")
PERMANENT_CODES = ("10001", "10002", "10005", "10009", "10012", "10013", "10041",
                   "20000", "20001", "40000", "40002")


@pytest.mark.parametrize("infocode", TRANSIENT_CODES)
def test_transient_infocodes(infocode: str, fast_clock: list[float]) -> None:
    """限流/繁忙 → :class:`TransientDataSourceError`,并退避重试 2s / 5s(共 3 次尝试)。"""
    session = FakeSession(FakeResponse(fail(infocode, "CUQPS_HAS_EXCEEDED_THE_LIMIT")))
    exc = expect_error(lambda: call(amap.geocode, session, "杭州"),
                       TransientDataSourceError, infocode, "瞬时", "高德")
    assert exc.source == "amap"
    assert len(session.calls) == amap.MAX_ATTEMPTS == 3
    assert fast_clock == [2.0, 5.0]
    assert amap.retry_backoff_s(1) == 2.0 and amap.retry_backoff_s(2) == 5.0
    assert amap.retry_backoff_s(9) == 5.0


@pytest.mark.parametrize("infocode", PERMANENT_CODES)
def test_permanent_infocodes(infocode: str, fast_clock: list[float]) -> None:
    """key/权限/参数/配额 → 永久 :class:`DataSourceError`(**不重试**)。"""
    session = FakeSession(FakeResponse(fail(infocode, "INVALID_USER_KEY")))
    exc = expect_error(lambda: call(amap.geocode, session, "杭州"),
                       DataSourceError, infocode, "高德")
    assert not isinstance(exc, TransientDataSourceError)
    assert len(session.calls) == 1
    assert fast_clock == []


def test_permanent_infocode_messages_are_chinese() -> None:
    """文案中文,且带上 infocode 对应的解释(便于晨报定位是 key 还是平台不符)。"""
    session = FakeSession(FakeResponse(fail("10009", "USERKEY_PLAT_NOMATCH")))
    exc = expect_error(lambda: call(amap.geocode, session, "杭州"), DataSourceError, "10009")
    assert "平台" in str(exc)
    session = FakeSession(FakeResponse(fail("10001", "INVALID_USER_KEY")))
    assert "key" in str(expect_error(lambda: call(amap.geocode, session, "杭州"), DataSourceError))
    session = FakeSession(FakeResponse(fail("10021", "CUQPS_HAS_EXCEEDED_THE_LIMIT")))
    assert "QPS" in str(expect_error(lambda: call(amap.geocode, session, "杭州"),
                                     TransientDataSourceError))


def test_unknown_infocode_is_permanent_with_raw_text(fast_clock: list[float]) -> None:
    """表外 infocode → 永久错误,文案里带 infocode 原文与 info 原文。"""
    session = FakeSession(FakeResponse(fail("30001", "ENGINE_RESPONSE_DATA_ERROR")))
    exc = expect_error(lambda: call(amap.geocode, session, "杭州"), DataSourceError,
                       "30001", "ENGINE_RESPONSE_DATA_ERROR")
    assert not isinstance(exc, TransientDataSourceError)
    assert len(session.calls) == 1
    assert fast_clock == []


def test_status_ok_passes_even_with_infocode_10000() -> None:
    session = FakeSession(FakeResponse({"status": "1", "infocode": "10000", "info": "OK",
                                        "geocodes": GEOCODE_OK["geocodes"]}))
    places = call(amap.geocode, session, "杭州西湖")
    assert places and places[0]["adcode"] == "330106"


@pytest.mark.parametrize("payload", [
    {"status": "0"},                                     # 缺 infocode / info
    {"status": 0, "infocode": "20000"},                  # status 是数字 0
    {},                                                  # 空对象
])
def test_status_edge_cases(payload: dict[str, Any]) -> None:
    session = FakeSession(FakeResponse(payload))
    exc = expect_error(lambda: call(amap.geocode, session, "杭州"), DataSourceError, "status")
    assert not isinstance(exc, TransientDataSourceError)


@pytest.mark.parametrize("payload", ["[]", '"text"', "null"])
def test_non_object_json_payload_raises(payload: str) -> None:
    session = FakeSession(FakeResponse(None, text=payload))
    expect_error(lambda: call(amap.geocode, session, "杭州"), DataSourceError, "响应格式异常")


def test_non_json_response_is_transient(fast_clock: list[float]) -> None:
    """200 + HTML 繁忙页 → 瞬时错误(与 Photon/Overpass 同口径),同样退避重试。"""
    session = FakeSession(FakeResponse(None, text="<html><body>502 Bad Gateway</body></html>"))
    exc = expect_error(lambda: call(amap.geocode, session, "杭州"),
                       TransientDataSourceError, "不是合法 JSON")
    assert "502 Bad Gateway" in str(exc)
    assert len(session.calls) == 3
    assert fast_clock == [2.0, 5.0]


# --------------------------------------------------------------------------- #
# 网络错误与重试
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("error", [
    requests.Timeout("read timed out"),
    requests.ConnectionError("connection refused"),
])
def test_network_errors_are_transient_and_retried(error: BaseException, fast_clock: list[float]) -> None:
    session = FakeSession(error)
    expect_error(lambda: call(amap.geocode, session, "杭州"), TransientDataSourceError)
    assert len(session.calls) == amap.MAX_ATTEMPTS
    assert fast_clock == [2.0, 5.0]


def test_retry_recovers_on_third_attempt() -> None:
    """前两次限流、第三次成功 → 正常返回结果(上层看不到抖动)。"""
    session = FakeSession(
        FakeResponse(fail("10021", "CUQPS_HAS_EXCEEDED_THE_LIMIT")),
        FakeResponse(fail("10015", "SERVER_BUSY")),
        FakeResponse(AROUND_OK),
    )
    places = call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1], types=("080106",))
    assert [place["name"] for place in places] == ["太舞滑雪场", "太子湾公园"]
    assert len(session.calls) == 3


def test_permanent_error_stops_retry_immediately(fast_clock: list[float]) -> None:
    session = FakeSession(FakeResponse(fail("10001", "INVALID_USER_KEY")), FakeResponse(AROUND_OK))
    expect_error(lambda: call(amap.search_around, session, HANGZHOU[0], HANGZHOU[1]),
                 DataSourceError, "10001")
    assert len(session.calls) == 1
    assert fast_clock == []


def test_http_error_status_maps_to_datasource_error() -> None:
    """高德 HTTP 码恒 200,但真收到 4xx/5xx 时沿用 ``_common`` 的口径(5xx 瞬时、4xx 永久)。"""
    session = FakeSession(FakeResponse(None, status_code=503, text="busy"))
    expect_error(lambda: call(amap.geocode, session, "杭州"), TransientDataSourceError, "503")
    session = FakeSession(FakeResponse(None, status_code=403, text="denied"))
    expect_error(lambda: call(amap.geocode, session, "杭州"), DataSourceError, "403")


# --------------------------------------------------------------------------- #
# 节流(§6.1:单进程最小间隔 0.6s,env 可覆盖)
# --------------------------------------------------------------------------- #


class ThrottleHarness:
    """假时钟 + 记录 ``sleep``:睡多久就把时钟推进多久(贴近真实 ``time.sleep`` 语义)。

    ``test_photon.py`` 的 :class:`FakeClock` 是「每读一次就前进固定步长」,拿来验节流
    会低估真实间隔(睡着的时候时钟也在走),所以这里单独做一个。
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        """模拟「请求之间的业务耗时」(不产生 sleep)。"""
        self.now += seconds

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(amap, "_CLOCK", self.clock)
        monkeypatch.setattr(amap, "_SLEEP", self.sleep)
        amap.reset_throttle()


def test_throttle_default_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """连发三次(时钟不走)→ 后两次各睡满 0.6s:两次请求的最小间隔达标。"""
    harness = ThrottleHarness()
    harness.install(monkeypatch)
    session = FakeSession(GEOCODE_OK)
    for _ in range(3):
        call(amap.geocode, session, "杭州西湖")
    assert harness.sleeps == [pytest.approx(0.6), pytest.approx(0.6)]
    assert all(wait >= amap.MIN_REQUEST_INTERVAL_S - 1e-9 for wait in harness.sleeps)
    assert amap.MIN_REQUEST_INTERVAL_S == 0.6


def test_throttle_accounts_for_elapsed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """上次请求后已经过了 0.2s → 只需再睡 0.4s(不是死等 0.6s)。"""
    harness = ThrottleHarness()
    harness.install(monkeypatch)
    session = FakeSession(GEOCODE_OK)
    call(amap.geocode, session, "杭州西湖")
    harness.advance(0.2)
    call(amap.geocode, session, "杭州西湖")
    assert harness.sleeps == [pytest.approx(0.4)]
    harness.advance(5.0)
    call(amap.geocode, session, "杭州西湖")
    assert harness.sleeps == [pytest.approx(0.4)]      # 间隔足够 → 不再睡


def test_throttle_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = ThrottleHarness()
    harness.install(monkeypatch)
    session = FakeSession(GEOCODE_OK)
    environ = {**ENVS, amap.ENV_MIN_INTERVAL: "0.05"}
    amap.geocode("杭州西湖", environ=environ, session=session)
    amap.geocode("杭州西湖", environ=environ, session=session)
    assert harness.sleeps == [pytest.approx(0.05)]


@pytest.mark.parametrize("value,expected", [("0", 0.0), ("-1", 0.0), ("abc", 0.6), ("", 0.6), ("0.4", 0.4)])
def test_min_interval_resolution(value: str, expected: float) -> None:
    environ = dict(ENVS)
    if value == "":
        environ.pop(amap.ENV_MIN_INTERVAL, None)
    else:
        environ[amap.ENV_MIN_INTERVAL] = value
    assert amap.min_interval_s(environ) == expected


def test_throttle_disabled_when_interval_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """间隔 0 → 完全不睡(离线脚本/压测用),但请求照发。"""
    harness = ThrottleHarness()
    harness.install(monkeypatch)
    session = FakeSession(GEOCODE_OK)
    environ = {**ENVS, amap.ENV_MIN_INTERVAL: "0"}
    for _ in range(4):
        amap.geocode("杭州西湖", environ=environ, session=session)
    assert harness.sleeps == []
    assert len(session.calls) == 4


def test_reset_throttle_allows_immediate_request(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = ThrottleHarness()
    harness.install(monkeypatch)
    session = FakeSession(GEOCODE_OK)
    call(amap.geocode, session, "杭州西湖")
    amap.reset_throttle()
    call(amap.geocode, session, "杭州西湖")
    assert harness.sleeps == []


def test_throttle_shared_across_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    """节流状态是**进程级**的:geocode → search_around → driving 连发也要各等 0.6s。"""
    harness = ThrottleHarness()
    harness.install(monkeypatch)
    geo = FakeSession(GEOCODE_OK)
    around = FakeSession(AROUND_OK)
    route = FakeSession(DRIVING_OK)
    call(amap.geocode, geo, "杭州西湖")
    call(amap.search_around, around, HANGZHOU[0], HANGZHOU[1])
    call(amap.driving, route, HANGZHOU[0], HANGZHOU[1], SHANGHAI[0], SHANGHAI[1])
    call(amap.search_polygon, around, RING)
    assert harness.sleeps == [pytest.approx(0.6)] * 3
    assert len(geo.calls) == 1 and len(around.calls) == 2 and len(route.calls) == 1
