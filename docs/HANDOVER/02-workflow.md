# 02 · 开发流程与工程约定

## 环境准备

仓库根已有虚拟环境 `.venv/`(Python 3.13,用 `uv` 建的;系统 python 没有 pip)。

```bash
cd /mnt/projects/Where2Go-Planner
.venv/bin/python --version     # 3.13.x
```

**没有** `backend/.venv`,别在 backend 目录里用系统 python 跑 pytest(没装 pytest)。

## 跑测试(每次改动后必须)

```bash
cd backend && ../.venv/bin/python -m pytest -q
# 2026-10-19 实测基线:1739 passed,约 126 秒
```

- 全 mock,**不触网、不需要任何 API key**。`conftest.py` 的 `no_network` autouse fixture 会把 `requests` 打网络那一步换成抛错,任何意外的真实网络调用都会当场炸出来。
- 另有两个 autouse fixture:关掉种子数据(`seeds_disabled_by_default`)、把 LLM 回填强制成同步(`inline_intro_backfill`),后者让既有断言零改动。
- **基线只增不减**。写新测试没问题,让已有测试变红就是回归。`AGENTS.md` 里写的 792 是 2026-10 初的旧数字,以实跑为准(这也是为什么本文用"实跑"而非"文档写着")。
- 单跑某个文件会快:`../.venv/bin/python -m pytest -q test_region_gate.py`。

## 起服务

```bash
cd /mnt/projects/Where2Go-Planner
set -a && source /opt/data/.env && set +a      # key 与代理都在这里;少了 set -a 变量不进子进程
.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
curl -s localhost:8000/api/health               # {"status":"ok","missing_required":[],"version":"..."}
```

- 改 **`.py` 必须重启**;改**前端 `index.html` 不用**(StaticFiles 每请求读盘)。
- uvicorn 没开 `--reload`。
- 别在占用中的端口上硬起,先 `pgrep -af uvicorn` 看一眼。

## 提交与推送

```bash
git commit -m "fix(境外): 具体做了什么"      # 前缀用 feat/fix/docs/test/chore + 作用域
git push   # ← 不要自己推,先问
```

**这条最容易被打破,也最重要:只 commit,不 push。** 推远端要单独获得产品负责人授权。历史上 push 出过凭据与流水线事故。

提交前自查:`git status --short` 必须只剩你想提交的东西;`docs/NIGHTLY-QUEUE.md` 里对应任务的状态与"结果"段要一起更新(验收数字、commit 短哈希、耗时)。

## 任务队列制度(`docs/NIGHTLY-QUEUE.md`)

这是本项目执行任务的主干,1251 行,按需检索不要通读。

- 条目状态机:`pending → running → done / blocked / needs_review`。
- **每条任务先有契约**:复杂任务在 `docs/TASK-N-CONTRACT.md` 里写死口径 —— 产品负责人的拍板结论、实测数据、只读清单(不超过 5 个确切文件)、逐字的落地契约(新文件路径、精确签名、字段名、响应键名、环境变量名)、验收标准、不许动的清单。队列条目只写"逐字执行 `docs/TASK-N-CONTRACT.md`"。
- 契约里必须给**授权降级路径**:凡可能被外部条件(网络、权限、配额)卡住的条目,写明"真机若报 X,允许当轮降级为 Y 并注明待升级",否则执行者会为一个验证不了的项整晚停摆。
- **R8**:派给外部编码 Agent 的任务,启动后 12 分钟内必须出现第一处写文件动作(看 rollout 日志的写类命令),否则立即 kill。**R9**:任务描述必须是"投喂式"的(只读清单 + 落地契约),不许写"见 docs/xxx"这种宽指引。**R10**:同一任务族熔断两次 → 当轮改由自己手写。
- **止损线 40 分钟**:单条任务超过 40 分钟一律 kill。病根通常是任务体量太大,不是 Agent 不行 —— 一条任务 = 一个模块级改动(≈2 个新文件 + 1 个 API + 1 组测试)。前后端要拆成两条。

### 夜班 cron(自动化执行器)

`Where2Go 夜班执行器` 每晚 **22:00 CST** 跑一次(半价时段),读队列里 `pending` 的条目,依赖未完成的跳过,队列空则直接 `[SILENT]` 不回复。单晚最多 4 条,不 push。任务定义在 Hermes 的 cron 配置里(job id `762ce6846b44`),改口径时提示词本身也要同步。

## 改口径要同步四处

改了任何约定(数据源、代理、发布流程、测试策略),**四处都要改**,漏一处后续就按旧口径跑:

1. `docs/NIGHTLY-QUEUE.md` 头部的「⚠️ 当日生效口径通告」(冲突时以它为准)
2. `AGENTS.md`(仓库根速查表;它是受保护文件,写工具直写会被拒,需用终端里写文件 + commit)
3. 相关契约文档 / 这套交接文档
4. 夜班 cron 提示词本身

## 验证代码的三层手段(前端尤其重要)

1. **静态断言**:`test_frontend_routes.py` 检查 DOM id / JS 函数名 / API 调用字符串是否存在于 `index.html`,外加 `node --check` 语法校验。它只能证"字符串在文件里",证不了逻辑对。
2. **把函数抽出来用 node 真跑**:纯函数(分组、URL 拼接、标题渲染)从 `index.html` 抠出来执行,模板见 `tools/` 与历史脚本。这一层抓到过浏览器根本起不来时发现不了的真 bug。
3. **真实浏览器 QA**:真实点击(不是合成事件)、页面加载后先 hook `window.onerror` / `unhandledrejection`,结束时查 `window.__errs` 是否为空。

## 大任务怎么切

阈值 10 分钟:预计 10 分钟内能完成的小修小补直接做;超过 10 分钟或整段交付类,交给子进程/夜班执行器,**主会话只做派单、验收、拍板、推送**。防上下文溢出的做法:素材先落盘、搜索次数收敛、禁止"改了→验→再改"的无限验证循环。

## 时间与成本

- 每条任务结束后在队列里回填**可复算的数字**:pytest 通过数、commit 短哈希、耗时。别写"已完成",要写"1739 passed / 126s / `6ef68be`"。
- 跨会话续接前先核对仓库实际状态(`git log --oneline -5 && git status --short` + 队列里 `pending` 条目),**不要相信"上次已经做了"的说法** —— 真的发生过"以为做完了,仓库里其实一行代码都没有"。
