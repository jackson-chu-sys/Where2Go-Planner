"""TASK-P3 单测:/setup 向导页 + ``POST /api/config/validate`` 真调校验端点 + 首页横幅。

口径(CONTAINER-SETUP-PLAN §四 P3 / 决策③):
* 校验端点**每步只打一次外部请求**(QPS 10021 防线)——用计数 stub 断言;
* 校验失败**不是 HTTP 错误**(恒 200 + ok=false + 中文 detail + infocode 解释);
* 明文 key **永不进响应**(json.dumps 后 grep,照 test_app_config 先例);
* setup.html 是独立静态页(不塞 index.html),向导文案含两个血泪坑
  (USERKEY_PLAT_NOMATCH / INVALID_USER_SCODE);
* index.html 横幅:``GET /api/health`` 缺必填 → 引导条(不强制重定向);配齐/接口挂 → 隐藏。

全程不触网:``no_network`` 兜底,数据源用 monkeypatch stub。
运行:``cd backend && ../.venv/bin/python -m pytest -q``
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
import requests

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import config as config_api  # noqa: E402
from app.main import STATIC, app  # noqa: E402
from data_sources import DataSourceError, TransientDataSourceError, amap  # noqa: E402
from db import init_db, make_engine, session_factory  # noqa: E402
from db.base import get_session  # noqa: E402
from services import app_config  # noqa: E402
from services import intro as intro_service  # noqa: E402

SETUP_HTML = STATIC / "setup.html"
INDEX_HTML = STATIC / "index.html"
NODE = shutil.which("node")

FAKE_KEY = "amapke...st01"
FAKE_JS = "jskeyf...de99"
FAKE_LLM = "sk-llm...wxyz"
SECRETS = (FAKE_KEY, FAKE_JS, FAKE_LLM)


# --------------------------------------------------------------------------- #
# fixtures(照 test_app_config.py 的套路)
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture(autouse=True)
def isolated_env() -> Iterator[None]:
    snapshot = dict(os.environ)
    llm_envs = sorted(
        {key for provider in intro_service.PROVIDERS for key in provider.env_api_keys}
        | {intro_service.ENV_API_KEY}
    )
    for key in app_config.ALLOWED_KEYS + tuple(llm_envs):
        os.environ.pop(key, None)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


@pytest.fixture()
def engine(tmp_path):
    created = make_engine(f"sqlite:///{tmp_path / 'setup_test.db'}")
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
    app.dependency_overrides[get_session] = lambda: session

    def call(method: str, path: str, *, body: Any = None) -> tuple[int, Any]:
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1", "method": method, "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"",
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

    try:
        yield call
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def setup_html() -> str:
    return SETUP_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def assert_no_plaintext(payload: Any) -> str:
    dumped = json.dumps(payload, ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in dumped, f"明文 key 绝不能出现在响应里:{secret[:6]}…"
    return dumped


def stub_geocode(monkeypatch: pytest.MonkeyPatch, behavior: Callable[..., Any]) -> list[dict]:
    """替换 amap.geocode 并记录调用(断言「只打一次请求」用)。"""
    calls: list[dict] = []

    def fake(address: str, **kwargs: Any) -> Any:
        calls.append({"address": address, **kwargs})
        return behavior(address, **kwargs)

    monkeypatch.setattr(amap, "geocode", fake)
    return calls


# --------------------------------------------------------------------------- #
# 1. POST /api/config/validate —— target=amap(Web服务 key 真调 geocode)
# --------------------------------------------------------------------------- #


def test_validate_amap_ok_returns_sample(http, monkeypatch) -> None:
    calls = stub_geocode(monkeypatch, lambda *a, **k: [
        {"formatted_address": "北京市", "province": "北京市", "city": "", "district": "",
         "adcode": "110000", "township": "", "lng": 116.407, "lat": 39.904},
    ])
    code, data = http("POST", "/api/config/validate",
                      body={"target": "amap", "value": FAKE_KEY})
    assert code == 200 and data["ok"] is True and data["target"] == "amap"
    assert data["sample"]["formatted_address"] == "北京市"
    assert data["sample"]["lng"] == 116.407 and data["sample"]["lat"] == 39.904
    assert len(calls) == 1, "QPS 防线:一次校验只允许打一次高德请求"
    assert calls[0]["address"] == config_api.PROBE_ADDRESS == "北京"
    assert calls[0]["environ"] == {amap.ENV_AMAP_KEY: FAKE_KEY}, "必须用传入的 key 现调"
    assert_no_plaintext(data)


def test_validate_amap_no_key_anywhere_is_ok_false_not_error(http) -> None:
    code, data = http("POST", "/api/config/validate", body={"target": "amap", "value": ""})
    assert code == 200 and data["ok"] is False and "还没填" in data["detail"]


def test_validate_amap_invalid_key_explains_infocode_and_masks(http, monkeypatch) -> None:
    def boom(*a, **k):
        raise DataSourceError("amap", f"地理编码 '北京' 失败:高德返回 status=0 · infocode=10001 "
                                      f"· key 不正确或已过期(key={FAKE_KEY})")

    stub_geocode(monkeypatch, boom)
    code, data = http("POST", "/api/config/validate", body={"target": "amap", "value": FAKE_KEY})
    assert code == 200 and data["ok"] is False
    assert data["infocode"] == "10001" and "key 不正确或已过期" in data["detail"]
    assert_no_plaintext(data), "错误文案里的明文 key 必须被替换成掩码"
    assert app_config.mask_value(FAKE_KEY) in data["detail"]


def test_validate_amap_platform_mismatch_hint(http, monkeypatch) -> None:
    def boom(*a, **k):
        raise DataSourceError("amap", "地理编码失败:infocode=10009 · "
                                      "key 与平台类型不符(JS key 不能调 Web 服务 REST 接口)")

    stub_geocode(monkeypatch, boom)
    code, data = http("POST", "/api/config/validate", body={"target": "amap", "value": FAKE_JS})
    assert code == 200 and data["ok"] is False and data["infocode"] == "10009"
    assert "平台" in data["detail"]


def test_validate_amap_zero_results_is_ok_false(http, monkeypatch) -> None:
    stub_geocode(monkeypatch, lambda *a, **k: [])
    code, data = http("POST", "/api/config/validate", body={"target": "amap", "value": FAKE_KEY})
    assert code == 200 and data["ok"] is False and "零结果" in data["detail"]


def test_validate_amap_falls_back_to_effective_value(http, session, monkeypatch) -> None:
    """value 留空 = 用当前生效值(已保存/环境变量里的 key)复测。"""
    calls = stub_geocode(monkeypatch, lambda *a, **k: [{"formatted_address": "北京市"}])
    app_config.set_key(session, app_config.ENV_AMAP_KEY, FAKE_KEY)
    code, data = http("POST", "/api/config/validate", body={"target": "amap"})
    assert code == 200 and data["ok"] is True
    assert calls[0]["environ"] == {amap.ENV_AMAP_KEY: FAKE_KEY}


# --------------------------------------------------------------------------- #
# 2. target=amap_js —— 10009 平台不符 = 恰好证明是真 JS key
# --------------------------------------------------------------------------- #


def test_validate_js_key_10009_means_pass(http, monkeypatch) -> None:
    def boom(*a, **k):
        raise DataSourceError("amap", "失败:infocode=10009 · "
                                      "key 与平台类型不符(JS key 不能调 Web 服务 REST 接口)")

    calls = stub_geocode(monkeypatch, boom)
    code, data = http("POST", "/api/config/validate", body={"target": "amap_js", "value": FAKE_JS})
    assert code == 200 and data["ok"] is True and data["infocode"] == "10009"
    assert "Web端(JS API)" in data["detail"]
    assert len(calls) == 1
    assert_no_plaintext(data)


def test_validate_js_key_invalid_key_fails(http, monkeypatch) -> None:
    def boom(*a, **k):
        raise DataSourceError("amap", "失败:infocode=10001 · key 不正确或已过期")

    stub_geocode(monkeypatch, boom)
    code, data = http("POST", "/api/config/validate", body={"target": "amap_js", "value": FAKE_JS})
    assert code == 200 and data["ok"] is False and data["infocode"] == "10001"


def test_validate_js_key_rest_success_warns_dual_platform(http, monkeypatch) -> None:
    calls = stub_geocode(monkeypatch, lambda *a, **k: [{"formatted_address": "北京市"}])
    code, data = http("POST", "/api/config/validate", body={"target": "amap_js", "value": FAKE_JS})
    assert code == 200 and data["ok"] is True and len(calls) == 1
    assert "平台隔离" in data["detail"] or "两个平台" in data["detail"] or "Web 服务" in data["detail"]


def test_validate_js_key_transient_error_is_not_pass(http, monkeypatch) -> None:
    def boom(*a, **k):
        raise TransientDataSourceError("amap", "失败:infocode=10021 · QPS 超限(瞬时,可退避重试)")

    stub_geocode(monkeypatch, boom)
    code, data = http("POST", "/api/config/validate", body={"target": "amap_js", "value": FAKE_JS})
    assert code == 200 and data["ok"] is False and "不可达" in data["detail"]


def test_validate_js_key_missing_is_ok_false(http) -> None:
    code, data = http("POST", "/api/config/validate", body={"target": "amap_js", "value": ""})
    assert code == 200 and data["ok"] is False and "还没填" in data["detail"]


# --------------------------------------------------------------------------- #
# 3. target=llm —— 1-token 测试消息(provider/base_url/model 临时覆盖)
# --------------------------------------------------------------------------- #


def test_validate_llm_ok(http, monkeypatch) -> None:
    sent: list[dict] = []

    class FakeClient:
        def __init__(self, resolved=None, **kwargs):
            self.resolved = resolved
            sent.append({"resolved": resolved, **kwargs})

        def chat(self, prompt, **kwargs):
            assert prompt == config_api.LLM_PROBE_PROMPT
            return "好"

    monkeypatch.setattr(intro_service, "LLMClient", FakeClient)
    code, data = http("POST", "/api/config/validate",
                      body={"target": "llm", "value": FAKE_LLM,
                            "provider": "deepseek", "base_url": "", "model": ""})
    assert code == 200 and data["ok"] is True and "好" in data["detail"]
    assert len(sent) == 1, "QPS/费用防线:只发一条测试消息"
    assert sent[0]["max_tokens"] == config_api.LLM_PROBE_MAX_TOKENS
    assert sent[0]["resolved"].api_key == FAKE_LLM
    assert_no_plaintext(data)


def test_validate_llm_no_key_is_skippable(http) -> None:
    code, data = http("POST", "/api/config/validate", body={"target": "llm", "value": ""})
    assert code == 200 and data["ok"] is False and "跳过" in data["detail"]


def test_validate_llm_chat_error_masks_key(http, monkeypatch) -> None:
    class BadClient:
        def __init__(self, resolved=None, **kwargs):
            pass

        def chat(self, prompt, **kwargs):
            raise DataSourceError("LLM", f"HTTP 状态码 401:key {FAKE_LLM} 无效")

    monkeypatch.setattr(intro_service, "LLMClient", BadClient)
    code, data = http("POST", "/api/config/validate", body={"target": "llm", "value": FAKE_LLM})
    assert code == 200 and data["ok"] is False
    assert_no_plaintext(data)
    assert app_config.mask_value(FAKE_LLM) in data["detail"]


def test_validate_llm_unexpected_exception_never_500(http, monkeypatch) -> None:
    class WeirdClient:
        def __init__(self, resolved=None, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(intro_service, "LLMClient", WeirdClient)
    code, data = http("POST", "/api/config/validate", body={"target": "llm", "value": FAKE_LLM})
    assert code == 200 and data["ok"] is False and "RuntimeError" in data["detail"]


def test_validate_llm_custom_base_url_model_passed_through(http, monkeypatch) -> None:
    captured: list[Any] = []

    class FakeClient:
        def __init__(self, resolved=None, **kwargs):
            captured.append(resolved)

        def chat(self, prompt, **kwargs):
            return "好"

    monkeypatch.setattr(intro_service, "LLMClient", FakeClient)
    code, data = http("POST", "/api/config/validate",
                      body={"target": "llm", "value": FAKE_LLM,
                            "base_url": "https://llm.example.com/v1/", "model": "test-model"})
    assert code == 200 and data["ok"] is True
    resolved = captured[0]
    assert resolved.base_url == "https://llm.example.com/v1", "末尾斜杠应被去掉"
    assert resolved.model == "test-model"
    assert resolved.chat_url == "https://llm.example.com/v1/chat/completions"


# --------------------------------------------------------------------------- #
# 4. 校验端点的入参校验(400 中文)与响应口径
# --------------------------------------------------------------------------- #


def test_validate_unknown_target_400(http) -> None:
    code, data = http("POST", "/api/config/validate", body={"target": "osrm"})
    assert code == 400 and "未知的校验目标" in data["detail"]


def test_validate_missing_target_400(http) -> None:
    code, data = http("POST", "/api/config/validate", body={"value": FAKE_KEY})
    assert code == 400 and "amap" in data["detail"]


def test_validate_bad_value_type_400(http) -> None:
    code, data = http("POST", "/api/config/validate", body={"target": "amap", "value": ["x"]})
    assert code == 400 and "字符串" in data["detail"]


def test_validate_missing_body_400(http) -> None:
    code, data = http("POST", "/api/config/validate")
    assert code == 400 and "请求体" in data["detail"]


def test_validate_response_always_carries_note(http) -> None:
    code, data = http("POST", "/api/config/validate", body={"target": "amap", "value": ""})
    assert code == 200 and "只发一次真实请求" in data["note"]


def test_validate_does_not_write_db(http, session, monkeypatch) -> None:
    """校验只读不写:保存是向导另一步(POST /api/config)的事。"""
    stub_geocode(monkeypatch, lambda *a, **k: [{"formatted_address": "北京市"}])
    http("POST", "/api/config/validate", body={"target": "amap", "value": FAKE_KEY})
    assert app_config.stored_value(session, app_config.ENV_AMAP_KEY) == "", "校验不落库"
    assert os.environ.get(app_config.ENV_AMAP_KEY, "") == "", "校验也不写 os.environ"


def test_validate_direct_call_fieldinfo_compat(session, monkeypatch) -> None:
    """单测/脚本直调(payload 是 FieldInfo)→ 400 中文,不 500。"""
    stub_geocode(monkeypatch, lambda *a, **k: [])
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:
        config_api.validate_config(payload=None, session=session)
    assert caught.value.status_code == 400


# --------------------------------------------------------------------------- #
# 5. setup.html 静态断言(4 步向导 / 文案 / 无内嵌 key)
# --------------------------------------------------------------------------- #


def test_setup_html_exists_and_is_served(setup_html: str) -> None:
    assert SETUP_HTML.is_file()
    for step in ("step1", "step2", "step3", "step4"):
        assert f'id="{step}"' in setup_html, f"向导缺第 {step} 步"
    assert "/setup" not in setup_html or "location.href" in setup_html  # 首页链接用 location.href="/"
    assert 'location.href="/"' in setup_html, "完成步要有回首页按钮"


def test_setup_html_calls_config_and_validate_apis(setup_html: str) -> None:
    assert '"/api/config"' in setup_html, "保存走 POST /api/config"
    assert '"/api/config/validate"' in setup_html, "校验走 POST /api/config/validate"
    assert '"/api/health"' in setup_html, "完成步用 /api/health 汇总"
    for env in ("WHERE2GO_AMAP_KEY", "WHERE2GO_AMAP_JS_KEY",
                "WHERE2GO_AMAP_SECURITY_JS_CODE", "WHERE2GO_LLM_PROVIDER",
                "WHERE2GO_LLM_BASE_URL", "WHERE2GO_LLM_MODEL", "WHERE2GO_LLM_API_KEY"):
        assert env in setup_html, f"向导必须能保存白名单键 {env}"


def test_setup_html_teaches_the_two_pitfalls(setup_html: str) -> None:
    assert "USERKEY_PLAT_NOMATCH" in setup_html and "10009" in setup_html, \
        "两 key 平台隔离的血泪坑文案必须在"
    assert "INVALID_USER_SCODE" in setup_html, "安全密钥缺失的坑文案必须在"
    assert "Web服务" in setup_html and "Web端(JS API)" in setup_html, "教程要写清两种平台的差别"
    assert "console.amap.com" in setup_html, "要给高德控制台链接"


def test_setup_html_no_embedded_keys(setup_html: str) -> None:
    assert not re.search(r"[0-9a-f]{32}", setup_html), "页面里不许出现 32 位 key 样态字符串"
    assert "securityJsCode:" not in setup_html.replace("window._AMapSecurityConfig", ""), \
        "安全密钥值不写死在页面(只讲机制)"


def test_setup_html_single_request_discipline(setup_html: str) -> None:
    """向导每次校验只调一次 validate(无循环批量探活)。"""
    assert setup_html.count("/api/config/validate") >= 3
    assert "for(" not in setup_html.split("/api/config/validate")[0].split("amapValidate")[-1]


def test_setup_html_optional_llm_and_skip(setup_html: str) -> None:
    assert "可选" in setup_html and "跳过" in setup_html, "LLM 步骤必须可跳过"
    assert "不配也能用" in setup_html or "不配 LLM" in setup_html


@pytest.mark.skipif(NODE is None, reason="环境未安装 node")
def test_setup_html_inline_js_passes_node_check(setup_html: str, tmp_path: Path) -> None:
    blocks = re.findall(r"<script>(.*?)</script>", setup_html, re.S)
    assert len(blocks) == 1, "setup.html 应只有一段内联脚本"
    script = tmp_path / "setup_inline.js"
    script.write_text(blocks[0], encoding="utf-8")
    result = subprocess.run([NODE, "--check", str(script)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"内联脚本语法错误:\n{result.stderr}"


# --------------------------------------------------------------------------- #
# 6. index.html 横幅(决策③:引导不强制)
# --------------------------------------------------------------------------- #


def test_index_banner_dom_hidden_by_default(index_html: str) -> None:
    assert '<div id="setupBanner"></div>' in index_html
    assert "#setupBanner{display:none" in index_html.replace("\n", "").replace("  ", ""), \
        "横幅默认隐藏(配齐时永远不出现)"


def test_index_banner_checks_health_and_links_setup(index_html: str) -> None:
    assert "async function checkSetupBanner()" in index_html
    assert 'getJSON("/api/health")' in index_html, "横幅数据源 = /api/health"
    assert 'href=\\"/setup\\"' in index_html or 'href="/setup"' in index_html, "引导链接去 /setup"
    assert "missing_required" in index_html, "消费 health 的 missing_required 字段"
    assert "checkSetupBanner().catch" in index_html, "boot 里挂横幅检查且失败静默"


def test_index_banner_no_forced_redirect(index_html: str) -> None:
    banner_fn = index_html[index_html.index("async function checkSetupBanner()"):]
    banner_fn = banner_fn[:banner_fn.index("function escapeHtml")]
    assert "location.href" not in banner_fn and "location.replace" not in banner_fn, \
        "决策③:只引导,不强制重定向"


def test_setup_route_redirects_to_static_page(http) -> None:
    """/setup → 302 /setup.html(横幅与 README 都写 /setup,静态页实际叫 setup.html)。"""
    code, _ = http("GET", "/setup")
    assert code in (302, 307), f"/setup 应重定向,实际 {code}"


@pytest.mark.skipif(NODE is None, reason="环境未安装 node")
def test_index_inline_js_still_passes_node_check(index_html: str, tmp_path: Path) -> None:
    blocks = re.findall(r"<script>(.*?)</script>", index_html, re.S)
    script = tmp_path / "index_inline.js"
    script.write_text(blocks[0], encoding="utf-8")
    result = subprocess.run([NODE, "--check", str(script)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"内联脚本语法错误:\n{result.stderr}"
