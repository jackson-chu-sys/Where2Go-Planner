# Where2Go 境外目的地支持:境内/境外双轨方案(TASK-19 调研)

> 调研执行:2026-10-10 夜班执行器(qwen3.8-max),容器内直连/代理对比实测 + web_search 计费核实 + browser_exec 真机复核。
> 本条**零代码改动**(pytest 1409 passed 基线未动);产出=本文档,供神朱拍板后拆实施 TASK。
> 实测脚本与原始输出:/tmp/overseas_probe{,2..13}.py 与同名 _result.txt(容器 /tmp,重启即失,关键数字已全部摘录于此)。

---

## 0. 问题重述(神朱 2026-10-10 三反馈)

1. Booking / Airbnb 外链搜不到推荐酒店(携程已修好);
2. 中文名搜外国目的地全部错误定位(高德把「瑞士因特拉肯」吸到成都双流小区);
3. 方向:境内维持高德,境外换 Google Maps 或 Azure Maps,并优化 Booking/Airbnb 外链。

## 1. 境外地图 + 地理编码选型对比矩阵(全部当日实测,2026-10-10)

### 1.1 网络可达性(容器直连,禁代理 vs 走代理)

| 服务 | 直连(产品无代理环境) | 走代理 | 结论 |
|---|---|---|---|
| **Google Maps**(maps.googleapis.com / khms 瓦片 / maps.gstatic.com) | ❌ TCP 全超时(Network unreachable / 8s timeout;DNS 返回被污染 IP 如 69.171.235.22、185.45.5.35) | ✅ geocode 200(无 key 报 key 错,链路通)、JS API 200 | **境内用户/境内服务器直连均不可达**;产品部署在境内 NAS,直连必死,除非用户全程挂代理 |
| **Azure Maps**(atlas.microsoft.com) | ❌ DNS 直接无解析(No address associated;不同 resolver 一次超时一次 NXDOMAIN) | ✅ 200 但 **20.8s~22.5s**(极慢) | 国际版端点在境内不可达;中国区端点(*.azuremapscn.com)**DNS NXDOMAIN**(该区域是世纪互联运营的 Azure China,Azure Maps 中国区仅部分服务,且需企业订阅) |
| **Photon**(photon.komoot.io) | ✅ 200,0.9~2.3s | ✅ | **直连可用**,已在降级链中 |
| **Nominatim**(nominatim.openstreetmap.org) | ✅ 200,0.4~0.8s(**10/10 复测直连已通**,推翻 9/28「直连不通」旧记录,当时疑为临时故障) | ✅ | 直连可用但 1 req/s 硬限流,只适合低频地理编码,不适合做 POI 主力 |
| **Overpass**(overpass-api.de) | ✅ 200,境外 POI 查询 8.5s(因特拉肯 5km 圆 30 条 elements,含 name) | ✅ 3.3s | **境外 OSM POI 数据充足**,直连可用——阶段1 的 Overpass 链路可作境外 POI 源复活 |
| **OSRM**(router.project-osrm.org) | ✅ 200,1.3s(因特拉肯→格林德瓦 19.7km/24min 正确) | ✅ 0.5s | 境外驾车路线可用 |
| **OSM 官方瓦片**(tile.openstreetmap.org) | ❌ 8s 超时(DNS 污染,解析到 199.59.149.136/182.50.139.56 假 IP) | 未测 | 直连不可用 |
| **tile.openstreetmap.de**(OSM 德国镜像) | ✅ 200,0.78s,28980B 真 PNG | — | **直连可用!**境外底图首选候选 |
| **tile.openstreetmap.fr**(OSM 法国镜像) | ✅ 200,0.75s,31584B 真 PNG | — | 直连可用,备线 |
| **basemaps.cartocdn.com**(CARTO) | ✅ 200,2.0s,2049B PNG | — | 直连可用,备线(a.basemaps.cartocdn.com 不通,注意主机名差异) |
| maps.wikimedia.org | ❌ 8.3s 超时 | — | 不可用 |
| **高德无 key 栅格**(webrd01.is.autonavi.com) | ✅ 0.05s,但**境外 z≥10 全是 179B 空白瓦片**(z=6/8 有图,z=10~16 空白;境内 z=14/16 是 21~27KB 真图) | — | 境外只能看国家级轮廓,不能当境外底图 |
| 高德卫星(webst01 style=6) | 境外返回 4235B **占位图**(因特拉肯与皇后镇 z14 瓦片 sha1 完全相同 = 同一张假图) | — | 境外无卫星数据 |
| 腾讯地图瓦片 | 境外 169B/671B ≈ 空白 | — | 不可用 |
| 天地图(无 tk) | HTTP 418 | — | 需 key,未验 |

### 1.2 地理编码中文境外地名命中(核心)

**高德 geocode(现状,复现失败形态)**:
- 「因特拉肯」→ **成都市双流区因特拉肯(东北门)**(status=1 count=1,错误吸附,status 层面完全看不出失败);
- 「瑞士因特拉肯」→ 同样吸到成都(count=2);
- 「皇后镇」→ **河南林州市皇后镇**(错误吸附);
- 「富良野」「巴黎」「Interlaken」→ status=0 无结果;
- POI 文本搜索:「富士山」「皇后镇」count=0;「Interlaken」count=6 但全是国内无关店铺(茵特拉根形象店等)。
- **硬结论**:高德对境外要么无结果、要么静默错误吸附;**不能用高德 geocode 结果判定境内外**(status=1 也可能错)。

**Photon(直连,1 req/s)**——中文境外命中**不稳定**:
- ✅ 命中:富良野(误差0.9km)、富士山(0.3km)——日本汉字地名 OK;
- ❌ 失配:「因特拉肯」→ 香港因特拉肯大道(9362km)、「皇后镇」→ 北京皇后镇休闲度假村(10837km)、「巴黎」→ 高雄巴黎(9972km)、「清迈」→ 珠海清迈泰式汤粉馆(1564km)、「瑞士因特拉肯」→ 0 结果;
- ✅ 英文名全部命中:Interlaken(0.3km)/Queenstown(0.2km)/Furano(1.5km)/Paris(0.4km);
- **英文名+国家后缀 7/7 全中且误差 ≤0.4km**(Interlaken Switzerland / Queenstown New Zealand / Furano Japan / Paris France / Chiang Mai Thailand / Zermatt Switzerland / Reykjavik Iceland);
- Photon 的 `country` 字段可靠:境内坐标逆地理返回 country=中国(上海/漠河实测),境外返回正确国家(瑞士实测)——**是境内外判定的可用信号**。

**Nominatim(带 `accept-language=zh-CN`)**——中文境外命中**全绿**:
- 「因特拉肯」0.3km、「瑞士因特拉肯」0.3km、「皇后镇」0.2km(新西兰)、「富良野」0.9km、「富士山」0.3km、「巴黎」2.4km(法国)、「清迈」0.1km(泰国),8/8 全中;
- `countrycodes=ch/nz/fr` 限定同样全中(消歧手段可用);
- display_name 自带中文译名(因特拉肯/皇后镇/法兰西岛大区);
- 限制:1 req/s 硬限流(公共实例政策),适合**低频地理编码**(用户搜城市),不适合批量 POI。

**结论(地理编码)**:境外中文地名 → **Nominatim(accept-language=zh-CN)主 + Photon 英文名兜底**;若 LLM 链路在(项目已有 intro.py 抽象),可加一步「LLM 把中文境外地名翻成 英文名+国家」再走 Photon(实测该路径 7/7 命中,LLM 翻译是项目已有能力,单次 <1s)。三腿都不依赖高德。

### 1.3 三家选型对比矩阵(汇总)

| 维度 | **Google Maps** | **Azure Maps** | **OSM 系(Leaflet + osm.de/fr 瓦片 + Photon/Nominatim)** |
|---|---|---|---|
| key 成本 | 需 GCP 账单账户;2025-03 起 $200/月抵扣改为每 SKU 免费额度(Geocoding 等 Essentials 10K 次/月、Maps Dynamic 10K 会话/月,超出 $5/千次)(developers.google.com/maps/billing-and-pricing/march-2025) | 需 Azure 订阅;Gen2 免费层 5000 次/月·SKU(Base Map 5000 事务=75000 瓦片);**Gen1 定价层 2026-09-15 退役,Gen2 是唯一现行层**(learn.microsoft.com/azure/azure-maps/how-to-manage-pricing-tier);微软退役的是 Bing Maps for Enterprise(2028-06),Azure Maps 是继任平台非退役 | **零 key 零费用**(公共瓦片+公共 geocoder;遵守 attribution 与限流即可) |
| 大陆可达性(直连) | ❌ **不可达**(实测 TCP 全超时,DNS 污染) | ❌ 国际版 DNS 无解析;中国区 *.azuremapscn.com NXDOMAIN | ✅ **osm.de/osm.fr/basemaps.cartocdn 直连实测全通(0.75~2s)**;osm.org 官方主站被污染不可用 |
| 中文境外地名 geocode | 支持(需可达才有意义) | 支持 | ✅ Nominatim zh-CN 8/8 全中;Photon 中文不稳(4/8)但英文名+国家 7/7 |
| 境外 POI 数据 | 强 | 中(TomTom 数据) | ✅ OSM 强(Overpass 因特拉肯实测 30 条含景点名) |
| 坐标系 | WGS-84(境外无 GCJ-02 偏移问题;境外全行业都是 WGS-84) | WGS-84 | WGS-84 |
| 前端改造量 | 换 JS API(第三套地图代码) | 换 Azure Maps Web SDK(第三套) | **回到 Leaflet**——项目阶段1(2026-09-09~09-30)完整跑过 Leaflet,git 历史里 index.html Leaflet 版本可直接考古复用,是**改造量最小**路径 |
| 风险 | 境内用户打开地图=白屏;计费账户需外币卡 | 中国区订阅门槛高;国际版不可达 | 公共瓦片限流/可用性波动(需多镜像轮换);Overpass 公共实例慢(20-80s 冷抓,已知) |

**选型结论(推荐)**:**境外走 OSM 系(Leaflet + osm.de/osm.fr 瓦片 + Nominatim/Photon 地理编码 + Overpass POI + OSRM 驾车),不采用 Google/Azure**。理由:①Google/Azure 在境内直连双双不可达(实测),而产品部署在境内 NAS、目标用户境内为主——这是**决定性一票否决**;②OSM 系零成本、直连实测全通、中文命中(经 Nominatim)反而比 Photon 好;③Leaflet 是项目已验证过的技术栈。Google Maps 仅作为「用户自愿挂代理」的可选增强,不进默认链路。Azure Maps 同理(若神朱手上有 key 也只能走代理,不改变结论)。

## 2. 境内外判定开关设计

**原则:不能用高德判定(错误吸附 status=1 看似成功)。**判定放在 `services/place_loader.py` 的起点解析层(geocode_with_fallback 处):

1. **判定函数**(新增 `services/region_gate.py` 或并入 place_loader):
   - 用户输入起点 → **先走 Photon/Nominatim**(已有降级链,直连可用):取返回的 `country` 字段(Nominatim `address.country_code` ISO 3166-1 alpha-2 最干净;Photon `properties.country` 是本地语言文本如「中国」);
   - `country_code == "CN"`(或 Photon country 含「中国」)→ **境内轨**(现状高德链路,零改动);
   - 否则 → **境外轨**(OSM 系链路);
   - OriginCache 表已有 geocoder/city 列,加一列 `region`(domestic/overseas)缓存判定结果,TTL 7 天同现状。
2. **失败兜底**:Photon+Nominatim 双失败(无网络)→ 默认境内轨(现状高德)+ note 标注「未能判定境内外,按境内处理」;用户可手动切轨(前端加「境外模式」开关,localStorage 记忆)。
3. **名称规则兜底(可选加速)**:输入以已知境外国家/城市前缀开头(「瑞士…」「日本…」等正则白名单)可跳过网络判定直接走境外轨;白名单不必求全,只是省一次网络。
4. **坐标落界校验(不推荐单独使用)**:中国陆地边界 bbox(73~135°E, 3~54°N)粗判会与周边国家混叠(河内/曼谷纬度在界内),只作「明显境外」的快速排除,最终以 geocoder country 字段为准。
5. **坐标系口径**:境外轨全程 WGS-84(OSM/Leaflet 原生);**绝不与 GCJ-02 混用**——Place 表已有 tags.source 标「高德」,境外行标「OSM」+ tags.coord_system="WGS-84",前端渲染按 region 分支(高德地图用 GCJ-02、Leaflet 用 WGS-84)。库内两轨数据用 `osm_type` 天然隔离(境内="amap",境外="osm_node/way/relation" 回归阶段1 身份)。

## 3. Booking / Airbnb 外链复核结论(问题①)

### 3.1 真机复核(browser_exec,2026-10-10,headless Chrome 境内出口)

**现状拼法 `ss=酒店名,城市名`(昆山柏轩酒店,昆山)→ h1「在此目的地及周边地区有 0 家住宿」**。但对照组证明这**不是拼法问题**:

| 测试 | URL 关键参数 | 结果 |
|---|---|---|
| ss=昆山柏轩酒店,昆山(现状) | `ss=%E6%98%86%E5%B1%B1...%2C%E6%98%86%E5%B1%B1` | 0 家 |
| ss=昆山柏轩酒店(纯名) | | 0 家 |
| ss=昆山(纯城市) | | 0 家 |
| ss=Interlaken(境外城市) | | 0 家 |
| ss=Shanghai / ss=上海 | | 0 家 |
| **ss=Pan Pacific Suzhou(先经 dest_id 搜索确认在架的酒店,英文名)** | | **0 家** |
| **dest_id=79514&dest_type=city(苏州,调研已知 id)** | | ✅ **正常出结果**(Pan Pacific Suzhou、苏州山塘街华贸姑苏里酒店公寓等) |
| **dest_id=-2552548&dest_type=city(因特拉肯,当晚从城市页取得)** | | ✅ **正常出结果**(Hotel Interlaken、Victoria Jungfrau 等 4+) |

**结论:本环境(境内出口 IP + headless)下 Booking 的 `ss=` 文本搜索路径整体降级为 0 结果,而 `dest_id/dest_type` 结构化参数路径完全正常。**神朱真人实测「搜不到」与 headless 一致指向:`ss` 对境内访客/风控环境不可靠,**结构化 dest_id 才是稳的路径**。另:昆山柏轩酒店可能本身未上 Booking(小体量酒店),但 Pan Pacific 对照已排除「拼法」因素。

**dest_id 获取途径(实测)**:
- autocomplete.json 需有效 aid(裸调返回空 results,10/9 调研已证)——**排除**;
- **城市页 `booking.com/city/<cc>/<slug>.zh-cn.html` 页面 HTML 里有 dest_id**(因特拉肯 -2552548、苏州 79514;curl 直连被 cookie 墙挡住「需您同意」,headless 浏览器过墙后可取;服务端预取不稳,调研报告 §A2 已注明 WAF)——可行但脆弱;
- **推荐落地:后端懒解析 + 永久缓存**(照 TASK-17 携程 hotelId 同款模式):收藏住宿时/打开面板时按城市名试 `ss=<城市拼音>` 城市页(服务端 curl,失败留空),成功则写 `summary.booking_dest_id`,前端外链优先带 dest_id,拿不到退 `ss=` 现状(不丢链接)。城市页 slug 是拼音(`suzhou`/`interlaken`),需要「中文城市名→拼音 slug」转换(pypinyin 是第三方依赖违反约定;可用 LLM 一次性翻译缓存,项目已有 LLM 抽象)。

### 3.2 修正后拼法(前端 bookLinksForStay 改动点)

```
Booking(境内): https://www.booking.com/searchresults.zh-cn.html
  ?dest_id=<summary.booking_dest_id>&dest_type=city      # 有 dest_id(优先)
  &ss=<酒店纯名>                                          # 城市内过滤
  &group_adults=2&no_rooms=1
  兜底(无 dest_id): 现状 ss=<酒店纯名>,<城市名> 不变
Airbnb: 境内隐藏(2022-07 起境内房源已停;真机实测对照坐实——Kunshan--China 页 h1 虚标
  「Over 1,000 stays」但实际房源卡片 0 个(a[href*="/rooms/"]=0),Interlaken--Switzerland 页
  18 张房源卡片正常;**h1 文案不可信,以卡片数为准**);
  境外启用: /s/<城市slug>--<国家slug>/homes(slug 宽容解析确认)
  境内/境外判定用 summary 里的 region 字段(收藏时从 Place/Stay 行透传,见 §2)
携程(境外): 实测 suggest 接口对境外返回 type="IntlCity"(因特拉肯 cityId=3167 瑞士、皇后镇 3860 新西兰)
  与 type="Hotel" 境外酒店(皇后镇湖畔诺富特 2196343);
  hotels.ctrip.com/hotels/detail/?hotelId=2196343 境外酒店 200 标题正确;
  hotels/list?cityId=3167 境外城市页 200「因特拉肯酒店预订查询」正常;
  → 现有 data_sources/ctrip.py 只认 type=="Hotel"(L100),**IntlCity 会被丢弃**——
    境外场景需把 IntlCity/IntlDistrict 也纳入(cityId 落 summary.ctrip_city_id,detail/list 两级外链口径不变,
    trip.com 国际站无需切换:hotels.ctrip.com 境外酒店/城市页实测已正常服务中文用户)
```

### 3.3 Airbnb 境内隐藏 + 境外启用的前端改动点

- `bookLinksForStay(item)`:读 `item.summary.region`(overseas/domestic,收藏时写入;存量收藏懒补齐按 summary.city 判定——city 非中文/不在境内城市名单 → overseas,判不出默认 domestic);
- domestic → 渠道=携程+Booking(Airbnb 整行不出);overseas → 渠道=携程(或 trip.com 同域)+Booking+Airbnb;
- 测试:test_frontend_routes.py 静态断言(渠道集合按 region 分支、Airbnb 字符串不出现在 domestic 分支)。

## 4. 双轨架构草案 + 拆条清单(每条 ≤1 晚)

### 4.1 架构(data_sources 层按 region 路由,形状仿 ADR-006 降级链)

```
用户输入起点
  → region_gate: Photon/Nominatim geocode → country 判定(§2)
  ├─ 境内轨(现状零改动): amap.py(POI/geocode/driving) + 高德 JS API 前端 + LLM 简介/推荐/估价
  └─ 境外轨(新增): osm_overseas.py(复活 Overpass 检索,四分类 tag 组沿用阶段1 classify.py 的 SEARCH_GROUPS)
       + Nominatim(zh-CN) geocode / Photon 英文名兜底
       + OSRM 驾车(router.project-osrm.org 实测可用;铁路/飞机沿用现有估算引擎,里程公式与国别无关)
       + 前端 Leaflet(境外时切换;index.html 从 git 历史 9/30 前版本考古复用 pin/环圈/弹窗组件)
       + LLM 简介/推荐/详情/估价链路零改动(候选来自境外检索,喂给同一套 intro/recommend/details/stays)
  存储: Place 表零改动(osm_type="osm_*" 回归阶段1 身份;tags.source="OSM";tags.coord_system="WGS-84")
  住宿: stays.py 加 osm 腿(tourism=hotel 等,阶段1 STAY_TAGS 现成),估价规则表境外品牌仍适用(希尔顿/万豪系已有)
  坐标: region_gate 结果写 OriginCache.region;前端地图按 region 选 AMap(境内 GCJ-02)/Leaflet(境外 WGS-84)
```

### 4.2 拆条清单(P 系列,建议实施顺序;每条 ≤1 晚,契约按「一任务一契约文档」规范另立)

| 条 | 内容 | 涉及 | 体量 |
|---|---|---|---|
| **P1 region_gate** | 境内外判定服务 + OriginCache.region 列 + /api/geocode 响应加 region 键(只加键) | services/region_gate.py(新)、db/models.py、app/api/places.py、≥15 测试 | 小(≈10a 体量) |
| **P2 境外 POI 检索链** | osm_overseas.py(Overpass 复活,四分类 SEARCH_GROUPS 照阶段1)+ place_loader 按 region 分支 + 入库身份 osm_* | data_sources/(新)、services/place_loader.py、classify.py(SEARCH_GROUPS 恢复)、≥20 测试 | 中(契约里明示实现/测试分两次调用) |
| **P3 境外路线+住宿** | routes.py 驾车按 region 分支走 OSRM(几何抽稀口径复用);stays.py 境外腿(OSM tourism)+ Nominatim 反查城市 | services/routes.py、stays.py、data_sources/osrm.py(从 git 历史恢复)、≥15 测试 | 中 |
| **P4 前端双轨地图** | region=overseas 时 Leaflet 渲染(瓦片 osm.de 主/osm.fr 备/basemaps.cartocdn 三线轮换,attribution 必带);境内高德 JS API 零改动;pin/环圈/弹窗组件从 9/30 前 index.html 考古复用 | index.html、test_frontend_routes.py、browser QA | 中偏大(前端单晚,神朱 2026-10-01 已裁定的降级条款思路适用:Leaflet 不可用→高德境外空白瓦片+提示,不空过) |
| **P5 OTA 外链修正** | §3.2/§3.3:Booking dest_id 懒解析+缓存(summary.booking_dest_id)、Airbnb 境内隐藏境外启用、ctrip.py 纳入 IntlCity/IntlDistrict | data_sources/ctrip.py、services/ota_links.py、index.html(bookLinksForStay)、≥10 测试 | 小~中(**可独立先行,不依赖 P1-P4**——境内 Booking 0 结果问题现在就存在) |

### 4.3 风险与开放问题(报神朱拍板)

1. **Overpass 冷抓慢回来**:境外轨冷抓一段 20~80s(公共实例),阶段1 老体验;渐进配额口径(TASK-6b)可复用缓解,但首次体感仍差。可接受?(境内高德 3~12s 的爽快感境外给不了)
2. **LLM 翻译腿**:中文境外地名 → 英文名+国家 的 LLM 翻译是否启用(Nominatim zh-CN 已 8/8 全中,翻译腿只是 Photon 兜底的增强;不启用则链路更简单)。
3. **P5 是否先行**:Booking 境内 0 结果问题不等境外轨,建议 P5 单独排下一晚先修(dest_id 懒解析 + Airbnb 境内隐藏)。
4. **Google/Azure 彻底出局确认**:本调研在境内出口实测双双不可达;若神朱预期用户群「人人有代理」,可重评 Google(免费额度 10K/月对 DEMO 足够),但默认不推荐。
5. **Airbnb 渠道**:境内隐藏后,境内住宿渠道=携程+Booking 两家;是否给境内补「美团/去哪儿」第三家?(未调研,如需另立条目)

## 5. 引用汇总

| # | 来源 | 用途 |
|---|---|---|
| 1 | 实测(容器内 /tmp/overseas_probe*.py,2026-10-10) | §1.1/§1.2 全部可达性与命中数字 |
| 2 | 实测(browser_exec headless,2026-10-10) | §3.1 Booking dest_id 对照、Airbnb Interlaken 18 卡片 |
| 3 | https://developers.google.com/maps/billing-and-pricing/march-2025 | Google 2025-03 计费改版:$200 月抵扣 → 每 SKU 免费额度(Essentials 10K) |
| 4 | https://mapsplatform.google.com/intl/en_uk/pricing | Google 免费额度表(Essentials 10K/Pro 5K/Enterprise 1K) |
| 5 | https://learn.microsoft.com/en-us/azure/azure-maps/how-to-manage-pricing-tier | Azure Maps Gen1 定价层 2026-09-15 退役、Gen2 现行 |
| 6 | https://azure.microsoft.com/en-us/pricing/details/azure-maps/ | Azure Maps Gen2 免费层 5000 事务/月(Base Map 1 事务=15 瓦片) |
| 7 | docs/research-ota-wechat-2026-10-09.md §A2/A3 | Booking dest_id 体系、autocomplete 需 aid、Airbnb 境内停摆(2022-07) |
| 8 | https://www.booking.com/city/ch/interlaken.zh-cn.html(headless 实取) | 因特拉肯 dest_id=-2552548 |
| 9 | git 历史(index.html @ 2026-09-30 前) | Leaflet 前端组件考古复用源 |
