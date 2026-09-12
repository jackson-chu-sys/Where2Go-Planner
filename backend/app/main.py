"""Where2Go 服务:静态页面(地图模式)+ /api。

路由:
* POC(阶段0,保留不动):``POST /api/discover``、``GET /api/categories``
* 阶段1a(地图骨架):``GET /api/places``、``GET /api/places/meta``、``GET /api/geocode``
* 阶段1c(打磨):``GET /api/geocode/reverse``(浏览器"我的位置" → 逆地理编码起点)、
  ``GET /api/places/intros``(LLM 补简介);滑雪/运动缺口由 ``services.seed_data`` 人工种子垫底
* 阶段2a(路线交通 M2):``GET /api/routes``(驾车 OSRM 真实 + 铁路/飞机估算,含费用估算与
  高德/Google/12306/OTA 跳转链接,见 ``services.routes``)
* 阶段2c(路线收藏):``POST /api/collections``(幂等 upsert)、``GET /api/collections``
  (可按 kind / 分组过滤)、``DELETE /api/collections/{id}``;存的是收藏那一刻的快照摘要
  (方式/时长/费用/里程),为 M4「统一收藏面板」铺路,见 ``app/api/collections.py``
"""
from __future__ import annotations
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from .api import collections, discover, places, routes

app = FastAPI(title="Where2Go 试用")
app.include_router(discover.router, prefix="/api")
app.include_router(places.router, prefix="/api")
app.include_router(routes.router, prefix="/api")
app.include_router(collections.router, prefix="/api")

STATIC = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
