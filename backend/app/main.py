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
"""
from __future__ import annotations
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from .api import collections, discover, places, routes, stays, trips

app = FastAPI(title="Where2Go 试用")
app.include_router(discover.router, prefix="/api")
app.include_router(places.router, prefix="/api")
app.include_router(routes.router, prefix="/api")
app.include_router(collections.router, prefix="/api")
app.include_router(stays.router, prefix="/api")
app.include_router(trips.router, prefix="/api")

STATIC = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
