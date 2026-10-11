"""境内外判定(region gate,TASK-20 境外双轨 P1):城市名 / 坐标 → ``domestic`` 还是 ``overseas``。

为什么单开一个模块:境内链路的地理编码主腿是**高德**(``services.place_loader``),而高德
只覆盖中国 —— 境外起点(因特拉肯、皇后镇…)必须换一套判定与数据口径。第一步不是换检索源,
而是先**判出这个起点在不在境内**,判据只能来自 OSM 系两家(Nominatim / Photon)的国家码。

两条链的方向**刻意相反**(2026-10-11 主会话实测,契约 §0):

* **正向(城市名 → 国家)**:**Nominatim 主 → Photon 兜底**。Photon 的
  ``properties.countrycode`` 本身可靠(上海 CN / Interlaken CH / Queenstown NZ),但它对
  **中文境外地名**会错定位到国内(「因特拉肯」→ 香港「因特拉肯大道」countrycode=CN,
  把境外判成境内!);Nominatim(``accept-language=zh-CN`` + ``addressdetails=1``)中文境外
  8/8 全对(因特拉肯→ch、皇后镇→nz、巴黎→fr)。
* **逆向(坐标 → 国家)**:**Photon 主 → Nominatim 兜底**。Photon reverse 的境外坐标 3/3
  正确,而中文正向那个坑不影响 reverse。

口径三条(与 ``place_loader`` 的降级链同源):

1. **绝不抛异常**:每一腿宽 ``except Exception``,两腿都失败(断网 / 限流 / 无 key /
   Nominatim 直连时好时坏)一律返回 ``domestic`` + ``detector="none"`` —— 判不出就按境内,
   现状链路零影响,**不猜**;调用方据 ``detector`` 决定要不要落缓存(none 不落,下次重试)。
2. **一腿答不出国家码 = 该腿失败**,降到下一腿(空 ``country_code`` 不当"境内")。
3. 本模块**不落库、不碰 session、不 import place_loader**(避免循环依赖):缓存是 API 层的事
   (``OriginCache.region``),这里只用 :mod:`data_sources.nominatim` 与 :mod:`data_sources.photon`
   两个数据源模块,单测可像 ``ds_geocode`` 那样直接替换模块属性。

国家码统一**大写**后交给 :func:`classify_country_code`:``CN`` → ``domestic``,其他非空 →
``overseas``,空 / ``None`` / 非字符串 → ``domestic``。
"""

from __future__ import annotations

from typing import Any

from data_sources.nominatim import geocode as ds_nominatim_geocode
from data_sources.nominatim import reverse as ds_nominatim_reverse
from data_sources.photon import geocode as ds_photon_geocode
from data_sources.photon import reverse as ds_photon_reverse

#: ``region`` 的两个取值(API 的 ``region`` 参数、``OriginCache.region`` 列都用这一套)
REGION_DOMESTIC = "domestic"
REGION_OVERSEAS = "overseas"
REGIONS: tuple[str, ...] = (REGION_DOMESTIC, REGION_OVERSEAS)
#: ``detector`` 的取值:哪条腿答出了国家码;``none`` = 两腿都没答上(按境内处理,不落缓存)
DETECTOR_NOMINATIM = "nominatim"
DETECTOR_PHOTON = "photon"
DETECTOR_NONE = "none"
#: 境内的 ISO 3166-1 alpha-2 码(大写口径;Nominatim 给小写、Photon 给大写,统一后比)
DOMESTIC_COUNTRY_CODE = "CN"
#: 正向兜底腿向 Photon 要几条候选:判定只用最相关的第一条,不必像起点解析那样要 5 条
PHOTON_LIMIT = 1
#: 逆向兜底腿的 Nominatim zoom:10 ≈ 区县级(与 place_loader.REVERSE_ZOOM 同口径)
REVERSE_ZOOM = 10


def classify_country_code(code: Any) -> str:
    """国家码 → region:``CN``(大小写不敏感)→ ``domestic``,其他非空 → ``overseas``。

    空串 / ``None`` / 非字符串(数字、字典、垃圾值)一律 ``domestic``:**判不出就按境内**,
    既有链路(高德 + 国内分段)零影响,也绝不靠猜把用户送到境外口径上。
    """
    if not isinstance(code, str):
        return REGION_DOMESTIC
    cleaned = code.strip().upper()
    if not cleaned or cleaned == DOMESTIC_COUNTRY_CODE:
        return REGION_DOMESTIC
    return REGION_OVERSEAS


def _unknown_region() -> dict[str, Any]:
    """两腿都失败时的固定形状:按境内处理、``country_code=None``、``detector="none"``。"""
    return {
        "region": REGION_DOMESTIC,
        "country_code": None,
        "detector": DETECTOR_NONE,
    }


def _result(country_code: str, detector: str) -> dict[str, Any]:
    """有腿答出国家码时的固定形状(``country_code`` 已是大写)。"""
    return {
        "region": classify_country_code(country_code),
        "country_code": country_code,
        "detector": detector,
    }


def _code_of(place: Any, key: str) -> str:
    """从一腿的结果里取国家码并归一成大写;形状不对 / 缺失一律空串(= 该腿失败)。"""
    if not isinstance(place, dict):
        return ""
    return str(place.get(key) or "").strip().upper()


def detect_region(city: str) -> dict[str, Any]:
    """城市名 → ``{"region", "country_code", "detector"}``(正向判定,**绝不抛**)。

    链:**Nominatim 主 → Photon 兜底 → 双失败 domestic/none**(方向理由见模块 docstring:
    Photon 正向会把中文境外地名错定位到国内)。两条腿都带 ``include_country=True``
    (Nominatim 因此才带 ``addressdetails=1``、accept-language 沿用 client 默认 zh-CN),
    默认口径的输出形状零变化。
    """
    cleaned = (city or "").strip()
    if not cleaned:
        return _unknown_region()
    try:
        code = _code_of(ds_nominatim_geocode(cleaned, include_country=True), "country_code")
        if code:
            return _result(code, DETECTOR_NOMINATIM)
    except Exception:  # noqa: BLE001 - 尽力而为:主腿失败只降级,不上抛(见模块 docstring)
        pass
    try:
        places = ds_photon_geocode(cleaned, limit=PHOTON_LIMIT, include_country=True)
        first = places[0] if isinstance(places, (list, tuple)) and places else None
        code = _code_of(first, "countrycode")
        if code:
            return _result(code, DETECTOR_PHOTON)
    except Exception:  # noqa: BLE001 - 同上:兜底腿失败就按境内,绝不把起点解析打挂
        pass
    return _unknown_region()


def detect_region_coords(lat: float, lng: float) -> dict[str, Any]:
    """坐标 → ``{"region", "country_code", "detector"}``(逆向判定,**绝不抛**)。

    链:**Photon reverse 主 → Nominatim reverse 兜底 → 双失败 domestic/none**。
    坐标本身不是数字时直接按"判不出"处理(连腿都不必打)。
    """
    try:
        latitude, longitude = float(lat), float(lng)
    except (TypeError, ValueError):
        return _unknown_region()
    try:
        code = _code_of(
            ds_photon_reverse(latitude, longitude, include_country=True), "countrycode"
        )
        if code:
            return _result(code, DETECTOR_PHOTON)
    except Exception:  # noqa: BLE001 - 尽力而为:主腿失败只降级,不上抛(见模块 docstring)
        pass
    try:
        code = _code_of(
            ds_nominatim_reverse(latitude, longitude, zoom=REVERSE_ZOOM, include_country=True),
            "country_code",
        )
        if code:
            return _result(code, DETECTOR_NOMINATIM)
    except Exception:  # noqa: BLE001 - 同上:兜底腿失败就按境内,不抛
        pass
    return _unknown_region()
