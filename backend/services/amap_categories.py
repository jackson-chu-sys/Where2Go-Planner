"""高德 POI 的检索分组与四分类归类(TASK-9a)。

高德切源后,「四分类」的识别线索从 OSM tag(``natural=peak`` / ``amenity=restaurant`` …)
换成高德的 **typecode + type 字符串 + 名称关键词**。本模块提供两件事:

1. :data:`AMAP_TYPE_GROUPS` —— 检索组(与既有 :data:`services.classify.SEARCH_GROUPS`
   同形:``category`` / ``group`` / ``budget`` + 检索参数 ``types`` 或 ``keywords``),
   TASK-9b 的 :mod:`services.place_loader` 按组打 :func:`data_sources.amap.search_around` /
   :func:`data_sources.amap.search_polygon`,再用 :func:`classify_amap_poi` 在本地归类。
2. :func:`classify_amap_poi` / :func:`dedupe_key` —— 归类与去重口径。

**硬约束(照 ``docs/TASK-9-CONTRACT.md`` §1.4 / §6.4)**:

* 归类规则里出现的 typecode / type 字符串**只能是 §1.4 实测过的锚点**
  (``080106`` 滑雪场、``080113`` 台球厅、``080500`` 休闲场所、``080000`` 体育休闲服务场所、
  ``110101`` 公园、``110105`` 城市广场、``110000`` 风景名胜;旅游景点、``050100`` 中餐厅、
  ``050301`` 肯德基、``050500`` 咖啡厅),**禁凭记忆编造中类码**;其余情况只按
  「``types=`` 大类码粗筛 + 返回的 ``type`` 字符串 / 名称关键词」判。
* 优先级 **滑雪 > 运动 > 人文美食 > 自然**(沿用既有语义),判不出来 → ``"其他"``;
  叠加 :func:`dedupe_key`(高德 POI id 全局唯一)保证一个点只落一类。
* 「小城古镇」组**不能用村庄码**:``190106`` 实测杭州周边 0 命中 → 改用
  ``keywords="古镇|老街|古城"``(配 ``city`` 参数,由 TASK-9b 传)。
* ``110000``(风景名胜)大类里既有公园广场也有山水景区,所以按 type 字符串 + 名称
  关键词二分:**公园/广场/古迹/博物馆/寺观 → 人文美食;山/湖/森林/湿地/瀑布等 → 自然**。
  (与 OSM 时代「``leisure=park`` 归自然」略有差异,是本次切源拍板的口径。)
  复合专名以**更具体的自然保护地**为准:「湿地公园/森林公园/国家公园/地质公园/
  自然保护区/风景名胜区」即使带「公园」二字也归自然(:func:`is_nature_reserve`),
  且只在风景名胜大类内生效(「森林公园餐厅」仍归人文美食)。

配额语义:各组 ``budget`` 直接沿用既有 :data:`services.classify.SEARCH_GROUPS` 的数值
(滑雪 80 / 运动 100 / 人文美食 = 人文古迹 120 + 特色美食 60 = 180 / 小城古镇 40 /
自然 140,合计 540 不变),TASK-9b 的渐进配额与递减口径因此零改动。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from db.models import UNCATEGORIZED

CATEGORY_NATURE = "自然风光"
CATEGORY_CULTURE = "小城人文美食"
CATEGORY_SKI = "滑雪场"
CATEGORY_SPORT = "运动"

# --------------------------------------------------------------------------- #
# typecode / type 字符串锚点(§1.4 实测原文,**勿凭记忆加中类码**)
# --------------------------------------------------------------------------- #

#: 滑雪场(实测崇礼 太舞/雪如意/密苑云顶/林语山谷 都命中这个码;``080115`` 实测 0 命中)
SKI_TYPECODE = "080106"
#: 体育休闲服务:``080113`` 台球厅 · ``080500`` 休闲场所 · ``080000`` 体育休闲服务场所
SPORT_TYPECODES: tuple[str, ...] = ("080000", "080113", "080500")
#: 体育休闲服务**大类**前码(``types=080000`` 粗筛后返回的中类码都以 08 开头)
SPORT_TYPECODE_PREFIX = "08"
#: 风景名胜大类里明确属于人文的:``110101`` 公园 · ``110105`` 城市广场
CULTURE_SCENIC_TYPECODES: tuple[str, ...] = ("110101", "110105")
#: 风景名胜**大类**前码(``110000`` 风景名胜相关;旅游景点)
SCENIC_TYPECODE_PREFIX = "11"
#: 餐饮服务:``050100`` 中餐厅 · ``050301`` 肯德基 · ``050500`` 咖啡厅
FOOD_TYPECODES: tuple[str, ...] = ("050100", "050301", "050500")
FOOD_TYPECODE_PREFIX = "05"

SKI_TYPE_TEXTS: tuple[str, ...] = ("滑雪场", "滑雪")
SPORT_TYPE_TEXTS: tuple[str, ...] = ("体育休闲服务", "运动场馆", "台球厅", "休闲场所")
CULTURE_TYPE_TEXTS: tuple[str, ...] = (
    "公园", "城市广场", "餐饮服务", "中餐厅", "肯德基", "咖啡厅",
)
NATURE_TYPE_TEXTS: tuple[str, ...] = ("风景名胜", "旅游景点")

# --------------------------------------------------------------------------- #
# 名称关键词(只在 typecode 判不出时兜底;优先级仍是 滑雪>运动>人文美食>自然)
# --------------------------------------------------------------------------- #

SKI_NAME_HINTS: tuple[str, ...] = ("滑雪", "雪场", "雪道", "冰雪乐园")
SPORT_NAME_HINTS: tuple[str, ...] = (
    "体育馆", "体育场", "体育中心", "运动", "游泳", "健身", "篮球", "足球", "网球",
    "羽毛球", "乒乓", "攀岩", "高尔夫", "马术", "滑冰", "轮滑", "台球", "射击", "赛马",
)
CULTURE_NAME_HINTS: tuple[str, ...] = (
    "古镇", "古城", "古村", "古街", "老街", "步行街", "博物馆", "纪念馆", "美术馆",
    "故居", "遗址", "城墙", "牌坊", "寺", "庙", "祠", "陵", "园林", "剧院", "书店",
    "公园", "广场", "动物园", "植物园", "美食", "小吃", "餐厅", "饭店", "咖啡", "茶馆",
)
#: 自然保护地专名:名字里出现这些词时,即使带「公园」二字也算**自然**
#: (「湿地公园」「森林公园」的语义重心在湿地/森林,契约把森林/湿地明列为自然)
NATURE_RESERVE_HINTS: tuple[str, ...] = (
    "森林公园", "湿地公园", "国家公园", "地质公园", "自然保护区", "自然保护地",
    "风景名胜区",
)
NATURE_NAME_HINTS: tuple[str, ...] = (
    "山", "峰", "岭", "崖", "湖", "水库", "瀑布", "溪", "峡", "谷", "泉", "河", "江",
    "森林", "林海", "湿地", "草原", "冰川", "溶洞", "滩", "岛", "自然保护区", "地质公园",
    "风景", "景区", "观景", "自然",
)
#: 明显的人工/园区设施:名字**以这些词结尾**(或就是这个词)时一律归「其他」,
#: 用后缀而不是包含,免得把「大门山景区」这类真地名误杀。
FACILITY_NAME_SUFFIXES: tuple[str, ...] = (
    "指示牌", "标示牌", "大门", "停车场", "停车区", "出入口", "入口", "出口",
    "检票口", "售票处", "管理处", "厕所", "卫生间", "公厕", "垃圾桶", "充电桩", "站台",
)

# --------------------------------------------------------------------------- #
# 检索分组(与 services.classify.SEARCH_GROUPS 同形;TASK-9b 按组打高德)
# --------------------------------------------------------------------------- #

AMAP_TYPE_GROUPS: tuple[dict[str, Any], ...] = (
    {"category": CATEGORY_SKI, "group": "滑雪场", "budget": 80,
     "types": (SKI_TYPECODE,), "keywords": None},
    {"category": CATEGORY_SPORT, "group": "运动场所", "budget": 100,
     "types": ("080000",), "keywords": None},
    {"category": CATEGORY_CULTURE, "group": "人文美食", "budget": 180,
     "types": ("110000", "050000"), "keywords": None},
    # §6.4:村庄码 190106 实测 0 命中 → 小城古镇改走关键词(配 city 参数)
    {"category": CATEGORY_CULTURE, "group": "小城古镇", "budget": 40,
     "types": (), "keywords": "古镇|老街|古城"},
    {"category": CATEGORY_NATURE, "group": "自然风光", "budget": 140,
     "types": ("110000",), "keywords": None},
)

#: 运动组要在本地把滑雪场排除掉(``types=080000`` 会把 ``080106`` 一起捞回来)
SPORT_EXCLUDED_TYPECODES: tuple[str, ...] = (SKI_TYPECODE,)


def _text(value: Any) -> str:
    """高德字段收敛成字符串(``None`` / 空数组 → ``""``),与 :mod:`data_sources.amap` 同口径。"""
    if value is None or isinstance(value, (list, dict, tuple)):
        return ""
    return str(value).strip()


def _hits(text: str, hints: tuple[str, ...]) -> bool:
    return any(hint in text for hint in hints)


def is_facility_name(name: str) -> bool:
    """名字是否是明显的人工/园区设施(指示牌 / 大门 / 停车场 …)→ 归「其他」。"""
    text = name.strip()
    if not text:
        return False
    return any(text == suffix or text.endswith(suffix) for suffix in FACILITY_NAME_SUFFIXES)


def is_ski(name: str, typecode: str, type_text: str) -> bool:
    """滑雪场判定:``080106`` / type 串含「滑雪场」/ 名字含「滑雪」等。"""
    return (
        typecode == SKI_TYPECODE
        or _hits(type_text, SKI_TYPE_TEXTS)
        or _hits(name, SKI_NAME_HINTS)
    )


def is_sport(name: str, typecode: str, type_text: str) -> bool:
    """运动场所判定:``08`` 大类(排除 ``080106`` 滑雪场)/ type 串 / 名称关键词。"""
    if typecode in SPORT_EXCLUDED_TYPECODES:
        return False
    if typecode.startswith(SPORT_TYPECODE_PREFIX) or typecode in SPORT_TYPECODES:
        return True
    return _hits(type_text, SPORT_TYPE_TEXTS) or _hits(name, SPORT_NAME_HINTS)


def is_nature_reserve(name: str, typecode: str) -> bool:
    """是否是自然保护地专名(湿地/森林/国家/地质公园、自然保护区、风景名胜区)。

    只在**风景名胜大类**(``11`` 前码,或 typecode 缺失只能靠名字判)时才生效:
    「森林公园餐厅」这种餐饮 POI 仍归人文美食,不被名字带偏。
    """
    if typecode.startswith(FOOD_TYPECODE_PREFIX):
        return False
    if typecode and not typecode.startswith(SCENIC_TYPECODE_PREFIX):
        return False
    return _hits(name, NATURE_RESERVE_HINTS)


def is_culture(name: str, typecode: str, type_text: str) -> bool:
    """小城人文美食判定:餐饮 ``05`` 大类 / 公园广场 ``110101``·``110105`` / 人文关键词。

    自然保护地专名(见 :func:`is_nature_reserve`)优先级高于「公园/广场」这类人文关键词,
    所以「西溪国家湿地公园」算自然,而「太子湾公园」「武林广场」算人文。
    """
    if typecode.startswith(FOOD_TYPECODE_PREFIX) or typecode in FOOD_TYPECODES:
        return True
    if is_nature_reserve(name, typecode):
        return False
    if typecode in CULTURE_SCENIC_TYPECODES:
        return True
    return _hits(type_text, CULTURE_TYPE_TEXTS) or _hits(name, CULTURE_NAME_HINTS)


def is_nature(name: str, typecode: str, type_text: str) -> bool:
    """自然风光判定:风景名胜 ``11`` 大类 / type 串 / 山水林湿等名称关键词。"""
    if typecode.startswith(SCENIC_TYPECODE_PREFIX):
        return True
    return _hits(type_text, NATURE_TYPE_TEXTS) or _hits(name, NATURE_NAME_HINTS)


def classify_amap_poi(poi: Mapping[str, Any]) -> str:
    """一条归一化高德 POI → 四分类之一(判不出来 → ``"其他"``)。

    优先级 **滑雪 > 运动 > 人文美食 > 自然**;明显的人工设施名(指示牌/大门/停车场)
    优先级最高,直接归「其他」——它们是高德把景区内部设施也当 POI 收录的噪声。

    入参是 :func:`data_sources.amap.parse_poi` 的归一化 dict(也接受原始高德 POI),
    只读 ``name`` / ``typecode`` / ``type`` 三个字段。
    """
    name = _text(poi.get("name"))
    typecode = _text(poi.get("typecode"))
    type_text = _text(poi.get("type"))
    if not name and not typecode and not type_text:
        return UNCATEGORIZED

    if is_facility_name(name):
        return UNCATEGORIZED
    if is_ski(name, typecode, type_text):
        return CATEGORY_SKI
    if is_sport(name, typecode, type_text):
        return CATEGORY_SPORT
    if is_culture(name, typecode, type_text):
        return CATEGORY_CULTURE
    if is_nature(name, typecode, type_text):
        return CATEGORY_NATURE
    return UNCATEGORIZED


def dedupe_key(poi: Mapping[str, Any]) -> tuple[str, str]:
    """去重键:``("amap", <高德 POI id>)`` —— id 全局唯一,同实体跨大类只归一类。

    存量 OSM 行的键仍是 ``(osm_type, osm_id)``,两者天然不撞;
    TASK-9b 入库时 ``Place.osm_type`` 也写 ``"amap"``。
    """
    return ("amap", _text(poi.get("id")))
