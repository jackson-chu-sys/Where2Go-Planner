# Where2Go 自托管容器(方案见 docs/CONTAINER-SETUP-PLAN.md P1,神朱 2026-10-15 拍板)
# 构建:docker build -t where2go .
# 运行:推荐 docker compose up -d(见 docker-compose.yml);数据全落 /data 卷,升级镜像不丢。
FROM python:3.13-slim

# 中文报错/日志不乱码;Python 不写 .pyc、日志即时刷出(容器惯例)
ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 非 root 运行(uid 1000,与 NAS 常见用户一致);/data 是唯一的可写卷(SQLite 库)
RUN useradd --create-home --uid 1000 where2go \
    && mkdir -p /data \
    && chown where2go:where2go /data

WORKDIR /app

# 先装依赖再拷代码:requirements 不变时缓存层可复用(纯 Python 依赖,无编译链)
COPY --chown=where2go:where2go backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 代码 + 前端静态页(tools/docs/内部文件已被 .dockerignore 排除)
COPY --chown=where2go:where2go backend /app/backend

USER where2go

# 数据卷:SQLite 库落这里(WHERE2GO_DB_URL 指进 /data,别改路径,否则升级丢数据)
VOLUME ["/data"]
ENV WHERE2GO_DB_URL=sqlite:////data/where2go.db \
    WHERE2GO_UI_MODE=release

EXPOSE 8000

# 健康检查:/api/places/meta 无 key 也 200(实测),只验「服务活着」,不验配置
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/places/meta',timeout=4).status==200 else 1)"

# entrypoint:检查 /data 可写(不可写给中文提示)+ 注入 AppConfig(见 P2)+ exec uvicorn
COPY --chown=where2go:where2go docker-entrypoint.sh /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]
