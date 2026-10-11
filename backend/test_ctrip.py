# -*- coding: utf-8 -*-
"""TASK-17:携程 suggest 数据源 + 住宿收藏懒补齐携程原生标识(全 mock 不触网)。

口径(调研 docs/research-ota-wechat-2026-10-09.md §A1):
* ``data_sources/ctrip.py``:公开 suggest 接口解析 hotelId/cityId,任何失败返回 None 不抛;
* ``app/api/collections.py::backfill_stay_ctrip_ids``:懒补齐写回 summary 持久化
  (ctrip_hotel_id/ctrip_city_id/ctrip_city_name),已有标识零网络、失败留空不阻塞、
  每请求限批 CTRIP_BACKFILL_BATCH 条。
"""

from __future__ import annotations

import json

import pytest
import requests

from data_sources import ctrip
from db import repository as repo
from db import init_db, make_engine, session_factory


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args, **kwargs):
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")
    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'ctrip_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)


class FakeSession:
    def __init__(self, payload=None, status_code: int = 200, error: Exception | None = None):
        self.payload = payload
        self.status_code = status_code
        self.error = error
        self.calls: list[dict] = []

    def request(self, method, url, params=None, data=None, timeout=None, headers=None):
        self.calls.append({"method": method, "url": url, "data": data,
                           "timeout": timeout, "headers": dict(headers or {})})
        if self.error is not None:
            raise self.error
        return FakeResponse(self.payload, self.status_code)


def hotel_payload(*rows):
    return {"Result": True, "ErrorCode": 0,
            "Response": {"searchResults": list(rows), "source": "City"}}


def hotel_row(hotel_id="83790270", city_id=83, city_name="昆山", word="昆山柏轩酒店",
              gd_lat=0.0, gd_lon=0.0, row_type="Hotel"):
    return {"id": hotel_id, "type": row_type, "word": word, "cityId": city_id,
            "cityName": city_name, "gdLat": gd_lat, "gdLon": gd_lon}


# --------------------------------------------------------------------------- #
# parse_suggest:响应解析健壮性
# --------------------------------------------------------------------------- #

def test_parse_suggest_hotel_rows() -> None:
    rows = ctrip.parse_suggest(hotel_payload(hotel_row(), hotel_row(hotel_id="80164179", word="柏轩酒店")))
    assert [row["hotel_id"] for row in rows] == ["83790270", "80164179"]
    assert rows[0]["city_id"] == 83 and rows[0]["city_name"] == "昆山"


def test_parse_suggest_skips_non_hotel_and_bad_rows() -> None:
    payload = hotel_payload(
        hotel_row(),
        hotel_row(hotel_id="12", row_type="City"),          # 非 Hotel 类型
        {"id": "", "type": "Hotel", "cityId": 1},            # id 缺失
        {"id": "9", "type": "Hotel"},                        # cityId 缺失
        "垃圾条目",
    )
    rows = ctrip.parse_suggest(payload)
    assert [row["hotel_id"] for row in rows] == ["83790270"]


@pytest.mark.parametrize("payload", [None, {}, {"Response": None}, {"Response": {}},
                                     {"Response": {"searchResults": "不是列表"}}, "文本"])
def test_parse_suggest_bad_shapes_return_empty(payload) -> None:
    assert ctrip.parse_suggest(payload) == []


# --------------------------------------------------------------------------- #
# suggest_hotel:调用与降级(全部注入 FakeSession,零网络)
# --------------------------------------------------------------------------- #

def test_suggest_hotel_returns_ids() -> None:
    fake = FakeSession(hotel_payload(hotel_row()))
    found = ctrip.suggest_hotel("昆山柏轩酒店", session=fake)
    assert found["hotel_id"] == "83790270" and found["city_id"] == 83
    assert found["matched"] is True and found["keyword"] == "昆山柏轩酒店"
    call = fake.calls[0]
    assert call["method"] == "POST" and call["url"] == ctrip.SUGGEST_URL
    body = json.loads(call["data"].decode("utf-8"))
    assert body["searchType"] == "H" and body["keyword"] == "昆山柏轩酒店"
    assert call["headers"]["Content-Type"] == "application/json"


def test_suggest_hotel_empty_keyword_skips_request() -> None:
    fake = FakeSession(hotel_payload(hotel_row()))
    assert ctrip.suggest_hotel("  ", session=fake) is None
    assert fake.calls == [], "空关键词不发请求"


@pytest.mark.parametrize("fake", [
    FakeSession(None, status_code=500),
    FakeSession(error=requests.ConnectionError("断网")),
    FakeSession(error=requests.Timeout("超时")),
    FakeSession({"Response": {"searchResults": []}}),
])
def test_suggest_hotel_degrades_to_none(fake) -> None:
    assert ctrip.suggest_hotel("某酒店", session=fake) is None, "任何失败都降级为 None 不抛"


def test_suggest_hotel_coordinate_disambiguation() -> None:
    far = hotel_row(hotel_id="1", gd_lat=39.9, gd_lon=116.4, word="北京同名店")
    near = hotel_row(hotel_id="2", gd_lat=31.386, gd_lon=120.981, word="昆山柏轩酒店")
    fake = FakeSession(hotel_payload(far, near))
    found = ctrip.suggest_hotel("昆山柏轩酒店", lat=31.3856, lng=120.9807, session=fake)
    assert found["hotel_id"] == "2", "坐标可用时取距离近的条目"


def test_suggest_hotel_city_hint_fallback() -> None:
    other = hotel_row(hotel_id="1", city_name="北京", word="柏轩酒店北京店")
    wanted = hotel_row(hotel_id="2", city_name="昆山", word="昆山柏轩酒店")
    fake = FakeSession(hotel_payload(other, wanted))
    # suggest 坐标常为 0(实测)→ 坐标门控失效,城市名消歧兜底
    found = ctrip.suggest_hotel("昆山柏轩酒店", city_hint="昆山", session=fake)
    assert found["hotel_id"] == "2"


# --------------------------------------------------------------------------- #
# 懒补齐:backfill_stay_ctrip_ids(经 GET /api/collections)
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _reset_cache():
    from services import ota_links
    ota_links._cache.clear()
    yield
    ota_links._cache.clear()


def _stay_kwargs(name="🛏️ 昆山柏轩酒店 · 住宿", summary=None):
    return dict(kind="place", to_lat=31.3856, to_lng=120.9807, to_name=name,
                summary=summary if summary is not None
                else {"stay_kind": "hotel", "price_estimate": "约¥300-500/晚", "city": "昆山"})


def test_backfill_writes_ctrip_ids_into_summary(session, monkeypatch) -> None:
    from app.api import collections as collections_api
    from services import ota_links
    repo.upsert_collection(session, **_stay_kwargs())
    calls = []

    def fake_suggest(keyword, **kwargs):
        calls.append(keyword)
        return {"hotel_id": "83790270", "city_id": 83, "city_name": "昆山",
                "word": "昆山柏轩酒店", "gd_lat": 0.0, "gd_lon": 0.0,
                "keyword": keyword, "matched": True}

    monkeypatch.setattr(ota_links.ctrip, "suggest_hotel", fake_suggest)
    payload = collections_api.list_collections(kind=None, cat=None, limit=None, session=session)
    summary = payload["collections"][0]["summary"]
    assert summary["ctrip_hotel_id"] == "83790270" and summary["ctrip_city_id"] == 83
    assert summary["ctrip_city_name"] == "昆山"
    assert calls == ["昆山柏轩酒店"], "解析词=酒店纯名(名字已含城市前缀不重复拼;剥 🛏️ 与 · 住宿)"
    assert "price_estimate" in summary and summary["city"] == "昆山", "既有 summary 键零变化"
    # 持久化验证:清缓存重查,不再触网
    ota_links._cache.clear()
    calls.clear()
    payload2 = collections_api.list_collections(kind=None, cat=None, limit=None, session=session)
    assert payload2["collections"][0]["summary"]["ctrip_hotel_id"] == "83790270"
    assert calls == [], "已补齐的行不再解析"


def test_backfill_failure_leaves_empty_and_does_not_block(session, monkeypatch) -> None:
    from app.api import collections as collections_api
    from services import ota_links
    repo.upsert_collection(session, **_stay_kwargs())

    def fail(*args, **kwargs):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(ota_links.ctrip, "suggest_hotel", fail)
    payload = collections_api.list_collections(kind=None, cat=None, limit=None, session=session)
    assert payload["count"] == 1, "解析失败不阻塞响应"
    summary = payload["collections"][0]["summary"]
    assert not summary.get("ctrip_hotel_id"), "失败留空不写回"
    assert summary["city"] == "昆山", "既有键零变化"


def test_backfill_skips_non_stay_rows(session, monkeypatch) -> None:
    from app.api import collections as collections_api
    from services import ota_links
    repo.upsert_collection(session, kind="place", to_lat=31.5, to_lng=121.5,
                           to_name="📍 西沙湿地 · 目的地")
    repo.upsert_collection(session, kind="route", mode="driving",
                           from_lat=31.2, from_lng=121.4, to_lat=31.5, to_lng=121.5)

    def boom(*args, **kwargs):
        raise AssertionError("非住宿行不该解析携程标识")

    monkeypatch.setattr(ota_links.ctrip, "suggest_hotel", boom)
    payload = collections_api.list_collections(kind=None, cat=None, limit=None, session=session)
    assert payload["count"] == 2
    assert all(not (item["summary"] or {}).get("ctrip_hotel_id") for item in payload["collections"])


def test_backfill_batch_limit(session, monkeypatch) -> None:
    from app.api import collections as collections_api
    from services import ota_links
    for index in range(ota_links.CTRIP_BACKFILL_BATCH + 2):
        repo.upsert_collection(
            session,
            kind="place",
            to_lat=round(31.3856 + index * 0.001, 7), to_lng=120.9807,  # 坐标各异防 ref_key 撞
            to_name=f"🛏️ 测试酒店{index} · 住宿",
            summary={"stay_kind": "hotel", "city": "昆山"},
        )
    calls = []

    def fake_suggest(keyword, **kwargs):
        calls.append(keyword)
        return {"hotel_id": str(1000 + len(calls)), "city_id": 83, "city_name": "昆山",
                "word": keyword, "gd_lat": 0.0, "gd_lon": 0.0, "keyword": keyword, "matched": True}

    monkeypatch.setattr(ota_links.ctrip, "suggest_hotel", fake_suggest)
    collections_api.list_collections(kind=None, cat=None, limit=None, session=session)
    assert len(calls) == ota_links.CTRIP_BACKFILL_BATCH, "每请求限批,其余下次开面板再补"
    # 再拉一次:补齐剩余的
    collections_api.list_collections(kind=None, cat=None, limit=None, session=session)
    assert len(calls) == ota_links.CTRIP_BACKFILL_BATCH + 2


def test_plain_stay_name_strips_decorations() -> None:
    from services import ota_links
    fn = ota_links.plain_stay_name
    assert fn("🛏️ 昆山柏轩酒店 · 住宿") == "昆山柏轩酒店"
    assert fn("🛏 某民宿 · 住宿") == "某民宿"
    assert fn("🏨 某酒店") == "某酒店"
    assert fn("裸名字") == "裸名字"
    assert fn(None) == ""


def test_ctrip_ids_two_level_keyword(monkeypatch) -> None:
    """城市+酒店名解析不到时退回纯酒店名;酒店名已含城市前缀则不重复拼。"""
    from services import ota_links
    ota_links._cache.clear()
    seen = []

    def fake_suggest(keyword, **kwargs):
        seen.append(keyword)
        if keyword == "昆山柏轩酒店":   # 模拟「城市+名」解析失败
            return None
        return {"hotel_id": "83790270", "city_id": 83, "city_name": "昆山",
                "word": "昆山柏轩酒店", "gd_lat": 0.0, "gd_lon": 0.0,
                "keyword": keyword, "matched": True}

    monkeypatch.setattr(ota_links.ctrip, "suggest_hotel", fake_suggest)
    # ① 酒店名不以城市开头:先「城市+名」,失败再纯名
    found = ota_links.ctrip_ids_for_stay("柏轩酒店", "昆山", None, None)
    assert found and found["hotel_id"] == "83790270"
    assert seen == ["昆山柏轩酒店", "柏轩酒店"], "两级关键词:先城市+名,失败再纯名"
    # ② 酒店名已含城市前缀(实测「上海上海…」重复前缀会解析失败):只发纯名
    seen.clear()
    ota_links._cache.clear()
    found2 = ota_links.ctrip_ids_for_stay("昆山柏轩酒店", "昆山", None, None)
    assert found2 is None and seen == ["昆山柏轩酒店"], \
        "名字自带城市前缀时不重复拼城市(fake 对该词返回 None,证明只发了一次)"
    ota_links._cache.clear()


# --------------------------------------------------------------------------- #
# TASK-24(境外双轨 P5):IntlCity 境外城市解析 + 懒补齐城市级兜底
# --------------------------------------------------------------------------- #

def intl_city_row(city_id="3167", city_name="因特拉肯", country="瑞士", word="因特拉肯"):
    return {"id": city_id, "type": "IntlCity", "word": word, "cityId": int(city_id),
            "cityName": city_name, "countryName": country}


def test_parse_suggest_intl_cities() -> None:
    payload = hotel_payload(
        intl_city_row(),
        intl_city_row(city_id="345428", city_name="因特拉肯", country="美国"),
        hotel_row(),                                   # Hotel 类型不混入
        {"id": "x", "type": "IntlCity", "cityId": 1},  # word/cityName 缺失不炸
    )
    rows = ctrip.parse_suggest_intl_cities(payload)
    assert [r["city_id"] for r in rows] == [3167, 345428, 1]
    assert rows[0]["country_name"] == "瑞士" and rows[0]["city_name"] == "因特拉肯"


@pytest.mark.parametrize("payload", [None, {}, {"Response": {"searchResults": "不是列表"}},
                                     {"Response": {"searchResults": [{"type": "IntlCity"}]}},  # cityId 缺失
                                     {"Response": {"searchResults": [{"type": "IntlCity", "cityId": "abc"}]}},  # 非数字
                                     {"Response": {"searchResults": [{"type": "IntlCity", "cityId": 0}]}}])  # ≤0
def test_parse_suggest_intl_cities_bad_shapes(payload) -> None:
    assert ctrip.parse_suggest_intl_cities(payload) == []


def test_parse_suggest_hotel_ignores_intl_city() -> None:
    """既有 parse_suggest(只认 Hotel)口径零变化:IntlCity 不得混入。"""
    rows = ctrip.parse_suggest(hotel_payload(intl_city_row(), hotel_row()))
    assert [r["hotel_id"] for r in rows] == ["83790270"]


def test_suggest_city_returns_first_and_keyword() -> None:
    fake = FakeSession(hotel_payload(intl_city_row(),
                                     intl_city_row(city_id="345428", country="美国")))
    found = ctrip.suggest_city("因特拉肯", session=fake)
    assert found == {"city_id": 3167, "city_name": "因特拉肯", "country_name": "瑞士",
                     "word": "因特拉肯", "keyword": "因特拉肯"}, "同名多国取首条"
    body = json.loads(fake.calls[0]["data"].decode("utf-8"))
    assert body["searchType"] == "D", "城市查询用 searchType=D(调研 §A1)"


@pytest.mark.parametrize("fake", [
    FakeSession(None, status_code=500),
    FakeSession(error=requests.ConnectionError("断网")),
    FakeSession({"Response": {"searchResults": []}}),
])
def test_suggest_city_degrades_to_none(fake) -> None:
    assert ctrip.suggest_city("某城", session=fake) is None


def test_suggest_city_empty_keyword_skips_request() -> None:
    fake = FakeSession(hotel_payload(intl_city_row()))
    assert ctrip.suggest_city("  ", session=fake) is None
    assert fake.calls == []


def test_backfill_intl_city_fallback_when_hotel_missing(session, monkeypatch) -> None:
    """TASK-24:境外酒店解析不到 Hotel → IntlCity 城市级兜底写回 summary。"""
    from app.api import collections as collections_api
    from services import ota_links
    repo.upsert_collection(session, kind="place", to_lat=46.686, to_lng=7.863,
                           to_name="🛏️ 某境外民宿 · 住宿",
                           summary={"stay_kind": "guest_house", "city": "因特拉肯"})

    monkeypatch.setattr(ota_links.ctrip, "suggest_hotel",
                        lambda *a, **k: None)   # 境外小体量酒店:解析不到
    monkeypatch.setattr(ota_links.ctrip, "suggest_city",
                        lambda *a, **k: {"city_id": 3167, "city_name": "因特拉肯",
                                         "country_name": "瑞士", "keyword": "因特拉肯"})
    payload = collections_api.list_collections(kind=None, cat=None, limit=None, session=session)
    summary = payload["collections"][0]["summary"]
    assert summary["ctrip_city_id"] == 3167 and summary["ctrip_city_fallback"] is True
    assert summary["ctrip_city_name"] == "因特拉肯" and summary["country"] == "瑞士"
    assert not summary.get("ctrip_hotel_id"), "酒店级没解析到就不写 hotel_id"
    # 持久化后零网络
    ota_links._cache.clear()
    calls = []
    monkeypatch.setattr(ota_links.ctrip, "suggest_hotel",
                        lambda *a, **k: calls.append("hotel"))
    monkeypatch.setattr(ota_links.ctrip, "suggest_city",
                        lambda *a, **k: calls.append("city"))
    payload2 = collections_api.list_collections(kind=None, cat=None, limit=None, session=session)
    assert payload2["collections"][0]["summary"]["ctrip_city_id"] == 3167
    assert calls == [], "城市兜底已落的行不再解析"


def test_backfill_domestic_city_no_intl_fallback(session, monkeypatch) -> None:
    """境内城市 suggest_city 返回 None(无 IntlCity 条目)→ 境内口径零变化,留空。"""
    from app.api import collections as collections_api
    from services import ota_links
    repo.upsert_collection(session, **_stay_kwargs())   # 昆山柏轩,city=昆山
    monkeypatch.setattr(ota_links.ctrip, "suggest_hotel", lambda *a, **k: None)
    seen = []
    monkeypatch.setattr(ota_links.ctrip, "suggest_city",
                        lambda *a, **k: (seen.append(a[0] if a else k.get("keyword")), None)[1])
    payload = collections_api.list_collections(kind=None, cat=None, limit=None, session=session)
    summary = payload["collections"][0]["summary"]
    assert not summary.get("ctrip_hotel_id") and not summary.get("ctrip_city_id")
    assert not summary.get("ctrip_city_fallback")


def test_backfill_hotel_preferred_over_city_fallback(session, monkeypatch) -> None:
    """酒店级解析成功时不触发城市兜底(境外大牌酒店有 Hotel 条目)。"""
    from app.api import collections as collections_api
    from services import ota_links
    repo.upsert_collection(session, kind="place", to_lat=-45.031, to_lng=168.662,
                           to_name="🛏️ 皇后镇湖畔诺富特酒店 · 住宿",
                           summary={"stay_kind": "hotel", "city": "皇后镇"})
    monkeypatch.setattr(ota_links.ctrip, "suggest_hotel",
                        lambda *a, **k: {"hotel_id": "2196343", "city_id": 3860,
                                         "city_name": "皇后镇", "word": "x",
                                         "gd_lat": 0.0, "gd_lon": 0.0,
                                         "keyword": "x", "matched": True})
    monkeypatch.setattr(ota_links.ctrip, "suggest_city",
                        lambda *a, **k: pytest.fail("酒店级成功不该触发城市兜底"))
    payload = collections_api.list_collections(kind=None, cat=None, limit=None, session=session)
    summary = payload["collections"][0]["summary"]
    assert summary["ctrip_hotel_id"] == "2196343" and not summary.get("ctrip_city_fallback")
