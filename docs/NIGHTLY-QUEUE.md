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

- 状态: pending
- 目标: 新建 `backend/data_sources/amap.py`(v3 REST:正向/逆地理编码、周边搜索、多边形搜索、驾车;归一化 POI 形状;`status/infocode` 错误翻译;0.4s 节流;直连代理口径)+ `backend/services/amap_categories.py`(四分类检索组 `AMAP_TYPE_GROUPS` + `classify_amap_poi` 优先级 滑雪>运动>人文美食>自然 + `dedupe_key=("amap", <高德POI id>)`)+ `_common.py` 加 `"amap": PROXY_OFF`。含分格纯函数 `grid_polygons` 与 `decode_polyline`。
- 只读清单: `backend/data_sources/_common.py`、`backend/data_sources/photon.py`(写法样板)、`backend/db/models.py`、`backend/db/repository.py`、`docs/TASK-9-CONTRACT.md`(§1 实测数据 + §3「TASK-9a」逐字执行)。
- 涉及: backend/data_sources/amap.py(新)、backend/services/amap_categories.py(新)、backend/data_sources/_common.py(加源)、backend/test_amap.py(新 ≥25 用例,全 mock 不触网)、backend/test_amap_categories.py
- 验收: pytest 基线 792 零回归 + ≥25 新增;`status!="1"` 按 infocode 分派 `DataSourceError`/`TransientDataSourceError`(10021→Transient)有用例;缺 key 报中文错不崩;归一化 POI 键名与契约逐字一致;`grid_polygons` 格子数/坐标顺序有用例;节流有用例(两次调用间隔 ≥0.4s,monkeypatch 时钟)。
- 结果: (待夜班回填)

---

## [TASK-9b] POI/住宿检索链切高德:入库身份改 amap + 分格多边形 + 渐进口径不变 + 清库脚本
- 依赖: TASK-9a(amap.py 与 amap_categories 已建)

- 状态: pending
- 目标: `services/classify.py` 检索组换 `AMAP_TYPE_GROUPS`(优先级/去重/预算语义保留);`services/place_loader.py` 的 `default_fetcher` 换高德:`low==0` → `search_around(radius=high)`,`low>0` → 包围盒 `grid_polygons` 分格 + `search_polygon` + 本地 haversine 收敛 `[low,high)`,扩格随 `fetch_rounds` 递增(口径同 `progressive_target_total`);入库 `osm_type="amap"`/`osm_id=<高德 id>`(表结构零改动)、`tags.source="高德"`、`place_source()` 加「高德」分支;`stays.py::search_stays` 换 `search_around(types="100000")`(半径阶梯/负缓存/估价口径全不变);新增 `tools/amap_cutover.py`(默认 --dry-run 报数,--apply 清派生行,**保留 Collection/CollectionCat/TripPlan**)。
- 只读清单: `backend/services/place_loader.py`、`backend/services/classify.py`、`backend/services/stays.py`、`backend/db/repository.py`、`docs/TASK-9-CONTRACT.md`(§3「TASK-9b」逐字执行)。
- 涉及: backend/services/(classify.py、place_loader.py、stays.py、amap_categories 已建)、backend/db/repository.py(place_source)、backend/app/api/(仅在不得已时改动,**响应键名必须零变化**)、tools/amap_cutover.py(新)、backend/test_places*.py/test_stays*.py/test_classify.py(改替身为 amap + 新增 ≥30 用例)
- 验收: pytest 零回归 + ≥30 新增;**`/api/places`、`/api/places/meta`、`/api/places/intros`、`/api/stays` 响应键名/形状逐键断言零变化**(前端零改动的证据);同 (城市,band) 二次查询读库零网络;`page_size/offset/more` 行为与旧口径一致;清库脚本 `--dry-run`/`--apply` 各有用例且不删用户数据。
- 结果: (待夜班回填)

---

## [TASK-9c] 地理编码切高德主链路 + 驾车切高德 + 物理删除 overpass.py/osrm.py
- 依赖: TASK-9a(amap.geocode/driving);与 TASK-9b 无强耦合,可并行/续做

- 状态: pending
- 目标: `resolve_origin_with_source` 主链路改 `amap.geocode`(`geocoder="amap"`),失败/无 key 回落 photon→nominatim(保留不删),`/api/geocode` 响应键与 `resolved` 语义不变;`OriginCache` **保留复用**(geocoder 存 amap,TTL 7 天不变;TASK-7a 的 Overpass 并行部分随 overpass.py 移除);`services/routes.py` 驾车改 `amap.driving`(真实 distance/duration,`kind="real"` 不变;**过路费 = `toll_distance_m` × 区域费率**,`cost_breakdown.mode="amap_toll_distance"`;油费/铁路/飞机估算/600km 阈值/机票公布价/人均/deep-link 全不变);geometry 用解码 polyline(抽稀口径不变);**物理删除(`git rm`)`data_sources/overpass.py`、`data_sources/osrm.py` 及其在测试中的直接引用**(神朱 10/01 二次确认:不保留休眠文件)。
- 只读清单: `backend/services/place_loader.py`、`backend/services/routes.py`、`backend/app/api/routes.py`、`backend/db/repository.py`、`docs/TASK-9-CONTRACT.md`(§1.5/§1.6 + §3「TASK-9c」逐字执行)。
- 涉及: backend/services/(place_loader.py、routes.py)、backend/app/api/(geocode/routes 透出)、backend/data_sources/(删 overpass.py、osrm.py)、backend/test_routes*.py/test_photon.py/test_data_sources.py(引用改 amap 替身 + 新增 ≥20 用例)
- 验收: pytest 零回归 + ≥20 新增;`/api/geocode` 键名不变且 `geocoder=amap` 命中;OriginCache 命中零网络有用例;驾车 `toll_cny` 来自 `toll_distance` 而非高德 `tolls`(用例断言 tolls=0 时仍算出非零过路费);铁路/飞机/机票/deep-link 断言零回归;全仓 `grep -rn "overpass\|osrm" backend/` 仅剩历史注释/文档。
- 结果: (待夜班回填)

---

## [TASK-9d] 前端地图切高德 JS API 2.0:Leaflet 退役 + /api/map-config 运行时注入 JS key(与本晚 9c 连做)
- 依赖: TASK-9b + TASK-9c(后端接口就绪;本项目前端零改动的约束由 9b 保证)

- 状态: pending
- 目标: `index.html` 移除 Leaflet(CDN+OSM 瓦片)改高德 JS API 2.0(官方路径;真机若报 `INVALID_USER_SCODE` 则降级为「保留 Leaflet + 换高德无 key 栅格瓦片」并注明待升级,不许空过);新增后端 `GET /api/map-config` 返回 `{"amap_js_key","amap_security_js_code"}`(取自 env,**不进 git、不写进 index.html**),前端加载 JS API **前**先写 `window._AMapSecurityConfig={securityJsCode:…}`(**安全密钥已由神朱提供并落 `.env`**)再动态注入 `<script>`、无 key 给明确降级文案;环圈→`AMap.Circle`(外实内虚)、pin→`AMap.Marker`(保留分类色/emoji 自绘)、点 pin→`AMap.InfoWindow` 承载现有 `popupHtml()`(含收藏/路线/「📄 查看详情」按钮,为 TASK-8b 铺路)、路线→`AMap.Polyline`(驾车实线/铁路虚线/飞机 `arcPoints` 弧线)、`fitView` 用环圈 bounds;`identityHtml()` 改「来源:高德 · POI <id> · GCJ-02」,种子行加 WGS-84 偏差提示;状态栏数据源文案改高德口径。
- 只读清单: `backend/app/static/index.html`、`backend/test_frontend_routes.py`、`backend/app/api/places.py`(加 map-config 的风格)、`docs/TASK-9-CONTRACT.md`(§3「TASK-9d」逐字执行)。
- 涉及: backend/app/static/index.html、backend/test_frontend_routes.py(≥12 新断言 + node --check)、backend/app/api/(新增 map-config 路由)
- 验收: pytest 零回归 + ≥12 新增断言(`/api/map-config` 调用、`AMap.` 使用、`AMap.InfoWindow`、**index.html 内不得出现 32 位 key 样态字符串**);browser_exec 真机 QA:开页 0 error → 高德地图渲染 → pin+环圈 → 点 pin 出 InfoWindow → 点路线出面板并画线;安全密钥已就绪,若仍报 `INVALID_USER_SCODE` → 按契约授权**当轮降级为「Leaflet + 高德无 key 栅格瓦片」并注明待升级**,不许空过。
- 结果: (待夜班回填)

---

## [TASK-8a1] 目的地图片链路:高德 POI 图(主) + 维基百科/Commons(兜底) + PlaceMedia 缓存 + /api/places/media

- 状态: pending
- 目标: 落地点详情弹窗的「相关图片」后端:新建 `data_sources/amap.py`(高德 Web 服务 place/text 取 `pois[0].photos`,带坐标门控防海外误配)与 `data_sources/wikimedia.py`(zh.wikipedia geosearch 优先、search 兜底 + Commons 相册补图);新服务 `services/place_media.py`(高德优先→维基兜底→无图占位,命中缓存 7 天/空结果负缓存 6h);新表 `PlaceMedia`;新 API `GET /api/places/media?place_ids=`(batch ≤20,读库优先、仅 miss 触网,单条失败不影响其余)。
- 只读清单: `backend/data_sources/_common.py`、`backend/data_sources/photon.py`、`backend/db/models.py`、`backend/db/repository.py`、`docs/TASK-8-CONTRACT.md`(§1~§3.8a1 逐字执行;参考只读 `backend/app/api/places.py` 的 details 批量形态)。
  - ⚠️ 前置依赖(2026-10-01 神朱裁定切换高德后补记):`data_sources/amap.py` 由 **TASK-9a(高德数据源层)** 首个创建;本条**不得新建第二个 amap.py**,改为「往既有 `amap.py` 追加 `search_poi_photos`」。执行顺序:排在 TASK-9a 之后;其余(place/text 取 photos、坐标门控、维基兜底、PlaceMedia、/api/places/media)口径不变。
- 涉及: backend/data_sources/(新增 amap.py、wikimedia.py、改 _common.py 加 amap 源)、backend/services/place_media.py(新)、backend/db/(models.py 加 PlaceMedia 表、repository.py 加读写)、backend/app/api/places.py(加 /api/places/media)、backend/test_*.py(≥22 新用例,全 mock 不触网)
- 验收: pytest baseline 792 零回归 + ≥22 新增;契约文档 §3「TASK-8a1」全部字段/响应键/ENV 名逐字照做;真机冒烟 `/api/places/media?place_ids=<西湖>` 出图并标注 source;缺 key 时降级不报错(返回 source=none/reason=no_key)。
- 结果: (待夜班回填)

---

## [TASK-8a2] 类别专属要点:LLM 按分类出结构化字段 + PlaceHighlight 永久缓存 + /api/places/highlights

- 状态: pending
- 目标: 新建 `services/highlights.py`:四分类固定字段表(自然→最佳季节/门票开放/游玩建议;人文美食→人文背景/必吃/代表小店;滑雪→雪道数与分级/开放期/适合人群;运动→项目/场地装备/适宜人群),严格 JSON 输出、**查不到置 null 并在 note 标「待核实」禁止编造**;预算 `HIGHLIGHT_MAX_TOKENS=600 / HIGHLIGHT_TIMEOUT_S=60`(必须按调用放大,勿沿用 intro.py 的 120/20s);新表 `PlaceHighlight`(生成后永久缓存);新 API `GET /api/places/highlights?place_ids=`(batch ≤10,读库优先)。
- 只读清单: `backend/services/details.py`(LLM 预算与缓存口径样板)、`backend/db/models.py`、`backend/db/repository.py`、`backend/app/api/places.py`(details 端点形态)、`docs/TASK-8-CONTRACT.md`(§3「TASK-8a2」逐字执行)。
- 涉及: backend/services/highlights.py(新)、backend/db/(models.py 加 PlaceHighlight、repository.py)、backend/app/api/places.py(加 /api/places/highlights)、backend/test_highlights.py(≥12 用例,LLM 全 mock 不触网)
- 验收: pytest 零回归 + ≥12 新增;四分类字段表逐字一致;「拿不到→null+待核实」有专门用例;LLM 异常/解析失败降级为 fields=[] 不抛 500;单次调用确实放大到 600/60(用例断言传入参数)。
- 结果: (待夜班回填)

---

## [TASK-8b] 前端目的地详情弹窗:pin「查看详情」+ 列表行整行可点 + 图片区 + 类别要点 + 路线/住宿按钮(依赖 8a1/8a2)

- 状态: pending
- 前置依赖: 排在 TASK-9c(前端地图切高德 JS API)之后 —— 弹窗直接建在高德地图上,避免 Leaflet→高德 对同一段 pin/弹窗代码重写两次。
- 目标: `index.html` 新增居中详情弹窗 `#placeModal`(role=dialog/aria-modal,Esc+点遮罩+✕ 关闭):标题/类别/距离 → 图片区(打开时才调 `/api/places/media`,骨架态,空/失败给「暂无图片」+**图源标注**「高德/维基百科」+ page_url 外链) → 详细介绍(复用 `detailTextOf` + `/api/places/details` 生成入口) → 类别专属要点(`/api/places/highlights`,骨架+「待核实」弱化样式) → `🚗 路线 / 🛏️ 住宿`(复用 `openRoutePanel`)+ ⭐收藏 → `identityHtml` 脚注。入口:①`popupHtml()` 加「📄 查看详情」按钮(pin 仍先出小 popup);②列表 `.pl-row` 整行可点开同一弹窗,行内既有按钮 `stopPropagation` 不回归。不新增前端 key。
- 只读清单: `backend/app/static/index.html`、`backend/test_frontend_routes.py`、`docs/TASK-8-CONTRACT.md`(§3「TASK-8b」逐字执行)。
- 涉及: backend/app/static/index.html、backend/test_frontend_routes.py(≥12 静态断言 + node --check)
- 验收: pytest 零回归 + ≥12 新增断言(DOM id/函数名/接口字符串/「图源」文案/batch 参数拼接/stopPropagation);browser_exec QA:开页 0 error → 点 pin →「查看详情」→ 弹窗出图/介绍/要点 → 路线住宿按钮出面板 → Esc 关闭 → 列表行开同一弹窗;既有收藏/路线/住宿行为零回归。
- 结果: (待夜班回填)

---

## 追加模板(新任务复制此段)

## [TASK-xxx] 标题
- 状态: 示例(勿执行;新任务复制本段并改成 pending)
- 目标:
- 涉及:
- 验收:
- 结果: (待夜班回填)
