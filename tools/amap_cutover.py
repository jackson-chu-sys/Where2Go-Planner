#!/usr/bin/env python3
"""TASK-9b 一次性清库脚本:清掉「坐标系/来源不一致」的派生行,**保留用户数据**。

高德切源(2026-10-01)后,存量派生数据有两处口径不一致,留着会让新抓的数据与旧行混在
一张表里(去重键不同源、坐标 GCJ-02 vs WGS-84 偏差 50~500m):

* ``Place.osm_type/osm_id`` 存量是 OSM 的 ``node/way/relation`` + 整数 id,新行是
  ``amap`` + ``crc32(高德 POI id)`` —— 同一实体两套身份,upsert 幂等失效;
* ``SegmentFetch`` 水位一旦存在,该 (城市, band) 就**只读库不重抓**,旧行永远消化不掉。

所以切源后跑一次本脚本:清派生行、留用户数据,再重抓即可(收藏是**快照**,不重算)。

清除:``places`` / ``segment_fetch`` / ``stays`` / ``stay_query_cache`` / ``origin_cache``
/ ``place_recommendations`` / ``place_details`` / ``place_highlights`` / ``place_media``
(后两张表是 TASK-8 的,尚未建则**自动跳过**)。
保留:``collections`` / ``collection_cats`` / ``trip_plans``(**用户数据,一行都不动**)。

用法(默认 ``--dry-run``,只报数不删)::

    python tools/amap_cutover.py                       # 报数(安全,可反复跑)
    python tools/amap_cutover.py --apply               # 真删(清派生行)
    python tools/amap_cutover.py --db sqlite:////tmp/x.db --apply

依赖:纯 stdlib + sqlalchemy(复用 :mod:`db.base` 的引擎与 URL 解析口径,
即 ``WHERE2GO_DB_URL`` 环境变量优先,缺省 ``backend/data/where2go.db``)。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import inspect, text  # noqa: E402

from db.base import get_engine, make_engine  # noqa: E402

#: 要清的**派生**表(顺序 = 报数顺序;不存在的表自动跳过)
DERIVED_TABLES: tuple[str, ...] = (
    "places",
    "segment_fetch",
    "stays",
    "stay_query_cache",
    "origin_cache",
    "place_recommendations",
    "place_details",
    # TASK-8(详情弹窗/相关图片)的表:尚未建则跳过,建了也一并清(都按 POI 派生)
    "place_highlights",
    "place_media",
)

#: **必须保留**的用户数据(收藏是快照、行程是编排结果,都不随数据源重算)
PRESERVED_TABLES: tuple[str, ...] = ("collections", "collection_cats", "trip_plans")


def resolve_engine(url: Optional[str] = None):
    """引擎:给了 ``--db`` 就按它建,否则用 :func:`db.base.get_engine`(env / 默认库文件)。"""
    return make_engine(url) if url else get_engine()


def existing_tables(engine) -> tuple[set[str], list[str]]:
    """库里实际存在的表 → (表名集合, 被跳过的表名列表)。"""
    present = set(inspect(engine).get_table_names())
    skipped = [name for name in DERIVED_TABLES if name not in present]
    return present, skipped


def count_rows(engine, table: str) -> int:
    """一张表的行数(表不存在时按 0 报,不抛)。"""
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


def report(engine, *, apply: bool) -> int:
    """报数(``--dry-run``)或真删(``--apply``);返回被清除的总行数。"""
    present, skipped = existing_tables(engine)
    targets = [name for name in DERIVED_TABLES if name in present]
    mode = "APPLY(真删)" if apply else "DRY-RUN(只报数,不删任何行)"
    print(f"[amap_cutover] {mode} · 库:{engine.url}")
    if not targets:
        print("[派生行] 库里没有需要清除的表(可能已经清过)")
    before = {name: count_rows(engine, name) for name in targets}
    for name in targets:
        print(f"  - {name:<22} {before[name]:>7} 行")
    if skipped:
        print(f"[跳过] 尚未建的表:{'、'.join(skipped)}")

    preserved = {name: count_rows(engine, name) for name in PRESERVED_TABLES if name in present}
    missing_preserved = [name for name in PRESERVED_TABLES if name not in present]
    for name, total in preserved.items():
        print(f"[保留] {name:<22} {total:>7} 行(用户数据,一行都不动)")
    if missing_preserved:
        print(f"[保留] 尚未建的表:{'、'.join(missing_preserved)}")

    if not apply:
        print(f"[结论] dry-run:将清除 {sum(before.values())} 行派生数据;加 --apply 才真删")
        return 0

    removed = 0
    with engine.begin() as connection:  # 一个事务里删完:中途失败整体回滚
        for name in targets:
            result = connection.execute(text(f"DELETE FROM {name}"))
            removed += int(result.rowcount or 0)
    after = {name: count_rows(engine, name) for name in targets}
    left = {name: total for name, total in after.items() if total}
    if left:
        raise RuntimeError(f"清除后仍有残留行:{left}")
    for name, total in preserved.items():
        now = count_rows(engine, name)
        if now != total:
            raise RuntimeError(f"用户数据被误删:{name} {total} → {now}")
    print(f"[完成] 已清除 {removed} 行派生数据;保留 {sum(preserved.values())} 行用户数据")
    print("[下一步] 重抓一个新城市做端到端:/api/places?origin=杭州&band=0_50")
    return removed


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 入口:默认 ``--dry-run``,``--apply`` 才真删。"""
    parser = argparse.ArgumentParser(
        description="高德切源(TASK-9b)一次性清库:清派生行,保留 Collection/CollectionCat/TripPlan"
    )
    parser.add_argument("--db", default=None, help="数据库 URL(默认 WHERE2GO_DB_URL 或 backend/data/where2go.db)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="只打印各表行数,不删任何行(默认)")
    group.add_argument("--apply", action="store_true", help="真删派生行(用户数据仍保留)")
    args = parser.parse_args(argv)
    engine = resolve_engine(args.db)
    report(engine, apply=bool(args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
