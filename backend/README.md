# backend · 免费数据源验证层(阶段0)+ 目的地入库、四分类与简介(阶段1a/1b/1c)+ 路线服务与费用估算(阶段2a)
+ 前端路线面板(阶段2b)

对应任务:`tasks/TASK-001-data-source-poc.md`(阶段0,ADR-006:免费、无需 key 的数据源)、
`docs/NIGHTLY-QUEUE.md` TASK-1a(阶段1a:Place 入库 + 检索 API + Leaflet 地图,
规格见 `docs/STAGE1-PLAN.md` 第 2/4 节)、TASK-1b(阶段1b:四分类优先级归类去重 +
LLM 一句话简介 + popup 卡片,规格见 `docs/STAGE1-PLAN.md` 第 3 节)、
TASK-1c(阶段1c:浏览器"我的位置"定位/换城 + 滑雪/运动**人工种子数据**垫底,
规格见 `docs/STAGE1-PLAN.md` 第 3 节"数据支撑现实")、
TASK-2a(阶段2a:多方式**路线服务 + 费用估算** + `GET /api/routes` + deep-link 跳转链接,
规格见 `docs/STAGE2-PLAN.md` 第 2/4 节)、
TASK-2b(阶段2b:**前端路线面板** —— 点 pin 出多方式卡片 + 地图画线 + deep-link 跳转,
规格见 `docs/STAGE2-PLAN.md` 第 3 节)。

## 目录

```
backend/
├─ requirements.txt              依赖(requests、fastapi/uvicorn、sqlalchemy;测试用 pytest)
├─ conftest.py                   pytest 路径引导 + 整套单测默认关闭种子数据(WHERE2GO_SEEDS=off)
├─ test_data_sources.py          阶段0:三个源的纯 mock 单测(不触网,39 个用例)
├─ test_places.py                阶段1a:入库/读库/API 的纯 mock 单测(不触网,20 个用例)
├─ test_classify.py              阶段1b:归类优先级/跨 tag 去重/检索并集/LLM 简介的纯 mock 单测(46 个用例)
├─ test_seed_data.py             阶段1c:种子数据校验/合并去重/来源标注/起点逆地理编码的纯 mock 单测(56 个用例)
├─ test_routes.py                阶段2a:路线编排/费用系数/deep-link/参数校验/OSRM 降级的纯 mock 单测(28 个用例)
├─ test_frontend_routes.py       阶段2b:前端路线面板静态页断言 + 前后端字段契约 + node 语法检查(23 个用例)
├─ data_sources/                 数据获取层(阶段0,免费无 key)
│  ├─ __init__.py                统一导出 route / geocode / reverse / nearby_places
│  ├─ _common.py                 User-Agent、timeout(≤20s)、JSON 请求与中文错误
│  ├─ osrm.py                    驾车路线 → {distance_km, duration_min}(with_geometry=True 时
│  │                             附 Leaflet 折线 [[lat, lng], ...],阶段2a 画线用;默认形状不变)
│  ├─ nominatim.py               正向/逆向地理编码 → {lat, lng, display_name}
│  ├─ overpass.py                周边 POI 检索 → [{lat, lng, name, tags}](with_id=True 时附 osm id/type;
│  │                             nearby_places_grouped = 多组 tag 并集、每组独立配额,一次请求查完四分类)
│  └─ verify_poc.py              真实网络端到端验证脚本(联网)
├─ db/                           存储层(阶段1a,SQLite + SQLAlchemy 2.0)
│  ├─ models.py                  Place / SegmentFetch 表定义 + 来源标注派生(place_source/is_seed)
│  ├─ base.py                    引擎与会话(懒加载;WHERE2GO_DB_URL 可覆盖库地址)
│  └─ repository.py              upsert / 按段检索 / 分类计数 / **来源计数** / 抓取水位 / 缺简介行查询
├─ services/                     业务层(阶段1a/1b/2a)
│  ├─ bands.py                   环形距离分段定义(POC 与入库共用同一口径)
│  ├─ classify.py                四分类归类引擎:OSM tag 线索 + 归类优先级 + 跨 tag 去重 + 检索并集分组
│  ├─ categories.py              classify 的兼容导入面(阶段1a 旧名字;新代码直接 import classify)
│  ├─ intro.py                   LLM 一句话简介:Provider 注册表 + 按 POI 缓存 + 失败降级 + CLI
│  ├─ reclassify.py              存量库重归类(category 旧值/空值按四分类规则重算,离线)+ CLI
│  ├─ seed_data.py               **人工种子数据**(滑雪场/运动,49 条)+ 与 OSM 合并去重 + 校验 CLI
│  ├─ routes.py                  阶段2a:多方式路线编排(驾车 OSRM 真实 + 铁路/飞机估算)
│  │                             + 费用估算系数 + deep-link 纯函数(高德/Google/12306/OTA)
│  └─ place_loader.py            (城市, band) 抓取入库编排(去重归类 → 入库 → 合并种子 → 补简介)
│                                + 浏览器 GPS 起点逆地理编码(resolve_reverse_origin)+ CLI
└─ app/                          HTTP 服务(FastAPI)
   ├─ main.py                    路由装配 + 静态页面挂载
   ├─ api/discover.py            POC 路由(阶段0,保留不动)
   ├─ api/places.py              GET /api/places、/api/places/meta、/api/places/intros、/api/geocode、
   │                             /api/geocode/reverse(浏览器"我的位置")
   ├─ api/routes.py              GET /api/routes(阶段2a:三方式时间 + 费用对比 + 跳转链接)
   └─ static/index.html          Leaflet 地图页(阶段2b:分类着色 pin + 简介 popup + 📍 我的位置
                                 + 路线面板 / 地图画线 / deep-link 跳转);list.html 为 POC 列表页
```

## 安装与运行

```bash
python3 -m venv .venv && . .venv/bin/activate      # 或用 uv venv .venv
pip install -r backend/requirements.txt

python -m pytest backend/ -q                       # 单测(mock,不触网;212 个用例)
python backend/test_data_sources.py                # 不装 pytest 也能跑阶段0 同一套断言

python -m uvicorn app.main:app --app-dir backend --port 8000   # Web(地图页 http://127.0.0.1:8000/)
python backend/data_sources/verify_poc.py          # 阶段0 真实验证(需联网,约 30-60s)
```

数据库默认落在 `backend/data/where2go.db`(已 gitignore,不上传),首次访问自动建表;
用 `WHERE2GO_DB_URL` 可切换(如 `sqlite:////tmp/x.db` 或 `sqlite:///:memory:`)。

`verify_poc.py` 链路:北京 → 正向定位可核地名 → 逆向确认当前位置 → 按类别
(自然风光 100 km / 旅游景点 50 km)检索真实 POI → 取最近一个 → OSRM 驾车路线,
并对时速、绕行比做合理性校验。任一步失败会打印中文报错并以非 0 退出码结束。

## 阶段1a:目的地入库与检索

**口径**:按 (城市, band) 抓取。首次查该段 → Nominatim 定位起点 → 按分段**上限半径**
查 Overpass(多 tag 并集,一次查完)→ haversine 收敛到 `[low, high)` 环内 → 去重 + 归类
→ 按唯一键 `(osm_type, osm_id, origin_city)` upsert 入库 → 记 `SegmentFetch` 水位。
**已入库的 (城市, band) 二次查询直接读 SQLite,不发任何网络请求**;分类过滤只作用在
读取阶段,所以换分类同样命中库。`refresh=true` 可强制重抓(仍不会产生重复行)。

```bash
python -m services.place_loader 上海 50_100        # CLI 预抓入库(cd backend 后运行)
python -m services.place_loader 上海 100_200 --refresh
```

```python
from db import init_db, make_engine, open_session
from services.place_loader import load_segment

engine = make_engine(); init_db(engine)
with open_session(engine) as session:
    out = load_segment(session, city="上海", band="50_100")
    print(out.source, out.network_used, len(out.places))   # 首次 overpass/True,再次 db/False
```

API(`app/api/places.py`,POC 的 `/api/discover`、`/api/categories` 行为不变):

| 路由 | 说明 |
|---|---|
| `GET /api/places?origin=&band=&category=` | 该段内已入库目的地;可选 `lat`/`lng`(免二次地理编码)、`refresh=true` |
| `GET /api/places/meta` | 分段与分类元信息(前端下拉/图例/pin 颜色的唯一出处) |
| `GET /api/geocode?city=` | 起点城市搜索(Nominatim),并回报该城市哪些分段已入库 |

前端 `app/static/index.html`:Leaflet 1.9.4(CDN,不打包)+ OSM 瓦片,原生 JS。
以起点为中心画当前 band 的环形范围圈(外圆 = 上限半径、虚线内圆 = 下限半径),
band 内 Place 按分类着色渲染 pin,点 pin 出弹窗(名称/分类/距起点/简介/OSM id),
支持切换 band、切换分类、城市搜索与"重新抓取"。CDN 不可用时给出降级提示,API 仍可用。

## 阶段1b:四分类归类去重 + LLM 简介

**归类**(`services/classify.py`,规格见 `docs/STAGE1-PLAN.md` 第 3 节):分类是**产品语义**,
不是 OSM tag 的 1:1。四分类各有一组 tag 识别线索 —— 自然(`natural=*`、`leisure=park|
nature_reserve|garden`、`waterway=waterfall`、`tourism=viewpoint`、`place=island`)、
人文美食(`historic=*`、`tourism=attraction|museum|gallery`、`amenity=restaurant|cafe|...`、
`cuisine=*`、`place=town|village`)、滑雪(`piste:*`、`ski=yes`、`sport=skiing|snowboard|...`、
`landuse=winter_sports`)、运动(`sport=*`、`leisure=sports_centre|pitch|stadium|...`);
按**归类优先级 滑雪 > 运动 > 人文美食 > 自然**首次命中即定类,**每个地物只归一类**,
认不出来落"其他"(不猜)。

**去重**:键 = OSM `(type, id)`。一次并集检索里同一实体常被多组 tag 命中(古镇同时带
`historic=castle` 与 `tourism=attraction`),`dedupe_places` 先合并 tags(归类因此看得到
全部线索)再定类;没有 OSM id 的种子数据(TASK-1c)按"名字 + 坐标"兜底去重。POC 里
"自然风光/旅游景点"按原始 tag 二分造成的重复由此消除 —— 带自然线索的泛景点只算自然,
除非另有 `historic` 或美食线索;`/api/discover` 也改成用同一引擎过滤,响应形状不变。

**检索**:按 band **上限半径**一次请求查完四分类 tag 并集,`SEARCH_GROUPS` 六组各带独立配额
(滑雪 80 / 运动 100 / 人文景点 120 / 小城古镇 40 / 美食 60 / 自然 140,合计 540),
避免 `sport=*`、餐厅这类高频 tag 把总量刷爆、山峰古镇一条不剩。分组并集属冷启动批量抓取
(单次成功请求实测 20-110s;公共实例繁忙时走端点链降级重试,整段抓取可达数分钟),
不受交互请求 20s 上限约束(`DEFAULT_GROUP_REQUEST_TIMEOUT_S=150`,
服务端 QL `timeout:120` 先到点则带部分分组结果返回);环内收敛仍在本地用 haversine 做。

**简介**(`services/intro.py`,ADR-002):LLM 生成一句话中文简介,**按 POI 缓存在
`Place.intro`** —— DB 就是缓存,已有简介的行不再调用,重抓也不覆盖已生成的简介。
Provider 注册表内置 DeepSeek 与阿里 Qwen(DashScope 兼容模式),统一走 OpenAI 兼容的
`POST {base_url}/chat/completions`;key **只从环境变量读**(`DEEPSEEK_API_KEY` /
`DASHSCOPE_API_KEY`,或用 `WHERE2GO_LLM_PROVIDER|BASE_URL|MODEL|API_KEY` 显式覆盖),
不落盘、不入库、不进日志、不出现在任何 API 响应里(`/api/places/meta` 只报 key 来自哪个
环境变量)。未配 key、超时、限流、响应格式异常一律**降级为空简介**,不抛异常、不阻塞入库,
下次可重试。简介在**入库之后**补(抓取路径每次最多 `INTRO_BATCH_LIMIT=40` 条,全量回填走
CLI);命中库直接读库的路径不调 LLM,保持"二次查询秒出"。

```bash
python -m services.intro 上海 --limit 60           # 只补缺简介的前 60 条(cd backend 后运行)
python -m services.intro 上海 --dry-run            # 只报还缺多少条,不调 LLM
python -m services.reclassify 上海 50_100          # 存量库重算 category(离线,不触网)
python -m services.reclassify --dry-run            # 只看会变多少条,不写库
python -m services.place_loader 上海 50_100 --refresh --intro-limit 24
```

新增/变化的 API:

| 路由 | 说明 |
|---|---|
| `GET /api/places?...&intros=&intro_limit=` | 响应多 `intro_stats`(本次 LLM 统计)与 `intro_pending`(仍缺简介条数) |
| `GET /api/places/intros?origin=&band=&category=&limit=` | 给已入库但缺简介的 POI 补简介(已有的不重复调用) |
| `GET /api/places/meta` | 多 `category_priority`、`search_groups`、`search_budget` 与 `llm`(**不含 key**) |

前端:pin 用 `L.divIcon` 按分类着色(自然绿 `#16a34a` / 人文橙 `#ea580c` / 滑雪蓝 `#2563eb` /
运动红 `#dc2626`)+ 分类 emoji;popup 卡片显示名称、同色分类徽标、距起点直线距离与分段、
分类含义、LLM 一句话简介(缺简介给空态提示)与 OSM 身份脚注;图例带各分类计数、归类优先级
与当前 LLM Provider;新增"补简介"按钮(每次最多 40 条,已有简介的跳过)。

## 阶段1c:起点定位/换城 + 滑雪/运动种子数据

**起点定位(需求 M1.01)**:前端「📍 我的位置」走 `navigator.geolocation` 拿 GPS 坐标 →
`GET /api/geocode/reverse?lat=&lng=` → Nominatim **逆**地理编码反查城市 → 地图重定位、
范围圈以**用户真实坐标**为圆心(不用行政区中心)。失败路径全部只给**可见提示、不抛 JS 错误**:

| 情况 | 前端表现 | 后端表现 |
|---|---|---|
| 浏览器不支持 / 非 https 或 localhost | 黄色提示,建议改用城市搜索 | 不发请求 |
| 用户拒绝授权 / 定位不可用 / 超时 | 黄色提示 + 保留当前起点 | 不发请求 |
| Nominatim 挂了或反查不出城市名 | 黄色提示"已用坐标作为起点",地图照常可查 | **仍是 HTTP 200**,`resolved=false`,起点名降级为 `我的位置(31.23,121.47)` |

坐标起点名只保留 2 位小数(约 1 km),避免 GPS 抖动每次都造出一个新 `origin_city` 把库切碎。
城市搜索(阶段1a 已有)同步做了健壮化:空输入给提示不发请求;**"没搜到"不再当错误**,
提示里带上当前起点并保持地图/已渲染 pin 不变,数据源真挂了才走红色错误框。

**种子数据**(`services/seed_data.py`):OSM 国内 `piste:*` / `sport=*` 稀疏(上海两段实测
滑雪场 1 条 / 运动 6 条),按神朱决策**种子垫底**。共 **49 条**人工补录目的地
(**滑雪场 28** + **运动 21**),滑雪场含崇礼的万龙/云顶/太舞/富龙/长城岭、北京周边的
南山/军都山/怀北/盘山、东北的北大湖/松花湖/亚布力/长白山/帽儿山、西北的将军山/可可托海/
丝绸之路,以及神农架/西岭雪山/大明山与首钢滑雪大跳台、广州融创/上海耀雪/太仓阿尔卑斯/
绍兴乔波等室内雪场;运动覆盖北京奥运场馆、上海国际赛车场、青岛帆船与金沙滩、观澜湖高尔夫、
蜈支洲岛潜水、阳朔攀岩、古龙峡漂流、妙峰山与草原天路骑行等。
坐标逐条用 Nominatim 正向编码取回、再用逆编码
核对所在区县(2026-09-10);简介是**静态文案**,所以种子**永远不会触发 LLM 调用**
(`fill_missing_intros` 只补 `intro` 为空的行),没有额度成本。

关键机制:

* **不新增列**:来源标注落在 `Place.tags["source"]="种子"`,`db.models.place_source()`
  再派生出统一的 `source` 字段(`种子` / `OSM`)给 API 与前端;存量库无需迁移,
  没有该键或写的是 `survey` 之类 OSM 原始值的一律算 `OSM`。
* **合并去重**:去重键 = **名字 + 坐标** —— 名字相等或互相包含(短名 ≥3 字)且大圆距离
  ≤ `DEDUPE_RADIUS_KM=2.0` 即视为同一地,**OSM 优先**、种子只垫缺口;种子列表内部同样去重。
* **两条路径都合并**:抓取路径与**读库路径**(`ensure_seeded`,幂等且纯本地)都会补种,
  所以 TASK-1b 抓好的存量库**不必重抓 Overpass** 也能拿到种子;补种不动 `fetched_at`。
* **分类自洽**:种子没有 OSM id,分类完全由 tags 决定,`validate()` 逐条核对
  `categorize(tags) == 声明的 category`(运动类 tag 刻意避开 ski/piste 等滑雪线索词,
  否则会被优先级抢去"滑雪场")。
* **开关**:环境变量 `WHERE2GO_SEEDS`,线上**默认开**;`0/false/no/off/关/关闭` 关闭。
  `backend/conftest.py` 里整套单测默认关掉,既有 87 个用例的条数断言因此零改动。

```bash
python -m services.seed_data --validate            # 自检:分类/坐标/简介/重名(cd backend 后运行)
python -m services.seed_data --list                # 按分类打印全部 49 条
python -m services.seed_data --city 上海           # 给该城市已入库分段补种(只读本地库,不触网)
python -m services.seed_data --city 北京 --band 50_100 --show 5
WHERE2GO_SEEDS=0 python -m services.place_loader 上海 50_100   # 关掉种子跑一遍
```

```python
from services import seed_data
from services.seed_data import attach_seeds, load_seeds, seeds_in_band

seeds = load_seeds(categories=["滑雪场"])          # 关掉开关时返回 []
in_band = seeds_in_band(seeds, 39.9042, 116.4074, require_band("50_100"))
fresh = attach_seeds(osm_rows, in_band)            # 只返回 OSM 没覆盖的那些
```

新增/变化的 API:

| 路由 | 说明 |
|---|---|
| `GET /api/geocode/reverse?lat=&lng=&zoom=` | 浏览器 GPS → 起点城市;**失败也返回 200**(`resolved=false` + 坐标起点) |
| `GET /api/places?...` | 响应多 `seeded`(本次补种条数)与 `counts_by_source`(`{"OSM": n, "种子": m}`);每条 Place 多 `source` 字段 |
| `GET /api/places/meta` | 多 `seeds`(`enabled` / `total` / `by_category` / `env`,前端图例用) |

前端:起点行新增「📍 我的位置」按钮与常见城市候选(`datalist`);新增黄色**轻提示**框
(与红色错误框分开)承载"定位被拒""城市没搜到""已用坐标作为起点"等可恢复情况;
popup 脚注按来源分流 —— 种子数据显示"来源:**人工种子数据**(OSM 国内覆盖不足,坐标人工核对)",
OSM 数据仍显示 `osm_type/osm_id`;状态栏报 `人工种子 N 条(本次补种 M 条)/ OSM K 条`,
图例报种子总量与分类分布;Leaflet 未加载时定位/搜索也只给提示、不产生 console 报错。

## 阶段2a:路线服务 + 费用估算 + deep-link(TASK-2a)

**口径**(`services/routes.py`,规格见 `docs/STAGE2-PLAN.md` 第 2/4 节):一次 `plan_routes()`
给出三种方式的**时间 + 费用**对比,每条路线形状统一 ——
`{mode, label, emoji, duration_min, cost_cny, distance_km, geometry, kind, degraded, source, note, links}`。

| 方式 | 出现条件 | 时间 | 费用(估算) | `kind` |
|---|---|---|---|---|
| 驾车 | 始终出现 | OSRM **真实**路网(含 geometry 折线) | 油费 8L/100km × 7.5 元/L + 高速占比 0.7 × 里程 × 0.5 元/km | `real` |
| 铁路 | 直线距离 ≥ **100 km** | 直线 × 1.2 / 220 km/h + 110 min 候车接驳 | 计费里程 × 0.45 元/km,20 元起步价下限(二等座) | `estimate` |
| 飞机 | 直线距离 ≥ **300 km** | 直线 × 1.1 / 780 km/h + 210 min 值机接驳 | 计费里程 × 0.6 元/km + 100 元机建燃油(经济舱) | `estimate` |

出现阈值与耗时系数**沿用 POC** `app/api/discover.py` 的 `_est_mode`(单测直接对拍同一函数,
避免两处口径漂移)。费用系数集中在 `services/routes.py` 开头一段常量
(`FUEL_L_PER_100KM` / `TOLL_CNY_PER_KM` / `RAIL_CNY_PER_KM` / `FLIGHT_CNY_PER_KM` …),
日后换真实票价/实时路况只改那一段;`cost_coefficients()` 与 `mode_rules()` 把当前系数与算式
原样吐给前端做「估算」标注,不必写死在页面里。每条 `note` 都带 `估算·非实时·以官方为准`
—— 不抓 12306/OTA 实时价、不代订(ADR-004/ADR-007)。

**OSRM 失败只降级、不 500**:公共实例繁忙、两点不在同一路网(跨海)、坐标离路网太远时,
驾车条目变成 `degraded=true`、`kind=estimate`、时长/费用/geometry 为 `null`,`note` 说明原因,
**导航 deep-link 照给**(跳转不依赖 OSRM);铁路/飞机不受影响。

**geometry 抽稀**:`data_sources.route(..., with_geometry=True)` 改传 `overview=full` +
`geometries=geojson`,并把 GeoJSON 的 `[lng, lat]` 换成 Leaflet 要的 `[lat, lng]`;服务端再按
`GEOMETRY_MAX_POINTS=1200` 等间隔抽稀(**首尾点必留**),免得长路线一次吐几千个点。
默认 `with_geometry=False`,阶段0 的返回形状与既有单测零改动。

**deep-link 全是纯函数**(不触网、不查库,中文地名按 UTF-8 百分号编码,同参数必得同 URL):

| 函数 | 目标 | 要点 |
|---|---|---|
| `amap_navigation_url()` | `uri.amap.com/navigation` | `from`/`to` = `lng,lat[,name]`(**经度在前**);缺起点就省略 `from`,高德自动用当前位置 |
| `google_maps_directions_url()` | `google.com/maps/dir/?api=1` | `origin`/`destination` = `lat,lng`(**纬度在前**,与高德相反);中国大陆不可访问,note 里写明 |
| `rail_12306_url()` | `kyfw.12306.cn/otn/leftTicket/init` | `fs`/`ts` 只带站名(没有站码映射,由 12306 自行匹配);`date` 默认今天(UTC+8) |
| `flight_ota_url()` | 去哪儿单程列表页 | **不用携程**:`flights.ctrip.com/online/list/oneway-{from}-{to}` 只认三字码,传中文城市名会被重定向回首页、查询条件丢失(2026-09-11 实测);去哪儿接受中文城市名。日后接入城市码映射只需换这个函数 |

没有地名时**不编站名**:12306/OTA 链接省略对应参数,留给用户在官方页面补全;
地图展示名降级成 `我的位置(31.2304,121.4737)`。

```python
from services.routes import cost_coefficients, mode_rules, plan_routes

plan = plan_routes(from_lat=31.2304, from_lng=121.4737,
                   to_lat=39.9042, to_lng=116.4074, to_name="北京", from_name="上海")
for r in plan.routes:                 # 驾车(real)/ 铁路 / 飞机(estimate)
    print(r["mode"], r["duration_min"], r["cost_cny"], r["kind"], [l["provider"] for l in r["links"]])
print(plan.distance_km, mode_rules()["rail_min_km"], cost_coefficients()["rail"]["formula"])
```

新增 API(`app/api/routes.py`):

| 路由 | 说明 |
|---|---|
| `GET /api/routes?from_lat=&from_lng=&to_lat=&to_lng=&to_name=&from_name=` | 三方式时间 + 费用对比;响应含 `routes[]`(每条带 `links[]`)、`distance_km`、`mode_rules`、`cost_model`、`generated_at`、`note` |

参数校验:缺参/空串/非数字/NaN/越界一律 **400 + 中文说明**(校验统一在服务层做,
所以四个坐标在 API 上声明成 `str`,免得同一个"坐标不对"一会儿 422 一会儿 400);
OSRM 不可用是**降级不是错误**,仍返回 200。`from_name` 是可选补充
(12306/OTA 链接的出发地名),`to_name` 建议带上(deep-link 文案/URL 用)。

**实测(2026-09-11,真实 OSRM)**:上海 → 北京 直线 1067.3 km;驾车 1196.4 km / 821 min /
1137 元(费用估算)、折线抽稀到 1200 点,整条请求 `elapsed_s ≈ 2.1`;铁路 459 min / 576 元
(12306 京沪高铁二等座公布价 553 元,估算偏差约 4%)、飞机 300 min / 804 元。
上海 → 太平洋中一点(无路网):驾车 `degraded=true`、数字为 `null`,铁路/飞机照常,HTTP 200。

## 阶段2b:前端路线面板 + 地图画线(TASK-2b)

**口径**:**纯前端**改造,只动 `app/static/index.html`(单文件原生 JS、Leaflet 走 CDN、无构建步骤);
后端逻辑与 API 形状**零改动** —— 阈值、系数、note 文案、deep-link 全部仍是阶段2a
`services/routes.py` 的唯一出处,页面只负责取数、展示、画线与降级提示
(规格见 `docs/STAGE2-PLAN.md` 第 3 节 1/2/3/5 条;第 4 条「收藏路线」是 TASK-2c)。

**打开面板**:点目的地 pin(popup 打开)→ 地图右上角浮出「🧭 路线对比」侧栏
(`<aside id="routePanel">`,CSS 默认 `display:none`,不占地图;窄屏 ≤680px 降级成地图下方的
普通卡片)。popup 里另留一个显式入口按钮「🚗 路线对比」,便于重开与键盘操作。面板调
`GET /api/routes?from_lat=&from_lng=&to_lat=&to_lng=&to_name=&from_name=`
(起点 = 当前 origin、终点 = 该 Place 坐标,`URLSearchParams` 自动百分号编码中文地名),
后端按距离阈值给几种方式就渲染几张卡片(上海→北京三张;50km 内通常只有驾车一张)。

**卡片**:emoji 图标 + 方式名 + 「真实 / 估算」徽标(按 `kind`,`real` 青、`estimate` 琥珀)+
OSRM 降级时额外的「数据源不可用」灰徽标(`degraded`,时长/里程为 `null` 时**不瞎估**)+
时长 / 费用 / 里程三格数字(`fmtDuration` 把分钟折成「X 小时 Y 分」、空值一律显示 `—`
而不是 `¥0`)+ 一行 `note`(自带 `估算·非实时·以官方为准`)+ 跳转按钮区。
面板脚注给出起终点直线距离、**生成时间**(`generated_at` 统一显示成 `YYYY-MM-DD HH:MM UTC`,
解析不了就原样显示)、后端 `cost_model.disclaimer` 与本次规划耗时 `elapsed_s`。

**画线**(独立图层 `routeLayer`,**先清后画 → 同一时刻地图上只有一条线**):

| 方式 | 线型 | 几何 |
|---|---|---|
| 驾车 | 青实线 `#0f766e` / 5px | 后端 OSRM `geometry` 折线;前端再按 `ROUTE_MAX_POINTS=600` 等间隔抽稀(**首尾必留**,坏点丢弃),后端已按 `GEOMETRY_MAX_POINTS=1200` 抽过一道 |
| 铁路 | 蓝虚线 `#2563eb`(`dashArray 10 8`) | 起点 → 终点示意直线 |
| 飞机 | 紫点虚线 `#7c3aed`(`dashArray 2 8`) | 二次贝塞尔弧(`arcPoints`,28 段、垂直偏移 12%),像航段而不是铁轨 |

铁路/飞机**没有真实轨迹数据**(免费源无班次/航路),所以明确画成**示意线**:图例文案
(`routePanelLegend`)与线的 tooltip 都写明「示意线,非真实轨迹」;OSRM 降级(`geometry=null`)
时驾车也退回示意线。点卡片换方式即换线,并 `fitBounds` 把整条路线收进视野(`maxZoom:12`);
点 ✕ 或按 Esc 关面板清线;换 band、换分类、重抓、换起点(`applyOrigin`)一律
`closeRoutePanel()`,免得图上留着以上一个起点为前提的旧路线。

**跳转**:每卡片按后端 `links[]` 渲染 `<a class="jump" target="_blank" rel="noopener noreferrer">`
(高德 / Google 导航、12306 车票查询、去哪儿机票),URL **一律用后端 deep-link 纯函数生成的
`link.url`**,前端不自己拼参数;`link.url` 过 `esc()` 再进 `href` 防注入,`link.note` 当 `title`
(如「Google 在中国大陆不可访问」「高德按 GCJ-02 解析,带地名可纠偏」)。点链接不会误触发
选中卡片/换线(事件委托里先把 `#routePanel a` 排除)。

**健壮性**:加载中 / 请求失败 / 后端返回空路线 / 起点或目的地坐标缺失,都在面板内给**中文可见
文案**(失败额外带「重试」按钮),不 `alert`、不抛 JS 错误、不影响地图与目的地列表;
快速连点多个 pin 用**递增 token** 丢弃过期响应,不串台;同一「起点 + 目的地」的结果按
`ROUTE_CACHE_LIMIT=40` 条做 FIFO 缓存(点回同一个 pin 秒开、不重复打 OSRM),换起点即清空。
卡片与链接都是运行时 `innerHTML` 生成的,统一走 `document` 事件委托(**无内联 `onclick=`**),
并支持键盘:Tab 聚焦卡片、Enter/空格选中画线、Esc 关闭面板清线。

**零回退**:阶段1a/1b/1c 的分类着色 pin、popup 简介卡片、band/分类切换、城市搜索、
「📍 我的位置」、「补简介」按钮与 Leaflet CDN 挂掉时的降级提示全部原样保留
(`test_frontend_routes.py` 里有一份专门的 id/函数清单断言守着)。

**测试**(`test_frontend_routes.py`,23 个用例,全 mock 不触网):页面没有构建步骤、也无需浏览器,
所以做三类**离线**断言 ——
① **DOM / JS 面**:面板容器与控件 id、卡片渲染 / 画线 / 格式化函数名都在;跳转是
`<a target="_blank" rel="noopener noreferrer">`;无内联 `onclick=`、无 `alert`/`document.write`;
面板默认隐藏、只在打开时 `display:block`;`drawRouteLine` 里「先清后画」的顺序也被断言。
② **前后端契约**:用替身 router 真跑一遍 `app.api.routes.list_routes`(上海→北京,三方式齐全 +
3000 点长 geometry),把面板那段 JS 里引用的 `data.*` / `route.*` / `link.*` 字段名逐个对回
真实响应的键 —— 后端改字段名而前端没跟上(或反过来)当场红;同时校验前端抽稀上限不比后端
`GEOMETRY_MAX_POINTS` 松、`ROUTE_MODE_*` 与 `KIND_BADGE` 覆盖后端所有 `mode`/`kind` 取值。
③ **零回退**清单(见上)。装了 `node` 时再加一条 `node --check` 对内联脚本做**语法**校验
(没装则 skip)。

## 用法示例(阶段0 数据源)

```python
from data_sources import geocode, nearby_places, reverse, route

origin = geocode("北京")                     # {'lat': 39.9057136, 'lng': 116.3912972, ...}
here = reverse(39.9042, 116.4074)            # 当前位置地名
places = nearby_places(origin["lat"], origin["lng"], 50_000,
                       {"tourism": "attraction"}, limit=20, require_name=True)
leg = route((origin["lng"], origin["lat"]), (places[0]["lng"], places[0]["lat"]))
# {'distance_km': 2.678, 'duration_min': 5.2}
```

## 真实 API 注意点(2026-09-08 实测)

* **OSRM**:`distance` 是米、`duration` 是秒,坐标顺序为 `lng,lat`;失败时 HTTP 仍可能
  200,需看 `code != "Ok"`。备选实例 `https://routing.openstreetmap.de/routed-car`
  实测结果一致,可用 `WHERE2GO_OSRM_ENDPOINT` 或 `endpoint=` 切换。
* **Nominatim**:响应里 `lat`/`lon` 是**字符串**;必须带可识别 `User-Agent`
  (`Where2Go-POC/0.1 (dev)`),官方政策 ≤1 次/秒,客户端已内置节流。
* **Overpass**:公共实例经常返回 `504 + HTML`("The server is probably too busy")。
  实测 `overpass-api.de` 繁忙时 `z.overpass-api.de`、`maps.mail.ru` 仍可用,因此
  客户端做了**端点链 + 重试 + 退避**降级(`used_endpoint` 可查实际服务方);
  `overpass.osm.ch` 实测无数据、`overpass.private.coffee` 数据陈旧数月,均未纳入默认链。
  另外 `around` 的结果**不按距离排序**,way/relation 需 `out center` 才有坐标,
  部分 POI 没有 `name` —— 解析层已按大圆距离重排、兼容 `center`、名字多级兜底。
  入库还需要 OSM 身份做防重,故解析层支持 `with_id=True`(附 `osm_type`/`osm_id`);
  该参数**默认关闭**,POC 的返回形状与既有断言不受影响。
  阶段1b 的分组并集查询(`nearby_places_grouped`)一次请求里放多段 `(...); out center N;`,
  公共实例繁忙时单次可达 100s+,故客户端超时按次放宽到 150s(仅批量冷启动路径,
  交互路径 `nearby_places` 仍是 ≤20s)。
* **LLM(默认 Qwen / 百炼 token-plan)**:默认 Provider = `qwen`,走百炼个人 TOKEN 的
  **token-plan 入口** `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`
  (OpenAI 兼容 `POST /chat/completions`),模型 `qwen3.8-max`,key 取环境变量
  `ALIBABA_TOKEN_PLAN_API_KEY`(该入口 **22:00-08:00 半价**,神朱 2026-09-10 拍板;夜班执行器
  的重活都安排在该窗)。⚠️ 必须用 token-plan 入口的 compatible-mode,**dashscope 官方入口**
  (`https://dashscope.aliyuncs.com/compatible-mode/v1`)对该 key 实测 `401 invalid_api_key`。
  `DEEPSEEK_API_KEY` 仍在注册表里作备选(实测单条约 1.2s,6 并发回填 378 条约 90s、0 条降级)。
  换供应商只需设 `WHERE2GO_LLM_PROVIDER` / `WHERE2GO_LLM_BASE_URL` /
  `WHERE2GO_LLM_MODEL` / `WHERE2GO_LLM_API_KEY`,调用方代码不动。
* **实测数据量(2026-09-09,上海)**:阶段1a 的 4-tag 抓取为 `50_100` 141 条 /
  `100_200` 237 条,首抓约 9-42s;阶段1b 六组并集重抓 `50_100` → 环内 **226 条**
  (新增写入 132 行),分类分布 小城人文美食 134 / 自然风光 85 / 运动 6 / 滑雪场 1,
  四分类在真实数据上都取到了(滑雪仍稀疏,与 `docs/STAGE1-PLAN.md` 第 3 节判断一致,
  种子垫底是 TASK-1c)。该次抓取撞上公共实例繁忙,含端点降级重试共约 9 分钟;
  二次读库仍约 0.01s、零网络请求。
* **LLM 简介实测(2026-09-09)**:上海两段共 463 条 Place 全部生成简介,0 条降级
  (378 条 + 重抓后新增 85 条,6 并发各约 90s / 20s);简介 15-47 字,平均 28 字。
* **OSM 国内小众分类覆盖不足**:滑雪/运动类 tag 在国内数据稀疏(实测上海两段内
  滑雪场 1 条 / 运动 6 条),已由阶段1c 的 49 条人工种子数据垫底(`services/seed_data.py`),
  见 `docs/STAGE1-PLAN.md` 第 3 节。
* **OSRM geometry(阶段2a 实测)**:画线要 `overview=full` + `geometries=geojson`,响应里
  `routes[0].geometry` 是 GeoJSON `LineString`(**经度在前**),需换成 Leaflet 的 `[lat, lng]`;
  长路线点数很大(上海→北京实测 6647 点),故服务端先抽到 1200 点再下发。
  跨海/离路网太远的两点 OSRM 返回 `code != "Ok"`(HTTP 仍可能 200),按**降级**处理。
* **deep-link 口径(2026-09-11 实测)**:高德 URI API 的 `from`/`to` 是 `lng,lat,name`
  (经度在前)、Google `maps/dir/?api=1` 的 `origin`/`destination` 是 `lat,lng`(纬度在前),
  两者顺序相反别搞混;12306 `leftTicket/init?linktypeid=dc&fs=&ts=&date=&flag=N,N,Y`
  只带中文站名即可打开查询页;携程机票列表页只认三字码(中文名会被重定向回首页),
  故 OTA 链接改用去哪儿。坐标是 WGS84,高德按 GCJ-02 解析,国内可能有百米级偏移
  —— 带上地名时高德可按名字纠偏,note 里已如实标注。
* **Nominatim 逆地理编码(阶段1c 实测)**:`zoom=10`(区县级)时中文 `display_name` 形如
  `浦东新区, 上海市, 200120, 中国`,由细到粗逗号分隔;`city_from_display_name` 去掉国家名与
  纯数字邮编后取第一个以 市/州/地区/盟 结尾的片段,挑不出城市就降级成坐标起点(不报错)。
  逆编码同样受 1 req/s 节流与公共实例限流影响,所以前端把它当**可失败**路径处理。

## 范围

阶段0 = 数据获取层;阶段1a 增加存储(SQLite)、入库编排、检索 API 与地图前端;
阶段1b 增加四分类优先级归类去重、LLM 一句话简介(按 POI 缓存)与 popup 卡片;
阶段1c 增加浏览器"我的位置"定位/换城健壮化与滑雪/运动人工种子数据(49 条,来源标注);
阶段2a 增加多方式路线服务(驾车 OSRM 真实 + 铁路/飞机估算)、费用估算系数、
`GET /api/routes` 与 deep-link 纯函数(**仅后端**);
阶段2b 增加**前端路线面板**(点 pin → 多方式卡片 + 时长/费用/时效标注 + 地图画线 +
deep-link 跳转,**纯前端**,后端不动)。
**尚不含**:路线收藏 `Collection` 表与「收藏路线」按钮(TASK-2c,`docs/STAGE2-PLAN.md`
第 3 节第 4 条)、用户系统、住宿 M3 与预订聚合 M4;公交/大巴等复合方式(免费源无班次数据,见
`docs/STAGE2-PLAN.md` 第 6 节风险 2);铁路/飞机的**真实轨迹**画线(免费源无航路/线路几何,
现为起终点示意线)。
