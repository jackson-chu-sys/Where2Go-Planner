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

- 状态: done
- 目标: 需求四分类(自然风光/小城人文美食/滑雪场/运动)检索与**优先级归类去重**(滑雪>运动>人文美食>自然,osm id+type 去重,一地只入一类);对入库 Place 生成**一句话简介**(LLM 缓存);地图 pin 分类图标 + popup 展示分类字段。规格见 docs/STAGE1-PLAN.md 第3/4节。
- 依赖: TASK-1a(Place 表已建、地图已渲染)。
- 涉及: backend/data_sources/(归类)、backend/models、LLM 简介(复用既有 DeepSeek/Qwen key,按 POI 缓存)、前端 popup
- 验收:
  1. 四分类各有 OSM tag 识别线索;同实体跨 tag 按优先级只入一类
  2. Place.category 正确;自然/景点不再重复(修复 POC 问题)
  3. 入库 Place 有 intro(LLM 生成,已生成的不重复调用);缓存落 DB
  4. 前端 pin 按分类着色,popup 显示分类+简介
  5. pytest backend/ 通过
- 结果: **完成**(commit `84acfa5`,2026-09-09)。
  - 归类引擎:新增 `backend/services/classify.py`,四分类各有一组 OSM tag 识别线索
    (自然=natural/leisure=park|nature_reserve/waterway/tourism=viewpoint/place=island;
    人文美食=historic/tourism=attraction|museum/amenity=restaurant|cafe/cuisine/place=town|village;
    滑雪=piste:*/ski=yes/sport=skiing|snowboard/landuse=winter_sports;
    运动=sport=*/leisure=sports_centre|pitch|stadium 等),认不出来落"其他",不猜。
    归类**优先级 滑雪 > 运动 > 人文美食 > 自然**,首次命中即定类,每个地物只归一类。
  - 去重:去重键 = OSM `(type, id)`;并集检索里同一实体被多组 tag 命中时**先合并 tags 再定类**
    (归类因此看得到全部线索),无 OSM id 的种子数据按"名字+坐标"兜底。
    **修复 POC 自然/景点重复**:带自然线索的泛景点只算自然,除非另有 historic 或美食线索;
    `/api/discover` 改用同一引擎过滤、响应形状不变;`services/categories.py` 降为兼容导入面,
    既有代码与 46 个单测零改动。
  - 检索:`backend/data_sources/overpass.py` 新增 `build_grouped_query`/`nearby_places_grouped`,
    按 band **上限半径一次请求**查完四分类 tag 并集(复用现有环形分段机制,环内收敛仍由
    `services.bands` 本地 haversine 做),每组独立 out 配额:滑雪 80 / 运动 100 / 人文景点 120 /
    小城古镇 40 / 美食 60 / 自然 140(合计 540),避免高频 tag 把总量刷爆;分组并集属冷启动批量
    抓取,客户端超时按次放宽到 150s,交互路径 `nearby_places` 仍 ≤20s、行为不变。
  - 入库与重归类:`services/place_loader` 改为 分组并集 → 环内收敛 → `classify_places` 去重归类 →
    upsert 写 `Place.category`(**抓取时即覆盖旧值/空值**);存量库另给 `services/reclassify.py`
    离线重算 CLI(`--only-legacy`/`--dry-run`,dry-run 不改 ORM 对象、不写库)。
  - LLM 简介:新增 `services/intro.py`,Provider 注册表(DeepSeek / 阿里 Qwen 兼容模式)统一走
    OpenAI 兼容 `POST {base_url}/chat/completions`;key 只从环境变量读,不落盘/不入库/不进日志/
    不出现在 API 响应。**按 POI 缓存在 `Place.intro`**,已有简介不再调用、重抓不覆盖;未配 key、
    超时、限流、格式异常一律降级为空简介,**不阻塞入库**。实际使用端点:**DeepSeek
    `https://api.deepseek.com`,模型 `deepseek-chat`**(`DEEPSEEK_API_KEY`,约 1.2s/条);环境里的
    DashScope key(`ALIBABA_TOKEN_PLAN_API_KEY`)实测 401 不可用,Qwen 仅作为注册表备选保留。
  - 前端 + API:`backend/app/static/index.html` pin 改为按分类着色的 `L.divIcon`
    (自然绿 `#16a34a` / 人文橙 `#ea580c` / 滑雪蓝 `#2563eb` / 运动红 `#dc2626` + emoji),图例同步;
    popup 卡片显示**分类徽章 + 距起点直线距离 + 一句话简介 + OSM id**,简介缺失给占位文案,
    新增「补简介」按钮 → `GET /api/places/intros`(走 DB 缓存)。`/api/places/meta` 暴露四分类
    color/emoji/blurb 与 LLM 描述(不含 key),`/api/places` 增加 `intro_pending`/`intro_stats`。
  - 测试:新增 `backend/test_classify.py` **41 个用例**,纯构造 elements 不触网(autouse
    `no_network` 把 `requests.Session.request` 换成抛错,偷跑网络当场失败),覆盖四分类识别、
    优先级归类、跨 tag 去重、并集分组单请求、loader 入库、intro 缓存/降级/不泄 key、reclassify、
    POC 重复修复与 API。`pytest backend/` = **87 passed**(原 46 个用例零改动)。
  - 真实链路实测(上海,2026-09-09):`50_100` 段六组并集重抓 → 环内 226 条、新增写入 132 行,
    分类 小城人文美食 134 / 自然风光 85 / 运动 6 / 滑雪场 1(四分类在真实数据上都取到了);
    二次查询 226 条 / 0.01s、`source=db`、`network_used=false`。全库 463 条 Place 简介
    **463/463 生成、0 条降级**;`reclassify --dry-run` 扫描 463 条、待改 0 条、旧值/空值 0 条;
    uvicorn(:8000)已重启并逐接口复验。
  - 备注:滑雪/运动在 OSM 国内仍稀疏(两段合计 滑雪场 1 条 / 运动 6 条),种子数据垫底属 TASK-1c。

---

## [TASK-1c] 打磨:起点定位/换城 + 种子数据 + 自动 QA

- 状态: done
- 目标: 产品打磨到可 DEMO。起点支持浏览器"我的位置"定位(Nominatim reverse)与城市搜索切换;OSM 国内缺失的滑雪/运动类补少量**种子数据**(人工坐标+简介);用 Hermes browser_exec 对本页面做一次自动 QA(能开、能查、无 JS 报错)。规格见 docs/STAGE1-PLAN.md。
- 依赖: TASK-1a + TASK-1b。
- 涉及: 前端(定位/搜索)、backend(种子数据源/脚本)、QA
- 验收:
  1. 页面能改起点城市并重定位范围圈;有"我的位置"按钮(可用则用,不可用给提示)
  2. 滑雪/运动类至少各有若干条可展示目的地(种子补齐),标注来源=种子
  3. browser_exec 自动走一遍:开页→选分段→出 pin→点 pin 见 popup→无 console 错误
  4. 三个里程碑验收全过 → 把 docs/STAGE1-PLAN.md 阶段1 标为完成
- 结果: **完成**(2026-09-10 夜班)。Codex commit `13ea2b6`:种子数据 49 条(滑雪 28/运动 21,backend/services/seed_data.py,source=种子标注);"我的位置"按钮+不可用降级提示;城市搜索空结果提示不报错;GET /api/geocode/reverse。pytest **143 passed**(87+56 新增,全 mock 不触网)。browser_exec 自动 QA 通过:开页→Leaflet 地图+瓦片加载→切 band→上海 50-100 出 226 pin→点 pin 弹窗(名称/分类/距起点/LLM 简介/来源徽标)→全程 window error 0 条。种子实测:上海 50-100 seeded=3(counts_by_source {OSM:226,种子:3});北京 200-300 冷抓 OSM 返回 0 条(公共 Overpass 繁忙,疑似降级),种子北戴河正常入库展示——白天可 refresh 重抓复核。

---

## [TASK-1d] 修复远距离分段(200-300/300-500)目的地稀少

- 状态: pending
- 目标: 修复真实 bug——远环数据被近处 POI 挤占。现状:Overpass 检索用 band 的**上限半径** (around:high) 查回一批按非距离序的 POI,总量受各组配额限制,远端 POI 几乎全被 0~low 范围内的近处 POI 占满;本地 haversine 过滤到 [low,high) 后所剩无几。实测水位:`北京 200_300` 入库仅 1 条、`上海 200_300` 仅 5 条,而 50_100=135、100_200=237(见 segment_fetch 表)。
- 修复思路(首选):Overpass QL 支持**集合差**,把"上限圆"减去"下限圆"只取环内再 out:
  `( nwr[tag](around:HIGH,lat,lng); - nwr[tag](around:LOW,lat,lng); ); out center N;`
  这样配额只花在环内 POI 上,不再被近处吃掉。若某类差集查询在公共实例上过慢,退回"上限圆 + 更大配额 + 本地过滤"并评估。保持分组并集、去重、归类口径不变。
- 涉及: backend/data_sources/overpass.py(新增环形差集查询,兼容就近写法与批量路径)、backend/services/place_loader.py(按 band 用环形查询)、backend/services/classify.py(SEARCH_GROUPS 若需适配)、backend/test_*.py
- 验收:
  1. 上海与北京 `200_300` 抓取(refresh)后各 band 环内条数显著上升且与地理常识相符(不应只有个位数)
  2. `50_100`/`100_200` 既有行为不回退(条数、分类口径不变)
  3. 环形差集在公共实例上可用(端点链/超时/降级照旧),真实联网跑通一次
  4. pytest backend/ 全绿
- 结果: (待夜班回填)

---

## 追加模板(新任务复制此段)

## [TASK-xxx] 标题
- 状态: 示例(勿执行;新任务复制本段并改成 pending)
- 目标:
- 涉及:
- 验收:
- 结果: (待夜班回填)
