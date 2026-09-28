"""TASK-6c 单测:住宿三件套 —— **负缓存 + 半径阶梯 + 估价异步批量回填**(BUG-3/5)。

全程不触网、不调真 LLM(照 :mod:`test_stays` 的套路):

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,偷偷联网当场失败;
* ``llm_key_absent`` 清掉所有 LLM key 环境变量(要 key 的用例自己 ``monkeypatch.setenv``);
* ``inline_background`` 把后台执行器换成同步的 :class:`~services.stays.InlineExecutor`,
  后台回填在测试里**确定性**跑完(不起真线程,不等);
* Overpass 用假客户端(可按查询里的 ``around:<半径>`` 过滤,模拟"小半径搜不到"),
  LLM 用假 client,DB 用 ``tmp_path`` 下的临时 SQLite。

覆盖:同坐标二次请求 **0 网络**(负缓存命中)、TTL 过期后重查、三档 ``reason``、
半径阶梯逐级扩与"显式给了半径就不扩"、``nearest_km``、批量估价 **5 家/prompt**、
批解析失败留 null 不抛、重试 ≤1、``estimating`` 标志与"入库即刻返回"、API 透传、
既有行为不回归(:func:`services.stays.search_stays` 仍降级成空列表、
:func:`services.stays.load_or_fetch_stays` 仍返回 list)。

运行:``cd backend && ../.venv/bin/python -m pytest test_stays_v2.py -q``
"""

from __future__ import annotations

import os
import re
import sys
from datetime import timedelta
from typing import Any, Optional

import pytest
import requests
from sqlalchemy import Float, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Session

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import stays as stays_api  # noqa: E402
from data_sources import DataSourceError, TransientDataSourceError  # noqa: E402
from data_sources import overpass as overpass_module  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db.models import (  # noqa: E402
    COORD_PRECISION,
    REASON_DATASOURCE_ERROR,
    REASON_NO_DATA,
    REASON_TIMEOUT,
    STAY_CACHE_EMPTY,
    STAY_CACHE_KINDS,
    STAY_REASONS,
    Stay,
    StayQueryCache,
    stay_cache_kind,
    stay_cache_reason,
    utcnow,
)
from services import stays as stay_service  # noqa: E402

# --------------------------------------------------------------------------- #
# 样本:起点上海人民广场;住宿按"正北 N 公里"摆放,便于精确控制落在哪一档半径里
# --------------------------------------------------------------------------- #

ORIGIN_LAT = 31.2304
ORIGIN_LNG = 121.4737
# haversine(R=6371) 下一度纬度约 111.195 km:用它把"距起点 N km"换成纬度偏移
KM_PER_DEGREE = 111.195
LEGACY_TOP_LEVEL_KEYS = {"lat", "lng", "radius_km", "count", "source", "note", "items"}
LLM_KEY_ENVS = (
    "WHERE2GO_LLM_API_KEY", "ALIBABA_TOKEN_PLAN_API_KEY", "DASHSCOPE_API_KEY",
    "QWEN_API_KEY", "DEEPSEEK_API_KEY",
)


def at_km(
    km: float,
    *,
    osm_id: int = 1,
    name: Optional[str] = "示例酒店",
    tourism: str = "hotel",
) -> dict[str, Any]:
    """起点**正北 km 公里处**的一家住宿(Overpass node element 形状)。"""
    tags: dict[str, Any] = {"tourism": tourism}
    if name:
        tags["name"] = name
    return {
        "type": "node",
        "id": osm_id,
        "lat": ORIGIN_LAT + km / KM_PER_DEGREE,
        "lon": ORIGIN_LNG,
        "tags": tags,
    }


def elements_at(*kms: float) -> list[dict[str, Any]]:
    """一批等距摆开的住宿(osm_id 从 101 起,名字互不相同)。"""
    return [
        at_km(km, osm_id=101 + index, name=f"测试酒店{index + 1}")
        for index, km in enumerate(kms)
    ]


def payload(elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {"elements": [dict(item) for item in elements]}


def _element_km(element: dict[str, Any]) -> float:
    center = element.get("center") or element
    return overpass_module.haversine_km(
        ORIGIN_LAT, ORIGIN_LNG, float(center["lat"]), float(center["lon"])
    )


class RadiusAwareOverpass:
    """假 Overpass:按查询里的 ``around:<半径>`` 过滤元素 —— 模拟"小半径搜不到、扩档才有"。

    ``error`` 让所有请求都失败;``error_radii`` 只让指定半径失败(测"某一档挂了")。
    """

    def __init__(
        self,
        elements: Optional[list[dict[str, Any]]] = None,
        *,
        error: Optional[BaseException] = None,
        error_radii: Optional[list[int]] = None,
    ):
        self.elements = list(elements or [])
        self.error = error
        self.error_radii = set(error_radii or ())
        self.queries: list[str] = []
        self.radii: list[int] = []
        self.calls = 0

    def execute(
        self, query: str, *, timeout: Optional[float] = None, reject_runtime_errors: bool = False
    ) -> Any:
        self.calls += 1
        self.queries.append(query)
        matched = re.search(r"around:(\d+)", query)
        radius = int(matched.group(1)) if matched else 0
        self.radii.append(radius)
        if self.error is not None:
            raise self.error
        if radius in self.error_radii:
            raise DataSourceError("overpass", f"半径 {radius} 这一档端点全挂")
        limit_km = radius / 1000.0
        return {"elements": [dict(item) for item in self.elements if _element_km(item) <= limit_km]}


class BatchLLM:
    """假 LLM:签名与 :class:`services.intro.LLMClient.chat` 一致(含 ``max_tokens``/``timeout``)。

    不给 ``responses`` 就**按 prompt 里的家数**自动生成规范的批量输出
    (``1|价格: 约¥210-310/晚|简介: 测试简介1。``),把 prompt 构造与解析串成闭环测。
    """

    def __init__(self, *responses: Any, enabled: bool = True, error: Optional[BaseException] = None):
        self.responses = list(responses)
        self.enabled = enabled
        self.error = error
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.options: list[dict[str, Any]] = []
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
        self.systems.append(system)
        self.options.append({"max_tokens": max_tokens, "timeout": timeout})
        if self.error is not None:
            raise self.error
        if not self.enabled:
            raise AssertionError("未配 key 的客户端不该被调用")
        if self.responses:
            return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        count = len(re.findall(r"^第\d+家$", prompt, flags=re.MULTILINE))
        if count == 0:  # 逐家口径(TASK-3a1 的 build_price_prompt)→ 回单条两行格式
            return "价格: 约¥200-400/晚\n简介: 位于市中心的经济型酒店。"
        return "\n".join(
            f"{index}|价格: 约¥{200 + index * 10}-{300 + index * 10}/晚|简介: 测试简介{index}。"
            for index in range(1, count + 1)
        )


class RecordingExecutor:
    """假执行器:只**记下**任务不跑(证明请求线程没有被后台回填阻塞)。"""

    def __init__(self) -> None:
        self.jobs: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

    def submit(self, fn, *args: Any, **kwargs: Any) -> Any:
        self.jobs.append((fn, args, kwargs))
        return None

    def run_all(self) -> list[Any]:
        return [fn(*args, **kwargs) for fn, args, kwargs in self.jobs]


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
    engine = make_engine(f"sqlite:///{tmp_path / 'stays_v2.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


def all_stays(session: Session) -> list[Stay]:
    return list(session.scalars(select(Stay).order_by(Stay.id)).all())


def all_cache(session: Session) -> list[StayQueryCache]:
    return list(session.scalars(select(StayQueryCache).order_by(StayQueryCache.id)).all())


def call_api(session: Session, monkeypatch: pytest.MonkeyPatch, **params: Any) -> dict[str, Any]:
    """直调端点函数(仓库没装 httpx/TestClient):参数按**字符串**传,与 HTTP 查询串一致。"""
    client = params.pop("client", None)
    if client is not None:
        monkeypatch.setattr(overpass_module, "default_client", lambda: client)
    llm = params.pop("llm", None)
    if llm is not None:
        monkeypatch.setattr(stay_service, "default_llm_client", lambda: llm)
    kwargs: dict[str, Any] = {"session": session}
    kwargs.update(
        {key: (value if value is None else str(value)) for key, value in params.items()}
    )
    kwargs.setdefault("lat", str(ORIGIN_LAT))
    kwargs.setdefault("lng", str(ORIGIN_LNG))
    return stays_api.list_stays(**kwargs)


# --------------------------------------------------------------------------- #
# 1. 负缓存表与常量口径
# --------------------------------------------------------------------------- #


def test_stay_query_cache_table_shape() -> None:
    table = StayQueryCache.__table__
    assert table.name == "stay_query_cache"
    assert {"id", "lat", "lng", "radius_m", "kind", "reason", "nearest_km", "fetched_at"} <= {
        column.name for column in table.columns
    }
    assert isinstance(table.c.id.type, Integer)
    assert isinstance(table.c.lat.type, Float) and isinstance(table.c.lng.type, Float)
    assert isinstance(table.c.radius_m.type, Integer)
    assert isinstance(table.c.kind.type, String) and table.c.kind.type.length == 32
    assert isinstance(table.c.reason.type, String) and table.c.reason.type.length == 32
    assert isinstance(table.c.nearest_km.type, Float) and table.c.nearest_km.nullable
    uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("lat", "lng", "radius_m", "kind") in uniques


def test_task_constants_are_pinned() -> None:
    assert stay_service.NEG_CACHE_TTL_S == 6 * 3600
    assert stay_service.STAY_RADIUS_LADDER_M == (5000, 10000, 30000)
    assert stay_service.PRICE_BATCH_SIZE == 5
    assert stay_service.PRICE_BATCH_RETRIES == 1
    assert stay_service.SYNC_ESTIMATE_MAX_ROWS == 5
    assert stay_service.PRICE_MODEL == "qwen3.8-max"
    assert stay_service.PRICE_PROVIDER_NAME == "qwen"
    assert STAY_REASONS == (REASON_NO_DATA, REASON_DATASOURCE_ERROR, REASON_TIMEOUT)
    assert STAY_CACHE_KINDS == (STAY_CACHE_EMPTY, REASON_DATASOURCE_ERROR, REASON_TIMEOUT)


@pytest.mark.parametrize(
    "reason,kind",
    [
        (REASON_NO_DATA, STAY_CACHE_EMPTY),
        ("", STAY_CACHE_EMPTY),
        (None, STAY_CACHE_EMPTY),
        (REASON_DATASOURCE_ERROR, REASON_DATASOURCE_ERROR),
        (REASON_TIMEOUT, REASON_TIMEOUT),
        ("随便什么", REASON_DATASOURCE_ERROR),
    ],
)
def test_reason_and_kind_round_trip(reason: Optional[str], kind: str) -> None:
    assert stay_cache_kind(reason) == kind
    assert stay_cache_reason(kind) in STAY_REASONS
    assert stay_cache_reason(STAY_CACHE_EMPTY) == REASON_NO_DATA


# --------------------------------------------------------------------------- #
# 2. 负缓存:写入幂等、TTL、命中即 0 网络
# --------------------------------------------------------------------------- #


def test_remember_negative_pins_coordinates_and_upserts(session: Session) -> None:
    first = stay_service.remember_negative(
        session, 31.12345678912, 121.98765432198, 5000, reason=REASON_NO_DATA, nearest_km=12.34
    )
    session.commit()
    assert first is not None
    assert first.lat == round(31.12345678912, COORD_PRECISION)
    assert first.lng == round(121.98765432198, COORD_PRECISION)
    assert first.radius_m == 5000 and first.kind == STAY_CACHE_EMPTY
    assert first.reason == REASON_NO_DATA
    assert first.nearest_km == 12.3  # 1 位小数
    assert first.fetched_at is not None

    second = stay_service.remember_negative(
        session, 31.12345678912, 121.98765432198, 5000.4, reason=REASON_NO_DATA
    )
    session.commit()
    rows = all_cache(session)
    assert len(rows) == 1 and rows[0].id == first.id, "同坐标同半径同 kind 必须 upsert,不能存两行"
    assert rows[0].nearest_km is None
    assert second is not None and second.id == first.id


def test_remember_negative_rejects_bad_input(session: Session) -> None:
    assert stay_service.remember_negative(session, 999.0, 0.0, 5000, reason=REASON_NO_DATA) is None
    assert stay_service.remember_negative(session, ORIGIN_LAT, ORIGIN_LNG, 0, reason=REASON_NO_DATA) is None
    assert all_cache(session) == []


def test_lookup_negative_hits_within_ttl_and_expires(session: Session) -> None:
    row = stay_service.remember_negative(
        session, ORIGIN_LAT, ORIGIN_LNG, 8000, reason=REASON_TIMEOUT
    )
    session.commit()
    hit = stay_service.lookup_negative(session, ORIGIN_LAT, ORIGIN_LNG, 8000)
    assert hit is not None and hit.id == row.id
    assert stay_service.negative_state(hit)["reason"] == REASON_TIMEOUT
    # 半径不同 → 不命中(键含半径)
    assert stay_service.lookup_negative(session, ORIGIN_LAT, ORIGIN_LNG, 5000) is None

    row.fetched_at = utcnow() - timedelta(seconds=stay_service.NEG_CACHE_TTL_S + 60)
    session.commit()
    assert stay_service.lookup_negative(session, ORIGIN_LAT, ORIGIN_LNG, 8000) is None, "过期即重查"
    age = stay_service.cache_age_seconds(row)
    assert age is not None and age > stay_service.NEG_CACHE_TTL_S


def test_negative_state_defaults() -> None:
    state = stay_service.negative_state(None)
    assert state == {"reason": None, "nearest_km": None, "kind": None, "fetched_at": None}


def test_second_request_same_origin_makes_zero_network_calls(session: Session) -> None:
    """核心验收:同坐标同半径二次请求 **0 网络**(负缓存命中,直接回缓存态)。"""
    client = RadiusAwareOverpass([])  # 半径内真的没有住宿
    first = stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000, client=client)
    assert client.calls == 1
    assert first.items == [] and first.reason == REASON_NO_DATA
    assert len(all_cache(session)) == 1

    again = RadiusAwareOverpass([])
    second = stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000, client=again)
    assert again.calls == 0, "6h 内同坐标同半径不该再触网"
    assert second.items == [] and second.reason == REASON_NO_DATA
    assert second.from_cache is True and second.source == stay_service.SOURCE_DB


def test_expired_negative_cache_triggers_refetch(session: Session) -> None:
    client = RadiusAwareOverpass([])
    stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000, client=client)
    assert client.calls == 1
    all_cache(session)[0].fetched_at = utcnow() - timedelta(hours=7)
    session.commit()

    stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000, client=client)
    assert client.calls == 2, "负缓存过期后必须重新检索"
    assert len(all_cache(session)) == 1, "重查后仍是 upsert,不堆行"


def test_refresh_bypasses_negative_cache(session: Session) -> None:
    client = RadiusAwareOverpass([])
    stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000, client=client)
    stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000, client=client, refresh=True
    )
    assert client.calls == 2, "refresh=true 强制重查(不看负缓存)"


def test_negative_cache_rows_do_not_pollute_stays(session: Session) -> None:
    stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000, client=RadiusAwareOverpass([])
    )
    assert all_stays(session) == [], "负缓存只进 stay_query_cache,不该凭空造住宿行"
    assert len(all_cache(session)) == 1


# --------------------------------------------------------------------------- #
# 3. 三档 reason + 失败不扩档
# --------------------------------------------------------------------------- #


def test_reason_no_data_when_search_succeeds_but_empty(session: Session) -> None:
    result = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000, client=RadiusAwareOverpass([])
    )
    assert result.items == [] and result.reason == REASON_NO_DATA
    assert all_cache(session)[0].kind == STAY_CACHE_EMPTY


def test_reason_datasource_error_on_overpass_failure(session: Session) -> None:
    client = RadiusAwareOverpass(error=DataSourceError("overpass", "所有端点均不可用"))
    result = stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000, client=client)
    assert result.items == [] and result.reason == REASON_DATASOURCE_ERROR
    assert all_cache(session)[0].kind == REASON_DATASOURCE_ERROR
    # 缓存态透传:第二次请求 0 网络,reason 仍是 datasource_error
    again = RadiusAwareOverpass(error=DataSourceError("overpass", "所有端点均不可用"))
    second = stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000, client=again)
    assert again.calls == 0 and second.reason == REASON_DATASOURCE_ERROR


def test_reason_timeout_on_overpass_timeout(session: Session) -> None:
    client = RadiusAwareOverpass(
        error=TransientDataSourceError("overpass", "请求超时(>30s):https://overpass.example/api")
    )
    result = stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000, client=client)
    assert result.items == [] and result.reason == REASON_TIMEOUT
    assert all_cache(session)[0].kind == REASON_TIMEOUT


@pytest.mark.parametrize(
    "exc,expected",
    [
        (TimeoutError("timed out"), REASON_TIMEOUT),
        (TransientDataSourceError("overpass", "请求超时(>20s):url"), REASON_TIMEOUT),
        (RuntimeError("HTTPSConnectionPool: Read timed out."), REASON_TIMEOUT),
        (DataSourceError("overpass", "所有 Overpass 端点均不可用(繁忙/超时)"), REASON_DATASOURCE_ERROR),
        (DataSourceError("overpass", "被限流(HTTP 429)"), REASON_DATASOURCE_ERROR),
        (RuntimeError("端点全挂"), REASON_DATASOURCE_ERROR),
        (ValueError("查询非法"), REASON_DATASOURCE_ERROR),
    ],
)
def test_classify_failure_buckets(exc: BaseException, expected: str) -> None:
    assert stay_service.classify_failure(exc) == expected


def test_failure_does_not_expand_ladder(session: Session) -> None:
    """检索失败**不逐级扩**:端点已经挂了,再打两遍只是白等(也避免被限流)。"""
    client = RadiusAwareOverpass(error=DataSourceError("overpass", "端点全挂"))
    result = stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, client=client)
    assert client.calls == 1
    assert client.radii == [5000]
    assert result.items == [] and result.reason == REASON_DATASOURCE_ERROR


def test_failure_keeps_db_rows_and_reports_reason(session: Session) -> None:
    """检索失败但库里有货 → 照旧返回库里的行(source=db),同时把 reason 透传出去。"""
    stay_service.upsert_stays(
        session,
        [
            {
                "osm_type": "node", "osm_id": 7, "name": "已入库旅舍", "kind": "hostel",
                "lat": ORIGIN_LAT + 1.0 / KM_PER_DEGREE, "lng": ORIGIN_LNG,
                "tags": {"tourism": "hostel"}, "price_estimate": "约¥150-260/晚",
            }
        ],
    )
    session.commit()
    client = RadiusAwareOverpass(error=DataSourceError("overpass", "端点全挂"))
    result = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=8000, client=client, refresh=True
    )
    assert [item["name"] for item in result.items] == ["已入库旅舍"]
    assert result.source == stay_service.SOURCE_DB
    assert result.reason == REASON_DATASOURCE_ERROR
    assert result.nearest_km == pytest.approx(1.0, abs=0.05)


# --------------------------------------------------------------------------- #
# 4. 半径阶梯 5→10→30 km
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "radius_m,expected",
    [(None, (5000, 10000, 30000)), (8000, (8000,)), (2500.4, (2500,)), (0, ()), (-5, ()), ("abc", ())],
)
def test_radius_ladder_only_when_radius_omitted(radius_m: Any, expected: tuple[int, ...]) -> None:
    assert stay_service.radius_ladder(radius_m) == expected


def test_ladder_stops_at_first_rung_with_results(session: Session) -> None:
    client = RadiusAwareOverpass(elements_at(1.0, 3.0))
    result = stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, client=client)
    assert client.radii == [5000] and client.calls == 1
    assert len(result.items) == 2 and result.radius_m == 5000
    assert result.expanded is False and result.reason is None
    assert result.nearest_km == pytest.approx(1.0, abs=0.05)


def test_ladder_expands_until_results_and_reports_nearest_km(session: Session) -> None:
    """5 km 内空 → 扩到 10 km 命中即停;``nearest_km`` = 最近一家的 haversine(1 位)。"""
    client = RadiusAwareOverpass(elements_at(7.0))
    result = stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, client=client)
    assert client.radii == [5000, 10000] and client.calls == 2
    assert len(result.items) == 1
    assert result.radius_m == 10000 and result.expanded is True
    assert result.reason is None and result.source == stay_service.SOURCE_FETCH
    assert result.nearest_km == pytest.approx(7.0, abs=0.05)
    assert round(result.nearest_km, 1) == result.nearest_km
    # 5 km 那一档的空结果也进了负缓存(下次同半径直接回缓存态)
    assert {row.radius_m: row.kind for row in all_cache(session)} == {5000: STAY_CACHE_EMPTY}


def test_ladder_expands_to_last_rung(session: Session) -> None:
    client = RadiusAwareOverpass(elements_at(15.0))
    result = stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, client=client)
    assert client.radii == [5000, 10000, 30000]
    assert len(result.items) == 1 and result.radius_m == 30000
    assert result.nearest_km == pytest.approx(15.0, abs=0.05)


def test_ladder_exhausted_reports_no_data(session: Session) -> None:
    """30 km 内都没有 → reason=no_data,三档半径各写一条负缓存。"""
    client = RadiusAwareOverpass(elements_at(45.0))
    result = stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, client=client)
    assert client.radii == [5000, 10000, 30000]
    assert result.items == [] and result.reason == REASON_NO_DATA
    assert result.radius_m == 30000 and result.expanded is True
    assert sorted(row.radius_m for row in all_cache(session)) == [5000, 10000, 30000]


def test_explicit_radius_never_expands(session: Session) -> None:
    client = RadiusAwareOverpass(elements_at(7.0))
    result = stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, radius_m=5000, client=client)
    assert client.radii == [5000], "调用方显式给了半径就只查那一档"
    assert result.items == [] and result.reason == REASON_NO_DATA
    assert result.requested_radius_m == 5000


def test_ladder_second_request_is_offline(session: Session) -> None:
    """阶梯跑完仍空 → 二次请求在第一档就命中负缓存,0 网络。"""
    stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, client=RadiusAwareOverpass([]))
    again = RadiusAwareOverpass([])
    result = stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, client=again)
    assert again.calls == 0
    assert result.reason == REASON_NO_DATA and result.from_cache is True


def test_nearest_km_comes_from_db_when_search_is_empty(session: Session) -> None:
    """空结果时 ``nearest_km`` 用**库里已知**的最近一家(只读库,不触网)。"""
    stay_service.upsert_stays(
        session,
        [
            {
                "osm_type": "node", "osm_id": 9, "name": "远处的度假村", "kind": "chalet",
                "lat": ORIGIN_LAT + 12.0 / KM_PER_DEGREE, "lng": ORIGIN_LNG,
                "tags": {"tourism": "chalet"},
            }
        ],
    )
    session.commit()
    result = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=5000, client=RadiusAwareOverpass([])
    )
    assert result.items == [] and result.reason == REASON_NO_DATA
    assert result.nearest_km == pytest.approx(12.0, abs=0.05)
    assert all_cache(session)[0].nearest_km == result.nearest_km
    assert stay_service.nearest_known_km(session, ORIGIN_LAT, ORIGIN_LNG, radius_m=5000) is None


# --------------------------------------------------------------------------- #
# 5. 批量估价:5 家/prompt、解析失败留 null、重试 ≤1
# --------------------------------------------------------------------------- #


def test_price_batches_chunks_by_five(session: Session) -> None:
    rows = [{"id": index, "name": f"酒店{index}"} for index in range(12)]
    batches = stay_service.price_batches(rows)
    assert [len(batch) for batch in batches] == [5, 5, 2]
    assert stay_service.price_batches([]) == []
    assert [len(batch) for batch in stay_service.price_batches(rows, batch_size=0)] == [5, 5, 2]


def test_stays_needing_price_skips_priced_and_unnamed(session: Session) -> None:
    stay_service.upsert_stays(
        session,
        [
            {"osm_type": "node", "osm_id": 1, "name": "有价酒店", "lat": ORIGIN_LAT,
             "lng": ORIGIN_LNG, "price_estimate": "约¥300-500/晚"},
            {"osm_type": "node", "osm_id": 2, "name": "", "lat": ORIGIN_LAT + 0.01,
             "lng": ORIGIN_LNG},
            {"osm_type": "node", "osm_id": 3, "name": "待估价酒店", "lat": ORIGIN_LAT + 0.02,
             "lng": ORIGIN_LNG},
        ],
    )
    session.commit()
    pending = stay_service.stays_needing_price(all_stays(session))
    assert [row.name for row in pending] == ["待估价酒店"]


def test_build_batch_price_prompt_lists_every_stay() -> None:
    batch = [
        {"name": f"酒店{index}", "kind": "hotel", "lat": ORIGIN_LAT + index * 0.01,
         "lng": ORIGIN_LNG, "distance_km": 1.0 + index, "tags": {"tourism": "hotel", "stars": "4"}}
        for index in range(1, 6)
    ]
    prompt = stay_service.build_batch_price_prompt(batch)
    for index in range(1, 6):
        assert f"第{index}家" in prompt and f"名称:酒店{index}" in prompt
    assert "stars=4" in prompt
    assert "请严格按下面 5 行输出" in prompt
    assert "1|价格: 约¥A-B/晚|简介: <40字内一句话>" in prompt
    assert "5|价格: 约¥A-B/晚|简介: <40字内一句话>" in prompt


def test_parse_batch_completion_maps_by_index_and_keeps_nulls() -> None:
    completion = (
        "3|价格: 约¥500-700/晚|简介: 湖景房。",
        "1|价格: 约¥200-300/晚|简介: 经济型。",
        "9|价格: 约¥1-2/晚|简介: 越界序号。",
        "这行没有序号",
    )
    parsed = stay_service.parse_batch_completion("\n".join(completion), 3)
    assert parsed[0] == ("约¥200-300/晚", "经济型。")
    assert parsed[1] == ("", ""), "缺行的那家留 null(不猜、不抛)"
    assert parsed[2] == ("约¥500-700/晚", "湖景房。")
    assert stay_service.parse_batch_completion(None, 2) == [("", ""), ("", "")]
    assert stay_service.parse_batch_completion("完全不是约定格式", 1) == [("", "")]


@pytest.mark.parametrize(
    "line,expected",
    [
        ("价格: 约¥300-500/晚|简介: 市中心。", ("约¥300-500/晚", "市中心。")),
        ("约¥300-500/晚|简介: 市中心。", ("约¥300-500/晚", "市中心。")),
        ("价格:300~500元 简介: 市中心。", ("约¥300-500/晚", "市中心。")),
        ("价格: 面议|简介: 市中心。", ("", "市中心。")),
        ("", ("", "")),
    ],
)
def test_parse_batch_line_tolerates_model_variants(line: str, expected: tuple[str, str]) -> None:
    assert stay_service.parse_batch_line(line) == expected


def test_estimate_batch_single_prompt_for_five_and_retry_once() -> None:
    batch = [{"name": f"酒店{index}"} for index in range(5)]
    llm = BatchLLM()
    parsed = stay_service.estimate_batch(batch, client=llm)
    assert llm.calls == 1, "5 家一个 prompt(不是 5 次调用)"
    assert llm.systems[0] == stay_service.BATCH_SYSTEM_PROMPT
    assert llm.options[0]["max_tokens"] == stay_service.PRICE_BATCH_MAX_TOKENS
    assert llm.options[0]["timeout"] == stay_service.PRICE_BATCH_TIMEOUT_S
    assert [price for price, _ in parsed] == [
        "约¥210-310/晚", "约¥220-320/晚", "约¥230-330/晚", "约¥240-340/晚", "约¥250-350/晚"
    ]
    assert all(intro for _, intro in parsed)


def test_estimate_batch_retries_at_most_once_on_error() -> None:
    batch = [{"name": "酒店1"}, {"name": "酒店2"}]
    llm = BatchLLM(error=DataSourceError("llm", "限流 429"))
    assert stay_service.estimate_batch(batch, client=llm) == [("", ""), ("", "")]
    assert llm.calls == 2, "1 次调用 + 最多 1 次重试"


def test_estimate_batch_retries_once_on_garbage_then_gives_up() -> None:
    batch = [{"name": "酒店1"}]
    llm = BatchLLM("我不知道这些酒店的价格")
    assert stay_service.estimate_batch(batch, client=llm) == [("", "")]
    assert llm.calls == 2


def test_estimate_batch_accepts_second_attempt() -> None:
    batch = [{"name": "酒店1"}, {"name": "酒店2"}]
    llm = BatchLLM("第一批全是废话", "1|价格: 约¥100-200/晚|简介: 青旅。\n2|价格: 约¥300/晚|简介: 民宿。")
    parsed = stay_service.estimate_batch(batch, client=llm)
    assert llm.calls == 2
    assert parsed == [("约¥100-200/晚", "青旅。"), ("约¥300/晚", "民宿。")]


def test_estimate_batch_swallows_unexpected_errors() -> None:
    batch = [{"name": "酒店1"}]
    llm = BatchLLM(error=RuntimeError("连接被重置"))
    assert stay_service.estimate_batch(batch, client=llm) == [("", "")]
    assert stay_service.estimate_batch([], client=llm) == []


def test_fill_prices_batched_uses_one_prompt_per_five(session: Session) -> None:
    stay_service.upsert_stays(
        session,
        [
            {"osm_type": "node", "osm_id": 200 + index, "name": f"酒店{index}",
             "lat": ORIGIN_LAT + index * 0.001, "lng": ORIGIN_LNG, "tags": {"tourism": "hotel"}}
            for index in range(7)
        ],
    )
    session.commit()
    rows = all_stays(session)
    llm = BatchLLM()
    filled = stay_service.fill_prices_batched(rows, client=llm)
    assert llm.calls == 2, "7 家 = 5 + 2 两个 prompt"
    assert filled == 7
    assert all(row.price_estimate and row.price_estimate.startswith("约¥") for row in rows)
    assert all(row.intro for row in rows)


def test_fill_prices_batched_leaves_null_when_model_fails(session: Session) -> None:
    stay_service.upsert_stays(
        session,
        [{"osm_type": "node", "osm_id": 300 + index, "name": f"酒店{index}",
          "lat": ORIGIN_LAT + index * 0.001, "lng": ORIGIN_LNG} for index in range(3)],
    )
    session.commit()
    rows = all_stays(session)
    llm = BatchLLM(error=DataSourceError("llm", "余额不足"))
    assert stay_service.fill_prices_batched(rows, client=llm) == 0
    assert llm.calls == 2
    assert all(row.price_estimate is None for row in rows), "解析不出就留 null,绝不编数字"


def test_resolve_price_llm_pins_qwen_token_plan_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """批量估价固定 qwen(token-plan)的 ``qwen3.8-max``;**不退回**别的供应商(不做双模型)。"""
    assert stay_service.resolve_price_llm().enabled is False, "没配 key → 停用客户端,不烧 token"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    assert stay_service.resolve_price_llm().enabled is False, "只有 deepseek key 也不换模型"

    monkeypatch.setenv("ALIBABA_TOKEN_PLAN_API_KEY", "qwen-key")
    llm = stay_service.resolve_price_llm()
    assert llm.enabled is True
    assert llm.resolved is not None
    assert llm.resolved.model == "qwen3.8-max"
    assert llm.resolved.provider == "qwen"
    assert "token-plan" in llm.resolved.base_url
    assert llm.resolved.key_env == "ALIBABA_TOKEN_PLAN_API_KEY"
    assert llm.resolved.api_key == "qwen-key"

    monkeypatch.setenv("WHERE2GO_LLM_MODEL", "qwen-custom")
    assert stay_service.resolve_price_llm().resolved.model == "qwen-custom"


def test_estimate_mode_policy() -> None:
    mode = stay_service.estimate_mode
    assert mode(None, pending_count=0) == stay_service.ESTIMATE_OFF
    assert mode(None, pending_count=1) == stay_service.ESTIMATE_SYNC
    assert mode(None, pending_count=5) == stay_service.ESTIMATE_SYNC
    assert mode(None, pending_count=6) == stay_service.ESTIMATE_ASYNC
    assert mode(None, pending_count=50) == stay_service.ESTIMATE_ASYNC
    assert mode("async", pending_count=1) == stay_service.ESTIMATE_ASYNC
    assert mode("sync", pending_count=99) == stay_service.ESTIMATE_SYNC
    assert mode("off", pending_count=99) == stay_service.ESTIMATE_OFF
    assert mode("乱写", pending_count=99) == stay_service.ESTIMATE_ASYNC
    assert mode("off", pending_count=0) == stay_service.ESTIMATE_OFF


# --------------------------------------------------------------------------- #
# 6. 异步回填:入库即刻返回、estimating 标志、后台自己开 Session
# --------------------------------------------------------------------------- #


def test_async_backfill_returns_immediately_and_fills_in_background(session: Session) -> None:
    """检索入库后**立即返回**(price_estimate=null),后台按 5 家/prompt 回填。"""
    client = RadiusAwareOverpass(elements_at(*[1.0 + index * 0.1 for index in range(7)]))
    recorder = RecordingExecutor()
    llm = BatchLLM()
    result = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=5000,
        client=client, llm=llm, executor=recorder, estimate="async",
    )
    assert len(result.items) == 7 == len(all_stays(session))
    assert all(item["price_estimate"] is None for item in result.items), "即刻返回,不等估价"
    assert all(item["price_is_estimate"] is False for item in result.items)
    assert result.estimating is True
    assert llm.calls == 0, "请求线程一次 LLM 都没调"
    assert len(recorder.jobs) == 1
    fn, args, kwargs = recorder.jobs[0]
    assert fn is stay_service.price_fill_job
    assert len(args[1]) == 7 and kwargs["batch_size"] == stay_service.PRICE_BATCH_SIZE

    stats = fn(*args, **kwargs)  # 跑后台线程体
    assert stats["scanned"] == 7 and stats["filled"] == 7 and stats["batches"] == 2
    assert llm.calls == 2
    session.expire_all()
    assert all(row.price_estimate for row in all_stays(session)), "回填后重查即有价格"


def test_auto_mode_switches_to_async_above_one_batch(session: Session) -> None:
    client = RadiusAwareOverpass(elements_at(*[1.0 + index * 0.1 for index in range(6)]))
    recorder = RecordingExecutor()
    result = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=5000,
        client=client, llm=BatchLLM(), executor=recorder,
    )
    assert result.estimating is True and len(recorder.jobs) == 1
    assert all(item["price_estimate"] is None for item in result.items)


def test_auto_mode_keeps_single_batch_sync(session: Session) -> None:
    """≤ 一批(5 家)就地算完再返回:首屏就有价格,``estimating=False``。"""
    client = RadiusAwareOverpass(elements_at(1.0, 2.0, 3.0))
    result = stay_service.load_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=5000, client=client, llm=BatchLLM()
    )
    assert result.estimating is False
    assert all(item["price_estimate"] for item in result.items)


def test_small_pending_batch_stays_sync_per_stay(session: Session) -> None:
    """既有口径不回归:一批以内(≤5 家)仍**逐家**同步估价(3 家 = 3 次调用)。"""
    client = RadiusAwareOverpass(elements_at(1.0, 2.0, 3.0))
    llm = BatchLLM("价格: 约¥200-400/晚\n简介: 位于市中心的经济型酒店。")
    items = stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=5000, client=client, llm=llm
    )
    assert llm.calls == 3
    assert all(item["price_estimate"] == "约¥200-400/晚" for item in items)
    assert items.estimating is False


def test_schedule_price_fill_skips_without_llm_key(session: Session) -> None:
    stay_service.upsert_stays(
        session,
        [{"osm_type": "node", "osm_id": 400 + index, "name": f"酒店{index}",
          "lat": ORIGIN_LAT + index * 0.001, "lng": ORIGIN_LNG} for index in range(7)],
    )
    session.commit()
    recorder = RecordingExecutor()
    scheduled = stay_service.schedule_price_fill(
        session, all_stays(session), executor=recorder
    )
    assert scheduled is False and recorder.jobs == [], "没配 key 就不排程(排了也白排)"
    assert stay_service.schedule_price_fill(session, [], executor=recorder) is False


def test_price_fill_job_opens_own_session_and_is_errorproof(session: Session) -> None:
    stay_service.upsert_stays(
        session,
        [{"osm_type": "node", "osm_id": 500 + index, "name": f"酒店{index}",
          "lat": ORIGIN_LAT + index * 0.001, "lng": ORIGIN_LNG} for index in range(5)],
    )
    session.commit()
    engine = session.get_bind()
    ids = [row.id for row in all_stays(session)]

    stats = stay_service.price_fill_job(engine, ids, client=BatchLLM())
    assert stats["filled"] == 5 and stats["batches"] == 1
    session.expire_all()
    assert all(row.price_estimate for row in all_stays(session))

    # 已经全都有价了 → 不再调 LLM;空 id / 无引擎 / 抛异常都不冒出来
    assert stay_service.price_fill_job(engine, ids, client=BatchLLM())["filled"] == 0
    assert stay_service.price_fill_job(engine, [], client=BatchLLM())["scanned"] == 0
    assert stay_service.price_fill_job(None, ids, client=BatchLLM())["scanned"] == 0
    broken = stay_service.price_fill_job(engine, [-1, -2], client=BatchLLM(error=RuntimeError("炸了")))
    assert broken["filled"] == 0


def test_background_executor_is_injectable() -> None:
    from concurrent.futures import ThreadPoolExecutor

    stay_service.set_background_executor(None)
    pool = stay_service.background_executor()
    try:
        assert isinstance(pool, ThreadPoolExecutor)
        assert stay_service.background_executor() is pool, "进程内共享,不重复建池"
    finally:
        pool.shutdown(wait=False)
        stay_service.set_background_executor(None)
    assert stay_service.INLINE_EXECUTOR.submit(lambda: 42).result() == 42


def test_inline_executor_captures_job_errors() -> None:
    def boom() -> None:
        raise RuntimeError("后台炸了")

    future = stay_service.INLINE_EXECUTOR.submit(boom)
    with pytest.raises(RuntimeError):
        future.result()


# --------------------------------------------------------------------------- #
# 7. API 透传:reason / nearest_km / estimating(既有出参形状不变)
# --------------------------------------------------------------------------- #


def test_api_happy_path_keeps_legacy_top_level_shape(session: Session, monkeypatch) -> None:
    body = call_api(
        session, monkeypatch,
        client=RadiusAwareOverpass(elements_at(1.0, 2.0)),
        llm=BatchLLM("价格: 约¥200-400/晚\n简介: 市中心。"),
        radius_km=8,
    )
    assert set(body) == LEGACY_TOP_LEVEL_KEYS, "正常结果的顶层形状必须与既有一致"
    assert body["count"] == 2 and body["radius_km"] == 8.0
    assert body["source"] == stay_service.SOURCE_FETCH
    assert all(item["price_estimate"] == "约¥200-400/晚" for item in body["items"])


def test_api_exposes_three_reason_tiers(session: Session, monkeypatch) -> None:
    empty = call_api(session, monkeypatch, client=RadiusAwareOverpass([]), radius_km=8)
    assert empty["count"] == 0 and empty["reason"] == REASON_NO_DATA
    assert empty["nearest_km"] is None and empty["estimating"] is False
    assert empty["source"] == stay_service.SOURCE_DB and empty["radius_km"] == 8.0
    assert LEGACY_TOP_LEVEL_KEYS <= set(empty)

    broken = call_api(
        session, monkeypatch, lat=31.5, lng=121.9,
        client=RadiusAwareOverpass(error=DataSourceError("overpass", "端点全挂")), radius_km=8,
    )
    assert broken["reason"] == REASON_DATASOURCE_ERROR and broken["estimating"] is False

    slow = call_api(
        session, monkeypatch, lat=31.6, lng=121.9,
        client=RadiusAwareOverpass(error=TransientDataSourceError("overpass", "请求超时(>30s)")),
        radius_km=8,
    )
    assert slow["reason"] == REASON_TIMEOUT
    assert "稍后再试" in slow["note"] and "负缓存" in slow["note"]


def test_api_negative_cache_makes_second_request_offline(session: Session, monkeypatch) -> None:
    first = RadiusAwareOverpass([])
    call_api(session, monkeypatch, client=first, radius_km=8)
    assert first.calls == 1
    second = RadiusAwareOverpass([])
    body = call_api(session, monkeypatch, client=second, radius_km=8)
    assert second.calls == 0, "6h 内同坐标同半径:0 网络"
    assert body["count"] == 0 and body["reason"] == REASON_NO_DATA


def test_api_without_radius_uses_ladder_and_echoes_default(session: Session, monkeypatch) -> None:
    client = RadiusAwareOverpass(elements_at(7.0))
    body = call_api(session, monkeypatch, client=client, llm=BatchLLM(
        "价格: 约¥200-400/晚\n简介: 市中心。"
    ))
    assert client.radii == [5000, 10000], "未给 radius_km → 服务层按阶梯扩"
    assert body["radius_km"] == stays_api.DEFAULT_RADIUS_KM, "出参恒回显默认 8 km"
    assert body["count"] == 1
    assert body["nearest_km"] == pytest.approx(7.0, abs=0.05)
    assert "阶梯" in body["note"]


def test_api_explicit_radius_is_converted_to_meters(session: Session, monkeypatch) -> None:
    client = RadiusAwareOverpass(elements_at(1.0, 7.0))
    body = call_api(session, monkeypatch, client=client, radius_km=2.5, llm=BatchLLM(
        "价格: 约¥200-400/晚\n简介: 市中心。"
    ))
    assert client.radii == [2500] and body["radius_km"] == 2.5
    assert [item["name"] for item in body["items"]] == ["测试酒店1"]


def test_api_reports_estimating_while_backfill_runs(session: Session, monkeypatch) -> None:
    """一大批住宿 → 后台批量回填:响应即刻返回 null 价格 + ``estimating=true``。"""
    recorder = RecordingExecutor()
    stay_service.set_background_executor(recorder)
    monkeypatch.setenv("ALIBABA_TOKEN_PLAN_API_KEY", "test-key")
    # 后台任务里解析 LLM 的入口换成假客户端:真客户端在本套单测里会被 no_network 打死
    monkeypatch.setattr(stay_service, "resolve_price_llm", lambda environ=None: BatchLLM())
    client = RadiusAwareOverpass(elements_at(*[1.0 + index * 0.1 for index in range(7)]))
    body = call_api(session, monkeypatch, client=client, radius_km=8)
    assert body["count"] == 7
    assert body["estimating"] is True
    assert all(item["price_estimate"] is None for item in body["items"])
    assert "后台批量回填" in body["note"]
    assert len(recorder.jobs) == 1

    stats = recorder.run_all()[0]  # 跑完后台任务:价格落库,下一次请求就有
    assert stats["filled"] == 7 and stats["batches"] == 2
    cached = call_api(session, monkeypatch, client=client, radius_km=8)
    assert cached["count"] == 7 and cached["source"] == stay_service.SOURCE_DB
    assert all(item["price_estimate"] for item in cached["items"])
    assert "estimating" not in cached, "回填完就不该再报「估价中」"


def test_api_without_llm_key_returns_null_prices_without_backfill(session: Session, monkeypatch) -> None:
    client = RadiusAwareOverpass(elements_at(*[1.0 + index * 0.1 for index in range(7)]))
    body = call_api(session, monkeypatch, client=client, radius_km=8)
    assert body["count"] == 7
    assert body.get("estimating", False) is False, "没配 key 就不排后台任务、不谎报「估价中」"
    assert set(body) == LEGACY_TOP_LEVEL_KEYS, "有结果且未降级 → 顶层形状保持既有 7 个字段"
    assert all(item["price_estimate"] is None for item in body["items"])
    assert all(item["estimated"] == stays_api.ESTIMATED_LABEL for item in body["items"])


# --------------------------------------------------------------------------- #
# 8. 既有行为不回归
# --------------------------------------------------------------------------- #


def test_search_stays_still_degrades_to_plain_empty_list() -> None:
    client = RadiusAwareOverpass(error=DataSourceError("overpass", "端点全挂"))
    assert stay_service.search_stays(ORIGIN_LAT, ORIGIN_LNG, 8000, client=client) == []
    assert stay_service.search_stays(999.0, 0.0, client=client) == []
    rows = stay_service.search_stays(ORIGIN_LAT, ORIGIN_LNG, 8000, client=RadiusAwareOverpass(elements_at(1.0)))
    assert isinstance(rows, list) and rows[0]["name"] == "测试酒店1"


def test_search_stays_detailed_reports_reason() -> None:
    rows, reason = stay_service.search_stays_detailed(
        ORIGIN_LAT, ORIGIN_LNG, 8000, client=RadiusAwareOverpass([])
    )
    assert rows == [] and reason == REASON_NO_DATA
    rows, reason = stay_service.search_stays_detailed(
        ORIGIN_LAT, ORIGIN_LNG, 8000, client=RadiusAwareOverpass(elements_at(1.0))
    )
    assert len(rows) == 1 and reason is None
    rows, reason = stay_service.search_stays_detailed(
        ORIGIN_LAT, ORIGIN_LNG, 8000,
        client=RadiusAwareOverpass(error=TimeoutError("timed out")),
    )
    assert rows == [] and reason == REASON_TIMEOUT


def test_load_or_fetch_stays_still_returns_list(session: Session) -> None:
    """既有入口:返回值还是 list(空结果 ``== []``),只是多挂了判别属性。"""
    broken = RadiusAwareOverpass(error=DataSourceError("overpass", "端点全挂"))
    empty = stay_service.load_or_fetch_stays(session, ORIGIN_LAT, ORIGIN_LNG, client=broken)
    assert isinstance(empty, list) and empty == []
    assert empty.reason == REASON_DATASOURCE_ERROR
    assert stay_service.load_or_fetch_stays(session, 999.0, 0.0, client=broken) == []

    # 上面两次失败已经写了负缓存,这里 refresh=True 强制重查(既有口径:refresh 跳过缓存)
    items = stay_service.load_or_fetch_stays(
        session, ORIGIN_LAT, ORIGIN_LNG, radius_m=5000, refresh=True,
        client=RadiusAwareOverpass(elements_at(1.0)), llm=BatchLLM(),
    )
    assert isinstance(items, list) and len(items) == 1
    assert items.source == stay_service.SOURCE_FETCH
    assert items.radius_m == 5000 and items.nearest_km == pytest.approx(1.0, abs=0.05)
    assert list(items) == items


def test_bad_origin_and_radius_degrade_without_network(session: Session) -> None:
    client = RadiusAwareOverpass(elements_at(1.0))
    result = stay_service.load_stays(session, 999.0, 0.0, client=client)
    assert result.items == [] and client.calls == 0
    result = stay_service.load_stays(session, ORIGIN_LAT, ORIGIN_LNG, radius_m=0, client=client)
    assert result.items == [] and client.calls == 0
