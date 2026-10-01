"""TASK-9b 单测:一次性清库脚本 ``tools/amap_cutover.py``。

覆盖:--dry-run 只报数不删、--apply 清派生行且**保留用户数据**
(Collection/CollectionCat/TripPlan)、尚未建的派生表自动跳过、report 返回值口径。
全程离线:临时 SQLite(tmp_path),零网络。

运行:``cd backend && ../.venv/bin/python -m pytest test_amap_cutover.py -q``
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
import requests
from sqlalchemy import select

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from db import init_db, make_engine, session_factory  # noqa: E402
from db.models import (  # noqa: E402
    Collection,
    Place,
    SegmentFetch,
    Stay,
    TripPlan,
    utcnow,
)

REPO_ROOT = Path(BACKEND_DIR).parent


def _load_cutover():
    """按路径加载仓库根的 ``tools/amap_cutover.py``(不在包路径里)。"""
    path = REPO_ROOT / "tools" / "amap_cutover.py"
    assert path.exists(), "清库脚本必须存在(TASK-9b)"
    spec = importlib.util.spec_from_file_location("amap_cutover", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cutover = _load_cutover()


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args, **kwargs):
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture()
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'cutover.db'}")
    init_db(eng)
    yield eng
    eng.dispose()


def seed_rows(engine) -> dict[str, int]:
    """灌派生行 + 用户数据;返回各表行数。"""
    session = session_factory(engine)()
    try:
        session.add(
            Place(
                osm_type="node", osm_id=1, name="旧 OSM 行", lat=31.0, lng=121.0,
                category="自然风光", origin_city="上海", band="0_50", tags={},
            )
        )
        session.add(
            Place(
                osm_type="amap", osm_id=2, name="新高德行", lat=31.0, lng=121.0,
                category="自然风光", origin_city="上海", band="0_50",
                tags={"source": "高德", "amap_id": "B0FFH00002"},
            )
        )
        session.add(
            SegmentFetch(
                origin_city="上海", band="0_50", source="overpass",
                origin_lat=31.0, origin_lng=121.0, fetched_at=utcnow(),
            )
        )
        session.add(
            Stay(
                osm_type="node", osm_id=7, name="旧旅舍", kind="hostel",
                lat=31.0, lng=121.0, tags={}, fetched_at=utcnow(),
            )
        )
        session.add(
            Collection(
                kind="route", ref_key="上海/杭州西湖", mode="driving", name="上海 → 杭州西湖",
                summary={"duration_min": 50},
            )
        )
        session.add(TripPlan(name="我的方案", note="", place_collection_id=None))
        session.commit()
        counts = {
            "places": len(session.scalars(select(Place)).all()),
            "segment_fetch": len(session.scalars(select(SegmentFetch)).all()),
            "stays": len(session.scalars(select(Stay)).all()),
            "collections": len(session.scalars(select(Collection)).all()),
            "trip_plans": len(session.scalars(select(TripPlan)).all()),
        }
        return counts
    finally:
        session.close()


def test_tables_lists_are_pinned() -> None:
    """契约口径:清哪些表、留哪些表都钉死(误改清单会立刻被这条抓住)。"""
    assert set(cutover.PRESERVED_TABLES) == {"collections", "collection_cats", "trip_plans"}
    for name in ("places", "segment_fetch", "stays", "stay_query_cache", "origin_cache",
                 "place_recommendations", "place_details"):
        assert name in cutover.DERIVED_TABLES, f"{name} 属派生行,必须清"
    assert not (set(cutover.DERIVED_TABLES) & set(cutover.PRESERVED_TABLES))


def test_dry_run_reports_but_deletes_nothing(engine, capsys) -> None:
    counts = seed_rows(engine)
    removed = cutover.report(engine, apply=False)
    out = capsys.readouterr().out
    assert removed == 0, "dry-run 不许删"
    assert "DRY-RUN" in out and "--apply" in out
    assert cutover.count_rows(engine, "places") == counts["places"]
    assert cutover.count_rows(engine, "stays") == counts["stays"]
    assert cutover.count_rows(engine, "collections") == counts["collections"]
    assert cutover.count_rows(engine, "trip_plans") == counts["trip_plans"]


def test_apply_clears_derived_rows_and_preserves_user_data(engine, capsys) -> None:
    counts = seed_rows(engine)
    removed = cutover.report(engine, apply=True)
    assert removed >= counts["places"] + counts["stays"] + counts["segment_fetch"]
    for name in ("places", "segment_fetch", "stays", "stay_query_cache", "origin_cache"):
        assert cutover.count_rows(engine, name) == 0, f"{name} 应被清空"
    # 用户数据一行不动
    session = session_factory(engine)()
    try:
        kept = session.scalars(select(Collection)).all()
        assert len(kept) == counts["collections"] and kept[0].name == "上海 → 杭州西湖"
        plans = session.scalars(select(TripPlan)).all()
        assert len(plans) == counts["trip_plans"] and plans[0].name == "我的方案"
    finally:
        session.close()


def test_apply_is_idempotent(engine) -> None:
    seed_rows(engine)
    cutover.report(engine, apply=True)
    assert cutover.report(engine, apply=True) == 0, "已清过再 --apply 应是 0 行"


def test_missing_tables_are_skipped(tmp_path) -> None:
    """空库(派生表尚未建)不抛:跳过并报数 0。"""
    eng = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    try:
        present, skipped = cutover.existing_tables(eng)
        assert "place_media" in skipped or "place_highlights" in skipped
        assert cutover.report(eng, apply=False) == 0
    finally:
        eng.dispose()


def test_cli_defaults_to_dry_run(engine, monkeypatch, capsys) -> None:
    counts = seed_rows(engine)
    url = str(engine.url)
    assert cutover.main(["--db", url]) == 0, "缺省参数必须是 dry-run"
    assert cutover.count_rows(engine, "places") == counts["places"]
    assert cutover.main(["--db", url, "--apply"]) == 0
    assert cutover.count_rows(engine, "places") == 0
    assert cutover.count_rows(engine, "collections") == counts["collections"]
