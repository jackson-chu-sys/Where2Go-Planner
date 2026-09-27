"""数据源层公共工具:统一 User-Agent、超时与中文错误处理。

三个免费数据源(OSRM / Nominatim / Overpass)共用这里的 HTTP 封装,保证:

* 每个请求都带 ``User-Agent``(Nominatim / Overpass 的礼貌要求);
* 每个请求都有 timeout,且不超过 ``MAX_TIMEOUT``(20s);
* 失败时抛出带明确中文说明的 :class:`DataSourceError`;其中可临时重试的
  (超时、连接失败、限流、5xx、服务器繁忙返回的 HTML 错误页)再细分成
  :class:`TransientDataSourceError`,方便上层做端点切换与重试。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping, Optional

import requests

USER_AGENT: str = "Where2Go-POC/0.1 (dev)"
DEFAULT_TIMEOUT: float = 15.0
MAX_TIMEOUT: float = 20.0
SNIPPET_LEN: int = 240

# --------------------------------------------------------------------------- #
# 代理口径(2026-09-27 实测后新增)
# --------------------------------------------------------------------------- #
# 背景:数据源层用 requests.Session,默认读环境变量里的 HTTP_PROXY/HTTPS_PROXY。
# 本机(绿联 NAS 容器)配了全局代理 http://192.168.1.210:7892,三个数据源的网络
# 可达性却**不一致**(2026-09-27 实测):
#   * Overpass:走代理 HTTP 504(10.2s)/读超时(37s);**直连 2.4s 正常**
#     —— 抓取慢(BUG-1)/住宿查不到(BUG-5)的主因之一
#   * OSRM:走代理 2.7s、直连 0.7s(都能通,直连更快)
#   * Nominatim:**直连连接失败**(15s),必须走代理(1.4s)
# 所以"一刀切走代理/一刀切不走代理"都不对。这里按**数据源**决定代理口径,可用
# ``WHERE2GO_PROXY_<源>`` 覆盖(源名大写:OVERPASS / OSRM / NOMINATIM / LLM):
#   * ``off``  → 强制直连(忽略环境变量里的全局代理)
#   * ``env``  → 沿用环境变量(HTTP_PROXY/HTTPS_PROXY/NO_PROXY,requests 默认行为)
#   * 其他值   → 当作该数据源专用代理 URL(如 ``http://192.168.1.210:7892``)
PROXY_OFF: str = "off"
PROXY_ENV: str = "env"
ENV_PROXY_PREFIX: str = "WHERE2GO_PROXY_"
DEFAULT_SOURCE_PROXY: dict[str, str] = {
    "overpass": PROXY_OFF,   # 实测走代理 504/超时,直连才通
    "osrm": PROXY_OFF,       # 直连更快(0.7s vs 2.7s),且目标站点不受限
    "nominatim": PROXY_ENV,  # 实测直连不通,必须走代理
    "llm": PROXY_ENV,        # aliyuncs / deepseek 在 NO_PROXY 白名单里,走不走都一样
}


def proxy_mode(source: Optional[str]) -> str:
    """某数据源的代理口径:``WHERE2GO_PROXY_<源>`` 优先,否则用默认表;未知源 = ``env``。"""
    if not source:
        return PROXY_ENV
    key = ENV_PROXY_PREFIX + str(source).strip().upper()
    given = (os.environ.get(key) or "").strip()
    if given:
        return given
    return DEFAULT_SOURCE_PROXY.get(str(source).strip().lower(), PROXY_ENV)


def apply_proxy_policy(session: Any, source: Optional[str]) -> Optional[dict[str, str]]:
    """把某数据源的代理口径应用到 session;返回实际生效的代理(``{}`` = 直连,``None`` = 沿用环境变量)。"""
    mode = proxy_mode(source)
    if mode == PROXY_ENV:
        session.trust_env = True
        session.proxies = {}
        return None
    session.trust_env = False
    if mode == PROXY_OFF:
        session.proxies = {}
        return {}
    proxies = {"http": mode, "https": mode}
    session.proxies = proxies
    return proxies


class DataSourceError(RuntimeError):
    """数据源调用失败:网络异常、超时、HTTP 错误码或响应格式与真实 API 不符。"""

    def __init__(self, source: str, message: str) -> None:
        self.source = source
        self.message = message
        super().__init__(f"[{source}] {message}")


class TransientDataSourceError(DataSourceError):
    """临时性失败(可重试 / 可换端点):超时、连接失败、限流、5xx、非 JSON 的繁忙错误页。"""


def build_session(user_agent: str = USER_AGENT, *, source: Optional[str] = None) -> requests.Session:
    """创建带统一 User-Agent 与 JSON Accept 头的 :class:`requests.Session`。

    ``source`` 给定时按 :func:`apply_proxy_policy` 应用该数据源的代理口径
    (``overpass`` / ``osrm`` / ``nominatim`` / ``llm``);不传 = 维持 requests 默认
    (读环境变量代理),现有调用与单测行为不变。
    """
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
    if source:
        apply_proxy_policy(session, source)
    return session


def normalize_timeout(timeout: Optional[float]) -> float:
    """把调用方传入的 timeout 收敛到 (0, 20] 秒区间。"""
    value = DEFAULT_TIMEOUT if timeout is None else float(timeout)
    if value <= 0:
        raise ValueError(f"timeout 必须为正数(秒),收到:{timeout!r}")
    return min(value, MAX_TIMEOUT)


def http_json(
    session: Any,
    url: str,
    *,
    source: str,
    method: str = "GET",
    params: Optional[Mapping[str, Any]] = None,
    data: Optional[Mapping[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    headers: Optional[Mapping[str, str]] = None,
) -> Any:
    """发起 HTTP 请求并解析 JSON;失败时抛出带中文说明的 :class:`DataSourceError`。"""
    try:
        response = session.request(
            method, url, params=params, data=data, timeout=timeout, headers=dict(headers or {})
        )
    except requests.Timeout as exc:
        raise TransientDataSourceError(source, f"请求超时(>{timeout:g}s):{url}") from exc
    except requests.ConnectionError as exc:
        raise TransientDataSourceError(source, f"网络连接失败:{_exc_text(exc)}") from exc
    except requests.RequestException as exc:
        raise DataSourceError(source, f"请求异常({type(exc).__name__}):{_exc_text(exc)}") from exc

    status = getattr(response, "status_code", None)
    if status == 429:
        raise TransientDataSourceError(source, f"被限流(HTTP 429),请稍后重试:{url}")
    if isinstance(status, int) and status >= 500:
        raise TransientDataSourceError(source, f"服务端错误(HTTP {status}):{_snippet(response)}")
    if status != 200:
        raise DataSourceError(source, f"HTTP 状态码 {status}:{_snippet(response)}")

    try:
        return json.loads(response.text)
    except (TypeError, ValueError) as exc:
        # Overpass 繁忙时会返回 504 + HTML 错误页;这里也兜住 200 + 非 JSON 的情况。
        raise TransientDataSourceError(
            source, f"响应不是合法 JSON(可能是 HTML 错误页):{_snippet(response)}"
        ) from exc


HTML_TAG_RE = re.compile(r"<[^>]+>")


def _snippet(response: Any, limit: int = SNIPPET_LEN) -> str:
    """截取响应正文片段用于排错:去掉 HTML 标签、压掉换行与多余空白。

    Overpass 繁忙时返回的是 HTML 错误页,原样截取只会看到一堆 XML 声明,
    去标签后才能读到真正的原因(如 "The server is probably too busy")。
    """
    text = getattr(response, "text", "") or ""
    if "<" in text and ">" in text:
        text = HTML_TAG_RE.sub(" ", text)
    return " ".join(text.split())[:limit]


def _exc_text(exc: BaseException, limit: int = SNIPPET_LEN) -> str:
    """异常信息单行化,便于打印中文报错。"""
    return " ".join(str(exc).split())[:limit]
