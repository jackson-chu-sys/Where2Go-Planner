"""Where2Go 试用服务:静态页面 + /api。"""
from __future__ import annotations
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from .api import discover

app = FastAPI(title="Where2Go 试用")
app.include_router(discover.router, prefix="/api")

STATIC = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
