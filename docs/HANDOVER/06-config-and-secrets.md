# 06 · 配置与密钥

## 配置的两条来源与优先级

```
容器 env / .env 文件   >   app_config 表(向导存的)   >   代码默认值
```

- 启动时 `services/app_config.load_into_environ()` 把库里的配置**注入 `os.environ`**,但**只填"env 未设或为空串"的键** —— 所以容器 `-e` 与 `.env` 永远压过数据库。
- 保存时(`POST /api/config`)写库 + 同步 `os.environ`,**热生效**,不用重启。
- 因此代码里 30 处 `os.environ.get(...)` 一行都不用改。
- **必须 `--workers 1`**:注入是进程内的,多 worker 会出现"有的进程有新 key、有的没有"。`docker-entrypoint.sh` 已经固定单 worker。

**白名单**:只有 `app_config.iter_fields()` 里登记过的键能通过 `/api/config` 读写;`GET /api/config` 只返回**掩码后的值**(`sk-****`)与 7 个白名单键。

## 环境变量全表

### 必填(否则地图与境内检索不可用)

| 变量 | 说明 |
|---|---|
| `WHERE2GO_AMAP_KEY` | 高德「Web 服务」key:后端 POI 检索 / 地理编码 / 驾车 |
| `WHERE2GO_AMAP_JS_KEY` | 高德「Web端(JS API)」key:前端地图 |
| `WHERE2GO_AMAP_SECURITY_JS_CODE` | JS key 配套安全密钥(缺失报 `INVALID_USER_SCODE`) |

三把 key 都能在 `/setup` 向导里配,存进数据库。高德开放平台免费申请,同一应用下分别建两个平台的 key,**不能混用**。

### 境外功能必配(境内不影响)

| 变量 | 说明 |
|---|---|
| `HTTPS_PROXY` / `HTTP_PROXY` | 境外链的 Nominatim 与 Overpass 在国内**直连不通**,必须走代理 |
| `NO_PROXY` | 建议把 `localhost,127.0.0.1,restapi.amap.com,photon.komoot.io,router.project-osrm.org` 排除掉 |

### 可选

| 变量 | 默认 | 说明 |
|---|---|---|
| `WHERE2GO_LLM_PROVIDER` | 自动 | `qwen` / `deepseek`;留空按已配 key 自动挑 |
| `WHERE2GO_LLM_API_KEY` | — | 任意 OpenAI 兼容 `/chat/completions` 的 key |
| `WHERE2GO_LLM_BASE_URL` / `WHERE2GO_LLM_MODEL` | 预置档 | 自定义 LLM 服务时才需要 |
| `WHERE2GO_DB_URL` | `sqlite:////data/where2go.db`(容器) | 容器内勿改,否则升级丢数据 |
| `WHERE2GO_UI_MODE` | `release`(容器) | 只认 `release`,其余一律 dev |
| `WHERE2GO_AMAP_MIN_INTERVAL_S` | `0.6` | 调小会触发 QPS 限流 10021 |
| `WHERE2GO_ORIGIN_CACHE_TTL_S` | `604800`(7 天) | 起点解析缓存 |
| `WHERE2GO_FUEL_PRICE_CNY_L` | `8.0` | 驾车费用估算油价 |
| `WHERE2GO_SEEDS` | 开 | 人工种子数据(国内滑雪/运动垫底),`off` 关闭 |
| `WHERE2GO_PLACE_MEDIA_TTL_S` / `_MISS_TTL_S` / `_MAX_IMAGES` | 604800 / 21600 / 6 | 图片缓存与上限 |
| `WHERE2GO_PROXY_<源>` | 见下 | 分数据源代理口径 |
| `WHERE2GO_OVERPASS_WORKERS` | `3` | 境外 Overpass 并发 |
| `WHERE2GO_*_ENDPOINT` | 官方 | 各数据源端点覆盖 |

**`WHERE2GO_PROXY_<源>`**:`off` = 直连 / `env` = 沿用 `HTTP(S)_PROXY` / 其他值 = 代理 URL。源名可取 `AMAP`/`NOMINATIM`/`PHOTON`/`LLM`/`OVERPASS`/`OSRM`/`WIKIMEDIA`。默认:高德、OSM 自驾、携程、Photon 直连;Nominatim、Overpass、LLM、维基走环境代理。改默认值前先读 `03-pitfalls.md` C 节。

兼容口径:`DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` / `QWEN_API_KEY` / `ALIBABA_TOKEN_PLAN_API_KEY` 也会被识别为对应 LLM 预置档的 key。

## 密钥在哪、怎么处理

- **仓库里没有任何真实密钥**。`.env.example` 是空模板;`.dockerignore` 排除 `.env` 与开发库;Release 流水线有断言守着镜像里不得出现这两样。
- 开发机上的 `.env` 在 Hermes 容器的 `/opt/data/.env`(权限 0600),里面除了本项目的高德 key、LLM key,还有 Hermes 自己的凭据。**不要把它复制进仓库目录**。
- `Github_PAT.txt`(仓库根)是**指针文件**,只写"凭据在哪",不含 token。
- Git 凭据在 `~/.git-credentials`,用 `credential.helper store --file=<绝对路径>` 引用。配置两步走:先加一个空值条目清掉继承链,再加 store 条目,否则会先用全局文件里的错 token。
- **本地明文密钥不加密** —— 这是产品负责人明确裁定过的,不要再提 age/sops 之类的方案。

## 硬性禁止

1. **任何 key 不得写进 `index.html` 或任何前端文件**。前端地图 key 走 `GET /api/map-config` 运行时注入。
2. **不得把 `.env`、`backend/data/*.db` 提交进 git 或打进镜像**。
3. **不得把掩码后的值(`sk-****`)写回配置** —— 那会把真 key 覆盖成垃圾字符串。
4. **不得在 API 响应或日志里回显密钥原文**;`/api/config` 只回掩码。
5. 验证凭据不要用 `ls-remote`(公开仓库读操作不需要认证,会假通过)。用 `curl -H "Authorization: Bearer <token>" https://api.github.com/user` 或 `git push --dry-run`。注意 `store` helper 认证失败时会**自动删掉凭据行**。
6. 容器里**不要**执行自更新类命令(历史上有过抹盘事故)。

## 向导(`/setup`)的工作方式

四步:① 高德 Web 服务 key ② 高德 JS key + 安全密钥 ③ LLM key(可跳过)④ 完成。每步都有"校验"按钮,会真打一次远端请求验证 key 有效性(`POST /api/config/validate`)。

配置保存进 `app_config` 表 → 注入 `os.environ` → 立刻生效。首页顶部横幅在"必填项缺失"时才出现;配置完整后 `/setup` 会 307 跳回首页。容器重建/升级镜像都不会丢配置(它在 `/data` 卷里的 SQLite 中)。
