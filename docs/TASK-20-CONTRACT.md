# TASK-20 契约:region_gate 境内外判定服务(境外双轨 P1)

> 神朱 2026-10-10 拍板「使用推荐选项」(docs/OVERSEAS-PLAN.md §2/§4.2)。本契约**逐字执行**,禁止自行探索仓库。
> 只读清单(读完立即写码,≤5 个):`backend/data_sources/photon.py`、`backend/data_sources/nominatim.py`、`backend/services/place_loader.py`(geocode_with_fallback/reverse_with_fallback/resolve_* 段)、`backend/app/api/places.py`(geocode_city/reverse_geocode 端点)、`backend/db/models.py`(OriginCache)+`backend/db/repository.py`(get/upsert_origin_cache)。
> 写码硬约定见仓库根 AGENTS.md(不新增依赖、全 mock 测试、只 commit 不 push)。

## 0. 主会话实测数据(2026-10-11,契约依据,勿重测)

- **Photon `properties.countrycode` 可靠**:上海→CN、Interlaken→CH、Queenstown NZ→NZ;reverse 同样带 countrycode(上海 CN/因特拉肯 CH/皇后镇 NZ,3/3 正确);Photon 直连可用(0.9~2.1s)。
- **⚠️ Photon 对中文境外地名错误定位**:「因特拉肯」→ 香港「因特拉肯大道」countrycode=CN(会把境外城市误判成境内!)。**Nominatim(accept-language=zh-CN + addressdetails=1)中文境外 8/8 全对**(因特拉肯→ch、皇后镇→nz、巴黎→fr;country_code 小写)。
- → **正向(城市名)判定链:Nominatim 主 → Photon 兜底**;**逆向(坐标)判定链:Photon 主 → Nominatim 兜底**(Photon reverse 境外坐标 3/3 正确,且中文正向的坑不影响 reverse)。
- Nominatim 直连时好时坏(10/10 通、10/11 urllib 报 unreachable、curl/requests 通——IPv4/IPv6 happy-eyeballs 差异),**两条腿都失败必须默认 domestic,不许抛**。
- 既有 `photon.parse_feature`/`nominatim.parse_place` 只返回 `{lat,lng,display_name}` 三键,既有测试断言该形状——**加 country 信息必须走 opt-in 参数,默认输出零变化**。

## 1. 落地契约(精确签名/字段/键名)

### 1.1 `backend/data_sources/photon.py`(opt-in 扩展,默认行为零变化)

- `parse_feature(feature, *, action=...)` 与 `parse_features(payload, *, action=...)` 增关键字参数 `include_country: bool = False`;True 时返回 dict **追加**两键:`"country": str`(properties.country 原样,可空串)、`"countrycode": str`(properties.countrycode 大写,缺失为 "")。False(默认)时输出与现状**逐键一致**。
- `PhotonClient.geocode/reverse` 与模块级 `geocode()/reverse()` 同样加 `include_country: bool = False` 透传。
- 节流/UA/端点/异常口径全部不动。

### 1.2 `backend/data_sources/nominatim.py`(opt-in 扩展,默认行为零变化)

- `NominatimClient._request` 不动;`geocode()/reverse()` 与模块级函数增 `include_country: bool = False`:True 时请求参数带 `addressdetails=1`,返回 dict 追加 `"country_code": str`(payload `address.country_code` **大写**,缺失 "")与 `"country": str`(address.country 原样,缺失 "")。默认 False 时请求参数与返回形状零变化(addressdetails 仍为 0)。
- `parse_place(payload, *, action=..., include_country=False)` 同步扩展(或在 client 层组装,二选一,保持模块内一致)。

### 1.3 新文件 `backend/services/region_gate.py`

```python
REGION_DOMESTIC = "domestic"
REGION_OVERSEAS = "overseas"
DETECTOR_NOMINATIM = "nominatim"
DETECTOR_PHOTON = "photon"
DETECTOR_NONE = "none"

def classify_country_code(code: Any) -> str:
    """国家码 → region。'CN'(大小写不敏感)→ domestic;其他非空 → overseas;
    空/None/非字符串 → domestic(判不出按境内,现状链路零影响,不猜)。"""

def detect_region(city: str) -> dict[str, Any]:
    """城市名 → {"region", "country_code", "detector"}。
    链:Nominatim(include_country=True, accept-language 沿用 client 默认)主 →
    Photon(include_country=True)兜底 → 双失败 {"region": "domestic",
    "country_code": None, "detector": "none"}。
    每一腿宽 except Exception(尽力而为口径,理由照 place_loader 降级链 docstring);
    一腿返回空 country_code 视为该腿失败,降下一腿。
    Nominatim 腿的 country_code 大写后走 classify_country_code。"""

def detect_region_coords(lat: float, lng: float) -> dict[str, Any]:
    """坐标 → 同形状 dict。链:Photon reverse(include_country=True)主 →
    Nominatim reverse(addressdetails)兜底 → 双失败 domestic/none。"""
```

- 本模块**不落库不碰 session**(缓存是 API 层的事);不 import place_loader(避免循环,直接用 data_sources 两模块)。

### 1.4 `backend/db/models.py` + `backend/db/repository.py`

- `OriginCache` 增列 `region: Mapped[str] = mapped_column(String(16), nullable=False, default="")`(空串=未判定;**SQLite 旧库 ALTER TABLE 补列**,照 db/base.py 既有 Stay.price_kind/fetch_rounds 的补列模式)。
- `repo.upsert_origin_cache(...)` 增关键字参数 `region: Any = None`:None → 不改动该行 region 现值;给了 → `clean_text(region, limit=16)` 写入。既有调用零改动兼容。

### 1.5 `backend/app/api/places.py`

- `GET /api/geocode` 增可选查询参数 `region: Optional[str] = Query(None, pattern="^(domestic|overseas)$")`(非法值 FastAPI 自动 422 → **不行**,本仓库口径是 400 中文:改为不设 pattern,函数体内校验,非法 → `HTTPException(400, "region 只支持 domestic 或 overseas")`)。
  - 响应**只加一个键** `"region"`:override 参数给了 → 直接用;否则缓存命中且 `cached.region` 非空 → 用缓存;否则 `region_gate.detect_region(cleaned)` 判定,**判定结果(detector != none 时)随 upsert_origin_cache 写回**;detector=none 时响应 region="domestic" 但**不写缓存**(下次重试)。
  - 既有键 `{origin, geocoder, bands, segments}` 逐键零变化。缓存命中路径:region 也命中则**零额外网络**(不许在命中路径调 detect_region)。
- `GET /api/geocode/reverse` 同样只加 `"region"` 键:用 `detect_region_coords(lat, lng)`(reverse 端点无缓存现状,不新增缓存)。
- `/api/places`、`/api/places/meta` 等其余端点**零改动**。

### 1.6 前端 `backend/app/static/index.html`(最小改动)

- 顶栏(城市搜索框旁)加一个 checkbox:`<label><input type="checkbox" id="regionOverride"> 境外模式</label>`(样式随现有顶栏,不新增 CSS 文件)。
- JS:启动读 `localStorage["w2g_region_override"]`("1"/"0");勾选变化即写回;`searchCity()` 调 `/api/geocode` 时,勾选则追加 `&region=overseas`,未勾选**不加参数**(自动判定)。
- 本条前端只做开关与传参;**地图/检索行为不变**(region 的消费在 TASK-21+)。状态栏可加一行小字显示当前 region(从 /api/geocode 响应读,有则显示「境内/境外」)。

## 2. 测试契约(全 mock 不触网,≥15 新增)

新文件 `backend/test_region_gate.py`(fixture 照 test_photon.py/test_ctrip.py 套路;autouse no_network 把 `requests.Session.request` 换抛错):

1. classify_country_code:CN/cn→domestic,CH/NZ→overseas,""/None/垃圾→domestic(≥3 例);
2. detect_region:Nominatim 返回 ch → overseas+detector=nominatim;返回 cn → domestic;Nominatim 抛错 → Photon 兜底(注入替身);双失败 → domestic/none **不抛**;Nominatim 空 country_code → 降 Photon(≥5 例);
3. detect_region_coords:Photon reverse CH → overseas;Photon 抛错 → Nominatim 兜底;双失败 → domestic/none(≥3 例);
4. photon include_country=True 追加 country/countrycode 键、**False 默认形状逐键与旧断言一致**;nominatim 同(≥2 例);
5. OriginCache.region 落库+upsert region=None 不覆盖现值(≥1 例);
6. API:monkeypatch region_gate.detect_region,`/api/geocode` 响应含 region 键、**既有键集逐键零变化**(照 LEGACY_TOP_LEVEL_KEYS 断言风格)、override 参数生效且 detector 不调用、非法 region → 400 中文、缓存命中路径零 detect_region 调用(注入计数替身断言)、detector=none 不写缓存(≥5 例);
7. `/api/geocode/reverse` 响应含 region(≥1 例);
8. 前端静态断言(test_frontend_routes.py 追加):regionOverride checkbox DOM、localStorage key、searchCity 拼 region=overseas、状态栏 region 文案(≥3 例)+ node --check。

既有 1429 用例**零改动零回归**。

## 3. 验收与禁止项

- `cd backend && ../.venv/bin/python -m pytest -q` 全绿(基线 **1429**,只增不减);不新增第三方依赖;**不动** place_loader 降级链/amap.py/index.html 地图层/collections 相关文件。
- commit 消息带 `TASK-20`;只 commit 不 push。
- 执行器复跑测试通过后才算 done;真机冒烟(重启 uvicorn,curl `/api/geocode?city=因特拉肯` 期望 region=overseas、`city=上海` 期望 domestic)由执行器做,Codex 不需要起服务。
