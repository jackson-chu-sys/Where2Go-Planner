# Where2Go 容器化 + 用户使用向导 · 实施方案(待神朱审阅)

> 2026-10-15 起草。上游依据:`docs/PUBLISH-PLAN.md` 第一节(P1-P5,神朱 2026-10-10 拍板「先做 GitHub 容器发布,微信小程序暂缓」)。
> 本文把 P1-P5 细化到可直接派活的粒度,并列出**需要拍板的 5 个决策点**(见第五节)。
> 仓库现状核对日期:2026-10-15(commit `ea019b5`,pytest 1632 passed)。

---

## 一、现状核对(实测,非推测)

| 项 | 实测结果 | 对方案的影响 |
|---|---|---|
| Dockerfile / compose / .github | **全无** | P1、P4 从零写 |
| 仓库可见性 | **已公开**(`api.github.com/repos/jackson-chu-sys/Where2Go-Planner` → 200) | 内部文件处置已是「事后补救」,不是「发布前拦截」→ 见决策点① |
| 凭据泄露 | `Github_PAT.txt` **未被 git 跟踪**且在 .gitignore;`AzureMapsKey.txt` 已不存在;tracked 文件里 `sk-`/`ghp_` 命中全是**测试假 key 与文档正则字样**(`sk-test-123`) | PUBLISH-PLAN §1.3 第1条的「删除+轮换」基本已完成,**无需紧急轮换**(若 PAT 曾在别处提交过仍需核,见决策点①备注) |
| 容器内能否 docker build | **不能**(无 `/var/run/docker.sock`、无 sudo、无 podman/buildah) | 本地无法验证镜像 → 验证策略见第三节 |
| AppConfig 表 | **不存在**(15 张表里没有) | P2 要新建 |
| env 读取点 | 30 处 `os.environ`,散在 15 个文件;**但普遍带 `environ=None` 参数注入**(测试友好) | P2 不改这 30 处 → 见第二节「配置注入策略」 |
| DB 路径 | 默认 `backend/data/where2go.db`,`WHERE2GO_DB_URL` 可覆盖;引擎**懒加载**(import 无副作用) | 容器用 `/data` 卷 + env 覆盖,**零代码改动** |
| healthcheck 端点 | `/api/places/meta` 在**无高德 key 时仍 200**(实测) | 可直接做 Dockerfile HEALTHCHECK |
| JS key 注入 | 已有 `GET /api/map-config` 运行时读 env(不写进 index.html) | 向导保存后**前端刷新即生效**,机制现成 |
| LLM 抽象 | `intro.py` provider 注册表 + `WHERE2GO_LLM_{PROVIDER,BASE_URL,MODEL,API_KEY}` 覆盖,**任意 OpenAI 兼容服务** | 向导第③步只是把这套 env 图形化,后端零改动 |
| 无 key 时的降级 | 高德缺 key → `MISSING_KEY_MESSAGE` 中文报错;LLM 缺 key → 空简介不抛异常 | 服务能起、页面能开,向导有存在意义(不是白屏) |
| 依赖 | requests / fastapi / uvicorn / pydantic / sqlalchemy(+pytest) | 无编译型依赖 → `python:3.13-slim` 直接装,无需 build 阶段 |
| LICENSE | **AGPL-3.0**(661 行) | 见决策点⑤:AGPL 对「用户自建容器」无传染问题,但**对外提供 SaaS 需开源**;若要宽松许可需现在换 |

---

## 二、核心技术决策:P2 配置怎么让 30 个 env 读取点看到

**问题**:神朱已拍板「配置存 SQLite `AppConfig` 表,读取顺序 = env 优先 → AppConfig 兜底」。但现有 30 处都是直接 `os.environ.get(...)`,逐个改成「查 DB」工程量大、且会把 DB 依赖渗进 data_sources 层(违反分层)。

**方案(最小工程量,零改动那 30 处)**:AppConfig 的值在**启动时与保存时注入 `os.environ`**:

```
启动: 读 AppConfig 全表 → 对每个 key,若 os.environ 里没有同名变量则 os.environ[key]=值
保存: 向导 POST /api/config → 写 AppConfig 表 + 同步 os.environ[key]=新值(进程内热生效)
优先级: 容器 env(-e / .env)始终压过 AppConfig —— 因为启动时只填「env 未设」的键
```

- 好处:`data_sources/`、`services/` **一行不改**;`/api/map-config` 现读 env 的机制天然生效(前端刷新即用新 key);测试口径不变(照旧 monkeypatch env)。
- 代价:多进程/多 worker 下需每进程各自注入 → 容器固定 `--workers 1`(SQLite + 单容器场景本就够用,现网 uvicorn 也是单进程)。
- 热生效边界:`intro.py` 的 provider 是**每次调用现解析**(`resolve_provider(environ)`),LLM key 改完立即生效;`amap.py` 同理现读 env。**唯一例外**是 `db/base.py` 的 engine 是进程内单例 → 改 `WHERE2GO_DB_URL` 需重启容器(向导里对这一项标「需重启」,或干脆不暴露它)。

**AppConfig 表契约**(照 `models.py` 既有风格):
```python
class AppConfig(Base):
    __tablename__ = "app_config"
    id            = Column(Integer, primary_key=True)
    key           = Column(String(64), nullable=False, unique=True)   # = env 变量名,如 WHERE2GO_AMAP_KEY
    value         = Column(Text, nullable=False, default="")
    source        = Column(String(16), nullable=False, default="setup")  # setup | env | default
    updated_at    = Column(DateTime, default=utcnow, onupdate=utcnow)
```
- 安全口径(照既有「key 不出响应」):`GET /api/config` **只回掩码值**(`已配置(sk-…1234)` / `未配置`)+ 每键的 `label/hint/required/needs_restart`;完整值永不进 API 响应、不进日志。
- 白名单:只允许向导暴露的键写入(高德 REST key、JS key、安全密钥、LLM 4 项),其余 env 只读展示,防止 `/api/config` 变成任意 env 注入面。

---

## 三、验证策略(本地无 docker daemon 的应对)

| 层次 | 在哪验 | 怎么验 |
|---|---|---|
| 单元/契约 | 本容器 | pytest 全 mock(基线 1632),新增 AppConfig + /api/config + /setup 用例 |
| 镜像语法 | 本容器 | `docker build` 跑不了 → 用 **hadolint 静态检查 Dockerfile**(如可装)+ 人工核;compose 用 `python -c "import yaml,sys;yaml.safe_load(open('docker-compose.yml'))"` 验语法 |
| entrypoint 脚本 | 本容器 | `bash -n` 语法检查 + 直接跑一遍(它只是 exec uvicorn) |
| 真实镜像构建 | **GitHub Actions** | P4 流水线首次跑通即验证;失败看日志 |
| 真机冒烟 | **神朱的 NAS** | Actions 出镜像后,`docker compose up -d` → 浏览器走向导 → 搜城市出 pin。这是唯一能证明「用户真能用」的一步 |
| 无 docker 的替代验证 | 本容器 | 用**假 env(无 key)**起 uvicorn,走一遍 `/setup` 全流程 + 保存 + 刷新首页,证明向导闭环(不验镜像层,验应用层) |

> 结论:镜像层必须靠 Actions + NAS 冒烟;应用层(向导闭环)本容器可完整验证。**建议 P1-P3 完成后先在本容器做「无 key 假 env 走向导」闭环验证,再推 P4 触发 Actions 构建。**

---

## 四、任务拆分(可直接入 NIGHTLY-QUEUE)

| 条目 | 内容 | 涉及 | 验收 | 预估 |
|---|---|---|---|---|
| **P1** | `Dockerfile`(python:3.13-slim、非 root uid 1000、`/data` 卷、HEALTHCHECK `/api/places/meta`、`WHERE2GO_UI_MODE=release`)+ `docker-compose.yml`(端口 8000、卷 `./data:/data`、`env_file: .env`)+ `.env.example`(全部 `WHERE2GO_*` 带中文注释)+ `docker-entrypoint.sh`(建 /data、注入配置、exec uvicorn --workers 1)+ `.dockerignore`(排 .venv/.git/backend/data/docs/tools/内部 txt) | 仓库根新文件 | 本容器:compose YAML 语法过、entrypoint `bash -n` 过、hadolint(若可用)无 error;.dockerignore 确认不含 `.env`/db | 1 晚 |
| **P2** | `AppConfig` 表 + `services/app_config.py`(load_into_environ / set_key / mask_value / 白名单 SCHEMA)+ `GET/POST /api/config` + `GET /api/health`(给向导与 compose 用,回「哪些必填项缺」)+ 启动注入(main.py lifespan) | `db/models.py`、新 `services/app_config.py`、新 `app/api/config.py`、`app/main.py` | pytest +≥25(掩码不漏值、白名单外键拒写、env 压过 AppConfig、注入后 amap/intro 立刻看到新值);基线零回归;`/api/config` 响应 grep 不到明文 key | 1 晚 |
| **P3** | `/setup` 向导页(新 `app/static/setup.html`,不塞进 index.html):4 步(高德 REST key → 高德 JS key+安全密钥 → LLM(可选,预置 DeepSeek/阿里/OpenAI + 自定义 base_url+model)→ 完成),每步带**图文申请教程 + 真调校验 + infocode 中文解释**;首页检测缺配置 → 顶部横幅引导(不强制重定向,见决策点③) | 新 `app/static/setup.html`、`app/api/config.py` 校验端点、`index.html` 加横幅 | browser QA 全流程:无 key 起服务 → 走向导 → 填假 key 看校验报错文案 → 填真 key 看通过 → 回首页地图出图;pytest 静态断言(向导 DOM/校验端点/横幅);0 JS error | 1-2 晚 |
| **P4** | `.github/workflows/ci.yml`(push/PR 跑 pytest,全 mock 无需 secrets)+ `release.yml`(打 tag `v*` → buildx multi-arch amd64+arm64 → push `ghcr.io/jackson-chu-sys/where2go:{tag,latest}`)+ 冒烟 job(容器起后 curl `/api/health`) | `.github/workflows/` | Actions 首跑绿;GHCR 出现镜像;NAS `docker pull` + `compose up` 成功 | 1 晚(含等 Actions) |
| **P5** | 仓库根 `README.md`(中英双语:一句话介绍、截图、quickstart 三条命令、key 申请教程链接、`WHERE2GO_*` 全表、FAQ:QPS 10021 / INVALID_USER_SCODE / USERKEY_PLAT_NOMATCH / 高德免费配额)+ 第三方归属与免责声明(高德条款、LLM 服务商、价格估算不代订)+ 内部文件处置(决策点①) | `README.md`(仓库根,现无)、内部文件 | README 渲染正常、命令可复制即跑;内部文件按拍板处置 | 0.5 晚 |
| **P6**(可选) | 发布版 UI 精简(`WHERE2GO_UI_MODE=release` 隐藏技术细节,见 PUBLISH-PLAN 第三节) | `index.html`、`/api/places/meta` 透出 ui_mode | release/dev 双模 browser QA 截图对照;pytest 静态断言 | 0.5-1 晚 |

**建议顺序**:P1 → P2 → P3(应用层闭环验证)→ P5 → P4(触发 Actions)→ P6。
**单晚 4 条上限内**:第 1 晚 P1+P2,第 2 晚 P3,第 3 晚 P5+P4,P6 视情况。

---

## 五、决策点(神朱 2026-10-15 拍板:「都按推荐」→ 全部取 A)

| # | 决策 | 拍板结果(A) |
|---|---|---|
| ① | 内部文件处置 | **保留不动**;仅在 README 顶部加一句「docs/ 与根目录 md 是开发过程记录,用户只需看 README」 |
| ② | 镜像架构 | **multi-arch amd64+arm64**,`ghcr.io/jackson-chu-sys/where2go:{latest,vX.Y.Z}` |
| ③ | 向导触发 | **首页顶部横幅引导**,不强制重定向(无 key 时列表/收藏/AI 仍可用) |
| ④ | 首版版本号 | **先 `v0.1.0-beta`**,NAS 冒烟通过后再打 `v1.0.0` |
| ⑤ | LICENSE | **维持 AGPL-3.0**;README 写明仓库地址以满足镜像分发的源码获取义务 |

以下为原始选项留档(实施时若有新情况按此回溯)。

**① 内部文件处置**(仓库已公开,`意见.txt`/`需求文档.txt`/`01-功能需求清单.md`/`02-项目计划与架构.md`/`docs/NIGHTLY-QUEUE.md`(1109 行,含开发内幕、内网 IP `192.168.1.210`、路径 `/opt/data/.env`、38 处「神朱」)均已被 git 跟踪)
- **A(推荐)**:保留不动。仓库本就定位为「公开开发过程」,这些文件是真实迭代记录;只在 README 顶部加一句「docs/ 与根目录 md 是开发过程记录,用户只需看 README」。零风险零工作量。
- **B**:移到 `docs/internal/` 并加进 `.gitignore`(**新提交不再含,但 git 历史里永久留存**,等于只是不再更新)。
- **C**:重写 git 历史彻底清除(`git filter-repo`)——会改写全部 44+ 未推 commit 的哈希、强推覆盖远端,代价大且历史仍可被 fork 留存。**不推荐**。

**② 镜像标签与仓库名**
- **A(推荐)**:`ghcr.io/jackson-chu-sys/where2go:latest` + 语义化 tag(`v1.0.0`),multi-arch(amd64+arm64)。
- **B**:只出 amd64(构建快一半;你的 NAS 是 Intel N100 = amd64,但其他 NAS 用户多为 arm)。

**③ 向导触发方式**
- **A(推荐)**:**首页顶部横幅引导**(「未配置高德 key,地图与检索不可用 → 点此配置」),不强制重定向。理由:无 key 时列表/收藏/AI 等部分功能仍可用,强制跳转会挡住只想看看的人;且 index.html 已有 showNote 机制,工程量最小。
- **B**:PUBLISH-PLAN 原口径——检测到缺配置时**所有页面重定向 `/setup`**(更「产品化」,但要改 index.html 启动逻辑 + 处理「配置完怎么跳回」)。

**④ 首版版本号与是否打 tag 触发发布**
- **A(推荐)**:先发 `v0.1.0-beta`(明示 beta,允许粗糙),Actions 出镜像后你在 NAS 冒烟,通过再打 `v1.0.0`。
- **B**:直接 `v1.0.0`。

**⑤ LICENSE 是否维持 AGPL-3.0**
- **A(推荐)**:维持 AGPL-3.0。用户自己 `docker run` 属「自用」,不触发开源义务;你若日后做 SaaS 需开源后端——这与项目定位(自托管容器)一致。
- **B**:换 MIT/Apache-2.0(更利于他人二次开发闭源分发,但失去 copyleft 保护)。

---

## 六、风险与已知坑(实施时照此避)

1. **容器内无 docker daemon** → 镜像层只能靠 Actions 验证;别在本容器反复试 `docker build`(会一直失败)。
2. **`--workers 1` 必须固定** → 配置注入是进程内 `os.environ`,多 worker 会出现「A worker 有新 key、B 没有」。
3. **非 root + 卷权限** → NAS 上挂 `./data` 时宿主目录属主可能不是 uid 1000,SQLite 建库会 `unable to open database file`;entrypoint 里要检测并给中文提示(compose 里注明 `user: "1000:1000"` 或让用户 chown)。
4. **高德两 key 平台隔离** → 向导第①②步必须写清「Web服务 key」与「Web端(JSAPI) key」是**同应用下两个不同 key**,混用报 `USERKEY_PLAT_NOMATCH`;安全密钥必须**先于** JS 脚本写入(`window._AMapSecurityConfig`),否则 `INVALID_USER_SCODE`——这两条已是血泪坑,文案要原文照抄进向导。
5. **QPS 限流** → 向导校验 key 时只打**一次** geocode,别做批量探活(会触发 `infocode=10021`);运行时已有 0.6s 最小间隔 + 2s/5s 退避,不要动。
6. **`.dockerignore` 必须排 `.env`、`backend/data/`、`.venv/`、`.git/`** → 否则会把宿主 db(2.8MB,含 463+ 条真实 POI 与 LLM 简介)和你的凭据打进镜像层(且镜像要发 GHCR)。
7. **AppConfig 白名单** → `/api/config` 若允许写任意键,等于给了远程改 `WHERE2GO_DB_URL`/`WHERE2GO_PROXY_*` 的能力(可把流量导向攻击者代理)。必须硬编码允许写的 7 个键。
8. **AGPL + 容器分发** → GHCR 发镜像属「分发」,需在镜像内保留 LICENSE 与源码获取途径(README 写明仓库地址即可满足)。
9. **前端 index.html 已 283KB / 5000+ 行** → 向导**另起 setup.html**,不要塞进 index.html(避免再一次巨型文件改动 + 前端契约断言连锁失败)。
10. **pytest 基线 1632 只增不减** → P2/P3 新增用例全 mock(向导校验端点用假 key + monkeypatch,不真打高德)。

---

## 七、验收总口径(全部满足才算发布就绪)

1. `docker compose up -d` 在**干净的 NAS/虚拟机**上能起(无宿主 .env 也能起,走向导配置)。
2. 浏览器打开 → 见横幅 → 进 `/setup` → 4 步走完 → 回首页搜「上海」出 pin、出 AI 推荐、出路线与住宿。
3. `docker compose down && up -d` 后**配置与已抓数据都还在**(卷生效)。
4. 换镜像 tag 升级后数据不丢、配置不丢。
5. README quickstart 三条命令可复制即跑;`WHERE2GO_*` 全表与代码一致(grep 核)。
6. pytest 全绿(基线 1632 + 新增);Actions CI + Release 双流水线绿;GHCR 有 multi-arch 镜像。
7. 未配 LLM key 时服务可用(简介/推荐降级,不 500、不白屏)。
8. `docker run` 镜像内 grep 不到任何真实 key、不含 `backend/data/where2go.db`。
