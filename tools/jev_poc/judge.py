"""廉价裁判:批量判定「这块工具输出当前任务还需要吗」。

范式(Jev/winnow):大工具输出先分块(splitter.py),把每块交给一个**廉价小模型**
做二元判定(keep/drop + 置信度);被 drop 的块不进上下文,只留 stub,restore key
可随时找回原文。裁判自身调用成本计入成本统计(JEV2 replay 对照)。

铁律:**任何失败一律降级为保留(keep)** —— 坏 JSON、超时、限流、无 key、网络错误
都不得导致数据被丢。drop 只能来自裁判明确的高置信判定。

Provider 复用 backend/services/intro.py 的注册表口径(qwen token-plan 优先、
deepseek 备选),但本模块**不 import backend**,保持 tools/ 独立、只用 requests。
key 只从环境变量读,不落盘/不打日志。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

import requests

from .splitter import Chunk

CHAT_PATH = "/chat/completions"
DEFAULT_TIMEOUT_S = 60.0

# 与 intro.py 同口径的 Provider 注册表(独立副本,tools/ 不依赖 backend)
PROVIDERS: tuple[dict, ...] = (
    {
        "name": "qwen",
        "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.8-max",
        "env_api_keys": ("ALIBABA_TOKEN_PLAN_API_KEY", "DASHSCOPE_API_KEY", "QWEN_API_KEY"),
    },
    {
        "name": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "env_api_keys": ("DEEPSEEK_API_KEY",),
    },
)

# 每千 token 价格(元),用于成本统计;PoC 口径,可随价格页更新
PRICE_PER_1K: Dict[str, Dict[str, float]] = {
    "qwen": {"input": 0.0012, "output": 0.0048},   # token-plan 半价窗近似
    "deepseek": {"input": 0.001, "output": 0.002},
}

SYSTEM_PROMPT = (
    "你是日志分块裁判。给定当前任务焦点和若干带编号的日志块,判断每块对该任务是否仍然需要。"
    "只输出 JSON 数组,形如 [{\"id\":0,\"keep\":true,\"confidence\":0.9},...],"
    "id 与输入块编号一一对应,不要输出其他内容。"
)

_DROP_MIN_CONFIDENCE = 0.75  # 低于此置信度即使 keep=false 也保留


@dataclass(frozen=True)
class Verdict:
    """单块裁决。``degraded=True`` 表示裁判失败降级为保留。"""

    keep: bool
    confidence: float
    reason: str = ""
    degraded: bool = False


@dataclass
class JudgeResult:
    """一次批量裁判的结果与成本统计。"""

    verdicts: List[Verdict] = field(default_factory=list)
    model: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    degraded_all: bool = False
    error: Optional[str] = None

    @property
    def kept(self) -> int:
        return sum(1 for v in self.verdicts if v.keep)

    @property
    def dropped(self) -> int:
        return sum(1 for v in self.verdicts if not v.keep)

    def cost_cny(self) -> float:
        """裁判自身消耗(元),按 PRICE_PER_1K 折算。"""
        provider = (self.model or "").lower()
        price = PRICE_PER_1K.get("qwen" if "qwen" in provider else "deepseek", PRICE_PER_1K["qwen"])
        return (
            self.prompt_tokens / 1000.0 * price["input"]
            + self.completion_tokens / 1000.0 * price["output"]
        )


def resolve_llm(environ: Optional[Mapping[str, str]] = None) -> Optional[Dict[str, str]]:
    """按注册表挑第一个有 key 的 provider(可用 WHERE2GO_LLM_PROVIDER 指定)。"""
    env = dict(os.environ if environ is None else environ)
    wanted = (env.get("WHERE2GO_LLM_PROVIDER") or "").strip().lower()
    ordered = PROVIDERS if not wanted else tuple(p for p in PROVIDERS if p["name"] == wanted) or PROVIDERS
    for provider in ordered:
        for key_env in provider["env_api_keys"]:
            key = (env.get(key_env) or "").strip()
            if key:
                return {
                    "name": provider["name"],
                    "base_url": env.get("WHERE2GO_LLM_BASE_URL") or provider["base_url"],
                    "model": env.get("WHERE2GO_LLM_MODEL") or provider["model"],
                    "api_key": key,
                }
    return None


def _all_keep(n: int, error: str) -> JudgeResult:
    """降级:全部保留。"""
    return JudgeResult(
        verdicts=[Verdict(keep=True, confidence=0.0, reason="degraded", degraded=True) for _ in range(n)],
        degraded_all=True,
        error=error,
    )


def parse_verdicts(raw: str, n_chunks: int, ids: Optional[Sequence[int]] = None) -> Optional[List[Verdict]]:
    """解析裁判回复。健壮性:剥 code fence、抓首个 JSON 数组、宽容单对象;
    ``ids`` 为各块的真实编号(分批调用时非 0 基),缺省按 0..n-1;
    解析不出 n_chunks 个有效条目 -> None(调用方降级)。"""
    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list) or len(data) < n_chunks:
        return None
    verdicts: List[Verdict] = []
    by_id: Dict[int, Any] = {}
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            by_id[item["id"]] = item
    expected_ids = list(range(n_chunks)) if ids is None else list(ids)
    for i in expected_ids:
        item = by_id.get(i)
        if not isinstance(item, dict) or not isinstance(item.get("keep"), bool):
            return None
        try:
            confidence = float(item.get("confidence", 1.0 if item["keep"] else 0.0))
        except (TypeError, ValueError):
            confidence = 1.0 if item["keep"] else 0.0
        confidence = max(0.0, min(1.0, confidence))
        keep = item["keep"] or confidence < _DROP_MIN_CONFIDENCE  # 低置信 drop -> 保留
        verdicts.append(Verdict(keep=keep, confidence=confidence, reason=str(item.get("reason", ""))[:200]))
    return verdicts


def build_user_prompt(task_focus: str, chunks: Sequence[Chunk], *, max_chars_per_chunk: int = 1600) -> str:
    """组装批量裁判 prompt:任务焦点 + 各块(截断防超长行反噬裁判窗口)。"""
    parts = [f"当前任务焦点:{task_focus}", f"共 {len(chunks)} 个日志块:", ""]
    for chunk in chunks:
        body = chunk.text
        if len(body) > max_chars_per_chunk:
            body = body[:max_chars_per_chunk] + "\n…[截断]"
        parts.append(f"=== 块 id={chunk.index} (lines {chunk.start_line}-{chunk.end_line}) ===")
        parts.append(body)
    return "\n".join(parts)


class JudgeClient:
    """裁判客户端:一次 HTTP 调用批量裁决;所有异常路径 -> 全部保留。"""

    def __init__(
        self,
        *,
        timeout: Optional[float] = None,
        environ: Optional[Mapping[str, str]] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.timeout = DEFAULT_TIMEOUT_S if timeout is None else float(timeout)
        self.environ = dict(os.environ if environ is None else environ)
        self.session = session or requests.Session()

    def judge(self, task_focus: str, chunks: Sequence[Chunk], *, max_chars_per_chunk: int = 1600) -> JudgeResult:
        if not chunks:
            return JudgeResult(verdicts=[])
        resolved = resolve_llm(self.environ)
        if resolved is None:
            return _all_keep(len(chunks), "未配置 LLM key(降级为全部保留)")
        payload = {
            "model": resolved["model"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(task_focus, chunks, max_chars_per_chunk=max_chars_per_chunk)},
            ],
            "temperature": 0,
        }
        try:
            response = self.session.post(
                resolved["base_url"].rstrip("/") + CHAT_PATH,
                json=payload,
                headers={"Authorization": f"Bearer {resolved['api_key']}"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001 —— 任何失败(超时/限流/网络/坏响应)都降级
            return _all_keep(len(chunks), f"裁判调用失败: {type(exc).__name__}(降级为全部保留)")
        usage = data.get("usage") or {}
        content = ""
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            pass
        verdicts = parse_verdicts(content, len(chunks), ids=[c.index for c in chunks])
        result = JudgeResult(
            model=resolved["model"],
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
        if verdicts is None:
            degraded = _all_keep(len(chunks), "裁判回复解析失败(降级为全部保留)")
            degraded.model = result.model
            degraded.prompt_tokens = result.prompt_tokens
            degraded.completion_tokens = result.completion_tokens
            return degraded
        result.verdicts = verdicts
        return result


def estimate_screening_savings(
    chunks: Sequence[Chunk], verdicts: Sequence[Verdict]
) -> Dict[str, Any]:
    """成本统计:筛前 vs 筛后 token 估算(同口径 approx_tokens)。

    筛后 = 保留块全文 + 被 drop 块的 stub 行(stub 也在上下文里占位)。
    """
    before = sum(c.approx_tokens for c in chunks)
    after = sum(
        c.approx_tokens if v.keep else max(1, -(-len(f"[jev-stub {c.key} lines={c.start_line}-{c.end_line}]") // 3))
        for c, v in zip(chunks, verdicts)
    )
    return {
        "chunks": len(chunks),
        "kept": sum(1 for v in verdicts if v.keep),
        "dropped": sum(1 for v in verdicts if not v.keep),
        "tokens_before": before,
        "tokens_after": after,
        "saved_tokens": before - after,
        "saved_ratio": round((before - after) / before, 4) if before else 0.0,
    }
