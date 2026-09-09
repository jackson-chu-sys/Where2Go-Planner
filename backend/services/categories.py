"""分类的**兼容导入面**:规则引擎已迁到 :mod:`services.classify`(TASK-1b)。

阶段1a 这里是"简化归类"(只保证每个 Place 有一个 category 值)。TASK-1b 把需求
四分类的完整 tag 线索、**归类优先级**(滑雪 > 运动 > 人文美食 > 自然)、**跨 tag 去重**
(键 = OSM ``type + id``)与检索并集(:data:`services.classify.SEARCH_GROUPS`)
都放进了 :mod:`services.classify`(docs/STAGE1-PLAN.md 第 3 节)。

本模块只保留旧的导入面,既有代码与单测无需改动;**新代码请直接 import
:mod:`services.classify`**。
"""

from __future__ import annotations

from services.classify import (
    CATEGORIES,
    CATEGORY_CULTURE,
    CATEGORY_NATURE,
    CATEGORY_PRIORITY,
    CATEGORY_SKI,
    CATEGORY_SPORT,
    CATEGORY_RULES,
    SEARCH_GROUPS,
    categorize,
    category_keys,
    category_of,
    classify_places,
    classify_tags,
    dedupe_places,
    find_category,
    is_known_category,
    normalize_tags,
    search_groups,
    search_tags,
)

__all__ = [
    "CATEGORIES",
    "CATEGORY_CULTURE",
    "CATEGORY_NATURE",
    "CATEGORY_PRIORITY",
    "CATEGORY_RULES",
    "CATEGORY_SKI",
    "CATEGORY_SPORT",
    "SEARCH_GROUPS",
    "categorize",
    "category_keys",
    "category_of",
    "classify_places",
    "classify_tags",
    "dedupe_places",
    "find_category",
    "is_known_category",
    "normalize_tags",
    "search_groups",
    "search_tags",
]
