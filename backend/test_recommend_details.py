"""功能2(AI 推荐)/ 功能3(2~3 句长介绍)/ 功能4(收藏父级)的后端单测。

口径:全部 **mock,不触网** —— LLM 用 :class:`FakeLLM` 替身(与 test_classify 同款),
数据源调用被 autouse 的 ``no_network`` 拦住。断言集中在三件事:

1. 推荐结果**按候选指纹缓存**(同分段重复请求不再调 LLM)、模型编造的 id 被丢弃、
   没有 key / 调用失败时**降级为按距离**(``degraded`` + ``basis=distance``)而不是报错;
2. 长介绍按 POI 缓存(已有不再生成)、单批上限生效、失败时 ``reason`` 区分得出来;
3. 收藏的父级标注写进 ``summary.parent``:显式给的原样收,路线收藏自动派生目的地。
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest
import requests
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api import collections as collections_api
from app.api import places as places_api
from db import repository as repo
from db.base import init_db, make_engine, session_factory
from db.models import CAT_MANUAL, Place, PlaceDetail, PlaceRecommendation
from services import details as detail_service
from services import intro as intro_service
from services import recommend as recommend_service


# --------------------------------------------------------------------------- #
# 替身与 fixtures
# --------------------------------------------------------------------------- #


class FakeLLM:
    """LLM 替身:记录 prompt、可切换成抛错/未配置(与 test_classify.FakeLLM 同形状)。"""

    def __init__(self, reply: str = "", error: Optional[Exception] = None,
                 enabled: bool = True) -> None:
        self.reply = reply
        self.error = error
        self._enabled = enabled
        self.prompts: list[str] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def label(self) -> str:
        return "假 LLM · fake-model"

    def chat(self, prompt: str, *, system: str = "") -> str:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.reply


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底:任何 requests 调用都视为测试失败(本套单测必须纯 mock)。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'feature_test.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


def seed(session: Session, *, osm_id: int, name: str, category: str = "自然风光",
         tags: Optional[dict[str, Any]] = None, lat: float = 31.5, lng: float = 121.5,
         city: str = "上海", band: str = "50_100") -> Place:
    row = Place(osm_type="node", osm_id=osm_id, name=name, lat=lat, lng=lng,
                category=category, tags=tags or {"natural": "peak"},
                origin_city=city, band=band)
    session.add(row)
    session.commit()
    return row


def place_dict(row: Place, distance_km: Optional[float] = 12.5) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "category": row.category, "tags": dict(row.tags or {}),
            "distance_km": distance_km, "lat": row.lat, "lng": row.lng}


# --------------------------------------------------------------------------- #
# 1. 推荐:纯函数(指纹 / 解析 / 兜底)
# --------------------------------------------------------------------------- #


def test_candidate_signature_is_order_insensitive_and_changes_with_candidates() -> None:
    rows = [{"id": 3}, {"id": 1}, {"id": 2}]
    assert recommend_service.candidate_signature(rows) == recommend_service.candidate_signature(
        [{"id": 1}, {"id": 2}, {"id": 3}]
    ), "指纹与候选顺序无关"
    assert recommend_service.candidate_signature(rows) != recommend_service.candidate_signature(
        [{"id": 1}, {"id": 2}]
    ), "候选变化 → 指纹变化 → 缓存自然失效"
    assert recommend_service.candidate_signature([]) == "empty"


def test_clamp_count_keeps_three_to_five() -> None:
    assert recommend_service.clamp_count(None) == recommend_service.DEFAULT_COUNT == 5
    assert recommend_service.clamp_count(0) == recommend_service.MIN_COUNT
    assert recommend_service.clamp_count(9) == recommend_service.MAX_COUNT
    assert recommend_service.clamp_count("3") == 3


def test_parse_recommendations_drops_unknown_and_duplicate_ids() -> None:
    places = [{"id": 11, "name": "A"}, {"id": 12, "name": "B"}]
    raw = json.dumps([
        {"place_id": 999, "reason": "编造的 id 应被丢弃"},
        {"place_id": 12, "reason": "值得去"},
        {"place_id": 12, "reason": "重复"},
        {"place_id": 11, "reason": "x" * 200},
    ], ensure_ascii=False)
    picked = recommend_service.parse_recommendations(raw, places, count=5)
    assert [item["place_id"] for item in picked] == [12, 11]
    assert [item["rank"] for item in picked] == [1, 2]
    assert len(picked[1]["reason"]) <= recommend_service.MAX_REASON_CHARS, "超长理由应截断"


def test_extract_json_array_tolerates_code_fence_and_wrapper() -> None:
    assert recommend_service.extract_json_array("```json\n[{\"place_id\": 1}]\n```") == [{"place_id": 1}]
    assert recommend_service.extract_json_array('说明文字 [{"place_id": 2}] 结尾') == [{"place_id": 2}]
    assert recommend_service.extract_json_array("模型今天不想输出") is None
    assert recommend_service.extract_json_array('{"items": [{"place_id": 3}]}') == [{"place_id": 3}]


def test_build_prompt_carries_facts_and_place_ids() -> None:
    places = [{"id": 7, "name": "四明山", "category": "自然风光", "distance_km": 130.0,
               "tags": {"natural": "peak"}}]
    prompt = recommend_service.build_prompt(places, count=3, origin_name="上海",
                                            band_label="100-200 km", category="自然风光")
    assert "place_id=7" in prompt and "四明山" in prompt and "130 km" in prompt
    assert "100-200 km" in prompt and "3 个" in prompt


# --------------------------------------------------------------------------- #
# 2. 推荐:服务层(缓存 / 降级 / refresh)
# --------------------------------------------------------------------------- #


def test_recommend_places_caches_by_signature(session) -> None:
    rows = [seed(session, osm_id=index, name=f"山{index}") for index in range(1, 5)]
    llm = FakeLLM(reply=json.dumps([{"place_id": rows[2].id, "reason": "视野最好"},
                                    {"place_id": rows[0].id, "reason": "离得近"}],
                                   ensure_ascii=False))
    first = recommend_service.recommend_places(
        session, origin_city="上海", band="50_100", places=[place_dict(row) for row in rows],
        count=5, client=llm,
    )
    assert [item["name"] for item in first["items"]] == ["山3", "山1"]
    assert first["items"][0]["reason"] == "视野最好"
    assert first["basis"] == recommend_service.BASIS_LLM and first["degraded"] is False
    assert first["cached"] is False and len(llm.prompts) == 1

    again = recommend_service.recommend_places(
        session, origin_city="上海", band="50_100", places=[place_dict(row) for row in rows],
        count=5, client=llm,
    )
    assert again["cached"] is True and len(llm.prompts) == 1, "同指纹命中缓存,不再调 LLM"
    assert [item["id"] for item in again["items"]] == [item["id"] for item in first["items"]]

    forced = recommend_service.recommend_places(
        session, origin_city="上海", band="50_100", places=[place_dict(row) for row in rows],
        count=5, client=llm, refresh=True,
    )
    assert forced["cached"] is False and len(llm.prompts) == 2, "refresh=true 强制重新推荐"


def test_recommend_places_degrades_by_distance_without_llm(session) -> None:
    rows = [seed(session, osm_id=index, name=f"点{index}") for index in range(1, 7)]
    payload = recommend_service.recommend_places(
        session, origin_city="上海", band="50_100",
        places=[place_dict(row, distance_km=index) for index, row in enumerate(rows, start=1)],
        count=3, client=FakeLLM(enabled=False),
    )
    assert payload["degraded"] is True and payload["basis"] == recommend_service.BASIS_DISTANCE
    assert payload["reason"] == "no_key"
    assert [item["name"] for item in payload["items"]] == ["点1", "点2", "点3"], "按传入顺序(距离升序)兜底"


def test_recommend_places_degrades_when_llm_fails_or_output_unparsable(session) -> None:
    rows = [seed(session, osm_id=index, name=f"点{index}") for index in range(1, 4)]
    places = [place_dict(row) for row in rows]

    boom = recommend_service.recommend_places(
        session, origin_city="上海", band="50_100", places=places, count=3,
        client=FakeLLM(error=RuntimeError("超时")),
    )
    assert boom["degraded"] is True and boom["reason"].startswith("llm_error:")
    assert len(boom["items"]) == 3, "失败也要给用户可用的按距离结果"

    garble = recommend_service.recommend_places(
        session, origin_city="上海", band="100_200", places=places, count=3,
        client=FakeLLM(reply="今天不想输出 JSON"),
    )
    assert garble["degraded"] is True and garble["reason"] == "unparsable"


def test_recommend_places_without_candidates_is_empty(session) -> None:
    payload = recommend_service.recommend_places(
        session, origin_city="上海", band="50_100", places=[], count=5, client=FakeLLM(reply="[]"),
    )
    assert payload["items"] == [] and payload["reason"] == "no_candidates"


# --------------------------------------------------------------------------- #
# 3. 推荐:API 端点
# --------------------------------------------------------------------------- #


def test_api_recommend_endpoint_maps_reasons_and_details(session, monkeypatch) -> None:
    rows = [seed(session, osm_id=index, name=f"山{index}") for index in range(1, 4)]
    llm = FakeLLM(reply=json.dumps([{"place_id": rows[0].id, "reason": "冬日雪景"}], ensure_ascii=False))
    monkeypatch.setattr(recommend_service, "default_llm_client", lambda: llm)
    repo.upsert_details(session, [{"place_id": rows[0].id, "text": "两句介绍。第二句。"}], provider=llm.label)
    session.commit()

    payload = places_api.recommend_places(origin="上海", band="50_100", category=None,
                                          count=3, refresh=False, session=session)
    assert payload["count"] == 1 and payload["band"]["key"] == "50_100"
    item = payload["items"][0]
    assert item["name"] == "山1" and item["reason"] == "冬日雪景"
    assert item["detail"] == "两句介绍。第二句。", "推荐条顺带带上已生成的长介绍"
    assert payload["detail_pending"] == 2
    assert payload["basis"] == recommend_service.BASIS_LLM


def test_api_recommend_endpoint_validates_band_and_city(session) -> None:
    with pytest.raises(HTTPException) as caught:
        places_api.recommend_places(origin="上海", band="9_99", category=None, count=None,
                                    refresh=False, session=session)
    assert caught.value.status_code == 400 and "未知距离分段" in str(caught.value.detail)
    with pytest.raises(HTTPException) as caught:
        places_api.recommend_places(origin="  ", band="50_100", category=None, count=None,
                                    refresh=False, session=session)
    assert caught.value.status_code == 400 and "起点城市不能为空" in str(caught.value.detail)


# --------------------------------------------------------------------------- #
# 4. 长介绍(功能3)
# --------------------------------------------------------------------------- #


def test_clean_detail_strips_prefix_and_closes_sentence() -> None:
    assert detail_service.clean_detail("介绍：这里有山有水\n适合秋天去") == "这里有山有水 适合秋天去。"
    assert detail_service.clean_detail("   ") == ""
    assert len(detail_service.clean_detail("很长" * 200)) <= detail_service.DETAIL_MAX_CHARS


def test_fill_missing_details_caches_per_poi(session) -> None:
    rows = [seed(session, osm_id=index, name=f"点{index}") for index in range(1, 4)]
    reply = "看点一是山顶视野开阔,天气好能看到远处的海湾;适合秋天上午出发,中午前登顶。"
    llm = FakeLLM(reply=reply)
    stats = detail_service.fill_missing_details(
        session, origin_city="上海", band="50_100", limit=2, client=llm, workers=1,
    )
    assert stats["filled"] == 2 and stats["pending"] == 1 and len(llm.prompts) == 2
    assert repo.count_places_missing_detail(session, origin_city="上海", band="50_100") == 1

    again = detail_service.fill_missing_details(
        session, origin_city="上海", band="50_100", limit=10, client=llm, workers=1,
    )
    assert again["scanned"] == 1 and again["pending"] == 0
    assert len(llm.prompts) == 3, "已有长介绍的 POI 不再调 LLM"
    text = repo.detail_map(session, [rows[0].id])[rows[0].id]
    assert text.endswith("。") and "看点一" in text


def test_fill_missing_details_reports_reason_when_llm_unavailable(session) -> None:
    seed(session, osm_id=1, name="点1")
    stats = detail_service.fill_missing_details(
        session, origin_city="上海", band="50_100", limit=5,
        client=FakeLLM(enabled=False), workers=1,
    )
    assert stats["filled"] == 0 and stats["reason"] == "no_key" and stats["pending"] == 1


def test_api_details_endpoint_returns_filled_texts(session, monkeypatch) -> None:
    rows = [seed(session, osm_id=index, name=f"点{index}") for index in range(1, 4)]
    monkeypatch.setattr(detail_service, "default_llm_client",
                        lambda: FakeLLM(reply="亮点是古村落的老街与小吃,适合傍晚散步;秋天来能看到晒秋。"))
    payload = places_api.fill_details(origin="上海", band="50_100", category=None, limit=2,
                                      place_ids=f"{rows[0].id},{rows[1].id}", session=session)
    assert payload["filled"] == 2 and payload["pending"] == 1
    assert {item["place_id"] for item in payload["items"]} == {rows[0].id, rows[1].id}
    assert all(item["text"] for item in payload["items"])
    assert payload["provider"].startswith("假 LLM")

    with pytest.raises(HTTPException) as caught:
        places_api.fill_details(origin="上海", band="50_100", category="美食", limit=None,
                                place_ids=None, session=session)
    assert caught.value.status_code == 400 and "未知分类" in str(caught.value.detail)


# --------------------------------------------------------------------------- #
# 5. 收藏父级(功能4)
# --------------------------------------------------------------------------- #


def test_collection_keeps_explicit_parent_in_summary(session) -> None:
    payload = collections_api.create_collection(
        payload={
            "kind": "place", "name": "📍 四明山 · 目的地", "osm_type": "node", "osm_id": 42,
            "to_lat": 29.7, "to_lng": 121.0,
            "summary": {"intro": "山景好"},
            "parent": {"kind": "place", "ref_key": "place:node/42", "name": "四明山"},
        },
        session=session,
    )
    row = payload["collection"]
    assert row["summary"]["parent"] == {"kind": "place", "ref_key": "place:node/42", "name": "四明山"}
    stored = repo.list_collections(session)[0]
    assert stored["summary"]["parent"]["ref_key"] == "place:node/42"


def test_route_collection_derives_parent_from_destination(session) -> None:
    payload = collections_api.create_collection(
        payload={
            "kind": "route", "mode": "driving", "to_name": "四明山",
            "osm_type": "node", "osm_id": 42, "to_lat": 29.7, "to_lng": 121.0,
            "from_lat": 31.23, "from_lng": 121.47, "from_name": "上海",
            "summary": {"duration_min": 120, "cost_cny": 150, "distance_km": 160},
        },
        session=session,
    )
    parent = payload["collection"]["summary"]["parent"]
    assert parent["ref_key"] == "place:node/42" and parent["name"] == "四明山", \
        "路线收藏自动挂到目的地(与目的地收藏的 ref_key 同口径)"
    assert payload["collection"]["ref_key"].startswith("route:")


def test_collection_rejects_bad_parent(session) -> None:
    with pytest.raises(HTTPException) as caught:
        collections_api.create_collection(
            payload={"kind": "place", "to_lat": 1.0, "to_lng": 2.0, "parent": {"name": "缺 ref_key"}},
            session=session,
        )
    assert caught.value.status_code == 400 and "ref_key" in str(caught.value.detail)
    with pytest.raises(HTTPException) as caught:
        collections_api.create_collection(
            payload={"kind": "place", "to_lat": 1.0, "to_lng": 2.0, "parent": "不是对象"},
            session=session,
        )
    assert caught.value.status_code == 400 and "JSON 对象" in str(caught.value.detail)


def test_recommendation_and_detail_tables_are_wired(session) -> None:
    """新表在建库时就位(不需要迁移):写入/读回都通。"""
    row = seed(session, osm_id=1, name="点1")
    created, is_new = repo.upsert_recommendation(
        session, origin_city="上海", band="50_100", signature="sig",
        items=[{"place_id": row.id, "rank": 1, "reason": "值得去"}], provider="假 LLM",
        basis=recommend_service.BASIS_LLM,
    )
    session.commit()
    assert is_new is True and session.query(PlaceRecommendation).count() == 1
    cached = repo.get_recommendation(session, origin_city="上海", band="50_100", signature="sig")
    assert cached.items[0]["reason"] == "值得去"
    assert repo.upsert_details(session, [{"place_id": row.id, "text": "两句介绍。"}]) == 1
    session.commit()
    assert session.query(PlaceDetail).count() == 1
    assert repo.detail_map(session, [row.id]) == {row.id: "两句介绍。"}


def test_placeholder_models_import_does_not_break_meta() -> None:
    """新增两张表不影响 /api/places/meta 的形状(前端元信息唯一出处)。"""
    meta = places_api.places_meta()
    assert {"bands", "categories", "llm", "seeds"} <= set(meta)
    assert meta["categories"] and all(item["key"] for item in meta["categories"])
    assert CAT_MANUAL == "manual"
