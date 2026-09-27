"""目的地长介绍(神朱 2026-09-27 功能3):列表里每条 2~3 句的"重点介绍"。

与 :mod:`services.intro` 的分工:

* ``Place.intro``  —— 弹窗里的**一句话**简介(短、恒在,已有缓存口径不动);
* 本模块写 ``PlaceDetail.text`` —— 地图下方列表里的 **2~3 句重点介绍**(60~160 字,
  讲看点/适合人群/最佳季节/怎么安排半天到一天),按 POI 缓存,生成失败不写行、下次可重试。

为什么单独一张表而不是复用 ``Place.intro``:两处文案长度、用途、生成时机都不同
(弹窗要短、列表要长;列表是**懒加载**分批生成的)。分开存就不会出现"列表生成把弹窗
文案改长了"或"弹窗的短文案被当成长介绍渲染"的串味问题,也便于日后单独调 prompt 与系数。

懒加载口径(前端配合):先渲染列表骨架(名称/分类/距离,0 延迟),再按
"推荐条优先 → 其余按距离"分批调 :func:`fill_missing_details`,**每批不阻塞 UI**。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Optional

from sqlalchemy.orm import Session

from data_sources import DataSourceError
from db import repository as repo
from services.intro import (
    DEFAULT_WORKERS,
    LLMClient,
    _run_batch,
)
from services.intro import default_client as default_llm_client
from services.intro import tag_facts

# 列表文案长度口径:2~3 句、40~200 字(下限只用来滤掉"模型没听懂"的一两个词)
DETAIL_MIN_CHARS = 20
DETAIL_MAX_CHARS = 200
FACT_TAGS = 8
# 单次请求最多生成多少条(前端分批调用,单批别太大,免得一次等太久)
DEFAULT_BATCH = 10
MAX_BATCH = 40

# 输出预算与超时(与推荐同因:默认 120 tokens / 20s 对 2~3 句长介绍偏紧)
DETAIL_MAX_TOKENS = 500
DETAIL_TIMEOUT_S = 45.0

SOURCE_NAME = "Detail"
SYSTEM_PROMPT = (
    "你是 Where2Go(周末去哪儿玩)的目的地编辑,为目的地写**两到三句**重点介绍。"
    "要求:只依据给出的名称/分类/距离/OSM 标签线索写,不要编造门票价格、电话、"
    "具体营业时间或用户评价;突出这个目的地**为什么值得去**(看点、适合人群、"
    "最佳季节或半日/一日安排建议),不要用空洞的形容词堆砌,不要提及 OSM、标签、"
    "数据源或模型。直接输出介绍正文(2~3 句、60~160 字),不要标题、编号或前缀。"
)


def clean_detail(text: Any, *, max_chars: int = DETAIL_MAX_CHARS) -> str:
    """清洗模型输出:去掉前缀标签/引号/换行,按句收尾,超长截断。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    for prefix in ("介绍:", "介绍：", "重点介绍:", "重点介绍：", "简介:", "简介："):
        if raw.startswith(prefix):
            raw = raw[len(prefix):].strip()
    raw = raw.strip("\"'“”‘’「」『』《》 \n")
    raw = " ".join(raw.split())
    if len(raw) > max_chars:
        raw = raw[:max_chars].rstrip(" ,,;;、、")
    if raw and raw[-1] not in "。!?!?！？":
        if len(raw) >= max_chars:  # 留一位给句号,免得清洗完反而超长
            raw = raw[: max_chars - 1].rstrip(" ,,;;、、")
        raw += "。"
    return raw


def build_detail_prompt(place: Mapping[str, Any]) -> str:
    """一条目的地 → 长介绍 prompt(名称 + 分类 + 距离 + 标签线索)。"""
    distance = place.get("distance_km")
    distance_text = "未知" if distance is None else f"{float(distance):g} km"
    tags = tag_facts(place.get("tags") if isinstance(place.get("tags"), Mapping) else None,
                     limit=FACT_TAGS)
    lines = [
        f"目的地:{place.get('name') or '(无名)'}",
        f"分类:{place.get('category') or '其他'}",
        f"距起点直线距离:{distance_text}",
    ]
    if tags:
        lines.append(f"标签线索:{tags}")
    lines.append(
        f"请写 2~3 句重点介绍(60~{DETAIL_MAX_CHARS} 字):为什么值得去、适合谁、什么季节或怎么安排。"
    )
    return "\n".join(lines)


def generate_detail(
    place: Mapping[str, Any],
    *,
    client: Optional[LLMClient] = None,
    max_chars: int = DETAIL_MAX_CHARS,
) -> str:
    """给一个目的地生成长介绍;**任何失败都返回空串**(降级,不阻塞列表)。"""
    llm = client if client is not None else default_llm_client()
    if not llm.enabled:
        return ""
    if not str(place.get("name") or "").strip():
        return ""
    try:
        raw = llm.chat(build_detail_prompt(place), system=SYSTEM_PROMPT,
                       max_tokens=DETAIL_MAX_TOKENS, timeout=DETAIL_TIMEOUT_S)
    except DataSourceError:
        return ""
    except Exception:  # noqa: BLE001 - 长介绍是增强项,任何异常都不得阻塞列表
        return ""
    return clean_detail(raw, max_chars=max_chars)


def fill_missing_details(
    session: Session,
    *,
    origin_city: Optional[str] = None,
    band: Optional[str] = None,
    category: Optional[str] = None,
    limit: Optional[int] = DEFAULT_BATCH,
    only_ids: Optional[Sequence[Any]] = None,
    client: Optional[LLMClient] = None,
    generator: Optional[Any] = None,
    workers: int = DEFAULT_WORKERS,
    commit: bool = True,
) -> dict[str, Any]:
    """给还没有长介绍的 POI 补 2~3 句介绍(DB 即缓存,已有的不再调 LLM)。

    返回 ``{"scanned", "filled", "failed", "pending", "provider", "reason"}``;
    ``reason`` 区分"没 key"(``no_key``)/"全部失败"(``all_failed``)/正常(``ok``),
    前端据此给出可操作提示而不是一句含糊的兜底话术(BUG-2 的可见性口径)。
    """
    rows = repo.select_places_missing_detail(
        session,
        origin_city=origin_city,
        band=band,
        category=category,
        only_ids=only_ids,
        limit=limit,
    )
    llm = client if client is not None else default_llm_client()
    provider = llm.label if llm.enabled else "未配置"
    pending = repo.count_places_missing_detail(
        session, origin_city=origin_city, band=band, category=category
    )
    if not rows:
        return {"scanned": 0, "filled": 0, "filled_ids": [], "failed": 0, "pending": pending,
                "provider": provider, "reason": "ok", "written": 0}

    payloads = [
        {
            "id": row.id,
            "name": row.name,
            "category": row.category,
            "distance_km": None,
            "tags": dict(row.tags or {}),
        }
        for row in rows
    ]
    produce = generator or (lambda place: generate_detail(place, client=llm))
    texts = _run_batch(produce, payloads, workers=workers)

    fresh: list[dict[str, Any]] = []
    for payload, text in zip(payloads, texts):
        cleaned = clean_detail(text)
        if len(cleaned) < DETAIL_MIN_CHARS:
            continue
        fresh.append({"place_id": payload["id"], "text": cleaned})
    written = repo.upsert_details(session, fresh, provider=provider) if fresh else 0
    if written and commit:
        session.commit()
    reason = "ok"
    if not fresh and rows:
        reason = "no_key" if not llm.enabled else "all_failed"
    return {
        "scanned": len(rows),
        "filled": len(fresh),
        "filled_ids": [int(item["place_id"]) for item in fresh],
        "failed": len(rows) - len(fresh),
        "pending": max(0, pending - len(fresh)),
        "provider": provider,
        "reason": reason,
        "written": written,
    }
