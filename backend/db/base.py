"""数据库引擎与会话:SQLite 文件库(默认)+ 可注入 URL(测试/脚本)。

默认库文件 ``backend/data/where2go.db``,可用环境变量 ``WHERE2GO_DB_URL`` 覆盖
(例如 ``sqlite:////tmp/x.db`` 或 ``sqlite:///:memory:``)。

引擎是**懒加载**的:import 本模块不会建目录、不会建表,第一次 :func:`get_engine`
才落盘,保证 `pytest` 与 `import app.main` 都不产生副作用。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Base

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BACKEND_DIR / "data" / "where2go.db"
ENV_DB_URL = "WHERE2GO_DB_URL"

_engine: Optional[Engine] = None


def database_url(url: Optional[str] = None) -> str:
    """解析库 URL:显式参数 > 环境变量 > 默认 SQLite 文件。

    SQLite 额外补 ``check_same_thread=False``:FastAPI 的同步端点跑在线程池里,
    连接会在不同线程间复用,不加这个参数 SQLAlchemy 会直接报错。
    """
    resolved = (url or os.environ.get(ENV_DB_URL) or f"sqlite:///{DEFAULT_DB_PATH}").strip()
    if not resolved:
        raise ValueError("数据库 URL 不能为空")
    if resolved.startswith("sqlite") and "check_same_thread" not in resolved:
        resolved += ("&" if "?" in resolved else "?") + "check_same_thread=False"
    return resolved


def make_engine(url: Optional[str] = None) -> Engine:
    """按 URL 新建引擎(不动进程内共享引擎);内存库用 StaticPool 才能跨会话共享。"""
    resolved = database_url(url)
    kwargs: dict[str, Any] = {}
    if resolved.startswith("sqlite"):
        _ensure_sqlite_dir(resolved)
        if ":memory:" in resolved:
            kwargs["poolclass"] = StaticPool
    return create_engine(resolved, **kwargs)


def _ensure_sqlite_dir(url: str) -> None:
    """文件型 SQLite:确保父目录存在(:memory: 与相对/绝对路径都要处理)。"""
    if ":memory:" in url:
        return
    path = url.split("?", 1)[0].replace("sqlite:///", "", 1)
    if not path:
        return
    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)


def init_db(engine: Engine) -> None:
    """建表(幂等:create_all 只建缺失的表)。"""
    Base.metadata.create_all(engine)


def get_engine() -> Engine:
    """进程内共享引擎(懒加载 + 自动建表)。"""
    global _engine
    if _engine is None:
        _engine = make_engine()
        init_db(_engine)
    return _engine


def set_engine(engine: Optional[Engine]) -> None:
    """替换/清空共享引擎(测试与脚本用;传 None 表示下次重新按环境变量加载)。"""
    global _engine
    _engine = engine


def session_factory(engine: Optional[Engine] = None) -> sessionmaker[Session]:
    """会话工厂:``expire_on_commit=False`` 让 commit 后仍能读对象属性。"""
    return sessionmaker(bind=engine or get_engine(), autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI 依赖:每请求一个 Session,请求结束即关闭。"""
    session = session_factory()()
    try:
        yield session
    finally:
        session.close()


def open_session(engine: Optional[Engine] = None) -> Session:
    """给脚本/CLI 用的会话(调用方负责 commit 与 close)。"""
    return session_factory(engine)()
