# Where2Go 仓库速查(Codex 冷启动先读这份,不要再全仓探索)

Python 3.13 + FastAPI + SQLAlchemy 2.0 + SQLite。venv 在仓库根 `.venv/`。
测试:`cd backend && ../.venv/bin/python -m pytest -q`(基线 **792** passed,全 mock 不触网)。
规格唯一事实源:01/02 需求架构文档 + docs/STAGE*-PLAN.md;任务契约见 docs/NIGHTLY-QUEUE.md。
**2026-10-01 起数据源全面切高德**(POI 检索/地理编码/驾车路线/前端地图;Overpass 与 OSRM 移除、Photon/Nominatim 留作降级)
→ 动 TASK-9* 代码前**必读 `docs/TASK-9-CONTRACT.md`**(v3 only、单查询 200 条上限、radius≤50km 需分格、高德 `tolls` 恒 0 用 `toll_distance` 算过路费、JS key 不进 git 走 /api/map-config)。

## 目录与模块契约(一行一个,签名以源码为准)
- `backend/db/models.py` — 全部 ORM 表:Place(唯一键 osm_type+osm_id+origin_city)/ SegmentFetch / Collection(唯一键 kind+ref_key+mode,mode 用空串不用 NULL)/ CollectionCat。工具:`utcnow() iso_utc() clean_text() COORD_PRECISION=7`。**新表也加在这里**。
- `backend/db/base.py` — engine + `get_session`(FastAPI Depends 用),SQLite 落 `backend/data/where2go.db`,`WHERE2GO_DB_URL` 可覆盖。
- `backend/db/repository.py` — upsert/序列化(`upsert_places() place_to_dict()` 风格;新表照此加对应函数)。
- `backend/data_sources/amap.py`(新,高德 v3 数据源:geocode/reverse_geocode/search_around/search_polygon/driving/grid_polygons)。`backend/data_sources/overpass.py` 与 `osrm.py` — **TASK-9c 后物理删除(`git rm`)**,存量签名如下仅供参考(勿在新代码里引用):端点链+重试。核心:`build_grouped_query(lat,lng,radius_m,groups,...)`、`OverpassClient.nearby_places_grouped(...)` 返回**已按距离排序**、带 osm_type/osm_id;`parse_places(payload,lat,lng,limit,require_name,with_id=True)`;`haversine_km()` 也在本模块。
- `backend/services/intro.py` — LLM 抽象:provider 注册表(DeepSeek/Qwen,OpenAI 兼容 /chat/completions),key 只读 env(`WHERE2GO_LLM_*` 或 `DEEPSEEK_API_KEY`);`resolve_provider() LLMClient.extract_completion() clean_intro()`;一切异常/无 key 降级为**空串,绝不抛**。
- `backend/services/place_loader.py` — Overpass→环内收敛→归类→upsert 的完整套路(新数据层照抄它的结构);`place_identity()` 定义幂等身份。
- `backend/services/bands.py` — 环形分段与 `filter_to_band`(haversine 复核)。
- `backend/app/api/*.py` — 路由风格统一:**裸 JSON + 400 中文报错**(不用 pydantic Body 模型,避免 422);`_optional_text()` 处理单测直调时的 FieldInfo;响应都带 `note` 口径说明。`app/main.py` include_router 挂 `/api`。
- `backend/app/static/index.html` — 单页(2026-10-01 起地图层由 Leaflet 改**高德 JS API 2.0**,TASK-9d)(改它不需要重启 uvicorn;~1550 行/26K tokens,256K 窗口下 Codex 可胜任——9/26 TASK-5b 前端首战通过;更大范围重构仍建议评估峰值上下文)。

## 写码硬约定
- 全部外部调用(高德/Photon/Nominatim/LLM/requests)必须可在测试里被替换;测试把 `requests.Session.request` 换成抛错来兜底(no_network autouse fixture 见 conftest.py / test_classify.py)。
- 坐标入库定点 7 位小数(models.COORD_PRECISION);幂等判定键必须复刻后端唯一键规则。
- 分类/去重优先级:滑雪>运动>人文美食>自然;去重键 **高德 POI id**(`osm_type="amap"`),存量 OSM 行按 (type,id)。
- 不新增第三方依赖;不改 POC 路由行为;金额/时长估算必须带"估算"标注字段。
- 完成后只 `git commit`(消息带任务号),**不要 git push**。
