"""TASK-25 单测:境外模式下地理编码**不再走高德** → 不再定位到国内同名 POI。

病根(2026-10-13 神朱直报 + 主会话四组实测,契约 ``docs/TASK-25-CONTRACT.md`` §0):
``/api/geocode`` 的 ``region`` 此前只是个**标签**,坐标始终由高德解析,而高德只服务境内、
对境外地名会 ``status=1`` 成功吸附**国内同名 POI**(「因特拉肯」→ 成都双流、「皇后镇」→
安阳林州、「巴黎」→ 高雄),OriginCache 里因此积了 13 行「amap + region 空串」的脏缓存。

修法三件:① 境外正向链 :func:`place_loader.geocode_overseas`(**Nominatim 主 → Photon 兜底**,
每腿带 ``include_country=True`` 做**国家码守卫**:判成 CN 或答不出国家码都当该腿错配降级);
② ``region`` 从 API 一路贯通到解析链(``geocode_with_fallback`` / ``resolve_origin*`` /
``reverse_with_fallback`` / ``resolve_reverse_origin*`` 全部加 ``region=None`` 关键字,
默认值 = 现状行为);③ ``/api/geocode`` 的缓存命中加两道**一致性闸**
(:func:`app.api.places._cache_row_is_consistent`),脏行再搜一次即自愈,不需要清库脚本。

全程不触网:``no_network``(autouse)把 :meth:`requests.Session.request` 换成抛错,
地理编码各腿用 ``monkeypatch`` 替换 :mod:`services.place_loader` 的模块属性
(``ds_geocode`` / ``ds_photon_geocode`` / ``ds_amap_geocode`` / ``ds_*_reverse``,
与 ``test_photon.py``、``test_region_gate.py`` 同一套路),断言**调用次数与参数**;
DB 用 ``tmp_path`` 下的临时 SQLite。

覆盖七组(契约 §2):
1. :func:`place_loader.geocode_overseas`:主腿命中即高德零调用、主腿抛错/判成境内/答不出
   国家码都降级、两腿全败抛 :class:`DataSourceError` 且消息含两腿原因;
2. :func:`place_loader.geocode_with_fallback` 的 ``region`` 分支(None/domestic → 高德照旧,
   overseas → 高德零调用);
3. :func:`place_loader.resolve_origin_with_source` 的 origin 四字段形状不变、坐标退化路径
   与 region 无关、注入的 ``(city)`` 单参替身不因新关键字报错;
4. API ``/api/geocode``:amap 脏行视为未命中并**覆盖写回**、region 串轨重解析、
   一致时零网络命中、响应键逐键零变化、非法 region 400 中文、境外链双失败 400 中文;
5. 自动判定路径(不带 region 参数)判出 overseas 时**也**走境外链;
6. 逆向:境外坐标高德零调用、境内照旧、双失败仍是 200 + ``resolved=false``;
7. 前端静态断言(在 ``test_frontend_routes.py`` 里):境外模式 label 的 title 说明改走 OSM 系。

运行:``cd backend && ../.venv/bin/python -m pytest -q test_geocode_overseas.py``
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

import pytest
import requests
from fastapi import HTTPException

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import places as places_api  # noqa: E402
from data_sources import DataSourceError  # noqa: E402
from db import init_db, make_engine, repository as repo, session_factory  # noqa: E402
from services import place_loader, region_gate  # noqa: E402

# 契约 §0 实测 ②:Nominatim(zh-CN + addressdetails=1)对中文境外名 8/8 全中
INTERLAKEN = {"lat": 46.6855, "lng": 7.8585}
QUEENSTOWN = {"lat": -45.0322, "lng": 168.6610}
FURANO = {"lat": 43.3469, "lng": 142.3917}
# 契约 §0 实测 ①/③:高德(与 Photon 正向)对同批中文名吸附到的**国内同名 POI**坐标 = 脏值
CHENGDU = {"lat": 30.482, "lng": 104.1}
ANYANG = {"lat": 35.880, "lng": 113.724}
KAOHSIUNG = {"lat": 22.656, "lng": 120.296}
SHANGHAI = {"lat": 31.2304, "lng": 121.4737}

NOMINATIM_CH = {**INTERLAKEN, "display_name": "因特拉肯, 瑞士",
                "country_code": "ch", "country": "瑞士"}
NOMINATIM_CN = {**SHANGHAI, "display_name": "上海市, 中国", "country_code": "cn", "country": "中国"}
PHOTON_NZ = {**QUEENSTOWN, "display_name": "皇后镇, 新西兰", "countrycode": "NZ", "country": "新西兰"}
PHOTON_CN = {**KAOHSIUNG, "display_name": "巴黎, 高雄市, 臺灣", "countrycode": "CN"}
PHOTON_JP = {**FURANO, "display_name": "富良野, 富良野市, 北海道, 日本", "countrycode": "JP"}

AMAP_SHANGHAI_ROW = {
    "formatted_address": "上海市", "province": "上海市", "city": "", "district": "",
    "adcode": "310000", "township": "", "lat": SHANGHAI["lat"], "lng": SHANGHAI["lng"],
}

# 既有 /api/geocode(+/reverse)的顶层键:TASK-25 一个键都不许动(逐键零变化)
LEGACY_GEOCODE_KEYS = {"origin", "geocoder", "region", "bands", "segments"}
LEGACY_REVERSE_KEYS = {"origin", "resolved", "geocoder", "region", "bands", "segments", "note"}
ORIGIN_KEYS = {"city", "name", "lat", "lng"}

OVERSEAS_CH = {"region": region_gate.REGION_OVERSEAS, "country_code": "CH",
               "detector": region_gate.DETECTOR_NOMINATIM}
DOMESTIC_CN = {"region": region_gate.REGION_DOMESTIC, "country_code": "CN",
               "detector": region_gate.DETECTOR_NOMINATIM}
UNKNOWN = {"region": region_gate.REGION_DOMESTIC, "country_code": None,
           "detector": region_gate.DETECTOR_NONE}


# --------------------------------------------------------------------------- #
# fixtures 与替身
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
    engine = make_engine(f"sqlite:///{tmp_path / 'geocode_overseas_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


def stub_nominatim(monkeypatch: pytest.MonkeyPatch, place: Optional[dict[str, Any]] = None,
                   *, error: Optional[BaseException] = None) -> list[dict[str, Any]]:
    """替换 ``place_loader.ds_geocode``(境外链**主腿** / 境内链末腿)并记录调用参数。"""
    calls: list[dict[str, Any]] = []

    def fake(query: str, *, limit: int = 1, include_country: bool = False, **kwargs: Any) -> Any:
        calls.append({"query": query, "limit": limit, "include_country": include_country})
        if error is not None:
            raise error
        return dict(place or {})

    monkeypatch.setattr(place_loader, "ds_geocode", fake)
    return calls


def stub_photon(monkeypatch: pytest.MonkeyPatch, places: Optional[list[dict[str, Any]]] = None,
                *, error: Optional[BaseException] = None) -> list[dict[str, Any]]:
    """替换 ``place_loader.ds_photon_geocode``(境外链**兜底腿** / 境内链第二腿)。"""
    calls: list[dict[str, Any]] = []

    def fake(query: str, *, limit: int = 5, include_country: bool = False, **kwargs: Any) -> Any:
        calls.append({"query": query, "limit": limit, "include_country": include_country})
        if error is not None:
            raise error
        return [dict(item) for item in (places or [])]

    monkeypatch.setattr(place_loader, "ds_photon_geocode", fake)
    return calls


def stub_amap(monkeypatch: pytest.MonkeyPatch,
              rows: Optional[list[dict[str, Any]]] = None) -> list[str]:
    """替换 ``place_loader.ds_amap_geocode``(境内主腿)并记录被问过的城市。"""
    calls: list[str] = []

    def fake(city: str) -> Any:
        calls.append(city)
        return [dict(row) for row in (rows if rows is not None else [AMAP_SHANGHAI_ROW])]

    monkeypatch.setattr(place_loader, "ds_amap_geocode", fake)
    return calls


def forbid_amap_forward(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """高德正向腿:境外轨**一次都不许打**。记录调用次数(抛错会被中间腿的宽 except 吞掉)。"""
    calls: list[str] = []

    def fake(city: str) -> Any:
        calls.append(city)
        raise AssertionError("境外轨不该打高德:高德会把境外名吸附到国内同名 POI")

    monkeypatch.setattr(place_loader, "ds_amap_geocode", fake)
    return calls


def forbid_amap_reverse(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, float]]:
    """高德逆向腿:境外 GPS 一次都不许问高德。"""
    calls: list[tuple[float, float]] = []

    def fake(lat: float, lng: float) -> Any:
        calls.append((lat, lng))
        raise AssertionError("境外坐标不该问高德逆地理")

    monkeypatch.setattr(place_loader, "ds_amap_reverse", fake)
    return calls


def stub_detect(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> list[str]:
    """替换 :func:`region_gate.detect_region` 并记录被问过的城市(计数替身)。"""
    calls: list[str] = []

    def fake(city: str) -> dict[str, Any]:
        calls.append(city)
        return dict(result)

    monkeypatch.setattr(region_gate, "detect_region", fake)
    return calls


def stub_detect_coords(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> list[tuple[float, float]]:
    """替换 :func:`region_gate.detect_region_coords` 并记录被问过的坐标。"""
    calls: list[tuple[float, float]] = []

    def fake(lat: float, lng: float) -> dict[str, Any]:
        calls.append((lat, lng))
        return dict(result)

    monkeypatch.setattr(region_gate, "detect_region_coords", fake)
    return calls


def spy_upsert(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """包一层 :func:`repo.upsert_origin_cache`:既记录写缓存的参数,也真的写进临时库。"""
    calls: list[dict[str, Any]] = []
    real = repo.upsert_origin_cache

    def fake(current: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return real(current, **kwargs)

    monkeypatch.setattr(repo, "upsert_origin_cache", fake)
    return calls


def stub_photon_reverse(monkeypatch: pytest.MonkeyPatch,
                        place: Optional[dict[str, Any]] = None,
                        *, error: Optional[BaseException] = None) -> list[tuple[float, float]]:
    calls: list[tuple[float, float]] = []

    def fake(lat: float, lng: float, **kwargs: Any) -> Any:
        calls.append((lat, lng))
        if error is not None:
            raise error
        return dict(place or {})

    monkeypatch.setattr(place_loader, "ds_photon_reverse", fake)
    return calls


def stub_nominatim_reverse(monkeypatch: pytest.MonkeyPatch,
                           place: Optional[dict[str, Any]] = None,
                           *, error: Optional[BaseException] = None) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(lat: float, lng: float, zoom: int = 10, **kwargs: Any) -> Any:
        calls.append({"lat": lat, "lng": lng, "zoom": zoom})
        if error is not None:
            raise error
        return dict(place or {})

    monkeypatch.setattr(place_loader, "ds_reverse", fake)
    return calls


# --------------------------------------------------------------------------- #
# 1. geocode_overseas:Nominatim 主 → Photon 兜底 + 国家码守卫,绝不打高德
# --------------------------------------------------------------------------- #


def test_geocode_overseas_nominatim_hit_never_touches_amap(monkeypatch: pytest.MonkeyPatch) -> None:
    """主腿命中(境外国家码)→ 直接返回,**高德零调用**、Photon 也不必再问。"""
    amap_calls = forbid_amap_forward(monkeypatch)
    nominatim = stub_nominatim(monkeypatch, NOMINATIM_CH)
    photon = stub_photon(monkeypatch, [PHOTON_NZ])

    geo, geocoder = place_loader.geocode_overseas("因特拉肯")

    assert geocoder == place_loader.GEOCODER_NOMINATIM
    assert geo == {"lat": INTERLAKEN["lat"], "lng": INTERLAKEN["lng"],
                   "display_name": "因特拉肯, 瑞士"}, "不再是成都双流的因特拉肯"
    assert amap_calls == [], "境外轨绝不打高德"
    assert photon == [], "主腿答上了就不该再问兜底腿"
    assert nominatim == [{"query": "因特拉肯", "limit": 1, "include_country": True}], \
        "必须 opt-in 国家码(守卫要用),且只打一次网络"


def test_geocode_overseas_returns_only_the_three_legacy_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回形状与 :func:`geocode_with_fallback` **完全一致**:国家码不许漏进返回值。"""
    forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, NOMINATIM_CH)
    geo, _ = place_loader.geocode_overseas("因特拉肯")
    assert set(geo) == {"lat", "lng", "display_name"}, "多带 country_code 会污染既有响应形状"


def test_geocode_overseas_nominatim_error_falls_back_to_photon(monkeypatch: pytest.MonkeyPatch) -> None:
    amap_calls = forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, error=DataSourceError("Nominatim", "直连超时"))
    photon = stub_photon(monkeypatch, [PHOTON_NZ])

    geo, geocoder = place_loader.geocode_overseas("皇后镇")

    assert geocoder == place_loader.GEOCODER_PHOTON
    assert geo == {"lat": QUEENSTOWN["lat"], "lng": QUEENSTOWN["lng"],
                   "display_name": "皇后镇, 新西兰"}
    assert amap_calls == []
    assert photon == [{"query": "皇后镇", "limit": place_loader.PHOTON_LIMIT, "include_country": True}]


def test_geocode_overseas_nominatim_cn_code_is_a_mismatch_and_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主腿判出**国内**国家码 = 错配 → 当失败降级到 Photon(不是拿它当结果)。"""
    forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, NOMINATIM_CN)
    stub_photon(monkeypatch, [PHOTON_NZ])

    geo, geocoder = place_loader.geocode_overseas("皇后镇")

    assert geocoder == place_loader.GEOCODER_PHOTON
    assert (geo["lat"], geo["lng"]) == (QUEENSTOWN["lat"], QUEENSTOWN["lng"]), \
        "国内坐标绝不能当境外结果返回"


def test_geocode_overseas_photon_cn_code_raises_with_both_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    """「巴黎→高雄」那条防线:兜底腿判成境内也当失败 → 两腿全败,消息含两腿原因。"""
    forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, error=DataSourceError("Nominatim", "服务繁忙(HTTP 503)"))
    stub_photon(monkeypatch, [PHOTON_CN])

    with pytest.raises(DataSourceError) as ctx:
        place_loader.geocode_overseas("巴黎")

    message = str(ctx.value)
    assert ctx.value.source == place_loader.GEOCODER_CHAIN_OVERSEAS == "Nominatim/Photon"
    assert "境外地理编码两个源都失败" in message and "巴黎" in message
    assert "Nominatim=服务繁忙" in message, "两腿原因都要报出来"
    assert "Photon=" in message and "境内" in message, "兜底腿要说明是判成境内被拒"


def test_geocode_overseas_blank_country_code_fails_the_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    """答不出国家码 = 这一腿失败(与 region_gate 同口径,**不当"境内"也不猜**)。"""
    forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, {**INTERLAKEN, "display_name": "因特拉肯, 瑞士"})
    stub_photon(monkeypatch, [{**QUEENSTOWN, "display_name": "皇后镇, 新西兰"}])

    with pytest.raises(DataSourceError) as ctx:
        place_loader.geocode_overseas("因特拉肯")

    message = str(ctx.value)
    assert "Nominatim=" in message and "Photon=" in message
    assert "没有国家码" in message


def test_geocode_overseas_both_legs_empty_result_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, {})
    stub_photon(monkeypatch, [])

    with pytest.raises(DataSourceError) as ctx:
        place_loader.geocode_overseas("新西伯利亚")

    assert ctx.value.source == "Nominatim/Photon"
    assert "新西伯利亚" in str(ctx.value)


def test_geocode_overseas_blank_city_is_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    amap_calls = forbid_amap_forward(monkeypatch)
    nominatim = stub_nominatim(monkeypatch, NOMINATIM_CH)
    with pytest.raises(ValueError, match="起点城市不能为空"):
        place_loader.geocode_overseas("   ")
    assert nominatim == [] and amap_calls == [], "空城市名快速失败,一次网都不触"


def test_geocode_overseas_does_not_call_detect_region_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """一次调用即拿到坐标 + 国家码:**不许**为了守卫再打一次判定源(契约 §1.1 ⚠️)。"""
    forbid_amap_forward(monkeypatch)
    nominatim = stub_nominatim(monkeypatch, NOMINATIM_CH)
    stub_photon(monkeypatch, [PHOTON_NZ])
    detects = stub_detect(monkeypatch, OVERSEAS_CH)

    place_loader.geocode_overseas("因特拉肯")

    assert detects == [], "守卫用同一腿的 include_country 结果,不再调 detect_region"
    assert len(nominatim) == 1, "Nominatim 只打一次网络"


# --------------------------------------------------------------------------- #
# 2. geocode_with_fallback 的 region 分支:overseas → 高德零调用
# --------------------------------------------------------------------------- #


def test_geocode_with_fallback_without_region_keeps_amap_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """``region`` 默认 None(以及 domestic / 垃圾值)→ 现状高德主链**一字不改**。"""
    for region in (None, "domestic", "DOMESTIC", "", "火星"):
        amap_calls = stub_amap(monkeypatch)
        nominatim = stub_nominatim(monkeypatch, NOMINATIM_CH)
        geo, geocoder = place_loader.geocode_with_fallback("上海", region=region)
        assert geocoder == place_loader.GEOCODER_AMAP, region
        assert (geo["lat"], geo["lng"]) == (SHANGHAI["lat"], SHANGHAI["lng"]), region
        assert amap_calls == ["上海"], region
        assert nominatim == [], f"境内链高德命中就不该问 Nominatim:{region}"


def test_geocode_with_fallback_positional_call_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """既有调用点(``discover.py``)只传一个位置参数:签名新增 keyword-only 不影响它。"""
    amap_calls = stub_amap(monkeypatch)
    geo, geocoder = place_loader.geocode_with_fallback("上海")
    assert geocoder == place_loader.GEOCODER_AMAP and amap_calls == ["上海"]
    assert geo["display_name"] == "上海市"


def test_geocode_with_fallback_overseas_skips_amap(monkeypatch: pytest.MonkeyPatch) -> None:
    amap_calls = forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, NOMINATIM_CH)

    geo, geocoder = place_loader.geocode_with_fallback("因特拉肯", region="overseas")

    assert geocoder == place_loader.GEOCODER_NOMINATIM
    assert (geo["lat"], geo["lng"]) == (INTERLAKEN["lat"], INTERLAKEN["lng"])
    assert amap_calls == []


def test_geocode_with_fallback_overseas_is_case_and_space_tolerant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """与 :func:`place_loader.is_overseas_region` 同口径:大小写/空白宽容。"""
    for region in ("overseas", " OVERSEAS ", "Overseas"):
        forbid_amap_forward(monkeypatch)
        stub_nominatim(monkeypatch, NOMINATIM_CH)
        assert place_loader.geocode_with_fallback("因特拉肯", region=region)[1] == "nominatim", region


def test_geocode_with_fallback_overseas_double_failure_source_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """境外链的报错源名**不含高德**(它压根没参与),便于运维分辨是哪条链挂了。"""
    forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, error=DataSourceError("Nominatim", "直连超时"))
    stub_photon(monkeypatch, error=DataSourceError("Photon", "被限流(HTTP 429)"))

    with pytest.raises(DataSourceError) as ctx:
        place_loader.geocode_with_fallback("因特拉肯", region=region_gate.REGION_OVERSEAS)

    assert ctx.value.source == "Nominatim/Photon"
    assert "高德" not in ctx.value.source and "amap" not in str(ctx.value)


# --------------------------------------------------------------------------- #
# 3. resolve_origin_with_source 的 region 贯通与形状不变
# --------------------------------------------------------------------------- #


def test_resolve_origin_with_source_overseas_uses_overseas_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    amap_calls = forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, NOMINATIM_CH)

    origin, geocoder = place_loader.resolve_origin_with_source("因特拉肯", region="overseas")

    assert set(origin) == ORIGIN_KEYS, "origin 四字段形状逐键不变"
    assert origin == {"city": "因特拉肯", "name": "因特拉肯, 瑞士", **INTERLAKEN}
    assert geocoder == place_loader.GEOCODER_NOMINATIM and amap_calls == []
    assert place_loader.resolve_origin("因特拉肯", region="overseas") == origin


def test_resolve_origin_with_source_domestic_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_amap(monkeypatch)
    origin, geocoder = place_loader.resolve_origin_with_source("上海", region="domestic")
    assert geocoder == place_loader.GEOCODER_AMAP
    assert origin == {"city": "上海", "name": "上海市", **SHANGHAI}


def test_resolve_origin_coordinates_ignore_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """给了 lat/lng 的坐标退化路径与 region 无关:不问任何地理编码源。"""
    amap_calls = forbid_amap_forward(monkeypatch)
    nominatim = stub_nominatim(monkeypatch, NOMINATIM_CH)

    origin, geocoder = place_loader.resolve_origin_with_source(
        "因特拉肯", lat=INTERLAKEN["lat"], lng=INTERLAKEN["lng"], region="overseas"
    )

    assert geocoder == place_loader.GEOCODER_NONE
    assert origin == {"city": "因特拉肯", "name": "因特拉肯", **INTERLAKEN}
    assert nominatim == [] and amap_calls == []


def test_resolve_origin_injected_single_arg_stub_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入的 ``(city)`` 单参替身不因新增 ``region`` 关键字报错(既有单测口径零改动)。"""
    seen: list[str] = []

    def legacy_stub(city: str) -> dict[str, Any]:
        seen.append(city)
        return {"city": city, "name": f"{city}, 中国", **SHANGHAI}

    origin, geocoder = place_loader.resolve_origin_with_source(
        "上海", geocoder=legacy_stub, region="overseas"
    )
    assert seen == ["上海"] and origin["lat"] == SHANGHAI["lat"]
    assert geocoder == place_loader.GEOCODER_NOMINATIM, "替身不带 geocoder 字段时仍按既有口径报 nominatim"


def test_resolve_origin_injected_region_aware_stub_receives_region(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Optional[str]] = []

    def aware_stub(city: str, *, region: Optional[str] = None) -> dict[str, Any]:
        seen.append(region)
        return {"city": city, "name": city, **INTERLAKEN, "geocoder": place_loader.GEOCODER_PHOTON}

    _, geocoder = place_loader.resolve_origin_with_source(
        "因特拉肯", geocoder=aware_stub, region="overseas"
    )
    assert seen == ["overseas"] and geocoder == place_loader.GEOCODER_PHOTON


def test_default_geocoder_threads_region(monkeypatch: pytest.MonkeyPatch) -> None:
    amap_calls = forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, NOMINATIM_CH)

    geo = place_loader.default_geocoder("因特拉肯", region="overseas")

    assert geo == {"city": "因特拉肯", "name": "因特拉肯, 瑞士", **INTERLAKEN,
                   "geocoder": place_loader.GEOCODER_NOMINATIM}
    assert amap_calls == []


# --------------------------------------------------------------------------- #
# 4. API /api/geocode:region 驱动解析链 + 缓存一致性闸(脏行自愈)
# --------------------------------------------------------------------------- #


def test_api_geocode_overseas_overwrites_dirty_amap_row(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """契约 §0 实测 ③ 的脏行(amap + region 空 + 成都坐标)→ 视为未命中并**覆盖写回**。"""
    repo.upsert_origin_cache(session, city="因特拉肯", name="四川省成都市双流区因特拉肯(东北门)",
                             lat=CHENGDU["lat"], lng=CHENGDU["lng"], geocoder="amap")
    session.commit()
    assert repo.get_origin_cache(session, city="因特拉肯").region == ""

    amap_calls = forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, NOMINATIM_CH)
    stub_photon(monkeypatch, [PHOTON_NZ])
    writes = spy_upsert(monkeypatch)
    detects = stub_detect(monkeypatch, UNKNOWN)

    payload = places_api.geocode_city(city="因特拉肯", region="overseas", session=session)

    assert payload["geocoder"] in {place_loader.GEOCODER_NOMINATIM, place_loader.GEOCODER_PHOTON}
    assert payload["geocoder"] == "nominatim" and payload["region"] == "overseas"
    assert payload["origin"] == {"city": "因特拉肯", "name": "因特拉肯, 瑞士", **INTERLAKEN}
    assert amap_calls == [] and detects == [], "override 生效:既不打高德也不调判定源"
    assert [write["region"] for write in writes] == ["overseas"], "覆盖写回要带生效 region"

    row = repo.get_origin_cache(session, city="因特拉肯")
    assert row.geocoder == "nominatim" and row.region == "overseas"
    assert (row.lat, row.lng) == (INTERLAKEN["lat"], INTERLAKEN["lng"]), "脏坐标被正确值覆盖(自愈)"


def test_api_geocode_overseas_rejects_amap_row_even_with_overseas_region(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一致性闸二:行里 region 已是 overseas 但 geocoder=amap → 仍当未命中重解析。"""
    repo.upsert_origin_cache(session, city="皇后镇", name="河南省安阳市林州市皇后镇",
                             lat=ANYANG["lat"], lng=ANYANG["lng"], geocoder="amap",
                             region="overseas")
    session.commit()
    amap_calls = forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, error=DataSourceError("Nominatim", "直连超时"))
    stub_photon(monkeypatch, [PHOTON_NZ])
    detects = stub_detect(monkeypatch, UNKNOWN)

    payload = places_api.geocode_city(city="皇后镇", session=session)

    assert payload["geocoder"] == "photon" and payload["region"] == "overseas"
    assert payload["origin"]["lat"] == QUEENSTOWN["lat"], "不再是安阳林州"
    assert amap_calls == [] and detects == [], "缓存里已判出 region → 不再问判定源"
    row = repo.get_origin_cache(session, city="皇后镇")
    assert row.geocoder == "photon" and (row.lat, row.lng) == (QUEENSTOWN["lat"], QUEENSTOWN["lng"])


def test_api_geocode_region_mismatch_is_reresolved(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """一致性闸一:缓存行 region 与本次生效 region 不一致 → 重新解析(不端旧轨坐标)。"""
    repo.upsert_origin_cache(session, city="巴黎", name="巴黎, 高雄市, 臺灣",
                             lat=KAOHSIUNG["lat"], lng=KAOHSIUNG["lng"], geocoder="photon",
                             region="domestic")
    session.commit()
    forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, {**{"lat": 48.8535, "lng": 2.3484},
                                 "display_name": "巴黎, 法兰西岛, 法国", "country_code": "fr"})
    detects = stub_detect(monkeypatch, UNKNOWN)

    payload = places_api.geocode_city(city="巴黎", region="overseas", session=session)

    assert payload["region"] == "overseas" and detects == []
    assert payload["origin"]["lat"] == 48.8535, "重解析拿到真正的巴黎,不是高雄"
    row = repo.get_origin_cache(session, city="巴黎")
    assert row.region == "overseas" and row.geocoder == "nominatim"


def test_api_geocode_consistent_overseas_row_hits_cache_with_zero_network(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一致就零网络命中:地理编码各腿与判定源全都不许被调。"""
    repo.upsert_origin_cache(session, city="因特拉肯", name="因特拉肯, 瑞士",
                             lat=INTERLAKEN["lat"], lng=INTERLAKEN["lng"],
                             geocoder="nominatim", region="overseas")
    session.commit()

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("命中缓存不该再触网")

    for attribute in ("ds_geocode", "ds_photon_geocode", "ds_amap_geocode"):
        monkeypatch.setattr(place_loader, attribute, boom)
    monkeypatch.setattr(region_gate, "detect_region", boom)
    writes = spy_upsert(monkeypatch)

    payload = places_api.geocode_city(city="因特拉肯", region="overseas", session=session)

    assert payload["geocoder"] == "nominatim" and payload["region"] == "overseas"
    assert payload["origin"] == {"city": "因特拉肯", "name": "因特拉肯, 瑞士", **INTERLAKEN}
    assert writes == [], "命中路径不写库"

    again = places_api.geocode_city(city="因特拉肯", session=session)
    assert again == payload, "不带 override 也命中(缓存里的 region 就是判定值)"


def test_api_geocode_response_keys_are_unchanged_overseas(session, monkeypatch: pytest.MonkeyPatch) -> None:
    forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, NOMINATIM_CH)
    stub_photon(monkeypatch, [PHOTON_NZ])

    payload = places_api.geocode_city(city="因特拉肯", region="overseas", session=session)

    assert set(payload) == LEGACY_GEOCODE_KEYS, "响应键集逐键零变化"
    assert set(payload["origin"]) == ORIGIN_KEYS, "origin 仍是四字段"
    assert [band["key"] for band in payload["bands"]] == [
        "0_50", "50_100", "100_200", "200_300", "300_500"]
    assert payload["segments"] == []


@pytest.mark.parametrize("bad", ["oversea", "火星", "domestic|overseas"])
def test_api_geocode_invalid_region_is_still_400_in_chinese(
    session, monkeypatch: pytest.MonkeyPatch, bad: str,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("非法 region 应快速失败,不触网")

    for attribute in ("ds_geocode", "ds_photon_geocode", "ds_amap_geocode"):
        monkeypatch.setattr(place_loader, attribute, boom)
    monkeypatch.setattr(region_gate, "detect_region", boom)

    with pytest.raises(HTTPException) as ctx:
        places_api.geocode_city(city="因特拉肯", region=bad, session=session)

    assert ctx.value.status_code == 400, "不是 pydantic 的 422"
    assert str(ctx.value.detail) == places_api.REGION_INVALID_MSG == "region 只支持 domestic 或 overseas"


def test_api_geocode_overseas_double_failure_is_400_with_both_reasons(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    amap_calls = forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, error=DataSourceError("Nominatim", "直连超时"))
    stub_photon(monkeypatch, error=DataSourceError("Photon", "被限流(HTTP 429)"))
    writes = spy_upsert(monkeypatch)

    with pytest.raises(HTTPException) as ctx:
        places_api.geocode_city(city="因特拉肯", region="overseas", session=session)

    assert ctx.value.status_code == 400
    detail = str(ctx.value.detail)
    assert "无法解析城市 '因特拉肯'" in detail
    assert "境外地理编码两个源都失败" in detail
    assert "Nominatim=直连超时" in detail and "Photon=被限流" in detail
    assert amap_calls == [] and writes == [], "失败不写缓存,下次还能重试"


def test_api_geocode_overseas_override_writes_cache_for_next_hit(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """override 也写缓存(§1.2.4):同名城市二次查询零网络且坐标仍正确。"""
    amap_calls = forbid_amap_forward(monkeypatch)
    nominatim = stub_nominatim(monkeypatch, NOMINATIM_CH)
    stub_photon(monkeypatch, [PHOTON_NZ])

    first = places_api.geocode_city(city="因特拉肯", region="overseas", session=session)
    assert nominatim and amap_calls == []

    monkeypatch.setattr(place_loader, "ds_geocode",
                        lambda *a, **k: pytest.fail("二次查询应命中缓存,零网络"))
    second = places_api.geocode_city(city="因特拉肯", region="overseas", session=session)
    assert second == first, "命中与否响应逐字段一致"
    assert repo.get_origin_cache(session, city="因特拉肯").region == "overseas"


def test_api_geocode_detector_none_keeps_domestic_row_usable(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """判不出境内外(detector=none)时:region 留空串下次重试,但境内命中口径**不动**(零网络)。"""
    amap_calls = stub_amap(monkeypatch)
    stub_detect(monkeypatch, UNKNOWN)
    writes = spy_upsert(monkeypatch)

    first = places_api.geocode_city(city="上海", session=session)
    assert first["region"] == "domestic" and first["geocoder"] == "amap"
    assert [write["region"] for write in writes] == [None], "detector=none 不写 region"
    assert repo.get_origin_cache(session, city="上海").region == ""

    second = places_api.geocode_city(city="上海", session=session)
    assert amap_calls == ["上海"], "本次仍判不出 → 无从比对,沿用现状命中(不再打高德)"
    assert second == first and len(writes) == 1


# --------------------------------------------------------------------------- #
# 5. 自动判定路径:不带 region 参数、判出 overseas 也要走境外链
# --------------------------------------------------------------------------- #


def test_api_geocode_auto_detected_overseas_uses_overseas_chain(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不只 override 才生效:后端自动判出 overseas 时同样绕开高德(契约 §2.5)。"""
    amap_calls = forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, NOMINATIM_CH)
    stub_photon(monkeypatch, [PHOTON_NZ])
    detects = stub_detect(monkeypatch, OVERSEAS_CH)

    payload = places_api.geocode_city(city="因特拉肯", session=session)

    assert detects == ["因特拉肯"] and amap_calls == []
    assert payload["region"] == "overseas" and payload["geocoder"] == "nominatim"
    assert payload["origin"]["lat"] == INTERLAKEN["lat"]
    row = repo.get_origin_cache(session, city="因特拉肯")
    assert row.region == "overseas" and row.geocoder == "nominatim"


def test_api_geocode_auto_detected_domestic_keeps_amap(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """境内零回归:自动判出 domestic → 高德主链照旧、region 落库。"""
    amap_calls = stub_amap(monkeypatch)
    nominatim = stub_nominatim(monkeypatch, NOMINATIM_CN)
    stub_detect(monkeypatch, DOMESTIC_CN)

    payload = places_api.geocode_city(city="上海", session=session)

    assert payload["region"] == "domestic" and payload["geocoder"] == "amap"
    assert payload["origin"] == {"city": "上海", "name": "上海市", **SHANGHAI}
    assert amap_calls == ["上海"] and nominatim == [], "高德命中就不必问境外链"
    assert repo.get_origin_cache(session, city="上海").region == "domestic"


def test_api_geocode_auto_detection_is_skipped_when_cache_has_region(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判定顺序 override > 缓存行 > 现场判定:缓存里已判出就不再问判定源(零额外网络)。"""
    forbid_amap_forward(monkeypatch)
    nominatim = stub_nominatim(monkeypatch, NOMINATIM_CH)
    stub_photon(monkeypatch, [PHOTON_NZ])
    detects = stub_detect(monkeypatch, OVERSEAS_CH)

    places_api.geocode_city(city="皇后镇", session=session)
    assert detects == ["皇后镇"] and len(nominatim) == 1

    monkeypatch.setattr(region_gate, "detect_region",
                        lambda city: pytest.fail("缓存里已判出 region 就不该再判定"))
    second = places_api.geocode_city(city="皇后镇", session=session)
    assert second["region"] == "overseas" and second["geocoder"] == "nominatim"
    assert len(nominatim) == 1, "命中缓存,零网络"


# --------------------------------------------------------------------------- #
# 6. 逆向(「我的位置」):境外 GPS 不问高德
# --------------------------------------------------------------------------- #


def test_reverse_with_fallback_overseas_skips_amap(monkeypatch: pytest.MonkeyPatch) -> None:
    amap_calls = forbid_amap_reverse(monkeypatch)
    photon = stub_photon_reverse(monkeypatch, {**FURANO, "display_name": "富良野市, 北海道, 日本"})
    nominatim = stub_nominatim_reverse(monkeypatch, {**FURANO, "display_name": "富良野市, 北海道, 日本"})

    geo, geocoder = place_loader.reverse_with_fallback(
        FURANO["lat"], FURANO["lng"], region="overseas"
    )

    assert geocoder == place_loader.GEOCODER_PHOTON and amap_calls == []
    assert geo["display_name"] == "富良野市, 北海道, 日本"
    assert photon == [(FURANO["lat"], FURANO["lng"])] and nominatim == []


def test_reverse_with_fallback_overseas_degrades_to_nominatim(monkeypatch: pytest.MonkeyPatch) -> None:
    forbid_amap_reverse(monkeypatch)
    stub_photon_reverse(monkeypatch, error=DataSourceError("Photon", "被限流(HTTP 429)"))
    nominatim = stub_nominatim_reverse(
        monkeypatch, {**QUEENSTOWN, "display_name": "皇后镇, 奥塔哥, 新西兰"}
    )

    geo, geocoder = place_loader.reverse_with_fallback(-45.0322, 168.6610, zoom=12,
                                                      region=region_gate.REGION_OVERSEAS)

    assert geocoder == place_loader.GEOCODER_NOMINATIM
    assert geo["display_name"] == "皇后镇, 奥塔哥, 新西兰"
    assert nominatim == [{"lat": -45.0322, "lng": 168.6610, "zoom": 12}], "zoom 仍透传给 Nominatim"


def test_reverse_with_fallback_overseas_double_failure_names_both_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbid_amap_reverse(monkeypatch)
    stub_photon_reverse(monkeypatch, error=DataSourceError("Photon", "网络连接失败"))
    stub_nominatim_reverse(monkeypatch, error=DataSourceError("Nominatim", "服务繁忙"))

    with pytest.raises(DataSourceError) as ctx:
        place_loader.reverse_overseas(46.6855, 7.8585)

    assert ctx.value.source == place_loader.REVERSE_CHAIN_OVERSEAS == "Photon/Nominatim"
    assert "Photon=网络连接失败" in str(ctx.value) and "Nominatim=服务繁忙" in str(ctx.value)


def test_reverse_with_fallback_domestic_still_amap_first(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[float, float]] = []

    def fake_amap(lat: float, lng: float) -> dict[str, Any]:
        calls.append((lat, lng))
        return {"lat": lat, "lng": lng, "province": "上海市", "city": "",
                "district": "浦东新区", "township": "陆家嘴街道"}

    monkeypatch.setattr(place_loader, "ds_amap_reverse", fake_amap)
    monkeypatch.setattr(place_loader, "ds_photon_reverse",
                        lambda *a, **k: pytest.fail("高德命中就不该降级"))

    for region in (None, "domestic", ""):
        geo, geocoder = place_loader.reverse_with_fallback(31.2304, 121.4737, region=region)
        assert geocoder == place_loader.GEOCODER_AMAP, region
        assert "上海市" in geo["display_name"], region
    assert calls == [(31.2304, 121.4737)] * 3


def test_resolve_reverse_origin_threads_region(monkeypatch: pytest.MonkeyPatch) -> None:
    amap_calls = forbid_amap_reverse(monkeypatch)
    stub_photon_reverse(monkeypatch, {**FURANO, "display_name": "富良野市, 北海道, 日本"})

    origin, geocoder = place_loader.resolve_reverse_origin_with_source(
        FURANO["lat"], FURANO["lng"], region="overseas"
    )

    assert geocoder == place_loader.GEOCODER_PHOTON and amap_calls == []
    assert origin == {"city": "富良野市", "name": "富良野市, 北海道, 日本",
                      "lat": FURANO["lat"], "lng": FURANO["lng"], "resolved": True}
    assert origin["lat"] == FURANO["lat"], "范围圈仍以传入的 GPS 坐标为圆心"


def test_resolve_reverse_origin_injected_two_arg_stub_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入的 ``(lat, lng)`` 两参替身照旧(既有单测口径零改动)。"""
    seen: list[tuple[float, float]] = []

    def legacy_stub(lat: float, lng: float) -> dict[str, Any]:
        seen.append((lat, lng))
        return {"lat": lat, "lng": lng, "display_name": "浦东新区, 上海市, 中国"}

    origin, geocoder = place_loader.resolve_reverse_origin_with_source(
        31.2304, 121.4737, reverse_geocoder=legacy_stub, region="overseas"
    )
    assert seen == [(31.2304, 121.4737)] and origin["city"] == "上海市"
    assert geocoder == place_loader.GEOCODER_NOMINATIM


def test_default_reverse_geocoder_stub_without_region_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """``default_reverse_geocoder`` 的既有替身是 ``(lat, lng, zoom=10)`` 三参签名:不能报 TypeError。"""
    seen: list[tuple[float, float, int]] = []

    def legacy_default(lat: float, lng: float, zoom: int = 10) -> dict[str, Any]:
        seen.append((lat, lng, zoom))
        return {"lat": lat, "lng": lng, "display_name": "浦东新区, 上海市, 中国"}

    monkeypatch.setattr(place_loader, "default_reverse_geocoder", legacy_default)
    origin, _ = place_loader.resolve_reverse_origin_with_source(31.2304, 121.4737, zoom=12,
                                                               region="overseas")
    assert seen == [(31.2304, 121.4737, 12)] and origin["resolved"] is True


def test_api_reverse_geocode_overseas_coords_never_ask_amap(session, monkeypatch: pytest.MonkeyPatch) -> None:
    amap_calls = forbid_amap_reverse(monkeypatch)
    stub_photon_reverse(monkeypatch, {**FURANO, "display_name": "富良野市, 北海道, 日本"})
    coords = stub_detect_coords(monkeypatch, {"region": region_gate.REGION_OVERSEAS,
                                              "country_code": "JP",
                                              "detector": region_gate.DETECTOR_PHOTON})

    payload = places_api.reverse_geocode(lat=FURANO["lat"], lng=FURANO["lng"], zoom=None,
                                         session=session)

    assert set(payload) == LEGACY_REVERSE_KEYS, "响应键逐键零变化"
    assert payload["region"] == "overseas" and payload["geocoder"] == "photon"
    assert payload["resolved"] is True and payload["origin"]["city"] == "富良野市"
    assert (payload["origin"]["lat"], payload["origin"]["lng"]) == (FURANO["lat"], FURANO["lng"])
    assert amap_calls == [] and coords == [(FURANO["lat"], FURANO["lng"])], "判定只调一次"


def test_api_reverse_geocode_domestic_coords_still_use_amap(session, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[float, float]] = []

    def fake_amap(lat: float, lng: float) -> dict[str, Any]:
        calls.append((lat, lng))
        return {"lat": lat, "lng": lng, "province": "上海市", "city": "",
                "district": "浦东新区", "township": "陆家嘴街道"}

    monkeypatch.setattr(place_loader, "ds_amap_reverse", fake_amap)
    stub_detect_coords(monkeypatch, DOMESTIC_CN)

    payload = places_api.reverse_geocode(lat=31.2304, lng=121.4737, zoom=None, session=session)

    assert payload["region"] == "domestic" and payload["geocoder"] == "amap"
    assert payload["origin"]["city"] == "上海市" and calls == [(31.2304, 121.4737)]


def test_api_reverse_geocode_overseas_double_failure_is_still_200(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """境外两腿全败**不是** 400:降级成坐标起点,前端照常画环(TASK-1c 口径不变)。"""
    forbid_amap_reverse(monkeypatch)
    stub_photon_reverse(monkeypatch, error=DataSourceError("Photon", "网络连接失败"))
    stub_nominatim_reverse(monkeypatch, error=DataSourceError("Nominatim", "服务繁忙"))
    stub_detect_coords(monkeypatch, OVERSEAS_CH)

    payload = places_api.reverse_geocode(lat=46.6855, lng=7.8585, zoom=None, session=session)

    assert payload["resolved"] is False and payload["geocoder"] == "none"
    assert payload["region"] == "overseas"
    assert payload["origin"]["city"] == "我的位置(46.69,7.86)"
    assert payload["note"]


def test_api_reverse_geocode_detection_failure_falls_back_to_domestic_chain(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判不出境内外(两腿都被 no_network 兜住)→ 按境内走高德链,不抛、仍 200。"""
    calls: list[tuple[float, float]] = []

    def fake_amap(lat: float, lng: float) -> dict[str, Any]:
        calls.append((lat, lng))
        return {"lat": lat, "lng": lng, "province": "上海市", "city": "",
                "district": "浦东新区", "township": "陆家嘴街道"}

    monkeypatch.setattr(place_loader, "ds_amap_reverse", fake_amap)

    payload = places_api.reverse_geocode(lat=31.2304, lng=121.4737, zoom=None, session=session)

    assert payload["region"] == "domestic" and payload["geocoder"] == "amap"
    assert calls == [(31.2304, 121.4737)]


# --------------------------------------------------------------------------- #
# 7. 口径守卫:境外链常量与"高德不参与"的静态约束
# --------------------------------------------------------------------------- #


def test_overseas_chain_source_names_exclude_amap() -> None:
    assert place_loader.GEOCODER_CHAIN_OVERSEAS == "Nominatim/Photon"
    assert place_loader.REVERSE_CHAIN_OVERSEAS == "Photon/Nominatim"
    assert place_loader.GEOCODER_CHAIN_SOURCE == "amap/Photon/Nominatim", "境内链源名不动"


def test_geocode_overseas_source_has_no_amap_reference() -> None:
    """静态守卫:境外正向/逆向链的函数体里不许出现高德调用(防回退)。"""
    import inspect

    for target in (place_loader.geocode_overseas, place_loader.photon_geocode_overseas,
                   place_loader.reverse_overseas):
        source = inspect.getsource(target)
        assert "amap" not in source.lower(), f"{target.__name__} 不该碰高德"


# --------------------------------------------------------------------------- #
# 8. TASK-26:境外模式搜境内城市 → CN 证据自动降级回境内链(不再 400)
# --------------------------------------------------------------------------- #


def test_geocode_overseas_cn_evidence_raises_domestic_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nominatim 主腿判 CN + Photon 也失败 → 抛 DomesticMismatchError(不是普通 DataSourceError)。"""
    forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, NOMINATIM_CN)
    stub_photon(monkeypatch, error=DataSourceError("Photon", "请求超时(>15s)"))

    with pytest.raises(place_loader.DomesticMismatchError) as ctx:
        place_loader.geocode_overseas("上海")

    message = str(ctx.value)
    assert "境外地理编码两个源都失败" in message
    assert "Nominatim=" in message and "Photon=" in message, "两腿原因仍要报全"
    assert issubclass(place_loader.DomesticMismatchError, DataSourceError), "既有捕获口径零变化"


def test_geocode_overseas_photon_cn_alone_is_not_domestic_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Photon 腿判 CN **不算**境内证据(巴黎→高雄防线):双失败仍是普通 DataSourceError。"""
    forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, error=DataSourceError("Nominatim", "服务繁忙(HTTP 503)"))
    stub_photon(monkeypatch, [PHOTON_CN])

    with pytest.raises(DataSourceError) as ctx:
        place_loader.geocode_overseas("巴黎")

    assert not isinstance(ctx.value, place_loader.DomesticMismatchError), \
        "Photon 的 CN 只当错配,不许触发降级"


def test_api_geocode_overseas_mode_domestic_city_downgrades_to_amap(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """神朱 10/14 直报场景:region=overseas 搜「上海」→ 200 降级 amap/domestic + note。"""
    amap_calls = stub_amap(monkeypatch)
    stub_nominatim(monkeypatch, NOMINATIM_CN)
    stub_photon(monkeypatch, error=DataSourceError("Photon", "请求超时(>15s)"))
    writes = spy_upsert(monkeypatch)

    payload = places_api.geocode_city(city="上海", region="overseas", session=session)

    assert payload["geocoder"] == "amap" and amap_calls == ["上海"]
    assert payload["region"] == "domestic"
    assert (payload["origin"]["lat"], payload["origin"]["lng"]) == (
        SHANGHAI["lat"], SHANGHAI["lng"])
    assert "note" in payload and "境内" in payload["note"] and "上海" in payload["note"]
    assert writes and writes[-1]["region"] == "domestic" and writes[-1]["geocoder"] == "amap", \
        "降级结果写缓存(domestic),下次零网络"
    row = repo.get_origin_cache(session, city="上海")
    assert row.region == "domestic" and row.geocoder == "amap"


def test_api_geocode_downgraded_row_second_query_hits_cache_zero_network(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """降级行落库后,再带 region=overseas 搜同名城市 → 直通缓存,地理编码零网络。"""
    repo.upsert_origin_cache(session, city="上海", name="上海市",
                             lat=SHANGHAI["lat"], lng=SHANGHAI["lng"],
                             geocoder="amap", region="domestic")
    session.commit()

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("降级行命中缓存不该触网")

    for attribute in ("ds_geocode", "ds_photon_geocode", "ds_amap_geocode"):
        monkeypatch.setattr(place_loader, attribute, boom)
    monkeypatch.setattr(region_gate, "detect_region", boom)
    writes = spy_upsert(monkeypatch)

    payload = places_api.geocode_city(city="上海", region="overseas", session=session)

    assert payload["geocoder"] == "amap" and payload["region"] == "domestic"
    assert "note" in payload and writes == [], "命中路径不写库"

    plain = places_api.geocode_city(city="上海", session=session)
    assert plain["geocoder"] == "amap" and "note" not in plain, \
        "不带 override 的常规响应没有 note 键(LEGACY 键集零变化)"


def test_api_geocode_overseas_city_unchanged_no_note_key(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """境外城市(因特拉肯)行为零变化:响应不含 note 键,键集仍是 LEGACY 五键。"""
    forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, NOMINATIM_CH)
    stub_photon(monkeypatch, [PHOTON_NZ])

    payload = places_api.geocode_city(city="因特拉肯", region="overseas", session=session)

    assert set(payload) == LEGACY_GEOCODE_KEYS, "常规境外响应键集逐键零变化"
    assert payload["region"] == "overseas" and payload["geocoder"] == "nominatim"


def test_api_geocode_downgrade_when_amap_also_fails_reports_both_chains(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CN 证据触发降级但境内链也失败(高德没 key/断网)→ 400,消息带两条链的原因。"""
    monkeypatch.setattr(place_loader, "ds_amap_geocode",
                        lambda city: (_ for _ in ()).throw(
                            DataSourceError("amap", "未配置高德 key")))
    # Nominatim 只对**境外腿**(include_country=True)答 CN 造出降级证据;
    # 境内降级链再问它时失败 —— 否则末腿成功就轮不到「境内链也失败」这条 400。
    def nominatim_cn_then_dead(query: str, *, limit: int = 1,
                               include_country: bool = False, **kwargs: Any) -> Any:
        if include_country:
            return dict(NOMINATIM_CN)
        raise DataSourceError("Nominatim", "直连超时")

    monkeypatch.setattr(place_loader, "ds_geocode", nominatim_cn_then_dead)
    stub_photon(monkeypatch, error=DataSourceError("Photon", "请求超时(>15s)"))
    writes = spy_upsert(monkeypatch)

    with pytest.raises(HTTPException) as ctx:
        places_api.geocode_city(city="上海", region="overseas", session=session)

    assert ctx.value.status_code == 400
    detail = str(ctx.value.detail)
    assert "无法解析城市 '上海'" in detail
    assert "境内降级链也失败" in detail and "未配置高德 key" in detail
    assert writes == [], "失败不写缓存"


def test_api_geocode_double_network_failure_without_cn_evidence_still_400(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两腿都是网络失败(无 CN 证据)→ 维持 TASK-25 的 400 口径,不降级。"""
    amap_calls = forbid_amap_forward(monkeypatch)
    stub_nominatim(monkeypatch, error=DataSourceError("Nominatim", "直连超时"))
    stub_photon(monkeypatch, error=DataSourceError("Photon", "被限流(HTTP 429)"))

    with pytest.raises(HTTPException) as ctx:
        places_api.geocode_city(city="因特拉肯", region="overseas", session=session)

    assert ctx.value.status_code == 400 and amap_calls == []
    assert "境外地理编码两个源都失败" in str(ctx.value.detail)


def test_api_geocode_downgrade_only_for_overseas_override_not_auto_detect(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自动判定路径判出 domestic 时本来就走境内链(现状),不会误挂 note。"""
    amap_calls = stub_amap(monkeypatch)
    stub_detect(monkeypatch, DOMESTIC_CN)

    payload = places_api.geocode_city(city="上海", session=session)

    assert payload["geocoder"] == "amap" and payload["region"] == "domestic"
    assert "note" not in payload and amap_calls == ["上海"]
