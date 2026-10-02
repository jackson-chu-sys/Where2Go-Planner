"""数据源层公共工具:统一 User-Agent、超时、代理口径与中文错误处理。

所有数据源(高德 / Nominatim / Photon / LLM)共用这里的 HTTP 封装,保证:

* 每个请求都带 ``User-Agent``(Nominatim 的礼貌要求);
* 每个请求都有 timeout,且不超过 ``MAX_TIMEOUT``(20s);
* 失败时抛出带明确中文说明的 :class:`DataSourceError`;其中可临时重试的
  (超时、连接失败、限流、5xx、服务器繁忙返回的 HTML 错误页)再细分成
  :class:`TransientDataSourceError`,方便上层做端点切换与重试。
"""

from __future__ import annotations

import json
import math
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
# 本机(绿联 NAS 容器)配了全局代理 http://192.168.1.210:7892,各数据源的网络
# 可达性却**不一致**(2026-09-27 / 2026-10-01 实测):
#   * 高德(TASK-9 起的主数据源):REST **国内直连**稳定 0.1~0.2s,走代理反而慢
#   * Nominatim:**直连连接失败**(15s),必须走代理(1.4s)
#   * Photon(TASK-6a 新增,地理编码降级链):**直连 1.1s 正常**,走代理 5s 挂
#     —— 产品环境没有 mihomo 代理,所以 Photon 必须 off;Nominatim 只作末腿降级
# 所以"一刀切走代理/一刀切不走代理"都不对。这里按**数据源**决定代理口径,可用
# ``WHERE2GO_PROXY_<源>`` 覆盖(源名大写:AMAP / NOMINATIM / PHOTON / LLM):
#   * ``off``  → 强制直连(忽略环境变量里的全局代理)
#   * ``env``  → 沿用环境变量(HTTP_PROXY/HTTPS_PROXY/NO_PROXY,requests 默认行为)
#   * 其他值   → 当作该数据源专用代理 URL(如 ``http://192.168.1.210:7892``)
PROXY_OFF: str = "off"
PROXY_ENV: str = "env"
ENV_PROXY_PREFIX: str = "WHERE2GO_PROXY_"
DEFAULT_SOURCE_PROXY: dict[str, str] = {
    "amap": PROXY_OFF,       # 高德 REST 国内直连(2026-10-01 实测),TASK-9 起的主数据源
    "nominatim": PROXY_ENV,  # 实测直连不通,必须走代理
    "photon": PROXY_OFF,     # 实测直连 1.1s、走代理 5s 挂(产品环境无代理)
    "llm": PROXY_ENV,        # aliyuncs / deepseek 在 NO_PROXY 白名单里,走不走都一样
    "wikimedia": PROXY_ENV,  # 维基/Commons(TASK-8a1 图片兜底):沿用环境变量代理口径
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
    (``amap`` / ``nominatim`` / ``photon`` / ``llm``);不传 = 维持 requests 默认
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
        # 公共实例繁忙时可能返回 504 + HTML 错误页;这里也兜住 200 + 非 JSON 的情况。
        raise TransientDataSourceError(
            source, f"响应不是合法 JSON(可能是 HTML 错误页):{_snippet(response)}"
        ) from exc


HTML_TAG_RE = re.compile(r"<[^>]+>")


def _snippet(response: Any, limit: int = SNIPPET_LEN) -> str:
    """截取响应正文片段用于排错:去掉 HTML 标签、压掉换行与多余空白。

    公共数据源繁忙时可能返回 HTML 错误页,原样截取只会看到一堆标记声明,
    去标签后才能读到真正的原因(如 "The server is probably too busy")。
    """
    text = getattr(response, "text", "") or ""
    if "<" in text and ">" in text:
        text = HTML_TAG_RE.sub(" ", text)
    return " ".join(text.split())[:limit]


def _exc_text(exc: BaseException, limit: int = SNIPPET_LEN) -> str:
    """异常信息单行化,便于打印中文报错。"""
    return " ".join(str(exc).split())[:limit]


# --------------------------------------------------------------------------- #
# 大圆距离(TASK-9c 起住这里:原先在 overpass.py,该模块随 Overpass 一并退役)
# --------------------------------------------------------------------------- #

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两点间大圆距离(公里):POI 排序、环带收敛与直线里程估算共用这一个口径。"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))
