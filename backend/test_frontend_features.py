"""神朱 2026-09-27 功能 1~4 的前端静态断言(``app/static/index.html``)。

页面是原生单文件 JS,这里同样**不跑浏览器**,只钉住四件事:

1. **功能1(0-50km)**:分段下拉仍然由 ``/api/places/meta`` 驱动(前端不硬编码分段);
2. **功能2(AI 推荐)**:存在调 ``/api/places/recommend`` 的加载函数与"重新推荐"入口,
   地图默认只画推荐点,另有"显示全部"开关;
3. **功能3(列表)**:地图下方有列表容器;列表先渲染骨架(名称/分类/距离)、
   长介绍走 ``/api/places/details`` **分批懒加载**并有进度提示;推荐条置顶;
4. **功能4(收藏)**:目的地 popup 里有收藏按钮(kind=place);收藏面板把
   路线/住宿收藏**挂在目的地下方**(两层),住宿收藏带 ``parent``。

外加:内联脚本过 ``node --check`` 语法校验(装了 node 才跑)。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.main import STATIC  # noqa: E402

INDEX_HTML = STATIC / "index.html"

# 新功能的 DOM id
FEATURE_IDS = [
    "placeList",        # 地图下方的目的地列表容器
    "placeListBody",    # 列表内容
    "placeListMsg",     # 列表状态/进度提示(生成介绍进度、失败原因)
    "recoMsg",          # AI 推荐状态(失败/降级原因)
    "recoRefresh",      # 「重新推荐」按钮
    "pinScope",         # 地图显示范围开关(仅推荐 / 全部)
    "detailMore",       # 「继续生成介绍」按钮
]
# 新功能的 JS 函数
FEATURE_FUNCTIONS = [
    "loadRecommend",       # GET /api/places/recommend
    "renderPlaceList",     # 渲染地图下方列表(推荐置顶)
    "placeRowHtml",        # 单行:名称/分类/距离/介绍/收藏与路线入口
    "loadDetails",         # GET /api/places/details 分批懒加载
    "detailTextOf",        # 取某条目的长介绍(缺则空)
    "renderPinsByScope",   # 按显示范围画针(仅推荐 / 全部)
    "placeFavItem",        # 已收藏判定键(kind=place + OSM 身份)
    "placeFavButtonHtml",  # popup/列表里的收藏按钮
    "addFavPlace",         # POST /api/collections(kind=place)
    "syncPlaceFavButtons", # 收藏状态同步
    "favTree",             # 收藏面板两层分组:目的地 → 路线/住宿
    "stayParentPayload",   # 住宿收藏带上父级(功能4)
]

# 前后端契约里的字段名(必须真的被前端用到,后端改字段名时这里当场红)
RECO_FIELDS = ["items", "reason", "rank", "degraded", "basis", "cached", "provider", "detail"]
DETAIL_FIELDS = ["items", "place_id", "text", "filled", "pending", "reason"]
COLLECTION_PARENT_FIELDS = ["parent", "ref_key", "kind", "name"]


def read_page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def inline_script(page: str) -> str:
    blocks = re.findall(r"<script>(.*?)</script>", page, re.DOTALL)
    return max(blocks, key=len) if blocks else ""


def test_new_dom_ids_are_present() -> None:
    page = read_page()
    for dom_id in FEATURE_IDS:
        assert re.search(rf'id="{dom_id}"', page), f"缺少新增 DOM id:{dom_id}"


def test_new_functions_are_defined() -> None:
    page = read_page()
    for name in FEATURE_FUNCTIONS:
        assert re.search(rf"function {name}\s*\(", page), f"缺少新增函数:{name}"


def test_frontend_calls_recommend_and_details_endpoints() -> None:
    page = read_page()
    assert "/api/places/recommend" in page, "AI 推荐必须调 /api/places/recommend"
    assert "/api/places/details" in page, "长介绍必须调 /api/places/details"
    for field in RECO_FIELDS:
        assert re.search(rf"\.{field}\b|\b{field}\s*[:=]", page), f"推荐响应字段 {field} 没被前端用到"
    for field in DETAIL_FIELDS:
        assert re.search(rf"\.{field}\b|\b{field}\s*[:=]", page), f"长介绍响应字段 {field} 没被前端用到"


def test_band_select_is_still_meta_driven() -> None:
    """功能1:前端不硬编码分段,0_50 由 /api/places/meta 带出来。"""
    page = read_page()
    assert "state.meta.bands" in page, "分段下拉仍应读 state.meta.bands"
    assert '"0_50"' not in page and "'0_50'" not in page, "前端不该硬编码分段 key"


def test_map_shows_recommended_by_default_with_scope_toggle() -> None:
    page = read_page()
    assert "pinScope" in page and re.search(r"renderPinsByScope\s*\(", page), \
        "地图画针必须走显示范围开关"
    assert re.search(r"state\.pinScope", page), "显示范围应存在 state 里(默认仅推荐)"


def test_place_list_puts_recommended_first_and_lazy_loads_details() -> None:
    page = read_page()
    script = inline_script(page)
    assert "renderPlaceList" in script and "placeListBody" in script
    assert re.search(r"detailMore", page), "要有「继续生成介绍」入口"
    # 懒加载:分批(带 limit)调 details,而不是一次性全量
    assert re.search(r"/api/places/details[^\"']*", page)
    assert re.search(r'limit:?\s*String\(|limit=\+?', page) or "limit:" in page, \
        "长介绍要分批(带 limit 参数)"


def test_place_favorite_button_and_two_level_fav_panel() -> None:
    page = read_page()
    assert re.search(r"function addFavPlace\s*\(", page), "目的地收藏必须走 POST /api/collections"
    assert re.search(r'kind:\s*"place"', page), "目的地/住宿收藏都用 kind=place"
    assert re.search(r"function favTree\s*\(", page), "收藏面板要按目的地做两层分组"
    assert "summary.parent" in page or ".parent" in page, "父级标注(路线/住宿挂在目的地下)要用到"


def test_stay_favorite_payload_carries_parent() -> None:
    page = read_page()
    assert re.search(r"function stayParentPayload\s*\(", page)
    # 住宿收藏的 payload 里必须出现 parent
    block = re.search(r"function addFavStay[\s\S]*?\n}", page)
    assert block, "addFavStay 应存在"
    assert "stayParentPayload" in block.group(0) or "parent" in block.group(0), \
        "住宿收藏要带上父级目的地(功能4:住宿挂在目的地下)"


def test_popup_has_place_favorite_button() -> None:
    page = read_page()
    block = re.search(r"function popupHtml[\s\S]*?\n}", page)
    assert block, "popupHtml 应存在"
    assert "placeFavButtonHtml" in block.group(0), "目的地信息框里要有收藏按钮"


def test_inline_script_passes_node_syntax_check() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("环境没有 node,跳过前端语法校验")
    script = inline_script(read_page())
    assert script, "没找到内联脚本"
    tmp = Path("/tmp") / "where2go_inline_check.js"
    tmp.write_text(script, encoding="utf-8")
    result = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True)
    assert result.returncode == 0, f"内联脚本语法错误:\n{result.stderr}"


def test_no_inline_handlers_or_alert_regression() -> None:
    """零回退:仍然没有内联 onclick / alert / document.write(与既有前端约定一致)。"""
    page = read_page()
    assert "onclick=" not in page
    assert not re.search(r"\balert\s*\(", page)
    assert "document.write" not in page
