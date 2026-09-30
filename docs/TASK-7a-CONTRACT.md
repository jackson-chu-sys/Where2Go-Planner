# TASK-7a 契约:环形抓取组间并行 + 地理编码持久缓存(解决「搜索/重新抓取慢」)

## 背景(2026-09-29 主会话实测,数据可信直接引用)
- 成都 50-100km 环、渐进配额 30:`nearby_places_ring` 六组**串行**整组请求,逐组直连计时 = 114.8 / 42.8 / 65.9 / 5.3 / 68.2 / 86.3s,**合计 383s**;各组 elements 仅 2~8 条 → 环形差集成本在扫描两个圆的几何,**与配额几乎无关**,减请求数才是唯一出路。
- `_ring_group_rows` 的按选择器降级也是**串行**(滑雪场组拆选择器实测 27/29/44/58s 逐条累加)。
- `/api/geocode` 每次实调 Photon(德国),**2.7~3.4s**;同一城市重复搜索重复付费。
- 对照:读库路径 0.03s。慢全在抓取/geocode 网络路径本身。

## 只读清单(先读这些再动手,禁止全仓探索)
`backend/data_sources/overpass.py`(`nearby_places_ring` / `_ring_group_rows` / `_ring_request` / `execute` / `FALLBACK_ENDPOINTS`)、`backend/data_sources/_common.py`(`build_session` / `http_json`)、`backend/db/models.py` + `backend/db/repository.py`(表与仓储写法,参考 `StayQueryCache` 与其 upsert/get)、`backend/app/api/places.py`(`geocode_city`)、`backend/services/place_loader.py`(`resolve_origin_with_source`)。

## 落地契约
### 1) 环形抓取组间并行(overpass.py)
- `nearby_places_ring` 对 groups 改为 **ThreadPoolExecutor 并行**;并发数 = `int(os.environ.get("WHERE2GO_OVERPASS_WORKERS", "3"))`,钳制 1~4;缺省行为(不设 env)= 3。
- 端点错峰:并行的 worker 依次从 `self.endpoints`(现链:overpass-api.de / z.overpass-api.de / maps.mail.ru)轮转选**起始端点**(第 i 组从 `endpoints[i % len]` 开始,失败仍走链上其余端点 + 原 2 次重试)。实现方式自选:给 `execute` 加可选 `start_index` 参数(默认 0,不改现有调用方语义),或每 worker 一个轻量副本。
- **线程安全:requests.Session 不保证线程安全** → 每个 worker 必须自己的 session(`threading.local` 或 per-worker client);不要共享 `self._session`。
- `_ring_group_rows` 的选择器降级循环同样并行(共享同一 pool 或嵌套 pool,嵌套并发 ≤2;避免总并发 >4 打死公共实例配额)。合并口径不变:合并 → `(type,id)` 去重 → 近到远取前 budget(`_merge_split_rows`)。
- **失败语义完全不变**:整组失败→拆选择器→仍全失败则抛 `DataSourceError`(带组名、不写残缺水位)。任何一组失败不得被吞成空列表。
- 结果不变:全局按 haversine 排序返回;`inner_radius_m==0` 单圆路径委托 `nearby_places_grouped` 保持原样(不并行也先不并行)。

### 2) 地理编码持久缓存(OriginCache)
- `db/models.py` 新表 `OriginCache`:字段 `city`(String 主键)/ `name` / `lat`(Float)/ `lng`(Float)/ `geocoder`(String)/ `updated_at`(DateTime, 与现有表同风格)。SQLite 旧库自动建表走现有 Base.metadata create_all 机制;若仓库有其他新表迁移先例,照抄。
- `db/repository.py`:`get_origin_cache(session, *, city) -> OriginCache | None` 与 `upsert_origin_cache(session, *, city, name, lat, lng, geocoder)`;写法对齐 `StayQueryCache` 既有函数。
- `app/api/places.py::geocode_city`:先查缓存,TTL = `int(os.environ.get("WHERE2GO_ORIGIN_CACHE_TTL_S", "604800"))`(默认 7 天);命中 → 直接返回,**响应形状与现在完全一致**(含 `geocoder` 字段原值、`bands` 组装不变,零网络);未命中/过期 → 走现有 `resolve_origin_with_source`,**仅当 geocoder ∈ {photon, nominatim}** 时写缓存(`none` 与坐标退化不写)。
- `place_loader.resolve_origin_with_source` 本体不改签名;缓存读写统一放 API 层即可,loader 内部重复 geocode 不强行接线。

### 3) 测试(全 mock,禁止触网)
- 存量 **785 passed 必须零回归**(先跑一次确认基线,再改;改动后复跑)。
- 新增 `test_overpass_ring_parallel`:monkeypatch `OverpassClient.execute`(或 `_ring_request`),记录每次调用的进入/离开时间,断言 ①峰值并发 ≥2(用时间区间重叠或计数栅栏),②总墙钟时间 < 串行累加耗时;断言端点起始轮转生效(第一次调用用 endpoints[0]、第二次用 endpoints[1]…)。
- 新增 `test_geocode_cache_hit`:mock resolve/Photon 计数——第一次调用打网络一次并写缓存;第二次 0 网络、响应一致;过期(改 TTL=0 或拨 updated_at)再走网络。

### 4) 不许动
前端 `index.html`、`services/classify.SEARCH_GROUPS` 配额、渐进目标 30/60/90 口径、intros 补简介逻辑、`nearby_places`(POC 路径)、端点链内容与顺序(只加错峰不改成员)。

## 验收(主会话复验,你自测后如不符别硬凑)
1. `cd backend && ../.venv/bin/python -m pytest -q` 全绿(≥789)。
2. commit message:`TASK-7a: 环形抓取组间并行+端点错峰 + OriginCache 地理编码持久缓存`。只 commit 不 push。
3. 真机冒烟由主会话做(新城市冷抓墙钟、geocode 二连击计时),你不用跑真网络。

## 执行纪律(R8/R9)
写码前探索 ≤12 分钟;先读 AGENTS.md;按本契约精确落地,契约没写的不要顺手改;两次迭代内未收敛就停下并在结尾报告卡点,不要无限重写。结尾输出:改动文件清单 + 测试结果 + 偏离契约之处(应为空)。
