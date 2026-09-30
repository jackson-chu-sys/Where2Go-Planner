"""地图模式 API:目的地检索(`/api/places`)+ 元信息 + 起点地理编码(`/api/geocode`)。

与 POC 的 `/api/discover`、`/api/categories` 并存,五条路由:

* ``GET /api/places?origin=&band=&category=`` —— 返回该 (城市, band) 内**已入库**的目的地
  (``category`` 为四分类优先级归类的结果,一地只属一类);未入库时按需抓一次 Overpass
  四分类 tag 并集落库,之后同一 (城市, band) 直接读 SQLite、不触网。
  可选 ``lat``/``lng``(前端已地理编码过就带上,省一次 Nominatim)、``refresh=true``(强制重抓)
  与 ``intros=false``(抓取后不调 LLM 补简介)。
  渐进抓取(TASK-6b):可选 ``page_size``(默认 15,1~100)/``offset``(默认 0,≥0)/
  ``more``(默认 false)—— 带任一参数即进分页模式,``places`` 只含本页并回报
  ``total_in_db``/``has_more``/``fetch_rounds``;``more=true`` 翻页越界且库内行数没到
  常规全量配额时自动再抓一轮(``target_total=30×(fetch_rounds+1)``)。三个都不带 = 旧口径返回全量。
* ``GET /api/places/meta`` —— 分段、四分类(含 pin 颜色/图标)、归类优先级、检索分组与
  LLM 简介配置的元信息(前端下拉/图例/状态栏的唯一出处,**不含任何 key**)。
* ``GET /api/places/intros?origin=&band=`` —— 给已入库但还没有简介的 POI 补 LLM 一句话简介
  (DB 即缓存,已有简介的不再调用;失败降级为空简介)。
* ``GET /api/geocode?city=`` —— 起点城市搜索(**Photon 主 + Nominatim 降级**,TASK-6a),
  并回报该城市哪些分段已入库(前端可提示"即时读库"还是"首次抓取")。
  响应里的 ``geocoder`` 标注这次是谁答的(``photon`` / ``nominatim``);两个源都失败
  才是错误 → **HTTP 400** 中文报错(消息里带上两边的失败原因)。
  TASK-7a 起这条路由带**地理编码持久缓存**(:class:`db.models.OriginCache`):实调 Photon
  每次 2.7~3.4s,而城市中心坐标基本不变,所以命中缓存(TTL 缺省 7 天)直接返回、
  **零网络**,响应形状与不走缓存时逐字段一致。
* ``GET /api/geocode/reverse?lat=&lng=`` —— 浏览器"我的位置"(TASK-1c):GPS 坐标 →
  **逆**地理编码(同样 Photon 主 + Nominatim 降级)反查城市起点。反查失败**不报错**,
  降级成坐标起点(``resolved=false``、``geocoder=none``),前端照样能画环、能查库。

``/api/places`` 的返回里带 ``seeded`` 与 ``counts_by_source``:OSM 国内滑雪/运动覆盖差,
缺口由 :mod:`services.seed_data` 的人工种子数据垫底,来源标注在每条 Place 的 ``source`` 字段。
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from data_sources import DataSourceError
from db import repository as repo
from db.base import get_session
from services import details as detail_service
from services import intro as intro_service
from services import place_loader
from services import recommend as recommend_service
from services import seed_data
from services.bands import DISTANCE_BANDS, band_keys, find_band
from services.classify import (
    CATEGORIES,
    CATEGORY_PRIORITY,
    category_keys,
    is_known_category,
    search_budget,
    search_groups,
)

router = APIRouter()

PLACES_NOTE = (
    "已入库的 (城市, band) 直接读 SQLite、不再触网;distance_km 为距起点的大圆直线距离"
    "(环形分段,不含城区)。分类为四分类优先级归类(滑雪 > 运动 > 人文美食 > 自然),"
    "按 OSM (type, id) 去重,一地只属一类;intro 为 LLM 一句话简介,按 POI 缓存。"
)
# 渐进抓取(TASK-6b,BUG-1 主修复)的分页口径:只在**带了分页参数**时追加到 note,
# 不带 page_size/offset/more 的老调用连 note 文案都保持原样。
PAGING_NOTE = (
    "渐进抓取:带 page_size/offset/more 任一参数即进分页模式,places 只含本页 —— "
    "库内该 (城市, band[, 分类]) 的行按距离升序、同距离按 (osm_type, osm_id) 决胜稳定排序后"
    "切 [offset, offset+page_size);分页模式下连冷启动首查也只抓一轮(30 配额),不再等整段全量。"
    "total_in_db = 库内总行数,fetch_rounds = 该分段已完成的抓取轮数,"
    "has_more = 库里还有下一页,或调用方带了 more=true 且该 band 还没抓到常规全量配额(还能再抓)。"
    "more=true 且翻页越界(offset ≥ 库内行数)、库内行数没到常规全量配额时自动再抓一轮"
    "(目标总量 30×(fetch_rounds+1)),扩抓按 (osm_type, osm_id) 去重、不覆盖已生成的 intro,"
    "当轮不阻塞在 LLM 简介上。三个参数都不带时行为与旧版一致:一次抓满配额、返回全量 places。"
)
# 分页默认值与边界(契约:首查 30、显示 15、加载更多每次 +30 —— 显示 15 就是这里的 page_size)
PAGE_SIZE_DEFAULT = 15
PAGE_SIZE_MIN = 1
PAGE_SIZE_MAX = 100
OFFSET_DEFAULT = 0
OFFSET_MIN = 0
# more 只认这几个写法(FastAPI 的 bool 解析口径更宽,但那样非法值会变成 422 而不是 400)
TRUE_TOKENS = frozenset({"1", "true", "t", "yes", "y", "on"})
FALSE_TOKENS = frozenset({"0", "false", "f", "no", "n", "off"})
# 地理编码持久缓存(TASK-7a):TTL 缺省 7 天,``0`` = 每次都重新问地理编码源。
ENV_ORIGIN_CACHE_TTL = "WHERE2GO_ORIGIN_CACHE_TTL_S"
ORIGIN_CACHE_TTL_DEFAULT_S = 604800
# 只缓存**真的问到了地理编码源**的结果:调用方直接给坐标 / 反查失败降级的 "none" 不写行。
ORIGIN_CACHE_GEOCODERS = frozenset(
    {place_loader.GEOCODER_PHOTON, place_loader.GEOCODER_NOMINATIM}
)
META_NOTE = (
    "分段:环形互斥,检索按 band 上限半径一次查四分类 tag 并集(每组独立配额);"
    "分类:四分类优先级归类的可选值(color/emoji 供前端 pin 使用);"
    "简介:LLM 生成后缓存在 Place.intro,已有简介不再调用,失败降级为空。"
)
INTROS_NOTE = (
    "只给 intro 为空的 POI 调 LLM(DB 即缓存);网络/额度失败降级为空简介,下次可重试。"
)
REVERSE_NOTE = (
    "浏览器定位(GPS 坐标)→ 逆地理编码反查城市起点(Photon 主 + Nominatim 降级,"
    "geocoder 字段标注这次是谁答的,none = 两个源都没答上);"
    "范围圈始终以传入的 GPS 坐标为圆心,不用行政区中心。"
    "反查失败/限流时 resolved=false,起点名降级为『我的位置(纬度,经度)』——"
    "仍是 HTTP 200,前端照常画环查库,不报错。"
)
RECOMMEND_NOTE = (
    "AI 推荐只读**已入库**的目的地(不触网抓取):按当前 (城市, band, 分类) 的候选"
    "让 LLM 挑 3~5 个最值得去的,附推荐理由;结果按候选指纹落库缓存,同分段重复查询不重复调 LLM。"
    "LLM 未配置 key 或调用失败时降级为『按距离取前 N 条』(degraded=true、basis=distance),不报错。"
    "推荐依据 = LLM 知识 + 候选的真实名称/分类/距离/OSM 标签(basis=llm+osm_tags),不额外联网抓资料。"
)
DETAILS_NOTE = (
    "长介绍(列表用 2~3 句)按 POI 缓存:只给**还没有**长介绍的 POI 调 LLM,"
    "单次最多 limit 条(前端分批懒加载);失败降级为不写行、下次可重试,"
    "reason 区分 no_key / all_failed / ok,便于前端给出可操作提示。"
)


def _clean(text: Optional[str]) -> Optional[str]:
    value = (text or "").strip()
    return value or None


def origin_cache_ttl_s() -> int:
    """地理编码缓存的 TTL(秒):``WHERE2GO_ORIGIN_CACHE_TTL_S``,缺省 7 天。

    非法值(空串、非整数)回落缺省值、负数按 0 处理;``0`` = 缓存永不当命中
    (每次都走网络),与住宿负缓存的 TTL 口径一致(见 :data:`services.stays.NEG_CACHE_TTL_S`)。
    """
    raw = (os.environ.get(ENV_ORIGIN_CACHE_TTL) or "").strip()
    try:
        value = int(raw) if raw else ORIGIN_CACHE_TTL_DEFAULT_S
    except ValueError:
        value = ORIGIN_CACHE_TTL_DEFAULT_S
    return max(0, value)


def origin_cache_age_s(row: Any) -> Optional[float]:
    """缓存行的年龄(秒);没有 ``updated_at`` → ``None``(视为不可用,不当命中)。"""
    moment = getattr(row, "updated_at", None)
    if moment is None:
        return None
    if moment.tzinfo is None:  # SQLite 读回的是 naive 时间,按 UTC 处理
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds()


def resolve_intro_limit(limit: Optional[int]) -> Optional[int]:
    """本次抓取最多补多少条简介:留空用默认批量,``0`` = 不限(冷启动全量)。"""
    if limit is None:
        return place_loader.INTRO_BATCH_LIMIT
    return None if int(limit) <= 0 else int(limit)


def _page_int(
    raw: Any,
    *,
    label: str,
    default: int,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """分页整数参数解析:留空/空串用默认,非整数或越界一律 **400 中文**。

    参数在路由上按 ``Optional[str]`` 收(不套 ``Query(ge=..., le=...)``),就是为了把
    "非法值"从 pydantic 的 422 变成本仓库统一的 400 中文报错;单测直接调路由函数时
    传 ``int`` 也照样认。
    """
    if raw is None or isinstance(raw, str) and not raw.strip():
        return default
    if isinstance(raw, bool):
        raise HTTPException(400, f"{label} 必须是整数,收到:{raw!r}")
    if isinstance(raw, int):
        value = int(raw)
    else:
        text = str(raw).strip()
        try:
            value = int(text)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"{label} 必须是整数,收到:{text!r}") from exc
    if minimum is not None and maximum is not None and not minimum <= value <= maximum:
        raise HTTPException(400, f"{label} 超出范围:应在 {minimum}~{maximum} 之间,收到:{value}")
    if minimum is not None and maximum is None and value < minimum:
        raise HTTPException(400, f"{label} 不能小于 {minimum},收到:{value}")
    if maximum is not None and minimum is None and value > maximum:
        raise HTTPException(400, f"{label} 不能大于 {maximum},收到:{value}")
    return value


def _page_bool(raw: Any, *, label: str, default: bool = False) -> bool:
    """分页布尔参数解析(``true/false``、``1/0``、``yes/no``、``on/off``);其余 **400 中文**。"""
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip()
    if not text:
        return default
    lowered = text.lower()
    if lowered in TRUE_TOKENS:
        return True
    if lowered in FALSE_TOKENS:
        return False
    raise HTTPException(400, f"{label} 只能是 true/false,收到:{text!r}")


def _page_sort_key(row: Mapping[str, Any]) -> tuple[float, str, int]:
    """分页排序键:距离升序 →(osm_type, osm_id)决胜;没有距离的排最后。"""
    distance = row.get("distance_km")
    return (
        float("inf") if distance is None else float(distance),
        str(row.get("osm_type") or ""),
        int(row.get("osm_id") or 0),
    )


def _page_rows(places: list[dict[str, Any]], paging: bool) -> list[dict[str, Any]]:
    """分页模式换成**稳定排序**后的行;非分页模式原样返回(旧口径:距离 + 名字决胜)。

    :func:`db.repository.list_places` 用名字决胜,同名不同 OSM id 的两行在翻页时会在
    两页之间漂移;分页改用唯一键 ``(osm_type, osm_id)`` 决胜,``[offset, offset+page_size)``
    才是可重复的稳定切片。
    """
    if not paging:
        return list(places)
    return sorted(places, key=_page_sort_key)


def _fetch_rounds(outcome: place_loader.SegmentOutcome) -> int:
    """该 (城市, band) 已完成的抓取轮数(水位缺失/旧库缺列时按 0)。"""
    return int((outcome.segment or {}).get("fetch_rounds") or 0)


@router.get("/places/meta")
def places_meta() -> dict[str, Any]:
    """分段 + 四分类 + 归类优先级 + 检索分组 + LLM 配置 + 种子概览(前端图例/状态栏的唯一出处)。"""
    return {
        "bands": [dict(band) for band in DISTANCE_BANDS],
        "categories": [dict(item) for item in CATEGORIES],
        "category_priority": list(CATEGORY_PRIORITY),
        "search_tags": place_loader.SEARCH_TAGS,
        "search_groups": [
            {"group": group["group"], "category": group["category"], "budget": group["budget"],
             "selectors": len(group["tags"])}
            for group in search_groups()
        ],
        "search_budget": search_budget(),
        "fetch_limit": place_loader.FETCH_LIMIT,
        "llm": intro_service.describe_llm(),
        "seeds": seed_data.seed_stats(),
        "note": META_NOTE,
    }


@router.get("/places/intros")
def fill_intros(
    origin: str = Query(..., min_length=1, description="起点城市名,如:上海"),
    band: Optional[str] = Query(None, description="距离分段 key,留空 = 该城市全部分段"),
    category: Optional[str] = Query(None, description="只补某个分类"),
    limit: Optional[int] = Query(None, ge=0, description="最多补多少条(0/留空 = 不限)"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """给已入库但缺简介的 POI 补 LLM 一句话简介(已有简介的不重复调用)。"""
    city = _clean(origin)
    if not city:
        raise HTTPException(400, "起点城市不能为空")
    wanted_band = _clean(band)
    if wanted_band and not find_band(wanted_band):
        raise HTTPException(400, f"未知距离分段:{band}(可选:{'、'.join(band_keys())})")
    wanted_category = _clean(category)
    if wanted_category and not is_known_category(wanted_category):
        raise HTTPException(400, f"未知分类:{category}(可选:{'、'.join(category_keys())})")

    started = time.monotonic()
    stats = intro_service.fill_missing_intros(
        session,
        origin_city=city,
        band=wanted_band,
        category=wanted_category,
        limit=(limit or None),
    )
    return {
        "origin_city": city,
        "band": wanted_band,
        "category": wanted_category,
        **stats,
        "elapsed_s": round(time.monotonic() - started, 2),
        "note": INTROS_NOTE,
    }


@router.get("/places")
def list_places(
    origin: str = Query(..., min_length=1, description="起点城市名,如:上海"),
    band: str = Query(..., description="距离分段 key:50_100 / 100_200 / 200_300 / 300_500"),
    category: Optional[str] = Query(None, description="分类过滤,留空返回该段全部"),
    lat: Optional[float] = Query(None, ge=-90, le=90, description="起点纬度(可选,免二次地理编码)"),
    lng: Optional[float] = Query(None, ge=-180, le=180, description="起点经度(可选)"),
    refresh: bool = Query(False, description="true = 强制重新抓取(会触网)"),
    # 渐进抓取(TASK-6b)三参:按**字符串**收 + 自己解析,非法值才能报本仓库统一的
    # 400 中文而不是 pydantic 的 422;默认值写成裸 None(不套 Query),单测直接调本函数
    # 不传这几个参数时拿到的就是 None,而不是 FieldInfo。
    page_size: Optional[str] = None,
    offset: Optional[str] = None,
    more: Optional[str] = None,
    intros: bool = True,
    intro_limit: Optional[int] = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """返回该 (城市, band) 内已入库的目的地(可按分类过滤;支持分页 + 渐进扩抓)。

    分页(TASK-6b,BUG-1 主修复):``page_size`` / ``offset`` / ``more`` **一个都不带**时
    完全走旧口径 —— 返回全量 ``places``;带了任意一个就进分页模式(缺省
    ``page_size=15`` / ``offset=0`` / ``more=false``),``places`` 只含
    ``[offset, offset+page_size)`` 这一页,并额外回报 ``total_in_db`` / ``has_more`` /
    ``fetch_rounds``。``more=true`` 且这一页越界(``offset >= 库内行数``)、库内行数还没到
    常规全量配额时,自动再抓一轮(``target_total = 30 × (fetch_rounds + 1)``)后重新排序切片。
    """
    city = _clean(origin)
    if not city:
        raise HTTPException(400, "起点城市不能为空")
    wanted_category = _clean(category)
    if wanted_category and not is_known_category(wanted_category):
        raise HTTPException(
            400, f"未知分类:{category}(可选:{'、'.join(category_keys())})"
        )
    # 分页参数**先**校验:非法就快速失败,一次网都不触(与城市/分类校验同一口径)。
    paging = page_size is not None or offset is not None or more is not None
    wanted_page_size = _page_int(
        page_size,
        label="page_size",
        default=PAGE_SIZE_DEFAULT,
        minimum=PAGE_SIZE_MIN,
        maximum=PAGE_SIZE_MAX,
    )
    wanted_offset = _page_int(offset, label="offset", default=OFFSET_DEFAULT, minimum=OFFSET_MIN)
    want_more = _page_bool(more, label="more")

    started = time.monotonic()
    # 分页模式下连**冷启动首查**也只抓一小轮(30 配额),否则首屏照样是分钟级(BUG-1 本体);
    # 三个参数都不带的老调用仍然一次抓满常规配额,行为与旧版完全一致。
    progressive_target: Optional[int] = None
    if paging:
        stored = repo.get_segment(session, origin_city=city, band=_clean(band) or "")
        progressive_target = place_loader.progressive_target_total(
            0 if stored is None else stored.fetch_rounds
        )

    def load(*, forced: bool, target_total: Optional[int], fill_intros: bool):
        """首查与扩抓共用同一套 load_segment 参数(只有 refresh/target_total/intros 不同)。"""
        return place_loader.load_segment(
            session,
            city=city,
            band=_clean(band) or "",
            category=wanted_category,
            lat=lat,
            lng=lng,
            refresh=forced,
            target_total=target_total,
            intros=fill_intros,
            intro_limit=resolve_intro_limit(intro_limit),
        )

    try:
        outcome = load(forced=refresh, target_total=progressive_target, fill_intros=intros)
        rows = _page_rows(outcome.places, paging)
        rounds = _fetch_rounds(outcome)
        intro_stats = outcome.intro_stats
        # 翻页越界 + 库内还没到常规全量配额 → 再抓一轮(30 ×(轮数 + 1))后重新切片。
        # 扩抓轮**不**补简介(intros=False):首屏要快,新行的简介交给 /api/places/intros;
        # upsert 只按 (osm_type, osm_id, origin_city) 去重,不会覆盖已生成的 intro。
        if (
            paging
            and want_more
            and wanted_offset >= len(rows)
            and repo.count_places(session, origin_city=city, band=outcome.band["key"])
            < search_budget()
        ):
            outcome = load(
                forced=True,
                target_total=place_loader.progressive_target_total(rounds),
                fill_intros=False,
            )
            rows = _page_rows(outcome.places, paging)
            rounds = _fetch_rounds(outcome)
            intro_stats = outcome.intro_stats or intro_stats
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except DataSourceError as exc:
        raise HTTPException(502, f"目的地检索失败(公共数据源繁忙,请稍后重试):{exc}") from exc

    band_def = outcome.band
    # 只有分页模式需要"这个 band 还能不能再抓"(has_more 的后半截),老路径不多查这一次。
    band_rows = repo.count_places(session, origin_city=city, band=band_def["key"]) if paging else 0
    page_rows = rows[wanted_offset:wanted_offset + wanted_page_size] if paging else rows
    total_in_db = len(rows)
    return {
        "origin": outcome.origin,
        "band": {
            "key": band_def["key"],
            "label": band_def["label"],
            "low_km": band_def["low"],
            "high_km": band_def["high"],
        },
        "category": wanted_category,
        "places": page_rows,
        "count": len(page_rows),
        "total_in_db": total_in_db,
        # has_more = 后面还有**可显示**的行:库里还有下一页,或者调用方愿意扩抓(more=true)
        # 且这个 band 还没抓到常规全量配额。少了后半截,"显示 15 / 每次 +30"的循环会在
        # 第一轮 30 条就走完(第 2 页 has_more=false → 前端再也不点加载更多)。
        "has_more": bool(
            paging
            and (
                wanted_offset + wanted_page_size < total_in_db
                or (want_more and band_rows < search_budget())
            )
        ),
        "fetch_rounds": rounds,
        "counts_by_category": outcome.counts_by_category,
        "source": outcome.source,
        "network_used": outcome.network_used,
        "fetched_at": outcome.fetched_at,
        "written": outcome.written,
        "seeded": outcome.seeded,
        "counts_by_source": outcome.counts_by_source,
        "intro_stats": intro_stats,
        "intro_pending": repo.count_places(
            session, origin_city=city, band=band_def["key"], missing_intro=True
        ),
        "detail_pending": repo.count_places_missing_detail(
            session, origin_city=city, band=band_def["key"], category=wanted_category
        ),
        "elapsed_s": round(time.monotonic() - started, 2),
        "note": (PLACES_NOTE + PAGING_NOTE) if paging else PLACES_NOTE,
    }


def _require_segment(session: Session, city: str, band: str, category: Optional[str]) -> dict[str, Any]:
    """校验 band/category 并返回分段定义(推荐/长介绍两个只读端点共用)。"""
    band_def = find_band(band)
    if band_def is None:
        raise HTTPException(400, f"未知距离分段:{band}(可选:{'、'.join(band_keys())})")
    if category and not is_known_category(category):
        raise HTTPException(400, f"未知分类:{category}(可选:{'、'.join(category_keys())})")
    return band_def


def _segment_origin(session: Session, city: str, band_key: str) -> dict[str, Any]:
    """取该 (城市, band) 入库时记下的起点(库里没有水位时坐标为空,距离一律 None)。"""
    record = repo.get_segment(session, origin_city=city, band=band_key)
    if record is None:
        return {"lat": None, "lng": None, "name": city}
    return {"lat": record.origin_lat, "lng": record.origin_lng, "name": record.origin_name or city}


@router.get("/places/recommend")
def recommend_places(
    origin: str = Query(..., min_length=1, description="起点城市名,如:上海"),
    band: str = Query(..., description="距离分段 key:0_50 / 50_100 / ..."),
    category: Optional[str] = Query(None, description="分类过滤,留空 = 该段全部"),
    count: Optional[int] = Query(None, ge=0, description="推荐条数(默认 5,收敛到 3~5)"),
    refresh: bool = Query(False, description="true = 忽略推荐缓存重新调 LLM"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """AI 推荐当前分段最值得去的 3~5 个目的地(只读库,不触网抓取;结果按候选指纹缓存)。"""
    city = _clean(origin)
    if not city:
        raise HTTPException(400, "起点城市不能为空")
    wanted_category = _clean(category)
    band_def = _require_segment(session, city, band, wanted_category)

    started = time.monotonic()
    seed_origin = _segment_origin(session, city, band_def["key"])
    places = repo.list_places(
        session,
        origin_city=city,
        band=band_def["key"],
        category=wanted_category,
        origin_lat=seed_origin["lat"],
        origin_lng=seed_origin["lng"],
    )
    outcome = recommend_service.recommend_places(
        session,
        origin_city=city,
        band=band_def["key"],
        category=wanted_category,
        places=places,
        count=(recommend_service.DEFAULT_COUNT if count is None else count),
        origin_name=seed_origin["name"],
        band_label=band_def["label"],
        refresh=refresh,
    )
    detail_texts = repo.detail_map(session, [item["id"] for item in outcome["items"]])
    for item in outcome["items"]:
        item["detail"] = detail_texts.get(int(item["id"]))
    return {
        "origin_city": city,
        "band": {"key": band_def["key"], "label": band_def["label"],
                 "low_km": band_def["low"], "high_km": band_def["high"]},
        "category": wanted_category,
        "count": len(outcome["items"]),
        **{key: value for key, value in outcome.items() if key != "items"},
        "items": outcome["items"],
        "detail_pending": repo.count_places_missing_detail(
            session, origin_city=city, band=band_def["key"], category=wanted_category
        ),
        "elapsed_s": round(time.monotonic() - started, 2),
        "note": RECOMMEND_NOTE,
    }


@router.get("/places/details")
def fill_details(
    origin: str = Query(..., min_length=1, description="起点城市名,如:上海"),
    band: str = Query(..., description="距离分段 key"),
    category: Optional[str] = Query(None, description="分类过滤,留空 = 该段全部"),
    limit: Optional[int] = Query(None, ge=0, description=f"本次最多生成几条(默认 {detail_service.DEFAULT_BATCH},0/留空 = 默认)"),
    place_ids: Optional[str] = Query(None, description="只给这些 id 生成(逗号分隔,前端「可见条优先」用)"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """给还没有长介绍的目的地补 2~3 句重点介绍(按 POI 缓存;前端分批懒加载)。"""
    city = _clean(origin)
    if not city:
        raise HTTPException(400, "起点城市不能为空")
    wanted_category = _clean(category)
    band_def = _require_segment(session, city, band, wanted_category)

    wanted_ids = [
        int(chunk) for chunk in (place_ids or "").replace("，", ",").split(",")
        if chunk.strip().lstrip("-").isdigit()
    ]
    batch = detail_service.DEFAULT_BATCH if not limit else min(int(limit), detail_service.MAX_BATCH)
    started = time.monotonic()
    stats = detail_service.fill_missing_details(
        session,
        origin_city=city,
        band=band_def["key"],
        category=wanted_category,
        limit=batch,
        only_ids=wanted_ids or None,
    )
    texts = repo.detail_map(session, stats.get("filled_ids") or wanted_ids)
    return {
        "origin_city": city,
        "band": band_def["key"],
        "category": wanted_category,
        "items": [{"place_id": int(pid), "text": text} for pid, text in sorted(texts.items())],
        **{key: value for key, value in stats.items() if key != "filled_ids"},
        "elapsed_s": round(time.monotonic() - started, 2),
        "note": DETAILS_NOTE,
    }


@router.get("/geocode")
def geocode_city(
    city: str = Query(..., min_length=1, description="城市名,如:北京"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """起点城市搜索(Photon 主 + Nominatim 降级),并回报该城市已入库的分段。

    两个源都失败(Photon 挂/空结果 **且** Nominatim 也挂)才是错误:按本仓库路由口径
    抛 **HTTP 400** 中文报错,消息里同时带上两边的失败原因,便于判断是断网还是单源故障。

    地理编码结果按城市名落 :class:`db.models.OriginCache`(TASK-7a):TTL
    (``WHERE2GO_ORIGIN_CACHE_TTL_S``,缺省 7 天)内命中就直接拼响应、**零网络** ——
    Photon 在德国,实调一次 2.7~3.4s,同一城市反复搜索没必要反复付费。命中与否
    响应形状完全一致(``origin`` 四字段 + ``geocoder`` 原值 + ``bands`` + ``segments``);
    未命中/过期才走 :func:`place_loader.resolve_origin_with_source`,并且只在
    ``geocoder`` ∈ {photon, nominatim} 时写缓存(给了坐标的 ``none`` 退化路径不写)。
    """
    cleaned = _clean(city)
    if not cleaned:
        raise HTTPException(400, "城市名不能为空")
    ttl = origin_cache_ttl_s()
    cached = repo.get_origin_cache(session, city=cleaned) if ttl > 0 else None
    age = origin_cache_age_s(cached) if cached is not None else None
    if cached is not None and age is not None and age <= ttl:
        origin = {
            "city": cleaned,
            "name": cached.name or cleaned,
            "lat": cached.lat,
            "lng": cached.lng,
        }
        geocoder = cached.geocoder or place_loader.GEOCODER_NONE
    else:
        try:
            origin, geocoder = place_loader.resolve_origin_with_source(cleaned)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except DataSourceError as exc:
            raise HTTPException(400, f"无法解析城市 '{cleaned}':{exc}") from exc
        if geocoder in ORIGIN_CACHE_GEOCODERS:
            repo.upsert_origin_cache(
                session,
                city=cleaned,
                name=origin.get("name") or cleaned,
                lat=origin["lat"],
                lng=origin["lng"],
                geocoder=geocoder,
            )
            session.commit()
    return {
        "origin": origin,
        "geocoder": geocoder,
        "bands": [dict(band) for band in DISTANCE_BANDS],
        "segments": repo.segment_overview(session, origin_city=cleaned),
    }


@router.get("/geocode/reverse")
def reverse_geocode(
    lat: float = Query(..., ge=-90, le=90, description="纬度(浏览器 GPS)"),
    lng: float = Query(..., ge=-180, le=180, description="经度(浏览器 GPS)"),
    zoom: Optional[int] = Query(
        None, ge=1, le=18, description="Nominatim zoom(仅降级到 Nominatim 时生效),留空 = 区县级"
    ),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """浏览器"我的位置" → 起点城市(Photon 主 + Nominatim 降级逆地理编码),**失败也返回 200**。

    与 ``/api/geocode`` 的区别:坐标已知,只需反查名字;``origin`` 里的 ``lat``/``lng``
    一律沿用传入的 GPS 坐标,范围圈要以用户真实位置为圆心。``resolved=false`` 时
    起点名降级为 ``我的位置(31.23,121.47)``,前端给个提示即可,不必当错误处理
    (所以这里**双失败也不报 400**,只把 ``geocoder`` 标成 ``none``)。
    """
    origin, geocoder = place_loader.resolve_reverse_origin_with_source(
        lat, lng, zoom=(place_loader.REVERSE_ZOOM if zoom is None else int(zoom))
    )
    return {
        "origin": origin,
        "resolved": bool(origin.get("resolved")),
        "geocoder": geocoder,
        "bands": [dict(band) for band in DISTANCE_BANDS],
        "segments": repo.segment_overview(session, origin_city=origin["city"]),
        "note": REVERSE_NOTE,
    }
