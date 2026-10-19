# 01 · 架构与代码地图

## 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 后端 | Python 3.13 + FastAPI | 单进程,容器内必须 `--workers 1`(见 06 配置热生效) |
| ORM | SQLAlchemy 2.0 + SQLite | 无迁移框架,建表靠 `init_db()`;新表加在 `db/models.py` |
| 前端 | 原生 JS 单页 + 两套地图引擎 | 高德 JS API 2.0(境内)/ Leaflet(境外),无构建步骤、无 npm |
| 测试 | pytest | 全 mock,`no_network` autouse fixture 兜底,不触网 |
| 打包 | Docker(multi-arch)+ GHCR | GitHub Actions 构建,amd64 + arm64 |

规模:`backend/` 非测试代码约 21,500 行,测试约 28,400 行(测试比代码多,这是刻意维持的)。

## 目录结构

```
backend/
  app/main.py             FastAPI 装配:include_router(prefix="/api") + /setup 页面 + 静态文件
  app/api/*.py            9 个路由模块(见下表"API 端点")
  app/static/index.html   单页前端(5163 行,全部 UI 在这里)
  app/static/setup.html   配置向导页(4 步)
  services/*.py           业务层(18 个模块,见下)
  data_sources/*.py       外部数据源客户端(8 个)
  db/{models,base,repository}.py
  test_*.py               测试(与 services / api 一一对应)
docs/                     开发过程记录 + 契约 + 交接文档(你正在读的)
tools/                    一次性脚本(性能探针、数据源实测、调研脚本)
docker-compose.yml / Dockerfile / docker-entrypoint.sh
```

## 服务层职责(`backend/services/`)

| 模块 | 职责 |
|---|---|
| `place_loader.py` | **主链路**:地理编码 → 分段检索 → 归类 → upsert;境内外分流、OriginCache 缓存、渐进抓取都在这里 |
| `region_gate.py` | 境内外判定(`detect_region`),境外下决定走 OSM 链还是高德链 |
| `bands.py` | 环形距离分段(0-50/50-100/100-200/200-300/300-500 km)与 `filter_to_band` |
| `classify.py` / `categories.py` / `amap_categories.py` | 四分类归属与去重优先级(滑雪 > 运动 > 人文美食 > 自然) |
| `intro.py` | LLM 抽象层(OpenAI 兼容),`LLMClient`;无 key 时**降级为空串,绝不抛** |
| `details.py` / `highlights.py` / `place_media.py` | 长介绍、要点、图片(高德图 + 维基兜底,带坐标门控) |
| `recommend.py` | AI 推荐 3~5 条目的地(缓存按候选指纹) |
| `planner.py` / `assistant.py` | AI 行程规划 / 全局问答助手(自己拼 session,不走通用 chat) |
| `routes.py` | 驾车(高德)/ 铁路 / 飞机(后两者是经验估算,无实时班次源) |
| `stays.py` | 住宿检索 + AI 批量估价(后台线程,不阻塞请求) |
| `trips.py` / `ota_links.py` | 行程方案 / OTA 外链(携程、Booking、Airbnb、12306) |
| `app_config.py` | `/setup` 向导的配置读写 + 注入 `os.environ`(见 06) |
| `seed_data.py` / `reclassify.py` | 人工种子数据(国内滑雪/运动垫底)/ 存量数据重分类 |

## 数据源层(`backend/data_sources/`)

| 模块 | 用途 | 代理口径 |
|---|---|---|
| `amap.py` | 高德 Web 服务 REST:**境内** POI 检索、地理编码、驾车 | 直连 |
| `osm_overseas.py` | Overpass:**境外** POI 检索 | 沿用环境代理(国内直连 502) |
| `osrm.py` | OSRM:**境外** 驾车路线 | 直连 |
| `photon.py` | Photon:地理编码/逆地理(降级腿 + 行政区级判据) | 直连 |
| `nominatim.py` | Nominatim:地理编码/逆地理(境外主腿) | 沿用环境代理(国内直连不通) |
| `wikimedia.py` | 维基百科/Commons 图片兜底 | 沿用环境代理 |
| `ctrip.py` | 携程城市/酒店 ID 解析(为 OTA 外链) | 直连 |
| `_common.py` | `build_session(user_agent, source=...)`:**所有代理口径的唯一开关** | — |

改任何一个数据源前,先读 `03-pitfalls.md` 对应小节。

## 数据表(15 张,均在 `db/models.py`)

`places`(唯一键 `osm_type+osm_id+origin_city`)、`segment_fetch`(分段抓取水位)、`origin_cache`(起点解析缓存,7 天)、`collections` + `collection_cats`(收藏与分类,唯一键 `kind+ref_key+mode`)、`stays` + `stay_query_cache`(住宿与负缓存)、`trip_plans`(行程方案)、`place_recommendations`(推荐缓存)、`place_details` / `place_highlights` / `place_media`(内容缓存)、`planner_sessions` + `planner_messages`(AI 对话)、`app_config`(向导配置)。

两个必须复刻的约定:
- 坐标入库定点 **7 位小数**(`models.COORD_PRECISION`)。前端做"是否已收藏"这类幂等判定时,必须按同样位数比较,否则判定静默失效。
- `Collection.mode` 用空串不用 NULL(唯一索引对 NULL 的处理会破功)。

## API 端点(全部挂在 `/api`,裸 JSON,错误一律 400 + 中文说明)

| 模块 | 端点 |
|---|---|
| `places.py` | `GET /places`(主检索)、`/places/meta`、`/places/intros`、`/places/details`、`/places/media`、`/places/highlights`、`/places/recommend`、`/geocode`、`/geocode/reverse`、`/map-config` |
| `discover.py` | `GET /categories`、`POST /discover` |
| `routes.py` | `GET /routes`(驾车/铁路/飞机对比) |
| `stays.py` | `GET /stays` |
| `collections.py` | `POST/GET /collections`、`DELETE /collections/{id}` |
| `trips.py` | `POST/GET /trip-plans`、`GET/DELETE /trip-plans/{id}` |
| `planner.py` | `POST/GET/DELETE /planner/messages`、`POST /planner/save` |
| `assistant.py` | `POST/GET/DELETE /assistant/messages` |
| `config.py` | `GET/POST /config`、`POST /config/validate`、`GET /health` |

页面路由:`GET /`(主界面)、`GET /setup`(向导,配置完会 307 回首页)。

**路由风格是刻意统一的**:不用 pydantic Body 模型(避免 422 和中文报错混在框架层),手写参数解析 + `_optional_text()` 兜底,响应永远带 `note` 字段解释口径。新加接口请照抄 `places.py` 的写法。

## 一次检索请求的完整链路(理解这个就理解了项目)

```
GET /api/places?origin=上海&band=0_50&categories=...&region=...
  → places.py 解析参数
  → place_loader.load_segment()
      ├─ 读 OriginCache 命中?→ 有坐标直接用(0.03s)
      ├─ 未命中 → geocode_with_fallback()
      │     ├─ region=overseas → Nominatim 主腿 → Photon 兜底腿(判据见 03)
      │     └─ 否则 → 高德 geocode
      ├─ 读库:该 (起点, band) 是否已抓过(segment_fetch)
      │     ├─ 有 → 直接查 places 表返回(亚秒)
      │     └─ 无 → 冷抓:高德分格检索 / Overpass 境外检索(QPS 节流)→ 归类 → upsert
      └─ 返回 places + 该段还有没有更多(next_cursor)
  → 前端渲染 pin;LLM 简介/长介绍由另外的接口按"已显示批次"补齐,不阻塞主链路
```

冷抓慢是历史常态(境外 30~60s),所以 TASK-12 之后 LLM 回填全部异步;**不要把 LLM 调用塞回主链路**。

## 前端结构(`app/static/index.html`)

单文件 5163 行,没有构建步骤。结构大致是:CSS(`.ui-release` 发布版样式门控)→ HTML 骨架 → 一大段 JS(状态 `state`、两套地图引擎、面板渲染、事件绑定)。

- 改它**不需要重启 uvicorn**(StaticFiles 每次请求读盘),浏览器刷新即生效。
- 但改**任何 `.py`** 都要重启进程。
- `state.mapEngine` 决定用高德还是 Leaflet;`isOverseasTrack()` 是"该不该走境外链"的唯一判定,新增调用境外接口的地方必须经过它。
- release 模式(`WHERE2GO_UI_MODE=release`)会隐藏技术细节,但**合规标注(「估算」「非实时」)必须保留**,有测试守着。
