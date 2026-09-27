# TASK-FE:功能1~4 前端实现(2026-09-27,神朱需求)

> 执行器:Codex。**验收 = 测试全绿**,不是"看起来对"。
> 后端(0-50km 分段 / 推荐 / 长介绍 / 收藏父级)已完成并已提交,**前端这一单只改
> `backend/app/static/index.html`**(如确有必要可改 `backend/test_frontend_routes.py` 的
> 静态清单,但**不许**删既有断言)。

## 0. 目标(神朱 2026-09-27 需求 1~4)

1. 重新添加 0-50KM 分段 —— **后端已完成**(`services/bands.py` 已有 `0_50`),
   前端**不需要硬编码**,分段下拉/图例/范围圈都由 `/api/places/meta` 驱动;
   只需保证选了 0-50km 后一切照常,并在该档给一句"含市区/近郊"的说明。
2. 地图默认**只显示 AI 推荐的 3~5 个**目的地(当前分类),其余不画针(可切换"显示全部")。
3. 地图**下方新增列表**:推荐条置顶(带推荐理由 + 2~3 句重点介绍),其余目的地按距离排列,
   每条 2~3 句重点介绍(**分批懒加载**,不能阻塞首屏)。
4. 目的地信息框(Leaflet popup)里有**收藏按钮**;收藏面板里,该目的地所属的
   **交通路线收藏**与**住宿收藏**以**多条目**挂在目的地下方(两层)。

## 1. 只读清单(不要全仓探索,只读这些)

- `backend/app/static/index.html` —— 唯一要改的文件(约 1800 行,单文件原生 JS + Leaflet CDN)
- `backend/app/api/places.py` —— `/api/places/recommend`、`/api/places/details` 的响应形状
- `backend/app/api/collections.py` —— `parent` 参数与 summary 透传
- `backend/test_frontend_features.py` —— **本单的验收测试**(先读它,它写死了要新增的
  DOM id / 函数名 / 字段名)
- `backend/test_frontend_routes.py` —— 既有前端静态断言(零回退,别破坏)
- `docs/FEATURE-PLAN-2026-09-27.md` —— 设计口径

## 2. 后端契约(已实现,直接调)

### 2.1 `GET /api/places/recommend?origin=<城市>&band=<band key>&category=<可选>&count=5&refresh=false`

```jsonc
{
  "origin_city": "上海",
  "band": {"key": "50_100", "label": "50-100 km", "low_km": 50, "high_km": 100},
  "category": null,
  "count": 4,
  "items": [
    {
      "id": 123, "osm_type": "node", "osm_id": 42, "name": "四明山", "category": "自然风光",
      "lat": 29.7, "lng": 121.0, "distance_km": 132.4, "intro": "一句话简介", "tags": {...},
      "source": "OSM", "origin_city": "上海", "band": "50_100",
      "rank": 1, "reason": "推荐理由(≤80 字,可能为空)", "detail": "已有的 2~3 句长介绍或 null"
    }
  ],
  "candidates": 80, "degraded": false, "basis": "llm+osm_tags", "cached": true,
  "provider": "阿里 Qwen(百炼 token-plan 兼容模式) · qwen3.8-max",
  "signature": "…", "generated_at": "…", "reason": "ok",
  "detail_pending": 433, "elapsed_s": 0.4, "note": "…"
}
```

- `degraded=true` + `basis="distance"` = **没有 AI 参与**(没 key / 调用失败),此时
  `reason` ∈ `no_key` / `llm_error:*` / `unparsable`;前端要如实标注"按距离推荐(原因…)"。
- `items=[]` 且 `reason="no_candidates"` = 该分段还没入库目的地 → 提示"先搜索/抓取该分段"。

### 2.2 `GET /api/places/details?origin=&band=&category=&limit=10&place_ids=1,2,3`

```jsonc
{
  "origin_city": "上海", "band": "50_100", "category": null,
  "items": [{"place_id": 123, "text": "两句半的重点介绍。"}],
  "scanned": 10, "filled": 8, "failed": 2, "pending": 425,
  "provider": "…", "reason": "ok", "written": 8, "elapsed_s": 12.3, "note": "…"
}
```

- **只给还没有长介绍的 POI 生成**(DB 即缓存);`limit` 单次上限 40;
- `place_ids` 可选(逗号分隔),用于"先给推荐条 + 首屏可见条"生成;
- `reason` ∈ `ok` / `no_key` / `all_failed` —— 失败要给可操作提示,别只转圈。

### 2.3 `GET /api/places` 新增字段 `detail_pending`(还没长介绍的条数)

### 2.4 `POST /api/collections`(收藏)

- 目的地收藏:`{"kind":"place","osm_type":"node","osm_id":42,"to_lat":..,"to_lng":..,
  "name":"📍 四明山 · 目的地","summary":{...}}`(唯一键 `(kind, ref_key, mode)`,幂等);
- 路线收藏:**后端自动**把 `summary.parent = {kind:"place", ref_key:"place:node/42", name:"四明山"}`
  写进去(用 `osm_type/osm_id` 或 `to_lat/to_lng` 派生;与目的地收藏的 `ref_key` 同口径);
- **住宿收藏**:前端要在请求体里带显式父级(它知道自己在哪个目的地面板里被收藏的):
  `"parent": {"kind":"place","ref_key":"place:node/42","name":"四明山"}`;
- 收藏列表 `GET /api/collections` 返回的每条都在 `summary.parent` 里带父级(旧数据可能没有)。

`ref_key` 规则(前端算判定键时必须一致):目的地/住宿 = `place:<osm_type>/<osm_id>`
(坐标兜底 `place:<lat 定点 7 位>,<lng 定点 7 位>`);路线 = `route:<起点坐标>-><目的地>`。

## 3. 前端落地契约(必须逐项实现,`test_frontend_features.py` 会逐条断言)

### 3.1 新增 DOM id

| id | 用途 |
|---|---|
| `placeList` | 地图下方的列表区块容器(`#mapWrap` 之后) |
| `placeListBody` | 列表内容(推荐段 + 其他目的地) |
| `placeListMsg` | 列表状态/进度提示(生成介绍进度、失败原因) |
| `detailMore` | 「继续生成介绍(还剩 N 条)」按钮 |
| `recoMsg` | AI 推荐状态(降级/失败原因) |
| `recoRefresh` | 「换一批/重新推荐」按钮(GET …&refresh=true) |
| `pinScope` | 地图显示范围开关(默认「仅推荐」,可切「全部目的地」) |

### 3.2 新增 JS 函数(名字必须一致)

- `loadRecommend()` —— 拉 `/api/places/recommend`,写入 `state.reco`,渲染推荐针与列表;
- `renderPlaceList()` —— 渲染列表:推荐条置顶(理由 + 长介绍),其余按距离;没有长介绍的
  行显示「生成介绍」入口而不是空白;
- `placeRowHtml(place, index, isReco)` —— 单行 HTML(名称/分类/距起点/介绍/⭐收藏/🚗路线入口);
- `loadDetails(batchSize, placeIds)` —— 调 `/api/places/details` 分批补长介绍,
  把 `items[].text` 写进 `state.reco.details[place_id]`,**就地更新**列表(不整页重渲染);
- `detailTextOf(place)` —— 取长介绍(没有则空串);
- `renderPinsByScope()` —— 按 `state.pinScope` 画针:`"reco"` 默认只画推荐点(用不同样式/更大
  pin 或带名称 tooltip 区分),`"all"` 画全部(沿用现有 `drawPins` 逻辑与分类配色);
- `placeFavItem(place)` / `placeFavButtonHtml(place, index)` / `addFavPlace(place, button)` /
  `syncPlaceFavButtons()` —— 目的地收藏(与既有 `favKey`/`currentFavKeys` 同一套判定);
- `favTree(items)` —— 收藏面板两层分组:目的地(有 `ref_key` 以 `place:` 开头且 `kind=="place"`
  且不是住宿)作父节点,路线(`kind=="route"` 或带 `summary.parent`)与住宿(`summary.stay_kind`
  或名字带「· 住宿」)按 `summary.parent.ref_key` 挂到对应父节点下;没有父级的旧收藏放
  「未归类」区块(不能丢、不能报错);
- `stayParentPayload(place)` —— 由当前打开面板的目的地生成 `{kind, ref_key, name}`,
  供 `addFavStay` 带进收藏请求。

### 3.3 行为要求

- **首屏不阻塞**:`loadPlaces()` 拿到目的地后立刻画针 + 渲染列表骨架,**推荐与长介绍异步**补;
  任一异步失败只在 `recoMsg`/`placeListMsg` 里给中文原因,不弹 `alert`、不抛未捕获异常。
- **懒加载**:长介绍默认一次 10 条(推荐条优先),成功后自动继续下一批,直到 `pending==0`
  或用户点停;每次请求都带 `limit`;不要一次请求几百条,不要阻塞 UI。
- **地图默认仅推荐**:切换 `pinScope` 时重画针并保持范围圈不变;`state.pinScope` 默认 `"reco"`。
- **popup 里加收藏按钮**:`popupHtml` 输出 `placeFavButtonHtml(place, index)`;已收藏显示
  「★ 已收藏」并禁用(与既有路线/住宿收藏按钮同款交互)。
- **收藏面板两层**:渲染顺序 = 目的地节点(⭐收藏按钮/名称/分类/距离)→ 缩进的路线多条
  (方式/时长/费用)+ 住宿多条(名称/预估价);每条都能单独取消收藏(沿用 `deleteFav`);
  行数不少、层级清晰(用缩进 + 小标题「🚗 路线 · N 条」「🛏️ 住宿 · N 条」)。
- **零回退**:阶段1/2/3/4/5 的既有 id、函数、文案、接口调用一个不能少
  (`test_frontend_routes.py` 会兜底)。
- 前端**不许**硬编码分段 key(如 `"0_50"`),分段一律从 `state.meta.bands` 来。

## 4. 硬约定(风格)

- 原生 JS,无构建步骤、**不引入任何新依赖**;Leaflet 走既有 CDN + SRI 标签;
- 事件一律 `addEventListener` + `data-*` 委托,**禁止内联 `onclick=`**、禁止 `alert`/`document.write`;
- HTML 一律过 `esc()`;文案中文、与既有语气一致;失败提示要能指导下一步操作;
- 数值展示沿用既有 `fmtKm/fmtDuration/fmtCost/num` 等函数,别重复造。

## 5. 验收命令(必须自己跑绿)

```bash
cd /mnt/projects/Where2Go-Planner/backend
../.venv/bin/python -m pytest -q          # 全绿(当前基线 526 passed + 本单新增)
node --check /dev/null 2>/dev/null; echo ok   # 可选:语法校验由测试覆盖
```

完成后 `git add -A && git commit -m "TASK-FE: 功能1~4 前端(仅推荐上地图 + 地图下列表懒加载长介绍 + 目的地收藏与两层收藏面板)"`,
**不要 push**。
