"""住宿检索 + 价格估算(TASK-3a1,阶段3a **服务层**;路由与前端由 TASK-3a2 接)。

为什么单独一张表而不复用 ``Place``:住宿是行程的**落脚点**,不进需求四分类,展示要的
是价格区间估算而不是分类标签,所以落 :class:`db.models.Stay`,唯一键 ``(osm_type, osm_id)``
—— 同一家酒店从不同起点搜到只存一行(详见该表 docstring)。

三段式(照 :mod:`services.place_loader` 的结构,只是范围缩到"一个坐标 + 半径"):

* :func:`search_stays` —— **高德** ``place/around`` 检索(TASK-9b):``types=100000``
  (住宿服务**大类**粗筛)按由近及远翻页,单查询 200 条硬上限(:data:`GROUP_BUDGET`,
  8 页 × 25 条),某页没拿满就停;具体 ``kind`` 在本地按返回的 ``type`` 中文串判
  (:func:`stay_kind`,**不凭记忆编造中类码**),入库身份是 ``osm_type="amap"`` +
  ``osm_id = crc32(高德 POI id)``(:func:`db.models.amap_osm_id`,原文存 ``tags["amap_id"]``,
  表结构零改动)。任何失败(无 key、限流、响应格式不对)一律降级成**空列表**,不抛给调用方。
* :func:`estimate_price` —— LLM 估价 + 一句话简介,**一次调用出两行**
  (``价格: 约¥A-B/晚`` / ``简介: <40字内>``):比拆两次调用省一半 token 与限流额度。
  复用 :class:`services.intro.LLMClient`(Provider 可切换、key 只读环境变量),
  未配 key / 超时 / 限流 / 格式不对 → ``("", "")``,**绝不抛异常、绝不阻塞入库**
  (与 :func:`services.intro.generate_intro` 同口径)。价格是**估算**:规范串恒带"约",
  ``currency`` 默认 ``CNY``,序列化时另给 ``price_is_estimate`` 标注(架构文档"AI 幻觉"
  对策:事实字段绑结构化来源,估算字段必须自带标注)。
* :func:`load_or_fetch_stays` —— **DB 即缓存**:该坐标半径内已有 ≥
  :data:`MIN_CACHED_ROWS` 行就直接读库返回(``source="db"``),否则检索 → upsert →
  只给**缺价格**的行调 LLM → 落库(``source="amap"``);``refresh=True`` 强制重抓。
  已有 ``price_estimate`` 的行**永不再调 LLM**,与 ``Place.intro`` 的缓存口径一致。

TASK-6c 在这个骨架上加了三件事(BUG-3/5):

* **负缓存**:空结果与检索失败都落一行 :class:`db.models.StayQueryCache`,
  :data:`NEG_CACHE_TTL_S`(6 小时)内同坐标同半径**直接回缓存态、不再触网**;
  过期即重查。缓存态带三档 ``reason`` —— ``no_data``(真的没有)/
  ``datasource_error``(高德报错)/ ``timeout``(超时),API 原样透传给前端分文案。
* **半径阶梯** :data:`STAY_RADIUS_LADDER_M`(5→10→30 km):调用方**没显式给半径**时,
  小半径空结果就逐级扩大再查,扩到有结果即停,并给 ``nearest_km``(最近一家的 haversine
  距离,1 位小数);显式给了半径就**只查那一档**(不擅自扩,尊重调用方口径)。
  检索**失败**不扩档 —— 数据源已经挂了,再打两遍只是白等。
* **估价异步回填**:检索入库后就能返回列表(``price_estimate`` 可为 null)。待估价的行数
  超过一批(:data:`PRICE_BATCH_SIZE` = 5 家)时不再阻塞请求,而是丢给后台线程**批量**
  回填(5 家一个 prompt、固定 ``qwen3.8-max`` 的 token-plan 注册项、单批重试 ≤ 1 次、
  解析不出就留 null 不抛),响应带 ``estimating=True``;一批以内仍就地算完,首屏即有价格。
  线程池口径照 :func:`services.intro._run_batch`,执行器可注入(测试/CLI 用同步执行器)。

TASK-6g 在估价路径最前面又插了一层(**规则层,0 token**):

* :data:`BRAND_PRICE_BANDS` 品牌价格带表(≥25 个连锁品牌,**中英文别名都收**、大小写不敏感、
  包含匹配、最长别名优先)+ ``hotel:stars``/``stars`` 星级档 + 类型兜底档
  (:data:`KIND_PRICE_BANDS`,hostel/guest_house/chalet/apartment),命中就直接出
  ``约¥A-B/晚``;再按 :data:`CITY_TIER_FACTOR` 做城市线级修正(一线 ×1.2、新一线 ×1.05、
  其他城市 ×0.9,认不出城市 ×1.0),取整到 :data:`PRICE_BAND_STEP` 元。
* 判档优先级 **品牌 > 星级 > 类型**(品牌与星级同时命中取品牌档)。命中规则的行标
  ``price_kind="rule"``,**一次 LLM 都不调**;规则未命中的行才进既有的批量 LLM
  (5 家/prompt、``qwen3.8-max``),标 ``price_kind="llm"``;两边都拿不到就仍是 null。
* 规则产物与 LLM 产物同口径**永久缓存**:``Stay.price_estimate``/``price_kind`` 一旦写下,
  重抓不覆盖(规则表日后调价也不会重算已入库的行,与 ``Place.intro`` 一致)。

CLI(给夜间预抓/排查用,联网)::

    python -m services.stays 31.2304 121.4737 --radius 8000 --limit 10
"""

from __future__ import annotations

import argparse
import math
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from data_sources import DataSourceError
from data_sources import amap
from data_sources import haversine_km
from db.models import (
    COORD_PRECISION,
    CURRENCY_LEN,
    DEFAULT_CURRENCY,
    KIND_LEN,
    NAME_LEN,
    PRICE_KIND_LEN,
    PRICE_KIND_LLM,
    PRICE_KIND_RULE,
    PRICE_KINDS,
    PRICE_LEN,
    REASON_DATASOURCE_ERROR,
    REASON_NO_DATA,
    REASON_TIMEOUT,
    AMAP_OSM_TYPE,
    AMAP_SOURCE,
    STAY_CACHE_EMPTY,
    STAY_REASON_LEN,
    Stay,
    StayQueryCache,
    amap_osm_id,
    clean_text,
    iso_utc,
    stay_cache_kind,
    stay_cache_reason,
    utcnow,
)
from services.intro import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MODEL,
    INTRO_TARGET_CHARS,
    LLMClient,
    ResolvedLLM,
    clean_intro,
    find_provider,
)
from services import amap_categories
from services.intro import default_client as default_llm_client

# 住宿类型(**值即 kind**):OSM 时代是 ``tourism=*`` 的取值,高德时代由
# :func:`stay_kind` 从返回的中文 ``type``/``name`` 归一到同一批值(前端/规则表口径不变)
STAY_TAGS: tuple[str, ...] = ("hotel", "guest_house", "hostel", "apartment", "chalet")
# 高德检索参数(TASK-9b,§1.4 实测锚点:``100000`` 住宿服务大类,
# ``100104`` 三星级宾馆 / ``100105`` 经济型连锁酒店)。**只用大类码粗筛**,
# 细分 kind 在本地按返回的 ``type`` 字符串判 —— 契约明令禁止凭记忆编造中类码。
STAY_TYPES = "100000"
STAY_TYPECODE_PREFIX = "10"
# 中文 type/name → kind 的关键词表(顺序 = 判定优先级,先具体后宽泛)
AMAP_KIND_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hostel", ("青年旅舍", "青旅", "背包客栈")),
    ("guest_house", ("民宿", "客栈", "家庭旅馆", "农家院", "民居")),
    ("apartment", ("公寓",)),
    ("chalet", ("度假", "别墅", "小屋", "山庄", "木屋", "营地")),
    ("hotel", ("宾馆", "酒店", "旅馆", "招待所", "饭店", "住宿服务", "客栈酒店")),
)
DEFAULT_RADIUS_M = 8000
LAT_LIMIT = 90.0
LNG_LIMIT = 180.0
# 服务端配额:住宿密度低,200 条足够 —— 恰好是高德**单查询**的硬上限(8 页 × 25 条)
GROUP_BUDGET = amap.MAX_ROWS_PER_QUERY
# 客户端 HTTP 超时:住宿检索是**交互路径**,不走 place_loader 那档冷启动超时
STAY_REQUEST_TIMEOUT_S = 30.0
DISTANCE_PRECISION = 2
# DB 即缓存:半径内已有这么多行就不再触网(refresh=True 可强制重抓)
MIN_CACHED_ROWS = 1
SOURCE_DB = "db"
SOURCE_FETCH = "amap"
METERS_PER_DEGREE = 111_320.0
# 高纬度兜底:cos(lat) 太小时经度包围盒会炸开,夹一个下限保证 SQL 粗筛仍收敛
MIN_COS_LAT = 0.05

# --- TASK-6c:负缓存 / 半径阶梯 / 后台批量估价 --- #
# 负缓存有效期:6 小时内同坐标同半径不再触网(空结果与失败都记)
NEG_CACHE_TTL_S = 6 * 3600
# 半径阶梯:调用方未显式给半径时,5 km 空 → 10 km → 30 km,扩到有结果即停
STAY_RADIUS_LADDER_M: tuple[int, ...] = (5000, 10000, 30000)
# ``nearest_km`` 的口径:最近一家的 haversine 距离,保留 1 位(给"最近的在 X km 外"提示)
NEAREST_KM_PRECISION = 1
# LLM 批量估价:每批 5 家一个 prompt(神朱 2026-09-28 定),单批重试不超过 1 次
PRICE_BATCH_SIZE = 5
PRICE_BATCH_RETRIES = 1
# 批量输出比单家长得多,token 预算与超时按调用放宽(照 intro.chat 的口径,不放开会被截断)
PRICE_BATCH_MAX_TOKENS = 1200
PRICE_BATCH_TIMEOUT_S = 90.0
# 后台回填线程数:估价是限流敏感路径,2 条足够,不与 place 简介抢额度
PRICE_BACKGROUND_WORKERS = 2
# 待估价行数 ≤ 这个值(= 一批)就地算完再返回;超过才转后台(首屏价格 vs. 请求不被拖死的折中)
SYNC_ESTIMATE_MAX_ROWS = PRICE_BATCH_SIZE
# 批量估价固定用注册表里的 qwen(token-plan 入口,qwen3.8-max):神朱定,**不做双模型**
PRICE_PROVIDER_NAME = "qwen"
PRICE_MODEL = "qwen3.8-max"
# 估价口径开关(``estimate=`` 参数):auto = 按待估行数自动选
ESTIMATE_AUTO = "auto"
ESTIMATE_SYNC = "sync"
ESTIMATE_ASYNC = "async"
ESTIMATE_OFF = "off"
ESTIMATE_MODES: tuple[str, ...] = (ESTIMATE_AUTO, ESTIMATE_SYNC, ESTIMATE_ASYNC, ESTIMATE_OFF)
# 超时判定的文案线索:``data_sources._common`` 把 requests.Timeout 包成"请求超时(>20s)"
TIMEOUT_HINTS: tuple[str, ...] = ("请求超时", "超时(>", "timed out", "timeout")

# --- TASK-6g:规则估价层(**0 token**,命中即出区间;未命中才走既有批量 LLM) --- #
# 为什么规则前置:连锁品牌与星级的房价带是**公开常识**,不必花 token 问模型;只有
# "没品牌、没星级、类型也不在兜底表里"的行才交给 LLM(神朱 2026-09-29 定)。
# 档位口径(元/晚,全国典型价):经济 / 中档 / 高档 / 奢华,再乘城市线级系数。
PRICE_TIER_ECONOMY = "economy"
PRICE_TIER_MIDSCALE = "midscale"
PRICE_TIER_UPSCALE = "upscale"
PRICE_TIER_LUXURY = "luxury"
PRICE_TIERS: tuple[str, ...] = (
    PRICE_TIER_ECONOMY, PRICE_TIER_MIDSCALE, PRICE_TIER_UPSCALE, PRICE_TIER_LUXURY,
)
# 奢华档规范写的是"约¥1200+":落区间串必须给上界,取 3000(安缦/宝格丽这类天花板)
PRICE_TIER_BANDS: dict[str, tuple[int, int]] = {
    PRICE_TIER_ECONOMY: (150, 300),
    PRICE_TIER_MIDSCALE: (300, 550),
    PRICE_TIER_UPSCALE: (600, 1200),
    PRICE_TIER_LUXURY: (1200, 3000),
}

# 品牌 → 档位(**中英文别名都收**)。匹配口径见 :func:`brand_token`:大小写不敏感 +
# 去空白/连字符/标点后**包含**匹配,命中**最长**别名优先 —— 所以 "Park Hyatt"(奢华)
# 不会被 "Hyatt"(高档)抢先,"Holiday Inn Express"(中档)与 "Crowne Plaza"(高档)
# 也互不误伤(两者互不包含)。
BRAND_TIERS: tuple[tuple[str, str], ...] = (
    # 经济型(约 ¥150-300)
    ("汉庭", PRICE_TIER_ECONOMY), ("hanting", PRICE_TIER_ECONOMY),
    ("如家", PRICE_TIER_ECONOMY), ("home inn", PRICE_TIER_ECONOMY),
    ("7天", PRICE_TIER_ECONOMY), ("7 days inn", PRICE_TIER_ECONOMY),
    ("7 days", PRICE_TIER_ECONOMY),
    ("锦江之星", PRICE_TIER_ECONOMY), ("jinjiang inn", PRICE_TIER_ECONOMY),
    ("城市便捷", PRICE_TIER_ECONOMY), ("city comfort", PRICE_TIER_ECONOMY),
    ("格林豪泰", PRICE_TIER_ECONOMY), ("greentree", PRICE_TIER_ECONOMY),
    ("速8", PRICE_TIER_ECONOMY), ("super 8", PRICE_TIER_ECONOMY),
    ("莫泰", PRICE_TIER_ECONOMY), ("motel 168", PRICE_TIER_ECONOMY),
    ("海友", PRICE_TIER_ECONOMY), ("hi inn", PRICE_TIER_ECONOMY),
    ("怡莱", PRICE_TIER_ECONOMY), ("elan", PRICE_TIER_ECONOMY),
    ("尚客优", PRICE_TIER_ECONOMY), ("thank inn", PRICE_TIER_ECONOMY),
    ("布丁", PRICE_TIER_ECONOMY), ("pod inn", PRICE_TIER_ECONOMY),
    ("99旅馆", PRICE_TIER_ECONOMY), ("99 inn", PRICE_TIER_ECONOMY),
    # 中档(约 ¥300-550)
    ("全季", PRICE_TIER_MIDSCALE), ("ji hotel", PRICE_TIER_MIDSCALE),
    ("亚朵", PRICE_TIER_MIDSCALE), ("atour", PRICE_TIER_MIDSCALE),
    ("维也纳", PRICE_TIER_MIDSCALE), ("vienna", PRICE_TIER_MIDSCALE),
    ("桔子", PRICE_TIER_MIDSCALE), ("orange", PRICE_TIER_MIDSCALE),
    ("麗枫", PRICE_TIER_MIDSCALE), ("丽枫", PRICE_TIER_MIDSCALE),
    ("lavande", PRICE_TIER_MIDSCALE),
    ("智选假日", PRICE_TIER_MIDSCALE), ("holiday inn express", PRICE_TIER_MIDSCALE),
    ("citigo", PRICE_TIER_MIDSCALE),
    ("美居", PRICE_TIER_MIDSCALE), ("mercure", PRICE_TIER_MIDSCALE),
    ("诺富特", PRICE_TIER_MIDSCALE), ("novotel", PRICE_TIER_MIDSCALE),
    ("宜必思", PRICE_TIER_MIDSCALE), ("ibis", PRICE_TIER_MIDSCALE),
    ("星程", PRICE_TIER_MIDSCALE),
    ("丽呈", PRICE_TIER_MIDSCALE), ("麗呈", PRICE_TIER_MIDSCALE),
    # 高档(约 ¥600-1200)
    ("希尔顿", PRICE_TIER_UPSCALE), ("hilton", PRICE_TIER_UPSCALE),
    ("万豪", PRICE_TIER_UPSCALE), ("marriott", PRICE_TIER_UPSCALE),
    ("喜来登", PRICE_TIER_UPSCALE), ("sheraton", PRICE_TIER_UPSCALE),
    ("洲际", PRICE_TIER_UPSCALE), ("intercontinental", PRICE_TIER_UPSCALE),
    ("凯悦", PRICE_TIER_UPSCALE), ("hyatt", PRICE_TIER_UPSCALE),
    ("香格里拉", PRICE_TIER_UPSCALE), ("shangri-la", PRICE_TIER_UPSCALE),
    ("皇冠假日", PRICE_TIER_UPSCALE), ("crowne plaza", PRICE_TIER_UPSCALE),
    ("雅高", PRICE_TIER_UPSCALE), ("accor", PRICE_TIER_UPSCALE),
    ("索菲特", PRICE_TIER_UPSCALE), ("sofitel", PRICE_TIER_UPSCALE),
    ("威斯汀", PRICE_TIER_UPSCALE), ("westin", PRICE_TIER_UPSCALE),
    ("万怡", PRICE_TIER_UPSCALE), ("courtyard", PRICE_TIER_UPSCALE),
    ("万丽", PRICE_TIER_UPSCALE), ("renaissance", PRICE_TIER_UPSCALE),
    ("凯宾斯基", PRICE_TIER_UPSCALE), ("kempinski", PRICE_TIER_UPSCALE),
    ("福朋", PRICE_TIER_UPSCALE), ("four points", PRICE_TIER_UPSCALE),
    ("铂尔曼", PRICE_TIER_UPSCALE), ("pullman", PRICE_TIER_UPSCALE),
    ("美爵", PRICE_TIER_UPSCALE), ("grand mercure", PRICE_TIER_UPSCALE),
    # 奢华(约 ¥1200+)
    ("丽思卡尔顿", PRICE_TIER_LUXURY), ("ritz-carlton", PRICE_TIER_LUXURY),
    ("宝格丽", PRICE_TIER_LUXURY), ("bulgari", PRICE_TIER_LUXURY),
    ("安缦", PRICE_TIER_LUXURY), ("aman", PRICE_TIER_LUXURY),
    ("华尔道夫", PRICE_TIER_LUXURY), ("waldorf", PRICE_TIER_LUXURY),
    ("柏悦", PRICE_TIER_LUXURY), ("park hyatt", PRICE_TIER_LUXURY),
    ("瑞吉", PRICE_TIER_LUXURY), ("st. regis", PRICE_TIER_LUXURY),
    ("半岛", PRICE_TIER_LUXURY), ("peninsula", PRICE_TIER_LUXURY),
    ("四季酒店", PRICE_TIER_LUXURY), ("four seasons", PRICE_TIER_LUXURY),
    ("文华东方", PRICE_TIER_LUXURY), ("mandarin oriental", PRICE_TIER_LUXURY),
    ("悦榕庄", PRICE_TIER_LUXURY), ("banyan tree", PRICE_TIER_LUXURY),
    ("松赞", PRICE_TIER_LUXURY), ("songtsam", PRICE_TIER_LUXURY),
    ("君悦", PRICE_TIER_LUXURY), ("grand hyatt", PRICE_TIER_LUXURY),
    ("瑰丽", PRICE_TIER_LUXURY), ("rosewood", PRICE_TIER_LUXURY),
    ("丽晶", PRICE_TIER_LUXURY), ("艾迪逊", PRICE_TIER_LUXURY),
)

# 品牌价格带表:别名 → ``(低, 高)`` 元/晚(:data:`BRAND_TIERS` × :data:`PRICE_TIER_BANDS`
# 摊平而来)。匹配用的是**归一后**的 :data:`BRAND_ALIAS_BANDS`(按别名长度倒序,最长优先)。
BRAND_PRICE_BANDS: dict[str, tuple[int, int]] = {
    alias: PRICE_TIER_BANDS[tier] for alias, tier in BRAND_TIERS
}

# 星级档(``hotel:stars`` / ``stars``):1-2 星 / 3 星 / 4 星 / 5 星
STARS_PRICE_BANDS: dict[int, tuple[int, int]] = {
    1: (100, 250),
    2: (100, 250),
    3: (250, 450),
    4: (450, 900),
    5: (900, 2000),
}
STARS_TAGS: tuple[str, ...] = ("stars", "hotel:stars", "stars:hotel")
# OSM 的星级写法五花八门(``4`` / ``4*`` / ``四星`` / ``S4``),中文数字也认
CN_STAR_DIGITS: dict[str, int] = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}
# 高德没有 ``stars`` tag,星级写在中文 ``type`` 里("住宿服务;宾馆酒店;三星级宾馆")
AMAP_STARS_RE = re.compile(r"([一二两三四五1-5])\s*星")

# 类型兜底档(**没品牌也没星级**时才用)。``hotel`` 刻意不在表里:"酒店"这一类的房价带
# 太宽(100 到 3000 都有),规则乱猜不如让 LLM 看名称猜 —— 兜底只给窄口径的四种类型。
KIND_PRICE_BANDS: dict[str, tuple[int, int]] = {
    "hostel": (50, 150),  # 床位价
    "guest_house": (200, 500),
    "chalet": (200, 500),
    "apartment": (300, 800),
}

# 城市线级修正系数:一线 ×1.2、新一线 ×1.05、其他城市 ×0.9
CITY_TIER_FIRST = "tier1"
CITY_TIER_NEW_FIRST = "new_tier1"
CITY_TIER_OTHER = "other"
CITY_TIER_FACTOR: dict[str, float] = {
    CITY_TIER_FIRST: 1.2,
    CITY_TIER_NEW_FIRST: 1.05,
    CITY_TIER_OTHER: 0.9,
}
# 认不出城市时的系数:中性 1.0 —— 既不打折也不加价,保持档位表原值。
# OSM 大量住宿行没有 ``addr:city``,把"缺标签"当成"低线城市"会系统性压价。
CITY_TIER_UNKNOWN_FACTOR = 1.0
TIER1_CITIES: tuple[str, ...] = ("北京", "上海", "广州", "深圳")
NEW_TIER1_CITIES: tuple[str, ...] = (
    "杭州", "成都", "武汉", "南京", "苏州", "重庆", "西安", "长沙", "天津", "郑州",
    "东莞", "青岛", "合肥", "佛山", "宁波", "无锡", "福州", "厦门", "济南", "大连",
    "沈阳", "昆明", "南昌", "贵阳", "太原", "石家庄", "哈尔滨", "长春", "南宁", "温州",
    "常州", "泉州", "嘉兴", "南通", "惠州", "徐州", "绍兴", "中山", "台州", "兰州",
    "烟台", "潍坊", "保定", "洛阳",
)
KNOWN_TIER_CITIES: tuple[str, ...] = TIER1_CITIES + NEW_TIER1_CITIES
# 城市线索:先看地址 tag,再从名称里捞(``上海虹桥康得思酒店`` 这种写法很常见)
CITY_TAGS: tuple[str, ...] = (
    "addr:city", "addr:town", "addr:municipality", "addr:district", "addr:province", "city",
    # 高德行的城市线索(:func:`stay_tags`):``cityname`` 是地级市名("杭州市"),
    # 正好对上线级表;``adname``(区县)刻意不收 —— 它不在城市表里会把一线城市压成 0.9。
    "cityname",
)
# 系数乘完取整到 5 元:区间好看,也不会把 150×1.05=157.5 这种尾数塞给用户
PRICE_BAND_STEP = 5
# 品牌别名归一要去掉的字符(空白/连字符/点/引号/括号…);大小写另算
BRAND_NOISE_RE = re.compile(r"[\s\-_.·、,，'\"“”‘’()（）\[\]]+")
# 回填统计里 ``provider`` 的口径:全靠规则表填完(没花 token)时用它,而不是"未配置"
RULE_PROVIDER_LABEL = "规则表(0 token)"

# 进 prompt 的住宿标签白名单(房价线索优先;上限 STAY_FACT_LIMIT 个,不塞整包 tag)
STAY_FACT_TAGS: tuple[str, ...] = (
    # "type"/"cityname"/"address" 是高德行的事实线索(中文分类原文 + 城市 + 地址),
    # 排在 OSM tag 之后、其余线索之前;白名单外的键仍不进 prompt。
    "tourism", "stars", "brand", "operator", "type", "cityname", "address", "rooms", "beds",
    "internet_access", "wheelchair", "addr:city", "addr:street", "opening_hours", "website",
)
STAY_FACT_LIMIT = 8
STAY_SYSTEM_PROMPT = (
    "你是 Where2Go(周末去哪儿玩)的住宿信息助手,为行程落脚点写**价格区间估算**与一句话简介。"
    "严格按用户要求的两行输出,不要编号、标题、解释或多余文字:"
    "第一行 `价格: 约¥A-B/晚`(人民币、一晚的大致区间,必须带“约”字;不确定就给宽一点的区间);"
    "第二行 `简介: <40字内一句话>`。"
    "价格只能依据名称、住宿类型、星级/房量等标签与所在城市做常识性估算,"
    "不得编造具体房型、电话、地址、促销或任何精确数字;"
    "简介只依据给出的名称/类型/标签,信息不足就写该类型的通用描述,"
    "不要提及 OSM、标签、数据源或模型。"
)
OUTPUT_FORMAT_LINES: tuple[str, ...] = (
    "请严格按下面两行输出,不要多余文字:",
    "价格: 约¥A-B/晚",
    "简介: <40字内一句话>",
)

PRICE_LINE_RE = re.compile(r"^(?:预估|估算|参考)?(?:价格|房价|价位|均价)\s*[:：]\s*(?P<value>.+)$")
INTRO_LINE_RE = re.compile(r"^(?:一句话)?简介\s*[:：]\s*(?P<value>.+)$")
PRICE_RANGE_RE = re.compile(
    r"(?P<low>\d[\d,，.]*)\s*(?:元|[¥￥])?\s*"
    r"(?:[-~－—–至到]\s*(?:[¥￥]?\s*)?(?P<high>\d[\d,，.]*))?"
)
QUOTE_CHARS = "\"'“”‘’「」『』 "
# 批量估价输出的行首序号(``1|…`` / ``1. …`` / ``1) …`` 都收)与字段分隔符
BATCH_INDEX_RE = re.compile(r"^(?P<index>\d{1,3})\s*[|｜.、)）:：\-]\s*(?P<rest>.*)$")
BATCH_SEPARATOR_RE = re.compile(r"[|｜]")
# 批量行里"简介"挤在价格后面(没有 ``|`` 分隔)时的兜底:不锚行首,只抓到下一个分隔符前
INTRO_INLINE_RE = re.compile(r"简介\s*[:：]\s*(?P<value>[^|｜\n]+)")


# --------------------------------------------------------------------------- #
# 归一工具
# --------------------------------------------------------------------------- #


def _get(stay: Any, key: str) -> Any:
    """从 Mapping 或 ORM 行里取字段(两种入参都收,调用方不必先转 dict)。"""
    if isinstance(stay, Mapping):
        return stay.get(key)
    return getattr(stay, key, None)


def _as_float(value: Any) -> Optional[float]:
    """宽容转 float:非数字 / NaN / inf → ``None``(降级,不抛)。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def coordinate(value: Any) -> Optional[float]:
    """坐标归一:定点 :data:`~db.models.COORD_PRECISION` 位小数;非法 → ``None``。"""
    number = _as_float(value)
    return None if number is None else round(number, COORD_PRECISION)


def coordinate_pair(lat: Any, lng: Any) -> Optional[tuple[float, float]]:
    """经纬度归一;缺一或越界 → ``None``。

    调用方据此降级:检索侧当成"没搜到"返回空列表,入库侧直接跳过该行。
    """
    latitude = coordinate(lat)
    longitude = coordinate(lng)
    if latitude is None or longitude is None:
        return None
    if not -LAT_LIMIT <= latitude <= LAT_LIMIT or not -LNG_LIMIT <= longitude <= LNG_LIMIT:
        return None
    return latitude, longitude


def amap_stay_kind(tags: Mapping[str, Any]) -> str:
    """高德行的 kind:按返回的中文 ``type``/``name`` 关键词归一到 :data:`STAY_TAGS`。

    **只按高德实际返回的字符串判,不凭记忆编造中类码**(§1.4 / §6.4 的硬约束);
    ``typecode`` 存在却不是 ``10``(住宿服务大类)开头的脏行 → 空串(不猜);
    大类命中但关键词都没命中 → 按 ``hotel`` 兜底(§1.4 的两个实测锚点 ``100104``
    三星级宾馆 / ``100105`` 经济型连锁酒店 都是酒店,而 :data:`KIND_PRICE_BANDS`
    刻意不收 ``hotel`` —— 酒店价带太宽,交给品牌/星级规则或 LLM)。
    """
    typecode = str(tags.get("typecode") or "").strip()
    type_text = str(tags.get("type") or "").strip()
    haystack = f"{type_text}|{tags.get('name') or ''}"
    if typecode and not typecode.startswith(STAY_TYPECODE_PREFIX):
        return ""
    for kind, hints in AMAP_KIND_HINTS:
        if any(hint in haystack for hint in hints):
            return kind
    return "hotel" if (typecode or type_text) else ""


def stay_kind(tags: Optional[Mapping[str, Any]]) -> str:
    """住宿类型归一:高德行看中文 ``type``/``name``,OSM/存量行看 ``tourism`` 值。

    高德分支(:func:`amap_stay_kind`,TASK-9b)优先:归一出来的仍是 :data:`STAY_TAGS`
    里那五个值,所以前端展示、:data:`KIND_PRICE_BANDS` 规则档与既有测试口径全都不变。
    OSM 分支:``tourism`` 值命中 :data:`STAY_TAGS` 即为 kind;命中不了就退回 ``tourism``
    原值(``motel``/``resort`` 这类同族写法照收,便于前端分组);连 ``tourism`` 都没有时
    看别的 tag 值里有没有住宿类型(``building=hotel`` 的少数写法),都没有 → 空串(不猜)。
    """
    normalized = {
        str(key).strip().lower(): str(value).strip().lower()
        for key, value in dict(tags or {}).items()
    }
    amap_kind = amap_stay_kind(normalized)
    if amap_kind:
        return amap_kind[:KIND_LEN]
    tourism = normalized.get("tourism", "")
    if tourism in STAY_TAGS:
        return tourism
    for tag in STAY_TAGS:
        if tag in normalized.values():
            return tag
    return tourism[:KIND_LEN]


def normalize_kind(row: Any) -> str:
    """一行的 kind:显式给了就用(小写归一),否则从 ``tags`` 推。"""
    explicit = clean_text(_get(row, "kind"), limit=KIND_LEN)
    if explicit:
        return explicit.lower()
    tags = _get(row, "tags")
    return stay_kind(tags if isinstance(tags, Mapping) else None)


def stay_identity(row: Any) -> Optional[tuple[str, int]]:
    """OSM 身份 ``(osm_type, osm_id)``;缺一个就返回 ``None``(无法幂等 upsert,该行跳过)。

    与 :data:`db.models.Stay` 的唯一键同口径;``osm_id=0`` 不是合法 OSM 身份,同样跳过。
    """
    osm_type = str(_get(row, "osm_type") or "").strip().lower()
    raw_id = _get(row, "osm_id")
    if not osm_type or raw_id is None or isinstance(raw_id, bool):
        return None
    try:
        osm_id = int(str(raw_id).strip())
    except (TypeError, ValueError):
        return None
    if osm_id == 0:
        return None
    return osm_type[:16], osm_id


def distance_km(origin_lat: Any, origin_lng: Any, lat: Any, lng: Any) -> Optional[float]:
    """haversine 距离(公里,保留 :data:`DISTANCE_PRECISION` 位);坐标不全 → ``None``。"""
    origin = coordinate_pair(origin_lat, origin_lng)
    point = coordinate_pair(lat, lng)
    if origin is None or point is None:
        return None
    return round(
        haversine_km(origin[0], origin[1], point[0], point[1]), DISTANCE_PRECISION
    )


def _raw_distance(origin_lat: float, origin_lng: float, lat: Any, lng: Any) -> Optional[float]:
    """未取整的 haversine 距离(SQL 粗筛后精确复核 + 排序用)。"""
    latitude = _as_float(lat)
    longitude = _as_float(lng)
    if latitude is None or longitude is None:
        return None
    return haversine_km(origin_lat, origin_lng, latitude, longitude)


# --------------------------------------------------------------------------- #
# 规则估价(TASK-6g:品牌 > 星级 > 类型兜底,再乘城市线级系数;**0 token**)
# --------------------------------------------------------------------------- #


def band_text(low: Any, high: Any) -> str:
    """``(低, 高)`` → 规范串 ``约¥A-B/晚``(相等/上界缺失 → ``约¥A/晚``)。

    LLM 路径的 :func:`normalize_price_range` 也走这里:规则与 LLM 两条路产出的价格串
    **格式必须一致**,"约"字就是估算标注(架构文档:估算字段必须自带标注)。
    非整数入参(``_clean_number`` 可能给出 ``300.5``)按**向下取整**成元,不塞小数房价。
    """
    left = int(_as_float(low) or 0)
    right = int(_as_float(high) or 0)
    if right and right != left:
        return f"约¥{left}-{right}/晚"[:PRICE_LEN]
    return f"约¥{left}/晚"[:PRICE_LEN]


def brand_token(text: Any) -> str:
    """品牌匹配用的归一:去空白/连字符/标点 + 转小写(``Shangri-La`` → ``shangrila``)。"""
    return BRAND_NOISE_RE.sub("", str(text or "")).lower()


# 归一后的别名表,按**别名长度倒序** —— 匹配时最长优先(``parkhyatt`` 先于 ``hyatt``)
BRAND_ALIAS_BANDS: tuple[tuple[str, tuple[int, int]], ...] = tuple(
    sorted(
        ((brand_token(alias), band) for alias, band in BRAND_PRICE_BANDS.items()),
        key=lambda item: (-len(item[0]), item[0]),
    )
)


def brand_band(name: Any) -> Optional[tuple[int, int]]:
    """名称 → 品牌价格带:大小写不敏感的**包含**匹配(最长别名优先);没命中 → ``None``。"""
    token = brand_token(name)
    if not token:
        return None
    for alias, band in BRAND_ALIAS_BANDS:
        if alias and alias in token:
            return band
    return None


def _first_star_digit(text: str) -> Optional[int]:
    """``4*`` / ``四星`` / ``S5`` 里的第一个星级数字(ASCII 数字或中文数字);没有 → ``None``。"""
    for char in text:
        if char in "0123456789":
            return int(char)
        if char in CN_STAR_DIGITS:
            return CN_STAR_DIGITS[char]
    return None


def stars_value(stay: Any) -> Optional[int]:
    """一行的星级(:data:`STARS_TAGS` 里任一个 tag)→ 1..5;认不出/超范围 → ``None``。"""
    tags = _get(stay, "tags")
    normalized = {
        str(key).strip().lower(): str(value).strip()
        for key, value in dict(tags if isinstance(tags, Mapping) else {}).items()
    }
    for key in STARS_TAGS:
        text = normalized.get(key, "")
        if not text:
            continue
        number = _first_star_digit(text)
        if number in STARS_PRICE_BANDS:
            return number
    return amap_stars_value(normalized)


def amap_stars_value(tags: Mapping[str, Any]) -> Optional[int]:
    """高德行的星级:从中文 ``type``/``name`` 里捞 ``三星级`` / ``4星`` → 1..5;认不出 → ``None``。

    刻意不复用 :func:`_first_star_digit` 扫全文:``7天连锁酒店`` 这种名称里的数字**不是**星级,
    必须锚在"星"字上。命中后仍走 :data:`STARS_PRICE_BANDS` 的档位校验(超范围当没有)。
    """
    haystack = f"{tags.get('type') or ''}|{tags.get('name') or ''}"
    match = AMAP_STARS_RE.search(str(haystack))
    if match is None:
        return None
    char = match.group(1)
    number = int(char) if char.isdigit() else CN_STAR_DIGITS.get(char)
    return number if number in STARS_PRICE_BANDS else None


def stars_band(stay: Any) -> Optional[tuple[int, int]]:
    """星级 → 价格带(1-2 星 / 3 星 / 4 星 / 5 星);没有星级 tag → ``None``。"""
    number = stars_value(stay)
    return None if number is None else STARS_PRICE_BANDS[number]


def kind_band(stay: Any) -> Optional[tuple[int, int]]:
    """类型兜底价带(:data:`KIND_PRICE_BANDS`);``hotel``/未知类型 → ``None``(交给 LLM)。"""
    return KIND_PRICE_BANDS.get(normalize_kind(stay))


def rule_band(stay: Any) -> Optional[tuple[int, int]]:
    """基础价格带,**品牌 > 星级 > 类型兜底**(品牌与星级同时命中取品牌档)。"""
    return brand_band(_get(stay, "name")) or stars_band(stay) or kind_band(stay)


def normalize_city_name(text: Any) -> str:
    """地名归一:去空白与结尾"市"(照 ``services.routes.normalize_city`` 的思路)。

    刻意**不 import** :mod:`services.routes`:服务层之间不横向依赖,城市小表内置在本模块。
    """
    core = str(text or "").strip()
    return (core.rstrip("市") or core).strip()


def city_tier(city: Any) -> Optional[str]:
    """城市名 → 线级档:一线 / 新一线 / 其他(空值 → ``None``)。

    判定用**最长包含匹配**(``上海市黄浦区`` / ``杭州西湖`` 都能归到城市);认得出来但
    不在线级表里的城市一律 :data:`CITY_TIER_OTHER`(×0.9)。
    """
    core = normalize_city_name(city)
    if not core:
        return None
    best: Optional[str] = None
    best_len = 0
    for name in KNOWN_TIER_CITIES:
        if name in core and len(name) > best_len:
            best_len = len(name)
            best = CITY_TIER_FIRST if name in TIER1_CITIES else CITY_TIER_NEW_FIRST
    return best or CITY_TIER_OTHER


def stay_city(stay: Any) -> Optional[str]:
    """一行的城市线索:地址 tag(:data:`CITY_TAGS`)> 显式 ``city`` 字段 > **名称**里的城市。

    名称里只认 :data:`KNOWN_TIER_CITIES`(``上海虹桥康得思酒店`` 这类写法),免得把
    ``半岛``/``四季`` 这种词误当城市;三处都没有 → ``None``(按中性系数处理)。
    """
    tags = _get(stay, "tags")
    normalized = {
        str(key).strip().lower(): str(value).strip()
        for key, value in dict(tags if isinstance(tags, Mapping) else {}).items()
    }
    for key in CITY_TAGS:
        value = normalized.get(key, "")
        if value and value.lower() not in ("no", "none", "unknown"):
            return normalize_city_name(value)
    explicit = normalize_city_name(_get(stay, "city"))
    if explicit:
        return explicit
    name = str(_get(stay, "name") or "")
    best = ""
    for city in KNOWN_TIER_CITIES:
        if city in name and len(city) > len(best):
            best = city
    return best or None


def city_price_factor(stay: Any) -> float:
    """城市线级系数:一线 ×1.2 / 新一线 ×1.05 / 其他城市 ×0.9 / 认不出城市 ×1.0。"""
    city = stay_city(stay)
    if not city:
        return CITY_TIER_UNKNOWN_FACTOR
    tier = city_tier(city)
    return CITY_TIER_FACTOR.get(tier, CITY_TIER_UNKNOWN_FACTOR) if tier else CITY_TIER_UNKNOWN_FACTOR


def round_band_value(value: Any) -> int:
    """系数乘完的价 → 取整到 :data:`PRICE_BAND_STEP`(5 元,四舍五入);非数字 → 0。"""
    number = _as_float(value)
    if number is None:
        return 0
    return int(math.floor(number / PRICE_BAND_STEP + 0.5)) * PRICE_BAND_STEP


def rule_price_estimate(stay: Any) -> Optional[tuple[int, int, str]]:
    """规则层估价 → ``(低, 高, "rule")``;命中不了规则 → ``None``(该行交给 LLM)。

    判档顺序 **品牌 > 星级 > 类型兜底**,再乘 :func:`city_price_factor` 并取整到 5 元。
    没有名称的行不估(与 :func:`stays_needing_price` 同口径:无名行是低质数据,不猜)。
    **0 token、不触网、绝不抛异常** —— 出任何意外都退回 ``None`` 让 LLM 兜。
    """
    if not str(_get(stay, "name") or "").strip():
        return None
    band = rule_band(stay)
    if band is None:
        return None
    try:
        factor = city_price_factor(stay)
        low = round_band_value(band[0] * factor)
        high = round_band_value(band[1] * factor)
    except Exception:  # noqa: BLE001 - 规则层是增强项,炸了就当没命中(退回 LLM)
        return None
    if low <= 0:
        return None
    return low, max(high, low), PRICE_KIND_RULE


def _write_price(stay: Any, price: str, kind: str) -> None:
    """就地写价格 + 出处标记(ORM 行与 dict 入参都收)。"""
    if isinstance(stay, dict):
        stay["price_estimate"] = price
        stay["price_kind"] = kind
        return
    stay.price_estimate = price
    stay.price_kind = kind


def apply_rule_prices(stays: Sequence[Any]) -> int:
    """给**缺价格且命中规则**的行就地补 ``(price_estimate, price_kind="rule")``,返回条数。

    与 LLM 路径看同一批候选(没价格 + 有名称),但 0 token,所以能在请求线程里同步跑完,
    不必排后台。已有价格的行原样跳过(**永久缓存**,规则表改了也不重算,与 LLM 产物同口径)。
    只改内存里的行,``commit`` 交给调用方(与 :func:`upsert_stays` 一致)。
    """
    filled = 0
    for stay in stays or ():
        if clean_text(_get(stay, "price_estimate"), limit=PRICE_LEN):
            continue
        rule = rule_price_estimate(stay)
        if rule is None:
            continue
        _write_price(stay, band_text(rule[0], rule[1]), rule[2])
        filled += 1
    return filled


def rows_without_price(stays: Sequence[Any]) -> list[Any]:
    """规则层跑完之后**还缺价格**的行(这些才是 LLM 的活儿)。"""
    return [
        stay for stay in (stays or ())
        if not clean_text(_get(stay, "price_estimate"), limit=PRICE_LEN)
    ]


# --------------------------------------------------------------------------- #
# 检索(高德 ``place/around``,TASK-9b)
# --------------------------------------------------------------------------- #

#: 检索实现的可注入签名:``(lat, lng, radius_m) -> 归一化高德 POI 列表``
#: (:func:`data_sources.amap.parse_poi` 的形状;测试传替身即可完全不触网)
StayFetchFn = Callable[..., list[dict[str, Any]]]


def stay_page_limit(budget: int = GROUP_BUDGET) -> int:
    """住宿检索最多翻几页:v3 固定 25 条/页、单查询 200 条硬上限(``page>=9`` 服务端恒空)。"""
    return max(1, min(amap.MAX_PAGE, math.ceil(max(1, int(budget)) / amap.PAGE_SIZE)))


def fetch_stay_pois(
    lat: float,
    lng: float,
    radius_m: float = DEFAULT_RADIUS_M,
    *,
    budget: int = GROUP_BUDGET,
    environ: Optional[Mapping[str, str]] = None,
    session: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """高德 ``place/around`` 检索住宿:``types=100000``(住宿服务**大类**),由近及远翻页。

    * **只用大类码粗筛**(§1.4 实测锚点:``100104`` 三星级宾馆 / ``100105`` 经济型连锁酒店),
      具体 ``kind`` 在本地按返回的 ``type`` 中文串判(:func:`stay_kind`)—— 契约明令
      **禁止凭记忆编造中类码**;
    * 翻页到 :func:`stay_page_limit`(单查询 200 条硬上限)或配额满为止,**某页没拿满就停**
      (``page>=9`` 服务端恒空,硬翻只是白烧配额);
    * 高德返回顺序已是由近及远,这里不再重排;组内按 ``("amap", POI id)`` 去重;
    * 半径 > 50km 由 :mod:`data_sources.amap` 钳制(住宿阶梯最大 30km,碰不到);
    * 无 key / 限流 / 响应异常一律抛 :class:`data_sources.DataSourceError`,
      由 :func:`search_stays_detailed` 兜成三档 reason(**绝不抛给调用方**)。
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for page in range(1, stay_page_limit(budget) + 1):
        batch = amap.search_around(
            lat,
            lng,
            radius_m=radius_m,
            types=STAY_TYPES,
            page=page,
            offset=amap.PAGE_SIZE,
            environ=environ,
            session=session,
        ) or []
        for poi in batch:
            key = amap_categories.dedupe_key(poi)
            if not key[1] or key in seen:
                continue
            seen.add(key)
            rows.append(dict(poi))
            if len(rows) >= max(1, int(budget)):
                return rows
        if len(batch) < amap.PAGE_SIZE:
            return rows
    return rows


def stay_tags(poi: Mapping[str, Any]) -> dict[str, Any]:
    """归一化高德 POI → ``Stay.tags``:来源标注 + POI id 原文 + 分类原文 + 地址线索。

    ``amap_id`` 必须留原文(``osm_id`` 是 crc32 哈希、不可逆);``type``/``typecode`` 是
    高德分类原文(:func:`stay_kind` / :func:`stars_value` 的判定依据,也是 LLM 的事实线索);
    ``cityname`` 给 :func:`stay_city` 做城市线级修正,``address``/``adname`` 只作展示与 prompt。
    """
    tags: dict[str, Any] = {
        "source": AMAP_SOURCE,
        "amap_id": str(poi.get("id") or "").strip(),
        "typecode": str(poi.get("typecode") or "").strip(),
        "type": str(poi.get("type") or "").strip(),
    }
    for key in ("address", "cityname", "adname"):
        value = str(poi.get(key) or "").strip()
        if value:
            tags[key] = value
    name = str(poi.get("name") or "").strip()
    if name:
        tags["name"] = name
    return tags


def stay_row(poi: Mapping[str, Any], origin_lat: float, origin_lng: float) -> dict[str, Any]:
    """一条归一化高德 POI → 住宿行(补 **crc32 身份**、``kind`` 与 ``distance_km``)。

    ``Stay.osm_id`` 也是 Integer 列,所以身份与 ``Place`` 同口径:
    ``osm_type="amap"``、``osm_id = crc32(POI id)``(:func:`db.models.amap_osm_id`),
    唯一键 ``(osm_type, osm_id)`` 的幂等语义原样保留;没有 id 的脏行 ``osm_id=None``,
    由 :func:`stay_identity` 判成"无法幂等 upsert"而跳过(不抛)。
    """
    tags = stay_tags(poi)
    poi_id = tags["amap_id"]
    return {
        "osm_type": AMAP_OSM_TYPE,
        "osm_id": amap_osm_id(poi_id) if poi_id else None,
        "name": str(poi.get("name") or "").strip(),
        "kind": stay_kind(tags),
        "lat": poi.get("lat"),
        "lng": poi.get("lng"),
        "tags": tags,
        "distance_km": distance_km(origin_lat, origin_lng, poi.get("lat"), poi.get("lng")),
    }


def search_stays(
    lat: float,
    lng: float,
    radius_m: int = DEFAULT_RADIUS_M,
    *,
    fetch_fn: Optional[StayFetchFn] = None,
) -> list[dict[str, Any]]:
    """检索起点半径内的住宿,按由近及远排序;**任何失败都返回空列表**(降级,不抛)。

    ``fetch_fn`` 可注入(测试替身,签名 ``(lat, lng, radius_m) -> 归一化高德 POI 列表``);
    缺省用 :func:`fetch_stay_pois`(高德 ``place/around`` + ``types=100000``)。
    需要知道"为什么空"的调用方(编排层/负缓存)用 :func:`search_stays_detailed`。
    """
    return search_stays_detailed(lat, lng, radius_m, fetch_fn=fetch_fn)[0]


def classify_failure(exc: BaseException) -> str:
    """检索异常 → 三档 reason 里的失败档:``timeout`` 或 ``datasource_error``。

    超时单独分档是因为前端文案不同(超时="稍后再试",其他="可重试")。
    :mod:`data_sources._common` 与 :mod:`data_sources.amap` 都把超时包成
    ``请求超时(...)`` 的 :class:`~data_sources.TransientDataSourceError`(高德的 QPS/日限流
    infocode 同样归 Transient),所以按**文案线索**判超时最稳;其余异常(无 key/权限/
    配额耗尽/响应格式不对)一律 ``datasource_error``。
    """
    if isinstance(exc, TimeoutError):  # socket.timeout 在 3.10+ 就是 TimeoutError
        return REASON_TIMEOUT
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(hint.lower() in text for hint in TIMEOUT_HINTS):
        return REASON_TIMEOUT
    return REASON_DATASOURCE_ERROR


def search_stays_detailed(
    lat: float,
    lng: float,
    radius_m: int = DEFAULT_RADIUS_M,
    *,
    fetch_fn: Optional[StayFetchFn] = None,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """同 :func:`search_stays`,但额外报**为什么空**:返回 ``(rows, reason)``。

    ``reason`` 三档(TASK-6c):``None`` = 拿到结果;:data:`REASON_NO_DATA` = 检索成功但
    半径内真没有;:data:`REASON_DATASOURCE_ERROR` / :data:`REASON_TIMEOUT` = 检索失败
    (按 :func:`classify_failure` 分档)。仍然**绝不抛异常**。
    """
    origin = coordinate_pair(lat, lng)
    if origin is None:
        return [], REASON_NO_DATA
    origin_lat, origin_lng = origin
    fetch = fetch_fn if fetch_fn is not None else fetch_stay_pois
    try:
        pois = fetch(origin_lat, origin_lng, radius_m)
    except DataSourceError as exc:
        return [], classify_failure(exc)
    except Exception as exc:  # noqa: BLE001 - 检索是增强项:参数非法/响应异常都当"没搜到"
        return [], classify_failure(exc)

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for poi in pois or []:
        if not isinstance(poi, Mapping):
            # 脏行(注入的替身/上游解析异常)不能让整次检索抛出去:跳过即可,
            # 与 :func:`data_sources.amap.parse_poi` 丢弃非对象行的口径一致。
            continue
        row = stay_row(poi, origin_lat, origin_lng)
        identity = stay_identity(row)
        if identity is None or identity in seen:
            continue
        seen.add(identity)
        rows.append(row)
    return (rows, None) if rows else ([], REASON_NO_DATA)


# --------------------------------------------------------------------------- #
# 负缓存 + 半径阶梯(TASK-6c)
# --------------------------------------------------------------------------- #


def radius_ladder(radius_m: Optional[float]) -> tuple[int, ...]:
    """本次要查的半径序列:**未显式给半径** → 走阶梯(5→10→30 km);给了 → 只查那一档。

    非法半径(0/负数/非数字)→ 空序列,调用方据此直接返回空结果(不触网)。
    """
    if radius_m is None:
        return STAY_RADIUS_LADDER_M
    resolved = _as_float(radius_m)
    if resolved is None or resolved <= 0:
        return ()
    return (int(round(resolved)),)


def cache_age_seconds(row: Any, *, now: Optional[Any] = None) -> Optional[float]:
    """负缓存行的年龄(秒);没有 ``fetched_at`` → ``None``(视为不可用,不当命中)。"""
    fetched = getattr(row, "fetched_at", None)
    if fetched is None:
        return None
    if fetched.tzinfo is None:  # SQLite 读回的是 naive 时间,按 UTC 处理
        fetched = fetched.replace(tzinfo=timezone.utc)
    moment = now if now is not None else utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - fetched).total_seconds()


def remember_negative(
    session: Session,
    lat: float,
    lng: float,
    radius_m: float,
    *,
    reason: str,
    nearest_km: Optional[float] = None,
) -> Optional[StayQueryCache]:
    """写/刷新一条负缓存(按 ``(定点坐标, 半径, kind)`` upsert),返回该行;入参非法 → ``None``。

    坐标先按 :data:`~db.models.COORD_PRECISION` 定点再入库,复刻唯一键口径 —— 否则
    ``31.230400000000004`` 与 ``31.2304`` 会存成两行,缓存永远命不中。
    只 ``flush`` 不 ``commit``:提交时机交给编排函数(与 :func:`upsert_stays` 一致)。
    """
    origin = coordinate_pair(lat, lng)
    if origin is None:
        return None
    radius = _as_float(radius_m)
    if radius is None or radius <= 0:
        return None
    kind = stay_cache_kind(reason)
    origin_lat, origin_lng = origin
    resolved_radius = int(round(radius))
    row = session.scalar(
        select(StayQueryCache).where(
            StayQueryCache.lat == origin_lat,
            StayQueryCache.lng == origin_lng,
            StayQueryCache.radius_m == resolved_radius,
            StayQueryCache.kind == kind,
        )
    )
    if row is None:
        row = StayQueryCache(
            lat=origin_lat, lng=origin_lng, radius_m=resolved_radius, kind=kind
        )
        session.add(row)
    distance = _as_float(nearest_km)
    row.reason = stay_cache_reason(kind)[:STAY_REASON_LEN]
    row.nearest_km = None if distance is None else round(distance, NEAREST_KM_PRECISION)
    row.fetched_at = utcnow()
    session.flush()
    return row


def lookup_negative(
    session: Session,
    lat: float,
    lng: float,
    radius_m: float,
    *,
    ttl_s: float = NEG_CACHE_TTL_S,
    now: Optional[Any] = None,
) -> Optional[StayQueryCache]:
    """命中未过期的负缓存就返回该行(**最新的一行优先**),否则 ``None`` = 该重新查了。"""
    origin = coordinate_pair(lat, lng)
    if origin is None:
        return None
    radius = _as_float(radius_m)
    if radius is None or radius <= 0:
        return None
    rows = session.scalars(
        select(StayQueryCache)
        .where(
            StayQueryCache.lat == origin[0],
            StayQueryCache.lng == origin[1],
            StayQueryCache.radius_m == int(round(radius)),
        )
        .order_by(StayQueryCache.fetched_at.desc(), StayQueryCache.id.desc())
    ).all()
    for row in rows:
        age = cache_age_seconds(row, now=now)
        if age is not None and age <= ttl_s:
            return row
    return None


def negative_state(row: Optional[StayQueryCache]) -> dict[str, Any]:
    """负缓存行 → 可直接透传 API 的缓存态(``reason`` / ``nearest_km`` / ``kind`` / 时间)。"""
    if row is None:
        return {"reason": None, "nearest_km": None, "kind": None, "fetched_at": None}
    distance = _as_float(row.nearest_km)
    return {
        "reason": stay_cache_reason(row.kind),
        "nearest_km": None if distance is None else round(distance, NEAREST_KM_PRECISION),
        "kind": row.kind or STAY_CACHE_EMPTY,
        "fetched_at": iso_utc(row.fetched_at),
    }


def nearest_known_km(
    session: Session,
    lat: float,
    lng: float,
    *,
    radius_m: Optional[float] = None,
) -> Optional[float]:
    """库里已知的最近一家住宿有多远(km,1 位);范围外/库里没有 → ``None``。

    空结果时用它给前端"最近的在 X km 外"提示(BUG-3):**只读库、不触网**,
    范围默认取阶梯最大档(30 km)—— 再远的住宿对"今晚住哪儿"没有参考价值。
    """
    limit = _as_float(radius_m)
    if limit is None or limit <= 0:
        limit = float(max(STAY_RADIUS_LADDER_M))
    rows = select_stays(session, lat, lng, limit)
    if not rows:
        return None
    origin = coordinate_pair(lat, lng)
    if origin is None:
        return None
    raw = _raw_distance(origin[0], origin[1], rows[0].lat, rows[0].lng)
    return None if raw is None else round(raw, NEAREST_KM_PRECISION)


# --------------------------------------------------------------------------- #
# 估价 + 简介(LLM)
# --------------------------------------------------------------------------- #


def resolve_llm(environ: Optional[Mapping[str, str]] = None) -> LLMClient:
    """LLM 客户端:给了 ``environ`` 就按它解析 Provider(测试传 ``{}`` 模拟"未配 key")。"""
    if environ is None:
        return default_llm_client()
    return LLMClient(environ=environ)


def stay_facts(tags: Optional[Mapping[str, Any]], *, limit: int = STAY_FACT_LIMIT) -> str:
    """住宿标签摘要 ``k=v; k=v``(白名单 + 上限),给 LLM 当房价线索。"""
    normalized = {
        str(key).strip().lower(): str(value).strip() for key, value in dict(tags or {}).items()
    }
    facts: list[str] = []
    for key in STAY_FACT_TAGS:
        value = normalized.get(key)
        if value and value.lower() not in ("no", "none", "unknown"):
            facts.append(f"{key}={value}")
        if len(facts) >= limit:
            break
    return "; ".join(facts)


def _where_line(stay: Any) -> str:
    """prompt 里的位置行:坐标(定点)+ 距起点公里数(有才带)。"""
    parts: list[str] = []
    point = coordinate_pair(_get(stay, "lat"), _get(stay, "lng"))
    if point is not None:
        parts.append(f"坐标:{point[0]:.7f},{point[1]:.7f}")
    distance = _as_float(_get(stay, "distance_km"))
    if distance is not None:
        parts.append(f"距起点:{distance:.1f} km")
    return " · ".join(parts)


def build_price_prompt(stay: Any) -> str:
    """按住宿拼 prompt:名称 + 类型 + 位置 + 标签摘要 + **严格两行**的输出格式要求。"""
    name = str(_get(stay, "name") or "").strip() or "(无名)"
    kind = normalize_kind(stay)
    lines = [f"名称:{name}", f"类型:{kind or '住宿'}"]
    where = _where_line(stay)
    if where:
        lines.append(where)
    facts = stay_facts(_get(stay, "tags") if isinstance(_get(stay, "tags"), Mapping) else None)
    if facts:
        lines.append(f"OSM 标签:{facts}")
    lines.extend(OUTPUT_FORMAT_LINES)
    return "\n".join(lines)


def _clean_number(text: Optional[str]) -> str:
    """``1,200.50`` → ``1200.5``;空 / 非数字 / ≤0 → ``""``。"""
    digits = str(text or "").replace(",", "").replace(",", "").strip().strip(".")
    number = _as_float(digits)
    if number is None or number <= 0:
        return ""
    return str(int(number)) if number == int(number) else f"{number:g}"


def parse_price(completion: Optional[str]) -> str:
    """取价格行 → 规范串 ``约¥A-B/晚``(单值则 ``约¥A/晚``);解析不出 → ``""``。

    规范化的意义:LLM 会写 ``300-500元``/``约 ¥300~500 每晚``/``1,200-1,800`` 等一堆变体,
    落库统一成一种带"约"的口径,前端不必再猜格式;"约"字就是估算标注。
    """
    for line in str(completion or "").splitlines():
        matched = PRICE_LINE_RE.match(line.strip())
        if matched is None:
            continue
        return normalize_price_range(matched.group("value").strip(QUOTE_CHARS))
    return ""


def normalize_price_range(value: Optional[str]) -> str:
    """一段文本里的第一个数字区间 → 规范串 ``约¥A-B/晚``(单值 ``约¥A/晚``);抓不出 → ``""``。

    从 :func:`parse_price` 里抽出来给**批量**输出复用:批量行是
    ``1|价格: 约¥300-500/晚|简介: …``,按 ``|`` 切开之后每段仍走同一套归一,
    口径不会两处漂("约"字这个估算标注恒在)。
    """
    numbers = PRICE_RANGE_RE.search(str(value or ""))
    if numbers is None:
        return ""
    low = _clean_number(numbers.group("low"))
    high = _clean_number(numbers.group("high"))
    if not low:
        return ""
    return band_text(low, high)


def parse_intro_line(completion: Optional[str], *, max_chars: int = INTRO_TARGET_CHARS) -> str:
    """取 ``简介:`` 行,复用 :func:`services.intro.clean_intro` 清洗(压一行/去引号/截断)。"""
    for line in str(completion or "").splitlines():
        matched = INTRO_LINE_RE.match(line.strip())
        if matched is None:
            continue
        return clean_intro(matched.group("value"), max_chars=max_chars)
    return ""


def estimate_price(
    stay: Any,
    *,
    client: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> tuple[str, str]:
    """给一处住宿估**价格区间 + 一句话简介**,返回 ``(price_estimate, intro)``。

    降级口径(与 :func:`services.intro.generate_intro` 一致):未配 key、没有名称、
    网络/限流异常、返回格式不对 —— 一律 ``("", "")``,**绝不抛异常**。
    已有 ``price_estimate`` 的行**原样返回、不调用 LLM**(DB 即缓存)。
    TASK-6g 起真正的口径在 :func:`estimate_price_tagged`(多返回一个出处标记),
    本函数是它的两元组薄壳,既有调用方与单测零改动。
    """
    price, intro, _kind = estimate_price_tagged(stay, client=client, environ=environ)
    return price, intro


def normalize_price_kind(value: Any) -> str:
    """价格出处标记归一:只认 :data:`~db.models.PRICE_KINDS`,其余 → ``""``(老行没有出处)。"""
    text = clean_text(value, limit=PRICE_KIND_LEN)
    lowered = (text or "").lower()
    return lowered if lowered in PRICE_KINDS else ""


def _write_intro(stay: Any, intro: str) -> None:
    """就地写简介(ORM 行与 dict 入参都收)。"""
    if isinstance(stay, dict):
        stay["intro"] = intro
        return
    stay.intro = intro


def estimate_price_tagged(
    stay: Any,
    *,
    client: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> tuple[str, str, str]:
    """逐家估价,返回 ``(price_estimate, intro, price_kind)``;**规则层前置**(TASK-6g)。

    顺序是"缓存 → 名称 → 规则 → LLM":

    1. 已有价格 → 原样返回(出处标记也照搬,**永久缓存**,规则表改了也不重算);
    2. 没有名称 → ``("", "", "")``(不猜,既有口径);
    3. :func:`rule_price_estimate` 命中品牌/星级/类型档 → 直接出 ``约¥A-B/晚``,
       ``price_kind="rule"``,**一次 LLM 都不调**(0 token;简介因此留空,不为简介烧额度);
    4. 未命中规则才走 LLM(``price_kind="llm"``),未配 key / 超时 / 限流 / 格式不对
       一律 ``("", "", "")``,**绝不抛异常**。
    """
    existing = clean_text(_get(stay, "price_estimate"), limit=PRICE_LEN)
    if existing:
        return (
            existing,
            str(_get(stay, "intro") or "").strip(),
            normalize_price_kind(_get(stay, "price_kind")),
        )

    if not str(_get(stay, "name") or "").strip():
        return "", "", ""

    rule = rule_price_estimate(stay)
    if rule is not None:
        return band_text(rule[0], rule[1]), str(_get(stay, "intro") or "").strip(), rule[2]

    llm = client if client is not None else resolve_llm(environ)
    if not llm.enabled:
        return "", "", ""
    try:
        completion = llm.chat(build_price_prompt(stay), system=STAY_SYSTEM_PROMPT)
    except DataSourceError:
        return "", "", ""
    except Exception:  # noqa: BLE001 - 估价是增强项,任何异常都不得阻塞入库
        return "", "", ""
    price = parse_price(completion)
    if not price:
        return "", "", ""
    return price, parse_intro_line(completion), PRICE_KIND_LLM


def estimate_missing(
    stays: Sequence[Any],
    *,
    client: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> int:
    """给**缺价格**的行就地补 ``(price_estimate, intro, price_kind)``,返回条数(不 commit)。

    逐家口径(TASK-3a1 遗留,``estimate=sync`` 档在用):每家先过
    :func:`rule_price_estimate` 的规则层(命中即 0 token 出价、``price_kind="rule"``),
    未命中才调一次 LLM(``price_kind="llm"``)。
    """
    estimated = 0
    for stay in stays:
        if clean_text(_get(stay, "price_estimate"), limit=PRICE_LEN):
            continue
        price, intro, kind = estimate_price_tagged(stay, client=client, environ=environ)
        if not price:
            continue
        _write_price(stay, price, kind)
        if intro and not str(_get(stay, "intro") or "").strip():
            _write_intro(stay, intro)
        estimated += 1
    return estimated


# --------------------------------------------------------------------------- #
# 批量估价 + 后台异步回填(TASK-6c:5 家/prompt、固定 qwen3.8-max、失败留 null)
# --------------------------------------------------------------------------- #

BATCH_SYSTEM_PROMPT = (
    "你是 Where2Go(周末去哪儿玩)的住宿信息助手,为行程落脚点**批量**写价格区间估算与一句话简介。"
    "用户一次给出若干家住宿,请**每家输出一行**,行数与序号必须与输入一一对应,"
    "每行严格写成 `序号|价格: 约¥A-B/晚|简介: <40字内一句话>`,"
    "不要标题、解释、空行或任何多余文字。"
    "价格只能依据名称、住宿类型、星级/房量等标签与所在城市做常识性估算,"
    "必须带“约”字,不得编造具体房型、电话、地址、促销或任何精确数字;"
    "简介只依据给出的名称/类型/标签,信息不足就写该类型的通用描述,"
    "不要提及 OSM、标签、数据源或模型。价格是**估算**,不是报价。"
)


def resolve_price_llm(environ: Optional[Mapping[str, str]] = None) -> LLMClient:
    """后台批量估价用的 LLM 客户端:**固定** qwen(token-plan 入口,``qwen3.8-max``)。

    口径照 :func:`services.intro.resolve_provider`(``WHERE2GO_LLM_*`` 可覆盖 base_url/model,
    key 只读环境变量),但**不退回别的供应商**(神朱 2026-09-28 定:不做双模型)——
    拿不到该 provider 的 key 就返回一个 ``enabled=False`` 的客户端,上层据此把价格留 null,
    既不烧 token 也不猜数字。
    """
    provider = find_provider(PRICE_PROVIDER_NAME)
    env = dict(os.environ if environ is None else environ)
    if provider is None:
        return LLMClient(environ={})
    api_key = ""
    key_env = ""
    for candidate in (ENV_API_KEY,) + tuple(provider.env_api_keys):
        value = str(env.get(candidate) or "").strip()
        if value:
            api_key, key_env = value, candidate
            break
    if not api_key:
        return LLMClient(environ={})
    return LLMClient(
        resolved=ResolvedLLM(
            provider=provider.name,
            label=provider.label,
            base_url=str(env.get(ENV_BASE_URL) or provider.base_url).strip().rstrip("/"),
            model=str(env.get(ENV_MODEL) or PRICE_MODEL or provider.model).strip(),
            api_key=api_key,
            key_env=key_env,
        )
    )


def stays_needing_price(stays: Sequence[Any]) -> list[Any]:
    """挑出**还能估价**的行:没有 ``price_estimate`` 且有名称。

    没有名称的行 LLM 只能瞎猜(:func:`estimate_price` 也直接降级),所以不排队、不占 token;
    已有价格的行**永不再估**(DB 即缓存,与 ``Place.intro`` 同口径)。
    """
    pending: list[Any] = []
    for stay in stays or ():
        if clean_text(_get(stay, "price_estimate"), limit=PRICE_LEN):
            continue
        if not str(_get(stay, "name") or "").strip():
            continue
        pending.append(stay)
    return pending


def price_batches(stays: Sequence[Any], *, batch_size: int = PRICE_BATCH_SIZE) -> list[list[Any]]:
    """按 :data:`PRICE_BATCH_SIZE`(5 家)切批;``batch_size`` 非法时退回默认。"""
    size = int(batch_size) if batch_size and int(batch_size) > 0 else PRICE_BATCH_SIZE
    rows = list(stays or ())
    return [rows[index:index + size] for index in range(0, len(rows), size)]


def batch_output_format_lines(count: int) -> tuple[str, ...]:
    """批量 prompt 的输出格式要求(逐行给出序号,模型照着填最稳)。"""
    sample = "\n".join(
        f"{index}|价格: 约¥A-B/晚|简介: <40字内一句话>" for index in range(1, max(1, count) + 1)
    )
    return (
        f"请严格按下面 {max(1, count)} 行输出,每行一家,序号与上面一一对应,不要多余文字:",
        sample,
    )


def build_batch_price_prompt(batch: Sequence[Any]) -> str:
    """一批住宿拼一个 prompt:逐家给名称/类型/位置/标签摘要,末尾附**严格逐行**的输出格式。"""
    rows = list(batch or ())
    lines: list[str] = [f"下面是 {len(rows)} 家住宿,请为每一家给出价格区间估算与一句话简介。", ""]
    for index, stay in enumerate(rows, start=1):
        name = str(_get(stay, "name") or "").strip() or "(无名)"
        lines.append(f"第{index}家")
        lines.append(f"名称:{name}")
        lines.append(f"类型:{normalize_kind(stay) or '住宿'}")
        where = _where_line(stay)
        if where:
            lines.append(where)
        facts = stay_facts(_get(stay, "tags") if isinstance(_get(stay, "tags"), Mapping) else None)
        if facts:
            lines.append(f"OSM 标签:{facts}")
        lines.append("")
    lines.extend(batch_output_format_lines(len(rows)))
    return "\n".join(lines)


def parse_batch_line(text: Optional[str]) -> tuple[str, str]:
    """批量输出的一行(去掉序号后)→ ``(price, intro)``;解析不出就是 ``("", "")``(留 null)。

    先按 ``|`` 切段,每段复用单条口径的 :func:`parse_price` / :func:`parse_intro_line`;
    首段额外允许"没有 ``价格:`` 前缀"的写法(``1|约¥300-500/晚|…``)—— 只有首段兜底,
    免得简介里的数字被当成房价。
    """
    raw = str(text or "").strip()
    if not raw:
        return "", ""
    parts = [part.strip() for part in BATCH_SEPARATOR_RE.split(raw) if part.strip()] or [raw]
    price = ""
    intro = ""
    for position, part in enumerate(parts):
        if not price:
            price = parse_price(part) or (
                normalize_price_range(part.strip(QUOTE_CHARS)) if position == 0 else ""
            )
        if not intro:
            intro = parse_intro_line(part)
    if not intro:  # 模型把两段挤在一起(``价格: … 简介: …``)时的兜底
        matched = INTRO_INLINE_RE.search(raw)
        if matched is not None:
            intro = clean_intro(matched.group("value"))
    return price, intro


def parse_batch_completion(completion: Optional[str], count: int) -> list[tuple[str, str]]:
    """一次批量输出 → 按**序号**归位的 ``count`` 条 ``(price, intro)``。

    缺行 / 序号越界 / 解析不出 → 该家留 ``("", "")``(价格 null,**不猜、不抛**);
    同一序号出现多行时后到的只补空位,不覆盖已解析出的值。
    """
    total = max(0, int(count))
    results: list[tuple[str, str]] = [("", "")] * total
    for line in str(completion or "").splitlines():
        matched = BATCH_INDEX_RE.match(line.strip())
        if matched is None:
            continue
        index = int(matched.group("index"))
        if not 1 <= index <= total:
            continue
        price, intro = parse_batch_line(matched.group("rest"))
        if not price and not intro:
            continue
        known_price, known_intro = results[index - 1]
        results[index - 1] = (price or known_price, intro or known_intro)
    return results


def estimate_batch(
    batch: Sequence[Any],
    *,
    client: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
    retries: int = PRICE_BATCH_RETRIES,
) -> list[tuple[str, str]]:
    """给一批(≤ :data:`PRICE_BATCH_SIZE` 家)估价:**一次 prompt** 出全部行。

    降级口径与 :func:`estimate_price` 一致:未配 key / 超时 / 限流 / 整批解析不出 →
    全留空,绝不抛异常;单批**重试不超过 :data:`PRICE_BATCH_RETRIES` 次**(1 次),
    重试只针对"调用失败或整批一个价格都没解析出来",部分解析成功就收下、缺的留 null。
    """
    rows = list(batch or ())
    if not rows:
        return []
    llm = client if client is not None else resolve_price_llm(environ)
    if not llm.enabled:
        return [("", "") for _ in rows]
    prompt = build_batch_price_prompt(rows)
    attempts = max(1, int(retries) + 1)
    for _ in range(attempts):
        try:
            completion = llm.chat(
                prompt,
                system=BATCH_SYSTEM_PROMPT,
                max_tokens=PRICE_BATCH_MAX_TOKENS,
                timeout=PRICE_BATCH_TIMEOUT_S,
            )
        except DataSourceError:
            continue
        except Exception:  # noqa: BLE001 - 估价是增强项,任何异常都不得冒到调用方
            continue
        parsed = parse_batch_completion(completion, len(rows))
        if any(price for price, _ in parsed):
            return parsed
    return [("", "") for _ in rows]


def fill_prices_batched(
    stays: Sequence[Any],
    *,
    client: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
    batch_size: int = PRICE_BATCH_SIZE,
) -> int:
    """按批给**缺价格**的行就地补 ``(price_estimate, intro)``,返回补上的条数(不 commit)。

    与 :func:`estimate_missing`(逐家一次调用)的区别只在**批量**:5 家一个 prompt,
    token 与限流额度都省到 1/5;写入口径完全一致(已有价格/简介不覆盖)。

    TASK-6g:**规则层前置** —— 先用 :func:`apply_rule_prices` 把命中品牌/星级/类型档的行
    就地填掉(``price_kind="rule"``,0 token),剩下的才切批进 prompt(``price_kind="llm"``)。
    所以"7 家全命中规则"这种情况一次 LLM 都不调,prompt 数也从 2 降到 0。
    """
    pending = stays_needing_price(stays)
    if not pending:
        return 0
    filled = apply_rule_prices(pending)
    remaining = rows_without_price(pending)
    if not remaining:
        return filled
    for batch in price_batches(remaining, batch_size=batch_size):
        for stay, (price, intro) in zip(batch, estimate_batch(batch, client=client, environ=environ)):
            if not price:
                continue
            _write_price(stay, price, PRICE_KIND_LLM)
            if intro and not str(_get(stay, "intro") or "").strip():
                _write_intro(stay, intro)
            filled += 1
    return filled


class InlineExecutor:
    """同步执行器:``submit`` 就在调用线程里跑完(测试断言与 CLI"等回填完再打印"用)。

    只实现 :class:`concurrent.futures.Executor` 用到的 ``submit``,返回值仍是
    :class:`~concurrent.futures.Future`,所以与真线程池可互换(依赖倒置,测试不必等线程)。
    """

    def submit(self, fn, *args: Any, **kwargs: Any) -> Future:
        future: Future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - 与线程池一致:异常进 Future,不冒出来
            future.set_exception(exc)
        return future


INLINE_EXECUTOR = InlineExecutor()

_background_executor: Optional[Executor] = None
_background_lock = threading.Lock()


def background_executor() -> Executor:
    """进程内共享的后台线程池(懒建;照 :func:`services.intro._run_batch` 的口径)。

    后台线程**不阻塞请求**:请求线程只 ``submit`` 一次就返回,估价在池子里慢慢跑;
    池子跑完的活儿写回同一个 SQLite 库(见 :func:`price_fill_job`,它自己开 Session)。
    """
    global _background_executor
    with _background_lock:
        if _background_executor is None:
            _background_executor = ThreadPoolExecutor(
                max_workers=PRICE_BACKGROUND_WORKERS, thread_name_prefix="stay-price"
            )
        return _background_executor


def set_background_executor(executor: Optional[Executor]) -> None:
    """替换/清空后台执行器(测试注入 :data:`INLINE_EXECUTOR` 或假执行器;传 ``None`` 复原)。"""
    global _background_executor
    with _background_lock:
        _background_executor = executor


def price_fill_job(
    engine: Any,
    stay_ids: Sequence[int],
    *,
    client: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
    batch_size: int = PRICE_BATCH_SIZE,
) -> dict[str, Any]:
    """后台线程体:按 id 重新取行 → 批量估价 → 写回 → commit,返回统计;**任何异常都吞掉**。

    自己开 Session:SQLAlchemy 的 Session 非线程安全,请求线程的 Session 绝不能跨线程用;
    而且调用方在 ``submit`` 之前已经 commit 过事实行(否则新连接看不见未提交数据)。

    TASK-6g:后台任务里也**先跑规则层**(0 token,即使没配 LLM key 也能把品牌/星级/类型
    档的行填上并 commit),剩下没命中规则的行才切批问 LLM;``stats`` 因此多一个
    ``rule_filled``,``filled`` = 规则命中数 + LLM 补上的数。
    """
    stats: dict[str, Any] = {
        "scanned": 0, "filled": 0, "rule_filled": 0, "batches": 0, "pending": 0,
        "provider": "未配置",
    }
    ids = [int(item) for item in (stay_ids or ()) if item is not None]
    if not ids or engine is None:
        return stats
    from db.base import session_factory  # 延迟导入:后台线程才需要,避免服务层 import 副作用

    session = session_factory(engine)()
    try:
        rows = list(session.scalars(select(Stay).where(Stay.id.in_(ids))).all())
        stats["scanned"] = len(rows)
        pending = stays_needing_price(rows)
        stats["pending"] = len(pending)
        if not pending:
            return stats
        stats["rule_filled"] = apply_rule_prices(pending)
        remaining = rows_without_price(pending)
        if stats["rule_filled"]:
            session.commit()
        if not remaining:
            stats["filled"] = stats["rule_filled"]
            stats["provider"] = RULE_PROVIDER_LABEL
            return stats
        llm = client if client is not None else resolve_price_llm(environ)
        # ``getattr``:注入的假客户端(单测)可以没有 label,不能因此把整个回填任务打死
        stats["provider"] = str(getattr(llm, "label", "") or "") if llm.enabled else "未配置"
        if not llm.enabled:
            stats["filled"] = stats["rule_filled"]
            return stats
        stats["batches"] = len(price_batches(remaining, batch_size=batch_size))
        stats["filled"] = stats["rule_filled"] + fill_prices_batched(
            remaining, client=llm, environ=environ, batch_size=batch_size
        )
        if stats["filled"]:
            session.commit()
    except Exception:  # noqa: BLE001 - 后台回填失败不得影响已经返回的响应
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 - 回滚失败也不该把线程打死
            pass
    finally:
        session.close()
    return stats


def schedule_price_fill(
    session: Session,
    stays: Sequence[Any],
    *,
    executor: Optional[Executor] = None,
    llm: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
    batch_size: int = PRICE_BATCH_SIZE,
) -> bool:
    """把缺价格的行交给后台**批量**回填;返回是否真的排上了(``estimating`` 就用它)。

    排不上的三种情况都返回 ``False``(前端不显示"估价中"):没有待估行、
    没配 LLM key(排了也白排)、执行器拒收。
    """
    pending = stays_needing_price(stays)
    ids = [int(_get(stay, "id")) for stay in pending if _get(stay, "id") is not None]
    if not ids:
        return False
    if llm is None and not resolve_price_llm(environ).enabled:
        return False
    engine = session.get_bind()
    runner = executor if executor is not None else background_executor()
    try:
        runner.submit(
            price_fill_job, engine, ids, client=llm, environ=environ, batch_size=batch_size
        )
    except Exception:  # noqa: BLE001 - 排不上就算了,价格留 null,下次请求再试
        return False
    return True


def estimate_mode(
    requested: Optional[str],
    *,
    pending_count: int,
) -> str:
    """决定这次的估价口径:``sync``(就地逐家算)/ ``async``(后台批量)/ ``off``(不算)。

    * 显式 ``estimate=sync|async|off`` 照办(没有待估行时一律 ``off``);
    * ``auto``(默认):**≤ 一批(5 家)就地算完再返回** —— 首屏就有价格,交互延迟可接受;
      **超过一批转后台批量回填** —— 一次 30 km 检索可能几十家,逐家串行能把请求拖到超时(BUG-3);
    * ``auto`` 的同步档沿用 TASK-3a1 的**逐家**口径(:func:`estimate_missing`),既有单测与
      既有 API 行为零改动;只有转后台时才换成 5 家/prompt 的批量口径。
    """
    mode = str(requested or ESTIMATE_AUTO).strip().lower()
    if mode not in ESTIMATE_MODES:
        mode = ESTIMATE_AUTO
    if pending_count <= 0:
        return ESTIMATE_OFF
    if mode in (ESTIMATE_SYNC, ESTIMATE_ASYNC, ESTIMATE_OFF):
        return mode
    if pending_count <= SYNC_ESTIMATE_MAX_ROWS:
        return ESTIMATE_SYNC
    return ESTIMATE_ASYNC


# --------------------------------------------------------------------------- #
# 入库 / 读库
# --------------------------------------------------------------------------- #


def select_stays(
    session: Session, lat: float, lng: float, radius_m: float = DEFAULT_RADIUS_M
) -> list[Stay]:
    """半径内的住宿行:SQL 先按经纬度**包围盒**粗筛,再用 haversine 精确复核 + 由近及远排序。

    住宿表按 OSM 身份存,不带起点城市/分段,所以"某坐标半径内"只能这样按坐标筛;
    包围盒让 SQLite 走 ``ix_stay_location`` 索引,不必全表算 haversine。
    """
    origin = coordinate_pair(lat, lng)
    if origin is None:
        return []
    origin_lat, origin_lng = origin
    radius = _as_float(radius_m)
    if radius is None or radius <= 0:
        return []
    lat_delta = radius / METERS_PER_DEGREE
    lng_delta = radius / (METERS_PER_DEGREE * max(MIN_COS_LAT, abs(math.cos(math.radians(origin_lat)))))
    rows = session.scalars(
        select(Stay).where(
            Stay.lat.between(origin_lat - lat_delta, origin_lat + lat_delta),
            Stay.lng.between(origin_lng - lng_delta, origin_lng + lng_delta),
        )
    ).all()
    matched: list[tuple[float, Stay]] = []
    for row in rows:
        raw = _raw_distance(origin_lat, origin_lng, row.lat, row.lng)
        if raw is None or raw > radius / 1000.0:
            continue
        matched.append((raw, row))
    matched.sort(key=lambda item: (item[0], item[1].id))
    return [row for _, row in matched]


def upsert_stays(session: Session, rows: Sequence[Mapping[str, Any]]) -> int:
    """按 ``(osm_type, osm_id)`` 幂等入库,返回写入行数;**不覆盖已生成的价格与简介**。

    刷新的是"抓取事实"(名称/类型/坐标/标签/距离/``fetched_at``);LLM 产物
    (``price_estimate``/``intro``)只在**该行还空着**时才写,重抓不冲掉已花的 token。
    只 ``flush`` 不 ``commit``:提交时机交给调用方(编排函数/脚本)。
    """
    written = 0
    for row in rows or ():
        identity = stay_identity(row)
        if identity is None:
            continue
        point = coordinate_pair(_get(row, "lat"), _get(row, "lng"))
        if point is None:
            continue
        lat, lng = point
        osm_type, osm_id = identity
        stay = session.scalar(select(Stay).where(Stay.osm_type == osm_type, Stay.osm_id == osm_id))
        created = stay is None
        if created:
            stay = Stay(osm_type=osm_type, osm_id=osm_id)
            session.add(stay)

        tags = _get(row, "tags")
        stay.name = clean_text(_get(row, "name"), limit=NAME_LEN) or ""
        stay.kind = normalize_kind(row)
        stay.lat = lat
        stay.lng = lng
        stay.tags = dict(tags) if isinstance(tags, Mapping) else {}
        distance = _as_float(_get(row, "distance_km"))
        if created or distance is not None:
            stay.distance_km = None if distance is None else round(distance, DISTANCE_PRECISION)
        if created or not str(stay.price_estimate or "").strip():
            stay.price_estimate = clean_text(_get(row, "price_estimate"), limit=PRICE_LEN)
            # 出处标记跟着价格走:价格没被覆盖时标记也不动(重抓不冲掉已有结论)
            stay.price_kind = normalize_price_kind(_get(row, "price_kind")) or None
        if created or not str(stay.intro or "").strip():
            stay.intro = clean_text(_get(row, "intro"))
        currency = clean_text(_get(row, "currency"), limit=CURRENCY_LEN)
        if currency:
            stay.currency = currency.upper()
        elif created:
            stay.currency = DEFAULT_CURRENCY
        stay.fetched_at = utcnow()
        written += 1

    if written:
        session.flush()
    return written


def stay_to_dict(
    stay: Stay,
    *,
    origin_lat: Optional[float] = None,
    origin_lng: Optional[float] = None,
    source: str = SOURCE_DB,
) -> dict[str, Any]:
    """序列化(:func:`db.repository.place_to_dict` 同风格):坐标定点、时间 ISO、带来源与估算标注。

    给了起点就按起点**现算** ``distance_km``(库里的值只是上次检索的快照);
    ``price_is_estimate`` 是估算标注字段 —— 价格是 LLM 的常识性区间,不是报价。
    """
    computed = (
        distance_km(origin_lat, origin_lng, stay.lat, stay.lng)
        if origin_lat is not None and origin_lng is not None
        else None
    )
    distance = computed if computed is not None else _as_float(stay.distance_km)
    return {
        "id": stay.id,
        "osm_type": stay.osm_type,
        "osm_id": stay.osm_id,
        "name": stay.name,
        "kind": stay.kind,
        "lat": coordinate(stay.lat),
        "lng": coordinate(stay.lng),
        "tags": dict(stay.tags or {}),
        "distance_km": None if distance is None else round(distance, DISTANCE_PRECISION),
        "price_estimate": stay.price_estimate or None,
        "price_is_estimate": bool(stay.price_estimate),
        "price_kind": normalize_price_kind(stay.price_kind) or None,
        "currency": stay.currency or DEFAULT_CURRENCY,
        "intro": stay.intro or None,
        "fetched_at": iso_utc(stay.fetched_at),
        "source": source,
    }


@dataclass(frozen=True)
class StaySearchResult:
    """一次住宿检索的**完整结论**:列表 + 判别信息(TASK-6c)。

    ``items`` 的形状与 :func:`stay_to_dict` 一致;``reason`` / ``nearest_km`` /
    ``estimating`` 放在**外层**而不是塞进每一行 —— 空结果时根本没有行可挂,
    而前端三档文案(no_data / datasource_error / timeout)恰恰只在空结果时要紧(BUG-3)。

    * ``reason``:``None`` = 正常拿到结果;否则是三档之一(见 :data:`db.models.STAY_REASONS`)。
    * ``nearest_km``:最近一家的距离(km,1 位);空结果时是**库里已知**的最近一家。
    * ``estimating``:后台正在批量回填价格(``price_estimate`` 此刻可能还是 null)。
    * ``radius_m``:实际生效的半径(阶梯可能扩过档);``requested_radius_m``:调用方显式给的。
    * ``expanded``:是否扩过档;``from_cache``:本次结论是否来自缓存(DB 正缓存 / 负缓存)。
    """

    items: list[dict[str, Any]]
    reason: Optional[str] = None
    nearest_km: Optional[float] = None
    estimating: bool = False
    source: str = SOURCE_DB
    radius_m: Optional[int] = None
    requested_radius_m: Optional[int] = None
    expanded: bool = False
    from_cache: bool = False


class StayItems(list):
    """:func:`load_or_fetch_stays` 的返回值:**仍然是 list**(既有调用方与断言零改动),
    只是额外挂了本次检索的判别信息(``reason`` / ``nearest_km`` / ``estimating`` / ``source`` …)。

    API 侧用 ``getattr`` 取,拿到普通 ``list``(例如单测里替换成的假返回值)也能降级成
    "没有判别信息",不会 KeyError。
    """

    reason: Optional[str] = None
    nearest_km: Optional[float] = None
    estimating: bool = False
    source: str = SOURCE_DB
    radius_m: Optional[int] = None
    requested_radius_m: Optional[int] = None
    expanded: bool = False
    from_cache: bool = False

    @classmethod
    def from_result(cls, result: StaySearchResult) -> "StayItems":
        """把 :class:`StaySearchResult` 摊成"带属性的列表"。"""
        items = cls(list(result.items))
        items.reason = result.reason
        items.nearest_km = result.nearest_km
        items.estimating = bool(result.estimating)
        items.source = result.source
        items.radius_m = result.radius_m
        items.requested_radius_m = result.requested_radius_m
        items.expanded = bool(result.expanded)
        items.from_cache = bool(result.from_cache)
        return items


def _nearest_of(rows: Sequence[Any], origin_lat: float, origin_lng: float) -> Optional[float]:
    """已由近及远排好序的行 → 最近一家的距离(km,1 位);空 → ``None``。"""
    if not rows:
        return None
    raw = _raw_distance(origin_lat, origin_lng, _get(rows[0], "lat"), _get(rows[0], "lng"))
    return None if raw is None else round(raw, NEAREST_KM_PRECISION)


def _finish_rows(
    session: Session,
    rows: Sequence[Stay],
    origin: tuple[float, float],
    radius_m: int,
    *,
    source: str,
    llm: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
    executor: Optional[Executor] = None,
    estimate: Optional[str] = None,
    reason: Optional[str] = None,
    expanded: bool = False,
    from_cache: bool = False,
    requested_radius_m: Optional[int] = None,
) -> StaySearchResult:
    """把库里的行按 :func:`estimate_mode` 的口径估完价(或排到后台),再序列化返回。"""
    origin_lat, origin_lng = origin
    pending = stays_needing_price(rows)
    # 规则层前置(TASK-6g):命中品牌/星级/类型档的行**当场 0 token 补价**并 commit,
    # 剩下的才进 LLM 队列 —— 全命中时 ``pending`` 变空,mode=off、estimating=False,
    # 既不调 LLM 也不排后台任务。
    if apply_rule_prices(pending):
        session.commit()
    pending = rows_without_price(pending)
    mode = estimate_mode(estimate, pending_count=len(pending))
    estimating = False
    if mode == ESTIMATE_SYNC:
        if estimate_missing(rows, client=llm, environ=environ):
            session.commit()
    elif mode == ESTIMATE_ASYNC:
        estimating = schedule_price_fill(
            session, pending, executor=executor, llm=llm, environ=environ
        )
    items = [
        stay_to_dict(row, origin_lat=origin_lat, origin_lng=origin_lng, source=source)
        for row in rows
    ]
    return StaySearchResult(
        items=items,
        reason=reason,
        nearest_km=_nearest_of(rows, origin_lat, origin_lng),
        estimating=estimating,
        source=source,
        radius_m=int(radius_m),
        requested_radius_m=requested_radius_m,
        expanded=expanded,
        from_cache=from_cache,
    )


def load_stays(
    session: Session,
    lat: float,
    lng: float,
    *,
    radius_m: Optional[int] = None,
    refresh: bool = False,
    fetch_fn: Optional[StayFetchFn] = None,
    llm: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
    executor: Optional[Executor] = None,
    estimate: Optional[str] = None,
) -> StaySearchResult:
    """住宿检索的**主编排**(TASK-6c 口径),返回带判别信息的 :class:`StaySearchResult`。

    每一档半径按这个顺序走(**绝不抛异常**):

    1. 正缓存:库里有 ≥ :data:`MIN_CACHED_ROWS` 行 → 直接读库返回(``source="db"``,零网络);
    2. 负缓存::data:`NEG_CACHE_TTL_S`(6h)内查过且是空/失败 → 直接回缓存态(零网络);
    3. 检索:成功有货 → upsert + commit(先落库,后台线程才看得见)→ 估价 → 返回;
       成功但空 → 写负缓存(``no_data``),**未显式给半径时**按阶梯扩到下一档;
       失败 → 写负缓存(``datasource_error`` / ``timeout``)并**停止扩档**(端点已挂,
       再打两遍只是白等),库里有货仍按 ``"db"`` 返回,不谎报来源。

    ``refresh=True`` 跳过 1、2 两层缓存强制重查;``estimate`` 见 :func:`estimate_mode`;
    ``executor`` 可注入(测试/CLI 用 :data:`INLINE_EXECUTOR` 把后台回填变成同步)。
    """
    ladder = radius_ladder(radius_m)
    requested = None if radius_m is None else (ladder[0] if ladder else None)
    origin = coordinate_pair(lat, lng)
    if origin is None or not ladder:
        return StaySearchResult(items=[], requested_radius_m=requested)
    origin_lat, origin_lng = origin

    for index, rung in enumerate(ladder):
        expanded = index > 0
        if not refresh:
            db_rows = select_stays(session, origin_lat, origin_lng, rung)
            if len(db_rows) >= MIN_CACHED_ROWS:
                return _finish_rows(
                    session, db_rows, origin, rung,
                    source=SOURCE_DB, llm=llm, environ=environ, executor=executor,
                    estimate=estimate, expanded=expanded, from_cache=True,
                    requested_radius_m=requested,
                )
            cached = lookup_negative(session, origin_lat, origin_lng, rung)
            if cached is not None:
                state = negative_state(cached)
                return StaySearchResult(
                    items=[],
                    reason=state["reason"],
                    nearest_km=state["nearest_km"],
                    source=SOURCE_DB,
                    radius_m=rung,
                    requested_radius_m=requested,
                    expanded=expanded,
                    from_cache=True,
                )

        rows, reason = search_stays_detailed(origin_lat, origin_lng, rung, fetch_fn=fetch_fn)
        if reason in (REASON_DATASOURCE_ERROR, REASON_TIMEOUT):
            db_rows = select_stays(session, origin_lat, origin_lng, rung)
            nearest = (
                _nearest_of(db_rows, origin_lat, origin_lng)
                if db_rows
                else nearest_known_km(session, origin_lat, origin_lng)
            )
            if not db_rows:
                remember_negative(
                    session, origin_lat, origin_lng, rung, reason=reason, nearest_km=nearest
                )
                session.commit()
                return StaySearchResult(
                    items=[], reason=reason, nearest_km=nearest, source=SOURCE_DB,
                    radius_m=rung, requested_radius_m=requested, expanded=expanded,
                )
            return _finish_rows(
                session, db_rows, origin, rung,
                source=SOURCE_DB, llm=llm, environ=environ, executor=executor,
                estimate=estimate, reason=reason, expanded=expanded,
                requested_radius_m=requested,
            )

        if rows:
            upsert_stays(session, rows)
            db_rows = select_stays(session, origin_lat, origin_lng, rung)
            # 先提交事实行:后台回填线程用的是**另一个连接**,看不见未提交的数据
            session.commit()
            return _finish_rows(
                session, db_rows, origin, rung,
                source=SOURCE_FETCH, llm=llm, environ=environ, executor=executor,
                estimate=estimate, expanded=expanded, requested_radius_m=requested,
            )

        # 检索成功但半径内真没有:refresh 时库里的老货照旧返回(不因为一次空检索就清空展示)
        db_rows = select_stays(session, origin_lat, origin_lng, rung) if refresh else []
        if db_rows:
            return _finish_rows(
                session, db_rows, origin, rung,
                source=SOURCE_DB, llm=llm, environ=environ, executor=executor,
                estimate=estimate, expanded=expanded, requested_radius_m=requested,
            )
        remember_negative(
            session, origin_lat, origin_lng, rung,
            reason=REASON_NO_DATA,
            nearest_km=nearest_known_km(session, origin_lat, origin_lng),
        )
        session.commit()

    nearest = nearest_known_km(session, origin_lat, origin_lng)
    return StaySearchResult(
        items=[],
        reason=REASON_NO_DATA,
        nearest_km=nearest,
        source=SOURCE_DB,
        radius_m=ladder[-1],
        requested_radius_m=requested,
        expanded=len(ladder) > 1,
    )


def load_or_fetch_stays(
    session: Session,
    lat: float,
    lng: float,
    *,
    radius_m: Optional[int] = None,
    refresh: bool = False,
    fetch_fn: Optional[StayFetchFn] = None,
    llm: Optional[LLMClient] = None,
    environ: Optional[Mapping[str, str]] = None,
    executor: Optional[Executor] = None,
    estimate: Optional[str] = None,
) -> StayItems:
    """读库优先的住宿列表(**DB 即缓存**),每项带 ``distance_km``(haversine,2 位)。

    既有入口,签名与返回类型向后兼容:返回的还是"行的列表"(空结果就是 ``[]``),
    只是换成了 :class:`StayItems` —— 额外挂着 ``reason`` / ``nearest_km`` / ``estimating``
    供 API 透传。要拿完整结论(:class:`StaySearchResult`)就用 :func:`load_stays`。

    ``radius_m=None``(调用方没给半径)走 :data:`STAY_RADIUS_LADDER_M` 阶梯;
    给了就只查那一档。起点非法或两边都拿不到数据 → 空列表(降级,不抛)。
    """
    return StayItems.from_result(
        load_stays(
            session,
            lat,
            lng,
            radius_m=radius_m,
            refresh=refresh,
            fetch_fn=fetch_fn,
            llm=llm,
            environ=environ,
            executor=executor,
            estimate=estimate,
        )
    )


def main(argv: Optional[list[str]] = None) -> int:
    """CLI:抓一个坐标半径内的住宿并入库估价。``python -m services.stays 31.2304 121.4737``"""
    parser = argparse.ArgumentParser(description="检索/入库周边住宿(带 LLM 价格估算与简介)")
    parser.add_argument("lat", type=float, help="起点纬度")
    parser.add_argument("lng", type=float, help="起点经度")
    parser.add_argument("--radius", type=int, default=DEFAULT_RADIUS_M, help=f"半径(米,默认 {DEFAULT_RADIUS_M})")
    parser.add_argument(
        "--ladder", action="store_true",
        help="不给死半径,按 5→10→30 km 阶梯自动扩(只在空结果时扩,扩到即停)",
    )
    parser.add_argument("--refresh", action="store_true", help="忽略库缓存,强制重新检索")
    parser.add_argument(
        "--estimate", default=ESTIMATE_AUTO, choices=list(ESTIMATE_MODES),
        help="估价口径:auto(默认,>5 家转后台批量)/ sync / async / off",
    )
    parser.add_argument(
        "--no-wait", action="store_true",
        help="后台批量估价不等待(CLI 默认用同步执行器,回填完再打印)",
    )
    parser.add_argument("--limit", type=int, default=None, help="最多显示多少条(默认:全部)")
    parser.add_argument("--db", default=None, help="数据库 URL(默认 WHERE2GO_DB_URL 或 backend/data/where2go.db)")
    args = parser.parse_args(argv)

    from db import init_db, make_engine, open_session

    engine = make_engine(args.db)
    init_db(engine)
    with open_session(engine) as session:
        result = load_stays(
            session,
            args.lat,
            args.lng,
            radius_m=None if args.ladder else args.radius,
            refresh=args.refresh,
            estimate=args.estimate,
            executor=None if args.no_wait else INLINE_EXECUTOR,
        )
    stays = result.items
    shown = stays if args.limit is None else stays[: max(0, int(args.limit))]
    for item in shown:
        distance = item["distance_km"]
        price = item["price_estimate"] or "未估价"
        print(
            f"[{item['kind'] or '住宿'}] {item['name'] or '(无名)'} · "
            f"{'-' if distance is None else f'{distance:.1f} km'} · {price}(估算) · {item['source']}"
        )
    tail = f"[完成] {len(shown)} / {len(stays)} 条 · 半径 {result.radius_m or args.radius} m"
    if result.reason:
        tail += f" · reason={result.reason}"
    if result.nearest_km is not None:
        tail += f" · 最近一家 {result.nearest_km} km"
    if result.estimating:
        tail += " · 估价后台回填中"
    print(tail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
