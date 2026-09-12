"""TASK-2c 单测:路线/目的地收藏(Collection)表 + 仓储 CRUD + ``/api/collections``。

全程不触网:

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,任何偷偷联网都会当场失败
  (收藏存的是快照,本来就不该重新算路线);
* DB 用 ``tmp_path`` 下的临时 SQLite 文件,不碰 ``backend/data/``;
* 完整 HTTP 链用手拼的最小 ASGI scope 驱动(仓库没装 httpx/TestClient),
  ``app.dependency_overrides`` 把 ``get_session`` 换成同一个临时库会话。

重点覆盖**幂等**:唯一键 ``(kind, ref_key, mode)`` 让重复收藏既不报错也不产生第二行,
只刷新快照并返回原行(``created=false``、``id`` 与 ``created_at`` 不变)。

运行:``python -m pytest backend/ -q``
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
from sqlalchemy.exc import IntegrityError

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import collections as collections_api  # noqa: E402
from app.main import app  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db import models  # noqa: E402
from db import repository as repo  # noqa: E402
from db.base import get_session  # noqa: E402
from db.models import Collection, CollectionCat  # noqa: E402
from services import routes as route_service  # noqa: E402

# --------------------------------------------------------------------------- #
# 样本数据:上海(起点)→ 崇明(目的地),以及一个"只有坐标"的无名点
# --------------------------------------------------------------------------- #

SHANGHAI = {"lat": 31.2304, "lng": 121.4737}
CHONGMING = {"lat": 31.5, "lng": 121.5}
REQUIRED_COLLECTION_COLUMNS = {
    "kind", "ref_key", "mode", "name", "osm_type", "osm_id",
    "from_lat", "from_lng", "from_name", "to_lat", "to_lng", "to_name",
    "summary", "cat_id", "created_at", "updated_at",
}
REQUIRED_CAT_COLUMNS = {"name", "note", "source", "sort_order", "created_at", "updated_at"}
REQUIRED_DICT_KEYS = {
    "id", "kind", "mode", "ref_key", "name", "osm_type", "osm_id",
    "from_lat", "from_lng", "from_name", "to_lat", "to_lng", "to_name",
    "summary", "cat_id", "cat_name", "created_at", "updated_at",
}
ROUTE_REF = "route:31.2304000,121.4737000->node/7"


def route_payload(**overrides: Any) -> dict[str, Any]:
    """一条"上海 → 崇明 · 驾车"的收藏请求体(``/api/routes`` 返回什么就存什么)。"""
    body: dict[str, Any] = {
        "kind": "route",
        "mode": "driving",
        "osm_type": "node",
        "osm_id": 7,
        "from_lat": SHANGHAI["lat"],
        "from_lng": SHANGHAI["lng"],
        "from_name": "上海",
        "to_lat": CHONGMING["lat"],
        "to_lng": CHONGMING["lng"],
        "to_name": "崇明",
        "summary": {
            "duration_min": 94, "cost_cny": 88, "distance_km": 122.4,
            "kind": "real", "degraded": False, "geometry": [[31.2, 121.4], [31.5, 121.5]],
        },
    }
    body.update(overrides)
    return body


def place_payload(**overrides: Any) -> dict[str, Any]:
    """一条目的地收藏(``kind=place``,没有出行方式)。"""
    body: dict[str, Any] = {
        "kind": "place",
        "osm_type": "way",
        "osm_id": -1234,
        "to_lat": CHONGMING["lat"],
        "to_lng": CHONGMING["lng"],
        "to_name": "西沙湿地",
    }
    body.update(overrides)
    return body


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


def http_request(
    method: str, path: str, *, body: Any = None, query: str = ""
) -> tuple[int, Any]:
    """直接驱动 ASGI app 走一遍**完整 HTTP 链**(含 FastAPI 解析),返回 ``(状态码, JSON)``。

    仓库没装 httpx/TestClient,所以自己拼最小 scope;不触网,DB 会话由 ``api_client`` 注入。
    """
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
    """兜底:任何 requests 调用都视为测试失败(本套单测必须纯 mock)。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture()
def session(tmp_path):
    """每个用例一个独立的临时 SQLite 库。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'collections_test.db'}")
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
    try:
        return lambda method, path, **kwargs: http_request(method, path, **kwargs)
    finally:
        pass
    # 说明:清理放在下面的 yield 版本里(见 api_client_clean),这里保持简单返回


@pytest.fixture()
def http(api_client):
    """同 :func:`api_client`,但用例结束后清理 ``dependency_overrides``,避免串味。"""
    yield api_client
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 表结构与唯一键(幂等的地基)
# --------------------------------------------------------------------------- #


def test_collection_table_has_required_columns_and_unique_key() -> None:
    columns = {column.name for column in Collection.__table__.columns}
    assert REQUIRED_COLLECTION_COLUMNS <= columns, f"Collection 缺字段:{REQUIRED_COLLECTION_COLUMNS - columns}"
    unique = {
        frozenset(column.name for column in constraint.columns)
        for constraint in Collection.__table__.constraints
        if type(constraint).__name__ == "UniqueConstraint"
    }
    assert frozenset({"kind", "ref_key", "mode"}) in unique, \
        f"唯一键应覆盖 (kind, ref_key, mode),实际:{unique}"


def test_collection_cat_id_is_nullable_fk_with_set_null() -> None:
    """分组是**可选**的,且删分组不该连带删收藏(外键声明为 SET NULL)。"""
    column = Collection.__table__.c.cat_id
    assert column.nullable is True, "cat_id 应可空:收藏不挂分组也要能用"
    assert column.primary_key is False
    foreign_keys = list(column.foreign_keys)
    assert {key.column.table.name for key in foreign_keys} == {"collection_cats"}
    assert {key.ondelete for key in foreign_keys} == {"SET NULL"}


def test_collection_cat_table_has_required_columns_and_unique_name() -> None:
    columns = {column.name for column in CollectionCat.__table__.columns}
    assert REQUIRED_CAT_COLUMNS <= columns, f"CollectionCat 缺字段:{REQUIRED_CAT_COLUMNS - columns}"
    unique = {
        frozenset(column.name for column in constraint.columns)
        for constraint in CollectionCat.__table__.constraints
        if type(constraint).__name__ == "UniqueConstraint"
    }
    assert frozenset({"name"}) in unique, f"分组名应唯一,实际:{unique}"


def test_unique_key_blocks_raw_duplicates_but_allows_other_modes(session) -> None:
    """DB 层防重:同 ``(kind, ref_key, mode)`` 直插两行必须被唯一键挡住;换方式则是另一条。"""
    session.add(Collection(kind="route", ref_key=ROUTE_REF, mode="driving", name="甲"))
    session.commit()
    session.add(Collection(kind="route", ref_key=ROUTE_REF, mode="driving", name="乙"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    assert repo.count_collections(session) == 1

    session.add(Collection(kind="route", ref_key=ROUTE_REF, mode="rail", name="丙"))
    session.flush()
    assert repo.count_collections(session) == 2, "同一路线的不同方式应各存一行"


def test_duplicate_cat_name_is_rejected(session) -> None:
    session.add(CollectionCat(name="周末去"))
    session.commit()
    session.add(CollectionCat(name="周末去"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    assert len(repo.list_collection_cats(session)) == 1


# --------------------------------------------------------------------------- #
# 引用串 / 坐标 / 默认标题(纯函数)
# --------------------------------------------------------------------------- #


def test_ref_key_prefers_osm_identity_and_falls_back_to_coordinates() -> None:
    assert models.collection_ref_key(
        kind="route", osm_type="node", osm_id=7,
        from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"],
    ) == ROUTE_REF
    assert models.collection_ref_key(kind="place", osm_type="way", osm_id=-1234) == "place:way/-1234"
    assert models.collection_ref_key(
        kind="place", to_lat=CHONGMING["lat"], to_lng=CHONGMING["lng"]
    ) == "place:31.5000000,121.5000000"
    # kind 进串里:同一个点"收藏目的地"与"收藏到它的路线"互不冲突
    assert models.collection_ref_key(
        kind="route", to_lat=CHONGMING["lat"], to_lng=CHONGMING["lng"],
        from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"],
    ).startswith("route:")


def test_point_key_is_stable_against_float_tails() -> None:
    """浮点尾巴不能把幂等打掉:同一个点两次收藏必须得到同一把钥匙。"""
    assert models.point_key(31.23040000000000004, 121.4737) == models.point_key("31.2304", 121.4737)
    assert models.point_key(31.2304, 121.4737) == "31.2304000,121.4737000"
    assert models.point_key(None, 121.4737) is None
    assert models.point_label(31.2304, 121.4737) == "31.2304,121.4737"


def test_ref_key_rejects_incomplete_reference() -> None:
    expect_error(
        lambda: models.collection_ref_key(kind="place"),
        ValueError, "收藏缺少目的地引用", "osm_type + osm_id", "to_lat + to_lng",
    )
    expect_error(
        lambda: models.collection_ref_key(kind="route", to_lat=31.5, to_lng=121.5),
        ValueError, "收藏路线缺少起点坐标",
    )


def test_kind_and_osm_validation_report_chinese_errors() -> None:
    assert models.collection_kind(" Route ") == "route"
    expect_error(lambda: models.collection_kind("boat"), ValueError, "未知收藏类型", "route", "place")
    expect_error(lambda: models.collection_kind(None), ValueError, "未知收藏类型")
    expect_error(
        lambda: models.osm_key("node", None), ValueError, "OSM 身份要成对给"
    )
    expect_error(lambda: models.osm_key("planet", 7), ValueError, "未知 osm_type", "node")
    expect_error(lambda: models.osm_key("node", 0), ValueError, "osm_id 不能为 0")
    expect_error(lambda: models.osm_key("node", "abc"), ValueError, "osm_id 必须是整数")


def test_coordinate_validation_matches_routes_wording() -> None:
    """坐标校验文案与 :func:`services.routes.require_coordinates` 同口径(前端提示不分两处)。"""
    assert models.optional_coordinate("lat", "31.2304", limit=models.LAT_LIMIT) == 31.2304
    assert models.optional_coordinate("lat", "  ", limit=models.LAT_LIMIT) is None
    expect_error(
        lambda: models.optional_coordinate("lat", "abc", limit=models.LAT_LIMIT),
        ValueError, "参数 lat 必须为数字",
    )
    expect_error(
        lambda: models.optional_coordinate("lat", 91, limit=models.LAT_LIMIT),
        ValueError, "参数 lat 超出 [-90, 90] 范围",
    )
    expect_error(
        lambda: models.optional_coordinate("lng", float("nan"), limit=models.LNG_LIMIT),
        ValueError, "不是有效数字",
    )
    expect_error(
        lambda: models.optional_coordinate("lat", True, limit=models.LAT_LIMIT),
        ValueError, "必须为数字",
    )


def test_default_collection_name_covers_routes_places_and_fallbacks() -> None:
    assert models.default_collection_name(
        kind="route", mode_label="驾车", from_name="上海", to_name="崇明"
    ) == "上海 → 崇明 · 驾车"
    assert models.default_collection_name(
        kind="place", to_name="西沙湿地"
    ) == "西沙湿地"
    # 没有地名就退化成坐标短标签,再缺就用兜底文案:收藏列表里每行都要可读
    assert models.default_collection_name(
        kind="route", mode_label="铁路(估算)",
        from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"], to_name="南京",
    ) == "31.2304,121.4737 → 南京 · 铁路(估算)"
    assert models.default_collection_name(kind="route") == "我的位置 → 目的地"
    assert models.default_collection_name(kind="place") == "目的地"
    assert len(models.default_collection_name(kind="place", to_name="长" * 400)) <= models.NAME_LEN


# --------------------------------------------------------------------------- #
# 快照摘要归一
# --------------------------------------------------------------------------- #


def test_summary_keeps_canonical_keys_and_drops_geometry() -> None:
    summary = repo.collection_summary(
        {"duration_min": "94", "cost_cny": 88.0, "distance_km": 122.4,
         "kind": "real", "degraded": False, "geometry": [[31.2, 121.4]]},
        mode="driving",
    )
    assert summary == {
        "mode": "driving", "duration_min": 94, "cost_cny": 88, "distance_km": 122.4,
        "kind": "real", "degraded": False,
    }, f"应补规范键、丢掉 geometry、整数不写成浮点,实际:{summary}"


def test_summary_defaults_missing_numbers_to_none_and_never_invents_values() -> None:
    """OSRM 降级时数字本来就是 null:照实存 ``None``,不瞎造数字。"""
    assert repo.collection_summary(None) == {
        "mode": "", "duration_min": None, "cost_cny": None, "distance_km": None,
    }
    summary = repo.collection_summary(
        {"duration_min": "abc", "cost_cny": float("nan"), "distance_km": True}, mode="rail"
    )
    assert summary["duration_min"] is None
    assert summary["cost_cny"] is None
    assert summary["distance_km"] is None
    assert summary["mode"] == "rail"


def test_summary_mode_argument_wins_over_payload_mode() -> None:
    """``place`` 收藏的 mode 恒为空串,不能被请求体里的值带偏。"""
    assert repo.collection_summary({"mode": "driving"}, mode=models.NO_MODE)["mode"] == ""
    assert repo.collection_summary({"mode": "rail"})["mode"] == "rail"


# --------------------------------------------------------------------------- #
# 仓储:写入与幂等
# --------------------------------------------------------------------------- #


def test_upsert_collection_writes_snapshot(session) -> None:
    row, created = repo.upsert_collection(
        session, kind="route", mode="driving", osm_type="node", osm_id=7,
        from_lat="31.2304", from_lng=SHANGHAI["lng"], from_name="上海",
        to_lat=CHONGMING["lat"], to_lng=CHONGMING["lng"], to_name="崇明",
        summary={"duration_min": 94, "cost_cny": 88, "distance_km": 122.4, "kind": "real"},
    )
    assert created is True
    assert row.ref_key == ROUTE_REF
    assert row.name == "上海 → 崇明"          # 仓储层不猜方式标签,标签由 API 层补
    assert (row.osm_type, row.osm_id) == ("node", 7)
    assert row.from_lat == pytest.approx(31.2304)
    assert row.summary["duration_min"] == 94 and row.summary["mode"] == "driving"
    assert row.cat_id is None
    assert REQUIRED_DICT_KEYS <= set(repo.collection_to_dict(row))


def test_upsert_collection_is_idempotent_and_refreshes_snapshot(session) -> None:
    """重复收藏:同一行、``created=False``、快照刷新,但 ``id`` 与 ``created_at`` 不变。"""
    first, created = repo.upsert_collection(
        session, kind="route", mode="driving", osm_type="node", osm_id=7,
        from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"], to_name="崇明",
        summary={"duration_min": 94, "cost_cny": 88},
    )
    assert created is True
    first_id, first_created_at = first.id, first.created_at
    tick()

    second, created_again = repo.upsert_collection(
        session, kind="route", mode="driving", osm_type="node", osm_id=7,
        from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"], to_name="崇明",
        summary={"duration_min": 80, "cost_cny": 70, "distance_km": 118.0, "kind": "real"},
    )
    assert created_again is False, "重复收藏不该报『已新建』"
    assert second.id == first_id, "重复收藏必须命中同一行"
    assert second.created_at == first_created_at, "收藏时间应保留第一次收藏的时刻"
    assert second.summary["duration_min"] == 80 and second.summary["kind"] == "real"
    assert second.updated_at > second.created_at
    assert repo.count_collections(session) == 1, "重复收藏不该产生第二行"


def test_string_coordinates_and_float_tails_hit_the_same_row(session) -> None:
    """"31.2304" 与 31.23040000000000004 是同一个点:必须收藏成同一行。"""
    repo.upsert_collection(session, kind="place", to_lat="31.2304", to_lng="121.4737")
    _, created = repo.upsert_collection(
        session, kind="place", to_lat=31.23040000000000004, to_lng=121.4737000000001
    )
    assert created is False and repo.count_collections(session) == 1


def test_mode_splits_routes_but_place_ignores_mode(session) -> None:
    """同一对起终点的不同方式是不同收藏;``place`` 收藏的 mode 恒为空串(否则幂等失效)。"""
    driving, _ = repo.upsert_collection(
        session, kind="route", mode="driving",
        from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"], osm_type="node", osm_id=7,
    )
    rail, _ = repo.upsert_collection(
        session, kind="route", mode="rail",
        from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"], osm_type="node", osm_id=7,
    )
    assert driving.ref_key == rail.ref_key, "ref_key 不含方式:驾车与铁路是同一个引用串"
    assert driving.id != rail.id, "同一引用串的不同方式应各存一行"
    assert repo.count_collections(session) == 2
    assert repo.count_by_kind(session) == {"route": 2, "place": 0}

    first, _ = repo.upsert_collection(session, kind="place", mode="driving", osm_type="node", osm_id=7)
    second, created = repo.upsert_collection(session, kind="place", mode="flight", osm_type="node", osm_id=7)
    assert first.mode == models.NO_MODE == second.mode, "place 收藏不该带方式"
    assert created is False and second.id == first.id
    assert repo.count_by_kind(session) == {"route": 2, "place": 1}


def test_place_and_route_of_same_target_do_not_collide(session) -> None:
    """引用串带 kind 前缀:同一个点"收藏目的地"与"收藏到它的路线"互不覆盖。"""
    route_row, _ = repo.upsert_collection(
        session, kind="route", mode="driving", osm_type="node", osm_id=7,
        from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"],
    )
    place_row, created = repo.upsert_collection(session, kind="place", osm_type="node", osm_id=7)
    assert created is True and place_row.id != route_row.id
    assert repo.count_collections(session) == 2


def test_upsert_collection_rejects_bad_input(session) -> None:
    expect_error(
        lambda: repo.upsert_collection(session, kind="boat", mode="driving"),
        ValueError, "未知收藏类型",
    )
    expect_error(
        lambda: repo.upsert_collection(session, kind="route", mode="driving"),
        ValueError, "收藏缺少目的地引用",
    )
    expect_error(
        lambda: repo.upsert_collection(
            session, kind="route", mode="driving", to_lat=31.5, to_lng=121.5
        ),
        ValueError, "收藏路线缺少起点坐标",
    )
    expect_error(
        lambda: repo.upsert_collection(
            session, kind="place", to_lat=999, to_lng=121.5
        ),
        ValueError, "超出 [-90, 90] 范围",
    )
    expect_error(
        lambda: repo.upsert_collection(
            session, kind="place", to_lat=31.5, to_lng=121.5, cat_id=999
        ),
        ValueError, "未知收藏分组", "cat_id=999",
    )
    assert repo.count_collections(session) == 0, "参数非法时应快速失败,不落库"


def test_upsert_collection_truncates_long_text(session) -> None:
    """前端多带几个字不该撞 ``String(n)``:按列宽截断后照常入库。"""
    row, _ = repo.upsert_collection(
        session, kind="place", to_lat=31.5, to_lng=121.5,
        name="长" * 400, to_name="名" * 400,
    )
    assert len(row.name) <= models.NAME_LEN
    assert len(row.to_name) <= models.POINT_NAME_LEN


def test_explicit_ref_key_is_honoured(session) -> None:
    """``ref_key`` 留给脚本/M4 显式指定(常规调用按坐标与 OSM 身份自动算)。"""
    row, created = repo.upsert_collection(
        session, kind="route", mode="driving", ref_key="route:custom->key",
        from_lat=31.0, from_lng=121.0, to_lat=32.0, to_lng=122.0,
    )
    assert created is True and row.ref_key == "route:custom->key"
    again, created_again = repo.upsert_collection(
        session, kind="route", mode="driving", ref_key="route:custom->key",
        from_lat=31.0, from_lng=121.0, to_lat=32.0, to_lng=122.0,
    )
    assert created_again is False and again.id == row.id


def test_get_collection_roundtrip(session) -> None:
    row, _ = repo.upsert_collection(session, kind="place", to_lat=31.5, to_lng=121.5)
    assert repo.get_collection(session, collection_id=row.id).id == row.id
    expect_error(
        lambda: repo.get_collection(session, collection_id="  "),
        ValueError, "缺少必要参数:收藏 id",
    )
    expect_error(
        lambda: repo.get_collection(session, collection_id="abc"),
        ValueError, "收藏 id 必须是整数",
    )
    expect_error(
        lambda: repo.get_collection(session, collection_id=0),
        ValueError, "收藏 id 必须是正整数",
    )


# --------------------------------------------------------------------------- #
# 仓储:列表 / 计数 / 删除
# --------------------------------------------------------------------------- #


def seed_three(session) -> list[Collection]:
    """造三条收藏:崇明驾车、崇明铁路、西沙湿地(place),按写入时间递增。"""
    rows = [
        repo.upsert_collection(
            session, kind="route", mode="driving", osm_type="node", osm_id=7,
            from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"], to_name="崇明",
            summary={"duration_min": 94},
        )[0],
        repo.upsert_collection(
            session, kind="route", mode="rail", osm_type="node", osm_id=7,
            from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"], to_name="崇明",
            summary={"duration_min": 180},
        )[0],
        repo.upsert_collection(
            session, kind="place", osm_type="way", osm_id=-1234,
            to_lat=CHONGMING["lat"], to_lng=CHONGMING["lng"], to_name="西沙湿地",
        )[0],
    ]
    for _ in rows:
        tick()
    return rows


def test_list_collections_returns_newest_first(session) -> None:
    driving, rail, place = seed_three(session)
    items = repo.list_collections(session)
    assert [item["id"] for item in items] == [place.id, rail.id, driving.id], "列表应新的在前"
    assert REQUIRED_DICT_KEYS <= set(items[0])
    assert items[0]["summary"]["mode"] == models.NO_MODE


def test_list_collections_filters_by_kind_mode_cat_and_limit(session) -> None:
    driving, rail, place = seed_three(session)
    cat, _ = repo.upsert_collection_cat(session, name="周末去")
    tick()
    grouped, grouped_created = repo.upsert_collection(
        session, kind="place", to_lat=31.6, to_lng=121.6, to_name="东滩湿地", cat_id=cat.id,
    )
    assert grouped_created is True, "换个目的地才算新收藏"

    assert [item["id"] for item in repo.list_collections(session, kind="route")] == [rail.id, driving.id]
    assert [item["id"] for item in repo.list_collections(session, kind="place")] == [grouped.id, place.id]
    assert [item["id"] for item in repo.list_collections(session, mode="rail")] == [rail.id]
    assert [item["id"] for item in repo.list_collections(session, cat_id=cat.id)] == [grouped.id]
    assert len(repo.list_collections(session, limit=2)) == 2
    expect_error(lambda: repo.list_collections(session, kind="boat"), ValueError, "未知收藏类型")
    expect_error(lambda: repo.list_collections(session, cat_id=999), ValueError, "未知收藏分组")


def test_list_collections_attaches_cat_name(session) -> None:
    cat, _ = repo.upsert_collection_cat(session, name="雪季计划")
    repo.upsert_collection(session, kind="place", to_lat=31.5, to_lng=121.5, cat_id=cat.id)
    repo.upsert_collection(session, kind="place", to_lat=32.5, to_lng=121.5)
    listed = repo.list_collections(session)
    assert {item["cat_name"] for item in listed} == {"雪季计划", None}


def test_counts_cover_kind_and_cat_filters(session) -> None:
    seed_three(session)
    cat, _ = repo.upsert_collection_cat(session, name="周末去")
    repo.upsert_collection(session, kind="place", to_lat=31.6, to_lng=121.6, cat_id=cat.id)
    assert repo.count_collections(session) == 4
    assert repo.count_collections(session, kind="route") == 2
    assert repo.count_collections(session, kind="place") == 2
    assert repo.count_collections(session, cat_id=cat.id) == 1
    assert repo.count_by_kind(session) == {"route": 2, "place": 2}


def test_count_by_kind_always_reports_both_kinds(session) -> None:
    """两个键恒在,前端不必判空。"""
    assert repo.count_by_kind(session) == {"route": 0, "place": 0}
    repo.upsert_collection(session, kind="place", to_lat=31.5, to_lng=121.5)
    assert repo.count_by_kind(session) == {"route": 0, "place": 1}


def test_delete_collection_reports_whether_a_row_went_away(session) -> None:
    row, _ = repo.upsert_collection(session, kind="place", to_lat=31.5, to_lng=121.5)
    assert repo.delete_collection(session, collection_id=row.id) is True
    assert repo.count_collections(session) == 0
    assert repo.delete_collection(session, collection_id=row.id) is False, "删第二次应返回 False"
    assert repo.delete_collection(session, collection_id=9999) is False
    expect_error(
        lambda: repo.delete_collection(session, collection_id=0),
        ValueError, "收藏 id 必须是正整数",
    )
    expect_error(
        lambda: repo.delete_collection(session, collection_id="abc"),
        ValueError, "收藏 id 必须是整数",
    )


def test_limit_is_clamped_to_at_least_one(session) -> None:
    seed_three(session)
    assert len(repo.list_collections(session, limit=0)) == 3, "limit=0 视为不限"
    assert len(repo.list_collections(session, limit=-5)) == 1


# --------------------------------------------------------------------------- #
# 仓储:收藏分组(CollectionCat,为 M4 铺路)
# --------------------------------------------------------------------------- #


def test_upsert_collection_cat_is_idempotent_by_name(session) -> None:
    cat, created = repo.upsert_collection_cat(session, name=" 周末去 ", note="两天一夜")
    assert created is True and cat.name == "周末去" and cat.note == "两天一夜"
    assert cat.source == models.CAT_MANUAL and cat.sort_order == 0

    same, created_again = repo.upsert_collection_cat(
        session, name="周末去", source=models.CAT_AUTO, sort_order=3
    )
    assert created_again is False and same.id == cat.id
    assert same.source == models.CAT_AUTO and same.sort_order == 3
    assert same.note == "两天一夜", "没给 note 时不该把已有备注抹掉"
    assert len(repo.list_collection_cats(session)) == 1


def test_upsert_collection_cat_rejects_empty_name_and_bad_source(session) -> None:
    expect_error(lambda: repo.upsert_collection_cat(session, name="   "), ValueError, "分组名不能为空")
    expect_error(lambda: repo.upsert_collection_cat(session, name=None), ValueError, "分组名不能为空")
    expect_error(
        lambda: repo.upsert_collection_cat(session, name="甲", source="robot"),
        ValueError, "未知分组来源", "manual", "auto",
    )


def test_list_collection_cats_orders_and_counts(session) -> None:
    late, _ = repo.upsert_collection_cat(session, name="乙", sort_order=5)
    early, _ = repo.upsert_collection_cat(session, name="甲", sort_order=1)
    repo.upsert_collection(session, kind="place", to_lat=31.5, to_lng=121.5, cat_id=early.id)
    repo.upsert_collection(session, kind="place", to_lat=31.6, to_lng=121.6, cat_id=early.id)
    repo.upsert_collection(session, kind="place", to_lat=31.7, to_lng=121.7, cat_id=late.id)

    listed = repo.list_collection_cats(session)
    assert [item["name"] for item in listed] == ["甲", "乙"], "先按 sort_order,再按名字"
    assert [item["collection_count"] for item in listed] == [2, 1]
    assert listed[0]["collection_count"] == 2 and listed[0]["source"] == models.CAT_MANUAL
    assert {"id", "name", "note", "source", "sort_order", "collection_count",
            "created_at", "updated_at"} <= set(listed[0])


def test_get_collection_cat_by_id_and_name(session) -> None:
    cat, _ = repo.upsert_collection_cat(session, name="周末去")
    assert repo.get_collection_cat(session, cat_id=cat.id).name == "周末去"
    assert repo.get_collection_cat(session, name="周末去").id == cat.id
    assert repo.get_collection_cat(session) is None
    assert repo.get_collection_cat(session, cat_id=9999) is None


def test_delete_collection_cat_detaches_collections_instead_of_deleting(session) -> None:
    """删分组只把旗下收藏**摘下来**:整理标签时不该误删路线。"""
    cat, _ = repo.upsert_collection_cat(session, name="周末去")
    first, _ = repo.upsert_collection(session, kind="place", to_lat=31.5, to_lng=121.5, cat_id=cat.id)
    second, _ = repo.upsert_collection(
        session, kind="route", mode="driving", osm_type="node", osm_id=7,
        from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"], cat_id=cat.id,
    )
    assert repo.delete_collection_cat(session, cat_id=cat.id) is True
    assert repo.count_collections(session) == 2, "收藏不该被连带删掉"
    assert repo.list_collection_cats(session) == []
    assert [item["cat_id"] for item in repo.list_collections(session)] == [None, None]
    assert repo.get_collection(session, collection_id=first.id).cat_id is None
    assert repo.get_collection(session, collection_id=second.id).cat_id is None
    assert repo.delete_collection_cat(session, cat_id=cat.id) is False


# --------------------------------------------------------------------------- #
# 收藏 API:POST /api/collections(幂等)
# --------------------------------------------------------------------------- #


def test_api_create_collection_payload_shape(session) -> None:
    payload = collections_api.create_collection(payload=route_payload(), session=session)
    collection = payload["collection"]
    assert payload["created"] is True and payload["idempotent"] is False
    assert payload["count"] == 1 and payload["counts_by_kind"] == {"route": 1, "place": 0}
    assert payload["elapsed_s"] >= 0 and "幂等" in payload["note"]
    assert REQUIRED_DICT_KEYS <= set(collection)
    assert collection["ref_key"] == ROUTE_REF
    assert collection["name"] == "上海 → 崇明 · 驾车", "API 层应把方式标签补进默认标题"
    assert collection["summary"] == {
        "mode": "driving", "duration_min": 94, "cost_cny": 88, "distance_km": 122.4,
        "kind": "real", "degraded": False,
    }, f"geometry 不该入库,实际:{collection['summary']}"
    assert collection["from_name"] == "上海" and collection["to_name"] == "崇明"
    assert collection["created_at"] and collection["updated_at"]


def test_api_create_collection_is_idempotent(session) -> None:
    """重复收藏返回 ``created=false`` + 原行 id,不报错、不产生第二行。"""
    first = collections_api.create_collection(payload=route_payload(), session=session)
    tick()
    second = collections_api.create_collection(
        payload=route_payload(summary={"duration_min": 80, "cost_cny": 70}), session=session
    )
    assert first["created"] is True
    assert second["created"] is False and second["idempotent"] is True
    assert second["collection"]["id"] == first["collection"]["id"]
    assert second["collection"]["created_at"] == first["collection"]["created_at"]
    assert second["collection"]["summary"]["duration_min"] == 80, "重复收藏应刷新快照"
    assert second["count"] == 1


def test_api_create_place_collection_has_no_mode(session) -> None:
    payload = collections_api.create_collection(payload=place_payload(), session=session)
    collection = payload["collection"]
    assert collection["kind"] == "place" and collection["mode"] == ""
    assert collection["name"] == "西沙湿地"
    assert collection["ref_key"] == "place:way/-1234"
    assert collection["summary"] == {
        "mode": "", "duration_min": None, "cost_cny": None, "distance_km": None,
    }
    # place 收藏带不带 mode 都幂等(mode 恒空串)
    again = collections_api.create_collection(payload=place_payload(mode="driving"), session=session)
    assert again["created"] is False and again["collection"]["id"] == collection["id"]


def test_api_create_collection_accepts_custom_name_and_cat(session) -> None:
    cat, _ = repo.upsert_collection_cat(session, name="雪季计划")
    payload = collections_api.create_collection(
        payload=route_payload(name="  春节回南京  ", cat_id=cat.id), session=session
    )
    assert payload["collection"]["name"] == "春节回南京"
    assert payload["collection"]["cat_id"] == cat.id
    assert payload["collection"]["cat_name"] == "雪季计划"


def test_api_create_collection_rejects_bad_payload(session) -> None:
    expect_http_error(
        lambda: collections_api.create_collection(payload=None, session=session),
        400, "缺少请求体", "JSON 对象",
    )
    expect_http_error(
        lambda: collections_api.create_collection(payload=[1, 2], session=session),
        400, "请求体必须是 JSON 对象", "list",
    )
    expect_http_error(
        lambda: collections_api.create_collection(payload={"mode": "driving"}, session=session),
        400, "未知收藏类型", "route", "place",
    )
    expect_http_error(
        lambda: collections_api.create_collection(payload={"kind": "boat"}, session=session),
        400, "未知收藏类型", "'boat'",
    )
    expect_http_error(
        lambda: collections_api.create_collection(
            payload={"kind": "route", "to_lat": 31.5, "to_lng": 121.5,
                     "from_lat": 31.2, "from_lng": 121.4}, session=session),
        400, "缺少必要参数:mode", "driving", "rail", "flight",
    )
    expect_http_error(
        lambda: collections_api.create_collection(
            payload=route_payload(mode="teleport"), session=session),
        400, "未知出行方式", "driving",
    )
    expect_http_error(
        lambda: collections_api.create_collection(
            payload={"kind": "route", "mode": "driving"}, session=session),
        400, "收藏缺少目的地引用",
    )
    expect_http_error(
        lambda: collections_api.create_collection(
            payload={"kind": "route", "mode": "driving", "to_lat": 31.5, "to_lng": 121.5},
            session=session),
        400, "收藏路线缺少起点坐标",
    )
    expect_http_error(
        lambda: collections_api.create_collection(
            payload=route_payload(from_lat="abc"), session=session),
        400, "必须为数字",
    )
    expect_http_error(
        lambda: collections_api.create_collection(
            payload=route_payload(to_lat=999), session=session),
        400, "超出 [-90, 90] 范围",
    )
    expect_http_error(
        lambda: collections_api.create_collection(
            payload=route_payload(summary="不是对象"), session=session),
        400, "summary 必须是 JSON 对象", "str",
    )
    expect_http_error(
        lambda: collections_api.create_collection(
            payload=route_payload(cat_id=999), session=session),
        400, "未知收藏分组", "cat_id=999",
    )
    assert repo.count_collections(session) == 0, "参数非法时应快速失败,不落库"


def test_api_create_collection_commits(session) -> None:
    """``get_session`` 依赖不 commit:端点必须自己提交,否则重启就丢收藏。"""
    collections_api.create_collection(payload=route_payload(), session=session)
    session.expunge_all()
    assert repo.count_collections(session) == 1, "commit 后新会话应能读到这条收藏"


# --------------------------------------------------------------------------- #
# 收藏 API:GET /api/collections
# --------------------------------------------------------------------------- #


def test_api_list_collections_payload_shape(session) -> None:
    seed_three(session)
    payload = collections_api.list_collections(kind=None, cat=None, limit=None, session=session)
    assert payload["count"] == 3 and payload["total"] == 3
    assert payload["kind"] is None and payload["cat_id"] is None
    assert payload["counts_by_kind"] == {"route": 2, "place": 1}
    assert payload["kinds"] == ["route", "place"]
    assert payload["modes"] == list(route_service.MODES)
    assert payload["cats"] == []
    assert REQUIRED_DICT_KEYS <= set(payload["collections"][0])
    assert "快照" in payload["note"]


def test_api_list_collections_filters_by_kind(session) -> None:
    seed_three(session)
    routes_only = collections_api.list_collections(kind="route", cat=None, limit=None, session=session)
    assert routes_only["count"] == 2 and routes_only["kind"] == "route"
    assert all(item["kind"] == "route" for item in routes_only["collections"])
    assert routes_only["total"] == 3, "total 是库里的全量,不随过滤变"

    places_only = collections_api.list_collections(kind=" place ", cat=None, limit=None, session=session)
    assert places_only["count"] == 1 and places_only["kind"] == "place"


def test_api_list_collections_filters_by_cat_and_limit(session) -> None:
    seed_three(session)
    cat, _ = repo.upsert_collection_cat(session, name="周末去")
    repo.upsert_collection(session, kind="place", to_lat=31.9, to_lng=121.9, cat_id=cat.id)

    payload = collections_api.list_collections(kind=None, cat=str(cat.id), limit=None, session=session)
    assert payload["count"] == 1 and payload["cat_id"] == cat.id
    assert payload["collections"][0]["cat_name"] == "周末去"
    assert payload["cats"][0]["collection_count"] == 1

    limited = collections_api.list_collections(kind=None, cat=None, limit="2", session=session)
    assert limited["count"] == 2
    capped = collections_api.list_collections(
        kind=None, cat=None, limit=str(collections_api.MAX_PAGE + 50), session=session
    )
    assert capped["count"] == 4, "超过上限的 limit 应按 MAX_PAGE 截断"


def test_api_list_collections_rejects_bad_query(session) -> None:
    expect_http_error(
        lambda: collections_api.list_collections(kind="boat", cat=None, limit=None, session=session),
        400, "未知收藏类型", "route", "place",
    )
    expect_http_error(
        lambda: collections_api.list_collections(kind=None, cat="abc", limit=None, session=session),
        400, "参数 cat 必须是整数",
    )
    expect_http_error(
        lambda: collections_api.list_collections(kind=None, cat="0", limit=None, session=session),
        400, "参数 cat 必须是正整数",
    )
    expect_http_error(
        lambda: collections_api.list_collections(kind=None, cat=None, limit="abc", session=session),
        400, "参数 limit 必须是整数",
    )
    expect_http_error(
        lambda: collections_api.list_collections(kind=None, cat="999", limit=None, session=session),
        400, "未知收藏分组", "cat_id=999",
    )


def test_api_list_collections_on_empty_db(session) -> None:
    payload = collections_api.list_collections(kind=None, cat=None, limit=None, session=session)
    assert payload == {**payload, "collections": [], "count": 0, "total": 0,
                       "counts_by_kind": {"route": 0, "place": 0}}


# --------------------------------------------------------------------------- #
# 收藏 API:DELETE /api/collections/{id}
# --------------------------------------------------------------------------- #


def test_api_delete_collection(session) -> None:
    created = collections_api.create_collection(payload=place_payload(), session=session)
    collection_id = created["collection"]["id"]
    payload = collections_api.delete_collection(collection_id=str(collection_id), session=session)
    assert payload["deleted"] is True and payload["id"] == collection_id
    assert payload["count"] == 0 and payload["counts_by_kind"] == {"route": 0, "place": 0}
    assert "只删这一条收藏" in payload["note"]
    assert repo.get_collection(session, collection_id=collection_id) is None


def test_api_delete_collection_reports_missing_as_404(session) -> None:
    expect_http_error(
        lambda: collections_api.delete_collection(collection_id="9999", session=session),
        404, "收藏不存在", "id=9999",
    )


def test_api_delete_collection_rejects_bad_id(session) -> None:
    expect_http_error(
        lambda: collections_api.delete_collection(collection_id="abc", session=session),
        400, "收藏 id 必须是整数", "'abc'",
    )
    expect_http_error(
        lambda: collections_api.delete_collection(collection_id="0", session=session),
        400, "收藏 id 必须是正整数",
    )
    expect_http_error(
        lambda: collections_api.delete_collection(collection_id="  ", session=session),
        400, "缺少必要参数:收藏 id",
    )


def test_api_delete_collection_commits(session) -> None:
    created = collections_api.create_collection(payload=place_payload(), session=session)
    collections_api.delete_collection(collection_id=str(created["collection"]["id"]), session=session)
    session.expunge_all()
    assert repo.count_collections(session) == 0, "删除也要 commit,否则重启又回来了"


# --------------------------------------------------------------------------- #
# 完整 HTTP 链(ASGI)+ 路由注册
# --------------------------------------------------------------------------- #


def test_http_post_get_delete_roundtrip(http) -> None:
    status, body = http("POST", "/api/collections", body=route_payload())
    assert status == 200, f"完整 HTTP 链应返回 200,实际 {status}:{body}"
    assert body["created"] is True and body["collection"]["name"] == "上海 → 崇明 · 驾车"
    collection_id = body["collection"]["id"]

    status, body = http("POST", "/api/collections", body=route_payload())
    assert status == 200 and body["created"] is False, "重复收藏应是 200 + created=false,不是 4xx/5xx"
    assert body["collection"]["id"] == collection_id

    status, body = http("GET", "/api/collections", query="kind=route")
    assert status == 200 and body["count"] == 1 and body["counts_by_kind"]["route"] == 1

    status, body = http("DELETE", f"/api/collections/{collection_id}")
    assert status == 200 and body["deleted"] is True and body["count"] == 0

    status, body = http("GET", "/api/collections")
    assert status == 200 and body["collections"] == []


def test_http_validation_errors_are_400_in_chinese(http) -> None:
    """参数不对一律 **400 + 中文**:不该出现 FastAPI 默认的 422 英文报错。"""
    status, body = http("POST", "/api/collections", body={"kind": "boat"})
    assert status == 400 and "未知收藏类型" in body["detail"]

    status, body = http("POST", "/api/collections", body=[1, 2])
    assert status == 400 and "请求体必须是 JSON 对象" in body["detail"]

    status, body = http("POST", "/api/collections", body={"kind": "route", "mode": "driving"})
    assert status == 400 and "收藏缺少目的地引用" in body["detail"]

    status, body = http("GET", "/api/collections", query="kind=boat")
    assert status == 400 and "未知收藏类型" in body["detail"]

    status, body = http("DELETE", "/api/collections/abc")
    assert status == 400 and "必须是整数" in body["detail"]

    status, body = http("DELETE", "/api/collections/9999")
    assert status == 404 and "收藏不存在" in body["detail"]


def test_http_place_collection_and_cat_listing(http) -> None:
    status, body = http("POST", "/api/collections", body=place_payload())
    assert status == 200 and body["collection"]["mode"] == ""
    status, body = http("GET", "/api/collections")
    assert status == 200 and body["counts_by_kind"] == {"route": 0, "place": 1}
    assert body["cats"] == [] and body["kinds"] == ["route", "place"]


def test_app_registers_collection_routes_alongside_existing_ones() -> None:
    paths = app.openapi()["paths"]
    assert {"get", "post"} <= set(paths["/api/collections"]), "GET/POST /api/collections 应已注册"
    assert "delete" in paths["/api/collections/{collection_id}"], "DELETE /api/collections/{id} 应已注册"
    existing = {"/api/discover", "/api/categories", "/api/places", "/api/places/meta",
                "/api/places/intros", "/api/geocode", "/api/geocode/reverse", "/api/routes"}
    assert existing <= set(paths), f"既有路由不该被挤掉:{existing - set(paths)}"


def test_collection_module_does_not_import_network_clients() -> None:
    """收藏是快照,不该触网:模块里不出现 requests / data_sources 依赖。"""
    source = Path(BACKEND_DIR) / "app" / "api" / "collections.py"
    text = source.read_text(encoding="utf-8")
    for forbidden in ("import requests", "from data_sources", "urlopen"):
        assert forbidden not in text, f"收藏 API 不该{forbidden}"
