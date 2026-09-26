"""TASK-5a 单测:行程方案(TripPlan)表 + ``services.trips`` 报价 + ``/api/trip-plans``。

全程不触网:

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错 —— 方案报价只读收藏快照,
  任何偷偷联网(重算路线 / 调 LLM 估价)都会当场失败;
* DB 用 ``tmp_path`` 下的临时 SQLite 文件,不碰 ``backend/data/``;
* 完整 HTTP 链用手拼的最小 ASGI scope 驱动(仓库没装 httpx/TestClient),
  ``app.dependency_overrides`` 把 ``get_session`` 换成同一个临时库会话。

重点覆盖三件事:**价文案解析**(脏输入一律 ``(None, None)`` 不猜数)、**报价求和与上下限**
(交通按快照 ``cost_cny`` 相加、住宿按价下限均值 × 晚数)、**引用被删的降级**
(方案不连带删,缺行进 ``quote.missing``)。另加幂等(唯一键 = 方案名)与 API 校验口径。

运行:``cd backend && ../.venv/bin/python -m pytest -q``
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest
import requests
from fastapi import HTTPException
from sqlalchemy import JSON, UniqueConstraint
from sqlalchemy.exc import IntegrityError

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import trips as trips_api  # noqa: E402
from app.main import app  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db import models  # noqa: E402
from db import repository as repo  # noqa: E402
from db.base import get_session  # noqa: E402
from db.models import TripPlan  # noqa: E402
from services import trips  # noqa: E402
from services.stays import upsert_stays  # noqa: E402

# --------------------------------------------------------------------------- #
# 样本数据:上海(起点)→ 崇明(目的地),外加两处住宿收藏
# --------------------------------------------------------------------------- #

SHANGHAI = {"lat": 31.2304, "lng": 121.4737}
CHONGMING = {"lat": 31.5, "lng": 121.5}
QUOTE_NOTE = "按收藏快照的当时口径估算 · 仅供参考"
REQUIRED_PLAN_COLUMNS = {
    "name", "note", "place_collection_id", "route_collection_ids",
    "stay_collection_ids", "created_at", "updated_at",
}
REQUIRED_DICT_KEYS = {
    "id", "name", "note", "place_collection_id", "route_collection_ids",
    "stay_collection_ids", "counts", "quote", "created_at", "updated_at",
}
REQUIRED_QUOTE_KEYS = {
    "total_cny_low", "total_cny_high", "transport_cny", "stay_nights",
    "per_stay", "missing", "kind", "note",
}


def route_collection(
    session,
    *,
    osm_id: int,
    cost_cny: Any = 88,
    mode: str = "driving",
    name: str = "上海 → 崇明",
    duration_min: Any = 94,
    distance_km: Any = 122.4,
) -> Any:
    """一条路线收藏(``kind=route``),快照里带 ``cost_cny``(报价的交通项就读它)。"""
    row, _ = repo.upsert_collection(
        session,
        kind="route",
        mode=mode,
        name=name,
        osm_type="node",
        osm_id=osm_id,
        from_lat=SHANGHAI["lat"],
        from_lng=SHANGHAI["lng"],
        from_name="上海",
        to_lat=CHONGMING["lat"],
        to_lng=CHONGMING["lng"],
        to_name="崇明",
        summary={
            "duration_min": duration_min, "cost_cny": cost_cny,
            "distance_km": distance_km, "kind": "real",
        },
    )
    return row


def place_collection(session, *, osm_id: int = -1234, name: str = "西沙湿地") -> Any:
    """一条目的地收藏(``kind=place``),方案里当"去哪儿"。"""
    row, _ = repo.upsert_collection(
        session, kind="place", name=name, osm_type="way", osm_id=osm_id,
        to_lat=CHONGMING["lat"], to_lng=CHONGMING["lng"], to_name=name,
    )
    return row


def stay_collection(
    session, *, osm_id: int, name: str = "崇明民宿", price_estimate: Any = "约¥250-450/晚"
) -> Any:
    """一条住宿收藏:价估算串留在**快照** ``summary["price_estimate"]`` 里。"""
    summary: dict[str, Any] = {}
    if price_estimate is not None:
        summary["price_estimate"] = price_estimate
    row, _ = repo.upsert_collection(
        session, kind="place", name=name, osm_type="node", osm_id=osm_id,
        to_lat=CHONGMING["lat"], to_lng=CHONGMING["lng"], to_name=name, summary=summary,
    )
    return row


def seed_refs(session) -> dict[str, Any]:
    """一套完整引用:1 个目的地 + 2 段路线(88 元 / 降级无费用)+ 2 处住宿。"""
    return {
        "place": place_collection(session),
        "drive": route_collection(session, osm_id=7, cost_cny=88),
        "rail": route_collection(session, osm_id=8, mode="rail", name="上海 → 崇明 · 火车",
                                 cost_cny=None, duration_min=120),
        "stay_a": stay_collection(session, osm_id=101, name="崇明民宿", price_estimate="约¥250-450/晚"),
        "stay_b": stay_collection(session, osm_id=102, name="陈家镇酒店", price_estimate="¥300/晚"),
    }


def seed_plan(session, *, name: str = "崇明两日游", **refs: Any) -> Any:
    """按 ``seed_refs`` 的引用建一份方案(可覆盖名字/引用)。"""
    given = refs or seed_refs(session)
    row, _ = trips.upsert_trip_plan(
        session,
        name=name,
        place_collection_id=given.get("place").id if given.get("place") else None,
        route_collection_ids=[item.id for item in (given.get("drive"), given.get("rail")) if item],
        stay_collection_ids=[item.id for item in (given.get("stay_a"), given.get("stay_b")) if item],
    )
    return row


def tick() -> None:
    """让两次写入的 ``created_at`` 真正拉开差距(列表"新的在前"才断言得稳)。"""
    time.sleep(0.002)


def expect_http_error(func: Callable[[], Any], status_code: int, *fragments: str) -> HTTPException:
    """断言 ``func()`` 抛出指定状态码的 HTTPException,且 detail 含全部片段。"""
    with pytest.raises(HTTPException) as caught:
        func()
    exc = caught.value
    assert exc.status_code == status_code, f"应为 HTTP {status_code},实际 {exc.status_code}"
    for fragment in fragments:
        assert fragment in str(exc.detail), f"detail 应包含 {fragment!r},实际:{exc.detail}"
    return exc


def expect_error(func: Callable[[], Any], exc_type: type, *fragments: str) -> Exception:
    """断言 ``func()`` 抛出 ``exc_type``,且错误信息包含全部 ``fragments``。"""
    with pytest.raises(exc_type) as caught:
        func()
    for fragment in fragments:
        assert fragment in str(caught.value), f"应包含 {fragment!r},实际:{caught.value}"
    return caught.value


def http_request(method: str, path: str, *, body: Any = None, query: str = "") -> tuple[int, Any]:
    """直接驱动 ASGI app 走一遍**完整 HTTP 链**(含 FastAPI 解析),返回 ``(状态码, JSON)``。"""
    raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
        ],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    chunks = [raw]
    payload = bytearray()
    status: dict[str, int] = {}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": chunks.pop(0) if chunks else b"",
                "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            payload.extend(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return status["code"], json.loads(payload.decode() or "null")


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底:任何 requests 调用都视为测试失败(方案报价只读快照,必须纯 mock)。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture()
def session(tmp_path):
    """每个用例一个独立的临时 SQLite 库。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'trips_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


@pytest.fixture()
def api_client(session) -> Callable[..., tuple[int, Any]]:
    """完整 HTTP 链用的客户端:把 ``get_session`` 依赖换成用例里的同一个临时库会话。"""
    app.dependency_overrides[get_session] = lambda: session
    return lambda method, path, **kwargs: http_request(method, path, **kwargs)


@pytest.fixture()
def http(api_client):
    """同 :func:`api_client`,但用例结束后清理 ``dependency_overrides``,避免串味。"""
    yield api_client
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 表结构:引用 collections.id 但**不建外键**(删收藏不连带删方案)
# --------------------------------------------------------------------------- #


def test_trip_plan_table_has_required_columns_and_unique_name() -> None:
    table = TripPlan.__table__
    assert table.name == "trip_plans"
    assert REQUIRED_PLAN_COLUMNS <= {column.name for column in table.columns}
    assert [column.name for column in table.primary_key.columns] == ["id"]
    unique_names = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_trip_plan_name" in unique_names, f"方案名要唯一,实际约束:{unique_names}"
    assert table.columns["name"].nullable is False
    assert table.columns["note"].nullable is True
    assert table.columns["place_collection_id"].nullable is True


def test_trip_plan_ref_columns_are_plain_integers_and_json_lists() -> None:
    """引用列不建外键:收藏被删时方案照旧能打开,报价按"已删除"降级。"""
    table = TripPlan.__table__
    for column in ("place_collection_id", "route_collection_ids", "stay_collection_ids"):
        assert not table.columns[column].foreign_keys, f"{column} 不该有外键"
    assert table.columns["place_collection_id"].nullable is True
    for column in ("route_collection_ids", "stay_collection_ids"):
        assert isinstance(table.columns[column].type, JSON), f"{column} 应是 JSON 数组"
        assert table.columns[column].nullable is False


def test_duplicate_plan_name_is_rejected_at_db_level(session) -> None:
    session.add(TripPlan(name="崇明两日游", route_collection_ids=[], stay_collection_ids=[]))
    session.commit()
    session.add(TripPlan(name="崇明两日游", route_collection_ids=[], stay_collection_ids=[]))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# --------------------------------------------------------------------------- #
# 价文案解析:parse_nightly_price(脏输入不猜数)
# --------------------------------------------------------------------------- #


def test_parse_nightly_price_reads_canonical_range_and_single() -> None:
    assert trips.parse_nightly_price("约¥250-450/晚") == (250.0, 450.0)
    assert trips.parse_nightly_price("¥300/晚") == (300.0, None)
    assert trips.parse_nightly_price("约¥1200/晚") == (1200.0, None)


def test_parse_nightly_price_reads_llm_variants() -> None:
    assert trips.parse_nightly_price("300-500元/晚") == (300.0, 500.0)
    assert trips.parse_nightly_price("约 ¥1,200~1,800 每晚") == (1200.0, 1800.0)
    assert trips.parse_nightly_price("价位:200—320") == (200.0, 320.0)
    assert trips.parse_nightly_price("1，200-1，800 元") == (1200.0, 1800.0)
    assert trips.parse_nightly_price("大约 180 到 260 一晚") == (180.0, 260.0)


def test_parse_nightly_price_never_invents_numbers_from_garbage() -> None:
    assert trips.parse_nightly_price(None) == (None, None)
    assert trips.parse_nightly_price("") == (None, None)
    assert trips.parse_nightly_price("   ") == (None, None)
    assert trips.parse_nightly_price("暂无报价") == (None, None)
    assert trips.parse_nightly_price("价格面议") == (None, None)
    assert trips.parse_nightly_price("abc") == (None, None)
    assert trips.parse_nightly_price([]) == (None, None)
    assert trips.parse_nightly_price({"low": 300}) == (None, None)
    assert trips.parse_nightly_price("¥0/晚") == (None, None), "0 元不是价,不猜数"
    assert trips.parse_nightly_price(object()) == (None, None)


def test_parse_nightly_price_fixes_reversed_range_without_guessing() -> None:
    assert trips.parse_nightly_price("约¥450-250/晚") == (250.0, 450.0)


def test_parse_nightly_price_accepts_plain_numbers_and_rejects_bool() -> None:
    assert trips.parse_nightly_price(320) == (320.0, None)
    assert trips.parse_nightly_price(320.5) == (320.5, None)
    assert trips.parse_nightly_price("420") == (420.0, None)
    assert trips.parse_nightly_price(0) == (None, None)
    assert trips.parse_nightly_price(True) == (None, None)


def test_parse_nightly_price_takes_first_range_and_ignores_season_note() -> None:
    assert trips.parse_nightly_price("约¥250-450/晚(旺季 500-800)") == (250.0, 450.0)


# --------------------------------------------------------------------------- #
# 报价:quote_plan(只读收藏快照,不重新调 /api/routes)
# --------------------------------------------------------------------------- #


def test_quote_plan_transport_sums_snapshots_and_skips_missing_costs(session) -> None:
    refs = seed_refs(session)
    bus = route_collection(session, osm_id=9, mode="rail", name="上海 → 崇明 · 城际", cost_cny=12)
    quote = trips.quote_plan(
        session, refs["place"].id, [refs["drive"].id, bus.id, refs["rail"].id], [], nights=1
    )
    assert quote["transport_cny"] == 100, "88 + 12,cost_cny 为 null 的那段跳过"
    assert quote["total_cny_low"] == 100 and quote["total_cny_high"] == 100
    assert quote["missing"] == [] and quote["per_stay"] == []


def test_quote_plan_place_collection_adds_no_cost(session) -> None:
    refs = seed_refs(session)
    quote = trips.quote_plan(session, refs["place"].id, [], [], nights=3)
    assert quote["transport_cny"] == 0
    assert quote["total_cny_low"] == 0 and quote["total_cny_high"] == 0
    assert quote["stay_nights"] == 3 and quote["missing"] == []


def test_quote_plan_stay_total_is_mean_low_times_nights(session) -> None:
    refs = seed_refs(session)
    quote = trips.quote_plan(
        session, None, [], [refs["stay_a"].id, refs["stay_b"].id], nights=2
    )
    assert [item["low"] for item in quote["per_stay"]] == [250, 300]
    assert quote["total_cny_low"] == 550, "均值 (250+300)/2=275 × 2 晚"
    assert quote["total_cny_high"] == 750, "上限档 (450+300)/2=375 × 2 晚(单值按上限=下限)"
    assert quote["stay_nights"] == 2


def test_quote_plan_high_equals_low_when_no_range_given(session) -> None:
    first = stay_collection(session, osm_id=201, name="青旅", price_estimate="¥300/晚")
    second = stay_collection(session, osm_id=202, name="公寓", price_estimate="约¥500/晚")
    quote = trips.quote_plan(session, None, [], [first.id, second.id], nights=1)
    assert quote["total_cny_low"] == 400 and quote["total_cny_high"] == 400


def test_quote_plan_skips_unparsable_stay_price_without_guessing(session) -> None:
    refs = seed_refs(session)
    mystery = stay_collection(session, osm_id=203, name="某客栈", price_estimate="暂无报价")
    quote = trips.quote_plan(
        session, None, [refs["drive"].id], [refs["stay_a"].id, mystery.id], nights=1
    )
    assert quote["per_stay"][1] == {
        "collection_id": mystery.id, "name": "某客栈",
        "price_estimate": "暂无报价", "low": None, "high": None,
    }
    assert quote["total_cny_low"] == 88 + 250, "解析不出价的住宿不进均值"
    assert quote["total_cny_high"] == 88 + 450


def test_quote_plan_falls_back_to_stay_table_when_snapshot_has_no_price(session) -> None:
    upsert_stays(session, [{
        "osm_type": "node", "osm_id": 77, "name": "崇明大酒店", "lat": 31.5, "lng": 121.5,
        "tags": {"tourism": "hotel"}, "price_estimate": "约¥400-600/晚",
    }])
    collected = stay_collection(session, osm_id=77, name="崇明大酒店", price_estimate=None)
    quote = trips.quote_plan(session, None, [], [collected.id], nights=1)
    assert quote["per_stay"][0]["price_estimate"] == "约¥400-600/晚"
    assert quote["total_cny_low"] == 400 and quote["total_cny_high"] == 600


def test_quote_plan_reports_deleted_refs_as_missing(session) -> None:
    """引用被删 → 方案不炸,缺行按"已删除"列进 ``missing``,其余照算。"""
    refs = seed_refs(session)
    plan = seed_plan(session, **refs)
    assert repo.delete_collection(session, collection_id=refs["rail"].id) is True
    assert repo.delete_collection(session, collection_id=refs["stay_a"].id) is True

    quote = trips.quote_plan(
        session, plan.place_collection_id, plan.route_collection_ids,
        plan.stay_collection_ids, nights=1,
    )
    assert quote["missing"] == [refs["rail"].id, refs["stay_a"].id]
    assert quote["transport_cny"] == 88, "只剩驾车那段"
    assert [item["collection_id"] for item in quote["per_stay"]] == [refs["stay_b"].id]
    assert quote["total_cny_low"] == 88 + 300


def test_quote_plan_reports_deleted_place_collection(session) -> None:
    refs = seed_refs(session)
    assert repo.delete_collection(session, collection_id=refs["place"].id) is True
    quote = trips.quote_plan(session, refs["place"].id, [], [])
    assert quote["missing"] == [refs["place"].id]
    assert quote["total_cny_low"] == 0 and quote["kind"] == "estimate"


def test_quote_plan_ignores_repeated_refs(session) -> None:
    refs = seed_refs(session)
    quote = trips.quote_plan(
        session, None, [refs["drive"].id, refs["drive"].id], [refs["stay_a"].id] * 3, nights=1
    )
    assert quote["transport_cny"] == 88, "同一段路线重复引用不该算两遍"
    assert len(quote["per_stay"]) == 1 and quote["total_cny_low"] == 88 + 250


def test_quote_plan_output_is_labeled_estimate(session) -> None:
    refs = seed_refs(session)
    quote = trips.quote_plan(
        session, refs["place"].id, [refs["drive"].id], [refs["stay_a"].id], nights=3
    )
    assert REQUIRED_QUOTE_KEYS <= set(quote)
    assert quote["kind"] == "estimate", "金额必须自带估算标注"
    assert quote["note"] == QUOTE_NOTE
    assert quote["stay_nights"] == 3
    assert all(isinstance(quote[key], (int, float)) for key in
               ("total_cny_low", "total_cny_high", "transport_cny"))


def test_quote_plan_accepts_string_nights_and_rejects_out_of_range(session) -> None:
    refs = seed_refs(session)
    assert trips.quote_plan(session, None, [], [refs["stay_a"].id], nights="2")["stay_nights"] == 2
    assert trips.quote_plan(session, None, [], [refs["stay_a"].id], nights=None)["stay_nights"] == 1
    assert trips.quote_plan(session, None, [], [], nights=trips.MAX_NIGHTS)["stay_nights"] == 60

    def reject(bad: Any) -> None:
        expect_error(
            lambda: trips.quote_plan(session, None, [], [], nights=bad), ValueError, "nights"
        )

    for bad in (0, -1, 61, "abc", True, 1.5, "0"):
        reject(bad)


def test_quote_plan_rejects_bad_ref_arguments(session) -> None:
    expect_error(lambda: trips.quote_plan(session, None, "1,2", []), ValueError, "整数数组", "str")
    expect_error(lambda: trips.quote_plan(session, None, {"id": 1}, []), ValueError, "整数数组")
    expect_error(lambda: trips.quote_plan(session, None, [0], []), ValueError, "正整数")
    expect_error(lambda: trips.quote_plan(session, None, ["a"], []), ValueError, "必须是整数")
    expect_error(lambda: trips.quote_plan(session, "abc", [], []), ValueError,
                 "place_collection_id", "必须是整数")
    expect_error(lambda: trips.quote_plan(session, -3, [], []), ValueError, "正整数")


# --------------------------------------------------------------------------- #
# 幂等 upsert 与序列化
# --------------------------------------------------------------------------- #


def test_upsert_trip_plan_writes_refs_and_timestamps(session) -> None:
    refs = seed_refs(session)
    row, created = trips.upsert_trip_plan(
        session,
        name="崇明两日游",
        note="周末出发",
        place_collection_id=refs["place"].id,
        route_collection_ids=[refs["drive"].id, refs["rail"].id],
        stay_collection_ids=[refs["stay_a"].id],
    )
    assert created is True and row.id and row.name == "崇明两日游"
    assert row.note == "周末出发"
    assert row.place_collection_id == refs["place"].id
    assert row.route_collection_ids == [refs["drive"].id, refs["rail"].id]
    assert row.stay_collection_ids == [refs["stay_a"].id]
    assert row.created_at is not None and row.updated_at is not None
    assert trips.count_trip_plans(session) == 1


def test_upsert_trip_plan_defaults_to_empty_ref_lists(session) -> None:
    row, created = trips.upsert_trip_plan(session, name="先建个空方案")
    assert created is True
    assert row.place_collection_id is None
    assert row.route_collection_ids == [] and row.stay_collection_ids == []
    assert row.note is None


def test_upsert_trip_plan_is_idempotent_by_name(session) -> None:
    """同名再提交 = 刷新引用、返回原行(``created=False``、id 与 created_at 不变)。"""
    refs = seed_refs(session)
    first, created_first = trips.upsert_trip_plan(
        session, name="崇明两日游", route_collection_ids=[refs["drive"].id]
    )
    assert created_first is True
    tick()
    extra = route_collection(session, osm_id=11, mode="rail", name="上海 → 崇明 · 高铁", cost_cny=60)
    second, created_second = trips.upsert_trip_plan(
        session,
        name="  崇明两日游  ",
        route_collection_ids=[refs["drive"].id, extra.id],
        stay_collection_ids=[refs["stay_a"].id],
    )
    assert created_second is False
    assert second.id == first.id
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at
    assert second.route_collection_ids == [refs["drive"].id, extra.id]
    assert second.stay_collection_ids == [refs["stay_a"].id]
    assert trips.count_trip_plans(session) == 1, "重名不该产生第二行"


def test_upsert_trip_plan_keeps_existing_note_when_blank(session) -> None:
    row, _ = trips.upsert_trip_plan(session, name="崇明两日游", note="预算控制")
    again, created = trips.upsert_trip_plan(session, name="崇明两日游", note="   ")
    assert created is False and again.id == row.id
    assert again.note == "预算控制", "空备注不该把已有备注抹掉"
    third, _ = trips.upsert_trip_plan(session, name="崇明两日游", note="改到三月")
    assert third.note == "改到三月"


def test_upsert_trip_plan_requires_a_name(session) -> None:
    def reject(bad: Any) -> None:
        expect_error(
            lambda: trips.upsert_trip_plan(session, name=bad), ValueError, "name", "不能为空"
        )

    for bad in (None, "", "   ", 123, [], object()):
        reject(bad)
    assert trips.count_trip_plans(session) == 0


def test_upsert_trip_plan_truncates_long_name(session) -> None:
    row, _ = trips.upsert_trip_plan(session, name="雪" * (models.NAME_LEN + 50))
    assert len(row.name) == models.NAME_LEN


def test_upsert_trip_plan_rejects_unknown_refs(session) -> None:
    refs = seed_refs(session)
    expect_error(
        lambda: trips.upsert_trip_plan(session, name="崇明两日游", place_collection_id=9999),
        ValueError, "未知收藏", "9999",
    )
    expect_error(
        lambda: trips.upsert_trip_plan(
            session, name="崇明两日游",
            route_collection_ids=[refs["drive"].id, 12345],
        ),
        ValueError, "未知收藏", "12345",
    )
    expect_error(
        lambda: trips.upsert_trip_plan(session, name="崇明两日游", stay_collection_ids=[777]),
        ValueError, "未知收藏", "777",
    )
    assert trips.count_trip_plans(session) == 0, "引用不存在时应快速失败,不落库"


def test_upsert_trip_plan_rejects_bad_ref_lists(session) -> None:
    expect_error(
        lambda: trips.upsert_trip_plan(session, name="崇明两日游", route_collection_ids="1,2"),
        ValueError, "route_collection_ids", "整数数组",
    )
    expect_error(
        lambda: trips.upsert_trip_plan(session, name="崇明两日游", stay_collection_ids=[None]),
        ValueError, "stay_collection_ids",
    )
    expect_error(
        lambda: trips.upsert_trip_plan(session, name="崇明两日游", place_collection_id="abc"),
        ValueError, "place_collection_id", "必须是整数",
    )


def test_upsert_trip_plan_dedups_refs_and_keeps_order(session) -> None:
    refs = seed_refs(session)
    row, _ = trips.upsert_trip_plan(
        session,
        name="崇明两日游",
        route_collection_ids=[refs["rail"].id, refs["drive"].id, refs["rail"].id],
    )
    assert row.route_collection_ids == [refs["rail"].id, refs["drive"].id]


def test_trip_plan_to_dict_shape_counts_and_quote(session) -> None:
    refs = seed_refs(session)
    row, _ = trips.upsert_trip_plan(
        session,
        name="崇明两日游",
        note="周末",
        place_collection_id=refs["place"].id,
        route_collection_ids=[refs["drive"].id],
        stay_collection_ids=[refs["stay_a"].id, refs["stay_b"].id],
    )
    quote = trips.quote_plan(
        session, row.place_collection_id, row.route_collection_ids, row.stay_collection_ids
    )
    payload = trips.trip_plan_to_dict(row, quote=quote)
    assert REQUIRED_DICT_KEYS <= set(payload)
    assert payload["counts"] == {"place": 1, "routes": 1, "stays": 2}
    assert payload["quote"] == quote and payload["quote"]["kind"] == "estimate"
    assert payload["created_at"] and payload["updated_at"]
    assert payload["note"] == "周末"


def test_trip_plan_to_dict_without_quote(session) -> None:
    row, _ = trips.upsert_trip_plan(session, name="空方案")
    payload = trips.trip_plan_to_dict(row)
    assert payload["quote"] is None
    assert payload["counts"] == {"place": 0, "routes": 0, "stays": 0}
    assert payload["place_collection_id"] is None
    assert payload["route_collection_ids"] == [] and payload["stay_collection_ids"] == []


def test_select_and_delete_trip_plan(session) -> None:
    refs = seed_refs(session)
    first = seed_plan(session, name="崇明两日游", **refs)
    tick()
    second = seed_plan(session, name="三月看花", **refs)
    assert [row.id for row in trips.select_trip_plans(session)] == [second.id, first.id], "新的在前"
    assert trips.select_trip_plans(session, limit=1)[0].id == second.id
    assert trips.get_trip_plan(session, trip_plan_id=first.id).name == "崇明两日游"
    assert trips.get_trip_plan(session, trip_plan_id=9999) is None
    assert trips.delete_trip_plan(session, trip_plan_id=first.id) is True
    assert trips.delete_trip_plan(session, trip_plan_id=first.id) is False
    assert trips.count_trip_plans(session) == 1
    assert repo.count_collections(session) == 5, "删方案不该动收藏"
    expect_error(lambda: trips.get_trip_plan(session, trip_plan_id="abc"), ValueError, "方案 id")
    expect_error(lambda: trips.delete_trip_plan(session, trip_plan_id=0), ValueError, "正整数")


# --------------------------------------------------------------------------- #
# 方案 API:POST /api/trip-plans(重名 = 刷新)
# --------------------------------------------------------------------------- #


def plan_payload(refs: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """一份完整方案的请求体(默认:1 目的地 + 2 路线 + 2 住宿 + 2 晚)。"""
    body: dict[str, Any] = {
        "name": "崇明两日游",
        "note": "周末出发",
        "place_collection_id": refs["place"].id,
        "route_collection_ids": [refs["drive"].id, refs["rail"].id],
        "stay_collection_ids": [refs["stay_a"].id, refs["stay_b"].id],
        "nights": 2,
    }
    body.update(overrides)
    return body


def test_api_create_trip_plan_payload_shape(session) -> None:
    refs = seed_refs(session)
    payload = trips_api.create_trip_plan(payload=plan_payload(refs), session=session)
    plan = payload["trip_plan"]
    assert payload["created"] is True and payload["idempotent"] is False
    assert payload["count"] == 1 and payload["nights"] == 2
    assert payload["elapsed_s"] >= 0 and "估算" in payload["note"]
    assert REQUIRED_DICT_KEYS <= set(plan)
    assert plan["name"] == "崇明两日游" and plan["note"] == "周末出发"
    assert plan["counts"] == {"place": 1, "routes": 2, "stays": 2}
    assert REQUIRED_QUOTE_KEYS <= set(payload["quote"])
    assert payload["quote"] == plan["quote"]
    assert payload["quote"]["transport_cny"] == 88, "火车那段 cost_cny 为 null,跳过"
    assert payload["quote"]["total_cny_low"] == 638, "88 + (250+300)/2 × 2 晚"
    assert payload["quote"]["total_cny_high"] == 838, "88 + (450+300)/2 × 2 晚"
    assert payload["quote"]["kind"] == "estimate" and payload["quote"]["note"] == QUOTE_NOTE
    assert plan["created_at"] and plan["updated_at"]


def test_api_create_trip_plan_is_idempotent_by_name(session) -> None:
    refs = seed_refs(session)
    first = trips_api.create_trip_plan(payload=plan_payload(refs), session=session)
    tick()
    second = trips_api.create_trip_plan(
        payload=plan_payload(refs, route_collection_ids=[refs["drive"].id], nights=1),
        session=session,
    )
    assert first["created"] is True
    assert second["created"] is False and second["idempotent"] is True
    assert second["trip_plan"]["id"] == first["trip_plan"]["id"]
    assert second["trip_plan"]["created_at"] == first["trip_plan"]["created_at"]
    assert second["trip_plan"]["route_collection_ids"] == [refs["drive"].id], "重名 = 刷新引用"
    assert second["quote"]["stay_nights"] == 1
    assert second["quote"]["total_cny_low"] == 88 + 275
    assert second["count"] == 1


def test_api_create_trip_plan_rejects_bad_payload(session) -> None:
    refs = seed_refs(session)
    expect_http_error(
        lambda: trips_api.create_trip_plan(payload=None, session=session),
        400, "缺少请求体", "JSON 对象",
    )
    expect_http_error(
        lambda: trips_api.create_trip_plan(payload=[1, 2], session=session),
        400, "请求体必须是 JSON 对象", "list",
    )
    expect_http_error(
        lambda: trips_api.create_trip_plan(payload={"note": "没有名字"}, session=session),
        400, "name", "不能为空",
    )
    expect_http_error(
        lambda: trips_api.create_trip_plan(payload=plan_payload(refs, name="  "), session=session),
        400, "name", "不能为空",
    )
    expect_http_error(
        lambda: trips_api.create_trip_plan(
            payload=plan_payload(refs, place_collection_id=9999), session=session),
        400, "未知收藏", "9999",
    )
    expect_http_error(
        lambda: trips_api.create_trip_plan(
            payload=plan_payload(refs, stay_collection_ids=[12345]), session=session),
        400, "未知收藏", "12345",
    )
    expect_http_error(
        lambda: trips_api.create_trip_plan(
            payload=plan_payload(refs, route_collection_ids="1,2"), session=session),
        400, "整数数组",
    )
    assert trips.count_trip_plans(session) == 0, "参数非法时应快速失败,不落库"


def test_api_create_trip_plan_rejects_nights_out_of_range_before_writing(session) -> None:
    refs = seed_refs(session)

    def reject(bad: Any) -> None:
        expect_http_error(
            lambda: trips_api.create_trip_plan(
                payload=plan_payload(refs, nights=bad), session=session),
            400, "nights",
        )

    for bad in (0, -1, 61, "abc", True, 1.5):
        reject(bad)
    assert trips.count_trip_plans(session) == 0, "晚数非法时不该先把方案写进去"
    ok = trips_api.create_trip_plan(payload=plan_payload(refs, nights=60), session=session)
    assert ok["quote"]["stay_nights"] == 60


def test_api_create_trip_plan_commits(session) -> None:
    """``get_session`` 依赖不 commit:端点必须自己提交,否则重启就丢方案。"""
    refs = seed_refs(session)
    trips_api.create_trip_plan(payload=plan_payload(refs), session=session)
    session.expunge_all()
    assert trips.count_trip_plans(session) == 1, "commit 后应能读到这份方案"


# --------------------------------------------------------------------------- #
# 方案 API:GET / DELETE
# --------------------------------------------------------------------------- #


def test_api_list_trip_plans_payload_shape(session) -> None:
    refs = seed_refs(session)
    trips_api.create_trip_plan(payload=plan_payload(refs, name="崇明两日游"), session=session)
    tick()
    trips_api.create_trip_plan(payload=plan_payload(refs, name="三月看花"), session=session)

    payload = trips_api.list_trip_plans(limit=None, session=session)
    assert payload["count"] == 2 and payload["total"] == 2 and payload["limit"] is None
    assert payload["kind"] == "estimate" and payload["nights"] == 1
    assert "估算" in payload["note"] and payload["elapsed_s"] >= 0
    assert [item["name"] for item in payload["trip_plans"]] == ["三月看花", "崇明两日游"], "新的在前"
    first = payload["trip_plans"][0]
    assert REQUIRED_DICT_KEYS <= set(first)
    assert first["counts"] == {"place": 1, "routes": 2, "stays": 2}
    assert REQUIRED_QUOTE_KEYS <= set(first["quote"])
    assert first["quote"]["stay_nights"] == 1
    assert first["quote"]["total_cny_low"] == 88 + 275, "列表按 1 晚报"


def test_api_list_trip_plans_limit_and_clamp(session) -> None:
    refs = seed_refs(session)
    for index in range(3):
        trips_api.create_trip_plan(payload=plan_payload(refs, name=f"方案 {index}"), session=session)
        tick()
    assert trips_api.list_trip_plans(limit="2", session=session)["count"] == 2
    assert trips_api.list_trip_plans(limit=None, session=session)["count"] == 3
    clamped = trips_api.list_trip_plans(limit=str(trips_api.MAX_PAGE + 50), session=session)
    assert clamped["limit"] == trips_api.MAX_PAGE and clamped["count"] == 3
    expect_http_error(lambda: trips_api.list_trip_plans(limit="abc", session=session),
                      400, "参数 limit 必须是整数")
    expect_http_error(lambda: trips_api.list_trip_plans(limit="0", session=session),
                      400, "正整数")


def test_api_list_trip_plans_tolerates_fieldinfo_default(session) -> None:
    """单测直调端点函数时 ``Query(None)`` 是 FieldInfo:``_optional_int`` 要当"没给"处理。"""
    from fastapi import Query

    payload = trips_api.list_trip_plans(limit=Query(None), session=session)
    assert payload["limit"] is None and payload["trip_plans"] == [] and payload["total"] == 0


def test_api_get_trip_plan_detail(session) -> None:
    refs = seed_refs(session)
    created = trips_api.create_trip_plan(payload=plan_payload(refs), session=session)
    plan_id = created["trip_plan"]["id"]
    payload = trips_api.get_trip_plan(trip_plan_id=str(plan_id), session=session)
    assert payload["trip_plan"]["id"] == plan_id
    assert payload["trip_plan"]["name"] == "崇明两日游"
    assert payload["quote"] == payload["trip_plan"]["quote"]
    assert payload["nights"] == 1 and payload["quote"]["stay_nights"] == 1
    assert payload["count"] == 1 and "估算" in payload["note"]
    assert len(payload["quote"]["per_stay"]) == 2


def test_api_get_trip_plan_reports_deleted_refs(session) -> None:
    """引用的收藏被删 → 详情照常 200,缺行列进 ``quote.missing``,方案不连带删。"""
    refs = seed_refs(session)
    created = trips_api.create_trip_plan(payload=plan_payload(refs), session=session)
    plan_id = created["trip_plan"]["id"]
    assert repo.delete_collection(session, collection_id=refs["stay_b"].id) is True
    session.commit()

    payload = trips_api.get_trip_plan(trip_plan_id=str(plan_id), session=session)
    assert payload["quote"]["missing"] == [refs["stay_b"].id]
    assert payload["quote"]["total_cny_low"] == 88 + 250, "只剩能报价的那处住宿"
    assert payload["trip_plan"]["counts"]["stays"] == 2, "方案里的引用照旧留着"
    assert trips.count_trip_plans(session) == 1


def test_api_get_trip_plan_bad_id_and_missing(session) -> None:
    expect_http_error(lambda: trips_api.get_trip_plan(trip_plan_id="abc", session=session),
                      400, "方案 id 必须是整数", "'abc'")
    expect_http_error(lambda: trips_api.get_trip_plan(trip_plan_id="0", session=session),
                      400, "方案 id 必须是正整数")
    expect_http_error(lambda: trips_api.get_trip_plan(trip_plan_id="  ", session=session),
                      400, "缺少必要参数:方案 id")
    expect_http_error(lambda: trips_api.get_trip_plan(trip_plan_id="9999", session=session),
                      404, "行程方案不存在", "9999")


def test_api_delete_trip_plan(session) -> None:
    refs = seed_refs(session)
    created = trips_api.create_trip_plan(payload=plan_payload(refs), session=session)
    plan_id = created["trip_plan"]["id"]
    payload = trips_api.delete_trip_plan(trip_plan_id=str(plan_id), session=session)
    assert payload["deleted"] is True and payload["id"] == plan_id
    assert payload["count"] == 0 and "只删这份方案" in payload["note"]
    assert repo.count_collections(session) == 5, "删方案不该动收藏"
    session.expunge_all()
    assert trips.count_trip_plans(session) == 0, "删除也要 commit"


def test_api_delete_trip_plan_bad_id_and_missing(session) -> None:
    expect_http_error(lambda: trips_api.delete_trip_plan(trip_plan_id="abc", session=session),
                      400, "必须是整数")
    expect_http_error(lambda: trips_api.delete_trip_plan(trip_plan_id="-1", session=session),
                      400, "必须是正整数")
    expect_http_error(lambda: trips_api.delete_trip_plan(trip_plan_id="9999", session=session),
                      404, "行程方案不存在")


# --------------------------------------------------------------------------- #
# 完整 HTTP 链(ASGI)+ 路由注册
# --------------------------------------------------------------------------- #


def test_http_post_get_delete_roundtrip(http, session) -> None:
    refs = seed_refs(session)
    session.commit()
    body = plan_payload(refs)

    status, payload = http("POST", "/api/trip-plans", body=body)
    assert status == 200, f"完整 HTTP 链应返回 200,实际 {status}:{payload}"
    assert payload["created"] is True and payload["quote"]["total_cny_low"] == 638
    plan_id = payload["trip_plan"]["id"]

    status, payload = http("POST", "/api/trip-plans", body=body)
    assert status == 200 and payload["created"] is False, "重名应是 200 + created=false"
    assert payload["trip_plan"]["id"] == plan_id

    status, payload = http("GET", "/api/trip-plans")
    assert status == 200 and payload["count"] == 1 and payload["total"] == 1
    assert payload["trip_plans"][0]["quote"]["kind"] == "estimate"

    status, payload = http("GET", "/api/trip-plans", query="limit=1")
    assert status == 200 and payload["count"] == 1 and payload["limit"] == 1

    status, payload = http("GET", f"/api/trip-plans/{plan_id}")
    assert status == 200 and payload["trip_plan"]["name"] == "崇明两日游"

    status, payload = http("DELETE", f"/api/trip-plans/{plan_id}")
    assert status == 200 and payload["deleted"] is True and payload["count"] == 0

    status, payload = http("GET", "/api/trip-plans")
    assert status == 200 and payload["trip_plans"] == []


def test_http_validation_errors_are_400_in_chinese(http, session) -> None:
    """参数不对一律 **400 + 中文**:不该出现 FastAPI 默认的 422 英文报错。"""
    refs = seed_refs(session)
    session.commit()

    status, body = http("POST", "/api/trip-plans", body={"note": "没有名字"})
    assert status == 400 and "name" in body["detail"]

    status, body = http("POST", "/api/trip-plans", body=[1, 2])
    assert status == 400 and "请求体必须是 JSON 对象" in body["detail"]

    status, body = http("POST", "/api/trip-plans", body=plan_payload(refs, place_collection_id=9999))
    assert status == 400 and "未知收藏" in body["detail"]

    status, body = http("POST", "/api/trip-plans", body=plan_payload(refs, nights=0))
    assert status == 400 and "nights" in body["detail"]

    status, body = http("GET", "/api/trip-plans", query="limit=abc")
    assert status == 400 and "必须是整数" in body["detail"]

    status, body = http("GET", "/api/trip-plans/abc")
    assert status == 400 and "必须是整数" in body["detail"]

    status, body = http("GET", "/api/trip-plans/9999")
    assert status == 404 and "行程方案不存在" in body["detail"]

    status, body = http("DELETE", "/api/trip-plans/9999")
    assert status == 404 and "行程方案不存在" in body["detail"]


def test_app_registers_trip_plan_routes_alongside_existing_ones() -> None:
    paths = app.openapi()["paths"]
    assert {"get", "post"} <= set(paths["/api/trip-plans"]), "GET/POST /api/trip-plans 应已注册"
    assert {"get", "delete"} <= set(paths["/api/trip-plans/{trip_plan_id}"]), \
        "GET/DELETE /api/trip-plans/{id} 应已注册"
    existing = {"/api/discover", "/api/categories", "/api/places", "/api/places/meta",
                "/api/places/intros", "/api/geocode", "/api/geocode/reverse", "/api/routes",
                "/api/collections", "/api/stays"}
    assert existing <= set(paths), f"既有路由不该被挤掉:{existing - set(paths)}"


def test_trips_modules_do_not_import_network_clients() -> None:
    """方案报价只读收藏快照,不该触网:模块里不出现 requests / data_sources / LLM 依赖。"""
    for relative in (("services", "trips.py"), ("app", "api", "trips.py")):
        text = Path(BACKEND_DIR).joinpath(*relative).read_text(encoding="utf-8")
        for forbidden in ("import requests", "from data_sources", "urlopen", "services.intro"):
            assert forbidden not in text, f"{'/'.join(relative)} 不该出现 {forbidden}"
