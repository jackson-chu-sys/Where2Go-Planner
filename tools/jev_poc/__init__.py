"""Jev 范式 PoC:工具结果进上下文前先过廉价裁判(TASK-JEV1)。

模块:

- :mod:`tools.jev_poc.splitter` —— 把日志/工具输出按 ~25 行分块(winnow 式),
  带 stub 存储(restore key 可逆还原原文,绝不丢数据)。
- :mod:`tools.jev_poc.judge` —— 廉价 LLM 裁判:OpenAI 兼容 ``POST /v1/chat/completions``
  单次调用批量问「当前任务还需要这块吗」;任何失败(坏 JSON/超时/限流/无 key)
  一律降级为**保留**。

设计记录见同目录 README.md。本包只依赖标准库 + requests(与 backend 一致)。
"""

from .splitter import Chunk, StubStore, split_text  # noqa: F401
from .judge import JudgeClient, JudgeResult, Verdict  # noqa: F401

__all__ = [
    "Chunk",
    "StubStore",
    "split_text",
    "JudgeClient",
    "JudgeResult",
    "Verdict",
]
