"""携程酒店 suggest(公开接口):酒店名 → 携程原生 ``hotelId`` / ``cityId``。

TASK-17(神朱 2026-10-09 拍板):住宿外链的城市要传 OTA **原生城市字段**,不再把
「城市+酒店名」拼进搜索词(实测经常搜不到)。调研(docs/research-ota-wechat-2026-10-09.md
§A1)确认:

* 携程 deep-link 最可靠形态 = ``https://hotels.ctrip.com/hotels/detail/?hotelId=<id>``
  (免登录、免日期、桌面浏览器直接渲染详情页);
* 次选列表页 = ``?cityId=<id>&searchWord=<酒店名>&directSearch=1&old=1``(``old=1``
  是命中开关,去掉回退全量);
* ``hotelId`` / ``cityId`` 从携程**公开 suggest 接口**解析(免登录、免 cookie):
  ``POST https://m.ctrip.com/restapi/soa2/21881/json/gaHotelSearchEngine``,
  ``{"searchType":"H","keyword":"<城市+酒店名>"}`` → ``Response.searchResults[0]``
  带 ``id``(hotelId)/``cityId``/``cityName``/``gdLat``/``gdLon``(高德坐标,可做
  同名异地消歧校验)。
* 携程 ``cityId`` 是自有体系(昆山=83),与高德 adcode(320583)**无换算关系**,
  只能动态解析 + 缓存,不维护静态映射表。

降级口径(照 wikimedia.py「尽力而为的增强源」):网络/HTTP/格式错误、空结果一律
返回 ``None`` **不抛** —— 外链是增强项,解析不到时前端退回旧搜索词拼法,不丢链接。
"""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Any, Mapping, Optional

from data_sources._common import (
    DataSourceError,
    build_session,
    normalize_timeout,
)

SOURCE_NAME = "ctrip"
SUGGEST_URL = "https://m.ctrip.com/restapi/soa2/21881/json/gaHotelSearchEngine"
# 与高德同口径的国内直连(代理表在 _common.DEFAULT_SOURCE_PROXY)
DEFAULT_TIMEOUT_S = 10.0
# 进程内最小间隔:懒补齐一次最多几条住宿,0.5s 足够礼貌
MIN_INTERVAL_S = 0.5
# 同名异地消歧:收藏坐标与 suggest 返回的高德坐标距离超过该值(米)视为不匹配
MAX_MATCH_M = 50_000.0

_HEAD = {
    "cid": "", "ctok": "", "cver": "1.0", "lang": "01", "sid": "",
    "syscode": "09", "auth": "", "extension": [],
}

_session: Optional[Any] = None
_session_lock = threading.Lock()
_last_call = 0.0
_throttle_lock = threading.Lock()


def _get_session() -> Any:
    global _session
    with _session_lock:
        if _session is None:
            _session = build_session(source=SOURCE_NAME)
        return _session


def _throttle() -> None:
    global _last_call
    with _throttle_lock:
        now = time.monotonic()
        wait = MIN_INTERVAL_S - (now - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_suggest(payload: Any) -> list[dict[str, Any]]:
    """解析 suggest 响应 → ``[{hotel_id, city_id, city_name, word, gd_lat, gd_lng}]``。

    只认 ``type == "Hotel"`` 的条目;``id``/``cityId`` 缺失的丢弃;坏结构返回 ``[]``
    (调用方据此降级,不猜)。
    """
    rows: list[dict[str, Any]] = []
    if not isinstance(payload, Mapping):
        return rows
    response = payload.get("Response")
    if not isinstance(response, Mapping):
        return rows
    results = response.get("searchResults")
    if not isinstance(results, list):
        return rows
    for item in results:
        if not isinstance(item, Mapping) or str(item.get("type") or "") != "Hotel":
            continue
        hotel_id = str(item.get("id") or "").strip()
        if not hotel_id.isdigit():
            continue
        raw_city = item.get("cityId")
        if raw_city is None:
            continue
        try:
            city_id = int(raw_city)
        except (TypeError, ValueError):
            continue
        rows.append({
            "hotel_id": hotel_id,
            "city_id": city_id,
            "city_name": str(item.get("cityName") or "").strip(),
            "word": str(item.get("word") or "").strip(),
            "gd_lat": float(item.get("gdLat") or 0.0),
            "gd_lng": float(item.get("gdLon") or 0.0),
        })
    return rows


def suggest_hotel(
    keyword: str,
    *,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    city_hint: Optional[str] = None,
    timeout: Optional[float] = None,
    session: Optional[Any] = None,
) -> Optional[dict[str, Any]]:
    """酒店名(建议「城市+酒店名」)→ 携程原生标识;解析不到/出错返回 ``None`` 不抛。

    ``lat``/``lng``(高德/GCJ-02 坐标)与 ``city_hint``(城市名)用于**同名异地消歧**,
    优先级见函数体注释;suggest 的坐标常为 0(实测),所以坐标只是首选、不作硬门控。
    """
    text = str(keyword or "").strip()
    if not text:
        return None
    http = session if session is not None else _get_session()
    body = json.dumps(
        {"searchType": "H", "keyword": text, "head": dict(_HEAD)}, ensure_ascii=False
    ).encode("utf-8")
    _throttle()
    try:
        response = http.request(
            "POST", SUGGEST_URL, data=body,
            timeout=normalize_timeout(timeout if timeout is not None else DEFAULT_TIMEOUT_S),
            headers={"Content-Type": "application/json"},
        )
        status = getattr(response, "status_code", None)
        if status != 200:
            return None
        rows = parse_suggest(json.loads(getattr(response, "text", "") or ""))
    except DataSourceError:
        return None
    except Exception:  # noqa: BLE001 - 尽力而为的增强源:任何失败都降级为 None
        return None
    if not rows:
        return None
    best = rows[0]
    matched = True
    # 消歧(2026-10-09 实测:suggest 的 gdLat/gdLon 常为 0,坐标门控只作首选):
    # ① 有坐标且 suggest 也带坐标 → 取距离 ≤ MAX_MATCH_M 的条目;
    # ② 坐标不可用 → 退而用城市名匹配(调用方传 city_hint);③ 都不行接受第一条
    #   (keyword 本就是「城市+酒店名」,首条置信度足够高,宁可用上也不丢链接)。
    if lat is not None and lng is not None:
        near = [
            row for row in rows
            if row["gd_lat"] and row["gd_lng"]
            and _haversine_m(float(lat), float(lng), row["gd_lat"], row["gd_lng"]) <= MAX_MATCH_M
        ]
        if near:
            best = near[0]
        elif city_hint:
            by_city = [row for row in rows if city_hint in row["city_name"] or row["city_name"] in city_hint]
            if by_city:
                best = by_city[0]
    elif city_hint:
        by_city = [row for row in rows if city_hint in row["city_name"] or row["city_name"] in city_hint]
        if by_city:
            best = by_city[0]
    return {**best, "keyword": text, "matched": matched}
