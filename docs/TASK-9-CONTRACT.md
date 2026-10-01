# TASK-9 契约：数据源全面切换高德（POI 检索 / 地理编码 / 驾车路线 / 前端地图）

> 神朱 2026-10-01 拍板（微信会话 08:47 定的口径 + 10/02 补确认）；主会话（赫沐神朱）2026-10-01 实测并定稿。
> 夜班执行器**逐字执行本文**，勿自行探索仓库、勿扩 scope。对应 STAGE1-PLAN / ADR-006 的「功能稳定后再切高德 key」正式落地。

## 0. 拍板口径（勿自行改）

1. **①POI 检索 ②前端地图瓦片 ③地理编码：全切高德**；**驾车一并切高德**（OSRM 移除后不能悬空）。
2. **Overpass 与 OSRM 彻底移除，不保留降级链、不留休眠文件**（神朱 2026-10-01 二次确认）：`data_sources/overpass.py`、`data_sources/osrm.py` **在 TASK-9c 落地后物理删除（`git rm`）**，连同 `test_*.py` 中对它们的直接引用（改为 amap 替身）。
3. **渐进配额口径完全不变**：首查 30 / 显示 15 / 「加载更多」每轮 +30、`PROGRESSIVE_STEP`、`SegmentFetch.fetch_rounds`、`/api/places` 的 `page_size/offset/more` 参数与响应形状**一律不动**（前端因此零改动）。
4. **检索必须走 v3 接口**（v5 翻页坏，见 §1.2）。
5. **Photon / Nominatim 保留为降级链**（仅当高德无 key / 超配额时回落），本次**不删、不新增功能**。
6. 拆四条执行：**9a 数据源层 → 9b POI/住宿检索链 → 9c 地理编码+驾车 → 9d 前端地图**；9d 当晚连做。TASK-8（图片/弹窗）全部排在 9d 之后。

## 1. 实测数据（2026-10-01 容器内实跑，**勿重测、勿改口径**）

### 1.1 Key 与连通
- `WHERE2GO_AMAP_KEY`（Web 服务，后端 REST）、`WHERE2GO_AMAP_JS_KEY`（JS API，前端地图）均在 `/opt/data/.env`（0600）。**JS key 调 REST 返回 `USERKEY_PLAT_NOMATCH`**（平台隔离正确，别混用）。
- 后端 REST **国内直连**（`--noproxy`）~~0.1~0.2s~~ 稳定可用；URL 前缀 `https://restapi.amap.com/v3/`。
- **QPS 限流实测**：密集连打会返回 `infocode=10021`（QPS 超限）→ 客户端必须**单进程最小请求间隔 ~0.4s** 节流（见 §3.9a）。

### 1.2 v3 vs v5（关键，决定接口选型）
| 项 | v3 | v5 |
|---|---|---|
| 翻页 | 正常（page 1↔2 无重叠，`offset`+`page` 稳定） | **坏**：`page=2` 与 `page=1` 返回**完全相同**的一页（overlap 25/25） |
| 结论 | **全部检索走 v3** | 禁用 |

### 1.3 单查询条数上限（决定分格设计）
- 实测**每个查询（around / polygon）最多只能取到 200 条**：`offset=25` 时 `page=1..8` 各返回 25 条，**`page≥9` 直接返回 0 条**（`infocode=10000`、`count` 字段仍显示 1000/600，**`count` 不可信**，别拿它当可取条数）。
- 半径上限：`radius=60000/100000` 返回的数据与 50000 一致（**radius 参数被截断在 50000**）→ band ≥ 50km 必须走 `place/polygon`。

### 1.4 分类 typecode 锚点（实测返回原文，映射表照此建）
| 大类码 | 大类名 | 实测 typecode / type 原文 |
|---|---|---|
| `080106` | 运动场馆;滑雪场 | 崇礼「太舞滑雪场/密苑云顶乐园(云顶滑雪场)」→ `体育休闲服务;运动场馆;滑雪场` |
| `080000` | 体育休闲服务 | `080113` 台球厅 · `080500` 休闲场所 · `080000` 体育休闲服务场所 |
| `110000` | 风景名胜 | `110101` 公园 · `110105` 城市广场 · `110000` 风景名胜相关;旅游景点 |
| `050000` | 餐饮服务 | `050100` 中餐厅 · `050301` 肯德基 · `050500` 咖啡厅 |
| `100000` | 住宿服务 | `100104` 三星级宾馆 · `100105` 经济型连锁酒店 |
- **禁凭记忆编造中类码**：归类规则只允许使用「本表实测过的 typecode/type 字符串」+「`types=` 大类码粗筛后在本地按返回的 `type` 字符串归类」。
- 崇礼滑雪场覆盖实测：`types=080106` 命中 太舞/雪如意/密苑云顶/林语山谷 等（正是 OSM 覆盖差、此前靠种子垫底的类别）。

### 1.5 驾车 v3 字段（**两个反直觉坑**）
`/v3/direction/driving?origin=lng,lat&destination=lng,lat&strategy=11&extensions=all`
- 可用：`route.paths[0].{distance(米), duration(秒), toll_distance(收费路段米), traffic_lights, steps[{polyline}]}`。实测 上海→杭州 `distance=175897 duration=10589 toll_distance=135514 traffic_lights=14`，`steps` 36 段。
- ⚠️ **`tolls` 恒为 `0`，`cost` 恒为 `null`（个人 key 不出过路费数值）** —— **不能用高德的过路费字段**。费用口径改为：**过路费 = `toll_distance` × 区域费率**（沿用现有东 0.45 / 中 0.40 / 西 0.35 元/km 常量），油费 = `distance` × 油耗口径不变。
- `steps[].polyline` 是 `"lng,lat;lng,lat;…"` 明文（**不是** Google 式编码折线），需解析成 `[lat,lng]` 并沿用现有抽稀口径 `GEOMETRY_MAX_POINTS`。

### 1.6 地理编码 / 逆地理
- 正编：`/v3/geocode/geo?address=杭州西湖` → `geocodes[0].{location:"lng,lat", formatted_address, province, city, district, adcode, citycode}`。
- 逆编：`/v3/geocode/regeo?location=lng,lat&extensions=base` → `regeocode.{formatted_address, addressComponent{province,city,district,adcode,township}}`。实测 杭州点 → 「浙江省杭州市西湖区灵隐街道曙光社区(浙大路)曙光新村」。
- **坐标系：高德全链 GCJ-02**。既然 POI + 地理编码 + 地图瓦片都切高德，内部自洽，**不需要坐标转换**；只有「种子数据(WGS-84)」混入时才需处理（见 §3.9d）。

### 1.7 前端 JS API 真机实测（主会话 2026-10-01，**已验通，9d 不必再摸索**）
最小页（`window._AMapSecurityConfig={securityJsCode:…}` → `<script src="https://webapi.amap.com/maps?v=2.0&key=…">` → `new AMap.Map`）在**真浏览器（CDP chrome）**里实测：
- `AMap.v = "2.0"` 正常加载；**没有出现 `INVALID_USER_SCODE`**（安全密钥已就绪并由主会话验证）；`window.__errs` **0 条**。
- `AMap.Map` / `AMap.Circle` / `AMap.Marker`（自绘 HTML content）/ `AMap.InfoWindow` **全部可用**；地图 `complete` 事件触发、渲染出 13 块瓦片。
- **瓦片 URL 实测为 `https://webrd04.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x=&y=&z=`** —— 即**官方 JS API 2.0 的底图本身就是 webrd 栅格**；所以「降级到无 key 栅格」与官方路径在底图上并无差别，只是绕过 JS API 的脚本加载。
- ⚠️ **实现坑（QA 必读）**：自绘 `content` 的 pin，点击**必须用 `marker.on("click", …)` 绑定**（AMap 在 overlay 层代理事件）；在自绘 div 上挂 `onclick`、或用 JS 合成 `dispatchEvent(new MouseEvent(...))` **都不触发**——真机 QA 要用**受信任的真实点击**（CDP 点坐标，`click_at_xy`）验证，否则会误判成「弹窗坏」。
- key 与 securityJsCode 由 `/api/map-config` 下发，**两者都不进 git、不进 `index.html`**。

## 2. 只读清单（≤5 个确切文件，读完立即开工，禁止浏览式探索）

- `backend/data_sources/_common.py` —— 错误类型（`DataSourceError`/`TransientDataSourceError`）、`build_session` 按源代理表
- `backend/data_sources/photon.py` —— 新数据源模块的写法样板（ENV 端点覆盖、`environ` 注入、解析与异常口径）
- `backend/db/models.py` —— Place/SegmentFetch/Collection 等表与列（**本任务不改表结构**）
- `backend/db/repository.py` —— `upsert_places` / `place_to_dict` / `REQUIRED_ITEM_KEYS` 的入库约定
- 本文 `docs/TASK-9-CONTRACT.md`

参考（**只读**）：`backend/services/place_loader.py`（渐进/水位/`default_fetcher` 注入点）、`backend/services/classify.py`（四分类与优先级）、`backend/services/stays.py`（住宿检索与负缓存）、`backend/app/api/places.py`（响应形状，**必须逐键保持不变**）。
9d 另读：`backend/app/static/index.html`、`backend/test_frontend_routes.py`。

## 3. 落地契约

### TASK-9a —— 高德数据源层

**新文件 `backend/data_sources/amap.py`**

常量：
```
SOURCE_NAME = "高德"
DEFAULT_ENDPOINT = "https://restapi.amap.com/v3"      # env WHERE2GO_AMAP_ENDPOINT
ENV_AMAP_KEY = "WHERE2GO_AMAP_KEY"
AUTO_MAX_RADIUS_M = 50000        # 实测 radius>50000 被截断
MAX_ROWS_PER_QUERY = 200         # 实测 page≥9 返回 0
PAGE_SIZE = 25                   # v3 固定 25 条/页(offset=25)
MAX_PAGE = 8                     # 25*8 = 200
MIN_REQUEST_INTERVAL_S = 0.4     # env WHERE2GO_AMAP_MIN_INTERVAL_S; 实测 QPS 超限 infocode=10021
```

公开函数（全部接受 `environ=` / `session=` 关键字，便于测试注入）：
- `def geocode(address: str, *, city: str | None = None, environ=None, session=None) -> list[dict]`
  → `[{"formatted_address","province","city","district","adcode","township","lng","lat"}]`；`location` 是 `"lng,lat"` 字符串需拆成 float；无结果返回 `[]`。
- `def reverse_geocode(lat: float, lng: float, *, environ=None, session=None) -> dict` → 同结构单条；无结果返回 `{}`。
- `def search_around(lat, lng, *, radius_m=50000, types=None, keywords=None, page=1, offset=PAGE_SIZE, environ=None, session=None) -> list[dict]`
- `def search_polygon(polygon, *, types=None, keywords=None, page=1, offset=PAGE_SIZE, environ=None, session=None) -> list[dict]`
  - `polygon` 收 `Sequence[tuple[float,float]]`（**lng,lat** 顺序，≥4 点）；也接受 `"119.6,29.8~120.7,30.8"` 对角线简写（实测可用），拼接时用 `;` 分隔。
  - 两者返回**归一化 POI**：`{"id","name","lat","lng","type","typecode","address","cityname","adname","distance_m"}`（`lat/lng` 为 float；`id` 为高德 POI id，如 `B023B17WWK`）。
- `def driving(origin_lat, origin_lng, dest_lat, dest_lng, *, strategy=11, with_geometry=True, environ=None, session=None) -> dict`
  → `{"distance_m","duration_s","toll_distance_m","traffic_lights","steps_n","polyline":[[lat,lng],…]}`；polyline 从 `steps[].polyline` 解析 + 抽稀（口径同 `osrm.GEOMETRY_MAX_POINTS`）。
- `def decode_polyline(text: str) -> list[list[float]]` —— 纯函数，`"lng,lat;…"` → `[[lat,lng],…]`。
- `def grid_polygons(min_lat, min_lng, max_lat, max_lng, rows: int, cols: int) -> list[list[tuple[float,float]]]` —— 纯函数，把包围盒切成 `rows×cols` 个矩形（每个矩形 4 点、`;` 顺序：左下/右下/右上/左上），**给 9b 的分格抓取用**。

错误与节流口径：
- 响应是 `{"status":"1"|"0", "infocode":"10000"|…, "info":…}`（**HTTP 码恒 200，别按 HTTP 判错**）。
- `status!="1"` 映射：`10001/10002/10003/10009`（key 无效/未开服务）→ `DataSourceError`（中文）；`10021/10019/10020`（QPS/配额）→ `TransientDataSourceError`；其余 → `DataSourceError` 带 `infocode` 原文。
- **未配 `WHERE2GO_AMAP_KEY`** → `DataSourceError("未配置 WHERE2GO_AMAP_KEY(高德 Web 服务 key)")`（上层据此回落降级链或给可见文案）。
- 网络错误/超时 → `TransientDataSourceError`。模块内节流：单进程内两次请求最小间隔 `MIN_REQUEST_INTERVAL_S`（threading.Lock + 上次时间戳，照 `photon._throttle` 写法）。
- `_common.DEFAULT_SOURCE_PROXY` 增加 `"amap": PROXY_OFF`（国内直连），`WHERE2GO_PROXY_AMAP` 可覆盖。

**新文件 `backend/services/amap_categories.py`**
- `AMAP_TYPE_GROUPS: tuple[dict, ...]`：四分类的**检索组**（与现有 `SEARCH_GROUPS` 同形，含 `category`/`group`/`budget`/`types`）：
  - 滑雪场 → `types=("080106",)`
  - 运动 → `types=("080000",)`（本地排除 `080106` 归滑雪）
  - 人文美食 → `types=("110000","050000")`
  - 自然风光 → `types=("110000",)`
- `def classify_amap_poi(poi: Mapping) -> str`：按 `typecode`/`type` 字符串 + 名称关键词判定，**优先级 滑雪 > 运动 > 人文美食 > 自然**（沿用既有语义），无法判定 → `"其他"`。
  - 规则表**只允许写实测过的 type 字符串**（§1.4）；每类至少覆盖表内 3 个锚点；明显的人工/园区设施名（如「指示牌」「大门」「停车场」）归 `其他`。
- `def dedupe_key(poi) -> tuple` → `("amap", poi["id"])`（高德 POI id 全局唯一，同实体跨大类只归一类）。

### TASK-9b —— POI / 住宿检索链切高德（**前端零改动的硬约束**）

- `services/classify.py`：`SEARCH_GROUPS` 的检索侧换成 `amap_categories.AMAP_TYPE_GROUPS`（OSM `tags` 选择器不再使用）；**优先级、去重语义、`budget` 递减/渐进配额口径全部保留**。
- `services/place_loader.py`：
  - band `low == 0` → `amap.search_around(lat, lng, radius_m=high, types=<该组大类>)`；
  - band `low > 0` → 以 band 外半径的**包围盒**经 `amap.grid_polygons` 切格，逐格 `amap.search_polygon`（每格 ≤200 条），本地 haversine 收敛到 `[low, high)`；
  - **扩格策略**：若某 band 抓回条数 < 目标配额且仍有未抓格子，按 `fetch_rounds` 递增扩格（口径与现有 `progressive_target_total` 一致：`PROGRESSIVE_STEP * (fetch_rounds+1)`）；
  - `default_fetcher(fetch_fn=...)` 注入点保持可替换（测试用替身）；`SegmentFetch` 水位语义不变（同 (城市, band) 二次查询读库零网络）。
- 入库身份（⚠️ 09:30 更正）：`Place.osm_id` 是 **`Mapped[int]`（Integer 列，见 models.py:117）**，而高德 POI id 是字符串（如 `B023B17WWK`）→ **必须哈希**：`osm_id = zlib.crc32(poi_id.encode("utf-8")) & 0xFFFFFFFF`（无符号 32 位、确定性、可重入）；原始 id 存 `tags["amap_id"]`（前端身份脚注可展示）。`osm_type="amap"`、`origin_city` 不变 → **Place 表结构零改动**；`tags` 写 `{"source":"高德","amap_id":…,"typecode":…,"type":…}`；`db.repository.place_source()` 增加「高德」来源分支（**已存量行的「种子」标注不受影响**）。幂等键 `osm_key(type,id)` 与收藏 ref_key 沿用既有口径（哈希稳定即全链自洽，**不许改 Collection 逻辑**）。
- `services/stays.py::search_stays` 换 `amap.search_around(types="100000")`；半径阶梯（5/10/30km）、负缓存（6h，`no_data`/`datasource_error`/`timeout` 三档）、批量 LLM 估价、`price_kind` 规则表**全不变**；`SOURCE_FETCH = "amap"`。
- **一次性清库脚本 `tools/amap_cutover.py`**（默认 `--dry-run` 只打印计数）：清除坐标系/来源不一致的派生行 —— `Place`、`SegmentFetch`、`Stay`、`StayQueryCache`、`OriginCache`、`PlaceRecommendation`、`PlaceDetail`、`PlaceHighlight`、`PlaceMedia`（后三张表若尚未建则跳过）；**必须保留用户数据 `Collection`/`CollectionCat`/`TripPlan`**（收藏是快照，不重算）。执行器在真机冒烟时先 `--dry-run` 报数，再 `--apply`。
- 种子数据：`WHERE2GO_SEEDS` 默认关闭不变；若启用，身份脚注仍标「人工种子数据（WGS-84 坐标，与高德底图存在 50~500m 偏移）」。

验收要点（**本节最硬的约束**）：`/api/places`、`/api/places/meta`、`/api/places/intros`、`/api/stays` 的**响应键名与形状零变化**，测试里必须有逐键断言（前端零改动的证据）。

### TASK-9c —— 地理编码 + 驾车切高德

- `services/place_loader.resolve_origin_with_source`：主链路 `amap.geocode`（`geocoder="amap"`）；失败/无 key → 回落 `photon`（保留）→ `nominatim`（保留）；返回形状、`resolved` 语义、`/api/geocode` 响应键（含 `geocoder`）**全部不变**。
- `OriginCache` **保留复用**（TASK-7a 建的持久缓存继续省高德配额）：geocoder 存 `"amap"`，TTL `WHERE2GO_ORIGIN_CACHE_TTL_S` 7 天不变；命中零网络。⚠️ TASK-7a 的 **Overpass 组间并行**部分随 overpass.py 一并移除，**OriginCache 部分保留**（若神朱后续要删，单独一条）。
- `services/routes.py` 驾车段：
  - 改用 `amap.driving`；`distance_km`/`duration_min` 用真实值，`kind="real"` 不变；
  - **过路费 = `toll_distance_m`/1000 × 区域费率**（东 0.45/中 0.40/西 0.35 元/km 常量沿用），`cost_breakdown` 增 `"mode":"amap_toll_distance"`（`toll_mode` 旧值 `osrm_refs` 退役）；油费口径不变（`WHERE2GO_FUEL_PRICE_CNY_L`、8L/100km）；
  - geometry 用解码 polyline（抽稀口径不变）；
  - **铁路/飞机估算、600km 飞行阈值、机票公布价锚定、`per_person_cny`、deep-link 全部不变**。
- `data_sources/osrm.py`、`data_sources/overpass.py` **在本条落地后物理删除（`git rm`）**（连同 `test_routes.py`/`test_data_sources.py`/`test_stays*.py` 中对它们的直接引用，改指 amap 替身）；`services/routes.py` 与 `services/stays.py` 不再 import 这两个模块。

### TASK-9d —— 前端地图切高德 JS API（Leaflet 退役）

- `index.html`：Leaflet（CDN + OSM 瓦片）整体换 **高德 JS API 2.0**（`https://webapi.amap.com/maps?v=2.0&key=…`）**—— 神朱 2026-10-01 拍板的官方路径**。
- **安全密钥已就绪**：`WHERE2GO_AMAP_SECURITY_JS_CODE` 已落 `/opt/data/.env`（0600，神朱 2026-10-01 提供）→ 前端在加载 JS API **之前**写 `window._AMapSecurityConfig = {securityJsCode: <值>}`，key 与 security code **都由 `/api/map-config` 下发**（响应形状：`{"amap_js_key","amap_security_js_code"}`），两者都**绝不写进 `index.html`**。
- **降级路径（神朱已授权，不必卡整晚；安全密钥就绪后应不再触发）**：真机实测若因缺安全密钥报 `INVALID_USER_SCODE`（或 JS key 环境不通），**当轮直接降级为「Leaflet 保留 + 底图瓦片换高德无 key 栅格」**（`https://webrd0{1,2,3,4}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}`），实现完照样 commit + 真机 QA，并在「结果」段注明「已降级为栅格瓦片，待神朱补安全密钥后升级 JS API」；**不得就此停下或留 needs_review 空过**。
- **JS key 不进 git**：新增后端 `GET /api/map-config` → `{"amap_js_key": <WHERE2GO_AMAP_JS_KEY 或 "">, "amap_security_js_code": <WHERE2GO_AMAP_SECURITY_JS_CODE 或 "">}`；前端**运行时**取 key 后动态注入 `<script>`，无 key 时页面给明确降级文案「地图未配置高德 JS key」。
  - 若实测 JS API 2.0 因缺**安全密钥**报 `INVALID_USER_SCODE`（地图/覆盖物是否受影响**必须在真机浏览器里验一发**），则进 `needs_review` 并在晨报里请神朱到高德控制台补「安全密钥」，同时把该值以 `WHERE2GO_AMAP_SECURITY_JS_CODE` 落 `.env`（**同样不进 git**）。
- 地图元素一一对应：环形范围圈 → `AMap.Circle`（外圆实线 / 内圆虚线，样式沿用现有 CSS 变量）；POI pin → `AMap.Marker` + 现有分类色/emoji 的自绘 `content`；点 pin → `AMap.InfoWindow` 承载**现有 `popupHtml()` 内容**（含「⭐收藏」「🚗路线对比」「📄查看详情」按钮，为 TASK-8b 铺路）；路线画线 → `AMap.Polyline`（驾车实线/铁路虚线/飞机弧线，弧线沿用 `arcPoints`）；`fitView` 用环圈 bounds。
- 身份脚注 `identityHtml()`：`osm_type="amap"` 的行显示「来源:**高德** · POI <id> · 坐标(GCJ-02)」；种子行加 WGS-84 偏差提示。
- 状态栏/页脚里的数据源文案（OSM/Overpass/Photon）改为高德口径；geocoder 字段值改为 `amap`（有降级时照实显示）。
- `test_frontend_routes.py`：删 Leaflet 断言，加高德断言（`/api/map-config` 调用、`AMap.` 使用、`AMap.InfoWindow`、**index.html 内不得出现 32 位高德 key 样态字符串**）。
- 真机 QA（browser_exec）：开页 hook `window.__errs` → 高德地图渲染 → pin + 环圈 → 点 pin 出 InfoWindow → 点路线出面板并画线 → 0 条 error；地图不通就报 needs_review 附控制台原文。

## 4. 验收

- `cd backend && ../.venv/bin/python -m pytest -q`：基线 **792 passed** 零回归；新增 **9a ≥ 25 / 9b ≥ 30 / 9c ≥ 20 / 9d ≥ 12**，全 mock 不触网。
- 每条一个 commit：`TASK-9a: 高德数据源层(...)` 依此类推。**夜班只 commit 不 push。**
- 真机冒烟（Python 有变先重启）：
  `cd /mnt/projects/Where2Go-Planner && set -a && source /opt/data/.env && set +a && nohup .venv/bin/python -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000 &`
  - 9b：`/api/places?origin=杭州&band=0_50` 首次抓取计时（**目标：秒级，对照 Overpass 的 125~383s**）、二次查询 `source=db` 且 0 网络、`page_size/offset/more` 与旧口径一致；`/api/stays?...` 正常。
  - 9c：`/api/geocode?city=杭州` → `geocoder=amap`、坐标正确；`/api/routes` 上海→杭州 驾车 `kind=real` 且 `duration_min ≈ 176`、`distance_km ≈ 175.9`、过路费来自 `toll_distance`。
  - 9d：浏览器 QA（见上）。
- 清库脚本：先 `--dry-run` 报数，再 `--apply`，然后重抓一个新城市做端到端。

## 5. 不许动 / 不许做

- `Place` / `Collection` / `TripPlan` 等**表结构**（只换数据来源，不改 schema）。
- 渐进配额口径、`/api/*` 响应键名、收藏/行程/住宿估价既有行为。
- `photon.py` / `nominatim.py`（保留为降级链，不删不改功能）。
- `AGENTS.md`（夜班不可写，晨报提示主会话白天同步）。
- 不扩 scope：不接高德「公交/火车票」商业接口（铁路/飞机仍走既有估算口径）、不做高德静态地图/天气、不做坐标转换服务（全链 GCJ-02 自洽）。

## 6. 更正与补充（2026-10-01 09:30，合并并发会话的实测笔记）

1. **节流与重试口径以本节为准**：`MIN_REQUEST_INTERVAL_S = 0.6`（不是 0.4s；实测连发第 3 个请求即 `10021 CUQPS_HAS_EXCEEDED_THE_LIMIT`），瞬时报错**退避重试 2s / 5s，最多 3 次**（照 `photon.py` 类级节流写法）。
2. **infocode 细分表（比 §3.9a 更全，照此实现）**：**Transient**（可重试）= `10004`（分钟超限）· `10014/10019/10020/10021/10029/10044`（QPS/日限流）· `10015/10016`（服务器繁忙）；**永久** = `10001`（key 无效）· `10002/10012/10041`（权限）· `10005`（IP 白名单）· `10009`（平台不符，即 JS key 调 REST）· `10013`（key 被删）· `20000/20001`（参数）· `40000/40002`（配额耗尽/到期）。全部映射为 `DataSourceError(SOURCE_NAME, 中文文案+infocode)` / `TransientDataSourceError`，`SOURCE_NAME="amap"`。
3. **必须用 v3**（v5 `page` 坏的）**且 radius 钳到 50000**；**单次查询深翻上限 ≈200 条**（8×25，`page=9` 返空）→ 拿不满就按 typecode 拆细 + 多边形分块，**禁止假设能取全量**。`types` 支持管道多值（`080106|110101` 实测混排可用）；`types` 与 `keywords` 建议二选一（同时给会按关键词排序偏移）。
4. **「小城古镇」组不能用村庄码**：`190106` 实测杭州周边 0 命中 → 该组改用 `keywords=古镇|老街|古城`（配 `city` 参数）。**滑雪场 = `080106`**（`080115` 实测 0 命中）；运动场馆大类 `080100`；公园广场 `110101`（归 `110000` 风景名胜大类）；餐饮 `050000`；住宿 `100000`。
5. **驾车**：`strategy` **不传**（实测传 `strategy=11` 可能返回多条 `paths`；不传更干净）；若返回多条一律取 `paths[0]`。`tolls` 恒 `0`、`cost` 恒 `null` 的口径不变（§1.5）→ 过路费仍用 `toll_distance` × 区域费率。
6. **坐标系**：高德全链 GCJ-02；存量旧行与种子是 WGS-84（偏差 ≤500m）。**瓦片切高德后新抓数据与底图自洽**；存量旧行偏差可接受（白天 refresh 重抓即消化），**种子静态数据不做坐标转换**（band 尺度 ≥5km，300m 不可见；产品化再议）。新代码内部一律高德坐标。
7. **`SegmentFetch.source` / `Stay.source` 写 `"amap"`**（列 String(16)，无迁移）；`OriginCache.geocoder` 值域扩为 `"amap"`（主）/`"photon"`/`"nominatim"`（降级），TTL 口径不动。
8. 前端 JS key 本期是否使用见队列头部通告的最终裁定（与「瓦片是否走无 key 栅格」二选一，**不许两个 key 都塞进前端**）。
