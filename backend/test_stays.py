"""TASK-3a1 单测:住宿(Stay)表 + :mod:`services.stays` 服务层。

全程不触网、不调真 LLM:

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,任何偷偷联网当场失败;
* Overpass 用假客户端(只实现 ``execute``),LLM 用假 client(只实现 ``enabled``/``chat``);
* DB 用 ``tmp_path`` 下的临时 SQLite,不碰 ``backend/data/``。

重点覆盖**幂等与降级**:唯一键 ``(osm_type, osm_id)`` 让重抓不产生第二行,LLM 产物
(``price_estimate``/``intro``)一旦生成就**不被重抓覆盖**;未配 key / 超时 / 限流 /
输出格式不对一律降级成空串,绝不抛异常。

运行:``cd backend && ../.venv/bin/python -m pytest test_stays.py -q``
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

import pytest
import requests
from sqlalchemy import JSON, Float, Integer, String, Text, select
from sqlalchemy.exc import IntegrityError

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from data_sources import DataSourceError  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db.models import (  # noqa: E402
    COORD_PRECISION,
    DEFAULT_CURRENCY,
    KIND_LEN,
    NAME_LEN,
    PRICE_LEN,
    Stay,
    iso_utc,
)
from services import stays as stay_service  # noqa: E402

# --------------------------------------------------------------------------- #
# 样本数据:起点上海人民广场,半径 8 km 内四种住宿(含一处无名公寓)
# --------------------------------------------------------------------------- #

ORIGIN_LAT = 31.2304
ORIGIN_LNG = 121.4737
GOOD_COMPLETION = "价格: 约¥200-400/晚\n简介: 位于市中心的经济型酒店。"
REQUIRED_STAY_COLUMNS = {
    "osm_type", "osm_id", "name", "kind", "lat", "lng", "tags",
    "distance_km", "price_estimate", "currency", "intro", "fetched_at",
}
REQUIRED_DICT_KEYS = {
    "id", "osm_type", "osm_id", "name", "kind", "lat", "lng", "tags",
    "distance_km", "price_estimate", "price_is_estimate", "currency",
    "intro", "fetched_at", "source",
}


def node(osm_id: int, lat: float, lng: float, tags: dict[str, Any]) -> dict[str, Any]:
    """一个 Overpass node element。"""
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lng, "tags": dict(tags)}


def way(osm_id: int, lat: float, lng: float, tags: dict[str, Any]) -> dict[str, Any]:
    """一个 way element:坐标只在 ``center`` 里(:func:`overpass.parse_element` 会取)。"""
    return {"type": "way", "id": osm_id, "center": {"lat": lat, "lon": lng}, "tags": dict(tags)}


SAMPLE_ELEMENTS: list[dict[str, Any]] = [
    node(1, 31.2400, 121.4900, {"tourism": "hotel", "name": "外滩华尔道夫酒店", "stars": "5"}),
    node(2, 31.2350, 121.4800, {"tourism": "hostel", "name": "老船长青旅"}),
    way(3, 31.2600, 121.5000, {"tourism": "guest_house", "name": "衡山路小筑"}),
    node(4, 31.2200, 121.4600, {"tourism": "apartment"}),
]
# 由近及远的预期顺序(见各用例断言)
SAMPLE_ORDER = ["老船长青旅", "", "外滩华尔道夫酒店", "衡山路小筑"]


def sample_payload() -> dict[str, Any]:
    return {"elements": [dict(item) for item in SAMPLE_ELEMENTS]}


def row(**overrides: Any) -> dict[str, Any]:
    """一条 upsert 入参(:func:`stays.search_stays` 的输出形状)。"""
    body: dict[str, Any] = {
        "osm_type": "node",
        "osm_id": 11,
        "name": "示例酒店",
        "kind": "hotel",
        "lat": 31.2400,
        "lng": 121.4900,
        "tags": {"tourism": "hotel"},
        "distance_km": 1.234,
    }
    body.update(overrides)
    return body


class FakeOverpass:
    """假 Overpass 客户端:记录查询与调用次数,可注入响应或异常。"""

    def __init__(self, payload: Optional[Any] = None, *, error: Optional[BaseException] = None):
        self.payload = {"elements": []} if payload is None else payload
        self.error = error
        self.queries: list[str] = []
        self.timeouts: list[Optional[float]] = []
        self.calls = 0

    def execute(
        self, query: str, *, timeout: Optional[float] = None, reject_runtime_errors: bool = False
    ) -> Any:
        self.calls += 1
        self.queries.append(query)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return self.payload


class FakeLLM:
    """假 LLM 客户端:只实现 :class:`services.intro.LLMClient` 用到的 ``enabled``/``chat``。"""

    def __init__(
        self,
        completion: Any = GOOD_COMPLETION,
        *,
        enabled: bool = True,
        error: Optional[BaseException] = None,
    ):
        self.completion = completion
        self.enabled = enabled
        self.error = error
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.calls = 0

    def chat(self, prompt: str, *, system: str = "") -> Any:
        self.calls += 1
        self.prompts.append(prompt)
        self.systems.append(system)
        if self.error is not None:
            raise self.error
        return self.completion


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
    engine = make_engine(f"sqlite:///{tmp_path / 'stays_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


def all_stays(session) -> list[Stay]:
    return list(session.scalars(select(Stay).order_by(Stay.id)).all())


# --------------------------------------------------------------------------- #
# 表结构与唯一键(幂等的地基)
# --------------------------------------------------------------------------- #


def test_stay_table_has_required_columns_and_types() -> None:
    table = Stay.__table__
    assert table.name == "stays"
    assert REQUIRED_STAY_COLUMNS <= {column.name for column in table.columns}
    assert isinstance(table.c.id.type, Integer)
    assert isinstance(table.c.osm_type.type, String) and table.c.osm_type.type.length == 16
    assert isinstance(table.c.osm_id.type, Integer)
    assert isinstance(table.c.name.type, String) and table.c.name.type.length == NAME_LEN
    assert isinstance(table.c.kind.type, String) and table.c.kind.type.length == KIND_LEN
    assert isinstance(table.c.lat.type, Float) and isinstance(table.c.lng.type, Float)
    assert isinstance(table.c.tags.type, JSON)
    assert isinstance(table.c.price_estimate.type, String)
    assert table.c.price_estimate.type.length == PRICE_LEN
    assert isinstance(table.c.currency.type, String) and table.c.currency.type.length == 8
    assert isinstance(table.c.intro.type, Text)
    assert table.c.currency.default.arg == DEFAULT_CURRENCY


def test_stay_nullable_columns_match_contract() -> None:
    table = Stay.__table__
    nullable = {name for name, column in table.columns.items() if column.nullable}
    assert {"distance_km", "price_estimate", "intro"} <= nullable
    assert not ({"osm_type", "osm_id", "name", "kind", "lat", "lng", "tags", "currency"} & nullable)
    assert table.c.fetched_at.default is not None


def test_stay_unique_key_and_location_index_exist() -> None:
    table = Stay.__table__
    unique = [item for item in table.constraints if item.__class__.__name__ == "UniqueConstraint"]
    keys = {tuple(column.name for column in item.columns) for item in unique}
    assert ("osm_type", "osm_id") in keys
    assert "ix_stay_location" in {index.name for index in table.indexes}


def test_unique_key_blocks_raw_duplicates_but_allows_other_osm_types(session) -> None:
    session.add(Stay(osm_type="node", osm_id=7, name="A", kind="hotel", lat=31.2, lng=121.4))
    session.commit()
    session.add(Stay(osm_type="node", osm_id=7, name="B", kind="hotel", lat=31.2, lng=121.4))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    session.add(Stay(osm_type="way", osm_id=7, name="B", kind="hotel", lat=31.2, lng=121.4))
    session.commit()
    assert len(all_stays(session)) == 2


# --------------------------------------------------------------------------- #
# 检索:Overpass QL 与解析
# --------------------------------------------------------------------------- #


def test_build_stay_query_is_single_group_union_without_name_filter() -> None:
    query = stay_service.build_stay_query(ORIGIN_LAT, ORIGIN_LNG, 8000)
    assert query.count("out center") == 1
    assert "(around:8000," in query.replace(" ", "") or "around:8000" in query
    for tag in stay_service.STAY_TAGS:
        assert f'["tourism"="{tag}"]' in query
    # 民宿/公寓常没有 name tag:服务端不得再加 ["name"] 过滤
    assert '["name"]' not in query
    assert len(stay_service.stay_groups()) == 1


def test_stay_tags_are_the_five_tourism_values() -> None:
    assert stay_service.STAY_TAGS == ("hotel", "guest_house", "hostel", "apartment", "chalet")


def test_search_stays_parses_elements_sorted_by_distance() -> None:
    client = FakeOverpass(sample_payload())
    rows = stay_service.search_stays(ORIGIN_LAT, ORIGIN_LNG, 8000, client=client)
    assert client.calls == 1
    assert [item["name"] for item in rows] == SAMPLE_ORDER
    assert [item["osm_id"] for item in rows] == [2, 4, 1, 3]
    distances = [item["distance_km"] for item in rows]
    assert distances == sorted(distances)
    assert all(round(value, 2) == value for value in distances)


def test_search_stays_keeps_osm_identity_and_tags() -> None:
    client = FakeOverpass(sample_payload())
    rows = stay_service.search_stays(ORIGIN_LAT, ORIGIN_LNG, client=client)
    first = rows[0]
    assert first["osm_type"] == "node" and first["osm_id"] == 2
    assert first["tags"]["tourism"] == "hostel"
    assert first["kind"] == "hostel"
    assert first["lat"] == 31.2350 and first["lng"] == 121.4800
    # way 的坐标来自 center
    guest_house = [item for item in rows if item["osm_id"] == 3][0]
    assert guest_house["osm_type"] == "way"
    assert guest_house["lat"] == 31.2600 and guest_house["lng"] == 121.5000


def test_search_stays_keeps_unnamed_stays() -> None:
    client = FakeOverpass(sample_payload())
    rows = stay_service.search_stays(ORIGIN_LAT, ORIGIN_LNG, client=client)
    apartment = [item for item in rows if item["osm_id"] == 4][0]
    assert apartment["name"] == ""
    assert apartment["kind"] == "apartment"


def test_search_stays_dedupes_by_osm_identity() -> None:
    payload = {"elements": [dict(SAMPLE_ELEMENTS[0]), dict(SAMPLE_ELEMENTS[0])]}
    rows = stay_service.search_stays(ORIGIN_LAT, ORIGIN_LNG, client=FakeOverpass(payload))
    assert len(rows) == 1


def test_search_stays_uses_client_timeout_for_interactive_path() -> None:
    client = FakeOverpass(sample_payload())
    stay_service.search_stays(ORIGIN_LAT, ORIGIN_LNG, client=client)
    assert client.timeouts == [stay_service.STAY_REQUEST_TIMEOUT_S]


@pytest.mark.parametrize(
    "payload",
    [{"nope": 1}, "不是对象", None, {"elements": "不是列表"}],
)
def test_search_stays_degrades_to_empty_on_bad_payload(payload: Any) -> None:
    rows = stay_service.search_stays(ORIGIN_LAT, ORIGIN_LNG, client=FakeOverpass(payload))
    assert rows == []


def test_search_stays_degrades_to_empty_on_any_error() -> None:
    for error in (DataSourceError("overpass", "端点全挂"), RuntimeError("boom")):
        rows = stay_service.search_stays(
            ORIGIN_LAT, ORIGIN_LNG, client=FakeOverpass(error=error)
        )
        assert rows == []


@pytest.mark.parametrize("lat,lng", [(999.0, 121.4737), (31.2304, 999.0), (None, 121.4737)])
def test_search_stays_rejects_bad_origin_without_calling_client(lat: Any, lng: Any) -> None:
    client = FakeOverpass(sample_payload())
    assert stay_service.search_stays(lat, lng, client=client) == []
    assert client.calls == 0


def test_stay_kind_normalizes_tourism_values() -> None:
    assert stay_service.stay_kind({"tourism": "Hotel"}) == "hotel"
    assert stay_service.stay_kind({"TOURISM": "  guest_house "}) == "guest_house"
    # 同族写法(motel/resort)照收,便于前端分组
    assert stay_service.stay_kind({"tourism": "motel"}) == "motel"
    # 少数把住宿类型写在别的 tag 上
    assert stay_service.stay_kind({"building": "hostel"}) == "hostel"
    assert stay_service.stay_kind({}) == ""
    assert stay_service.stay_kind(None) == ""


def test_normalize_kind_prefers_explicit_value_and_truncates() -> None:
    assert stay_service.normalize_kind(row(kind="Chalet", tags={"tourism": "hotel"})) == "chalet"
    assert stay_service.normalize_kind(row(kind="", tags={"tourism": "hostel"})) == "hostel"
    assert len(stay_service.normalize_kind(row(kind="x" * 80))) == KIND_LEN


def test_stay_identity_rejects_incomplete_or_zero_id() -> None:
    assert stay_service.stay_identity(row()) == ("node", 11)
    assert stay_service.stay_identity(row(osm_id="12")) == ("node", 12)
    assert stay_service.stay_identity(row(osm_type="WAY")) == ("way", 11)
    assert stay_service.stay_identity(row(osm_id=None)) is None
    assert stay_service.stay_identity(row(osm_id=0)) is None
    assert stay_service.stay_identity(row(osm_id="abc")) is None
    assert stay_service.stay_identity(row(osm_type="")) is None


def test_coordinate_pins_seven_decimals() -> None:
    assert stay_service.coordinate(31.12345678912) == round(31.12345678912, COORD_PRECISION)
    assert stay_service.coordinate("31.12345678912") == 31.1234568
    assert stay_service.coordinate(None) is None
    assert stay_service.coordinate("abc") is None
    assert stay_service.coordinate(float("nan")) is None


def test_distance_km_matches_haversine_and_degrades() -> None:
    value = stay_service.distance_km(ORIGIN_LAT, ORIGIN_LNG, 31.2350, 121.4800)
    assert value == pytest.approx(0.79, abs=0.02)
    assert stay_service.distance_km(ORIGIN_LAT, ORIGIN_LNG, None, 121.48) is None


# --------------------------------------------------------------------------- #
# 估价 + 简介(LLM):prompt、解析、降级
# --------------------------------------------------------------------------- #


def test_build_price_prompt_carries_facts_and_two_line_format() -> None:
    prompt = stay_service.build_price_prompt(
        row(name="外滩华尔道夫酒店", kind="hotel", lat=31.24, lng=121.49,
            tags={"tourism": "hotel", "stars": "5", "wheelchair": "no", "note": "别写我"},
            distance_km=1.2)
    )
    assert "名称:外滩华尔道夫酒店" in prompt
    assert "类型:hotel" in prompt
    assert "坐标:31.2400000,121.4900000" in prompt
    assert "距起点:1.2 km" in prompt
    assert "tourism=hotel" in prompt and "stars=5" in prompt
    # 白名单外与 no/none/unknown 不进 prompt
    assert "note" not in prompt and "wheelchair" not in prompt
    assert "价格: 约¥A-B/晚" in prompt
    assert "简介: <40字内一句话>" in prompt


def test_stay_facts_limits_and_filters_values() -> None:
    tags = {key: "1" for key in stay_service.STAY_FACT_TAGS}
    tags["internet_access"] = "unknown"
    tags["not_in_whitelist"] = "x"
    facts = stay_service.stay_facts(tags)
    assert len(facts.split("; ")) <= stay_service.STAY_FACT_LIMIT
    assert "not_in_whitelist" not in facts and "internet_access" not in facts
    assert stay_service.stay_facts(None) == ""


def test_estimate_price_returns_price_and_intro() -> None:
    llm = FakeLLM()
    price, intro = stay_service.estimate_price(row(), client=llm)
    assert price == "约¥200-400/晚"
    assert intro == "位于市中心的经济型酒店。"
    assert llm.calls == 1
    assert llm.systems == [stay_service.STAY_SYSTEM_PROMPT]
    assert "名称:示例酒店" in llm.prompts[0]


def test_estimate_price_accepts_orm_rows(session) -> None:
    stay_service.upsert_stays(session, [row()])
    session.commit()
    stay = all_stays(session)[0]
    price, intro = stay_service.estimate_price(stay, client=FakeLLM())
    assert price == "约¥200-400/晚" and intro


def test_estimate_price_skips_llm_when_price_already_set() -> None:
    llm = FakeLLM()
    price, intro = stay_service.estimate_price(
        row(price_estimate="约¥888-999/晚", intro="老牌酒店。"), client=llm
    )
    assert (price, intro) == ("约¥888-999/晚", "老牌酒店。")
    assert llm.calls == 0


def test_estimate_price_requires_a_name() -> None:
    llm = FakeLLM()
    assert stay_service.estimate_price(row(name="  "), client=llm) == ("", "")
    assert llm.calls == 0


def test_estimate_price_degrades_without_key() -> None:
    llm = FakeLLM(enabled=False)
    assert stay_service.estimate_price(row(), client=llm) == ("", "")
    assert llm.calls == 0
    # environ={} → resolve_provider 找不到 key → LLMClient.enabled False
    assert stay_service.estimate_price(row(), environ={}) == ("", "")


def test_estimate_price_degrades_on_exception() -> None:
    for error in (DataSourceError("llm", "限流"), RuntimeError("boom")):
        llm = FakeLLM(error=error)
        assert stay_service.estimate_price(row(), client=llm) == ("", "")
        assert llm.calls == 1


@pytest.mark.parametrize(
    "completion",
    ["", None, "不知道", "简介: 只有简介没有价格", "价格: 暂无报价", "价格: 约¥0/晚", 42],
)
def test_estimate_price_degrades_on_unparsable_output(completion: Any) -> None:
    assert stay_service.estimate_price(row(), client=FakeLLM(completion)) == ("", "")


@pytest.mark.parametrize(
    "line,expected",
    [
        ("价格: 约¥300-500/晚", "约¥300-500/晚"),
        ("房价:300~500元", "约¥300-500/晚"),
        ("价格: 约 ¥1,200 - 1,800 每晚", "约¥1200-1800/晚"),
        ("价位:280", "约¥280/晚"),
        ("均价:￥300-300", "约¥300/晚"),
        ("预估价格:400 到 600 元", "约¥400-600/晚"),
        ("价格: 300-500", "约¥300-500/晚"),
    ],
)
def test_parse_price_normalizes_variants(line: str, expected: str) -> None:
    assert stay_service.parse_price(f"简介: 随便。\n{line}") == expected


def test_parse_price_returns_empty_when_nothing_usable() -> None:
    assert stay_service.parse_price("这附近住宿不多") == ""
    assert stay_service.parse_price("价格: 面议") == ""
    assert stay_service.parse_price(None) == ""


def test_parse_intro_line_cleans_and_truncates() -> None:
    assert stay_service.parse_intro_line("简介: 湖边民宿,安静。") == "湖边民宿,安静。"
    assert stay_service.parse_intro_line("价格: 约¥1-2/晚") == ""
    long_line = "简介: " + "很长的一句话" * 20
    cleaned = stay_service.parse_intro_line(long_line)
    assert cleaned.endswith("。")
    assert len(cleaned) <= stay_service.INTRO_TARGET_CHARS + 1


# --------------------------------------------------------------------------- #
# 入库:幂等、不覆盖 LLM 产物、坐标定点
# --------------------------------------------------------------------------- #


def test_upsert_stays_inserts_rows_with_defaults(session) -> None:
    written = stay_service.upsert_stays(session, [row()])
    session.commit()
    assert written == 1
    stay = all_stays(session)[0]
    assert stay.name == "示例酒店" and stay.kind == "hotel"
    assert stay.tags == {"tourism": "hotel"}
    assert stay.currency == DEFAULT_CURRENCY
    assert stay.distance_km == 1.23
    assert stay.price_estimate is None and stay.intro is None
    assert stay.fetched_at is not None


def test_upsert_stays_is_idempotent_by_osm_identity(session) -> None:
    stay_service.upsert_stays(session, [row()])
    session.commit()
    first = all_stays(session)[0]
    first_id, first_fetched = first.id, first.fetched_at

    written = stay_service.upsert_stays(
        session, [row(name="改名后的酒店", lat=31.2500, distance_km=2.5)]
    )
    session.commit()
    assert written == 1
    stays = all_stays(session)
    assert len(stays) == 1
    assert stays[0].id == first_id
    assert stays[0].name == "改名后的酒店"
    assert stays[0].lat == 31.2500
    assert stays[0].distance_km == 2.5
    assert iso_utc(stays[0].fetched_at) >= iso_utc(first_fetched)


def test_upsert_stays_never_overwrites_generated_price_and_intro(session) -> None:
    stay_service.upsert_stays(session, [row(price_estimate="约¥300-500/晚", intro="老牌酒店。")])
    session.commit()
    stay_service.upsert_stays(
        session, [row(name="重抓后的名字", price_estimate="约¥1-2/晚", intro="新简介。")]
    )
    session.commit()
    stays = all_stays(session)
    assert len(stays) == 1
    assert stays[0].price_estimate == "约¥300-500/晚"
    assert stays[0].intro == "老牌酒店。"
    assert stays[0].name == "重抓后的名字"


def test_upsert_stays_fills_price_only_when_column_is_empty(session) -> None:
    stay_service.upsert_stays(session, [row(intro="只有简介。")])
    session.commit()
    stay_service.upsert_stays(session, [row(price_estimate="约¥600-800/晚", intro="新简介。")])
    session.commit()
    stay = all_stays(session)[0]
    assert stay.price_estimate == "约¥600-800/晚"
    assert stay.intro == "只有简介。"


def test_upsert_stays_keeps_snapshot_distance_when_row_has_none(session) -> None:
    stay_service.upsert_stays(session, [row(distance_km=3.5)])
    session.commit()
    stay_service.upsert_stays(session, [row(distance_km=None)])
    session.commit()
    assert all_stays(session)[0].distance_km == 3.5


def test_upsert_stays_skips_rows_without_identity_or_coordinates(session) -> None:
    written = stay_service.upsert_stays(
        session,
        [
            row(osm_id=None),
            row(osm_id=0),
            row(osm_type=""),
            row(lat=None),
            row(lng="abc"),
            row(lat=999.0),
        ],
    )
    session.commit()
    assert written == 0
    assert all_stays(session) == []


def test_upsert_stays_pins_coordinates_and_truncates_long_text(session) -> None:
    stay_service.upsert_stays(
        session,
        [
            row(
                lat=31.12345678912,
                lng=121.98765432198,
                name="酒" * 400,
                price_estimate="约¥" + "9" * 200,
            )
        ],
    )
    session.commit()
    stay = all_stays(session)[0]
    assert stay.lat == round(31.12345678912, COORD_PRECISION)
    assert stay.lng == round(121.98765432198, COORD_PRECISION)
    assert len(f"{stay.lat:.10f}".rstrip("0")) - len("31.") == COORD_PRECISION
    assert len(stay.name) == NAME_LEN
    assert len(stay.price_estimate) == PRICE_LEN


def test_upsert_stays_uppercases_currency_and_honours_explicit_value(session) -> None:
    stay_service.upsert_stays(session, [row(osm_id=1, currency="usd"), row(osm_id=2, currency="")])
    session.commit()
    by_id = {item.osm_id: item for item in all_stays(session)}
    assert by_id[1].currency == "USD"
    assert by_id[2].currency == DEFAULT_CURRENCY


def test_estimate_missing_only_touches_rows_without_price(session) -> None:
    stay_service.upsert_stays(
        session, [row(osm_id=1, price_estimate="约¥100-200/晚"), row(osm_id=2)]
    )
    session.commit()
    llm = FakeLLM()
    estimated = stay_service.estimate_missing(all_stays(session), client=llm)
    assert estimated == 1
    assert llm.calls == 1


# --------------------------------------------------------------------------- #
# 读库:半径过滤、排序、序列化
# --------------------------------------------------------------------------- #


def test_select_stays_filters_by_radius_and_sorts_by_distance(session) -> None:
    stay_service.upsert_stays(
        session,
        [
            row(osm_id=1, lat=31.2400, lng=121.4900),
            row(osm_id=2, lat=31.2350, lng=121.4800),
            row(osm_id=3, lat=32.5000, lng=122.5000),  # ~170 km 外
        ],
    )
    session.commit()
    near = stay_service.select_stays(session, ORIGIN_LAT, ORIGIN_LNG, 8000)
    assert [item.osm_id for item in near] == [2, 1]
    wide = stay_service.select_stays(session, ORIGIN_LAT, ORIGIN_LNG, 200_000)
    assert [item.osm_id for item in wide] == [2, 1, 3]
    assert stay_service.select_stays(session, ORIGIN_LAT, ORIGIN_LNG, 0) == []
    assert stay_service.select_stays(session, 999.0, 0.0, 8000) == []


def test_stay_to_dict_shape_and_estimate_flag(session) -> None:
    stay_service.upsert_stays(session, [row(price_estimate="约¥300-500/晚", intro="老牌酒店。")])
    session.commit()
    item = stay_service.stay_to_dict(
        all_stays(session)[0], origin_lat=ORIGIN_LAT, origin_lng=ORIGIN_LNG, source="db"
    )
    assert REQUIRED_DICT_KEYS <= set(item)
    assert item["price_is_estimate"] is True
    assert item["currency"] == DEFAULT_CURRENCY
    assert item["source"] == "db"
    assert item["distance_km"] == pytest.approx(1.88, abs=0.05)
    assert item["fetched_at"].startswith("20")
    assert item["lat"] == round(31.2400, COORD_PRECISION)

    stay_service.upsert_stays(session, [row(osm_id=12)])
    session.commit()
    bare = stay_service.stay_to_dict(all_stays(session)[1])
    assert bare["price_estimate"] is None and bare["price_is_estimate"] is False
    assert bare["distance_km"] == 1.23  # 没有起点时用库里的快照


# --------------------------------------------------------------------------- #
# 编排:DB 即缓存
# --------------------------------------------------------------------------- #


def test_load_or_fetch_stays_fetches_upserts_and_estimates(session) -> None:
    client = FakeOverpass(sample_payload())
    llm = FakeLLM()
    items = stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, client=client, llm=llm
    )
    assert client.calls == 1
    assert len(items) == 4 == len(all_stays(session))
    assert [item["name"] for item in items] == SAMPLE_ORDER
    assert all(item["source"] == "overpass" for item in items)
    # 无名公寓不调 LLM(没有名称就不猜价格)
    assert llm.calls == 3
    assert items[0]["price_estimate"] == "约¥200-400/晚"
    assert items[0]["price_is_estimate"] is True
    assert items[0]["intro"] == "位于市中心的经济型酒店。"
    assert items[1]["price_estimate"] is None and items[1]["price_is_estimate"] is False


def test_load_or_fetch_stays_cache_hit_is_offline_and_llm_free(session) -> None:
    first = stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, client=FakeOverpass(sample_payload()), llm=FakeLLM()
    )
    cached_client = FakeOverpass(sample_payload())
    cached_llm = FakeLLM()
    second = stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, client=cached_client, llm=cached_llm
    )
    assert cached_client.calls == 0
    assert cached_llm.calls == 0
    assert [item["id"] for item in second] == [item["id"] for item in first]
    assert all(item["source"] == "db" for item in second)
    assert all(item["price_estimate"] for item in second if item["name"])


def test_load_or_fetch_stays_refresh_refetches_without_re_estimating(session) -> None:
    stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, client=FakeOverpass(sample_payload()), llm=FakeLLM()
    )
    client = FakeOverpass(sample_payload())
    llm = FakeLLM()
    items = stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, refresh=True, client=client, llm=llm
    )
    assert client.calls == 1
    assert llm.calls == 0  # 已有价格的行永不再调 LLM
    assert len(items) == 4 == len(all_stays(session))
    assert all(item["source"] == "overpass" for item in items)


def test_load_or_fetch_stays_refresh_keeps_db_rows_when_fetch_fails(session) -> None:
    stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, client=FakeOverpass(sample_payload()), llm=FakeLLM()
    )
    broken = FakeOverpass(error=DataSourceError("overpass", "端点全挂"))
    items = stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, refresh=True, client=broken, llm=FakeLLM()
    )
    assert broken.calls == 1
    assert len(items) == 4
    assert all(item["source"] == "db" for item in items)


def test_load_or_fetch_stays_degrades_to_empty(session) -> None:
    broken = FakeOverpass(error=DataSourceError("overpass", "端点全挂"))
    assert stay_service.load_or_fetch_stays(session, ORIGIN_LAT, ORIGIN_LNG, client=broken) == []
    assert stay_service.load_or_fetch_stays(session, 999.0, 0.0, client=broken) == []
    assert broken.calls == 1
    assert all_stays(session) == []


def test_load_or_fetch_stays_survives_missing_llm_key(session) -> None:
    client = FakeOverpass(sample_payload())
    items = stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, client=client, llm=FakeLLM(enabled=False)
    )
    assert len(items) == 4
    assert all(item["price_estimate"] is None for item in items)
    assert all(item["price_is_estimate"] is False for item in items)
    assert [item["name"] for item in items] == SAMPLE_ORDER
    # 落库了,配上 key 后重抓才会补价格
    assert len(all_stays(session)) == 4


def test_load_or_fetch_stays_estimates_only_new_rows_on_second_origin(session) -> None:
    stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, client=FakeOverpass(sample_payload()), llm=FakeLLM()
    )
    # 换个起点(半径盖住同一批住宿),但强制重抓:老行不再调 LLM
    client = FakeOverpass(sample_payload())
    llm = FakeLLM()
    items = stay_service.load_or_fetch_stays(
        session, 31.2400, 121.4800, refresh=True, client=client, llm=llm
    )
    assert llm.calls == 0
    assert len(items) == 4
    distances = [item["distance_km"] for item in items]
    assert distances == sorted(distances)
    assert all_stays(session)[0].distance_km == items[0]["distance_km"]
