"""Where2Go 服务:静态页面(地图模式)+ /api。

路由:
* POC(阶段0,保留不动):``POST /api/discover``、``GET /api/categories``
* 阶段1a(地图骨架):``GET /api/places``、``GET /api/places/meta``、``GET /api/geocode``
* 阶段1c(打磨):``GET /api/geocode/reverse``(浏览器"我的位置" → 逆地理编码起点)、
  ``GET /api/places/intros``(LLM 补简介);滑雪/运动缺口由 ``services.seed_data`` 人工种子垫底
* 阶段2a(路线交通 M2):``GET /api/routes``(驾车高德真实 + 铁路/飞机估算,含费用估算与
  高德/Google/12306/OTA 跳转链接,见 ``services.routes``)
* 阶段2c(路线收藏):``POST /api/collections``(幂等 upsert)、``GET /api/collections``
  (可按 kind / 分组过滤)、``DELETE /api/collections/{id}``;存的是收藏那一刻的快照摘要
  (方式/时长/费用/里程),为 M4「统一收藏面板」铺路,见 ``app/api/collections.py``
* 阶段3a(住宿 M3):``GET /api/stays``(起点坐标或 place_id + 半径 → 周边落脚点,
  带 AI 价格区间**估算**与简介,DB 即缓存,见 ``app/api/stays.py`` / ``services/stays.py``)
* M4(行程方案,TASK-5a):``POST /api/trip-plans``(按名字幂等 upsert)、
  ``GET /api/trip-plans``(新的在前,每项带报价)、``GET/DELETE /api/trip-plans/{id}``;
  总花费只按收藏快照的"当时口径"**估算**(不重新调 ``/api/routes``),见 ``app/api/trips.py``
* TASK-10b(AI 行程规划,阶段 A):``POST/GET/DELETE /api/planner/messages``(多轮对话 +
  历史回放 + 清空)与 ``POST /api/planner/save``(把 AI 排的地点**幂等**落成既有 ``TripPlan``);
  编排在 ``services/planner.py``,路由见 ``app/api/planner.py``
* TASK-11a(全局 AI 助手,只做后端):``POST/GET/DELETE /api/assistant/messages``
  (通用旅行问答 + 历史回放 + 清空;会话 key 带 ``chat-`` 前缀,**复用** planner 的两张会话表);
  编排在 ``services/assistant.py``,路由见 ``app/api/assistant.py``
* TASK-P2(容器化配置):``GET/POST /api/config``(``/setup`` 向导的配置读写,**只回掩码**、
  单键保存即热生效)与 ``GET /api/health``(恒 200,报缺哪些必填项 + 版本号);
  白名单与注入逻辑在 ``services/app_config.py``,路由见 ``app/api/config.py``。
  启动时由下面的 ``lifespan`` 把库里(``AppConfig``)的配置注入 ``os.environ``
  —— **只填 env 未设的键**,所以容器 ``-e`` / ``.env`` 永远压过向导存的值,
  那 30 处直接读 env 的代码一行不改(见 docs/CONTAINER-SETUP-PLAN.md §2)。
"""
from __future__ import annotations
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from .api import assistant, collections, config, discover, planner, places, routes, stays, trips
from db.base import open_session
from services import app_config


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """启动:把向导存进 SQLite 的配置注入 ``os.environ``(**env 已有的键不覆盖**)。

    用 ``lifespan`` 而不是已弃用的 ``@app.on_event("startup")``。注入发生在 import 之后、
    第一个请求之前,所以 ``data_sources`` / ``services`` 里那 30 处 ``os.environ.get(...)``
    照常现读就能拿到值;建表由 ``open_session`` → ``get_engine`` → ``init_db`` 顺带完成
    (旧库 ``create_all`` 自动补 ``app_config`` 表,无需迁移)。

    刻意**不打日志**(仓库无 logging 惯例,且值可能是明文密钥):想知道哪些键生效、
    来自 env 还是向导,打 ``GET /api/config`` 看每项的 ``configured`` / ``source``。
    """
    with open_session() as session:
        app_config.load_into_environ(session)
    yield


app = FastAPI(title="Where2Go 试用", lifespan=lifespan)
app.include_router(discover.router, prefix="/api")
app.include_router(places.router, prefix="/api")
app.include_router(routes.router, prefix="/api")
app.include_router(collections.router, prefix="/api")
app.include_router(stays.router, prefix="/api")
app.include_router(trips.router, prefix="/api")
app.include_router(planner.router, prefix="/api")
app.include_router(assistant.router, prefix="/api")
app.include_router(config.router, prefix="/api")

STATIC = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
