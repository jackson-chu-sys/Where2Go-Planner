"""TASK-6b 单测:渐进抓取(配额缩放)+ ``/api/places`` 分页(BUG-1 主修复)。

覆盖四件事,全程 mock、不触网:

1. **配额缩放**::func:`services.place_loader.scale_search_groups` 把 :data:`SEARCH_GROUPS`
   各组配额按比例缩到总量 ≈ ``target_total``(每组至少 2 条),仍然**一次**查完分组并集;
2. **轮数落库**::attr:`db.models.SegmentFetch.fetch_rounds` 每完成一轮抓取 +1,旧库缺列由
   :func:`db.base.ensure_columns` 用 ``ALTER TABLE ADD COLUMN`` 补上(轻量迁移);
3. **分页**``page_size`` / ``offset`` / ``more`` 三参 → 距离升序 +(osm_type, osm_id)
   决胜的稳定切片;越界 + ``more=true`` 且库内行数没到常规全量配额时自动扩抓一轮;
4. **兼容**:三个参数一个都不带时行为与旧版完全一致(一次抓满配额、返回全量 places)。

抓取/地理编码用替身注入(:mod:`services.place_loader` 的模块默认实现被 monkeypatch 换掉),
``no_network`` 把 :meth:`requests.Session.request` 换成抛错兜底,DB 用 ``tmp_path`` 下的临时
SQLite。运行:``cd backend && ../.venv/bin/python -m pytest -q``
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import urlencode

import pytest
import requests
from fastapi import HTTPException
from sqlalchemy import inspect, text

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import places as places_api  # noqa: E402
from app.main import app  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db import repository as repo  # noqa: E402
from db.base import get_session  # noqa: E402
from db.models import Place, SegmentFetch  # noqa: E402
from services import intro as intro_service  # noqa: E402
from services import place_loader  # noqa: E402
from services.classify import search_budget  # noqa: E402

# --------------------------------------------------------------------------- #
# 样本数据:上海为起点,沿正北方向在 50-100 km 环内等距铺点(1 度纬度 ≈ 111.32 km)
# --------------------------------------------------------------------------- #

SHANGHAI = {"lat": 31.2304, "lng": 121.4737}
ORIGIN = {"city": "上海", "name": "上海市, 中国", **SHANGHAI}
KM_PER_DEGREE = 111.32
BAND = "50_100"
FULL_BUDGET = search_budget()
# 环内铺点:起点 50.5 km、间距 0.08 km —— 540 个点也还在 100 km 以内(最远 93.6 km);
# 坐标只由 index 决定,所以第 N 轮的点集是第 N-1 轮的**超集**,正好验扩抓去重。
FIRST_KM = 50.5
STEP_KM = 0.08
# 四分类轮转:每 4 个点覆盖一次全部分类,方便断言 counts_by_category
TAG_CYCLE: tuple[dict[str, str], ...] = (
    {"natural": "peak", "ele": "309"},                  # 自然风光
    {"tourism": "attraction", "historic": "yes"},       # 小城人文美食
    {"sport": "climbing", "leisure": "sports_centre"},  # 运动
    {"piste:type": "downhill"},                         # 滑雪场
)


def point_at(
    km: float,
    *,
    name: str,
    osm_id: int,
    osm_type: str = "node",
    tags: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """构造距上海 ``km`` 公里(正北)的 Overpass 风格候选点。"""
    return {
        "osm_type": osm_type,
        "osm_id": osm_id,
        "name": name,
        "lat": round(SHANGHAI["lat"] + km / KM_PER_DEGREE, 6),
        "lng": SHANGHAI["lng"],
        "tags": dict(tags if tags is not None else TAG_CYCLE[osm_id % len(TAG_CYCLE)]),
    }


def band_point(index: int) -> dict[str, Any]:
    """环内第 ``index`` 个点(``osm_id = index + 1``)。"""
    return point_at(
        FIRST_KM + index * STEP_KM,
        name=f"环内点{index + 1:04d}",
        osm_id=index + 1,
        tags=TAG_CYCLE[index % len(TAG_CYCLE)],
    )


def band_points(count: int) -> list[dict[str, Any]]:
    return [band_point(index) for index in range(count)]


# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #


class RingFetcher:
    """Overpass 替身:**认 ``groups`` 关键字**(与 ``place_loader.default_fetcher`` 同签名)。

    默认按"配额合计 = 条数"造环内候选(每 1 配额 1 条),所以 ``calls[i]["total"]``
    直接就是这一轮要求 Overpass 取多少条;传 ``rows`` 可以改成返回固定候选。
    """

    def __init__(self, rows: Optional[list[dict[str, Any]]] = None) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        lat: float,
        lng: float,
        band: dict[str, Any],
        groups: Optional[list[dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        resolved = list(groups) if groups is not None else list(place_loader.SEARCH_GROUPS)
        self.calls.append(
            {
                "lat": lat,
                "lng": lng,
                "band": band["key"],
                "groups_arg": groups,
                "group_count": len(resolved),
                "budgets": [int(group["budget"]) for group in resolved],
                "total": sum(int(group["budget"]) for group in resolved),
                "selectors": sum(len(group["tags"]) for group in resolved),
                "groups": [group["group"] for group in resolved],
            }
        )
        if self.rows is not None:
            return [dict(row) for row in self.rows]
        return band_points(self.calls[-1]["total"])

    @property
    def count(self) -> int:
        return len(self.calls)

    @property
    def totals(self) -> list[int]:
        return [call["total"] for call in self.calls]


class LegacyFetcher:
    """老式三参替身(既有单测的签名):不认 ``groups``,加了配额缩放也不能把它调炸。"""

    def __init__(self, rows: Optional[list[dict[str, Any]]] = None) -> None:
        self.rows = band_points(4) if rows is None else rows
        self.calls: list[dict[str, Any]] = []

    def __call__(self, lat: float, lng: float, band: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append({"lat": lat, "lng": lng, "band": band["key"]})
        return [dict(row) for row in self.rows]


class ExplodingFetcher:
    """被调用就说明"不该抓的时候抓了",直接失败。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(args)
        raise AssertionError("这一路不应该再触网抓取 Overpass")

    @property
    def count(self) -> int:
        return len(self.calls)


class FixedGeocoder:
    """地理编码替身:固定返回上海坐标,并记录被调用的城市。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, city: str) -> dict[str, Any]:
        self.calls.append(city)
        return {"lat": SHANGHAI["lat"], "lng": SHANGHAI["lng"], "display_name": f"{city}市, 中国"}


class IntroRecorder:
    """LLM 简介回填替身:只记调用次数(扩抓轮不该调它)。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, session: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"filled": 0, "failed": 0, "pending": 0, "provider": "none", "reason": "no_key"}


def expect_http_error(func, status_code: int, *fragments: str) -> HTTPException:
    """断言 ``func()`` 抛出指定状态码的 HTTPException,且 detail 含全部片段。"""
    with pytest.raises(HTTPException) as caught:
        func()
    exc = caught.value
    assert exc.status_code == status_code, f"应为 HTTP {status_code},实际 {exc.status_code}"
    for fragment in fragments:
        assert fragment in str(exc.detail), f"detail 应包含 {fragment!r},实际:{exc.detail}"
    return exc


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
def no_llm_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉所有可能的 LLM key:无 key 时简介一律降级为空,不存在偷偷联网的分支。"""
    blocked = {"DEEPSEEK_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"}
    for name in list(os.environ):
        if name.startswith("WHERE2GO_LLM") or name in blocked:
            monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def session(tmp_path):
    """每个用例一个独立的临时 SQLite 库。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'progressive_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


@pytest.fixture()
def offline(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """API 层不接收 fetcher 参数,这里替换模块默认实现(仍是替身,不触网)。

    ``default_fetcher`` 的替身要**转发 groups**,否则测不出扩抓轮的配额缩放。
    """
    holder = SimpleNamespace(fetcher=RingFetcher(), geocoder=FixedGeocoder())
    monkeypatch.setattr(
        place_loader,
        "default_fetcher",
        lambda lat, lng, band, groups=None: holder.fetcher(lat, lng, band, groups=groups),
    )
    monkeypatch.setattr(place_loader, "default_geocoder", lambda city: holder.geocoder(city))
    return holder


@pytest.fixture()
def intros(monkeypatch: pytest.MonkeyPatch) -> IntroRecorder:
    """替换 LLM 简介回填入口,记录调用次数(不调真 LLM、不触网)。"""
    recorder = IntroRecorder()
    monkeypatch.setattr(intro_service, "fill_missing_intros", recorder)
    return recorder


@pytest.fixture()
def http(session):
    """完整 HTTP 链客户端(仓库没装 httpx/TestClient,自己拼最小 ASGI scope)。"""
    app.dependency_overrides[get_session] = lambda: session

    def call(path: str, **params: Any) -> tuple[int, Any]:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        return http_request("GET", path, query=query)

    try:
        yield call
    finally:
        app.dependency_overrides.pop(get_session, None)


def http_request(method: str, path: str, *, query: str = "") -> tuple[int, Any]:
    """直接驱动 ASGI app 走一遍**完整 HTTP 链**(含 FastAPI 参数解析)。"""
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", b"0"),
        ],
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
# 便捷封装
# --------------------------------------------------------------------------- #


def api(session, *, band: str = BAND, category=None, page_size=None, offset=None, more=None,
        refresh: bool = False, intros: bool = True, **kwargs):
    """直调 ``GET /api/places`` 的路由函数(与既有单测同一口径,全部关键字传参)。"""
    return places_api.list_places(
        origin="上海", band=band, category=category, lat=None, lng=None, refresh=refresh,
        page_size=page_size, offset=offset, more=more, intros=intros, session=session, **kwargs,
    )


def seed_rows(session, rows, *, band: str = BAND, city: str = "上海", rounds: int = 1,
              source: str = "overpass") -> SegmentFetch:
    """直接入库若干行 + 一条抓取水位(不触网),摆出"已经抓过 N 轮"的库状态。"""
    items = place_loader.to_place_items(rows)
    repo.upsert_places(session, origin_city=city, band=band, items=items)
    record = repo.record_segment(
        session, origin_city=city, band=band, origin=ORIGIN, place_count=len(items), source=source
    )
    record.fetch_rounds = int(rounds)
    session.commit()
    return record


def seed_db(session, count: int, **kwargs) -> SegmentFetch:
    """入库环内前 ``count`` 个点(等距、id 连续)。"""
    return seed_rows(session, band_points(count), **kwargs)


def stored_rows(session, *, band: str = BAND, city: str = "上海") -> list[dict[str, Any]]:
    return repo.list_places(session, origin_city=city, band=band,
                            origin_lat=SHANGHAI["lat"], origin_lng=SHANGHAI["lng"])


def load(session, *, target_total=None, fetcher=None, geocoder=None, refresh: bool = False, **kwargs):
    """便捷封装:注入替身调 :func:`services.place_loader.load_segment`。"""
    return place_loader.load_segment(
        session, city="上海", band=BAND, target_total=target_total, refresh=refresh,
        fetcher=fetcher if fetcher is not None else RingFetcher(),
        geocoder=geocoder or FixedGeocoder(), intros=False, **kwargs,
    )


# --------------------------------------------------------------------------- #
# 1. 配额缩放(SEARCH_GROUPS → target_total)
# --------------------------------------------------------------------------- #


def test_scale_search_groups_sums_to_target_total() -> None:
    """30/60/90/120 四轮:各组按比例缩放后合计正好是目标总量。"""
    for target in (30, 60, 90, 120):
        groups = place_loader.scale_search_groups(target)
        assert groups is not None
        total = sum(group["budget"] for group in groups)
        assert total == target, f"target_total={target} 时配额合计应为 {target},实际 {total}"
        assert len(groups) == len(place_loader.SEARCH_GROUPS), "分组数不变(仍是一次查完并集)"


def test_scale_search_groups_keeps_ratio_selectors_and_original() -> None:
    """缩放保权重:大分组仍大于小分组,tag 线索不动,原配额不被就地改。"""
    before = [int(group["budget"]) for group in place_loader.SEARCH_GROUPS]
    scaled = place_loader.scale_search_groups(30)
    budgets = {group["group"]: group["budget"] for group in scaled}
    assert budgets["自然风光"] > budgets["运动场所"] > budgets["小城古镇"], f"权重被缩放打乱:{budgets}"
    assert sum(budgets.values()) == 30
    for original, row in zip(place_loader.SEARCH_GROUPS, scaled):
        assert row is not original, "必须返回副本,不能就地改 SEARCH_GROUPS"
        assert row["group"] == original["group"] and row["category"] == original["category"]
        assert row["tags"] == list(original["tags"]), "tag 线索不能因为缩放而改动"
        assert row["budget"] < int(original["budget"]), "缩到 30 时每组都该变小"
    assert [int(group["budget"]) for group in place_loader.SEARCH_GROUPS] == before == [
        80, 100, 120, 40, 60, 140
    ], "原配额合计 540 不能被就地改"


def test_scale_search_groups_keeps_every_group_above_minimum() -> None:
    """目标总量很小时,每组至少留 MIN_GROUP_BUDGET 条,四分类都不会整类消失。"""
    groups = place_loader.scale_search_groups(5)
    assert place_loader.MIN_GROUP_BUDGET == 2
    assert [group["budget"] for group in groups] == [place_loader.MIN_GROUP_BUDGET] * len(groups)
    assert {group["category"] for group in groups} == {
        group["category"] for group in place_loader.SEARCH_GROUPS
    }


def test_scale_search_groups_none_keeps_full_budget() -> None:
    """``target_total=None`` = 旧口径:返回 None;给满 540 时与原配额一致。"""
    assert place_loader.scale_search_groups(None) is None
    full = place_loader.scale_search_groups(FULL_BUDGET)
    assert [group["budget"] for group in full] == [
        int(group["budget"]) for group in place_loader.SEARCH_GROUPS
    ]
    assert sum(group["budget"] for group in full) == FULL_BUDGET == 540


def test_progressive_target_total_grows_by_round() -> None:
    """下一轮目标总量 = 30 ×(已完成轮数 + 1):30 / 60 / 90 ..."""
    assert place_loader.PROGRESSIVE_STEP == 30
    assert place_loader.progressive_target_total(None) == 30
    assert place_loader.progressive_target_total(0) == 30
    assert [place_loader.progressive_target_total(rounds) for rounds in (1, 2, 3)] == [60, 90, 120]
    assert place_loader.progressive_target_total(-5) == 30, "负轮数按 0 起算"


def test_load_segment_sends_scaled_groups_in_one_request(session) -> None:
    """给了 target_total:仍只发**一次**请求,分组配额合计 = 30,tag 并集口径不变。"""
    fetcher = RingFetcher()
    outcome = load(session, target_total=30, fetcher=fetcher)
    assert fetcher.count == 1, "渐进抓取也是一次查完分组并集,不能拆成多次请求"
    assert fetcher.totals == [30]
    call = fetcher.calls[0]
    assert call["groups_arg"] is not None and call["group_count"] == len(place_loader.SEARCH_GROUPS)
    assert call["groups"] == [group["group"] for group in place_loader.SEARCH_GROUPS]
    assert call["selectors"] == sum(len(group["tags"]) for group in place_loader.SEARCH_GROUPS)
    assert call["band"] == BAND and (call["lat"], call["lng"]) == (SHANGHAI["lat"], SHANGHAI["lng"])
    assert outcome.source == place_loader.SOURCE_OVERPASS and outcome.network_used is True
    assert len(outcome.places) == 30, "环内 30 条候选全部入库"


def test_load_segment_without_target_total_uses_full_budget(session) -> None:
    """不给 target_total:配额与旧版一致(540),且按三参老签名调用。"""
    fetcher = RingFetcher()
    load(session, fetcher=fetcher)
    assert fetcher.totals == [FULL_BUDGET]
    assert fetcher.calls[0]["groups_arg"] is None, "旧口径不传缩放后的分组"


def test_load_segment_keeps_three_arg_fetcher_working(session) -> None:
    """既有单测的三参替身不认 groups:给了 target_total 也不能把它调炸(向后兼容)。"""
    fetcher = LegacyFetcher()
    outcome = load(session, target_total=30, fetcher=fetcher)
    assert len(fetcher.calls) == 1 and fetcher.calls[0]["band"] == BAND
    assert [row["name"] for row in outcome.places] == [
        "环内点0001", "环内点0002", "环内点0003", "环内点0004"
    ]


# --------------------------------------------------------------------------- #
# 2. fetch_rounds:列、递增、旧库补列
# --------------------------------------------------------------------------- #


def test_segment_fetch_has_fetch_rounds_column(session) -> None:
    """新列照既有列风格:Integer / NOT NULL / 默认 0。"""
    column = SegmentFetch.__table__.columns["fetch_rounds"]
    assert column.nullable is False, "fetch_rounds 必须 NOT NULL"
    assert column.default.arg == 0
    record = SegmentFetch(origin_city="上海", band=BAND, origin_name="上海市, 中国",
                          origin_lat=SHANGHAI["lat"], origin_lng=SHANGHAI["lng"], place_count=0)
    session.add(record)
    session.commit()
    assert record.fetch_rounds == 0


def test_load_segment_increments_fetch_rounds_each_round(session) -> None:
    """每完成一轮抓取 +1:首抓 1,扩抓 2,再扩抓 3(轮数落库,下一轮据此推目标总量)。"""
    fetcher = RingFetcher()
    first = load(session, target_total=30, fetcher=fetcher)
    assert first.segment["fetch_rounds"] == 1 and len(first.places) == 30
    second = load(session, target_total=60, fetcher=fetcher, refresh=True)
    assert second.segment["fetch_rounds"] == 2 and len(second.places) == 60
    third = load(session, target_total=90, fetcher=fetcher, refresh=True)
    assert third.segment["fetch_rounds"] == 3 and len(third.places) == 90
    assert fetcher.totals == [30, 60, 90]
    record = repo.get_segment(session, origin_city="上海", band=BAND)
    assert record.fetch_rounds == 3 and record.place_count == 90, "扩抓轮的水位记库内真实条数"


def test_bump_fetch_rounds_and_serialisation_tolerate_null(session) -> None:
    """旧库补列前的 NULL 轮数一律按 0 起算,不炸。"""
    record = seed_db(session, 3, rounds=0)
    record.fetch_rounds = None  # 内存态模拟历史行(NOT NULL 列不能真写 NULL)
    assert repo.segment_to_dict(record)["fetch_rounds"] == 0
    assert places_api._fetch_rounds(SimpleNamespace(segment={"fetch_rounds": None})) == 0
    assert places_api._fetch_rounds(SimpleNamespace(segment=None)) == 0
    record.fetch_rounds = 0
    assert repo.bump_fetch_rounds(session, record) == 1
    session.commit()
    assert repo.get_segment(session, origin_city="上海", band=BAND).fetch_rounds == 1


def test_segment_to_dict_exposes_fetch_rounds(session) -> None:
    """水位序列化带 fetch_rounds(概览/API 都从这里读)。"""
    seed_db(session, 5, rounds=2)
    payload = repo.segment_to_dict(repo.get_segment(session, origin_city="上海", band=BAND))
    assert payload["fetch_rounds"] == 2 and payload["place_count"] == 5
    assert [row["fetch_rounds"] for row in repo.segment_overview(session, origin_city="上海")] == [2]


def test_init_db_backfills_missing_fetch_rounds_column(tmp_path) -> None:
    """轻量迁移:旧库缺列时 init_db 用 ALTER TABLE ADD COLUMN 补上,且幂等。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE segment_fetch ("
            "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
            "origin_city VARCHAR(120) NOT NULL, band VARCHAR(16) NOT NULL, "
            "origin_name VARCHAR(300) NOT NULL, origin_lat FLOAT NOT NULL, origin_lng FLOAT NOT NULL, "
            "place_count INTEGER NOT NULL, source VARCHAR(16) NOT NULL, fetched_at DATETIME NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO segment_fetch (origin_city, band, origin_name, origin_lat, origin_lng, "
            "place_count, source, fetched_at) VALUES ('上海', '50_100', '上海市, 中国', "
            "31.2304, 121.4737, 12, 'overpass', '2026-09-01 00:00:00')"
        ))
    before = {row["name"] for row in inspect(engine).get_columns("segment_fetch")}
    assert "fetch_rounds" not in before, "前置条件:旧库没有这一列"

    init_db(engine)
    init_db(engine)  # 再跑一次也不能报错(幂等)
    after = {row["name"] for row in inspect(engine).get_columns("segment_fetch")}
    assert "fetch_rounds" in after

    with session_factory(engine)() as current:
        record = repo.get_segment(current, origin_city="上海", band=BAND)
        assert record is not None and record.fetch_rounds == 0, "历史行补列后应为 0"
        assert record.place_count == 12 and record.origin_name == "上海市, 中国", "补列不能动既有数据"
        assert repo.bump_fetch_rounds(current, record) == 1
        current.commit()
        assert repo.get_segment(current, origin_city="上海", band=BAND).fetch_rounds == 1
    engine.dispose()


# --------------------------------------------------------------------------- #
# 3. /api/places 分页
# --------------------------------------------------------------------------- #


def test_api_without_paging_params_keeps_legacy_behaviour(session, offline) -> None:
    """兼容硬要求:不带 page_size/offset/more → 全量 places + 旧 note,一次网都不多触。"""
    seed_db(session, 30, rounds=1)
    offline.fetcher = ExplodingFetcher()
    payload = api(session)
    assert payload["count"] == 30 and len(payload["places"]) == 30
    assert payload["total_in_db"] == 30 and payload["has_more"] is False
    assert payload["fetch_rounds"] == 1
    assert payload["note"] == places_api.PLACES_NOTE, "不分页时 note 文案保持原样"
    assert payload["source"] == "db" and payload["network_used"] is False
    expected = sorted(stored_rows(session), key=lambda row: (row["distance_km"], row["name"]))
    assert [row["id"] for row in payload["places"]] == [row["id"] for row in expected], \
        "旧口径排序(距离 + 名字)不变"


def test_api_legacy_cold_fetch_still_uses_full_budget(session, offline) -> None:
    """不分页的冷启动照旧一次抓满 540 配额(旧行为),轮数记 1。"""
    payload = api(session)
    assert offline.fetcher.totals == [FULL_BUDGET]
    assert payload["count"] == FULL_BUDGET and payload["total_in_db"] == FULL_BUDGET
    assert payload["fetch_rounds"] == 1 and payload["has_more"] is False


def test_api_cold_fetch_in_paging_mode_only_grabs_one_round(session, offline) -> None:
    """分页模式的**冷启动首查**只抓一轮 30 配额(BUG-1 本体:首屏不再等整段全量)。"""
    payload = api(session, page_size=15)
    assert offline.fetcher.totals == [30], "首查目标总量 = 30 × (0 轮 + 1)"
    assert offline.geocoder.calls == ["上海"]
    assert payload["count"] == 15 and len(payload["places"]) == 15
    assert payload["total_in_db"] == 30 and payload["has_more"] is True
    assert payload["fetch_rounds"] == 1 and payload["source"] == "overpass"
    assert payload["network_used"] is True
    assert [row["osm_id"] for row in payload["places"]] == list(range(1, 16)), "第一页 = 最近的 15 条"


def test_api_paging_defaults_to_page_size_15_offset_0(session, offline) -> None:
    """只带 more(或只带 offset)也进分页模式:缺省 page_size=15、offset=0。"""
    seed_db(session, 30, rounds=1)
    offline.fetcher = ExplodingFetcher()
    for kwargs in ({"more": "false"}, {"offset": "0"}, {"offset": 0}):
        payload = api(session, **kwargs)
        assert payload["count"] == 15, f"{kwargs} 应默认每页 15 条"
        assert [row["osm_id"] for row in payload["places"]] == list(range(1, 16))
        assert "渐进抓取" in payload["note"]


def test_api_pages_are_stable_and_disjoint(session, offline) -> None:
    """翻遍全部页:不重不漏,拼回来正好是库内全量。"""
    seed_db(session, 30, rounds=1)
    offline.fetcher = ExplodingFetcher()
    seen: list[int] = []
    flags: list[bool] = []
    for start in range(0, 30, 7):
        payload = api(session, page_size=7, offset=str(start), more="false")
        seen.extend(row["osm_id"] for row in payload["places"])
        flags.append(payload["has_more"])
        assert payload["total_in_db"] == 30, "total_in_db 与页码无关"
    assert seen == list(range(1, 31)), f"分页应不重不漏:{seen}"
    assert len(set(seen)) == 30
    assert flags == [True, True, True, True, False], "最后一页 has_more=false"


def test_api_page_order_breaks_distance_ties_by_osm_key(session, offline) -> None:
    """同距离的行按 (osm_type, osm_id) 决胜 → 切片稳定;不分页时仍按名字。"""
    tied = [
        point_at(60, name="B古镇", osm_id=7, osm_type="way", tags={"natural": "peak"}),
        point_at(60, name="A山峰", osm_id=9, osm_type="node", tags={"natural": "peak"}),
        point_at(60, name="Z湖区", osm_id=1, osm_type="relation", tags={"natural": "peak"}),
        point_at(60, name="M岩壁", osm_id=3, osm_type="node", tags={"natural": "peak"}),
    ]
    seed_rows(session, tied, rounds=1)
    offline.fetcher = ExplodingFetcher()

    assert [row["name"] for row in api(session)["places"]] == ["A山峰", "B古镇", "M岩壁", "Z湖区"]

    first = api(session, page_size=2, offset=0, more="false")
    second = api(session, page_size=2, offset=2, more="false")
    assert [row["name"] for row in first["places"]] == ["M岩壁", "A山峰"]
    assert [row["name"] for row in second["places"]] == ["Z湖区", "B古镇"]
    assert [(row["osm_type"], row["osm_id"]) for row in first["places"] + second["places"]] == [
        ("node", 3), ("node", 9), ("relation", 1), ("way", 7)
    ], "决胜键 = (osm_type, osm_id),翻页才不会漂"


def test_api_offset_beyond_rows_returns_empty_page(session, offline) -> None:
    """越界页(more=false):空列表 + has_more=false,不触网。"""
    seed_db(session, 30, rounds=1)
    offline.fetcher = ExplodingFetcher()
    payload = api(session, page_size=15, offset=45, more="false")
    assert payload["places"] == [] and payload["count"] == 0
    assert payload["total_in_db"] == 30 and payload["has_more"] is False
    assert payload["fetch_rounds"] == 1


def test_api_has_more_covers_expandable_band(session, offline) -> None:
    """库内这一页走完、但该 band 还能再抓 + more=true → has_more 仍为 true(加载更多不断链)。"""
    seed_db(session, 30, rounds=1)
    offline.fetcher = ExplodingFetcher()
    exhausted = api(session, page_size=15, offset=15, more="false")
    assert exhausted["has_more"] is False, "more=false 时 has_more 只看库里还有没有下一页"
    expandable = api(session, page_size=15, offset=15, more="true")
    assert expandable["total_in_db"] == 30 and expandable["count"] == 15
    assert expandable["has_more"] is True, f"库内 30 < 配额 {FULL_BUDGET},还该能继续加载"


def test_api_more_true_expands_one_round(session, offline) -> None:
    """越界 + more=true → 扩抓一轮:target_total = 30 × (1 + 1) = 60,轮数变 2。"""
    seed_db(session, 30, rounds=1)
    payload = api(session, page_size=15, offset=30, more="true")
    assert offline.fetcher.totals == [60], f"扩抓目标总量应为 60:{offline.fetcher.totals}"
    assert offline.fetcher.count == 1, "一次扩抓 = 一次 Overpass 请求"
    assert offline.geocoder.calls == [], "扩抓复用库内起点,不再地理编码"
    assert payload["fetch_rounds"] == 2
    assert payload["total_in_db"] == 60 and payload["count"] == 15
    assert payload["has_more"] is True
    assert payload["network_used"] is True and payload["source"] == "overpass"
    assert [row["osm_id"] for row in payload["places"]] == list(range(31, 46)), "扩抓后重新排序再切片"


def test_api_more_true_expands_repeatedly(session, offline) -> None:
    """连续两次"加载更多":30 → 60 → 90,轮数与页内容同步往前走。"""
    seed_db(session, 30, rounds=1)
    first = api(session, page_size=15, offset=30, more="true")
    assert (first["total_in_db"], first["fetch_rounds"]) == (60, 2)
    second = api(session, page_size=15, offset=60, more="true")
    assert (second["total_in_db"], second["fetch_rounds"]) == (90, 3)
    assert offline.fetcher.totals == [60, 90]
    assert [row["osm_id"] for row in second["places"]] == list(range(61, 76))


def test_api_more_false_never_expands(session, offline) -> None:
    """more 缺省/false 时:越界就是空页,绝不触发扩抓。"""
    seed_db(session, 30, rounds=1)
    offline.fetcher = ExplodingFetcher()
    for more_value in (None, "false", False, "0"):
        payload = api(session, page_size=15, offset=30, more=more_value)
        assert payload["places"] == [] and payload["total_in_db"] == 30
        assert payload["fetch_rounds"] == 1, f"more={more_value!r} 不该产生新一轮抓取"
    assert offline.fetcher.count == 0


def test_api_more_true_keeps_offset_in_range(session, offline) -> None:
    """more=true 但页没越界:只读库,不扩抓。"""
    seed_db(session, 30, rounds=1)
    offline.fetcher = ExplodingFetcher()
    payload = api(session, page_size=15, offset=15, more="true")
    assert payload["count"] == 15 and payload["total_in_db"] == 30
    assert payload["fetch_rounds"] == 1 and offline.fetcher.count == 0


def test_api_expansion_stops_at_full_quota(session, offline) -> None:
    """库内行数已到常规全量配额(540):more=true 也不再抓,has_more 收敛为 false。"""
    seed_db(session, FULL_BUDGET, rounds=18)
    offline.fetcher = ExplodingFetcher()
    payload = api(session, page_size=15, offset=str(FULL_BUDGET), more="true")
    assert payload["total_in_db"] == FULL_BUDGET
    assert payload["places"] == [] and payload["has_more"] is False
    assert payload["fetch_rounds"] == 18 and offline.fetcher.count == 0


def test_api_expansion_dedups_and_keeps_generated_fields(session, offline) -> None:
    """扩抓去重:(osm_type, osm_id) 不产生重复行,已生成的 intro 不被覆盖。"""
    seed_db(session, 30, rounds=1)
    before = stored_rows(session)[0]
    place = session.get(Place, before["id"])
    place.intro = "既有简介"
    session.commit()

    payload = api(session, page_size=15, offset=30, more="true")
    assert payload["total_in_db"] == 60

    rows = stored_rows(session)
    keys = [(row["osm_type"], row["osm_id"]) for row in rows]
    assert len(rows) == len(keys) == len(set(keys)) == 60, "扩抓后不能有重复行"
    assert session.get(Place, before["id"]).intro == "既有简介", "upsert 不能覆盖已生成的 intro"


def test_api_expansion_round_skips_llm_intros(session, offline, intros) -> None:
    """首查会补简介,扩抓轮不阻塞在 LLM 上(交给 /api/places/intros)。"""
    api(session, page_size=15)
    assert len(intros.calls) == 1, "冷启动首查照旧补简介"
    api(session, page_size=15, offset=30, more="true")
    assert len(intros.calls) == 1, "扩抓轮不该再调 LLM 简介回填"
    assert repo.get_segment(session, origin_city="上海", band=BAND).fetch_rounds == 2


def test_api_paging_with_category_filter(session, offline) -> None:
    """分类过滤 + 分页:切的是该分类的行,分类计数仍是整段口径。"""
    seed_db(session, 30, rounds=1)
    offline.fetcher = ExplodingFetcher()
    payload = api(session, category="滑雪场", page_size=3, offset=0, more="false")
    assert payload["category"] == "滑雪场"
    assert payload["places"] and all(row["category"] == "滑雪场" for row in payload["places"])
    assert payload["count"] == 3
    assert payload["total_in_db"] == payload["counts_by_category"]["滑雪场"]
    assert sum(payload["counts_by_category"].values()) == 30, "分类计数覆盖整段,不受分页影响"
    tail = api(session, category="滑雪场", page_size=3,
               offset=str(payload["total_in_db"]), more="false")
    assert tail["places"] == [] and tail["has_more"] is False


def test_api_paging_keeps_legacy_fields(session, offline) -> None:
    """分页只**加**字段:既有字段一个不少,口径不变。"""
    seed_db(session, 30, rounds=1)
    offline.fetcher = ExplodingFetcher()
    legacy = api(session)
    paged = api(session, page_size=15, offset=0, more="false")
    assert set(legacy) == set(paged), "分页不能增删既有字段(新增的三个字段两边都在)"
    assert {"total_in_db", "has_more", "fetch_rounds"} <= set(paged)
    for key in ("origin", "band", "category", "counts_by_category", "counts_by_source", "source",
                "network_used", "fetched_at", "written", "seeded", "intro_stats",
                "intro_pending", "detail_pending"):
        assert paged[key] == legacy[key], f"{key} 口径应保持一致"
    assert paged["band"] == {"key": BAND, "label": "50-100 km", "low_km": 50, "high_km": 100}
    assert paged["count"] == 15 and legacy["count"] == 30
    assert paged["elapsed_s"] >= 0 and legacy["elapsed_s"] >= 0


# --------------------------------------------------------------------------- #
# 4. 参数校验(400 中文,不触网)与 HTTP 链
# --------------------------------------------------------------------------- #


def test_api_rejects_bad_page_size(session, offline) -> None:
    """page_size 非法/越界 → 400 中文,且一次网都不触;边界值 1/100 合法。"""
    offline.fetcher = ExplodingFetcher()
    for bad in ("0", "-1", "101", "abc", "1.5", True, False):
        expect_http_error(lambda value=bad: api(session, page_size=value), 400, "page_size")
    assert offline.fetcher.count == 0, "参数非法时应快速失败,不触网"

    seed_db(session, 20, rounds=1)
    assert api(session, page_size=" ")["count"] == 15, "空串按没传处理 → 默认 15"
    assert api(session, page_size=100)["count"] == 20, "page_size 上界 100 合法"
    assert api(session, page_size="1")["count"] == 1, "page_size 下界 1 合法"


def test_api_rejects_bad_offset(session, offline) -> None:
    """offset 负数/非整数 → 400 中文;0 合法。"""
    offline.fetcher = ExplodingFetcher()
    for bad in ("-1", "abc", "1e3", True, False):
        expect_http_error(lambda value=bad: api(session, offset=value), 400, "offset")
    assert offline.fetcher.count == 0
    seed_db(session, 5, rounds=1)
    assert api(session, offset=0)["count"] == 5
    assert api(session, offset="2")["count"] == 3


def test_api_rejects_bad_more(session, offline) -> None:
    """more 不是布尔写法 → 400 中文(true/false/1/0/yes/no/on/off 都认)。"""
    offline.fetcher = ExplodingFetcher()
    for bad in ("maybe", "2", "ture"):
        expect_http_error(lambda value=bad: api(session, more=value), 400, "more", "true/false")
    seed_db(session, 4, rounds=1)
    for good in ("true", "TRUE", "1", "yes", "on", True):
        payload = api(session, page_size=2, offset=2, more=good)
        assert payload["count"] == 2 and payload["has_more"] is True, f"more={good!r} 应还能继续加载"
    for good in ("false", "0", "no", "off", "", False):
        payload = api(session, page_size=2, offset=2, more=good)
        assert payload["count"] == 2 and payload["has_more"] is False, f"more={good!r} 不该再往下走"


def test_api_paging_validation_runs_before_any_fetch(session, offline) -> None:
    """分页参数先校验:非法时连冷启动抓取都不发起,也不留水位。"""
    expect_http_error(lambda: api(session, page_size=0), 400, "page_size")
    expect_http_error(lambda: api(session, offset=-3), 400, "offset")
    expect_http_error(lambda: api(session, more="maybe"), 400, "more")
    assert offline.fetcher.count == 0 and offline.geocoder.calls == []
    assert repo.get_segment(session, origin_city="上海", band=BAND) is None


def test_api_rejects_bad_band_and_category_before_paging(session, offline) -> None:
    """分页模式下非法 band/category 仍是既有的 400 中文口径。"""
    offline.fetcher = ExplodingFetcher()
    expect_http_error(lambda: api(session, band="0_10", page_size=15), 400, "未知距离分段")
    expect_http_error(lambda: api(session, category="美食", page_size=15), 400, "未知分类")
    expect_http_error(
        lambda: places_api.list_places(origin="  ", band=BAND, category=None, lat=None, lng=None,
                                       refresh=False, page_size=15, offset=None, more=None,
                                       session=session),
        400, "起点城市不能为空",
    )
    assert offline.fetcher.count == 0


def test_http_paging_round_trip(session, offline, http) -> None:
    """完整 HTTP 链:查询串分页与直调路由结果一致。"""
    seed_db(session, 30, rounds=1)
    offline.fetcher = ExplodingFetcher()
    status, payload = http("/api/places", origin="上海", band=BAND, page_size=10, offset=10, more="false")
    assert status == 200
    assert payload["count"] == 10 and payload["total_in_db"] == 30 and payload["fetch_rounds"] == 1
    assert [row["osm_id"] for row in payload["places"]] == list(range(11, 21))
    direct = api(session, page_size=10, offset=10, more="false")
    assert [row["id"] for row in direct["places"]] == [row["id"] for row in payload["places"]]
    assert direct["has_more"] == payload["has_more"]


def test_http_bad_paging_params_are_400_not_422(session, offline, http) -> None:
    """HTTP 链上非法分页参数也是 **400 中文**(裸 JSON 口径,不掉进 pydantic 的 422)。"""
    seed_db(session, 3, rounds=1)
    offline.fetcher = ExplodingFetcher()
    for query in ({"page_size": "0"}, {"page_size": "abc"}, {"offset": "-1"}, {"more": "maybe"}):
        status, payload = http("/api/places", origin="上海", band=BAND, **query)
        assert status == 400, f"{query} 应为 400,实际 {status}:{payload}"
        assert isinstance(payload.get("detail"), str) and payload["detail"], f"应有中文报错:{payload}"
    status, payload = http("/api/places", origin="上海", band=BAND, page_size=15)
    assert status == 200 and payload["count"] == 3 and payload["total_in_db"] == 3


def test_http_expansion_round_trip(session, offline, http) -> None:
    """完整 HTTP 链上的扩抓:more=true 越界 → 抓一轮 60,轮数变 2。"""
    seed_db(session, 30, rounds=1)
    status, payload = http("/api/places", origin="上海", band=BAND, page_size=15, offset=30, more="true")
    assert status == 200
    assert offline.fetcher.totals == [60]
    assert payload["fetch_rounds"] == 2 and payload["total_in_db"] == 60
    assert [row["osm_id"] for row in payload["places"]] == list(range(31, 46))


def test_openapi_exposes_paging_params() -> None:
    """分页参数进 OpenAPI(前端 TASK-6e 照它接)。"""
    params = {item["name"] for item in app.openapi()["paths"]["/api/places"]["get"]["parameters"]}
    assert {"origin", "band", "category", "page_size", "offset", "more"} <= params
