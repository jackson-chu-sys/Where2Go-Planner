"""OSM tags → 需求分类的**简化**归类(阶段 1a)。

⚠️ 边界:TASK-1a 只需要"每个 Place 有一个 category 值 + 前端能按分类过滤/着色"。
需求四分类的**完整 tag 线索、优先级归类去重、LLM 简介**是 TASK-1b 的活
(docs/STAGE1-PLAN.md 第 3 节),本模块到时会被替换,所以规则刻意保持简单:

* 一个 Place 只会得到一个 category(按 :data:`CATEGORY_RULES` 顺序首次命中);
* 有 ``natural`` 等自然线索时优先算"自然风光",避免 POC 里
  "自然风光/旅游景点"同一地物重复出现的问题;
* 认不出来就落到 :data:`UNCATEGORIZED`("其他"),不猜。

分类的产品语义(需求文档 / STAGE1-PLAN 第 3 节):自然风光、小城人文美食、滑雪场、运动。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable, Optional

from db.models import UNCATEGORIZED

CATEGORY_NATURE = "自然风光"
CATEGORY_CULTURE = "小城人文美食"
CATEGORY_SKI = "滑雪场"
CATEGORY_SPORT = "运动"

# 前端 pin 颜色/图标(与 STAGE1-PLAN 第 2 节一致:自然=绿、人文=橙、滑雪=蓝、运动=红)
CATEGORIES: tuple[dict[str, Any], ...] = (
    {"key": CATEGORY_NATURE, "label": "自然风光", "color": "#16a34a", "emoji": "⛰️"},
    {"key": CATEGORY_CULTURE, "label": "小城人文美食", "color": "#ea580c", "emoji": "🏮"},
    {"key": CATEGORY_SKI, "label": "滑雪场", "color": "#2563eb", "emoji": "⛷️"},
    {"key": CATEGORY_SPORT, "label": "运动", "color": "#dc2626", "emoji": "🏀"},
    {"key": UNCATEGORIZED, "label": "其他", "color": "#6b7280", "emoji": "📍"},
)

SKI_KEYS = ("piste:type", "piste:name", "piste:difficulty", "ski")
SKI_SPORTS = {"skiing", "ski", "snowboard", "alpine", "nordic", "cross-country"}
SPORT_KEYS = ("sport",)
SPORT_LEISURE = {"sports_centre", "pitch", "stadium", "swimming_pool", "climbing", "golf_course"}
FOOD_AMENITY = {
    "restaurant", "cafe", "bar", "pub", "fast_food", "food_court", "biergarten", "ice_cream",
}
CULTURE_TOURISM = {"attraction", "museum", "gallery", "artwork", "theme_park", "zoo"}
CULTURE_PLACE = {"town", "village", "city", "hamlet", "suburb", "neighbourhood"}
NATURE_LEISURE = {"park", "nature_reserve", "garden", "beach", "forest"}
NATURE_TOURISM = {"viewpoint"}
NATURE_WATERWAY = {"waterfall", "river", "lake", "spring", "rapids"}

CategoryRule = tuple[str, Callable[[dict[str, str]], bool]]


def category_keys() -> tuple[str, ...]:
    """全部分类 key(含"其他"),供 API 校验与前端下拉使用。"""
    return tuple(str(item["key"]) for item in CATEGORIES)


def is_known_category(key: Optional[str]) -> bool:
    """是否是已知分类。"""
    return (key or "").strip() in category_keys()


def find_category(key: str) -> Optional[dict[str, Any]]:
    """按 key 取分类元信息(label/color/emoji);未知返回 None。"""
    wanted = (key or "").strip()
    return next((item for item in CATEGORIES if item["key"] == wanted), None)


def normalize_tags(tags: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """tag 键值归一:小写、去空白,便于写规则(值缺失记为空串)。"""
    normalized: dict[str, str] = {}
    for key, value in dict(tags or {}).items():
        normalized[str(key).strip().lower()] = "" if value is None else str(value).strip().lower()
    return normalized


def _has_nature_clue(tags: dict[str, str]) -> bool:
    return bool(
        tags.get("natural")
        or tags.get("leisure") in NATURE_LEISURE
        or tags.get("waterway") in NATURE_WATERWAY
        or tags.get("tourism") in NATURE_TOURISM
    )


def _is_ski(tags: dict[str, str]) -> bool:
    if any(key in tags for key in SKI_KEYS):
        return True
    if tags.get("sport") in SKI_SPORTS:
        return True
    return any("ski" in value for key, value in tags.items() if key in ("leisure", "tourism", "sport"))


def _is_sport(tags: dict[str, str]) -> bool:
    return bool(tags.get("sport")) or tags.get("leisure") in SPORT_LEISURE


def _is_culture(tags: dict[str, str]) -> bool:
    # 有自然线索的 attraction(如山景)归"自然风光",不算人文,避免 POC 的重复问题。
    if _has_nature_clue(tags):
        return False
    return bool(
        tags.get("historic")
        or tags.get("cuisine")
        or tags.get("amenity") in FOOD_AMENITY
        or tags.get("tourism") in CULTURE_TOURISM
        or tags.get("place") in CULTURE_PLACE
    )


CATEGORY_RULES: tuple[CategoryRule, ...] = (
    (CATEGORY_SKI, _is_ski),
    (CATEGORY_SPORT, _is_sport),
    (CATEGORY_CULTURE, _is_culture),
    (CATEGORY_NATURE, _has_nature_clue),
)


def categorize(tags: Optional[Mapping[str, Any]]) -> str:
    """给一组 OSM tags 归类;首次命中即返回,认不出来返回"其他"。"""
    normalized = normalize_tags(tags)
    for category, matches in CATEGORY_RULES:
        if matches(normalized):
            return category
    return UNCATEGORIZED
