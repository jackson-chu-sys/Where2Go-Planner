#!/usr/bin/env python3
"""阶段0 POC · 免费数据源**真实网络**端到端验证(会真的联网,不放进单测)。

链路:北京起点 → ①Nominatim 正向定位到可核地名 → ②Nominatim 逆向确认"当前位置"
→ ③Overpass 按类别(自然风光 / 旅游景点)检索周边真实 POI → ④取最近的一个
→ ⑤OSRM 驾车路线,并对距离/耗时做合理性校验(非 0、时速与绕行比在合理区间)。

运行方式(任选其一)::

    python backend/data_sources/verify_poc.py
    cd backend && python -m data_sources.verify_poc
    cd backend/data_sources && python verify_poc.py

逐源打印结果与耗时;任一环节失败都给出**明确中文报错**,并以非 0 退出码结束。
"""

from __future__ import annotations

import dataclasses
import functools
import os
import sys
import time
from typing import Any, Callable, Optional

if __package__ in (None, ""):  # 直接 `python verify_poc.py` 运行时补上包父目录
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_sources import (  # noqa: E402
    OSRM_ALT_ENDPOINT,
    USER_AGENT,
    DataSourceError,
    NominatimClient,
    OsrmClient,
    OverpassClient,
    haversine_km,
)

# --------------------------------------------------------------------------- #
# 验证配置
# --------------------------------------------------------------------------- #

BEIJING_LAT = 39.9042
BEIJING_LNG = 116.4074
GEOCODE_QUERY = "北京"
HTTP_TIMEOUT = 20.0  # 技术约束:所有请求 timeout ≤ 20s
TOP_N_PRINT = 5
MIN_PLACES_PER_CATEGORY = 3

# AC1 要求"按类别在 50-100km 内返回 ≥3 个真实 POI"
GEOCODE_DEVIATION_KM = 50.0  # 正向结果与北京市中心的可接受偏差
REVERSE_DEVIATION_KM = 5.0  # 逆向结果与查询坐标的可接受偏差
MIN_SPEED_KMH = 5.0  # 驾车隐含平均时速下限
MAX_SPEED_KMH = 140.0  # 驾车隐含平均时速上限
MIN_DETOUR_RATIO = 0.9  # 驾车距离 / 直线距离 下限
MAX_DETOUR_RATIO = 5.0  # 驾车距离 / 直线距离 上限(长途按比值)
# 短途按绝对余量兜底:实测北京市中心直线 0.5 km 的景点驾车需 2.7 km
# (老城区单行线/禁左/无直连道路),纯比值阈值会误判为异常。
MAX_ABS_DETOUR_KM = 10.0


@dataclasses.dataclass(frozen=True)
class Category:
    """一类目的地(POI)的 Overpass 检索条件。"""

    label: str
    tags: list[dict[str, str]]
    radius_m: int
    element_types: str
    reason: str


CATEGORIES: tuple[Category, ...] = (
    Category(
        label="自然风光",
        tags=[{"natural": "peak"}, {"natural": "waterfall"}],
        radius_m=100_000,
        # 山峰/瀑布在 OSM 里基本都是 node;只查 node 可显著降低公共实例超时概率。
        element_types="node",
        reason="100 km 内,山峰/瀑布以 node 为主",
    ),
    Category(
        label="旅游景点",
        tags=[{"tourism": "attraction"}, {"tourism": "viewpoint"}],
        radius_m=50_000,
        # 景点常是 way/relation(园区、建筑群),需要 nwr + `out center` 才有坐标。
        element_types="nwr",
        reason="50 km 内,景点含 way/relation",
    ),
)


class CheckFailure(AssertionError):
    """验证未通过(数值不合理 / 结果不满足 AC)。"""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


# --------------------------------------------------------------------------- #
# 上下文与步骤骨架
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class Context:
    """跨步骤共享的状态与客户端。"""

    nominatim: NominatimClient = dataclasses.field(default_factory=lambda: NominatimClient(timeout=HTTP_TIMEOUT))
    overpass: OverpassClient = dataclasses.field(
        default_factory=lambda: OverpassClient(timeout=HTTP_TIMEOUT, retries=2, retry_backoff_s=1.0)
    )
    osrm: OsrmClient = dataclasses.field(default_factory=lambda: OsrmClient(timeout=HTTP_TIMEOUT))
    origin: dict[str, Any] = dataclasses.field(
        default_factory=lambda: {
            "lat": BEIJING_LAT,
            "lng": BEIJING_LNG,
            "display_name": "(内置坐标,正向编码失败时回退)",
        }
    )
    places: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    nearest: Optional[dict[str, Any]] = None
    leg: Optional[dict[str, float]] = None


@dataclasses.dataclass
class StepResult:
    """单个验证步骤的结果。"""

    title: str
    ok: bool
    detail: str
    elapsed_s: float


StepFunc = Callable[[Context], tuple[str, Any]]


def run_step(index: int, total: int, title: str, func: StepFunc, ctx: Context) -> StepResult:
    """执行并打印一个验证步骤:成功打印结果与耗时,失败打印中文报错。"""
    print(f"\n【{index}/{total} · {title}】")
    started = time.monotonic()
    try:
        detail, _ = func(ctx)
    except DataSourceError as exc:
        return _fail(title, started, f"数据源调用失败 → {exc}")
    except CheckFailure as exc:
        return _fail(title, started, f"验证未通过 → {exc}")
    except Exception as exc:  # noqa: BLE001
        return _fail(title, started, f"未预期错误({type(exc).__name__}) → {exc}")
    elapsed = time.monotonic() - started
    print(detail.rstrip())
    print(f"  → [OK] 耗时 {elapsed:.1f}s")
    return StepResult(title, True, f"通过,耗时 {elapsed:.1f}s", elapsed)


def _fail(title: str, started: float, detail: str) -> StepResult:
    """失败时把中文报错就地打印在该步骤下,便于定位是哪个数据源出的问题。"""
    elapsed = time.monotonic() - started
    print(f"  → [失败] 耗时 {elapsed:.1f}s")
    print(f"  {detail}")
    return StepResult(title, False, detail, elapsed)


# --------------------------------------------------------------------------- #
# 各验证步骤
# --------------------------------------------------------------------------- #


def step_forward_geocode(ctx: Context) -> tuple[str, Any]:
    """① 正向地理编码:地名 → 坐标(要求可核地名)。"""
    started = time.monotonic()
    place = ctx.nominatim.geocode(GEOCODE_QUERY)
    deviation = haversine_km(BEIJING_LAT, BEIJING_LNG, place["lat"], place["lng"])
    check(bool(place["display_name"].strip()), "display_name 为空,无法核对地名")
    check(
        deviation <= GEOCODE_DEVIATION_KM,
        f"定位结果偏离预期北京市中心 {deviation:.1f} km(阈值 {GEOCODE_DEVIATION_KM:.0f} km)",
    )
    ctx.origin = place
    detail = (
        f'  查询: "{GEOCODE_QUERY}"\n'
        f'  结果: {place["display_name"]}\n'
        f'  坐标: lat={place["lat"]:.6f}, lng={place["lng"]:.6f}(请求耗时 {time.monotonic() - started:.1f}s)\n'
        f"  核对: 与内置北京市中心偏差 {deviation:.2f} km ≤ {GEOCODE_DEVIATION_KM:.0f} km ✓"
    )
    return detail, place


def step_reverse_geocode(ctx: Context) -> tuple[str, Any]:
    """② 逆地理编码:坐标 → 地名(用于"当前位置")。"""
    place = ctx.nominatim.reverse(BEIJING_LAT, BEIJING_LNG)
    deviation = haversine_km(BEIJING_LAT, BEIJING_LNG, place["lat"], place["lng"])
    check(bool(place["display_name"].strip()), "逆地理编码没有返回可核地名")
    check(
        deviation <= REVERSE_DEVIATION_KM,
        f"逆地理编码结果偏离查询坐标 {deviation:.1f} km(阈值 {REVERSE_DEVIATION_KM:.0f} km)",
    )
    detail = (
        f"  查询坐标: lat={BEIJING_LAT}, lng={BEIJING_LNG}\n"
        f'  当前位置: {place["display_name"]}\n'
        f"  核对: 匹配点距查询坐标 {deviation:.3f} km ≤ {REVERSE_DEVIATION_KM:.0f} km ✓"
    )
    return detail, place


def step_overpass(ctx: Context, category: Category) -> tuple[str, Any]:
    """③ Overpass:按类别检索周边真实 POI(AC1 要求 ≥3 个)。"""
    started = time.monotonic()
    places = ctx.overpass.nearby_places(
        ctx.origin["lat"],
        ctx.origin["lng"],
        category.radius_m,
        category.tags,
        limit=20,
        require_name=True,
        element_types=category.element_types,
    )
    check(
        len(places) >= MIN_PLACES_PER_CATEGORY,
        f"{category.label}类只找到 {len(places)} 个有名字的 POI,少于 {MIN_PLACES_PER_CATEGORY} 个",
    )
    distances = [haversine_km(ctx.origin["lat"], ctx.origin["lng"], item["lat"], item["lng"]) for item in places]
    for place in places:
        place["category"] = category.label
    ctx.places.extend(places)

    lines = [
        f'  检索条件: tags={category.tags}, element_types={category.element_types}({category.reason})',
        f"  半径: {category.radius_m / 1000:.0f} km,中心: ({ctx.origin['lat']:.4f}, {ctx.origin['lng']:.4f})",
        f"  实际服务端点: {ctx.overpass.used_endpoint}(请求耗时 {time.monotonic() - started:.1f}s)",
        f"  命中: {len(places)} 个有名字的 POI(≥{MIN_PLACES_PER_CATEGORY} ✓),"
        f" 距离范围 {min(distances):.1f}-{max(distances):.2f} km",
        f"  最近 {min(TOP_N_PRINT, len(places))} 个:",
    ]
    for item, distance in list(zip(places, distances, strict=True))[:TOP_N_PRINT]:
        tags = ", ".join(f"{key}={value}" for key, value in list(item["tags"].items())[:3])
        lines.append(f'    · {item["name"]} — {distance:.1f} km [{tags}]')
    return "\n".join(lines), places


def _pick_nearest(ctx: Context) -> dict[str, Any]:
    check(bool(ctx.places), "前面没有检索到任何 POI,无法选择最近目的地")
    nearest = min(
        ctx.places,
        key=lambda item: haversine_km(ctx.origin["lat"], ctx.origin["lng"], item["lat"], item["lng"]),
    )
    ctx.nearest = nearest
    return nearest


def step_route(ctx: Context) -> tuple[str, Any]:
    """④⑤ OSRM:起点 → 最近目的地的驾车路线 + 合理性校验。"""
    nearest = _pick_nearest(ctx)
    start = (ctx.origin["lng"], ctx.origin["lat"])
    end = (nearest["lng"], nearest["lat"])
    started = time.monotonic()
    leg = ctx.osrm.route(start, end)
    ctx.leg = leg

    distance_km = leg["distance_km"]
    duration_min = leg["duration_min"]
    check(distance_km > 0, f"驾车距离应 > 0,实际 {distance_km}")
    check(duration_min > 0, f"驾车耗时应 > 0,实际 {duration_min}")

    speed_kmh = distance_km / (duration_min / 60.0)
    check(
        MIN_SPEED_KMH <= speed_kmh <= MAX_SPEED_KMH,
        f"隐含平均时速 {speed_kmh:.1f} km/h 不在合理区间 [{MIN_SPEED_KMH:.0f}, {MAX_SPEED_KMH:.0f}]",
    )
    straight_km = max(haversine_km(ctx.origin["lat"], ctx.origin["lng"], nearest["lat"], nearest["lng"]), 1e-6)
    ratio = distance_km / straight_km
    min_allowed_km = MIN_DETOUR_RATIO * straight_km
    max_allowed_km = max(straight_km * MAX_DETOUR_RATIO, straight_km + MAX_ABS_DETOUR_KM)
    check(
        min_allowed_km <= distance_km <= max_allowed_km,
        f"驾车距离 {distance_km:.2f} km 相对直线 {straight_km:.2f} km 不合理"
        f"(合理区间 [{min_allowed_km:.2f}, {max_allowed_km:.2f}] km)",
    )

    detail = (
        f'  最近目的地: {nearest["name"]}(类别:{nearest.get("category", "未知")})\n'
        f"  起点: ({start[1]:.6f}, {start[0]:.6f}) → 终点: ({end[1]:.6f}, {end[0]:.6f})\n"
        f"  直线距离: {straight_km:.2f} km\n"
        f"  驾车结果: distance_km={distance_km}, duration_min={duration_min}"
        f"(请求耗时 {time.monotonic() - started:.1f}s)\n"
        f"  合理性: 隐含平均时速 {speed_kmh:.1f} km/h ∈ [{MIN_SPEED_KMH:.0f}, {MAX_SPEED_KMH:.0f}] ✓;"
        f" 绕行比 {ratio:.2f},驾车距离落在合理区间 [{min_allowed_km:.2f}, {max_allowed_km:.2f}] km ✓"
    )
    return detail, leg


def step_route_alt_endpoint(ctx: Context) -> tuple[str, Any]:
    """⑥ 验证 OSRM 端点可切换(备选公共实例给出同量级结果)。"""
    nearest = _pick_nearest(ctx)
    assert ctx.leg is not None, "主端点路线尚未取得"
    alt = OsrmClient(OSRM_ALT_ENDPOINT, timeout=HTTP_TIMEOUT)
    leg = alt.route((ctx.origin["lng"], ctx.origin["lat"]), (nearest["lng"], nearest["lat"]))
    ratio = leg["distance_km"] / max(ctx.leg["distance_km"], 1e-6)
    check(0.7 <= ratio <= 1.5, f"备选端点结果与主端点差异过大(比值 {ratio:.2f})")
    detail = (
        f"  备选端点: {alt.endpoint}\n"
        f'  到同一目的地({nearest["name"]}): distance_km={leg["distance_km"]}, duration_min={leg["duration_min"]}\n'
        f"  与主端点({ctx.osrm.endpoint})比值: {ratio:.3f} ∈ [0.7, 1.5] ✓"
    )
    return detail, leg


# --------------------------------------------------------------------------- #
# 汇总输出
# --------------------------------------------------------------------------- #


def print_header(ctx: Context) -> None:
    print("=" * 78)
    print("Where2Go 阶段0 · 免费数据源 POC 真实网络端到端验证")
    print("=" * 78)
    print(f"  时间(UTC)   : {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}")
    print(f"  User-Agent  : {USER_AGENT}")
    print(f"  HTTP 超时   : {HTTP_TIMEOUT:.0f}s(约束 ≤20s)")
    print(f"  起点        : 北京 lat={BEIJING_LAT}, lng={BEIJING_LNG}(正向查询词:{GEOCODE_QUERY!r})")
    print(f"  Nominatim   : {ctx.nominatim.endpoint}(节流 ≥{ctx.nominatim.min_interval:.0f}s/次)")
    print(f"  OSRM        : {ctx.osrm.endpoint}")
    print(f"  Overpass    : {ctx.overpass.endpoint}")
    print(f"  Overpass 备用: {'、'.join(ctx.overpass.fallback_endpoints)}")
    print("  POI 类别    : " + " / ".join(f"{item.label}({item.radius_m // 1000} km)" for item in CATEGORIES))


def print_summary(results: list[StepResult], ctx: Context) -> int:
    print("\n" + "=" * 78)
    print("验证汇总")
    print("=" * 78)
    for index, result in enumerate(results, start=1):
        flag = "OK  " if result.ok else "失败"
        print(f"  [{flag}] {index}. {result.title} —— {result.detail}")

    print("\nAC1 逐项核对:")
    per_category = {
        category.label: sum(1 for place in ctx.places if place.get("category") == category.label)
        for category in CATEGORIES
    }
    category_text = "、".join(f"{label} {count} 个" for label, count in per_category.items())
    ac1 = [
        (
            bool(ctx.origin.get("display_name")) and "内置坐标" not in str(ctx.origin.get("display_name")),
            f'正向定位到可核地名:{ctx.origin.get("display_name")}',
        ),
        (
            all(count >= MIN_PLACES_PER_CATEGORY for count in per_category.values()),
            f"按类别检索到真实 POI(每类 ≥{MIN_PLACES_PER_CATEGORY}):{category_text}",
        ),
        (
            ctx.leg is not None and ctx.leg["distance_km"] > 0 and ctx.leg["duration_min"] > 0,
            "最近目的地驾车路线:"
            + (
                f'distance_km={ctx.leg["distance_km"]}, duration_min={ctx.leg["duration_min"]}'
                if ctx.leg
                else "未取得"
            ),
        ),
    ]
    for passed, text in ac1:
        print(f"  [{'✓' if passed else '✗'}] {text}")

    failures = [result for result in results if not result.ok]
    total_elapsed = sum(result.elapsed_s for result in results)
    print(f"\n结论: {len(results) - len(failures)}/{len(results)} 步通过,总耗时 {total_elapsed:.1f}s")
    if failures:
        print("失败步骤的中文报错见上方;常见原因:公共实例繁忙(Overpass 504)、网络不可达或被限流。")
    return 1 if failures or not all(passed for passed, _ in ac1) else 0


def main() -> int:
    ctx = Context()
    print_header(ctx)

    plan: list[tuple[str, StepFunc]] = [
        ("Nominatim 正向地理编码:地名 → 坐标", step_forward_geocode),
        ("Nominatim 逆地理编码:坐标 → 当前位置地名", step_reverse_geocode),
    ]
    for category in CATEGORIES:
        plan.append(
            (
                f"Overpass POI 检索:{category.label}类({category.radius_m // 1000} km 内)",
                functools.partial(step_overpass, category=category),
            )
        )
    plan.append(("OSRM 驾车路线:起点 → 最近目的地(含合理性校验)", step_route))
    plan.append(("OSRM 端点可切换性:备选实例交叉验证", step_route_alt_endpoint))

    results = [
        run_step(index, len(plan), title, func, ctx) for index, (title, func) in enumerate(plan, start=1)
    ]
    return print_summary(results, ctx)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[中断] 用户手动终止验证。")
        raise SystemExit(130) from None
