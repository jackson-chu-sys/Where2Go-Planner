"""维基百科 / Wikimedia Commons 图片兜底数据源(TASK-8a1)。

为什么要有(见 ``docs/TASK-8-CONTRACT.md`` §1):高德 POI 图是**主**图源,但乡镇与小
景点经常 ``photos=[]``,所以用维基补一手 —— 8/8 命中且有图,~1.5s,直连可用。

实测口径(2026-10-01 容器内实跑,**勿改**):

* **geosearch 优先于 search**:``generator=geosearch&ggscoord=lat|lng&ggsradius=10000
  &ggslimit=5&prop=pageimages`` 拿到的是**点近邻的实景页**(西湖点 → 钱王祠 / 丁鹤年墓亭,
  崇儒乡点 → 崇儒畲族乡 / 飞路塔);名称直搜(``generator=search``)会落到**上位页**
  (「汤泽高原滑雪场」→「湯澤町」)→ 只作兜底。
* 主图 = ``prop=pageimages&piprop=thumbnail&pithumbsize=800`` 的 ``thumbnail.source``。
* Commons 相册:``generator=search&gsrsearch=…&gsrnamespace=6&prop=imageinfo&iiprop=url
  &iiurlwidth=640`` → ``imageinfo[0].thumburl``,**带 ``?utm_*`` 查询串,落库前剥掉**。

失败口径与 :mod:`data_sources.amap` **相反**:维基只是兜底装饰,任何异常(断网、超时、
限流、响应形状不符、页面没有图)都收敛成 ``{"images": [], "via": "none"}``,**绝不抛**
—— 调用方(:mod:`services.place_media`)据此写负缓存,不让一次图片兜底毁掉详情弹窗。
公开函数都接受 ``environ=`` / ``session=`` 关键字注入,单测全程 mock 不触网。
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any, Mapping, Optional
from urllib.parse import quote

from ._common import (
    USER_AGENT,
    build_session,
    http_json,
    normalize_timeout,
)

SOURCE_NAME = "wikimedia"

#: 中文维基与 Commons 的 API 端点(``WHERE2GO_WIKI_API`` / ``WHERE2GO_COMMONS_API`` 可覆盖)
WIKI_API = "https://zh.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKI_HOST = "https://{lang}.wikipedia.org"
ENV_WIKI_API = "WHERE2GO_WIKI_API"
ENV_COMMONS_API = "WHERE2GO_COMMONS_API"

DEFAULT_LANG = "zh"
DEFAULT_IMAGE_LIMIT = 4
IMAGE_LIMIT_MAX = 20
#: geosearch 口径(契约 §1):半径 10km、最多 5 页,取**最近且有主图**的一页
GEOSEARCH_RADIUS_M = 10000
GEOSEARCH_LIMIT = 5
#: 名称直搜只作兜底,取最匹配的一条(实测会落到上位页,给多了反而挑错)
SEARCH_LIMIT = 1
THUMB_SIZE_PX = 800
COMMONS_THUMB_WIDTH_PX = 640
#: Commons 的文件命名空间(``File:``)
FILE_NAMESPACE = 6
EXTRACT_MAX_LEN = 400
REQUEST_TIMEOUT_S = 10.0

#: ``via`` 三态:近邻地理搜索命中 / 名称直搜兜底命中 / 什么都没拿到
VIA_GEOSEARCH = "geosearch"
VIA_SEARCH = "search"
VIA_NONE = "none"
VIAS: tuple[str, ...] = (VIA_GEOSEARCH, VIA_SEARCH, VIA_NONE)

_LANG_RE = re.compile(r"[^a-z0-9-]")

_session_lock = threading.Lock()
_default_session: Optional[Any] = None


# --------------------------------------------------------------------------- #
# 基础工具:session / 端点 / 归一化
# --------------------------------------------------------------------------- #


def _resolve_session(session: Optional[Any]) -> Any:
    """注入的 session 优先;否则用进程内共享的默认 session(代理口径见 ``_common``)。"""
    global _default_session
    if session is not None:
        return session
    with _session_lock:
        if _default_session is None:
            # "wikimedia" 在 _common.DEFAULT_SOURCE_PROXY 里是 env:沿用环境变量代理
            _default_session = build_session(USER_AGENT, source=SOURCE_NAME)
        return _default_session


def clean_lang(lang: Any) -> str:
    """语言码归一(``zh`` / ``en`` / ``zh-hant`` …);非法值回落 :data:`DEFAULT_LANG`。"""
    text = _LANG_RE.sub("", str(lang or "").strip().lower())
    return text or DEFAULT_LANG


def resolve_wiki_api(lang: Any = DEFAULT_LANG, environ: Optional[Mapping[str, str]] = None) -> str:
    """维基 API 端点:``WHERE2GO_WIKI_API`` 覆盖 > 按语言拼 host(缺省 ``zh`` = :data:`WIKI_API`)。"""
    env = os.environ if environ is None else environ
    given = str(env.get(ENV_WIKI_API) or "").strip()
    if given:
        return given
    language = clean_lang(lang)
    if language == DEFAULT_LANG:
        return WIKI_API
    return f"{WIKI_HOST.format(lang=language)}/w/api.php"


def resolve_commons_api(environ: Optional[Mapping[str, str]] = None) -> str:
    """Commons API 端点:``WHERE2GO_COMMONS_API`` 覆盖 > :data:`COMMONS_API`。"""
    env = os.environ if environ is None else environ
    given = str(env.get(ENV_COMMONS_API) or "").strip()
    return given or COMMONS_API


def clean_limit(limit: Any) -> int:
    """图片张数上限钳到 ``[1, 20]``;非法值回落 :data:`DEFAULT_IMAGE_LIMIT`。"""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_IMAGE_LIMIT
    return max(1, min(value, IMAGE_LIMIT_MAX))


def clean_coords(lat: Any, lng: Any) -> Optional[tuple[float, float]]:
    """坐标归一 ``(lat, lng)``;拿不到/越界返回 ``None``(**不抛**:退化成名称直搜)。"""
    try:
        latitude = float(lat)
        longitude = float(lng)
    except (TypeError, ValueError):
        return None
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        return None
    return latitude, longitude


def normalize_image_url(url: Any) -> Optional[str]:
    """图片 URL 归一:``http://`` 升 ``https://``、协议相对 ``//`` 补 ``https:``;其余 → ``None``。

    **绝不编造图源**:认不出来的 url 直接丢掉(前端 https 页面里 http 图会被当混合内容拦掉)。
    """
    text = str(url or "").strip()
    if not text:
        return None
    if text.startswith("//"):
        text = "https:" + text
    if text.startswith("http://"):
        text = "https://" + text[len("http://"):]
    if not text.startswith("https://") or len(text) <= len("https://"):
        return None
    return text


def strip_thumb_query(url: Any) -> str:
    """剥掉 ``?`` 之后的查询串(Commons 的 ``thumburl`` 带 ``?utm_*``,契约 §1 要求落库前去掉)。"""
    return str(url or "").strip().split("?", 1)[0]


def empty_result() -> dict[str, Any]:
    """兜底空结果的固定形状(键**恒定**,前端只读固定键)。"""
    return {
        "page_title": "",
        "page_url": "",
        "extract": "",
        "images": [],
        "via": VIA_NONE,
    }


# --------------------------------------------------------------------------- #
# 响应解析(formatversion=1 的 ``pages`` 是 dict,=2 是 list,两种都吃)
# --------------------------------------------------------------------------- #


def _page_index(page: Mapping[str, Any]) -> int:
    """generator 给的 ``index``(geosearch 下就是**距离序**);没有则排到最后。"""
    try:
        return int(page.get("index"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1 << 30


def parse_pages(payload: Any) -> list[dict[str, Any]]:
    """``query.pages`` → 页面列表,按 ``index`` 升序(geosearch 下 = 由近及远)。"""
    if not isinstance(payload, dict):
        return []
    query = payload.get("query")
    if not isinstance(query, dict):
        return []
    raw = query.get("pages")
    if isinstance(raw, list):
        pages = [item for item in raw if isinstance(item, dict)]
    elif isinstance(raw, dict):
        pages = [item for item in raw.values() if isinstance(item, dict)]
    else:
        return []
    return sorted(pages, key=_page_index)


def page_title(page: Any) -> str:
    """页面标题;脏数据 → ``""``。"""
    if not isinstance(page, Mapping):
        return ""
    return str(page.get("title") or "").strip()


def page_thumbnail(page: Any) -> Optional[str]:
    """页面主图(``prop=pageimages&piprop=thumbnail``)→ https URL;无图 → ``None``。

    兼容三种形状:``pageimages.thumbnail.source``(formatversion=1)、
    ``thumbnail.source``(formatversion=2)与 ``pageimages`` 直接是 url 字符串。
    """
    if not isinstance(page, Mapping):
        return None
    for holder in (page.get("pageimages"), page.get("thumbnail")):
        if isinstance(holder, Mapping):
            inner = holder.get("thumbnail")
            thumb = inner if isinstance(inner, Mapping) else holder
            url = normalize_image_url(
                strip_thumb_query(thumb.get("source") or thumb.get("url"))
            )
            if url:
                return url
        elif isinstance(holder, str):
            url = normalize_image_url(strip_thumb_query(holder))
            if url:
                return url
    return None


def page_url(page: Any, *, lang: Any = DEFAULT_LANG) -> str:
    """维基页外链:``inprop=url`` 给的 ``fullurl`` 优先,没有就按标题拼一个。"""
    if not isinstance(page, Mapping):
        return ""
    given = str(page.get("fullurl") or "").strip()
    if given:
        return given
    title = page_title(page)
    if not title:
        return ""
    host = WIKI_HOST.format(lang=clean_lang(lang))
    return f"{host}/wiki/{quote(title.replace(' ', '_'), safe='')}"


def page_extract(page: Any) -> str:
    """首段纯文本摘要(``prop=extracts&exintro&explaintext``);压空白并截到 400 字。"""
    if not isinstance(page, Mapping):
        return ""
    text = page.get("extract")
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:EXTRACT_MAX_LEN]


def dedupe_images(images: Any) -> list[dict[str, str]]:
    """图片列表按 url 去重保序,只留 ``{"url","title"}`` 两个键;没有合法 url 的丢掉。"""
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in images or []:
        if not isinstance(item, Mapping):
            continue
        url = normalize_image_url(item.get("url"))
        if url is None or url in seen:
            continue
        seen.add(url)
        merged.append({"url": url, "title": str(item.get("title") or "").strip()})
    return merged


def nearest_with_image(payload: Any) -> Optional[dict[str, Any]]:
    """geosearch 响应里**最近且有主图**的一页(``parse_pages`` 已按 index = 距离序排好)。"""
    for page in parse_pages(payload):
        if page_thumbnail(page):
            return page
    return None


# --------------------------------------------------------------------------- #
# 请求参数(照契约 §1 的实测查询串拼)
# --------------------------------------------------------------------------- #


def page_props() -> dict[str, Any]:
    """页面级 prop 参数:主图(800px 缩略图)+ 首段摘要 + 页面外链。"""
    return {
        "prop": "pageimages|extracts|info",
        "piprop": "thumbnail",
        "pithumbsize": THUMB_SIZE_PX,
        "inprop": "url",
        "exintro": 1,
        "explaintext": 1,
        "exchars": EXTRACT_MAX_LEN,
        "exlimit": "max",
    }


def geosearch_params(lat: float, lng: float) -> dict[str, Any]:
    """近邻实景页检索参数(``ggscoord`` 是 **纬度|经度**,与高德相反,别写错)。"""
    return {
        "action": "query",
        "format": "json",
        "generator": "geosearch",
        "ggscoord": f"{float(lat):.6f}|{float(lng):.6f}",
        "ggsradius": GEOSEARCH_RADIUS_M,
        "ggslimit": GEOSEARCH_LIMIT,
        **page_props(),
    }


def search_params(keyword: str) -> dict[str, Any]:
    """名称直搜兜底参数(实测会落到上位页,所以只取最匹配的一条)。"""
    return {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": str(keyword),
        "gsrlimit": SEARCH_LIMIT,
        **page_props(),
    }


def commons_params(keyword: str, limit: int) -> dict[str, Any]:
    """Commons 文件检索参数(命名空间 6 = ``File:``,缩略图宽 640)。"""
    return {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": str(keyword),
        "gsrnamespace": FILE_NAMESPACE,
        "gsrlimit": max(1, int(limit)),
        "prop": "imageinfo",
        "iiprop": "url",
        "iiurlwidth": COMMONS_THUMB_WIDTH_PX,
    }


def _query(session: Any, url: str, params: Mapping[str, Any]) -> Any:
    """发一次维基 API 请求(JSON);失败抛 :class:`DataSourceError`,由上层兜住。"""
    return http_json(
        session,
        url,
        source=SOURCE_NAME,
        params=dict(params),
        timeout=normalize_timeout(REQUEST_TIMEOUT_S),
    )


def resolve_page(
    session: Any,
    wiki_api: str,
    keyword: str,
    coords: Optional[tuple[float, float]],
) -> tuple[Optional[dict[str, Any]], str]:
    """定位一个维基页:先 geosearch(近邻有图页)再 search(名称直搜)兜底。

    返回 ``(page, via)``;两条路都没页 → ``(None, "none")``。
    """
    if coords is not None:
        page = nearest_with_image(
            _query(session, wiki_api, geosearch_params(coords[0], coords[1]))
        )
        if page is not None:
            return page, VIA_GEOSEARCH
    for page in parse_pages(_query(session, wiki_api, search_params(keyword))):
        if page_title(page):
            return page, VIA_SEARCH
    return None, VIA_NONE


def commons_images(
    session: Any,
    keyword: str,
    environ: Optional[Mapping[str, str]] = None,
    *,
    limit: int = DEFAULT_IMAGE_LIMIT,
) -> list[dict[str, str]]:
    """Commons 相册补图:``[{"url", "title"}]``,``thumburl`` 已剥掉查询串并升 https。"""
    wanted = int(limit)
    if wanted <= 0:
        return []
    payload = _query(session, resolve_commons_api(environ), commons_params(keyword, wanted))
    images: list[dict[str, str]] = []
    for page in parse_pages(payload):
        info = page.get("imageinfo")
        item = None
        if isinstance(info, list) and info and isinstance(info[0], Mapping):
            item = info[0]
        elif isinstance(info, Mapping):
            item = info
        if item is None:
            continue
        url = normalize_image_url(strip_thumb_query(item.get("thumburl") or item.get("url")))
        if url is None:
            continue
        images.append({"url": url, "title": page_title(page)})
        if len(images) >= wanted:
            break
    return images


# --------------------------------------------------------------------------- #
# 对外主入口
# --------------------------------------------------------------------------- #


def wikipedia_media(
    name: str,
    lat: float,
    lng: float,
    *,
    lang: str = DEFAULT_LANG,
    limit: int = DEFAULT_IMAGE_LIMIT,
    environ: Optional[Mapping[str, str]] = None,
    session: Optional[Any] = None,
) -> dict[str, Any]:
    """查一个 POI 的维基资料:``{"page_title", "page_url", "extract", "images", "via"}``。

    顺序:**geosearch**(点近邻的实景页,``via="geosearch"``)→ **名称直搜**兜底
    (``via="search"``)→ 主图取该页 ``pageimages`` 的 800px 缩略图 → 再用 Commons
    文件检索补到 ``limit`` 张(``thumburl`` 剥查询串)。

    **任何失败都返回 :func:`empty_result`(``via="none"``),不抛**:没配代理、断网、
    限流、页面没图、响应形状不符一律降级,调用方据此写负缓存(见
    :mod:`services.place_media`)。``name`` 为空时连请求都不发。
    """
    keyword = str(name or "").strip()
    language = clean_lang(lang)
    count = clean_limit(limit)
    result = empty_result()
    if not keyword:
        return result

    env = dict(os.environ if environ is None else environ)
    resolved_session = _resolve_session(session)
    coords = clean_coords(lat, lng)
    try:
        page, via = resolve_page(resolved_session, resolve_wiki_api(language, env), keyword, coords)
    except Exception:  # noqa: BLE001 - 兜底图源:任何异常都降级成"没查到",绝不炸详情弹窗
        return empty_result()
    if page is None:
        return result

    title = page_title(page)
    result.update({
        "page_title": title,
        "page_url": page_url(page, lang=language),
        "extract": page_extract(page),
        "via": via,
    })
    images: list[dict[str, str]] = []
    thumb = page_thumbnail(page)
    if thumb:
        images.append({"url": thumb, "title": title})
    try:
        images.extend(
            commons_images(
                resolved_session, keyword, env, limit=max(0, count - len(images))
            )
        )
    except Exception:  # noqa: BLE001 - Commons 只是补充,挂了也保留维基主图
        pass
    result["images"] = dedupe_images(images)[:count]
    return result
