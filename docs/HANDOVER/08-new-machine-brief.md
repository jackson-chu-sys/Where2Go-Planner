# 08 · 新机器交接单(无代理 · 无夜班 · 直接可用)

> 这份是 `07-new-machine-setup.md` 的**替代版**,针对当前这次交接的具体情况:
> **新机器不配代理,不排夜班定时任务,所有活由你在会话里直接做完。**
> 照着从头做到尾,一小时之内你就能改代码、跑测试、发版,并且知道接下来该干什么。

---

## 0. 这台机器的边界(先记在心里,能省掉很多误判)

| 条件 | 后果 |
|---|---|
| **没有代理** | **境外目的地实际不可用**(境外 POI 抓取依赖 Overpass,国内直连 502)。地理编码偶尔还能成,因为 Photon 国内可直连,但抓不到 POI 等于没用 |
| **没有代理** | ✅ 境内功能**完全不受影响**(高德链路本来就走直连) |
| **没有代理** | ✅ **测试照样全绿** —— 1739 个用例全 mock、零网络,`no_network` fixture 会把真实网络调用变成抛错。所以"测试绿"不代表"境外能跑" |
| **没有夜班 cron** | 队列(第 5 节)里的任务**不会自己跑**,需要你在会话里一条条推进;不要等定时任务 |
| **不要删境外代码** | 境外轨是产品能力,只是这台机器验不了。删了就是功能倒退 |

**一句话**:这台机器上,你是"境内功能 + 代码质量"的守护者;**不要在没有代理的环境里给境外功能下结论**(也别把境外的失败当成回归)。

---

## 1. 从零到跑起来(约 15 分钟)

```bash
# 1) 克隆
git clone https://github.com/jackson-chu-sys/Where2Go-Planner.git
cd Where2Go-Planner

# 2) Python 3.13 环境(需要 uv;系统 python 通常没有 pip,且 PEP 668 会拦)
uv venv .venv --python 3.13
.venv/bin/python -m pip install -r backend/requirements.txt

# 3) 跑测试(全 mock,不需要任何 key、不需要网络)
cd backend && ../.venv/bin/python -m pytest -q
# 期望:1739 passed 左右,约 126 秒。跑不绿先别往下走。

# 4) 配 key(三个高德 key 就够了;LLM key 可选)
cd .. && cp .env.example .env
# 编辑 .env,填:
#   WHERE2GO_AMAP_KEY                高德「Web 服务」key
#   WHERE2GO_AMAP_JS_KEY             高德「Web端(JS API)」key
#   WHERE2GO_AMAP_SECURITY_JS_CODE   JS key 配套安全密钥
#   可选 WHERE2GO_LLM_*              任意 OpenAI 兼容服务(不配只是没有 AI 功能)
#   代理相关整段留空即可

# 5) 起服务
set -a && source .env && set +a
.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
```

改 `.py` 必须重启进程;改前端 `backend/app/static/index.html` **不用重启**,刷新浏览器即可。

### 验收(三条 curl 必须通)

```bash
curl -s localhost:8000/api/health
# → {"status":"ok","missing_required":[],"version":"0.2.0-beta"}

curl -s "localhost:8000/api/geocode?city=上海"
# → geocoder=amap, region=domestic

curl -s "localhost:8000/api/geocode?city=上海&region=overseas"
# → 自动降级:geocoder=amap, region=domestic, 带 note 说明「已按境内口径解析」
```

再打开浏览器 `localhost:8000`,搜一个境内城市能出 pin。**到这里环境就算就绪了。**

> 境外不要在这台机器上验:`city=苏黎世` 在这里会报错或定位不准,那是缺代理,**不是 bug**。

---

## 2. 工作方式(和上一个 Agent 保持一致的约定)

1. **只 commit,不 push**。推远端每次都要单独获得授权。
2. **不代订、不抓实时价**。价格一律标"估算",预订一律跳官方链接。这是产品红线。
3. **不新增第三方依赖**。测试必须能在无网络、无 key 的机器上全绿。
4. 产品负责人**不写代码**,方案由你定;需要他拍板时**给可点选的选项**,不要开放式提问。
5. 每次改完:跑全量 pytest → 更新 `docs/NIGHTLY-QUEUE.md` 对应条目(写通过数、commit 短哈希,别写"已完成")→ commit。
6. 历史结论都带日期和前提,**引用前先核对当时的配置今天是否还生效**(踩过:旧结论"上下文窗口装不下前端",实际是配置文件手动把窗口设成了 64K)。

必读顺序:`AGENTS.md`(32 行速查)→ `docs/HANDOVER/01-architecture.md` → `03-pitfalls.md`(动数据源/前端/LLM 之前)。

---

## 3. 后续任务清单

按优先级排。每条都给了目标、涉及文件、验收标准,以及"不需要做什么"。**这三条已经写进 `docs/NIGHTLY-QUEUE.md` 了(状态 `pending`,编号 TASK-28 / TASK-29 / TASK-30)** —— 做完一条就把该条状态改 `done` 并回填通过数与 commit 短哈希,别让任务状态只活在对话里。

### T1 · 高德 `level` 判据落码,让脏缓存能自愈(P0,最该先做)

**背景**:`origin_cache` 表存的是"城市名 → 坐标"的解析结果,TTL 7 天。历史上有过把境外中文名错误吸附成国内地点的行(如 `北海道 → 香港油尖旺北海街`、`新加坡 → 陕西咸阳新加坡村`),这些行一旦写进去,7 天内每次搜索都是错的。交接前已手工删了 6 行,但**新代码只能防新增,不能修存量**。

**已验证的判据**(2026-10-18 实测 12/12 正确):高德 geocode 返回的 `level` 字段能区分真假行政区 —— 真城市是「省/市/区县」,错误吸附是「兴趣点/道路/村庄」。零额外请求(0.05s)。

**要做**:在境内地理编码采信前加这道判据;判成非行政区级时,不写 `origin_cache`(或写一个短 TTL 的标记),让下次搜索能重新解析。

**涉及**:`backend/data_sources/amap.py`、`backend/services/place_loader.py`、对应的 `test_*.py`

**验收**:单测覆盖「真城市采信 / POI 吸附不采信 / 字段缺失沿用旧行为」三种;全量 pytest 不回归;构造一行假脏数据,验证第二次搜索能自愈。

### T2 · 无代理环境下的产品化提示(P0,这台机器特有)

**背景**:没有代理时搜境外城市,用户看到的是 400 + 一段英文异常(Nominatim `Network is unreachable` / Overpass `502`)。这是**部署条件问题**,不该以报错形态丢给最终用户。

**要做**:检测到境外链路不可达时,返回一句人能看懂的中文说明(比如"当前环境未配置代理,境外目的地暂不可用"),并且在 release UI 里不要暴露技术堆栈。**不要**改成静默返回空列表 —— 用户会以为"这里没有地方可去"。

**涉及**:`backend/services/place_loader.py`(失败聚合处)、`backend/services/region_gate.py`、`backend/app/static/index.html`(提示样式)、测试

**验收**:无代理环境下 `curl "localhost:8000/api/geocode?city=苏黎世&region=overseas"` 返回中文可读提示;有代理时行为不变(可用 `WHERE2GO_PROXY_NOMINATIM=http://...` 临时指一个代理做对照)。

### T3 · 容器冒烟 + 打 `v1.0.0`(P0,发版收尾)

**背景**:`v0.2.0-beta` 镜像已发布(含全部已有修复),但 `v1.0.0` 还没打 —— 原计划是"beta 在真机冒烟通过后再打正式版"。

**要做**:这台机器上装 Docker → `docker compose up -d` 起 `:latest` → 按 `04-deploy-release.md` 的冒烟清单过一遍(health / release UI / 向导 / 境内搜一个城市 / 重建后数据还在)→ 全绿后**申请 push 授权**,打 tag `v1.0.0` 触发 Release 流水线 → 用匿名 registry 接口核对 tag 与 amd64+arm64 双架构。

**涉及**:无代码改动;`docker-compose.yml`、`.github/workflows/release.yml`、README 版本说明

**验收**:GHCR 出现 `1.0.0` tag 且双架构齐全;README 里的版本号与说明同步更新。

### T4 · 等产品负责人拍板的三件(P2,别自己动)

1. **高德「世界地图」权限**(队列条目 `[TASK-WORLDMAP]`,状态 blocked):他在向高德提工单问价。若批下来,可以**下线 Leaflet 境外轨**(境外 GCJ-02≡WGS-84,OSM 数据可直接打 pin),境内外同引擎同代码。批下来之前**不得启动**,而且它只解决底图,不解决境外 POI/geocode/路线。
2. **境外轨 CDN 依赖**:Leaflet 走 unpkg,境内真实用户可能加载不到(已有降级提示)。彻底解决要自托管 Leaflet 或用国内镜像,需要他决定值不值得做。
3. **微信小程序**:2026-10-10 已拍板暂缓(资质门槛),不要自行启动。

### T5 · 超时线复核(P3,可选)

`PLANNER_TIMEOUT_S=180` 与 `ASSISTANT_TIMEOUT_S=120` 在高峰期实测贴线(同一 prompt 平峰 12~23s、高峰 73~184s)。后台线程路径可放心放宽,同步路径要权衡。改前读 `03-pitfalls.md` D 节 —— 那里的教训是"超时的表现不是报错,而是永远显示生成中"。

### 完成定义

每条任务收尾都要有:全量 pytest 输出(通过数)、commit 短哈希、队列条目回填。**不要交一份"我改了代码"的口头报告。**

---

## 4. 高频踩坑(只列最容易重复犯的,完整版见 03)

- **境内检索不走 v5**:高德 v5 翻页是坏的;单查询最多 200 条,`radius` 截断 50000。
- **高德 `tolls` 恒 0、`cost` 恒 null**:过路费只能用 `toll_distance` × 费率自算。
- **QPS 10021**:请求间隔 0.6 秒,别调小。
- **key 不能混用**:REST 用「Web 服务」key,前端用「Web端(JS API)」key;任何 key 都不许写进 `index.html`。
- **JS 正则剥 emoji 不能用字符类**:`[📍🛏️]` 会拆散 UTF-16 代理对,导致 `URI malformed`、整个面板渲染崩。必须写 `(?:📍|🛏️)`。
- **验证点击要用真实点击**:合成 `dispatchEvent` 对高德无效。
- **测试里 monkeypatch 的替身必须活到后台任务结束**:否则表现为偶发 flake,实际是确定性竞态。
- **Docker 里的文件执行位看 `git ls-files -s`**:`ls -l` 显示 755 不代表 git index 记的是 100755。
- **不要用 `ls-remote` 验凭据**(公开仓库读操作不需要认证,会假通过)。

---

## 5. 任务队列怎么用

`docs/NIGHTLY-QUEUE.md`(1251 行)原本是"夜班执行器每晚读它"的队列。这台机器不排夜班,但**文件照样是最权威的任务台账**,规则不变:

- 状态机:`pending → running → done / blocked / needs_review`;做之前把状态改成 `running`,做完回填结果。
- 新增任务时照 `## [TASK-xx] 标题 / 状态 / 目标 / 涉及 / 验收 / 结果` 的格式,**放在 `## 追加模板` 之前**。
- 头部有「⚠️ 当日生效口径通告」,**冲突时以它为准**。
- 队列里已有 `done` 的历史条目记录了当年的实测数据与踩坑,**遇到相似问题先搜这里,不要从零调查**。
- 当前队列无 `pending` 条目;唯一未完成的是 `[TASK-WORLDMAP]`(blocked,等拍板)。上面第 3 节的 T1~T3 需要你**新建条目**再开工。

---

## 6. 最后:交接是否成功,看这五条

1. `cd backend && ../.venv/bin/python -m pytest -q` → 1739 passed 左右
2. `curl localhost:8000/api/health` → `status=ok`
3. `curl "localhost:8000/api/geocode?city=上海"` → `geocoder=amap`
4. 浏览器搜一个境内城市 → 出 pin
5. `git log --oneline -1` → 能看到 `0448bb5` 或更新,且 `git status --short` 干净

五条全过 = 交接完成,直接从第 3 节的 T1 开始干活。
