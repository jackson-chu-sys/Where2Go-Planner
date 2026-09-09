# backend · 免费数据源验证层(阶段0)+ 目的地入库、四分类与简介(阶段1a/1b)

对应任务:`tasks/TASK-001-data-source-poc.md`(阶段0,ADR-006:免费、无需 key 的数据源)、
`docs/NIGHTLY-QUEUE.md` TASK-1a(阶段1a:Place 入库 + 检索 API + Leaflet 地图,
规格见 `docs/STAGE1-PLAN.md` 第 2/4 节)、TASK-1b(阶段1b:四分类优先级归类去重 +
LLM 一句话简介 + popup 卡片,规格见 `docs/STAGE1-PLAN.md` 第 3 节)。

## 目录

```
backend/
├─ requirements.txt              依赖(requests、fastapi/uvicorn、sqlalchemy;测试用 pytest)
├─ conftest.py                   pytest 路径引导
├─ test_data_sources.py          阶段0:三个源的纯 mock 单测(不触网,26 个用例)
├─ test_places.py                阶段1a:入库/读库/API 的纯 mock 单测(不触网,20 个用例)
├─ test_classify.py              阶段1b:归类优先级/跨 tag 去重/检索并集/LLM 简介的纯 mock 单测(41 个用例)
├─ data_sources/                 数据获取层(阶段0,免费无 key)
│  ├─ __init__.py                统一导出 route / geocode / reverse / nearby_places
│  ├─ _common.py                 User-Agent、timeout(≤20s)、JSON 请求与中文错误
│  ├─ osrm.py                    驾车路线 → {distance_km, duration_min}
│  ├─ nominatim.py               正向/逆向地理编码 → {lat, lng, display_name}
│  ├─ overpass.py                周边 POI 检索 → [{lat, lng, name, tags}](with_id=True 时附 osm id/type;
│  │                             nearby_places_grouped = 多组 tag 并集、每组独立配额,一次请求查完四分类)
│  └─ verify_poc.py              真实网络端到端验证脚本(联网)
├─ db/                           存储层(阶段1a,SQLite + SQLAlchemy 2.0)
│  ├─ models.py                  Place / SegmentFetch 表定义
│  ├─ base.py                    引擎与会话(懒加载;WHERE2GO_DB_URL 可覆盖库地址)
│  └─ repository.py              upsert / 按段检索 / 分类计数 / 抓取水位 / 缺简介行查询
├─ services/                     业务层(阶段1a/1b)
│  ├─ bands.py                   环形距离分段定义(POC 与入库共用同一口径)
│  ├─ classify.py                四分类归类引擎:OSM tag 线索 + 归类优先级 + 跨 tag 去重 + 检索并集分组
│  ├─ categories.py              classify 的兼容导入面(阶段1a 旧名字;新代码直接 import classify)
│  ├─ intro.py                   LLM 一句话简介:Provider 注册表 + 按 POI 缓存 + 失败降级 + CLI
│  ├─ reclassify.py              存量库重归类(category 旧值/空值按四分类规则重算,离线)+ CLI
│  └─ place_loader.py            (城市, band) 抓取入库编排(去重归类 → 入库 → 补简介)+ CLI
└─ app/                          HTTP 服务(FastAPI)
   ├─ main.py                    路由装配 + 静态页面挂载
   ├─ api/discover.py            POC 路由(阶段0,保留不动)
   ├─ api/places.py              GET /api/places、/api/places/meta、/api/places/intros、/api/geocode
   └─ static/index.html          Leaflet 地图页(阶段1b:分类着色 pin + 简介 popup);list.html 为 POC 列表页
```

## 安装与运行

```bash
python3 -m venv .venv && . .venv/bin/activate      # 或用 uv venv .venv
pip install -r backend/requirements.txt

python -m pytest backend/ -q                       # 单测(mock,不触网;87 个用例)
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
* **LLM(DeepSeek)**:`POST https://api.deepseek.com/chat/completions`、模型 `deepseek-chat`,
  key 取环境变量 `DEEPSEEK_API_KEY`;实测单条约 1.2s,6 并发回填 378 条约 90s、0 条降级。
  环境里的 DashScope key(`ALIBABA_TOKEN_PLAN_API_KEY`)实测 `401 invalid_api_key`,故默认
  Provider 走 DeepSeek;换供应商只需设 `WHERE2GO_LLM_PROVIDER` / `WHERE2GO_LLM_BASE_URL` /
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
  滑雪场为 0 条),种子数据垫底是 TASK-1c 的活,见 `docs/STAGE1-PLAN.md` 第 3 节。

## 范围

阶段0 = 数据获取层;阶段1a 增加存储(SQLite)、入库编排、检索 API 与地图前端;
阶段1b 增加四分类优先级归类去重、LLM 一句话简介(按 POI 缓存)与 popup 卡片。
**尚不含**:种子数据与浏览器定位(TASK-1c)、收藏 `Collection` 表、用户系统与
正式路线/住宿模块(阶段 2+)。
