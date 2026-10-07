"""全局 AI 助手(TASK-11a,**只做后端**):通用旅行问答的编排 + 会话读写。

拍板口径(神朱:所有界面侧边加 AI 对话窗口,用户可描述、提问 → 定位 = **通用旅行助手**):

1. **复用行程会话的两张表** —— :class:`~db.models.PlannerSession` / :class:`~db.models.PlannerMessage`
   表结构**零改动**;助手会话的 ``session_key`` 由调用方带 ``"chat-"`` 前缀与行程会话
   (:mod:`services.planner` 用的是裸 ``uuid4().hex``)区分。本模块**不校验前缀**:
   给什么 key 就原样存取 —— 前缀是"约定"不是"约束",前端换前缀不必改后端。
2. **只答旅行** —— 系统提示(:data:`SYSTEM_PROMPT`)写死:答旅行问题 / 描述目的地 /
   给实用建议;拿不到的事实**明说**、禁止编造;中文回复、纯文本 ≤400 字
   (**不输出 JSON**:侧边抽屉直接把 ``reply`` 当文本渲染,不像行程那样要解析结构)。
3. **输出预算必须按调用放大** —— :data:`ASSISTANT_MAX_TOKENS` / :data:`ASSISTANT_TIMEOUT_S`
   显式传给 ``LLMClient.chat``:沿用 :mod:`services.intro` 的 120 tokens / 20s 默认值会把
   回答静默截断(2026-09-27 的项目 LLM 预算坑,别再踩)。
4. **降级一律不抛** —— 四态 ``no_key`` / ``timeout`` / ``error`` / ``parse_error``
   (原因码与 :func:`services.planner.plan_turn` **同一套**),``degraded=True`` + ``reason`` +
   含原因码的中文 ``reply``,消息**照样落库**(前端能回放失败原因,绝不静默)。
5. **上下文只当背景** —— 前端把"当前城市 / 环带 / 收藏摘要"塞进 ``context``,截
   :data:`CONTEXT_MAX_CHARS` 字后拼进 prompt;``None`` / 空白则**整块不拼**
   (省预算,也免得模型以为用户收藏了什么)。

写库口径与 planner 一致::func:`chat_turn` / :func:`clear_session` **自己 commit**
(一轮对话动辄几十秒,LLM 返回后必须立刻落盘,不能等调用方记得提交);
:func:`list_messages` 只读。会话/消息的读写全部**复用** :mod:`services.planner` 的
同名函数(同一套表语义,别在这里另写一套)。本模块除了 ``LLMClient`` 那一次 chat 不触网。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from sqlalchemy.orm import Session

from data_sources import DataSourceError
from db.base import open_session
from db.models import SESSION_KEY_LEN, clean_text, iso_utc, utcnow
from services import planner as planner_service
from services.intro import (
    LLMClient,
    default_client as default_llm_client,
    resolve_provider,
)
from services.planner import (  # 归一/判定口径直接复用 planner 的,别在这里另写一套
    _as_text,
    _is_timeout,
    _snippet,
)

# --------------------------------------------------------------------------- #
# 预算与硬上限(契约 TASK-11a 逐字)
# --------------------------------------------------------------------------- #

ASSISTANT_MAX_TOKENS = 800       # 必须按调用放大(勿用 intro 默认 120/20s;见项目 LLM 预算坑)
ASSISTANT_TIMEOUT_S = 120.0
ASSISTANT_HISTORY_LIMIT = 12     # 送进 prompt 的历史消息上限(超出只留最近 12 条)
CONTEXT_MAX_CHARS = 500          # 前端给的"当前页面上下文"截断长度

SESSION_PREFIX = "chat-"         # 助手会话 key 前缀(与行程会话区分;服务层不强制校验)
MAX_MESSAGE_LEN = planner_service.MAX_MESSAGE_LEN  # 2000:与行程对话同一口径

ROLE_USER = planner_service.ROLE_USER
ROLE_ASSISTANT = planner_service.ROLE_ASSISTANT

REASON_NO_KEY = planner_service.REASON_NO_KEY
REASON_TIMEOUT = planner_service.REASON_TIMEOUT
REASON_ERROR = planner_service.REASON_ERROR
REASON_PARSE_ERROR = planner_service.REASON_PARSE_ERROR

SYSTEM_PROMPT = (
    "你是 Where2Go(周末去哪儿玩)里的**通用旅行助手**,在站点各个页面的侧边窗口回答用户提问。"
    "硬约束(逐条遵守,违反=返工):"
    "1)只聊旅行:回答旅行相关的问题、描述目的地(玩法/季节/看点)、给出实用建议"
    "(交通与住宿的一般常识、行程思路、天气与人流的应对、亲子/滑雪/徒步等主题的注意事项);"
    "2)拿不到的事实(门票价格、开放时间、班次、营业状态、实时天气等)一律明说"
    "「不确定,请出行前再核实」,**禁止编造**具体数字与名称;"
    "3)用户给了「当前页面上下文」(城市 / 环带 / 收藏摘要)时,只把它当背景参考,"
    "不要假定用户已经收藏了什么,也不要替用户排完整行程(排行程是「🤖 AI 行程」那个入口的事);"
    "4)与旅行无关的问题,一句话说明自己只负责旅行话题,并把话题引回出行;"
    "5)用**中文**回复,**纯文本**、不超过 400 字:不要 JSON、不要 markdown 围栏与标题、"
    "不要分点编号堆砌,像朋友一样把话说清楚。"
)

# 降级兜底文案(中文,含原因码 + 可重试提示):四态一律走这里,前端直接显示
DEGRADED_REPLIES: dict[str, str] = {
    REASON_NO_KEY: (
        "AI 助手暂时不可用(原因:no_key)—— 服务端没有配置可用的 LLM key。"
        "请先配置 WHERE2GO_LLM_API_KEY(或 DEEPSEEK_API_KEY / ALIBABA_TOKEN_PLAN_API_KEY),"
        "然后重发这一句即可,对话历史不会丢。"
    ),
    REASON_TIMEOUT: (
        "AI 助手超时了(原因:timeout)—— 模型没在 "
        f"{int(ASSISTANT_TIMEOUT_S)} 秒内回完。可以稍后重试,"
        "或把问题拆短一点再问(例如只问一个目的地)。"
    ),
    REASON_ERROR: (
        "AI 助手调用失败(原因:error)—— 模型服务报错或网络异常。"
        "请稍后重试;若连续失败,请看后端日志里的 LLM 报错原文。"
    ),
    REASON_PARSE_ERROR: (
        "AI 这一轮返回的内容不是可用的中文纯文本(原因:parse_error),已按降级处理、没有编造回答。"
        "回复「重试」或换一种说法再问一次。"
    ),
}

CONTEXT_HEADER = "【当前页面上下文(仅供参考,不要假定用户收藏了什么)】"
HISTORY_HEADER = f"【历史对话(最近 {ASSISTANT_HISTORY_LIMIT} 条)】"
EMPTY_HISTORY = "(这是本会话的第一轮,没有历史)"
OUTPUT_REQUIREMENT = (
    "【输出要求】中文纯文本、不超过 400 字;不要 JSON、不要 markdown 围栏与标题;"
    "拿不到的事实明说「不确定,请出行前再核实」,禁止编造。"
)


# --------------------------------------------------------------------------- #
# LLM 客户端(唯一入口仍是 services.intro)
# --------------------------------------------------------------------------- #


def resolve_client(
    client: Optional[LLMClient] = None, environ: Optional[Mapping[str, str]] = None
) -> LLMClient:
    """LLM 客户端的**唯一入口**(:mod:`services.intro` 的 resolve_provider / LLMClient)。

    与 :func:`services.planner.resolve_client` 同一口径:给了 ``client`` 就用它(替身),
    给了 ``environ`` 就按它解析 provider(验无 key 降级),都没有就用进程内共享的默认客户端
    (单测把 :data:`default_llm_client` 换成替身,所以本模块**不触网**也能测)。
    """
    if client is not None:
        return client
    if environ is None:
        return default_llm_client()
    return LLMClient(resolve_provider(environ), environ=environ)


def degraded_reply(reason: str, *, detail: Any = None) -> str:
    """降级文案:固定中文兜底 + **原因码**,必要时把细节(异常/原文片段)附在后面。"""
    text = DEGRADED_REPLIES.get(reason, DEGRADED_REPLIES[REASON_ERROR])
    snippet = _snippet(detail)
    if snippet:
        text = f"{text}\n原始片段:{snippet}"
    return text


# --------------------------------------------------------------------------- #
# 会话 key / 上下文归一
# --------------------------------------------------------------------------- #


def new_session_key() -> str:
    """新助手会话 key:``"chat-" + uuid4().hex``(前缀让它与行程会话在同一张表里可区分)。"""
    return f"{SESSION_PREFIX}{uuid.uuid4().hex}"


def resolve_session_key(value: Any) -> str:
    """会话 key 归一:非空就**原样**用(按列宽截断,不校验前缀),空/None → :func:`new_session_key`。

    前端把 key 存 ``localStorage`` 实现"刷新不丢对话";第一次提问还没有 key,由这里补发。
    """
    return clean_text(value, limit=SESSION_KEY_LEN) or new_session_key()


def truncate_context(context: Any) -> str:
    """页面上下文归一:非字符串/空白 → ``""``(整块不拼进 prompt),否则截 500 字。"""
    return _as_text(context)[:CONTEXT_MAX_CHARS]


# --------------------------------------------------------------------------- #
# prompt 组装
# --------------------------------------------------------------------------- #


def _history_block(history: Sequence[Mapping[str, Any]]) -> str:
    """历史块:只留最近 :data:`ASSISTANT_HISTORY_LIMIT` 条,单条再按 planner 的 600 字截。"""
    if not history:
        return EMPTY_HISTORY
    lines = []
    for item in history:
        who = "用户" if str(item.get("role")) == ROLE_USER else "助手"
        content = str(item.get("content") or "").strip()[: planner_service.HISTORY_ITEM_CHARS]
        lines.append(f"{who}:{content}")
    return "\n".join(lines)


def build_prompt(
    message: str, *, history: Sequence[Mapping[str, Any]] = (), context: Optional[str] = None
) -> str:
    """拼一轮 prompt:历史 → 页面上下文(**空则整块不拼**)→ 本轮用户消息 → 输出要求。"""
    parts = [f"{HISTORY_HEADER}\n{_history_block(history)}"]
    brief = truncate_context(context)
    if brief:
        parts.append(f"{CONTEXT_HEADER}\n{brief}")
    parts.append(f"【本轮用户消息】\n{_as_text(message)}")
    parts.append(OUTPUT_REQUIREMENT)
    return "\n\n".join(parts)


def extract_reply(raw: Any) -> tuple[str, Optional[str]]:
    """收下模型的**纯文本**回答;不合法 → ``("", 原文前 200 字)`` 交给 ``parse_error`` 降级。

    两条判废口径(系统提示第 5 条要求纯文本):空/非字符串算废;剥掉 ```` ``` ```` 围栏后
    仍以 ``{`` / ``[`` 开头(模型回了 JSON)也算废 —— 侧边抽屉是按纯文本渲染的,
    把一坨 JSON 甩给用户不如明说降级。
    """
    if not isinstance(raw, str) or not raw.strip():
        return "", _snippet(raw)
    text = planner_service.strip_code_fence(raw).strip()
    if not text or text.startswith(("{", "[")):
        return "", _snippet(raw)
    return text, None


# --------------------------------------------------------------------------- #
# 会话与消息读写(全部复用 services.planner 的同名函数:同一套表语义)
# --------------------------------------------------------------------------- #


@contextmanager
def _session_scope(session: Optional[Session]) -> Iterator[Session]:
    """``session=None`` 时自己开一个(脚本/CLI 直调),用完关掉;给了就用调用方的(**不关**)。

    API 层永远走 ``Depends(get_session)`` 传进来,所以这里只是给"没有 FastAPI 依赖注入"
    的调用方兜底,绝不偷偷复用别人的会话。
    """
    if session is not None:
        yield session
        return
    own = open_session()
    try:
        yield own
    finally:
        own.close()


def list_messages(
    session_key: str, *, limit: int = planner_service.LIST_LIMIT_DEFAULT, session: Optional[Session] = None
) -> list[dict[str, Any]]:
    """回放一个助手会话的历史:**最近 ``limit`` 条(默认 50、上限 200),时间正序**。

    未知 ``session_key`` → ``[]``(**不 404**:刷新页面时前端 ``localStorage`` 里的 key 可能已失效)。
    序列化键名照 planner 的 ``items``(``role`` / ``content`` / ``payload`` / ``created_at``)。
    """
    with _session_scope(session) as db:
        return planner_service.list_messages(db, session_key=session_key, limit=limit)


def clear_session(session_key: str, *, session: Optional[Session] = None) -> int:
    """清空一个助手会话的消息,返回删掉的条数;**会话行保留**(``session_key`` 继续可用)。

    删会话行会让前端的 ``localStorage`` key 变孤儿,所以只删消息(与
    :func:`services.planner.clear_session` 同口径)。未知 key → ``0``,重复清空同样不报错。
    **自己 commit**(删除不可逆)。
    """
    with _session_scope(session) as db:
        return planner_service.clear_session(db, session_key=session_key)


# --------------------------------------------------------------------------- #
# 一轮对话
# --------------------------------------------------------------------------- #


def chat_turn(
    session_key: str,
    message: str,
    *,
    context: Optional[str] = None,
    session: Optional[Session] = None,
) -> dict[str, Any]:
    """跑一轮通用旅行问答:存 user 消息 → 组 prompt → 调 LLM → 存 assistant 消息。

    返回形状(**键名逐字一致**,契约 TASK-11a)::

        {"session_key","reply","degraded","reason","message_id"}

    * ``session_key`` 空/None → 补发 ``"chat-" + uuid4().hex``;给了就**原样存取**(不校验前缀);
    * ``message`` 空或超 :data:`MAX_MESSAGE_LEN`(2000)→ :class:`ValueError`(API 层转 **400 中文**);
    * ``context`` = 前端给的当前页面上下文(城市/环带/收藏摘要),截 500 字后拼进 prompt,
      空则不拼;截断后的值随 user 消息的 ``payload`` 存着(回放时能看到当时在哪个页面问的);
    * ``message_id`` = 本轮 **assistant 消息**的 id(前端可据它做"重新生成"/定位);
    * 四种降级(``no_key`` / ``timeout`` / ``error`` / ``parse_error``)**一律不抛**:
      ``degraded=True`` + ``reason`` + 中文 ``reply``,消息照样落库;
    * 本函数**自己 commit**:一轮 LLM 动辄几十秒,返回后必须立刻落盘。
    """
    text = _as_text(message)
    if not text:
        raise ValueError("缺少必要参数:message(对话内容不能为空)")
    if len(text) > MAX_MESSAGE_LEN:
        raise ValueError(f"message 过长:{len(text)} 字,上限 {MAX_MESSAGE_LEN} 字(请分几轮说)")

    key = resolve_session_key(session_key)
    brief = truncate_context(context)
    with _session_scope(session) as db:
        row, _created = planner_service.get_or_create_session(db, session_key=key, title=text)
        user_message = planner_service.add_message(
            db, row, role=ROLE_USER, content=text, payload={"context": brief or None}
        )
        turn_index = planner_service.count_turns(db, row)
        prompt = build_prompt(
            text,
            history=planner_service.recent_history(
                db, row, before_id=user_message.id, limit=ASSISTANT_HISTORY_LIMIT
            ),
            context=brief,
        )
        generated_at = iso_utc(utcnow())
        llm = resolve_client()

        if not llm.enabled:
            return _finish_turn(
                db,
                row,
                reply=degraded_reply(REASON_NO_KEY),
                degraded=True,
                reason=REASON_NO_KEY,
                turn_index=turn_index,
                generated_at=generated_at,
            )

        try:
            raw = llm.chat(
                prompt,
                system=SYSTEM_PROMPT,
                max_tokens=ASSISTANT_MAX_TOKENS,
                timeout=ASSISTANT_TIMEOUT_S,
            )
        except DataSourceError as exc:
            reason = REASON_TIMEOUT if _is_timeout(exc) else REASON_ERROR
            return _finish_turn(
                db,
                row,
                reply=degraded_reply(reason, detail=getattr(exc, "message", exc)),
                degraded=True,
                reason=reason,
                turn_index=turn_index,
                generated_at=generated_at,
            )
        except Exception as exc:  # noqa: BLE001 - 降级四态一律不抛(侧边窗口要看到可见文案)
            reason = REASON_TIMEOUT if _is_timeout(exc) else REASON_ERROR
            return _finish_turn(
                db,
                row,
                reply=degraded_reply(reason, detail=exc),
                degraded=True,
                reason=reason,
                turn_index=turn_index,
                generated_at=generated_at,
            )

        reply, snippet = extract_reply(raw)
        if not reply:
            return _finish_turn(
                db,
                row,
                reply=degraded_reply(REASON_PARSE_ERROR, detail=snippet),
                degraded=True,
                reason=REASON_PARSE_ERROR,
                turn_index=turn_index,
                generated_at=generated_at,
            )
        return _finish_turn(
            db,
            row,
            reply=reply,
            degraded=False,
            reason=None,
            turn_index=turn_index,
            generated_at=generated_at,
        )


def _finish_turn(
    db: Session,
    row: Any,
    *,
    reply: str,
    degraded: bool,
    reason: Optional[str],
    turn_index: int,
    generated_at: Optional[str],
) -> dict[str, Any]:
    """存 assistant 消息(``payload`` = 降级标记 + 轮次)并拼响应(**键名逐字**)。"""
    message = planner_service.add_message(
        db,
        row,
        role=ROLE_ASSISTANT,
        content=reply,
        payload={
            "degraded": bool(degraded),
            "reason": reason,
            "turn_index": int(turn_index),
            "generated_at": generated_at,
        },
    )
    db.commit()
    return {
        "session_key": row.session_key,
        "reply": reply,
        "degraded": bool(degraded),
        "reason": reason,
        "message_id": int(message.id),
    }
