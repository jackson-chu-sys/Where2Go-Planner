"""TASK-6g 单测:住宿估价 v2 —— **规则层前置**(品牌/星级/类型 + 城市系数,0 token)。

全程不触网、不调真 LLM(照 :mod:`test_stays_v2` 的套路):

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,偷偷联网当场失败;
* ``llm_key_absent`` 清掉所有 LLM key 环境变量(要 key 的用例自己 ``monkeypatch.setenv``);
* LLM 用两种假客户端 —— :class:`CountingLLM`(记调用次数、按 prompt 里的家数回规范输出)
  与 :class:`ExplodingLLM`(被调用就记一笔并报错,用来证明**规则命中路径 0 LLM 调用**);
* Overpass 用假客户端,DB 用 ``tmp_path`` 下的临时 SQLite。

覆盖:品牌表规模与中英文别名(大小写不敏感、包含匹配、最长别名优先)、星级档
(``四星``/``5*``/``hotel:stars`` 变体)、类型兜底档、城市线级三档系数(一线 ×1.2 /
新一线 ×1.05 / 其他 ×0.9,认不出城市走中性 ×1.0)、品牌与星级同时命中取品牌档、
规则未命中回落既有批量 LLM、``price_kind`` 落库与 API 透出、旧库补列迁移、
**永久缓存**(已有价格不重算、规则表改了也不跟着变)。

运行:``cd backend && ../.venv/bin/python -m pytest test_stays_rule_price.py -q``
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Optional

import pytest
import requests
from sqlalchemy import String, inspect, select, text
from sqlalchemy.orm import Session

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import stays as stays_api  # noqa: E402
from data_sources import overpass as overpass_module  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db.base import COLUMN_MIGRATIONS  # noqa: E402
from db.models import (  # noqa: E402
    PRICE_KIND_LEN,
    PRICE_KIND_LLM,
    PRICE_KIND_RULE,
    PRICE_KINDS,
    Stay,
)
from services import stays as stay_service  # noqa: E402

# --------------------------------------------------------------------------- #
# 样本:起点上海人民广场;住宿都摆在起点 0.5 km 内(落在最小的那一档半径里)
# --------------------------------------------------------------------------- #

ORIGIN_LAT = 31.2304
ORIGIN_LNG = 121.4737
LLM_KEY_ENVS = (
    "WHERE2GO_LLM_API_KEY", "ALIBABA_TOKEN_PLAN_API_KEY", "DASHSCOPE_API_KEY",
    "QWEN_API_KEY", "DEEPSEEK_API_KEY",
)
# 规范里点名的品牌(最低集合):中英文别名都必须在表里
SPEC_BRAND_ALIASES = (
    "汉庭", "hanting", "如家", "home inn", "7天", "7 days inn", "锦江之星", "jinjiang inn",
    "城市便捷", "city comfort", "格林豪泰", "greentree", "速8", "super 8", "莫泰",
    "motel 168", "海友", "hi inn", "怡莱", "elan",
    "全季", "ji hotel", "亚朵", "atour", "维也纳", "vienna", "桔子", "orange", "麗枫",
    "lavande", "智选假日", "holiday inn express", "citigo", "美居", "mercure",
    "诺富特", "novotel",
    "希尔顿", "hilton", "万豪", "marriott", "喜来登", "sheraton", "洲际",
    "intercontinental", "凯悦", "hyatt", "香格里拉", "shangri-la", "皇冠假日",
    "crowne plaza", "雅高", "accor", "索菲特", "sofitel",
    "丽思卡尔顿", "ritz-carlton", "宝格丽", "bulgari", "安缦", "aman", "华尔道夫",
    "waldorf", "柏悦", "park hyatt", "瑞吉", "st. regis", "半岛", "peninsula",
)
# 旧库(没有 price_kind 列)的最小 DDL:用来验证 init_db 的 ALTER TABLE 补列
LEGACY_STAYS_DDL = (
    "CREATE TABLE stays ("
    "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
    "osm_type VARCHAR(16) NOT NULL, osm_id INTEGER NOT NULL, name VARCHAR(255) NOT NULL, "
    "kind VARCHAR(32) NOT NULL, lat FLOAT NOT NULL, lng FLOAT NOT NULL, tags JSON NOT NULL, "
    "distance_km FLOAT, price_estimate VARCHAR(64), currency VARCHAR(8) NOT NULL, "
    "intro TEXT, fetched_at DATETIME NOT NULL, "
    "CONSTRAINT uq_stay_osm UNIQUE (osm_type, osm_id))"
)


def element(osm_id: int, name: str, tourism: str = "hotel", **tags: Any) -> dict[str, Any]:
    """起点附近的一家住宿(Overpass node element 形状)。"""
    body: dict[str, Any] = {"tourism": tourism}
    if name:
        body["name"] = name
    body.update(tags)
    return {"type": "node", "id": osm_id, "lat": ORIGIN_LAT + 0.004, "lon": ORIGIN_LNG, "tags": body}


def row(osm_id: int = 1, name: str = "示例酒店", kind: str = "hotel", **overrides: Any) -> dict[str, Any]:
    """一条 upsert 入参(:func:`stays.search_stays` 的输出形状)。"""
    body: dict[str, Any] = {
        "osm_type": "node",
        "osm_id": osm_id,
        "name": name,
        "kind": kind,
        "lat": ORIGIN_LAT + 0.004,
        "lng": ORIGIN_LNG,
        "tags": {"tourism": kind},
        "distance_km": 0.44,
    }
    body.update(overrides)
    return body


class FakeOverpass:
    """假 Overpass:固定回一批元素,并记下调用次数。"""

    def __init__(self, elements: Optional[list[dict[str, Any]]] = None):
        self.elements = list(elements or [])
        self.queries: list[str] = []
        self.calls = 0

    def execute(
        self, query: str, *, timeout: Optional[float] = None, reject_runtime_errors: bool = False
    ) -> Any:
        self.calls += 1
        self.queries.append(query)
        return {"elements": [dict(item) for item in self.elements]}


class CountingLLM:
    """假 LLM:签名与 :class:`services.intro.LLMClient.chat` 一致,记**调用次数**与 prompt。

    不给 ``responses`` 就按 prompt 里的家数自动生成规范输出(批量 ``序号|价格:…|简介:…``,
    逐家 ``价格: …`` 两行),把 prompt 构造与解析串成闭环测。
    """

    def __init__(self, *responses: Any, enabled: bool = True, error: Optional[BaseException] = None):
        self.responses = list(responses)
        self.enabled = enabled
        self.error = error
        self.label = "假 LLM"
        self.prompts: list[str] = []
        self.calls = 0

    def chat(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        self.calls += 1
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        if not self.enabled:
            raise AssertionError("未配 key 的客户端不该被调用")
        if self.responses:
            return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        count = len(re.findall(r"^第\d+家$", prompt, flags=re.MULTILINE))
        if count == 0:  # 逐家口径(TASK-3a1 的 build_price_prompt)
            return "价格: 约¥200-400/晚\n简介: 位于市中心的经济型酒店。"
        return "\n".join(
            f"{index}|价格: 约¥{200 + index * 10}-{300 + index * 10}/晚|简介: 测试简介{index}。"
            for index in range(1, count + 1)
        )


class ExplodingLLM(CountingLLM):
    """被调用就报错的假 LLM:用来证明"规则命中 → **0 LLM 调用**"。"""

    def __init__(self) -> None:
        super().__init__()
        self.label = "不该被调用的 LLM"

    def chat(self, prompt: str, **kwargs: Any) -> Any:  # noqa: D102 - 见类 docstring
        self.calls += 1
        self.prompts.append(prompt)
        raise AssertionError("规则命中就不该调 LLM(0 token 口径被破坏)")


class RecordingExecutor:
    """假执行器:只**记下**后台任务不跑(证明没有排后台回填)。"""

    def __init__(self) -> None:
        self.jobs: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

    def submit(self, fn, *args: Any, **kwargs: Any) -> Any:
        self.jobs.append((fn, args, kwargs))
        return None


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
def llm_key_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认"没配 LLM key":要 key 的用例自己 setenv(避免读到开发机上的真 key)。"""
    for key in LLM_KEY_ENVS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def inline_background() -> Any:
    """后台执行器换成同步的:不起真线程,回填在断言前就跑完。"""
    stay_service.set_background_executor(stay_service.INLINE_EXECUTOR)
    try:
        yield stay_service.INLINE_EXECUTOR
    finally:
        stay_service.set_background_executor(None)


@pytest.fixture()
def session(tmp_path):
    """每个用例一个独立的临时 SQLite 库。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'stays_rule.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


def all_stays(session: Session) -> list[Stay]:
    return list(session.scalars(select(Stay).order_by(Stay.id)).all())


# --------------------------------------------------------------------------- #
# 1. 规则表口径:品牌(中英文)/ 星级 / 类型 / 城市系数
# --------------------------------------------------------------------------- #


def test_brand_table_covers_spec_minimum_set() -> None:
    """品牌价格带表 ≥25 个品牌,规范点名的中英文别名一个都不能少。"""
    aliases = set(stay_service.BRAND_PRICE_BANDS)
    assert len(aliases) >= 25, f"品牌别名只有 {len(aliases)} 个"
    missing = [item for item in SPEC_BRAND_ALIASES if item not in aliases]
    assert not missing, f"缺品牌别名:{missing}"
    assert set(stay_service.PRICE_TIERS) == set(stay_service.PRICE_TIER_BANDS)
    assert stay_service.PRICE_TIER_BANDS[stay_service.PRICE_TIER_ECONOMY] == (150, 300)
    assert stay_service.PRICE_TIER_BANDS[stay_service.PRICE_TIER_MIDSCALE] == (300, 550)
    assert stay_service.PRICE_TIER_BANDS[stay_service.PRICE_TIER_UPSCALE] == (600, 1200)
    assert stay_service.PRICE_TIER_BANDS[stay_service.PRICE_TIER_LUXURY][0] == 1200, "奢华档 1200+"
    # 每个别名都指向一个合法档位区间(表是 BRAND_TIERS × PRICE_TIER_BANDS 摊平的)
    assert set(stay_service.BRAND_PRICE_BANDS.values()) <= set(stay_service.PRICE_TIER_BANDS.values())
    # 归一后的匹配表与品牌表同规模(别名归一后不该互相撞掉)
    assert len(stay_service.BRAND_ALIAS_BANDS) == len(aliases)


def test_star_kind_and_city_tables_are_pinned() -> None:
    """星级档 / 类型档 / 城市系数的数值按规范钉死(改表就是改口径,必须显式)。"""
    assert stay_service.STARS_PRICE_BANDS == {
        1: (100, 250), 2: (100, 250), 3: (250, 450), 4: (450, 900), 5: (900, 2000),
    }
    assert stay_service.KIND_PRICE_BANDS == {
        "hostel": (50, 150), "guest_house": (200, 500),
        "chalet": (200, 500), "apartment": (300, 800),
    }
    assert "hotel" not in stay_service.KIND_PRICE_BANDS, "hotel 房价带太宽,交给 LLM"
    assert stay_service.CITY_TIER_FACTOR == {
        stay_service.CITY_TIER_FIRST: 1.2,
        stay_service.CITY_TIER_NEW_FIRST: 1.05,
        stay_service.CITY_TIER_OTHER: 0.9,
    }
    assert stay_service.CITY_TIER_UNKNOWN_FACTOR == 1.0
    assert set(stay_service.TIER1_CITIES) == {"北京", "上海", "广州", "深圳"}
    assert len(stay_service.NEW_TIER1_CITIES) >= 12, "新一线至少 12 城"
    for city in ("杭州", "成都", "武汉", "南京", "苏州", "重庆", "西安", "长沙", "天津", "郑州", "东莞", "青岛"):
        assert city in stay_service.NEW_TIER1_CITIES, f"{city} 应在新一线表里"


def test_brand_match_is_case_insensitive_and_substring() -> None:
    """品牌匹配:大小写不敏感 + 名称**包含**匹配(中英文都认)。"""
    economy = stay_service.PRICE_TIER_BANDS[stay_service.PRICE_TIER_ECONOMY]
    midscale = stay_service.PRICE_TIER_BANDS[stay_service.PRICE_TIER_MIDSCALE]
    for name in ("汉庭酒店", "汉庭酒店(上海人民广场店)", "Hanting Hotel", "HANTING", "hanting inn"):
        assert stay_service.brand_band(name) == economy, name
    for name in ("如家快捷酒店", "Home Inn", "HOME INN SHANGHAI"):
        assert stay_service.brand_band(name) == economy, name
    for name in ("7天酒店", "7 Days Inn", "7 DAYS"):
        assert stay_service.brand_band(name) == economy, name
    for name in ("速8酒店", "Super 8 Motel"):
        assert stay_service.brand_band(name) == economy, name
    for name in ("亚朵酒店", "Atour Hotel", "ATOUR"):
        assert stay_service.brand_band(name) == midscale, name
    for name in ("全季酒店", "Ji Hotel"):
        assert stay_service.brand_band(name) == midscale, name
    assert stay_service.brand_band("某某宾馆") is None
    assert stay_service.brand_band("") is None and stay_service.brand_band(None) is None


def test_longest_brand_alias_wins() -> None:
    """最长别名优先:``Park Hyatt``(奢华)不被 ``Hyatt``(高档)抢先,假日两兄弟互不误伤。"""
    luxury = stay_service.PRICE_TIER_BANDS[stay_service.PRICE_TIER_LUXURY]
    upscale = stay_service.PRICE_TIER_BANDS[stay_service.PRICE_TIER_UPSCALE]
    midscale = stay_service.PRICE_TIER_BANDS[stay_service.PRICE_TIER_MIDSCALE]
    assert stay_service.brand_band("Park Hyatt Shanghai") == luxury
    assert stay_service.brand_band("上海柏悦酒店") == luxury
    assert stay_service.brand_band("Grand Hyatt") == luxury
    assert stay_service.brand_band("Hyatt Regency") == upscale, "凯悦是高档,不是奢华"
    assert stay_service.brand_band("Holiday Inn Express") == midscale
    assert stay_service.brand_band("智选假日酒店") == midscale
    assert stay_service.brand_band("Crowne Plaza") == upscale
    assert stay_service.brand_band("皇冠假日酒店") == upscale
    assert stay_service.brand_band("Shangri-La Hotel") == upscale
    assert stay_service.brand_band("Ritz-Carlton") == luxury and stay_service.brand_band("RITZ CARLTON") == luxury


def test_stars_value_reads_osm_variants() -> None:
    """星级:``stars``/``hotel:stars`` 都认,``4``/``4*``/``四星``/``S5`` 都能解析。"""
    assert stay_service.stars_value({"tags": {"stars": "4"}}) == 4
    assert stay_service.stars_value({"tags": {"hotel:stars": "5"}}) == 5
    assert stay_service.stars_value({"tags": {"stars": "四星"}}) == 4
    assert stay_service.stars_value({"tags": {"stars": "3*"}}) == 3
    assert stay_service.stars_value({"tags": {"stars": "S2"}}) == 2
    assert stay_service.stars_value({"tags": {"STARS": " 4 "}}) == 4
    assert stay_service.stars_value({"tags": {"stars": "0"}}) is None
    assert stay_service.stars_value({"tags": {"stars": "9"}}) is None
    assert stay_service.stars_value({"tags": {"stars": ""}}) is None
    assert stay_service.stars_value({"tags": {}}) is None
    assert stay_service.stars_value({"tags": "不是字典"}) is None
    assert stay_service.stars_value(None) is None
    assert stay_service.stars_band({"tags": {"stars": "1"}}) == (100, 250)
    assert stay_service.stars_band({"tags": {"stars": "5"}}) == (900, 2000)
    assert stay_service.stars_band({"tags": {}}) is None


def test_kind_band_only_covers_narrow_kinds() -> None:
    """类型兜底档:hostel 是床位价;``hotel``/未知类型不出规则价(交给 LLM)。"""
    assert stay_service.kind_band({"kind": "hostel"}) == (50, 150)
    assert stay_service.kind_band({"tags": {"tourism": "hostel"}}) == (50, 150)
    assert stay_service.kind_band({"kind": "guest_house"}) == (200, 500)
    assert stay_service.kind_band({"kind": "CHALET"}) == (200, 500)
    assert stay_service.kind_band({"kind": "apartment"}) == (300, 800)
    assert stay_service.kind_band({"kind": "hotel"}) is None
    assert stay_service.kind_band({"kind": "motel"}) is None
    assert stay_service.kind_band({}) is None


def test_city_tier_factor_has_three_tiers() -> None:
    """城市系数三档:一线 ×1.2、新一线 ×1.05、其他城市 ×0.9。"""
    for city in ("北京", "上海", "广州", "深圳", "北京市朝阳区", "上海市"):
        assert stay_service.city_tier(city) == stay_service.CITY_TIER_FIRST, city
        assert stay_service.city_price_factor({"name": "某酒店", "tags": {"addr:city": city}}) == 1.2
    for city in ("杭州", "成都", "武汉", "苏州", "东莞市", "青岛市"):
        assert stay_service.city_tier(city) == stay_service.CITY_TIER_NEW_FIRST, city
        assert stay_service.city_price_factor({"name": "某酒店", "tags": {"addr:city": city}}) == 1.05
    for city in ("昆山", "诸暨", "某县城"):
        assert stay_service.city_tier(city) == stay_service.CITY_TIER_OTHER, city
        assert stay_service.city_price_factor({"name": "某酒店", "tags": {"addr:city": city}}) == 0.9
    assert stay_service.city_tier("") is None and stay_service.city_tier(None) is None


def test_city_hint_falls_back_to_name_and_neutral_when_unknown() -> None:
    """城市线索:地址 tag 优先,其次名称里的城市;都没有 → 中性 ×1.0(不打折也不加价)。"""
    assert stay_service.stay_city({"name": "上海虹桥康得思酒店"}) == "上海"
    assert stay_service.city_price_factor({"name": "杭州西湖亚朵酒店"}) == 1.05
    assert stay_service.stay_city({"name": "上海大厦", "tags": {"addr:city": "昆山市"}}) == "昆山"
    assert stay_service.stay_city({"name": "某酒店"}) is None
    assert stay_service.city_price_factor({"name": "某酒店"}) == 1.0
    # 脏 tag 值(no/unknown)不算城市线索
    assert stay_service.stay_city({"name": "某酒店", "tags": {"addr:city": "unknown"}}) is None


def test_rule_price_estimate_applies_factor_and_rounds_to_step() -> None:
    """规则估价 = 档位区间 × 城市系数,取整到 5 元,并带上 ``price_kind="rule"``。"""
    assert stay_service.rule_price_estimate({"name": "汉庭酒店"}) == (150, 300, PRICE_KIND_RULE)
    assert stay_service.rule_price_estimate(
        {"name": "汉庭酒店", "tags": {"addr:city": "上海"}}
    ) == (180, 360, PRICE_KIND_RULE)
    assert stay_service.rule_price_estimate(
        {"name": "汉庭酒店", "tags": {"addr:city": "杭州"}}
    ) == (160, 315, PRICE_KIND_RULE)
    assert stay_service.rule_price_estimate(
        {"name": "汉庭酒店", "tags": {"addr:city": "昆山"}}
    ) == (135, 270, PRICE_KIND_RULE)
    low, high, kind = stay_service.rule_price_estimate({"name": "亚朵酒店", "tags": {"addr:city": "成都"}})
    assert kind == PRICE_KIND_RULE and low < high
    assert low % stay_service.PRICE_BAND_STEP == 0 and high % stay_service.PRICE_BAND_STEP == 0
    assert stay_service.band_text(low, high) == f"约¥{low}-{high}/晚"
    assert stay_service.band_text(300, 300) == "约¥300/晚"
    assert stay_service.round_band_value(157.5) == 160 and stay_service.round_band_value(135) == 135
    assert stay_service.round_band_value("不是数字") == 0


def test_brand_beats_stars_beats_kind() -> None:
    """判档优先级 **品牌 > 星级 > 类型**(品牌与星级同时命中取品牌档)。"""
    branded = {"name": "汉庭酒店", "kind": "hotel", "tags": {"tourism": "hotel", "stars": "5"}}
    assert stay_service.rule_band(branded) == (150, 300), "汉庭是经济型,5 星标签不顶用"
    assert stay_service.rule_price_estimate(branded) == (150, 300, PRICE_KIND_RULE)
    assert stay_service.rule_band({"name": "某大酒店", "tags": {"stars": "5"}}) == (900, 2000)
    assert stay_service.rule_band({"name": "某宾馆", "tags": {"stars": "3"}}) == (250, 450)
    assert stay_service.rule_band({"name": "老船长青旅", "kind": "hostel"}) == (50, 150)
    # 品牌是青旅档的反例:命中品牌就走品牌档,哪怕 kind 是 hostel
    assert stay_service.rule_band({"name": "希尔顿酒店", "kind": "hostel"}) == (600, 1200)


def test_rule_price_estimate_needs_a_name_and_never_raises() -> None:
    """没有名称不猜(与 :func:`stays_needing_price` 同口径);脏入参一律 ``None`` 不抛。"""
    assert stay_service.rule_price_estimate({"name": "", "kind": "hostel"}) is None
    assert stay_service.rule_price_estimate({"name": "   ", "tags": {"stars": "5"}}) is None
    assert stay_service.rule_price_estimate({"kind": "hostel"}) is None
    assert stay_service.rule_price_estimate(None) is None
    assert stay_service.rule_price_estimate({"name": "某某宾馆", "kind": "hotel"}) is None
    assert stay_service.rule_price_estimate({"name": "某公寓", "kind": "未知类型"}) is None
    assert stay_service.rule_price_estimate({"name": "汉庭酒店", "tags": ["不是字典"]}) == (
        150, 300, PRICE_KIND_RULE,
    )


def test_normalize_price_kind_only_accepts_known_values() -> None:
    """出处标记只认 ``rule``/``llm``,其余(含老行的 NULL)归一成空串。"""
    assert stay_service.normalize_price_kind(PRICE_KIND_RULE) == "rule"
    assert stay_service.normalize_price_kind(" LLM ") == "llm"
    assert stay_service.normalize_price_kind("瞎写") == ""
    assert stay_service.normalize_price_kind(None) == ""
    assert stay_service.normalize_price_kind(123) == ""
    assert stay_service.normalize_price_kind("r" * 99) == ""
    assert set(PRICE_KINDS) == {"rule", "llm"}


# --------------------------------------------------------------------------- #
# 2. 规则命中 → 0 LLM 调用;未命中 → 回落既有批量 LLM
# --------------------------------------------------------------------------- #


def test_rule_hit_makes_zero_llm_calls() -> None:
    """命中规则的行**一次 LLM 都不调**(注入会被调用就报错的假客户端验证)。"""
    llm = ExplodingLLM()
    price, intro, kind = stay_service.estimate_price_tagged(
        {"name": "如家快捷酒店", "kind": "hotel"}, client=llm
    )
    assert (price, kind) == ("约¥150-300/晚", PRICE_KIND_RULE)
    assert intro == "", "规则层不出简介(不为简介烧 token)"
    assert llm.calls == 0
    # 既有两元组口径不变(调用方零改动)
    assert stay_service.estimate_price({"name": "Atour Hotel"}, client=llm) == ("约¥300-550/晚", "")
    assert stay_service.estimate_price({"name": "老船长青旅", "kind": "hostel"}, client=llm) == (
        "约¥50-150/晚", "",
    )
    assert llm.calls == 0


def test_rule_miss_falls_back_to_llm_and_tags_it() -> None:
    """规则未命中(没品牌没星级、类型也不在兜底表)→ 走既有 LLM 路径,标 ``llm``。"""
    plain = {"name": "湖畔客栈", "kind": "hotel", "tags": {"tourism": "hotel"}}
    assert stay_service.rule_price_estimate(plain) is None
    llm = CountingLLM()
    price, intro, kind = stay_service.estimate_price_tagged(plain, client=llm)
    assert (price, kind) == ("约¥200-400/晚", PRICE_KIND_LLM)
    assert intro == "位于市中心的经济型酒店。" and llm.calls == 1
    # 没配 key 的客户端:规则未命中的行仍降级成空(不猜数字)
    assert stay_service.estimate_price_tagged(plain, client=CountingLLM(enabled=False)) == ("", "", "")


def test_estimate_missing_sync_path_writes_price_kind(session: Session) -> None:
    """逐家口径(``estimate=sync``)也分得出出处:规则命中不调 LLM,未命中才调。"""
    stay_service.upsert_stays(
        session, [row(osm_id=1, name="汉庭酒店"), row(osm_id=2, name="湖畔客栈")]
    )
    session.commit()
    llm = CountingLLM()
    assert stay_service.estimate_missing(all_stays(session), client=llm) == 2
    assert llm.calls == 1, "只有规则未命中的那家该调 LLM"
    by_name = {item.name: item for item in all_stays(session)}
    assert by_name["汉庭酒店"].price_estimate == "约¥150-300/晚"
    assert by_name["汉庭酒店"].price_kind == PRICE_KIND_RULE
    assert by_name["汉庭酒店"].intro is None
    assert by_name["湖畔客栈"].price_estimate == "约¥200-400/晚"
    assert by_name["湖畔客栈"].price_kind == PRICE_KIND_LLM
    assert by_name["湖畔客栈"].intro == "位于市中心的经济型酒店。"


def test_fill_prices_batched_asks_llm_only_for_rule_misses(session: Session) -> None:
    """批量口径:规则命中的行**不进 prompt**,只剩未命中的切批(5 家/prompt)。"""
    stay_service.upsert_stays(
        session,
        [
            row(osm_id=1, name="汉庭酒店"),
            row(osm_id=2, name="如家快捷"),
            row(osm_id=3, name="亚朵酒店"),
            row(osm_id=4, name="老船长青旅", kind="hostel"),
            row(osm_id=5, name="衡山路小筑", kind="guest_house"),
            row(osm_id=6, name="湖畔客栈"),
            row(osm_id=7, name="某某宾馆"),
        ],
    )
    session.commit()
    rows = all_stays(session)
    llm = CountingLLM()
    assert stay_service.fill_prices_batched(rows, client=llm) == 7
    assert llm.calls == 1, "5 家命中规则 → 只剩 2 家进 1 个 prompt(原来要 2 个)"
    assert "汉庭" not in llm.prompts[0] and "老船长" not in llm.prompts[0]
    assert "湖畔客栈" in llm.prompts[0] and "某某宾馆" in llm.prompts[0]
    kinds = {item.name: item.price_kind for item in all_stays(session)}
    assert kinds["汉庭酒店"] == PRICE_KIND_RULE and kinds["老船长青旅"] == PRICE_KIND_RULE
    assert kinds["湖畔客栈"] == PRICE_KIND_LLM and kinds["某某宾馆"] == PRICE_KIND_LLM


def test_all_rule_batch_never_calls_llm(session: Session) -> None:
    """整批都命中规则 → ``fill_prices_batched`` 0 次 LLM 调用,价格全部落库。"""
    stay_service.upsert_stays(
        session,
        [
            row(osm_id=1, name="汉庭酒店"),
            row(osm_id=2, name="7天酒店"),
            row(osm_id=3, name="锦江之星"),
            row(osm_id=4, name="格林豪泰酒店"),
            row(osm_id=5, name="莫泰酒店"),
            row(osm_id=6, name="海友酒店"),
        ],
    )
    session.commit()
    llm = ExplodingLLM()
    assert stay_service.fill_prices_batched(all_stays(session), client=llm) == 6
    assert llm.calls == 0
    assert all(item.price_kind == PRICE_KIND_RULE for item in all_stays(session))


def test_load_stays_rule_only_rows_skip_llm_and_backfill(session: Session) -> None:
    """检索入库后:全命中规则 → 首屏即有价格、``estimating=False``、不排后台、0 LLM。"""
    client = FakeOverpass(
        [
            element(1, "汉庭酒店"),
            element(2, "如家快捷"),
            element(3, "老船长青旅", tourism="hostel"),
            element(4, "亚朵酒店"),
            element(5, "衡山路小筑", tourism="guest_house"),
            element(6, "桔子酒店"),
            element(7, "维也纳酒店"),
        ]
    )
    llm = ExplodingLLM()
    recorder = RecordingExecutor()
    result = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=5000,
        client=client, llm=llm, executor=recorder,
    )
    assert client.calls == 1 and len(result.items) == 7
    assert llm.calls == 0, "规则命中路径 0 LLM 调用"
    assert result.estimating is False and recorder.jobs == [], "全命中就不该排后台回填"
    assert all(item["price_kind"] == PRICE_KIND_RULE for item in result.items)
    assert all(item["price_estimate"] and item["price_is_estimate"] for item in result.items)
    assert {item.price_kind for item in all_stays(session)} == {PRICE_KIND_RULE}

    # 第二次请求:命中 DB 正缓存(0 网络、0 LLM),价格原样返回
    offline = FakeOverpass([])
    again = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=5000, client=offline, llm=ExplodingLLM()
    )
    assert offline.calls == 0 and again.from_cache is True and again.estimating is False
    assert [item["price_estimate"] for item in again.items] == [
        item["price_estimate"] for item in result.items
    ]


def test_load_stays_mixed_rows_send_only_misses_to_llm(session: Session) -> None:
    """混合批:4 家命中规则 + 2 家未命中 → 只有那 2 家逐家问 LLM(auto 档 ≤5 家走同步)。"""
    client = FakeOverpass(
        [
            element(1, "汉庭酒店"),
            element(2, "亚朵酒店"),
            element(3, "老船长青旅", tourism="hostel"),
            element(4, "希尔顿酒店"),
            element(5, "湖畔客栈"),
            element(6, "某某宾馆"),
        ]
    )
    llm = CountingLLM()
    result = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=5000, client=client, llm=llm
    )
    assert llm.calls == 2, "6 家里只有 2 家规则未命中"
    assert result.estimating is False
    by_name = {item["name"]: item for item in result.items}
    assert by_name["汉庭酒店"]["price_kind"] == PRICE_KIND_RULE
    assert by_name["希尔顿酒店"]["price_estimate"] == "约¥600-1200/晚"
    assert by_name["湖畔客栈"]["price_kind"] == PRICE_KIND_LLM
    assert by_name["湖畔客栈"]["intro"], "LLM 那一路照旧给简介"


def test_price_fill_job_fills_rules_without_llm_key(session: Session) -> None:
    """后台回填任务:没配 key 也能靠规则表把能填的填掉(``rule_filled``),不烧 token。"""
    stay_service.upsert_stays(
        session,
        [
            row(osm_id=1, name="汉庭酒店"),
            row(osm_id=2, name="亚朵酒店"),
            row(osm_id=3, name="某某宾馆"),
        ],
    )
    session.commit()
    engine = session.get_bind()
    ids = [item.id for item in all_stays(session)]

    stats = stay_service.price_fill_job(engine, ids)  # client=None + 没配 key → LLM 停用
    assert stats["scanned"] == 3 and stats["pending"] == 3
    assert stats["rule_filled"] == 2 and stats["filled"] == 2
    assert stats["batches"] == 0, "规则未命中的那家没 key 也不切批(不猜数字)"
    assert stats["provider"] == "未配置", "还有行等着问 LLM,provider 照旧报未配置"
    session.expire_all()
    by_name = {item.name: item for item in all_stays(session)}
    assert by_name["汉庭酒店"].price_estimate == "约¥150-300/晚"
    assert by_name["汉庭酒店"].price_kind == PRICE_KIND_RULE
    assert by_name["某某宾馆"].price_estimate is None and by_name["某某宾馆"].price_kind is None


def test_price_fill_job_all_rule_rows_never_touch_llm(session: Session) -> None:
    """后台任务里全是规则命中行 → 注入的 LLM 一次都不该被调用。"""
    stay_service.upsert_stays(
        session,
        [row(osm_id=index, name=name) for index, name in enumerate(
            ["汉庭酒店", "如家快捷", "7天酒店", "锦江之星", "格林豪泰酒店", "怡莱酒店"], start=1
        )],
    )
    session.commit()
    engine = session.get_bind()
    ids = [item.id for item in all_stays(session)]
    llm = ExplodingLLM()
    stats = stay_service.price_fill_job(engine, ids, client=llm)
    assert llm.calls == 0
    assert stats["rule_filled"] == 6 and stats["filled"] == 6 and stats["batches"] == 0
    assert stats["provider"] == stay_service.RULE_PROVIDER_LABEL
    session.expire_all()
    assert all(item.price_kind == PRICE_KIND_RULE for item in all_stays(session))


# --------------------------------------------------------------------------- #
# 3. 落库 / 序列化 / API 透出 / 永久缓存 / 旧库补列
# --------------------------------------------------------------------------- #


def test_stay_table_has_price_kind_column_and_migration() -> None:
    """``Stay.price_kind``:String(16)、可空(老行是 NULL),并在轻量迁移表里登记。"""
    table = Stay.__table__
    assert "price_kind" in {column.name for column in table.columns}
    assert isinstance(table.c.price_kind.type, String)
    assert table.c.price_kind.type.length == PRICE_KIND_LEN == 16
    assert table.c.price_kind.nullable, "老行没有出处信息 → NULL(不猜)"
    assert ("stays", "price_kind") in {(name, column) for name, column, _ in COLUMN_MIGRATIONS}


def test_init_db_backfills_price_kind_on_legacy_db(tmp_path) -> None:
    """旧库缺列 → ``init_db`` 用 ALTER TABLE 补上(幂等),补完就能写规则价。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'legacy_stays.db'}")
    with engine.begin() as connection:
        connection.execute(text(LEGACY_STAYS_DDL))
        connection.execute(text(
            "INSERT INTO stays (osm_type, osm_id, name, kind, lat, lng, tags, currency, fetched_at) "
            "VALUES ('node', 4242, '老行酒店', 'hotel', 31.2304, 121.4737, '{}', 'CNY', "
            "'2026-09-01 00:00:00')"
        ))
    assert "price_kind" not in {c["name"] for c in inspect(engine).get_columns("stays")}

    init_db(engine)
    init_db(engine)  # 再跑一次也不能报错(幂等)
    assert "price_kind" in {c["name"] for c in inspect(engine).get_columns("stays")}

    with session_factory(engine)() as current:
        legacy = current.scalar(select(Stay).where(Stay.osm_id == 4242))
        assert legacy is not None and legacy.price_kind is None, "历史行补列后应为 NULL"
        assert legacy.name == "老行酒店", "补列不能动既有数据"
        assert stay_service.apply_rule_prices([legacy]) == 0, "hotel 无品牌无星级 → 仍交给 LLM"
        stay_service.upsert_stays(current, [row(osm_id=4243, name="汉庭酒店")])
        current.commit()
        fresh = current.scalar(select(Stay).where(Stay.osm_id == 4243))
        stay_service.apply_rule_prices([fresh])
        current.commit()
        assert fresh.price_estimate == "约¥150-300/晚" and fresh.price_kind == PRICE_KIND_RULE
    engine.dispose()


def test_upsert_stays_keeps_price_kind_and_rejects_unknown(session: Session) -> None:
    """出处标记跟着价格走:重抓不冲掉;非法值归一成 NULL。"""
    stay_service.upsert_stays(
        session,
        [row(osm_id=1, name="汉庭酒店", price_estimate="约¥150-300/晚", price_kind=PRICE_KIND_RULE)],
    )
    session.commit()
    stay_service.upsert_stays(session, [row(osm_id=1, name="汉庭酒店(重抓)")])
    session.commit()
    stay = all_stays(session)[0]
    assert stay.name == "汉庭酒店(重抓)", "抓取事实照旧刷新"
    assert stay.price_estimate == "约¥150-300/晚" and stay.price_kind == PRICE_KIND_RULE

    stay_service.upsert_stays(
        session, [row(osm_id=2, name="某酒店", price_estimate="约¥100/晚", price_kind="瞎写")]
    )
    session.commit()
    assert all_stays(session)[1].price_kind is None, "只认 rule/llm,其余落 NULL"


def test_apply_rule_prices_skips_priced_rows(session: Session) -> None:
    """:func:`apply_rule_prices` 只碰缺价格的行(**永久缓存**:规则表改了也不重算)。"""
    stay_service.upsert_stays(
        session,
        [
            row(osm_id=1, name="汉庭酒店", price_estimate="约¥888-999/晚", price_kind=PRICE_KIND_LLM),
            row(osm_id=2, name="亚朵酒店"),
            row(osm_id=3, name=""),
        ],
    )
    session.commit()
    rows = all_stays(session)
    assert stay_service.apply_rule_prices(rows) == 1
    assert rows[0].price_estimate == "约¥888-999/晚" and rows[0].price_kind == PRICE_KIND_LLM
    assert rows[1].price_estimate == "约¥300-550/晚" and rows[1].price_kind == PRICE_KIND_RULE
    assert rows[2].price_estimate is None, "没有名称就不猜"
    assert stay_service.rows_without_price(rows) == [rows[2]], "只剩没价格的那行"
    assert stay_service.apply_rule_prices([]) == 0 and stay_service.apply_rule_prices(rows) == 0


def test_cached_rule_price_survives_rule_table_change(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已入库的价格是永久缓存:把规则表临时改掉,老行不重算,新行才用新表。"""
    stay_service.upsert_stays(session, [row(osm_id=1, name="汉庭酒店")])
    session.commit()
    assert stay_service.apply_rule_prices(all_stays(session)) == 1
    session.commit()
    before = all_stays(session)[0].price_estimate
    assert before == "约¥150-300/晚"

    monkeypatch.setattr(
        stay_service, "BRAND_ALIAS_BANDS", ((stay_service.brand_token("汉庭"), (995, 1995)),)
    )
    assert stay_service.rule_price_estimate({"name": "汉庭酒店"}) == (995, 1995, PRICE_KIND_RULE)

    result = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=5000,
        client=FakeOverpass([]), llm=ExplodingLLM(),
    )
    assert result.from_cache is True
    assert result.items[0]["price_estimate"] == before, "缓存命中不重算"
    assert result.items[0]["price_kind"] == PRICE_KIND_RULE

    stay_service.upsert_stays(session, [row(osm_id=2, name="汉庭酒店(新店)")])
    session.commit()
    assert stay_service.apply_rule_prices(all_stays(session)) == 1, "新行才按新表估"
    session.commit()
    by_id = {item.osm_id: item for item in all_stays(session)}
    assert by_id[1].price_estimate == before and by_id[2].price_estimate == "约¥995-1995/晚"


def test_stay_to_dict_exposes_price_kind(session: Session) -> None:
    """序列化带 ``price_kind``;``price_is_estimate`` 仍是估算标注(规则价也是估算)。"""
    stay_service.upsert_stays(session, [row(osm_id=1, name="汉庭酒店"), row(osm_id=2, name="某某宾馆")])
    session.commit()
    stay_service.apply_rule_prices(all_stays(session))
    session.commit()
    priced = stay_service.stay_to_dict(all_stays(session)[0])
    assert priced["price_estimate"] == "约¥150-300/晚"
    assert priced["price_kind"] == PRICE_KIND_RULE
    assert priced["price_is_estimate"] is True and priced["currency"] == "CNY"
    bare = stay_service.stay_to_dict(all_stays(session)[1])
    assert bare["price_estimate"] is None and bare["price_kind"] is None
    assert bare["price_is_estimate"] is False


def test_api_items_expose_price_kind(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """API 白名单透出 ``price_kind``(``rule``/``llm``/null),note 里写明口径。"""
    assert "price_kind" in stays_api.ITEM_KEYS
    client = FakeOverpass([element(1, "汉庭酒店"), element(2, "某某宾馆")])
    monkeypatch.setattr(overpass_module, "default_client", lambda: client)
    llm = CountingLLM()
    monkeypatch.setattr(stay_service, "default_llm_client", lambda: llm)

    body = stays_api.list_stays(
        session=session, lat=str(ORIGIN_LAT), lng=str(ORIGIN_LNG), radius_km="8"
    )
    assert body["count"] == 2 and client.calls == 1
    assert "price_kind" in body["note"], "note 应写明 price_kind 的口径"
    by_name = {item["name"]: item for item in body["items"]}
    assert by_name["汉庭酒店"]["price_estimate"] == "约¥150-300/晚"
    assert by_name["汉庭酒店"]["price_kind"] == PRICE_KIND_RULE
    assert by_name["汉庭酒店"]["estimated"] == stays_api.ESTIMATED_LABEL
    assert by_name["汉庭酒店"]["intro"] is None, "规则层不出简介"
    # 规则未命中的那家才走 LLM(两家一共只调 1 次)
    assert by_name["某某宾馆"]["price_estimate"] == "约¥200-400/晚"
    assert by_name["某某宾馆"]["price_kind"] == PRICE_KIND_LLM
    assert by_name["某某宾馆"]["intro"] == "位于市中心的经济型酒店。"
    assert llm.calls == 1, "命中规则的行不进 prompt"


def test_api_without_llm_key_still_returns_rule_prices(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没配 key:规则层照旧出价(0 token),未命中的行留 null —— 接口照常 200。"""
    client = FakeOverpass([element(1, "汉庭酒店"), element(2, "某某宾馆")])
    monkeypatch.setattr(overpass_module, "default_client", lambda: client)

    body = stays_api.list_stays(
        session=session, lat=str(ORIGIN_LAT), lng=str(ORIGIN_LNG), radius_km="8"
    )
    assert body["count"] == 2 and client.calls == 1
    by_name = {item["name"]: item for item in body["items"]}
    assert by_name["汉庭酒店"]["price_estimate"] == "约¥150-300/晚"
    assert by_name["汉庭酒店"]["price_kind"] == PRICE_KIND_RULE
    assert by_name["某某宾馆"]["price_estimate"] is None
    assert by_name["某某宾馆"]["price_kind"] is None, "估不出来就是 null(不编数字)"
    assert all(item["estimated"] == stays_api.ESTIMATED_LABEL for item in body["items"])
