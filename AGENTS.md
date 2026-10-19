# Where2Go 仓库速查(新接手的 Agent 先读这份,不要全仓探索)

Python 3.13 + FastAPI + SQLAlchemy 2.0 + SQLite,前端是原生 JS 单页(无构建步骤)。venv 在仓库根 `.venv/`。
测试:`cd backend && ../.venv/bin/python -m pytest -q`(基线 **1739 passed**,约 126s,全 mock 不触网)。**改动后必须实跑,基线只增不减。**
规格唯一事实源:`01-功能需求清单.md`、`02-项目计划与架构.md`;任务与历史验收见 `docs/NIGHTLY-QUEUE.md`(1251 行,按需检索别通读)。
**交接文档在 `docs/HANDOVER/`** —— 架构/流程/踩坑/部署/现状/配置六份,接手第一天按 `docs/HANDOVER/README.md` 的顺序读。
铁律:**只 commit 不 push**(推远端要单独授权);**不代订、不抓实时价**;**不新增第三方依赖**。

## 数据源口径(2026-10 现状,两条轨)
- **境内走高德**:POI 检索 / 地理编码 / 驾车 / 前端地图。动这块前必读 `docs/TASK-9-CONTRACT.md`,硬数字:v3 才有正确翻页(v5 翻页是坏的)、单查询 200 条上限、`radius` 截断在 50000、`tolls` 恒 0 与 `cost` 恒 null(过路费只能用 `toll_distance` 算)、QPS 10021 需 0.6s 节流、JS key 走 `/api/map-config` 绝不进 git。
- **境外走 OSM 系**:`data_sources/osm_overseas.py`(Overpass)+ `osrm.py` + Leaflet 第二套地图引擎。**Nominatim 与 Overpass 在国内直连不通,必须配 HTTPS_PROXY**。中文境外城市名的行政区级判据在 `photon.admin_place_verdict()`(三态,不可简化成"只看国家码")。
- 代理口径唯一开关:`data_sources/_common.build_session(user_agent, source=...)`,可用 `WHERE2GO_PROXY_<源>` 覆盖。别一刀切。

## 目录与模块契约(签名以源码为准)
- `backend/db/models.py` — 全部 15 张 ORM 表(places/segment_fetch/origin_cache/collections/collection_cats/stays/stay_query_cache/trip_plans/place_recommendations/place_details/place_highlights/place_media/planner_sessions/planner_messages/app_config)。工具:`utcnow() iso_utc() clean_text() COORD_PRECISION=7`。**新表也加在这里**。
- `backend/db/base.py` — engine + `get_session`;SQLite 默认落 `backend/data/where2go.db`,容器内 `WHERE2GO_DB_URL=sqlite:////data/where2go.db`。
- `backend/services/place_loader.py` — **主链路**(地理编码→分段检索→归类→upsert),新数据层照抄它的结构。`place_identity()` 定义幂等身份。
- `backend/services/region_gate.py` — 境内外判定;境外下的采信规则有坑,改前读 `docs/HANDOVER/03-pitfalls.md` B 节。
- `backend/services/intro.py` — LLM 抽象(OpenAI 兼容);无 key 时降级为空串,**绝不抛**。多条目/长文必须按调用放大 `max_tokens` 与超时。
- `backend/services/` 其余模块(planner/assistant/recommend/details/highlights/place_media/stays/routes/trips/ota_links/bands/classify/categories/amap_categories/app_config/seed_data/reclassify)一行职责见 `docs/HANDOVER/01-architecture.md` 的表。
- `backend/app/api/*.py` — 9 个路由模块,统一风格:**裸 JSON + 400 中文报错**(不用 pydantic Body 模型,避免 422),响应带 `note` 说明口径。`app/main.py` 以 prefix="/api" 挂载。
- `backend/app/static/index.html` — 单页前端(5163 行,含两套地图引擎与全部面板)。改它**不需要重启** uvicorn;改任何 `.py` 都要重启。
- `backend/app/static/setup.html` — `/setup` 四步配置向导。
- `docker-entrypoint.sh` / `Dockerfile` / `docker-compose.yml` — 容器入口(校验 /data 可写 + **单 worker**)、镜像定义、compose。

## 写码硬约定
- 一切外部调用(高德/Photon/Nominatim/Overpass/OSRM/LLM/requests)必须可在测试里替换;`conftest.py` 的 `no_network` autouse fixture 会把真实网络调用变成抛错。
- 坐标入库定点 7 位小数(`models.COORD_PRECISION`);前端做幂等判定必须复刻同样位数。`Collection.mode` 用空串不用 NULL。
- 分类去重优先级:滑雪 > 运动 > 人文美食 > 自然;去重键境内用高德 POI id(`osm_type="amap"`,crc32 后入库),OSM 行用 (type,id)。
- 金额/时长一律标"估算"或"非实时",release 模式也必须保留这些标注(有测试守着)。
- 改口径要同步四处:队列头部通告、本文件、相关契约与交接文档、夜班 cron 提示词。
- 完成后 `git commit`(消息带任务号或作用域),**不要 git push**;并在 `docs/NIGHTLY-QUEUE.md` 对应条目回填通过数与 commit 短哈希。
