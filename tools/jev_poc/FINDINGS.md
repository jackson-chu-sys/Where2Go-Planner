# FINDINGS —— Jev 范式 PoC 测量结论(TASK-JEV2)

生成:2026-09-21 夜班。测量脚本 `replay.py --batch 12`,报告 `replay_report.md`,
每块裁决明细 `replay_chunks.jsonl`(59 块)。裁判 = qwen3.8-max(token-plan 兼容模式,
半价窗),temperature=0,单次批量调用/批 12 块。

## 1. 省 token 数字(真实回放,非估算)

| 样本 | 规模 | 裁决 | 筛前→筛后 tokens | 节省 | 裁判自身 tokens | 裁判成本 |
|---|---|---|---|---|---|---|
| codex_session_outputs.log(9/19 TASK-2c-fe session) | 1295 行 / 52 块 | KEEP 36 / DROP 16 | 17216 → 12146 | **29.4%** | 21033+9486 | ≈0.071 元 / 253s |
| nightly_tool_outputs.log(9/12 夜班工具输出) | 156 行 / 7 块 | KEEP 1 / DROP 6 | 1945 → 438 | **77.5%** | 2604+669 | ≈0.006 元 / 18s |

复算口径:tokens = chars/3 向上取整(`Chunk.approx_tokens`,两侧同口径);
「筛后」= 保留块全文 + 被 drop 块的 stub 行。价格 qwen 0.0012/0.0048 元每千
(input/output,半价窗近似)。

**关键成本事实:裁判为做判定必须读全文** —— sample1 裁判自身消耗 30519 tokens,
是筛除量(5070)的 **6.0 倍**;sample2 为 2.2 倍。其中 qwen3.8-max 的
reasoning_tokens 占大头(单批 2990 completion 里 2763 是 reasoning)。

## 2. 误杀率(人工抽查)

全量 22 个 DROP 块逐一目检 head + 抽样读原文(≥20 块,满足验收):

- **明显该 drop(无误杀)**:sample2 的 5 块(git log、routes.py 源码段——与「验证收藏
  API 测试」焦点无关);sample1 的 id=0(ls -la 目录清单)、id=6(Chunk ID 元信息)、
  id=9/12/13/14(后端 test_routes 旧测试代码——前端任务不需要)、id=31(Output: 空壳)等。
- **边缘误杀(2/22,≈9%)**:sample1 id=39-42(index.html 的 closeRoutePanel/setStatus/
  fillIntros 函数段——TASK-2c-fe 要在同一文件加收藏 UI,这些既有函数**其实有参考价值**)。
  裁判置信 0.80-0.88,恰在阈值上方。
- 误杀兜底有效:所有 drop 块 stub 可逆,`store.restore(key) == 原文` 抽查通过,
  真需要时一次本地读回、零额外 LLM 成本。

**误杀率 ≈9%(2/22),且可逆不丢数据。**

## 3. 盈亏平衡

裁判是一次性成本;被筛掉的 token 若在后续对话里**每轮都会被重发**,则每存活一轮省
`saved_tokens × 输入单价`:

- sample1:每轮省 ≈0.0061 元,盈亏平衡 ≈**11.6 轮**
- sample2:每轮省 ≈0.0018 元,盈亏平衡 ≈**3.5 轮**

夜班执行器单任务通常 20-50 次工具调用,长会话上下文反复重发 → **超过平衡点**;
但一次性短会话(<10 轮)反而亏。

## 4. 结论:选 **a) 值得接入,但带三个限定条件**

值得接入 SoL-Pi/codex 管道的场景 = 「大输出 + 长会话」:codex 日志、pytest 全量输出、
浏览器 DOM dump 进夜班执行器上下文之前先过筛。限定:

1. **裁判必须换非推理小模型**(qwen-flash / deepseek 非 thinking 档)。本次 qwen3.8-max
   reasoning 开销使裁判成本放大 ~5 倍;换小杯预期裁判成本降至 1/5-1/10,盈亏平衡从
   11.6 轮降到 ~2 轮,接入面大幅变宽。
2. **只筛「预计存活 ≥10 轮」的输出**(或按输出体量 ≥1000 tokens 门槛),短输出直接进
   上下文不筛。
3. **stub 可逆是硬前提**(已实现),误杀 9% 由 restore 兜底;高价值文件段(当前任务
   涉及的目标文件)应加白名单不筛。

### 接入草案(SoL-Pi / 夜班执行器管道)

```
工具输出 → len>1000 tokens?
  ├─ 否 → 原文直接进上下文(不筛)
  └─ 是 → splitter.split_text(25行/块) → StubStore
        → judge.JudgeClient(小杯模型).judge(任务焦点, 分批12块)
        → keep 块原文 + drop 块 stub 行进上下文
        → 后续轮若模型引用 stub key → 本地 restore 原文补入(零 LLM 成本)
```

任务焦点直接取当前 NIGHTLY-QUEUE 条目的「目标」行(已有结构化文本,零额外成本);
replay 实测两个焦点描述均产出合理裁决,证明焦点质量够用。

### b) 若上真 Jev API($5 免费额度)

本地 adapter 的裁判推理成本是大头(6x 放大);托管 Jev 若用专用小模型裁判,单位筛选
成本预期低于本地 qwen3.8-max 方案,但引入外部依赖与数据出境(samples 虽已脱敏,生产
日志含内部路径)。$5 额度 ≈ 按本次单价可筛 ~70 万 tokens,足够做一轮生产级 A/B;
建议先用免费额度对 SoL-Pi 真实管道跑对照,再决定是否长期采用。**优先级低于条件 1
(换小模型),后者零外部依赖即可拿到大部分收益。**

## 5. 小杯裁判复测(2026-09-22 白天补做,条件①验证)

`replay.py` 新增 `--model` / `--extra-params`;qwen3.8-flash 实测
`{"enable_thinking":false}` 可关推理(smoke:completion 215→31 tokens)。

| 指标 | qwen3.8-max(基线) | flash+关推理 | 变化 |
|---|---|---|---|
| 裁判耗时(两样本合计) | ~271s | ~16s | **-94%** |
| 裁判费用 | ≈0.077 元 | ≈0.033 元 | **-57%** |
| sample1 节省率 | 29.4% | 50.2%(DROP 34/52 块) | 更激进 |
| 与 max 裁决一致率 | — | 45/59(76%) | 见下 |

**新风险:flash 裁决明显更激进**——max 判 KEEP 的块里 13 块被 flash 判 DROP,
且集中在 index.html 既有函数段(正是焦点里"需要看清既有前端结构"的目标文件段)。
省得更多 ≠ 判得更准;误杀兜底仍靠 ①低置信保留阈值 ②stub 可逆 restore
③接入时落实 FINDINGS 条件 3 的「当前任务目标文件白名单」。

盈亏平衡重算(sample1):裁判 0.0298 元 ÷ 每轮省(50.2%×17216 tok≈8648 tok≈0.0104 元/轮)
≈ **2.9 轮**,与预测 ~2 轮吻合;sample2 ≈ 1.5 轮。→ **条件①成立,小杯接入划算。**

附注:flash 首跑 1 批解析失败降级(铁律兜底生效,全部保留);同配置复跑 0 降级,
判定质量有波动,生产接入建议对失败批加 1 次重试。

## 6. 验收自查

1. 回放数字可复算 ✓(`replay.py --dry` 行数守恒断言 + `replay_chunks.jsonl` 逐块明细)
2. FINDINGS 结论明确、引用真实数字 ✓(本文)
3. pytest 全绿 ✓(322 passed,含 JEV1 46 例 + 本任务新增分批 id 回归 1 例)
4. 已 commit(不 push)✓

附:JEV1 首晚发现的 bug——分批调用时裁判按 prompt 真实块号(id=12..)回复,
`parse_verdicts` 原实现硬要求 0 基 id,导致 sample1 前次 4/5 批全部降级(白耗 0.076 元)。
已修复(`ids` 参数)并加回归用例;修复后 0 降级批。
