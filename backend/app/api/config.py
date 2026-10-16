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

import re
from collections.abc import Mapping
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic.fields import FieldInfo
from sqlalchemy.orm import Session

from data_sources import DataSourceError, TransientDataSourceError, amap
from db.base import get_session
from services import app_config
from services import intro as intro_service

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


# --------------------------------------------------------------------------- #
# TASK-P3:向导「真调校验」端点(每步只打**一次**外部请求,防高德 QPS 10021)
# --------------------------------------------------------------------------- #

#: 可校验的目标:①高德 Web 服务 key ②高德 JS key ③LLM(发 1-token 测试消息)
VALIDATE_TARGETS: tuple[str, ...] = ("amap", "amap_js", "llm")
#: Web 服务 key 的探针:一次正向地理编码(固定城市,不做批量探活)
PROBE_ADDRESS = "北京"
#: JS key 探针的预期 infocode:10009 = 「平台不符」恰好证明它**就是** JS 平台的 key
JS_KEY_EXPECTED_INFOCODE = "10009"
LLM_PROBE_PROMPT = "连通性测试,请只回复一个字:好"
LLM_PROBE_MAX_TOKENS = 16
LLM_PROBE_TIMEOUT_S = 20.0
_INFOCODE_RE = re.compile(r"infocode=(\d+)")

VALIDATE_NOTE = (
    "校验只发一次真实请求(高德 QPS 限制,连打会报 10021);结果不落库,key 不回显明文。"
)


def _scrub(text: str, *values: str) -> str:
    """把可能混进错误文案的明文值替换成掩码(**完整值永不进响应**,照 P2 口径)。"""
    out = str(text or "")
    for value in values:
        cleaned = (value or "").strip()
        if cleaned and cleaned in out:
            out = out.replace(cleaned, app_config.mask_value(cleaned))
    return out


def _extract_infocode(text: str) -> str:
    match = _INFOCODE_RE.search(str(text or ""))
    return match.group(1) if match else ""


def _validate_amap(session: Session, value: str) -> dict[str, Any]:
    """真调一次 ``geocode("北京")`` 校验**Web 服务** key;失败给 infocode 中文解释。"""
    key = value.strip() or app_config.effective_value(session, app_config.ENV_AMAP_KEY)
    if not key:
        return {"ok": False, "target": "amap", "infocode": "",
                "detail": "还没填 key:先粘贴「Web服务」平台的高德 key 再点校验。"}
    try:
        rows = amap.geocode(PROBE_ADDRESS, environ={amap.ENV_AMAP_KEY: key})
    except DataSourceError as exc:  # 含 Transient(重试打满后也归到这一档)
        text = _scrub(exc.message or str(exc), key)
        return {"ok": False, "target": "amap",
                "infocode": _extract_infocode(text), "detail": text}
    if not rows:
        return {"ok": False, "target": "amap", "infocode": "",
                "detail": f"key 能用但地理编码「{PROBE_ADDRESS}」零结果(罕见),请核对 key 权限。"}
    first = rows[0]
    return {
        "ok": True, "target": "amap", "infocode": "",
        "detail": (f"校验通过:geocode(「{PROBE_ADDRESS}」)返回 {len(rows)} 条,"
                   f"首条「{first.get('formatted_address') or PROBE_ADDRESS}」。"),
        "sample": {"formatted_address": first.get("formatted_address", ""),
                   "lng": first.get("lng"), "lat": first.get("lat")},
    }


def _validate_amap_js(session: Session, value: str) -> dict[str, Any]:
    """用 JS key 打一次 REST 探针:**返回 10009(平台不符)= 校验通过**。

    JS key 与 Web 服务 key 平台隔离(血泪坑:混用报 ``USERKEY_PLAT_NOMATCH``,
    infocode 10009)。所以「拿它调 REST 被拒、且拒的原因正是平台不符」恰恰证明
    它是一张真的「Web端(JS API)」key;返回 10001/10013 等则是 key 本身无效。
    安全密钥(securityJsCode)无法在服务端校验(只在浏览器加载地图脚本时用),
    向导文案已写明:填错会在首页地图报 ``INVALID_USER_SCODE``。
    """
    key = value.strip() or app_config.effective_value(session, app_config.ENV_AMAP_JS_KEY)
    if not key:
        return {"ok": False, "target": "amap_js", "infocode": "",
                "detail": "还没填 key:先粘贴「Web端(JS API)」平台的高德 key 再点校验。"}
    try:
        amap.geocode(PROBE_ADDRESS, environ={amap.ENV_AMAP_KEY: key})
    except DataSourceError as exc:
        text = _scrub(exc.message or str(exc), key)
        infocode = _extract_infocode(text)
        if infocode == JS_KEY_EXPECTED_INFOCODE:
            return {
                "ok": True, "target": "amap_js", "infocode": infocode,
                "detail": ("校验通过:高德返回 10009「key 与平台类型不符」——这恰好证明它是一张"
                           "「Web端(JS API)」key(与 Web 服务 key 平台隔离、不能混用);"
                           "前端地图将用它 + 安全密钥加载。"),
            }
        if isinstance(exc, TransientDataSourceError):
            return {"ok": False, "target": "amap_js", "infocode": infocode,
                    "detail": f"高德暂时不可达,无法判定:{text}(稍后重试即可,别连点)"}
        return {"ok": False, "target": "amap_js", "infocode": infocode, "detail": text}
    # REST 居然成功了:说明这张 key 同时开了 Web 服务平台(或填的其实是 Web 服务 key)
    return {
        "ok": True, "target": "amap_js", "infocode": "",
        "detail": ("这张 key 也能调通 Web 服务 REST(可能同时开通了两个平台,或填成了 Web 服务 key)。"
                   "地图大概率能用,但请确认它是在「Web端(JS API)」平台下申请的;"
                   "两个 key 平台隔离,前端地图只认 JS key + 安全密钥。"),
    }


def _validate_llm(session: Session, body: Mapping[str, Any], value: str) -> dict[str, Any]:
    """发一条 1-token 测试消息校验 LLM 配置(provider/base_url/model 可临时覆盖)。"""
    overrides = {
        intro_service.ENV_PROVIDER: _optional_text(body.get("provider")) or "",
        intro_service.ENV_BASE_URL: _optional_text(body.get("base_url")) or "",
        intro_service.ENV_MODEL: _optional_text(body.get("model")) or "",
    }
    key = value.strip() or app_config.effective_value(session, app_config.ENV_LLM_API_KEY)
    environ: dict[str, str] = {name: text for name, text in overrides.items() if text}
    if key:
        environ[intro_service.ENV_API_KEY] = key
    resolved = intro_service.resolve_provider(environ if environ else None)
    if resolved is None:
        return {"ok": False, "target": "llm", "infocode": "",
                "detail": ("没有可校验的 LLM key:这一步是可选的,可以直接跳过;"
                           "要校验就填 key(任意 OpenAI 兼容服务)。")}
    try:
        client = intro_service.LLMClient(
            resolved=resolved, timeout=LLM_PROBE_TIMEOUT_S, max_tokens=LLM_PROBE_MAX_TOKENS
        )
        reply = client.chat(LLM_PROBE_PROMPT)
    except DataSourceError as exc:
        text = _scrub(exc.message or str(exc), key, resolved.api_key)
        return {"ok": False, "target": "llm", "infocode": "",
                "detail": f"[{resolved.label} · {resolved.model}] {text}"}
    except Exception as exc:  # noqa: BLE001 —— 校验端点绝不 500,一律给中文解释
        text = _scrub(f"{type(exc).__name__}: {exc}", key, resolved.api_key)
        return {"ok": False, "target": "llm", "infocode": "",
                "detail": f"[{resolved.label} · {resolved.model}] {text}"}
    return {
        "ok": True, "target": "llm", "infocode": "",
        "detail": (f"校验通过:[{resolved.label} · {resolved.model}] 回复"
                   f"「{(reply or '').strip()[:20] or '(空)'}」。"),
    }


@router.post("/config/validate")
def validate_config(
    payload: Any = Body(
        None,
        description='{"target": "amap|amap_js|llm", "value": "待校验的值(可空=用当前生效值)", '
                    'llm 另可带 "provider"/"base_url"/"model"}',
    ),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """向导每步的**真调校验**(恒 200,``ok`` 字段报成败;校验失败不是 HTTP 错误)。

    * ``target=amap`` —— 用给的(或已生效的)Web 服务 key 真调一次 ``geocode("北京")``;
    * ``target=amap_js`` —— 用 JS key 打同一次探针,**10009 平台不符 = 通过**(见函数注释);
    * ``target=llm`` —— 发一条 1-token 测试消息(provider/base_url/model 可临时覆盖)。

    QPS 口径(CONTAINER-SETUP-PLAN §6.5):每次校验**只发一次请求**,不做批量探活;
    高德的瞬时错误由 ``amap._request`` 内置退避重试(2s/5s),永久错误(key 无效)不重试。
    """
    body = _payload_dict(payload)
    target = (_optional_text(body.get("target")) or "").strip().lower()
    if target not in VALIDATE_TARGETS:
        raise HTTPException(
            400, f"未知的校验目标:{target or '(缺)'}(可选:{'、'.join(VALIDATE_TARGETS)})"
        )
    value = body.get("value")
    if value is None:
        value = ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise HTTPException(400, f"待校验的 value 必须是字符串,收到:{type(value).__name__}")
    text = str(value)
    if target == "amap":
        result = _validate_amap(session, text)
    elif target == "amap_js":
        result = _validate_amap_js(session, text)
    else:
        result = _validate_llm(session, body, text)
    result.setdefault("note", VALIDATE_NOTE)
    return result
