# TASK-25 契约:境外模式下地理编码仍走高德 → 定位到国内同名 POI(神朱 2026-10-13 直报 bug)

> 本契约**逐字执行**。只读清单(≤6 处,读完立即写码,禁止浏览式探索仓库):
> ①`backend/services/place_loader.py`(geocode_with_fallback L279-310 / amap_geocode L237 / photon_geocode L271 /
> resolve_origin_with_source L341-373 / reverse_with_fallback L376-406 / default_reverse_geocoder L409 /
> resolve_reverse_origin_with_source L460-500);
> ②`backend/app/api/places.py`(geocode_city L807-895 / reverse_geocode L898-929 / _region_override L257 /
> _cached_region L271 / _region_write_back L277 / ORIGIN_CACHE_GEOCODERS L124);
> ③`backend/services/region_gate.py`(全文 150 行:detect_region / detect_region_coords / classify_country_code / 常量);
> ④`backend/data_sources/nominatim.py`(geocode/reverse 签名与 include_country/include_address opt-in 口径);
> ⑤`backend/data_sources/photon.py`(同上);⑥`backend/test_region_gate.py`(替身套路:替换模块属性注入假 geocode)。
> 写码硬约定见 AGENTS.md(不新增依赖、全 mock 测试不触网、只 commit 不 push、改 Python 后重启 uvicorn)。

## 0. 实测依据(2026-10-13 22:10 CST 主会话实测,**勿重测**)

**症状(神朱原话)**:「勾选了境外模式搜索了几个境外城市/地名还是定位上海」。

**实测 ①:`/api/geocode?city=<境外名>&region=overseas` 的坐标仍由高德解析**(region 只是个标签):

| 输入 | 现状返回 geocoder | 现状返回 name | 坐标 |
|---|---|---|---|
| 因特拉肯 | **amap** | 四川省成都市双流区因特拉肯(东北门) | 30.482,104.100 ❌ |
| 皇后镇 | **amap** | 河南省安阳市林州市皇后镇 | 35.880,113.724 ❌ |
| 巴黎 | **photon** | 巴黎, 高雄市, 臺灣 | 22.656,120.296 ❌ |
| 富良野 | photon | 富良野, 富良野市, 北海道, 日本 | 43.347,142.392 ✅ |

**实测 ②:Nominatim(`accept-language=zh-CN` + `addressdetails=1`)对同批中文名 8/8 全中**:
因特拉肯→cc=ch(46.6855,7.8585)、皇后镇→cc=nz(-45.0322,168.6610)、吉隆坡→cc=my(3.1517,101.6942)、
新加坡→cc=sg(1.3571,103.8195)、巴黎→cc=fr(48.8535,2.3484)、富良野→cc=jp(43.3469,142.3917)、
札幌→cc=jp(43.0686,141.3509)、新西伯利亚→cc=ru(55.0288,82.9227)。

**实测 ③:OriginCache 里已有 13 行「amap + region 空串」的脏缓存**,其中确认错配的:
`因特拉肯→成都双流`、`皇后镇→安阳林州`、`吉隆坡→凯里`、`新加坡→长武县`、`北海道→香港油尖旺北海街`、
`巴黎→高雄`、`札幌上海→上海市`、`新西伯利亚上海→上海市`、`新西伯利亚新西伯利亚上海→上海市`。
(后三行是**顶栏输入框被挤成 22px**导致默认值「上海」删不掉、用户输入被拼接而成——该 CSS 病根已由
commit `8e6895d` 修掉:输入框 22px→245px、`locate<->band` 重叠消除;本条只治解析链与脏缓存。)

**结论**:境外模式下**必须绕开高德**(高德只服务境内,且对境外名会 status=1 成功吸附国内同名 POI——
不能用它的结果判境内外,见 region_gate docstring);境外正向链 = **Nominatim 主 → Photon 兜底**(与
`region_gate.detect_region` 同向,理由相同:Photon 对中文境外名会错定位到国内)。

## 1. 落地契约

### 1.1 `backend/services/place_loader.py`:新增境外正向链 + region 参数贯通

- 新常量:`GEOCODER_CHAIN_OVERSEAS = f"{NOMINATIM_SOURCE}/{PHOTON_SOURCE}"`(境外链报错时用的源名)。
- 新函数 `def geocode_overseas(city: str) -> tuple[dict[str, Any], str]`:
  - 链:**Nominatim 主(`ds_geocode`,即 `data_sources.geocode`)→ Photon 兜底(`photon_geocode`)**,
    **不打高德**;
  - **国家码守卫(硬要求)**:每一腿的结果都要判国家码——Nominatim 腿用
    `ds_geocode(cleaned, include_country=True)` 取 `country_code`;Photon 腿用
    `photon_geocode(cleaned)` 后再单独调 `region_gate` 已用的同口径取值(`ds_photon_geocode(..., include_country=True)`
    的 `countrycode`)。**若某腿判出的国家码归类为 `domestic`(即 `region_gate.classify_country_code(code) == REGION_DOMESTIC`),
    视为该腿错配、当失败降级到下一腿**(这是「巴黎→高雄」「因特拉肯→成都」的直接防线);
    国家码为空 = 该腿失败(与 region_gate 口径一致,不猜);
  - 两腿都失败 → 抛 `DataSourceError(GEOCODER_CHAIN_OVERSEAS, "境外地理编码两个源都失败,无法解析 <city>:<两腿原因>")`;
  - 宽 `except Exception` 只在中间腿(理由照 `geocode_with_fallback` docstring);返回形状与
    `geocode_with_fallback` **完全一致**:`({"lat","lng","display_name"}, "nominatim"|"photon")`。
  - ⚠️ 为避免重复网络请求:本函数**一次调用即拿到坐标 + 国家码**(用 `include_country=True` 的那次结果直接
    作为返回值),不要再调一次 `region_gate.detect_region`。
- `geocode_with_fallback(city, *, region: Optional[str] = None)`:加 region 关键字参数
  (None/domestic → 现状高德主链**零改动**;`REGION_OVERSEAS` → 直接 `return geocode_overseas(city)`)。
- `default_geocoder(city, *, region=None)` 与 `resolve_origin(city, *, lat, lng, geocoder, region=None)`、
  `resolve_origin_with_source(city, *, lat, lng, geocoder, region=None)`:同样加 region 透传
  (**默认值 None = 现状行为,既有调用点与既有单测零改动**)。给了 lat/lng 的坐标退化路径不受 region 影响。
- 逆向(「我的位置」):**`reverse_with_fallback(lat, lng, *, zoom, region=None)`** 加 region 参数——
  `REGION_OVERSEAS` 时**跳过高德腿**,走 **Photon reverse 主 → Nominatim reverse 兜底**
  (与 `region_gate.detect_region_coords` 同向);国内坐标高德腿照旧。
  `default_reverse_geocoder(lat, lng, zoom, *, region=None)` 与
  `resolve_reverse_origin(lat, lng, *, reverse_geocoder, zoom, region=None)`、
  `resolve_reverse_origin_with_source(..., region=None)` 同步透传(注入替身的既有单测口径不变)。
- 境外行 `display_name` 沿用现状拼装(Nominatim/Photon 已给中文);**不做 GCJ-02 转换**(境外 WGS-84 直用)。

### 1.2 `backend/app/api/places.py`:`region` 真正驱动解析链 + 缓存不再串轨

- `geocode_city`:
  1. `override = _region_override(region)` 后,**先确定本次生效 region 的判定顺序**:
     `override`(给了就用,且**不再调 detect_region**)→ 缓存行 `_cached_region(row)` →
     `region_gate.detect_region(cleaned)` 现场判定;
  2. **缓存命中新增两道一致性闸**(修脏缓存的关键):
     - 若本次生效 region 与 `_cached_region(row)` 不一致(**含 cached 为空串/None 的情况**)→ **视为未命中**,
       走现场解析并用新结果 `upsert_origin_cache(...)` 覆盖该行(带 region);
     - 若 `cached.geocoder == "amap"` 而本次生效 region 是 `overseas` → 同样**视为未命中**
       (高德不可能正确解析境外城市,这是 §0 实测 ③ 的 13 行脏缓存的自愈路径);
  3. 现场解析改成 `place_loader.resolve_origin_with_source(cleaned, region=resolved_region)`
     —— **这是本条的核心修复**:overseas 时坐标来自 Nominatim/Photon,不再是高德吸附的国内 POI;
  4. `override` 情形下**也要写缓存**(现状是 override 不写:但 override 现在会改变坐标来源,
     写回才能让同一城市二次查询零网络且坐标正确;`region=` 写生效值),`detector=none` 仍不写 region(留空串下次重试);
  5. 响应键与形状**逐键零变化**(`origin/geocoder/region/bands/segments`);非法 region 仍 400 中文;
     三腿/两腿全失败仍 400 中文(境外链的报错消息带上两腿原因)。
- `reverse_geocode`:先 `detected = region_gate.detect_region_coords(lat, lng)`,再把
  `region=detected["region"]` 透传给 `resolve_reverse_origin_with_source(...)`
  (**境外 GPS 不再问高德逆地理**);响应键零变化,失败仍 200 + `resolved=false`。
- `ORIGIN_CACHE_GEOCODERS` 不变(仍含 amap/photon/nominatim)。

### 1.3 前端(`backend/app/static/index.html`)最小改动

- **不改** TASK-21/23 已落地的 region 透传逻辑(`regionQuery()` / `isOverseasTrack()` 照旧)。
- 顶栏「境外模式」label 的 `title` 文案补一句:勾上后**坐标解析改走 OSM 系(Nominatim/Photon),不再用高德**
  (让神朱这类用户明白开关到底改了什么)。只改 title 字符串,不改结构、不加 key。
- ⚠️ 顶栏 CSS(commit `8e6895d` 的 `.field-city`/`.field-narrow`/`.inline{flex-wrap:wrap}`/输入框
  `min-width:150px`)与 `test_frontend_routes.py` 末尾两条布局断言**不得回退**。

### 1.4 脏缓存自愈(不写一次性脚本)

- **不新增 tools/ 脚本、不手动 DELETE**:§0 实测 ③ 的脏行由 §1.2 的两道一致性闸在下次搜索同名城市时
  自动覆盖(因特拉肯/皇后镇/巴黎… 再搜一次即自愈)。
- 执行器真机验收时必须验证这一点(见 §3)。

## 2. 测试契约(全 mock,≥20 新增)

新文件 `backend/test_geocode_overseas.py`(替身套路照 `test_region_gate.py`:monkeypatch 模块属性注入假
geocode/reverse,断言**调用次数**与**参数**;autouse no_network 兜底):

1. `geocode_overseas`:Nominatim 命中(境外国家码)→ 返回 nominatim 坐标、**高德零调用**;
   Nominatim 抛错 → 降 Photon;Nominatim 返回**国内国家码(CN)→ 视为错配降级**(巴黎→高雄 那条防线);
   Photon 也国内/为空 → 抛 DataSourceError 且消息含两腿原因(≥6 例);
2. `geocode_with_fallback(region=...)`:None/domestic → 高德腿被调(现状零变化);overseas → 高德零调用(≥2 例);
3. `resolve_origin_with_source(region="overseas")` → origin 四字段形状不变、坐标来自境外链;
   给了 lat/lng 时 region 不影响(坐标退化路径)(≥2 例);
4. API `/api/geocode?region=overseas`:**缓存行是 amap 且 region 空 → 视为未命中并覆盖写回**
   (断言 upsert 被调、新行 geocoder∈{nominatim,photon}、region=overseas)(≥3 例);
   缓存行 region 与本次生效 region 不一致 → 重新解析;一致 → **零网络**直接命中(≥2 例);
   响应键集与既有 LEGACY 断言逐键零变化;非法 region 400 中文(≥2 例);
5. 自动判定路径:不带 region 参数、`detect_region` 判出 overseas → **也走境外链**(不只 override 才生效)(≥2 例);
6. 逆向:`/api/geocode/reverse` 对境外坐标 → 高德 reverse 零调用、Photon/Nominatim 被调;
   境内坐标 → 高德照旧;失败仍 200 + resolved=false(≥3 例);
7. 前端静态断言(追加到 `test_frontend_routes.py`):境外模式 label 的 title 含「Nominatim」或「OSM 系」字样;
   **§1.3 的 CSS 布局断言仍在**(不许回退)(≥1 例)+ node --check。

既有基线(TASK-23 + 顶栏 CSS 修复后的实际数字,**以实跑为准**,当前 1572 passed)零回归。

## 3. 验收与禁止项

- `cd backend && ../.venv/bin/python -m pytest -q` 全绿(只增不减);`node --check`;commit 消息带 `TASK-25`;只 commit 不 push。
- **禁止**:改 TASK-20~23 已落地的 region_gate / osm_overseas / osrm / Leaflet 双轨行为;
  改境内(高德)解析链的既有行为与响应键;改 `/api/places`、`/api/routes`、`/api/stays` 的响应形状;
  动 collections/trips/planner/assistant;新增第三方依赖;新增一次性清库脚本;回退顶栏 CSS 修复。
- 真机冒烟(**执行器做,Codex 不需要联网**):重启 uvicorn(先 `set -a && source /opt/data/.env && set +a`)→
  ①`/api/geocode?city=因特拉肯&region=overseas` → geocoder∈{nominatim,photon}、name 含「因特拉肯/Interlaken」、
  坐标 ≈(46.68,7.86)**不再是成都**;②不带 region 参数搜「皇后镇」→ 自动判定 overseas → 坐标 ≈(-45.03,168.66)
  **不再是安阳**;③二次查询同名城市 → 命中缓存零网络且坐标仍正确(验证脏行已被覆盖自愈);
  ④`/api/geocode?city=上海`(不带 region)→ geocoder=amap、region=domestic、坐标不变(**境内零回归**);
  ⑤browser_exec 真机:勾「境外模式」搜「因特拉肯」→ 地图切 Leaflet、起点名与环圈在瑞士、列表出境外 POI、0 JS error。
