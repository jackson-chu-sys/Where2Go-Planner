"""LLM 一句话简介:Provider 抽象(ADR-002)+ 按 POI 缓存到 ``Place.intro``。

设计要点(docs/STAGE1-PLAN.md 第 3 节"简介生成"、02-项目计划与架构.md ADR-002):

* **Provider 可切换**::data:`PROVIDERS` 是一张注册表(DeepSeek / 阿里 Qwen 兼容模式),
  统一走 OpenAI 兼容的 ``POST {base_url}/chat/completions``;换供应商只改注册表或
  环境变量(``WHERE2GO_LLM_PROVIDER`` / ``WHERE2GO_LLM_BASE_URL`` / ``WHERE2GO_LLM_MODEL`` /
  ``WHERE2GO_LLM_API_KEY``),调用方代码不动。key **只从环境变量读**,不落盘、不入库、不进日志。
* **按 POI 缓存**:DB 就是缓存 —— :func:`fill_missing_intros` 只取 ``intro`` 为空的行
  (见 :func:`db.repository.select_places`),已有简介的 POI **绝不重复调用 LLM**;
  重新抓取也不会覆盖已生成的简介(:func:`db.repository.upsert_places` 已保证)。
* **失败降级**:LLM 未配置 key、超时、限流、返回格式异常,一律降级成"空简介"
  (:func:`generate_intro` 返回 ``""`),**不抛异常、不阻塞入库**;下一轮再补。
* 文案口径:一句话中文简介(≤ :data:`MAX_INTRO_CHARS` 字),只依据名称/分类/OSM 标签,
  不编造票价、营业时间等事实字段(架构文档"AI 幻觉"对策:事实字段绑定结构化来源)。

CLI(给夜间预抓用,联网)::

    python -m services.intro 上海 50_100 --limit 40      # 只补缺简介的前 40 条
    python -m services.intro 上海 --workers 6            # 该城市全部 band
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from data_sources import DataSourceError
from data_sources._common import USER_AGENT, build_session, http_json
from db import repository as repo
from services.bands import find_band

SOURCE_NAME = "LLM"
CHAT_PATH = "/chat/completions"

ENV_PROVIDER = "WHERE2GO_LLM_PROVIDER"
ENV_API_KEY = "WHERE2GO_LLM_API_KEY"
ENV_BASE_URL = "WHERE2GO_LLM_BASE_URL"
ENV_MODEL = "WHERE2GO_LLM_MODEL"

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_TOKENS = 120
DEFAULT_TEMPERATURE = 0.35
DEFAULT_WORKERS = 4
MAX_WORKERS = 8
MAX_INTRO_CHARS = 60
INTRO_TARGET_CHARS = 40
FACT_LIMIT = 8

SYSTEM_PROMPT = (
    "你是 Where2Go(周末去哪儿玩)的目的地文案助手,为地图 popup 写一句话中文简介。"
    f"要求:1)只输出一句话,不超过 {INTRO_TARGET_CHARS} 个汉字,不要引号、标题、编号或解释;"
    "2)只能依据给出的名称、分类与 OSM 标签,不得编造票价、营业时间、具体数字;"
    "3)信息不足时写符合该分类的通用描述,不要提及 OSM、标签、数据源或模型。"
)

# 分类 → 文案侧重(STAGE1-PLAN 第3节表格里的"简介/卡片字段")
CATEGORY_HINTS: dict[str, str] = {
    "自然风光": "突出景观类型与看点(山/湖/瀑布/公园/岛屿),有海拔等标签可自然带出。",
    "小城人文美食": "突出人文背景、古镇/街区气质或特色美食,体现烟火气。",
    "滑雪场": "突出雪场与雪道特点,有雪道类型/难度标签可带出。",
    "运动": "突出可做的运动项目与场地类型、适宜人群。",
    "其他": "客观描述该地点的类型与去处气质。",
}

# 进 prompt 的标签白名单(按此顺序,最多 FACT_LIMIT 个):给事实线索,又不把整包 tag 塞进去
FACT_TAGS: tuple[str, ...] = (
    "natural", "ele", "height", "waterway", "tourism", "historic", "amenity", "cuisine",
    "sport", "leisure", "piste:type", "piste:difficulty", "place", "landuse", "water",
    "denomination", "start_date", "architect", "website", "opening_hours", "fee",
)

IntroGenerator = Callable[[Mapping[str, Any]], str]


@dataclass(frozen=True)
class LLMProvider:
    """一个可切换的 LLM 供应商(OpenAI 兼容 chat/completions)。"""

    name: str
    label: str
    base_url: str
    model: str
    env_api_keys: tuple[str, ...]


# ADR-002:现状用既有 Qwen(百炼 token-plan)/ DeepSeek key;产品化后可在此追加(或走环境变量覆盖)。
# 默认 = qwen(百炼个人 TOKEN 的 token-plan 入口,22:00-08:00 半价,神朱 2026-09-10 拍板):
# 注意必须用 token-plan 入口的 compatible-mode,而非 dashscope 官方入口(后者对该 key 返回 401)。
PROVIDERS: tuple[LLMProvider, ...] = (
    LLMProvider(
        name="qwen",
        label="阿里 Qwen(百炼 token-plan 兼容模式)",
        base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        model="qwen3.8-max",
        env_api_keys=("ALIBABA_TOKEN_PLAN_API_KEY", "DASHSCOPE_API_KEY", "QWEN_API_KEY"),
    ),
    LLMProvider(
        name="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        env_api_keys=("DEEPSEEK_API_KEY",),
    ),
)


@dataclass(frozen=True)
class ResolvedLLM:
    """实际生效的 Provider + key(只在本对象内持有 key,不外泄到日志/接口)。"""

    provider: str
    label: str
    base_url: str
    model: str
    api_key: str
    key_env: str

    @property
    def chat_url(self) -> str:
        """chat/completions 端点。"""
        return self.base_url.rstrip("/") + CHAT_PATH


def find_provider(name: Optional[str]) -> Optional[LLMProvider]:
    """按名字找 Provider(大小写/别名宽容);找不到返回 None。"""
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    return next(
        (item for item in PROVIDERS if wanted in (item.name, item.label.lower())),
        None,
    )


def resolve_provider(environ: Optional[Mapping[str, str]] = None) -> Optional[ResolvedLLM]:
    """决定用哪个 Provider:环境变量显式覆盖 > 注册表里第一个配了 key 的。

    没有任何可用 key 时返回 ``None`` —— 上层据此**跳过** LLM 调用(空简介降级)。
    """
    env = dict(os.environ if environ is None else environ)
    override_key = (env.get(ENV_API_KEY) or "").strip()
    wanted_name = (env.get(ENV_PROVIDER) or "").strip()
    base_provider = find_provider(wanted_name) or (PROVIDERS[0] if wanted_name else None)

    if override_key:
        provider = base_provider or PROVIDERS[0]
        return ResolvedLLM(
            provider=wanted_name or provider.name,
            label=(env.get(ENV_PROVIDER) or provider.label).strip() or provider.label,
            base_url=(env.get(ENV_BASE_URL) or provider.base_url).strip().rstrip("/"),
            model=(env.get(ENV_MODEL) or provider.model).strip(),
            api_key=override_key,
            key_env=ENV_API_KEY,
        )

    for provider in PROVIDERS:
        for key_env in provider.env_api_keys:
            api_key = (env.get(key_env) or "").strip()
            if api_key:
                return ResolvedLLM(
                    provider=provider.name,
                    label=provider.label,
                    base_url=(env.get(ENV_BASE_URL) or provider.base_url).strip().rstrip("/"),
                    model=(env.get(ENV_MODEL) or provider.model).strip(),
                    api_key=api_key,
                    key_env=key_env,
                )
    return None


def describe_llm(environ: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """给 ``/api/places/meta`` 用的 LLM 说明(**不含 key**,只报 key 来自哪个环境变量)。"""
    resolved = resolve_provider(environ)
    available = [
        {"name": item.name, "label": item.label, "base_url": item.base_url,
         "model": item.model, "key_env": list(item.env_api_keys)}
        for item in PROVIDERS
    ]
    if resolved is None:
        return {
            "enabled": False,
            "provider": None, "label": None, "base_url": None, "model": None, "key_env": None,
            "providers": available,
            "note": "环境里没有可用的 LLM key,简介会留空(不影响入库与地图);"
                    f"配置 {'/'.join(PROVIDERS[0].env_api_keys)} 或 {ENV_API_KEY} 后重试。",
        }
    return {
        "enabled": True,
        "provider": resolved.provider,
        "label": resolved.label,
        "base_url": resolved.base_url,
        "model": resolved.model,
        "key_env": resolved.key_env,
        "providers": available,
        "note": "简介按 POI 缓存在 Place.intro,已有简介不再调用;失败降级为空简介。",
    }


class LLMClient:
    """OpenAI 兼容 chat/completions 客户端(复用数据源层的 HTTP 封装与中文错误)。"""

    def __init__(
        self,
        resolved: Optional[ResolvedLLM] = None,
        *,
        environ: Optional[Mapping[str, str]] = None,
        session: Optional[Any] = None,
        timeout: Optional[float] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> None:
        self.resolved = resolved if resolved is not None else resolve_provider(environ)
        self.timeout = DEFAULT_TIMEOUT_S if timeout is None else float(timeout)
        self.max_tokens = max(16, int(max_tokens))
        self.temperature = float(temperature)
        self._session = session if session is not None else build_session(USER_AGENT)

    @property
    def enabled(self) -> bool:
        """是否拿到了可用 key(没有就直接降级,不发请求)。"""
        return self.resolved is not None

    @property
    def label(self) -> str:
        """人类可读的供应商名(状态栏/日志用)。"""
        if self.resolved is None:
            return "未配置"
        return f"{self.resolved.label} · {self.resolved.model}"

    def chat(self, prompt: str, *, system: str = SYSTEM_PROMPT) -> str:
        """发一次对话补全,返回文本;失败抛 :class:`data_sources.DataSourceError`。"""
        resolved = self.resolved
        if resolved is None:
            raise DataSourceError(
                SOURCE_NAME,
                f"未配置 LLM key(需要 {' / '.join(PROVIDERS[0].env_api_keys)} 或 {ENV_API_KEY})",
            )
        payload = {
            "model": resolved.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
        data = http_json(
            self._session,
            resolved.chat_url,
            source=f"{SOURCE_NAME}/{resolved.label}",
            method="POST",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=self.timeout,
            headers={
                "Authorization": f"Bearer {resolved.api_key}",
                "Content-Type": "application/json",
            },
        )
        return extract_completion(data)


def extract_completion(payload: Any) -> str:
    """从 OpenAI 兼容响应里取 ``choices[0].message.content``;格式不符抛错(中文说明)。"""
    if not isinstance(payload, dict):
        raise DataSourceError(SOURCE_NAME, f"响应不是 JSON 对象:{type(payload).__name__}")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        # 供应商侧错误(余额/限流/模型名)通常带 error.message
        error = payload.get("error")
        detail = error.get("message") if isinstance(error, dict) else None
        raise DataSourceError(SOURCE_NAME, f"响应里没有 choices({detail or '格式与 OpenAI 兼容协议不符'})")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise DataSourceError(SOURCE_NAME, "响应里的 message.content 为空")
    return content


_default_client: Optional[LLMClient] = None


def default_client() -> LLMClient:
    """进程内共享的默认客户端(按环境变量自动选 Provider)。"""
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client


# --------------------------------------------------------------------------- #
# 简介文案
# --------------------------------------------------------------------------- #


def tag_facts(tags: Optional[Mapping[str, Any]], *, limit: int = FACT_LIMIT) -> str:
    """把 OSM 标签里对文案有用的部分摘成 ``k=v; k=v``(白名单 + 上限,避免塞整包 tag)。"""
    normalized = {str(key).strip().lower(): str(value).strip() for key, value in dict(tags or {}).items()}
    facts: list[str] = []
    for key in FACT_TAGS:
        value = normalized.get(key)
        if value and value.lower() not in ("no", "none", "yes"):
            facts.append(f"{key}={value}")
        if len(facts) >= limit:
            break
    return "; ".join(facts)


def build_intro_prompt(place: Mapping[str, Any]) -> str:
    """按 POI 拼 prompt:名称 + 分类(含产品含义)+ 位置 + OSM 标签事实。"""
    name = str(place.get("name") or "").strip() or "(无名)"
    category = str(place.get("category") or "其他").strip()
    hint = CATEGORY_HINTS.get(category, CATEGORY_HINTS["其他"])
    lines = [f"名称:{name}", f"分类:{category}", f"文案侧重:{hint}"]
    city = str(place.get("origin_city") or "").strip()
    band = find_band(place.get("band"))
    where = " · ".join(part for part in (f"起点城市:{city}" if city else "",
                                         f"距离分段:{band['label']}" if band else "") if part)
    if where:
        lines.append(where)
    facts = tag_facts(place.get("tags"))
    if facts:
        lines.append(f"OSM 标签:{facts}")
    lines.append("请写一句话中文简介。")
    return "\n".join(lines)


def clean_intro(text: Optional[str], *, max_chars: int = MAX_INTRO_CHARS) -> str:
    """LLM 输出清洗:压成一行、去掉包裹引号与多余前缀,超长截断并补句号。"""
    cleaned = " ".join(str(text or "").split())
    for prefix in ("简介:", "简介:", "一句话简介:", "答:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    cleaned = cleaned.strip("\"'“”‘’「」『』《》 ")
    if not cleaned:
        return ""
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip(",,;;、、  ")
    if cleaned and cleaned[-1] not in "。!?!?":
        cleaned += "。"
    return cleaned


def generate_intro(
    place: Mapping[str, Any],
    *,
    client: Optional[LLMClient] = None,
    max_chars: int = MAX_INTRO_CHARS,
) -> str:
    """给一个 POI 生成一句话简介;**任何失败都返回空串**(降级,不阻塞入库)。"""
    llm = client if client is not None else default_client()
    if not llm.enabled:
        return ""
    if not str(place.get("name") or "").strip():
        return ""
    try:
        raw = llm.chat(build_intro_prompt(place))
    except DataSourceError:
        return ""
    except Exception:  # noqa: BLE001 - 简介是增强项,任何异常都不得阻塞入库
        return ""
    return clean_intro(raw, max_chars=max_chars)


def fill_missing_intros(
    session: Session,
    *,
    origin_city: Optional[str] = None,
    band: Optional[str] = None,
    category: Optional[str] = None,
    limit: Optional[int] = None,
    client: Optional[LLMClient] = None,
    generator: Optional[IntroGenerator] = None,
    workers: int = DEFAULT_WORKERS,
    commit: bool = True,
) -> dict[str, Any]:
    """给库里**还没有简介**的 POI 批量补一句话简介(DB 即缓存,已有的不重复调用)。

    返回统计 ``{"scanned", "filled", "failed", "pending", "provider"}``;``failed`` 是
    降级成空简介的条数(网络/额度/格式问题),下次调用会自然重试。
    并发只用于 LLM 请求,DB 写入仍在调用线程完成(SQLAlchemy Session 非线程安全)。
    """
    rows = repo.select_places(
        session,
        origin_city=origin_city,
        band=band,
        category=category,
        missing_intro=True,
        limit=limit,
    )
    llm = client if client is not None else default_client()
    provider = llm.label if llm.enabled else "未配置"
    pending = repo.count_places(
        session, origin_city=origin_city, band=band, category=category, missing_intro=True
    )
    if not rows:
        return {"scanned": 0, "filled": 0, "failed": 0, "pending": pending, "provider": provider}

    produce = generator or (lambda place: generate_intro(place, client=llm))
    payloads = [
        {
            "id": row.id,
            "name": row.name,
            "category": row.category,
            "tags": dict(row.tags or {}),
            "origin_city": row.origin_city,
            "band": row.band,
        }
        for row in rows
    ]
    texts = _run_batch(produce, payloads, workers=workers)

    filled = 0
    for row, text in zip(rows, texts):
        cleaned = clean_intro(text)
        if not cleaned:
            continue
        row.intro = cleaned
        filled += 1
    if filled and commit:
        session.commit()
    return {
        "scanned": len(rows),
        "filled": filled,
        "failed": len(rows) - filled,
        "pending": max(0, pending - filled),
        "provider": provider,
    }


def _run_batch(produce: IntroGenerator, payloads: Sequence[Mapping[str, Any]], *, workers: int) -> list[str]:
    """并发跑 LLM(单条顺序时直接调用,便于测试与排错)。"""
    if workers <= 1 or len(payloads) <= 1:
        return [str(produce(payload) or "") for payload in payloads]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, workers)) as pool:
        return [str(text or "") for text in pool.map(produce, payloads)]


def main(argv: Optional[list[str]] = None) -> int:
    """CLI:给已入库 POI 补简介。``python -m services.intro 上海 50_100 --limit 40``"""
    parser = argparse.ArgumentParser(description="为库里缺简介的 POI 生成 LLM 一句话简介(按 POI 缓存)")
    parser.add_argument("city", nargs="?", default=None, help="起点城市名(默认:全部城市)")
    parser.add_argument("band", nargs="?", default=None, help="距离分段 key(默认:该城市全部分段)")
    parser.add_argument("--category", default=None, help="只补某个分类")
    parser.add_argument("--limit", type=int, default=None, help="最多补多少条(默认:不限)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"并发数(默认 {DEFAULT_WORKERS})")
    parser.add_argument("--db", default=None, help="数据库 URL(默认 WHERE2GO_DB_URL 或 backend/data/where2go.db)")
    parser.add_argument("--dry-run", action="store_true", help="只报缺简介的条数,不调 LLM")
    args = parser.parse_args(argv)

    from db import init_db, make_engine, open_session

    engine = make_engine(args.db)
    init_db(engine)
    with open_session(engine) as session:
        if args.dry_run:
            pending = repo.count_places(
                session, origin_city=args.city, band=args.band, missing_intro=True
            )
            total = repo.count_places(session, origin_city=args.city, band=args.band)
            print(f"[待补简介] {pending} / 库内 {total} 条 · LLM:{describe_llm()['label'] or '未配置'}")
            return 0
        stats = fill_missing_intros(
            session,
            origin_city=args.city,
            band=args.band,
            category=args.category,
            limit=args.limit,
            workers=args.workers,
        )
    print(
        f"[完成] 生成 {stats['filled']} 条简介 · 降级 {stats['failed']} 条 · "
        f"仍缺 {stats['pending']} 条 · Provider:{stats['provider']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
