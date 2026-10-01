"""TASK-3a1 单测:住宿(Stay)表 + :mod:`services.stays` 服务层。

全程不触网、不调真 LLM:

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,任何偷偷联网当场失败;
* 高德检索用假 ``fetch_fn``(签名 ``(lat, lng, radius_m) -> 归一化 POI``),
  LLM 用假 client(只实现 ``enabled``/``chat``);
* DB 用 ``tmp_path`` 下的临时 SQLite,不碰 ``backend/data/``。

重点覆盖**幂等与降级**:唯一键 ``(osm_type, osm_id)``(TASK-9b 起是 ``("amap", crc32(POI id))``)
让重抓不产生第二行,LLM 产物
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

from data_sources import DataSourceError, TransientDataSourceError, amap  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db.models import (  # noqa: E402
    AMAP_OSM_TYPE,
    AMAP_SOURCE,
    COORD_PRECISION,
    DEFAULT_CURRENCY,
    KIND_LEN,
    NAME_LEN,
    PRICE_LEN,
    Stay,
    amap_osm_id,
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


#: 高德返回的中文 ``type`` → 本地 kind(:func:`services.stays.amap_stay_kind` 的关键词口径)。
#: 只用 ``types=100000`` 大类粗筛,细分 kind 在本地按 type 字符串判(禁编造中类码)。
STAY_TYPE_TEXTS: dict[str, str] = {
    "hotel": "住宿服务;宾馆酒店;星级酒店",
    "hostel": "住宿服务;宾馆酒店;青年旅舍",
    "guest_house": "住宿服务;宾馆酒店;民宿",
    "apartment": "住宿服务;公寓式酒店;公寓",
    "chalet": "住宿服务;宾馆酒店;度假村",
}


def poi(
    id_text: str,
    name: str,
    lat: float,
    lng: float,
    *,
    kind: str = "hotel",
    type_text: Optional[str] = None,
    typecode: str = "100000",
    cityname: str = "",
) -> dict[str, Any]:
    """一条**归一化高德 POI**(:func:`data_sources.amap.parse_poi` 的输出形状)。

    ``typecode`` 一律用 §1.4 实测过的住宿大类 ``100000``(细分 kind 靠 type 中文串);
    ``cityname`` 默认留空,免得城市线级系数(上海 ×1.2)改动既有的规则价断言 ——
    带城市线索的价带另有专门用例覆盖。
    """
    return {
        "id": id_text,
        "name": name,
        "lat": lat,
        "lng": lng,
        "type": STAY_TYPE_TEXTS[kind] if type_text is None else type_text,
        "typecode": typecode,
        "address": "",
        "cityname": cityname,
        "adname": "",
        "distance_m": None,
    }


# 高德**已按由近及远排序**返回,服务层不再重排 → 样本顺序即预期顺序。
SAMPLE_POIS: list[dict[str, Any]] = [
    poi("B0FFH0BOAT", "老船长青旅", 31.2350, 121.4800, kind="hostel"),
    poi("B0FFH0APT0", "", 31.2200, 121.4600, kind="apartment"),
    poi("B0FFH0WALD", "外滩华尔道夫酒店", 31.2400, 121.4900),
    poi("B0FFH0HOME", "衡山路小筑", 31.2600, 121.5000, kind="guest_house"),
]
SAMPLE_IDS = [item["id"] for item in SAMPLE_POIS]
# 由近及远的预期顺序(见各用例断言)
SAMPLE_ORDER = ["老船长青旅", "", "外滩华尔道夫酒店", "衡山路小筑"]


def sample_pois() -> list[dict[str, Any]]:
    return [dict(item) for item in SAMPLE_POIS]


def row(**overrides: Any) -> dict[str, Any]:
    """一条 upsert 入参(:func:`stays.search_stays` 的输出形状)。"""
    body: dict[str, Any] = {
        "osm_type": AMAP_OSM_TYPE,
        "osm_id": amap_osm_id("B0FFH0DEMO"),
        "name": "示例酒店",
        "kind": "hotel",
        "lat": 31.2400,
        "lng": 121.4900,
        "tags": {"source": AMAP_SOURCE, "amap_id": "B0FFH0DEMO", "type": STAY_TYPE_TEXTS["hotel"]},
        "distance_km": 1.234,
    }
    body.update(overrides)
    return body


class FakeAmapFetch:
    """高德检索替身:签名 = :data:`services.stays.StayFetchFn`(``(lat, lng, radius_m) -> POI 列表``)。

    记录每次调用的坐标与半径(验半径阶梯/显式半径),``error`` 让所有请求抛错(验降级)。
    """

    def __init__(self, pois: Optional[list[dict[str, Any]]] = None,
                 *, error: Optional[BaseException] = None):
        self.pois = [] if pois is None else list(pois)
        self.error = error
        self.calls: list[tuple[float, float, float]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __call__(self, lat: float, lng: float, radius_m: Any = None) -> list[dict[str, Any]]:
        self.calls.append((lat, lng, radius_m))
        if self.error is not None:
            raise self.error
        return [dict(item) for item in self.pois]


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
# 检索:高德 place/around 参数、翻页与归一化
# --------------------------------------------------------------------------- #


def test_fetch_stay_pois_uses_amap_around_with_lodging_typecode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """检索参数口径:``place/around`` + ``types=100000``(住宿**大类**粗筛)、v3 25 条/页。"""
    calls: list[dict[str, Any]] = []

    def fake_search(lat: Any, lng: Any, *, radius_m: Any = None, types: Any = None,
                    keywords: Any = None, page: int = 1, offset: Any = None,
                    environ: Any = None, session: Any = None) -> list[dict[str, Any]]:
        calls.append({"lat": lat, "lng": lng, "radius_m": radius_m, "types": types,
                      "keywords": keywords, "page": page, "offset": offset})
        return []

    monkeypatch.setattr(amap, "search_around", fake_search)
    assert stay_service.fetch_stay_pois(ORIGIN_LAT, ORIGIN_LNG, 8000) == []
    assert calls == [{"lat": ORIGIN_LAT, "lng": ORIGIN_LNG, "radius_m": 8000,
                      "types": stay_service.STAY_TYPES, "keywords": None,
                      "page": 1, "offset": amap.PAGE_SIZE}], "第一页就空 → 不再翻页"
    assert stay_service.STAY_TYPES == "100000", "§1.4 实测锚点:住宿服务大类(不编造中类码)"
    assert stay_service.SOURCE_FETCH == "amap", "§6.7:Stay.source / API source 写 amap"
    assert stay_service.GROUP_BUDGET == amap.MAX_ROWS_PER_QUERY == 200
    assert stay_service.stay_page_limit() == amap.MAX_PAGE == 8, "单查询 200 条硬上限"
    assert stay_service.stay_page_limit(30) == 2 and stay_service.stay_page_limit(0) == 1
    assert stay_service.STAY_REQUEST_TIMEOUT_S == 30.0, "交互路径的客户端超时口径不变"


def test_fetch_stay_pois_pages_until_budget_or_short_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """翻页:配额抓满即停;某页没拿满也停(``page>=9`` 服务端恒空,硬翻只是白烧配额)。"""
    rows = [
        poi(f"B0FFH{index:05d}", f"测试酒店{index}", ORIGIN_LAT + index * 0.001, ORIGIN_LNG)
        for index in range(60)
    ]
    pages: list[int] = []

    def fake_search(lat: Any, lng: Any, *, radius_m: Any = None, types: Any = None,
                    keywords: Any = None, page: int = 1, offset: Any = None,
                    environ: Any = None, session: Any = None) -> list[dict[str, Any]]:
        pages.append(page)
        start = (page - 1) * int(offset)
        return [dict(row) for row in rows[start:start + int(offset)]]

    monkeypatch.setattr(amap, "search_around", fake_search)
    assert len(stay_service.fetch_stay_pois(ORIGIN_LAT, ORIGIN_LNG, 8000)) == 60
    assert pages == [1, 2, 3], "25 + 25 + 10:最后一页没拿满就停"

    pages.clear()
    capped = stay_service.fetch_stay_pois(ORIGIN_LAT, ORIGIN_LNG, 8000, budget=30)
    assert len(capped) == 30 and pages == [1, 2], "配额抓满即停"
    assert [row["id"] for row in capped] == [row["id"] for row in rows[:30]]


def test_stay_tags_are_the_five_tourism_values() -> None:
    assert stay_service.STAY_TAGS == ("hotel", "guest_house", "hostel", "apartment", "chalet")


def test_search_stays_normalizes_amap_pois_by_distance() -> None:
    fake = FakeAmapFetch(sample_pois())
    rows = stay_service.search_stays(ORIGIN_LAT, ORIGIN_LNG, 8000, fetch_fn=fake)
    assert fake.calls == [(ORIGIN_LAT, ORIGIN_LNG, 8000)], "半径原样交给检索层"
    assert [item["name"] for item in rows] == SAMPLE_ORDER, "高德已由近及远返回,服务层不再重排"
    assert [item["tags"]["amap_id"] for item in rows] == SAMPLE_IDS
    distances = [item["distance_km"] for item in rows]
    assert distances == sorted(distances)
    assert all(round(value, 2) == value for value in distances)
    assert distances[0] == pytest.approx(0.79, abs=0.02)


def test_search_stays_uses_default_radius_when_omitted() -> None:
    fake = FakeAmapFetch(sample_pois())
    stay_service.search_stays(ORIGIN_LAT, ORIGIN_LNG, fetch_fn=fake)
    assert fake.calls[-1][2] == stay_service.DEFAULT_RADIUS_M == 8000


def test_search_stays_keeps_amap_identity_and_tags() -> None:
    rows = stay_service.search_stays(
        ORIGIN_LAT, ORIGIN_LNG, fetch_fn=FakeAmapFetch(sample_pois())
    )
    first = rows[0]
    assert (first["osm_type"], first["osm_id"]) == (AMAP_OSM_TYPE, amap_osm_id("B0FFH0BOAT")), \
        "入库身份 = osm_type『amap』 + osm_id『crc32(POI id)』(表结构零改动)"
    assert first["tags"]["amap_id"] == "B0FFH0BOAT", "crc32 不可逆 → 原文必须留在 tags"
    assert first["tags"]["source"] == AMAP_SOURCE == "高德"
    assert first["tags"]["type"] == STAY_TYPE_TEXTS["hostel"]
    assert first["tags"]["typecode"] == "100000"
    assert first["kind"] == "hostel", "kind 由中文 type 归一到既有五个值(不是中类码)"
    assert first["lat"] == 31.2350 and first["lng"] == 121.4800
    guest_house = [item for item in rows if item["tags"]["amap_id"] == "B0FFH0HOME"][0]
    assert guest_house["kind"] == "guest_house"
    assert guest_house["lat"] == 31.2600 and guest_house["lng"] == 121.5000


def test_search_stays_keeps_unnamed_stays() -> None:
    rows = stay_service.search_stays(
        ORIGIN_LAT, ORIGIN_LNG, fetch_fn=FakeAmapFetch(sample_pois())
    )
    apartment = [item for item in rows if item["tags"]["amap_id"] == "B0FFH0APT0"][0]
    assert apartment["name"] == "", "没有名称的行照收(规则层与 LLM 都不会替它猜价)"
    assert apartment["kind"] == "apartment"
    assert "name" not in apartment["tags"], "空名称不写进 tags"


def test_search_stays_dedupes_by_amap_id() -> None:
    doubled = [dict(SAMPLE_POIS[0]), dict(SAMPLE_POIS[0])]
    rows = stay_service.search_stays(ORIGIN_LAT, ORIGIN_LNG, fetch_fn=FakeAmapFetch(doubled))
    assert len(rows) == 1, "同一条 POI 只留一行(去重键 = 高德 POI id)"


@pytest.mark.parametrize(
    "pois",
    [[], [None], ["不是对象"], [{"name": "缺 id"}],
     [{"id": "  ", "name": "空 id", "lat": 31.2, "lng": 121.4}]],
)
def test_search_stays_degrades_to_empty_on_bad_rows(pois: Any) -> None:
    """脏行(没有 POI id / 根本不是对象)一律降级成空列表,绝不抛给调用方。"""
    assert stay_service.search_stays(
        ORIGIN_LAT, ORIGIN_LNG, fetch_fn=lambda *args: list(pois)
    ) == []


def test_search_stays_degrades_to_empty_on_any_error() -> None:
    errors = (
        DataSourceError("amap", "未配置 WHERE2GO_AMAP_KEY(高德 Web 服务 key)"),
        TransientDataSourceError("amap", "请求超时(>30s):https://restapi.amap.com/v3/place/around"),
        DataSourceError("amap", "周边检索失败:高德返回 status=0 · infocode=10021"),
        RuntimeError("boom"),
    )
    for error in errors:
        rows = stay_service.search_stays(
            ORIGIN_LAT, ORIGIN_LNG, fetch_fn=FakeAmapFetch(error=error)
        )
        assert rows == [], f"检索失败必须降级成空列表:{error}"


@pytest.mark.parametrize("lat,lng", [(999.0, 121.4737), (31.2304, 999.0), (None, 121.4737)])
def test_search_stays_rejects_bad_origin_without_calling_fetch_fn(lat: Any, lng: Any) -> None:
    fake = FakeAmapFetch(sample_pois())
    assert stay_service.search_stays(lat, lng, fetch_fn=fake) == []
    assert fake.calls == [], "起点非法就不该触网"


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
    assert stay_service.stay_identity(row()) == (AMAP_OSM_TYPE, amap_osm_id("B0FFH0DEMO"))
    assert stay_service.stay_identity(row(osm_id="12")) == (AMAP_OSM_TYPE, 12)
    assert stay_service.stay_identity(row(osm_type="AMAP")) == (
        AMAP_OSM_TYPE, amap_osm_id("B0FFH0DEMO")
    )
    assert stay_service.stay_identity(row(osm_id=None)) is None
    assert stay_service.stay_identity(row(osm_id=0)) is None
    assert stay_service.stay_identity(row(osm_id="abc")) is None
    assert stay_service.stay_identity(row(osm_type="")) is None
    # 存量 OSM 行(清库脚本跑之前)的身份口径不变
    assert stay_service.stay_identity({"osm_type": "node", "osm_id": 11}) == ("node", 11)


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
    assert stay.tags == {
        "source": AMAP_SOURCE, "amap_id": "B0FFH0DEMO", "type": STAY_TYPE_TEXTS["hotel"],
    }
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
    client = FakeAmapFetch(sample_pois())
    llm = FakeLLM()
    items = stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, fetch_fn=client, llm=llm
    )
    assert client.call_count == 1
    assert len(items) == 4 == len(all_stays(session))
    assert [item["name"] for item in items] == SAMPLE_ORDER
    assert all(item["source"] == stay_service.SOURCE_FETCH for item in items)
    # TASK-6g:三行有名称的住宿全部命中规则表(青旅/华尔道夫/民宿)→ **0 次 LLM 调用**
    assert llm.calls == 0
    assert items[0]["price_estimate"] == "约¥50-150/晚", "hostel 类型档(床位价)"
    assert items[0]["price_kind"] == stay_service.PRICE_KIND_RULE
    assert items[0]["price_is_estimate"] is True
    assert items[0]["intro"] is None, "规则层只出价格,不为简介烧 token"
    # 无名公寓:没有名称就不猜价格(规则层与 LLM 层同一个候选口径)
    assert items[1]["price_estimate"] is None and items[1]["price_is_estimate"] is False
    assert items[1]["price_kind"] is None
    assert items[2]["price_estimate"] == "约¥1200-3000/晚", "华尔道夫命中奢华品牌档"
    assert items[3]["price_estimate"] == "约¥200-500/晚", "民宿走 guest_house 类型兜底档"
    assert all(item["price_kind"] == "rule" for item in items if item["name"])


def test_load_or_fetch_stays_cache_hit_is_offline_and_llm_free(session) -> None:
    first = stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, fetch_fn=FakeAmapFetch(sample_pois()), llm=FakeLLM()
    )
    cached_client = FakeAmapFetch(sample_pois())
    cached_llm = FakeLLM()
    second = stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, fetch_fn=cached_client, llm=cached_llm
    )
    assert cached_client.call_count == 0
    assert cached_llm.calls == 0
    assert [item["id"] for item in second] == [item["id"] for item in first]
    assert all(item["source"] == "db" for item in second)
    assert all(item["price_estimate"] for item in second if item["name"])


def test_load_or_fetch_stays_refresh_refetches_without_re_estimating(session) -> None:
    stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, fetch_fn=FakeAmapFetch(sample_pois()), llm=FakeLLM()
    )
    client = FakeAmapFetch(sample_pois())
    llm = FakeLLM()
    items = stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, refresh=True, fetch_fn=client, llm=llm
    )
    assert client.call_count == 1
    assert llm.calls == 0  # 已有价格的行永不再调 LLM
    assert len(items) == 4 == len(all_stays(session))
    assert all(item["source"] == stay_service.SOURCE_FETCH for item in items)


def test_load_or_fetch_stays_refresh_keeps_db_rows_when_fetch_fails(session) -> None:
    stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, fetch_fn=FakeAmapFetch(sample_pois()), llm=FakeLLM()
    )
    broken = FakeAmapFetch(error=DataSourceError("amap", "周边检索失败:高德返回 status=0 · infocode=40000"))
    items = stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, refresh=True, fetch_fn=broken, llm=FakeLLM()
    )
    assert broken.call_count == 1
    assert len(items) == 4
    assert all(item["source"] == "db" for item in items)


def test_load_or_fetch_stays_degrades_to_empty(session) -> None:
    broken = FakeAmapFetch(error=DataSourceError("amap", "周边检索失败:高德返回 status=0 · infocode=40000"))
    assert stay_service.load_or_fetch_stays(session, ORIGIN_LAT, ORIGIN_LNG, fetch_fn=broken) == []
    assert stay_service.load_or_fetch_stays(session, 999.0, 0.0, fetch_fn=broken) == []
    assert broken.call_count == 1
    assert all_stays(session) == []


def test_load_or_fetch_stays_survives_missing_llm_key(session) -> None:
    client = FakeAmapFetch(sample_pois())
    items = stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, fetch_fn=client, llm=FakeLLM(enabled=False)
    )
    assert len(items) == 4
    assert [item["name"] for item in items] == SAMPLE_ORDER
    # TASK-6g:规则层 **0 token**,没配 key 也照样出价;只有 LLM 那一路降级成 null
    assert [item["price_estimate"] for item in items] == [
        "约¥50-150/晚", None, "约¥1200-3000/晚", "约¥200-500/晚",
    ]
    assert all(item["intro"] is None for item in items), "简介只有 LLM 那一路会给"
    assert all(item["price_kind"] in (None, "rule") for item in items)
    assert items[1]["price_is_estimate"] is False
    # 落库了,配上 key 后重抓才会给规则未命中的行补 LLM 价格
    assert len(all_stays(session)) == 4


def test_load_or_fetch_stays_estimates_only_new_rows_on_second_origin(session) -> None:
    stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, fetch_fn=FakeAmapFetch(sample_pois()), llm=FakeLLM()
    )
    # 换个起点(半径盖住同一批住宿),但强制重抓:老行不再调 LLM
    client = FakeAmapFetch(sample_pois())
    llm = FakeLLM()
    items = stay_service.load_or_fetch_stays(
        session, 31.2400, 121.4800, refresh=True, fetch_fn=client, llm=llm
    )
    assert llm.calls == 0
    assert len(items) == 4
    distances = [item["distance_km"] for item in items]
    assert distances == sorted(distances)
    assert all_stays(session)[0].distance_km == items[0]["distance_km"]
