"""配置 API(TASK-P2):``/setup`` 向导的配置读写 + 容器健康检查。

三条路由:

* ``GET /api/config`` —— 白名单里 7 个配置项的**当前状态**(label / hint / required / secret /
  group / configured / source / masked)。**只回掩码**:secret 键且已配置时给 ``ab…yz`` 这样的
  掩码,完整值**永不进响应**(照 ``services.intro.describe_llm`` 的「key 不出响应」口径)。
* ``POST /api/config`` —— **单键**保存(``{"key": "...", "value": "..."}``):写 ``AppConfig`` 表
  + 同步 ``os.environ`` → **热生效、无需重启**(``amap.resolve_key()`` / ``intro.resolve_provider()``
  都是每次调用现读 env)。``value`` 为空串 = **清除**该项向导配置。白名单外的键一律 **400 中文**
  ——这条路由**不是**任意 env 注入面。
* ``GET /api/health`` —— 给 Dockerfile HEALTHCHECK / compose / 向导横幅用:
  ``{"status": "ok" | "missing_config", "missing_required": [...], "version": "0.1.0-beta"}``。
  **缺必填项不报 5xx**(恒 200):服务是可用的,只是功能降级(地图不出图 / 简介留空),
  报 5xx 会让容器被判死并无限重启。

配置优先级:**容器环境变量永远压过向导存的值**(``services.app_config.load_into_environ``
启动时只填「env 未设或为空串」的键),所以 ``source`` 报 ``env`` 时说明这项是容器给的、
在向导里改不动它(要改就改 ``.env`` / ``-e``)。

校验口径与 ``/api/collections``、``/api/places`` 一致:缺参 / 非法一律 **400 + 中文报错**,
所以请求体故意收成裸 JSON 对象(``Body(None)``)而不是 pydantic 模型 —— 否则字段类型不对会被
FastAPI 拦成 422 英文报错,同一个"参数不对"就有两种状态码。本模块**不触网**。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic.fields import FieldInfo
from sqlalchemy.orm import Session

from db.base import get_session
from services import app_config

router = APIRouter()

# 版本口径(神朱拍板决策④):先 beta,NAS 冒烟过了再升 v1.0.0
APP_VERSION = "0.1.0-beta"

CONFIG_NOTE = (
    "配置保存在容器数据卷的数据库中;环境变量优先于向导配置。key 不回显明文。"
)
SAVED_NOTE = "已保存并即时生效(无需重启)"
CLEARED_NOTE = (
    "已清除该项向导配置并即时生效(无需重启);"
    "若容器环境变量里设了同名变量,仍以环境变量为准。"
)
HEALTH_OK = "ok"
HEALTH_MISSING_CONFIG = "missing_config"


def _optional_text(value: Any) -> Optional[str]:
    """可选文本参数归一:只认真正的 ``str``。

    直接调用端点函数(单测 / 脚本)时,FastAPI 的 ``Body(None)`` 默认值不会被解析,
    传进来是 ``FieldInfo`` 对象;这里统一把"没给"归一成 ``None``(同 ``app.api.collections``)。
    """
    return value if isinstance(value, str) else None


def _payload_dict(payload: Any) -> dict[str, Any]:
    """请求体归一:必须是 JSON 对象(``{...}``),否则 **400**(中文报错)。"""
    if payload is None or isinstance(payload, FieldInfo):
        raise HTTPException(
            400, '缺少请求体:POST /api/config 需要 JSON 对象 {"key": "...", "value": "..."}'
        )
    if not isinstance(payload, Mapping):
        raise HTTPException(400, f"请求体必须是 JSON 对象,收到:{type(payload).__name__}")
    return dict(payload)


def _resolve_key(body: Mapping[str, Any]) -> str:
    """取出并校验配置项名(白名单);缺失 / 非字符串 / 白名单外 → **400**(中文报错)。"""
    raw = _optional_text(body.get("key"))
    if raw is None:
        raise HTTPException(
            400,
            '缺少配置项 key:请求体要给出 {"key": "环境变量名", "value": "值"}'
            f"(可配置:{'、'.join(app_config.ALLOWED_KEYS)})",
        )
    name = raw.strip()
    if not name:
        raise HTTPException(400, "配置项 key 不能为空(必须是白名单里的环境变量名)")
    if not app_config.is_allowed(name):
        raise HTTPException(
            400,
            f"不允许配置的配置项:{name}(可配置:{'、'.join(app_config.ALLOWED_KEYS)})",
        )
    return name


def _resolve_value(name: str, body: Mapping[str, Any]) -> str:
    """取出配置项的值:缺 ``value`` 或给空串都算**清除**;非字符串标量按字符串存。"""
    value = body.get("value")
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(
            400, f"配置项 {name} 的值必须是字符串,收到:{type(value).__name__}"
        )
    return str(value)


def _item_of(session: Session, field: app_config.ConfigField) -> dict[str, Any]:
    """一个配置项的**对外状态**(不含明文:secret 项只给掩码)。"""
    source = app_config.display_source(session, field.key)
    configured = app_config.is_configured(session, field.key)
    return {
        "key": field.key,
        "label": field.label,
        "hint": field.hint,
        "required": field.required,
        "secret": field.secret,
        "group": field.group,
        "configured": configured,
        "source": source,
        "masked": (
            app_config.masked_effective(session, field.key)
            if field.secret and configured
            else ""
        ),
    }


@router.get("/config")
def read_config(session: Session = Depends(get_session)) -> dict[str, Any]:
    """白名单配置项的当前状态(**只回掩码,不回明文**)+ 还缺哪些必填项。"""
    return {
        "items": [_item_of(session, field) for field in app_config.iter_fields()],
        "missing_required": app_config.missing_required(session),
        "note": CONFIG_NOTE,
    }


@router.post("/config")
def save_config(
    payload: Any = Body(
        None, description='要保存的配置项 JSON 对象:{"key": "环境变量名", "value": "值(空串=清除)"}'
    ),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """保存**一个**配置项:写库 + 同步 ``os.environ``(**热生效,无需重启**)。

    向导逐个键保存(不做批量):每步保存完当场真调数据源校验,错了立刻给用户中文解释。
    ``source`` 用 :func:`app_config.display_source` 报「这值是谁给的」:保存成功 → ``setup``;
    清除后 → 空串(``set_key`` 会把进程内 env 也置空,容器 env 要重启才回来)。
    """
    body = _payload_dict(payload)
    name = _resolve_key(body)
    value = _resolve_value(name, body)
    try:
        app_config.set_key(session, name, value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    configured = app_config.is_configured(session, name)
    return {
        "ok": True,
        "key": name,
        "configured": configured,
        "source": app_config.display_source(session, name),
        "note": SAVED_NOTE if value else CLEARED_NOTE,
    }


@router.get("/health")
def health(session: Session = Depends(get_session)) -> dict[str, Any]:
    """健康检查(**恒 200**):必填项缺了报 ``missing_config``,不报 5xx。

    容器 HEALTHCHECK 打这里就够了 —— 服务本身能应答就是"活着";缺 key 是**功能降级**
    (地图不出图 / 简介留空),不是进程故障,报 5xx 只会让容器被无谓重启。
    ``missing_required`` 直接给向导横幅用。
    """
    missing = app_config.missing_required(session)
    return {
        "status": HEALTH_OK if not missing else HEALTH_MISSING_CONFIG,
        "missing_required": missing,
        "version": APP_VERSION,
    }
