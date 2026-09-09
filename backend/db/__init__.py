"""Where2Go 持久层:SQLite + SQLAlchemy 2.0(TASK-1a)。

分三个模块:

* :mod:`db.models` —— ``Place`` / ``SegmentFetch`` 表定义;
* :mod:`db.base` —— 引擎、会话工厂、建表与 FastAPI 依赖;
* :mod:`db.repository` —— 读写封装(upsert 防重、按 (城市, band, 分类) 查询)。

用法::

    from db import open_session, init_db, make_engine
    from db import repository as repo

    engine = make_engine()          # 默认 backend/data/where2go.db
    init_db(engine)
    with open_session(engine) as session:
        rows = repo.list_places(session, origin_city="上海", band="50_100")
"""

from .base import (
    DEFAULT_DB_PATH,
    ENV_DB_URL,
    database_url,
    get_engine,
    get_session,
    init_db,
    make_engine,
    open_session,
    session_factory,
    set_engine,
)
from .models import (
    UNCATEGORIZED,
    Base,
    Place,
    SegmentFetch,
    iso_utc,
    utcnow,
)
from .repository import (
    count_by_category,
    get_segment,
    latest_city_origin,
    list_places,
    place_to_dict,
    record_segment,
    segment_overview,
    segment_to_dict,
    upsert_places,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "ENV_DB_URL",
    "UNCATEGORIZED",
    "Base",
    "Place",
    "SegmentFetch",
    "database_url",
    "get_engine",
    "get_session",
    "init_db",
    "make_engine",
    "open_session",
    "session_factory",
    "set_engine",
    "utcnow",
    "iso_utc",
    "count_by_category",
    "get_segment",
    "latest_city_origin",
    "list_places",
    "place_to_dict",
    "record_segment",
    "segment_overview",
    "segment_to_dict",
    "upsert_places",
]
