# 04 · 部署与发版

## 镜像坐标

```
ghcr.io/jackson-chu-sys/where2go
tag: 0.1.0-beta | 0.2.0-beta | latest      ← 注意 semver 模式产出的 tag 不带 v
平台: linux/amd64 + linux/arm64            ← 同一份 OCI index
可见性: public(匿名可拉,不需要 docker login)
```

`latest` 永远指向最近一次 tag 构建。当前 `latest` == `0.2.0-beta` 构建。

## 版本历史

| 版本 | 日期 | 内容 |
|---|---|---|
| `v0.1.0-beta` | 2026-10-16 | 容器化基线:P1 Docker/compose、P2 配置 API、P3 `/setup` 向导、P5 README、P6 release UI;含 entrypoint 执行位修复 |
| `v0.2.0-beta` | 2026-10-19 | 境外中文城市名定位修复(TASK-27):Photon 行政区级判据、境外链境内证据双源、Overpass 走代理;pytest 1739 |

`v1.0.0` **尚未发布**。发布条件见 `05-status-and-todo.md`。

## 本地构建(没有流水线时)

```bash
docker build -t where2go .
# 或
docker compose up -d --build
```

`.dockerignore` 已排除 `docs/`、`tools/`、`.env`、`backend/data/*.db`。**镜像里绝不能出现真实库文件和 `.env`**,Release 流水线的冒烟步骤里有硬断言守着(见下)。

## CI 与 Release 流水线

**CI**(`.github/workflows/ci.yml`):push 到 `main` 或任何 PR 触发。ubuntu-latest + Python 3.13 + `pip install -r backend/requirements.txt`,然后 `cd backend && python -m pytest -q`。**零 secrets、零网络**,20 分钟超时。

**Release**(`.github/workflows/release.yml`):推 `v*` tag 触发,步骤顺序是:

1. checkout → QEMU → buildx → 登录 GHCR(用 `GITHUB_TOKEN`)
2. `docker/metadata-action`:semver 版本 tag + `latest`
3. **先构建一份 amd64 镜像到本机,跑冒烟**:无 key 起容器,轮询 `/api/health` 直到 200,再 `curl /api/health`、`/api/places/meta`、`/setup.html`;然后用两条 `! docker exec ... test -e` 断言镜像里**没有** `backend/data/where2go.db`、**没有** `.env`
4. 冒烟通过后才 `build-push-action` 推 multi-arch 到 GHCR

所以"镜像能起来"这件事在合并前就被验过了。仓库 Settings → Actions 需要「Read and write packages」权限。

## 容器运行参数(三处必配)

```yaml
image: ghcr.io/jackson-chu-sys/where2go:latest
ports:   ["8000:8000"]
volumes: ["./data:/data"]        # SQLite 库与全部缓存都在这里,升级/重建不丢
```

可选但常见:

- `env_file: .env`(也可以完全不用,走 `/setup` 向导把配置存进数据库)
- 境外功能必须:`HTTPS_PROXY` / `HTTP_PROXY`(境外链的 Nominatim 与 Overpass 在国内直连不通)
- 宿主目录属主不是 uid 1000 时:解开 compose 里的 `user: "1000:1000"`,或 `sudo chown -R 1000:1000 ./data`

**页面**:`http://<设备IP>:8000`,首次进去顶部有横幅引导学生进 `/setup`(四步:高德 Web 服务 key → JS key + 安全密钥 → LLM key 可跳过 → 完成)。配置存进 `app_config` 表,**重建容器不丢**。

## 当前部署实例(交接时)

- **NAS(绿联 DXP4800 Plus)宿主 `192.168.1.210`,端口 `8001`** → 跑着 `v0.1.0-beta` 镜像(release UI,`/api/health` 200,向导已配置过)。
- 该实例**不含** TASK-27 的境外修复 → 在它上面搜「因特拉肯」仍会被高德吸附到成都双流。
- 待办:在 NAS Docker 应用里「更新镜像」(会重新 pull `:latest` → 拿到 `0.2.0-beta` 构建),并补上代理环境变量。
- 宿主 `:8000` 是开发实例(直接跑 uvicorn 的那台),两者数据独立,不要混淆。

## 更新与回滚

```bash
# compose 方式
docker compose pull && docker compose up -d

# docker run 方式
docker pull ghcr.io/jackson-chu-sys/where2go:latest
docker rm -f where2go && docker run -d --name where2go ...   # data 卷不动就安全
```

回滚:把 `image:` 换成旧 tag(`:0.1.0-beta`)重建即可。数据库结构向后兼容,没做过破坏性迁移。

## 发版 checklist

1. `cd backend && ../.venv/bin/python -m pytest -q` → 全绿,记下通过数
2. 队列里相关条目的状态与"结果"段已更新(通过数、commit 哈希)
3. 提交并**申请 push 授权**,推 `main`
4. `git tag -a vX.Y.Z-beta -m "..."` + `git push origin vX.Y.Z-beta`
5. 等 Release 绿(约 6~8 分钟),用匿名 registry 接口核对 tag 与双架构:
   ```
   GET https://ghcr.io/token?scope=repository:jackson-chu-sys/where2go:pull
   GET /v2/jackson-chu-sys/where2go/tags/list
   GET /v2/jackson-chu-sys/where2go/manifests/<tag>   (看 manifests[].platform)
   ```
6. 通知产品负责人更新镜像,并做真机冒烟(见下)

## 真机冒烟清单

```bash
curl -s http://<IP>:8001/api/health          # {"status":"ok","missing_required":[],"version":"..."}
curl -s -o /dev/null -w '%{http_code}\n' http://<IP>:8001/          # 200
curl -s http://<IP>:8001/api/places/meta | head -c 200              # ui_mode=release
```

浏览器侧:首页是发布版样式(技术细节隐藏、合规标注保留)→ `/setup` 可进 → 搜一个境内城市出 pin → 搜「因特拉肯」应定位到瑞士(需代理)→ `docker compose down && up -d` 后数据与配置还在。

冒烟通过才打 `v1.0.0`(产品负责人 2026-10-15 的决策④:beta 先行,NAS 冒烟过再正式版)。
