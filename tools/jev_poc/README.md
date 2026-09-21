# jev_poc —— Jev 范式 PoC:工具结果进上下文前先过廉价裁判

对应 NIGHTLY-QUEUE 的 TASK-JEV1(采集与分块)/ TASK-JEV2(回放测量与结论)。

## 问题

夜班执行器最大的 token 消耗点是「大工具输出整体进上下文」:Codex 日志、pytest 长输出、
浏览器 DOM dump 等动辄几千行,其中大部分对当前任务焦点已无价值,却按全价计入每轮对话。

## 范式(winnow / Jev 式筛子)

```
大输出 ──splitter──▶ 25 行块 ──judge(廉价LLM,单次批量调用)──▶ keep/drop+置信度
                                            │
                          keep → 原文进上下文
                          drop → 只放一行 stub([jev-stub key lines=a-b] 首行…)
                                  原文存 StubStore,restore key 可逆还原
```

铁律:**裁判任何失败(坏 JSON/超时/限流/无 key/网络错)一律降级为全部保留**,
drop 只能来自明确且置信度 ≥0.75 的判定 —— 省 token 绝不以丢数据为代价。

## 文件

- `splitter.py` — 按行分块(默认 25 行/块,行数守恒、可逐字符还原),`StubStore`
  提供 stub↔原文可逆;`Chunk.approx_tokens` 统一 chars/3 估算口径。
- `judge.py` — `JudgeClient.judge(task_focus, chunks)`:OpenAI 兼容
  `POST {base_url}/chat/completions` 单次调用批量裁决;Provider 注册表与
  backend/services/intro.py 同口径(qwen token-plan 优先、deepseek 备选,key 只从
  环境变量读);`parse_verdicts` 宽容解析(剥 code fence、抓 JSON 数组);
  `estimate_screening_savings` 输出筛前/筛后 token 与节省比。
- `samples/` — 脱敏后的真实回放素材(codex session jsonl 片段 + 夜班工具输出样本)。
- `replay.py` — (JEV2)对 samples 跑裁判,输出对照报告。
- `FINDINGS.md` — (JEV2)测量结论。

## 依赖

只需 `requests`(backend/requirements.txt 已有)+ 标准库;pytest 用例在
`backend/test_jev_poc.py`(全 mock 不触网)。裁判真实调用需要
`ALIBABA_TOKEN_PLAN_API_KEY` 或 `DEEPSEEK_API_KEY`(见 /opt/data/.env)。

## 环境变量

| 变量 | 作用 |
|---|---|
| `WHERE2GO_LLM_PROVIDER` | 指定 qwen/deepseek(默认按注册表挑第一个有 key 的) |
| `WHERE2GO_LLM_BASE_URL` / `WHERE2GO_LLM_MODEL` | 覆盖端点/模型 |

## 快速使用

```python
from tools.jev_poc import split_text, StubStore, JudgeClient

store = StubStore()
chunks = split_text(open("/tmp/w2g_task.log").read(), store=store)
result = JudgeClient().judge("修复远距离分段 Overpass 查询", chunks)
for chunk, verdict in zip(chunks, result.verdicts):
    print(store.stub_text(chunk) if not verdict.keep else chunk.text)
print(result.kept, result.dropped, f"{result.cost_cny():.4f} 元")
```
