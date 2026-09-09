"""既有库重归类:把 ``Place.category`` 的**旧值/空值**按四分类规则重算(TASK-1b)。

为什么需要:阶段1a 入库时用的是"简化归类"(只认 ``natural``/``tourism`` 等少量 tag,
滑雪场/运动基本认不出来),库里存量行的 ``category`` 与新规则不一致;而且 POC 的
"自然风光/旅游景点"是**按 OSM 原始 tag 二分**,同一地物两个 tag 并存就重复出现在两类
(docs/STAGE1-PLAN.md 第 3 节"根因")。

两条修复路径,互补:

1. **抓取时覆盖**(默认已生效):``services.place_loader.to_place_items`` 每次抓取都用
   :func:`services.classify.classify_places` 重新去重 + 归类,``upsert_places`` 覆盖
   ``category``(但**不覆盖**已生成的 ``intro``),所以 ``refresh=true`` 重抓即自动修正;
2. **本模块的存量重算**:不触网、只读库里已存的 ``tags`` 重算分类,给不想重抓的存量库用。

去重口径:唯一键 ``(osm_type, osm_id, origin_city)`` 保证一个 OSM 实体在一个城市库里
只有一行,叠加"一地只归一类"的优先级规则,自然/景点不会再重复。

CLI(离线,不触网)::

    python -m services.reclassify                 # 全库重算
    python -m services.reclassify 上海 50_100      # 只重算某 (城市, band)
    python -m services.reclassify --dry-run        # 只看会变多少,不写库
"""

from __future__ import annotations

import argparse
from collections import Counter
from typing import Any, Optional

from sqlalchemy.orm import Session

from db import repository as repo
from services.classify import categorize, category_keys, is_known_category

TOP_TRANSITIONS = 8


def is_legacy_category(category: Optional[str]) -> bool:
    """分类值是否算"旧值/空值"(不在四分类 + 其他的已知取值里)。"""
    return not is_known_category(category)


def reclassify_row(row: Any) -> Optional[str]:
    """按库里存的 ``tags`` 重算一行分类;与现值相同返回 ``None``(表示无需改动)。"""
    expected = categorize(row.tags)
    current = (row.category or "").strip()
    if current == expected:
        return None
    return expected


def reclassify_places(
    session: Session,
    *,
    origin_city: Optional[str] = None,
    band: Optional[str] = None,
    only_legacy: bool = False,
    commit: bool = True,
) -> dict[str, Any]:
    """重算已入库 ``Place`` 的分类,返回统计。

    ``only_legacy=True`` 只动"旧值/空值"的行(``category`` 不在已知分类里);默认全量
    重算,因为阶段1a 的简化归类与四分类规则本身就有差异(滑雪/运动当时认不出来)。
    ``commit=False`` 是纯试算:只统计"会怎么变",不改 ORM 对象、不写库
    (否则后续的 ``session.scalar(...)`` 会触发 autoflush 把改动带进去)。

    返回 ``{"scanned", "changed", "unchanged", "legacy", "by_category", "transitions"}``;
    ``transitions`` 是 ``"旧值 → 新值": 条数`` 的降序列表(取前 :data:`TOP_TRANSITIONS`)。
    """
    rows = repo.select_places(session, origin_city=origin_city, band=band)
    if only_legacy:
        rows = [row for row in rows if is_legacy_category(row.category)]

    changed = 0
    legacy = 0
    transitions: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    for row in rows:
        if is_legacy_category(row.category):
            legacy += 1
        expected = reclassify_row(row)
        if expected is None:
            by_category[(row.category or "").strip()] += 1
            continue
        transitions[f"{(row.category or '').strip() or '(空)'} → {expected}"] += 1
        by_category[expected] += 1
        if commit:
            row.category = expected
        changed += 1

    if changed and commit:
        session.commit()
    known = set(category_keys())
    ordered = {key: by_category[key] for key in sorted(by_category, key=lambda k: (k not in known, k))}
    return {
        "scanned": len(rows),
        "changed": changed,
        "unchanged": len(rows) - changed,
        "legacy": legacy,
        "by_category": ordered,
        "transitions": transitions.most_common(TOP_TRANSITIONS),
        "committed": bool(changed and commit),
    }


def main(argv: Optional[list[str]] = None) -> int:
    """CLI:重算库内分类。``python -m services.reclassify 上海 50_100``"""
    parser = argparse.ArgumentParser(description="按四分类规则重算已入库 Place 的 category(离线,不触网)")
    parser.add_argument("city", nargs="?", default=None, help="起点城市名(默认:全部城市)")
    parser.add_argument("band", nargs="?", default=None, help="距离分段 key(默认:该城市全部分段)")
    parser.add_argument("--only-legacy", action="store_true", help="只重算 category 为旧值/空值的行")
    parser.add_argument("--dry-run", action="store_true", help="只报变化,不写库")
    parser.add_argument("--db", default=None, help="数据库 URL(默认 WHERE2GO_DB_URL 或 backend/data/where2go.db)")
    args = parser.parse_args(argv)

    from db import init_db, make_engine, open_session

    engine = make_engine(args.db)
    init_db(engine)
    with open_session(engine) as session:
        stats = reclassify_places(
            session,
            origin_city=args.city,
            band=args.band,
            only_legacy=args.only_legacy,
            commit=not args.dry_run,
        )

    scope = " · ".join(part for part in (args.city, args.band) if part) or "全库"
    verb = "待改" if args.dry_run else "已改"
    print(
        f"[重归类] {scope} · 扫描 {stats['scanned']} 条 · {verb} {stats['changed']} 条 · "
        f"未变 {stats['unchanged']} 条 · 旧值/空值 {stats['legacy']} 条"
    )
    print(f"[分类分布] {' · '.join(f'{name} {total}' for name, total in stats['by_category'].items())}")
    for label, total in stats["transitions"]:
        print(f"  - {label}:{total} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
