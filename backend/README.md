# backend · 免费数据源验证层(阶段0)+ 目的地入库与地图 API(阶段1a)

对应任务:`tasks/TASK-001-data-source-poc.md`(阶段0,ADR-006:免费、无需 key 的数据源)、
`docs/NIGHTLY-QUEUE.md` TASK-1a(阶段1a:Place 入库 + 检索 API + Leaflet 地图,
规格见 `docs/STAGE1-PLAN.md` 第 2/4 节)。

## 目录

```
backend/
├─ requirements.txt              依赖(requests、fastapi/uvicorn、sqlalchemy;测试用 pytest)
├─ conftest.py                   pytest 路径引导
├─ test_data_sources.py          阶段0:三个源的纯 mock 单测(不触网,26 个用例)
├─ test_places.py                阶段1a:入库/读库/API 的纯 mock 单测(不触网,20 个用例)
├─ data_sources/                 数据获取层(阶段0,免费无 key)
│  ├─ __init__.py                统一导出 route / geocode / reverse / nearby_places
│  ├─ _common.py                 User-Agent、timeout(≤20s)、JSON 请求与中文错误
│  ├─ osrm.py                    驾车路线 → {distance_km, duration_min}
│  ├─ nominatim.py               正向/逆向地理编码 → {lat, lng, display_name}
│  ├─ overpass.py                周边 POI 检索 → [{lat, lng, name, tags}](with_id=True 时附 osm id/type)
│  └─ verify_poc.py              真实网络端到端验证脚本(联网)
├─ db/                           存储层(阶段1a,SQLite + SQLAlchemy 2.0)
│  ├─ models.py                  Place / SegmentFetch 表定义
│  ├─ base.py                    引擎与会话(懒加载;WHERE2GO_DB_URL 可覆盖库地址)
│  └─ repository.py              upsert / 按段检索 / 分类计数 / 抓取水位
├─ services/                     业务层(阶段1a)
│  ├─ bands.py                   环形距离分段定义(POC 与入库共用同一口径)
│  ├─ categories.py              OSM tags → 分类的**简化**归类(四分类优先级去重是 TASK-1b)
│  └─ place_loader.py            (城市, band) 抓取入库编排 + CLI
└─ app/                          HTTP 服务(FastAPI)
   ├─ main.py                    路由装配 + 静态页面挂载
   ├─ api/discover.py            POC 路由(阶段0,保留不动)
   ├─ api/places.py              GET /api/places、/api/places/meta、/api/geocode
   └─ static/index.html          Leaflet 地图页(阶段1a);list.html 为 POC 列表页
```

## 安装与运行

```bash
python3 -m venv .venv && . .venv/bin/activate      # 或用 uv venv .venv
pip install -r backend/requirements.txt

python -m pytest backend/ -q                       # 单测(mock,不触网;46 个用例)
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
查 Overpass(多 tag 并集,一次查完)→ haversine 收敛到 `[low, high)` 环内 → 简化归类
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
band 内 Place 按分类着色渲染 pin,点 pin 出弹窗(名称/分类/距起点/OSM id),
支持切换 band、切换分类、城市搜索与"重新抓取"。CDN 不可用时给出降级提示,API 仍可用。

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
* **实测数据量(2026-09-09,上海)**:`50_100` 段 141 条、`100_200` 段 237 条;
  首抓约 9-42s(Overpass 公共实例波动),二次读库约 0.01s。
* **OSM 国内小众分类覆盖不足**:滑雪/运动类 tag 在国内数据稀疏(实测上海两段内
  滑雪场为 0 条),种子数据垫底是 TASK-1c 的活,见 `docs/STAGE1-PLAN.md` 第 3 节。

## 范围

阶段0 = 数据获取层;阶段1a 增加存储(SQLite)、入库编排、检索 API 与地图前端。
**尚不含**:四分类优先级归类去重与 LLM 简介(TASK-1b)、种子数据与浏览器定位
(TASK-1c)、收藏 `Collection` 表、用户系统与正式路线/住宿模块(阶段 2+)。
