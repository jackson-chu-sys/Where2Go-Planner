# Where2Go 夜间任务队列(NIGHTLY-QUEUE)

> 夜班执行器(每晚 22:00 CST, qwen3.8-max)读取本文件,依次执行**未完成**条目。
> 队列空 → 整轮跳过(0 token)。白天把重活追加为条目;完成后更新该条状态。
> 执行顺序 = 文件内自上而下(依赖已排好)。continuity=true,单晚做不完下轮续。
>
> 条目格式:
> ```
> ## [TASK-xxx] 标题
> - 状态: pending | running | done | blocked | needs_review
> - 目标: <做什么>
> - 涉及: <文件/路径>
> - 验收: <可验证标准>
> - 结果: <夜班执行后回填>
> ```

---

## [TASK-1a] 地图骨架:Place 入库 + 检索 API + Leaflet 地图

- 状态: done
- 目标: 把 POC 文字列表升级为地图模式。后端把 Overpass 抓到的目的地落入 SQLite(Place 表),提供检索 API;前端用 Leaflet 地图按当前环形距离段渲染 pin。规格见 docs/STAGE1-PLAN.md(第2/4节)。
- 依赖: POC 已有 backend/data_sources/(OSRM/Nominatim/Overpass 可用)+ backend/app(FastAPI 试用 web)。
- 涉及: backend/(新建 models/db、改造 api、static 前端)
- 验收:
  1. SQLite Place 表(含 osm id/type、name、lat、lng、category、intro、tags),SQLAlchemy
  2. Overpass 结果按(城市,band)抓取后落库,已入库城市二次查询不再触发网络(读库)
  3. 检索 API: GET /api/places?origin=&band=&category= 返回该段内已入库目的地
  4. 前端 Leaflet 地图:起点为中心画距离环;band 内 Place 以 pin 渲染;点 pin 出弹窗(名称/分类/距起点)
  5. 依赖写 backend/requirements.txt(leaflet 走 CDN,不打包)
  6. pytest backend/ 通过
- 结果: **完成**(commit `3302ace`,2026-09-09)。
  - 存储:新增 `backend/db/`(models/base/repository)。`Place` 表含
    osm_type/osm_id/name/lat/lng/category/intro/tags/origin_city/band,
    唯一键 `(osm_type, osm_id, origin_city)` 防重;`SegmentFetch` 记 (城市, band) 抓取水位。
    SQLite 落 `backend/data/where2go.db`(已 gitignore),`WHERE2GO_DB_URL` 可覆盖。
  - 抓取入库:新增 `backend/services/`(bands/categories/place_loader)。按分段**上限半径**
    查 Overpass + haversine 收敛到环内(复用现有 overpass.py,解析层加 opt-in `with_id=True`);
    **已入库 (城市, band) 二次查询直接读库、零网络请求**;`refresh=true` 强制重抓不产生重复行。
    分类为阶段1a 简化归类(滑雪/运动/人文美食/自然/其他),四分类优先级去重与 LLM 简介留给 TASK-1b,
    `category`/`intro` 字段已预留(重抓不覆盖已生成的 intro)。
  - API:`GET /api/places?origin=&band=&category=`(+可选 lat/lng/refresh)、
    `GET /api/places/meta`、`GET /api/geocode?city=`(复用 Nominatim)。
    POC 的 `/api/discover`、`/api/categories` 行为不变;分段定义收敛到 services.bands 只出一份。
  - 前端:`backend/app/static/index.html` 改为 Leaflet 1.9.4(CDN,带 SRI)+ OSM 瓦片的地图页,
    原生 JS 无 React。起点为中心画 band 环形圈(外圆=上限、虚线内圆=下限),pin 按分类着色,
    弹窗显示名称/分类/距起点/OSM id;可切 band、切分类、城市搜索、重新抓取;CDN 挂了有降级提示。
    原 POC 列表页保留为 `list.html` 并互链。
  - 依赖:`backend/requirements.txt` 加 `sqlalchemy>=2.0,<3`(Leaflet 走 CDN 不打包)。
  - 测试:新增 `backend/test_places.py` 20 个用例(网络全 mock),覆盖落库、二次查询不触网、
    换 band 分别入库、refresh 不重复、表结构/唯一键、API 过滤与校验、POC 路由未破坏。
    `pytest backend/` = **46 passed**(原 26 + 新 20),原有用例零改动。
  - 真实链路实测(上海):`50_100` 段首抓 141 条 / 9.1s → 二次读库 141 条 / 0.01s(`source=db`,
    `network_used=false`);`100_200` 段首抓 237 条 / 41.6s(复用库内起点,未再调 Nominatim)
    → 二次 0.01s;分类过滤与非法 band/category 的 400 校验均通过。
  - 备注:OSM 国内滑雪/运动 tag 稀疏(上海两段内滑雪场 0 条),种子数据垫底属 TASK-1c。

---

## [TASK-1b] 四分类归类 + LLM 简介 + popup 卡片

- 状态: pending
- 目标: 需求四分类(自然风光/小城人文美食/滑雪场/运动)检索与**优先级归类去重**(滑雪>运动>人文美食>自然,osm id+type 去重,一地只入一类);对入库 Place 生成**一句话简介**(LLM 缓存);地图 pin 分类图标 + popup 展示分类字段。规格见 docs/STAGE1-PLAN.md 第3/4节。
- 依赖: TASK-1a(Place 表已建、地图已渲染)。
- 涉及: backend/data_sources/(归类)、backend/models、LLM 简介(复用既有 DeepSeek/Qwen key,按 POI 缓存)、前端 popup
- 验收:
  1. 四分类各有 OSM tag 识别线索;同实体跨 tag 按优先级只入一类
  2. Place.category 正确;自然/景点不再重复(修复 POC 问题)
  3. 入库 Place 有 intro(LLM 生成,已生成的不重复调用);缓存落 DB
  4. 前端 pin 按分类着色,popup 显示分类+简介
  5. pytest backend/ 通过
- 结果: (待夜班回填)

---

## [TASK-1c] 打磨:起点定位/换城 + 种子数据 + 自动 QA

- 状态: pending
- 目标: 产品打磨到可 DEMO。起点支持浏览器"我的位置"定位(Nominatim reverse)与城市搜索切换;OSM 国内缺失的滑雪/运动类补少量**种子数据**(人工坐标+简介);用 Hermes browser_exec 对本页面做一次自动 QA(能开、能查、无 JS 报错)。规格见 docs/STAGE1-PLAN.md。
- 依赖: TASK-1a + TASK-1b。
- 涉及: 前端(定位/搜索)、backend(种子数据源/脚本)、QA
- 验收:
  1. 页面能改起点城市并重定位范围圈;有"我的位置"按钮(可用则用,不可用给提示)
  2. 滑雪/运动类至少各有若干条可展示目的地(种子补齐),标注来源=种子
  3. browser_exec 自动走一遍:开页→选分段→出 pin→点 pin 见 popup→无 console 错误
  4. 三个里程碑验收全过 → 把 docs/STAGE1-PLAN.md 阶段1 标为完成
- 结果: (待夜班回填)

---

## 追加模板(新任务复制此段)

## [TASK-xxx] 标题
- 状态: pending
- 目标:
- 涉及:
- 验收:
- 结果: (待夜班回填)
