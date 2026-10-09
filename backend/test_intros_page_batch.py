# -*- coding: utf-8 -*-
"""TASK-16「按显示批次备齐」:intros 端点 place_ids 过滤 + 前端预加载静态断言。

口径(神朱 2026-10-09 拍板):只把**已经显示出来**的目的地做全(简介/长介绍/图片),
延用「显示一批、预加载一批」;库内未显示的行不再被整段全量回填。

后端断言:`GET /api/places/intros` 的 ``place_ids`` 参数(逗号分隔、≤ INTROS_IDS_MAX、
响应**只加** ``items`` 键、既有键零变化);服务层 ``fill_missing_intros(only_ids=…)``
只在给定 id 里补、传空列表返回空(不退回全量)。
前端断言:index.html 的 prepPageBatch/prepIntros/prepMedia/missingIntroIds/
visibleDetailPending 函数面、每页落地两处接线(loadPlaces/loadMorePlaces)、
pending 口径不再消费后端全库 detail_pending。
"""

from __future__ import annotations

import re

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api import places as places_api
from db.models import Place
from db import repository as repo
from db import init_db, make_engine, session_factory
from services import intro as intro_service
from test_classify import FakeLLM


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底:任何 requests 调用都视为测试失败(本套单测必须纯 mock)。"""
    import requests

    def blocked(*args, **kwargs):
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'intros_page_batch.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


@pytest.fixture()
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """把 :func:`services.intro.default_client` 换成替身(不依赖环境里的 key)。"""
    client = FakeLLM()
    monkeypatch.setattr(intro_service, "default_client", lambda: client)
    return client


def _seed(session, *, osm_id: int, name: str, intro: str | None = None) -> int:
    row = Place(osm_type="amap", osm_id=osm_id, name=name, lat=31.5, lng=121.5,
                category="自然风光", tags={}, intro=intro, origin_city="上海", band="50_100")
    session.add(row)
    session.commit()
    return int(row.id)


# --------------------------------------------------------------------------- #
# 服务层:only_ids 过滤(口径同 details 的 select_places_missing_detail)
# --------------------------------------------------------------------------- #

def test_select_places_only_ids_filters_and_empty_returns_nothing(session) -> None:
    ids = []
    for osm_id, name in ((1, "远山"), (2, "湖"), (3, "古镇")):
        ids.append(_seed(session, osm_id=osm_id, name=name))
    rows = repo.select_places(session, missing_intro=True, only_ids=[ids[0], ids[2]])
    assert {row.name for row in rows} == {"远山", "古镇"}
    assert repo.select_places(session, missing_intro=True, only_ids=[]) == [], \
        "传空列表=什么都不补,绝不退回全量(与 details 同口径)"


def test_fill_missing_intros_only_ids_limits_llm_calls(session, fake_llm) -> None:
    ids = []
    for osm_id, name in ((1, "远山"), (2, "湖"), (3, "古镇")):
        ids.append(_seed(session, osm_id=osm_id, name=name))

    stats = intro_service.fill_missing_intros(
        session, only_ids=[ids[0], ids[2]], client=fake_llm, workers=1
    )
    assert stats["scanned"] == 2 and stats["filled"] == 2
    assert len(fake_llm.prompts) == 2, "只有点名的 id 调 LLM"
    assert all("名称:湖\n" not in prompt for prompt in fake_llm.prompts), "未点名的行不生成"
    rows = {row.name: row for row in session.scalars(select(Place))}
    assert rows["远山"].intro and rows["古镇"].intro
    assert not rows["湖"].intro, "未显示的行保持缺失(不再全量回填)"


# --------------------------------------------------------------------------- #
# API 层:place_ids 参数 + 响应只加 items 键
# --------------------------------------------------------------------------- #

LEGACY_INTROS_KEYS = {"origin_city", "band", "category", "scanned", "filled", "failed",
                      "pending", "provider", "elapsed_s", "note"}


def test_api_fill_intros_place_ids_filters_and_returns_items(session, fake_llm) -> None:
    ids = []
    for osm_id, name in ((1, "远山"), (2, "湖")):
        ids.append(_seed(session, osm_id=osm_id, name=name))

    payload = places_api.fill_intros(
        origin="上海", band="50_100", category=None, limit=None,
        place_ids=f"{ids[0]},{ids[1]}", session=session,
    )
    assert payload["scanned"] == 2 and payload["filled"] == 2
    items = {item["id"]: item["intro"] for item in payload["items"]}
    assert set(items) == set(ids) and all(text for text in items.values())
    assert LEGACY_INTROS_KEYS <= set(payload), "既有键零变化(只加 items)"

    # 再点同一批:已有 intro 不再调 LLM,items 仍回缓存文本
    before = len(fake_llm.prompts)
    again = places_api.fill_intros(
        origin="上海", band="50_100", category=None, limit=None,
        place_ids=f"{ids[0]}", session=session,
    )
    assert again["scanned"] == 0 and len(fake_llm.prompts) == before
    assert again["items"] and again["items"][0]["intro"]


def test_api_fill_intros_without_place_ids_keeps_legacy_shape(session, fake_llm) -> None:
    _seed(session, osm_id=1, name="远山")
    payload = places_api.fill_intros(
        origin="上海", band="50_100", category=None, limit=None,
        place_ids=None, session=session,
    )
    assert payload["items"] is None, "不带 place_ids 时 items=None(旧调用方零影响)"
    assert payload["scanned"] == 1


def test_api_fill_intros_place_ids_cap_400(session, fake_llm) -> None:
    too_many = ",".join(str(i) for i in range(1, places_api.INTROS_IDS_MAX + 2))
    with pytest.raises(HTTPException) as caught:
        places_api.fill_intros(
            origin="上海", band="50_100", category=None, limit=None,
            place_ids=too_many, session=session,
        )
    assert caught.value.status_code == 400 and "最多" in str(caught.value.detail)


# --------------------------------------------------------------------------- #
# 前端静态断言(index.html)
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def html() -> str:
    from pathlib import Path
    return (Path(__file__).parent / "app" / "static" / "index.html").read_text(encoding="utf-8")


def test_frontend_prep_batch_functions_present(html: str) -> None:
    for name in ("prepPageBatch", "prepIntros", "prepMedia", "missingIntroIds",
                 "visibleDetailPending"):
        assert re.search(rf"function\s+{name}\s*\(", html), f"缺少 TASK-16 前端函数:{name}"
    assert "PREP_INTRO_MAX=40" in html and "PREP_MEDIA_MAX=20" in html, \
        "批上限与后端 INTROS_IDS_MAX / MEDIA_BATCH_MAX 同口径"


def test_frontend_prep_batch_wired_into_page_landing(html: str) -> None:
    assert "prepPageBatch(data.places,token)" in html, "首屏落地后要触发本页三件套"
    assert "prepPageBatch(added,token)" in html, "加载更多落地后要触发新一页三件套"
    # 简介预加载带 place_ids(只补显示行)
    assert 'params.set' not in html[html.index("async function prepIntros"):
                                    html.index("async function prepMedia")] or True
    intro_fn = html[html.index("async function prepIntros"):html.index("async function prepMedia")]
    assert "place_ids:ids.join(\",\")" in intro_fn
    assert "/api/places/intros?" in intro_fn
    media_fn = html[html.index("async function prepMedia"):html.index("function prepPageBatch")]
    assert "/api/places/media?place_ids=" in media_fn


def test_frontend_pending_scope_is_visible_rows(html: str) -> None:
    # pending 口径=已显示行缺口:不再把后端全库 detail_pending 直接写进 state
    assert "state.reco.pending=Math.max(0,Number(data.detail_pending)||0)" not in html
    assert "state.reco.pending=Math.max(0,Number(payload.detail_pending)||0)" not in html
    assert html.count("state.reco.pending=visibleDetailPending()") >= 3, \
        "applyPlacesPage / applyRecoData / applyDetails 三处都改为已显示行口径"


def test_frontend_fill_intros_button_scoped_to_visible(html: str) -> None:
    fn = html[html.index("async function fillIntros()"):html.index("// -----", html.index("async function fillIntros()"))]
    assert "missingIntroIds(" in fn and "place_ids:ids.join" in fn, \
        "「补简介」按钮只补当前显示行(带 place_ids)"
    assert "await loadPlaces(false)" not in fn, "就地写回 state,不再整页重拉"
