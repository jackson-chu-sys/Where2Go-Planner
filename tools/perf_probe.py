"""TASK-12 分段计时:冷抓 0_50 / ring 50_100 / 「加载更多」扩抓各一次。

插桩点:
- data_sources.amap 的 search_around/search_polygon/geocode:次数+累计耗时(节流含在内);
- services.intro.fill_missing_intros:耗时;
- repo.upsert_places + record_segment:落库耗时;
- load_segment 总墙钟。

用法:cd backend && set -a && source /opt/data/.env && set +a && ../.venv/bin/python ../tools/perf_probe.py <city>
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from data_sources import amap as ds_amap  # noqa: E402
from services import intro as intro_service  # noqa: E402
from services import place_loader  # noqa: E402
from db import repository as repo  # noqa: E402
from db.base import get_session  # noqa: E402

STATS: dict[str, dict[str, float]] = {}


def _wrap(obj, name, bucket):
    orig = getattr(obj, name)
    stats = STATS.setdefault(bucket, {"calls": 0, "seconds": 0.0})

    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            return orig(*args, **kwargs)
        finally:
            stats["calls"] += 1
            stats["seconds"] += time.perf_counter() - t0

    setattr(obj, name, wrapper)
    return orig


def run_phase(session, city: str, band: str, **kwargs):
    t0 = time.perf_counter()
    outcome = place_loader.load_segment(session, city=city, band=band, **kwargs)
    wall = time.perf_counter() - t0
    print(
        f"[{band}] wall={wall:.1f}s places={len(outcome.places)} source={outcome.source} "
        f"written={outcome.written} seeded={outcome.seeded} intro={outcome.intro_stats}",
        flush=True,
    )
    for bucket, s in sorted(STATS.items()):
        print(f"    {bucket}: calls={int(s['calls'])} seconds={s['seconds']:.1f}", flush=True)
    return wall


def main() -> int:
    city = sys.argv[1] if len(sys.argv) > 1 else "成都"
    _wrap(ds_amap, "search_around", "amap.search_around")
    _wrap(ds_amap, "search_polygon", "amap.search_polygon")
    _wrap(ds_amap, "geocode", "amap.geocode")
    _wrap(place_loader, "ds_amap_geocode", "place_loader.amap_geocode")
    _wrap(intro_service, "fill_missing_intros", "intro.fill_missing_intros")
    _wrap(repo, "upsert_places", "repo.upsert_places")

    import contextlib
    with contextlib.closing(next(get_session())) as session:
        print(f"=== city={city} ===", flush=True)
        for key, s in STATS.items():
            s["calls"] = 0; s["seconds"] = 0.0
        run_phase(session, city, "0_50")
        for key, s in STATS.items():
            s["calls"] = 0; s["seconds"] = 0.0
        run_phase(session, city, "50_100")
        # 扩抓一轮:target_total 下一轮
        for key, s in STATS.items():
            s["calls"] = 0; s["seconds"] = 0.0
        rec = repo.get_segment(session, origin_city=city, band="0_50")
        rounds = int(getattr(rec, "fetch_rounds", 0) or 0)
        t0 = time.perf_counter()
        outcome = place_loader.load_segment(
            session, city=city, band="0_50", refresh=True,
            target_total=place_loader.progressive_target_total(rounds),
        )
        wall = time.perf_counter() - t0
        print(f"[0_50 扩抓 round={rounds+1}] wall={wall:.1f}s places={len(outcome.places)} written={outcome.written} intro={outcome.intro_stats}", flush=True)
        for bucket, s in sorted(STATS.items()):
            print(f"    {bucket}: calls={int(s['calls'])} seconds={s['seconds']:.1f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
