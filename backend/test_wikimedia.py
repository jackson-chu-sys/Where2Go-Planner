"""TASK-8a1 单测:维基/Commons 图片兜底数据源(:mod:`data_sources.wikimedia`)。

全程不触网:

* ``no_network`` 把 :meth:`requests.Session.request` 换成直接抛错,任何偷偷联网当场失败;
* HTTP 用 :class:`FakeSession` 替身,按顺序返回预设响应,断言**端点、查询参数
  (ggscoord 的 ``lat|lng`` 顺序、ggsradius/ggslimit、pithumbsize、gsrnamespace、iiurlwidth)**
  与**真实响应形状下的解析**(formatversion=1 的 ``pages`` dict / =2 的 list 都吃);
* 兜底纪律:任何失败(HTTP 5xx、超时、非 JSON、页面没图)一律收敛成
  ``{"images": [], "via": "none"}``,**绝不抛**;Commons 的 ``thumburl`` 剥掉 ``?`` 后查询串。

运行:``cd backend && ../.venv/bin/python -m pytest -q test_wikimedia.py``
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

import pytest
import requests

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from data_sources import wikimedia  # noqa: E402

# --------------------------------------------------------------------------- #
# 测试替身(与 backend/test_amap.py 同一套写法)
# --------------------------------------------------------------------------- #


class FakeResponse:
    """最小 response 替身:只需要 ``status_code`` 与 ``text``。"""

    def __init__(self, payload: Any = None, *, status_code: int = 200, text: Optional[str] = None) -> None:
        self.status_code = status_code
        if text is None:
            text = "" if payload is None else json.dumps(payload, ensure_ascii=False)
        self.text = text


class FakeSession:
    """记录调用参数并按顺序返回预设响应的 session 替身(不触网)。"""

    def __init__(self, *responses: Any) -> None:
        given = list(responses) or [{}]
        self.responses = [
            item if isinstance(item, (FakeResponse, BaseException)) else FakeResponse(item)
            for item in given
        ]
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        headers: Optional[dict[str, str]] = None,
        **kwargs: Any,
    ) -> FakeResponse:
        self.calls.append({"method": method, "url": url, "params": dict(params or {}), "timeout": timeout})
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底:任何 requests 调用都视为测试失败(本套单测必须纯 mock)。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


# --------------------------------------------------------------------------- #
# 真实响应样本(2026-10-01 契约 §1 实测形状;formatversion=1 → pages 是 dict)
# --------------------------------------------------------------------------- #

WEST_LAKE = (30.246, 120.149)  # (lat, lng)

GEO_PAGE_NEAR_NO_IMAGE = {
    "pageid": 111, "ns": 0, "index": 1, "title": "丁鹤年墓亭",
    "fullurl": "https://zh.wikipedia.org/wiki/%E4%B8%81%E9%B9%A4%E5%B9%B4%E5%A2%93%E4%BA%AD",
    "extract": "丁鹤年墓亭位于杭州西湖西南。",
}
GEO_PAGE_WITH_IMAGE = {
    "pageid": 222, "ns": 0, "index": 2, "title": "钱王祠",
    "fullurl": "https://zh.wikipedia.org/wiki/%E9%92%B1%E7%8E%8B%E7%A5%A0",
    "extract": "钱王祠在西湖东岸,祀吴越国王。",
    "pageimages": {"thumbnail": {
        "source": "http://upload.wikimedia.org/zh/thumb/q/q1/Qianwang.jpg/800px-Qianwang.jpg",
        "width": 800, "height": 600,
    }},
}
GEO_OK = {"query": {"pages": {"111": GEO_PAGE_NEAR_NO_IMAGE, "222": GEO_PAGE_WITH_IMAGE}}}
GEO_NO_IMAGE = {"query": {"pages": {"111": GEO_PAGE_NEAR_NO_IMAGE}}}
GEO_EMPTY = {"query": {"pages": {}}}

SEARCH_PAGE = {
    "pageid": 333, "ns": 0, "index": 1, "title": "湯澤町",
    "fullurl": "https://zh.wikipedia.org/wiki/%E6%B9%AF%E6%BE%A4%E7%94%BA",
    "extract": "湯澤町位於日本新潟縣。",
    "pageimages": {"thumbnail": {"source": "https://upload.wikimedia.org/yuzawa.jpg"}},
}
SEARCH_OK = {"query": {"pages": {"333": SEARCH_PAGE}}}
SEARCH_EMPTY = {"query": {"pages": {}}}

COMMONS_FILE_1 = {
    "pageid": 9001, "ns": 6, "index": 1, "title": "File:West Lake 1.jpg",
    "imageinfo": [{
        "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/W1.jpg/640px-W1.jpg?utm_source=foo",
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/W1.jpg",
    }],
}
COMMONS_FILE_2 = {
    "pageid": 9002, "ns": 6, "index": 2, "title": "File:West Lake 2.jpg",
    "imageinfo": [{
        "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/b/bc/W2.jpg/640px-W2.jpg?20261001",
        "url": "https://upload.wikimedia.org/wikipedia/commons/b/bc/W2.jpg",
    }],
}
COMMONS_OK = {"query": {"pages": {"9001": COMMONS_FILE_1, "9002": COMMONS_FILE_2}}}
WIKI_THUMB_HTTPS = "https://upload.wikimedia.org/zh/thumb/q/q1/Qianwang.jpg/800px-Qianwang.jpg"


def media(session: FakeSession, **kwargs: Any) -> dict[str, Any]:
    """统一入口:名字/坐标给默认值,``environ`` 恒定注入(不读真实环境变量)。"""
    options: dict[str, Any] = {"environ": {}, "session": session}
    options.update(kwargs)
    return wikimedia.wikipedia_media(options.pop("name", "西湖"), options.pop("lat", WEST_LAKE[0]),
                                     options.pop("lng", WEST_LAKE[1]), **options)


# --------------------------------------------------------------------------- #
# geosearch 优先 / search 兜底 / via 字段
# --------------------------------------------------------------------------- #


def test_geosearch_hit_picks_nearest_page_with_image() -> None:
    """近邻两页:最近的没图、次近的有图 → 取**有图**的那页,via=geosearch。"""
    result = media(FakeSession(GEO_OK, COMMONS_OK))
    assert result["via"] == "geosearch"
    assert result["page_title"] == "钱王祠"
    assert result["page_url"] == GEO_PAGE_WITH_IMAGE["fullurl"]
    assert result["extract"] == "钱王祠在西湖东岸,祀吴越国王。"
    assert result["images"][0] == {"url": WIKI_THUMB_HTTPS, "title": "钱王祠"}


def test_geosearch_request_params_match_contract() -> None:
    """geosearch 查询串按契约 §1:ggscoord=lat|lng(纬度在前)、半径 10km、5 页、主图 800px。"""
    session = FakeSession(GEO_OK, COMMONS_OK)
    media(session)
    params = session.calls[0]["params"]
    assert session.calls[0]["url"] == wikimedia.WIKI_API
    assert params["generator"] == "geosearch"
    assert params["ggscoord"] == "30.246000|120.149000"
    assert params["ggsradius"] == 10000
    assert params["ggslimit"] == 5
    assert params["piprop"] == "thumbnail"
    assert params["pithumbsize"] == 800
    assert params["inprop"] == "url"
    assert params["format"] == "json"
    assert session.calls[0]["timeout"] == wikimedia.REQUEST_TIMEOUT_S


def test_search_fallback_when_geosearch_has_no_image() -> None:
    """近邻页都没图 → 名称直搜兜底(实测会落到上位页),via=search。"""
    session = FakeSession(GEO_NO_IMAGE, SEARCH_OK, COMMONS_OK)
    result = media(session, name="汤泽高原滑雪场")
    assert result["via"] == "search"
    assert result["page_title"] == "湯澤町"
    params = session.calls[1]["params"]
    assert params["generator"] == "search"
    assert params["gsrsearch"] == "汤泽高原滑雪场"
    assert params["gsrlimit"] == 1


def test_search_fallback_when_geosearch_returns_no_page() -> None:
    result = media(FakeSession(GEO_EMPTY, SEARCH_OK, COMMONS_OK))
    assert result["via"] == "search"
    assert result["images"][0]["url"] == "https://upload.wikimedia.org/yuzawa.jpg"


def test_invalid_coordinates_skip_geosearch() -> None:
    """坐标非法(纬度越界)→ 不发 geosearch,直接走名称直搜(**不抛**)。"""
    session = FakeSession(SEARCH_OK, COMMONS_OK)
    result = media(session, lat=999.0, lng=120.149)
    assert result["via"] == "search"
    assert session.calls[0]["params"]["generator"] == "search"


def test_no_page_anywhere_returns_none_via() -> None:
    """两条路都没页 → 空结果、via=none,且**不再**去问 Commons(不猜图源)。"""
    session = FakeSession(GEO_EMPTY, SEARCH_EMPTY, COMMONS_OK)
    result = media(session)
    assert result == wikimedia.empty_result()
    assert result["via"] == "none"
    assert len(session.calls) == 2


# --------------------------------------------------------------------------- #
# Commons 补图:thumburl 剥查询串、补到 limit、失败不扩散
# --------------------------------------------------------------------------- #


def test_commons_params_and_thumburl_query_stripped() -> None:
    session = FakeSession(GEO_OK, COMMONS_OK)
    result = media(session)
    params = session.calls[1]["params"]
    assert session.calls[1]["url"] == wikimedia.COMMONS_API
    assert params["gsrnamespace"] == 6
    assert params["iiurlwidth"] == 640
    assert params["iiprop"] == "url"
    assert params["gsrlimit"] == 3  # limit=4 减去维基主图 1 张
    assert all("?" not in image["url"] for image in result["images"])
    assert result["images"][1]["title"] == "File:West Lake 1.jpg"


def test_commons_fills_up_to_limit() -> None:
    """维基主图 1 张 + Commons 2 张 → limit=4 时共 3 张;limit=2 时截到 2 张。"""
    assert len(media(FakeSession(GEO_OK, COMMONS_OK), limit=4)["images"]) == 3
    result = media(FakeSession(GEO_OK, COMMONS_OK), limit=2)
    assert len(result["images"]) == 2
    assert result["images"][0]["url"] == WIKI_THUMB_HTTPS


def test_commons_not_called_when_limit_is_one() -> None:
    """limit=1 时维基主图已占满 → 不再发 Commons 请求(省一次网络)。"""
    session = FakeSession(GEO_OK, COMMONS_OK)
    result = media(session, limit=1)
    assert len(session.calls) == 1
    assert result["images"] == [{"url": WIKI_THUMB_HTTPS, "title": "钱王祠"}]


def test_commons_failure_keeps_wiki_thumbnail() -> None:
    """Commons 挂了(连接失败)→ 维基主图仍在,**不抛**。"""
    result = media(FakeSession(GEO_OK, requests.ConnectionError("commons down")))
    assert result["via"] == "geosearch"
    assert result["images"] == [{"url": WIKI_THUMB_HTTPS, "title": "钱王祠"}]


def test_commons_http_error_keeps_wiki_thumbnail() -> None:
    result = media(FakeSession(GEO_OK, FakeResponse(None, status_code=500, text="boom")))
    assert [image["url"] for image in result["images"]] == [WIKI_THUMB_HTTPS]


def test_commons_dedupe_against_wiki_thumbnail() -> None:
    """Commons 返回与维基主图同一个 url → 去重,只剩一张。"""
    same = {"query": {"pages": {"9001": {
        "ns": 6, "index": 1, "title": "File:Same.jpg",
        "imageinfo": [{"thumburl": WIKI_THUMB_HTTPS + "?utm_x=1"}],
    }}}}
    result = media(FakeSession(GEO_OK, same))
    assert result["images"] == [{"url": WIKI_THUMB_HTTPS, "title": "钱王祠"}]


# --------------------------------------------------------------------------- #
# 失败一律降级(不抛)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("response", [
    FakeResponse(None, status_code=500, text="server error"),
    FakeResponse(None, status_code=429, text="too many requests"),
    FakeResponse(None, text="<html><body>too busy</body></html>"),
    requests.Timeout("read timed out"),
    requests.ConnectionError("connection refused"),
])
def test_wiki_failures_degrade_to_empty(response: Any) -> None:
    """HTTP 5xx / 429 / 非 JSON / 超时 / 连接失败 → 空结果、via=none,**绝不抛**。"""
    result = media(FakeSession(response))
    assert result == wikimedia.empty_result()
    assert result["images"] == []
    assert result["via"] == "none"


def test_empty_name_skips_network() -> None:
    """名字为空 → 连请求都不发(不猜图源)。"""
    session = FakeSession(GEO_OK, COMMONS_OK)
    assert media(session, name="   ") == wikimedia.empty_result()
    assert session.calls == []


def test_page_without_thumbnail_yields_no_images() -> None:
    """有页但页里没主图、Commons 也空 → page_title/page_url 仍在,images 为空。"""
    page = {"query": {"pages": {"333": {
        "pageid": 333, "index": 1, "title": "崇儒畲族乡",
        "fullurl": "https://zh.wikipedia.org/wiki/%E5%B4%87%E5%84%92%E7%95%B2%E6%97%8F%E4%B9%A1",
        "extract": "崇儒畲族乡位于霞浦县。",
    }}}}
    result = media(FakeSession(GEO_EMPTY, page, {"query": {"pages": {}}}))
    assert result["via"] == "search"
    assert result["page_title"] == "崇儒畲族乡"
    assert result["images"] == []


# --------------------------------------------------------------------------- #
# 端点 / 语言 / 归一化工具
# --------------------------------------------------------------------------- #


def test_lang_switches_wiki_host() -> None:
    session = FakeSession(GEO_OK, COMMONS_OK)
    media(session, lang="en")
    assert session.calls[0]["url"] == "https://en.wikipedia.org/w/api.php"


def test_env_endpoint_override() -> None:
    """``WHERE2GO_WIKI_API`` / ``WHERE2GO_COMMONS_API`` 可覆盖端点(内网镜像/单测用)。"""
    session = FakeSession(GEO_OK, COMMONS_OK)
    media(session, environ={
        wikimedia.ENV_WIKI_API: "https://wiki.internal/w/api.php",
        wikimedia.ENV_COMMONS_API: "https://commons.internal/w/api.php",
    })
    assert session.calls[0]["url"] == "https://wiki.internal/w/api.php"
    assert session.calls[1]["url"] == "https://commons.internal/w/api.php"


def test_resolve_api_defaults_and_os_environ_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    assert wikimedia.resolve_wiki_api("zh", {}) == wikimedia.WIKI_API
    assert wikimedia.resolve_commons_api({}) == wikimedia.COMMONS_API
    monkeypatch.setenv(wikimedia.ENV_WIKI_API, "https://zh.m.wikipedia.org/w/api.php")
    assert wikimedia.resolve_wiki_api("zh") == "https://zh.m.wikipedia.org/w/api.php"


def test_url_normalization_helpers() -> None:
    """http → https、协议相对补 https:、相对路径/空值一律丢(**不编造图源**)。"""
    assert wikimedia.normalize_image_url("http://a/x.jpg") == "https://a/x.jpg"
    assert wikimedia.normalize_image_url("//upload.wikimedia.org/x.jpg") == "https://upload.wikimedia.org/x.jpg"
    assert wikimedia.normalize_image_url("/relative.jpg") is None
    assert wikimedia.normalize_image_url("https://") is None
    assert wikimedia.normalize_image_url([]) is None
    assert wikimedia.strip_thumb_query("https://a/b.jpg?utm_source=x&y=1") == "https://a/b.jpg"
    assert wikimedia.strip_thumb_query(None) == ""


def test_clean_limit_and_lang_clamp() -> None:
    assert wikimedia.clean_limit(0) == 1
    assert wikimedia.clean_limit(999) == wikimedia.IMAGE_LIMIT_MAX
    assert wikimedia.clean_limit("x") == wikimedia.DEFAULT_IMAGE_LIMIT
    assert wikimedia.clean_lang("ZH-hant") == "zh-hant"
    assert wikimedia.clean_lang(None) == wikimedia.DEFAULT_LANG


def test_pages_parsed_in_index_order_and_v2_shape() -> None:
    """``pages`` 兼容 dict(formatversion=1)与 list(=2),都按 ``index`` 距离序排。"""
    v1 = wikimedia.parse_pages(GEO_OK)
    assert [page["title"] for page in v1] == ["丁鹤年墓亭", "钱王祠"]
    v2_payload = {"query": {"pages": [
        {"title": "远页", "index": 2, "thumbnail": {"source": "https://u/far.jpg"}},
        {"title": "近页", "index": 1},
    ]}}
    pages = wikimedia.parse_pages(v2_payload)
    assert [page["title"] for page in pages] == ["近页", "远页"]
    assert wikimedia.page_thumbnail(pages[1]) == "https://u/far.jpg"
    assert wikimedia.nearest_with_image(v2_payload)["title"] == "远页"
    assert wikimedia.parse_pages("nope") == []


def test_page_url_falls_back_to_title() -> None:
    """没有 ``fullurl`` 就按标题拼维基页外链(空格转下划线、URL 编码)。"""
    assert wikimedia.page_url({"title": "West Lake"}) == "https://zh.wikipedia.org/wiki/West_Lake"
    assert wikimedia.page_url({"title": "西湖"}, lang="zh").endswith("/wiki/%E8%A5%BF%E6%B9%96")
    assert wikimedia.page_url({"title": ""}) == ""
    assert wikimedia.page_url(None) == ""
