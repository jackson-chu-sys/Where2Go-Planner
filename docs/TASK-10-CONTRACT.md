# TASK-10 契约：AI 对话式行程规划（收藏面板里的「🤖 AI 行程」）

> 神朱 2026-10-01 拍板；主会话（赫沐神朱）同期定稿。需求登记：`01-功能需求清单.md` **M4.09（修订⑦）**。
> 夜班执行器**逐字执行本文**，勿自行探索仓库、勿扩 scope。**排在 TASK-9d 与 TASK-8* 之后**（神朱：明晚/后晚做）。

## 0. 拍板口径（勿自行改）

1. **对话形态**：**多轮可迭代**，带上下文历史（能继续说「第二天换成古镇」「预算压到 1500」再改）。
2. **收藏口径**：**不自动带**——只有用户在对话里**点名/引用**的收藏才能排进行程；未点名的一律不得纳入。
3. **粒度（两阶段）**：**本次只做阶段 A —— 先给出「每天的目的地行程」**（每天去哪、顺序、理由）；**交通/住宿/费用等细化留到用户确认之后再谈（阶段 B，本次不实现，见 §6）**。
4. **落库**：**展示不自动落库**；用户点「⭐ 存为行程方案」才落成既有 `TripPlan`（复用其报价与列表）。
5. 拆三条：**10a 后端 planner 服务（LLM 编排 + 会话表）→ 10b 后端 API（对话 + 存为方案）→ 10c 前端对话框 UI**。10b 依赖 10a，10c 依赖 10b。

## 1. 现有资产（已存在，**复用，勿重写**）

- `services/trips.py`：`upsert_trip_plan()` / `quote_plan()` / `trip_plan_to_dict()` —— 手工勾选收藏组合 + 「当时口径」总花费估算（`QUOTE_KIND="estimate"`、`QUOTE_NOTE` 免责文案、`nights` 是**报价参数不进表**、同名 upsert 幂等）。
- `db/models.py::TripPlan`：引用列存 `Collection.id`（**故意不建外键**，缺行按 `missing` 降级）。
- `app/api/trips.py`：`POST/GET/DELETE /api/trip-plans`（裸 JSON + 400 中文报错的统一风格）。
- `services/intro.py`：**LLM Provider 抽象**（`resolve_provider()` + `LLMClient`，key 只读 env，一切异常降级为**空串不抛**）—— 本任务的 LLM 调用一律走它，别新写 HTTP 客户端。
- `app/api/collections.py` + `db/models.py::Collection`（唯一键 `(kind, ref_key, mode)`，`mode` 用空串不用 NULL；`ref_key` 里 OSM 身份 `type/id` 优先、坐标定点 **7 位**小数 = `models.COORD_PRECISION`）—— 存为方案时要**幂等建**目的地收藏，必须复刻这套口径。
- 前端：`index.html` 收藏面板已有 tab 机制（「我的收藏」/「行程方案」，`state.tripPlan.tab`），第三个 tab 照它加。

## 2. 只读清单（≤5 个确切文件，读完立即开工，禁止浏览式探索）

- `backend/services/intro.py` —— LLM Provider 抽象与降级口径（本任务 LLM 调用唯一入口）
- `backend/services/trips.py` —— `upsert_trip_plan` / `quote_plan` 的签名与返回形状（存为方案时直接调）
- `backend/db/models.py` —— 表风格（`PlannerSession`/`PlannerMessage` 照现有表加）+ `Collection` 唯一键
- `backend/app/api/trips.py` —— 路由风格样板（裸 JSON + 400 中文 + `_optional_text/_payload_dict` 等小工具）
- 本文 `docs/TASK-10-CONTRACT.md`

参考（**只读**）：`backend/app/api/collections.py`（收藏 upsert 口径）、`backend/app/static/index.html` 的收藏面板 tab 区块。
⚠️ 写码前先读 `docs/NIGHTLY-QUEUE.md` 头部通告（数据源已切高德，别引用 `overpass.py`/`osrm.py`——它们已物理删除）。

## 3. 落地契约

### TASK-10a —— 后端 planner 服务（LLM 编排 + 会话持久化）

**新表（追加进 `backend/db/models.py`，照现有风格）**
- `PlannerSession`：`id / session_key (String(64), unique, index) / title (String(120), default "") / created_at / updated_at`
- `PlannerMessage`：`id / session_id (Integer, index) / role (String(16): "user"|"assistant") / content (Text) / payload (JSON, nullable) / created_at`

**新文件 `backend/services/planner.py`**
```python
PLANNER_MAX_TOKENS = 1200      # 必须按调用放大(勿用 intro 默认 120/20s;见项目 LLM 预算坑)
PLANNER_TIMEOUT_S  = 180
HISTORY_LIMIT      = 12        # 送进 prompt 的历史消息上限(超出只留最近 12 条)
MAX_MESSAGE_LEN    = 2000
DEFAULT_DAYS       = 3
MAX_DAYS           = 15
STOPS_PER_DAY_MIN  = 2
STOPS_PER_DAY_MAX  = 4

def plan_turn(session, *, session_key, message, collection_briefs=(), nights=None,
              client=None, environ=None) -> dict: ...
def parse_itinerary(text) -> tuple[Optional[dict], Optional[str]]: ...   # 纯函数
def list_messages(session, *, session_key, limit=50) -> list[dict]: ...
def clear_session(session, *, session_key) -> int: ...
```
- `plan_turn` 流程：取/建 `PlannerSession` → 存 user 消息 → 组 prompt（系统提示 + 最近 `HISTORY_LIMIT` 条历史 + 本轮消息 + 收藏素材清单）→ 调 LLM（`intro.LLMClient.chat(prompt, system=…, max_tokens=PLANNER_MAX_TOKENS, timeout=PLANNER_TIMEOUT_S)`）→ `parse_itinerary` → 存 assistant 消息（`payload` 存结构化行程）→ 返回。
- 返回形状（**键名逐字一致**）：
  `{"session_key","title","reply","itinerary"|None,"degraded":bool,"reason":str|None,"turn_index":int,"generated_at":ISO-UTC}`
- `itinerary` 结构化形状（严格，前端只读这些键）：
  ```
  {"days":[{"day":1,"base":"杭州","stops":[{"name":"西湖","reason":"…","collection_id":3|null}],"tip":"…"}],
   "days_count":N,"summary":"…","unused_collections":["名称"]}
  ```
- **`SYSTEM_PROMPT` 硬约束（逐条写进去）**：
  1. 你是自由行行程规划师；**只把用户点名/引用的收藏排进行程，未点名的收藏一律不得纳入**（收藏清单只作"可选素材"）；用户完全没点名时，按用户描述的需求自由选点。
  2. **本阶段只排「每天的目的地」**：不输出交通方式报价、不输出住宿名称与价格、不输出总预算（这些等用户确认后再细化）——违反 = 返工。
  3. 拿不到的事实（门票、开放时间、雪道数等）写「待核实」，**禁止编造**。
  4. 每天 `2~4` 个目的地，按**地理顺路**排序（同一天内不要横跨城市两端）；天数由用户说的天数决定，没说默认 `DEFAULT_DAYS`，上限 `MAX_DAYS`。
  5. 每个 stop 给**一句**具体理由（点名收藏的必须用该收藏自己的信息，不许套话）。
  6. **只输出一个 JSON 对象，不要 markdown 围栏、不要解释文字。**
- `parse_itinerary` 容错：剥 ```` ```json ```` 围栏 / 取第一个 `{` 到最后一个 `}` / 校验 `days` 是非空数组且每天有 `day`+`stops` → 失败返回 `(None, 原文前 200 字)`，**不抛**。
- 降级（一律**不抛**，前端要看到可见文案）：无 LLM key → `degraded=True, reason="no_key"`；超时 → `reason="timeout"`；其他异常 → `reason="error"`；解析失败 → `itinerary=None, reason="parse_error"`，`reply` 给中文兜底（含原因与「可重试」提示）。
- `collection_briefs` 精简口径：元素 `{"id","kind","title","summary"}`（`summary` 截断 120 字），由 API 层从库内取。

### TASK-10b —— 后端 API（对话 + 存为方案）

**新文件 `backend/app/api/planner.py`**（风格照 `app/api/trips.py`：裸 JSON、400 中文、`_optional_text` 式小工具）
- `POST /api/planner/messages`
  body `{"session_key": str|null, "message": str, "collection_ids": [int]|null, "nights": int|null}`
  → `session_key` 为空则后端生成 `uuid4().hex` 并返回；`collection_ids` 给了就只把这几条作为素材清单，**没给则取当前全部收藏**（prompt 仍硬约束"未点名不得纳入"）；`message` 空/超 `MAX_MESSAGE_LEN` → **400 中文**。
  返回：`plan_turn` 的响应形状，原样透出。
- `GET /api/planner/messages?session_key=&limit=`（`limit` 默认 50、上限 200）→ `{"session_key","title","items":[{"role","content","payload","created_at"}]}`；未知 session → `items: []`（不 404）。
- `DELETE /api/planner/messages?session_key=` → `{"session_key","deleted":N}`。
- `POST /api/planner/save`
  body `{"session_key","name","stops":[{"name","collection_id"|null}],"legs":[int],"stay":[int],"nights":int|null}`
  逻辑：①对没有 `collection_id` 的 stop，在库内 `Place` 按名称匹配（**精确同名优先**，其次包含匹配；多命中取距离/入库顺序最前的一条；命中不到 → 进 `unmatched`）②命中的地点**幂等建 `Collection`**（`kind="place"`，`ref_key` 走既有 `osm_key(type,id)` 口径，坐标定点 7 位）③用这些 collection id + 传入的 `legs`/`stay` 调 **既有** `services.trips.upsert_trip_plan()`（同名 = 刷新幂等）④返回
  `{"trip_plan": {...含 quote...}, "matched":[{"name","place_id","collection_id"}], "unmatched":["名称"]}`；一个都匹配不到 → **400 中文**（"行程里的地点都不在库内，先在目的地列表里搜到它们再试"）。
- `app/main.py` 里 include 新 router 到 `/api`。
- 测试 ≥18 例（全 mock 不触网）：路由校验/400 文案、`collection_ids` 与「取全部」两条路径、`plan_turn` 降级四态（no_key/timeout/error/parse_error）、`save` 的匹配与幂等（同名重复保存不产生新行）、`/api/trip-plans` 与 `/api/collections` **既有响应键零变化**。

### TASK-10c —— 前端对话框 UI（收藏面板第三个 tab）

文件：`backend/app/static/index.html` + `backend/test_frontend_routes.py`
- 收藏面板加 **第三个 tab「🤖 AI 行程」**（沿用现有 tab 机制与样式，与「我的收藏」「行程方案」并列互斥显示）。
- 面板内容：
  1. **消息流**：用户/助手气泡；助手气泡里渲染**行程卡片**（每天一块：`Day N · base` + `stops` 的名称与一句理由 + `tip`）；`degraded=true` 时显示可见文案 + 原因（no_key/timeout/error/parse_error），**绝不静默**。
  2. **输入区**：`textarea`（Enter 发送、Shift+Enter 换行）+ 发送按钮；发送中按钮禁用 + 文案「AI 规划中…（首次约 1 分钟）」。
  3. **点名收藏（关键，对应拍板口径 2）**：输入框上方渲染当前收藏的**可点标签**（`@名称`）；点一下 = 把该条加入本次请求的 `collection_ids` 并在输入框插入引用文本；**未点名的收藏绝不自动出现在请求里**。标签区显示「已点名 N 条」。
  4. **底部操作**：「⭐ 存为行程方案」（有 itinerary 时才可用 → `POST /api/planner/save`，成功显示方案名与总预算估算，并列出 `unmatched` 提示「N 个地点未在库内，未纳入」）、「🗑 清空对话」（`DELETE`）。
  5. **回放**：切到该 tab 时 `GET /api/planner/messages` 恢复历史（含行程卡片）。
- 状态：`state.planner = {sessionKey, items:[], busy:false, mentioned:[], lastItinerary:null}`；`session_key` 存 `localStorage["w2g_planner_session"]`（刷新不丢对话）。
- 静态断言 ≥12 例（DOM id / 函数名 / 接口字符串 / tab 注册 / 点名标签存在 / 清空与存为方案按钮）+ `node --check`。
- 真机 QA（browser_exec，受信任点击）：开页 0 error → 切「🤖 AI 行程」→ 输入「3 天，亲子，不要太累」发送 → 出**按天行程卡片**（每天 2~4 个目的地 + 理由）→ 追问「第二天换成古镇」→ 卡片更新 → 点一个收藏标签再问「用这条」→ 行程里出现该收藏 → 点「⭐ 存为行程方案」→ 面板出现方案与预算估算 → 「🗑 清空对话」→ 全程 `window.__errs` 0 条。

## 4. 验收

- `cd backend && ../.venv/bin/python -m pytest -q`：**执行时以当日基线为准**（TASK-9 落地后应 ≥ 792+9 系列增量）**零回归**；新增 **10a ≥18 / 10b ≥18 / 10c ≥12**，全 mock 不触网。
- 每条一个 commit：`TASK-10a: …` / `TASK-10b: …` / `TASK-10c: …`。**夜班只 commit 不 push。**
- 真机冒烟（10a/10b，Python 有变先重启 uvicorn，命令见队列头部）：
  `curl -s -XPOST localhost:8000/api/planner/messages -H 'Content-Type: application/json' -d '{"message":"3天 亲子 不要太累"}'` → 记**墙钟耗时**与返回是否带结构化 `days`；再用 `session_key` 追问一句验证多轮上下文生效。
  ⚠️ 若单轮 >180s 或 JSON 解析频繁失败，**记 needs_review 并把实测数字写进结果段**（说明 `PLANNER_MAX_TOKENS/TIMEOUT` 需要调），不要擅自改成交互式流式。
- 10c 真机 QA 见 §3.10c 末。

## 5. 不许动 / 不许做

- `TripPlan` / `Collection` / `CollectionCat` 的**表结构与既有行为**；`/api/trip-plans`、`/api/collections` 的**响应键名**。
- 既有「行程方案」tab 的手工勾选组合流程（只在收藏面板**新增**第三个 tab）。
- 需求文档 `01-功能需求清单.md` / `02-项目计划与架构.md`（夜班不可改规格；有异议写进晨报）。
- `AGENTS.md`（夜班不可写，晨报提示主会话同步）。
- 不扩 scope：**不做阶段 B**（交通/住宿/费用细化）、不做流式输出、不做多会话管理 UI、不新引第三方依赖。

## 6. 阶段 B（本次**不实现**，仅记录设计意图，待神朱验收 A 后定细节）

用户确认「每天的目的地」后，再细化整体方案：把每天停靠点串成路线（逐段调 `/api/routes`）、按城市/天数匹配住宿（`/api/stays`）、给总预算（复用 `services.trips.quote_plan` 的"当时口径"估算与免责标注），并一键落成 `TripPlan`。
**本次只交付阶段 A**；阶段 B 的口径（用真实路线还是估算、住宿按天还是选一家、预算维度）届时单独拍板。
