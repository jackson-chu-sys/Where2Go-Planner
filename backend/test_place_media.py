"""TASK-8a1 单测:目的地图片链路(高德 POI 图 + 维基兜底 + PlaceMedia 缓存 + /api/places/media)。

全程不触网:

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,任何偷偷联网当场失败;
* 高德 ``/place/text`` 用 :class:`FakeSession` 替身,断言 **URL 路径与查询参数**、坐标门控、
  ``status != "1"`` 不抛、HTTP 错误抛 :class:`DataSourceError`、``http://`` 升 ``https://``;
* ``no_sleep`` 把 :data:`amap._SLEEP` 换成记录器 —— 节流 0.6s 与退避 2s/5s 都不会真的等;
* 服务层用 :func:`stub_amap` / :func:`stub_wiki` 替换两个源,另有**端到端**用例用
  :func:`stub_http` 把两个数据源的 session 都换成同一个替身(真走一遍解析);
* DB 用 ``tmp_path`` 里的独立 SQLite(``make_engine`` + ``init_db``,与 test_collections.py 同套路),
  缓存命中/负缓存/TTL 过期靠直接改 ``fetched_at`` 模拟,不 sleep。

覆盖契约要点:坐标门控、缺 key/配额 → ``[]``、图源四态(amap/wikimedia/mixed/none)、
7 天命中缓存 + 6 小时负缓存、单 POI 图上限、批量 ≤20 且按入参顺序、单条失败不扩散。

运行:``cd backend && ../.venv/bin/python -m pytest -q test_place_media.py``
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from typing import Any, Callable, Optional

import pytest
import requests
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import places as places_api  # noqa: E402
from app.main import app  # noqa: E402
from data_sources import amap, wikimedia  # noqa: E402
from data_sources._common import DataSourceError, TransientDataSourceError  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db import models  # noqa: E402
from db import repository as repo  # noqa: E402
from db.base import get_session, set_engine  # noqa: E402
from services import place_media  # noqa: E402

# --------------------------------------------------------------------------- #
# 测试替身(与 backend/test_amap.py 同一套写法)
# --------------------------------------------------------------------------- #


class FakeResponse:
    """最小 response 替身:只需要 ``status_code`` 与 ``text``。"""

    def __init__(self, payload: Any = None, *, status_code: int = 200, text: Optional[str] = None) -> None:
        self.status_code = status_code
        if text is None:
            text = "" if payload is None else json.dumps(payload, ensure_ascii=False)
        self.text = text


class FakeSession:
    """记录调用参数并按顺序返回预设响应(响应用完就重复最后一条,便于数"发了几次请求")。"""

    def __init__(self, *responses: Any) -> None:
        given = list(responses) or [{}]
        self.responses = [
            item if isinstance(item, (FakeResponse, BaseException)) else FakeResponse(item)
            for item in given
        ]
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append({"method": method, "url": url, "params": dict(params or {}), "timeout": timeout})
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]


def expect_error(func: Callable[[], Any], exc_type: type, *fragments: str) -> Exception:
    """断言 ``func()`` 抛出 ``exc_type``,且错误信息包含全部 ``fragments``。"""
    with pytest.raises(exc_type) as caught:
        func()
    for fragment in fragments:
        assert fragment in str(caught.value), f"应包含 {fragment!r},实际:{caught.value}"
    return caught.value


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
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """节流(0.6s)与退避(2s/5s)都不真的等,只把秒数记下来供断言。"""
    slept: list[float] = []
    monkeypatch.setattr(amap, "_SLEEP", slept.append)
    return slept


@pytest.fixture()
def session(tmp_path):
    """每个用例一个独立的临时 SQLite 库(**绝不碰 backend/data/where2go.db**)。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'place_media_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


@pytest.fixture()
def api_client(session):
    """完整 HTTP 链用的客户端:把 ``get_session`` 依赖换成用例里的同一个临时库会话。"""
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield lambda method, path, query="": http_request(method, path, query=query)
    finally:
        app.dependency_overrides.pop(get_session, None)


def http_request(method: str, path: str, *, query: str = "") -> tuple[int, Any]:
    """直接驱动 ASGI app 走一遍**完整 HTTP 链**(仓库没装 httpx/TestClient,自己拼最小 scope)。"""
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                    (b"content-length", b"0")],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    payload = bytearray()
    status: dict[str, int] = {}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            payload.extend(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return status["code"], json.loads(payload.decode() or "null")


# --------------------------------------------------------------------------- #
# 样本数据(契约 §1 的实测形状:西湖 3 张图、汤泽误配、坐标 "lng,lat")
# --------------------------------------------------------------------------- #

ENV: dict[str, str] = {amap.ENV_AMAP_KEY: "test-amap-key", amap.ENV_MIN_INTERVAL: "0"}
WEST_LAKE = (30.246, 120.149)   # (lat, lng)
NEARBY = (30.250, 120.155)      # 距西湖点约 0.7km(门控阈值测试用)

POI_WEST_LAKE = {
    "id": "B023B0AJQ5", "name": "西湖", "location": "120.149000,30.246000",
    "photos": [
        {"title": "西湖全景", "url": "http://store.is.autonavi.com/showpic/aaa.jpg"},
        {"title": "", "url": "http://store.is.autonavi.com/showpic/bbb.jpg"},
        {"title": "重复图", "url": "http://store.is.autonavi.com/showpic/aaa.jpg"},
    ],
}
POI_FAR = {
    "id": "B0FFH00000", "name": "狂飙乐园滑雪场", "location": "138.810000,36.930000",
    "photos": [{"title": "别处的图", "url": "http://store.is.autonavi.com/showpic/far.jpg"}],
}
POI_NO_PHOTOS = {
    "id": "B0FFH11111", "name": "崇儒畲族乡", "location": "120.149000,30.246000", "photos": [],
}
POI_NO_LOCATION = {"id": "B0FFH22222", "name": "西湖", "photos": [{"url": "http://a/1.jpg"}]}


def ok(payload: dict[str, Any]) -> dict[str, Any]:
    """高德成功响应的公共外壳(**HTTP 恒 200**,成败看 status/infocode)。"""
    return {"status": "1", "info": "OK", "infocode": "10000", "count": "1", **payload}


def fail(infocode: str, info: str = "ERROR") -> dict[str, Any]:
    return {"status": "0", "info": info, "infocode": infocode}


TEXT_OK = ok({"pois": [POI_WEST_LAKE]})
TEXT_FAR = ok({"pois": [POI_FAR]})
TEXT_NO_PHOTOS = ok({"pois": [POI_NO_PHOTOS]})
TEXT_EMPTY = ok({"pois": []})
TEXT_NO_LOCATION = ok({"pois": [POI_NO_LOCATION]})

WIKI_THUMB = "https://upload.wikimedia.org/zh/thumb/q/q1/Qianwang.jpg/800px-Qianwang.jpg"
WIKI_PAGE_URL = "https://zh.wikipedia.org/wiki/%E9%92%B1%E7%8E%8B%E7%A5%A0"
WIKI_HIT = {
    "page_title": "钱王祠", "page_url": WIKI_PAGE_URL, "extract": "钱王祠在西湖东岸。",
    "images": [{"url": WIKI_THUMB, "title": "钱王祠"}], "via": "geosearch",
}
AMAP_URLS = [
    "https://store.is.autonavi.com/showpic/aaa.jpg",
    "https://store.is.autonavi.com/showpic/bbb.jpg",
]
AMAP_IMAGES = [{"url": AMAP_URLS[0], "title": "西湖全景"}, {"url": AMAP_URLS[1], "title": ""}]

#: 维基端到端用的响应样本(geosearch 命中有图页 + Commons 两张)
GEO_HIT = {"query": {"pages": {"222": {
    "pageid": 222, "ns": 0, "index": 1, "title": "钱王祠", "fullurl": WIKI_PAGE_URL,
    "extract": "钱王祠在西湖东岸。",
    "pageimages": {"thumbnail": {"source": WIKI_THUMB.replace("https://", "http://")}},
}}}}
COMMONS_TWO = {"query": {"pages": {
    "9001": {"ns": 6, "index": 1, "title": "File:West Lake 1.jpg",
             "imageinfo": [{"thumburl": "https://upload.wikimedia.org/c1.jpg?utm_source=x"}]},
    "9002": {"ns": 6, "index": 2, "title": "File:West Lake 2.jpg",
             "imageinfo": [{"thumburl": "https://upload.wikimedia.org/c2.jpg?20261001"}]},
}}}


def make_place(session, *, name: str = "西湖", lat: float = WEST_LAKE[0], lng: float = WEST_LAKE[1],
               osm_id: int = 1001, category: str = "自然") -> models.Place:
    """往临时库里塞一条 ``Place``(高德来源),返回 ORM 行。"""
    repo.upsert_places(session, origin_city="杭州", band="0_50", items=[{
        "osm_type": models.AMAP_OSM_TYPE, "osm_id": osm_id, "name": name,
        "lat": lat, "lng": lng, "category": category, "tags": {"amap_id": "B023B0AJQ5"},
    }])
    session.commit()
    return session.scalars(select(models.Place).where(models.Place.osm_id == osm_id)).one()


def stub_amap(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[tuple[Any, ...]]:
    """把 :func:`amap.search_poi_photos` 换成固定返回值(或异常)的替身,并记录调用。"""
    calls: list[tuple[Any, ...]] = []

    def fake(name: str, lat: float, lng: float, **kwargs: Any) -> Any:
        calls.append((name, lat, lng, kwargs))
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return result(name, lat, lng, **kwargs)
        return [dict(item) for item in result]

    monkeypatch.setattr(place_media.amap, "search_poi_photos", fake)
    return calls


def stub_wiki(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[tuple[Any, ...]]:
    """把 :func:`wikimedia.wikipedia_media` 换成固定返回值(或异常)的替身,并记录调用。"""
    calls: list[tuple[Any, ...]] = []

    def fake(name: str, lat: float, lng: float, **kwargs: Any) -> Any:
        calls.append((name, lat, lng, kwargs))
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(place_media.wikimedia, "wikipedia_media", fake)
    return calls


def stub_http(monkeypatch: pytest.MonkeyPatch, http: FakeSession) -> FakeSession:
    """让两个数据源都用同一个 HTTP 替身(端到端用例:真走一遍请求拼装与响应解析)。"""
    monkeypatch.setattr(place_media.amap, "_resolve_session", lambda *a, **k: http)
    monkeypatch.setattr(place_media.wikimedia, "_resolve_session", lambda *a, **k: http)
    return http


def age_media_row(session, place_id: int, *, seconds: float) -> None:
    """把缓存行的 ``fetched_at`` 往前挪,模拟 TTL 过期(不 sleep)。"""
    row = repo.get_place_media(session, place_id=place_id)
    row.fetched_at = models.utcnow() - timedelta(seconds=seconds)
    session.commit()


# --------------------------------------------------------------------------- #
# amap.search_poi_photos:请求参数 / http→https / 坐标门控
# --------------------------------------------------------------------------- #


def test_search_poi_photos_request_params() -> None:
    """``/place/text`` + keywords/location(**lng,lat**)/radius/offset=1/extensions=all。"""
    http = FakeSession(TEXT_OK)
    amap.search_poi_photos("西湖", WEST_LAKE[0], WEST_LAKE[1], environ=ENV, session=http)
    call = http.calls[0]
    assert call["url"] == f"{amap.DEFAULT_ENDPOINT}{amap.SEARCH_TEXT_PATH}"
    params = call["params"]
    assert params["keywords"] == "西湖"
    assert params["location"] == "120.149000,30.246000"
    assert params["radius"] == 20000
    assert params["offset"] == 1
    assert params["extensions"] == "all"
    assert params["key"] == "test-amap-key"


def test_search_poi_photos_upgrades_http_and_dedupes() -> None:
    """3 张图(其中 1 张重复)→ 2 张,``http://`` 全部升 ``https://``,title 原样带出。"""
    photos = amap.search_poi_photos("西湖", WEST_LAKE[0], WEST_LAKE[1],
                                    environ=ENV, session=FakeSession(TEXT_OK))
    assert photos == [
        {"url": AMAP_URLS[0], "title": "西湖全景"},
        {"url": AMAP_URLS[1], "title": ""},
    ]
    assert all(item["url"].startswith("https://") for item in photos)


def test_coordinate_gate_rejects_far_poi() -> None:
    """契约 §1 的误配案例:坐标在杭州、命中日本汤泽的滑雪场 → ``[]``(不抛、不贴别处的图)。"""
    assert amap.search_poi_photos("汤泽高原滑雪场", WEST_LAKE[0], WEST_LAKE[1],
                                  environ=ENV, session=FakeSession(TEXT_FAR)) == []


def test_coordinate_gate_threshold_is_env_overridable() -> None:
    """``WHERE2GO_AMAP_MAX_MATCH_M``:0.7km 外的 POI 默认(5000m)算命中、收紧到 100m 就不算。"""
    payload = ok({"pois": [{
        "id": "X", "name": "西湖", "location": f"{NEARBY[1]:.6f},{NEARBY[0]:.6f}",
        "photos": [{"title": "近邻", "url": "http://a/near.jpg"}],
    }]})
    assert len(amap.search_poi_photos("西湖", WEST_LAKE[0], WEST_LAKE[1],
                                      environ=ENV, session=FakeSession(payload))) == 1
    tight = {**ENV, amap.ENV_AMAP_MAX_MATCH_M: "100"}
    assert amap.search_poi_photos("西湖", WEST_LAKE[0], WEST_LAKE[1],
                                  environ=tight, session=FakeSession(payload)) == []
    loose = {**ENV, amap.ENV_AMAP_MAX_MATCH_M: "9999000"}
    assert len(amap.search_poi_photos("汤泽高原滑雪场", WEST_LAKE[0], WEST_LAKE[1],
                                      environ=loose, session=FakeSession(TEXT_FAR))) == 1
    assert amap.resolve_max_match_m(loose) == 9999000
    assert amap.resolve_max_match_m({**ENV, amap.ENV_AMAP_MAX_MATCH_M: "abc"}) == amap.DEFAULT_MAX_MATCH_M
    assert amap.resolve_max_match_m(ENV) == 5000


@pytest.mark.parametrize("payload", [
    TEXT_NO_PHOTOS,     # photos 为空数组(实测崇儒乡只有 1 张,不少乡镇 POI 是 0 张)
    TEXT_NO_LOCATION,   # 有图但没有 location → 门控无法判定 = 不用它的图
    TEXT_EMPTY,         # pois 为空
    ok({}),             # 连 pois 都没有
    ok({"pois": [{"id": "X", "name": "西湖", "location": "120.149,30.246"}]}),  # 没有 photos 键
    ok({"pois": ["dirty"]}),                                                     # pois[0] 不是对象
])
def test_no_usable_photos_returns_empty(payload: Any) -> None:
    assert amap.search_poi_photos("西湖", WEST_LAKE[0], WEST_LAKE[1],
                                  environ=ENV, session=FakeSession(payload)) == []


def test_dirty_photos_are_filtered() -> None:
    """空 url / 相对路径 / 非 dict / 非 http(s) 都按"无效图源"丢掉,裸字符串也认。"""
    payload = ok({"pois": [{
        "id": "X", "name": "西湖", "location": "120.149000,30.246000",
        "photos": [
            {"title": "空", "url": ""}, {"title": "相对", "url": "/showpic/x.jpg"},
            {"title": "无 url"}, "not-a-url", {"url": "http://a/ok.jpg"},
            "http://a/raw-string.jpg", {"url": "ftp://a/x.jpg"},
        ],
    }]})
    assert amap.search_poi_photos("西湖", WEST_LAKE[0], WEST_LAKE[1],
                                  environ=ENV, session=FakeSession(payload)) == [
        {"url": "https://a/ok.jpg", "title": ""},
        {"url": "https://a/raw-string.jpg", "title": ""},
    ]
    assert amap.parse_photos("nope") == []
    assert amap.normalize_photo_url("https://") is None


def test_radius_is_clamped_to_amap_limit() -> None:
    http = FakeSession(TEXT_OK)
    amap.search_poi_photos("西湖", WEST_LAKE[0], WEST_LAKE[1], radius_m=90000,
                           environ=ENV, session=http)
    assert http.last["params"]["radius"] == amap.AUTO_MAX_RADIUS_M


def test_endpoint_env_override() -> None:
    http = FakeSession(TEXT_OK)
    amap.search_poi_photos("西湖", WEST_LAKE[0], WEST_LAKE[1],
                           environ={**ENV, amap.ENV_ENDPOINT: "https://amap.internal/v3/"}, session=http)
    assert http.last["url"] == "https://amap.internal/v3/place/text"


# --------------------------------------------------------------------------- #
# amap.search_poi_photos:失败口径(status != "1" → [];HTTP 错 → DataSourceError)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("infocode", ["10001", "10009", "40000", "20000", "10012"])
def test_business_failures_return_empty(infocode: str) -> None:
    """缺 key / 平台不符 / 配额耗尽 / 参数非法 → ``[]``,**不抛**(永久错误不重试)。"""
    http = FakeSession(FakeResponse(fail(infocode)))
    assert amap.search_poi_photos("西湖", WEST_LAKE[0], WEST_LAKE[1], environ=ENV, session=http) == []
    assert len(http.calls) == 1


def test_transient_infocode_retries_then_returns_empty(no_sleep: list[float]) -> None:
    """限流(10021)退避重试打满后也**不抛**,当"没图"处理;退避 2s/5s。"""
    http = FakeSession(FakeResponse(fail("10021", "CUQPS_HAS_EXCEEDED_THE_LIMIT")))
    assert amap.search_poi_photos("西湖", WEST_LAKE[0], WEST_LAKE[1], environ=ENV, session=http) == []
    assert len(http.calls) == amap.MAX_ATTEMPTS
    assert no_sleep == [2.0, 5.0]


def test_parse_poi_photos_tolerates_bad_payload() -> None:
    assert amap.parse_poi_photos("nope", lat=30.0, lng=120.0) == []
    assert amap.parse_poi_photos(None, lat=30.0, lng=120.0) == []
    assert amap.parse_poi_photos({"status": "0"}, lat=30.0, lng=120.0) == []
    assert amap.parse_poi_photos(TEXT_OK, lat=30.246, lng=120.149, max_match_m="abc") == [
        {"url": AMAP_URLS[0], "title": "西湖全景"}, {"url": AMAP_URLS[1], "title": ""},
    ]


def test_http_error_raises_datasource_error(no_sleep: list[float]) -> None:
    """HTTP 5xx 属瞬时错误:退避重试打满后**抛**(由调用方降级),不软化。"""
    http = FakeSession(FakeResponse(None, status_code=500, text="boom"))
    expect_error(lambda: amap.search_poi_photos("西湖", WEST_LAKE[0], WEST_LAKE[1],
                                                environ=ENV, session=http),
                 TransientDataSourceError, "服务端错误")
    assert len(http.calls) == amap.MAX_ATTEMPTS
    assert no_sleep == [2.0, 5.0]


def test_timeout_raises_datasource_error() -> None:
    http = FakeSession(requests.Timeout("read timed out"))
    expect_error(lambda: amap.search_poi_photos("西湖", WEST_LAKE[0], WEST_LAKE[1],
                                                environ=ENV, session=http),
                 DataSourceError, "请求超时")


def test_missing_key_raises_datasource_error() -> None:
    """没配 ``WHERE2GO_AMAP_KEY`` → 抛中文 DataSourceError(服务层据此记 reason=no_key)。"""
    http = FakeSession(TEXT_OK)
    expect_error(
        lambda: amap.search_poi_photos("西湖", WEST_LAKE[0], WEST_LAKE[1],
                                       environ={amap.ENV_MIN_INTERVAL: "0"}, session=http),
        DataSourceError, amap.MISSING_KEY_MESSAGE,
    )
    assert http.calls == []


def test_bad_arguments_raise_value_error() -> None:
    with pytest.raises(ValueError, match="name 不能为空"):
        amap.search_poi_photos("  ", WEST_LAKE[0], WEST_LAKE[1], environ=ENV, session=FakeSession(TEXT_OK))
    with pytest.raises(ValueError, match="纬度超出"):
        amap.search_poi_photos("西湖", 999.0, WEST_LAKE[1], environ=ENV, session=FakeSession(TEXT_OK))


# --------------------------------------------------------------------------- #
# PlaceMedia 表与 repository 读写
# --------------------------------------------------------------------------- #


def test_place_media_table_and_upsert_roundtrip(session) -> None:
    """建表 + upsert 幂等:``place_id`` 唯一,重复写只刷新内容(不产生第二行)。"""
    place = make_place(session)
    row = repo.upsert_place_media(session, place_id=place.id, source="amap",
                                  images=[{"url": AMAP_URLS[0], "title": "西湖全景", "source": "amap"}])
    session.commit()
    assert repo.get_place_media(session, place_id=place.id) is row
    assert session.query(models.PlaceMedia).count() == 1
    # place_id 唯一(UniqueConstraint):同一 POI 塞第二行直接 IntegrityError
    with pytest.raises(IntegrityError):
        session.add(models.PlaceMedia(place_id=place.id, source="none", images=[]))
        session.flush()
    session.rollback()
    assert session.query(models.PlaceMedia).count() == 1

    again = repo.upsert_place_media(session, place_id=place.id, source="none", images=[], reason="no_data")
    session.commit()
    assert again.id == row.id
    assert session.query(models.PlaceMedia).count() == 1
    dumped = repo.media_to_dict(again)
    assert dumped["source"] == "none"
    assert dumped["reason"] == "no_data"
    assert dumped["images"] == []
    assert dumped["page_url"] is None
    assert dumped["fetched_at"].endswith("+00:00")


def test_upsert_place_media_normalizes_source_and_images(session) -> None:
    """脏图源归一:未知 source → ``none``,没 url 的条目丢掉,**有图时 reason 恒空**。"""
    place = make_place(session)
    row = repo.upsert_place_media(
        session, place_id=place.id, source="weird",
        images=[{"title": "没有 url"}, {"url": "https://a/1.jpg", "title": "有图", "source": "amap"}],
        reason="no_data",
    )
    assert row.source == models.MEDIA_SOURCE_NONE
    assert row.images == [{"url": "https://a/1.jpg", "title": "有图", "source": "amap"}]
    assert row.reason is None
    with pytest.raises(ValueError, match="place_id"):
        repo.upsert_place_media(session, place_id=None, source="amap", images=[])
    with pytest.raises(ValueError, match="正整数"):
        repo.get_place_media(session, place_id=0)


def test_media_map_batch_read(session) -> None:
    """批量读:一次查询取多行,脏 id 直接跳过(不抛)。"""
    first = make_place(session, osm_id=1001)
    make_place(session, osm_id=1002, name="太子湾公园")
    repo.upsert_place_media(session, place_id=first.id, source="amap",
                            images=[{"url": AMAP_URLS[0], "title": "", "source": "amap"}])
    session.commit()
    found = repo.media_map(session, [first.id, 999, first.id, "x", None])
    assert set(found) == {first.id}
    assert repo.media_map(session, []) == {}


# --------------------------------------------------------------------------- #
# services.place_media:图源四态
# --------------------------------------------------------------------------- #


def test_fetch_media_amap_only(session, monkeypatch) -> None:
    """高德命中、维基空 → ``source="amap"``,每张图标 ``source``,并落库。"""
    place = make_place(session)
    calls = stub_wiki(monkeypatch, wikimedia.empty_result())
    amap_calls = stub_amap(monkeypatch, AMAP_IMAGES)
    result = place_media.fetch_media_for_place(place, environ=ENV, session=session)
    assert result["place_id"] == place.id
    assert result["source"] == "amap"
    assert [item["url"] for item in result["images"]] == AMAP_URLS
    assert all(item["source"] == "amap" for item in result["images"])
    assert set(result["images"][0]) == {"url", "title", "source"}
    assert result["reason"] is None
    assert result["cached"] is False
    assert result["fetched_at"].endswith("+00:00")
    assert repo.get_place_media(session, place_id=place.id).source == "amap"
    assert amap_calls[0][:3] == ("西湖", WEST_LAKE[0], WEST_LAKE[1])
    assert amap_calls[0][3]["radius_m"] == place_media.AMAP_RADIUS_M
    assert calls[0][3]["limit"] == place_media.WIKI_IMAGE_LIMIT


def test_fetch_media_mixed_when_both_have_images(session, monkeypatch) -> None:
    """高德 2 张 + 维基 1 张 → ``source="mixed"``,高德图在前、维基图在后,``page_url`` 透出。"""
    place = make_place(session)
    stub_wiki(monkeypatch, WIKI_HIT)
    stub_amap(monkeypatch, AMAP_IMAGES)
    result = place_media.fetch_media_for_place(place, environ=ENV, session=session)
    assert result["source"] == "mixed"
    assert [item["source"] for item in result["images"]] == ["amap", "amap", "wikimedia"]
    assert result["images"][-1]["url"] == WIKI_THUMB
    assert result["page_url"] == WIKI_PAGE_URL
    assert result["reason"] is None


def test_fetch_media_wikimedia_only(session, monkeypatch) -> None:
    """高德没图(photos 空)、维基有图 → ``source="wikimedia"``。"""
    place = make_place(session, name="崇儒畲族乡")
    stub_wiki(monkeypatch, WIKI_HIT)
    stub_amap(monkeypatch, [])
    result = place_media.fetch_media_for_place(place, environ=ENV, session=session)
    assert result["source"] == "wikimedia"
    assert [item["url"] for item in result["images"]] == [WIKI_THUMB]
    assert result["reason"] is None


def test_fetch_media_none_when_no_images(session, monkeypatch) -> None:
    """两个源都没图 → ``source="none"`` + ``reason="no_data"``(**不编造图片 URL**)。"""
    place = make_place(session)
    stub_wiki(monkeypatch, wikimedia.empty_result())
    stub_amap(monkeypatch, [])
    result = place_media.fetch_media_for_place(place, environ=ENV, session=session)
    assert result["source"] == "none"
    assert result["images"] == []
    assert result["reason"] == "no_data"
    assert result["page_url"] is None
    assert repo.get_place_media(session, place_id=place.id).reason == "no_data"


def test_fetch_media_end_to_end_over_one_http_session(session, monkeypatch) -> None:
    """端到端(不替换两个数据源函数):高德 place/text + 维基 geosearch + Commons 走同一个替身。"""
    place = make_place(session)
    http = stub_http(monkeypatch, FakeSession(TEXT_OK, GEO_HIT, COMMONS_TWO))
    result = place_media.fetch_media_for_place(place, environ=ENV, session=session)
    assert result["source"] == "mixed"
    assert [item["url"] for item in result["images"]] == [
        AMAP_URLS[0], AMAP_URLS[1], WIKI_THUMB,
        "https://upload.wikimedia.org/c1.jpg", "https://upload.wikimedia.org/c2.jpg",
    ]
    assert [item["source"] for item in result["images"]] == ["amap", "amap", "wikimedia",
                                                             "wikimedia", "wikimedia"]
    assert result["page_url"] == WIKI_PAGE_URL
    assert [call["params"].get("keywords") or call["params"].get("generator") for call in http.calls] == [
        "西湖", "geosearch", "search",
    ]


def test_fetch_media_missing_key_reason(session, monkeypatch) -> None:
    """没配高德 key(且维基也没图)→ ``reason="no_key"``,前端可提示"配好 key 即可用"。"""
    place = make_place(session)
    stub_wiki(monkeypatch, wikimedia.empty_result())
    result = place_media.fetch_media_for_place(place, environ={amap.ENV_MIN_INTERVAL: "0"}, session=session)
    assert (result["source"], result["reason"]) == ("none", "no_key")


def test_fetch_media_amap_error_still_falls_back_to_wiki(session, monkeypatch) -> None:
    """高德 HTTP 报错 → 该源当没图,维基兜底照样出图(``source="wikimedia"``、reason 清空)。"""
    place = make_place(session)
    stub_wiki(monkeypatch, WIKI_HIT)
    stub_amap(monkeypatch, DataSourceError("amap", "服务端错误(HTTP 500)"))
    result = place_media.fetch_media_for_place(place, environ=ENV, session=session)
    assert result["source"] == "wikimedia"
    assert result["reason"] is None


def test_fetch_media_records_error_reason_when_both_fail(session, monkeypatch) -> None:
    """高德报错 + 维基也空 → ``reason="error"``(可重试,只写 6 小时负缓存)。"""
    place = make_place(session)
    stub_wiki(monkeypatch, wikimedia.empty_result())
    stub_amap(monkeypatch, TransientDataSourceError("amap", "请求超时(>15s)"))
    result = place_media.fetch_media_for_place(place, environ=ENV, session=session)
    assert (result["source"], result["reason"]) == ("none", "error")


def test_wiki_exception_does_not_break_amap(session, monkeypatch) -> None:
    """维基替身意外抛错(它承诺不抛,服务层再兜一层)→ 高德图照常返回。"""
    place = make_place(session)
    stub_wiki(monkeypatch, RuntimeError("wiki exploded"))
    stub_amap(monkeypatch, AMAP_IMAGES)
    result = place_media.fetch_media_for_place(place, environ=ENV, session=session)
    assert result["source"] == "amap"
    assert [item["url"] for item in result["images"]] == AMAP_URLS


def test_fetch_media_accepts_mapping(session, monkeypatch) -> None:
    """契约签名收 ``Mapping``:给 dict 也能跑(不必是 ORM 行);没有 id 抛中文 ValueError。"""
    place = make_place(session)
    stub_wiki(monkeypatch, wikimedia.empty_result())
    stub_amap(monkeypatch, AMAP_IMAGES)
    result = place_media.fetch_media_for_place(
        {"id": place.id, "name": "西湖", "lat": WEST_LAKE[0], "lng": WEST_LAKE[1]},
        environ=ENV, session=session,
    )
    assert result["place_id"] == place.id
    assert result["source"] == "amap"
    with pytest.raises(ValueError, match="正整数 id"):
        place_media.fetch_media_for_place({"name": "西湖"}, environ=ENV, session=session)


def test_place_without_name_or_coords_is_no_data(session, monkeypatch) -> None:
    """名称/坐标缺失 → 连请求都不发,``reason="no_data"`` 并写负缓存。"""
    place = make_place(session, name="")
    wiki_calls = stub_wiki(monkeypatch, WIKI_HIT)
    amap_calls = stub_amap(monkeypatch, AMAP_IMAGES)
    result = place_media.fetch_media_for_place(place, environ=ENV, session=session)
    assert (result["source"], result["reason"]) == ("none", "no_data")
    assert wiki_calls == [] and amap_calls == []


# --------------------------------------------------------------------------- #
# services.place_media:缓存(命中 7 天 / 负缓存 6 小时 / TTL=0)
# --------------------------------------------------------------------------- #


def test_cache_hit_is_zero_network(session, monkeypatch) -> None:
    """第二次查询命中缓存:**零网络**(两个源都不再被调用),``cached=True``。"""
    place = make_place(session)
    stub_wiki(monkeypatch, wikimedia.empty_result())
    amap_calls = stub_amap(monkeypatch, AMAP_IMAGES)
    first = place_media.fetch_media_for_place(place, environ=ENV, session=session)
    assert first["cached"] is False and len(amap_calls) == 1

    second = place_media.fetch_media_for_place(place, environ=ENV, session=session)
    assert len(amap_calls) == 1                     # 没有第二次触网
    assert second["cached"] is True
    assert second["images"] == first["images"]
    assert second["fetched_at"] == first["fetched_at"]
    assert second["source"] == first["source"] == "amap"


def test_miss_cache_expires_after_six_hours(session, monkeypatch) -> None:
    """负缓存(source=none)6 小时内命中;超 6 小时重新触网。"""
    place = make_place(session)
    stub_wiki(monkeypatch, wikimedia.empty_result())
    amap_calls = stub_amap(monkeypatch, [])
    assert place_media.fetch_media_for_place(place, environ=ENV, session=session)["source"] == "none"
    assert len(amap_calls) == 1

    age_media_row(session, place.id, seconds=place_media.MEDIA_MISS_TTL_DEFAULT_S - 60)
    assert place_media.fetch_media_for_place(place, environ=ENV, session=session)["cached"] is True
    assert len(amap_calls) == 1

    age_media_row(session, place.id, seconds=place_media.MEDIA_MISS_TTL_DEFAULT_S + 60)
    assert place_media.fetch_media_for_place(place, environ=ENV, session=session)["cached"] is False
    assert len(amap_calls) == 2


def test_hit_cache_expires_after_seven_days(session, monkeypatch) -> None:
    """有图的行按 7 天 TTL:过期后重新触网,新结果覆盖旧行(仍只有一行)。"""
    place = make_place(session)
    stub_wiki(monkeypatch, wikimedia.empty_result())
    stub_amap(monkeypatch, AMAP_IMAGES)
    assert place_media.fetch_media_for_place(place, environ=ENV, session=session)["source"] == "amap"

    age_media_row(session, place.id, seconds=place_media.MEDIA_TTL_DEFAULT_S - 60)
    assert place_media.fetch_media_for_place(place, environ=ENV, session=session)["cached"] is True

    age_media_row(session, place.id, seconds=place_media.MEDIA_TTL_DEFAULT_S + 60)
    stub_amap(monkeypatch, [])
    refreshed = place_media.fetch_media_for_place(place, environ=ENV, session=session)
    assert refreshed["cached"] is False
    assert (refreshed["source"], refreshed["reason"]) == ("none", "no_data")
    assert session.query(models.PlaceMedia).count() == 1


def test_ttl_zero_never_hits_cache(session, monkeypatch) -> None:
    """``WHERE2GO_PLACE_MEDIA_TTL_S=0`` → 缓存永不当命中(每次都触网)。"""
    place = make_place(session)
    stub_wiki(monkeypatch, wikimedia.empty_result())
    amap_calls = stub_amap(monkeypatch, AMAP_IMAGES)
    env = {**ENV, place_media.ENV_MEDIA_TTL_S: "0"}
    assert place_media.fetch_media_for_place(place, environ=env, session=session)["cached"] is False
    assert place_media.fetch_media_for_place(place, environ=env, session=session)["cached"] is False
    assert len(amap_calls) == 2
    assert place_media.media_ttl_s(env) == 0
    assert place_media.media_ttl_s({**ENV, place_media.ENV_MEDIA_TTL_S: "abc"}) == 604800
    assert place_media.media_miss_ttl_s(ENV) == 21600


def test_max_images_env_caps_result(session, monkeypatch) -> None:
    """``WHERE2GO_PLACE_MEDIA_MAX_IMAGES=2`` → 单 POI 只留 2 张(高德优先,截断后不算 mixed)。"""
    place = make_place(session)
    stub_wiki(monkeypatch, WIKI_HIT)
    stub_amap(monkeypatch, AMAP_IMAGES)
    env = {**ENV, place_media.ENV_MEDIA_MAX_IMAGES: "2"}
    result = place_media.fetch_media_for_place(place, environ=env, session=session)
    assert [item["url"] for item in result["images"]] == AMAP_URLS
    assert result["source"] == "amap"
    assert place_media.max_images(env) == 2
    assert place_media.max_images({**ENV, place_media.ENV_MEDIA_MAX_IMAGES: "999"}) == 20
    assert place_media.max_images({**ENV, place_media.ENV_MEDIA_MAX_IMAGES: "x"}) == 6


def test_fetch_media_without_session_uses_shared_engine(tmp_path, monkeypatch) -> None:
    """不给 ``session`` 时自己开短会话并 commit(夜间任务/脚本直调,不依赖 FastAPI 的 Depends)。"""
    url = f"sqlite:///{tmp_path / 'own_session.db'}"
    monkeypatch.setenv("WHERE2GO_DB_URL", url)
    engine = make_engine(url)
    init_db(engine)
    set_engine(engine)
    own = session_factory(engine)()
    try:
        place = make_place(own, osm_id=2001)
        stub_wiki(monkeypatch, wikimedia.empty_result())
        stub_amap(monkeypatch, AMAP_IMAGES)
        result = place_media.fetch_media_for_place({"id": place.id, "name": "西湖",
                                                    "lat": WEST_LAKE[0], "lng": WEST_LAKE[1]}, environ=ENV)
        assert result["cached"] is False and result["source"] == "amap"
        # 已经自己 commit 了:换一个会话也读得到
        other = session_factory(engine)()
        try:
            assert repo.get_place_media(other, place_id=place.id).source == "amap"
            age_media_row(other, place.id, seconds=0)
            assert place_media.fetch_media_for_place(
                {"id": place.id, "name": "西湖", "lat": WEST_LAKE[0], "lng": WEST_LAKE[1]},
                environ=ENV,
            )["cached"] is True
        finally:
            other.close()
    finally:
        own.close()
        set_engine(None)
        engine.dispose()


# --------------------------------------------------------------------------- #
# 批量:顺序 / 去重 / 未知 id / 单条失败不扩散
# --------------------------------------------------------------------------- #


def test_batch_keeps_input_order_and_dedupes(session, monkeypatch) -> None:
    """``items`` 严格按入参 id 顺序返回,重复 id 只回一条;第二轮全部命中缓存。"""
    first = make_place(session, osm_id=3001, name="西湖")
    second = make_place(session, osm_id=3002, name="太子湾公园")
    third = make_place(session, osm_id=3003, name="灵隐寺")
    stub_wiki(monkeypatch, wikimedia.empty_result())
    stub_amap(monkeypatch, lambda name, *a, **k: [{"url": f"https://a/{name}.jpg", "title": name}])

    items = place_media.fetch_media_for_places(session, [third.id, first.id, first.id, second.id], environ=ENV)
    assert [item["place_id"] for item in items] == [third.id, first.id, second.id]
    assert [item["images"][0]["url"] for item in items] == [
        "https://a/灵隐寺.jpg", "https://a/西湖.jpg", "https://a/太子湾公园.jpg",
    ]
    assert all(item["cached"] is False for item in items)

    again = place_media.fetch_media_for_places(session, [first.id, second.id, third.id], environ=ENV)
    assert [item["place_id"] for item in again] == [first.id, second.id, third.id]
    assert all(item["cached"] is True for item in again)


def test_batch_unknown_id_is_no_data(session, monkeypatch) -> None:
    """库里没有这个 id → ``reason="no_data"``、``fetched_at=None``,不触网也不落库。"""
    stub_wiki(monkeypatch, wikimedia.empty_result())
    stub_amap(monkeypatch, AMAP_IMAGES)
    items = place_media.fetch_media_for_places(session, [424242], environ=ENV)
    assert items == [place_media.empty_item(424242, reason="no_data")]
    assert items[0]["fetched_at"] is None
    assert session.query(models.PlaceMedia).count() == 0
    assert place_media.fetch_media_for_places(session, [], environ=ENV) == []
    assert place_media.fetch_media_for_places(session, ["x", None], environ=ENV) == []


def test_batch_single_failure_does_not_spread(session, monkeypatch) -> None:
    """第 2 条炸了 → 该条 ``reason="error"``,前后两条照常出图且已 commit(rollback 不牵连)。"""
    first = make_place(session, osm_id=4001, name="西湖")
    broken = make_place(session, osm_id=4002, name="炸的那条")
    third = make_place(session, osm_id=4003, name="灵隐寺")
    stub_wiki(monkeypatch, wikimedia.empty_result())
    stub_amap(monkeypatch, lambda name, *a, **k: [{"url": f"https://a/{name}.jpg", "title": name}])

    real = place_media.fetch_media_for_place

    def flaky(place: Any, **kwargs: Any) -> Any:
        if place_media.place_id_of(place) == broken.id:
            raise RuntimeError("boom")
        return real(place, **kwargs)

    monkeypatch.setattr(place_media, "fetch_media_for_place", flaky)
    items = place_media.fetch_media_for_places(session, [first.id, broken.id, third.id], environ=ENV)
    assert [item["place_id"] for item in items] == [first.id, broken.id, third.id]
    assert (items[1]["source"], items[1]["reason"]) == ("none", "error")
    assert items[0]["images"] and items[2]["images"]
    assert repo.get_place_media(session, place_id=first.id) is not None
    assert repo.get_place_media(session, place_id=third.id) is not None
    assert repo.get_place_media(session, place_id=broken.id) is None


# --------------------------------------------------------------------------- #
# GET /api/places/media
# --------------------------------------------------------------------------- #


@pytest.fixture()
def media_env(monkeypatch) -> None:
    """API 用例走 ``os.environ``(路由不传 ``environ=``),所以把 key 与节流都设成测试值。"""
    monkeypatch.setenv(amap.ENV_AMAP_KEY, ENV[amap.ENV_AMAP_KEY])
    monkeypatch.setenv(amap.ENV_MIN_INTERVAL, "0")


def test_api_media_returns_items_in_input_order(session, api_client, media_env, monkeypatch) -> None:
    first = make_place(session, osm_id=5001, name="西湖")
    second = make_place(session, osm_id=5002, name="太子湾公园")
    stub_wiki(monkeypatch, wikimedia.empty_result())
    stub_amap(monkeypatch, lambda name, *a, **k: [{"url": f"https://a/{name}.jpg", "title": name}])

    status, payload = api_client("GET", "/api/places/media", query=f"place_ids={second.id},{first.id}")
    assert status == 200
    assert [item["place_id"] for item in payload["items"]] == [second.id, first.id]
    assert payload["count"] == 2
    assert payload["items"][0]["source"] == "amap"
    assert set(payload["items"][0]) == {
        "place_id", "source", "images", "page_url", "reason", "cached", "fetched_at",
    }
    assert "高德" in payload["note"] and "维基" in payload["note"]
    assert "no_key" in payload["note"]


def test_api_media_second_call_hits_cache(session, api_client, media_env, monkeypatch) -> None:
    """第二次请求读库、零触网(``cached=true``),图源函数只被调过一次。"""
    place = make_place(session, osm_id=5101)
    stub_wiki(monkeypatch, wikimedia.empty_result())
    amap_calls = stub_amap(monkeypatch, AMAP_IMAGES)

    first_status, first = api_client("GET", "/api/places/media", query=f"place_ids={place.id}")
    second_status, second = api_client("GET", "/api/places/media", query=f"place_ids={place.id}")
    assert (first_status, second_status) == (200, 200)
    assert first["items"][0]["cached"] is False
    assert second["items"][0]["cached"] is True
    assert second["items"][0]["images"] == first["items"][0]["images"]
    assert len(amap_calls) == 1


def test_api_media_batch_over_limit_is_400(session, api_client, media_env) -> None:
    ids = ",".join(str(index) for index in range(1, 22))
    status, payload = api_client("GET", "/api/places/media", query=f"place_ids={ids}")
    assert status == 400
    assert "一次最多 20 个" in payload["detail"]
    assert "21" in payload["detail"]


def test_api_media_batch_at_limit_is_ok(session, api_client, media_env, monkeypatch) -> None:
    """正好 20 个不报错(库里没这些 id → 每条 no_data,不触网)。"""
    stub_wiki(monkeypatch, wikimedia.empty_result())
    stub_amap(monkeypatch, AMAP_IMAGES)
    ids = ",".join(str(index) for index in range(1, 21))
    status, payload = api_client("GET", "/api/places/media", query=f"place_ids={ids}")
    assert status == 200
    assert payload["count"] == 20
    assert all(item["reason"] == "no_data" and item["source"] == "none" for item in payload["items"])


@pytest.mark.parametrize("query,fragment", [
    ("", "缺少必要参数"),
    ("place_ids=%20%20", "解析后为空"),
    ("place_ids=,,", "解析后为空"),
    ("place_ids=1,abc", "只能是逗号分隔的正整数"),
    ("place_ids=-3", "只能是逗号分隔的正整数"),
    ("place_ids=0", "必须是正整数"),
])
def test_api_media_bad_place_ids_is_400(session, api_client, media_env, query: str, fragment: str) -> None:
    status, payload = api_client("GET", "/api/places/media", query=query)
    assert status == 400
    assert fragment in payload["detail"]


def test_api_media_route_is_registered(session, api_client, media_env, monkeypatch) -> None:
    """路由挂在 ``/api`` 前缀下(不是 404),中文逗号(浏览器会百分号编码)与重复 id 也能解析。"""
    place = make_place(session, osm_id=5201)
    stub_wiki(monkeypatch, wikimedia.empty_result())
    stub_amap(monkeypatch, AMAP_IMAGES)
    status, payload = api_client(
        "GET", "/api/places/media", query=f"place_ids={place.id}%EF%BC%8C{place.id}"
    )
    assert status == 200
    assert payload["count"] == 1


def test_api_media_direct_call_handles_fieldinfo(session) -> None:
    """单测/脚本直调路由函数时 ``Query(None)`` 是 FieldInfo → 归一成"没给" → 400 中文。"""
    exc = expect_error(lambda: places_api.places_media(session=session), HTTPException, "缺少必要参数")
    assert exc.status_code == 400
    over = ",".join(str(index) for index in range(1, 22))
    with pytest.raises(HTTPException) as caught:
        places_api.places_media(place_ids=over, session=session)
    assert caught.value.status_code == 400
    assert places_api._media_ids("3，1, 2 ,,3") == [3, 1, 2]
    assert places_api._optional_text(None) is None
