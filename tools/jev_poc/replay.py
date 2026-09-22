#!/usr/bin/env python3
"""TASK-JEV2 回放测量:对 samples/ 的历史工具输出模拟夜班 turn 跑裁判,输出对照报告。

用法::

    set -a; . /opt/data/.env; set +a
    .venv/bin/python tools/jev_poc/replay.py [--batch N] [--dry]

- ``--batch N``:每批喂裁判的块数(默认 12,防超窗口);
- ``--dry``:不调 LLM,只打印分块统计(行数守恒自检)。

报告落 ``tools/jev_poc/replay_report.md``:原 token vs 筛后 token、可筛除比例、
裁判自身消耗、按现价折算的省钱口径。每块裁决明细同落,供人工抽查误杀标注
(``tools/jev_poc/replay_chunks.jsonl``)。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.jev_poc.judge import JudgeClient, estimate_screening_savings  # noqa: E402
from tools.jev_poc.splitter import StubStore, split_text  # noqa: E402

SAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replay_report.md")
CHUNKS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replay_chunks.jsonl")

# 模拟一次夜班 turn 的「任务焦点描述」(人工从该 turn 上下文提取,口径写死便于复算):
# sample1 = 2026-09-19 Codex session(TASK-2c-fe 前端收藏 UI);
# sample2 = 2026-09-12 夜班执行器工具输出(TASK-2c 收藏 API 验证)。
SCENARIOS = (
    {
        "file": "codex_session_outputs.log",
        "focus": (
            "在 backend/app/static/index.html 实现 TASK-2c-fe 前端路线收藏 UI:"
            "路线卡片加收藏按钮(POST /api/collections)、我的收藏弹层(GET/DELETE),"
            "不改后端;需要看清 collections API 的请求体字段与既有前端结构。"
        ),
    },
    {
        "file": "nightly_tool_outputs.log",
        "focus": (
            "验证收藏 API(TASK-2c):后端 Collection 表与 /api/collections 增删查"
            "是否通过测试,pytest 是否全绿。"
        ),
    },
)


def run_scenario(scenario: dict, *, batch: int, dry: bool) -> dict:
    path = os.path.join(SAMPLES_DIR, scenario["file"])
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    store = StubStore()
    chunks = split_text(text, store=store)
    # 行数守恒自检(验收:数字可复算)
    assert sum(c.n_lines for c in chunks) == len(text.splitlines()), "行数不守恒!"
    assert "".join(c.text for c in chunks) == text, "拼接还原失败!"

    row: dict = {
        "file": scenario["file"],
        "focus": scenario["focus"],
        "lines": len(text.splitlines()),
        "chars": len(text),
        "chunks": len(chunks),
        "tokens_before": sum(c.approx_tokens for c in chunks),
    }
    if dry:
        row["dry"] = True
        return row

    client = JudgeClient(timeout=180)
    verdicts, judge_cost, judge_prompt, judge_completion, elapsed = [], 0.0, 0, 0, 0.0
    for start in range(0, len(chunks), batch):
        group = chunks[start : start + batch]
        t0 = time.time()
        result = client.judge(scenario["focus"], group)
        elapsed += time.time() - t0
        verdicts.extend(result.verdicts)
        judge_cost += result.cost_cny()
        judge_prompt += result.prompt_tokens
        judge_completion += result.completion_tokens
        row.setdefault("degraded_batches", 0)
        row["degraded_batches"] += 1 if result.degraded_all else 0
    assert len(verdicts) == len(chunks)

    stats = estimate_screening_savings(chunks, verdicts)
    row.update(stats)
    row.update(
        judge_cost_cny=round(judge_cost, 6),
        judge_prompt_tokens=judge_prompt,
        judge_completion_tokens=judge_completion,
        judge_seconds=round(elapsed, 1),
    )
    # 每块明细落盘(人工抽查误杀用)
    with open(CHUNKS_PATH, "a", encoding="utf-8") as fh:
        for chunk, verdict in zip(chunks, verdicts):
            fh.write(json.dumps({
                "file": scenario["file"], "id": chunk.index,
                "lines": [chunk.start_line, chunk.end_line],
                "tokens": chunk.approx_tokens, "keep": verdict.keep,
                "confidence": verdict.confidence, "degraded": verdict.degraded,
                "head": chunk.lines[0][:100] if chunk.lines else "",
            }, ensure_ascii=False) + "\n")
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=12)
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--model", default=None,
                        help="裁判模型覆盖(如 qwen3.8-flash);设 WHERE2GO_LLM_MODEL")
    parser.add_argument("--extra-params", default=None,
                        help='额外请求参数 JSON(如 \'{"enable_thinking":false}\' 关推理);设 WHERE2GO_LLM_EXTRA_PARAMS')
    args = parser.parse_args()
    if args.model:
        os.environ["WHERE2GO_LLM_MODEL"] = args.model
    if args.extra_params:
        os.environ["WHERE2GO_LLM_EXTRA_PARAMS"] = args.extra_params

    if not args.dry:
        open(CHUNKS_PATH, "w").close()  # 清旧明细
    rows = [run_scenario(s, batch=args.batch, dry=args.dry) for s in SCENARIOS]

    lines = ["# jev_poc 回放报告(TASK-JEV2)", "",
             f"生成:`replay.py --batch {args.batch}`{' --dry' if args.dry else ''}", ""]
    for row in rows:
        lines.append(f"## {row['file']}")
        lines.append(f"- 焦点:{row['focus']}")
        lines.append(f"- 规模:{row['lines']} 行 / {row['chars']} 字符 / {row['chunks']} 块 / 筛前 ≈{row['tokens_before']} tokens")
        if row.get("dry"):
            lines.append("- (dry:未调裁判)")
            lines.append("")
            continue
        lines.append(f"- 裁决:KEEP {row['kept']} / DROP {row['dropped']}(降级批 {row.get('degraded_batches', 0)})")
        lines.append(f"- 筛后 ≈{row['tokens_after']} tokens,节省 {row['saved_tokens']}({row['saved_ratio'] * 100:.1f}%)")
        lines.append(f"- 裁判自身:{row['judge_prompt_tokens']}+{row['judge_completion_tokens']} tokens,"
                     f"≈{row['judge_cost_cny']} 元,{row['judge_seconds']}s")
        # 省钱口径:被筛掉的 token 若在对话里存活 K 轮,节省 = saved_tokens × 单价 × K;
        # 裁判是一次性成本。盈亏平衡轮数 = 裁判成本 / (saved_tokens 单轮价值)。
        lines.append("")
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("\n".join(lines))
    print(f"\n报告已落 {REPORT_PATH};每块明细 {CHUNKS_PATH}")


if __name__ == "__main__":
    main()
