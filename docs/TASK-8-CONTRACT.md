# TASK-8 契约：目的地详情弹窗 + 相关图片 + 类别专属要点

> 神朱 2026-10-01 拍板；主会话（赫沐神朱）2026-10-01 实测并定稿。
> 夜班执行器**逐字执行本文**，勿自行探索仓库、勿扩 scope。对应需求条目 M1.04 / M1.07 / M1.08（P0/P1，**不是新增需求**）。

## 0. 拍板口径（勿自行改）

1. **图片**：高德 POI 图为主 + 维基百科/Commons 兜底；按 POI 落库，命中缓存 7 天；缺图给「暂无图片」占位，**不得编造图片 URL**。
2. **交互**：地图 pin **仍先出 Leaflet 小 popup**（popup 内新增「📄 查看详情」按钮）→ 打开居中详情弹窗；地图下方列表行**整行可点**、直接开**同一个**弹窗。两处入口共用一个 modal。
3. **详细介绍**：现有 `/api/places/details` 的 2~3 句长介绍 **＋** 新增 LLM「类别专属要点」（自然→最佳季节/门票/开放信息/游玩建议；人文美食→人文背景/必吃/代表小店；滑雪→雪道数与分级/开放期/适合人群；运动→项目/场地装备/适宜人群）。按 POI 永久缓存。
4. 拆三条：**TASK-8a1（图片链路）→ TASK-8a2（类别要点）→ TASK-8b（前端弹窗）**；8b 依赖前两条。拆条原因：6c/6d/6g/7a 四条都在 40min 止损线附近，单条塞太多必被 kill。

## 1. 实测数据（主会话 2026-10-01 容器内实跑，**勿重测、勿改口径**）

- **高德 Web 服务**（key 已在运行中的 uvicorn env）：
  `https://restapi.amap.com/v3/place/text?key=<WHERE2GO_AMAP_KEY>&keywords=西湖&location=120.149,30.246&radius=20000&offset=1&extensions=all`
  → 取 `pois[0].photos[{title,url}]`。实测：西湖 **3 张** / 崇儒乡 1 张 / 上海虹桥站 3 张，**0.1~0.2s**，**直连**可用（国内端点）。
- **高德误配风险**：坐标在日本汤泽（138.81,36.93）查「汤泽高原滑雪场」→ 命中「狂飙乐园滑雪场」、photos=0。**必须做坐标门控**（见 §3）。
- **维基百科**：`https://zh.wikipedia.org/w/api.php` + `prop=pageimages&piprop=thumbnail&pithumbsize=800`，8/8 命中且有图，~1.5s，直连可用。
- **维基 geosearch（更像 POI 实景，优先于 search）**：
  `generator=geosearch&ggscoord=30.246|120.149&ggsradius=10000&ggslimit=5&prop=pageimages`
  → 西湖点近邻命中「钱王祠 / 丁鹤年墓亭」；崇儒乡点 →「崇儒畲族乡 / 飞路塔」。
  名称直搜（`generator=search`）会落到上位页（「汤泽高原滑雪场」→「湯澤町」），**只作兜底**。
- **Commons 相册**：
  `commons.wikimedia.org/w/api.php?generator=search&gsrsearch=West Lake Hangzhou&gsrnamespace=6&gsrlimit=4&prop=imageinfo&iiprop=url&iiurlwidth=640`
  → `imageinfo[0].thumburl`（**含 `?utm_*` 查询串，落库前剥掉**）。
- **key 位置**：`/opt/data/.env` 的 `WHERE2GO_AMAP_KEY`（Web 服务）与 `WHERE2GO_AMAP_JS_KEY`；运行中的 uvicorn 进程 env 里两把都在。
  **后端只用 `WHERE2GO_AMAP_KEY`；任何 key 都不得写进前端 `index.html`**（凭据纪律）。

## 2. 只读清单（≤5 个确切文件，读完立即开工，**禁止浏览式探索仓库**）

- `backend/data_sources/_common.py` —— `build_session(...)` 的按源代理口径（要加 `"amap"` 源）
- `backend/data_sources/photon.py` —— 新增数据源模块的写法样板（ENV 端点覆盖 / `environ` 注入 / 异常口径）
- `backend/db/models.py` —— 表与列的写法风格、坐标定点 7 位、json 列用法
- `backend/db/repository.py` —— 缓存读写的既有风格（`PlaceDetail` / `OriginCache` / `StayQueryCache` 三处照抄）
- 本文 `docs/TASK-8-CONTRACT.md`

参考（**只读、勿改**）：`backend/app/api/places.py` 的 `/api/places/details`（批量 + 降级形态）、`backend/services/details.py`（LLM 预算口径）。
8b 另读：`backend/app/static/index.html`、`backend/test_frontend_routes.py`。

## 3. 落地契约

### TASK-8a1 —— 图片链路

**新文件 `backend/data_sources/amap.py`**
- 常量：`AMAP_ENDPOINT = "https://restapi.amap.com/v3/place/text"`、`ENV_AMAP_KEY = "WHERE2GO_AMAP_KEY"`、`ENV_AMAP_MAX_MATCH_M = "WHERE2GO_AMAP_MAX_MATCH_M"`（默认 `5000`）。
- `def search_poi_photos(name: str, lat: float, lng: float, *, radius_m: int = 20000, environ=None, session=None) -> list[dict]`
  - 返回 `[{"url": str, "title": str}]`；`http://` 统一升成 `https://`；只保留看起来有效的 url。
  - **门控**：仅当 `pois[0]` 与入参 (lat,lng) 的 haversine 距离 ≤ 阈值 **且** `photos` 非空时返回图片，否则返回 `[]`（**不抛异常**）。
  - `status != "1"`（key 缺失 / 配额 / 无结果）→ 返回 `[]`，不抛；HTTP 错误、超时 → 抛 `DataSourceError`（由调用方降级）。
  - 走**直连**：`_common.build_session(..., source="amap")`（在 `_common` 里加 `"amap"` 源，默认直连，`WHERE2GO_PROXY_AMAP` 可覆盖为 `off/env/URL`）。

**新文件 `backend/data_sources/wikimedia.py`**
- 常量：`WIKI_API = "https://zh.wikipedia.org/w/api.php"`、`COMMONS_API = "https://commons.wikimedia.org/w/api.php"`。
- `def wikipedia_media(name: str, lat: float, lng: float, *, lang: str = "zh", limit: int = 4, environ=None, session=None) -> dict`
  - 返回 `{"page_title", "page_url", "extract", "images": [{"url","title"}], "via": "geosearch"|"search"|"none"}`。
  - 顺序：先 `generator=geosearch`（`ggscoord=lat|lng`、`ggsradius=10000`、`ggslimit=5`）取近邻页，取距离最近且有图的一页；无页/无图 → `generator=search`（`gsrsearch=name`、`gsrlimit=1`）。
  - 主图 = 该页 `prop=pageimages&piprop=thumbnail&pithumbsize=800` 的 `thumbnail.source`；再用 Commons 搜索该名称补至 ≤`limit` 张（`gsrnamespace=6`、`iiurlwidth=640`），thumburl **剥掉 `?` 之后的查询串**。
  - 任何失败返回 `{"images": [], "via": "none", ...}`，**不抛**。

**新表 `PlaceMedia`**（追加进 `backend/db/models.py`，**不改既有列**）
`id / place_id (FK places.id, unique) / source (String: amap|wikimedia|mixed|none) / images (JSON: [{"url","title","source"}]) / page_url (String, nullable) / reason (String, nullable: no_key|no_data|error) / fetched_at (DateTime)`
旧库靠 `create_all` 自动建表，**不迁移存量**。

**新服务 `backend/services/place_media.py`**
- `def fetch_media_for_place(place: Mapping, *, environ=None) -> dict` → `{"place_id", "source", "images", "page_url", "reason", "cached", "fetched_at"(ISO UTC)}`
- 顺序：库内未过期缓存 → 高德（命中即 `source="amap"`）→ 维基兜底（两者都有图 → `"mixed"`；只有维基图 → `"wikimedia"`）→ 都没图 → `source="none"` + `reason`。
- TTL：命中 `WHERE2GO_PLACE_MEDIA_TTL_S` 默认 **604800**（7 天）；空结果/失败 `WHERE2GO_PLACE_MEDIA_MISS_TTL_S` 默认 **21600**（6 小时**负缓存**——别把一次空结果永久钉死，同 6c 教训）。
- 单 POI 图片上限 `WHERE2GO_PLACE_MEDIA_MAX_IMAGES` 默认 **6**。

**新 API** `GET /api/places/media?place_ids=1,2,3`（batch ≤ 20，超限 400）
→ `{"items": [ ...按入参顺序... ]}`；**读库优先，仅 miss 才触网**；单条失败不影响其余（该条 `reason="error"`）；响应形状稳定（前端只读固定键）。

### TASK-8a2 —— 类别专属要点

**新文件 `backend/services/highlights.py`**
- 预算：`HIGHLIGHT_MAX_TOKENS = 600`、`HIGHLIGHT_TIMEOUT_S = 60` —— **必须按调用放大**，绝不可沿用 `intro.LLMClient` 的 `120/20s` 默认（否则静默截断→空要点，极难排查，见项目 skill 的「LLM 输出预算坑」）。
- 固定字段表 `CATEGORY_FIELDS`：自然→[最佳季节, 门票/开放信息, 游玩建议]；人文美食→[人文背景, 必吃, 代表小店]；滑雪→[雪道数与分级, 开放期, 适合人群]；运动→[项目, 场地/装备, 适宜人群]；其他→[亮点, 建议]。
- `def fetch_highlights(place, *, client=None, environ=None) -> dict` → `{"place_id", "category", "fields": [{"label", "value"}], "note", "cached"}`
- 要求严格 JSON；**查不到就 `value=null` 并在 `note` 标注「待核实」，禁止编造票价/雪道数/店名**；解析失败 → `fields=[]` + `note="解析失败"`，不抛。
- **新表 `PlaceHighlight`**：`id / place_id (unique) / category / fields (JSON) / note (String) / generated_at (DateTime)`；生成后**永久缓存**（同 `PlaceDetail` 口径）。

**新 API** `GET /api/places/highlights?place_ids=1,2`（batch ≤ 10）→ `{"items": [...]}`，读库优先。

### TASK-8b —— 前端详情弹窗（依赖 8a1 / 8a2）

文件：`backend/app/static/index.html` + `backend/test_frontend_routes.py`（静态断言）

- 新增 `#placeModal`：`<div id="placeModal" class="modal-mask" role="dialog" aria-modal="true" aria-labelledby="placeModalTitle" hidden>`；**Esc / 点遮罩 / ✕ 关闭**；打开时记住来源焦点、关闭归还。
- 内容顺序：
  1. 标题 + 类别 badge（与 pin 同色）+ 距起点 / 分段
  2. **图片区**：主图 + 缩略图条；弹窗打开**时才**调 `GET /api/places/media`；加载中骨架态；空/失败 → 「暂无图片」+ reason 文案；**必须标注图源**（「图源：高德」/「维基百科」/「高德 + 维基百科」），有 `page_url` 时给维基页外链。
  3. **详细介绍**：复用 `detailTextOf()`；缺则带「生成介绍」按钮（沿用 `/api/places/details`）。
  4. **类别专属要点**：调 `GET /api/places/highlights`，骨架态 + 「待核实」弱化样式。
  5. 动作行：`🚗 路线 / 🛏️ 住宿`（复用既有 `js-routes` → `openRoutePanel`）+ ⭐收藏（复用 `placeFavButtonHtml`）。
  6. 来源脚注：复用 `identityHtml()`。
- 入口：① `popupHtml()` 里加「📄 查看详情」按钮（`class="js-detail-modal"`、`data-i`）；② 列表 `.pl-row` **整行可点**开弹窗，行内既有 `.js-fav / .js-routes / .js-detail-gen` 一律 `stopPropagation`，**既有行为零回归**。
- 不新增任何前端 key；`window.__errs` 必须 **0 条**。

## 4. 验收

- `cd backend && ../.venv/bin/python -m pytest -q`：基线 **792 passed** 零回归；新增用例 **8a1 ≥ 22 / 8a2 ≥ 12 / 8b ≥ 12**，全 mock 不触网。
- 每条一个 commit，message 形如 `TASK-8a1: 高德 POI 图 + 维基/Commons 兜底 + PlaceMedia 缓存 + /api/places/media`。**夜班只 commit 不 push。**
- 真机冒烟（Python 有变时先重启）：
  `cd /mnt/projects/Where2Go-Planner && set -a && source /opt/data/.env && set +a && nohup .venv/bin/python -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000 &`
  抽验 `/api/places/media?place_ids=<库内西湖那条>` 出图、`/api/places/highlights?place_ids=...` 出字段。
- browser_exec QA（8b）：开页 hook `window.__errs` → 点 pin → 小 popup 有「查看详情」→ 开弹窗出图/介绍/要点 → 点「🚗 路线 / 🛏️ 住宿」出面板 → Esc 关闭 → 点列表行开同一弹窗 → 全程 error 0 条。

## 5. 不许动 / 不许做

- `services/routes.py`、`services/stays.py`、`services/bands.py`、`data_sources/overpass.py` 的搜索与端点链口径。
- `Place` 表既有列语义（**只新增表，不改既有列**）。
- `AGENTS.md`（夜班不可写，晨报里提示主会话白天同步）。
- 前端既有收藏 / 路线 / 住宿交互。
- 不扩 scope：不做住宿图片、不做高德路线替换 OSRM、不做自定义弹窗动画框架。
