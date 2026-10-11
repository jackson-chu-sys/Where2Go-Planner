"""TASK-20 单测:境内外判定(``services/region_gate``)+ 两个地理编码源的 opt-in 国家码 +
``/api/geocode`` 的 ``region`` 键与前端「境外模式」开关的后端半边。

全程不触网:``no_network``(autouse)把 :meth:`requests.Session.request` 换成抛错,
判定链的两条腿用 ``monkeypatch`` 替换 :mod:`services.region_gate` 的模块属性
(``ds_nominatim_geocode`` / ``ds_photon_geocode`` / ``ds_nominatim_reverse`` /
``ds_photon_reverse``,与 test_photon.py 替换 ``place_loader.ds_*`` 同一套路);
两个数据源的 ``include_country`` 扩展用 :class:`FakeSession` 替身断言**请求参数与返回形状**;
DB 用 ``tmp_path`` 下的临时 SQLite。

覆盖六件事(契约 §2):
1. :func:`region_gate.classify_country_code` 的三档(CN / 其他非空 / 空与垃圾);
2. **正向判定链 Nominatim 主 → Photon 兜底**(Photon 正向会把中文境外地名错定到国内),
   一腿空国家码 = 该腿失败,双失败 ``domestic``/``none`` 且**绝不抛**;
3. **逆向判定链 Photon 主 → Nominatim 兜底**;
4. ``include_country`` 是 opt-in:默认输出与既有断言**逐键一致**(只有 lat/lng/display_name),
   Nominatim 只在 opt-in 时才带 ``addressdetails=1``;
5. ``OriginCache.region`` 的落库/补列与 ``upsert_origin_cache(region=None)`` 不覆盖现值;
6. API 层:响应只多 ``region`` 一个键、override 生效且不调判定源、非法值 400 中文、
   缓存命中零判定、``detector=none`` 不写缓存(下次重试)。

运行:``cd backend && ../.venv/bin/python -m pytest -q``
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import requests
from fastapi import HTTPException
from sqlalchemy import text

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import places as places_api  # noqa: E402
from data_sources import nominatim, photon  # noqa: E402
from db import init_db, make_engine, repository as repo, session_factory  # noqa: E402
from db.models import REGION_LEN  # noqa: E402
from services import place_loader, region_gate  # noqa: E402

INTERLAKEN = {"lat": 46.6863, "lng": 7.8632}
SHANGHAI = {"lat": 31.2304, "lng": 121.4737}

# 既有 /api/geocode(+/reverse)的顶层键:TASK-20 只允许**多**一个 region,其余逐键零变化
LEGACY_GEOCODE_KEYS = {"origin", "geocoder", "bands", "segments"}
LEGACY_REVERSE_KEYS = {"origin", "resolved", "geocoder", "bands", "segments", "note"}


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


class FakeResponse:
    """最小 response 替身:``http_json`` 只看 ``status_code`` 与 ``text``。"""

    def __init__(self, payload: Any = None, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = "" if payload is None else json.dumps(payload, ensure_ascii=False)


class FakeSession:
    """记录调用参数并按顺序回放预设响应(不触网);响应不够时重复最后一条。"""

    def __init__(self, *responses: Any) -> None:
        self.responses: list[Any] = list(responses) or [FakeResponse({})]
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, params: Any = None, data: Any = None,
                timeout: Any = None, headers: Any = None, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, "params": params,
                           "timeout": timeout, "headers": headers})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


PHOTON_CH = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [7.8632, 46.6863]},
        "properties": {"osm_type": "R", "osm_id": 1682395, "name": "Interlaken",
                       "country": "Schweiz", "countrycode": "CH"},
    }],
}
PHOTON_NO_COUNTRY = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [7.8632, 46.6863]},
        "properties": {"name": "Interlaken"},
    }],
}
NOMINATIM_CH = [{"lat": "46.6863", "lon": "7.8632", "display_name": "因特拉肯, 瑞士",
                 "address": {"country_code": "ch", "country": "瑞士"}}]
NOMINATIM_REVERSE_NZ = {"lat": "-45.0312", "lon": "168.6626", "display_name": "皇后镇, 新西兰",
                        "address": {"country_code": "nz", "country": "新西兰"}}


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
    engine = make_engine(f"sqlite:///{tmp_path / 'region_gate_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


def stub_origin(monkeypatch: pytest.MonkeyPatch, *, geocoder: str = place_loader.GEOCODER_PHOTON) -> None:
    """替换起点解析降级链:API 用例只关心 region,不该真去问地理编码源。"""
    monkeypatch.setattr(
        place_loader, "resolve_origin_with_source",
        lambda city: ({"city": city, "name": f"{city}, 中国", **SHANGHAI}, geocoder),
    )


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


OVERSEAS_CH = {"region": region_gate.REGION_OVERSEAS, "country_code": "CH",
               "detector": region_gate.DETECTOR_NOMINATIM}
UNKNOWN = {"region": region_gate.REGION_DOMESTIC, "country_code": None,
           "detector": region_gate.DETECTOR_NONE}


# --------------------------------------------------------------------------- #
# 1. classify_country_code:CN → 境内,其他非空 → 境外,判不出 → 境内
# --------------------------------------------------------------------------- #


def test_classify_country_code_cn_is_domestic_case_insensitive() -> None:
    for code in ("CN", "cn", "Cn", "  cn  "):
        assert region_gate.classify_country_code(code) == region_gate.REGION_DOMESTIC, code


def test_classify_country_code_foreign_codes_are_overseas() -> None:
    for code in ("CH", "ch", "NZ", "fr", "JP", "xx"):
        assert region_gate.classify_country_code(code) == region_gate.REGION_OVERSEAS, code


def test_classify_country_code_blank_or_garbage_is_domestic() -> None:
    """判不出国家码一律按境内:空串 / None / 非字符串都不猜、不上抛。"""
    for code in ("", "   ", None, 7, 0.0, {}, [], object()):
        assert region_gate.classify_country_code(code) == region_gate.REGION_DOMESTIC, repr(code)


# --------------------------------------------------------------------------- #
# 2. detect_region:Nominatim 主 → Photon 兜底 → 双失败 domestic/none
# --------------------------------------------------------------------------- #


def test_detect_region_prefers_nominatim_and_uppercases_code(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_nominatim(query: str, *, limit: int = 1, include_country: bool = False) -> dict[str, Any]:
        seen.append({"query": query, "include_country": include_country})
        return {**INTERLAKEN, "display_name": "因特拉肯, 瑞士",
                "country_code": "ch", "country": "瑞士"}

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Nominatim 答上了就不该再问 Photon")

    monkeypatch.setattr(region_gate, "ds_nominatim_geocode", fake_nominatim)
    monkeypatch.setattr(region_gate, "ds_photon_geocode", boom)
    assert region_gate.detect_region("因特拉肯") == {
        "region": region_gate.REGION_OVERSEAS, "country_code": "CH",
        "detector": region_gate.DETECTOR_NOMINATIM,
    }
    assert seen == [{"query": "因特拉肯", "include_country": True}], "必须 opt-in 国家码"


def test_detect_region_nominatim_cn_is_domestic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(region_gate, "ds_nominatim_geocode",
                        lambda query, *, limit=1, include_country=False: {
                            **SHANGHAI, "display_name": "上海市, 中国", "country_code": "cn"})
    monkeypatch.setattr(region_gate, "ds_photon_geocode",
                        lambda *a, **k: pytest.fail("境内也不该再问 Photon"))
    assert region_gate.detect_region("上海") == {
        "region": region_gate.REGION_DOMESTIC, "country_code": "CN",
        "detector": region_gate.DETECTOR_NOMINATIM,
    }


def test_detect_region_falls_back_to_photon_when_nominatim_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def nominatim_boom(query: str, *, limit: int = 1, include_country: bool = False) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    def fake_photon(query: str, *, limit: int = 5, include_country: bool = False) -> list[dict[str, Any]]:
        seen.append({"query": query, "limit": limit, "include_country": include_country})
        return [{**INTERLAKEN, "display_name": "Interlaken, Schweiz",
                 "country": "Schweiz", "countrycode": "CH"}]

    monkeypatch.setattr(region_gate, "ds_nominatim_geocode", nominatim_boom)
    monkeypatch.setattr(region_gate, "ds_photon_geocode", fake_photon)
    assert region_gate.detect_region("因特拉肯") == {
        "region": region_gate.REGION_OVERSEAS, "country_code": "CH",
        "detector": region_gate.DETECTOR_PHOTON,
    }
    assert seen == [{"query": "因特拉肯", "limit": region_gate.PHOTON_LIMIT, "include_country": True}]


def test_detect_region_blank_nominatim_code_falls_through_to_photon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一腿答不出国家码 = 该腿失败(空串**不当**境内),降到 Photon。"""
    monkeypatch.setattr(region_gate, "ds_nominatim_geocode",
                        lambda query, *, limit=1, include_country=False: {
                            **INTERLAKEN, "display_name": "Interlaken", "country_code": "", "country": ""})
    monkeypatch.setattr(region_gate, "ds_photon_geocode",
                        lambda query, *, limit=5, include_country=False: [
                            {**INTERLAKEN, "display_name": "Interlaken", "countrycode": "ch"}])
    assert region_gate.detect_region("Interlaken") == {
        "region": region_gate.REGION_OVERSEAS, "country_code": "CH",
        "detector": region_gate.DETECTOR_PHOTON,
    }


def test_detect_region_double_failure_is_domestic_none_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("两条腿都挂了(断网 / 限流 / Nominatim 直连时好时坏)")

    monkeypatch.setattr(region_gate, "ds_nominatim_geocode", boom)
    monkeypatch.setattr(region_gate, "ds_photon_geocode", boom)
    assert region_gate.detect_region("因特拉肯") == UNKNOWN, "双失败按境内 + detector=none,绝不抛"


def test_detect_region_blank_city_skips_both_legs() -> None:
    """空城市名连腿都不打:直接判不出(调用方本来就会先 400)。"""
    for city in ("", "   ", None):
        assert region_gate.detect_region(city) == UNKNOWN, repr(city)


def test_detect_region_photon_empty_or_malformed_result_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(region_gate, "ds_nominatim_geocode",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("直连超时")))
    monkeypatch.setattr(region_gate, "ds_photon_geocode", lambda *a, **k: [])
    assert region_gate.detect_region("因特拉肯") == UNKNOWN
    # 兜底腿返回的不是列表 / 不是 dict 也只当失败,不抛
    monkeypatch.setattr(region_gate, "ds_photon_geocode", lambda *a, **k: "垃圾")
    assert region_gate.detect_region("因特拉肯") == UNKNOWN


# --------------------------------------------------------------------------- #
# 3. detect_region_coords:Photon reverse 主 → Nominatim reverse 兜底
# --------------------------------------------------------------------------- #


def test_detect_region_coords_prefers_photon_reverse(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_photon(lat: float, lng: float, *, include_country: bool = False) -> dict[str, Any]:
        seen.append({"lat": lat, "lng": lng, "include_country": include_country})
        return {**INTERLAKEN, "display_name": "Interlaken, Schweiz", "countrycode": "CH"}

    monkeypatch.setattr(region_gate, "ds_photon_reverse", fake_photon)
    monkeypatch.setattr(region_gate, "ds_nominatim_reverse",
                        lambda *a, **k: pytest.fail("Photon 答上了就不该再问 Nominatim"))
    assert region_gate.detect_region_coords(46.6863, 7.8632) == {
        "region": region_gate.REGION_OVERSEAS, "country_code": "CH",
        "detector": region_gate.DETECTOR_PHOTON,
    }
    assert seen == [{"lat": 46.6863, "lng": 7.8632, "include_country": True}]


def test_detect_region_coords_falls_back_to_nominatim_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def photon_boom(lat: float, lng: float, *, include_country: bool = False) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    def fake_nominatim(lat: float, lng: float, *, zoom: int = 10,
                       include_country: bool = False) -> dict[str, Any]:
        seen.append({"lat": lat, "lng": lng, "zoom": zoom, "include_country": include_country})
        return {"lat": -45.0312, "lng": 168.6626, "display_name": "皇后镇, 新西兰",
                "country_code": "nz", "country": "新西兰"}

    monkeypatch.setattr(region_gate, "ds_photon_reverse", photon_boom)
    monkeypatch.setattr(region_gate, "ds_nominatim_reverse", fake_nominatim)
    assert region_gate.detect_region_coords(-45.0312, 168.6626) == {
        "region": region_gate.REGION_OVERSEAS, "country_code": "NZ",
        "detector": region_gate.DETECTOR_NOMINATIM,
    }
    assert seen == [{"lat": -45.0312, "lng": 168.6626,
                     "zoom": region_gate.REVERSE_ZOOM, "include_country": True}]


def test_detect_region_coords_double_failure_and_bad_coords_are_domestic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("两条腿都挂了")

    monkeypatch.setattr(region_gate, "ds_photon_reverse", boom)
    monkeypatch.setattr(region_gate, "ds_nominatim_reverse", boom)
    assert region_gate.detect_region_coords(31.2304, 121.4737) == UNKNOWN
    # 坐标不是数字:连腿都不打,直接判不出(绝不抛)
    assert region_gate.detect_region_coords("x", "y") == UNKNOWN
    assert region_gate.detect_region_coords(None, None) == UNKNOWN


def test_detect_region_coords_without_any_stub_stays_domestic() -> None:
    """不注入替身(两条腿都被 ``no_network`` 兜住)也必须 domestic/none,不抛到调用方。"""
    assert region_gate.detect_region_coords(31.2304, 121.4737) == UNKNOWN
    assert region_gate.detect_region("上海") == UNKNOWN


# --------------------------------------------------------------------------- #
# 4. include_country 是 opt-in:默认形状逐键与既有断言一致
# --------------------------------------------------------------------------- #


def test_photon_parse_feature_default_shape_is_unchanged() -> None:
    feature = PHOTON_CH["features"][0]
    assert photon.parse_feature(feature) == {
        "lat": 46.6863, "lng": 7.8632, "display_name": "Interlaken, Schweiz",
    }, "默认三键,一个不多(既有单测断言的就是这个形状)"
    assert photon.parse_features(PHOTON_CH) == [photon.parse_feature(feature)]


def test_photon_include_country_appends_two_keys() -> None:
    feature = PHOTON_CH["features"][0]
    parsed = photon.parse_feature(feature, include_country=True)
    assert parsed == {"lat": 46.6863, "lng": 7.8632, "display_name": "Interlaken, Schweiz",
                      "country": "Schweiz", "countrycode": "CH"}
    assert photon.parse_features(PHOTON_CH, include_country=True) == [parsed]
    # 缺国家码 → 空串(不是 None、不抛);countrycode 一律大写
    missing = photon.parse_feature(PHOTON_NO_COUNTRY["features"][0], include_country=True)
    assert missing["country"] == "" and missing["countrycode"] == ""


def test_photon_client_passes_include_country_through() -> None:
    fake = FakeSession(FakeResponse(PHOTON_CH))
    client = photon.PhotonClient(session=fake, min_interval=0)
    assert set(client.geocode("因特拉肯")[0]) == {"lat", "lng", "display_name"}
    assert client.geocode("因特拉肯", include_country=True)[0]["countrycode"] == "CH"
    assert fake.calls[0]["params"] == {"q": "因特拉肯", "limit": photon.DEFAULT_LIMIT}
    assert fake.calls[1]["params"] == fake.calls[0]["params"], "Photon 参数口径不受 opt-in 影响"

    reverse_fake = FakeSession(FakeResponse(PHOTON_CH))
    reverse_client = photon.PhotonClient(session=reverse_fake, min_interval=0)
    assert set(reverse_client.reverse(46.6863, 7.8632)) == {"lat", "lng", "display_name"}
    assert reverse_client.reverse(46.6863, 7.8632, include_country=True)["country"] == "Schweiz"
    assert reverse_fake.calls[0]["params"] == reverse_fake.calls[1]["params"] == {
        "lat": "46.686300", "lon": "7.863200"}


def test_nominatim_parse_place_default_shape_is_unchanged() -> None:
    assert nominatim.parse_place(NOMINATIM_CH[0]) == {
        "lat": 46.6863, "lng": 7.8632, "display_name": "因特拉肯, 瑞士",
    }, "默认三键,一个不多"
    with_country = nominatim.parse_place(NOMINATIM_CH[0], include_country=True)
    assert with_country == {"lat": 46.6863, "lng": 7.8632, "display_name": "因特拉肯, 瑞士",
                            "country_code": "CH", "country": "瑞士"}, "小写 ch 要归一成大写"
    # 没有 address 段(忘了带 addressdetails=1)→ 两个空串,不抛
    bare = nominatim.parse_place({"lat": "1", "lon": "2", "display_name": "x"}, include_country=True)
    assert bare["country_code"] == "" and bare["country"] == ""


def test_nominatim_client_only_sends_addressdetails_when_opted_in() -> None:
    fake = FakeSession(FakeResponse(NOMINATIM_CH))
    client = nominatim.NominatimClient(session=fake, min_interval=0)
    assert set(client.geocode("因特拉肯")) == {"lat", "lng", "display_name"}
    assert fake.calls[0]["params"]["addressdetails"] == 0, "默认口径仍是 addressdetails=0"
    assert client.geocode("因特拉肯", include_country=True)["country_code"] == "CH"
    assert fake.calls[1]["params"]["addressdetails"] == 1, "opt-in 才要 address 段"

    reverse_fake = FakeSession(FakeResponse(NOMINATIM_REVERSE_NZ))
    reverse_client = nominatim.NominatimClient(session=reverse_fake, min_interval=0)
    assert set(reverse_client.reverse(-45.0312, 168.6626)) == {"lat", "lng", "display_name"}
    assert reverse_fake.calls[0]["params"]["addressdetails"] == 0
    assert reverse_client.reverse(-45.0312, 168.6626, include_country=True)["country_code"] == "NZ"
    assert reverse_fake.calls[1]["params"]["addressdetails"] == 1
    assert reverse_fake.calls[1]["params"]["zoom"] == 10, "zoom 口径不变"


# --------------------------------------------------------------------------- #
# 5. OriginCache.region:落库、None 不覆盖、旧库补列
# --------------------------------------------------------------------------- #


def test_upsert_origin_cache_region_defaults_and_none_keeps_value(session) -> None:
    row = repo.upsert_origin_cache(session, city="上海", name="上海市, 中国",
                                   lat=SHANGHAI["lat"], lng=SHANGHAI["lng"], geocoder="photon")
    assert row.region == "", "不传 region = 空串(还没判出),既有调用零改动兼容"

    repo.upsert_origin_cache(session, city="上海", name="上海市, 中国", lat=SHANGHAI["lat"],
                             lng=SHANGHAI["lng"], geocoder="photon", region="overseas")
    assert repo.get_origin_cache(session, city="上海").region == "overseas"

    repo.upsert_origin_cache(session, city="上海", name="上海市, 中国", lat=SHANGHAI["lat"],
                             lng=SHANGHAI["lng"], geocoder="photon", region=None)
    assert repo.get_origin_cache(session, city="上海").region == "overseas", "None 不覆盖现值"

    repo.upsert_origin_cache(session, city="上海", name="上海市, 中国", lat=SHANGHAI["lat"],
                             lng=SHANGHAI["lng"], geocoder="photon", region="domestic")
    assert repo.get_origin_cache(session, city="上海").region == "domestic"

    long = repo.upsert_origin_cache(session, city="上海", name="上海市, 中国", lat=SHANGHAI["lat"],
                                    lng=SHANGHAI["lng"], geocoder="photon", region="x" * 40)
    assert len(long.region) == REGION_LEN, "按列宽截断,不撞 String(16)"
    junk = repo.upsert_origin_cache(session, city="上海", name="上海市, 中国", lat=SHANGHAI["lat"],
                                    lng=SHANGHAI["lng"], geocoder="photon", region=7)
    assert junk.region == "", "非字符串按空串(还没判出)入库"


def test_origin_cache_region_column_is_backfilled_on_legacy_sqlite(tmp_path) -> None:
    """旧库(没有 region 列)靠 ``db.base.COLUMN_MIGRATIONS`` 的 ALTER TABLE 补列。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE origin_cache (city VARCHAR(120) NOT NULL, name VARCHAR(255) NOT NULL,"
            " lat FLOAT NOT NULL, lng FLOAT NOT NULL, geocoder VARCHAR(16) NOT NULL,"
            " updated_at DATETIME NOT NULL, PRIMARY KEY (city))"
        ))
        connection.execute(text(
            "INSERT INTO origin_cache VALUES"
            " ('上海','上海市, 中国',31.2304,121.4737,'photon','2026-10-01 00:00:00')"
        ))
    def columns() -> set[str]:
        with engine.connect() as connection:
            return {str(row[1]) for row in connection.execute(text("PRAGMA table_info(origin_cache)"))}

    assert "region" not in columns(), "旧库确实没有这一列(才需要 ALTER TABLE 补)"

    init_db(engine)   # create_all(只建缺失的表)+ ensure_columns(补 region 列)
    assert "region" in columns(), "补列后旧库也有 region 了"
    current = session_factory(engine)()
    try:
        row = repo.get_origin_cache(current, city="上海")
        assert row is not None, "补列不该丢数据"
        assert row.region == "", "旧行补列后 = 空串(还没判出)"
        assert (row.lat, row.lng, row.geocoder) == (31.2304, 121.4737, "photon")
        repo.upsert_origin_cache(current, city="上海", name=row.name, lat=row.lat,
                                 lng=row.lng, geocoder=row.geocoder, region="overseas")
        current.commit()
        assert repo.get_origin_cache(current, city="上海").region == "overseas"
    finally:
        current.close()
        engine.dispose()


def test_region_gate_stays_off_db_and_place_loader() -> None:
    """契约 §1.3:不落库不碰 session、不 import place_loader(避免循环依赖)。"""
    source = (Path(BACKEND_DIR) / "services" / "region_gate.py").read_text(encoding="utf-8")
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert imports, "本用例依赖 import 行都在行首"
    assert not any("place_loader" in line for line in imports), "不 import place_loader"
    assert not any(re.search(r"\b(db|sqlalchemy)\b", line) for line in imports), "不碰 DB/session"
    for name in ("REGION_DOMESTIC", "REGION_OVERSEAS", "DETECTOR_NOMINATIM",
                 "DETECTOR_PHOTON", "DETECTOR_NONE"):
        assert hasattr(region_gate, name), f"契约 §1.3 的常量少了 {name}"


# --------------------------------------------------------------------------- #
# 6. API 层:/api/geocode 的 region 键、override、400 中文、缓存口径
# --------------------------------------------------------------------------- #


def test_api_geocode_adds_only_the_region_key(session, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_origin(monkeypatch)
    calls = stub_detect(monkeypatch, OVERSEAS_CH)
    # 不传 region:单测直调时默认值是 FieldInfo,应归一成"没给" → 自动判定,不报 400
    payload = places_api.geocode_city(city="因特拉肯", session=session)
    assert LEGACY_GEOCODE_KEYS <= set(payload), "既有键一个不少"
    assert set(payload) == LEGACY_GEOCODE_KEYS | {"region"}, "只多 region 一个键"
    assert payload["region"] == "overseas" and calls == ["因特拉肯"]
    assert payload["origin"] == {"city": "因特拉肯", "name": "因特拉肯, 中国", **SHANGHAI}
    row = repo.get_origin_cache(session, city="因特拉肯")
    assert row is not None and row.region == "overseas", "判定结果随缓存一起落库"


def test_api_geocode_override_skips_detector_and_cache_write(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_origin(monkeypatch)
    calls = stub_detect(monkeypatch, OVERSEAS_CH)
    payload = places_api.geocode_city(city="上海", region="overseas", session=session)
    assert payload["region"] == "overseas" and calls == [], "override 生效且不调判定源"
    row = repo.get_origin_cache(session, city="上海")
    assert row is not None and row.region == "", "override 是每次请求的临时口径,不写进 7 天缓存"

    domestic = places_api.geocode_city(city="上海", region="domestic", session=session)
    assert domestic["region"] == "domestic" and calls == []
    assert repo.get_origin_cache(session, city="上海").region == "", "另一档 override 同样不写库"


def test_api_geocode_region_override_is_case_insensitive_and_blank_is_auto(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_origin(monkeypatch)
    calls = stub_detect(monkeypatch, OVERSEAS_CH)
    assert places_api.geocode_city(city="上海", region=" OVERSEAS ", session=session)["region"] == "overseas"
    assert calls == [], "给了合法 override 就不判定"
    assert places_api.geocode_city(city="上海", region="  ", session=session)["region"] == "overseas"
    assert calls == ["上海"], "空串 = 没给 → 走自动判定"


def test_api_geocode_invalid_region_is_400_in_chinese(session, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_origin(monkeypatch)
    stub_detect(monkeypatch, OVERSEAS_CH)
    for bad in ("火星", "oversea", "1", "domestic|overseas"):
        with pytest.raises(HTTPException) as ctx:
            places_api.geocode_city(city="上海", region=bad, session=session)
        assert ctx.value.status_code == 400, f"{bad} 应是 400 而不是 pydantic 的 422"
        assert places_api.REGION_INVALID_MSG in str(ctx.value.detail)
        assert str(ctx.value.detail) == "region 只支持 domestic 或 overseas"


def test_api_geocode_cache_hit_with_region_never_detects_again(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_origin(monkeypatch)
    calls = stub_detect(monkeypatch, OVERSEAS_CH)
    first = places_api.geocode_city(city="因特拉肯", session=session)
    assert calls == ["因特拉肯"] and first["region"] == "overseas"

    second = places_api.geocode_city(city="因特拉肯", session=session)
    assert calls == ["因特拉肯"], "缓存里已判出 region → 命中路径零额外网络(不许再调 detect_region)"
    assert second == first, "命中与否响应逐字段一致"
    assert second["region"] == "overseas"


def test_api_geocode_detector_none_is_domestic_and_not_cached(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_origin(monkeypatch)
    calls = stub_detect(monkeypatch, UNKNOWN)
    first = places_api.geocode_city(city="上海", session=session)
    assert first["region"] == "domestic" and calls == ["上海"]
    row = repo.get_origin_cache(session, city="上海")
    assert row is not None, "地理编码本身成功了,坐标照旧落缓存"
    assert row.region == "", "detector=none 不写 region:留空串,下次还能重试"

    second = places_api.geocode_city(city="上海", session=session)
    assert calls == ["上海", "上海"], "命中缓存但没判出过 → 再判一次"
    assert second == first, "口径不变(仍按境内回报)"


def test_api_geocode_backfills_region_on_legacy_cache_row(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """补列前的旧行(region="")命中时补判一次并**写回**,之后就是零额外网络。"""
    repo.upsert_origin_cache(session, city="皇后镇", name="皇后镇, 新西兰",
                             lat=-45.0312, lng=168.6626, geocoder="photon")
    session.commit()
    assert repo.get_origin_cache(session, city="皇后镇").region == ""
    monkeypatch.setattr(place_loader, "resolve_origin_with_source",
                        lambda city: pytest.fail("命中缓存不该再问地理编码源"))
    calls = stub_detect(monkeypatch, {"region": region_gate.REGION_OVERSEAS, "country_code": "NZ",
                                      "detector": region_gate.DETECTOR_PHOTON})

    first = places_api.geocode_city(city="皇后镇", session=session)
    assert first["region"] == "overseas" and calls == ["皇后镇"]
    assert first["origin"] == {"city": "皇后镇", "name": "皇后镇, 新西兰",
                               "lat": -45.0312, "lng": 168.6626}
    row = repo.get_origin_cache(session, city="皇后镇")
    assert row.region == "overseas" and row.geocoder == "photon", "补判只写 region,其余照原值"

    second = places_api.geocode_city(city="皇后镇", session=session)
    assert calls == ["皇后镇"], "写回之后就零判定"
    assert second == first


def test_api_geocode_override_wins_over_cached_region(session, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_origin(monkeypatch)
    stub_detect(monkeypatch, OVERSEAS_CH)
    places_api.geocode_city(city="上海", session=session)
    assert repo.get_origin_cache(session, city="上海").region == "overseas"

    calls = stub_detect(monkeypatch, OVERSEAS_CH)
    payload = places_api.geocode_city(city="上海", region="domestic", session=session)
    assert payload["region"] == "domestic" and calls == [], "override 优先于缓存里的判定"
    assert repo.get_origin_cache(session, city="上海").region == "overseas", "且不覆盖缓存"


def test_api_reverse_geocode_reports_region(session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        place_loader, "resolve_reverse_origin_with_source",
        lambda lat, lng, zoom=None: ({"city": "因特拉肯", "name": "因特拉肯", "lat": lat,
                                      "lng": lng, "resolved": True}, place_loader.GEOCODER_PHOTON),
    )
    coords = stub_detect_coords(monkeypatch, {"region": region_gate.REGION_OVERSEAS,
                                              "country_code": "CH",
                                              "detector": region_gate.DETECTOR_PHOTON})
    payload = places_api.reverse_geocode(lat=46.6863, lng=7.8632, zoom=None, session=session)
    assert LEGACY_REVERSE_KEYS <= set(payload), "既有键一个不少"
    assert set(payload) == LEGACY_REVERSE_KEYS | {"region"}, "只多 region 一个键"
    assert payload["region"] == "overseas" and coords == [(46.6863, 7.8632)]


def test_api_reverse_geocode_region_never_breaks_the_200_contract(
    session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判定失败(两条腿都被 no_network 兜住)也仍是 200 + ``region=domestic``,不连累定位。"""
    monkeypatch.setattr(
        place_loader, "resolve_reverse_origin_with_source",
        lambda lat, lng, zoom=None: ({"city": "我的位置(31.23,121.47)", "name": "我的位置(31.23,121.47)",
                                      "lat": lat, "lng": lng,
                                      "resolved": False}, place_loader.GEOCODER_NONE),
    )
    payload = places_api.reverse_geocode(lat=31.2304, lng=121.4737, zoom=None, session=session)
    assert payload["resolved"] is False and payload["geocoder"] == "none"
    assert payload["region"] == "domestic"


def test_geocode_route_exposes_region_param_without_pattern() -> None:
    """``region`` 走函数体校验(400 中文),所以 OpenAPI 上**不该**出现 pattern/enum 的 422 口径。"""
    from app.main import app

    params = {item["name"]: item for item in app.openapi()["paths"]["/api/geocode"]["get"]["parameters"]}
    assert "region" in params and params["region"]["required"] is False
    schema = params["region"]["schema"]
    assert "pattern" not in schema, "非法值要 400 中文,不能被 pydantic 拦成 422"
    reverse = {item["name"] for item in
               app.openapi()["paths"]["/api/geocode/reverse"]["get"]["parameters"]}
    assert "region" not in reverse, "reverse 端点只加响应键,不加查询参数"
