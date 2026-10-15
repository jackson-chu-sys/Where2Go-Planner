"""TASK-P2 单测:``AppConfig`` 表 + ``services/app_config`` + ``/api/config``、``/api/health``。

全程不触网、不碰 ``backend/data/``:

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错(本模块压根不该联网);
* ``isolated_env`` 快照 ``os.environ`` 并**清空白名单 7 键 + LLM 各家 key**
  (:func:`app_config.set_key` / :func:`app_config.load_into_environ` 会真的写 ``os.environ``,
  不清就会串味,也会让宿主机的真 key 干扰 ``resolve_provider`` 断言);
* DB 用 ``tmp_path`` 下的临时 SQLite 文件;完整 HTTP 链用手拼的最小 ASGI scope 驱动
  (仓库没装 httpx/TestClient),``app.dependency_overrides`` 把 ``get_session`` 换成同一个会话。

重点覆盖四条口径:①**env 压过库**(启动注入只填 env 未设的键);②**热生效**
(保存后 ``amap.resolve_key()`` / ``intro.resolve_provider()`` 立刻看到新值,无需重启);
③**白名单**(白名单外的键拒写,``/api/config`` 不是任意 env 注入面);
④**明文不出接口**(``json.dumps`` 后 grep 不到任何 key 原文,照 test_classify.py 的先例)。

运行:``cd backend && ../.venv/bin/python -m pytest -q``
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Callable, Iterator

import pytest
import requests
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app import main as main_module  # noqa: E402
from app.api import config as config_api  # noqa: E402
from app.main import app  # noqa: E402
from data_sources import DataSourceError, amap  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db import base as db_base  # noqa: E402
from db.base import get_session  # noqa: E402
from db.models import AppConfig  # noqa: E402
from services import app_config  # noqa: E402
from services import intro as intro_service  # noqa: E402

# --------------------------------------------------------------------------- #
# 样本数据(**全是假 key**,只为断言"明文不出接口")
# --------------------------------------------------------------------------- #

FAKE_AMAP_KEY = "sk-amap-fake-0123456789abcdef"
FAKE_JS_KEY = "sk-jsapi-fake-9876543210zyxw"
FAKE_SCODE = "scode-fake-abcdef0123456789"
FAKE_LLM_KEY = "sk-llm-fake-abcdefghijklmnopqrstuvwxyz"
SECRET_VALUES = (FAKE_AMAP_KEY, FAKE_JS_KEY, FAKE_SCODE, FAKE_LLM_KEY)

AMAP_KEYS = (
    app_config.ENV_AMAP_KEY,
    app_config.ENV_AMAP_JS_KEY,
    app_config.ENV_AMAP_SECURITY_JS_CODE,
)
LLM_ENVS = tuple(
    sorted(
        {key for provider in intro_service.PROVIDERS for key in provider.env_api_keys}
        | {intro_service.ENV_API_KEY}
    )
)

REQUIRED_COLUMNS = {"id", "key", "value", "source", "updated_at"}
ITEM_KEYS = {
    "key", "label", "hint", "required", "secret", "group", "configured", "source", "masked",
}
EXPECTED_KEYS = {
    "WHERE2GO_AMAP_KEY", "WHERE2GO_AMAP_JS_KEY", "WHERE2GO_AMAP_SECURITY_JS_CODE",
    "WHERE2GO_LLM_PROVIDER", "WHERE2GO_LLM_API_KEY", "WHERE2GO_LLM_BASE_URL",
    "WHERE2GO_LLM_MODEL",
}


# --------------------------------------------------------------------------- #
# 断言小工具(照 test_collections.py 的口径)
# --------------------------------------------------------------------------- #


def expect_http(func: Callable[[], Any], status_code: int = 400, *fragments: str) -> HTTPException:
    """断言 ``func()`` 抛出指定状态码的 :class:`HTTPException`,且 detail 含全部 ``fragments``。"""
    with pytest.raises(HTTPException) as caught:
        func()
    exc = caught.value
    assert exc.status_code == status_code, f"应为 HTTP {status_code},实际 {exc.status_code}"
    for fragment in fragments:
        assert fragment in str(exc.detail), f"detail 应包含 {fragment!r},实际:{exc.detail}"
    return exc


def expect_error(func: Callable[[], Any], exc_type: type, *fragments: str) -> Exception:
    """断言 ``func()`` 抛出 ``exc_type``,且错误信息包含全部 ``fragments``。"""
    with pytest.raises(exc_type) as caught:
        func()
    for fragment in fragments:
        assert fragment in str(caught.value), f"应包含 {fragment!r},实际:{caught.value}"
    return caught.value


def assert_no_plaintext(payload: Any, values: tuple[str, ...] = SECRET_VALUES) -> str:
    """把响应整体 ``json.dumps`` 后 grep:**任何明文 key 都不许出现**(照 test_classify.py 先例)。"""
    dumped = json.dumps(payload, ensure_ascii=False)
    for secret in values:
        assert secret not in dumped, f"明文 key 绝不能出现在响应/日志里:{secret[:6]}…"
    return dumped


def seed_row(session: Any, key: str, value: str, source: str = "setup") -> AppConfig:
    """直接往 ``app_config`` 表塞一行(模拟向导早先存过的配置)。"""
    row = AppConfig(key=key, value=value, source=source)
    session.add(row)
    session.commit()
    return row


def rows_of(session: Any, key: str) -> list[AppConfig]:
    return list(session.scalars(select(AppConfig).where(AppConfig.key == key)))


def item_of(payload: dict[str, Any], key: str) -> dict[str, Any]:
    for item in payload["items"]:
        if item["key"] == key:
            return item
    raise AssertionError(f"GET /api/config 的 items 里没有 {key}")


def http_request(method: str, path: str, *, body: Any = None, query: str = "") -> tuple[int, Any]:
    """直接驱动 ASGI app 走一遍**完整 HTTP 链**(含 FastAPI 解析),返回 ``(状态码, JSON)``。

    仓库没装 httpx/TestClient,所以自己拼最小 scope;DB 会话由 ``http`` fixture 覆盖注入。
    """
    raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
        ],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    chunks = [raw]
    payload = bytearray()
    status: dict[str, int] = {}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": chunks.pop(0) if chunks else b"",
                "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            payload.extend(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return status["code"], json.loads(payload.decode() or "null")


def run_lifespan() -> None:
    """跑一遍 ``app.main.lifespan``(启动注入),用完即退出上下文。"""

    async def drive() -> None:
        async with main_module.lifespan(main_module.app):
            return None

    asyncio.run(drive())


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底:任何 requests 调用都视为测试失败(配置读写必须纯本地)。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture(autouse=True)
def isolated_env() -> Iterator[None]:
    """快照并复原 ``os.environ``;开跑前**清空**白名单 7 键与各家 LLM key。

    两件事都必要:``set_key`` / ``load_into_environ`` 会真写 ``os.environ``(不清就串味);
    宿主机若真有 ``DEEPSEEK_API_KEY``,``resolve_provider`` 的"没配 → None"断言会假绿。
    """
    snapshot = dict(os.environ)
    for key in app_config.ALLOWED_KEYS + LLM_ENVS:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


@pytest.fixture()
def engine(tmp_path):
    """每个用例一个独立的临时 SQLite 库(不碰 ``backend/data/``)。"""
    created = make_engine(f"sqlite:///{tmp_path / 'app_config_test.db'}")
    init_db(created)
    try:
        yield created
    finally:
        created.dispose()


@pytest.fixture()
def session(engine) -> Iterator[Any]:
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()


@pytest.fixture()
def http(session) -> Iterator[Callable[..., tuple[int, Any]]]:
    """完整 HTTP 链用的客户端:把 ``get_session`` 依赖换成用例里的同一个临时库会话。"""
    app.dependency_overrides[get_session] = lambda: session

    def call(method: str, path: str, *, body: Any = None, query: str = "") -> tuple[int, Any]:
        return http_request(method, path, body=body, query=query)

    try:
        yield call
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def lifespan_engine(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """给 lifespan 用例的临时库:把 ``db.base.get_engine`` 指过去(**不动**进程内共享引擎)。"""
    created = make_engine(f"sqlite:///{tmp_path / 'lifespan.db'}")
    init_db(created)
    monkeypatch.setattr(db_base, "get_engine", lambda: created)
    try:
        yield created
    finally:
        created.dispose()


# --------------------------------------------------------------------------- #
# 1. 表结构与白名单(唯一键 / 默认值 / 7 个键)
# --------------------------------------------------------------------------- #


def test_app_config_table_has_required_columns_and_unique_key() -> None:
    columns = {column.name for column in AppConfig.__table__.columns}
    assert REQUIRED_COLUMNS <= columns, f"AppConfig 缺字段:{REQUIRED_COLUMNS - columns}"
    unique = {
        constraint.name: {column.name for column in constraint.columns}
        for constraint in AppConfig.__table__.constraints
        if type(constraint).__name__ == "UniqueConstraint"
    }
    assert unique.get("uq_app_config_key") == {"key"}, f"唯一键应是 uq_app_config_key(key),实际:{unique}"


def test_app_config_key_column_is_unique_and_indexed(session) -> None:
    assert AppConfig.__table__.c.key.index is True, "key 要建索引:启动注入按 key 查"
    seed_row(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    session.add(AppConfig(key=app_config.ENV_AMAP_KEY, value="second"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    assert len(rows_of(session, app_config.ENV_AMAP_KEY)) == 1, "唯一键挡住了第二行"


def test_app_config_defaults_source_setup_and_empty_value(session) -> None:
    row = AppConfig(key=app_config.ENV_LLM_MODEL)
    session.add(row)
    session.commit()
    assert row.source == "setup", "默认来源是向导(setup)"
    assert row.value == "", "value 默认空串(不是 NULL)"
    assert row.updated_at is not None, "updated_at 由 utcnow 兜底"
    assert "sk-" not in repr(row) and FAKE_AMAP_KEY not in repr(row), "__repr__ 不打印值"


def test_schema_has_exactly_the_seven_whitelisted_keys() -> None:
    assert len(app_config.SCHEMA) == 7, f"白名单应恰好 7 键,实际 {len(app_config.SCHEMA)}"
    assert {field.key for field in app_config.SCHEMA} == EXPECTED_KEYS
    assert app_config.ALLOWED_KEYS == tuple(field.key for field in app_config.SCHEMA)


def test_schema_flags_required_secret_and_group() -> None:
    by_key = {field.key: field for field in app_config.iter_fields()}
    for key in AMAP_KEYS:
        field = by_key[key]
        assert field.required is True, f"{key} 是必填(缺了地图/检索不可用)"
        assert field.secret is True, f"{key} 是密钥,GET 只回掩码"
        assert field.group == "amap"
    assert by_key[app_config.ENV_LLM_API_KEY].secret is True
    assert by_key[app_config.ENV_LLM_API_KEY].required is False, "LLM 是可选的(降级为空简介)"
    for key in (app_config.ENV_LLM_PROVIDER, app_config.ENV_LLM_BASE_URL, app_config.ENV_LLM_MODEL):
        assert by_key[key].secret is False and by_key[key].group == "llm"
    assert app_config.REQUIRED_KEYS == AMAP_KEYS


def test_schema_hints_explain_the_amap_pitfalls() -> None:
    by_key = {field.key: field for field in app_config.iter_fields()}
    assert "USERKEY_PLAT_NOMATCH" in by_key[app_config.ENV_AMAP_JS_KEY].hint, \
        "JS key 的 hint 要写明与 Web服务 key 平台隔离、混用报 USERKEY_PLAT_NOMATCH"
    assert "INVALID_USER_SCODE" in by_key[app_config.ENV_AMAP_SECURITY_JS_CODE].hint, \
        "安全密钥的 hint 要写明缺失报 INVALID_USER_SCODE"
    assert "Web服务" in by_key[app_config.ENV_AMAP_KEY].hint
    assert all(field.label and field.hint for field in app_config.SCHEMA), "每项都要有中文 label/hint"


def test_no_whitelisted_key_needs_restart() -> None:
    """当前 7 个键改完都**热生效**(amap/intro 每次调用现读 env),needs_restart 恒 False。"""
    assert all(field.needs_restart is False for field in app_config.SCHEMA)


def test_is_allowed_only_accepts_whitelisted_keys() -> None:
    for key in EXPECTED_KEYS:
        assert app_config.is_allowed(key) is True
    for bad in ("PATH", "WHERE2GO_DB_URL", "DEEPSEEK_API_KEY", "", None, 123, "  "):
        assert app_config.is_allowed(bad) is False, f"{bad!r} 不该可写"
    assert app_config.get_field(app_config.ENV_AMAP_KEY).label == "高德 Web 服务 key"
    assert app_config.get_field("WHERE2GO_DB_URL") is None


def test_db_url_is_deliberately_not_configurable() -> None:
    """``WHERE2GO_DB_URL`` 不进白名单:engine 是进程内单例,改了必须重启容器,向导不暴露。"""
    assert "WHERE2GO_DB_URL" not in app_config.ALLOWED_KEYS
    expect_error(lambda: app_config.set_key(None, "WHERE2GO_DB_URL", "sqlite:///x.db"),
                 ValueError, "不允许配置的配置项")


# --------------------------------------------------------------------------- #
# 2. load_into_environ:env 压过库(启动注入)
# --------------------------------------------------------------------------- #


def test_load_into_environ_injects_rows_and_returns_key_names(session) -> None:
    seed_row(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    seed_row(session, app_config.ENV_LLM_MODEL, "deepseek-chat")
    injected = app_config.load_into_environ(session)
    assert injected == [app_config.ENV_AMAP_KEY, app_config.ENV_LLM_MODEL], "按 SCHEMA 顺序报注入的键名"
    assert os.environ[app_config.ENV_AMAP_KEY] == FAKE_AMAP_KEY
    assert os.environ[app_config.ENV_LLM_MODEL] == "deepseek-chat"


def test_load_into_environ_never_overwrites_container_env(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """容器 env 永远赢:注入只填「env 未设」的键。"""
    monkeypatch.setenv(app_config.ENV_AMAP_KEY, "from-container-env")
    seed_row(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    seed_row(session, app_config.ENV_AMAP_JS_KEY, FAKE_JS_KEY)
    injected = app_config.load_into_environ(session)
    assert injected == [app_config.ENV_AMAP_JS_KEY], "env 已有的键不该被库里的值覆盖"
    assert os.environ[app_config.ENV_AMAP_KEY] == "from-container-env"
    assert amap.resolve_key() == "from-container-env", "amap 现读 env → 拿到的还是容器给的值"


def test_load_into_environ_treats_empty_env_as_unset(session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(app_config.ENV_AMAP_KEY, "   ")
    seed_row(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    assert app_config.load_into_environ(session) == [app_config.ENV_AMAP_KEY], "空串 env 视为未设"
    assert os.environ[app_config.ENV_AMAP_KEY] == FAKE_AMAP_KEY


def test_load_into_environ_skips_empty_rows_and_empty_table(session) -> None:
    assert app_config.load_into_environ(session) == [], "空表 → 什么都不注入"
    seed_row(session, app_config.ENV_LLM_API_KEY, "")
    seed_row(session, app_config.ENV_LLM_MODEL, "   ")
    assert app_config.load_into_environ(session) == [], "库里空值等于没配"
    assert app_config.ENV_LLM_API_KEY not in os.environ


def test_load_into_environ_ignores_rows_outside_the_whitelist(session) -> None:
    """纵深防御:表里被塞了白名单外的行也**不注入**(本表不是任意 env 注入面)。"""
    seed_row(session, "WHERE2GO_UI_MODE", "release")
    seed_row(session, "LD_PRELOAD", "/tmp/evil.so")
    seed_row(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    injected = app_config.load_into_environ(session)
    assert injected == [app_config.ENV_AMAP_KEY]
    assert "LD_PRELOAD" not in os.environ and "WHERE2GO_UI_MODE" not in os.environ


# --------------------------------------------------------------------------- #
# 3. set_key:白名单 + 幂等 upsert + 清除 + 热生效
# --------------------------------------------------------------------------- #


def test_set_key_rejects_keys_outside_the_whitelist(session) -> None:
    expect_error(lambda: app_config.set_key(session, "PATH", "/tmp"), ValueError,
                 "不允许配置的配置项", "PATH")
    expect_error(lambda: app_config.set_key(session, "", "x"), ValueError, "不允许配置的配置项")
    expect_error(lambda: app_config.set_key(session, None, "x"), ValueError, "不允许配置的配置项")
    assert list(session.scalars(select(AppConfig))) == [], "被拒的键不该落库"
    assert os.environ.get("PATH") != "/tmp", "被拒的键也不该进 os.environ"
    assert "WHERE2GO_DB_URL" not in os.environ


def test_set_key_strips_value_and_writes_setup_row(session) -> None:
    app_config.set_key(session, app_config.ENV_AMAP_KEY, f"  {FAKE_AMAP_KEY}\n")
    rows = rows_of(session, app_config.ENV_AMAP_KEY)
    assert len(rows) == 1 and rows[0].value == FAKE_AMAP_KEY, "值去空白后入库"
    assert rows[0].source == "setup"
    assert os.environ[app_config.ENV_AMAP_KEY] == FAKE_AMAP_KEY, "同步 os.environ(热生效)"


def test_set_key_is_an_idempotent_upsert(session) -> None:
    app_config.set_key(session, app_config.ENV_AMAP_JS_KEY, FAKE_JS_KEY)
    first_id = rows_of(session, app_config.ENV_AMAP_JS_KEY)[0].id
    app_config.set_key(session, app_config.ENV_AMAP_JS_KEY, "sk-jsapi-second-value-000")
    rows = rows_of(session, app_config.ENV_AMAP_JS_KEY)
    assert len(rows) == 1, "唯一键 → 重复保存不产生第二行"
    assert rows[0].id == first_id and rows[0].value == "sk-jsapi-second-value-000"
    assert os.environ[app_config.ENV_AMAP_JS_KEY] == "sk-jsapi-second-value-000"


def test_set_key_with_empty_value_deletes_the_row(session) -> None:
    seed_row(session, app_config.ENV_LLM_API_KEY, FAKE_LLM_KEY)
    os.environ[app_config.ENV_LLM_API_KEY] = FAKE_LLM_KEY
    app_config.set_key(session, app_config.ENV_LLM_API_KEY, "   ")
    assert rows_of(session, app_config.ENV_LLM_API_KEY) == [], "空串 = 清除向导配置(删行)"
    assert app_config.is_configured(session, app_config.ENV_LLM_API_KEY) is False


def test_set_key_with_empty_value_blanks_env_instead_of_popping(session) -> None:
    """清除时把 ``os.environ`` 置**空串**而不是 pop:与 load 的「空串视为未设」口径一致。"""
    app_config.set_key(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    app_config.set_key(session, app_config.ENV_AMAP_KEY, "")
    assert app_config.ENV_AMAP_KEY in os.environ, "故意不 pop(容器 env 是外部给的)"
    assert os.environ[app_config.ENV_AMAP_KEY] == ""
    assert app_config.effective_source(session, app_config.ENV_AMAP_KEY) == ""


def test_set_key_commits_so_a_second_session_sees_it(session, engine) -> None:
    app_config.set_key(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    other = session_factory(engine)()
    try:
        assert app_config.stored_value(other, app_config.ENV_AMAP_KEY) == FAKE_AMAP_KEY
    finally:
        other.close()


def test_set_key_coerces_non_string_values(session) -> None:
    app_config.set_key(session, app_config.ENV_LLM_MODEL, 123)
    assert app_config.stored_value(session, app_config.ENV_LLM_MODEL) == "123"
    assert os.environ[app_config.ENV_LLM_MODEL] == "123"


# --------------------------------------------------------------------------- #
# 4. 热生效:保存完 amap / intro 立刻看到新值(直接调函数,不起服务)
# --------------------------------------------------------------------------- #


def test_amap_resolve_key_fails_then_works_after_set_key(session) -> None:
    expect_error(amap.resolve_key, DataSourceError, "WHERE2GO_AMAP_KEY")
    app_config.set_key(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    assert amap.resolve_key() == FAKE_AMAP_KEY, "改完立刻生效,不用重启"


def test_set_key_overrides_container_env_in_process(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """向导保存是**显式动作**:进程内直接盖掉容器 env(下次重启才回到 env 优先)。"""
    monkeypatch.setenv(app_config.ENV_AMAP_KEY, "from-container-env")
    app_config.set_key(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    assert amap.resolve_key() == FAKE_AMAP_KEY
    assert app_config.effective_source(session, app_config.ENV_AMAP_KEY) == "env", \
        "来源仍报 env(env 非空即 env 优先),但进程内生效的是刚保存的值"


def test_llm_provider_resolves_right_after_set_key(session) -> None:
    assert intro_service.resolve_provider() is None, "什么都没配 → 不调 LLM(空简介降级)"
    app_config.set_key(session, app_config.ENV_LLM_PROVIDER, "deepseek")
    app_config.set_key(session, app_config.ENV_LLM_API_KEY, FAKE_LLM_KEY)
    app_config.set_key(session, app_config.ENV_LLM_BASE_URL, "https://llm.example.com/v1/")
    app_config.set_key(session, app_config.ENV_LLM_MODEL, "my-model")
    resolved = intro_service.resolve_provider()
    assert resolved is not None and resolved.provider == "deepseek"
    assert resolved.api_key == FAKE_LLM_KEY and resolved.model == "my-model"
    assert resolved.base_url == "https://llm.example.com/v1", "末尾斜杠按 intro 口径去掉"
    described = intro_service.describe_llm()
    assert described["enabled"] is True and described["model"] == "my-model"
    assert_no_plaintext(described, (FAKE_LLM_KEY,)), "describe_llm 也绝不回显 key"


def test_clearing_the_llm_key_disables_llm_again(session) -> None:
    app_config.set_key(session, app_config.ENV_LLM_API_KEY, FAKE_LLM_KEY)
    assert intro_service.resolve_provider() is not None
    app_config.set_key(session, app_config.ENV_LLM_API_KEY, "")
    assert intro_service.resolve_provider() is None, "清除后回到「没配 LLM」降级态"
    assert intro_service.describe_llm()["enabled"] is False


# --------------------------------------------------------------------------- #
# 5. mask_value / effective_source / missing_required
# --------------------------------------------------------------------------- #


def test_mask_value_boundaries() -> None:
    assert app_config.mask_value("") == ""
    assert app_config.mask_value(None) == ""
    assert app_config.mask_value("   ") == ""
    assert app_config.mask_value("a") == "****"
    assert app_config.mask_value("abcd") == "****", "长度 ≤4 全掩(留头尾等于泄露大半)"
    assert app_config.mask_value("abcde") == "ab…de", "长度 5 起给「前2 + … + 后2」"
    assert app_config.mask_value(FAKE_AMAP_KEY) == "sk…ef"


def test_mask_value_never_reveals_the_secret() -> None:
    masked = app_config.mask_value(FAKE_LLM_KEY)
    assert FAKE_LLM_KEY not in masked and len(masked) < len(FAKE_LLM_KEY)
    assert masked == "sk…yz" and masked.count("…") == 1


def test_effective_source_three_states(session, monkeypatch: pytest.MonkeyPatch) -> None:
    key = app_config.ENV_AMAP_KEY
    assert app_config.effective_source(session, key) == "", "都没配 → 空串"
    seed_row(session, key, FAKE_AMAP_KEY)
    assert app_config.effective_source(session, key) == "setup", "只有库里有 → setup"
    monkeypatch.setenv(key, "from-container-env")
    assert app_config.effective_source(session, key) == "env", "env 非空 → env(压过库)"


def test_effective_source_falls_back_when_env_is_blank(session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(app_config.ENV_AMAP_JS_KEY, "")
    seed_row(session, app_config.ENV_AMAP_JS_KEY, FAKE_JS_KEY)
    assert app_config.effective_source(session, app_config.ENV_AMAP_JS_KEY) == "setup"
    assert app_config.effective_value(session, app_config.ENV_AMAP_JS_KEY) == FAKE_JS_KEY


def test_is_configured_mirrors_effective_source(session, monkeypatch: pytest.MonkeyPatch) -> None:
    key = app_config.ENV_AMAP_SECURITY_JS_CODE
    assert app_config.is_configured(session, key) is False
    app_config.set_key(session, key, FAKE_SCODE)
    assert app_config.is_configured(session, key) is True
    monkeypatch.setenv(key, "")
    app_config.set_key(session, key, "")
    assert app_config.is_configured(session, key) is False


def test_display_source_attributes_wizard_values_to_setup(session) -> None:
    """``set_key`` 会同步写 ``os.environ``,但展示上它仍是**向导存的**(否则每次重启都误报 env)。"""
    app_config.set_key(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    assert app_config.effective_source(session, app_config.ENV_AMAP_KEY) == "env", \
        "严格口径:env 非空即 env(set_key 同步了 env)"
    assert app_config.display_source(session, app_config.ENV_AMAP_KEY) == "setup", \
        "展示口径:env 的值就是库里那份 → 报 setup"
    assert item_of(config_api.read_config(session=session), app_config.ENV_AMAP_KEY)["source"] == "setup"


def test_display_source_reports_env_when_the_container_value_differs(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(app_config.ENV_AMAP_KEY, "from-container-env")
    seed_row(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    assert app_config.display_source(session, app_config.ENV_AMAP_KEY) == "env"
    assert app_config.effective_value(session, app_config.ENV_AMAP_KEY) == "from-container-env"


def test_display_source_after_startup_injection_is_setup(session) -> None:
    seed_row(session, app_config.ENV_AMAP_JS_KEY, FAKE_JS_KEY)
    app_config.load_into_environ(session)
    assert app_config.display_source(session, app_config.ENV_AMAP_JS_KEY) == "setup"
    assert app_config.display_source(session, app_config.ENV_LLM_MODEL) == "", "没配 → 空串"


def test_missing_required_lists_the_three_amap_keys_when_nothing_set(session) -> None:
    assert app_config.missing_required(session) == list(AMAP_KEYS)


def test_missing_required_shrinks_as_keys_get_configured(session) -> None:
    app_config.set_key(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    assert app_config.missing_required(session) == [
        app_config.ENV_AMAP_JS_KEY, app_config.ENV_AMAP_SECURITY_JS_CODE,
    ]
    app_config.set_key(session, app_config.ENV_AMAP_JS_KEY, FAKE_JS_KEY)
    app_config.set_key(session, app_config.ENV_AMAP_SECURITY_JS_CODE, FAKE_SCODE)
    assert app_config.missing_required(session) == []


def test_missing_required_satisfied_by_env_or_db(session, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in AMAP_KEYS:
        monkeypatch.setenv(key, "from-container-env")
    assert app_config.missing_required(session) == [], "容器 env 配齐 → 不缺"
    for key in AMAP_KEYS:
        monkeypatch.delenv(key)
        seed_row(session, key, FAKE_AMAP_KEY)
    assert app_config.missing_required(session) == [], "向导配齐(只在库里)→ 也不缺"


# --------------------------------------------------------------------------- #
# 6. GET /api/config:形状 + 明文不出接口
# --------------------------------------------------------------------------- #


def test_get_config_lists_all_seven_items_with_exact_shape(session) -> None:
    payload = config_api.read_config(session=session)
    assert set(payload) == {"items", "missing_required", "note"}, f"响应顶层形状变了:{set(payload)}"
    assert [item["key"] for item in payload["items"]] == list(app_config.ALLOWED_KEYS)
    for item in payload["items"]:
        assert set(item) == ITEM_KEYS, f"{item['key']} 的字段形状变了:{set(item)}"
        assert item["configured"] is False and item["source"] == "" and item["masked"] == ""
    assert payload["missing_required"] == list(AMAP_KEYS)
    assert "环境变量优先" in payload["note"] and "不回显明文" in payload["note"]


def test_get_config_never_leaks_plaintext(session, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_row(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    seed_row(session, app_config.ENV_AMAP_SECURITY_JS_CODE, FAKE_SCODE)
    seed_row(session, app_config.ENV_LLM_API_KEY, FAKE_LLM_KEY)
    monkeypatch.setenv(app_config.ENV_AMAP_JS_KEY, FAKE_JS_KEY)
    dumped = assert_no_plaintext(config_api.read_config(session=session))
    assert "sk…ef" in dumped, "掩码要给出来(用户确认填的是哪个 key)"
    assert "sk…yz" in dumped


def test_get_config_masks_only_secret_configured_items(session) -> None:
    app_config.set_key(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    app_config.set_key(session, app_config.ENV_LLM_PROVIDER, "deepseek")
    app_config.set_key(session, app_config.ENV_LLM_BASE_URL, "https://llm.example.com/v1")
    payload = config_api.read_config(session=session)
    assert item_of(payload, app_config.ENV_AMAP_KEY) == {
        "key": app_config.ENV_AMAP_KEY,
        "label": "高德 Web 服务 key",
        "hint": item_of(payload, app_config.ENV_AMAP_KEY)["hint"],
        "required": True, "secret": True, "group": "amap",
        "configured": True, "source": "setup", "masked": "sk…ef",
    }
    provider = item_of(payload, app_config.ENV_LLM_PROVIDER)
    assert provider["configured"] is True and provider["masked"] == "", \
        "非 secret 项也不回显值(统一「值不出接口」口径),只报 configured/source"
    assert item_of(payload, app_config.ENV_LLM_MODEL)["masked"] == "", "未配置 → 没有掩码"


def test_get_config_reports_env_source(session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(app_config.ENV_AMAP_KEY, "from-container-env")
    seed_row(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    item = item_of(config_api.read_config(session=session), app_config.ENV_AMAP_KEY)
    assert item["source"] == "env" and item["configured"] is True
    assert item["masked"] == "fr…nv", "掩码按**实际生效**的值算(env 优先)"


def test_get_config_direct_call_is_compatible(session) -> None:
    """单测直调端点函数(不经 FastAPI)也能用:``Depends`` 参数显式传 session 即可。"""
    payload = config_api.read_config(session=session)
    assert len(payload["items"]) == 7
    assert config_api.read_config(session) == payload


# --------------------------------------------------------------------------- #
# 7. POST /api/config:单键保存 + 校验 400
# --------------------------------------------------------------------------- #


def test_post_config_saves_and_reports_setup(session) -> None:
    payload = config_api.save_config(
        {"key": app_config.ENV_AMAP_KEY, "value": FAKE_AMAP_KEY}, session=session
    )
    assert payload == {
        "ok": True, "key": app_config.ENV_AMAP_KEY, "configured": True,
        "source": "setup", "note": "已保存并即时生效(无需重启)",
    }
    assert app_config.stored_value(session, app_config.ENV_AMAP_KEY) == FAKE_AMAP_KEY
    assert amap.resolve_key() == FAKE_AMAP_KEY, "保存后立刻热生效"
    assert_no_plaintext(payload)


def test_post_config_strips_value_and_accepts_missing_value_as_clear(session) -> None:
    app_config.set_key(session, app_config.ENV_AMAP_JS_KEY, FAKE_JS_KEY)
    saved = config_api.save_config(
        {"key": app_config.ENV_AMAP_JS_KEY, "value": f" {FAKE_JS_KEY} "}, session=session
    )
    assert saved["configured"] is True and saved["source"] == "setup"
    cleared = config_api.save_config({"key": app_config.ENV_AMAP_JS_KEY}, session=session)
    assert cleared["ok"] is True and cleared["configured"] is False and cleared["source"] == ""
    assert "清除" in cleared["note"]
    assert rows_of(session, app_config.ENV_AMAP_JS_KEY) == []


def test_post_config_clearing_blanks_the_in_process_env(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """向导点「清除」是**显式动作**:进程内连 env 一起置空(容器 env 要重启才回来)。"""
    monkeypatch.setenv(app_config.ENV_AMAP_KEY, "from-container-env")
    payload = config_api.save_config({"key": app_config.ENV_AMAP_KEY, "value": ""}, session=session)
    assert payload["ok"] is True and payload["configured"] is False and payload["source"] == ""
    assert "清除" in payload["note"] and "环境变量" in payload["note"]
    assert os.environ[app_config.ENV_AMAP_KEY] == ""
    expect_error(amap.resolve_key, DataSourceError, "WHERE2GO_AMAP_KEY")


def test_post_config_rejects_keys_outside_the_whitelist(session) -> None:
    expect_http(lambda: config_api.save_config({"key": "PATH", "value": "/tmp"}, session=session),
                400, "不允许配置的配置项", "PATH")
    expect_http(
        lambda: config_api.save_config({"key": "WHERE2GO_DB_URL", "value": "sqlite:///x"}, session=session),
        400, "不允许配置的配置项",
    )
    assert list(session.scalars(select(AppConfig))) == [], "被拒的键不落库"
    assert os.environ.get("PATH") != "/tmp"


def test_post_config_validates_the_body(session) -> None:
    expect_http(lambda: config_api.save_config(session=session), 400, "缺少请求体"), \
        "单测直调时 payload 是 FieldInfo → 归一成「没给请求体」"
    expect_http(lambda: config_api.save_config(None, session=session), 400, "缺少请求体")
    expect_http(lambda: config_api.save_config(["key", "value"], session=session), 400,
                "请求体必须是 JSON 对象", "list")
    expect_http(lambda: config_api.save_config("WHERE2GO_AMAP_KEY=x", session=session), 400,
                "请求体必须是 JSON 对象", "str")


def test_post_config_validates_key_and_value(session) -> None:
    expect_http(lambda: config_api.save_config({"value": "x"}, session=session), 400, "缺少配置项 key")
    expect_http(lambda: config_api.save_config({"key": "  ", "value": "x"}, session=session), 400,
                "配置项 key 不能为空")
    expect_http(lambda: config_api.save_config({"key": 123, "value": "x"}, session=session), 400,
                "缺少配置项 key")
    expect_http(
        lambda: config_api.save_config({"key": app_config.ENV_AMAP_KEY, "value": {"a": 1}}, session=session),
        400, "值必须是字符串", "dict",
    )


def test_post_config_response_never_echoes_the_value(session) -> None:
    payload = config_api.save_config(
        {"key": app_config.ENV_LLM_API_KEY, "value": FAKE_LLM_KEY}, session=session
    )
    dumped = assert_no_plaintext(payload, (FAKE_LLM_KEY,))
    assert "masked" not in dumped and "value" not in json.loads(dumped), \
        "POST 响应只报 ok/key/configured/source/note,连掩码都不给"


# --------------------------------------------------------------------------- #
# 8. GET /api/health:恒 200,缺必填项只是降级
# --------------------------------------------------------------------------- #


def test_health_reports_missing_config(session) -> None:
    payload = config_api.health(session=session)
    assert set(payload) == {"status", "missing_required", "version"}, f"形状变了:{set(payload)}"
    assert payload == {
        "status": "missing_config", "missing_required": list(AMAP_KEYS), "version": "0.1.0-beta",
    }


def test_health_reports_ok_when_required_keys_are_set(session, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in AMAP_KEYS:
        monkeypatch.setenv(key, "from-container-env")
    assert config_api.health(session=session) == {
        "status": "ok", "missing_required": [], "version": "0.1.0-beta",
    }
    assert_no_plaintext(config_api.health(session=session), ("from-container-env",))


# --------------------------------------------------------------------------- #
# 9. 完整 HTTP 链 + lifespan 启动注入
# --------------------------------------------------------------------------- #


def test_http_get_config_returns_200_without_plaintext(http, session) -> None:
    seed_row(session, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
    seed_row(session, app_config.ENV_LLM_API_KEY, FAKE_LLM_KEY)
    status, payload = http("GET", "/api/config")
    assert status == 200 and len(payload["items"]) == 7
    dumped = assert_no_plaintext(payload)
    assert "环境变量优先" in dumped


def test_http_post_then_get_roundtrip(http, session) -> None:
    status, saved = http("POST", "/api/config",
                         body={"key": app_config.ENV_AMAP_KEY, "value": FAKE_AMAP_KEY})
    assert status == 200 and saved["ok"] is True and saved["source"] == "setup"
    assert saved["note"] == "已保存并即时生效(无需重启)"
    assert amap.resolve_key() == FAKE_AMAP_KEY, "HTTP 保存后进程内立刻热生效"

    status, payload = http("GET", "/api/config")
    assert status == 200
    item = item_of(payload, app_config.ENV_AMAP_KEY)
    assert item["configured"] is True and item["masked"] == "sk…ef" and item["source"] == "setup"
    assert payload["missing_required"] == [
        app_config.ENV_AMAP_JS_KEY, app_config.ENV_AMAP_SECURITY_JS_CODE,
    ]
    assert_no_plaintext(payload)


def test_http_post_bad_key_returns_400_chinese(http) -> None:
    status, payload = http("POST", "/api/config", body={"key": "PATH", "value": "/tmp"})
    assert status == 400 and "不允许配置的配置项" in payload["detail"]
    status, payload = http("POST", "/api/config")
    assert status == 400 and "缺少请求体" in payload["detail"]


def test_http_health_is_200_even_when_config_is_missing(http) -> None:
    status, payload = http("GET", "/api/health")
    assert status == 200, "缺 key 是功能降级,不是进程故障 → 绝不报 5xx"
    assert payload["status"] == "missing_config" and payload["version"] == "0.1.0-beta"
    assert payload["missing_required"] == list(AMAP_KEYS)


def test_lifespan_injects_db_config_into_environ(lifespan_engine) -> None:
    boot = session_factory(lifespan_engine)()
    try:
        seed_row(boot, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
        seed_row(boot, app_config.ENV_AMAP_JS_KEY, FAKE_JS_KEY)
    finally:
        boot.close()
    run_lifespan()
    assert os.environ[app_config.ENV_AMAP_KEY] == FAKE_AMAP_KEY
    assert os.environ[app_config.ENV_AMAP_JS_KEY] == FAKE_JS_KEY
    assert amap.resolve_key() == FAKE_AMAP_KEY, "启动注入后,那 30 处 env 读取点照常现读就能拿到"


def test_lifespan_keeps_container_env_priority(
    lifespan_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(app_config.ENV_AMAP_KEY, "from-container-env")
    boot = session_factory(lifespan_engine)()
    try:
        seed_row(boot, app_config.ENV_AMAP_KEY, FAKE_AMAP_KEY)
        seed_row(boot, app_config.ENV_LLM_MODEL, "deepseek-chat")
    finally:
        boot.close()
    run_lifespan()
    assert os.environ[app_config.ENV_AMAP_KEY] == "from-container-env"
    assert os.environ[app_config.ENV_LLM_MODEL] == "deepseek-chat"


def test_lifespan_survives_an_empty_config_table(lifespan_engine) -> None:
    run_lifespan()
    assert all(key not in os.environ for key in app_config.ALLOWED_KEYS), "空库 → 一个键都不注入"


def test_routes_are_mounted_under_api_prefix() -> None:
    paths = set(app.openapi().get("paths", {}))
    assert {"/api/config", "/api/health"} <= paths, f"路由没挂上 /api:{sorted(p for p in paths if p.startswith('/api'))[:8]}"
    assert config_api.APP_VERSION == "0.1.0-beta"
    assert callable(main_module.lifespan), "启动注入挂在 lifespan 上(不用已弃用的 on_event)"
