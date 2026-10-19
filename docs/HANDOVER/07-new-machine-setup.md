# 07 · 换机器重建清单

> 这份文档就是为"**代码搬到另一台机器、换另一个 Agent 接手**"写的。原来的开发环境是绿联 NAS 上的 Hermes 容器,很多东西是那台机器的本地状态,**不会跟着 git 走**。
>
> 📌 **本次交接的实际情况是新机器不配代理、不排夜班** —— 那种情况请看 `08-new-machine-brief.md`(直接可用版,含后续任务清单),本份作为通用清单保留(含代理与定时任务的迁移办法)。

---

## 先判断你要哪种环境

| 场景 | 需要装什么 | 看哪节 |
|---|---|---|
| 只想把它跑起来给人看 / 自己用 | 只要 Docker | §1 |
| 要改代码、跑测试、发版 | Python 3.13 + git + Docker(发版用) | §2 起全部 |

---

## §1 只跑容器(不需要源码)

```bash
docker pull ghcr.io/jackson-chu-sys/where2go:latest
docker run -d --name where2go --restart unless-stopped \
  -p 8000:8000 -v "$PWD/data:/data" \
  ghcr.io/jackson-chu-sys/where2go:latest
```

打开 `http://localhost:8000` → 走 `/setup` 向导填三把高德 key。**到这就完事了**,不需要 Python、不需要仓库。

要境外目的地,加三个环境变量(否则境外功能不可用):

```
-e HTTPS_PROXY=<你的代理> -e HTTP_PROXY=<你的代理>
-e NO_PROXY=localhost,127.0.0.1,restapi.amap.com,photon.komoot.io,router.project-osrm.org
```

---

## §2 要开发:装什么

| 组件 | 版本/说明 | 备注 |
|---|---|---|
| Python | **3.13.x** | 旧版没测过;不要用系统自带的旧 python |
| uv | 最新即可 | 用来建 venv(系统 python 通常没有 pip,且 PEP 668 会拦) |
| git | 任意较新版本 | |
| Docker | 仅发版/验镜像时需要 | 日常开发不需要 |

```bash
git clone https://github.com/jackson-chu-sys/Where2Go-Planner.git
cd Where2Go-Planner
uv venv .venv --python 3.13
.venv/bin/python -m pip install -r backend/requirements.txt
```

依赖只有 6 个包:requests / fastapi / uvicorn / pydantic / sqlalchemy / pytest(见 `backend/requirements.txt`)。venv 大约 60 MB。前端没有 npm,没有构建步骤。

验证:

```bash
cd backend && ../.venv/bin/python -m pytest -q     # 期望 1739 passed,约 126s,不触网
```

跑不绿就先别往下走 —— 说明 Python 版本或依赖不对。

---

## §3 必须自己准备的环境变量

仓库里**没有任何真实密钥**。你需要在一个新文件里准备好这些(可以放 `.env`,也可以直接注入环境变量):

| 变量 | 从哪来 | 缺了会怎样 |
|---|---|---|
| `WHERE2GO_AMAP_KEY` | 高德开放平台免费申请(Web 服务 key) | 境内搜不了、地图空 |
| `WHERE2GO_AMAP_JS_KEY` | 同上,建「Web端(JS API)」key | 前端地图起不来 |
| `WHERE2GO_AMAP_SECURITY_JS_CODE` | 建 JS key 时一并给出 | 报 `INVALID_USER_SCODE` |
| `DEEPSEEK_API_KEY` 或 `ALIBABA_TOKEN_PLAN_API_KEY` 或 `WHERE2GO_LLM_API_KEY` | 你的 LLM 账号 | AI 简介/推荐/行程规划降级为空,**不报错** |
| `HTTPS_PROXY` / `HTTP_PROXY` | 你的代理 | **境外目的地完全不可用** |

当前这台的取值在旧机器的 `/opt/data/.env`(Hermes 容器内,权限 0600),里面只有 `WHERE2GO_AMAP_*` 三个键和代理,LLM 用的是 `ALIBABA_TOKEN_PLAN_API_KEY` / `DEEPSEEK_API_KEY`。

**搬家姿势**:把旧机器这三个 key 的值抄过去,或者去高德重新申请。**不要**把整个 `/opt/data/.env` 复制进仓库目录(那里还有 Hermes 自己的凭据,和本项目无关)。

代理地址注意:旧环境用的是 NAS 上的 mihomo(`http://192.168.1.210:7892`)。新机器如果不在同一个局域网,这个地址不通,要换成新机器自己的代理。

---

## §4 不会跟着 git 走的三样东西

### 4.1 开发库(`backend/data/where2go.db`)

不入 git(被 `.gitignore` 挡着)。新机器从空库开始,`init_db()` 会自动建表,种子数据会自动灌。**不要从旧机器拷贝这个库**,里面有过期的脏缓存(历史上出过 `北海道 → 香港北海街`、`新加坡 → 陕西咸阳新加坡村` 这类错行)。

容器部署时的库在 `./data/where2go.db`(宿主卷),那是用户数据,升级镜像不丢;**开发库和生产库是两个文件,别混**。

### 4.2 Git 凭据

```bash
git config --local credential.helper ''
git config --local credential.helper 'store --file=/绝对路径/.git-credentials'
```

两步缺一不可(先清空继承链,再加 store,否则会先用全局文件里的错 token)。文件内容格式 `https://<user>:<PAT>@github.com`,权限 0600。

PAT 需要 **repo** 权限;如果还要动 `.github/workflows/`,必须额外有 **workflow** scope,否则任何触碰该目录的 commit 会被远端**整推拒收**。

验凭据别用 `ls-remote`(公开仓库读操作不需要认证,会假通过)。用:

```bash
curl -s -H "Authorization: Bearer <PAT>" https://api.github.com/user
git push --dry-run
```

注意 `store` helper 在认证失败时会**自动删掉**凭据行。

### 4.3 夜班定时任务

旧环境的每晚 22:00 执行器是一条 Hermes cron 任务(`docs/NIGHTLY-QUEUE.md` 驱动的),**不在仓库里,换环境必须重新注册**,否则队列永远没人处理。

重新注册时提示词必须包含:读 `docs/NIGHTLY-QUEUE.md` → 头部「当日生效口径通告」优先 → 遵守 R8/R9/R10 与 40 分钟止损 → 只处理 `pending` 且依赖已 done 的条目 → 队列空则直接跳过不回复 → 每条做完更新状态与结果段 → **只 commit 不 push** → 单晚最多 4 条。

如果新环境不用 Hermes,用别的调度器也行,把上面这些写进提示词即可。

---

## §5 换机器后的验收清单

按顺序做,全过就算交接完成:

```bash
# 1. 代码与依赖
cd Where2Go-Planner
git log --oneline -1                 # 应是 961624b 或更新
cd backend && ../.venv/bin/python -m pytest -q    # 1739 passed 左右

# 2. 起服务(需要 .env)
set -a && source <你的 .env 路径> && set +a
../.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000 &
curl -s localhost:8000/api/health    # status=ok

# 3. 境内链路(用你配的 key)
curl -s "localhost:8000/api/geocode?city=上海"       # geocoder=amap, region=domestic

# 4. 境外链路(验证代理与 TASK-27 修复)
curl -s "localhost:8000/api/geocode?city=苏黎世"     # 应是瑞士 47.37/8.54,不是天津
curl -s "localhost:8000/api/geocode?city=上海&region=overseas"   # 应自动降级 domestic + note,不是 400

# 5. 前端
# 浏览器打开 localhost:8000,搜一个城市应出 pin;界面上技术细节是否隐藏取决于 WHERE2GO_UI_MODE
```

第 4 步是最容易翻车的:苏黎世如果仍旧定位到国内,说明代理没配好或代码是旧版本。

---

## §6 换成别的 Agent 框架时要注意

旧环境是 Hermes Agent + 一个本地 skill(`where2go-project`),那份 skill 里积累了很多实测结论,内容已经合进 `docs/HANDOVER/` 的 01/03 两份。**新环境不带这个 skill 也没关系,以本目录的文档为准。**

Agent 需要提前知道的三件事(否则会重犯旧错):

1. **只 commit 不 push**,推远端要单独授权。
2. 产品负责人**不写代码**,技术方案由 Agent 定;要他做选择时给**可点选的选项**。
3. 历史结论都带日期和前提 —— 引用前先核对当时的配置今天是否还生效(踩过:旧结论说"上下文窗口装不下前端",实际是配置文件把窗口手动设成了 64K)。

`AGENTS.md` 是给 Agent 冷启动看的,保持同步;它和本目录冲突时,以本目录 + 仓库当前代码为准(AGENTS.md 只有 32 行,是索引不是事实源)。
