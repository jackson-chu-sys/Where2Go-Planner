"""全局 AI 助手 API(TASK-11a):``/api/assistant/messages`` 的对话三件套(**只做后端**)。

三条路由(编排全在 :mod:`services.assistant`,本模块只做**参数归一 / 会话 key 补发**):

* ``POST /api/assistant/messages`` —— 发一轮通用旅行问答。``session_key`` 空则后端补发
  ``"chat-" + uuid4().hex``(前缀用来和「🤖 AI 行程」的会话在同一张表里区分);
  ``context`` 是前端给的当前页面上下文(城市 / 环带 / 收藏摘要),服务层截 500 字后拼进 prompt。
  响应是 :func:`services.assistant.chat_turn` 的形状**原样透出**
  (``{"session_key","reply","degraded","reason","message_id"}``,键名逐字,前端只读这些键,
  所以这里**不加** ``note``/``elapsed_s``)。
* ``GET /api/assistant/messages?session_key=&limit=`` —— 回放历史(``limit`` 默认 50、上限 200);
  **未知 session → ``items: []``**(不 404:刷新页面时前端的 ``localStorage`` key 可能已失效)。
* ``DELETE /api/assistant/messages`` —— 「🗑 清空对话」:请求体 ``{"session_key": …}``,
  只删消息、**会话行保留**(``session_key`` 继续可用),未知 key → ``deleted: 0``,同样不 404。

校验口径与 ``/api/planner/messages``、``/api/trip-plans`` 一致:缺参 / 非法一律 **400 + 中文报错**,
所以请求体故意收成裸 JSON 对象(``Body(None)``)而不是 pydantic 模型 —— 否则字段类型不对会被
FastAPI 拦成 422 英文报错,同一个"参数不对"就有两种状态码。
本模块**不触网**(除了 ``chat_turn`` 里那一次 LLM chat),其余只读写 SQLite。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from db import models
from db.base import get_session
from services import assistant as assistant_service
from services import planner as planner_service

router = APIRouter()

DEFAULT_PAGE = planner_service.LIST_LIMIT_DEFAULT  # 50:GET 历史消息的默认条数
MAX_PAGE = planner_service.LIST_LIMIT_MAX          # 200:前端传更大也按这个截断(与 planner 同口径)

MESSAGES_NOTE = (
    "全局 AI 助手(通用旅行问答)的历史回放:最近 limit 条、时间正序;"
    "未知 session_key 返回空 items(不 404,前端 localStorage 里的 key 可能已失效)。"
    "助手会话与「🤖 AI 行程」会话共用 planner_sessions / planner_messages 两张表,"
    "靠 session_key 的 chat- 前缀区分;payload.context 是提问那一刻前端给的页面上下文(截 500 字)。"
)
CLEAR_NOTE = (
    "清空对话只删消息、保留会话行:同一个 session_key 可以继续用(前端不必换 key 重开一轮);"
    "未知 key → deleted=0,重复清空同样不报错(不 404)。"
)


def _optional_text(value: Any) -> Optional[str]:
    """可选文本参数归一:只认真正的 ``str``。

    直接调用端点函数(单测 / 脚本)时,FastAPI 的 ``Query(None)`` 默认值不会被解析,
    传进来是 ``FieldInfo`` 对象;这里统一把"没给"归一成 ``None``(同 ``app.api.planner``)。
    """
    return value if isinstance(value, str) else None


def _optional_int(name: str, value: Any) -> Optional[int]:
    """可选整数参数归一:没给 → ``None``;非整数 / 非正数 → **400**(中文报错)。"""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (str, int)):
        return None  # FieldInfo 等"没给"的情况
    text = str(value).strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError as exc:
        raise HTTPException(400, f"参数 {name} 必须是整数,收到:{value!r}") from exc
    if number <= 0:
        raise HTTPException(400, f"参数 {name} 必须是正整数,收到:{value!r}")
    return number


def _payload_dict(payload: Any, *, endpoint: str) -> dict[str, Any]:
    """请求体归一:必须是 JSON 对象(``{...}``),否则 **400**(中文报错)。"""
    if payload is None:
        raise HTTPException(400, f"缺少请求体:{endpoint} 需要 JSON 对象")
    if not isinstance(payload, Mapping):
        raise HTTPException(400, f"请求体必须是 JSON 对象,收到:{type(payload).__name__}")
    return dict(payload)


def _required_session_key(value: Any) -> str:
    """``session_key`` 归一(GET / DELETE 用):缺 / 空白 → **400**(中文报错)。

    这两个端点**不补发** key:补发会让前端拿着一个空会话继续问,历史反而对不上。
    """
    key = models.clean_text(value, limit=models.SESSION_KEY_LEN)
    if not key:
        raise HTTPException(
            400, "缺少必要参数:session_key(先 POST /api/assistant/messages 拿一个)"
        )
    return key


def _session_key_of(body: Mapping[str, Any]) -> str:
    """POST 的 ``session_key`` 归一:给了就原样用(**不校验 chat- 前缀**),空则后端补发一个。"""
    key = models.clean_text(body.get("session_key"), limit=models.SESSION_KEY_LEN)
    return key or assistant_service.new_session_key()


def _message_of(body: Mapping[str, Any]) -> str:
    """提问内容归一:空 / 非字符串 / 超 :data:`services.assistant.MAX_MESSAGE_LEN` → **400**。"""
    raw = body.get("message")
    if raw is not None and not isinstance(raw, str):
        raise HTTPException(400, f"参数 message 必须是字符串,收到:{type(raw).__name__}")
    text = models.clean_text(raw)
    if not text:
        raise HTTPException(400, "缺少必要参数:message(提问内容不能为空)")
    if len(text) > assistant_service.MAX_MESSAGE_LEN:
        raise HTTPException(
            400,
            f"message 过长:{len(text)} 字,上限 {assistant_service.MAX_MESSAGE_LEN} 字(请分几句说)",
        )
    return text


def _context_of(body: Mapping[str, Any]) -> Optional[str]:
    """页面上下文归一:``None`` = 前端没带(整块不拼进 prompt);非字符串 → **400** 中文。

    这里**不截断**:截 :data:`services.assistant.CONTEXT_MAX_CHARS`(500)字是服务层的事,
    存进 ``payload`` 的也是截断后的值,两处口径才不会打架。
    """
    raw = body.get("context")
    if raw is None:
        return None
    text = _optional_text(raw)
    if text is None:
        raise HTTPException(400, f"参数 context 必须是字符串,收到:{type(raw).__name__}")
    return text


@router.post("/assistant/messages")
def post_message(
    payload: Any = Body(None, description="对话 JSON 对象:session_key / message / context"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """发一轮通用旅行问答;响应是 :func:`services.assistant.chat_turn` 的形状**原样透出**。

    降级四态(``no_key`` / ``timeout`` / ``error`` / ``parse_error``)由服务层**不抛**地写成
    ``degraded=True`` + ``reason`` + 中文 ``reply``,所以这里只有"参数不对"才会 400。
    """
    body = _payload_dict(payload, endpoint="POST /api/assistant/messages")
    message = _message_of(body)
    context = _context_of(body)
    try:
        return assistant_service.chat_turn(
            _session_key_of(body), message, context=context, session=session
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/assistant/messages")
def get_messages(
    session_key: Optional[str] = Query(None, description="会话 key(POST /api/assistant/messages 返回的)"),
    limit: Optional[str] = Query(None, description=f"最多返回几条(正整数,上限 {MAX_PAGE}),默认 {DEFAULT_PAGE}"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """回放一个助手会话的历史(**时间正序**);未知 session → ``items: []``(不 404)。"""
    key = _required_session_key(_optional_text(session_key))
    resolved_limit = _optional_int("limit", _optional_text(limit))
    resolved_limit = DEFAULT_PAGE if resolved_limit is None else min(resolved_limit, MAX_PAGE)
    return {
        "session_key": key,
        "items": assistant_service.list_messages(key, limit=resolved_limit, session=session),
        "note": MESSAGES_NOTE,
    }


@router.delete("/assistant/messages")
def delete_messages(
    payload: Any = Body(None, description="清空对话 JSON 对象:session_key"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """「🗑 清空对话」:只删消息、**会话行保留**;未知 key → ``deleted: 0``(不 404)。"""
    body = _payload_dict(payload, endpoint="DELETE /api/assistant/messages")
    key = _required_session_key(body.get("session_key"))
    return {
        "session_key": key,
        "deleted": assistant_service.clear_session(key, session=session),
        "note": CLEAR_NOTE,
    }
