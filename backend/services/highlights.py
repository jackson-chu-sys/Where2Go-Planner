"""类别专属要点(神朱 2026-10-01 拍板,TASK-8a2):详情弹窗第 4 块的**结构化字段**。

与 :mod:`services.details` 的分工:

* ``PlaceDetail.text`` —— 地图下方列表里的 **2~3 句自由文本**长介绍(已有,不动);
* 本模块写 ``PlaceHighlight.fields`` —— 弹窗里按**分类固定字段表**出的要点
  (``[{"label", "value"}]``),同一份数据两处用途不同,所以分表存(契约 §5:只新增表)。

字段表(:data:`CATEGORY_FIELDS`,契约逐字)按分类走:自然→最佳季节/门票/开放信息/游玩建议;
人文美食→人文背景/必吃/代表小店;滑雪→雪道数与分级/开放期/适合人群;
运动→项目/场地装备/适宜人群;其余→亮点/建议。库内 ``Place.category`` 存的是**全称**
(``自然风光`` / ``小城人文美食`` / ``滑雪场`` / ``运动``),所以选表用**包含匹配**
(:func:`category_key`,优先级照 :data:`services.classify.CATEGORY_PRIORITY`:
滑雪 > 运动 > 人文美食 > 自然),不硬编码全等 —— 存量/种子数据的分类名有出入也不会掉进「其他」。

两条硬口径(踩过坑,别改):

1. **输出预算必须按调用放大**(:data:`HIGHLIGHT_MAX_TOKENS` / :data:`HIGHLIGHT_TIMEOUT_S`)。
   ``intro.LLMClient`` 的默认 120 tokens / 20s 是给"一句话简介"的,用它出多字段 JSON 会被
   **静默截断** → JSON 不完整 → 解析失败 → 空要点,现象和"模型不听话"一模一样、极难排查。
2. **查不到就 ``value=null``,并在 ``note`` 里标「待核实」**;严禁编造票价、雪道条数与分级、
   店名、营业时间。宁可弹窗上少一条,也不要给出一条看着很像真的的假数据。

降级口径(全部**不抛**):LLM 异常 / 坏 JSON → ``fields=[]`` + ``note="解析失败"``;
没配 key → ``fields=[]`` + ``note`` 说明未配置。失败**不写缓存行**(下次可重试),
成功的结果按 POI **永久缓存**在 :class:`db.models.PlaceHighlight`(命中即回、零 LLM)。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from data_sources import DataSourceError
from db import models
from db import repository as repo
from services.intro import (
    LLMClient,
    default_client as default_llm_client,
    resolve_provider,
    tag_facts,
)

# --------------------------------------------------------------------------- #
# 预算与字段表
# --------------------------------------------------------------------------- #

# 输出预算与超时:**按调用放大**传给 llm.chat(),绝不沿用 LLMClient 的 120/20s 默认
# (多字段 JSON 比一句话简介长得多,默认值会被静默截断 → 解析失败 → 空要点)。
HIGHLIGHT_MAX_TOKENS = 600
HIGHLIGHT_TIMEOUT_S = 60.0

FACT_TAGS = 8
FALLBACK_CATEGORY = "其他"

#: 分类 → **固定字段表**(契约 §3 TASK-8a2 逐字;键是短名,匹配见 :func:`category_key`)
CATEGORY_FIELDS: dict[str, tuple[str, ...]] = {
    "自然": ("最佳季节", "门票/开放信息", "游玩建议"),
    "人文美食": ("人文背景", "必吃", "代表小店"),
    "滑雪": ("雪道数与分级", "开放期", "适合人群"),
    "运动": ("项目", "场地/装备", "适宜人群"),
    FALLBACK_CATEGORY: ("亮点", "建议"),
}

#: 包含匹配的关键词表,**顺序即优先级**(滑雪 > 运动 > 人文美食 > 自然,同归类优先级):
#: 库内分类是全称(``滑雪场`` / ``小城人文美食`` / ``自然风光``),用 ``关键词 in category``
#: 判定,别硬编码全等 —— 种子数据写 ``滑雪`` / ``自然`` 这种短名时也要落到同一张字段表。
CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("滑雪", ("滑雪", "雪场", "雪道", "ski")),
    ("运动", ("运动", "sport")),
    ("人文美食", ("人文", "美食", "culture", "food")),
    ("自然", ("自然", "风光", "nature", "natural")),
)

#: 模型把"查不到"写成这些词时,一律归一成 ``None``(前端按「待核实」弱化,不显示占位话术)
NULL_LITERALS: frozenset[str] = frozenset({
    "", "-", "null", "none", "n/a", "na", "unknown",
    "未知", "不详", "暂无", "查不到", "待核实", "不确定", "无法确定",
})

SOURCE_NAME = "Highlight"
SYSTEM_PROMPT = (
    "你是 Where2Go(周末去哪儿玩)的目的地编辑,为目的地按**给定的字段表**产出结构化要点。"
    "硬规则:只依据给出的名称/分类/距离/标签线索与你确知的公开信息作答;"
    "**某个字段查不到或不确定,就把它的值写成 JSON 的 null**(键仍要保留,不要用「未知」"
    "「暂无」这类占位话术代替 null);**严禁编造门票价格、雪道条数与分级、营业时间、"
    "电话号码、店名或用户评价**。每个字段的值写一句话(20~60 字),讲具体信息而不是空话,"
    "不要提及 OSM、标签、数据源或模型。**只输出一个 JSON 对象**:键逐字等于字段表里的"
    "字段名,值是字符串或 null;不要 Markdown 代码块、不要注释、不要任何解释文字。"
)

# note 口径(前端/晨报都读它判断这条要点能不能直接用)
NOTE_BASE = (
    "要点由 LLM 按分类的固定字段表生成,按 POI 永久缓存在 PlaceHighlight;"
    "value=null 表示该字段未核实,不编造票价/雪道数/店名。"
)
NOTE_PARSE_FAILED = "解析失败"
NOTE_GENERATE_FAILED = "生成失败"
NOTE_NO_KEY = "未配置 LLM key(要点未生成,配好即可重试)"
NOTE_NO_NAME = "缺少目的地名称(要点未生成)"
NOTE_NO_PLACE = "目的地不存在(库内没有这个 place_id)"


def category_key(category: Any) -> str:
    """分类名 → :data:`CATEGORY_FIELDS` 的键(**包含匹配**,按优先级首次命中即定)。

    ``自然风光``→``自然``、``小城人文美食``→``人文美食``、``滑雪场``→``滑雪``、``运动``→``运动``;
    空值 / 认不出的分类一律落 :data:`FALLBACK_CATEGORY`(字段表 = 亮点/建议)。
    """
    text = str(category or "").strip().lower()
    if not text:
        return FALLBACK_CATEGORY
    for key, keywords in CATEGORY_KEYWORDS:
        if any(word.lower() in text for word in keywords):
            return key
    return FALLBACK_CATEGORY


def fields_for_category(category: Any) -> tuple[str, ...]:
    """该分类的**固定字段表**(顺序即前端渲染顺序,也即要求模型输出的键顺序)。"""
    return CATEGORY_FIELDS[category_key(category)]


# --------------------------------------------------------------------------- #
# prompt 与解析
# --------------------------------------------------------------------------- #


def build_highlights_prompt(place: Mapping[str, Any]) -> str:
    """一条目的地 → 结构化要点 prompt(名称 + 分类 + 距离 + 标签线索 + **逐字字段表**)。"""
    category = str(place.get("category") or FALLBACK_CATEGORY)
    labels = fields_for_category(category)
    distance = place.get("distance_km")
    distance_text = "未知" if distance is None else f"{float(distance):g} km"
    tags = tag_facts(
        place.get("tags") if isinstance(place.get("tags"), Mapping) else None, limit=FACT_TAGS
    )
    lines = [
        f"目的地:{place.get('name') or '(无名)'}",
        f"分类:{category}",
        f"距起点直线距离:{distance_text}",
    ]
    if tags:
        lines.append(f"标签线索:{tags}")
    lines.append(f"请按下面的**固定字段表**出要点(键逐字照抄、顺序也照抄):{'、'.join(labels)}")
    lines.append(
        "输出**严格 JSON 对象**(不要代码块、不要解释):"
        + json.dumps({label: "一句话要点或 null" for label in labels}, ensure_ascii=False)
    )
    lines.append("查不到或不确定的字段,值写 null(键保留);不要编造票价/雪道数/店名。")
    return "\n".join(lines)


def _unwrap_payload(payload: Any) -> Any:
    """容忍模型多包一层(``{"fields": {...}}`` / ``{"要点": [...]}``):只有一个键就往下钻。"""
    current = payload
    for _ in range(3):
        if isinstance(current, Mapping) and len(current) == 1:
            key, value = next(iter(current.items()))
            if str(key).strip().lower() in {"fields", "field", "要点", "highlight", "highlights", "data"}:
                current = value
                continue
        break
    return current


def _clean_value(value: Any) -> Optional[str]:
    """字段值归一:空/占位话术 → ``None``(= 待核实);其余压成单行字符串。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = ";".join(str(item).strip() for item in value if str(item).strip())
    elif isinstance(value, Mapping):
        value = json.dumps(value, ensure_ascii=False)
    text = " ".join(str(value).split()).strip().strip("\"'“”‘’「」『』")
    if text.lower() in NULL_LITERALS:  # 中英文占位话术都归一成 null(表里存的是小写)
        return None
    return text or None


def _pick(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    """从模型给的 Mapping 里按同义词取键(容忍 ``label/name/字段``、``value/text/值``)。"""
    for name in names:
        if name in mapping:
            return mapping[name]
    lowered = {str(key).strip().lower(): item for key, item in mapping.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


LABEL_KEYS = ("label", "name", "field", "字段", "要点")
VALUE_KEYS = ("value", "text", "content", "值", "内容")


def parse_highlights(raw: Any, labels: Sequence[str]) -> tuple[list[dict[str, Any]], bool]:
    """模型输出 → ``([{"label", "value"}], ok)``。

    ``ok=False`` = JSON 解析不出来(调用方降级成 ``fields=[]`` + ``note="解析失败"``)。
    解析出来以后**一律按字段表重排**:模型多给的键丢掉、少给的键补 ``value=None``
    (→ note 标「待核实」),这样响应形状永远等于该分类的固定字段表,前端不用兜键。
    """
    text = str(raw or "").strip()
    if not text:
        return [], False
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    # 模型爱在 JSON 前后加一句"好的,以下是..."→ 只截最外层括号之间的部分再解析
    openers = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if openers:
        start = min(openers)
        end = max(text.rfind("}"), text.rfind("]"))
        if end > start:
            text = text[start:end + 1]
    try:
        payload = _unwrap_payload(json.loads(text))
    except (ValueError, TypeError):
        return [], False

    values: dict[str, Any] = {}
    if isinstance(payload, Mapping):
        values = {str(key).strip(): item for key, item in payload.items()}
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, Mapping):
                label = _pick(item, LABEL_KEYS)
                if label is not None:
                    values[str(label).strip()] = _pick(item, VALUE_KEYS)
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                values[str(item[0]).strip()] = item[1]
    else:
        return [], False
    if not values:
        return [], False

    lowered = {key.lower(): value for key, value in values.items()}
    fields: list[dict[str, Any]] = []
    for label in labels:
        key = str(label).strip()
        found = values.get(key, lowered.get(key.lower()))
        fields.append({"label": key, "value": _clean_value(found)})
    return fields, True


def note_for_fields(fields: Sequence[Mapping[str, Any]]) -> str:
    """有 ``value=None`` 的字段 → note 里**标「待核实」**(前端据此弱化显示)。"""
    pending = sum(1 for item in fields if item.get("value") is None)
    if not pending:
        return NOTE_BASE
    return f"{NOTE_BASE} 待核实:{pending}/{len(fields)} 个字段查不到(value=null)。"


# --------------------------------------------------------------------------- #
# 生成与缓存
# --------------------------------------------------------------------------- #


def place_id_of(place: Any) -> Optional[int]:
    """``Place`` ORM 行 / 含 id 的 dict → 正整数 id;拿不到返回 ``None``。"""
    raw: Any = None
    if isinstance(place, Mapping):
        raw = place.get("id", place.get("place_id"))
    else:
        raw = getattr(place, "id", None)
    try:
        resolved = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return resolved if resolved > 0 else None


def _place_payload(place: Any) -> dict[str, Any]:
    """ORM 行 / dict 统一成 prompt 用的 Mapping(标签只在 ORM 行上需要转 dict)。"""
    if isinstance(place, Mapping):
        return dict(place)
    tags = getattr(place, "tags", None)
    return {
        "id": getattr(place, "id", None),
        "name": getattr(place, "name", ""),
        "category": getattr(place, "category", "") or FALLBACK_CATEGORY,
        "distance_km": getattr(place, "distance_km", None),
        "tags": dict(tags or {}),
    }


def resolve_client(
    client: Optional[LLMClient] = None, environ: Optional[Mapping[str, str]] = None
) -> LLMClient:
    """LLM 客户端的**唯一入口**(:mod:`services.intro` 的 resolve_provider/LLMClient)。

    给了 ``client`` 就用它(单测注入替身);给了 ``environ`` 就按它解析 provider
    (脚本/单测可指定无 key 的环境);都没有就用进程内共享的默认客户端。
    """
    if client is not None:
        return client
    if environ is None:
        return default_llm_client()
    return LLMClient(resolve_provider(environ), environ=environ)


def empty_item(
    place_id: Any, *, note: str, category: Any = "", cached: bool = False
) -> dict[str, Any]:
    """降级形状(``fields=[]``):键与成功时**逐字一致**,前端只读固定键不用兜。"""
    return {
        "place_id": int(place_id) if str(place_id).strip().lstrip("-").isdigit() else place_id,
        "category": str(category or ""),
        "fields": [],
        "note": note,
        "cached": cached,
        "generated_at": None,
    }


def fetch_highlights(
    place: Any,
    *,
    client: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
    session: Optional[Session] = None,
) -> dict[str, Any]:
    """一个 POI 的类别专属要点:**永久缓存优先 → LLM 生成**,返回固定形状的 dict。

    → ``{"place_id", "category", "fields": [{"label", "value"}], "note", "cached", "generated_at"}``
    (``cached=True`` 表示这次**零 LLM** 读了库)。

    ``session`` 给了才读写缓存(:func:`db.repository.get_place_highlight` /
    :func:`db.repository.upsert_place_highlight`),并且只 ``flush``、**提交时机交给调用方**
    (与 :func:`services.place_media.fetch_media_for_place` 一致);不给就只生成不落库
    (脚本/单测用,免得偷偷写进 ``backend/data/where2go.db``)。
    **任何失败都不抛**:异常/坏 JSON → ``fields=[]`` + ``note="解析失败"``,且**不写缓存行**
    (失败结果永久钉死就没法重试了,同 :mod:`services.details` 的降级口径)。
    """
    payload = _place_payload(place)
    place_id = place_id_of(payload)
    category = str(payload.get("category") or "").strip() or FALLBACK_CATEGORY
    if place_id is None:
        raise ValueError(f"place 必须带正整数 id(Place 行或含 id 的 dict),收到:{place!r}")

    if session is not None:
        row = repo.get_place_highlight(session, place_id=place_id)
        if row is not None:
            return {**repo.highlight_to_dict(row), "cached": True}

    labels = fields_for_category(category)
    llm = resolve_client(client, environ)
    if not llm.enabled:
        return empty_item(place_id, note=NOTE_NO_KEY, category=category)
    if not str(payload.get("name") or "").strip():
        return empty_item(place_id, note=NOTE_NO_NAME, category=category)

    try:
        raw = llm.chat(
            build_highlights_prompt(payload),
            system=SYSTEM_PROMPT,
            max_tokens=HIGHLIGHT_MAX_TOKENS,
            timeout=HIGHLIGHT_TIMEOUT_S,
        )
    except DataSourceError:
        return empty_item(place_id, note=NOTE_PARSE_FAILED, category=category)
    except Exception:  # noqa: BLE001 - 要点是增强项,任何异常都不得冒到 API 变 500
        return empty_item(place_id, note=NOTE_PARSE_FAILED, category=category)

    fields, ok = parse_highlights(raw, labels)
    if not ok or not fields:
        return empty_item(place_id, note=NOTE_PARSE_FAILED, category=category)

    note = note_for_fields(fields)
    if session is None:
        return {
            "place_id": place_id,
            "category": category,
            "fields": fields,
            "note": note,
            "cached": False,
            "generated_at": None,
        }
    row = repo.upsert_place_highlight(
        session, place_id=place_id, fields=fields, category=category, note=note
    )
    return {**repo.highlight_to_dict(row), "cached": False}


def fetch_highlights_for_places(
    session: Session,
    place_ids: Iterable[Any],
    *,
    client: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> list[dict[str, Any]]:
    """批量取要点(**按入参顺序**返回,重复 id 只回一条;读库优先,仅 miss 才调 LLM)。

    单条失败**不扩散**:该条 ``fields=[]`` + note,其余照常。每条成功就 ``commit`` 一次 ——
    第 N 条炸了要 ``rollback`` 才能继续用这个会话,不先提交会把前面已经花过 LLM 配额
    生成的要点一起丢掉(与 :func:`services.place_media.fetch_media_for_places` 同一口径)。
    """
    env = dict(os.environ if environ is None else environ)
    wanted: list[int] = []
    for raw in place_ids or []:
        try:
            resolved = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if resolved > 0 and resolved not in wanted:
            wanted.append(resolved)
    if not wanted:
        return []

    cached_rows = repo.highlight_map(session, wanted)
    missing = [place_id for place_id in wanted if place_id not in cached_rows]
    places: dict[int, models.Place] = {}
    if missing:
        rows = session.scalars(select(models.Place).where(models.Place.id.in_(missing)))
        places = {int(row.id): row for row in rows}

    items: list[dict[str, Any]] = []
    for place_id in wanted:
        row = cached_rows.get(place_id)
        if row is not None:
            items.append({**repo.highlight_to_dict(row), "cached": True})
            continue
        place = places.get(place_id)
        if place is None:
            items.append(empty_item(place_id, note=NOTE_NO_PLACE))
            continue
        try:
            items.append(
                fetch_highlights(place, client=client, environ=env, session=session)
            )
            session.commit()
        except Exception:  # noqa: BLE001 - 单条失败只影响这一条
            session.rollback()
            items.append(empty_item(place_id, note=NOTE_GENERATE_FAILED))
    return items
