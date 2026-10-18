"""TASK-21 单测:境外轨 POI 检索链(``osm_overseas.py`` 复活 + ``region`` 分支)。

全程不触网:

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,任何偷偷联网当场失败;
* Overpass 用 :class:`FakeSession` 替身(照 ``test_data_sources.py`` 的考古样板),
  断言**查询语句**(单圆 ``around`` / 环形差集 ``( … ; - … ; )`` / ``out center N`` 配额)
  与**真实响应形状**下的解析;
* ``place_loader`` 的抓取用替身注入(``fetcher=`` 或 monkeypatch ``default_fetcher*``),
  DB 用 ``tmp_path`` 下的临时 SQLite,不碰 ``backend/data/``。

覆盖契约 §2 的 1~5 组(第 6 组前端静态断言在 ``test_frontend_routes.py``)。

运行:``cd backend && ../.venv/bin/python -m pytest -q``
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from types import SimpleNamespace
from typing import Any, Optional

import pytest
import requests
from fastapi import HTTPException

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import data_sources as ds  # noqa: E402
from app.api import places as places_api  # noqa: E402
from data_sources import osm_overseas  # noqa: E402
from data_sources._common import DEFAULT_SOURCE_PROXY, PROXY_ENV, PROXY_OFF, apply_proxy_policy, proxy_mode  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db import repository as repo  # noqa: E402
from db.models import (  # noqa: E402
    AMAP_SOURCE,
    OSM_SOURCE,
    SEED_SOURCE,
    SOURCE_TAG,
    Place,
    amap_osm_id,
    place_source,
)
from services import classify, place_loader, region_gate  # noqa: E402
from services.bands import find_band  # noqa: E402

# --------------------------------------------------------------------------- #
# 测试替身(样板:git show 2845eb1^:backend/test_data_sources.py)
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

    @property
    def query(self) -> str:
        """Overpass 用 POST ``data={"data": <QL>}`` 发查询,这里取出语句本体。"""
        return str((self.data or {}).get("data") or "")


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


class ExplodingFetcher:
    """已入库分段再调它就说明"又触网了",直接失败。"""

    def __call__(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("已入库的 (城市, band) 不应再次抓取")


# --------------------------------------------------------------------------- #
# 样本:因特拉肯(2026-10-10 实测起点,WGS-84)。Overpass 实测特征照旧 ——
# node 带 lat/lon;way/relation 只有 `out center` 时才有 center;部分地物没有 name。
# --------------------------------------------------------------------------- #

INTERLAKEN = {"lat": 46.6863, "lng": 7.8632}
KM_PER_DEGREE = 111.32


def point_at(km: float) -> tuple[float, float]:
    """起点正北 ``km`` 公里处的 (lat, lng)。"""
    return round(INTERLAKEN["lat"] + km / KM_PER_DEGREE, 7), INTERLAKEN["lng"]


def node(osm_id: int, km: float, tags: dict[str, Any]) -> dict[str, Any]:
    lat, lng = point_at(km)
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lng, "tags": tags}


def way(osm_id: int, km: float, tags: dict[str, Any]) -> dict[str, Any]:
    """way/relation:只有 ``out center`` 才带 center(解析要兼容)。"""
    lat, lng = point_at(km)
    return {"type": "way", "id": osm_id, "center": {"lat": lat, "lon": lng}, "tags": tags}


def payload(*elements: dict[str, Any]) -> dict[str, Any]:
    return {"version": 0.6, "generator": "Overpass API 0.7.62.11", "elements": list(elements)}


PEAK = node(101, 3.0, {"natural": "peak", "name": "Harder Kulm", "ele": "1322"})
CASTLE = way(202, 6.0, {"historic": "castle", "tourism": "attraction", "name": "Schloss Interlaken"})
SKI = node(303, 9.0, {"piste:type": "downhill", "sport": "ski", "name": "Beatenberg Piste"})
PARK = node(404, 12.0, {"leisure": "park", "name": "Höheweg Park"})
UNNAMED = node(505, 15.0, {"natural": "peak", "ele": "900"})
CAFE = node(606, 18.0, {"amenity": "cafe", "cuisine": "coffee_shop", "name": "Cafe Läderach"})

OVERPASS_PAYLOAD = payload(PEAK, CASTLE, SKI, PARK, UNNAMED, CAFE)

# 一行一组的迷你分组(断言查询语句时用,免得六组语句糊在一起看不清)
MINI_GROUPS = [
    {"category": "自然风光", "group": "自然风光", "budget": 30,
     "element_types": "nwr", "tags": ({"natural": "peak"},)},
    {"category": "小城人文美食", "group": "人文古迹/景点", "budget": 12,
     "element_types": "nw", "tags": ({"historic": None},)},
]


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底:任何 requests 调用都视为测试失败(本套单测必须纯 mock)。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture(autouse=True)
def serial_ring(monkeypatch: pytest.MonkeyPatch) -> None:
    """环形差集钉在**串行**口径(``WORKERS=1``):请求条数与先后才可断言。"""
    monkeypatch.setenv(osm_overseas.ENV_WORKERS, "1")


@pytest.fixture()
def session(tmp_path):
    """每个用例一个独立的临时 SQLite 库。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'osm_overseas_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


@pytest.fixture()
def osm_fetcher(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """把境外轨的**默认**抓取换成替身(记录 groups/fetch_rounds,返回 Overpass 形状的行)。"""
    calls: list[dict[str, Any]] = []
    rows = [
        {"name": "Harder Kulm", "lat": point_at(3.0)[0], "lng": INTERLAKEN["lng"],
         "tags": {"natural": "peak"}, "osm_type": "node", "osm_id": 101},
        {"name": "Beatenberg Piste", "lat": point_at(9.0)[0], "lng": INTERLAKEN["lng"],
         "tags": {"piste:type": "downhill", "sport": "ski"}, "osm_type": "node", "osm_id": 303},
    ]

    def fake(lat: float, lng: float, band: dict[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
        calls.append({"lat": lat, "lng": lng, "band": band["key"], **kwargs})
        return [dict(row) for row in rows]

    monkeypatch.setattr(place_loader, "default_fetcher_osm", fake)
    return SimpleNamespace(calls=calls, rows=rows, fn=fake)


@pytest.fixture()
def amap_fetcher(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """境内轨的**默认**抓取替身:返回归一化高德 POI,用来断言"没走错轨"。"""
    calls: list[dict[str, Any]] = []

    def fake(lat: float, lng: float, band: dict[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
        calls.append({"lat": lat, "lng": lng, "band": band["key"], **kwargs})
        # 行摆在**请求原点**正北 60 km:两轨(上海 / 因特拉肯)都落在 50_100 环内
        return [{
            "id": "B0FFHPEAK1", "name": "远山", "lat": round(float(lat) + 60 / KM_PER_DEGREE, 7),
            "lng": float(lng), "type": "风景名胜;旅游景点", "typecode": "110000",
            "address": "", "cityname": "", "adname": "", "distance_m": 60000,
        }]

    monkeypatch.setattr(place_loader, "default_fetcher", fake)
    return SimpleNamespace(calls=calls, fn=fake)


def fake_geocoder(city: str) -> dict[str, Any]:
    """地理编码替身:固定返回因特拉肯坐标(不触网)。"""
    return {"lat": INTERLAKEN["lat"], "lng": INTERLAKEN["lng"],
            "display_name": f"{city}, Schweiz"}


def exploding_geocoder(city: str) -> dict[str, Any]:
    raise AssertionError(f"已入库/已给坐标时不应触发地理编码:{city}")


def load_overseas(session, *, city="因特拉肯", band="0_50", fetcher=None,
                  lat=INTERLAKEN["lat"], lng=INTERLAKEN["lng"],
                  region=region_gate.REGION_OVERSEAS, **kwargs):
    """便捷封装:境外轨 ``load_segment``(默认给好起点坐标、关简介回填)。"""
    return place_loader.load_segment(
        session,
        city=city,
        band=band,
        lat=lat,
        lng=lng,
        fetcher=fetcher,
        geocoder=fake_geocoder,
        intros=False,
        region=region,
        **kwargs,
    )


# =========================================================================== #
# ① osm_overseas 恢复完整性(git 历史 overpass.py → 境外轨专用模块)
# =========================================================================== #


def test_osm_overseas_restores_the_full_overpass_surface() -> None:
    """恢复面:查询构造 / 解析 / 客户端方法 / haversine 一个不少,源名与代理口径不变。"""
    for name in (
        "build_query", "build_grouped_query", "build_grouped_ring_query", "parse_element",
        "parse_places", "tags_to_selectors", "normalize_element_types", "haversine_km",
        "OverpassClient", "OverpassRuntimeError", "default_client", "nearby_places",
        "ring_workers", "resolve_group_limit", "runtime_error_remark",
    ):
        assert hasattr(osm_overseas, name), f"osm_overseas 缺 {name}(恢复不完整)"
    for name in ("nearby_places_grouped", "nearby_places_ring", "execute"):
        assert hasattr(osm_overseas.OverpassClient, name), f"OverpassClient 缺 {name}"
    # §1.1:SOURCE_NAME **保留原值**(_common 的代理表按源名小写查)
    assert osm_overseas.SOURCE_NAME == "Overpass"
    # 2026-10-18:直连 502/不可达 → 口径从 off 改 env(详见 _common.DEFAULT_SOURCE_PROXY 注释)
    assert proxy_mode(osm_overseas.SOURCE_NAME) == PROXY_ENV
    # 端点链 = 阶段1 实测口径(overpass-api.de / z.overpass-api.de / maps.mail.ru)
    assert osm_overseas.DEFAULT_ENDPOINT == "https://overpass-api.de/api/interpreter"
    assert osm_overseas.FALLBACK_ENDPOINTS == (
        "https://z.overpass-api.de/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    )
    assert osm_overseas.__doc__.startswith("Overpass API(境外轨)"), "docstring 首行要标明境外轨"
    assert "TASK-21" in osm_overseas.__doc__ and "amap.py" in osm_overseas.__doc__


def test_osm_overseas_stays_out_of_the_package_export_surface() -> None:
    """境外轨模块**不进**包导出面:TASK-9c 的退役断言(境内不许再碰 Overpass)保持成立。"""
    for retired in ("OverpassClient", "OVERPASS_ENDPOINT", "OVERPASS_FALLBACK_ENDPOINTS",
                    "nearby_places", "route", "OsrmClient"):
        assert retired not in ds.__all__, f"{retired} 不该回到 data_sources.__all__"
        assert not hasattr(ds, retired), f"{retired} 不该被包级导出"
    assert "osm_overseas" not in ds.__all__


def test_overpass_proxy_policy_is_env() -> None:
    """§1.1(2026-10-18 更新):``"overpass": PROXY_ENV`` —— 国内直连 502/不可达,
    走 NAS 代理 30.9s 可用(实测),9/27「代理 504」口径已过时;无代理环境
    trust_env 下等价直连,行为不劣化。"""
    assert DEFAULT_SOURCE_PROXY["overpass"] == PROXY_ENV
    session = SimpleNamespace(trust_env=False, proxies={"http": "http://127.0.0.1:7892"})
    assert apply_proxy_policy(session, "overpass") is None, "env = 沿用环境变量(trust_env=True, proxies 清空)"
    assert session.trust_env is True and session.proxies == {}
    # 仍可用 WHERE2GO_PROXY_OVERPASS 覆盖(与其他源同一口径)
    previous = os.environ.get("WHERE2GO_PROXY_OVERPASS")
    os.environ["WHERE2GO_PROXY_OVERPASS"] = "off"
    try:
        assert proxy_mode("overpass") == "off"
    finally:
        if previous is None:
            os.environ.pop("WHERE2GO_PROXY_OVERPASS", None)
        else:
            os.environ["WHERE2GO_PROXY_OVERPASS"] = previous


def test_grouped_query_is_a_single_circle_union_with_per_group_quota() -> None:
    """单圆并集:一次请求放多组 ``( 选择器 ); out center N;``,``require_name`` 挂 ``["name"]``。"""
    query = osm_overseas.build_grouped_query(
        INTERLAKEN["lat"], INTERLAKEN["lng"], 50_000, MINI_GROUPS, query_timeout=120
    )
    assert query == (
        "[out:json][timeout:120];\n"
        "(\n"
        '  node["natural"="peak"]["name"](around:50000,46.686300,7.863200);\n'
        '  way["natural"="peak"]["name"](around:50000,46.686300,7.863200);\n'
        '  relation["natural"="peak"]["name"](around:50000,46.686300,7.863200);\n'
        ");\n"
        "out center 30;\n"
        "(\n"
        '  node["historic"]["name"](around:50000,46.686300,7.863200);\n'
        '  way["historic"]["name"](around:50000,46.686300,7.863200);\n'
        ");\n"
        "out center 12;\n"
    )
    assert query.count("(around:50000") == 5, "两组 5 条选择器 × 元素类型展开都在同一次请求里"


def test_grouped_ring_query_subtracts_the_inner_circle() -> None:
    """环形差集(TASK-1d 口径):``( 上限圆; - 下限圆; ); out center N;``,配额只花在环内。"""
    query = osm_overseas.build_grouped_ring_query(
        INTERLAKEN["lat"], INTERLAKEN["lng"], 100_000, 50_000, MINI_GROUPS[:1], query_timeout=240
    )
    assert query == (
        "[out:json][timeout:240];\n"
        "(\n"
        "  (\n"
        '    node["natural"="peak"]["name"](around:100000,46.686300,7.863200);\n'
        '    way["natural"="peak"]["name"](around:100000,46.686300,7.863200);\n'
        '    relation["natural"="peak"]["name"](around:100000,46.686300,7.863200);\n'
        "  );\n"
        "  -\n"
        "  (\n"
        '    node["natural"="peak"]["name"](around:50000,46.686300,7.863200);\n'
        '    way["natural"="peak"]["name"](around:50000,46.686300,7.863200);\n'
        '    relation["natural"="peak"]["name"](around:50000,46.686300,7.863200);\n'
        "  );\n"
        ");\n"
        "out center 30;\n"
    )
    assert query.count("around:100000") == query.count("around:50000") == 3, "上下限圆各扫一遍"


def test_ring_query_falls_back_to_single_circle_without_inner_radius() -> None:
    """``inner_radius_m`` 为 0 → 退化成单圆,输出与 ``build_grouped_query`` 逐字一致。"""
    plain = osm_overseas.build_grouped_query(
        INTERLAKEN["lat"], INTERLAKEN["lng"], 50_000, MINI_GROUPS, query_timeout=120
    )
    for inner in (0, 0.0, None):
        assert osm_overseas.build_grouped_ring_query(
            INTERLAKEN["lat"], INTERLAKEN["lng"], 50_000, inner, MINI_GROUPS, query_timeout=120
        ) == plain


def test_parse_places_sorts_by_distance_and_keeps_osm_identity() -> None:
    """``with_id=True`` 的行形状:{lat,lng,name,tags,osm_type,osm_id};按距离由近及远。"""
    rows = osm_overseas.parse_places(
        payload(PEAK, CASTLE, UNNAMED), INTERLAKEN["lat"], INTERLAKEN["lng"],
        limit=None, with_id=True,
    )
    assert [row["name"] for row in rows] == ["Harder Kulm", "Schloss Interlaken", ""]
    assert set(rows[0]) == {"lat", "lng", "name", "tags", "osm_type", "osm_id"}
    assert (rows[0]["osm_type"], rows[0]["osm_id"]) == ("node", 101)
    # way 的坐标来自 `out center`,与 node 同一形状
    assert (rows[1]["osm_type"], rows[1]["osm_id"]) == ("way", 202)
    assert rows[1]["lat"] == round(point_at(6.0)[0], 7)
    # require_name 在服务端/解析层滤掉无名地物(阶段1 口径)
    named = osm_overseas.parse_places(
        payload(PEAK, UNNAMED), INTERLAKEN["lat"], INTERLAKEN["lng"],
        limit=None, require_name=True, with_id=True,
    )
    assert [row["osm_id"] for row in named] == [101]


def test_nearby_places_grouped_posts_one_query_and_parses_the_answer() -> None:
    """客户端单圆路径:一次 POST(语句在 ``data``),回来的是带 OSM 身份的排序行。"""
    session = FakeSession(FakeResponse(OVERPASS_PAYLOAD))
    client = osm_overseas.OverpassClient(session=session)
    rows = client.nearby_places_grouped(
        INTERLAKEN["lat"], INTERLAKEN["lng"], 50_000, MINI_GROUPS, require_name=True, with_id=True
    )
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call.method == "POST" and call.url == osm_overseas.DEFAULT_ENDPOINT
    assert call.headers.get("User-Agent") == osm_overseas.__dict__["USER_AGENT"]
    assert "around:50000" in call.query and "out center 30" in call.query
    assert '["name"]' in call.query, "require_name=True → 每个选择器都挂 [\"name\"]"
    assert [row["name"] for row in rows] == [
        "Harder Kulm", "Schloss Interlaken", "Beatenberg Piste", "Höheweg Park", "Cafe Läderach",
    ]
    assert all({"osm_type", "osm_id"} <= set(row) for row in rows)


def test_nearby_places_ring_sends_one_difference_query_per_group() -> None:
    """环形路径(串行口径):每组一次差集请求,顺序 = 组序,合并后仍按距离排序。"""
    session = FakeSession(FakeResponse(payload(PEAK)), FakeResponse(payload(CASTLE)))
    client = osm_overseas.OverpassClient(session=session)
    rows = client.nearby_places_ring(
        INTERLAKEN["lat"], INTERLAKEN["lng"], 100_000, 50_000, MINI_GROUPS,
        require_name=True, with_id=True,
    )
    assert len(session.calls) == 2, "两组 = 两次请求(差集不能塞进一次)"
    for call, budget in zip(session.calls, (30, 12)):
        assert "around:100000" in call.query and "around:50000" in call.query
        assert f"out center {budget};" in call.query
        assert "\n  -\n" in call.query, "每组语句都是 ( 上限圆; - 下限圆; )"
    assert [row["osm_id"] for row in rows] == [101, 202], "合并后按离起点由近及远"


def test_nearby_places_ring_delegates_to_single_circle_when_band_starts_at_zero() -> None:
    """下限 = 0 时没有内圈可减:委托单圆并集(一次请求,语句里不出现差集)。"""
    session = FakeSession(FakeResponse(OVERPASS_PAYLOAD))
    client = osm_overseas.OverpassClient(session=session)
    rows = client.nearby_places_ring(
        INTERLAKEN["lat"], INTERLAKEN["lng"], 50_000, 0, MINI_GROUPS, require_name=True, with_id=True
    )
    assert len(session.calls) == 1 and "\n  -\n" not in session.calls[0].query
    assert len(rows) == 5, "require_name 滤掉无名山峰,其余全收"


def test_ring_failure_is_loud_not_silent() -> None:
    """某组始终拿不到数 → 抛 :class:`DataSourceError`(不写残缺水位),错误里带组名。"""
    busy = FakeResponse(status_code=504, text="<html><body>The server is probably too busy.</body></html>")
    session = FakeSession(busy)
    client = osm_overseas.OverpassClient(session=session, retries=1, retry_backoff_s=0.0)
    with pytest.raises(ds.DataSourceError) as caught:
        client.nearby_places_ring(
            INTERLAKEN["lat"], INTERLAKEN["lng"], 100_000, 50_000, MINI_GROUPS[:1], with_id=True
        )
    text = str(caught.value)
    assert "自然风光" in text and "取数失败" in text
    assert client.used_endpoint is None


# =========================================================================== #
# ② default_fetcher_osm(境外轨的默认抓取)
# =========================================================================== #


def test_default_fetcher_osm_uses_single_circle_for_the_zero_band() -> None:
    """``0_50``(下限 0)→ 单圆并集:半径 = band 上限,一次请求。"""
    session = FakeSession(FakeResponse(OVERPASS_PAYLOAD))
    rows = place_loader.default_fetcher_osm(
        INTERLAKEN["lat"], INTERLAKEN["lng"], find_band("0_50"),
        groups=MINI_GROUPS, session=session,
    )
    assert len(session.calls) == 1
    assert "around:50000" in session.calls[0].query and "\n  -\n" not in session.calls[0].query
    assert rows and rows[0]["name"] == "Harder Kulm"


def test_default_fetcher_osm_uses_ring_difference_for_far_bands() -> None:
    """下限 > 0 → 环形差集:每组一次请求,上限/下限半径都来自 band。"""
    session = FakeSession(FakeResponse(payload(PEAK)))
    rows = place_loader.default_fetcher_osm(
        INTERLAKEN["lat"], INTERLAKEN["lng"], find_band("200_300"),
        groups=MINI_GROUPS, session=session,
    )
    assert len(session.calls) == 2
    for call in session.calls:
        assert "around:300000" in call.query and "around:200000" in call.query
        assert "\n  -\n" in call.query
    assert [row["osm_id"] for row in rows] == [101, 101]


def test_default_fetcher_osm_defaults_to_the_osm_groups_and_passes_quota_through() -> None:
    """缺省组 = :data:`classify.OSM_SEARCH_GROUPS`;传入的配额逐组落进 ``out center N``。"""
    session = FakeSession(FakeResponse(payload(PEAK)))
    scaled = place_loader.scale_search_groups(30, place_loader.OSM_SEARCH_GROUPS)
    place_loader.default_fetcher_osm(
        INTERLAKEN["lat"], INTERLAKEN["lng"], find_band("0_50"), groups=scaled, session=session
    )
    query = session.calls[0].query
    assert len(session.calls) == 1, "单圆路径把六组塞进**一次**请求"
    for group in scaled:
        assert f"out center {group['budget']};" in query, f"{group['group']} 的配额没透传"
    # 缺省不传 groups 时用 OSM 六组(不是高德组):语句里应出现 OSM tag 选择器
    default_session = FakeSession(FakeResponse(payload(PEAK)))
    place_loader.default_fetcher_osm(
        INTERLAKEN["lat"], INTERLAKEN["lng"], find_band("0_50"), session=default_session
    )
    default_query = default_session.calls[0].query
    assert '["piste:type"]' in default_query and '["natural"~' in default_query
    assert "types=" not in default_query and "keywords" not in default_query, "不许混进高德参数"
    for group in classify.OSM_SEARCH_GROUPS:
        assert f"out center {group['budget']};" in default_query


def test_default_fetcher_osm_returns_overpass_row_shape_without_typecode() -> None:
    """返回行形状 = ``parse_places(with_id=True)``:有 OSM 身份、**没有** typecode。"""
    session = FakeSession(FakeResponse(OVERPASS_PAYLOAD))
    rows = place_loader.default_fetcher_osm(
        INTERLAKEN["lat"], INTERLAKEN["lng"], find_band("0_50"), groups=MINI_GROUPS, session=session
    )
    assert rows, "替身响应里有条目就该解析出来"
    for row in rows:
        assert set(row) == {"name", "lat", "lng", "tags", "osm_type", "osm_id"}
        assert "typecode" not in row and "id" not in row
        assert row["osm_type"] in ("node", "way", "relation")
        assert isinstance(row["osm_id"], int)
    # 距离排序照旧由 parse_places 完成(不做本地 haversine 收敛:环内过滤在服务端)
    distances = [osm_overseas.haversine_km(INTERLAKEN["lat"], INTERLAKEN["lng"], r["lat"], r["lng"])
                 for r in rows]
    assert distances == sorted(distances)


def test_default_fetcher_osm_honours_injected_environ_and_session() -> None:
    """``environ`` 决定端点(不再回落宿主 env),``session`` 原样共用(断言打在它身上)。"""
    session = FakeSession(FakeResponse(payload(PEAK)))
    place_loader.default_fetcher_osm(
        INTERLAKEN["lat"], INTERLAKEN["lng"], find_band("0_50"), groups=MINI_GROUPS,
        environ={osm_overseas.ENV_ENDPOINT: "https://overpass.internal.example.com/api"},
        session=session,
    )
    assert session.calls[0].url == "https://overpass.internal.example.com/api"
    # environ 给了但没有端点键 → 默认端点(不读宿主 os.environ)
    assert place_loader.osm_endpoint({}) == osm_overseas.DEFAULT_ENDPOINT
    assert place_loader.osm_endpoint(None) is None, "None = 交回客户端自己解析(现状口径)"


def test_default_fetcher_osm_accepts_fetch_rounds_without_grid_expansion() -> None:
    """``fetch_rounds`` 只是与高德轨对齐的形参:Overpass 有差集,不分格、不递增扩格。"""
    session = FakeSession(FakeResponse(payload(PEAK)))
    first = place_loader.default_fetcher_osm(
        INTERLAKEN["lat"], INTERLAKEN["lng"], find_band("50_100"),
        groups=MINI_GROUPS[:1], fetch_rounds=0, session=session,
    )
    second_session = FakeSession(FakeResponse(payload(PEAK)))
    second = place_loader.default_fetcher_osm(
        INTERLAKEN["lat"], INTERLAKEN["lng"], find_band("50_100"),
        groups=MINI_GROUPS[:1], fetch_rounds=5, session=second_session,
    )
    assert len(session.calls) == len(second_session.calls) == 1
    assert session.calls[0].query == second_session.calls[0].query
    assert [row["osm_id"] for row in first] == [row["osm_id"] for row in second]


def test_scale_search_groups_scales_the_given_groups_with_the_same_formula() -> None:
    """§1.2:``scale_search_groups`` 第二参给了就缩**传入组**,公式/分母(540)不变。"""
    assert place_loader.scale_search_groups(None, place_loader.OSM_SEARCH_GROUPS) is None
    scaled = place_loader.scale_search_groups(30, place_loader.OSM_SEARCH_GROUPS)
    assert [group["group"] for group in scaled] == [g["group"] for g in classify.OSM_SEARCH_GROUPS]
    assert sum(group["budget"] for group in scaled) == 30, "首轮总量 30"
    assert all(group["budget"] >= place_loader.MIN_GROUP_BUDGET for group in scaled)
    assert scaled[0]["budget"] == round(80 * 30 / place_loader.FULL_SEARCH_BUDGET)
    # 组内容(tags/element_types)原样带过去,且没有就地改掉常量
    assert scaled[0]["tags"] == classify.OSM_SEARCH_GROUPS[0]["tags"]
    assert classify.OSM_SEARCH_GROUPS[0]["budget"] == 80
    # 缺省(不传第二参)仍是高德组:现状口径零变化
    amap_scaled = place_loader.scale_search_groups(30)
    assert [group["group"] for group in amap_scaled] == [g["group"] for g in place_loader.SEARCH_GROUPS]
    assert sum(group["budget"] for group in amap_scaled) == 30
    assert "types" in amap_scaled[0] or "keywords" in amap_scaled[0]


def test_osm_search_groups_budget_matches_the_amap_budget() -> None:
    """OSM 六组合计 = 高德组合计 = 540:两轨共用同一套渐进配额口径的前提。"""
    assert sum(group["budget"] for group in classify.OSM_SEARCH_GROUPS) == 540
    assert place_loader.FULL_SEARCH_BUDGET == 540
    assert sum(group["budget"] for group in place_loader.OSM_SEARCH_GROUPS) == place_loader.FULL_SEARCH_BUDGET
    # 四分类都覆盖(「其他」不是检索目标,只在归类兜底时出现)
    assert {group["category"] for group in classify.OSM_SEARCH_GROUPS} == set(
        classify.CATEGORY_PRIORITY
    ) == set(classify.category_keys()) - {classify.UNCATEGORIZED}
    assert all("tags" in group and "element_types" in group
               for group in classify.OSM_SEARCH_GROUPS), "OSM 组必须带 tag 选择器与元素类型"


# =========================================================================== #
# ③ to_place_items(region=overseas):OSM 归类 + 原生身份 + WGS-84 标注
# =========================================================================== #


def osm_row(name: str, km: float, tags: dict[str, Any], *, osm_type="node", osm_id: int = 1) -> dict[str, Any]:
    lat, lng = point_at(km)
    return {"name": name, "lat": lat, "lng": lng, "tags": tags,
            "osm_type": osm_type, "osm_id": osm_id}


def test_overseas_items_use_native_osm_identity_not_crc32() -> None:
    """身份 = ``(node|way|relation, 原生 id)``:不 crc32、不写 ``osm_type="amap"``。"""
    items = place_loader.to_place_items(
        [osm_row("Harder Kulm", 3.0, {"natural": "peak"}, osm_id=101),
         osm_row("Schloss Interlaken", 6.0, {"historic": "castle"}, osm_type="way", osm_id=202)],
        region=region_gate.REGION_OVERSEAS,
    )
    assert [(item["osm_type"], item["osm_id"]) for item in items] == [("node", 101), ("way", 202)]
    assert items[0]["osm_id"] != amap_osm_id("101"), "境外行绝不做 crc32 压缩"
    assert all(item["osm_type"] in ("node", "way", "relation") for item in items)


def test_overseas_items_classify_by_osm_tags_with_ski_priority() -> None:
    """归类走 OSM tag 规则,优先级 滑雪 > 运动 > 人文美食 > 自然(一地只入一类)。"""
    items = {
        item["name"]: item["category"]
        for item in place_loader.to_place_items(
            [
                osm_row("雪场", 9.0, {"piste:type": "downhill", "sport": "ski"}, osm_id=1),
                osm_row("体育馆", 10.0, {"leisure": "sports_centre", "sport": "yoga"}, osm_id=2),
                osm_row("古堡", 11.0, {"historic": "castle", "tourism": "attraction"}, osm_id=3),
                osm_row("餐馆", 12.0, {"amenity": "restaurant", "cuisine": "swiss"}, osm_id=4),
                osm_row("山峰", 13.0, {"natural": "peak"}, osm_id=5),
            ],
            region="overseas",
        )
    }
    assert items["雪场"] == classify.CATEGORY_SKI, "有 sport=ski 也先按滑雪场归类"
    assert items["体育馆"] == classify.CATEGORY_SPORT
    assert items["古堡"] == classify.CATEGORY_CULTURE and items["餐馆"] == classify.CATEGORY_CULTURE
    assert items["山峰"] == classify.CATEGORY_NATURE


def test_overseas_items_tag_coord_system_and_derive_osm_source() -> None:
    """``tags`` = 原始 tag + ``coord_system="WGS-84"``,且**不写 source 键** → 派生「OSM」。"""
    items = place_loader.to_place_items(
        [osm_row("Harder Kulm", 3.0, {"natural": "peak", "ele": "1322"}, osm_id=101)],
        region="overseas",
    )
    tags = items[0]["tags"]
    assert tags["coord_system"] == "WGS-84"
    assert tags["natural"] == "peak" and tags["ele"] == "1322", "原始 tag 原样保留"
    assert "source" not in tags and "amap_id" not in tags and "typecode" not in tags
    assert place_source(tags) == OSM_SOURCE, "无 source 键 → place_source 派生「OSM」"


def test_overseas_items_dedupe_by_osm_identity_and_merge_tags() -> None:
    """去重键 ``(osm_type, osm_id)``:同一实体被多组命中只留一行,tag 取并集后再归类。"""
    items = place_loader.to_place_items(
        [
            osm_row("Beatenberg", 9.0, {"tourism": "attraction"}, osm_id=303),
            osm_row("Beatenberg", 9.0, {"piste:type": "downhill"}, osm_id=303),
        ],
        region="overseas",
    )
    assert len(items) == 1
    assert items[0]["tags"]["piste:type"] == "downhill" and items[0]["tags"]["tourism"] == "attraction"
    assert items[0]["category"] == classify.CATEGORY_SKI, "合并后才归类 → 看到全部线索"


def test_overseas_items_keep_the_amap_free_shape_for_rows_without_osm_id() -> None:
    """没有 OSM 身份的行退回 :func:`place_identity` 的指纹兜底(``("point", 负数)``)。"""
    lat, lng = point_at(4.0)
    items = place_loader.to_place_items(
        [{"name": "无名 Aussichtspunkt", "lat": lat, "lng": lng, "tags": {"tourism": "viewpoint"}}],
        region="overseas",
    )
    assert items[0]["osm_type"] == "point" and items[0]["osm_id"] < 0
    assert items[0]["tags"]["coord_system"] == "WGS-84"


def test_domestic_branch_of_to_place_items_is_untouched() -> None:
    """境内分支零变化:高德 POI → ``("amap", crc32)`` + ``tags["source"]="高德"``。"""
    amap_row = {
        "id": "B0FFHMOUNT", "name": "远山", "lat": 31.8, "lng": 121.4737,
        "type": "风景名胜;旅游景点", "typecode": "110000",
    }
    for region in (None, "", region_gate.REGION_DOMESTIC, "DOMESTIC", "garbage"):
        items = place_loader.to_place_items([dict(amap_row)], **({} if region is None else {"region": region}))
        assert items[0]["osm_type"] == "amap"
        assert items[0]["osm_id"] == amap_osm_id("B0FFHMOUNT")
        assert items[0]["tags"]["source"] == "高德" and items[0]["tags"]["amap_id"] == "B0FFHMOUNT"
        assert "coord_system" not in items[0]["tags"]
        assert items[0]["category"] == classify.CATEGORY_NATURE
    assert place_loader.is_overseas_region("OVERSEAS ") is True
    assert place_loader.is_overseas_region(None) is False and place_loader.is_overseas_region("") is False


# =========================================================================== #
# ④ load_segment(region=overseas):入库身份 / 水位 source / 渐进 / 缓存兜底
# =========================================================================== #


def stored_places(session, *, city="因特拉肯", band="0_50") -> list[Place]:
    """库内该 (城市, band) 的行(按 osm_id 排,便于逐行断言身份)。"""
    return session.query(Place).filter(
        Place.origin_city == city, Place.band == band).order_by(Place.osm_id).all()


def test_load_segment_overseas_writes_osm_rows_and_marks_the_watermark(session, osm_fetcher) -> None:
    """境外轨抓取 → 入库行身份/tags/归类 + 水位 ``source="osm"`` + ``outcome.region``。"""
    outcome = load_overseas(session, fetcher=osm_fetcher.fn)
    assert outcome.region == region_gate.REGION_OVERSEAS
    assert outcome.source == place_loader.SOURCE_OSM and outcome.network_used is True
    assert outcome.segment["source"] == "osm"
    rows = {row["name"]: row for row in outcome.places}
    assert set(rows) == {"Harder Kulm", "Beatenberg Piste"}
    peak = rows["Harder Kulm"]
    assert (peak["osm_type"], peak["osm_id"]) == ("node", 101)
    assert peak["tags"]["coord_system"] == "WGS-84" and "source" not in peak["tags"]
    assert peak["source"] == OSM_SOURCE, "序列化出来的来源标注是「OSM」"
    assert peak["category"] == classify.CATEGORY_NATURE
    assert rows["Beatenberg Piste"]["category"] == classify.CATEGORY_SKI
    assert outcome.counts_by_source[OSM_SOURCE] == 2, "两行都标注成「OSM」"
    assert outcome.counts_by_source[AMAP_SOURCE] == 0, "境外轨不落高德行"
    assert outcome.written == 2
    # 入库行本体(不只是序列化结果)也照这个身份/tags
    stored = {(row.osm_type, row.osm_id): row for row in stored_places(session)}
    assert set(stored) == {("node", 101), ("node", 303)}, "身份是原生 OSM (type, id),不 crc32"
    assert stored[("node", 101)].tags["coord_system"] == "WGS-84"
    assert "source" not in stored[("node", 101)].tags
    # 距离是 load_segment 共用的 filter_to_band 复核出来的(两轨同一口径)
    assert 0 < peak["distance_km"] < 50


def test_load_segment_overseas_second_query_reads_the_db_without_network(session, osm_fetcher) -> None:
    """二次查询读库零网络:水位命中 → ``source="db"``、不再调抓取、region 照报。"""
    first = load_overseas(session, fetcher=osm_fetcher.fn)
    assert len(osm_fetcher.calls) == 1
    second = load_overseas(session, fetcher=ExplodingFetcher())
    assert second.source == place_loader.SOURCE_DB and second.network_used is False
    assert second.region == region_gate.REGION_OVERSEAS
    assert len(osm_fetcher.calls) == 1, "已入库就不许再抓"
    assert [row["id"] for row in second.places] == [row["id"] for row in first.places]
    assert second.written == 0


def test_load_segment_overseas_scales_the_osm_groups_progressively(session, osm_fetcher) -> None:
    """渐进配额:``target_total=30`` 缩的是 OSM 六组(合计 30),不是高德组。"""
    load_overseas(session, fetcher=osm_fetcher.fn, target_total=30)
    call = osm_fetcher.calls[0]
    groups = call["groups"]
    assert [group["group"] for group in groups] == [g["group"] for g in classify.OSM_SEARCH_GROUPS]
    assert sum(group["budget"] for group in groups) == 30
    assert "tags" in groups[0] and "types" not in groups[0], "缩放的是 OSM tag 组"
    assert call["fetch_rounds"] == 0
    # 第二轮(加载更多)目标总量 = 30 × (轮数 + 1),口径与境内轨一致
    second = load_overseas(session, fetcher=osm_fetcher.fn, refresh=True,
                           target_total=place_loader.progressive_target_total(1))
    assert sum(group["budget"] for group in osm_fetcher.calls[-1]["groups"]) == 60
    assert second.segment["fetch_rounds"] == 2


def test_load_segment_region_none_falls_back_to_the_origin_cache(session, osm_fetcher, amap_fetcher) -> None:
    """``region=None`` → 读 ``OriginCache.region`` 兜底(TASK-20 判定结果),不发额外网络。"""
    repo.upsert_origin_cache(
        session, city="因特拉肯", name="Interlaken, Schweiz",
        lat=INTERLAKEN["lat"], lng=INTERLAKEN["lng"], geocoder="nominatim",
        region=region_gate.REGION_OVERSEAS,
    )
    session.commit()
    outcome = place_loader.load_segment(
        session, city="因特拉肯", band="0_50",
        lat=INTERLAKEN["lat"], lng=INTERLAKEN["lng"],
        geocoder=exploding_geocoder, intros=False,
    )
    assert outcome.region == "overseas" and outcome.source == place_loader.SOURCE_OSM
    assert len(osm_fetcher.calls) == 1 and not amap_fetcher.calls, "走的是境外轨"
    assert place_loader.resolve_segment_region(session, "因特拉肯") == "overseas"


def test_load_segment_region_none_without_cache_stays_domestic(session, osm_fetcher, amap_fetcher) -> None:
    """缓存里没有行 / region 空串 / 垃圾值 → 一律 domestic(现状境内链路零影响)。"""
    assert place_loader.resolve_segment_region(session, "上海") == region_gate.REGION_DOMESTIC
    repo.upsert_origin_cache(session, city="上海", name="上海市, 中国", lat=31.2304, lng=121.4737,
                             geocoder="amap")
    session.commit()
    assert place_loader.resolve_segment_region(session, "上海") == "domestic", "空串 = 还没判出"
    outcome = place_loader.load_segment(
        session, city="上海", band="50_100", lat=31.2304, lng=121.4737,
        fetcher=amap_fetcher.fn, geocoder=exploding_geocoder, intros=False,
    )
    assert outcome.region == region_gate.REGION_DOMESTIC
    assert outcome.source == place_loader.SOURCE_AMAP and outcome.segment["source"] == "amap"
    assert not osm_fetcher.calls and len(amap_fetcher.calls) == 1
    row = outcome.places[0]
    assert row["osm_type"] == "amap" and row["osm_id"] == amap_osm_id("B0FFHPEAK1")
    assert row["tags"]["source"] == "高德" and "coord_system" not in row["tags"]
    # 显式 region 优先于缓存
    repo.upsert_origin_cache(session, city="上海", name="上海市, 中国", lat=31.2304, lng=121.4737,
                             geocoder="amap", region=region_gate.REGION_OVERSEAS)
    session.commit()
    assert place_loader.resolve_segment_region(session, "上海", "domestic") == "domestic"
    assert place_loader.resolve_segment_region(session, "上海", " overseas ") == "overseas"


def test_load_segment_overseas_region_wins_over_the_domestic_cache(session, osm_fetcher) -> None:
    """缓存写着 domestic,调用方显式 ``region=overseas`` → 走境外轨(override 优先)。"""
    repo.upsert_origin_cache(session, city="因特拉肯", name="Interlaken", lat=INTERLAKEN["lat"],
                             lng=INTERLAKEN["lng"], geocoder="nominatim",
                             region=region_gate.REGION_DOMESTIC)
    session.commit()
    outcome = load_overseas(session, fetcher=osm_fetcher.fn, region="overseas")
    assert outcome.region == "overseas" and outcome.source == place_loader.SOURCE_OSM
    assert len(osm_fetcher.calls) == 1


def test_explicit_fetcher_wins_over_the_region_branch(session, amap_fetcher) -> None:
    """``fetcher=`` 显式注入优先于 region 分支(既有单测口径不变),入库仍按境外身份。"""
    outcome = load_overseas(session, band="50_100", fetcher=amap_fetcher.fn, region="overseas")
    assert len(amap_fetcher.calls) == 1, "注入的替身照用,不换 default_fetcher_osm"
    assert outcome.region == "overseas" and outcome.source == place_loader.SOURCE_OSM
    row = outcome.places[0]
    # 高德形状的行过了境外归类:没有 OSM 身份 → 指纹兜底,绝不 crc32
    assert row["osm_type"] == "point" and row["osm_id"] < 0
    assert row["tags"]["coord_system"] == "WGS-84" and "source" not in row["tags"]


def test_load_segment_overseas_keeps_shared_pipelines(session, osm_fetcher) -> None:
    """种子/水位/环带复核/简介回填口径两轨共用:境外轨不另开一套。"""
    seeds = [{"name": "种子雪场", "lat": point_at(20.0)[0], "lng": INTERLAKEN["lng"],
              "category": classify.CATEGORY_SKI,
              # 种子口径:tags 里带 source=种子(:func:`services.seed_data.load_seeds` 归一化时打的)
              "tags": {"piste:type": "downhill", SOURCE_TAG: SEED_SOURCE},
              "intro": "人工种子"}]
    outcome = load_overseas(session, fetcher=osm_fetcher.fn, seeds=seeds)
    assert outcome.seeded == 1 and outcome.written == 3
    sources = {row["name"]: row["source"] for row in outcome.places}
    assert sources["种子雪场"] == "种子" and sources["Harder Kulm"] == OSM_SOURCE
    record = repo.get_segment(session, origin_city="因特拉肯", band="0_50")
    assert record.source == "osm" and record.fetch_rounds == 1
    assert record.origin_lat == round(INTERLAKEN["lat"], 7)
    # 环外候选(超过 band 上限)照旧被 filter_to_band 丢掉 —— 与境内轨同一段代码
    far = load_overseas(session, band="0_50", refresh=True, seeds=[],
                        fetcher=lambda lat, lng, band: [
                            dict(osm_fetcher.rows[0]),
                            {"name": "太远", "lat": point_at(80.0)[0], "lng": INTERLAKEN["lng"],
                             "tags": {"natural": "peak"}, "osm_type": "node", "osm_id": 999},
                        ])
    assert "太远" not in {row["name"] for row in far.places}


def test_load_segment_rejects_a_bad_region_value(session, osm_fetcher) -> None:
    """垃圾 ``region`` 不猜:按 domestic 走(与 region_gate 的"判不出按境内"同口径)。"""
    outcome = load_overseas(session, band="50_100", fetcher=amap_shaped_fetcher(), region="moon")
    assert outcome.region == region_gate.REGION_DOMESTIC
    assert outcome.source == place_loader.SOURCE_AMAP
    assert outcome.places[0]["osm_type"] == "amap", "垃圾 region 走的是境内高德轨"


def amap_shaped_fetcher() -> Any:
    """``region="moon"`` 会挑高德轨:给一个能跑的高德形状替身(行摆在原点正北 60 km)。"""
    def fake(lat: float, lng: float, band: dict[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
        return [{"id": "B0FFHPEAK1", "name": "远山",
                 "lat": round(float(lat) + 60 / KM_PER_DEGREE, 7), "lng": float(lng),
                 "type": "风景名胜", "typecode": "110000"}]

    return fake


# =========================================================================== #
# ⑤ API:/api/places 的 region 参数与响应键
# =========================================================================== #

LEGACY_PLACES_KEYS = {
    "origin", "band", "category", "places", "count", "total_in_db", "has_more", "fetch_rounds",
    "counts_by_category", "source", "network_used", "fetched_at", "written", "seeded",
    "counts_by_source", "intro_stats", "intro_pending", "detail_pending", "elapsed_s", "note",
}


@pytest.fixture()
def stub_load(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """替身 ``load_segment``:记录 kwargs,返回一份可配置 region 的 outcome。"""
    calls: list[dict[str, Any]] = []
    state = {"region": region_gate.REGION_DOMESTIC, "source": place_loader.SOURCE_AMAP}

    def fake(session, **kwargs: Any):
        calls.append(kwargs)
        return place_loader.SegmentOutcome(
            origin={"city": kwargs["city"], "name": kwargs["city"],
                    "lat": INTERLAKEN["lat"], "lng": INTERLAKEN["lng"]},
            band=dict(find_band(kwargs["band"])),
            places=[],
            source=state["source"],
            region=state["region"],
            counts_by_category={},
            counts_by_source={},
        )

    monkeypatch.setattr(place_loader, "load_segment", fake)
    return SimpleNamespace(calls=calls, state=state)


def call_api(session, **params: Any) -> dict[str, Any]:
    """直调 ``GET /api/places`` 路由函数(全关键字传参,与既有单测同一口径)。"""
    kwargs: dict[str, Any] = {
        "origin": "因特拉肯", "band": "0_50", "category": None, "lat": None, "lng": None,
        "refresh": False, "page_size": None, "offset": None, "more": None,
        "region": None, "intros": False, "intro_limit": None, "session": session,
    }
    kwargs.update(params)
    return places_api.list_places(**kwargs)


def test_places_response_adds_only_the_region_key(session, stub_load) -> None:
    """响应**只多一个** ``region`` 键:既有键(含 TASK-6b 分页键)逐键零变化。"""
    body = call_api(session)
    assert set(body) == LEGACY_PLACES_KEYS | {"region"}, f"键集对不上:{sorted(set(body))}"
    assert LEGACY_PLACES_KEYS <= set(body), "既有键一个不少"
    assert body["region"] == region_gate.REGION_DOMESTIC
    stub_load.state["region"] = region_gate.REGION_OVERSEAS
    stub_load.state["source"] = place_loader.SOURCE_OSM
    overseas = call_api(session)
    assert set(overseas) == set(body), "两轨的响应键集完全一致"
    assert overseas["region"] == "overseas" and overseas["source"] == "osm"


def test_places_region_override_is_passed_through_to_load_segment(session, stub_load) -> None:
    """``region=overseas`` 原样透传 ``load_segment``;缺省传 ``None``(交回缓存兜底)。"""
    call_api(session, region="overseas")
    assert stub_load.calls[-1]["region"] == "overseas"
    call_api(session)
    assert stub_load.calls[-1]["region"] is None
    call_api(session, region="domestic")
    assert stub_load.calls[-1]["region"] == "domestic"
    # 大小写/空白归一(与 /api/geocode 的 _region_override 同一个函数)
    call_api(session, region=" OVERSEAS ")
    assert stub_load.calls[-1]["region"] == "overseas"


def test_places_rejects_an_unknown_region_with_a_chinese_400(session, stub_load) -> None:
    """非法 region → **400 中文**(不是 pydantic 的 422),且一次抓取都不发起。"""
    for bad in ("moon", "境内", "oversea", "1"):
        with pytest.raises(HTTPException) as caught:
            call_api(session, region=bad)
        assert caught.value.status_code == 400
        assert str(caught.value.detail) == places_api.REGION_INVALID_MSG == "region 只支持 domestic 或 overseas"
    assert not stub_load.calls, "非法值要快速失败,不许先抓一轮"


def test_places_region_works_in_paging_mode_too(session, stub_load) -> None:
    """分页三参与 region 互不干扰:带上分页参数照样透传 region、照样回报生效值。"""
    stub_load.state["region"] = region_gate.REGION_OVERSEAS
    body = call_api(session, region="overseas", page_size="15", offset="0", more="false")
    assert body["region"] == "overseas"
    assert body["band"] == {"key": "0_50", "label": "0-50 km", "low_km": 0, "high_km": 50}
    assert body["count"] == 0 and body["has_more"] is False
    assert stub_load.calls[-1]["region"] == "overseas"
    assert stub_load.calls[-1]["target_total"] == place_loader.PROGRESSIVE_STEP


def test_places_region_validates_before_the_band_and_city_errors(session) -> None:
    """校验顺序:城市空 / 分类非法仍先报各自的 400(既有口径不变)。"""
    with pytest.raises(HTTPException) as caught:
        call_api(session, origin="   ", region="overseas")
    assert caught.value.status_code == 400 and "起点城市不能为空" in str(caught.value.detail)
    with pytest.raises(HTTPException) as caught:
        call_api(session, category="美食", region="overseas")
    assert caught.value.status_code == 400 and "未知分类" in str(caught.value.detail)
    with pytest.raises(HTTPException) as caught:
        call_api(session, band="0_10", region="overseas")
    assert caught.value.status_code == 400 and "未知距离分段" in str(caught.value.detail)


def test_places_end_to_end_overseas_round_trip(session, osm_fetcher) -> None:
    """不替身 ``load_segment`` 的真链路:境外轨冷抓 → 响应 region/source 与入库行一致。"""
    body = call_api(session, region="overseas", lat=INTERLAKEN["lat"], lng=INTERLAKEN["lng"],
                    page_size="15", offset="0", more="false")
    assert body["region"] == "overseas" and body["source"] == place_loader.SOURCE_OSM
    assert body["network_used"] is True and body["written"] == 2
    assert body["total_in_db"] == 2 and body["count"] == 2
    assert {row["name"] for row in body["places"]} == {"Harder Kulm", "Beatenberg Piste"}
    assert all(row["source"] == OSM_SOURCE for row in body["places"])
    assert all(row["tags"]["coord_system"] == "WGS-84" for row in body["places"])
    # 二次查询:读库、零网络、region 仍照报(前端据此保留境外文案)
    cached = call_api(session, region="overseas", lat=INTERLAKEN["lat"], lng=INTERLAKEN["lng"],
                      page_size="15", offset="0", more="false")
    assert cached["source"] == place_loader.SOURCE_DB and cached["region"] == "overseas"
    assert cached["network_used"] is False and cached["total_in_db"] == 2
    assert len(osm_fetcher.calls) == 1
