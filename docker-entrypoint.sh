#!/bin/bash
# Where2Go 容器入口(方案见 docs/CONTAINER-SETUP-PLAN.md P1 / 风险§3)
set -e

DATA_DIR="${WHERE2GO_DATA_DIR:-/data}"

# /data 必须可写(SQLite 库要落在这里)。NAS 挂宿主目录时属主常常不是 uid 1000,
# 这里提前检测并给中文提示,别让用户对着 "unable to open database file" 猜。
if ! touch "$DATA_DIR/.w2g-write-test" 2>/dev/null; then
    echo "==============================================================" >&2
    echo "错误:数据目录 $DATA_DIR 不可写,服务无法启动。" >&2
    echo "" >&2
    echo "容器以 uid 1000(where2go)运行,而挂载进来的目录属主不是它。" >&2
    echo "请在宿主机执行(把 ./data 换成你的实际挂载路径):" >&2
    echo "    sudo chown -R 1000:1000 ./data" >&2
    echo "或在 docker-compose.yml 里加一行:  user: \"$(id -u):$(id -g)\"" >&2
    echo "==============================================================" >&2
    exit 1
fi
rm -f "$DATA_DIR/.w2g-write-test"

echo "[where2go] 数据目录:$DATA_DIR(库文件:$WHERE2GO_DB_URL)"
echo "[where2go] 未配置高德 key 时页面会提示前往 /setup 向导;配置保存在数据库,重建容器不丢。"

# 单 worker:AppConfig 配置注入是进程内 os.environ(见 CONTAINER-SETUP-PLAN §2),
# 多 worker 会出现「有的进程有新 key、有的没有」。SQLite 单容器场景 1 个 worker 足够。
exec python -m uvicorn app.main:app \
    --app-dir /app/backend \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 1
