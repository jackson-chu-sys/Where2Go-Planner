# TASK-21 契约:境外 POI 检索链(Overpass 复活 + region 分支)

> 依赖 TASK-20(region_gate + OriginCache.region 已落地,验收 done 后才开工)。
> 本契约**逐字执行**。只读清单(≤6 处,读完立即写码):
> ①`git show 2845eb1^:backend/data_sources/overpass.py`(1093 行,完整历史实现,**恢复它的唯一权威来源**);
> ②`backend/services/classify.py`(OSM_SEARCH_GROUPS 仍在、classify_places/dedupe_places/categorize 未删);
> ③`backend/services/place_loader.py`(default_fetcher/to_place_items/load_segment/scale_search_groups);
> ④`backend/app/api/places.py`(list_places);⑤`docs/TASK-20-CONTRACT.md`(region 键口径);⑥`git show 2845eb1^:backend/test_data_sources.py`(overpass 测试样板,考古替身写法)。
> 写码硬约定见 AGENTS.md(不新增依赖、全 mock、只 commit 不 push)。

## 0. 实测依据(2026-10-10/11,勿重测)

- Overpass 直连可用:因特拉肯 5km 圆四分类并集 8.5s 返 30 条(含 name);端点链 `overpass-api.de` / `z.overpass-api.de` / `maps.mail.ru`(阶段1 口径,历史 overpass.py 已内置)。
- 境外 OSM 数据充足(自然/景点 tag 齐全);阶段1 冷抓慢(单组 20~380s)由**渐进配额**(target_total=30×轮)天然缓解,首查只抓 30 条。
- `data_sources/_common.py` 现状代理表无 overpass 键;历史口径 `"overpass": PROXY_OFF`(直连 2.4s、代理 504)。

## 1. 落地契约

### 1.1 恢复 `backend/data_sources/osm_overseas.py`

- `git show 2845eb1^:backend/data_sources/overpass.py > backend/data_sources/osm_overseas.py`,然后**只做三处修改**:
  1. 模块 docstring 首行改为「Overpass API(境外轨):按 tag 检索周边 POI —— TASK-21 从 git 历史恢复(原 overpass.py,2845eb1 删除),仅服务 region=overseas;境内一律走 amap.py。」
  2. 文件内 `SOURCE_NAME = "overpass"` 改为 `SOURCE_NAME = "overpass"`(**不改**,保留原值——_common 代理表按源名查)。
  3. 若历史文件 import 了已不存在的模块(如 verify_poc),删该 import;其余**零改动**(build_grouped_query/build_grouped_ring_query/nearby_places_grouped/nearby_places_ring/parse_places/haversine_km 等全部原样保留)。
- `_common.py` 的 `DEFAULT_SOURCE_PROXY` 加 `"overpass": PROXY_OFF`(注释:境外轨 POI 源,10/10 实测直连 8.5s 可用、代理反而 504)。

### 1.2 `backend/services/place_loader.py` region 分支

- 顶部 import:`from data_sources import osm_overseas`(照 amap import 风格);`SOURCE_OSM = "osm"`(SegmentFetch.source 用)。
- 新函数 `default_fetcher_osm(lat, lng, band, *, groups=None, fetch_rounds=None, environ=None, session=None) -> list[dict]`:
  - groups 缺省用 `classify.OSM_SEARCH_GROUPS`(budget 合计 540 与 amap 组一致);
  - `band_inner_radius_m(band) <= 0` → `osm_overseas.OverpassClient(...).nearby_places_grouped(lat, lng, radius_m=band_radius_m(band), groups=groups, with_id=True)`(单圆);
  - 否则 → `nearby_places_ring(lat, lng, outer_m=high, inner_m=low, groups=groups, with_id=True)`(环形差集,阶段1 TASK-1d 口径);
  - 返回行形状 = parse_places with_id=True 的 `{name, lat, lng, tags, osm_type, osm_id}`(**不含 typecode**);
  - 渐进:groups 传入前由调用方(load_segment)缩放,本函数不管 target_total;
  - **不做本地 haversine 收敛**(nearby_places_ring/grouped 已内置环内过滤与距离排序——恢复的历史实现自带,读 ①确认后照用;若历史实现需要调用方收敛,则在函数内用 osm_overseas.haversine_km 收敛到 [low,high))。
- `scale_search_groups(target_total, groups=None)` 加第二参:None → 现状 SEARCH_GROUPS(amap);给了 → 对传入组缩放(公式与 FULL_SEARCH_BUDGET=540 口径不变)。
- `to_place_items(candidates, *, region=REGION_DOMESTIC)`:region=overseas 时走 `classify.classify_places(candidates)`(OSM tag 归类,优先级滑雪>运动>人文美食>自然)+ `place_identity(row)` 身份(osm_type=node/way/relation + 原生 id,**不 crc32**)+ `tags = dict(row tags) + {"coord_system": "WGS-84"}`(不写 source 键 → place_source 派生「OSM」);domestic 分支现状零改动。region 常量从 services.region_gate import(TASK-20 产物)。
- `load_segment(...)` 增关键字参数 `region: Optional[str] = None`:
  - None → 查 `repo.get_origin_cache(session, city=city)`,有行且 region 非空用之,否则 domestic(**缓存兜底判定,不发额外网络**);
  - region=overseas → fetcher 用 default_fetcher_osm、scale 传 OSM_SEARCH_GROUPS、水位 `source=SOURCE_OSM`、to_place_items(region=overseas);
  - domestic → 现状零改动(amap);
  - `fetcher=` 显式注入优先于 region 分支(测试口径不变);
  - intro 异步回填/渐进/water-mark/去重口径全部共用现状代码,不分轨。

### 1.3 `backend/app/api/places.py`

- `list_places` 增可选参数 `region: Optional[str] = None`(照 page_size 的裸 None + 手工校验模式:非法值 → 400 中文「region 只支持 domestic 或 overseas」;合法或缺省 → 透传 load_segment)。
- 响应**只加一个键** `"region"`(本次实际生效值:override > OriginCache > domestic);既有键(含 TASK-6b 分页键)逐键零变化。
- `/api/places/intros`、`/api/places/media` 等其余端点零改动(LLM 简介对境外行天然可用——候选名来自 OSM tags.name)。

### 1.4 前端(本条最小改动)

- `index.html`:loadPlaces/loadMorePlaces 的 `/api/places` URL,当境外模式开关(TASK-20 的 #regionOverride)勾选时追加 `&region=overseas`;状态栏来源文案 region=overseas 时显示「OSM 实时抓取」(沿用既有 source 文案分支,新增 osm 键)。**地图渲染切换是 TASK-23(P4),本条不动地图层**——境外 pin 在高德地图上会偏移显示(WGS-84 当 GCJ-02 用),属预期过渡态,前端在 region=overseas 时于状态栏加提示「境外数据(WGS-84),地图切换见后续版本」。

## 2. 测试契约(全 mock,≥20 新增)

新文件 `backend/test_osm_overseas.py`(替身样板考古 ⑥):
1. osm_overseas 恢复完整性:build_grouped_query/build_grouped_ring_query/parse_places(with_id)/nearby_places_grouped/nearby_places_ring 存在且可用假 session 跑通(≥4 例,含环形差集语句断言 `( … ; - … ; )` 与 out center);
2. default_fetcher_osm:0_50 单圆路径/环形路径/组配额透传/with_id 行形状(≥4 例);
3. to_place_items(region=overseas):OSM 归类优先级(滑雪 tag 行不入运动)/身份=(node,原生id) 不 crc32/tags.coord_system=WGS-84/无 source 键 place_source→「OSM」;domestic 分支零变化断言(≥5 例);
4. load_segment(region=overseas):假 fetcher 注入 → 入库行身份/tags/水位 source="osm";二次查询读库零网络;渐进 target_total 对 OSM_SEARCH_GROUPS 缩放(sum≈30);region=None 时查 OriginCache 兜底(mock repo)(≥5 例);
5. API:/api/places 响应含 region 键、既有键集逐键零变化(LEGACY 断言)、非法 region 400 中文、region=overseas 透传 load_segment(计数替身)(≥4 例);
6. 前端静态断言(test_frontend_routes.py 追加):region=overseas 拼接/状态栏 OSM 文案/开关勾选联动(≥3 例)+ node --check。

既有基线(TASK-20 完成后的数字,**以实跑为准**)零回归。

## 3. 验收与禁止项

- pytest 全绿(基线=TASK-20 验收后的实际数字,只增不减);`node --check`;commit 消息带 `TASK-21`;只 commit 不 push。
- **禁止**:改 amap.py/region_gate.py 的既有行为;改 classify.py 的 SEARCH_GROUPS 指向(境内检索组不动);改 index.html 地图层;动 collections/trips/planner/assistant;新增第三方依赖。
- 真机冒烟(执行器做,Codex 不需要):重启 uvicorn → `/api/geocode?city=Interlaken` 确认 region=overseas 落缓存 → `/api/places?origin=Interlaken&band=0_50&page_size=15&region=overseas` 冷抓出 OSM 行(source=OSM、坐标 WGS-84 在因特拉肯周边)→ 二次查询 source=db 零网络。
