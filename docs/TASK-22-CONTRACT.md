# TASK-22 契约:境外路线 + 住宿(OSRM 驾车复活 + OSM 住宿腿)

> 依赖 TASK-21(region 分支的 place_loader 已落地,验收 done 后才开工)。
> 本契约**逐字执行**。只读清单(≤6 处,读完立即写码):
> ①`git show 2845eb1^:backend/data_sources/osrm.py`(275 行,完整历史实现,恢复的唯一权威来源);
> ②`backend/services/routes.py`(driving_route/default_router/plan_routes/费用引擎段 L300~460、L1100~1360);
> ③`backend/services/stays.py`(search_stays/fetch_stay_pois/stay_kind 的 OSM 分支仍在/stay_city);
> ④`backend/app/api/routes.py`(list_routes)、⑤`backend/app/api/stays.py`;⑥`docs/TASK-21-CONTRACT.md`(region 透传口径)。
> 写码硬约定见 AGENTS.md。

## 0. 实测依据(勿重测)

- OSRM demo 服务器境外可用:因特拉肯→格林德瓦 `router.project-osrm.org/route/v1/driving/7.863,46.686;8.036,46.624` 直连 1.3s,distance=19.7km/duration=24min 正确;demo 服务器**无 tolls/steps 数据**(response 里 legs[].steps 通常空)→ 境外过路费只能走 heuristic(总里程×0.55×区域费率)。
- `region_by_longitude`(routes.py L308)是**中国境内东/中/西**费率分区——境外坐标会落到「西」档,费率 0.35 元/km 对境外并不成立。境外过路费口径:**保留公式但 note 里明示「境外费率按中国西部档估算,仅供参考」**(不新造国别费率表,神朱拍板的最小工程量原则)。
- stays.py 的 `stay_kind` OSM 分支(tourism tag 命中 STAY_TAGS)与 `search_stays` 的 Overpass 实现都还在 git 历史(TASK-9b 前的版本),现版只走 amap.search_around(types="100000")。

## 1. 落地契约

### 1.1 恢复 `backend/data_sources/osrm.py`

- `git show 2845eb1^:backend/data_sources/osrm.py > backend/data_sources/osrm.py`,零改动恢复(SOURCE_NAME="OSRM"、DEFAULT_ENDPOINT、parse_route/parse_geometry/parse_steps、OsrmClient、模块级 route() 函数);若历史文件 import 了已删模块,只删该 import。
- `_common.py` 代理表加 `"osrm": PROXY_OFF`(境外轨驾车,10/10 实测直连 1.3s)。
- `data_sources/__init__.py` 若历史版本导出过 osrm 符号,按现仓库风格补导出(不破坏既有导出)。

### 1.2 `backend/services/routes.py` region 分支

- 新函数 `osrm_leg(leg: Mapping) -> dict`:把 osrm.route() 返回的 `{distance_m, duration_s, geometry, steps}` 归一成 `amap_leg` 同形状(`distance_km/duration_min/toll_distance_km=None/traffic_lights=None/steps_n/geometry`)——**toll_distance_km 恒 None**(OSRM demo 无收费数据,费用引擎自动退 heuristic,该退化路径已有测试)。
- `default_router_osrm(start_lnglat, end_lnglat)`:调 `osrm.route(...)`(WGS-84 坐标直接用,不换序——OSRM 是 lng,lat 口径,与 RouterFn 入参一致),返回 osrm_leg(...)。失败抛 DataSourceError(driving_route 已有降级捕获)。
- `driving_route(...)` 增关键字参数 `region: Optional[str] = None`(None=domestic):overseas → `router = router or default_router_osrm`;cost_breakdown 的 note 追加境外口径文案(见 §0);domestic → 现状零改动(amap)。`toll_mode` 境外恒 heuristic(既有常量,不新增)。
- `plan_routes(...)` 增 `region: Optional[str] = None` 透传给 driving_route;铁路/飞机估算腿**不分轨**(里程公式与国别无关;RAIL_SEED_FARES 境外城市对不命中→自然走费率估算;机场表不命中→机票不出价仅 deep-link,均为既有降级路径,零改动)。
- `mode_rules()/cost_coefficients()` 零改动。

### 1.3 `backend/app/api/routes.py`

- `list_routes` 增可选参数 `region: Optional[str] = Query(None)`(非法值 → 400 中文,同 TASK-20/21 口径);响应**只加 `"region"` 键**(生效值);其余键逐键零变化。

### 1.4 `backend/services/stays.py` region 分支

- `fetch_stay_pois(lat, lng, radius_m, ...)` 增 `region: Optional[str] = None`:overseas → 用 TASK-21 恢复的 `osm_overseas`(Overpass)按 `tourism in STAY_TAGS` 单圆检索(阶段1 历史实现:`git show <TASK-9b前>:backend/services/stays.py` 里 search_stays 的 Overpass 版本可考古,但**注意**现版函数签名/负缓存/阶梯已重构——只把「数据源调用」段按 region 分支,阶梯/负缓存/估价/price_kind 全共用现状代码);行归一为现版 fetch 行形状(osm_type/osm_id/name/kind/lat/lng/tags);kind 用现成 `stay_kind` 的 OSM 分支。
- `load_stays(...)` 增 `region` 透传;`Stay` 身份:境外行 osm_type=node/way/relation + 原生 id(与 TASK-21 Place 同口径,**不 crc32**);坐标定点 7 位;`stay_city`:境外行用 Nominatim reverse(include_country=True,TASK-20 扩展)取 address.city||town||village,失败 None(不阻塞;懒补齐口径不变)。
- 半径阶梯/负缓存(StayQueryCache 键含坐标+radius,region 不进键——同一坐标不会既是境内又是境外,零冲突)/批量 LLM 估价/规则表全不分轨。

### 1.5 `backend/app/api/stays.py`

- `/api/stays` 增可选 `region` 参数(非法 400 中文);响应**只加 `"region"` 键**;items 既有键零变化。

### 1.6 前端(最小改动)

- index.html:境外模式勾选时 `/api/routes` 与 `/api/stays` 调用追加 `&region=overseas`(与 TASK-21 的 /api/places 同款开关);路线卡片 toll_mode=heuristic 且 region=overseas 时 note 已有境外文案(后端拼,前端透出即可,零改动);住宿卡片零改动。

## 2. 测试契约(全 mock,≥15 新增)

新文件 `backend/test_routes_overseas.py` + `backend/test_stays_overseas.py`(替身套路照 test_routes.py/test_stays_v2.py):
1. osrm 恢复完整性:parse_route/parse_geometry 假 payload 跑通、osrm_leg 归一形状(toll_distance_km=None)、default_router_osrm 失败抛 DataSourceError(≥3 例);
2. driving_route(region=overseas):router 默认走 osrm(注入假 osrm.route 断言被调、amap.driving 零调用)、toll_mode=heuristic、note 含境外口径文案;domestic 零变化(amap 被调、toll_mode 照旧)(≥4 例);
3. plan_routes 透传 region;铁路/飞机腿不受 region 影响(境外坐标对:铁路 ≥100km 出、机票机场表不命中→无价有 link)(≥2 例);
4. API /routes?region=overseas 响应含 region 键、既有键集零变化、非法 region 400 中文(≥2 例);
5. stays overseas:OSM 腿检索(假 Overpass payload)→ kind 归一(tourism=hotel→hotel)/身份 node 原生 id/负缓存共用(同坐标同半径二次零网络)/stay_city 境外 Nominatim(mock)取值+失败 None 不阻塞(≥4 例);
6. API /stays?region= 同款断言(≥2 例);
7. 前端静态断言:routes/stays URL 追加 region 参数接线(≥2 例)+ node --check。

既有基线(TASK-21 后实际数字)零回归。

## 3. 验收与禁止项

- pytest 全绿(只增不减);node --check;commit 带 `TASK-22`;只 commit 不 push。
- **禁止**:改境内(amap)路线/住宿行为;改费用系数/种子票价/机场表;改负缓存/阶梯/估价结构;动 collections/trips/planner;新增依赖。
- 真机冒烟(执行器做):uvicorn 重启 → `/api/routes?from_lat=46.686&from_lng=7.863&to_lat=46.624&to_lng=8.036&region=overseas` 驾车 kind=real(OSRM ~19.7km/24min)→ `/api/stays?lat=46.686&lng=7.863&region=overseas` 出因特拉肯住宿(OSM 行,kind 正确)→ 境内同款请求(上海)零回归。
