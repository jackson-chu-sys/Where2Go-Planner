# Where2Go · 周末去哪儿玩 自由行规划器

> 📁 **给访客的说明**:本仓库的 `docs/` 目录与根目录下的中文 md/txt(需求文档、开发队列、契约等)是**开发过程记录**,普通用户只需要看这份 README。
>
> 📁 **Note for visitors**: `docs/` and the Chinese markdown/txt files at the repo root are **development-process records**. As a user, you only need this README.

以「起点城市 + 距离分段」为核心的自由行目的地发现工具:在地图上按 0-50 / 50-100 / 100-200 / 200-300 / 300-500 km 环形分段浏览目的地(自然风光 / 小城人文美食 / 滑雪场 / 运动四分类),点任意目的地看驾车/铁路/飞机路线对比与费用估算、周边住宿 AI 预估价,收藏后组合成行程方案看总花费,并可一键跳转高德/12306/携程等官方渠道预订(**不代订、不抓实时价**)。支持境外目的地(OSM/Leaflet 双轨)。

A self-hosted trip planner built around "origin city + distance bands": browse destinations on a map in rings (0–50 / 50–100 / 100–200 / 200–300 / 300–500 km) across four categories (nature / culture & food / ski / sports), compare driving/rail/flight routes with cost estimates, see AI-estimated hotel prices, collect favorites into trip plans with a total budget, and deep-link out to Amap/12306/Ctrip etc. for actual booking (**no proxy booking, no live prices**). Overseas destinations supported (OSM/Leaflet dual-track).

---

## 快速开始 Quickstart(三条命令)

```bash
git clone https://github.com/jackson-chu-sys/Where2Go-Planner.git && cd Where2Go-Planner
docker compose up -d          # 首次会自动拉取镜像
```

浏览器打开 **http://localhost:8000** → 首页顶部横幅提示「尚未完成配置」→ 点进 **/setup 配置向导**,按 4 步填入你自己的高德 key(免费申请,向导内有图文教程)→ 回首页搜「上海」出图。

Open **http://localhost:8000**, follow the banner into the **/setup wizard**, paste your own Amap (Gaode) keys (free; step-by-step tutorial included), then search a city.

> - 配置存在 `./data` 卷的 SQLite 里,`docker compose down && up -d` / 升级镜像都**不丢配置、不丢已抓数据**。
> - 也可以不用向导:`cp .env.example .env` 填好后重启容器(环境变量**永远优先**于向导存的值)。
> - 启动报「数据目录不可写」:`sudo chown -R 1000:1000 ./data`(或解开 compose 里 `user: "1000:1000"`)。
> - 镜像尚未发布到 GHCR 时(P4 流水线未跑),用本地构建:`docker compose up -d --build`。

## 申请高德 key(免费,约 3 分钟)How to get Amap keys

1. 打开 [高德开放平台控制台 → 应用管理 → 我的应用](https://console.amap.com/dev/key/app),注册并实名认证(个人开发者免费配额即够自用)。
2. 「创建新应用」→ 应用里「添加 Key」,**服务平台选「Web服务」**→ 这是后端用的 `WHERE2GO_AMAP_KEY`。
3. 同一应用再「添加 Key」,**服务平台选「Web端(JS API)」**→ 得到前端地图用的 `WHERE2GO_AMAP_JS_KEY` 和配套的**安全密钥** `WHERE2GO_AMAP_SECURITY_JS_CODE`。
4. 把三个值填进 /setup 向导(第 1、2 步会**真调一次高德接口校验**,填错当场给中文解释)。

⚠️ 两把 key **平台隔离、不能混用**(混用报 `USERKEY_PLAT_NOMATCH`,infocode 10009);安全密钥填错 → 地图空白报 `INVALID_USER_SCODE`。

## 环境变量全表 Environment variables

全部可选(必填三项也可在向导里配);完整带注释样例见 [.env.example](.env.example)。下表与代码 grep 核对一致(2026-10-16):

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `WHERE2GO_AMAP_KEY` | ✅ | — | 高德「Web服务」key:地理编码 / POI 检索 / 驾车路线 |
| `WHERE2GO_AMAP_JS_KEY` | ✅ | — | 高德「Web端(JS API)」key:前端地图 |
| `WHERE2GO_AMAP_SECURITY_JS_CODE` | ✅ | — | JS key 配套安全密钥(缺失报 INVALID_USER_SCODE) |
| `WHERE2GO_LLM_PROVIDER` | — | 自动 | 预置档 `qwen` / `deepseek`;留空按已配 key 自动挑 |
| `WHERE2GO_LLM_API_KEY` | — | — | 任意 OpenAI 兼容 `/chat/completions` 的 key;不配则 AI 简介/推荐降级为空 |
| `WHERE2GO_LLM_BASE_URL` | — | 预置档端点 | 自定义 LLM 服务根地址(如 `https://api.deepseek.com`) |
| `WHERE2GO_LLM_MODEL` | — | 预置档模型 | 如 `deepseek-chat` / `qwen-plus` |
| `WHERE2GO_DB_URL` | — | `sqlite:////data/where2go.db`(容器) | 数据库位置;容器内勿改,否则升级丢数据 |
| `WHERE2GO_UI_MODE` | — | `release`(容器) | `release` 隐藏技术细节 / `dev` 全量口径 |
| `WHERE2GO_AMAP_MIN_INTERVAL_S` | — | `0.6` | 高德请求最小间隔秒(调小会触发 QPS 限流 10021) |
| `WHERE2GO_AMAP_ENDPOINT` | — | `https://restapi.amap.com/v3` | 高德 REST 端点(勿改 v5,翻页是坏的) |
| `WHERE2GO_AMAP_MAX_MATCH_M` | — | `5000` | 起点地理编码与输入城市名的最大匹配距离(米) |
| `WHERE2GO_ORIGIN_CACHE_TTL_S` | — | `604800`(7 天) | 起点解析缓存 TTL |
| `WHERE2GO_FUEL_PRICE_CNY_L` | — | `8.0` | 驾车费用估算油价(元/L,估算口径) |
| `WHERE2GO_SEEDS` | — | 开 | 人工种子数据(国内滑雪/运动垫底);关闭填 `off` |
| `WHERE2GO_PLACE_MEDIA_TTL_S` | — | `604800` | POI 图片缓存 TTL |
| `WHERE2GO_PLACE_MEDIA_MISS_TTL_S` | — | `21600` | 图片空结果负缓存 TTL |
| `WHERE2GO_PLACE_MEDIA_MAX_IMAGES` | — | `6`(≤20) | 单 POI 图片上限 |
| `WHERE2GO_NOMINATIM_ENDPOINT` | — | 官方 | 地理编码降级链(Nominatim) |
| `WHERE2GO_PHOTON_ENDPOINT` | — | 官方 | 地理编码降级链(Photon) |
| `WHERE2GO_OVERPASS_ENDPOINT` | — | 官方 | **境外轨** OSM Overpass 端点 |
| `WHERE2GO_OVERPASS_WORKERS` | — | `3` | 境外轨 Overpass 并发 |
| `WHERE2GO_OSRM_ENDPOINT` | — | 官方 | **境外轨** OSRM 驾车路线端点 |
| `WHERE2GO_WIKI_API` / `WHERE2GO_COMMONS_API` | — | 官方 | 维基/Commons 图片兜底端点 |
| `WHERE2GO_PROXY_<源>` | — | 见 .env.example | 分数据源代理口径:`off` 直连 / `env` 沿用 `HTTP(S)_PROXY` / 其他值=代理 URL(源名:AMAP/NOMINATIM/PHOTON/LLM/OVERPASS/OSRM/WIKIMEDIA) |

兼容口径:`DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` / `QWEN_API_KEY` / `ALIBABA_TOKEN_PLAN_API_KEY` 也会被识别为对应 LLM 预置档的 key。

## FAQ

**Q: 报错/横幅出现 `infocode=10021`?**
高德 QPS 超限。服务端已内置单进程 0.6s 最小间隔 + 2s/5s 退避重试;若仍出现说明有别的程序共用这张 key,或把 `WHERE2GO_AMAP_MIN_INTERVAL_S` 调大。别在向导里连点「校验」(每次校验只打一次请求,连点也会触发)。

**Q: 地图整块空白,控制台报 `INVALID_USER_SCODE`?**
安全密钥(`WHERE2GO_AMAP_SECURITY_JS_CODE`)缺失或与 JS key 不配套。回 /setup 第 2 步重新粘贴**创建 JS key 时一并给出的那串**。

**Q: 校验/检索报 `USERKEY_PLAT_NOMATCH`(infocode 10009)?**
两把高德 key 填反了:后端 REST 只能用「Web服务」key,前端地图只能用「Web端(JS API)」key。同一应用下分别创建,不能混用。

**Q: 高德免费配额够吗?**
个人开发者认证后的免费日配额(地理编码/检索各数千次/日)对单人规划完全够用;配额耗尽报 infocode 10003/40000,次日自动恢复。已入库的城市二次查询**零网络请求**(读本地 SQLite)。

**Q: 不配 LLM key 会怎样?**
服务照常:地图、检索、路线、收藏、行程方案全部可用;POI 简介与 AI 推荐/行程规划降级为空,不报错。

**Q: 怎么升级?**
`docker compose pull && docker compose up -d`。数据与配置都在 `./data` 卷里,不丢。

## 从源码运行(不用 Docker)Run from source

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cp .env.example .env   # 填 key(或起服务后走 /setup 向导)
.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
```

开发测试:`cd backend && ../.venv/bin/python -m pytest -q`(全 mock 不触网,1700+ 用例)。

## 许可与源码获取 License & source

本项目以 **AGPL-3.0** 许可发布(见 [LICENSE](LICENSE))。你通过 Docker 运行的容器镜像对应的**完整源码**即本仓库:<https://github.com/jackson-chu-sys/Where2Go-Planner>(镜像 tag 与仓库 commit 一一对应)。自行 `docker run` 属自用,不触发额外开源义务;若基于它对外提供网络服务(SaaS),需按 AGPL 公开你的修改。

This project is licensed under **AGPL-3.0** (see [LICENSE](LICENSE)). The full source code corresponding to any published container image is this repository: <https://github.com/jackson-chu-sys/Where2Go-Planner>.

## 第三方归属与免责声明 Attribution & disclaimer

- **地图与 POI 数据**:境内检索/地理编码/驾车路线与底图来自 [高德开放平台](https://lbs.amap.com/)(需自备 key,使用行为受[高德服务条款](https://lbs.amap.com/home/term/)约束);境外轨数据来自 [OpenStreetMap](https://www.openstreetmap.org/copyright)(© OpenStreetMap contributors,ODbL)、Overpass API、OSRM、Photon/Nominatim;POI 图片部分来自 Wikimedia Commons。
- **LLM**:AI 简介/推荐由你自选的第三方大模型服务生成(DeepSeek / 阿里百炼 / 任意 OpenAI 兼容服务),内容可能存在错误,请以官方信息为准;key 由你自备,费用自付,本项目不留存、不回显任何 key 明文。
- **价格与时长**:路线费用、住宿参考价均为**估算**(标注「AI 预估 · 仅供参考 · 以 OTA 实时为准」),本项目**不代订、不抓取实时价格**;「前往预订」仅生成跳转各官方渠道(高德/Google 地图/12306/去哪儿/携程/Booking/Airbnb)搜索页的 deep-link。
- 本项目为个人开源工具,与上述任何服务商无隶属或背书关系。

Third-party data: Amap/Gaode (domestic maps & POI, bring your own key, subject to Amap ToS), OpenStreetMap contributors (ODbL) / Overpass / OSRM / Photon / Nominatim (overseas track), Wikimedia Commons (images). AI features use your own LLM provider; prices/durations are **estimates only** — this tool never books on your behalf nor scrapes live prices; booking buttons are plain deep-links to official sites. Not affiliated with or endorsed by any provider above.
