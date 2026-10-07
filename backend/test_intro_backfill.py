"""TASK-12 单测:简介后台异步回填(不阻塞抓取返回)+ inflight 去重 + 降级不抛。

口径:
* 生产路径(真 ThreadPoolExecutor):``load_segment`` 抓取入库后**立即返回**,
  ``intro_stats=None``(前端走「待补简介」既有分支),简介由后台 worker 用
  **自开的 session** 补齐;
* InlineExecutor(conftest autouse,全部既有测试)下同步执行、返回真实 stats,
  所以既有断言零改动 —— 本文件是**唯一**显式换真线程池的地方;
* 后台 worker 任何异常(LLM 挂/库锁)一律吞掉,绝不向调用方冒泡。

全程不触网(no_network autouse + LLM 替身注入)。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from typing import Any, Optional

import pytest
import requests

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from db import init_db, make_engine, session_factory  # noqa: E402
from db import repository as repo  # noqa: E402
from db.models import Place  # noqa: E402
from services import intro as intro_service  # noqa: E402
from services import place_loader  # noqa: E402
from services.bands import require_band  # noqa: E402

SHANGHAI = {"lat": 31.2304, "lng": 121.4737}
KM_PER_DEGREE = 111.32
BAND = "50_100"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """任何偷偷联网的请求当场失败。"""

    def boom(self: requests.Session, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"测试不许触网:{args[:2]} {kwargs.get('url', '')}")

    monkeypatch.setattr(requests.Session, "request", boom, raising=True)


@pytest.fixture()
def session(tmp_path) -> Iterator[Any]:
    engine = make_engine(f"sqlite:///{tmp_path / 'backfill.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


class SlowFakeLLM:
    """LLM 替身:每次 chat 睡 ``delay`` 秒(模拟真实 3~5s/条),证明返回不被阻塞。"""

    label = "替身 · slow-fake"

    def __init__(self, *, delay: float = 0.3, text: str = "一句话简介。") -> None:
        self.delay = delay
        self.text = text
        self.calls = 0
        self.lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return True

    def chat(self, prompt: str, **kwargs: Any) -> str:
        with self.lock:
            self.calls += 1
        time.sleep(self.delay)
        return self.text


class ExplodingLLM:
    """每次调用都抛错:验证后台 worker 吞异常、不炸进程。"""

    label = "替身 · explode"

    @property
    def enabled(self) -> bool:
        return True

    def chat(self, prompt: str, **kwargs: Any) -> str:
        raise RuntimeError("LLM 炸了")


def point_at(km: float, *, name: str, osm_id: int) -> dict[str, Any]:
    """构造距上海 ``km`` 公里(正北)的环内高德风格候选。"""
    return {
        "id": f"B0{osm_id:06d}",
        "name": name,
        "lat": round(SHANGHAI["lat"] + km / KM_PER_DEGREE, 6),
        "lng": SHANGHAI["lng"],
        "typecode": "110202",
        "type": "风景名胜;风景名胜",
    }


class FakeFetcher:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls = 0

    def __call__(self, lat: float, lng: float, band: Mapping[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
        self.calls += 1
        return list(self.rows)


@pytest.fixture()
def real_executor() -> Iterator[None]:
    """本文件专用:换成真线程池(覆盖 conftest 的 InlineExecutor)。"""
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-backfill")
    intro_service.set_backfill_executor(pool)
    try:
        yield
    finally:
        pool.shutdown(wait=True)
        intro_service.set_backfill_executor(None)


def _load(session, fetcher: FakeFetcher, llm: Any, **kwargs: Any):
    monkey_llm = llm
    orig_client = intro_service.default_client
    intro_service.default_client = lambda: monkey_llm  # type: ignore[assignment]
    try:
        return place_loader.load_segment(
            session, city="上海", band=BAND, fetcher=fetcher,
            geocoder=lambda city: {"city": city, "name": city, **SHANGHAI, "geocoder": "amap"},
            seeds=[], **kwargs,
        )
    finally:
        intro_service.default_client = orig_client


# --------------------------------------------------------------------------- #
# 核心:异步回填不阻塞返回
# --------------------------------------------------------------------------- #


def test_load_segment_returns_before_intros_with_real_pool(session, real_executor) -> None:
    rows = [point_at(60 + i * 2, name=f"远山{i}", osm_id=100 + i) for i in range(6)]
    fetcher = FakeFetcher(rows)
    llm = SlowFakeLLM(delay=0.4)

    t0 = time.perf_counter()
    outcome = _load(session, fetcher, llm, intros=True)
    wall = time.perf_counter() - t0

    assert outcome.source == "amap" and outcome.written == 6
    assert outcome.intro_stats is None, "异步口径:返回时不带 stats(前端走「待补简介」分支)"
    assert wall < 6 * llm.delay, f"返回不该等 6 条 LLM({wall:.2f}s ≥ {6 * llm.delay}s)"
    assert all(not p["intro"] for p in outcome.places), "返回瞬间简介还没补上"

    session.rollback()  # 释放调用方读事务快照(SQLite SHARED 锁会挡后台 worker 写库)
    intro_service.wait_backfill(timeout=15)
    stored = repo.list_places(session, origin_city="上海", band=BAND,
                              origin_lat=SHANGHAI["lat"], origin_lng=SHANGHAI["lng"])
    assert stored and all(row["intro"] for row in stored), "后台补完后库内每行都有简介"
    assert llm.calls == 6

def test_sync_intros_flag_keeps_old_contract(session, real_executor) -> None:
    """CLI/测试的同步口径:sync_intros=True 时 stats 是真实 dict、返回前已补齐。"""
    fetcher = FakeFetcher([point_at(60, name="远山", osm_id=7)])
    llm = SlowFakeLLM(delay=0.05)
    outcome = _load(session, fetcher, llm, intros=True, sync_intros=True)
    assert isinstance(outcome.intro_stats, dict)
    assert outcome.intro_stats["filled"] == 1 and outcome.intro_stats["scanned"] == 1
    assert all(p["intro"] for p in outcome.places)


def test_inline_executor_mode_returns_real_stats(session) -> None:
    """conftest 默认 InlineExecutor:start_backfill 同步执行并返回 stats(既有断言口径)。"""
    fetcher = FakeFetcher([point_at(60, name="远山", osm_id=8)])
    outcome = _load(session, fetcher, SlowFakeLLM(delay=0.01), intros=True)
    assert isinstance(outcome.intro_stats, dict) and outcome.intro_stats["filled"] == 1


def test_no_intros_flag_skips_llm_entirely(session, real_executor) -> None:
    fetcher = FakeFetcher([point_at(60, name="远山", osm_id=9)])
    llm = SlowFakeLLM(delay=0.3)
    outcome = _load(session, fetcher, llm, intros=False)
    intro_service.wait_backfill(timeout=2)
    assert outcome.intro_stats is None and llm.calls == 0


# --------------------------------------------------------------------------- #
# start_backfill 单元口径
# --------------------------------------------------------------------------- #


def seed_place(session, *, name: str = "某山", intro: Optional[str] = None) -> Place:
    row = Place(osm_type="amap", osm_id=abs(hash(name)) % 10**8, name=name,
                lat=31.5, lng=121.5, category="自然风光", tags={},
                intro=intro, origin_city="上海", band=BAND)
    session.add(row)
    session.commit()
    return row


def test_start_backfill_returns_none_when_nothing_pending(session, real_executor) -> None:
    seed_place(session, intro="已有简介。")
    assert intro_service.start_backfill(session, origin_city="上海", band=BAND) is None


def test_start_backfill_dedupes_inflight(session, real_executor, monkeypatch) -> None:
    seed_place(session, name="甲山")
    gate = threading.Event()
    orig_worker = intro_service._backfill_worker

    def slow_worker(*args: Any, **kwargs: Any) -> Any:
        gate.wait(10)
        return orig_worker(*args, **kwargs)

    monkeypatch.setattr(intro_service, "_backfill_worker", slow_worker)
    llm = SlowFakeLLM(delay=0.01)
    orig_client = intro_service.default_client
    intro_service.default_client = lambda: llm  # type: ignore[assignment]
    try:
        first = intro_service.start_backfill(session, origin_city="上海", band=BAND)
        second = intro_service.start_backfill(session, origin_city="上海", band=BAND)
        assert first is None and second is None, "线程池模式一律立即返回 None"
        assert intro_service._backfill_inflight, "第一次提交后应处于 inflight"
    finally:
        gate.set()
        intro_service.wait_backfill(timeout=10)
        intro_service.default_client = orig_client
    assert not intro_service._backfill_inflight, "worker 落地后必须清 inflight"


def test_worker_swallows_llm_explosion(session, real_executor) -> None:
    seed_place(session, name="乙山")
    orig_client = intro_service.default_client
    intro_service.default_client = lambda: ExplodingLLM()  # type: ignore[assignment]
    try:
        assert intro_service.start_backfill(session, origin_city="上海", band=BAND) is None
        intro_service.wait_backfill(timeout=10)
    finally:
        intro_service.default_client = orig_client
    assert not intro_service._backfill_inflight
    stored = repo.list_places(session, origin_city="上海", band=BAND,
                              origin_lat=31.5, origin_lng=121.5)
    assert stored and not stored[0]["intro"], "炸了就降级空简介,行还在"


def test_worker_uses_fresh_session_not_caller_session(session, real_executor) -> None:
    """worker 必须自开 session:调用方 session 在回填期间被 close 也不该炸。"""
    seed_place(session, name="丙山")
    llm = SlowFakeLLM(delay=0.2)
    orig_client = intro_service.default_client
    intro_service.default_client = lambda: llm  # type: ignore[assignment]
    try:
        intro_service.start_backfill(session, origin_city="上海", band=BAND)
        session.close()  # 调用方 session 立即关闭
        intro_service.wait_backfill(timeout=10)
    finally:
        intro_service.default_client = orig_client
    engine = make_engine(str(session.get_bind().url))
    probe = session_factory(engine)()
    try:
        rows = repo.select_places(probe, origin_city="上海", band=BAND)
        assert rows and all(row.intro for row in rows), "后台用自开 session 补齐了简介"
    finally:
        probe.close()
        engine.dispose()


def test_read_from_db_path_never_triggers_backfill(session, real_executor) -> None:
    """二次查询读库路径:intro_stats 保持 None、零 LLM 调用(既有口径不回退)。"""
    fetcher = FakeFetcher([point_at(60, name="丁山", osm_id=11)])
    llm = SlowFakeLLM(delay=0.01)
    _load(session, fetcher, llm, intros=True)
    intro_service.wait_backfill(timeout=10)
    calls_before = llm.calls
    outcome = place_loader.load_segment(
        session, city="上海", band=BAND, fetcher=fetcher, seeds=[], intros=True,
    )
    assert outcome.source == "db" and outcome.network_used is False
    assert outcome.intro_stats is None
    assert llm.calls == calls_before, "读库路径一条 LLM 都不许调"
