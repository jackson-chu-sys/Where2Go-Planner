"""Where2Go 服务:静态页面(地图模式)+ /api。

路由:
* POC(阶段0,保留不动):``POST /api/discover``、``GET /api/categories``
* 阶段1a(地图骨架):``GET /api/places``、``GET /api/places/meta``、``GET /api/geocode``
"""
from __future__ import annotations
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from .api import discover, places

app = FastAPI(title="Where2Go 试用")
app.include_router(discover.router, prefix="/api")
app.include_router(places.router, prefix="/api")

STATIC = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
