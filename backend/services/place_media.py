"""目的地图片链路(TASK-8a1):高德 POI 图为主 + 维基/Commons 兜底 + DB 缓存。

一条链路三个环节(契约 ``docs/TASK-8-CONTRACT.md`` §0/§3):

1. **读库优先**::class:`db.models.PlaceMedia` 有未过期的行就直接回、**零网络**
   (命中 TTL ``WHERE2GO_PLACE_MEDIA_TTL_S`` 缺省 7 天;空结果/失败只缓存
   ``WHERE2GO_PLACE_MEDIA_MISS_TTL_S`` 缺省 6 小时 —— **负缓存**,别把一次空结果
   永久钉死,同 TASK-6c 的教训)。
2. **高德**:``/place/text`` + ``extensions=all`` 取 ``pois[0].photos``
   (:func:`data_sources.amap.search_poi_photos`,带**坐标门控**,同名异地误配直接当没查到)。
3. **维基兜底**::func:`data_sources.wikimedia.wikipedia_media`(geosearch 近邻实景页优先、
   名称直搜兜底、Commons 相册补图)。两边都有图 → ``source="mixed"``;只有高德 →
   ``"amap"``;只有维基 → ``"wikimedia"``;都没图 → ``"none"`` + ``reason``。

纪律:**只存真的从数据源拿到的 url,没图就是空数组**(绝不编造图源);缺 key / 报错 /
没数据三档 ``reason`` 透传给前端分文案(「配置高德 key 后可用」/「可重试」/「暂无图片」)。
单 POI 图片上限 ``WHERE2GO_PLACE_MEDIA_MAX_IMAGES`` 缺省 6 张(高德图在前、维基图补足)。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from data_sources import amap, wikimedia
from data_sources._common import DataSourceError
from db import models
from db import repository as repo
from db.base import session_factory

# 命中缓存的 TTL(秒):缺省 7 天(契约 §0「命中缓存 7 天」)
ENV_MEDIA_TTL_S = "WHERE2GO_PLACE_MEDIA_TTL_S"
MEDIA_TTL_DEFAULT_S = 604800
# 空结果/失败的**负缓存** TTL(秒):缺省 6 小时(别把一次空结果永久钉死)
ENV_MEDIA_MISS_TTL_S = "WHERE2GO_PLACE_MEDIA_MISS_TTL_S"
MEDIA_MISS_TTL_DEFAULT_S = 21600
# 单 POI 图片上限(张)
ENV_MEDIA_MAX_IMAGES = "WHERE2GO_PLACE_MEDIA_MAX_IMAGES"
MEDIA_MAX_IMAGES_DEFAULT = 6
MEDIA_MAX_IMAGES_LIMIT = 20

#: 高德 ``/place/text`` 的检索半径(米,契约 §1 实测值);维基 Commons 每次补几张
AMAP_RADIUS_M = 20000
WIKI_IMAGE_LIMIT = 4
IMAGE_KEYS: tuple[str, ...] = ("url", "title", "source")


# --------------------------------------------------------------------------- #
# 口径解析(env 可覆盖,非法值一律回落默认,与 places.origin_cache_ttl_s 同风格)
# --------------------------------------------------------------------------- #


def _env_int(environ: Mapping[str, str], name: str, default: int, *, maximum: Optional[int] = None) -> int:
    """读一个非负整数环境变量:空/非法回落 ``default``,负数按 0,给了上限就钳住。"""
    raw = str(environ.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    value = max(0, value)
    return value if maximum is None else min(value, maximum)


def media_ttl_s(environ: Optional[Mapping[str, str]] = None) -> int:
    """命中缓存的 TTL(秒):``WHERE2GO_PLACE_MEDIA_TTL_S``,缺省 7 天(``0`` = 永不当命中)。"""
    env = os.environ if environ is None else environ
    return _env_int(env, ENV_MEDIA_TTL_S, MEDIA_TTL_DEFAULT_S)


def media_miss_ttl_s(environ: Optional[Mapping[str, str]] = None) -> int:
    """负缓存的 TTL(秒):``WHERE2GO_PLACE_MEDIA_MISS_TTL_S``,缺省 6 小时。"""
    env = os.environ if environ is None else environ
    return _env_int(env, ENV_MEDIA_MISS_TTL_S, MEDIA_MISS_TTL_DEFAULT_S)


def max_images(environ: Optional[Mapping[str, str]] = None) -> int:
    """单 POI 图片上限:``WHERE2GO_PLACE_MEDIA_MAX_IMAGES``,缺省 6、最多 20。"""
    env = os.environ if environ is None else environ
    value = _env_int(env, ENV_MEDIA_MAX_IMAGES, MEDIA_MAX_IMAGES_DEFAULT, maximum=MEDIA_MAX_IMAGES_LIMIT)
    return value


def cache_age_s(row: Any) -> Optional[float]:
    """缓存行的年龄(秒);没有 ``fetched_at`` → ``None``(视为不可用,不当命中)。"""
    moment = getattr(row, "fetched_at", None)
    if moment is None:
        return None
    if moment.tzinfo is None:  # SQLite 读回的是 naive 时间,按 UTC 处理
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds()


def cache_is_fresh(row: Any, environ: Optional[Mapping[str, str]] = None) -> bool:
    """这行缓存还算不算数:**有图**看 7 天 TTL、**空结果/失败**只看 6 小时负缓存 TTL。"""
    if row is None:
        return False
    age = cache_age_s(row)
    if age is None:
        return False
    has_images = bool(getattr(row, "images", None))
    ttl = media_ttl_s(environ) if has_images else media_miss_ttl_s(environ)
    return age <= ttl


# --------------------------------------------------------------------------- #
# 图片归一 / 合并
# --------------------------------------------------------------------------- #


def image_entry(raw: Any, *, source: str) -> Optional[dict[str, str]]:
    """一条图源记录 → ``{"url", "title", "source"}``;没有合法 url 的丢掉(**不编造图源**)。"""
    if not isinstance(raw, Mapping):
        return None
    url = str(raw.get("url") or "").strip()
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    if not url.startswith("https://") or len(url) <= len("https://"):
        return None
    return {"url": url, "title": str(raw.get("title") or "").strip(), "source": source}


def merge_images(
    amap_images: Optional[Iterable[Any]],
    wiki_images: Optional[Iterable[Any]],
    *,
    limit: int = MEDIA_MAX_IMAGES_DEFAULT,
) -> list[dict[str, str]]:
    """两个源的图片合并:**高德在前、维基补足**,按 url 去重,最多 ``limit`` 张。"""
    wanted = max(0, int(limit))
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for source, group in (
        (models.MEDIA_SOURCE_AMAP, amap_images),
        (models.MEDIA_SOURCE_WIKIMEDIA, wiki_images),
    ):
        for raw in group or []:
            entry = image_entry(raw, source=source)
            if entry is None or entry["url"] in seen:
                continue
            seen.add(entry["url"])
            merged.append(entry)
            if len(merged) >= wanted:
                return merged
    return merged


def resolve_source(images: Iterable[Mapping[str, Any]]) -> str:
    """按**最终入库的图片**判图源四态:两边都有 → ``mixed``,单边 → 该源,空 → ``none``。"""
    sources = {str(item.get("source") or "") for item in images or []}
    has_amap = models.MEDIA_SOURCE_AMAP in sources
    has_wiki = models.MEDIA_SOURCE_WIKIMEDIA in sources
    if has_amap and has_wiki:
        return models.MEDIA_SOURCE_MIXED
    if has_amap:
        return models.MEDIA_SOURCE_AMAP
    if has_wiki:
        return models.MEDIA_SOURCE_WIKIMEDIA
    return models.MEDIA_SOURCE_NONE


def empty_item(place_id: Any, *, reason: str = models.MEDIA_REASON_NO_DATA, cached: bool = False) -> dict[str, Any]:
    """没落库(未知 id / 单条异常)时的固定形状,键与 :func:`repo.media_to_dict` 一致。"""
    return {
        "place_id": int(place_id),
        "source": models.MEDIA_SOURCE_NONE,
        "images": [],
        "page_url": None,
        "reason": models.media_reason(reason),
        "cached": cached,
        "fetched_at": None,
    }


# --------------------------------------------------------------------------- #
# place 入参(ORM 行或 dict 都吃)
# --------------------------------------------------------------------------- #


def _field(place: Any, key: str) -> Any:
    """从 ``Place`` ORM 行或 dict 里取一个字段(契约签名收 ``Mapping``,ORM 行也兼容)。"""
    if isinstance(place, Mapping):
        return place.get(key)
    return getattr(place, key, None)


def place_id_of(place: Any) -> Optional[int]:
    """``place`` 的主键;拿不到正整数 → ``None``。"""
    raw = _field(place, "id")
    if raw is None:
        raw = _field(place, "place_id")
    try:
        resolved = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return resolved if resolved > 0 else None


def place_coords(place: Any) -> Optional[tuple[float, float]]:
    """``(lat, lng)``;缺失/非数字/越界 → ``None``(**不抛**,调用方按"查不了"降级)。"""
    return wikimedia.clean_coords(_field(place, "lat"), _field(place, "lng"))


# --------------------------------------------------------------------------- #
# 触网:高德 → 维基
# --------------------------------------------------------------------------- #


def _amap_images(name: str, lat: float, lng: float, env: Mapping[str, str]) -> tuple[list[dict[str, str]], Optional[str]]:
    """问高德要 POI 图;返回 ``(图片, reason)``。业务失败 = 空图,reason 记缺 key / 报错。"""
    try:
        images = amap.search_poi_photos(name, lat, lng, radius_m=AMAP_RADIUS_M, environ=env)
    except DataSourceError as exc:
        # 缺 key 是"配置问题"(配好即可用),与"网络/配额报错"分开给前端文案
        reason = (
            models.MEDIA_REASON_NO_KEY
            if amap.MISSING_KEY_MESSAGE in str(exc)
            else models.MEDIA_REASON_ERROR
        )
        return [], reason
    except Exception:  # noqa: BLE001 - 坐标/名称脏数据等:图片是装饰,不该炸详情弹窗
        return [], models.MEDIA_REASON_ERROR
    return [item for item in (images or []) if isinstance(item, Mapping)], None


def _wiki_media(name: str, lat: float, lng: float, env: Mapping[str, str]) -> dict[str, Any]:
    """问维基要图与页面链接;它自己承诺不抛,这里再兜一层(替身/意外异常也不炸)。"""
    try:
        result = wikimedia.wikipedia_media(
            name, lat, lng, limit=WIKI_IMAGE_LIMIT, environ=env
        )
    except Exception:  # noqa: BLE001
        return {}
    return result if isinstance(result, Mapping) else {}


def fetch_remote(place: Any, environ: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """现场问两个源,拼出 :func:`repo.upsert_place_media` 要的那四个字段(**不落库**)。"""
    env = dict(os.environ if environ is None else environ)
    name = models.clean_text(_field(place, "name")) or ""
    coords = place_coords(place)
    reason: Optional[str] = None
    amap_images: list[Any] = []
    wiki_images: list[Any] = []
    page_url: Optional[str] = None

    if not name or coords is None:
        # 名称/坐标都没有 → 连问都没法问(库里脏行),记 no_data 并写负缓存
        reason = models.MEDIA_REASON_NO_DATA
    else:
        lat, lng = coords
        amap_images, reason = _amap_images(name, lat, lng, env)
        wiki = _wiki_media(name, lat, lng, env)
        wiki_images = [item for item in (wiki.get("images") or []) if isinstance(item, Mapping)]
        page_url = models.clean_text(wiki.get("page_url"), limit=models.MEDIA_PAGE_URL_LEN)

    images = merge_images(amap_images, wiki_images, limit=max_images(env))
    if images:
        reason = None
    elif reason is None:
        reason = models.MEDIA_REASON_NO_DATA
    return {
        "source": resolve_source(images),
        "images": images,
        "page_url": page_url,
        "reason": reason,
    }


# --------------------------------------------------------------------------- #
# 对外主入口
# --------------------------------------------------------------------------- #


def fetch_media_for_place(
    place: Any,
    *,
    environ: Optional[Mapping[str, str]] = None,
    session: Optional[Session] = None,
) -> dict[str, Any]:
    """一个 POI 的图片:库内未过期缓存 → 高德 → 维基兜底,返回固定形状的 dict。

    → ``{"place_id", "source", "images", "page_url", "reason", "cached", "fetched_at"}``
    (``fetched_at`` 为 ISO UTC;``cached=True`` 表示这次**零网络**读了库)。

    ``session`` 不给就自己开一个短会话并 ``commit``;给了就只 ``flush``,提交时机交给
    调用方(与 :func:`db.repository.upsert_place_media` 一致)。
    """
    env = dict(os.environ if environ is None else environ)
    place_id = place_id_of(place)
    if place_id is None:
        raise ValueError(f"place 必须带正整数 id(Place 行或含 id 的 dict),收到:{place!r}")

    owned = session is None
    current = session if session is not None else session_factory()()
    try:
        row = repo.get_place_media(current, place_id=place_id)
        if cache_is_fresh(row, env):
            return {**repo.media_to_dict(row), "cached": True}
        outcome = fetch_remote(place, env)
        row = repo.upsert_place_media(current, place_id=place_id, **outcome)
        if owned:
            current.commit()
        return {**repo.media_to_dict(row), "cached": False}
    finally:
        if owned:
            current.close()


def fetch_media_for_places(
    session: Session,
    place_ids: Iterable[Any],
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> list[dict[str, Any]]:
    """批量取图片(**按入参顺序**返回,重复 id 只回一条;读库优先,仅 miss 才触网)。

    单条失败**不扩散**:该条 ``source="none"`` + ``reason="error"``,其余照常。
    每条成功就 ``commit`` 一次 —— 第 N 条炸了要 ``rollback`` 才能继续用这个会话,
    不先提交会把前面已经花过网络配额抓到的图一起丢掉。
    """
    env = dict(os.environ if environ is None else environ)
    wanted: list[int] = []
    for raw in place_ids or []:
        try:
            resolved = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if resolved > 0 and resolved not in wanted:
            wanted.append(resolved)
    if not wanted:
        return []

    rows = {
        int(row.id): row
        for row in session.scalars(select(models.Place).where(models.Place.id.in_(wanted)))
    }
    items: list[dict[str, Any]] = []
    for place_id in wanted:
        place = rows.get(place_id)
        if place is None:
            items.append(empty_item(place_id, reason=models.MEDIA_REASON_NO_DATA))
            continue
        try:
            items.append(fetch_media_for_place(place, environ=env, session=session))
            session.commit()
        except Exception:  # noqa: BLE001 - 单条失败只影响这一条
            session.rollback()
            items.append(empty_item(place_id, reason=models.MEDIA_REASON_ERROR))
    return items
