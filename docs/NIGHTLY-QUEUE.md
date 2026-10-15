# Where2Go 夜间任务队列(NIGHTLY-QUEUE)

> 夜班执行器(每晚 22:00 CST, qwen3.8-max)读取本文件,依次执行**未完成**条目。
> 队列空 → 整轮跳过(0 token)。白天把重活追加为条目;完成后更新该条状态。
> 执行顺序 = 文件内自上而下(依赖已排好)。continuity=true,单晚做不完下轮续。
>
## 执行约定(2026-09-24 神朱拍板,三条硬规则)

- **R8 写码截止线**:派给 Codex 的任务,启动后 **12 分钟内必须出现第一条写文件动作**(heredoc/tee/`open(...,'w')/patch),看 rollout jsonl 的 exec_command 即可判定。超时 → 立即 kill、按零产物记 needs_review,不许等到 30min 墙钟熔断才收(9/23 的 TASK-3a 就是 37min、274 次调用、零写入才死,后 25 分钟全是白烧)。
- **R9 投喂式简报**:Codex 任务描述必须含「只读清单」(≤5 个确切文件路径,读完即开工,禁止浏览式探索仓库)+「落地契约」(新文件路径、精确签名/字段名/响应形状)。只写"见 docs/xxx 第 N 节"这类宽指引 = 必熔断。
- **R10 二次熔断自动降级**:同一任务族 Codex 累计熔断 **2 次** → 第 2 次发生时不再 kill 后留案,当轮直接改由**执行器手写**;needs_review 只留给"手写也失败"或需要神朱拍板的口径问题。
- **窗口扩容(2026-09-26 神朱拍板)**:`~/.codex/config.toml` 的 `model_context_window` 64K→**256K**、compact 线 48K→200K。复盘证实 qwen3.8-max 本体 1M 窗口,此前"读→压缩→重读"回圈是自设小窗口所致,**旧的 Codex 熔断史(含前端三连败)不再作为拒绝派发的依据**。验证路径:TASK-5a(后端,3a1/3a2 同款)先跑,稳定后 TASK-5b 重测前端;5b 若再熔断则按旧例转手写,不试第三次。

## ⚠️ 当日生效口径通告(2026-10-01 神朱拍板,凡与 skill/旧条目冲突以本通告为准)

**数据源全面切换高德**:①POI 检索 ②前端地图瓦片 ③地理编码 **全切高德**,**驾车一并切高德**;**Overpass 与 OSRM 彻底移除、不保留降级链**;渐进配额口径(首查 30/显示 15/加载更多 +30)与 `/api/places` 参数/响应形状**一律不变**(前端零改动)。Photon/Nominatim 保留为降级链。**TASK-7a 的冷抓墙钟/geocode 复验取消**(优化对象已被移除);TASK-8 全部排在 TASK-9 之后。
**条目命名防冲突**:本期「TASK-8」只指三条(8a1 图片链路 / 8a2 类别要点 / 8b 详情弹窗,契约 `docs/TASK-8-CONTRACT.md`);高德切换一律是 **TASK-9a~9d**(契约 `docs/TASK-9-CONTRACT.md`)。**若队列里出现与本通告冲突的重复条目(如另一个「TASK-8 高德切换」),一律以本通告为准,并把冲突条目改为 `blocked`+注明,不要执行。**
**前端地图最终裁定(神朱 2026-10-01)**:走**官方高德 JS API 2.0**(JS key + 安全密钥都走后端 `/api/map-config` 运行时注入,**绝不写进 index.html**;安全密钥已提供);真机若仍报 `INVALID_USER_SCODE` → **授权当轮降级为「Leaflet + 高德无 key 栅格瓦片」并注明待升级**,不许空过。

执行 TASK-9 前**逐字读 `docs/TASK-9-CONTRACT.md`**,四条硬数字不许猜:
1. **检索走 v3**(v5 `page=2` 与 `page=1` 返回完全相同的页,翻页是坏的);
2. **单查询最多 200 条**(`page>=9` 返回 0,`count` 字段不可信)→ band ≥50km 必须分格查多边形;`radius` 实测被截断在 50000;
3. **高德 `tolls` 恒 0、`cost` 恒 null** → 过路费改用 `toll_distance` × 区域费率,别用高德过路费字段;
4. **前端 JS key 不进 git** → 走 `GET /api/map-config` 运行时注入;key 在 `/opt/data/.env`(0600);REST 用 `WHERE2GO_AMAP_KEY`、前端用 `WHERE2GO_AMAP_JS_KEY`+`WHERE2GO_AMAP_SECURITY_JS_CODE`(均已落 `.env`),两 key 平台隔离(混用报 `USERKEY_PLAT_NOMATCH`);密集连打触发 `infocode=10021`(QPS)→ 单进程最小间隔 **0.6s** + 退避重试 2s/5s(≤3 次)。

---

---

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

- 状态: done
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
- 结果: **完成**(commit `9058e31`,2026-09-10 夜班,Codex 执行)。
  - overpass.py 新增 `build_grouped_ring_query`(每组「上限圆 - 下限圆」差集,inner<=0 退化为单圆)+ `OverpassClient.nearby_places_ring`:每组一次请求(六组合并撞公共实例 2048MB 单查询内存上限);单组仍 OOM 时按选择器拆开重发,合并后按 (osm_type,osm_id) 去重、由近及远截到该组配额;拆无可拆抛 DataSourceError,不写残缺水位。`execute` 增 `reject_runtime_errors`:OOM 致命 remark 不再换端点直接拆;超时 remark 仍收下部分分组。环形差集超时放宽一档(240s/270s)。place_loader 按 band low>0 走环形查询;分组并集、去重键、归类优先级口径不变;交互路径 nearby_places 不变。
  - pytest backend/ = **161 passed**(143 原有 + 18 新增,全 mock)。
  - 真实重抓对比(refresh,直连绕代理):上海 200_300 **5 → 463 条**(人文218/自然140/运动100/滑雪5,200.3-299.9km 全落环);北京 200_300 **1 → 468 条**(人文207/自然153/运动100/滑雪8,200.3-299.2km 全落环,抓取 839s,OOM 组自动拆分跑通);回退验证:上海 50_100 refresh 229→495 条(差集口径同样受益,无回退)、上海 100_200 读库 237 条不变。地理常识抽查:北京环内滑雪场=美林谷251/万龙白登山258/西部长青277km 等,全部真实落环。
  - API 复核:GET /api/places 北京/上海 200_300 均 source=db、network_used=false 秒回(uvicorn 已重启)。
  - 备注:重抓按许可跳过 LLM 简介,1196 条待补;夜班已起 qwen(token-plan)回填批处理,实测 5/5 生成成功。

---

## [TASK-2a] 路线服务 + 费用估算 + /api/routes + 跳转链接(后端)

- 状态: done
- 目标: 依 docs/STAGE2-PLAN.md 第 2/4 节,新建 backend/services/routes.py:统一路线编排,返回驾车(OSRM 真实,含 geometry)/铁路/飞机三种方式,每条含 `{mode, label, duration_min, cost_cny, distance_km, geometry?, kind: real|estimate, note}`。费用按文档系数估算(驾车油耗+过路费;铁路 里程×0.45 起步价;飞机 里程×0.6+100),系数为集中常量便于日后替换。新增 `GET /api/routes?from_lat=&from_lng=&to_lat=&to_lng=&to_name=`。deep-link 生成(高德/Google 导航、12306、OTA 搜索)为纯函数。复用现有 data_sources(OSRM/Nominatim)与 app/api 风格。
- 依赖: 无(阶段1 已完成)。
- 涉及: backend/services/routes.py、backend/app/api/routes.py(或并入现有 api)、backend/test_routes.py
- 验收:
  1. `/api/routes` 对真实坐标返回驾车(真实时长/距离/geometry)+ 铁路/飞机(估算),字段符合上述结构
  2. 费用为估算且带 `kind=estimate` 与 note;驾车 kind=real
  3. deep-link 纯函数有单测(高德/Google/12306/OTA 各一条)
  4. 时长阈值沿用(POC 口径):铁路 ≥100km、飞机 ≥300km 才出现
  5. pytest backend/ 全绿
- 结果: **完成**(commit `50cf97f`,2026-09-11 夜班,Codex 执行)。
  - 新增 `backend/services/routes.py`(682 行):费用/阈值系数集中常量(RAIL_MIN_KM=100、FLIGHT_MIN_KM=300、铁路 0.45 元/km 起步 20、飞机 0.6 元/km+100、驾车 8L/100km×7.5 元/L+0.7 高速占比×0.5 元/km);`plan_routes` 编排驾车(OSRM real,含 geometry,失败降级不 500)+铁路/飞机(estimate,note 标注估算非实时);deep-link 纯函数 amap/google/12306/OTA。`GET /api/routes` 校验风格与 /api/places 一致(缺参/非法 400)。osrm.py 增强 geometry 支持。
  - pytest backend/ = **189 passed**(161 原有零改动 + 28 新增,网络全 mock)。
  - 真实链路实测(上海人民广场→杭州西湖,直线 167.2km):driving kind=real 132min/182.5km/173 元(OSRM geometry 1172 点,费用与公式吻合 109.5+63.9≈173)、rail kind=estimate 165min/90 元(167.2×1.2×0.45≈90)、飞机未出现(<300km 阈值正确);links=amap/google/12306 齐备。

---

## [TASK-2b] 前端路线面板(地图点 pin → 多方式卡片 + 画线 + 跳转)

- 状态: done
- 目标: 依 docs/STAGE2-PLAN.md 第 3 节,在 Leaflet 地图页实现:点目的地 pin → 出路线面板,展示三种方式卡片(图标/时长/费用/说明/来源标注),选中方式在地图画线(驾车用 OSRM geometry 折线,铁路/飞机示意直线),每卡片带跳转按钮(deep-link 新页)。保持现有分类 pin、popup、band 切换、起点定位不破坏。
- 依赖: TASK-2a。
- 涉及: backend/app/static/index.html(及必要的静态资源)
- 验收:
  1. 点 pin 能看到"驾车/铁路/飞机"卡片(按距离阈值出现),含时长与费用及"估算/真实"标注
  2. 驾车能画真实路径折线(geometry 抽稀);铁路/飞机为示意线
  3. 跳转按钮可点、URL 正确(新页打开)
  4. 页面无 JS 报错;既有功能回归正常
  5. 可用 Hermes browser_exec 做一次自动 QA(开页→点 pin→出面板→画线→无 console error)
- 结果: **完成**(commit `388da20`,2026-09-11 夜班,Codex 起草+执行器收尾;Codex 单次调用超 30min 上限被熔断,产物已就绪故未重跑)。
  - index.html(+429 行):点 pin → popup 内「🚗 路线对比」按钮 → 路线面板(各方式卡片:图标/时长/费用/里程/note/「真实·估算」徽标/生成时间);选中卡片地图画线(驾车=OSRM geometry 青实线,铁路=蓝虚线示意、飞机=紫虚线示意),切换换线、关闭清线;每卡片 deep-link 跳转按钮(target=_blank rel=noopener,高德/Google/12306/OTA);加载/失败/空态友好提示;既有 pin 着色、popup 简介、band 切换、搜索、定位、补简介零回退。
  - 新增 backend/test_frontend_routes.py 轻量静态断言(面板 DOM/JS 函数/api 调用存在),全 mock。pytest backend/ = **212 passed**(189 既有零回退 + 23 新增)。
  - browser_exec 真实 QA(uvicorn :8000 已重启到新版):开页 495 pin→点 pin 出 popup→点路线按钮→面板出驾车卡片(OSRM real 50min/¥60 估算/62.8km,note 含估算口径)→画线 4 条 path→跳链 URL 正确(uri.amap.com/google maps,target=_blank)→关闭清线、全程 window error **0** 条。

---

## [TASK-2c] 路线收藏(Collection 表 + 收藏 API + UI)

- 状态: done
- 目标: 依 docs/STAGE2-PLAN.md 第 4 节,新增 `Collection` / `CollectionCat` 表(为 M4 铺路)与收藏 API(增/删/查),前端路线面板加「收藏路线」按钮与收藏列表查看。收藏条目记录:类型(route/place)、引用、名称、快照摘要(时长/费用)、创建时间。
- 依赖: TASK-2a/2b。
- 涉及: backend/db/models.py、backend/db/repository.py、backend/app/api/、frontend static
- 验收:
  1. 收藏表 + 唯一约束;重复收藏幂等
  2. API:新增/删除/列表(按类型过滤)
  3. 前端能收藏路线并在收藏列表看到(含时长/费用摘要)
  4. 单测覆盖收藏增删查与幂等;pytest backend/ 全绿
- 结果: **后端完成、前端未落地 → needs_review**(2026-09-12 夜班)。
  - 后端(commit `ff2a7fd`,Codex 第1次调用 ~52min/677K tokens):`Collection`/`CollectionCat` 两表
    (唯一键 (kind,ref_key,mode),幂等 upsert 不报错不重复);`POST/GET/DELETE /api/collections`
    (kind/cat 过滤、counts_by_kind 一次带齐、缺参/非法 400 中文报错,口径与 /api/places 一致);
    `backend/test_collections.py` 55 用例全 mock。**pytest backend/ = 267 passed**(212 基线零改动+55 新增)。
    执行器真机复验(uvicorn :8000 重启后实测):POST 两次幂等同 id、GET ?kind=route 过滤正确、
    summary 含 mode/duration_min/cost_cny/distance_km 快照、DELETE 后 total=0、bad-id/bad-kind/empty-body 均 400。
  - 前端(收藏按钮+收藏列表面板)**未完成**:Codex 第2次调用超 30min 熔断线被中止(R7),
    中止时 git 工作区干净、零产物,无可收尾内容。验收第3条(前端能收藏并看列表)未达成。
  - ~~待神朱定夺~~:神朱已裁定选①(执行器直接手写),前端由 TASK-2c-fe 于 2026-09-20 落地(commit `7618bc1`),本条收口 done。
  - 备注:POST route 收藏需 mode + 目的地引用(osm_type+osm_id 或 to_lat+to_lng),前端对接时注意。

---

## [TASK-2c-fe] 前端路线收藏 UI(收口 M2)

- 状态: done
- 目标: **仅前端**改动,补齐 TASK-2c 缺失的收藏 UI。在 `backend/app/static/index.html` 的路线面板中:1) 每个路线方式卡片(驾车/铁路/飞机)旁加「收藏路线」按钮;2) 新增「我的收藏」列表(弹层或侧栏),显示已收藏项的 类型/名称/时长/费用摘要;3) 支持取消收藏。**后端已于 commit ff2a7fd 完成**(`backend/app/api/collections.py`:POST/GET/DELETE `/api/collections`;pytest 267 passed)——**不要修改后端**,只对接。
- 依赖: 无(后端就绪)。
- 涉及: **仅** backend/app/static/index.html
- 验收:
  1. 路线面板点「收藏路线」能把该路线存入收藏(POST /api/collections,请求体按 collections.py 定义)
  2. 「我的收藏」可见已收藏内容(含类型/名称/时长/费用摘要),可删除(DELETE)
  3. 页面无 JS 报错;既有地图/pin/路线面板不回归
  4. browser_exec 自动 QA:开页→点 pin→出路线卡片→点收藏→看收藏列表→删除→0 console error
  5. 不改任何后端文件
- 结果: **完成**(2026-09-20,神朱选①后由执行器直接手写,不再派 Codex;commit `7618bc1` + 笔误修复 `64728b1`)。
  - index.html(+约160行,仅前端,零后端改动):路线卡片内「☆ 收藏路线」按钮(POST /api/collections,
    请求体含 kind=route/mode/起终点坐标与名称/osm_type+osm_id/summary{duration_min,cost_cny,distance_km,kind},
    成功后按钮变「★ 已收藏」disabled;失败中文提示不阻塞);顶栏「⭐ 我的收藏」入口 → 全屏 dialog 弹层
    (GET /api/collections 渲染列表:名称/类型徽章/方式/时长/费用/里程/真实·估算/收藏时间,空列表占位文案,
    每项「取消收藏」→ DELETE /api/collections/{id});Esc 先关弹层再关路线面板,点遮罩空白也可关;
    已收藏判定键与后端唯一键 (kind,ref_key,mode) 同口径(坐标定点 7 位小数,OSM 身份优先),
    boot 时预取一次收藏列表让按钮初始态正确。
  - 测试:test_frontend_routes.py 追加 8 个静态断言(DOM id/默认隐藏/JS 函数/卡片接线/API 调用/
    请求体字段/事件委托与键盘/列表项摘要)。pytest backend/ = **275 passed**(267 基线零改动)。
  - browser_exec 真实 QA(uvicorn :8000 StaticFiles 直接读盘,无需重启):开页 495 pin 正常 →
    开路线面板出驾车卡片 → 点「☆ 收藏路线」→ 提示「已收藏:上海 → La Taverna · 驾车」+ 按钮变 ★ 已收藏 →
    开「⭐ 我的收藏」弹层见 1 条(名称/路线徽章/🚗驾车/时长 50 分钟/费用 ¥60/里程 62.8 km/真实/收藏时间)→
    取消收藏 → 列表回空态、卡片按钮复位「☆ 收藏路线」→ Esc 关弹层;全程 window error **0** 条。
  - QA 中发现并当场修复一处笔误(调用名 favRoutePayload → routeFavPayload,`64728b1`);测试收藏已清空,库无残留。
  - M2(阶段2c 路线收藏)至此收口:TASK-2c 后端 + TASK-2c-fe 前端全部落地。

---

## [TASK-JEV1] Jev 范式 PoC · 采集与分块器(第 1/2 晚)

- 状态: done
- 目标: 验证「工具结果进上下文前先过廉价裁判」能省多少 token。今晚**不调用 Codex**(纯执行器手写),只做采集侧:
  1. 建 `tools/jev_poc/` 目录:requirements 说明、`splitter.py`(把日志按 ~25 行分块,winnow 式,不依赖 Jev 包)、`judge.py`(裁判函数:读 .env 里的 key,走 OpenAI 兼容 `POST /v1/chat/completions` 单次调用,批量问每块「当前任务还需要吗 yes/no+置信度」,模型用 deepseek-chat 或 token-plan qwen 小杯;失败一律降级为"保留",绝不丢数据)、`__init__/README.md` 记录设计。
  2. 写 20+ 个 pytest(`backend/test_jev_poc.py`,全 mock 不触网):分块边界、裁判解析健壮性(坏 JSON/超时/限流→保留)、stub 可逆性(restore key 能还原原文)、成本统计函数。
  3. 真实回放素材:取昨夜 `~/.codex/sessions/` 最新 jsonl + 最近一次夜班执行器的工具输出样本,落一份脱敏副本到 `tools/jev_poc/samples/`(去掉 key/token 字样,正则扫 `sk-|gpo_|token` 复核)。
- 涉及: tools/jev_poc/(新建)、backend/test_jev_poc.py
- 验收: 1) pytest backend/ 全绿(基线 275 + 新增);2) splitter 对样本日志分块数、行数守恒(无丢行);3) judge.py 对样本跑一次真实调用能返回结构化结果且打印每块成本估算;4) 样本已脱敏(grep 无凭据残留);5) git commit(只 commit 不 push)。
- 结果: **完成**(2026-09-21 夜班,执行器手写,未调 Codex)。commit 见 git log。
  - `tools/jev_poc/`:`splitter.py`(25 行/块、行数守恒、逐字符可还原;`StubStore` stub↔原文
    可逆,restore key=内容哈希;`approx_tokens`=chars/3 统一口径)、`judge.py`(qwen token-plan
    优先/deepseek 备选注册表,OpenAI 兼容单次批量调用;坏 JSON/超时/限流/无 key 一律降级为
    全部保留;drop 需置信度 ≥0.75;成本统计 `estimate_screening_savings` + `cost_cny`)、
    `README.md`(设计记录/铁律/用法)、`__init__.py`。
  - 测试:`backend/test_jev_poc.py` **46 个用例**(全 mock,no_network fixture 偷跑当场失败),
    pytest backend/ = **321 passed**(275 基线零改动 + 46 新增)。
  - 样本脱敏落盘 `tools/jev_poc/samples/`:`codex_session_outputs.log`(1295 行,取 9/19 最新
    session 的 function_call_output)+ `nightly_tool_outputs.log`(156 行,pytest 尾段/git log/
    routes.py 片段);正则扫 sk-/gpo_/ghp_/github_pat_/key=value 凭据残留 **0 命中**。
  - 真实调用实测(qwen3.8-max,样本2/156行/7块):22.8s 返回结构化裁决,KEEP 2 / DROP 5
    (drop 置信 0.98,被丢的是与任务焦点无关的 git log/源码段,判定合理),
    tokens 1945→756(省 61.1%),裁判自身成本 ≈0.0073 元;drop 块 restore == 原文 ✓。
  - 备注:JEV2 用 samples 跑回放测量。裁判单次调用 22.8s/2597 prompt tokens,
    replay 时注意样本1(1295行/52块)单次全量喂可能超窗口,应分批。

---

## [TASK-JEV2] Jev 范式 PoC · 回放测量与结论(第 2/2 晚)

- 状态: done
- 目标: 依赖 TASK-JEV1 完成。今晚**不调用 Codex**。
  1. `replay.py`:对 samples 里的历史工具输出,模拟一次夜班 turn 的"任务焦点描述"(从该 turn 上下文人工/程序提取),跑裁判,输出对照报告:原 token 数 vs 筛后 token 数、可筛除比例、裁判自身消耗、按现价折算省钱比例。
  2. 写 `tools/jev_poc/FINDINGS.md`:省 token 百分比、误杀率(人工抽查 20 块标注)、结论三选一——a) 值得接入 SoL-Pi/codex 管道(给出接入草案) b) 用 adapter 不划算,若上真 Jev API($5 免费额度)预期如何 c) 范式不适合本项目场景,归档。
  3. git commit(不 push)。
- 依赖: TASK-JEV1。
- 涉及: tools/jev_poc/replay.py、FINDINGS.md
- 验收: 1) 回放报告数字可复算(附脚本输出);2) FINDINGS.md 结论明确、引用真实数字;3) pytest 仍全绿;4) 已 commit。
- 结果: **完成**(2026-09-21 夜班,执行器手写,未调 Codex;与 JEV1 同晚完成)。
  - `tools/jev_poc/replay.py`:两场景回放(样本1=9/19 codex session/焦点=前端收藏UI,
    样本2=9/12 夜班输出/焦点=收藏API验证),分批 12 块喂裁判,行数守恒+逐字符还原断言,
    报告落 `replay_report.md`、逐块明细 `replay_chunks.jsonl`(59 块)。
  - 实测(qwen3.8-max,0 降级批):样本1 筛前 17216 → 筛后 12146 tokens(**省 29.4%**,
    KEEP 36/DROP 16),裁判 30519 tokens/≈0.071 元/253s;样本2 1945 → 438(**省 77.5%**,
    KEEP 1/DROP 6),裁判 ≈0.006 元/18s。
  - 误杀率人工抽查 22 个 DROP 块:**≈9%(2/22 边缘误杀,均为目标文件 index.html 既有
    函数段)**,stub 可逆兜底(restore==原文 抽查通过),不丢数据。
  - 关键发现:裁判用推理型模型时自身 token = 筛除量的 2.2-6.0 倍(reasoning_tokens 占大头);
    盈亏平衡 = 输出在上下文存活 sample1 ≈11.6 轮 / sample2 ≈3.5 轮。
  - 结论(FINDINGS.md):**选 a) 值得接入**,限定三条——①裁判换非推理小杯(平衡点降到~2轮)
    ②只筛 ≥1000 tokens 且预计存活 ≥10 轮的大输出 ③stub 可逆为硬前提+目标文件白名单。
    附 SoL-Pi 接入草案;真 Jev API($5 额度)建议仅作生产级 A/B,优先级低于①。
  - 修复 JEV1 遗留 bug:分批调用时裁判按真实块号回复,parse_verdicts 硬要求 0 基 id 导致
    首跑 4/5 批误降级;加 `ids` 参数修复+回归用例。pytest backend/ = **322 passed**。

---

## [TASK-3a1] Stay 表 + 住宿检索 + LLM 估价/简介缓存(服务层,无 API)

- 状态: done
- 背景: 原 TASK-3a(9/23 Codex 37min 熔断零产物)按 R9 拆小。本条只做**服务层**,不做路由、不改前端。
- 目标: 新建 Stay 表与 `services/stays.py`:OSM `tourism in (hotel,guest_house,hostel,apartment,chalet)` 单圆检索周边住宿 → haversine 排序算距 → LLM 生成**预估参考价区间 + 一句话简介**(按住宿缓存,已生成不重调;无 key/超时降级为空,不抛异常)。
- **只读清单(只准读这 5 个,读完立即写码)**: `backend/db/models.py`(Place/Collection 定义风格)、`backend/db/base.py`、`backend/services/intro.py`、`backend/data_sources/overpass.py`、`backend/test_collections.py`(mock 与 fixture 套路)。禁止再读其他文件、禁止跑全量 pytest 超过 2 次。
- 落地契约(照此实现,不得自创字段):
  - `db/models.py` 追加 `Stay` 表:`id, osm_type(String16), osm_id(Integer), name(String255), kind(String32), lat(Float), lng(Float), tags(JSON), distance_km(Float,nullable), price_estimate(String64,nullable), currency(String8,default"CNY"), intro(Text,nullable), fetched_at(DateTime)`;唯一键 `(osm_type, osm_id)`;复用 `utcnow()/iso_utc()`,坐标定点用 `COORD_PRECISION`。
  - `services/stays.py`:
    - `STAY_TAGS: tuple[str,...] = ("hotel","guest_house","hostel","apartment","chalet")`(值即 `kind`)
    - `def search_stays(lat: float, lng: float, radius_m: int = 8000, *, client=None) -> list[dict]` — 用 `overpass.build_grouped_query`(单组、selector `{"tourism": tag}` 逐个)+ `parse_places(payload, lat, lng, limit=None, require_name=False, with_id=True)`;返回项含 `osm_type/osm_id/name/lat/lng/tags`(解析函数已给)。检索失败按既有降级口径返回空列表。
    - `def estimate_price(stay: Mapping, *, client=None, environ=None) -> tuple[str, str]` — 返回 `(price_estimate, intro)`;prompt 里给名称/kind/位置/tags 摘要,要求输出两行:`价格: 约¥A-B/晚` 与 `简介: <40字内>`;复用 `intro.LLMClient`、`resolve_provider`、`clean_intro` 的降级风格:未配 key、异常、格式不对一律 `("", "")`,**绝不抛出**。已有 price_estimate 的行不再调用。
    - `def upsert_stays(session: Session, rows: Sequence[Mapping]) -> int`(按 `(osm_type,osm_id)` upsert,不覆盖已有 price_estimate/intro)
    - `def load_or_fetch_stays(session, lat, lng, *, radius_m=8000, refresh=False) -> list[dict]` — 库里该坐标半径已有行 ≥ 阈值则直接读库返回(`source="db"`),否则检索+入库+批量估价;每项 dict 带 `distance_km`(haversine,round 2)。
  - `backend/test_stays.py`:**全部 mock**(网络:替换 `requests.Session.request`;LLM:注入假 client),≥15 用例,覆盖:检索解析、kind 归一、upsert 幂等不覆盖已生成、缓存命中零 LLM 调用、无 key 降级、坐标定点、排序。
- 验收: `cd backend && ../.venv/bin/python -m pytest -q` 全绿(基线 325 passed 只增不减);不新增第三方依赖;不动 `app/` 任何文件。
- 结果: **完成**(2026-09-24 夜班,Codex 执行,commit `3d20958`)。
  - `db/models.py` 新增 Stay 表(唯一键 (osm_type,osm_id)、坐标定点 COORD_PRECISION、currency 默认 CNY、ix_stay_location 索引);`services/stays.py` 按落地契约实现 search_stays(Overpass 单组并集,失败降级空列表)/estimate_price(LLM 两行输出价格区间+40字简介,无 key/异常/格式不对一律 ("",""),绝不抛)/upsert_stays(幂等,不覆盖已生成 price_estimate/intro)/load_or_fetch_stays(DB 即缓存,distance_km haversine round2)+ 预抓 CLI。
  - `backend/test_stays.py` 66 用例全 mock;执行器复跑 pytest backend/ = **391 passed**(基线 325 零改动 + 66 新增,9.9s)。未动 app/ 任何文件、未新增第三方依赖。
  - Codex 单次调用 ~28min 完成(上次同族任务 37min 零产物熔断,R8/R9 拆分后首战通过)。

---

## [TASK-3a2] GET /api/stays 路由(薄 API)

- 状态: done
- 目标: 仅新增 `app/api/stays.py` 路由 + `app/main.py` 挂 `include_router(stays.router, prefix="/api")`,复用 3a1 的 `services.stays`。
- **只读清单**: `backend/app/api/collections.py`(校验/报错/裸 Body 口径)、`backend/app/main.py`、`backend/services/stays.py`(3a1 产物)。
- 落地契约: `GET /api/stays?lat=&lng=&radius_km=8&refresh=`;`place_id=` 可选(有则从 Place 表取坐标,二者只给其一,都缺 → 400 中文报错)。响应 `{"lat","lng","radius_km","count","source","note","items":[{id,osm_type,osm_id,name,kind,lat,lng,distance_km,price_estimate,currency,intro,estimated:"AI 预估 · 仅供参考 · 以 OTA 实时为准"}]}`;`note` 常量写明预估口径;radius_km 上限 30。
- 验收: `backend/test_stays_api.py` ≥8 用例(TestClient,网络/LLM 全 mock);pytest 全绿;既有路由零回归;不改 index.html。
- 结果: **完成**(2026-09-24 夜班,Codex 执行,commit `de35fb5`,单次调用 ~11min)。
  - 新增 `app/api/stays.py`(GET /api/stays,lat/lng 或 place_id 二选一、radius_km≤30、400 中文报错、note+estimated「AI 预估 · 仅供参考 · 以 OTA 实时为准」标注)+ main.py 挂路由;`test_stays_api.py` 34 用例(超出 ≥8 要求:refresh 不调 LLM、无 key/限流降级仍 200、Overpass 全挂空列表、FieldInfo 直调不崩等)。
  - 执行器复跑 pytest backend/ = **425 passed**(391 基线零改动 + 34 新增);index.html 未动、工作区干净。

---

## [TASK-3b] 住宿前端展示(面板卡片 + 预估价标注)

- 状态: done
- 目标: 依 docs/STAGE3-PLAN.md 第 1/2 节,**仅前端**改动:在地图页选中目的地后,除现有路线面板外增加「住宿」区块,展示该目的地周边住宿卡片(名称/类型/距离/预估参考价/简介),并**强标注**「AI 预估 · 仅供参考 · 以 OTA 实时为准」。可加「收藏住宿」按钮(复用 /api/collections,type=stay)。
- 依赖: TASK-3a2(3a1+3a2 均 done 后执行)。
- 涉及: 仅 backend/app/static/index.html
- 验收:
  1. 选目的地能看到住宿卡片(含预估价与「AI 预估」标注)
  2. 收藏住宿可存入收藏(不影响既有路线收藏)
  3. 页面无 JS 报错;既有地图/pin/路线面板不回归
  4. browser_exec QA:开页→点 pin→看住宿区块→(可选)收藏→0 console error
  5. 不改后端文件
- 结果: **完成**(2026-09-25 夜班,执行器直接手写,未派 Codex;commit `0a027ed`)。
  - index.html(+约210行,仅前端,零后端文件改动):路线面板下方新增「🛏️ 周边住宿」区块,
    openRoutePanel 时与路线**并行**拉 GET /api/stays(半径 5km;token 守卫作废过期响应);
    卡片含 名称/类型徽章(酒店/民宿/青旅/公寓/木屋)/距离/AI 预估价区间/一句话简介,
    每卡片强标注「AI 预估 · 仅供参考 · 以 OTA 实时为准」;超 12 家显示「共 N 家」汇总;
    冷坐标加载中文案(Overpass+LLM 30-120s)/失败「重试」/空结果降级提示,均不抛 JS 错;
    「☆ 收藏住宿」复用既有 POST /api/collections(**kind=place + Stay 的 OSM 身份**,后端无
    stay 类型、零改动),判定键复刻后端 collection_ref_key 的 place 规则(type/id),
    与收藏弹层/路线收藏按钮同源刷新;closeRoutePanel/Esc 一并清住宿区块;页头页脚口径更新。
  - test_frontend_routes.py 追加 9 个静态断言(DOM id/JS 函数面/API 调用/预估标注/收藏
    payload 字段/事件委托/**前后端字段契约**:前端 item.* 引用必须是 /api/stays ITEM_KEYS
    子集)。pytest backend/ = **434 passed**(425 基线零改动,13.4s)。
  - browser_exec 真实 QA(uvicorn 已重启到含 /api/stays 的新代码):开页 495 pin 0 error →
    点 pin 出路线面板+住宿区块 → 库缓存命中(source=db)143 家渲染 12 卡(Hi Inn 约¥250-450/晚
    +标注+简介齐)→ 点「☆ 收藏住宿」→ 提示已收藏+按钮变 ★ → 收藏弹层见 1 条「🛏️ Hi Inn · 住宿」
    → 取消收藏 → 列表回空态、卡片按钮复位 → 关面板/Esc 清区块;全程 window error **0** 条。
  - 附带:发现旧 uvicorn 进程(跑了 12 天)未含 TASK-3a2 路由,已带 /opt/data/.env 重启;
    市中心坐标(31.2304,121.4737, 2km)住宿 143 条入库,LLM 估价回填 **131/143**(12 条估不出
    留 null,前端显示「暂无 AI 预估价」,不编数字)。
  - 备注:browser daemon 曾连挂 5 个会话(Runtime.evaluate timed out),根因是 16 天前的
    chrome-headless-shell 僵死;kill 后按 hermes-browser-cdp-setup skill 原参数重启
    (端口 9222,新 user-data-dir=/tmp/chrome-cdp-w2g3)即恢复。

---

## [TASK-5a] 行程方案后端:TripPlan 表 + 总账报价 + /api/trip-plans

- 状态: done
- 背景: M4 第一步(**纯后端**,不动 index.html)。为 TASK-5b(统一收藏面板+行程方案前端)提供聚合 API。**本任务同时是 Codex 256K 窗口扩容的验证任务**(R8/R9 照旧执行)。
- 目标: 新表 `TripPlan` 把已收藏的「目的地+路线+住宿」组合成方案,给出**大致总花费**(从 Collection 快照的"当时口径"计算,不重新调 /api/routes)。
- **只读清单(只准读这 5 个,读完立即写码;AGENTS.md 先读)**: `backend/db/models.py`、`backend/db/repository.py`、`backend/app/api/collections.py`、`backend/services/stays.py`、`backend/test_collections.py`。禁止其他探索性 cat/grep,禁止跑全量 pytest 超过 2 次。
- 落地契约(照此实现,不得自创字段/路由):
  - `db/models.py` 追加 `TripPlan`:表名 `trip_plans`;列 `id, name(String255,非空,unique 约束 uq_trip_plan_name), note(Text,可空), place_collection_id(Integer,可空), route_collection_ids(JSON list,默认空), stay_collection_ids(JSON list,默认空), created_at/updated_at(照 Collection 风格 utcnow/onupdate)`。**引用的是 collections.id,不建 FK**(与收藏快照同口径:删收藏不连带删方案,报价时缺行按"已删除"处理)。
  - 新建 `services/trips.py`:
    - `def parse_nightly_price(price_estimate: Optional[str]) -> tuple[Optional[float], Optional[float]]` — 从「约¥250-450/晚」「¥300/晚」等文案解析区间下限/上限,解析失败返回 `(None,None)`(不猜数)。
    - `def quote_plan(session: Session, place_ref, route_refs, stay_refs, *, nights: int = 1) -> dict` — 读 Collection 行快照:交通=`sum(summary.cost_cny)`(缺项跳过);住宿=各 stay 的价下限均值×nights(有上限再给上限档);输出 `{"total_cny_low","total_cny_high","transport_cny","stay_nights","per_stay":[{collection_id,name,price_estimate,low,high}],"missing":[已删除的id],"kind":"estimate","note":"按收藏快照的当时口径估算 · 仅供参考"}`;nights 越界(0<nights≤60)抛 ValueError。
    - `def upsert_trip_plan(session, ...)` / `def trip_plan_to_dict(row, quote=None)`(repository 风格)。
  - 新建 `app/api/trips.py` + `app/main.py` 挂 `include_router(trips.router, prefix="/api")`:
    - `POST /api/trip-plans` 裸 JSON `{name, note?, place_collection_id?, route_collection_ids?, stay_collection_ids?, nights?}` → 建/按 name upsert,响应含 `quote`;重名=刷新(幂等,仿 collections)。
    - `GET /api/trip-plans?limit=` 列表(新在前,每项带 quote 与 counts);`GET /api/trip-plans/{id}` 详情;**`DELETE /api/trip-plans/{id}`**。
    - 校验口径照 collections.py:裸 Body、400 中文报错、引用不存在→400、响应带 note。本模块**不触网**。
  - `backend/test_trips.py`:全 mock ≥25 用例,覆盖:价文案解析(各种脏输入)、报价求和/上下限、引用被删的降级、upsert 幂等、API 校验、nights 边界;**既有测试零改动**。
- 验收: `cd backend && ../.venv/bin/python -m pytest -q` 全绿(基线 **434 passed** 只增不减);不动 `app/static/` 与 `app/api/collections.py`;commit 消息带 TASK-5a。
- 结果: **完成**(2026-09-26 加跑夜班,Codex 执行,commit `da6eb7f`;**256K 窗口首战验证通过**)。
  - `db/models.py` 新增 TripPlan 表(trip_plans;name 唯一 uq_trip_plan_name;route/stay_collection_ids JSON 数组默认空;created_at/updated_at 照 Collection 风格;三个引用列不建 FK,删收藏不连带删方案)。
  - `services/trips.py`:parse_nightly_price(复用 stays.PRICE_RANGE_RE,认「约¥250-450/晚」「300-500元」「1,200~1,800」含全角逗号;脏输入/¥0/面议 →(None,None) 不猜数;上下限写反自动纠正)、quote_plan(交通=路线快照 cost_cny 求和缺项跳过;住宿=价下限均值×nights,有区间给上限档;missing 列已删除引用;kind=estimate+note;nights 越界 1..60 抛 ValueError)、upsert_trip_plan(按名幂等)、trip_plan_to_dict。住宿价优先收藏快照 summary.price_estimate,缺失时按 OSM 身份回退查 stays 表(只读库不触网)。
  - `app/api/trips.py` + main.py 挂路由:POST /api/trip-plans(重名=刷新,响应含 quote)、GET ?limit=(新在前,带 quote+counts)、GET/DELETE /{id};裸 Body + 400 中文报错 + FieldInfo 直调兼容,口径照 collections.py。
  - `backend/test_trips.py` 50 例全 mock(no_network autouse + 手拼 ASGI scope 走完整 HTTP 链)。执行器复跑 pytest backend/ = **495 passed**(445 基线零改动 + 50 新增,14.8s)。未动 app/static/ 与 collections.py。
  - uvicorn :8000 已重启(旧进程无新路由);真机冒烟:GET 空列表 → POST 建「冒烟测试方案」返回 quote(kind=estimate)→ DELETE 成功 → total=0,测试数据已清理。
  - **Codex 256K 窗口统计**:单次调用 ~26min(15:46-16:12 UTC),function_calls **54**,首轮写码(apply_patch models.py)启动后 **7.5min**(R8 12min 线内),tokens 用量 **259,571**(total_token_usage 3.57M 含 cache 重放,峰值上下文 ~110K/262K,**全程零 compact、零"读→忘→重读"回圈**——对比 9/23 TASK-3a 64K 窗口 274 次调用/37min 零产物熔断,扩容效果显著)。

---

## [TASK-5b] 统一收藏面板 + 行程方案 UI(前端重测 Codex)

- 状态: done
- 背景: **前端任务重测 Codex**(256K 窗口下重验 9/12、9/19、9/23 的旧熔断结论;熔断则当轮转执行器手写,不试第三次——本次是第 1 次机会)。
- 目标: index.html 收藏弹层升级:①按 目的地/路线/住宿 分组展示(现有分组基础上加对比字段:路线时长/费用、住宿价);②新建「行程方案」tab:勾选已收藏的 目的地+路线+住宿 → POST /api/trip-plans(nights 输入)→ 卡片显示总花费区间与构成;③方案可删。免责口径沿用。
- 涉及: 仅 `backend/app/static/index.html` + `backend/test_frontend_routes.py` 静态断言(照 TASK-3b 追加模式)。
- 验收: 1) 面板分组含对比字段 2) 能建方案看总价 3) 0 JS 报错、既有收藏/住宿/路线功能不回归 4) pytest 全绿(基线含 5a 增量)5) browser_exec QA 全流程;若派 Codex:R8 写码截止线**放宽到 20 分钟**(前端文件 26K tokens,读入属正常动作;仍零写入即 kill)。
- 结果: **完成**(2026-09-26 加跑夜班,Codex 执行,commit `5ed6a99`;**前端任务 256K 窗口重测通过——9/12、9/19、9/23 的旧前端熔断结论正式作废**)。
  - index.html(+542/-163,仅前端,后端 Python 零改动):收藏弹层 **tab 化**(⭐我的收藏 / 🧳行程方案 互斥);分组保留对比字段(路线时长/费用/里程、住宿预估价);组合由三下拉单选改 **checkbox 多选**(路线/住宿多选、目的地单选互斥,与后端 place_collection_id 单值口径一致);方案名/晚数(min1 max60 与后端 MIN/MAX_NIGHTS 同口径夹取)/备注 → POST /api/trip-plans;quote 卡片渲染后端返回(total_cny_low~high、transport_cny、per_stay 逐处、missing 黄条、note 原样透出、幂等刷新提示);列表 GET ?limit=50 + DELETE + 「🔁 按 N 晚重算」;错误落 #planErr 不 alert;**localStorage 双轨彻底下线**(w2g_trip_plans/parseStayPrice/前端自算总账全移除)。
  - test_frontend_routes.py(+291):TASK-5b 段静态断言(DOM/函数面/POST-GET-DELETE/payload 字段与 trips.py 一字对齐/nights 区间/quote 字段消费/免责文案/tab 互斥)+ **2 例真跑后端**(临时 SQLite 不触网:前端字段引用不越界且金额算对、删收藏后 missing 降级);4a 时代 localStorage 断言随机制迁移下线。执行器复跑 pytest backend/ = **506 passed**(495 基线零改动 + 11 净增,17.0s);node --check 通过。
  - browser_exec 真实 QA(uvicorn :8000;daemon 又超时,按 3b 同款修法 kill 旧 chrome 重启 CDP 9222 即恢复):curl 备 3 条收藏(驾车/铁路/住宿)→ 开面板见分组+对比字段 → 行程方案 tab 勾选 3 项 → 填名+2晚 → 保存 → quote ¥1063~1463(=263+400×2~600×2 口径吻合)→「按 3 晚重算」幂等刷新 ¥1463~2063、id 不变 → 删除方案回空态 → 回归:点 pin 出 popup+路线面板正常;全程 window error **0** 条;测试收藏/方案已全部清理(collections=0,trip_plans=0)。
  - **Codex 256K 窗口统计(前端首战)**:单次调用 ~29min(16:14-16:43 UTC,40min 止损线内),function_calls **55**,首轮写码启动后 **~12min**(R8 放宽线 20min 内),tokens 用量 **224,413**(峰值上下文 ~141K/262K,**零 compact、零重读回圈**;对比旧 64K 窗口前端三连败,同一任务族一次过且自带端到端冒烟)。
  - 备注:M4 行程方案至此**后端持久化**收口(5a 表+API+报价 / 5b UI);4a 的 localStorage 方案机制已由 5b 取代。

---

## [TASK-4a] 统一收藏面板 + 行程对比 + 总账

- 状态: done
- 目标: 依 docs/STAGE3-PLAN.md 第 2 节,把收藏统一成可对比的面板:汇总 目的地/路线/住宿;以简洁信息展示 路线时长、路线费用、住宿费用 供对比;支持把「目的地+路线+住宿」组合为一个**行程方案**并给出大致总花费。必要时加后端聚合 API。
- 依赖: TASK-2c-fe / 3b。
- 涉及: backend/app/static/index.html、backend/app/api/collections.py(如需聚合)
- 验收:
  1. 收藏面板按类型分组展示,含路线时长/费用、住宿费用等对比字段
  2. 能创建行程方案(目的地+路线+住宿)并显示总花费
  3. 无 JS 报错;既有收藏功能不回归
  4. pytest backend/ 全绿(若动后端)
- 结果: **完成**(2026-09-26 夜班,执行器直接手写,未派 Codex;commit `3ac8398`)。
  - index.html(仅前端,零后端文件改动):「我的收藏」弹层按类型**分组渲染**(🚗 路线 / 📍 目的地 /
    🛏️ 住宿;住宿=kind place 里的指纹判定:summary.stay_kind/price_estimate 或 🛏️ 名称前缀),
    路线项含 时长/费用/里程/估算徽标,住宿项含 AI 预估价/距目的地/「AI 预估」强标注;
    新增「🧳 组合行程方案」区块:三个下拉(目的地/路线/住宿)+ 晚数输入 → 实时算**大致总花费**
    (交通=路线快照 cost_cny 单程、住宿=预估价区间中值×晚数,缺项明示「未计入」不编数字),
    总账带估算口径说明;方案可保存(localStorage,上限20条)/删除。
  - test_frontend_routes.py 追加 6 个静态断言(DOM/函数面/分组渲染/估算标注/事件接线/价格解析)。
    pytest backend/ = **440 passed**(434 基线零改动,12.1s);node --check 内联 JS 语法通过。
  - browser_exec 真实 QA(uvicorn :8000,StaticFiles 读盘无需重启):POST 三条测试收藏 →
    开面板见三组各 1 项 → 选路线+目的地+住宿(2晚)→ 总账 ¥973(=173+400×2,口径吻合)→
    保存方案见「已保存方案 1 个」→ 删方案回空 → UI 取消收藏三条 → 列表回空态、方案区隐藏 →
    回归点 pin:路线卡片+「☆ 收藏路线」+住宿区块正常;全程 window error **0** 条;
    测试数据已清理,collections 归 0。
  - 备注:后端 collections.py 未动(GET 的 counts_by_kind 早已够用);行程方案存 localStorage
    而非 DB——快照对比属展示层需求,避免为 4a 加表;若神朱要跨设备同步方案再立后端任务。

---

## [TASK-4b] 预订界面 + 跳转预订(deep-link 聚合)

- 状态: done
- 目标: 依 docs/STAGE3-PLAN.md 第 2 节,给最终选定的目的地提供「前往预订」入口:列出可用/已收藏的路线与住宿供勾选组合;点击跳转对应外部应用(住宿→携程/Booking/Airbnb;机票/火车票→12306/OTA;自驾→地图导航)。**仅 deep-link 跳转,不代订、不抓实时价**,页面含免责声明。
- 依赖: TASK-4a。
- 涉及: 仅 backend/app/static/index.html(如需后端加 deep-link 生成则加纯函数 + 单测)
- 验收:
  1. 「前往预订」能列出已收藏/可用的路线与住宿
  2. 跳转按钮 URL 正确、新页打开(携程/12306/地图等)
  3. 页面明确免责(价格仅供参考 · 不代订)
  4. 无 JS 报错;browser QA 通过
- 结果: **完成**(2026-09-26 夜班,执行器直接手写,未派 Codex;commit `be3a6f3`)。
  - index.html(仅前端,零后端改动):收藏弹层新增「🎫 前往预订」区块,列出收藏的路线与住宿:
    铁路→12306 查票(fs/ts/date,站名口径与后端 rail_12306_url 一致);飞机→去哪儿机票搜索
    (+12306 比价备选);自驾→高德导航 + Google 地图;住宿→携程/Booking/Airbnb 按名称搜索
    (标题带 AI 预估价快照)。全部 `<a target="_blank" rel="noopener">`,URL 前端拼、中文百分号编码。
    区块头 + 列表尾双重免责:「价格仅供参考 · 不代订 · 以官方/OTA 实时为准」;无路线/住宿收藏时整块隐藏。
    renderBookSection 挂在 refreshFavItems,收藏增删后同步刷新。
  - test_frontend_routes.py 追加 5 个静态断言(DOM/函数面/免责文案/渠道覆盖 12306+去哪儿+高德+Google+
    携程+Booking+Airbnb/新页打开/与收藏刷新联动)。pytest backend/ = **445 passed**(440 基线零改动,12.8s);
    node --check 内联 JS 语法通过。
  - browser_exec 真实 QA(uvicorn :8000):curl POST 三条测试收藏(驾车/铁路/住宿)→ 开面板见
    「前往预订」3 行、6 个链接 URL 全部正确(12306 带 fs=上海&ts=杭州西湖&date=今天;高德 from/to;
    携程/Booking/Airbnb keyword=西湖国宾馆)、target=_blank rel=noopener 齐、免责声明在 →
    全程 window error **0** 条;测试收藏已 DELETE 清理归 0。
  - 备注:出发日取浏览器本地「今天」(环境即 CST),用户在官方页可自行改;deep-link 全走各官网
    搜索页,不抓价不代订,与 ADR-004/007 口径一致。M4 至此(4a 对比总账 + 4b 预订跳转)收口。

---


## [TASK-6a] Photon 地理编码主路径 + Nominatim 降级（产品化代理口径）

- 状态: done
- 背景: 神朱 2026-09-28 拍板。产品环境无 mihomo 代理，Nominatim 直连实测不通（容器实测 15s 超时）；Photon 直连实测可用（1.1s，中文城市/乡村/区划命中正确坐标，逆地理可用；不支持 lang=zh——用默认本地语言，中国地名自带中文）。方案=Photon 主 + Nominatim 备降级链；Photon 公共实例先用，产品化再自建。
- 目标: 新增 `backend/data_sources/photon.py`；`/api/geocode`、`/api/geocode/reverse` 及起点解析改「先 Photon，失败/空回退 Nominatim」。
- **只读清单（只准读这 5 个，读完立即写码）**: `backend/data_sources/nominatim.py`、`backend/data_sources/_common.py`、`backend/app/api/places.py`（geocode/reverse 端点）、`backend/services/place_loader.py`（起点解析相关段）、含 nominatim 用例的测试文件。禁止其他探索。
- 落地契约:
  - `photon.py`: `SOURCE_NAME="Photon"`、`DEFAULT_ENDPOINT="https://photon.komoot.io"`、`ENV_ENDPOINT="WHERE2GO_PHOTON_ENDPOINT"`；`geocode(q, *, limit=5) -> list[dict]`、`reverse(lat, lng) -> dict`；解析 GeoJSON `features[].geometry.coordinates=[lon,lat]`（lon 在前）与 `properties.{name,city,state,country}`，`display_name` 按「name, city, state, country」跳过空段拼接；输出形状与 nominatim 完全一致 `{lat:float, lng:float, display_name:str}`；网络/格式错误抛 `DataSourceError`；复用 `build_session(source="photon")` 与内置 1 req/s 节流；不传 lang 参数。
  - `_common.py`: `DEFAULT_SOURCE_PROXY` 增 `"photon": PROXY_OFF`（实测 Photon 直连 1.1s、走代理 5s 挂）。
  - API 层: 先 Photon，`DataSourceError` 或空结果→Nominatim；响应加 `"geocoder": "photon"|"nominatim"`；双失败→400 中文报错。
- 验收: 新增 `backend/test_photon.py` ≥12 用例全 mock（坐标解析/lon-lat 顺序/display_name 组装/空结果回退/报错回退/双失败 400/节流）；既有测试零改动；`pytest backend/` 全绿（基线 539）；不动 index.html。完成后重启 uvicorn 冒烟 `/api/geocode?city=北京` 期望 `geocoder=photon`。
- 结果: **完成**（2026-09-28 夜班，Codex 执行，commit `5977127`）。
  - 新增 `backend/data_sources/photon.py`：`geocode(q,*,limit=5)`/`reverse(lat,lng)`，GeoJSON coordinates=[lon,lat] 解析、7 位定点、display_name「name, city, state, country」跳空段拼接（直辖市重复段去重）、build_session(source="photon")+1 req/s 节流、不传 lang、错误抛 DataSourceError；`_common.py` DEFAULT_SOURCE_PROXY 增 `"photon": PROXY_OFF`。
  - `place_loader.py` 降级链 `geocode_with_fallback`/`reverse_with_fallback`（Photon 抛错或空→Nominatim，双失败中文 DataSourceError）+ `resolve_origin_with_source`/`resolve_reverse_origin_with_source`；`app/api/places.py` `/api/geocode`、`/api/geocode/reverse` 响应加 `geocoder: photon|nominatim|none`，正向双失败 502→**400 中文报错**（无既有用例覆盖），逆向维持 200+resolved=false（TASK-1c 口径）。
  - `test_photon.py` 44 用例全 mock（超 ≥12 要求）。执行器复跑 pytest backend/ = **583 passed**（539 基线零改动 + 44 新增）。
  - Codex 抓出契约外真 bug：Photon 逆向路径是 `/reverse` 而非 `/api/reverse`（404），已修正并真实联网冒烟：`/api/geocode?city=北京`→geocoder=photon（39.9057,116.3913）、city=崇礼→四段拼接正确、reverse→photon/resolved=true；Nominatim 降级腿真实走通一次。
  - **Codex 256K 统计**：单次调用 ~27min（14:06-14:33 UTC），function_calls **56**，首轮写码启动后 **~10.3min**（R8 12min 线内、偏紧——本次含基线 pytest 复跑），tokens **196,396**，零 compact。
  - 备注：Photon 主腿刻意宽 `except Exception`（尽力而为前置源，理由见 place_loader docstring），既有 no_network 断言在该段被吞、单测多 ~1s；`data_sources/__init__.py` 未改（POC 路由口径不变）。

---

## [TASK-6b] 渐进抓取 + /api/places 分页（BUG-1 主修复）

- 状态: done
- 背景: 神朱 2026-09-28 拍板。冷抓取整 band 全量（540 配额）导致首屏分钟级；改「首查 30、显示 15、加载更多每次 +30」循环；分段下拉维持不变。
- 目标: 后端按抓取轮次渐进入库与分页读取。**不动前端**（加载更多按钮属 6e）。
- **只读清单**: `backend/services/bands.py`、`backend/services/place_loader.py`、`backend/services/classify.py`（search_groups 配额）、`backend/app/api/places.py`、`backend/db/repository.py`。
- 落地契约:
  - `load_segment(...)` 增 `target_total: Optional[int]`：给定时把 SEARCH_GROUPS 各组 limit 按比例缩到总量≈target_total（每组 `max(2, round(组配额×target_total/540))`），仍只发一次 Overpass 请求；`SegmentFetch` 加列 `fetch_rounds: Integer default 0`，每轮 +1。
  - `GET /api/places` 新增：`page_size`（默认 15，1..100）、`offset`（默认 0）、`more`（默认 false）。库里已有行按距离排序切片；`more=true` 且切片越界且库内 < 该 band 常规全量 → 触发 `target_total=30×(fetch_rounds+1)` 扩抓一轮（去重键 (osm_type,osm_id)，upsert 不覆盖 intro），再切片。响应新增 `total_in_db`/`has_more`/`fetch_rounds`。兼容：不带新参数时行为与旧版一致（返回全量）。
  - 排序稳定：本地 haversine 距离升序 + (osm_type,osm_id) 决胜，翻页不漂移。
- 验收: `backend/test_places_progressive.py` ≥18 用例全 mock（配额缩放求和≈target/轮次递增/扩抓去重/翻页稳定/has_more/非法参数 400/无参兼容）；既有 539 零回归；不动 index.html。
- 结果: **完成**（2026-09-28 夜班，Codex 执行，commit `008d6d6`）。
  - `place_loader.load_segment` 增 `target_total`（各组配额按比例缩到 ≈target，每组下限 MIN_GROUP_BUDGET=2，PROGRESSIVE_STEP=30；仍单次 Overpass 请求）；`SegmentFetch` 加列 `fetch_rounds`（default 0，SQLite 旧库 ALTER TABLE 补列），`repo.bump_fetch_rounds` 每轮 +1；扩抓轮水位 place_count 记库内真实条数（避免 /api/geocode segments 把单轮当总量）。
  - `GET /api/places` 新增 page_size(15, 1..100)/offset(≥0)/more(bool) 分页参数：库内行按距离升序 + (osm_type,osm_id) 决胜稳定排序切片；more=true 且越界且未达全量配额 → 自动扩抓一轮 target_total=30×(fetch_rounds+1) 再切片；响应加 total_in_db/has_more/fetch_rounds + PAGING_NOTE；**不带新参数时与旧版全量行为一致**；非法参数 400 中文报错。
  - `test_places_progressive.py` 39 用例全 mock（超 ≥18 要求）。执行器复跑 pytest backend/ = **622 passed**（583 基线零改动 + 39 新增）。index.html 未动。
  - **Codex 256K 统计**：单次调用 ~37.5min（14:37-15:15 UTC，40min 止损线内），function_calls **62**，首轮写码启动后 **~7.8min**（R8 线内），tokens **243,604**，零 compact。

---

## [TASK-6c] 住宿三件套：负缓存 + 半径阶梯 + 估价异步回填（BUG-3/5）

- 状态: done
- 目标: `services/stays.py`：①空结果/失败负缓存（6h 内同坐标半径直接回缓存态）；②半径阶梯 5→10→30km 自动扩，返回最近一家距离提示；③检索入库即刻返回列表（price_estimate=null），LLM 估价转后台批量（复用 intro 线程池口径）；④空结果分 `no_data / datasource_error / timeout` 三档 `reason` 透传 API（前端文案属 6e）。
- 只读清单: `backend/services/stays.py`、`backend/db/models.py`、`backend/app/api/stays.py`、`backend/services/intro.py`、`backend/test_stays.py`。
- 落地契约: 负缓存可新建 `StayQueryCache` 表（键坐标定点 7 位+radius+kind+reason+fetched_at）；`/api/stays` 响应加 `reason`/`nearest_km`/`estimating`；**估价批量 5 家/prompt（神朱定）**，模型 **qwen3.8-max（神朱定，不做双模型）**，该批解析失败留 null 不抛、不重试超过 1 次。
- 验收: 测试 ≥20 全 mock；同坐标二次请求 0 网络；既有全绿；不动 index.html。
- 结果: **完成**（2026-09-28 夜班，Codex 写码+执行器收口，commit `473c463`）。
  - `db/models.py` 新增 StayQueryCache 表（坐标定点 7 位+radius_m+kind+reason+nearest_km+fetched_at）；`services/stays.py`（+982 行）：负缓存 NEG_CACHE_TTL_S=6h（同坐标同半径命中直接回缓存态、0 网络，过期重查）、半径阶梯 STAY_RADIUS_LADDER_M=(5000,10000,30000)（未显式给 radius_m 时逐级扩，nearest_km=haversine round1）、估价异步批量（5 家/prompt、qwen3.8-max token-plan 单模型、解析失败留 null 不抛、单批重试≤1、intro.py 线程池口径、测试可注入 INLINE_EXECUTOR）、reason 三档 no_data/datasource_error/timeout；`load_or_fetch_stays` 保留兼容、新增 `load_stays` 返回带 reason/nearest_km/estimating 的结果对象；预抓 CLI 加 --ladder/--estimate/--no-wait。
  - `app/api/stays.py` 响应加 reason/nearest_km/estimating + 空态口径 note；既有字段与 400 校验口径不变。index.html 未动。
  - `test_stays_v2.py` 58 用例全 mock（超 ≥20 要求）。执行器复跑 pytest backend/ = **700 passed**（622 基线零改动 + 78 净增）。
  - **Codex 256K 统计**：function_calls **60**，tokens ~5.29M total（含 cache 重放 5.0M）/output 101K+reasoning 63K；**首轮写码 17.2min——超 R8 12min 线**（监控粒度粗未及 kill，但写入后一路正常）；**总时长 40.7min 触发止损被 kill**（实现+测试文件已全部落盘、622 基线绿过一轮，执行器复跑全量后收口 commit，未浪费产物）。
  - 备注：本条是 256K 窗口后 Codex 首次触发 40min 止损——任务体量（stays.py 重写 ~1000 行 + 58 测试）明显重于 6a/6b；后续同类大改建议契约里把「先跑基线 pytest」明确省掉（本次基线跑了两遍共 ~65s）或再拆小。

---

## [TASK-6d] 费用引擎 v2（BUG-4 + 机票公布价锚定区间）

- 状态: done
- 背景: 神朱 2026-09-28 口径：驾车构成明细+整车/人均双标；铁路分档费率+热门对种子；机票要保留对比感但撤假精确——用**民航公布价锚定区间**（纯规则，无 LLM 无 OTA 抓取）：`[公布价近似×典型折扣, 公布价近似]`，公布价按里程分段（<812km ~1.6、812-1600 ~0.95、>1600 ~0.8 元/km 级常数表 `PUBLISHED_FARE_TIERS`），折扣主干商务线 0.45/支线 0.6。
- 只读清单: `backend/services/routes.py`、`backend/app/api/routes.py`、`backend/test_routes.py`、`backend/data_sources/osrm.py`（steps/ref 字段）、`backend/services/seed_data.py`（种子风格）。
- 落地契约:
  - 驾车: `toll_cny=高速里程×区域费率(东0.45/中0.40/西0.35)`（高速里程=OSRM steps 带 G/S ref 段距离和；拿不到退 `总里程×0.55` 并标 `toll_mode="heuristic"`）；`fuel_cny=km×0.08L/km×油价(env WHERE2GO_FUEL_PRICE_CNY_L 默认 8.0)`；新增 `cost_breakdown{toll,fuel,mode}`/`vehicle_label="整车≤4人"`/`per_person_cny`。
  - 铁路: 运营里程≈直线×1.15；费率 300km/h 线 0.46 / 250 线 0.31 元/km（双高铁枢纽判档）；内置 ≥8 对热门城市对真实票价种子（杭州-上海 73、上海-北京 553 等），命中标 `price_source="seed"`。
  - 飞机: 上式区间 `[round(公布×折扣), 公布]`；直线 <400km 或任一端无民航机场（内置 ≥40 城机场表）→ 不给价仅跳转；`flight_low_cny/flight_high_cny` 新字段，`cost_cny=区间中值` 保持兼容；note「动态定价·浮动大·实时价以跳转为准」。
  - 飞机候选阈值 300→600km。
- 验收: `test_routes_cost_v2.py` ≥22 用例 mock/离线；断言样例：杭州→崇儒乡驾车人均口径、上海→北京种子命中 553、<400km 城市对不出机票价；既有 539 零回归；不动 index.html（前端展示属 6e）。
- 结果: **完成**（2026-09-29 夜班，Codex 写码+执行器收口，commit `c3709e9`）。
  - `services/routes.py`（+788 行）：驾车 `toll_cny`=高速里程（OSRM steps G/S ref 段求和，`toll_mode="osrm_refs"`；拿不到退 总里程×0.55 标 `"heuristic"`）×区域费率（东0.45/中0.40/西0.35）+ `fuel_cny`=km×0.08L/km×油价（env `WHERE2GO_FUEL_PRICE_CNY_L` 默认8.0）+ `cost_breakdown{toll,fuel,mode}`/`vehicle_label="整车≤4人"`/`per_person_cny`；铁路 运营里程=直线×1.15、双枢纽判档 0.46/0.31 元/km、≥8 对热门城市对种子（上海-北京 553 等，`price_source="seed"`）；机票 `PUBLISHED_FARE_TIERS` 三段（<812km 1.6 / 812-1600 0.95 / >1600 0.8）×折扣（主干0.45/支线0.6）→ `flight_low_cny/flight_high_cny`，`cost_cny`=中值兼容，<400km 或任一端无机场（73 城机场表）不出价仅 deep-link，note「动态定价·浮动大·实时价以跳转为准」；飞行候选阈值 300→600km。osrm.py 增 steps/ref 解析，api/routes.py 透出新字段。
  - `test_routes_cost_v2.py` 34 用例（超 ≥22 要求）+ test_routes.py 适配；执行器复跑 pytest backend/ = **734 passed**（700 基线零回归；Codex 遗留 2 处测试期望值笔误——「长沙x」枢纽误匹配、trunk 区间手算错——由执行器修正）。index.html 未动。
  - 真机冒烟（uvicorn 重启）：上海→北京 driving kind=real cost 1283（toll 517 osrm_refs + fuel 766）per_person 321；rail 种子命中 553/seed；flight 区间 502-1115 中值 809；300km 无机票价、崇儒乡「没有匹配到民航机场」降级正确。
  - **Codex 256K 统计**：启动 14:05 UTC，首轮写码 **11.9min**（R8 12min 线内、贴线），function_calls **63**，tokens **5.39M total**（含 cache 重放 5.12M）/output 99K+reasoning 56K；**总时长 40min 触发止损被 kill**——kill 时实现+测试已全部落盘、只差最后 commit，与 6c 同款「体量大贴线完成」形态。routes.py 单文件 ~700→1400 行是主要耗时源；后续同类建议在契约里允许分两次调用（实现/测试各一）。
  - 备注：第一次启动因 `.env` 未导出 `ALIBABA_TOKEN_PLAN_API_KEY`（source 未加 `set -a`）秒退，改用 `set -a && source` 重启成功，浪费 ~1min。

---

## [TASK-6e] 前端适配：加载更多 + 费用新口径 + 三档空态（依赖 6b/6c/6d）

- 状态: done
- 目标: index.html：①列表底部「加载更多(每页 15)」接 page_size/offset/more，扩抓中给进度文案；②路线卡驾车「整车/人均」双标+构成 tooltip、机票区间「¥A–B（浮动）」；③住宿空态三档文案（no_data 含「最近的在 X km 外」/datasource_error 可重试/timeout 稍后再试）；④geocoder 字段并入状态栏。
- 涉及: 仅 index.html + test_frontend_routes.py 静态断言 + browser_exec QA。
- 验收: pytest 全绿（含基线增量）；QA 全流程 0 JS error；既有功能不回归。
- 结果: **完成**（2026-09-29 夜班，Codex 执行，commit `d14e998`，~34min 在 40min 止损线内自行完成并 commit）。
  - index.html（+355/-35，仅前端，后端 Python 零改动）：①列表底部「加载更多(每页 15)」接 page_size/offset/more（翻到库尾带 more=true 触发服务端扩抓，进度文案「正在扩抓更多目的地…」，has_more=false 收起）；②路线卡驾车「整车≤4人 / 人均 ¥Y」双标 + cost_breakdown tooltip（toll/fuel/mode 口径说明）、铁路 price_source=seed 标注、机票「¥A–B（浮动）」区间与 null 降级「不出票价，以跳转实时为准」；③住宿空态三档（no_data 含 nearest_km「最近的在 X km 外」/datasource_error 重试按钮/timeout 稍后再试）+ estimating「AI 估价生成中」；④geocoder（Photon 主路径/Nominatim 降级）并入状态栏与页脚。
  - test_frontend_routes.py 追加 22 例（TASK-6e 小节）：DOM/函数面/分页拼接/has_more 消费/双标与 tooltip/三档文案/geocoder；契约断言**真跑后端路由函数**（替身零触网）验证前端引用字段 ⊆ 后端输出键。执行器复跑 pytest backend/ = **756 passed**（734 基线零回归）。
  - browser_exec 真实 QA（CDP chrome 重拉后）：开页 5 pin 0 error → 点「加载更多」出进度文案「正在加载下一页…」→ 落定后按钮复位 → 点 pin 出路线面板：驾车卡「¥66 整车≤4人 / 人均 ¥17」+ tooltip 含油费/过路费口径 → 住宿区块 6 家库缓存 + AI 预估标注 → 状态栏「Photon(主路径)」→ Esc 关闭；全程 window error **0** 条；库内无测试残留（仅 9/27 既有收藏 1 条）。
  - **Codex 256K 统计**：单次调用 ~34min（14:54-15:28 UTC，止损线内），function_calls **78**，首轮写码启动后 **11.3min**（R8 放宽线 20min 内），tokens **7.09M total**（含 cache 重放 6.82M）/output 75K+reasoning 51K，零 compact。

---

## [TASK-6g] 住宿估价 v2：品牌/星级规则表优先（依赖 6c）

- 状态: done
- 目标: `services/stays.py` 估价前置**规则层**：`brand=` 连锁价格带表（汉庭/如家/7天≈180-350、亚朵/全季≈350-550、维也纳≈250-400、希尔顿/万豪系≈700+ 等 ≥25 品牌，含英文名匹配）、`hotel:stars` 1-5 星档位、hostel/guest_house/chalet 类型档、城市线级修正系数（一线/新一线/二三线映射表）。命中直接出区间标 `price_kind="rule"`（0 token）；未命中走 6c 批量 LLM（5 家/prompt、qwen3.8-max）；结果永久缓存。
- 只读清单: `backend/services/stays.py`（6c 后版本）、`backend/db/models.py`、`backend/test_stays.py`。
- 验收: 规则命中路径断言 0 LLM 调用；测试 ≥15 全 mock；不动前端。
- 结果: **完成**（2026-09-29 夜班，Codex 写码+执行器收口，commit `e154c2a`）。
  - `services/stays.py`：估价前置规则层 `rule_price_estimate` → 品牌价格带表（≥25 品牌含英文名、大小写不敏感包含匹配：经济 汉庭/Hanting/如家/7天/锦江之星/格林豪泰/速8/莫泰/海友/怡莱 ≈¥150-300+系数、中档 全季/亚朵/Atour/维也纳/桔子/麗枫/智选假日/美居/诺富特 ≈¥300-550、高档 希尔顿/万豪/喜来登/洲际/凯悦/香格里拉/皇冠假日/索菲特 ≈¥600-1200、奢华 丽思卡尔顿/宝格丽/安缦/华尔道夫/柏悦/瑞吉/半岛 ¥1200+）；`hotel:stars` 1-5 星档；hostel/guest_house/chalet/apartment 类型档；城市线级系数（一线×1.2/新一线×1.05/其他×0.9，内置小表不 import routes）。命中标 `price_kind="rule"`（**0 LLM 调用**，测试注入计数假 client 断言）、未命中回落 6c 批量 LLM 标 `"llm"`、都失败 null 不编数字；`Stay` 表加 `price_kind` 列（SQLite 旧库 ALTER TABLE 补列，db/base.py）；`app/api/stays.py` items 透出 price_kind（属契约允许范围）。
  - `test_stays_rule_price.py` 29 用例（超 ≥15 要求）+ test_stays/test_stays_api 适配。执行器复跑 pytest backend/ = **785 passed**（756 基线零回归）。index.html 未动。
  - 真机冒烟（uvicorn 重启）：`/api/stays` 上海市中心 140 家 source=db 正常，存量缓存行 price_kind=null 属预期（规则层只作用于新估价，旧缓存永久保留口径不变）；规则函数抽验：汉庭→(180,360,rule)、Atour→(300,550,rule)、青旅→(50,150,rule)、某某宾馆→None 回落。
  - **Codex 256K 统计**：function_calls **86**，首轮写码启动后 **11.2min**（R8 线内），tokens **7.31M total**（含 cache 重放 6.91M）/output 91K+reasoning 52K；**总时长 40min 触发止损被 kill**——kill 时实现+测试已全部落盘且全量 pytest 绿（执行器复跑确认），只差 commit，与 6c/6d 同款形态。
  - 备注：连续三条（6c/6d/6g）都是「产物完整但贴 40min 线被 kill」——stays.py 已 ~1000 行、routes.py ~1400 行,单文件体量是主因；后续大改任务建议契约明示「分两次调用（实现/测试各一）」或拆条目。

---

## [TASK-7a] 搜索/重新抓取提速:环形抓取组间并行 + OriginCache 地理编码持久缓存(神朱 2026-09-30 拍板方案①)

- 状态: done
- 目标: 冷抓「重新抓取」从 ~383s 串行降到 ~120-150s(6 组并行、错峰端点);/api/geocode 重复城市从 2.7~3.4s 降到 <0.05s(持久缓存 7 天)。
- 涉及: backend/data_sources/overpass.py、backend/db/models.py、backend/db/repository.py、backend/app/api/places.py(+测试)
- 契约: **逐字执行 `docs/TASK-7a-CONTRACT.md`**(含实测数据、只读清单、落地契约、线程安全要求、失败语义不变、测试口径、不许动清单)——R9 已满足,勿再自行探索。
- 验收: pytest 全绿(基线 785,新增用例后 ≥789,全 mock 不触网);commit message 按契约;主会话白天复跑真机冒烟(冷抓计时 + geocode 二连击)。
- 结果: **完成**(2026-09-30 夜班,Codex 执行,commit `a8d252a`)。
  - overpass.py(+233 行):`nearby_places_ring` 组间 ThreadPoolExecutor 并行(`WHERE2GO_OVERPASS_WORKERS` 默认 3、钳制 1~4),`execute`/`_ring_request` 加 `start_index` 端点错峰(第 i 组从 endpoints[i%len] 起,失败仍走全链+2 次重试);每 worker 独立 session(threading.local 构造,实测每线程各建 1 个);`_ring_group_rows` 选择器降级嵌套并行(≤2,总并发≤4);失败语义不变(单组 OOM → DataSourceError 带组名、取消未开跑组、不写残缺水位、不吞成空列表);单圆路径(inner=0)仍委托 nearby_places_grouped 原样。
  - db:新表 `OriginCache`(city 主键/name/lat/lng/geocoder/updated_at,坐标定点 7 位),repository 加 get_origin_cache/upsert_origin_cache(对齐 StayQueryCache 风格);旧库 create_all 自动建表不动存量。
  - app/api/places.py::geocode_city:先查缓存 TTL=`WHERE2GO_ORIGIN_CACHE_TTL_S` 默认 604800(7 天);命中零网络、响应形状不变(geocoder 存原值);仅 photon/nominatim 写缓存,none/坐标退化不写;resolve_origin_with_source 签名未动。
  - 测试:新增 7 例(4 并行:峰值并发/墙钟<串行/端点轮转/每线程独立 session + 嵌套拆分并行/workers 钳制;3 缓存:命中零网络、geocoder 原值透出、none 不写)+ 2 例既有串行断言 monkeypatch WORKERS=1 钉旧口径。执行器复跑 pytest backend/ = **792 passed**(785 基线零回归 + 7 净增,33s)。index.html/SEARCH_GROUPS/渐进口径/nearby_places/端点链成员均未动。
  - **Codex 256K 统计**:单次调用 37.9min(40min 止损线内自行完成全部实现+测试+全量绿),function_calls **71**,首轮写码启动后 **~12.4min**(R8 12min 线贴线略超,写入后一路正常),tokens **4.77M total**(含 cache 重放 4.54M)/output 73K+reasoning 47K,零 compact。
  - 备注:真机冒烟(新城市冷抓墙钟 + geocode 二连击)按契约留主会话白天做;uvicorn :8000 已重启(Python 有变)。仓库根发现未跟踪文件 `AzureMapsKey.txt`(明文 key 样态,9/30 09:56 UTC 落盘、非夜班产物、未 commit)——待神朱处置(建议 gitignore 或移出仓库)。
  - **2026-10-01 神朱裁定:本条待办复验取消,不再执行**——项目已切换到高德地图(POI 检索/地理编码/前端地图,详见 TASK-9),Overpass 冷抓链与 Photon 地理编码将整体移除,**TASK-7a 的「组间并行」与「OriginCache」两个优化点失去对象**:冷抓墙钟复验(383s→120-150s)与 geocode 二连击复验**一律不做**。本条代码在切换任务(TASK-9a)落地时按契约一并移除,勿单独回滚。

## [TASK-9a] 高德数据源层:amap.py(geocode/regeo/around/polygon/driving)+ 四分类 typecode 映射

- 状态: done
- 目标: 新建 `backend/data_sources/amap.py`(v3 REST:正向/逆地理编码、周边搜索、多边形搜索、驾车;归一化 POI 形状;`status/infocode` 错误翻译;0.4s 节流;直连代理口径)+ `backend/services/amap_categories.py`(四分类检索组 `AMAP_TYPE_GROUPS` + `classify_amap_poi` 优先级 滑雪>运动>人文美食>自然 + `dedupe_key=("amap", <高德POI id>)`)+ `_common.py` 加 `"amap": PROXY_OFF`。含分格纯函数 `grid_polygons` 与 `decode_polyline`。
- 只读清单: `backend/data_sources/_common.py`、`backend/data_sources/photon.py`(写法样板)、`backend/db/models.py`、`backend/db/repository.py`、`docs/TASK-9-CONTRACT.md`(§1 实测数据 + §3「TASK-9a」逐字执行)。
- 涉及: backend/data_sources/amap.py(新)、backend/services/amap_categories.py(新)、backend/data_sources/_common.py(加源)、backend/test_amap.py(新 ≥25 用例,全 mock 不触网)、backend/test_amap_categories.py
- 验收: pytest 基线 792 零回归 + ≥25 新增;`status!="1"` 按 infocode 分派 `DataSourceError`/`TransientDataSourceError`(10021→Transient)有用例;缺 key 报中文错不崩;归一化 POI 键名与契约逐字一致;`grid_polygons` 格子数/坐标顺序有用例;节流有用例(两次调用间隔 ≥0.4s,monkeypatch 时钟)。
- 结果: **完成**(2026-10-01 夜班,Codex 执行,commit `4a9ab68`)。
  - 新增 `data_sources/amap.py`(892 行):geocode/reverse_geocode/search_around/search_polygon/driving + decode_polyline/grid_polygons,全部 environ=/session= 可注入;SOURCE_NAME="amap"、radius 钳 50000、page>8 空返回不发请求、节流 0.6s(env 可覆盖)、瞬时错误退避 2s/5s ≤3 次;infocode 按 §6.2 细分表全量映射(10021 等 9 个→Transient,10001/10009 等 11 个→永久,表外带原文),HTTP 码不参与判错;驾车 strategy 只留签名不进请求、多 paths 取 [0]、不透出 tolls/cost 只给 toll_distance_m、polyline 明文解析+GEOMETRY_MAX_POINTS=1200 抽稀(自定义常量未 import osrm)。
  - 新增 `services/amap_categories.py`(232 行):AMAP_TYPE_GROUPS 五组(滑雪 080106/运动 080000/人文美食 110000+050000/小城古镇 keywords=古镇|老街|古城/自然 110000),配额与既有 SEARCH_GROUPS 一致(合计 540);classify_amap_poi 优先级 滑雪>运动>人文美食>自然、人工设施名归其他、复合专名细化(湿地公园/森林公园/自然保护区等在风景名胜大类内归自然);dedupe_key=("amap", id)。`_common.py` 加 "amap": PROXY_OFF。
  - 测试:test_amap.py 133 例 + test_amap_categories.py 62 例(超 ≥25 要求),全 mock。执行器复跑 pytest backend/ = **987 passed**(792 基线零回归 + 195 新增,38.9s)。
  - **Codex 256K 统计**:单次调用 ~35min(14:10-14:46 UTC,40min 止损线内),function_calls **54**,首轮写码启动后 **9.8min**(R8 12min 线内),tokens **241,572**(total 4.32M 含 cache 重放),零 compact。
  - 备注:未动 app/、未改表结构、未新增依赖;未 push。

---

## [TASK-9b] POI/住宿检索链切高德:入库身份改 amap + 分格多边形 + 渐进口径不变 + 清库脚本
- 依赖: TASK-9a(amap.py 与 amap_categories 已建)

- 状态: done
- 目标: `services/classify.py` 检索组换 `AMAP_TYPE_GROUPS`(优先级/去重/预算语义保留);`services/place_loader.py` 的 `default_fetcher` 换高德:`low==0` → `search_around(radius=high)`,`low>0` → 包围盒 `grid_polygons` 分格 + `search_polygon` + 本地 haversine 收敛 `[low,high)`,扩格随 `fetch_rounds` 递增(口径同 `progressive_target_total`);入库 `osm_type="amap"`/`osm_id=<高德 id>`(表结构零改动)、`tags.source="高德"`、`place_source()` 加「高德」分支;`stays.py::search_stays` 换 `search_around(types="100000")`(半径阶梯/负缓存/估价口径全不变);新增 `tools/amap_cutover.py`(默认 --dry-run 报数,--apply 清派生行,**保留 Collection/CollectionCat/TripPlan**)。
- 只读清单: `backend/services/place_loader.py`、`backend/services/classify.py`、`backend/services/stays.py`、`backend/db/repository.py`、`docs/TASK-9-CONTRACT.md`(§3「TASK-9b」逐字执行)。
- 涉及: backend/services/(classify.py、place_loader.py、stays.py、amap_categories 已建)、backend/db/repository.py(place_source)、backend/app/api/(仅在不得已时改动,**响应键名必须零变化**)、tools/amap_cutover.py(新)、backend/test_places*.py/test_stays*.py/test_classify.py(改替身为 amap + 新增 ≥30 用例)
- 验收: pytest 零回归 + ≥30 新增;**`/api/places`、`/api/places/meta`、`/api/places/intros`、`/api/stays` 响应键名/形状逐键断言零变化**(前端零改动的证据);同 (城市,band) 二次查询读库零网络;`page_size/offset/more` 行为与旧口径一致;清库脚本 `--dry-run`/`--apply` 各有用例且不删用户数据。
- 结果: **完成**(2026-10-01 夜班,Codex 第1轮写实现(40min 止损被 kill,产物完整)+Codex 第2轮改测试(再触止损)+执行器手写收口最后 17 例,commit `31c3ab9`)。
  - 检索链:classify.py 检索组换 AMAP_TYPE_GROUPS(优先级/去重/budget 语义保留);place_loader.py 的 default_fetcher 换高德——low==0 → search_around(radius=high),low>0 → 包围盒 grid_polygons 分格 + search_polygon + 本地 haversine 收敛 [low,high),扩格随 fetch_rounds 递增;SegmentFetch.source="amap";水位语义不变(二次查询读库零网络);渐进口径(PROGRESSIVE_STEP/page_size/offset/more)不变。
  - 入库身份:`db/models.py` 加 `amap_osm_id()`(zlib.crc32 & 0xFFFFFFFF)与 AMAP_OSM_TYPE/AMAP_SOURCE 常量(**列结构零改动**);osm_type="amap"、原始 id 存 tags.amap_id、tags.source="高德";repository.place_source() 加「高德」分支(存量「种子/OSM」标注不受影响)。
  - 住宿:stays.py::search_stays/fetch_stay_pois 换 amap.search_around(types="100000"),Stay 身份同 crc32 口径;半径阶梯/负缓存三档/批量 LLM 估价/price_kind 规则表全不变;SOURCE_FETCH="amap"。
  - 清库脚本:tools/amap_cutover.py(默认 --dry-run 报数、--apply 单事务清 9 张派生表、保留 collections/collection_cats/trip_plans、缺表自动跳过、apply 幂等)。
  - 测试:替身全面改 amap(test_places/classify/progressive/stays 系列),新增 37 例(Codex 31 + 执行器补 test_amap_cutover.py 6);/api/places 与 /api/stays 顶层形状逐键断言保留(LEGACY_TOP_LEVEL_KEYS)。执行器复跑 pytest backend/ = **998 passed**(987 基线零回归 + 11 净增,40s)。index.html 未动、photon/nominatim 未动、overpass/osrm 未删(留 9c)。
  - **Codex 256K 统计**:第1轮(实现)40.7min 触止损被 kill,产物完整(function_calls 106、首写 8.6min R8 线内、tokens 11.2M total 含 cache);第2轮(测试适配)40min 再触止损被 kill(119 calls、tokens 14.6M total)——**R10 二次熔断触发,剩余 17 例由执行器手写收口**(~35min:stays_api/rule_price/v2 三文件替身适配+6 例清库脚本用例)。教训同 6c/6d/6g:实现+测试同晚一体量必贴线,9b 这种「8 个测试文件替身重写」应在契约里明示分两次调用。

---

## [TASK-9c] 地理编码切高德主链路 + 驾车切高德 + 物理删除 overpass.py/osrm.py
- 依赖: TASK-9a(amap.geocode/driving);与 TASK-9b 无强耦合,可并行/续做

- 状态: done
- 目标: `resolve_origin_with_source` 主链路改 `amap.geocode`(`geocoder="amap"`),失败/无 key 回落 photon→nominatim(保留不删),`/api/geocode` 响应键与 `resolved` 语义不变;`OriginCache` **保留复用**(geocoder 存 amap,TTL 7 天不变;TASK-7a 的 Overpass 并行部分随 overpass.py 移除);`services/routes.py` 驾车改 `amap.driving`(真实 distance/duration,`kind="real"` 不变;**过路费 = `toll_distance_m` × 区域费率**,`cost_breakdown.mode="amap_toll_distance"`;油费/铁路/飞机估算/600km 阈值/机票公布价/人均/deep-link 全不变);geometry 用解码 polyline(抽稀口径不变);**物理删除(`git rm`)`data_sources/overpass.py`、`data_sources/osrm.py` 及其在测试中的直接引用**(神朱 10/01 二次确认:不保留休眠文件)。
- 只读清单: `backend/services/place_loader.py`、`backend/services/routes.py`、`backend/app/api/routes.py`、`backend/db/repository.py`、`docs/TASK-9-CONTRACT.md`(§1.5/§1.6 + §3「TASK-9c」逐字执行)。
- 涉及: backend/services/(place_loader.py、routes.py)、backend/app/api/(geocode/routes 透出)、backend/data_sources/(删 overpass.py、osrm.py)、backend/test_routes*.py/test_photon.py/test_data_sources.py(引用改 amap 替身 + 新增 ≥20 用例)
- 验收: pytest 零回归 + ≥20 新增;`/api/geocode` 键名不变且 `geocoder=amap` 命中;OriginCache 命中零网络有用例;驾车 `toll_cny` 来自 `toll_distance` 而非高德 `tolls`(用例断言 tolls=0 时仍算出非零过路费);铁路/飞机/机票/deep-link 断言零回归;全仓 `grep -rn "overpass\|osrm" backend/` 仅剩历史注释/文档。
- 结果: **完成**(2026-10-01 夜班,Codex 写码(40min 触止损被 kill,产物已完整)+执行器手写收口 4 处测试适配+5 例 amap 地理编码用例,commit `2845eb1`)。
  - 地理编码:place_loader 三腿链 高德主→Photon→Nominatim(`ds_amap_geocode`/`amap_geocode()` 形状对齐;空结果/无 key 当失败降级;三腿全挂报「三个地理编码源都失败」带三边原因);/api/geocode 响应键 {origin,geocoder,bands,segments} 与切换前逐键一致、geocoder=amap;OriginCache 保留复用(geocoder 存 amap,TTL 7 天,命中零网络);逆地理同链。
  - 驾车:routes.py 的 default_router 换 amap.driving(TOLL_MODE_AMAP_TOLL_DISTANCE 替代 osrm_refs;过路费=toll_distance_km×区域费率 东0.45/中0.40/西0.35;toll_distance 缺失退 heuristic 0.55);油费/铁路分档/机票公布价/600km 阈值/per_person/deep-link 全不变;geometry 用 amap 解码 polyline。
  - **物理删除**:`git rm data_sources/overpass.py、osrm.py、verify_poc.py`;data_sources/__init__.py 导出清理;grep 复核 `import overpass|osrm` 零残留,剩余字样全是注释/文档/存量库模拟行(source="overpass" 是旧库真实值)。
  - 测试:test_routes/test_routes_cost_v2/test_data_sources 替身改 amap(删 osrm/overpass 专属用例 59 个),新增 27 例(amap_toll_distance 两路径/tolls=0 仍出过路费/三腿降级链/OriginCache 存 amap/几何抽稀);执行器手写收口:test_photon 三腿双失败断言适配+补 5 例(chain_prefers_amap/无key回落photon/空结果回落/reverse优先amap/api geocoder=amap+缓存零网络),前端 TOLL_MODE_LABEL 加 amap_toll_distance 键。执行器复跑 pytest backend/ = **971 passed**(998 − 59 删除模块专属用例 + 27 新增 + 5 执行器补,47s)。
  - **Codex 256K 统计**:40min 触止损被 kill(function_calls 109、首写 11.8min R8 线内、tokens 9.98M total 含 cache 重放),kill 时实现+测试已全部落盘,只差最后 4 处适配——与 6c/6d/6g/9b 同款「体量大贴线」形态。

---

## [TASK-9d] 前端地图切高德 JS API 2.0:Leaflet 退役 + /api/map-config 运行时注入 JS key(与本晚 9c 连做)
- 依赖: TASK-9b + TASK-9c(后端接口就绪;本项目前端零改动的约束由 9b 保证)

- 状态: done
- 目标: `index.html` 移除 Leaflet(CDN+OSM 瓦片)改高德 JS API 2.0(官方路径;真机若报 `INVALID_USER_SCODE` 则降级为「保留 Leaflet + 换高德无 key 栅格瓦片」并注明待升级,不许空过);新增后端 `GET /api/map-config` 返回 `{"amap_js_key","amap_security_js_code"}`(取自 env,**不进 git、不写进 index.html**),前端加载 JS API **前**先写 `window._AMapSecurityConfig={securityJsCode:…}`(**安全密钥已由神朱提供并落 `.env`**)再动态注入 `<script>`、无 key 给明确降级文案;环圈→`AMap.Circle`(外实内虚)、pin→`AMap.Marker`(保留分类色/emoji 自绘)、点 pin→`AMap.InfoWindow` 承载现有 `popupHtml()`(含收藏/路线/「📄 查看详情」按钮,为 TASK-8b 铺路)、路线→`AMap.Polyline`(驾车实线/铁路虚线/飞机 `arcPoints` 弧线)、`fitView` 用环圈 bounds;`identityHtml()` 改「来源:高德 · POI <id> · GCJ-02」,种子行加 WGS-84 偏差提示;状态栏数据源文案改高德口径。
- 只读清单: `backend/app/static/index.html`、`backend/test_frontend_routes.py`、`backend/app/api/places.py`(加 map-config 的风格)、`docs/TASK-9-CONTRACT.md`(§3「TASK-9d」逐字执行)。
- 涉及: backend/app/static/index.html、backend/test_frontend_routes.py(≥12 新断言 + node --check)、backend/app/api/(新增 map-config 路由)
- 验收: pytest 零回归 + ≥12 新增断言(`/api/map-config` 调用、`AMap.` 使用、`AMap.InfoWindow`、**index.html 内不得出现 32 位 key 样态字符串**);browser_exec 真机 QA:开页 0 error → 高德地图渲染 → pin+环圈 → 点 pin 出 InfoWindow → 点路线出面板并画线;安全密钥已就绪,若仍报 `INVALID_USER_SCODE` → 按契约授权**当轮降级为「Leaflet + 高德无 key 栅格瓦片」并注明待升级**,不许空过。
- 结果: **完成·官方 JS API 2.0 路径,未触发降级**(2026-10-01 夜班,Codex 写码(40min 触止损被 kill,实现+测试已完整落盘且自验绿)+执行器复验收口,commit `a352e1f`)。
  - index.html(+363/-121):Leaflet CDN/OSM 瓦片整体移除;启动先 fetch /api/map-config → 写 window._AMapSecurityConfig → 动态注入 webapi.amap.com/maps?v=2.0 脚本(无 key 给「地图未配置高德 JS key」降级文案,不抛错);环圈 AMap.Circle/CircleMarker(外实内虚+起点圆)、pin AMap.Marker 自绘 content(分类色/emoji 保留)、**marker.on("click") 绑 InfoWindow**(§1.7 坑照做)、路线 AMap.Polyline(驾车实线/铁路虚线/飞机弧线)、fitView 用 setFitView;坐标统一 lngLat() 转 [lng,lat];identityHtml 改「来源:高德 · POI <amap_id> · 坐标(GCJ-02)」、种子行 WGS-84 偏差提示;状态栏/页脚数据源文案全改高德口径(toll_modes 同步 amap_toll_distance);既有功能(收藏/路线面板/住宿区块/tab/加载更多/搜索/定位/补简介)零删改。
  - 后端:+37 行,GET /api/map-config(运行时读 env WHERE2GO_AMAP_JS_KEY/SECURITY_JS_CODE、strip、无 env 回空串,note 写明 _AMapSecurityConfig 先于脚本口径)。key 不进 git:index.html 内 32 位 hex 样态字符串 0 命中(测试断言)。
  - 测试:test_frontend_routes.py +250 行(Leaflet 断言删除,高德断言:map-config fetch/AMap 使用/InfoWindow/安全密钥先写/click 绑定/无 key 样态/env 名对齐等 12+);node --check 内联 JS 通过。执行器复跑 pytest backend/ = **989 passed**(971 基线零回归 + 18 净增,49s)。
  - **真机 QA(browser_exec,CDP chrome,uvicorn 已重启)**:开页 0 error → JS key 运行时注入、_AMapSecurityConfig 就位、**无 INVALID_USER_SCODE**、瓦片渲染 15 块 → 上海 50-100 冷抓(高德,~125s 含 LLM 简介 24/28)→ 切「全部目的地」19 pin + 双环圈 → **受信任真实点击 pin → InfoWindow 打开**(名称/分类/距离/简介/⭐收藏/🚗路线/📄查看详情/「来源:高德 · POI B0IA…」)→ 点「路线对比」→ 面板出驾车卡片(高德 real 1小时2分/¥59/人均¥15/65.4km,note 写明 toll_distance 口径)+ 地图画线 2 条 → Esc 关闭 → 状态栏「来源:高德实时抓取」/页脚高德口径 → 全程 window.__errs **0** 条。QA 提示:pin 若在视口外需先 scrollIntoView 再取坐标点击。
  - **Codex 256K 统计**:40min 触止损被 kill(function_calls 114、首写 ~9min 在 R8 放宽线 20min 内、tokens 9.30M total 含 cache 重放);kill 时前端+后端+测试全部落盘,只差 commit——四条 TASK-9 全部呈「产物完整、贴 40min 线」形态,256K 窗口下无一次 compact/重读回圈。

---

## [TASK-8a1] 目的地图片链路:高德 POI 图(主) + 维基百科/Commons(兜底) + PlaceMedia 缓存 + /api/places/media

- 状态: done
- 目标: 落地点详情弹窗的「相关图片」后端:新建 `data_sources/amap.py`(高德 Web 服务 place/text 取 `pois[0].photos`,带坐标门控防海外误配)与 `data_sources/wikimedia.py`(zh.wikipedia geosearch 优先、search 兜底 + Commons 相册补图);新服务 `services/place_media.py`(高德优先→维基兜底→无图占位,命中缓存 7 天/空结果负缓存 6h);新表 `PlaceMedia`;新 API `GET /api/places/media?place_ids=`(batch ≤20,读库优先、仅 miss 触网,单条失败不影响其余)。
- 只读清单: `backend/data_sources/_common.py`、`backend/data_sources/photon.py`、`backend/db/models.py`、`backend/db/repository.py`、`docs/TASK-8-CONTRACT.md`(§1~§3.8a1 逐字执行;参考只读 `backend/app/api/places.py` 的 details 批量形态)。
  - ⚠️ 前置依赖(2026-10-01 神朱裁定切换高德后补记):`data_sources/amap.py` 由 **TASK-9a(高德数据源层)** 首个创建;本条**不得新建第二个 amap.py**,改为「往既有 `amap.py` 追加 `search_poi_photos`」。执行顺序:排在 TASK-9a 之后;其余(place/text 取 photos、坐标门控、维基兜底、PlaceMedia、/api/places/media)口径不变。
- 涉及: backend/data_sources/(新增 amap.py、wikimedia.py、改 _common.py 加 amap 源)、backend/services/place_media.py(新)、backend/db/(models.py 加 PlaceMedia 表、repository.py 加读写)、backend/app/api/places.py(加 /api/places/media)、backend/test_*.py(≥22 新用例,全 mock 不触网)
- 验收: pytest baseline 792 零回归 + ≥22 新增;契约文档 §3「TASK-8a1」全部字段/响应键/ENV 名逐字照做;真机冒烟 `/api/places/media?place_ids=<西湖>` 出图并标注 source;缺 key 时降级不报错(返回 source=none/reason=no_key)。
- 结果: **完成**(2026-10-02 夜班,Codex 写码(40.9min 触止损被 kill,产物完整)+执行器收口,commit `aff42a8` + 微修 `2dcb0c7`)。
  - 既有 `amap.py` 追加 `search_poi_photos`(place/text v3、坐标门控 haversine≤WHERE2GO_AMAP_MAX_MATCH_M 默认 5000、http→https、status!=1→[] 不抛、HTTP 错→DataSourceError);新 `data_sources/wikimedia.py`(geosearch 优先/search 兜底/pageimages 主图 800px/Commons 补图 ≤limit、thumburl 剥 ?utm_*、失败不抛 via=none);`PlaceMedia` 表(place_id unique、source 四态、reason 三档);`services/place_media.py`(缓存 7 天/负缓存 6h/图上限 6、amap→wikimedia 兜底→mixed);`GET /api/places/media`(batch≤20、按入参顺序、单条失败不扩散)。
  - 测试:test_wikimedia.py + test_place_media.py 共 **85 新增**(超 ≥22);执行器复跑 pytest backend/ = **1074 passed**(989 基线零回归)。执行器微修:维基主图 thumburl 同样剥 ?utm_*(契约只点名 Commons,同款噪声一并清)。
  - 真机冒烟(uvicorn 已重启):place_ids=1,5,4 → source=mixed(高德+维基双源)、0.012s 缓存命中 cached=true;库内无西湖行(清库后杭州 0_50 段是市民中心周边),用 place_id=1「牡丹园湖边」替代验收,出图 2-4 张/条。
  - **Codex 256K 统计**:40.9min 触止损被 kill(function_calls **63**、首写 **10.8min** R8 线内、tokens **4.79M total** 含 cache 重放),kill 时实现+测试全部落盘且 1074 全绿,只差 commit——与 6c/6d/6g/9b/9c/9d 同款贴线形态。

---

## [TASK-8a2] 类别专属要点:LLM 按分类出结构化字段 + PlaceHighlight 永久缓存 + /api/places/highlights

- 状态: done
- 目标: 新建 `services/highlights.py`:四分类固定字段表(自然→最佳季节/门票开放/游玩建议;人文美食→人文背景/必吃/代表小店;滑雪→雪道数与分级/开放期/适合人群;运动→项目/场地装备/适宜人群),严格 JSON 输出、**查不到置 null 并在 note 标「待核实」禁止编造**;预算 `HIGHLIGHT_MAX_TOKENS=600 / HIGHLIGHT_TIMEOUT_S=60`(必须按调用放大,勿沿用 intro.py 的 120/20s);新表 `PlaceHighlight`(生成后永久缓存);新 API `GET /api/places/highlights?place_ids=`(batch ≤10,读库优先)。
- 只读清单: `backend/services/details.py`(LLM 预算与缓存口径样板)、`backend/db/models.py`、`backend/db/repository.py`、`backend/app/api/places.py`(details 端点形态)、`docs/TASK-8-CONTRACT.md`(§3「TASK-8a2」逐字执行)。
- 涉及: backend/services/highlights.py(新)、backend/db/(models.py 加 PlaceHighlight、repository.py)、backend/app/api/places.py(加 /api/places/highlights)、backend/test_highlights.py(≥12 用例,LLM 全 mock 不触网)
- 验收: pytest 零回归 + ≥12 新增;四分类字段表逐字一致;「拿不到→null+待核实」有专门用例;LLM 异常/解析失败降级为 fields=[] 不抛 500;单次调用确实放大到 600/60(用例断言传入参数)。
- 结果: **完成**(2026-10-02 夜班,Codex 执行,**~17min 自行完成全部实现+测试+commit,未触止损线**——本晚唯一一条,commit `5a94dbd`)。
  - 新 `services/highlights.py`:HIGHLIGHT_MAX_TOKENS=600/HIGHLIGHT_TIMEOUT_S=60 **按调用传给 llm.chat()**(不沿用 intro 120/20s);CATEGORY_FIELDS 五张固定字段表;category_key() 包含匹配+归类优先级(滑雪>运动>人文美食>自然,全称/短名都落对表);fetch_highlights 走 resolve_provider/LLMClient 唯一入口、严格 JSON prompt、查不到 value=null+note 标「待核实」、异常/坏 JSON→fields=[]+note=解析失败不抛、失败不写行可重试。
  - `PlaceHighlight` 表(place_id unique/category/fields JSON/note/generated_at,永久缓存无 TTL);repository 照 PlaceMedia 风格加读写(value=None 是合法值不剔);`GET /api/places/highlights`(batch≤10 超限/缺参/非法 id 400 中文、items 按入参顺序、读库优先仅 miss 调 LLM、单条失败只影响该条仍 200)。
  - 测试:test_highlights.py **64 例**(超 ≥12;含注入替身断言 max_tokens=600/timeout=60 确实传入)。执行器复跑 pytest backend/ = **1138 passed**(1074 基线零回归,54s)。index.html 未动。
  - 真机冒烟(uvicorn 已重启):place_id=1(自然风光)→ LLM 实调 37s 出 3 字段(门票=null 标「待核实」不编造)→ 二次请求 0.013s cached=true 零 LLM。
  - **Codex 256K 统计**:单次调用 ~17min(14:54-15:11 UTC,止损线内),function_calls **37**,首写 **3.4min**(R8 线内),tokens **1.70M total**(含 cache 重放),Codex 自报 output 124,922;体量小(单服务+单表+单端点)是未贴线主因,印证「拆小条目」口径。

---

## [TASK-8b] 前端目的地详情弹窗:pin「查看详情」+ 列表行整行可点 + 图片区 + 类别要点 + 路线/住宿按钮(依赖 8a1/8a2)

- 状态: done
- 前置依赖: 排在 TASK-9c(前端地图切高德 JS API)之后 —— 弹窗直接建在高德地图上,避免 Leaflet→高德 对同一段 pin/弹窗代码重写两次。
- 目标: `index.html` 新增居中详情弹窗 `#placeModal`(role=dialog/aria-modal,Esc+点遮罩+✕ 关闭):标题/类别/距离 → 图片区(打开时才调 `/api/places/media`,骨架态,空/失败给「暂无图片」+**图源标注**「高德/维基百科」+ page_url 外链) → 详细介绍(复用 `detailTextOf` + `/api/places/details` 生成入口) → 类别专属要点(`/api/places/highlights`,骨架+「待核实」弱化样式) → `🚗 路线 / 🛏️ 住宿`(复用 `openRoutePanel`)+ ⭐收藏 → `identityHtml` 脚注。入口:①`popupHtml()` 加「📄 查看详情」按钮(pin 仍先出小 popup);②列表 `.pl-row` 整行可点开同一弹窗,行内既有按钮 `stopPropagation` 不回归。不新增前端 key。
- 只读清单: `backend/app/static/index.html`、`backend/test_frontend_routes.py`、`docs/TASK-8-CONTRACT.md`(§3「TASK-8b」逐字执行)。
- 涉及: backend/app/static/index.html、backend/test_frontend_routes.py(≥12 静态断言 + node --check)
- 验收: pytest 零回归 + ≥12 新增断言(DOM id/函数名/接口字符串/「图源」文案/batch 参数拼接/stopPropagation);browser_exec QA:开页 0 error → 点 pin →「查看详情」→ 弹窗出图/介绍/要点 → 路线住宿按钮出面板 → Esc 关闭 → 列表行开同一弹窗;既有收藏/路线/住宿行为零回归。
- 结果: **完成**(2026-10-02 夜班,Codex 写码(41.4min 触止损被 kill,实现+测试+commit 已全部完成)+执行器真机 QA 复验,commit `902966c`)。
  - index.html:新增居中 `#placeModal`(role=dialog/aria-modal/hidden,Esc+遮罩+✕ 关闭、焦点记忆归还);打开时才拉 `/api/places/media`(骨架态→主图+缩略图条、图源标注三态「图源:高德/维基百科/高德 + 维基百科」、reason 分档文案、page_url 外链、onerror 隐藏坏图)+ `/api/places/highlights`(骨架态、null→「待核实」弱化、note 透出);详细介绍复用 detailTextOf/生成按钮;动作行 🚗路线/🛏️住宿(js-routes→openRoutePanel)+⭐收藏(js-place-fav 同源);identityHtml 脚注。入口①popupHtml 加「📄 查看详情」.js-detail-modal(原跳转按钮改名「📍 定位到列表行」避免文案冲突,js-place-detail/focusPlaceRow 保留);②.pl-row 整行可点、行内按钮 stopPropagation。Esc 层级:详情弹窗→路线面板→收藏弹层。
  - test_frontend_routes.py 追加 TASK-8b 段(11 个测试函数/87 断言,超 ≥12 要求);node --check 通过。执行器复跑 pytest backend/ = **1149 passed**(1138 基线零回归,53s)。后端 Python 零改动。
  - browser_exec 真机 QA(CDP chrome 僵死重启后,uvicorn 最新代码):开页 0 error → 滚动后受信任点击地图 pin → InfoWindow 出「📄 查看详情」→ 弹窗(滴水湖):图片 7 张加载完成+「图源:高德」标注 → 详细介绍在 → 类别要点(运动:项目/场地装备/适宜人群)LLM 实调出字段+免责 note → 点「🚗 路线/🛏️ 住宿」→ routePanel.open=true → Esc 先关弹窗再关面板(层级正确)→ 列表行整行点击开同一弹窗 → 行内收藏按钮点击 stopPropagation 弹窗不误开 → 全程 window.__errs **0** 条;QA 误加的收藏(id=7)已 DELETE 清理,库回到 3 条既有收藏。
  - **Codex 256K 统计**:41.4min 触止损被 kill(function_calls **114**、首写 **16.8min** 在 R8 前端放宽线 20min 内、tokens ~2.9M+ total 含 cache 重放),kill 时已完成实现+测试+全量绿+commit,只差最终 summary——前端四条(5b/6e/9d/8b)全部呈贴线形态。

---

## [TASK-10a] AI 行程规划后端服务:多轮对话编排 + PlannerSession/PlannerMessage 表 + 结构化行程解析(神朱 2026-10-01 拍板 M4.09)

- 状态: done
- 依赖: TASK-8b 之后(与 TASK-8 无耦合,神朱定「明晚/后晚做」)
- 目标: 新建 `backend/services/planner.py`:多轮对话编排(带 `HISTORY_LIMIT=12` 条历史)、系统提示硬约束(**只纳入用户点名/引用的收藏,未点名一律不得纳入**;**本阶段只排「每天的目的地」,不出交通/住宿/报价**;拿不到的写「待核实」禁编造;每天 2~4 点按顺路排序;只输出一个 JSON 对象)、`parse_itinerary` 容错解析(剥围栏/取首个 `{`…末个 `}`/字段校验,失败不抛)、LLM 预算 `PLANNER_MAX_TOKENS=1200 / PLANNER_TIMEOUT_S=180`(必须按调用放大)、降级四态 no_key/timeout/error/parse_error 一律不抛;新增表 `PlannerSession`/`PlannerMessage`。
- 只读清单: `backend/services/intro.py`(LLM Provider 抽象,唯一入口)、`backend/services/trips.py`、`backend/db/models.py`、`backend/app/api/trips.py`(风格样板)、`docs/TASK-10-CONTRACT.md`(§3「TASK-10a」逐字执行)。
- 涉及: backend/services/planner.py(新)、backend/db/models.py(加两表)、backend/db/repository.py(如需)、backend/test_planner.py(≥18 用例,LLM 全 mock 不触网)
- 验收: pytest 零回归 + ≥18 新增;`plan_turn` 返回键名逐字符合契约;降级四态各有专门用例(断言不抛且 `reply` 非空);`parse_itinerary` 对「带 ```json 围栏 / 前后有解释文字 / 非 JSON / days 缺字段」各有用例;历史超 12 条只送最近 12;prompt 里确实含「未点名不得纳入」与「本阶段不排交通住宿」两条硬约束(用例断言 prompt 文本)。
- 结果: **完成**(2026-10-02 夜班,Codex 执行,**~14.4min 自行完成实现+测试+commit,未触止损线**,commit `07058ba`)。
  - models.py 追加 PlannerSession(session_key unique+index/title/created_at/updated_at)与 PlannerMessage(session_id index/role/content/payload JSON nullable/created_at),两表不建外键、create_all 自动建、既有列零改动。
  - 新 `services/planner.py`:八常量逐字落地(1200/180/12/2000/3/15/2/4);plan_turn(取建会话→存 user→组 prompt→LLMClient.chat(system=SYSTEM_PROMPT, max_tokens=1200, timeout=180) 按调用放大→parse_itinerary→存 assistant(payload 带 itinerary/degraded/reason)→自 commit);返回键名逐字契约形状;SYSTEM_PROMPT 六条硬约束逐条写入且 prompt 尾部复述「未点名不得纳入」「不排交通/住宿/预算」;parse_itinerary 纯函数(剥围栏→首{末}→days 非空+每天有 day/stops,days_count 重算,失败 (None,前200字) 不抛);降级四态 no_key/timeout/error/parse_error 一律不抛、reply 中文兜底含原因码+可重试、消息照样落库;nights 阶段 A 不进 prompt(只存 payload 留给阶段 B);list_messages/clear_session(只删消息保留会话行)。
  - 测试 test_planner.py **47 例**(超 ≥18;含预算按调用传入断言、六条系统约束+两条 prompt 硬约束文本断言、summary 截 120 字、历史超 12 只送最近 12 且排除本轮、跨 session 可见证明 commit、降级四态参数化 6 剧本)。执行器复跑 pytest backend/ = **1196 passed**(1149 基线零回归,63s)。未动 index.html/API 路由(10b)/TripPlan/Collection。
  - **Codex 256K 统计**:单次调用 ~14.4min(16:09-16:24 UTC),function_calls **30**,首写 **4.3min**(R8 线内),tokens **1.19M total**(含 cache 重放);中途曾陷入 ruff 默认规则告警(仓库无 lint 配置),自行放弃未扩散。连续两条(8a2/10a)「拆小条目→远低于止损线」,与 6c/6d/6g/9b/9c/9d/8a1/8b 的贴线形态对比进一步印证拆条口径。

---

## [TASK-10b] AI 行程规划 API:POST/GET/DELETE /api/planner/messages + POST /api/planner/save(匹配入库并落 TripPlan)

- 状态: done
- 依赖: TASK-10a
- 目标: 新建 `backend/app/api/planner.py`(裸 JSON + 400 中文,风格照 trips.py):`POST /api/planner/messages`(`session_key` 空则后端 `uuid4().hex`;`collection_ids` 给了只用这些做素材、没给取当前全部收藏,但 **prompt 仍硬约束未点名不得纳入**;message 空/超长 400)、`GET /api/planner/messages`(未知 session 返回空 items 不 404)、`DELETE /api/planner/messages`、`POST /api/planner/save`(无 `collection_id` 的 stop 按名称在库内 Place 匹配→**幂等**建 `Collection(kind="place")`→调既有 `services.trips.upsert_trip_plan()`→返回 `{trip_plan, matched, unmatched}`,一个都匹配不到则 400 中文);`app/main.py` 挂 `/api`。
- 只读清单: `backend/app/api/trips.py`、`backend/app/api/collections.py`(收藏 upsert/ref_key 口径)、`backend/services/trips.py`、`backend/services/planner.py`(10a 产物)、`docs/TASK-10-CONTRACT.md`(§3「TASK-10b」逐字执行)。
- 涉及: backend/app/api/planner.py(新)、backend/app/main.py、backend/test_planner_api.py(≥18 用例,全 mock 不触网)
- 验收: pytest 零回归 + ≥18 新增;400 文案有用例;`collection_ids` 有无两条路径都有用例;save 的同名重复保存**幂等**有用例(不产生新 Place/Collection/TripPlan 行);`/api/trip-plans` 与 `/api/collections` **响应键逐键零变化**断言;save 返回的 quote 复用既有 `estimate` 口径与免责文案。
- 结果: **完成**(2026-10-03 夜班,Codex 执行,**~32min 自行完成实现+测试+真机冒烟+commit,未触止损线**,commit `3bc85e5`)。
  - 新 `app/api/planner.py`:POST /api/planner/messages(session_key 空→uuid4().hex;collection_ids 给了只取这几条素材、没给取全部收藏;message 空/超 2000 400 中文;返回 plan_turn 形状原样透出)、GET(limit 默认 50 上限 200,未知 session 空 items 不 404)、DELETE(调 clear_session)、POST /api/planner/save(无 collection_id 的 stop 库内 Place 名称匹配:精确同名优先→包含匹配→多命中取 id 最小;幂等建 Collection(kind=place、osm_key ref_key、坐标定点 7 位、mode 空串);调既有 trips.upsert_trip_plan;全不匹配 400「行程里的地点都不在库内…」);main.py 挂 /api。
  - `test_planner_api.py` **54 例**(超 ≥18;含降级四态、两条素材路径、save 幂等、/api/trip-plans 与 /api/collections 响应键逐键零变化)。执行器复跑 pytest backend/ = **1250 passed**(1196 基线零回归,73s)。index.html 未动。
  - 真机冒烟(uvicorn :8000 已重启):「3天 亲子 不要太累」**73s** 返回结构化 days(3天×3 stops,键名逐字契约)→ 同 session 追问「第二天换成古镇」**75s**,Day2 正确改朱家角古镇(多轮上下文生效,无 >180s/解析失败,不需调预算)→ save 匹配 place_id=30/unmatched 正确、同名重复保存幂等(plan id 不变、collection 不重复)→ 400 文案三处全对;冒烟数据(方案/收藏/会话消息)已清理,库回 3 条既有收藏。
  - **Codex 256K 统计**:单次调用 ~32min(14:04-14:36 UTC,止损线内),function_calls **72**,首写 **11.2min**(R8 线内),tokens **5.53M total**(含 cache 重放 5.32M)/output 58K+reasoning 28K,零 compact。注:第一次启动因 source .env 未加 set -a 秒退(9/28 同坑重踩,~2min 损失),重启即成功。

---

## [TASK-10c] AI 行程前端对话框:收藏面板第三个 tab「🤖 AI 行程」+ 多轮对话 + 点名收藏 + 行程卡片 + 存为方案

- 状态: done
- 依赖: TASK-10b
- 目标: `index.html` 收藏面板加第三个 tab「🤖 AI 行程」(沿用现有 tab 机制,与「我的收藏」「行程方案」并列互斥):消息流(用户/助手气泡,助手气泡渲染按天行程卡片 `Day N · base` + stops 名称/一句理由 + tip;`degraded` 显示可见原因,绝不静默)、输入区(Enter 发送/Shift+Enter 换行/发送中「AI 规划中…(首次约 1 分钟)」)、**点名收藏标签**(点一下把该条加入本次 `collection_ids` 并插入 @引用文本,**未点名的收藏绝不自动进请求**)、底部「⭐ 存为行程方案」(POST /api/planner/save,显示方案名+总预算估算+unmatched 提示)与「🗑 清空对话」、切 tab 时 GET 回放历史;`session_key` 存 localStorage。
- 只读清单: `backend/app/static/index.html`(收藏面板 tab 区块)、`backend/test_frontend_routes.py`、`docs/TASK-10-CONTRACT.md`(§3「TASK-10c」逐字执行)。
- 涉及: backend/app/static/index.html、backend/test_frontend_routes.py(≥12 静态断言 + node --check)
- 验收: pytest 零回归 + ≥12 新增断言;browser_exec 真机 QA(受信任点击):开页 0 error → 切 tab → 「3 天 亲子 不要太累」→ 出按天行程卡片 → 追问「第二天换成古镇」→ 卡片更新 → 点收藏标签再问 → 行程含该收藏 → 「⭐ 存为行程方案」→ 出现方案与预算 → 「🗑 清空对话」→ `window.__errs` 0 条;既有收藏/行程方案 tab 零回归。
- 结果: **完成**(2026-10-03 夜班,Codex 执行,**~31min 自行完成实现+测试+commit,未触止损线**,commit `6e448ab`)。
  - index.html(+524/-14,仅前端,后端 Python 零改动):收藏面板第三个 tab「🤖 AI 行程」(与我的收藏/行程方案并列互斥);消息流用户/助手气泡+按天行程卡片(`第N天 · base` + stops 名称/理由 + 💡tip + 「@点名收藏」徽标);degraded 显示可见原因绝不静默;textarea Enter 发送/Shift+Enter 换行、发送中按钮禁用+「AI 规划中…(首次约 1 分钟)」;点名收藏标签(#plannerTags,点一下加入 collection_ids+插入 @引用,「已点名 N 条」计数);「⭐ 存为行程方案」(POST /api/planner/save,显示方案名+总预算+unmatched 提示)/「🗑 清空对话」(DELETE);切 tab GET 回放;session_key 存 localStorage["w2g_planner_session"]。
  - test_frontend_routes.py +421 行(TASK-10c 段静态断言,超 ≥12 要求;含 node --check)。执行器复跑 pytest backend/ = **1268 passed**(1250 基线零回归,70s)。
  - browser_exec 真机 QA(uvicorn :8000):开页 0 error → 切「🤖 AI 行程」→ 点名标签渲染 3 条既有收藏 → 发「3天 亲子 不要太累」:发送中态正确(按钮禁用+文案)→ **首轮 LLM 超时 180s,degraded=timeout 气泡可见、给出重试指引、不编造行程(降级口径正确)** → 重试成功:3 天行程卡片(每天 2 stops+理由+tip)→ 点 2 个收藏标签(「已点名 2 条」+@引用插入输入框)→ 追问「第二天换成古镇,用点名的收藏」(184s):Day2 正确改古镇、点名收藏带「@点名收藏」徽标进行程 → 「⭐ 存为行程方案」:方案建成(¥0 估算·1晚·免责文案)+「纳入 3 个地点 · 3 个未在库内」提示 → 行程方案 tab 见新方案 → 「🗑 清空对话」回空态 → 回归:我的收藏 tab 3 条正常、地图 5 pin 正常;全程 window.__errs **0** 条。QA 数据已清理(方案删除、库回 3 条既有收藏)。
  - **Codex 256K 统计**:单次调用 ~31min(14:38-15:09 UTC,止损线内),function_calls **80**,首写 **9.8min**(R8 前端放宽线内),tokens **6.19M total**(含 cache 重放)/output 73K+reasoning 45K,零 compact。
  - 备注(报主会话):QA 实测单轮 LLM 73s~184s,**180s 超时线偏紧**(首轮 timeout 降级、重试 184s 贴线通过)——契约预留口径:若后续频繁超时,可考虑把 PLANNER_TIMEOUT_S 上调到 240,待神朱拍板,未擅自改。

---

## [TASK-11a] 全局 AI 助手后端:/api/assistant/messages(通用旅行问答,复用 Planner 会话表)

- 状态: done
- 背景: 神朱 2026-10-07 拍板:所有界面侧边加 AI 对话窗口,用户可描述、提问(定位=通用旅行助手,兼可触发现有行程规划)。本条只做后端;前端侧边抽屉属 TASK-11b。
- 目标: 新建 `backend/services/assistant.py`:`chat_turn(session_key, message, *, context=None)` —— 复用 `PlannerSession`/`PlannerMessage` 两表(**表结构零改动**,session_key 由调用方带 `chat-` 前缀区分行程会话);系统提示=通用旅行助手(答旅行问题/描述目的地/给实用建议;拿不到的事实明说、禁编造;中文回复,纯文本 ≤400 字);`context`(前端传当前城市/band/收藏摘要,截 500 字)拼入 prompt;LLM 预算 `ASSISTANT_MAX_TOKENS=800 / ASSISTANT_TIMEOUT_S=120` **按调用传给 llm.chat()**(勿沿用 intro 的 120/20s);降级四态 no_key/timeout/error/parse_error 一律不抛、reply 中文兜底含原因码。新建 `backend/app/api/assistant.py`:POST/GET/DELETE `/api/assistant/messages`,风格照 planner.py(裸 JSON、400 中文、未知 session 返回空 items 不 404、message 空/超 2000 字 400);main.py 挂 `/api`。
- 只读清单: `backend/services/planner.py`、`backend/app/api/planner.py`、`backend/services/intro.py`(LLMClient 唯一入口)、`backend/db/models.py`、`backend/test_planner_api.py`(mock 套路)。
- 涉及: backend/services/assistant.py(新)、backend/app/api/assistant.py(新)、backend/app/main.py、backend/test_assistant.py(新)
- 验收: pytest 零回归(基线 1268)+ ≥15 新增全 mock;预算按调用传入有用例断言;降级四态各有用例(不抛且 reply 非空);context 截断有用例;`/api/planner/*` 与 `/api/trip-plans` 响应键零变化断言;不动 index.html。
- 结果: **完成**(2026-10-07 夜班,Codex 执行,**~17.8min 自行完成实现+测试+commit,未触止损线**,commit `cc35888`)。
  - 新 `services/assistant.py`:四常量逐字落地(ASSISTANT_MAX_TOKENS=800/ASSISTANT_TIMEOUT_S=120 按调用传 llm.chat、ASSISTANT_HISTORY_LIMIT=12、CONTEXT_MAX_CHARS=500);chat_turn 返回精确五键 {session_key,reply,degraded,reason,message_id};复用 PlannerSession/PlannerMessage 两表零改动(list_messages/clear_session 直接复用 planner 同名函数,会话行保留);系统提示=通用旅行助手(中文纯文本≤400字、禁编造、排完整行程仍归「🤖 AI 行程」);降级四态 no_key/timeout/error/parse_error 不抛、reply 含原因码、消息照样落库;context 截 500 字拼 prompt、None/空白不拼。
  - 新 `app/api/assistant.py`:POST/GET/DELETE /api/assistant/messages(裸 JSON+400 中文+_optional_text+note,风格照 planner.py);session_key 空则补发 "chat-"+uuid4().hex,给了原样存取;main.py 挂 /api。
  - `test_assistant.py` 39 函数/**59 用例**(超 ≥15;含预算断言 800/120≠intro 默认、四态各例、context 截断、chat- 前缀、多轮历史、API 400 中文全谱、助手/行程会话同表互不污染)。执行器复跑 pytest backend/ = **1327 passed**(1268 基线零回归,78s)。index.html/db/models.py/planner.py 未动。
  - **Codex 256K 统计**:单次调用 ~17.8min(14:05-14:23 UTC,止损线内),function_calls **32**,首写 **7.1min**(R8 线内),tokens **1.46M total**(含 cache 重放 1.32M)/output 41K+reasoning 19K,零 compact。连续三条(8a2/10a/11a)小体量条目均远低于止损线,拆条口径持续有效。

---

## [TASK-11b] 全局 AI 助手前端:右侧边抽屉(💬 问答 / 🧳 行程 双模式)

- 状态: done
- 依赖: TASK-11a
- 目标: `index.html` 加**全局侧边抽屉**(右侧,可折叠,悬浮入口按钮,展开时不遮挡地图主操作区;与详情弹窗/收藏弹层/路线面板共存,Esc 层级排在最外层):消息流用户/助手气泡;双模式切换——💬 问答走 `/api/assistant/messages`(自动带 context:当前起点城市+选中 band+收藏条数摘要),🧳 行程走既有 `/api/planner/messages`(复用 10c 的按天行程卡片渲染/点名收藏标签/存为方案,可直接把 10c 的 UI 组件抽出复用,收藏面板里的「🤖 AI 行程」tab 保留不删);degraded 显示可见原因绝不静默;session_key 存 localStorage(与行程 session 分开);Enter 发送/Shift+Enter 换行/发送中禁用态。不新增前端 key。
- 只读清单: `backend/app/static/index.html`(收藏面板 tab 区块 + 10c 段)、`backend/test_frontend_routes.py`、`backend/app/api/assistant.py`(11a 产物)。
- 涉及: 仅 backend/app/static/index.html + backend/test_frontend_routes.py
- 验收: pytest 零回归 + ≥12 静态断言(抽屉 DOM/双模式接口字符串/context 拼接/Esc 层级/与 placeModal 共存)+ node --check;browser_exec 真机 QA(受信任点击):开页 0 error → 开抽屉问「上海周边有什么古镇」→ 出回答 → 切 🧳 模式发行程请求 → 出行程卡片 → 既有收藏面板「🤖 AI 行程」tab 零回归 → `window.__errs` 0 条。
- 结果: **完成**(2026-10-07 夜班,Codex 执行,**~32.7min 自行完成实现+测试+commit,未触止损线**,commit `a952552`)。
  - index.html(+486 行,仅前端,后端 Python 零改动):右侧可折叠抽屉 #assistDrawer + 悬浮入口 #assistFab(宽 ≤340px、z-index 1900=盖路线面板低于收藏弹层/详情弹窗,四者共存互不遮挡;展开时 body.assist-open 把路线面板左移让位);💬 问答走 /api/assistant/messages,context=起点城市+分段+分类筛选+收藏摘要 前端截 500 字并在 #assistCtx 回显,degraded 复用 PLANNER_REASON_TEXT 可见原因绝不静默,key 存 w2g_assistant_session;🧳 行程与收藏面板「🤖 AI 行程」tab 共用 state.planner/w2g_planner_session(镜像钩子同步、plannerInputBox() 按焦点认前台),点名收藏标签/按天行程卡片/存为方案全复用 10c,该 tab 原样保留;Enter 发送/Shift+Enter 换行/发送中禁用+进行中文案;Esc 层级 详情弹窗→收藏弹层→路线面板→抽屉(既有三层行为一字未改)。
  - test_frontend_routes.py +426 行(16 新用例:抽屉 DOM/层级/双模式接口字符串/context 拼接与 500 字上限/Esc 层级/placeModal 共存/degraded 可见/双 localStorage key/发送禁用态/10c 复用零回归/无前端 key + 1 例真跑 11a 端点契约(no_key 降级路径,零触网));node --check 通过;index.html 无 32 位 key 样态。执行器复跑 pytest backend/ = **1343 passed**(1327 基线零回归,78s)。
  - browser_exec 真机 QA(uvicorn 已重启):开页 0 error → 点 #assistFab 开抽屉、#assistCtx 回显「起点城市:上海;距离分段:50-100 km;收藏 8 条…」(97/500字)→ 发问答:首轮 **LLM 超时 120s → degraded=timeout 气泡可见**(原因码+重试指引+「没有编造答案」,降级口径正确)→ 重试成功出中文回答(朱家角,含「请出行前再核实」)→ 切 🧳 模式:点名标签渲染 8 条既有收藏 → 发「一天,只排上海耀雪冰雪世界」→ 按天行程卡片正确 → 收藏面板「🤖 AI 行程」tab 见同一消息(state 共用生效、零回归)→ Esc 层级实测:第一次 Esc 关收藏弹层、第二次才关抽屉 → localStorage 双 key(w2g_assistant_session 带 chat- 前缀 / w2g_planner_session 独立)→ 全程 window.__errs **0** 条;QA 会话消息已 DELETE 清理(deleted 2/4/2,会话行保留属契约口径)。
  - **Codex 256K 统计**:单次调用 ~32.7min(14:28-15:01 UTC,止损线内),function_calls **70**,首写 **12.2min**(R8 前端放宽线 20min 内),tokens **5.09M total**(含 cache 重放 4.86M)/output 72K+reasoning 45K,零 compact。
  - 备注(报主会话):问答首轮实测再次超时(token-plan 端点 120s 线),重试 ~110s 成功——与 10c 备注同源问题:LLM 端点高峰期延迟大,ASSISTANT_TIMEOUT_S=120 与 PLANNER_TIMEOUT_S=180 是否上调待神朱拍板,未擅自改。

---

## [TASK-12] 性能调查+优化:搜索位置/重新抓取慢(执行器手写,不派 Codex)

- 状态: done
- 背景: 神朱 2026-10-07 反馈:切高德后搜索位置和重新抓取仍慢(比 Overpass 时代好一些)。已知线索(夜班执行器写码前实测确认,勿直接采信):①`load_segment` 的 LLM 简介回填是**同步**的(INTRO_BATCH_LIMIT=40、workers=4,首屏冷抓要等最多 40 条 LLM,9d QA 实测上海 50-100 冷抓 ~125s 大头疑似在此);②amap 节流 0.6s 是**进程级**、`default_fetcher` 五组是**串行** for 循环,ring band 还要 × 格子数 × 翻页数,请求次数×(0.6s+RTT) 线性累积;③page_limit 可能发出拿不满的空页请求。
- 目标: 先**分段计时**(uvicorn 真机冷抓 0_50 与 ring band 各一次 + 「加载更多」扩抓一次:amap 网络请求次数/耗时、LLM intro 耗时、落库耗时,落一份 docs/PERF-NOTES.md);再按数据实施优化,候选(按计时结果取舍):a) intro 回填改**异步后台**(照 stays 6c 的 estimating 口径:先返回列表 intro=null,后台线程池补,前端已有「补简介」入口兜底);b) 首查 INTRO_BATCH_LIMIT 降到 15(=首屏显示数);c) default_fetcher 组间并行(节流锁天然串行化请求,收益=RTT 重叠,需测 QPS 10021 不触发);d) 消除空页请求。**渐进配额/page_size/offset/more 口径与 /api/places 响应形状一律不变,前端零改动**。
- 涉及: backend/services/place_loader.py、backend/services/intro.py、backend/data_sources/amap.py(如需)、docs/PERF-NOTES.md(新)、backend/test_*.py
- 验收: PERF-NOTES.md 有基线/优化后对照数字;优化后冷抓首屏墙钟较基线**降 ≥30%**(达不到须给数据说明原因);pytest 零回归(基线含 11a 增量)+ 新增用例覆盖异步 intro 不阻塞返回;真机冒烟:冷抓秒级回列表、intro 后台补齐可见;uvicorn 重启。
- 结果: **完成**(2026-10-07 夜班,**执行器手写**,commit `8bf3813`)。
  - 分段计时(tools/perf_probe.py 插桩,成都全新冷抓):线索①证实——**LLM intro 同步回填占墙钟 73%~98%**(40 条 ≈150~165s;amap 检索仅 2.5~52s);线索②次要(amap 耗时≈纯节流时间,RTT 已被节流窗吸收);线索③未证实(_paged 已有配额满/页不满即停)。数字全落 docs/PERF-NOTES.md。
  - 实施候选 a:`services/intro.py` 新增后台回填(InlineExecutor/set_backfill_executor/start_backfill/wait_backfill,照 stays 6c 口径;worker 自开 session(get_bind 拿 engine,测试 tmp 库隔离)、(city,band) inflight 去重、异常全吞降级);`place_loader.load_segment` 增 `sync_intros=False` 默认异步(入库 commit 后立即返回 intro_stats=None → 前端走「待补简介 N 条」既有分支,**/api/places 响应键零变化、前端零改动**);CLI 加 --sync-intros 保留同步口径;conftest autouse 注入 InlineExecutor → 既有 intro_stats 断言零改动。候选 b/c/d 按计时数据不做(b 无必要、c 收益<30%且有 QPS 10021 风险、d 未证实)。
  - 效果(武汉 vs 成都基线):0_50 冷抓 **173.4s→12.3s(-93%)**、50_100 ring **198.2s→50.2s(-75%)**、扩抓 round2 **168.1s→3.0s(-98%)**,远超 ≥30% 验收线。
  - 测试:test_intro_backfill.py 9 用例(真线程池:返回不阻塞/sync 旧契约/no-intros 零 LLM/inflight 去重/worker 吞异常/worker 自开 session/读库路径零回填)。pytest backend/ = **1352 passed**(1343 基线零回归,84s)。
  - 真机冒烟(uvicorn :8000 已重启):西安 0_50 全新冷抓 `GET /api/places?page_size=15` **3.3s** 秒回(intro_stats=None/intro_pending=29);~2.5min 后库内 intro 缺 29→**1**(后台自动补齐);二次查询 source=db 秒回。
  - 坑记录:SQLite 读事务 SHARED 锁挡后台 worker 写库 → start_backfill 提交前 session.rollback() 释放快照(已进 PERF-NOTES)。

---

## [TASK-13] 收藏目的地加详情链接:站内 #placeModal + 「在高德中查看」外链

- 状态: done
- 背景: 神朱 2026-10-07:我的收藏中的目的地应可点开具体介绍页面。拍板口径=站内详情弹窗(复用 TASK-8b 的 #placeModal:图片/详细介绍/类别要点,数据走自家 API)+ 另附「在高德中查看」外链按钮。
- 目标: ①后端 `GET /api/collections` items 增 `place_id` 字段(按收藏快照的 amap 身份/osm_key 在 Place 表匹配,匹配不到=None;**只加键不改既有键**,响应向后兼容);②前端收藏列表「📍 目的地」分组条目名称变可点链接 → 有 place_id 则打开既有 #placeModal(复用 openPlaceModal 同款入口,数据零新接口);无 place_id(库已清/种子)则链接置灰+title 说明,另给「在高德中查看」外链兜底(`https://uri.amap.com/search?keyword=<名称>&center=<lng>,<lat>`,target=_blank rel=noopener);③placeModal 动作行加「🔗 在高德中查看」外链按钮(所有入口通用)。
- 只读清单: `backend/app/api/collections.py`、`backend/db/repository.py`(ref_key 口径)、`backend/app/static/index.html`(renderFavItems + placeModal 段)、`backend/test_frontend_routes.py`、`backend/test_collections.py`。
- 涉及: backend/app/api/collections.py(加键)、backend/app/static/index.html、backend/test_collections.py、backend/test_frontend_routes.py
- 验收: pytest 零回归 + 后端 ≥4 用例(place_id 匹配/无匹配=None/既有键逐键零变化断言)+ 前端 ≥8 静态断言(链接 DOM/置灰态/高德外链 URL 拼接/placeModal 复用);browser QA:收藏一个库内目的地 → 列表点名称 → 详情弹窗出图/介绍/要点 → 「在高德中查看」URL 正确 → Esc 层级正常 → 0 JS error。
- 结果: **完成**(2026-10-07 夜班,**执行器手写**,commit `2a657e8`)。
  - 后端 `app/api/collections.py`:`GET /api/collections` items **只加 `place_id` 键**(`_match_place_id`:kind=place 且 ref_key 以 place: 开头 → OSM/amap 身份 `place:<type>/<id>` 优先、坐标定点串 7 位精确匹配兜底,同身份多行取 id 最小;路线/住宿/库外=None 不猜);既有键零变化。
  - 前端 index.html:收藏列表「📍 目的地」名称变可点链接(.js-fav-detail → `openFavPlaceModal` 用 place_id+收藏快照合成 place 开既有 #placeModal,零新接口;快照 detail_text 进详细介绍缓存、osm 身份透传保「★ 已收藏」判定同口径);place_id=None 置灰(fp-nm-dim+title 说明)+「🔗 在高德中查看」外链兜底(amapViewUrl:`https://uri.amap.com/search?keyword=<名称>&center=<lng>,<lat>`,target=_blank rel=noopener);placeModal 动作行统一加「🔗 在高德中查看」(所有入口通用);事件委托 preventDefault+stopPropagation。
  - 测试:test_collections.py +5 例(osm 身份匹配/坐标兜底/库外 None/路线 None/既有键逐键零变化);test_planner_api.py 2 处键集断言适配(PLACE_ID 只加键,并顺带断言 planner save 的收藏能匹配到 place_id);test_frontend_routes.py +6 静态断言(超 ≥8 要求合计,链接 DOM/弹窗复用/置灰态/外链 URL/委托/快照透传);node --check 通过。pytest backend/ = **1363 passed**(1352 基线零回归,86s)。
  - browser QA(CDP chrome 两次僵死重启后,media/highlights 请求已 stub 防图片加载拖死页面):库内收藏「上海耀雪冰雪世界」名称可点 → #placeModal 开(标题/★ 已收藏/动作行含「🔗 在高德中查看」,URL=uri.amap.com/search?keyword=… target=_blank rel=noopener;uvicorn 日志证实弹窗打开时 /api/places/media?place_ids=55 与 /api/places/highlights?place_ids=55 均 200)→ 库外收藏「大山」置灰+title 说明+兜底外链 URL 正确 → Esc 层级:先关弹窗再关收藏面板 → 全程 window.__errs **0** 条;未新增测试收藏(用库内既有 8 条验证),无需清理。
  - 备注:QA 中 browser daemon 两次被图片密集加载拖僵(1+1 都超时),kill daemon+chrome 重启并 stub loadPlaceMedia/loadPlaceHighlights 后恢复——重页面 QA 可先 stub 媒体加载再交互,记入经验。

---

## [TASK-14] 收藏/预订模块:每目的地行加 路线/住宿 外链(「取消收藏」旁)+ 预订模块按目的地分组

- 状态: done
- 依赖: TASK-13(同区域改动,串行避免冲突)
- 背景: 神朱 2026-10-07:前往预订模块应改为**每个目的地下边挂该目的地的路线和住宿外链**,且路线/住宿外链按钮放在收藏行「取消收藏」旁边。
- 目标: 仅前端。①「我的收藏」目的地行:「取消收藏」旁加「🚗 路线」「🛏️ 住宿」外链按钮——路线按该目的地坐标生成(有已收藏路线快照优先按其 mode 出链:铁路→12306、飞机→去哪儿、驾车→高德导航;无收藏路线则给高德导航+12306 双链),住宿→携程/Booking/Airbnb 按名称搜索(复用 renderBookSection 既有 URL 拼接函数,不新造);②「🎫 前往预订」区块重组为**按目的地分组**:目的地小标题(名称+「📄 详情」链接复用 13 的 placeModal 入口)→ 缩进子项=该目的地的路线外链行+住宿外链行(已收藏的带时长/费用/预估价快照),不再是平铺两张列表;无对应收藏的目的地给默认搜索链;免责声明口径不变(价格仅供参考 · 不代订 · 以官方/OTA 实时为准);③目的地↔路线/住宿收藏的归属关系用收藏快照里的坐标/osm 身份判定(前端判定,后端零改动;判不出归属的收藏落「未分组」小节,不丢条目)。
- 只读清单: `backend/app/static/index.html`(renderFavItems/renderBookSection/deleteFav 段)、`backend/test_frontend_routes.py`、`backend/app/api/collections.py`(summary 字段)。
- 涉及: 仅 backend/app/static/index.html + backend/test_frontend_routes.py
- 验收: pytest 零回归 + ≥10 静态断言(分组 DOM/外链 URL 拼接与既有渠道覆盖一致/取消收藏旁按钮位置/未分组兜底/免责文案保留)+ node --check;browser QA:备 目的地+路线+住宿 三条收藏 → 收藏列表目的地行见 🚗/🛏️ 外链且 URL 正确 → 前往预订区块按目的地分组、子项外链齐 → 取消收藏联动刷新 → 既有收藏/方案/AI tab 零回归 → 0 JS error;测试收藏清理。
- 结果: **完成**(2026-10-08 上午神朱点名后**执行器手写**,commits `627c9fb`+`aeb9be8`)。
  - ①目的地行动作排 `.fp-acts`:「取消收藏」旁并排路线外链(有已收藏路线→复用 bookLinksForRoute 按 mode 出链:驾车→高德导航/铁路→12306/飞机→去哪儿;无收藏路线→高德导航+12306 双链兜底,终点=目的地坐标)+ 住宿外链(携程/Booking/Airbnb 按 plainFavName 纯名搜索,完全复用既有 URL 拼接,不新造);全部 target=_blank rel=noopener。
  - ②「🎫 前往预订」重组为按目的地分组:`groupBookByPlace` 用收藏快照 `summary.parent.ref_key` 归属判定(与 favTree 同源)→ `.bk-grp` 小标题(📍 名称 + 「📄 详情」链接复用 TASK-13 的 openFavPlaceModal 开既有 #placeModal + 计数)→ 缩进子项 `bookRowHtml`(路线=🚗 名称+时长/费用快照,住宿=🛏️ 纯名+AI 预估价快照);判不出归属落「🗂️ 未分组」小节按类型平铺,一条不丢;免责声明口径原样保留。
  - ③**抓到并修复一个真 bug**:我初版 `plainFavName` 用字符类 `[📍🛏️🛏]` 剥前缀——JS 正则无 u 标志时字符类按 UTF-16 单元匹配,把 emoji 代理对拆开剥掉半个 → 孤立代理项 → `encodeURIComponent` 抛 `URI malformed`,**整个收藏面板渲染崩**。改为交替序列 `(?:📍|🛏️|🛏|🏨)` 修复,并新增 node 真跑回归用例(6 种前缀全过、无孤立代理);另修真机 QA 发现的住宿子项 emoji 双写(bookSubTitle 对住宿名先过 plainFavName)。
  - 测试:test_frontend_routes.py **+11 静态断言**(动作排位置/路线外链复用与兜底双链/住宿三渠道/分组 DOM/归属判定口径/未分组兜底/免责保留/📄详情复用 openFavPlaceModal/子项快照/新页打开/不新增前端 key)+ 代理项真 bug 回归(node 子进程真跑);node --check 通过;pytest backend/ = **1376 passed**(1365 基线零回归,71s)。
  - browser QA(真机,库内既有 11 条收藏,未污染数据):收藏列表 4 个目的地行动作排齐(取消收藏+路线/住宿外链,URL 正确、全部 _blank/noopener)→ 前往预订 4 组(亭林园/上海耀雪冰雪世界/松花湖滑雪场/大山),📍有 place_id 的 3 组带「📄 详情」→ 点开 #placeModal 正常、Esc 层级(先弹窗后面板)正常 → 子项快照真实显示(昆山柏轩 ¥220-380/皇冠假日 ¥720-1440/驾车 1小时16分 ¥59)→ 联动:POST 一条亭林园铁路路线收藏 → 刷新后亭林园组 2→3 行、收藏 11→12 → DELETE → 3→2 行、12→11,两处联动刷新全对 → 三 tab(收藏/行程方案/AI 行程)零回归 → 全程 window.__errs **0**;测试收藏(id=15)已删,库还原 11 条。

---

## [TASK-15] 外链挪到路线/住宿子行「取消收藏」旁 + OTA 搜索词加城市

- 状态: done
- 依赖: TASK-14(同区域改动,串行避免冲突)
- 背景: 神朱 2026-10-08 对 TASK-14 的两点返工意见:①外链按钮放错了行——应放在**路线子行和住宿子行各自的「取消收藏」旁边**(交通路线旁=高德+Google Maps 外链,住宿旁=OTA 外链),而不是挂在目的地父行上;②住宿 OTA 链接只拿酒店名去搜,OTA 基本搜错地方或搜不到——搜索词应带**城市+酒店名**(如「昆山 昆山柏轩酒店」)。
- 目标:
  ① **外链位置迁移**(收藏面板 + 前往预订区块两处一致):
     - 目的地父行 `.fp-acts`:移除 TASK-14 加的路线/住宿外链(placeActsHtml 只留「取消收藏」),前往预订分组小标题保留「📄 详情」。
     - 路线子行(favTree 缩进子行 + `.bk-row` 预订子行)的「取消收藏」旁:**高德导航外链 + Google Maps 外链**——用该路线收藏自身的 from/to 坐标拼:高德 `https://uri.amap.com/navigation?from=<lng>,<lat>,<from_name>&to=<lng>,<lat>,<to_name>&mode=<car|bus>`(mode 映射:driving→car,rail/flight→bus 兜底或仅给 to 点),Google `https://www.google.com/maps/dir/?api=1&origin=<lat>,<lng>&destination=<lat>,<lng>&travelmode=driving`(铁路/飞机映射 transit);无 from 坐标(库外收藏)则退化为目的地搜索链(高德 uri.amap.com/search + Google maps/search/?api=1&query=)。
     - 住宿子行(收藏树缩进住宿子行 + `.bk-row` 预订住宿行)的「取消收藏」旁:**OTA 外链(携程/Booking/Airbnb,渠道沿用既有 bookLinksForStay)**,但搜索词按 ② 改为「城市 + 酒店纯名」。
     - 注意:预订区块 `.bk-row` 目前行内没有「取消收藏」按钮,只有收藏树子行有——预订区块保持「行尾外链」布局即可,收藏树子行外链放 `.fp-del` 同一动作排(参照 TASK-14 `.fp-acts` 样式,子行复用)。
  ② **住宿 OTA 搜索词加城市**(神朱原话:「应该是加城市和住宿名称」):
     - 城市来源(三层,按序兜底):a) 新收藏:`/api/stays` 响应**只加一个 `city` 键**(用现成的 `services/stays.py:stay_city()`——高德行有 `cityname` tag,OSM 行有 `addr:city`,名称兜底;既有键零变化,口径同 TASK-13 的 place_id),前端收藏住宿时把 `city` 写进 `summary.city`(summary 是自由 JSON 快照,`_summary_of` 原样收,后端零迁移);b) 存量收藏(summary 无 city):后端 `GET /api/collections` 对住宿条目按 to_lat/to_lng 用既有逆地理降级链(place_loader 的高德 regeo,有 OriginCache 同款缓存思路可仿)一次性反查城市、写回 summary 持久化(懒补齐,失败就留空不阻塞响应);c) 都拿不到:退回纯酒店名搜索(现状,不比现在差)。
     - 搜索词拼法:携程 `https://so.ctrip.com/s?keyword=<city><酒店纯名>`(中文直接连写,如「昆山昆山柏轩酒店」或 `<city> <酒店纯名>`,以实际搜索效果为准,QA 时真开一次链接验证能搜到);Booking `https://www.booking.com/searchresults.zh-cn.html?ss=<city>+<酒店纯名>`;Airbnb `https://www.airbnb.com/s/<city>--<酒店纯名>/homes` 或 query 参数形式(以能出结果为准)。
  ③ 酒店纯名继续用 `plainFavName`(剥 🛏️ 前缀与「· 住宿」后缀;⚠️ TASK-14 踩坑:剥 emoji 必须用交替序列,禁字符类)。
- 只读清单: `backend/app/static/index.html`(placeActsHtml/favItemHtml/favKidsHtml/bookRowHtml/bookSubTitle/bookLinksForStay/bookLinksForRoute/renderBookSection 段)、`backend/services/stays.py`(stay_city/stay payload 组装处 L843 附近)、`backend/app/api/stays.py`、`backend/app/api/collections.py`(_summary_of)、`backend/services/place_loader.py`(逆地理降级链)、`backend/test_frontend_routes.py`、`backend/test_collections.py`、`backend/test_stays*.py`。
- 涉及: backend/app/api/stays.py(+city 键)、backend/services/stays.py(payload)、backend/app/api/collections.py(存量懒补 city,可挪 repository/service 层)、backend/app/static/index.html、backend/test_frontend_routes.py、backend/test_collections.py、backend/test_stays*.py
- 验收: pytest 零回归(当前基线 **1376 passed**)+ 后端用例≥5(/api/stays 带 city 且既有键零变化、city 缺失=None 不炸、存量收藏懒补 city 写回 summary、逆地理失败不阻塞)+ 前端静态断言≥8(父行不再有外链、路线子行含高德+Google 且 URL 拼接正确含 mode 映射、住宿子行含三 OTA 且搜索词含 city+纯名、无 city 退化为纯名、全部 _blank/noopener)+ node --check;browser QA:收藏面板路线子行取消收藏旁见高德/Google 外链且 URL 坐标正确 → 住宿子行 OTA 链接搜索词=「城市+酒店名」→ **真开一次携程链接确认能搜到那家酒店**(昆山柏轩酒店为验收样例,它的高德 cityname=苏州市/昆山,注意县级市口径,搜「昆山」比「苏州市」更准——stay_city 若返回地级市,QA 时看实际效果再定是否用名称里的城市优先)→ 前往预订区块同步生效 → 取消收藏联动刷新 → 三 tab 零回归 → 0 JS error;测试收藏清理。
- 结果: **完成**(2026-10-08 夜班,**执行器手写**,commit `747e5b8`)。
  - ① 外链迁移:目的地父行 `placeActsHtml` 只留「取消收藏」;新增 `childActsHtml` —— 收藏树路线/住宿子行的 `.fp-acts` 动作排=「取消收藏」+外链组(路线=`uri.amap.com/navigation` 高德导航(自身 from/to 坐标+名称,driving→car、rail/flight→bus)+ Google Maps dir(origin/destination=自身坐标,driving→driving、其余→transit);无 from 坐标退化 to 点导航;全库外无坐标退化目的地搜索链;住宿=OTA 三渠道);前往预订 `.bk-row` 保持行尾外链布局,与收藏树子行同一 URL 构造函数(bookLinksForRoute/bookLinksForStay),两处一致;TASK-14 的 `bookLinksForPlaceRoute/bookLinksForPlaceStay` 随父行外链下线(12306/去哪儿按契约「路线=高德+Google Maps」口径移除)。
  - ② 城市三层兜底:a) 后端 `/api/stays` items **只加一个 `city` 键**(用现成 `services/stays.stay_city()`,既有键零变化),前端收藏住宿时写进 `summary.city`(后端 `_summary_of` 原样收,零迁移);b) 存量收藏:`GET /api/collections` 里 `backfill_stay_cities()` 懒补齐——住宿指纹行(kid=place+住宿指纹)summary 缺 city 且有坐标 → `place_loader.reverse_with_fallback`(高德→Photon→Nominatim 降级链)反查城市(去尾「市」),**写回 summary 持久化**,进程内 TTL 缓存(成功 7 天/失败 1h,仿 OriginCache 口径),反查失败留空不阻塞响应(下次开面板再试);c) 都拿不到 → 前端退纯酒店名搜索。搜索词拼法:携程 `keyword=<city><纯名>`(连写)、Booking `ss=<city>+<纯名>`(空格)、Airbnb `/s/<city>--<纯名>/homes`。
  - ③ **抓到并修复一个真 bug(验收时发现)**:携程搜索域 `so.ctrip.com` **已死**(公共 DNS 223.5.5.5/1.12.12.12 均 NXDOMAIN,与容器网络无关;`www.ctrip.com` 正常)——即 TASK-4b 以来所有携程外链对真实用户都打不开。换成实测可用的 `https://hotels.ctrip.com/hotels/list?keyword=`(curl 200、页面内 keyword=「昆山昆山柏轩酒店」正确带入;headless 下被携程风控跳登录页,真浏览器无此问题)。
  - 验收样例(昆山柏轩酒店,县级市口径):其收藏行懒补齐反查得 **city=昆山**(比 stay_city 的 cityname=苏州市 更准,正是神朱提示的「搜昆山比苏州市准」);新收藏走 /api/stays city 键(stay_city 返回地级市「苏州」,契约指定用现成函数,未改其行为)——存量懒补齐路径恰好补上县级市精度。
  - 测试:后端 +8 用例(test_stays_api 3 例:city 键来源 cityname/缺失=None 不炸/名称兜底+形状断言只加 city 键;test_collections 5 例:懒补写回+持久化后不再反查/有 city 与非住宿行零触网/失败留空不阻塞+既有 summary 键零变化/缓存去重) ;前端 test_frontend_routes +5 新断言函数(childActsHtml 子行动作排/路线外链自身坐标+mode 映射+库外退化/住宿搜索词含 city+无 city 退化/summary.city 快照/addFavStay payload 带 city)+ TASK-14 旧断言按返工口径改写(父行不再有外链、渠道断言更新)。pytest backend/ = **1386 passed**(1376 基线零回归;首跑 1 例 test_intro_backfill 真线程池偶发超时,复跑 2 次均过,与本任务无关)。node --check 通过。
  - browser QA(真机,CDP chrome 又僵死按 skill 重启):收藏树 4 个父行动作排=仅「取消收藏」0 外链;7 条子行外链全对(路线=高德 navigation from/to 坐标+名称正确编码、Google dir origin/destination+travelmode 映射;住宿=携程/Booking/Airbnb 搜索词=城市+纯名,如「昆山昆山柏轩酒店(亭林园…)」);真开携程链接:hotels.ctrip.com 200 且 keyword 正确(风控跳登录页,见 ③);前往预订 4 组子行外链与收藏树一致、免责文案在、📄详情在;联动:POST QA 住宿收藏(id=15,city=昆山)→ fav 11→13、bookRows 7→8、子行三 OTA 链接搜索词含城市 → DELETE → 11/7 还原;三 tab(收藏/行程方案/AI 行程)零回归;Esc 关面板;全程 window.__errs **0** 条;测试收藏已清理(库回 11 条)。
  - uvicorn :8000 已重启(带 /opt/data/.env);真机 curl:/api/stays 昆山坐标 189 行全带 city=苏州;/api/collections 存量 3 条住宿收藏懒补齐 city=昆山/上海/吉林 并持久化。

---

## [TASK-16] 简介/详情/图片改「按显示批次备齐」:只把已显示的 15 条做全,不再整批全量回填(神朱 2026-10-09 拍板)

- 状态: done
- 背景: 神朱 2026-10-09 反馈:目的地生成简介还是「一批出来后全量生成所有介绍」,先显示的目的地反而没介绍、要等。拍板口径=**只把已经显示出来的目的地做全**(简介 intro/长介绍 detail/图片 media 都备好),延用「显示一批、预加载一批」,每批显示即触发该批备齐,不用等就能直接看。
- 现状(写码前已核实):intro=整段异步后台回填(start_backfill,最多 40 条/轮);detail=前端自动续批直到该段 pending=0(全量);media=打开弹窗才拉。→ LLM 花费烧在没显示的行上,而显示中的行反而排在后面。
- 目标(执行器手写,不派 Codex):
  ① 后端 `GET /api/places/intros` 加 `place_ids` 参数(逗号分隔,≤40,复用 details 端点的解析口径;`fill_missing_intros` 加 `only_ids` 透传 `repo.select_places(missing_intro=True, only_ids=…)`),既有键零变化;
  ② 前端:**删掉「全量自动续批」**——detail 续批上限改为「当前已显示行里还缺文本的条数」(pendingDetailIds 已按显示行取 id,只需把 pending 口径从后端全量 detail_pending 改为前端已显示行的缺口数);「补简介」按钮与每页落地(applyPlacesPage 后)自动触发**本页三件套预加载**:`intros?place_ids=本页缺简介的 id` + `details?place_ids=本页缺长介绍的 id`(自动续批只在本页范围内)+ `media?place_ids=本页 id`(batch ≤20 正好覆盖 15 条,读库优先、落 PlaceMedia 缓存,弹窗打开即秒出);
  ③ intro 后台全量回填(start_backfill)收窄:load_segment 异步回填只对**本页/本轮已入库且将被显示**的行没意义——保留后台回填但把 limit 收到 ≤ 页大小×2(30),不再整段 40+;「补简介」按钮改为只补当前显示行(带 place_ids);
  ④ 状态栏/列表副标题的「还剩 N 条待生成」口径同步改为「本页已显示行」的缺口数,不再报全库 pending。
- 涉及: backend/app/api/places.py(intros 端点)、backend/services/intro.py(fill_missing_intros 加 only_ids)、backend/services/place_loader.py(回填 limit)、backend/app/static/index.html、backend/test_*.py
- 验收: pytest 零回归(基线 1386)+ 新增用例(intros place_ids 过滤/≤40 上限/detail pending 口径);真机 QA:换城市冷抓 → 首屏 15 条骨架秒出 → ≤90s 内本页 15 条 intro+detail 全齐、点任意行弹窗图片秒出(缓存命中)→「加载更多」下一页同样自动备齐 → **库内未显示行的 intro/detail 不再被生成**(抽查 detail_pending 全库数不降);0 JS error。
- 结果: **完成**(2026-10-09 夜班,执行器手写,commit `6bf314c`)。
  - 后端:`GET /api/places/intros` 加 `place_ids`(逗号分隔、≤INTROS_IDS_MAX=40、解析口径同 details 端点;响应**只加** `items` 键=这些 id 的当前 intro 文本,既有键零变化);`repo.select_places` 加 `only_ids`(空列表=什么都不补,绝不退回全量);`intro.fill_missing_intros` 加 `only_ids` 透传;`INTRO_BATCH_LIMIT` 40→30(=页大小×2,后台回填只垫当前页与下一页的量)。
  - 前端:每页落地(loadPlaces 首屏 + loadMorePlaces 加载更多)自动 `prepPageBatch(rows)` 三件套——①`prepIntros` 本页缺简介行(≤40)调 intros?place_ids=,items 就地写回 state.places(不整页重拉);②长介绍沿用 loadDetails 分批(pendingDetailIds 本就只取已显示行);③`prepMedia` 本页 id(≤20=MEDIA_BATCH_MAX)预热 /api/places/media 落 PlaceMedia 缓存,弹窗打开秒出。**全量自动续批下线**:state.reco.pending 三处(applyPlacesPage/applyRecoData/applyDetails)全改为 `visibleDetailPending()`(已显示行缺口数),状态栏 introNote 同口径(「本页待补简介 N 条」);「补简介」按钮只补当前显示行(missingIntroIds+place_ids)。
  - 测试:新增 test_intros_page_batch.py **9 用例**(only_ids 过滤/空列表不退回全量/未点名行 0 LLM 调用/intros place_ids+items/既有键零变化 LEGACY_INTROS_KEYS/不带 place_ids 旧形状 items=None/超 40 上限 400/前端函数面与接线/pending 口径)+ test_place_modal_fetches 断言按预热口径改写(prepMedia 批量预热允许、loadPlaceMedia 逐条仍禁)。pytest backend/ = **1395 passed**(1386 基线零回归 + 9 新增,78s)。
  - 真机验证(uvicorn 重启,curl+browser QA):南京 0_50 冷抓 3.4s 秒回 15 条(intro 0 条)→ intros place_ids=15 个 id:55s 生成 **15/15**、items 全带文本 → details place_ids 10 条:47s 生成 **10/10** → media 15 id 预热 14.8s(9 amap/4 mixed/1 wikimedia/1 no_data、43 张图)→ 二次 media **cached=true 0.011s**(弹窗秒出) → browser:上海 0_50 全新段,首屏骨架秒出 → 自动续批只在已显示行内补(17/17 长介绍齐)→ 点行开弹窗图片 5 张(source=mixed)→「加载更多」新一页 17→32 条自动备齐(32/32 detail、intro 31/32)→ **未显示行不再被生成**(全库 detail_pending 从 26 只降到与显示行数吻合)→ 全程 window.__errs **0** 条。
  - 备注:后台 start_backfill(≤30 条)仍会在冷抓时垫一轮,与前端按页补齐同库缓存、天然幂等不冲突;LLM 高峰时单批 details 47-55s,intro+detail 双批并行时页面首屏不受影响(全异步)。

---

## [TASK-17] 住宿外链修复:城市传 OTA 原生城市字段,不再拼进搜索词(神朱 2026-10-09 拍板)

- 状态: done
- 依赖: 调研子任务产出 docs/research-ota-wechat-2026-10-09.md §A(三家 OTA 城市参数体系)
- 背景: 神朱 2026-10-09:TASK-15 把城市拼进搜索词(如「昆山昆山柏轩酒店」)实测多例 OTA 搜不到。应把城市传给 OTA 的**城市字段**(携程 city 代码 / Booking dest_id+dest_type / Airbnb slug),搜索词只留酒店纯名。
- 目标: 按调研结论改 `bookLinksForStay`(收藏树住宿子行 + 其余复用点):城市名→OTA 城市标识的映射(优先用调研确认的免维护拼法;必须建映射表时,表放前端 JS 常量或后端纯函数+单测,覆盖收藏里出现过的城市并给「映射不到→退回现状搜索词拼法」兜底,不丢链接);携程换回带 city 参数的列表页 URL;搜索词=酒店纯名(plainFavName,禁字符类剥 emoji)。
- 涉及: backend/app/static/index.html(bookLinksForStay 及调用点)、backend/test_frontend_routes.py(静态断言改口径)
- 验收: pytest 零回归;静态断言:三家 URL 含城市字段、搜索词不含城市前缀、映射不到时兜底旧拼法;browser QA:真开昆山柏轩酒店(样例)三家外链,携程/Booking 出正确酒店结果(截图 URL 佐证),0 JS error。
- 结果: **完成**(2026-10-09 夜班,执行器手写,commit `5ab0b0c`;按调研报告 §A 结论实施,**不维护静态城市映射表**)。
  - 后端:新 `data_sources/ctrip.py`(携程**公开免鉴权 suggest 接口** `m.ctrip.com/restapi/soa2/21881/json/gaHotelSearchEngine` 解析 hotelId/cityId/cityName;POST+0.5s 节流+直连代理口径;坏结构/HTTP 错/空结果一律 None 不抛;坐标消歧+城市名消歧兜底——实测 gdLat/gdLon 常为 0);新 `services/ota_links.py`(懒补齐 `backfill_stay_ctrip_ids`:写回 `summary.ctrip_hotel_id/ctrip_city_id/ctrip_city_name` 持久化、已有标识零网络、失败负缓存 1h、每请求限批 5 条;**两级关键词**:「城市+酒店名」失败退「纯酒店名」——实测「上海上海冰雪世界皇冠假日」重复前缀解析不到、纯名命中;plain_stay_name 剥 emoji 用交替序列);`GET /api/collections` 接线(city 补齐在前、ctrip 在后,不 or 短路;触网逻辑放 services 层——collections API 有「不 import data_sources」架构护栏,首版直接加在 API 被护栏用例抓住后迁移)。
  - 前端 bookLinksForStay 携程三级:①`ctrip_hotel_id` → **`hotels.ctrip.com/hotels/detail/?hotelId=`**(调研实测最可靠,免登录免日期直达详情页);②只有 `ctrip_city_id` → `hotels/list?cityId=&searchWord=&directSearch=1&old=1`(old=1 是命中开关,searchWord 只放纯名,城市走原生 cityId);③解析失败 → keyword 兜底(不丢链接,且名字已含城市前缀不再重复拼「吉林吉林…」);Booking `ss=酒店纯名,城市名`+人数参数(dest_id 需 aid 不能裸调,调研结论暂不建映射表);Airbnb `/s/<城市>--China/homes`(slug 宽容解析;境内房源已停,入口保留给境外场景)。
  - 测试:test_ctrip.py **22 用例**(parse 健壮性/坏形状/降级 None/坐标与城市名消歧/两级关键词/懒补齐写回+持久化后零网络/失败留空不阻塞/非住宿行跳过/限批 5+次轮补齐/纯名剥离);test_collections 加 autouse stub(防既有用例真打 m.ctrip.com);test_task15_stay_links 断言改三级口径。pytest backend/ = **1409 passed**(1387 基线零回归 + 22 新增,86s);node --check 通过。
  - 真机验证(uvicorn 重启):GET /api/collections 两轮——昆山柏轩 `hotelId=83790270`/`cityId=83`、上海冰雪世界皇冠假日 `121198627`/`2` 解析成功并持久化,吉林冬日光辉(万科松花湖店)解析不到(携程无此词条)走 keyword 兜底且不再重复城市前缀;curl 真开两个 detail 链接 **200 且标题=正确酒店名**(「昆山柏轩酒店(亭林园玉山广场地铁站店)预订价格…」);携程 list 兜底页 `cityId=83&searchWord=柏轩` SSR 命中「柏轩酒店(亭林园玉山广场地铁站店)」;browser QA:收藏树 3 条住宿子行外链 URL 全对、_blank/noopener 齐、0 JS error。**Booking headless QA 受限**:容器内 headless 对任何 ss(含纯「上海」)都显示 0 家——AWS WAF 对无头环境的限制(调研 §A2 已预警「真人浏览器可开、不要服务端预取校验」),拼法按调研口径落地,真人浏览器复核留白天主会话。
  - 备注(报主会话):①Airbnb 2022-07 起境内房源已停,境内住宿的 Airbnb 链接无实际可订结果——建议白天拍板是否对境内行隐藏该渠道;②Booking dest_id 若要消歧更准,可后续维护地级市映射表(autocomplete 需 aid,城市页可爬),当前 ss=「酒店名,城市名」已带城市语义。

---

## [TASK-18] 移除「🎫 前往预订」区块(与收藏树子行外链功能重叠,神朱 2026-10-09 拍板)

- 状态: done
- 目标: 仅前端。删除收藏弹层里的「🎫 前往预订」区块(renderBookSection/groupBookByPlace/bookRowHtml/bookSubTitle 及其 DOM、样式、调用点);**保留** bookLinksForRoute/bookLinksForStay(收藏树子行外链仍在用)与 TASK-13 的「📄 详情」入口;免责声明随区块一并移除(子行外链本身不带免责文案则无需补)。test_frontend_routes.py 里前往预订相关断言改删,收藏树子行外链断言保留。
- 涉及: 仅 backend/app/static/index.html + backend/test_frontend_routes.py
- 验收: pytest 零回归(删断言后总数下降属预期,注明);node --check;browser QA:收藏面板只剩「我的收藏/行程方案/AI 行程」内容、目的地/路线/住宿子行外链正常、无前往预订残留 DOM、0 JS error。
- 结果: **完成**(2026-10-09 夜班,执行器手写,commit `d49ade6`)。
  - index.html(-约130行,仅前端):favBookSection/favBookList DOM、bk-* 全套 CSS、renderBookSection/groupBookByPlace/bookRowHtml/bookSubTitle/bookPlaceGroupHtml/BOOK_DISCLAIMER/todayCST(12306 时代遗留,已无引用)、#favBookList .js-book-detail 事件委托分支一并下线;refreshFavItems 不再调 renderBookSection;**保留** bookLinksForRoute/bookLinksForStay/bookLinksHtml/splitRouteNames/cleanStayName/plainFavName/childActsHtml(收藏树子行外链在用)与 TASK-13「📄 详情」入口。
  - test_frontend_routes.py:TASK-4b 段 5 个测试改为 test_book_section_removed(12 个 token 全仓 0 残留断言)+ test_book_link_builders_kept_for_fav_tree;TASK-14 段 7 个区块专属测试删除/改写为子行外链断言(新页打开+noopener、无前端 key);PLAN_SECTION_END/book_section 切片标记同步改。pytest backend/ = **1387 passed**(1395−8 区块专属断言,符合预期;node --check 通过)。
  - browser QA:开收藏面板 → favBookSection/favBookList 均不存在、页面文本 0 处「前往预订」(仅 JS 注释)→ 收藏树 11 条目正常、子行外链 17 个照常渲染 → 全程 window.__errs **0** 条。

---

## [TASK-19] 境外目的地支持:境内/境外双轨方案调研(神朱 2026-10-10 拍板入队,**纯调研出方案,不写码**)

- 状态: done
- 背景(神朱 2026-10-10 三个反馈):
  ① 携程外链已好(TASK-17),但 **Booking / Airbnb 仍搜不到推荐的酒店**;
  ② 用中文名搜外国目的地**全部错误定位**(主会话 10/10 实测坐实:高德 geocode「瑞士因特拉肯」→成都双流某小区、「日本富良野」→香港北海街;境外 POI「皇后镇」「富士山」count=0——高德境外无数据,属数据源边界);
  ③ 神朱方向:**中国大陆境内 / 境外两套方案**——境内维持现状(高德),境外换 Google Maps 或 Azure Maps,并对 Booking/Airbnb 优化外链。
- 目标:产出 `docs/OVERSEAS-PLAN.md` 方案文档(含推荐路线与工作量拆解),供神朱拍板后再拆实施 TASK。**本条不改任何代码**。
- 调研清单(逐项给实测数据/引用):
  1. **境外地图+地理编码选型对比**(重点):
     - Google Maps JS API:大陆网络可达性实测(容器内 curl tiles/googleapis.com + 说明真实用户在墙内的后果)、计费(每月 $200 免费额度现状)、geocoding 中文地名支持;
     - Azure Maps:微软 2024-05 宣布退役的是 Bing Maps for Enterprise(2028-06 止),Azure Maps 是继任战略平台(非退役;Gen1 定价层 2026-09-15 退役→用 Gen2);大陆可达性实测、免费额度、中文地名 geocode 实测;神朱手里或有一把 Azure Maps key(仓库根 AzureMapsKey.txt 已删,历史见 docs/PUBLISH-PLAN.md 1.3);
     - **零成本备选**:OSM/Leaflet 底图(项目阶段1有现成经验)+ Photon/Nominatim 地理编码(已有降级链,Photon 可直连、境外 OSM 数据强)——实测 Photon 对「因特拉肯/皇后镇/富良野」中文与英文名命中率、返回坐标正确性;
     - 结论矩阵:key 成本/大陆可达性/中文地名命中/与现有 GCJ-02 坐标体系的兼容(境外无偏移问题,WGS-84 直接用)。
  2. **境内外判定开关设计**:起点 geocode 后按 country 字段/坐标落界判定(高德 geocode 境外会错误返回国内坐标→不能用高德判;应先用 Photon 或名称规则判「是否境外城市」);给出判定函数落点(geocode 服务层)与失败兜底。
  3. **Booking/Airbnb 外链真机复核与修正**(问题①):
     - 现状:`ss=酒店名,城市名`(Booking)、`/s/城市--China/homes`(Airbnb)。10/9 夜班 headless 被 AWS WAF 挡住未验成,神朱真人实测也搜不到;
     - 复核:真人浏览器(browser_exec)实测 2-3 家收藏酒店的 Booking 链接,确认失败形态(ss 里逗号语法?酒店名含分店括号?);对照调研报告 §A2 修正拼法(试 `ss=酒店名` + `dest_id/dest_type` 从城市页取、或纯城市 ss);
     - Airbnb:境内房源已停(2022-07)——**境内行隐藏 Airbnb 渠道、只在境外方案里启用**,给出前端判定改动点;
     - 境外场景:Booking 是主力(dest_id 体系覆盖全球),Airbnb slug 境外可用;携程境外是否应跳 trip.com(国际站)而非 hotels.ctrip.com,给实测结论。
  4. **双轨架构草案**:data_sources 层按「境内=amap / 境外=选型结果」路由(参考 ADR-006 降级链的既有形状);前端地图按 ui 配置切换(高德 JS API vs 选型结果);LLM 简介/推荐链路境外可用(候选来自境外检索)。工作量按 P 系列拆条格式给出(每条 ≤1 晚)。
- 涉及(只读):docs/research-ota-wechat-2026-10-09.md、backend/services/intro.py、backend/data_sources/(amap.py/photon.py/_common.py)、backend/app/static/index.html(bookLinksForStay)、docs/PUBLISH-PLAN.md
- 验收:docs/OVERSEAS-PLAN.md 落盘——含 ①选型对比矩阵(每项带实测数字或引用 URL)②境内外判定设计 ③Booking/Airbnb 复核结论与修正拼法(真人浏览器实测截图 URL/结果佐证)④双轨架构草案+拆条清单;pytest 基线不动(零代码改动);队列回填结果摘要。
- 结果: **完成**(2026-10-10 夜班,执行器手写调研,commit 见 git log;**零代码改动**,pytest 基线 1409 passed 复跑确认)。产出 `docs/OVERSEAS-PLAN.md`(五章)。
  - **①选型结论:境外走 OSM 系(Leaflet + osm.de/osm.fr 瓦片 + Nominatim/Photon + Overpass/OSRM),不采用 Google/Azure**——Google(maps.googleapis.com/khms/gstatic)与 Azure(atlas.microsoft.com)容器直连**双双不可达**(TCP 全超时 / DNS 无解析,DNS 污染证据已录),产品部署境内 NAS 是一票否决;OSM 镜像瓦片 tile.openstreetmap.de/.fr 与 basemaps.cartocdn.com **直连实测全通**(0.75~2s 真 PNG),Overpass 境外 POI(因特拉肯 30 条)、OSRM 境外驾车(19.7km/24min)直连可用;高德无 key 栅格境外 z≥10 全空白(179B)、卫星境外是占位假图(两地同 sha1)——高德只能服务境内坐实。
  - **②中文境外地名 geocode**:Nominatim(accept-language=zh-CN)**8/8 全中**(因特拉肯 0.3km/皇后镇 0.2km/巴黎 2.4km);Photon 中文 4/8 不稳(因特拉肯→香港、皇后镇→北京),但**英文名+国家 7/7 全中 ≤0.4km**;判定开关=geocoder country 字段(Nominatim country_code=CN / Photon country 含中国),**不能用高德判定**(「因特拉肯」status=1 错误吸附成都,当晚复现)。
  - **③Booking 复核(真机 headless)**:现状 `ss=酒店名,城市名` 与纯名/纯城市/英文名**全 0 家**;但先经 dest_id 搜索确认在架的 Pan Pacific Suzhou 用 ss= 同样 0 家,而 **dest_id=79514(苏州)/dest_id=-2552548(因特拉肯,当晚从城市页 HTML 实取)结构化参数正常出结果** → 结论:ss 文本路径对境内访客不可靠,修正=后端懒解析 dest_id 落 summary(照 TASK-17 携程 hotelId 模式)+ 前端优先 dest_id、兜底现状 ss。**Airbnb**:Kunshan--China 页 h1 虚标「Over 1,000 stays」但房源卡片 0 个,Interlaken--Switzerland 18 卡片正常 → 境内隐藏、境外启用(判定用 summary.region)。**携程境外**:suggest 对境外返回 type=IntlCity(因特拉肯 cityId=3167 瑞士)/Hotel(皇后镇诺富特 2196343),detail/list 页境外 200 标题正确,无需切 trip.com;现有 ctrip.py 只认 type=="Hotel",境外需纳入 IntlCity。
  - **④双轨架构草案 + 拆条**:region_gate(P1)→ 境外 POI 检索链(P2)→ 境外路线+住宿(P3)→ 前端 Leaflet 双轨地图(P4,组件从 9/30 前 git 历史考古复用)→ OTA 外链修正(**P5 可独立先行**,建议下晚先修);存储零改表(osm_type 天然隔离、OriginCache 加 region 列);坐标系境外 WGS-84/境内 GCJ-02 不混用。开放问题 5 项(Overpass 冷抓慢回归、LLM 翻译腿、P5 先行、Google/Azure 出局确认、境内第三渠道)列 §4.3 待神朱拍板。

---

## [TASK-20] 境外双轨 P1:region_gate 境内外判定服务(神朱 2026-10-10 拍板「使用推荐选项」,方案见 docs/OVERSEAS-PLAN.md §2/§4.2)

- 状态: done
- 目标: 新增 `services/region_gate.py`:起点解析后经 Photon/Nominatim 的 country 字段判定 domestic/overseas(Nominatim country_code=CN;Photon country 含「中国」);OriginCache 加 `region` 列(TTL 同 7 天);`/api/geocode` 响应**只加** `region` 键;双失败默认 domestic+note;前端加「境外模式」手动开关(localStorage 记忆,可覆盖自动判定)。
- 涉及: backend/services/region_gate.py(新)、backend/db/models.py(OriginCache.region)、backend/app/api/places.py、index.html(开关)、backend/test_region_gate.py(≥15 用例全 mock)
- 验收: pytest 零回归+≥15 新增;/api/geocode 既有键零变化;判定用例覆盖(中文境外名→overseas、境内→domestic、双失败→domestic 兜底、手动开关覆盖)。
- 结果: **完成**(2026-10-12 00:04 CST,commit `7148151`,按 `docs/TASK-20-CONTRACT.md` §1/§2 逐字落地)。
  - `services/region_gate.py`(150 行):classify_country_code(CN→domestic、其他非空→overseas、空/None→domestic 不猜)+ detect_region(**Nominatim 主→Photon 兜底**——Photon 正向会把中文境外地名错定国内,「因特拉肯」→香港 countrycode=CN,实测坑已避)+ detect_region_coords(Photon reverse 主→Nominatim 兜底);双失败一律 domestic/detector=none 绝不抛。
  - photon.py/nominatim.py 国家信息走 **opt-in** include_country 参数,默认输出逐键与既有断言一致;OriginCache 增列 region(ALTER TABLE 迁移);`/api/geocode` 与 `/reverse` **只多 region 一个键**(优先级 override>缓存>现场判定;override 不写共享缓存;detector=none 不写缓存下次重试;命中缓存零额外网络)。
  - 前端「境外模式」开关 #regionOverride(localStorage w2g_region_override,字面写法避开既有 .xxx() 断言口径)。
  - 测试 test_region_gate.py + test_frontend_routes.py 新增(≥15 要求);执行器复跑 pytest backend/ = **1467 passed**(1429 基线零回归,135s——region_gate 判定链沿用共享 client 节流,4 个测试文件 +11.5s 属契约备注预期)。
  - 执行器真机冒烟(uvicorn 已重启到 7148151):`/api/geocode?city=上海` → geocoder=amap/**region=domestic**;`/api/geocode?city=Interlaken` → geocoder=photon/**region=overseas**(46.6855,7.8585 正确落因特拉肯)。

---

## [TASK-21] 境外双轨 P2:境外 POI 检索链(Overpass 复活,依赖 TASK-20)

- 状态: done
- 目标: 新增 `data_sources/osm_overseas.py`(复活阶段1 Overpass 检索,四分类 SEARCH_GROUPS 口径从 git 历史 classify.py 考古);place_loader 按 region 分支(overseas → Overpass 环形/单圆 + 本地 haversine);入库身份 osm_type="osm_node/way/relation"、tags.source="OSM"、tags.coord_system="WGS-84";渐进配额口径(target_total/fetch_rounds)复用。
- 涉及: backend/data_sources/osm_overseas.py(新)、backend/services/place_loader.py、classify.py、backend/test_osm_overseas.py(≥20 用例全 mock)
- 验收: pytest 零回归+≥20 新增;境内链路(amap)零改动;二次查询读库零网络;真机冒烟一次境外城市(因特拉肯)冷抓。
- 结果: **完成**(2026-10-12 夜班,Codex 执行,commit `9892a52`,按 `docs/TASK-21-CONTRACT.md` 逐字落地)。真机境外冷抓冒烟首夜因 Overpass 网络面不可达未完成,**同晚 TASK-23 QA 期间网络恢复后已补验成功**(见下)。
  - `data_sources/osm_overseas.py`(1093 行):`git show 2845eb1^:backend/data_sources/overpass.py` 完整恢复,仅改模块 docstring(境外轨定位),函数面零改动;`_common.py` DEFAULT_SOURCE_PROXY 加 `"overpass": PROXY_OFF`;`data_sources/__init__.py` 导出。
  - `place_loader.py`(+219):`default_fetcher_osm`(low≤0 单圆 nearby_places_grouped / low>0 环形差集 nearby_places_ring,行形状=parse_places with_id,不含 typecode);`scale_search_groups` 加第二参(groups 给了对传入组缩放,公式与 FULL_SEARCH_BUDGET=540 不变);`to_place_items(region=overseas)` 走 classify_places OSM tag 归类 + place_identity 原生 (osm_type,osm_id) 不 crc32 + tags 加 coord_system=WGS-84(不写 source 键→place_source 派生「OSM」);`load_segment` 增 region 参(None→查 OriginCache 兜底不发网络;overseas→OSM 轨+水位 source="osm";fetcher= 显式注入优先);SegmentOutcome 增 region 字段;SOURCE_OSM/SOURCE_LABELS 常量。domestic 分支零改动。
  - `app/api/places.py`(+17):list_places 增可选 region 参(裸 None+手工校验,非法值 400 中文「region 只支持 domestic 或 overseas」),响应**只加** region 键(override>OriginCache>domestic),既有键逐键零变化。
  - index.html(+13/-2,契约 §1.4 最小改动):placesParams 在境外开关勾选时 `params.set("region","overseas")`、geocode URL 追加 regionQuery();状态栏 osm→「OSM 实时抓取(境外轨)」+境外 WGS-84 提示;地图层未动(切换属 TASK-23,过渡态 pin 偏移属预期)。
  - 测试:`test_osm_overseas.py` 975 行 + test_frontend_routes.py 追加(含 TASK-20 时代「/api/places 不受开关影响」断言按 TASK-21 口径改写 1 处,Codex 已在 summary 中主动申报)。执行器复跑 pytest backend/ = **1512 passed**(1467 基线零回归 + 45 新增,136s),超契约 ≥20 要求;node --check 在 test_frontend_routes 内跑过。
  - 真机冒烟(部分→**已补验**):uvicorn 已重启到 9892a52;`/api/geocode?city=Interlaken` → region=overseas 正确落缓存;首夜 `/api/places?origin=Interlaken&band=0_50&region=overseas` 冷抓 → **Overpass 三端点全挂**(直连+代理均 TCP 失败,当时返回中文降级报错、库内无残缺水位/无 Interlaken 行,失败语义正确);境内对照 `/api/places?origin=上海&band=0_50` → source=db region=domestic 秒回零影响。**同晚 TASK-23 QA 期间网络恢复后补验成功:Interlaken 0_50 冷抓 19 条 OSM 行入库(segment_fetch source="osm",fetch_rounds=2),列表显示 Jungfraubahnen/Trümmelbachfälle/Burgitor 等真实境外地名,LLM 简介按显示批次生成,契约 §3 境外冷抓冒烟闭环。**
  - **Codex 256K 统计**:单次调用 **44.7min**(超 40min 止损线 ~5min——kill 前已进入最终测试+commit 阶段,产物完整故按惯例收口未重跑),function_calls **123**,首轮写码启动后 **1.74min**(R8 12min 线内,历史最快),tokens 用量 **315,298**(total 12.0M 含 cache 重放),零 compact。体量:恢复 1093 行历史文件+975 行测试是耗时主因。

---

## [TASK-22] 境外双轨 P3:境外路线+住宿(OSRM 驾车复活 + OSM 住宿腿,依赖 TASK-21)

- 状态: done
- 目标: routes.py 驾车按 region 分支走 OSRM(router.project-osrm.org,git 历史恢复 osrm.py,几何抽稀口径复用;过路费境外=里程×区域费率简化,无 toll_distance 数据源);铁路/飞机估算引擎沿用;stays.py 加 OSM 住宿腿(tourism=hotel 等阶段1 STAY_TAGS)、城市反查用 Nominatim(zh-CN);估价规则表境外品牌沿用。
- 涉及: backend/data_sources/osrm.py(恢复)、backend/services/routes.py、stays.py、backend/test_routes_overseas.py/test_stays_overseas.py(≥15 用例)
- 验收: pytest 零回归+≥15 新增;境内路线/住宿口径零变化;真机冒烟境外一次(因特拉肯→格林德瓦驾车、周边住宿)。
- 结果: **完成**(2026-10-12 夜班,Codex 写码(42min 触 40min 止损线被 kill,**实现+测试已全部落盘**)+执行器复跑收口 commit,commit `8fda640`,按 `docs/TASK-22-CONTRACT.md` 逐字落地)。
  - `data_sources/osrm.py`(11.5KB):`git show 2845eb1^:backend/data_sources/osrm.py` 零改动恢复;`_common.py` 代理表加 `"osrm": PROXY_OFF`;`__init__.py` 补导出。
  - `routes.py`(+120):`osrm_leg`(OSRM 返回归一成 amap_leg 同形状,toll_distance_km 恒 None→费用自动退 heuristic)+ `default_router_osrm`;`driving_route` 增 region 参(overseas→OSRM 轨、source="osrm"、note 追加境外口径「境外费率按中国西部档估算,仅供参考」、router= 显式注入优先;domestic 现状零改动);`plan_routes` 增 region 透传(铁路/飞机估算腿不分轨,既有降级路径)。
  - `stays.py`(+333):`fetch_stay_pois` 增 region 分支(overseas→osm_overseas 单圆检索 tourism in STAY_TAGS,行归一现版形状,kind 用现成 stay_kind OSM 分支,身份 node/way/relation+原生 id 不 crc32);`stay_city` 境外走 Nominatim reverse(include_country/include_address,TASK-20 扩展的 opt-in)取 city||town||village、失败 None 不阻塞;半径阶梯/负缓存(键不含 region)/批量 LLM 估价/price_kind 规则表全共用现状代码。`nominatim.py` +56 行(include_address opt-in)。
  - API:`/api/routes` 与 `/api/stays` 各增可选 region 参(非法 400 中文)+响应**只加 region 键**,既有键逐键零变化。前端 index.html(+6/-0):loadRoutes/loadStays 在境外开关勾选时追加 region=overseas(§1.6 最小改动,住宿/路线卡片零改动)。**注:该前端口径当晚 TASK-23 QA 抓到缺口并已修(commit `ce18725`,改 `isOverseasTrack()`:开关或后端自动判定任一境外都带 region)——routes/stays 后端不做自动判定,只认开关会让「搜境外城市未勾开关」的驾车错走高德。**
  - 测试:`test_routes_overseas.py` 583 行 + `test_stays_overseas.py` 671 行(契约 §2 七组:osrm 恢复完整性/overseas 驾车分支+domestic 零变化/plan_routes 透传/API region 键/OSM 住宿腿+负缓存共用+stay_city/API stays region/前端接线)。执行器复跑 pytest backend/ = **1552 passed**(1512 基线零回归 + 40 新增,141s),超 ≥15 要求;node --check 通过。
  - 真机冒烟(uvicorn 已重启到 8fda640,执行器实测):①境外驾车 `/api/routes?...46.686,7.863→46.624,8.036&region=overseas` → **kind=real、source=osrm、19.7km/24min**(与契约 §0 实测数字吻合)、geometry 1161 点、cost 17 元(toll 4 heuristic + fuel 13)、note 含境外西部档口径、region=overseas、铁路腿未出(<100km 阈值正确);首两次请求 OSRM 公共实例 SSL 握手超时→服务层降级 unavailable(降级语义正确、不 500),第三次成功——公共 demo 实例波动,非代码问题。②境外住宿 `/api/stays?lat=46.686&lng=7.863&region=overseas` → **count=106、source=osm**(Backpackers Villa Sonnenhof hostel 约¥45-135/晚 rule、Du Nord hotel 约¥405-810/晚 rule、city=因特拉肯/Interlaken,kind 归一与规则估价全生效)。③境内对照零回归:/api/routes 上海→杭州 kind=real source=amap 172.3km、/api/stays 上海 197 家 source=db region=domestic。
  - **Codex 256K 统计**:总时长 **42min** 触 40min 止损线被 kill(kill 时实现+两测试文件已全部落盘,执行器复跑全绿后收口 commit),function_calls ~120,首轮写码启动后 **0.35min**(R8 12min 线内,历史最快),tokens 用量 ~12M total(含 cache 重放),零 compact。routes.py+stays.py 两个大文件(1400+1000 行)是本条贴线主因,与 6c/6d/6g/9b/9c 同形态;派单时已加「先实现后测试、全量 pytest 只跑一次」提醒,未见反复读大文件回圈。

---

## [TASK-23] 境外双轨 P4:前端双轨地图(Leaflet 境外轨,依赖 TASK-22)

- 状态: done
- 目标: index.html 按 region 分支:domestic → 高德 JS API 2.0(零改动);overseas → Leaflet(从 9/30 前 git 历史考古复用 pin/环圈/InfoWindow 组件),瓦片三线轮换 tile.openstreetmap.de 主/osm.fr 备/basemaps.cartocdn.com 兜底(attribution 必带);坐标系境外 WGS-84 直用;降级条款:三瓦片线全挂→提示「境外地图暂不可用」不白屏不空过。
- 涉及: 仅 backend/app/static/index.html + backend/test_frontend_routes.py(≥12 静态断言 + node --check)
- 验收: pytest 零回归+≥12 断言;境内高德链路零回归;browser QA:境外城市搜索→Leaflet 渲染→pin→弹窗→0 JS error。
- 结果: **完成**(2026-10-12 夜班,Codex 写码(42min 触 40min 止损线被 kill,产物完整)+执行器收口 commit + **QA 抓到 1 个真 bug 当场修**,commits `7697419`+`ce18725`,按 `docs/TASK-23-CONTRACT.md` 逐字落地)。
  - index.html(+382):`state.mapEngine`(amap|leaflet,缺省 amap)、双容器 #map/#mapLeaflet 互斥显隐(同一时刻只渲染一个引擎);Leaflet 1.9.4 **懒加载**(首次进境外才注入 unpkg CDN script+link,SRI 校验,加载失败状态栏提示不白屏);瓦片三线(osm.de 主 subdomains a/b/c → osm.fr 备 → basemaps.cartocdn.com 兜底,按 tileerror 错误率切换,attribution「© OpenStreetMap contributors」必带,maxZoom=18);渲染双轨(L.circle 外实内虚+起点圆/L.marker+L.divIcon 复用 pinHtml/L.popup 复用 popupHtml/L.polyline 驾车实线铁路虚线飞机弧线/fitBounds,[lat,lng] 顺序与高德相反已处理);**境外 WGS-84 直用零转换**(全文件无任何 GCJ 转换函数进境外路径);状态栏 OVERSEAS_COORD_HINT=「境外模式 · OSM/Leaflet · WGS-84」、TASK-21 过渡提示移除;切轨走 redrawMapTrack()(复用既有 drawRings/renderPinsByScope 入口,不另写流程)+window resize 派发让 Leaflet 重算容器;高德轨代码零删改(AMap.Marker/Circle/InfoWindow/setFitView 断言全在)。
  - test_frontend_routes.py(+454):TASK-23 十组断言(懒加载+SRI/瓦片三线+attribution/mapEngine 切换函数/L.* 调用面/无 key 样态/GCJ 不进境外路径/状态栏文案/高德轨零删改/node --check/引擎无关函数零改动)。执行器复跑 pytest backend/ = **1570 passed**(1552 基线零回归 + 17 净增 + QA 修正 1 例)。执行器收窄 1 处旧断言:dispatchEvent 全局禁用→pin 段禁用(引擎切换的 window resize 派发属合法用途,契约 §2.4 只禁「合成事件兜底点击」)。
  - **QA 抓到并修复 1 个真 bug(commit ce18725)**:TASK-22 的前端接线只在「手动勾选境外开关」时给 /api/routes、/api/stays 带 region=overseas,但契约 §3 QA②要求**搜索境外城市(未勾开关)** 也走 OSRM——而这两个端点后端不做自动判定(只认显式参数,无 OriginCache 兜底,与 /api/places 不同)→ 实测境外驾车错走高德、降级 unavailable。修法:新增 `isOverseasTrack()`(regionOverride || regionSeen==overseas)统一判定,routes/stays 两处透传改走它;/api/places 维持原口径(后端有自动判定兜底);测试同步改口径+新增 helper 双触发源断言。
  - browser QA(真机,CDP chrome 带代理重启后):①境内回归:上海 0_50/50_100 → engine=amap、30 pin、状态栏「境内外:境内」、0 error;②境外全流程:搜「Interlaken」→ region=overseas → **Leaflet 渲染**(瓦片 8-12 块全 loaded,src=tile.openstreetmap.de,attribution「Leaflet | © OpenStreetMap contributors」)→ 环圈 2-3 条 + 0_50 段 15 pin(divIcon 复用 .pin)→ 受信任真实点击 pin → L.popup(名称/分类/距起点/简介/收藏/路线/详情按钮全在)→「🚗 路线对比」→ **OSRM 驾车卡片 real 29min/30.3km/¥25(人均 ¥6)+note 含「境外费率按中国西部档估算」**+ Leaflet 折线 3 条 → 列表行点开 #placeModal(动作行「🚗路线/🛏️住宿」「🔗在高德中查看」齐)→ Esc 层级正确(先弹窗);③境外住宿腿:因特拉肯周边 **37 家 OSM 住宿 source=osm**(guest_house/hotel kind 归一、规则估价 约¥180-450/晚、city=因特拉肯;首查 0 家是修复前无-region 旧请求写入的 6h 负缓存残留,refresh 后即出,负缓存口径本身正确);④切回上海 → engine=amap、高德容器恢复显示、Leaflet 隐藏、0 error;全程 window.__errs **0** 条;QA 未新增收藏/测试数据,开关已还原未勾选态。
  - 环境备注(报主会话):**headless chrome 直连 unpkg/jsdelivr/cdnjs 全部 SSL 握手超时(-100),QA chrome 必须带 `--proxy-server=http://192.168.1.210:7892 --proxy-bypass-list='localhost,127.0.0.1,192.168.*'` 才能加载 Leaflet CDN**;tile.openstreetmap.de 直连正常(0.8s)。真实境内用户若同样到不了 unpkg,境外轨会走「地图组件加载失败」降级(列表/收藏不受影响)——建议白天评估 Leaflet 换 npmmirror(registry.npmmirror.com 直连实测 200)或自托管。
  - **Codex 256K 统计**:总时长 **42min** 触 40min 止损线被 kill(kill 时正在收尾测试断言,两文件产物已全部落盘),function_calls ~120,首轮写码启动后 **~19min**(R8 前端放宽线 20min 内、贴线;且写入用 python heredoc 而非 apply_patch,R8 检测模式注意别漏判),tokens ~12M total(含 cache 重放),零 compact。index.html 3200+ 行是本条耗时主因。

---

## [TASK-24] 境外双轨 P5:OTA 外链修正(Booking 坐标参数 + Airbnb 境内隐藏 + ctrip IntlCity;不依赖 P1~P4)

- 状态: done
- 目标: ①**Booking 外链加坐标参数**(实测口径见 docs/OVERSEAS-PLAN.md §4.4,dest_id 路线已实测放弃):bookLinksForStay 的 Booking URL 在既有 ss 基础上追加 `&latitude=<to_lat>&longitude=<to_lng>`(收藏行自带坐标;无坐标的库外收藏退回现状 ss 拼法不丢链接);②**Airbnb 境内隐藏**:domestic(现状全部收藏)渠道=携程+Booking 两家,region=overseas 才出 Airbnb(/s/<slug>/homes);region 判定过渡期用 summary.region(P1 落地后自动有值)缺省 domestic;③**ctrip.py 纳入 IntlCity**:parse_suggest 增 type=="IntlCity" 解析(city_id 落 summary.ctrip_city_id,境外酒店 detail 解析不到时 list 页 cityId 兜底),既有 Hotel 口径零变化。
- 只读清单: backend/app/static/index.html(bookLinksForStay 段 L3360~3403)、backend/data_sources/ctrip.py、backend/test_ctrip.py、backend/test_frontend_routes.py(TASK-15 段 L2683~2745)、docs/OVERSEAS-PLAN.md §3/§4.4。
- 涉及: 仅前端 index.html + backend/data_sources/ctrip.py + 两个测试文件;**不动** ota_links.py 懒补齐结构、不动 collections API。
- 验收: pytest 零回归(基线 1409)+≥10 新增(IntlCity 解析/既有 Hotel 零变化/Booking URL 含 latitude+longitude/无坐标退化/Airbnb domestic 分支不出/overseas 出);node --check;browser QA:收藏树住宿子行 Booking URL 带坐标、真开一次昆山柏轩 Booking 链接出结果列表(≥1 卡片)、Airbnb 按钮境内行消失、0 JS error;测试收藏清理。
- 结果: **完成**(2026-10-10 当晚神朱拍板「使用推荐选项今晚开始」后执行器手写,commit 见 git log)。
  - **实施前实测推翻 dest_id 懒解析方案**(调研 §3.1 原设想):①curl 直连城市页永远撞 cookie 墙(3 种 cookie 变体全「需您同意」);②autocomplete 裸调 3 个公开 aid 全空;③headless 批量采集不稳(20 城采到 14,西安/长沙抓到周边城市错误 id)。**替代=Booking URL 加 `latitude=&longitude=`**(坐标圈选实测 3/3 全命中:昆山 2156 家/上海 2179 家/因特拉肯 2874 家;收藏行自带 to_lat/to_lng,零映射表零后端),口径修正已落 docs/OVERSEAS-PLAN.md §4.4。
  - 前端 index.html(bookLinksForStay):有坐标 → Booking `ss=<酒店纯名>&latitude=&longitude=`;无坐标库外收藏退既有 `ss=名,城市` 拼法不丢链接;Airbnb 仅 `summary.region==="overseas"` 出(缺省 domestic 隐藏;slug 用 summary.country,P1/TASK-20 落地后自动有值);携程三级口径不变。
  - 后端:data_sources/ctrip.py 增 `parse_suggest_intl_cities`(只认 type=="IntlCity",既有 parse_suggest 零变化)+ `suggest_city`(searchType=D,同名多国取首条,失败 None 不抛);services/ota_links.py 增 `ctrip_city_for_stay`(TTL 缓存壳)+ backfill 接线(酒店级解析失败 → 城市级 IntlCity 兜底写 summary.ctrip_city_id/ctrip_city_fallback/country,已落行零网络;境内城市无 IntlCity 条目自然 None,境内口径零影响)。collections API 未动。
  - 测试:test_ctrip.py +12(IntlCity 解析/坏形状 6 参数化/Hotel 口径零变化/suggest_city 首条+searchType=D/降级 3 参数化/懒补齐城市兜底+持久化零网络/境内不触发/酒店优先)+ test_frontend_routes.py +4(坐标参数断言/无坐标退化/Airbnb region 分支/**node 真跑 bookLinksForStay 三形态 URL 逐字断言**,含真实 num() 实现)。pytest backend/ = **1429 passed**(1409 基线零回归 + 20 新增);node --check 通过。
  - browser QA(uvicorn 已重启,真机):收藏面板 3 条住宿子行 Booking URL 全带 latitude/longitude(真实收藏坐标)、Airbnb 0 个、携程 detail/list/keyword 三级正常 → **真开昆山柏轩 Booking 链接:h1「昆山柏轩酒店(亭林园玉山广场地铁站店):共2,153家住宿」+ 5 卡片正常渲染**(修复前同链接 0 家)→ window.__errs 0 条;未新增测试收藏(用库内既有 11 条验证),无需清理。
  - 备注:吉林冬日光辉(万科松花湖店)在携程 suggest 无词条、城市「吉林」也无 IntlCity → 走 keyword 兜底+Booking 坐标圈选,均不丢链接。真机复现了神朱问题①的修复闭环。

---

## [TASK-25] 境外模式下地理编码仍走高德 → 定位到国内同名 POI(神朱 2026-10-13 直报 bug)

- 状态: done
- 背景: 神朱 2026-10-13 两个反馈之一(另一个=顶栏输入框被挤成 22px,已由 commit `8e6895d` 当场修掉)。实测坐实:`/api/geocode?city=因特拉肯&region=overseas` 返回 **geocoder=amap、name=四川省成都市双流区因特拉肯(东北门)**——`region` 只是个标签,坐标仍走高德主链,而高德对境外中文名会 status=1 成功吸附国内同名 POI(皇后镇→安阳林州、巴黎→高雄、新加坡→长武县、吉隆坡→凯里、北海道→香港)。OriginCache 已积累 **13 行 amap 脏缓存**(含 `札幌上海`/`新西伯利亚上海` 这类被挤扁输入框拼接出来的行)。对照实测:Nominatim zh-CN 对同批 8 个境外中文名 **8/8 全中**(country_code + 坐标都对)。
- 目标: 境外解析链 = **Nominatim 主 → Photon 兜底**(不打高德),并加**国家码守卫**(某腿判出 CN = 错配,当失败降级);`region` 参数从 API 一路贯通到 place_loader 的解析函数;**自动判定为 overseas 时也走境外链**(不只 override 才生效);缓存加两道一致性闸(生效 region 与缓存不一致 / 缓存 geocoder=amap 但本次 overseas → 视为未命中并覆盖写回 = 脏缓存自愈,不写一次性脚本);逆向「我的位置」同理(境外 GPS 不问高德)。前端只改境外模式 label 的 title 文案。
- 契约: **逐字执行 `docs/TASK-25-CONTRACT.md`**(含 §0 四组实测数据、§1 精确签名、§2 ≥20 测试用例分组、§3 禁止项与真机冒烟步骤)——R9 已满足,勿再自行探索仓库。
- 涉及: backend/services/place_loader.py(新增 geocode_overseas + region 贯通)、backend/app/api/places.py(geocode_city/reverse_geocode)、backend/app/static/index.html(仅 label title)、backend/test_geocode_overseas.py(新)、backend/test_frontend_routes.py(追加)
- 验收: pytest 零回归(基线 **1572 passed**)+ ≥20 新增全 mock;`/api/geocode`、`/api/places`、`/api/routes`、`/api/stays` 响应键逐键零变化;境内(高德)链路零回归;脏缓存靠一致性闸自愈(不新增清库脚本);真机冒烟 5 步见契约 §3;顶栏 CSS 修复(`8e6895d`)不得回退。
- 结果: **完成**(代码由 2026-10-13 白天主会话派 Codex 落地,commit `0cf0762`;**夜班执行器本轮只做验收+真机冒烟**,未重复写码)。
  - 代码产物(Codex,6 文件 +1365/-83):`services/place_loader.py` 新增 `geocode_overseas`(Nominatim 主→Photon 兜底,每腿 `include_country=True` 一次拿坐标+国家码,`_require_overseas_code` 守卫判 CN/空即降级)、`photon_geocode_overseas`、境外逆向链 `reverse_overseas`(Photon 主→Nominatim 兜底);常量 `GEOCODER_CHAIN_OVERSEAS`/`REVERSE_CHAIN_OVERSEAS`(源名不含高德);region 贯通 geocode_with_fallback / default_geocoder / resolve_origin(_with_source) / reverse_with_fallback / default_reverse_geocoder / resolve_reverse_origin(_with_source)(默认 None = 现状行为,既有单测零改动);境外 WGS-84 直用不做 GCJ-02。`app/api/places.py`:生效 region(override > 缓存行 > 现场判定)真正驱动解析链;缓存命中加两道一致性闸 `_cache_row_is_consistent`(region 串轨 / amap×overseas)→ 脏行再搜即自愈覆盖写回,未写一次性清库脚本;`_region_write_back` 被一致性闸取代故移除;reverse_geocode 先 `detect_region_coords` 再透传 region(境外 GPS 不问高德,判定只调一次);响应键与形状逐键零变化,非法 region 仍 400 中文,境外两腿全败 400 且消息带两腿原因。前端 index.html **只改**「境外模式」label 的 title。测试:新增 `test_geocode_overseas.py` **47 例**(契约 §2 第 1~6 组)+ `test_frontend_routes.py` +4 例(第 7 组)+ `test_region_gate.py` 按新口径校准 5 处既有断言。
  - 验收数字(执行器本轮实跑):`cd backend && ../.venv/bin/python -m pytest -q` = **1623 passed**(1572 基线零回归 + 51 新增,121.98s);`amap × region=overseas` 缓存行 = **0**;顶栏 CSS 修复(`8e6895d`)与 regionQuery()/isOverseasTrack() 未回退(守卫断言在测)。
  - 真机冒烟(契约 §3 ①-④ 全过,uvicorn :8000 已在跑新代码):①`/api/geocode?city=因特拉肯&region=overseas` → **geocoder=nominatim、name=「因特拉肯, Interlaken-Oberhasli …, 瑞士」、lat/lng=46.6855231/7.8585139**(不再是成都双流 30.482/104.100);②不带 region 搜「皇后镇」→ 自动判定 overseas、nominatim、**-45.0321923/168.661 新西兰**(不再是安阳林州);③二次查因特拉肯 → **0.016s 命中缓存、坐标仍正确**(脏行已被覆盖自愈;另抽查「巴黎」photon 脏行→nominatim 法国 48.8535/2.3484、「吉隆坡」amap 脏行→nominatim 马来西亚 3.1517/101.6942,一致性闸均生效);④`/api/geocode?city=上海` → **geocoder=amap、region=domestic、31.230525/121.473667 不变**(境内零回归)。
  - browser_exec 真机 QA(§3 ⑤):勾「境外模式」→ 搜「因特拉肯」→ 地图切 **Leaflet 境外轨(amap-container 不可见)**、`/api/places?...&region=overseas` 200 且坐标为瑞士、境外冷抓入库 **30 条**(50-100km 段,来源「OSM 实时抓取 → 已入库」)、AI 推荐置顶 5 条(Verbier;Savoleyres 滑雪场 距起点 80.9km 等,均为瑞士真实 POI)、列表已显示 16 条、**window error 0 条**;`/api/geocode` 首次调用出现过一次 400(前端境外轨预热竞态),重试即 200,不影响渲染,已记录待观察。
  - **Codex 256K 窗口统计(rollout-2026-10-13T14-56-16)**:单次调用 **33.5min**(14:56-15:29 UTC,40min 止损线内)、function_calls **81**、首轮写码(apply_patch)启动后 **10.5min**(R8 12min 线内)、total_tokens **6,158,728**(其中 cached_input 5.90M / output 76,812 / reasoning 41,235),零 compact、零「读→忘→重读」回圈。
  - 备注:队列里该条 10/13 已由主会话派 Codex 并提交,夜班执行器按纪律先查产物目录+git log 发现已完成,**未双跑**,本轮转为验收+回填(10/5 阶段二同款处置)。

---

## [TASK-26] 境外模式搜境内城市:国家码守卫判 CN 时自动降级回境内链(神朱 2026-10-14 拍板)

- 状态: done
- 背景: TASK-25 落地后,勾着「境外模式」搜境内城市(如上海)→ 境外链国家码守卫判 CN 拒收 → 两腿全败 400「城市定位失败」(神朱 10/14 直报,夜班当场复现:`/api/geocode?city=上海&region=overseas` → Nominatim 答 CN 被守卫拒 + Photon 超时 → 400)。守卫本身按设计工作(防「巴黎→高雄」错配),但**对真境内城市不该报错,该降级**。
- 目标(神朱拍板口径):境外链任一腿拿到 **CN 正面证据**(DomesticMismatch)→ API 层**自动降级回境内高德链**重解析,响应 region=domestic、缓存写 domestic(下次不带 override 也命中零网络),并加 `note` 说明「已按境内解析,建议关掉境外模式」;两腿都是网络失败/空结果(无 CN 证据)仍 400 不变。境外城市(因特拉肯等)行为零变化。前端 searchCity 把 note 用 showNote 提示。
- 涉及: backend/services/place_loader.py(DomesticMismatchError + _require_overseas_code/geocode_overseas 改抛)、backend/app/api/places.py(geocode_city 降级分支 + note 键)、backend/app/static/index.html(searchCity 提示)、backend/test_geocode_overseas.py(追加第 8 组)、backend/test_frontend_routes.py(静态断言)
- 验收: pytest 零回归(基线 1623 passed,LEGACY 键断言按新增 note 键校准)+ ≥8 新增全 mock;真机冒烟:勾境外模式搜上海 → 200/amap/domestic/note 非空;搜因特拉肯 → 仍 nominatim/overseas;二次搜上海命中缓存零网络。
- 结果: **完成**(2026-10-15 夜班,执行器手写收口——上一轮夜班已把主体代码写到工作区但未提交未验收,本轮修完 2 个测试问题后全量验证落地)。
  - 实现口径(按神朱 10/14 拍板):`place_loader` 新增 `DomesticMismatchError`(DataSourceError 子类,既有捕获零变化);`_require_overseas_code` 判 CN 改抛它;**只有 Nominatim 主腿的 CN 算境内正面证据**(契约 TASK-25 §0 实测 zh-CN 8/8 全中),Photon 腿的 CN 仍只当错配失败(巴黎→高雄防线不松)。`geocode_overseas` 双失败时有 CN 证据抛 DomesticMismatch、无证据维持普通 400。API 层 `geocode_city` 捕获后自动降级回境内高德链:响应 `region=domestic`+`geocoder=amap`+`note`(「已按境内解析,建议关掉境外模式」),缓存写 domestic 行;降级行再被 overseas override 命中时直通缓存(零网络)。境内链也失败 → 400 报两条链原因。常规响应键集不变(note 只在降级时出现)。前端 `applyOrigin` 见 note+domestic → 关境外开关+showNote 提示。
  - 本轮修复:①`test_frontend_routes.py` 契约断言按新增 note 键校准(applyOrigin 白名单);②`test_api_geocode_downgrade_when_amap_also_fails_reports_both_chains` 原 stub 让 Nominatim 无条件答 CN,境内降级链末腿反而成功、400 触发不了——改为按 include_country 分流(境外腿答 CN 造证据、境内腿失败)。
  - pytest backend/ = **1631 passed**(1623 基线零回归 + 8 新增,全 mock;首跑曾有 1 例 intro_backfill 线程池偶发 flake,单跑与复跑全量均过,与本任务无关)。
  - 真机冒烟(uvicorn :8000 已带 /opt/data/.env 重启):①境外模式搜上海 → 200/amap/domestic/note 非空(0.14s,含降级重解析+写缓存);②因特拉肯 → 仍 nominatim/overseas(瑞士 46.686/7.859,行为零变化);③二次搜上海(带 overseas override)→ 0.014s 缓存命中、仍带 note;④不带 override 常规响应无 note 键(LEGACY 五键不变)。
  - 备注:test_frontend_routes.py 的 applyOrigin 契约断言是前后端字段一致性闸,以后 geocode 响应加键都要同步校准这处白名单。

---

## [TASK-P1] 容器化基础文件:Dockerfile + compose + .env.example + entrypoint(神朱 2026-10-15 拍板「都按推荐」)

- 状态: done
- 目标: 按 docs/CONTAINER-SETUP-PLAN.md 第四节 P1 执行(执行器手写,≤30min 小任务):仓库根新增 `Dockerfile`(python:3.13-slim、非 root uid 1000、VOLUME /data、HEALTHCHECK /api/places/meta、ENV WHERE2GO_DB_URL=sqlite:////data/where2go.db WHERE2GO_UI_MODE=release)、`docker-compose.yml`(8000 端口、./data:/data、env_file .env、restart unless-stopped)、`.env.example`(全部 WHERE2GO_* 带中文注释+默认值)、`docker-entrypoint.sh`(建 /data、权限检测中文提示、exec uvicorn --workers 1)、`.dockerignore`(排 .git/.venv/backend/data/docs/tools/内部 txt/*.env)。
- 涉及: 仓库根新文件(零 Python 改动)
- 验收: compose YAML 语法过(python yaml.safe_load)、entrypoint `bash -n` 过、.dockerignore 覆盖 .env 与 backend/data;pytest 基线零回归(1632);commit。
- 结果: **完成**(2026-10-15 夜班,执行器手写;commit 见 git log)。
  - 5 个新文件:`Dockerfile`(slim+非 root uid 1000+VOLUME /data+HEALTHCHECK urllib 探 /api/places/meta+先装依赖后拷代码的缓存分层)、`docker-compose.yml`(GHCR latest 镜像、./data:/data、env_file `required:false`(compose≥2.24,注释给了老版降级写法)、`user: "1000:1000"` 备用行注释)、`docker-entrypoint.sh`(/data 可写检测→中文 chown 提示+`user:` 指引→exec uvicorn --workers 1)、`.env.example`(3 必填 key+5 LLM+17 可选项,全部带中文注释与实测默认值)、`.dockerignore`(排 .git/.venv/backend/data/docs/tools/tasks/内部 txt/测试文件,`!backend/requirements.txt`+`!.env.example` 例外)。
  - 附带:`.gitignore` 加 `!.env.example` 例外(原 `.env*` 会把样例一起挡掉)。
  - 验证:entrypoint `bash -n` 过;compose 用 pyyaml safe_load 解析过(image/ports/volumes/env_file/restart 五字段核对);`git check-ignore` 确认 5 文件均可提交;pytest backend/ = **1632 passed**(零 Python 改动,基线零回归)。
  - 已知限制:本容器无 docker daemon,`docker build` 无法本地验证 → 镜像层留给 P4 的 GitHub Actions 首跑验证(见 CONTAINER-SETUP-PLAN 第三节验证策略)。
  - 坑记录:write_file 会把形似密钥的字串(如 `WHERE2GO_AMAP_SECURITY_JS_CODE=`、`DASHSCOPE_API_KEY=`)掩成 `…`/`***` —— 写完 .env 类文件必须 grep 扫残留,已用 python 脚本复原。

---

## [TASK-P2] AppConfig 表 + 配置读写 API(/api/config + /api/health)

- 状态: pending
- 目标: 按 CONTAINER-SETUP-PLAN 第二节「配置注入策略」:AppConfig 表(key/value/source/updated_at,唯一键 key)+ `services/app_config.py`(白名单 SCHEMA 7 键、load_into_environ 启动注入「env 未设才填」、set_key 写表+同步 os.environ、mask_value 掩码)+ `GET /api/config`(只回掩码+label/hint/required/needs_restart)、`POST /api/config`(白名单外键 403/400)、`GET /api/health`(必填缺失清单,给 compose 与向导用)+ main.py lifespan 启动注入。
- 依赖: P1(非硬依赖,可并行;建议 P1 先行)。
- 涉及: backend/db/models.py、新 backend/services/app_config.py、新 backend/app/api/config.py、backend/app/main.py、新 backend/test_app_config.py
- 验收: pytest +≥25 全 mock(掩码不漏明文、白名单外键拒写、env 压过 AppConfig、set_key 后 amap.resolve_key/intro.resolve_provider 立即看到新值、/api/config 响应 grep 不到明文 key);基线 1632 零回归;不动那 30 处 env 读取点;commit。
- 结果: (待回填)

---

## [TASK-P3] /setup 用户向导页(4 步 + 真调校验 + 首页横幅)

- 状态: pending
- 目标: 按 CONTAINER-SETUP-PLAN P3:新 `app/static/setup.html`(不塞 index.html)4 步向导——①高德 Web服务 key(图文教程+后端真调一次 geocode("北京") 校验,infocode 中文解释);②高德 JS key+安全密钥(写明两 key 平台隔离/USERKEY_PLAT_NOMATCH/INVALID_USER_SCODE 血泪坑文案);③LLM 可选(预置 DeepSeek/阿里/OpenAI 下拉+自定义 base_url+model+key,发 1-token 测试消息);④完成(保存→POST /api/config→回首页)。校验端点只打一次请求防 QPS 10021。index.html 顶部横幅:GET /api/health 缺必填 → 引导条(不强制重定向,决策③)。
- 依赖: P2。
- 涉及: 新 backend/app/static/setup.html、backend/app/api/config.py(校验端点)、backend/app/static/index.html(横幅)、backend/test_frontend_routes.py(静态断言)
- 验收: browser QA 全流程(无 key 起服务→横幅→向导 4 步→假 key 看报错文案→真 key 通过→回首页出图)0 JS error;pytest 静态断言+基线零回归;commit。
- 结果: (待回填)

---

## [TASK-P5] README(中英双语)+ 第三方归属/免责 + 内部文件说明

- 状态: pending
- 目标: 仓库根 README.md:一句话介绍、quickstart(compose 三命令)、key 申请教程(高德控制台图文链接)、WHERE2GO_* 全表(与代码 grep 核一致)、FAQ(10021 QPS/INVALID_USER_SCODE/USERKEY_PLAT_NOMATCH/免费配额)、AGPL 与源码获取说明(决策⑤)、免责(高德条款/LLM 服务商/价格估算不代订)、顶部注明「docs/ 与根目录 md 是开发过程记录,用户只需看 README」(决策①,内部文件保留不动)。
- 依赖: P1(命令要抄 compose);P2/P3 完成后核健康检查与向导入口描述。
- 涉及: README.md(仓库根,现无)
- 验收: 命令可复制即跑;env 表与 grep 结果一致;commit。
- 结果: (待回填)

---

## [TASK-P4] GitHub Actions:CI + GHCR Release 流水线(multi-arch)

- 状态: pending
- 目标: `.github/workflows/ci.yml`(push/PR 跑 pytest,全 mock 零 secrets)+ `release.yml`(tag v* → buildx amd64+arm64(决策②)→ push ghcr.io/jackson-chu-sys/where2go:{tag,latest} + 容器冒烟 curl /api/places/meta)。注意:本容器无 docker daemon,镜像层只能靠 Actions 验证(CONTAINER-SETUP-PLAN 第三节);push 需神朱授权(仓库 44+ commit 未推)。
- 依赖: P1;发布需 P2/P3(否则镜像无向导)。
- 涉及: .github/workflows/
- 验收: Actions 首跑绿;GHCR 出 multi-arch 镜像;NAS docker pull + compose up 冒烟(神朱配合)。
- 结果: (待回填)

---

## [TASK-P6] 发布版 UI 精简(WHERE2GO_UI_MODE=release)

- 状态: pending
- 目标: 按 PUBLISH-PLAN 第三节:ui_mode 从 /api/places/meta 透出,release 隐藏状态栏技术细节/AI 副标模型名/长介绍批次消息/POI id/坐标系/补简介按钮;「估算」徽标保留(合规);dev 模式一切照旧。
- 依赖: P3(横幅与向导落地后再动 index.html,避免双改)。
- 涉及: backend/app/static/index.html、backend/app/api/places.py(meta 透出)、backend/test_frontend_routes.py
- 验收: release/dev 双模 browser QA 对照 PUBLISH-PLAN §3.1 表逐条核;pytest 静态断言;commit。
- 结果: (待回填)

---

## 追加模板(新任务复制此段)
## [TASK-xxx] 标题
- 状态: 示例(勿执行;新任务复制本段并改成 pending)
- 目标:
- 涉及:
- 验收:
- 结果: (待夜班回填)
