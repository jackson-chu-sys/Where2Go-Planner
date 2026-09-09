"""四分类归类引擎:OSM tags → 需求四分类 + 优先级归类 + 跨 tag 去重(TASK-1b)。

规格见 docs/STAGE1-PLAN.md 第 3 节,三条硬规则:

1. **归类优先级**:滑雪场 > 运动 > 小城人文美食 > 自然风光,每个地物只归一类
   (:data:`CATEGORY_RULES` 按此顺序排列,首次命中即定类);
2. **去重键 = OSM ``type + id``**:同一实体被多组检索 tag 命中时合并 tags 后只留一行
   (:func:`dedupe_places`),没有 OSM id 的种子数据由调用方给 ``identity`` 兜底;
3. **检索**:按 band 上限半径一次查四分类 tag 的**并集**,每组带独立配额
   (:data:`SEARCH_GROUPS`),避免某一类(如餐厅)把总量刷爆。

分类与 OSM tag 的对照(STAGE1-PLAN 第 3 节表格):

============  ==========================================================  ======================================
分类          产品含义                                                     OSM 识别线索
============  ==========================================================  ======================================
自然风光      山/湖/瀑布/公园等自然景观                                     ``natural=peak/waterfall/water/...``、
                                                                            ``leisure=park/nature_reserve/garden``、
                                                                            ``waterway=waterfall/...``、``tourism=viewpoint``、
                                                                            ``place=island``
小城人文美食  古镇/人文街区/特色美食                                        ``historic=*``、``tourism=attraction/museum/...``、
                                                                            ``amenity=restaurant/cafe/...``、``cuisine=*``、
                                                                            ``place=town/village``
滑雪场        滑雪目的地                                                    ``piste:type=*``、``sport=skiing/...``、``ski=yes``、
                                                                            ``landuse=winter_sports``
运动          特定运动场所                                                  ``sport=*``、``leisure=sports_centre/pitch/stadium/...``
============  ==========================================================  ======================================

**修复 POC 的重复问题**:POC 直接拿 ``natural=peak`` / ``tourism=attraction`` 当分类,
同一地物两个 tag 并存就同时出现在"自然风光/旅游景点"两类里。这里分类是**产品语义**:
带自然线索的泛景点(``tourism=attraction``/``place``)只算自然风光,除非它另有
``historic=*`` 或美食线索(``cuisine``/``amenity=restaurant`` 等)才算人文美食;
再叠加优先级与 ``(type, id)`` 去重,一个点绝不会出现在两类。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Optional, Union

from db.models import OSM_ELEMENT_TYPES, UNCATEGORIZED

CATEGORY_NATURE = "自然风光"
CATEGORY_CULTURE = "小城人文美食"
CATEGORY_SKI = "滑雪场"
CATEGORY_SPORT = "运动"

# 归类优先级:滑雪(最专一)→ 运动 → 人文美食 → 自然风光(最泛)。
CATEGORY_PRIORITY: tuple[str, ...] = (CATEGORY_SKI, CATEGORY_SPORT, CATEGORY_CULTURE, CATEGORY_NATURE)

# 前端 pin 颜色/图标(STAGE1-PLAN 第 2 节:自然=绿、人文=橙、滑雪=蓝、运动=红)
CATEGORIES: tuple[dict[str, Any], ...] = (
    {
        "key": CATEGORY_NATURE, "label": "自然风光", "color": "#16a34a", "emoji": "⛰️",
        "blurb": "山 / 湖 / 瀑布 / 公园等自然景观",
    },
    {
        "key": CATEGORY_CULTURE, "label": "小城人文美食", "color": "#ea580c", "emoji": "🏮",
        "blurb": "古镇 / 人文街区 / 特色美食",
    },
    {
        "key": CATEGORY_SKI, "label": "滑雪场", "color": "#2563eb", "emoji": "⛷️",
        "blurb": "滑雪目的地(雪道 / 雪场)",
    },
    {
        "key": CATEGORY_SPORT, "label": "运动", "color": "#dc2626", "emoji": "🏀",
        "blurb": "特定运动场所(球类 / 攀岩 / 骑行 / 水上)",
    },
    {
        "key": UNCATEGORIZED, "label": "其他", "color": "#6b7280", "emoji": "📍",
        "blurb": "四分类线索都没命中的地物(不猜)",
    },
)

# --------------------------------------------------------------------------- #
# 识别线索(归类规则用;检索线索见 SEARCH_GROUPS)
# --------------------------------------------------------------------------- #

FALSY_VALUES = {"", "no", "none", "false", "0"}

SKI_TAG_KEYS = ("piste:type", "piste:name", "piste:difficulty", "piste:ref")
SKI_FLAG_KEYS = ("ski", "snowboard")
SKI_SPORT_VALUES = {
    "ski", "skiing", "snowboard", "snowboarding", "alpine", "nordic", "cross-country",
    "cross_country", "freeride", "downhill", "ski_jumping", "winter_sports",
}
SKI_VALUE_HINTS = ("ski", "snowboard", "piste", "winter_sports")
SKI_LANDUSE = {"winter_sports"}

SPORT_KEYS = ("sport",)
SPORT_LEISURE = {
    "sports_centre", "pitch", "stadium", "swimming_pool", "climbing", "golf_course",
    "horse_riding", "track", "sports_hall", "fitness_centre", "water_park", "marina",
}

FOOD_AMENITY = {
    "restaurant", "cafe", "bar", "pub", "fast_food", "food_court", "biergarten", "ice_cream",
}
CULTURE_TOURISM = {"attraction", "museum", "gallery", "artwork", "theme_park", "zoo"}
CULTURE_PLACE = {"town", "village", "city", "hamlet", "suburb", "neighbourhood", "borough"}
CULTURE_KEYS = ("historic", "cuisine")

NATURE_KEYS = ("natural",)
NATURE_LEISURE = {"park", "nature_reserve", "garden", "beach", "forest", "dog_park"}
NATURE_TOURISM = {"viewpoint"}
NATURE_WATERWAY = {"waterfall", "river", "lake", "spring", "rapids", "stream"}
NATURE_PLACE = {"island", "islet"}

CategoryRule = tuple[str, Callable[[dict[str, str]], bool]]
IdentityFn = Callable[[Mapping[str, Any]], tuple[Any, ...]]
TagValue = Union[str, None]
TagSelector = Union[Mapping[str, TagValue], str]


def category_keys() -> tuple[str, ...]:
    """全部分类 key(含"其他"),供 API 校验与前端下拉使用。"""
    return tuple(str(item["key"]) for item in CATEGORIES)


def is_known_category(key: Optional[str]) -> bool:
    """是否是已知分类。"""
    return (key or "").strip() in category_keys()


def find_category(key: Optional[str]) -> Optional[dict[str, Any]]:
    """按 key 取分类元信息(label/color/emoji/blurb);未知返回 None。"""
    wanted = (key or "").strip()
    return next((item for item in CATEGORIES if item["key"] == wanted), None)


def normalize_tags(tags: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """tag 键值归一:小写、去空白,便于写规则(值缺失记为空串)。"""
    normalized: dict[str, str] = {}
    for key, value in dict(tags or {}).items():
        normalized[str(key).strip().lower()] = "" if value is None else str(value).strip().lower()
    return normalized


def _flagged(tags: dict[str, str], key: str) -> bool:
    """tag 存在且值不是否定语义(``no``/``false``/``0``)。"""
    value = tags.get(key)
    return value is not None and value not in FALSY_VALUES


def _has_nature_clue(tags: dict[str, str]) -> bool:
    """自然线索:``natural=*``、公园/保护区、瀑布河流、观景点、岛屿。"""
    return bool(
        any(_flagged(tags, key) for key in NATURE_KEYS)
        or tags.get("leisure") in NATURE_LEISURE
        or tags.get("waterway") in NATURE_WATERWAY
        or tags.get("tourism") in NATURE_TOURISM
        or tags.get("place") in NATURE_PLACE
    )


def _is_ski(tags: dict[str, str]) -> bool:
    """滑雪场:``piste:*``、``ski=yes``、``sport=skiing``、冬季运动用地。"""
    if any(_flagged(tags, key) for key in SKI_TAG_KEYS):
        return True
    if any(_flagged(tags, key) for key in SKI_FLAG_KEYS):
        return True
    if tags.get("landuse") in SKI_LANDUSE:
        return True
    sports = [value for value in (tags.get("sport"), tags.get("sport:1"), tags.get("leisure"), tags.get("tourism")) if value]
    if any(value in SKI_SPORT_VALUES for value in sports):
        return True
    return any(hint in value for value in sports for hint in SKI_VALUE_HINTS)


def _is_sport(tags: dict[str, str]) -> bool:
    """运动:任意 ``sport=*``(非滑雪,滑雪已被更高优先级吃掉)或运动场地。"""
    return bool(_flagged(tags, "sport")) or tags.get("leisure") in SPORT_LEISURE


def _culture_reasons(tags: dict[str, str]) -> frozenset[str]:
    """人文美食线索分类:``historic`` / ``food`` / ``tourism`` / ``place``。"""
    reasons: set[str] = set()
    if _flagged(tags, "historic"):
        reasons.add("historic")
    if _flagged(tags, "cuisine") or tags.get("amenity") in FOOD_AMENITY:
        reasons.add("food")
    if tags.get("tourism") in CULTURE_TOURISM:
        reasons.add("tourism")
    if tags.get("place") in CULTURE_PLACE:
        reasons.add("place")
    return frozenset(reasons)


def _is_culture(tags: dict[str, str]) -> bool:
    """小城人文美食:古迹/景点/博物馆/餐厅/小城古镇。

    POC 修复点:地物带自然线索时,只有**明确的人文或美食线索**(``historic`` /
    ``cuisine`` / 餐厅类 ``amenity``)才算人文美食;单纯的 ``tourism=attraction``
    或 ``place=town`` 让给"自然风光",避免同一地物重复出现在两类。
    """
    reasons = _culture_reasons(tags)
    if not reasons:
        return False
    if _has_nature_clue(tags):
        return bool(reasons & {"historic", "food"})
    return True


CATEGORY_RULES: tuple[CategoryRule, ...] = (
    (CATEGORY_SKI, _is_ski),
    (CATEGORY_SPORT, _is_sport),
    (CATEGORY_CULTURE, _is_culture),
    (CATEGORY_NATURE, _has_nature_clue),
)


def categorize(tags: Optional[Mapping[str, Any]]) -> str:
    """给一组 OSM tags 归类:按 :data:`CATEGORY_PRIORITY` 首次命中即返回,认不出来返回"其他"。"""
    normalized = normalize_tags(tags)
    for category, matches in CATEGORY_RULES:
        if matches(normalized):
            return category
    return UNCATEGORIZED


classify_tags = categorize


def category_of(item: Mapping[str, Any]) -> str:
    """给一条候选地物(带 ``tags``)归类。"""
    return categorize(item.get("tags"))


# --------------------------------------------------------------------------- #
# 跨 tag 去重(键 = OSM type + id)
# --------------------------------------------------------------------------- #


def default_identity(item: Mapping[str, Any]) -> tuple[Any, ...]:
    """默认去重键:``(osm_type, osm_id)``;缺 OSM id 时退化成"类型+名字+坐标"。

    真实抓取走 ``with_id=True`` 一定有 OSM 身份;兜底分支保证纯构造的测试数据
    与 TASK-1c 的种子数据同样能去重。
    """
    osm_type = str(item.get("osm_type") or "").strip().lower()
    osm_id = item.get("osm_id")
    if osm_type in OSM_ELEMENT_TYPES and isinstance(osm_id, int) and not isinstance(osm_id, bool):
        return (osm_type, osm_id)
    return (osm_type or "unknown", str(item.get("name") or ""), item.get("lat"), item.get("lng"))


def dedupe_places(
    items: Iterable[Mapping[str, Any]],
    *,
    identity: Optional[IdentityFn] = None,
) -> list[dict[str, Any]]:
    """按去重键合并同一地物:保留首次出现的顺序,``tags`` 取并集(首次出现的值优先)。

    一次并集检索里,同一实体常被多组 tag 命中(如古镇同时有 ``historic=castle``
    与 ``tourism=attraction``),这里合成一行后再归类,归类看到的就是**全部线索**,
    优先级判定因此稳定。
    """
    key_of = identity or default_identity
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for item in items or []:
        key = key_of(item)
        row = merged.get(key)
        if row is None:
            merged[key] = dict(item)
            order.append(key)
            continue
        combined = dict(item.get("tags") or {})
        combined.update(row.get("tags") or {})
        row["tags"] = combined
        if not str(row.get("name") or "").strip():
            row["name"] = item.get("name") or ""
        for field in ("lat", "lng"):
            if row.get(field) is None:
                row[field] = item.get(field)
    return [merged[key] for key in order]


def classify_places(
    items: Iterable[Mapping[str, Any]],
    *,
    identity: Optional[IdentityFn] = None,
) -> list[dict[str, Any]]:
    """去重 + 归类:返回带 ``category`` 的地物列表(每个 OSM 实体一行、只归一类)。"""
    rows = dedupe_places(items, identity=identity)
    for row in rows:
        row["category"] = categorize(row.get("tags"))
    return rows


# --------------------------------------------------------------------------- #
# 检索线索:四分类 tag 并集(单次 Overpass 请求,每组独立配额)
# --------------------------------------------------------------------------- #

DEFAULT_ELEMENT_TYPES = "nwr"

SKI_SEARCH_TAGS: tuple[TagSelector, ...] = (
    {"piste:type": None},
    {"landuse": "winter_sports"},
    '["sport"~"^(ski|skiing|ski_jumping|snowboard|snowboarding|nordic|alpine|cross-country|freeride)$"]',
    '["ski"~"^(yes|true|1)$"]',
)
SPORT_SEARCH_TAGS: tuple[TagSelector, ...] = (
    {"sport": None},
    '["leisure"~"^(sports_centre|pitch|stadium|swimming_pool|golf_course|climbing|horse_riding|track|sports_hall|fitness_centre|water_park)$"]',
)
HERITAGE_SEARCH_TAGS: tuple[TagSelector, ...] = (
    {"historic": None},
    '["tourism"~"^(attraction|museum|gallery|artwork|theme_park|zoo)$"]',
)
TOWN_SEARCH_TAGS: tuple[TagSelector, ...] = (
    '["place"~"^(town|village|hamlet)$"]',
)
FOOD_SEARCH_TAGS: tuple[TagSelector, ...] = (
    '["amenity"~"^(restaurant|cafe|bar|pub|fast_food|food_court|biergarten|ice_cream)$"]',
    {"cuisine": None},
)
NATURE_SEARCH_TAGS: tuple[TagSelector, ...] = (
    '["natural"~"^(peak|hill|volcano|waterfall|water|wood|forest|beach|cave_entrance|spring|hot_spring|glacier|wetland|cliff|rock|stone|scrub|bay|cape|valley|ridge|dune|geyser)$"]',
    '["leisure"~"^(park|nature_reserve|garden|beach|forest)$"]',
    '["waterway"~"^(waterfall|rapids|spring|river|lake)$"]',
    {"tourism": "viewpoint"},
    '["place"~"^(island|islet)$"]',
)

# 每组独立配额:一次请求查完四分类并集,又不让某一类(如餐厅/村庄)挤掉其他类。
SEARCH_GROUPS: tuple[dict[str, Any], ...] = (
    {"category": CATEGORY_SKI, "group": "滑雪场", "budget": 80,
     "element_types": DEFAULT_ELEMENT_TYPES, "tags": SKI_SEARCH_TAGS},
    {"category": CATEGORY_SPORT, "group": "运动场所", "budget": 100,
     "element_types": DEFAULT_ELEMENT_TYPES, "tags": SPORT_SEARCH_TAGS},
    {"category": CATEGORY_CULTURE, "group": "人文古迹/景点", "budget": 120,
     "element_types": DEFAULT_ELEMENT_TYPES, "tags": HERITAGE_SEARCH_TAGS},
    {"category": CATEGORY_CULTURE, "group": "小城古镇", "budget": 40,
     "element_types": DEFAULT_ELEMENT_TYPES, "tags": TOWN_SEARCH_TAGS},
    {"category": CATEGORY_CULTURE, "group": "特色美食", "budget": 60,
     "element_types": DEFAULT_ELEMENT_TYPES, "tags": FOOD_SEARCH_TAGS},
    {"category": CATEGORY_NATURE, "group": "自然风光", "budget": 140,
     "element_types": DEFAULT_ELEMENT_TYPES, "tags": NATURE_SEARCH_TAGS},
)


def search_groups(categories: Optional[Sequence[str]] = None) -> list[dict[str, Any]]:
    """检索分组(可按分类过滤);每组含 ``tags``(Overpass 选择器并集)与 ``budget``。"""
    wanted = {str(item).strip() for item in categories} if categories else None
    groups = [
        {
            "category": group["category"],
            "group": group["group"],
            "budget": int(group["budget"]),
            "element_types": group["element_types"],
            "tags": list(group["tags"]),
        }
        for group in SEARCH_GROUPS
        if wanted is None or group["category"] in wanted
    ]
    return groups


def search_tags(categories: Optional[Sequence[str]] = None) -> list[TagSelector]:
    """四分类检索线索的**并集**(去重、保持顺序):一份 tag 列表一次查完。"""
    union: list[TagSelector] = []
    seen: set[str] = set()
    for group in search_groups(categories):
        for tag in group["tags"]:
            marker = str(tag) if isinstance(tag, str) else "|".join(f"{k}={v}" for k, v in sorted(tag.items()))
            if marker in seen:
                continue
            seen.add(marker)
            union.append(tag)
    return union


def search_budget(categories: Optional[Sequence[str]] = None) -> int:
    """一次并集检索的服务端取数上限(各组配额之和)。"""
    return sum(group["budget"] for group in search_groups(categories))
