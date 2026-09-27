"""AI 推荐(神朱 2026-09-27 功能2):从当前分段的候选目的地里挑 3~5 个"最值得去"的。

口径(与 :mod:`services.intro` 同一套 LLM 抽象、同一个 key,ADR-002):

* **输入**:已入库的 (起点城市, band, 分类) 目的地(前端已经拿到的那批);
  候选最多 :data:`CANDIDATE_LIMIT` 条(默认按距离由近到远,超出的丢弃,控 token);
  每条只喂"名称 / 分类 / 距起点 / OSM 标签摘要"——**不额外联网抓资料**,
  推荐依据 = LLM 自身知识 + 这批真实候选事实,响应里用 ``basis="llm+osm_tags"`` 标注。
  (将来要接"联网抓公开资料再推荐",只需在 :func:`build_prompt` 前把资料拼进候选 facts,
  接口与缓存形状不用动。)
* **输出**:严格 JSON ``[{"place_id", "reason"}, ...]``;解析容错(代码块/多余文字/数字 id),
  解析不出来就**降级**成"按距离取前 N 条 + 无推荐语",``degraded=True``。
* **缓存**:结果落 ``place_recommendations`` 表,键 = (城市, band, 分类, **候选指纹**);
  指纹 = 候选 id 集合 + 条数的 sha1(见 :func:`candidate_signature`),所以分段重新抓取 /
  换分类都会自然算出一条新缓存,同指纹重复请求**不再烧 token**。
* **失败语义**:LLM 未配置 / 调用失败 / 输出不可解析都不抛错,一律降级;调用方看
  ``degraded`` + ``reason`` 决定怎么标注。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from sqlalchemy.orm import Session

from data_sources import DataSourceError
from db import repository as repo
from db.models import utcnow
from services.intro import LLMClient
from services.intro import default_client as default_llm_client
from services.intro import tag_facts

# 候选上限:一条推荐请求最多喂多少条候选给 LLM(控 token 与成本)
CANDIDATE_LIMIT = 80
# 推荐条数:默认 5,允许 3~5(需求原文"三至五个")
DEFAULT_COUNT = 5
MIN_COUNT = 3
MAX_COUNT = 5
# 推荐理由长度上限(字)
MAX_REASON_CHARS = 80
FACT_TAGS = 6

BASIS_LLM = "llm+osm_tags"
BASIS_DISTANCE = "distance"

# 输出预算与超时(**2026-09-27 实测踩坑**):一条推荐理由 ≤80 字,5 条 = 400+ 汉字,
# 再叠 JSON 结构;LLM 客户端默认 max_tokens=120 / timeout=20s 会把结果**截断或读超时**,
# 表现为"推荐拿到 provider 却是降级结果"。实测 qwen3.8-max 对这份 prompt 单次要 45~95s
# (与候选条数关系不大,是模型侧延迟),所以超时给到 180s 留足余量。
RECO_MAX_TOKENS = 900
RECO_TIMEOUT_S = 180.0
# 降级结果的缓存寿命:**降级(没走成 AI)不该一直挂着**。命中降级缓存但已过期时自动重算,
# 既不会每次请求都去打一个正在挂的 LLM,也不会让"一次失败"钉死整个分段。
DEGRADED_RETRY_S = 600.0

SOURCE_NAME = "Recommend"
SYSTEM_PROMPT = (
    "你是 Where2Go(周末去哪儿玩)的目的地推荐助手。用户会给你一批**真实存在**的候选"
    "目的地(编号 = place_id,附名称、分类、距起点直线距离、部分 OSM 标签线索),"
    "请结合你对这些地方的了解,挑出最值得去的目的地并排序。"
    "判断依据优先看:独特性/知名度、适合周末出行、季节与人群匹配度、交通可达性(距离越近越易成行)。"
    "宁可挑有具体看点的地方(名山、古镇、温泉、雪场、知名步道等),不要挑名字含糊的村庄或普通街区。"
    "严格只输出一个 JSON 数组,不要任何解释、标题、代码块标记或多余文字:"
    '[{"place_id": 数字, "reason": "不超过 60 字的推荐理由(看点/适合谁/最佳季节)"}]'
    "。理由必须具体、不要空洞形容词,不要编造门票、电话、具体营业时间等无法确认的信息。"
)

JSON_BLOCK_RE = re.compile(r"\[.*\]", re.DOTALL)


def candidate_signature(places: Sequence[Mapping[str, Any]]) -> str:
    """候选集合指纹:有条目就按 id 升序拼串(稳定、与顺序无关),空集合用 ``empty``。"""
    ids = sorted(int(item["id"]) for item in places or [] if item.get("id") is not None)
    if not ids:
        return "empty"
    raw = f"n={len(ids)};" + ",".join(str(item) for item in ids)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]  # 仅做缓存键,非安全用途


def clamp_count(value: Any) -> int:
    """推荐条数收敛到 [MIN_COUNT, MAX_COUNT](非法值用默认值)。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_COUNT
    return max(MIN_COUNT, min(MAX_COUNT, number))


def degraded_cache_fresh(row: Any, *, now: Optional[Any] = None) -> bool:
    """降级推荐缓存是否还算新鲜(新鲜 = 直接复用,不重算)。

    不能把"降级"钉死:LLM 没配 key / 超时那一次算出来的按距离结果,如果在缓存里躺到永远,
    用户就会一直看到"按距离推荐"。这里给它 :data:`DEGRADED_RETRY_S` 的寿命。
    """
    stamp = getattr(row, "updated_at", None) or getattr(row, "created_at", None)
    if stamp is None:
        return False
    current = utcnow()
    try:
        if getattr(stamp, "tzinfo", None) is None:
            stamp = stamp.replace(tzinfo=current.tzinfo)
        return (current - stamp).total_seconds() < DEGRADED_RETRY_S
    except TypeError:  # 时间戳类型异常时按"不新鲜"处理,宁可重算一次
        return False


def candidate_facts(place: Mapping[str, Any], index: int) -> str:
    """一条候选 → 一行事实(编号 + 名称 + 分类 + 距离 + 标签摘要)。"""
    distance = place.get("distance_km")
    distance_text = "—" if distance is None else f"{float(distance):g} km"
    tags = tag_facts(place.get("tags") if isinstance(place.get("tags"), Mapping) else None,
                     limit=FACT_TAGS)
    parts = [
        f"{index}. place_id={place.get('id')}",
        str(place.get("name") or "(无名)"),
        str(place.get("category") or ""),
        f"距起点 {distance_text}",
    ]
    if tags:
        parts.append(f"标签:{tags}")
    return " | ".join(part for part in parts if part)


def build_prompt(
    places: Sequence[Mapping[str, Any]],
    *,
    count: int,
    origin_name: Optional[str] = None,
    band_label: Optional[str] = None,
    category: Optional[str] = None,
) -> str:
    """拼推荐 prompt:一段上下文 + 逐行候选 + 明确的输出要求。"""
    lines = [
        f"起点:{origin_name or '未知'}",
        f"距离分段:{band_label or '未知'}",
        f"分类:{category or '全部'}",
        f"候选目的地共 {len(places)} 条:",
    ]
    lines.extend(candidate_facts(place, index + 1) for index, place in enumerate(places))
    lines.append(
        f"请从上面候选中挑出 {count} 个最值得去的目的地(按推荐程度从高到低排序),"
        f"只输出 JSON 数组,每项形如 {{\"place_id\": 数字, \"reason\": \"不超过 60 字的理由\"}}。"
        "place_id 必须取自上面的候选,不要编造。"
    )
    return "\n".join(lines)


def extract_json_array(text: Any) -> Optional[list[Any]]:
    """从模型输出里抠出 JSON 数组(容忍代码块 ```json``` 与前后多余文字)。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    for candidate in (raw, raw.strip("`")):
        try:
            data = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(data, list):
            return data
        if isinstance(data, Mapping) and isinstance(data.get("items"), list):
            return list(data["items"])
    match = JSON_BLOCK_RE.search(raw)
    if match:
        try:
            data = json.loads(match.group(0))
        except (TypeError, ValueError):
            return None
        if isinstance(data, list):
            return data
    return None


def parse_recommendations(
    text: Any, places: Sequence[Mapping[str, Any]], *, count: int
) -> list[dict[str, Any]]:
    """模型输出 → ``[{"place_id", "rank", "reason"}]``。

    容错规则:只认候选里真实存在的 ``place_id``(模型编造的 id 直接丢弃)、去重、
    理由截断到 :data:`MAX_REASON_CHARS`;有效条数不足 ``count`` 时**不补齐**
    (补出来的没有推荐语,交给调用方判断是否降级)。解析不出任何条目时返回空列表。
    """
    rows = extract_json_array(text)
    if not rows:
        return []
    known = {int(item["id"]): item for item in places if item.get("id") is not None}
    picked: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        raw_id = item.get("place_id", item.get("id"))
        try:
            place_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if place_id not in known or place_id in seen:
            continue
        seen.add(place_id)
        reason = str(item.get("reason") or item.get("why") or "").strip()
        if len(reason) > MAX_REASON_CHARS:
            reason = reason[:MAX_REASON_CHARS].rstrip(" ,,;;、、")
        picked.append({"place_id": place_id, "rank": len(picked) + 1, "reason": reason})
        if len(picked) >= count:
            break
    return picked


def fallback_items(places: Sequence[Mapping[str, Any]], *, count: int) -> list[dict[str, Any]]:
    """LLM 不可用时的兜底:按传入顺序(调用方已按距离升序)取前 N 条,无推荐语。"""
    return [
        {"place_id": int(item["id"]), "rank": index + 1, "reason": ""}
        for index, item in enumerate(places[:count])
        if item.get("id") is not None
    ]


def recommend_places(
    session: Session,
    *,
    origin_city: str,
    band: str,
    category: Optional[str] = None,
    places: Sequence[Mapping[str, Any]],
    count: Any = DEFAULT_COUNT,
    origin_name: Optional[str] = None,
    band_label: Optional[str] = None,
    client: Optional[LLMClient] = None,
    refresh: bool = False,
    commit: bool = True,
) -> dict[str, Any]:
    """推荐当前分段的 3~5 个目的地:命中缓存直接返回,否则调 LLM 并落库。

    返回 ``{"items", "candidates", "degraded", "cached", "provider", "basis",
    "signature", "generated_at", "reason"}``:

    * ``items`` —— ``[{**place_dict, "rank", "reason"}]``(顺序 = 推荐顺序,已映射回候选);
    * ``candidates`` —— 本次参与推荐的候选条数(超过 :data:`CANDIDATE_LIMIT` 会被截断);
    * ``degraded=True`` 且 ``basis="distance"`` 表示**没有 AI 参与**(没 key / 调用失败 /
      输出不可解析),此时 ``reason`` 说明具体原因,前端标注"按距离推荐"而不是"AI 推荐"。
    """
    wanted = clamp_count(count)
    candidates = list(places or [])[:CANDIDATE_LIMIT]
    signature = candidate_signature(candidates)
    if not candidates:
        return {
            "items": [], "candidates": 0, "degraded": True, "cached": False,
            "provider": None, "basis": BASIS_DISTANCE, "signature": signature,
            "generated_at": None, "reason": "no_candidates",
        }

    by_id = {int(item["id"]): dict(item) for item in candidates if item.get("id") is not None}
    cached_row = None if refresh else repo.get_recommendation(
        session, origin_city=origin_city, band=band, category=category, signature=signature
    )
    if cached_row is not None and cached_row.degraded and not degraded_cache_fresh(cached_row):
        cached_row = None  # 降级缓存过期 → 自动重算(见 DEGRADED_RETRY_S)
    if cached_row is not None:
        return _payload(cached_row.items, by_id, cached_row, cached=True)

    llm = client if client is not None else default_llm_client()
    provider = llm.label if llm.enabled else None
    degraded = False
    reason = "ok"
    if not llm.enabled:
        items = fallback_items(candidates, count=wanted)
        degraded, reason = True, "no_key"
        basis = BASIS_DISTANCE
    else:
        try:
            raw = llm.chat(
                build_prompt(candidates, count=wanted, origin_name=origin_name,
                             band_label=band_label, category=category),
                system=SYSTEM_PROMPT,
                max_tokens=RECO_MAX_TOKENS,
                timeout=RECO_TIMEOUT_S,
            )
            items = parse_recommendations(raw, candidates, count=wanted)
        except DataSourceError as exc:
            items, reason = [], f"llm_error:{type(exc).__name__}"
        except Exception as exc:  # noqa: BLE001 - 推荐是增强项,任何异常都不得阻塞出图
            items, reason = [], f"llm_error:{type(exc).__name__}"
        if not items:
            items = fallback_items(candidates, count=wanted)
            degraded = True
            if reason == "ok":
                reason = "unparsable"
        basis = BASIS_LLM

    row, _created = repo.upsert_recommendation(
        session,
        origin_city=origin_city,
        band=band,
        category=category,
        signature=signature,
        items=items,
        provider=provider,
        basis=basis,
        degraded=degraded,
    )
    if commit:
        session.commit()
    payload = _payload(row.items, by_id, row, cached=False)
    payload["reason"] = reason
    return payload


def _payload(
    items: Sequence[Mapping[str, Any]],
    by_id: Mapping[int, dict[str, Any]],
    row: Any,
    *,
    cached: bool,
) -> dict[str, Any]:
    """把存库的 ``items`` 映射回完整目的地 dict(已删除/不在候选里的条目自动跳过)。"""
    mapped: list[dict[str, Any]] = []
    for item in items or []:
        target = by_id.get(int(item.get("place_id") or -1))
        if not target:
            continue
        merged = dict(target)
        merged["rank"] = int(item.get("rank") or len(mapped) + 1)
        merged["reason"] = str(item.get("reason") or "")
        mapped.append(merged)
    return {
        "items": mapped,
        "candidates": len(by_id),
        "degraded": bool(row.degraded),
        "cached": bool(cached),
        "provider": row.provider or None,
        "basis": row.basis or BASIS_DISTANCE,
        "signature": row.signature,
        "generated_at": _iso(row.updated_at or row.created_at),
        "reason": "degraded" if row.degraded else "ok",
    }


def _iso(value: Any) -> Optional[str]:
    """时间戳 → ISO8601 字符串(与 API 层其它路由同口径)。"""
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)
