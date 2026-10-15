"""向导配置(TASK-P2):``AppConfig`` 表的**白名单读写** + 注入 ``os.environ``。

容器版没有 ``.env``:用户在 ``/setup`` 向导里填高德 / LLM 的 key,存进 SQLite
(:class:`db.models.AppConfig`)。而既有代码有 30 处直接 ``os.environ.get(...)``
(``data_sources/amap.py``、``services/intro.py`` …),逐处改成查库会把 DB 依赖渗进数据源层。
所以本模块走**反方向**(docs/CONTAINER-SETUP-PLAN.md §2 拍板):

* **启动时**(:func:`load_into_environ`,由 ``app/main.py`` 的 lifespan 调)把库里的值注入
  ``os.environ`` —— 但**只填「env 未设或为空串」的键**,所以容器 ``-e`` / ``.env`` 永远压过库;
* **保存时**(:func:`set_key`,由 ``POST /api/config`` 调)写库 + 同步 ``os.environ``,
  进程内**热生效** —— ``amap.resolve_key()`` 与 ``intro.resolve_provider()`` 都是每次调用现读 env,
  改完立刻生效、**不需要重启**(唯一例外是 ``db.base`` 的 engine 单例,所以
  ``WHERE2GO_DB_URL`` 故意**不在白名单里**);
* 那 30 处 env 读取点**一行不改**。

安全口径两条(照 ``services.intro.describe_llm`` 的「key 不出响应」先例):

1. **白名单**:可写的键就是 :data:`SCHEMA` 里那 7 个,白名单外一律
   :class:`ValueError`(API 层转 400)—— ``/api/config`` 绝不能变成任意 env 注入面;
2. **不回显明文**::func:`mask_value` 只给「前 2 + … + 后 2」的掩码,完整值永不进 API 响应、
   永不进日志(:func:`effective_value` 是**服务端内部**用的,拿它去算掩码 / 真调校验,
   不要直接返回给前端)。

本模块不触网:只读写 SQLite 与 ``os.environ``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AppConfig

# 分组(向导分步用):①高德三件套 ②LLM 四件套(可选)
GROUP_AMAP = "amap"
GROUP_LLM = "llm"
GROUPS: tuple[str, ...] = (GROUP_AMAP, GROUP_LLM)

# 来源标记:setup = 向导写进库的;env = 容器环境变量(优先级更高)
SOURCE_SETUP = "setup"
SOURCE_ENV = "env"

# 掩码口径:空 → 空串;短到没法「留头留尾」→ 全掩;否则前 2 + 省略号 + 后 2
MASK_ALL = "****"
MASK_HEAD = 2
MASK_TAIL = 2
MASK_ELLIPSIS = "…"

ENV_AMAP_KEY = "WHERE2GO_AMAP_KEY"
ENV_AMAP_JS_KEY = "WHERE2GO_AMAP_JS_KEY"
ENV_AMAP_SECURITY_JS_CODE = "WHERE2GO_AMAP_SECURITY_JS_CODE"
ENV_LLM_PROVIDER = "WHERE2GO_LLM_PROVIDER"
ENV_LLM_API_KEY = "WHERE2GO_LLM_API_KEY"
ENV_LLM_BASE_URL = "WHERE2GO_LLM_BASE_URL"
ENV_LLM_MODEL = "WHERE2GO_LLM_MODEL"


@dataclass(frozen=True)
class ConfigField:
    """一个**可被向导配置**的环境变量(白名单条目)。

    * ``key`` —— 环境变量名,同时也是 ``AppConfig.key``;
    * ``label`` / ``hint`` —— 向导里给人看的中文名与坑文案(``hint`` 要写清填错会报什么);
    * ``required`` —— 缺了它核心功能不可用(高德三件套),``/api/health`` 会列出来;
    * ``secret`` —— 是密钥:``GET /api/config`` 只回掩码,永不回显明文;
    * ``needs_restart`` —— 改完是否需要重启容器。**当前 7 个键全是 ``False``**
      (amap / intro 都每次调用现读 env);字段留着是给以后可能暴露的
      ``WHERE2GO_DB_URL`` 之类(engine 是进程内单例,必须重启);
    * ``group`` —— 向导分步归组(:data:`GROUP_AMAP` / :data:`GROUP_LLM`)。
    """

    key: str
    label: str
    hint: str
    required: bool = False
    secret: bool = False
    needs_restart: bool = False
    group: str = GROUP_AMAP


SCHEMA: tuple[ConfigField, ...] = (
    ConfigField(
        key=ENV_AMAP_KEY,
        label="高德 Web 服务 key",
        hint="高德开放平台 → 应用管理 → 添加 Key,服务平台选「Web服务」;"
             "后端 POI 检索 / 地理编码 / 驾车路线全靠它。填成 JS key 会报 "
             "USERKEY_PLAT_NOMATCH(infocode 10009);日配额用尽报 10003。",
        required=True,
        secret=True,
        group=GROUP_AMAP,
    ),
    ConfigField(
        key=ENV_AMAP_JS_KEY,
        label="高德 Web 端(JS API)key",
        hint="前端地图用的 key,申请时服务平台选「Web端(JS API)」。与「Web服务」key "
             "**平台隔离、不能混用**:拿它调后端 REST 会报 USERKEY_PLAT_NOMATCH(infocode 10009)。",
        required=True,
        secret=True,
        group=GROUP_AMAP,
    ),
    ConfigField(
        key=ENV_AMAP_SECURITY_JS_CODE,
        label="高德 JS 安全密钥(securityJsCode)",
        hint="2021-12 之后申请的 JS API key 必须配套安全密钥,且要在加载地图脚本**之前**写好 "
             "window._AMapSecurityConfig;缺失或不匹配 → 地图整块空白并报 INVALID_USER_SCODE。",
        required=True,
        secret=True,
        group=GROUP_AMAP,
    ),
    ConfigField(
        key=ENV_LLM_PROVIDER,
        label="LLM 服务商(可选)",
        hint="留空则自动挑注册表里第一个配了 key 的服务商;也可显式写 deepseek / qwen,"
             "或用 WHERE2GO_LLM_BASE_URL + MODEL 指向任意 OpenAI 兼容服务。",
        group=GROUP_LLM,
    ),
    ConfigField(
        key=ENV_LLM_API_KEY,
        label="LLM API key(可选)",
        hint="不配也能用:POI 简介 / AI 行程会降级为空,地图与检索照常。"
             "任何 OpenAI 兼容 /chat/completions 的 key 都行。",
        secret=True,
        group=GROUP_LLM,
    ),
    ConfigField(
        key=ENV_LLM_BASE_URL,
        label="LLM 接口地址(可选)",
        hint="OpenAI 兼容服务的根地址,如 https://api.deepseek.com;"
             "后端自己拼 /chat/completions,末尾斜杠会被去掉。",
        group=GROUP_LLM,
    ),
    ConfigField(
        key=ENV_LLM_MODEL,
        label="LLM 模型名(可选)",
        hint="如 deepseek-chat / qwen-plus;留空用该服务商的默认模型。",
        group=GROUP_LLM,
    ),
)

FIELDS_BY_KEY: dict[str, ConfigField] = {field.key: field for field in SCHEMA}
ALLOWED_KEYS: tuple[str, ...] = tuple(field.key for field in SCHEMA)
REQUIRED_KEYS: tuple[str, ...] = tuple(field.key for field in SCHEMA if field.required)


def iter_fields() -> tuple[ConfigField, ...]:
    """白名单全量(顺序 = 向导展示顺序:高德三件套 → LLM 四件套)。"""
    return SCHEMA


def get_field(key: Any) -> Optional[ConfigField]:
    """按环境变量名取白名单条目;不在白名单里返回 ``None``。"""
    return FIELDS_BY_KEY.get(_clean(key))


def is_allowed(key: Any) -> bool:
    """这个键**允许被向导写入**吗?(白名单外的 env 只读,不给写)"""
    return _clean(key) in FIELDS_BY_KEY


def load_into_environ(session: Session) -> list[str]:
    """把库里的配置注入 ``os.environ``,返回**实际注入的键名**列表。

    口径三条:

    1. **env 优先**:`os.environ` 里已有非空值的键**绝不覆盖** —— 容器 ``-e`` / ``.env``
       始终压过向导存的值(空串视为未设,与 :func:`effective_source` 同一口径);
    2. **只认白名单**:表里若有白名单外的行(手工塞的),**忽略不注入** —— 本表不是
       任意 env 的注入面;
    3. 库里空值的行跳过(等于没配)。

    启动时由 ``app/main.py`` 的 lifespan 调一次;多 worker 下每个进程各自调
    (容器固定 ``--workers 1``,见 CONTAINER-SETUP-PLAN §2)。
    """
    stored = {
        _clean(row.key): _clean(row.value) for row in session.scalars(select(AppConfig))
    }
    injected: list[str] = []
    for field in SCHEMA:
        value = stored.get(field.key, "")
        if not value:
            continue
        if env_value(field.key):
            continue
        os.environ[field.key] = value
        injected.append(field.key)
    return injected


def set_key(session: Session, key: Any, value: Any) -> None:
    """写一个配置项(白名单 + 幂等 upsert)+ 同步 ``os.environ``(**热生效**)。

    * 白名单外 → :class:`ValueError`(API 层转 400 中文);
    * 值去空白后为空 = **清除**:删掉库里这一行,并把 ``os.environ[key]`` 置成**空串**
      (故意**不 pop**:容器 env 是外部给的,库里删行只代表「向导配置已清除」,
      置空串与 :func:`load_into_environ` 的「空串视为未设」口径一致);
    * 非空 = upsert 一行(``source="setup"``)+ ``os.environ[key]=值``,
      ``amap.resolve_key()`` / ``intro.resolve_provider()`` 立刻看到新值,无需重启。

    写完就 ``commit``(向导一次保存一项,提交语义比"攒着"更直观);提交失败时
    **不动** ``os.environ``,免得库里没存下、进程里却生效了。
    """
    name = _clean(key)
    if not is_allowed(name):
        raise ValueError(
            f"不允许配置的配置项:{key!r}(可配置:{'、'.join(ALLOWED_KEYS)})"
        )
    text = _clean(value)
    row = session.scalars(select(AppConfig).where(AppConfig.key == name)).first()
    if not text:
        if row is not None:
            session.delete(row)
        session.commit()
        os.environ[name] = ""
        return
    if row is None:
        session.add(AppConfig(key=name, value=text, source=SOURCE_SETUP))
    else:
        row.value = text
        row.source = SOURCE_SETUP
    session.commit()
    os.environ[name] = text


def env_value(key: Any) -> str:
    """``os.environ`` 里这个键的值(去空白);没设或空串都返回 ``""``。"""
    return _clean(os.environ.get(_clean(key)))


def stored_value(session: Session, key: Any) -> str:
    """库里(``AppConfig``)这个键的值(去空白);没有行或空值返回 ``""``。"""
    name = _clean(key)
    row = session.scalars(select(AppConfig).where(AppConfig.key == name)).first()
    return _clean(row.value) if row is not None else ""


def effective_value(session: Session, key: Any) -> str:
    """当前**实际生效**的值:env 优先,回落到库。

    ⚠️ 内部用(算掩码、真调数据源校验):**绝不可**原样进 API 响应或日志。
    """
    return env_value(key) or stored_value(session, key)


def effective_source(session: Session, key: Any) -> str:
    """当前生效值的**来源标记**:``"env"`` / ``"setup"`` / ``""``(都没配)。

    ``os.environ`` 非空 → ``env``(容器给的,压过库);否则库里非空 → ``setup``;
    都空 → 空串(= 未配置)。向导横幅与 ``/api/health`` 据此判断"缺哪些必填项"。
    """
    if env_value(key):
        return SOURCE_ENV
    if stored_value(session, key):
        return SOURCE_SETUP
    return ""


def is_configured(session: Session, key: Any) -> bool:
    """这个键现在**配好了**吗(不管来自 env 还是向导)。"""
    return bool(effective_source(session, key))


def display_source(session: Session, key: Any) -> str:
    """给向导 / ``GET /api/config`` 展示的**来源标记**:``env`` / ``setup`` / ``""``。

    与 :func:`effective_source` 的差别只在一处判据,却很重要:
    :func:`set_key` 与 :func:`load_into_environ` 都会把**库里的值同步进 ``os.environ``**
    (热生效),于是一个明明是向导存下来的值,按「env 非空即 env」的严格口径会被报成
    ``env`` —— 向导就会误告「这项由容器 env 决定、在这儿改不动」,而且**每次重启后都这样**。
    所以这里加一条:env 的值与库里的值**相同** → 认定它来自库(``setup``);
    不同才是容器 env 压过来的值(``env``)。**优先级本身没变**
    (env 给了不同的值时仍然 env 生效),只是把"谁给的"报准。
    """
    effective = effective_value(session, key)
    if not effective:
        return ""
    stored = stored_value(session, key)
    if stored and stored == effective:
        return SOURCE_SETUP
    return effective_source(session, key)


def missing_required(session: Session) -> list[str]:
    """必填项里**还没配**的键名列表(顺序同 :data:`SCHEMA`);空列表 = 全配齐。"""
    return [field.key for field in SCHEMA if field.required and not is_configured(session, field.key)]


def mask_value(value: Any) -> str:
    """把值打成掩码:空 → ``""``;长度 ≤ 4 → ``"****"``;否则 ``前2 + … + 后2``。

    掩码是**不可逆**的,给用户"确认自己填的是哪个 key"用(``ab…yz``),
    完整值永不进 API 响应与日志(照 ``services.intro.describe_llm`` 口径)。
    """
    text = _clean(value)
    if not text:
        return ""
    if len(text) <= MASK_HEAD + MASK_TAIL:
        return MASK_ALL
    return f"{text[:MASK_HEAD]}{MASK_ELLIPSIS}{text[-MASK_TAIL:]}"


def masked_effective(session: Session, key: Any) -> str:
    """当前生效值的**掩码**(``GET /api/config`` 用这个,不碰明文)。"""
    return mask_value(effective_value(session, key))


def _clean(value: Any) -> str:
    """归一文本:``None`` → 空串,其余 ``str()`` 后去首尾空白(与 ``models.clean_text`` 同口径)。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()
