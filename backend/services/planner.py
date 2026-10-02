"""AI 对话式行程规划(TASK-10a,**阶段 A**):多轮编排 + 会话持久化 + 结构化行程解析。

拍板口径(docs/TASK-10-CONTRACT.md §0,勿自行改):

1. **多轮可迭代** —— 每轮把最近 :data:`HISTORY_LIMIT` 条历史送进 prompt,所以用户能继续说
   「第二天换成古镇」「预算压到 1500」再改;历史就是 :class:`~db.models.PlannerMessage` 行,
   没有别的状态机。
2. **收藏不自动带** —— 只有用户在对话里**点名/引用**的收藏(``collection_briefs``)才进
   prompt,而且 prompt 里写死「未点名一律不得纳入」;收藏清单只是"可选素材"。
3. **阶段 A 只排「每天的目的地」** —— 不出交通方式报价、不出住宿名称与价格、不出总预算
   (那些是阶段 B,见契约 §6,本次不实现)。``nights`` 因此**不进 prompt**,只随 user 消息的
   ``payload`` 存起来,给阶段 B / 「存为行程方案」复用。
4. **展示不自动落库** —— 本模块只写 ``PlannerSession`` / ``PlannerMessage`` 两张对话表,
   绝不碰 :class:`~db.models.TripPlan`(存为方案是 TASK-10b 的 ``/api/planner/save``)。

LLM 口径(与 :mod:`services.intro` / :mod:`services.highlights` 同一套):

* 一律走 :class:`services.intro.LLMClient`,**别新写 HTTP 客户端**;``client`` / ``environ``
  两个注入口给单测与脚本(见 :func:`resolve_client`)。
* **输出预算必须按调用放大**(:data:`PLANNER_MAX_TOKENS` / :data:`PLANNER_TIMEOUT_S`):
  沿用 ``LLMClient`` 的 120 tokens / 20s 默认值会把多日行程 JSON 静默截断 → 解析失败
  → 用户只看到"AI 没反应"(2026-09-27 的项目 LLM 预算坑,别再踩)。
* **降级一律不抛**(:func:`plan_turn` 的四态 ``no_key`` / ``timeout`` / ``error`` /
  ``parse_error``),``reply`` 给中文兜底文案(含原因 + 可重试提示),前端**绝不静默**。
* 解析容错在 :func:`parse_itinerary`(纯函数):剥 ```` ```json ```` 围栏 → 取首 ``{`` 到末
  ``}`` → 校验 ``days`` 是非空数组且每天有 ``day`` + ``stops``;失败返回 ``(None, 原文前 200 字)``。

写库口径:``plan_turn`` / :func:`clear_session` 自己 ``commit``(一轮对话动辄几十秒,
LLM 返回后必须立刻落盘,不能等调用方记得提交);:func:`list_messages` 只读。
本模块**不触网**(除了 ``LLMClient`` 那一次 chat),只读写 SQLite。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from data_sources import DataSourceError, TransientDataSourceError
from db.models import (
    PLANNER_ROLES,
    PLANNER_TITLE_LEN,
    SESSION_KEY_LEN,
    PlannerMessage,
    PlannerSession,
    clean_text,
    iso_utc,
    utcnow,
)
from services.intro import (
    LLMClient,
    default_client as default_llm_client,
    resolve_provider,
)

# --------------------------------------------------------------------------- #
# 预算与硬上限(契约 §3 TASK-10a 逐字)
# --------------------------------------------------------------------------- #

PLANNER_MAX_TOKENS = 1200      # 必须按调用放大(勿用 intro 默认 120/20s;见项目 LLM 预算坑)
PLANNER_TIMEOUT_S = 180.0
HISTORY_LIMIT = 12             # 送进 prompt 的历史消息上限(超出只留最近 12 条)
MAX_MESSAGE_LEN = 2000
DEFAULT_DAYS = 3
MAX_DAYS = 15
STOPS_PER_DAY_MIN = 2
STOPS_PER_DAY_MAX = 4

# 素材清单里每条收藏的摘要截断长度(契约:collection_briefs 的 summary 截 120 字)
BRIEF_SUMMARY_LEN = 120
# 单条历史进 prompt 的截断长度:助手轮的行程摘要可能很长,全塞进去会把预算吃光
HISTORY_ITEM_CHARS = 600
# 解析失败时回给调用方的原文片段长度(契约:原文前 200 字)
PARSE_SNIPPET_LEN = 200
# 会话标题取首轮用户消息的前若干字(列宽 PLANNER_TITLE_LEN)
TITLE_CHARS = 40
# list_messages 的条数上限(与 TASK-10b 的 API 口径一致)
LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 200

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

REASON_NO_KEY = "no_key"
REASON_TIMEOUT = "timeout"
REASON_ERROR = "error"
REASON_PARSE_ERROR = "parse_error"
DEGRADE_REASONS: tuple[str, ...] = (
    REASON_NO_KEY,
    REASON_TIMEOUT,
    REASON_ERROR,
    REASON_PARSE_ERROR,
)

CODE_FENCE = "```"
FENCE_LANGS = ("", "json", "javascript", "js")

SYSTEM_PROMPT = (
    "你是 Where2Go(周末去哪儿玩)的自由行行程规划师,按用户的多轮对话排出「每天的目的地行程」。"
    "硬约束(逐条遵守,违反=返工):"
    f"1)只把用户点名/引用的收藏排进行程,未点名一律不得纳入 —— 收藏清单只是「可选素材」;"
    "用户完全没点名时,按用户描述的需求自由选点;"
    f"2)本阶段只排「每天的目的地」:不输出交通方式与交通报价、不输出住宿名称与价格、"
    "不输出总预算(这些等用户确认行程后再细化),也不要在 tip / summary 里塞票价;"
    "3)拿不到的事实(门票、开放时间、雪道数、营业状态等)一律写「待核实」,禁止编造;"
    f"4)每天 {STOPS_PER_DAY_MIN}~{STOPS_PER_DAY_MAX} 个目的地,按地理顺路排序"
    f"(同一天内不要横跨城市两端来回跑);天数由用户说的天数决定,没说默认 {DEFAULT_DAYS} 天,"
    f"上限 {MAX_DAYS} 天;"
    "5)每个 stop 给一句具体理由(点名收藏的必须用该收藏自己的信息写,不许套话);"
    "6)只输出一个 JSON 对象,不要 markdown 围栏、不要解释文字。"
)

# 输出形状逐字写进 prompt:前端只读这些键,少一个就多一处崩
OUTPUT_SHAPE = (
    '{"days":[{"day":1,"base":"当天所在城市/区域","stops":[{"name":"地点名",'
    '"reason":"一句具体理由","collection_id":3}],"tip":"当天一句提示"}],'
    '"days_count":1,"summary":"一句话总体思路","unused_collections":["点了名但没排进去的收藏名称"]}'
)

# 降级兜底文案(中文,含原因码 + 可重试提示):四态一律走这里,前端直接显示
DEGRADED_REPLIES: dict[str, str] = {
    REASON_NO_KEY: (
        "AI 行程规划暂时不可用(原因:no_key)—— 服务端没有配置可用的 LLM key。"
        "请先配置 WHERE2GO_LLM_API_KEY(或 DEEPSEEK_API_KEY / ALIBABA_TOKEN_PLAN_API_KEY),"
        "然后重发这一轮即可,对话历史不会丢。"
    ),
    REASON_TIMEOUT: (
        "AI 行程规划超时了(原因:timeout)—— 模型没在 "
        f"{int(PLANNER_TIMEOUT_S)} 秒内返回完整行程。可以稍后重试一轮,"
        "或把天数减少、点名的收藏少几条再问,通常就能出结果。"
    ),
    REASON_ERROR: (
        "AI 行程规划调用失败(原因:error)—— 模型服务报错或网络异常。"
        "请稍后重试;若连续失败,请看后端日志里的 LLM 报错原文。"
    ),
    REASON_PARSE_ERROR: (
        "AI 这一轮返回的内容不是合法的行程 JSON(原因:parse_error),已按降级处理、没有编造行程。"
        "回复「重试」或换一种说法(例如明确说「3 天,每天 2~3 个点」)再问一次。"
    ),
}


# --------------------------------------------------------------------------- #
# LLM 客户端与降级判定
# --------------------------------------------------------------------------- #


def resolve_client(
    client: Optional[LLMClient] = None, environ: Optional[Mapping[str, str]] = None
) -> LLMClient:
    """LLM 客户端的**唯一入口**(:mod:`services.intro` 的 resolve_provider / LLMClient)。

    给了 ``client`` 就用它(单测注入替身);给了 ``environ`` 就按它解析 provider
    (脚本/单测可指定"无 key 的环境"验降级);都没有就用进程内共享的默认客户端。
    与 :func:`services.highlights.resolve_client` 同一口径,别在这里另写一套。
    """
    if client is not None:
        return client
    if environ is None:
        return default_llm_client()
    return LLMClient(resolve_provider(environ), environ=environ)


def _is_timeout(exc: BaseException) -> bool:
    """异常是不是"超时":认 :class:`TransientDataSourceError` 的中文超时文案,也认
    ``requests`` 的 ``Timeout``/``ReadTimeout``(类名里带 Timeout)—— 单测替身可能直接抛后者。"""
    message = str(getattr(exc, "message", "") or exc)
    if isinstance(exc, TransientDataSourceError) and "超时" in message:
        return True
    haystack = f"{type(exc).__name__} {message}".lower()
    return "timeout" in haystack or "timed out" in haystack or "超时" in message


def degraded_reply(reason: str, *, detail: Any = None) -> str:
    """降级文案:固定中文兜底 + 原因码,必要时把细节(异常/原文片段)附在后面。"""
    text = DEGRADED_REPLIES.get(reason, DEGRADED_REPLIES[REASON_ERROR])
    snippet = _snippet(detail)
    if snippet:
        text = f"{text}\n原始片段:{snippet}"
    return text


# --------------------------------------------------------------------------- #
# 归一工具
# --------------------------------------------------------------------------- #


def _snippet(text: Any) -> str:
    """原文片段:去空白后截 :data:`PARSE_SNIPPET_LEN` 字(解析失败时给调用方看的东西)。"""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    return text.strip()[:PARSE_SNIPPET_LEN]


def _as_text(value: Any) -> str:
    """宽容转文本:非字符串 → ``""``(bool 也算非字符串,免得 ``True`` 变成地点名)。"""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _as_int(value: Any, fallback: int) -> int:
    """宽容转 int:``"2\"`` / ``2.0`` 都认;转不出来用 ``fallback``(天号按顺序补)。"""
    if isinstance(value, bool):
        return fallback
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def resolve_session_key(value: Any) -> str:
    """会话 key 归一:非空就用它(按列宽截断),空/None → 后端生成 ``uuid4().hex``。

    前端把 key 存 ``localStorage`` 实现"刷新不丢对话";第一次对话还没有 key,由这里补发。
    """
    return clean_text(value, limit=SESSION_KEY_LEN) or uuid.uuid4().hex


def normalize_briefs(collection_briefs: Optional[Iterable[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """收藏素材清单归一:元素只留 ``{"id","kind","title","summary"}``,``summary`` 截 120 字。

    契约口径:素材由 API 层(TASK-10b)从库内取好再传进来,**planner 只消费不查库**;
    没有名字也没有摘要的元素直接丢掉(进 prompt 只会浪费预算)。
    """
    briefs: list[dict[str, Any]] = []
    for raw in collection_briefs or ():
        if not isinstance(raw, Mapping):
            continue
        title = _as_text(raw.get("title"))
        summary = _as_text(raw.get("summary"))
        if not title and not summary:
            continue
        raw_id = raw.get("id")
        briefs.append(
            {
                "id": None if raw_id is None else _as_int(raw_id, 0) or None,
                "kind": _as_text(raw.get("kind")),
                "title": title,
                "summary": summary[:BRIEF_SUMMARY_LEN],
            }
        )
    return briefs


def _optional_nights(value: Any) -> Optional[int]:
    """晚数:阶段 A **不参与规划**,只随消息存起来给阶段 B / 存为方案用;非法值 → ``None``。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        nights = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return nights if nights > 0 else None


# --------------------------------------------------------------------------- #
# 行程解析(纯函数)
# --------------------------------------------------------------------------- #


def strip_code_fence(text: str) -> str:
    """剥 ```` ```json ```` / ```` ``` ```` 围栏(硬约束 6 说了不要围栏,但模型常常还是加)。"""
    body = text.strip()
    if not body.startswith(CODE_FENCE):
        return body
    body = body[len(CODE_FENCE) :]
    head, separator, rest = body.partition("\n")
    if separator and head.strip().lower() in FENCE_LANGS:
        body = rest
    body = body.rstrip()
    if body.endswith(CODE_FENCE):
        body = body[: -len(CODE_FENCE)]
    return body.strip()


def _normalize_stop(raw: Any) -> Optional[dict[str, Any]]:
    """一个停靠点 → ``{"name","reason","collection_id"}``;没有名字 → ``None``(丢掉)。

    宽容口径:模型偶尔把 stop 写成裸字符串(``"西湖"``),按"只有名字"收下,
    省得整份行程因为一个字符串就判解析失败。
    """
    if isinstance(raw, str):
        name = raw.strip()
        return {"name": name, "reason": "", "collection_id": None} if name else None
    if not isinstance(raw, Mapping):
        return None
    name = _as_text(raw.get("name"))
    if not name:
        return None
    raw_id = raw.get("collection_id")
    collection_id = None if raw_id in (None, "") else (_as_int(raw_id, 0) or None)
    return {"name": name, "reason": _as_text(raw.get("reason")), "collection_id": collection_id}


def _normalize_day(raw: Any, fallback_index: int) -> Optional[dict[str, Any]]:
    """一天 → ``{"day","base","stops","tip"}``;缺 ``day`` / ``stops`` 或没有有效 stop → ``None``。"""
    if not isinstance(raw, Mapping):
        return None
    if "day" not in raw or "stops" not in raw:
        return None
    stops_raw = raw.get("stops")
    if not isinstance(stops_raw, list):
        return None
    stops = [stop for stop in (_normalize_stop(item) for item in stops_raw) if stop is not None]
    if not stops:
        return None
    return {
        "day": _as_int(raw.get("day"), fallback_index),
        "base": _as_text(raw.get("base")),
        "stops": stops,
        "tip": _as_text(raw.get("tip")),
    }


def parse_itinerary(text: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """把模型返回的文本解析成**结构化行程**;失败返回 ``(None, 原文前 200 字)``,**绝不抛**。

    容错三步(契约 §3):剥 ```` ```json ```` 围栏 → 取第一个 ``{`` 到最后一个 ``}``
    (模型爱在 JSON 前后加解释文字)→ 校验 ``days`` 是**非空数组**且每天有 ``day`` + ``stops``。
    ``days_count`` 一律按归一后的天数**重算**(不信模型自报的数字),``unused_collections``
    归一成字符串数组(模型没给就是空数组)。
    """
    if not isinstance(text, str) or not text.strip():
        return None, _snippet(text)
    candidate = strip_code_fence(text)
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        return None, _snippet(text)
    try:
        data = json.loads(candidate[start : end + 1])
    except (TypeError, ValueError):
        return None, _snippet(text)
    if not isinstance(data, Mapping):
        return None, _snippet(text)

    raw_days = data.get("days")
    if not isinstance(raw_days, list) or not raw_days:
        return None, _snippet(text)
    days: list[dict[str, Any]] = []
    for index, item in enumerate(raw_days, start=1):
        day = _normalize_day(item, index)
        if day is None:
            return None, _snippet(text)
        days.append(day)

    unused_raw = data.get("unused_collections")
    unused = (
        [name for name in (_as_text(item) for item in unused_raw) if name]
        if isinstance(unused_raw, list)
        else []
    )
    return (
        {
            "days": days,
            "days_count": len(days),
            "summary": _as_text(data.get("summary")),
            "unused_collections": unused,
        },
        None,
    )


def build_reply(itinerary: Mapping[str, Any]) -> str:
    """成功轮的中文 ``reply``:总体思路 + 每天去了哪(顺序即顺路顺序)。

    助手消息的 ``content`` 就是它,所以**历史里带着每天的地点名** —— 下一轮用户说
    「第二天换成古镇」时,prompt 里的历史足够让模型改对地方(不必回放整份 JSON)。
    """
    days = list(itinerary.get("days") or [])
    lines = []
    for day in days:
        names = " → ".join(str(stop.get("name") or "") for stop in (day.get("stops") or []))
        base = str(day.get("base") or "").strip()
        head = f"第{day.get('day')}天" + (f"({base})" if base else "")
        lines.append(f"{head}:{names}")
    summary = str(itinerary.get("summary") or "").strip()
    title = f"已排出 {len(days)} 天行程" + (f" · {summary}" if summary else "")
    return (
        "\n".join([title, *lines])
        + "\n(本阶段只排「每天的目的地」:交通、住宿与总预算等你确认行程后再细化)"
    )


# --------------------------------------------------------------------------- #
# prompt 组装
# --------------------------------------------------------------------------- #


def _history_block(history: Sequence[Mapping[str, Any]]) -> str:
    """历史块:只留最近 :data:`HISTORY_LIMIT` 条,单条再按 :data:`HISTORY_ITEM_CHARS` 截。"""
    if not history:
        return "(这是本会话的第一轮,没有历史)"
    lines = []
    for item in history:
        who = "用户" if str(item.get("role")) == ROLE_USER else "助手"
        content = str(item.get("content") or "").strip()[:HISTORY_ITEM_CHARS]
        lines.append(f"{who}:{content}")
    return "\n".join(lines)


def _briefs_block(briefs: Sequence[Mapping[str, Any]]) -> str:
    """素材块:**未点名不得纳入**这句硬约束在这里再写一遍(贴着清单,模型最不容易漏)。"""
    if not briefs:
        return "(本轮没有收藏素材:按用户描述的需求自由选点,不要假造用户收藏过什么)"
    lines = []
    for brief in briefs:
        label = f"#{brief.get('id')}" if brief.get("id") is not None else "#"
        kind = f" · {brief['kind']}" if brief.get("kind") else ""
        summary = f":{brief['summary']}" if brief.get("summary") else ""
        lines.append(f"- {label}{kind}「{brief.get('title') or ''}」{summary}")
    return "\n".join(lines)


def build_prompt(
    message: str,
    *,
    history: Sequence[Mapping[str, Any]] = (),
    collection_briefs: Sequence[Mapping[str, Any]] = (),
) -> str:
    """一轮的用户 prompt:历史 → 本轮消息 → 收藏素材 → 输出形状与硬约束复述。

    系统约束在 :data:`SYSTEM_PROMPT` 里(走 ``chat(system=…)``);这里把最关键的两条
    (「未点名一律不得纳入」「不排交通/住宿/预算」)连同输出形状再复述一次 —— 长 prompt 里
    系统指令容易被稀释,复述一遍实测更稳。
    """
    return "\n\n".join(
        (
            f"【对话历史(最近 {len(history)} 条)】\n{_history_block(history)}",
            f"【本轮用户消息】\n{str(message).strip()}",
            "【收藏素材清单(只有用户点名/引用的才排进行程,未点名一律不得纳入)】\n"
            f"{_briefs_block(collection_briefs)}",
            f"【天数口径】用户说了天数就按用户的(上限 {MAX_DAYS} 天);"
            f"没说就按 {DEFAULT_DAYS} 天;每天 {STOPS_PER_DAY_MIN}~{STOPS_PER_DAY_MAX} 个目的地,顺路排序。",
            "【输出要求】只输出一个 JSON 对象,不要 markdown 围栏、不要解释文字,形状逐字如下:\n"
            f"{OUTPUT_SHAPE}\n"
            "stops[].collection_id 只有当这个 stop 就是上面素材清单里的某条收藏时才填它的 id,"
            "否则填 null;点了名但没排进去的收藏写进 unused_collections。\n"
            "再强调两条硬约束:①未点名一律不得纳入;②本阶段只排「每天的目的地」,"
            "不排交通、不排住宿、不出预算报价;拿不到的事实写「待核实」,禁止编造。",
        )
    )


# --------------------------------------------------------------------------- #
# 会话与消息读写
# --------------------------------------------------------------------------- #


def get_session(session: Session, *, session_key: Any) -> Optional[PlannerSession]:
    """按 ``session_key`` 取会话行;不存在 → ``None``(未知会话不报错,回放给空历史)。"""
    key = clean_text(session_key, limit=SESSION_KEY_LEN)
    if not key:
        return None
    return session.scalar(select(PlannerSession).where(PlannerSession.session_key == key))


def get_or_create_session(
    session: Session, *, session_key: Any, title: Any = ""
) -> tuple[PlannerSession, bool]:
    """取/建会话行,返回 ``(行, 是否新建)``;**同 key 复用同一行**(只顶 ``updated_at``)。

    ``title`` 只在新建、或存量行标题为空时写入(取首轮用户消息前 :data:`TITLE_CHARS` 字),
    之后不被后续轮次覆盖 —— 标题就是"这轮对话最初想干嘛"。只 ``flush`` 不 ``commit``。
    """
    key = resolve_session_key(session_key)
    row = session.scalar(select(PlannerSession).where(PlannerSession.session_key == key))
    resolved_title = _as_text(title)[:TITLE_CHARS]
    if row is None:
        row = PlannerSession(session_key=key, title=resolved_title[:PLANNER_TITLE_LEN])
        session.add(row)
        session.flush()
        return row, True
    if not (row.title or "").strip() and resolved_title:
        row.title = resolved_title[:PLANNER_TITLE_LEN]
        session.flush()
    return row, False


def add_message(
    session: Session,
    row: PlannerSession,
    *,
    role: str,
    content: Any,
    payload: Optional[Mapping[str, Any]] = None,
) -> PlannerMessage:
    """追加一条消息(user / assistant);``payload`` 存结构化行程,可空。只 ``flush`` 不 ``commit``。"""
    if role not in PLANNER_ROLES:
        raise ValueError(f"未知消息角色:{role!r}(可选:{'、'.join(PLANNER_ROLES)})")
    message = PlannerMessage(
        session_id=int(row.id),
        role=role,
        content=_as_text(content),
        payload=None if payload is None else dict(payload),
    )
    session.add(message)
    row.updated_at = utcnow()
    session.flush()
    return message


def recent_history(
    session: Session, row: PlannerSession, *, before_id: Optional[int] = None, limit: int = HISTORY_LIMIT
) -> list[dict[str, Any]]:
    """最近 ``limit`` 条历史(**时间正序**返回,便于直接拼进 prompt)。

    ``before_id`` 用来排除本轮刚存进去的 user 消息(它在 prompt 里单独有「本轮用户消息」块,
    重复出现会让模型以为用户说了两遍)。
    """
    stmt = select(PlannerMessage).where(PlannerMessage.session_id == int(row.id))
    if before_id is not None:
        stmt = stmt.where(PlannerMessage.id < int(before_id))
    stmt = stmt.order_by(PlannerMessage.id.desc()).limit(max(1, int(limit)))
    rows = list(session.scalars(stmt))
    rows.reverse()
    return [{"role": item.role, "content": item.content or ""} for item in rows]


def count_turns(session: Session, row: PlannerSession) -> int:
    """第几轮 = 该会话里的 **user 消息条数**(含本轮),给响应的 ``turn_index``。"""
    return int(
        session.scalar(
            select(func.count(PlannerMessage.id)).where(
                PlannerMessage.session_id == int(row.id), PlannerMessage.role == ROLE_USER
            )
        )
        or 0
    )


def message_to_dict(row: PlannerMessage) -> dict[str, Any]:
    """消息序列化(**键名逐字**照契约 §3 TASK-10b 的 ``items``:role/content/payload/created_at)。"""
    payload = row.payload
    return {
        "role": row.role,
        "content": row.content or "",
        "payload": dict(payload) if isinstance(payload, Mapping) else None,
        "created_at": iso_utc(row.created_at),
    }


def list_messages(session: Session, *, session_key: Any, limit: int = LIST_LIMIT_DEFAULT) -> list[dict[str, Any]]:
    """回放历史:**最近 ``limit`` 条,时间正序**;未知 ``session_key`` → ``[]``(不 404)。"""
    row = get_session(session, session_key=session_key)
    if row is None:
        return []
    try:
        resolved = int(limit)
    except (TypeError, ValueError):
        resolved = LIST_LIMIT_DEFAULT
    if resolved <= 0:
        resolved = LIST_LIMIT_DEFAULT
    resolved = min(resolved, LIST_LIMIT_MAX)
    rows = list(
        session.scalars(
            select(PlannerMessage)
            .where(PlannerMessage.session_id == int(row.id))
            .order_by(PlannerMessage.id.desc())
            .limit(resolved)
        )
    )
    rows.reverse()
    return [message_to_dict(item) for item in rows]


def clear_session(session: Session, *, session_key: Any) -> int:
    """清空一个会话的消息,返回删掉的条数;**会话行保留**(``session_key`` 继续可用)。

    契约只要求「🗑 清空对话」,删会话行会让前端的 ``localStorage`` key 变孤儿,所以只删消息。
    未知 key → ``0``(重复清空同样不报错)。**自己 commit**(删除是不可逆操作)。
    """
    row = get_session(session, session_key=session_key)
    if row is None:
        return 0
    deleted = session.execute(
        delete(PlannerMessage).where(PlannerMessage.session_id == int(row.id))
    ).rowcount
    session.flush()
    session.commit()
    return int(deleted or 0)


# --------------------------------------------------------------------------- #
# 一轮对话
# --------------------------------------------------------------------------- #


def plan_turn(
    session: Session,
    *,
    session_key: Any,
    message: Any,
    collection_briefs: Iterable[Mapping[str, Any]] = (),
    nights: Any = None,
    client: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """跑一轮 AI 行程对话:存 user 消息 → 组 prompt → 调 LLM → 解析 → 存 assistant 消息。

    返回形状(**键名逐字一致**,契约 §3 TASK-10a)::

        {"session_key","title","reply","itinerary"|None,"degraded","reason","turn_index","generated_at"}

    * ``session_key`` 空/None → 后端补发 ``uuid4().hex``(前端存起来实现多轮);
    * ``message`` 空或超 :data:`MAX_MESSAGE_LEN` → :class:`ValueError`(API 层转 **400 中文**);
    * ``collection_briefs`` = 用户**点名**的收藏素材(``{"id","kind","title","summary"}``,
      由 API 层给);没点名就传空,模型按需求自由选点;
    * ``nights`` 阶段 A **不进 prompt**(不出住宿),只随 user 消息 ``payload`` 存着;
    * 四种降级(``no_key`` / ``timeout`` / ``error`` / ``parse_error``)**一律不抛**:
      ``degraded=True`` + ``reason`` + 中文 ``reply``,消息照样落库(前端能回放失败原因);
    * 本函数**自己 commit**:一轮 LLM 动辄几十秒,返回后必须立刻落盘。
    """
    text = _as_text(message)
    if not text:
        raise ValueError("缺少必要参数:message(对话内容不能为空)")
    if len(text) > MAX_MESSAGE_LEN:
        raise ValueError(f"message 过长:{len(text)} 字,上限 {MAX_MESSAGE_LEN} 字(请分几轮说)")

    key = resolve_session_key(session_key)
    row, _created = get_or_create_session(session, session_key=key, title=text)
    briefs = normalize_briefs(collection_briefs)
    resolved_nights = _optional_nights(nights)
    user_message = add_message(
        session,
        row,
        role=ROLE_USER,
        content=text,
        payload={
            "nights": resolved_nights,
            "collection_ids": [brief["id"] for brief in briefs if brief.get("id") is not None],
        },
    )
    turn_index = count_turns(session, row)
    prompt = build_prompt(
        text,
        history=recent_history(session, row, before_id=user_message.id, limit=HISTORY_LIMIT),
        collection_briefs=briefs,
    )
    generated_at = iso_utc(utcnow())
    llm = resolve_client(client, environ)

    if not llm.enabled:
        return _finish_turn(
            session,
            row,
            reply=degraded_reply(REASON_NO_KEY),
            itinerary=None,
            degraded=True,
            reason=REASON_NO_KEY,
            turn_index=turn_index,
            generated_at=generated_at,
        )

    try:
        raw = llm.chat(
            prompt,
            system=SYSTEM_PROMPT,
            max_tokens=PLANNER_MAX_TOKENS,
            timeout=PLANNER_TIMEOUT_S,
        )
    except DataSourceError as exc:
        reason = REASON_TIMEOUT if _is_timeout(exc) else REASON_ERROR
        return _finish_turn(
            session,
            row,
            reply=degraded_reply(reason, detail=getattr(exc, "message", exc)),
            itinerary=None,
            degraded=True,
            reason=reason,
            turn_index=turn_index,
            generated_at=generated_at,
        )
    except Exception as exc:  # noqa: BLE001 - 降级四态一律不抛(前端要看到可见文案)
        reason = REASON_TIMEOUT if _is_timeout(exc) else REASON_ERROR
        return _finish_turn(
            session,
            row,
            reply=degraded_reply(reason, detail=exc),
            itinerary=None,
            degraded=True,
            reason=reason,
            turn_index=turn_index,
            generated_at=generated_at,
        )

    itinerary, snippet = parse_itinerary(raw)
    if itinerary is None:
        return _finish_turn(
            session,
            row,
            reply=degraded_reply(REASON_PARSE_ERROR, detail=snippet),
            itinerary=None,
            degraded=True,
            reason=REASON_PARSE_ERROR,
            turn_index=turn_index,
            generated_at=generated_at,
        )
    return _finish_turn(
        session,
        row,
        reply=build_reply(itinerary),
        itinerary=itinerary,
        degraded=False,
        reason=None,
        turn_index=turn_index,
        generated_at=generated_at,
    )


def _finish_turn(
    session: Session,
    row: PlannerSession,
    *,
    reply: str,
    itinerary: Optional[dict[str, Any]],
    degraded: bool,
    reason: Optional[str],
    turn_index: int,
    generated_at: Optional[str],
) -> dict[str, Any]:
    """存 assistant 消息(``payload`` = 结构化行程 + 降级原因)并拼响应(**键名逐字**)。"""
    add_message(
        session,
        row,
        role=ROLE_ASSISTANT,
        content=reply,
        payload={
            "itinerary": itinerary,
            "degraded": bool(degraded),
            "reason": reason,
            "turn_index": int(turn_index),
            "generated_at": generated_at,
        },
    )
    session.commit()
    return {
        "session_key": row.session_key,
        "title": row.title or "",
        "reply": reply,
        "itinerary": itinerary,
        "degraded": bool(degraded),
        "reason": reason,
        "turn_index": int(turn_index),
        "generated_at": generated_at,
    }
