"""TASK-2b 单测:前端路线面板(``app/static/index.html``)的轻量断言。

页面是**原生 JS 单文件**(Leaflet 走 CDN,没有构建步骤),所以这里不跑浏览器,
只做三类**离线**断言:

1. **DOM / JS 面**:路线面板的容器与控件 id、卡片渲染与画线相关函数名都在;
   跳转链接是 ``<a target="_blank" rel="noopener">``;没有内联 ``onclick=``、
   没有 ``alert``/``document.write``;面板默认 ``display:none``,只在打开时显示。
2. **前后端契约**:用**替身 router**(不触网)真跑一遍 :func:`app.api.routes.list_routes`,
   把前端在路线面板那一段里引用到的 ``data.*`` / ``route.*`` / ``link.*`` 字段名
   逐个对回响应里的真实键 —— 后端改字段名而前端没跟上(或反过来)时,这里当场红。
   同时校验前端抽稀上限不比后端 ``GEOMETRY_MAX_POINTS`` 松。
3. **零回退**:阶段1a/1b/1c 的分类 pin、popup 简介、band 切换、城市搜索、
   「📍 我的位置」「补简介」相关 id 与函数一个不少;Leaflet CDN + SRI 仍在。

若环境里装了 ``node``,再加一条 ``node --check`` 对内联脚本做**语法**校验
(仍不触网、不需要浏览器);没装就 skip。

运行:``python -m pytest backend/ -q``
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import requests

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api import collections as collections_api  # noqa: E402
from app.api import routes as routes_api  # noqa: E402
from app.api import trips as trips_api  # noqa: E402
from app.main import STATIC, app as fastapi_app  # noqa: E402
from db.base import init_db, make_engine, session_factory  # noqa: E402
from services import routes as route_service  # noqa: E402
from services import trips as trip_service  # noqa: E402

INDEX_HTML = STATIC / "index.html"
# 路线面板那一段 JS 的切片边界(前面是 popup/identity,后面是 drawPins)
# 契约断言的 JS 切片:从「路线面板」常量段起,到 drawPins 之前(含 openRoutePanel/画线/格式化)
PANEL_START = "路线面板(TASK-2b,阶段2b)"
PANEL_END = "function drawPins("

# 面板必须有的 DOM id:容器 + 标题/副标题 + 状态提示 + 重试 + 卡片 + 图例 + 时效脚注 + 关闭
PANEL_IDS = [
    "mapWrap", "map", "routePanel", "routePanelTitle", "routePanelSub", "routePanelMsg",
    "routeRetry", "routeCards", "routePanelLegend", "routePanelMeta", "routePanelClose",
]
# 面板相关的 JS 函数(展示 / 画线 / 状态 / 格式化)
PANEL_FUNCTIONS = [
    "openRoutePanel", "closeRoutePanel", "loadRoutes", "applyRouteData", "renderRouteCards",
    "routeCardHtml", "routeLinksHtml", "selectRouteMode", "drawRouteLine", "clearRouteLine",
    "thinPoints", "arcPoints", "setRouteMsg", "renderRouteMeta", "fmtDuration", "fmtCost",
    "fmtGeneratedAt", "routeEnds",
]
# 阶段1a/1b/1c 既有功能的 id 与函数(零回退清单)
STAGE1_IDS = ["city", "cityList", "goCity", "locate", "band", "category", "reload", "intro",
              "status", "legend", "note", "err", "map"]
STAGE1_FUNCTIONS = ["initMap", "renderBandOptions", "renderCategoryOptions", "renderLegend",
                    "drawRings", "fitRings", "popupHtml", "identityHtml", "drawPins",
                    "loadMeta", "searchCity", "applyOrigin", "locateMe", "onLocated",
                    "onLocateFailed", "loadPlaces", "fillIntros", "categoryMeta"]
# 后端每条路线的键(services/routes.py 的路线形状),前端应逐字段用到
ROUTE_FIELDS = ["mode", "label", "emoji", "duration_min", "cost_cny", "distance_km",
                "geometry", "kind", "degraded", "note", "links"]
LINK_FIELDS = ["provider", "label", "url", "note"]

SHANGHAI = {"lat": "31.2304", "lng": "121.4737", "name": "上海"}
BEIJING = {"lat": "39.9042", "lng": "116.4074", "name": "北京"}   # 直线约 1067km → 三方式都出现


class FakeRouter:
    """OSRM 替身:返回一条带**长 geometry** 的驾车 leg(前端抽稀逻辑才有意义),不触网。"""

    def __init__(self, *, distance_km: float = 1216.4, duration_min: float = 812.0,
                 points: int = 3000) -> None:
        self.leg = {
            "distance_km": distance_km,
            "duration_min": duration_min,
            "geometry": [[SHANGHAI_LAT + (BEIJING_LAT - SHANGHAI_LAT) * i / (points - 1),
                          SHANGHAI_LNG + (BEIJING_LNG - SHANGHAI_LNG) * i / (points - 1)]
                         for i in range(points)],
        }
        self.calls = 0

    def __call__(self, start_lnglat: Any, end_lnglat: Any) -> dict[str, Any]:
        self.calls += 1
        return dict(self.leg)


SHANGHAI_LAT, SHANGHAI_LNG = float(SHANGHAI["lat"]), float(SHANGHAI["lng"])
BEIJING_LAT, BEIJING_LNG = float(BEIJING["lat"]), float(BEIJING["lng"])


def read_index() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def panel_section(html: str) -> str:
    """切出路线面板那一段 JS(契约断言只在这一段里找字段引用)。"""
    start = html.index(PANEL_START)
    end = html.index(PANEL_END, start)
    return html[start:end]


def referenced_fields(section: str, variable: str) -> set[str]:
    """找出 ``variable.<字段名>`` 形式的引用(去掉 ``||`` 之类的误匹配)。"""
    return set(re.findall(rf"\b{re.escape(variable)}\.([A-Za-z_][A-Za-z0-9_]*)", section))


def js_constant(html: str, name: str) -> int:
    match = re.search(rf"const\s+{re.escape(name)}\s*=\s*(\d+)", html)
    assert match, f"index.html 里找不到常量 {name}"
    return int(match.group(1))


def inline_script(html: str) -> str:
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(blocks) == 1, "页面应只有一段内联脚本(其余是带 src 的 CDN 引用)"
    return blocks[0]


# --------------------------------------------------------------------------- #
# fixtures:全程不触网
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底:任何 requests 调用都视为测试失败(本套单测必须纯 mock)。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("单测不允许触网:requests.Session.request 被调用")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture(scope="module")
def html() -> str:
    return read_index()


@pytest.fixture()
def payload(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """用替身 router 跑一遍**真实 API 函数**,拿到前端要消费的响应(上海 → 北京,三方式齐全)。"""
    monkeypatch.setattr(route_service, "default_router", FakeRouter())
    return routes_api.list_routes(
        from_lat=SHANGHAI["lat"], from_lng=SHANGHAI["lng"],
        to_lat=BEIJING["lat"], to_lng=BEIJING["lng"],
        to_name=BEIJING["name"], from_name=SHANGHAI["name"],
    )


# --------------------------------------------------------------------------- #
# 1. 页面装配:静态页就是被挂载的那一份,/api/routes 已注册
# --------------------------------------------------------------------------- #


def test_index_html_is_the_mounted_static_page() -> None:
    assert INDEX_HTML.is_file(), f"缺少静态页:{INDEX_HTML}"
    mounts = [route for route in fastapi_app.routes if type(route).__name__ == "Mount"]
    assert mounts, "根路径应挂载 StaticFiles(地图页)"
    directory = getattr(getattr(mounts[-1], "app", None), "directory", None)
    assert directory and Path(directory).resolve() == STATIC.resolve(), "挂载目录应是 app/static"
    assert "/api/routes" in fastapi_app.openapi()["paths"], "前端要调的 /api/routes 应已注册(TASK-2a)"


# --------------------------------------------------------------------------- #
# 2. DOM / JS 面:面板、卡片、画线、跳转
# --------------------------------------------------------------------------- #


def test_route_panel_dom_ids_present(html: str) -> None:
    for element_id in PANEL_IDS:
        assert f'id="{element_id}"' in html, f"缺少路线面板 DOM id:{element_id}"


def test_route_panel_functions_present(html: str) -> None:
    for name in PANEL_FUNCTIONS:
        assert re.search(rf"function\s+{re.escape(name)}\s*\(", html), f"缺少 JS 函数:{name}"


def test_route_panel_is_hidden_until_opened(html: str) -> None:
    assert re.search(r"#routePanel\{[^}]*display:none", html), "面板默认应 display:none(不占地图)"
    assert 'panel.style.display="block"' in html, "打开面板时应显式置 display:block"
    assert 'panel.style.display="none"' in html, "关闭面板时应显式置 display:none"
    assert '<aside id="routePanel"' in html, "面板用 aside 侧栏语义"
    assert html.index('<div id="map">') < html.index('<aside id="routePanel"'), "面板应挂在地图容器里"


def test_pin_click_opens_panel_and_popup_keeps_route_entry(html: str) -> None:
    assert 'marker.on("popupopen"' in html, "点 pin(popup 打开)应触发路线面板"
    assert "openRoutePanel(place)" in html, "popupopen 应调用 openRoutePanel"
    assert "js-routes" in html, "popup 内应保留一个显式的路线入口按钮"
    assert "popupHtml(place,index)" in html, "popup 按钮要靠 pin 序号取回该 Place"
    assert "state.places" in html, "当前渲染的 Place 列表应存进 state 供面板取用"


def test_frontend_calls_api_routes_with_required_params(html: str) -> None:
    assert '"/api/routes?"' in html, "应调用 GET /api/routes"
    section = panel_section(html)
    for param in ("from_lat", "from_lng", "to_lat", "to_lng"):
        assert f"{param}:String(" in section, f"/api/routes 缺少必要参数 {param}"
    for optional in ("to_name", "from_name"):
        assert f'params.set("{optional}"' in section, f"应带上 {optional}(deep-link 文案/URL 用)"
    assert "new URLSearchParams(" in section, "查询串应用 URLSearchParams 拼装(自动百分号编码)"


def test_route_card_shows_icon_duration_cost_note_badge_and_time(html: str, payload: dict[str, Any]) -> None:
    section = panel_section(html)
    assert "routeCardHtml" in section and "rp-ico" in section, "卡片应有图标位"
    for field in ("duration_min", "cost_cny", "distance_km", "note", "kind", "degraded"):
        assert field in section, f"卡片应展示 {field}"
    assert "fmtDuration(" in section and "fmtCost(" in section and "fmtKm(" in section, "数字要格式化"
    assert re.search(r"KIND_BADGE=\{real:.*?estimate:", html, re.S), "应有真实/估算徽标映射"
    assert "fmtGeneratedAt(" in section and "generated_at" in section, "应展示生成时间"
    assert "disclaimer" in section, "应引用后端 cost_model.disclaimer 做「估算」标注"
    assert payload["generated_at"], "后端要给出 generated_at 供前端展示"


def test_deep_links_use_backend_urls_and_open_new_tab(html: str) -> None:
    section = panel_section(html)
    assert '<a class="jump"' in section, "跳转应是 <a>(不是按钮),URL 用后端 links[].url"
    assert 'esc(link.url)' in section, "URL 要过 esc() 再进 href,防注入"
    assert 'target="_blank"' in section, "跳转在新页打开"
    assert 'rel="noopener noreferrer"' in section, "新页必须 rel=noopener"
    assert "link.label" in section and "link.note" in section, "按钮文案/提示用后端给的 label/note"
    assert 'target.closest("#routePanel a")' in html, "点跳转链接不应误触发选卡片/换线"


def test_mode_switch_draws_one_line_and_close_clears_it(html: str) -> None:
    section = panel_section(html)
    assert "L.layerGroup()" in html, "画线应有专用图层"
    assert "routeLayer.clearLayers()" in section, "画线前先清图层 → 同一时刻只有一条线"
    draw = section[section.index("function drawRouteLine("):]
    assert draw.index("clearRouteLine()") < draw.index("L.polyline("), "drawRouteLine 应先清后画"
    close = section[section.index("function closeRoutePanel("):]
    assert "clearRouteLine()" in close, "关闭面板要清线"
    assert "fitBounds(" in section, "切换方式后应把路线 fit 进视野"


def test_driving_uses_geometry_and_others_are_dashed_schematic(html: str) -> None:
    assert "route.geometry" in html, "驾车应优先用后端 geometry 折线"
    assert "L.polyline(" in html, "画线用 L.polyline"
    styles = re.search(r"const ROUTE_LINE_STYLE=\{(.*?)\};", html, re.S)
    assert styles, "应集中定义各方式的线型"
    body = styles.group(1)
    for mode in ("driving", "rail", "flight"):
        assert mode in body, f"线型缺少 {mode}"
    assert body.count("dashArray") == 2, "铁路/飞机用虚线示意,驾车用实线"
    colors = re.findall(r'color:"(#[0-9a-fA-F]{6})"', body)
    assert len(set(colors)) == 3, f"三种方式颜色应互不相同:{colors}"
    assert "arcPoints(" in html, "飞机示意线应有弧线(区别于铁路直线)"


def test_frontend_thinning_is_tighter_than_backend(html: str, payload: dict[str, Any]) -> None:
    limit = js_constant(html, "ROUTE_MAX_POINTS")
    driving = next(item for item in payload["routes"] if item["mode"] == "driving")
    assert len(driving["geometry"]) == route_service.GEOMETRY_MAX_POINTS, "后端已按上限抽稀"
    assert limit <= route_service.GEOMETRY_MAX_POINTS, "前端抽稀上限不应比后端更松"
    assert "首尾必留" in html, "抽稀要保住首尾点(画线不缩水)"


def test_loading_error_and_empty_states_are_friendly(html: str) -> None:
    section = panel_section(html)
    assert "正在规划路线" in section, "加载中要有提示"
    assert "路线查询失败" in section, "失败要有提示(并说明可重试)"
    assert "暂时给不出路线" in section, "后端返回空数组时要有友好提示"
    assert "数据源不可用" in section, "OSRM 降级(degraded)要在卡片上标出来"
    assert "coordsReady(" in section, "坐标缺失/起点未设时给提示而不是抛错"
    assert "token!==state.routePanel.token" in section, "过期响应要丢弃(快速连点 pin 不串台)"
    assert "alert(" not in html and "document.write" not in html, "不用 alert/document.write"


def test_no_inline_event_handlers(html: str) -> None:
    assert "onclick=" not in html, "卡片是运行时生成的,应走事件委托而不是内联 onclick"
    assert 'addEventListener("click",onDocumentClick)' in html, "点击走委托"
    assert 'addEventListener("keydown",onDocumentKeydown)' in html, "键盘可达(Enter/空格/Esc)"


# --------------------------------------------------------------------------- #
# 3. 前后端契约:前端引用的字段必须在真实响应里存在
# --------------------------------------------------------------------------- #


def test_payload_has_three_modes_with_links(payload: dict[str, Any]) -> None:
    modes = [item["mode"] for item in payload["routes"]]
    assert modes == ["driving", "rail", "flight"], f"上海→北京应出三方式,实际 {modes}"
    for item in payload["routes"]:
        missing = [field for field in ROUTE_FIELDS if field not in item]
        assert not missing, f"{item['mode']} 缺字段 {missing}"
        assert item["links"], f"{item['mode']} 应带 deep-link"
        for link in item["links"]:
            assert set(LINK_FIELDS) <= set(link), f"link 缺字段:{link}"
            assert link["url"].startswith("http"), f"link.url 应是绝对 URL:{link['url']}"
    providers = {link["provider"] for item in payload["routes"] for link in item["links"]}
    expected = {route_service.LINK_PROVIDER_AMAP, route_service.LINK_PROVIDER_GOOGLE,
                route_service.LINK_PROVIDER_12306, route_service.LINK_PROVIDER_OTA}
    assert expected <= providers, f"provider 应齐备:{providers}"


def test_frontend_only_reads_fields_the_backend_returns(html: str, payload: dict[str, Any]) -> None:
    section = panel_section(html)
    envelope = referenced_fields(section, "data")
    # detail 是 FastAPI **错误**响应的字段(getJSON 统一读来当报错文案),不在成功响应里
    unknown = envelope - set(payload) - {"detail"}
    assert not unknown, f"前端读了后端没有的响应字段:{sorted(unknown)}(响应键:{sorted(payload)})"
    for key in ("routes", "distance_km", "generated_at"):
        assert key in envelope, f"前端应使用响应里的 {key}"

    routes = payload["routes"]
    route_keys = {key for item in routes for key in item}
    used_route = referenced_fields(section, "route") | referenced_fields(html, "route")
    assert not (used_route - route_keys - {"mode"}), \
        f"前端读了路线里没有的字段:{sorted(used_route - route_keys)}"
    for field in ROUTE_FIELDS:
        assert field in route_keys, f"后端路线缺字段 {field}"
        assert re.search(rf"\broute\.{field}\b", html), f"前端没有消费路线字段 {field}"

    link_keys = {key for item in routes for link in item["links"] for key in link}
    used_link = referenced_fields(section, "link")
    assert not (used_link - link_keys), f"前端读了 link 里没有的字段:{sorted(used_link - link_keys)}"
    for field in ("url", "label"):
        assert field in used_link, f"前端应使用 link.{field}"


def test_panel_uses_backend_origin_and_destination_for_line_ends(html: str, payload: dict[str, Any]) -> None:
    section = panel_section(html)
    assert "data.from" in section and "data.to" in section, "画线端点应优先用后端回传的 from/to"
    assert "state.origin" in section and "panel.place" in section, "后端没给时退回当前起点与该 pin"
    for key in ("from", "to"):
        assert set(payload[key]) >= {"lat", "lng", "name"}, f"响应 {key} 应含 lat/lng/name"


def test_mode_labels_and_line_styles_cover_every_backend_mode(html: str, payload: dict[str, Any]) -> None:
    emoji_map = re.search(r"const ROUTE_MODE_EMOJI=\{(.*?)\};", html, re.S)
    label_map = re.search(r"const ROUTE_MODE_LABEL=\{(.*?)\};", html, re.S)
    assert emoji_map and label_map, "应有方式 → emoji / 文案的兜底映射"
    for item in payload["routes"]:
        assert item["mode"] in emoji_map.group(1), f"emoji 映射缺 {item['mode']}"
        assert item["mode"] in label_map.group(1), f"文案映射缺 {item['mode']}"
        assert re.search(rf"\b{item['mode']}\s*:\s*\{{", html), f"线型缺 {item['mode']}"


def test_badge_kinds_match_backend_kind_values(html: str, payload: dict[str, Any]) -> None:
    kinds = {item["kind"] for item in payload["routes"]}
    assert kinds == {"real", "estimate"}, f"后端 kind 取值应是 real/estimate,实际 {kinds}"
    badge = re.search(r"const KIND_BADGE=\{(.*?)\};", html, re.S)
    assert badge, "应有 kind → 徽标文案映射"
    for kind in kinds:
        assert kind in badge.group(1), f"徽标映射缺 {kind}"
    assert "degraded" in html, "应处理 degraded(OSRM 降级)"


# --------------------------------------------------------------------------- #
# 4. 零回退:阶段1a/1b/1c 既有功能
# --------------------------------------------------------------------------- #


def test_stage1_dom_and_functions_not_regressed(html: str) -> None:
    for element_id in STAGE1_IDS:
        assert f'id="{element_id}"' in html, f"既有 DOM id 丢失:{element_id}"
    for name in STAGE1_FUNCTIONS:
        assert re.search(rf"function\s+{re.escape(name)}\s*\(", html), f"既有 JS 函数丢失:{name}"


def test_stage1_behaviours_still_wired(html: str) -> None:
    assert "/api/places/meta" in html and "/api/places?" in html, "分类/band/pin 数据源不变"
    assert "/api/geocode?city=" in html and "/api/geocode/reverse?" in html, "城市搜索与我的位置不变"
    assert "/api/places/intros?" in html, "「补简介」按钮不变"
    assert 'addEventListener("click",locateMe)' in html, "「📍 我的位置」仍绑定"
    assert 'addEventListener("click",fillIntros)' in html, "「补简介」仍绑定"
    assert "navigator.geolocation" in html, "浏览器定位逻辑仍在"
    assert "L.divIcon(" in html and "categoryMeta(" in html, "pin 仍按分类着色"
    assert "bindPopup(popupHtml(place,index))" in html, "popup 仍走 popupHtml(含简介)"
    assert "leaflet@1.9.4" in html and "integrity=" in html, "Leaflet 仍走 CDN + SRI"
    assert "if(!window.L)" in html, "CDN 挂了的降级提示仍在"


def test_route_layer_does_not_disturb_existing_layers(html: str) -> None:
    assert "rings=L.layerGroup().addTo(map);" in html, "范围圈图层不变"
    assert "pins=L.layerGroup().addTo(map);" in html, "pin 图层不变"
    assert "routeLayer=L.layerGroup().addTo(map);" in html, "画线另开图层,不与 pin/环混用"
    assert "closeRoutePanel();" in html, "重画 pin / 换起点时应收掉面板与线"


# --------------------------------------------------------------------------- #
# 5. 语法校验(有 node 就跑,没有就 skip;不触网、不需要浏览器)
# --------------------------------------------------------------------------- #


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="环境未安装 node,跳过内联脚本语法检查")
def test_inline_js_passes_node_syntax_check(html: str, tmp_path: Path) -> None:
    script = tmp_path / "index_inline.js"
    script.write_text(inline_script(html), encoding="utf-8")
    result = subprocess.run([NODE, "--check", str(script)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"内联脚本语法错误:\n{result.stderr}"


def test_inline_script_is_single_block_and_utf8(html: str) -> None:
    assert inline_script(html), "内联脚本不应为空"
    assert '<meta charset="utf-8">' in html, "中文文案依赖 utf-8 声明"
    assert "ROUTE_MAX_POINTS" in html and "ROUTE_LINE_LEGEND" in html, "画线常量/图例文案在脚本里"


# --------------------------------------------------------------------------- #
# 6. 我的收藏 UI(TASK-2c-fe):卡片收藏按钮 + 收藏弹层 + /api/collections 对接
#    (轻量静态断言:DOM/JS 函数/API 调用都在页面里;交互验证走 browser_exec QA)
# --------------------------------------------------------------------------- #

FAV_SECTION_START = "我的收藏(TASK-2c-fe)"


def fav_section(html: str) -> str:
    start = html.index(FAV_SECTION_START)
    end = html.index('$("goCity").addEventListener', start)
    return html[start:end]


def test_fav_panel_dom_ids_present(html: str) -> None:
    for element_id in ("openFav", "favPanel", "favPanelClose", "favPanelList", "favPanelErr"):
        assert f'id="{element_id}"' in html, f"缺少收藏 UI DOM id:{element_id}"
    assert 'id="favPanel"' in html and 'role="dialog"' in html, "收藏列表应是 dialog 弹层"


def test_fav_panel_hidden_by_default(html: str) -> None:
    assert re.search(r"#favPanel\{[^}]*display:none", html), "收藏弹层默认应 display:none"


def test_fav_functions_present(html: str) -> None:
    section = fav_section(html)
    for name in ("addFavRoute", "deleteFav", "openFavPanel", "closeFavPanel",
                 "renderFavItems", "refreshFavItems", "favButtonHtml", "syncFavButtons",
                 "routeFavPayload", "favItemHtml", "sendJSON"):
        assert re.search(rf"function\s+{re.escape(name)}\s*\(", section), f"缺少收藏 JS 函数:{name}"


def test_fav_card_button_wired_into_route_card(html: str) -> None:
    assert "favButtonHtml(route)" in panel_section(html), "路线卡片 HTML 应包含收藏按钮"
    assert "js-fav" in html and "收藏路线" in html, "收藏按钮的 class 与文案应在页面里"


def test_fav_calls_collections_api(html: str) -> None:
    section = fav_section(html)
    assert 'sendJSON("/api/collections",{method:"POST"' in section, "收藏应 POST /api/collections"
    assert 'getJSON("/api/collections")' in section, "收藏列表应 GET /api/collections"
    assert 'method:"DELETE"' in section and '/api/collections/"' in section, \
        "取消收藏应 DELETE /api/collections/{id}"


def test_fav_payload_carries_required_fields(html: str) -> None:
    section = fav_section(html)
    payload_section = section[section.index("function routeFavPayload"):section.index("function favButtonHtml")]
    for field in ("kind:", "mode:", "from_lat", "from_lng", "to_lat", "to_lng",
                  "duration_min", "cost_cny", "distance_km"):
        assert field in payload_section, f"收藏请求体缺少字段 {field}"
    assert "osm_type" in payload_section and "osm_id" in payload_section, \
        "目的地有 OSM 身份时应带上(后端 ref_key 优先用 OSM 身份)"


def test_fav_click_handling_and_keyboard(html: str) -> None:
    assert 'target.closest("#routeCards .js-fav")' in html, "收藏按钮点击应走事件委托拦截(不误触选卡片)"
    assert 'target.closest("#favPanelList .js-fav-del")' in html, "取消收藏按钮应走事件委托"
    assert "closeFavPanel" in html[html.index("function onDocumentKeydown")
                                   :html.index("function drawPins(")], \
        "Esc 应先关收藏弹层"
    assert '$("openFav").addEventListener("click",openFavPanel)' in html, "入口按钮应绑定 openFavPanel"
    assert '$("favPanelClose").addEventListener("click",closeFavPanel)' in html, "关闭按钮应绑定 closeFavPanel"


def test_fav_item_shows_summary_fields(html: str) -> None:
    section = fav_section(html)
    item_html = section[section.index("function favItemHtml"):section.index("function renderFavItems")]
    for token in ("fmtDuration(summary.duration_min)", "fmtCost(summary.cost_cny)",
                  "fmtKm(summary.distance_km)", "js-fav-del"):
        assert token in item_html, f"收藏列表项缺少展示元素 {token}"
    assert "fp-empty" in section, "空列表应有占位文案"


# --------------------------------------------------------------------------- #
# 7. 住宿区块(TASK-3b):路线面板内「周边住宿」卡片 + AI 预估价强标注 + 收藏住宿
#    (轻量静态断言,同 TASK-2c-fe 模式;交互验证走 browser_exec QA)
# --------------------------------------------------------------------------- #

STAY_SECTION_START = "住宿区块(TASK-3b):openRoutePanel"


def stay_section(html: str) -> str:
    start = html.index(STAY_SECTION_START)
    end = html.index("// 卡片选中与跳转都走事件委托", start)
    return html[start:end]


def test_stay_dom_ids_present(html: str) -> None:
    for element_id in ("staySection", "stayTitle", "staySub", "stayMsg",
                       "stayCards", "stayMore", "stayRetry"):
        assert f'id="{element_id}"' in html, f"缺少住宿区块 DOM id:{element_id}"
    # 住宿区块在路线面板 aside 内(面板开时才有意义),默认 display:none
    panel = html[html.index('<aside id="routePanel"'):html.index("</aside>")]
    assert 'id="staySection"' in panel, "住宿区块应放在路线面板内"
    assert 'id="staySection" style="display:none"' in html, "住宿区块默认应隐藏"


def test_stay_functions_present(html: str) -> None:
    section = stay_section(html)
    for name in ("openStaySection", "closeStaySection", "loadStays", "applyStayData",
                 "renderStayCards", "stayCardHtml", "setStayMsg", "staysCacheKey",
                 "stayFavButtonHtml", "syncStayFavButtons", "addFavStay", "stayFavItem"):
        assert re.search(rf"function\s+{re.escape(name)}\s*\(", section), f"缺少住宿 JS 函数:{name}"


def test_stay_calls_api(html: str) -> None:
    section = stay_section(html)
    assert 'getJSON("/api/stays?"' in section, "住宿应 GET /api/stays"
    assert "data.items" not in section, "住宿段不得用 data.* 变量名(会被路线契约测试误扫)"
    assert "radius_km" in section and "STAYS_RADIUS_KM" in html, "请求应带检索半径参数"
    assert "token!==state.stays.token" in section, "在飞的住宿响应应有 token 守卫(换 pin 作废)"


def test_stay_card_shows_required_fields(html: str) -> None:
    section = stay_section(html)
    card_html = section[section.index("function stayCardHtml"):section.index("function renderStayCards")]
    for token in ("item.name", "item.kind", "item.price_estimate", "item.distance_km",
                  "item.intro", "STAYS_EST_FALLBACK", "item.estimated"):
        assert token in card_html, f"住宿卡片缺少展示字段 {token}"


def test_stay_estimate_label_prominent(html: str) -> None:
    # 强标注口径:后端 ESTIMATED_LABEL 原文 + 前端兜底常量,都必须出现
    assert "AI 预估 · 仅供参考 · 以 OTA 实时为准" in html, "每张卡片须强标注 AI 预估口径"
    assert "不代订" in html and "实时价" in html, "页脚应写明不代订、不抓实时价"


def test_stay_fav_uses_place_kind(html: str) -> None:
    section = stay_section(html)
    payload = section[section.index("async function addFavStay"):section.index("function stayCardHtml")]
    assert 'kind:"place"' in payload, "收藏住宿走既有 kind=place(后端无 stay 类型,不改后端)"
    for token in ("osm_type:item.osm_type", "osm_id:item.osm_id", "to_lat", "to_lng", "price_estimate"):
        assert token in payload, f"收藏住宿请求体缺少 {token}"
    # 已收藏判定键与后端唯一键 (kind,ref_key,mode) 同口径:place:{type}/{id}
    key_fn = section[section.index("function stayFavItem"):section.index("function stayFavButtonHtml")]
    assert '"place:"' in key_fn and "osm_type" in key_fn, "判定键须复刻后端 collection_ref_key 的 place 规则"


def test_stay_section_wired_into_route_panel(html: str) -> None:
    section = html[html.index("async function openRoutePanel"):html.index("function closeStaySection")]
    assert "openStaySection(place,state.stays.token)" in section, "开路线面板应同时拉住宿"
    assert "closeStaySection()" in html[html.index("function closeRoutePanel"):], \
        "关路线面板应一并清住宿区块"
    assert '$("stayRetry").addEventListener("click"' in html, "住宿重试按钮应绑定"


def test_stay_fav_click_delegated(html: str) -> None:
    assert 'target.closest("#stayCards .js-stay-fav")' in html, "收藏住宿按钮应走事件委托拦截"
    assert "syncStayFavButtons();" in html[html.index("async function refreshFavItems"):], \
        "收藏列表刷新应同步住宿按钮态"


def test_stays_contract_matches_api_item_shape(html: str) -> None:
    """前端引用的 item.* 字段必须是 GET /api/stays 出参投影白名单(ITEM_KEYS+estimated)的子集。"""
    from app.api.stays import ITEM_KEYS  # noqa: PLC0415
    section = stay_section(html)
    referenced = referenced_fields(section, "item") - {"kind"}
    # item.kind 也在白名单里;剔除 JS 本地变量后逐一核对
    allowed = set(ITEM_KEYS) | {"estimated", "kind"}
    unknown = {name for name in referenced if name not in allowed}
    assert not unknown, f"前端引用了 /api/stays 不存在的字段:{sorted(unknown)}"


# --------------------------------------------------------------------------- #
# TASK-4a:统一收藏面板(类型分组)+ 行程方案组合与总账(纯前端,不动后端)
# --------------------------------------------------------------------------- #
def test_fav_grouping_dom_and_functions(html: str) -> None:
    assert 'id="favPaneFav"' in html and 'id="favPanelList"' in html
    for fn in ("function favGroupOf(", "function favGroups(", "function renderFavItems(",
               "function favItemHtml("):
        assert fn in html, f"缺少 {fn}"


def test_fav_list_grouped_by_type(html: str) -> None:
    render = html[html.index("function renderFavItems"):html.index("function setFavErr")]
    assert "favGroupOf" in render and "fp-grp" in render, "收藏列表应按类型分组渲染"
    assert "路线收藏" in render and "目的地收藏" in render and "住宿收藏" in render


def test_fav_group_of_stay_fingerprint(html: str) -> None:
    group = html[html.index("function favGroupOf"):html.index("function setFavErr")]
    assert '"route"' in group and "stay_kind" in group and "price_estimate" in group


# 方案组合与总账自 TASK-5b 起改为**后端持久化**(/api/trip-plans):localStorage 双轨、
# 三下拉单选与前端自算总账一并下线,对应断言迁到下面的「TASK-5b」小节。


# --------------------------------------------------------------------------- #
# TASK-4b:前往预订(deep-link 聚合,纯前端;只跳转、不代订、不抓实时价)
# --------------------------------------------------------------------------- #
def test_book_dom_and_functions(html: str) -> None:
    assert 'id="favBookSection"' in html and 'id="favBookList"' in html
    for fn in ("function renderBookSection(", "function bookLinksForRoute(",
               "function bookLinksForStay(", "function splitRouteNames(",
               "function cleanStayName("):
        assert fn in html, f"缺少 {fn}"


def test_book_section_has_disclaimer(html: str) -> None:
    section = html[html.index('id="favBookSection"'):html.index("</main>")]
    assert "不代订" in section and "实时" in section, "预订区块必须含免责声明"
    js_part = html[html.index("function renderBookSection"):]
    assert "BOOK_DISCLAIMER" in js_part


def test_book_links_cover_required_channels(html: str) -> None:
    route_fn = html[html.index("function bookLinksForRoute"):html.index("function bookLinksForStay")]
    assert "kyfw.12306.cn" in route_fn, "铁路应跳 12306"
    assert "flight.qunar.com" in route_fn, "飞机应跳 OTA 机票搜索"
    assert "amap.com" in route_fn and "google.com/maps" in route_fn, "自驾应跳地图导航"
    stay_fn = html[html.index("function bookLinksForStay"):html.index("function bookRowHtml")]
    for host in ("ctrip.com", "booking.com", "airbnb.com"):
        assert host in stay_fn, f"住宿应可跳 {host}"


def test_book_links_open_new_page(html: str) -> None:
    row = html[html.index("function bookRowHtml"):html.index("function renderBookSection")]
    assert 'target="_blank"' in row and 'rel="noopener"' in row


def test_book_section_refreshes_with_fav_list(html: str) -> None:
    refresh = html[html.index("async function refreshFavItems"):html.index("async function openFavPanel")]
    assert "renderBookSection()" in refresh, "收藏列表刷新应同步预订区块"


# --------------------------------------------------------------------------- #
# TASK-5b:收藏面板「行程方案」tab —— 勾选收藏(checkbox 多选)→ POST /api/trip-plans
#          (后端持久化,重名 = 刷新幂等)→ 卡片展示后端 quote(总花费区间 + 构成 +
#          missing 已删除提示 + note 口径);列表 GET(新的在前)、删除 DELETE。
#          原 TASK-4a 的 localStorage 方案机制已下线(单一事实源在后端),三例迁到本节。
# --------------------------------------------------------------------------- #

PLAN_SECTION_START = "行程方案(TASK-5b,M4)"
PLAN_SECTION_END = "// 前往预订(TASK-4b)"

# 方案 tab 必须有的 DOM id:两个 tab 钮 + 两个 pane + 表单 + 勾选面 + quote + 列表
PLAN_DOM_IDS = [
    "favTabFav", "favTabPlan", "favPaneFav", "favPanePlan", "favPlanSection", "planErr",
    "planName", "planNights", "planNote", "planPicker", "planSummary", "planSave",
    "planQuote", "planSaved", "planCount", "planList", "planApiNote",
]
PLAN_FUNCTIONS = [
    "switchFavTab", "renderPlanPicker", "planPickRowHtml", "syncPlanPicked", "planPickedIds",
    "planPickedTotal", "planSummaryText", "onPlanPickChange", "planPayload", "planNights",
    "postTripPlan", "savePlan", "loadTripPlans", "tripPlanCardHtml", "renderTripPlans",
    "deleteTripPlan", "recalcTripPlan", "onPlanListClick", "planQuoteHtml", "renderPlanQuote",
    "planPerStayHtml", "moneyRange", "setPlanErr", "setPlanBusy", "pickedCount",
]
# 后端 quote_plan() 的出参键:前端要逐字段消费(总花费区间 + 构成 + 估算标注)
QUOTE_FIELDS = ["total_cny_low", "total_cny_high", "transport_cny", "stay_nights",
                "per_stay", "missing", "kind", "note"]
PER_STAY_FIELDS = ["collection_id", "name", "price_estimate", "low", "high"]
# POST /api/trip-plans 的请求体键(裸 JSON;字段名以 app/api/trips.py 为准)
PLAN_REQUEST_FIELDS = ["name", "note", "place_collection_id", "route_collection_ids",
                       "stay_collection_ids", "nights"]


def plan_section(html: str) -> str:
    """切出「行程方案(TASK-5b)」那一段 JS(契约断言只在这一段里找字段引用)。"""
    start = html.index(PLAN_SECTION_START)
    end = html.index(PLAN_SECTION_END, start)
    return html[start:end]


@pytest.fixture()
def plan_session(tmp_path: Path):
    """独立临时 SQLite 库:真跑 collections / trip-plans 端点函数,不触网、不碰应用库。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'frontend_plans.db'}")
    init_db(engine)
    current = session_factory(engine)()
    try:
        yield current
    finally:
        current.close()
        engine.dispose()


@pytest.fixture()
def saved_plan(plan_session) -> dict[str, Any]:
    """造三类收藏(目的地 / 路线 / 住宿)并 POST 一份 2 晚方案,拿到真实响应。"""
    place = collections_api.create_collection(session=plan_session, payload={
        "kind": "place", "osm_type": "node", "osm_id": 7000001, "name": "杭州西湖",
        "to_lat": 30.2500, "to_lng": 120.1600,
    })["collection"]
    leg = collections_api.create_collection(session=plan_session, payload={
        "kind": "route", "mode": "driving", "from_name": "上海", "to_name": "杭州西湖",
        "from_lat": 31.2304, "from_lng": 121.4737, "to_lat": 30.2500, "to_lng": 120.1600,
        "summary": {"duration_min": 105.0, "cost_cny": 320.5, "distance_km": 175.2,
                    "kind": "real"},
    })["collection"]
    stay = collections_api.create_collection(session=plan_session, payload={
        "kind": "place", "osm_type": "way", "osm_id": 7000002, "name": "🛏️ 西湖边客栈 · 住宿",
        "to_lat": 30.2510, "to_lng": 120.1610,
        "summary": {"stay_kind": "guest_house", "price_estimate": "约¥250-450/晚",
                    "distance_km": 1.2},
    })["collection"]
    response = trips_api.create_trip_plan(session=plan_session, payload={
        "name": "周末去杭州", "note": "两人自驾", "nights": 2,
        "place_collection_id": place["id"], "route_collection_ids": [leg["id"]],
        "stay_collection_ids": [stay["id"]],
    })
    response["stay_id"] = stay["id"]
    return response


def test_plan_tab_dom_ids_present(html: str) -> None:
    for element_id in PLAN_DOM_IDS:
        assert f'id="{element_id}"' in html, f"缺少行程方案 tab 的 DOM id:{element_id}"
    assert re.search(r'<div id="favPanePlan"[^>]*style="display:none"', html), \
        "方案 pane 默认隐藏(与收藏 pane 互斥)"
    assert re.search(r'#favPaneFav|id="favPaneFav"', html), "收藏 pane 是默认显示的那个"
    for tab_id in ("favTabFav", "favTabPlan"):
        assert re.search(rf'<button type="button" id="{tab_id}"[^>]*role="tab"', html), \
            f"{tab_id} 应是 role=tab 的按钮"


def test_plan_tab_functions_present(html: str) -> None:
    section = plan_section(html)
    for name in PLAN_FUNCTIONS:
        assert re.search(rf"function\s+{re.escape(name)}\s*\(", section), f"缺少方案 JS 函数:{name}"
    # 勾选面与收藏面板共用同一套分组口径(favGroups 定义在收藏段,方案段调用)
    assert re.search(r"function\s+favGroups\s*\(", html), "缺少收藏分组函数 favGroups"
    assert "favGroups()" in section, "方案勾选面应复用收藏分组,而不是另立一套"


def test_plan_tab_switch_is_wired_and_mutually_exclusive(html: str) -> None:
    assert '$("favTabFav").addEventListener("click",()=>switchFavTab("fav"))' in html
    assert '$("favTabPlan").addEventListener("click",()=>switchFavTab("plan"))' in html
    switch = html[html.index("function switchFavTab"):html.index('$("goCity").addEventListener')]
    assert 'panes[key].style.display=active?"block":"none"' in switch, "两个 pane 必须互斥显示"
    assert 'tabs[key].classList.toggle("sel",active)' in switch, "选中 tab 要有视觉态"
    assert 'setAttribute("aria-selected"' in switch, "tab 要同步 aria-selected"
    assert "renderPlanPicker();loadTripPlans();" in switch, "切到方案 tab 应渲染勾选面并拉一次列表"


def test_plan_picker_uses_checkboxes_instead_of_selects(html: str) -> None:
    row = plan_section(html)
    row = row[row.index("function planPickRowHtml"):row.index("function renderPlanPicker")]
    assert 'type="checkbox"' in row and "js-plan-pick" in row, "勾选用 checkbox(不再用下拉单选)"
    assert 'data-group="' in row and 'data-id="' in row, "勾选行要带分组与收藏 id"
    # 对比字段齐全:路线时长/费用/里程、住宿预估价(要求①)
    for token in ("fmtDuration(summary.duration_min)", "fmtCost(summary.cost_cny)",
                  "fmtKm(summary.distance_km)", "summary.price_estimate"):
        assert token in row, f"勾选行缺少对比字段 {token}"
    # 旧的三下拉 + localStorage 双轨必须彻底下线(要求⑤)
    for gone in ("planDest", "planRoute", "planStay", "PLAN_STORE_KEY", "w2g_trip_plans",
                 "loadSavedPlans", "renderSavedPlans", "parseStayPrice", "localStorage"):
        assert gone not in html, f"旧 localStorage 方案机制应已移除:{gone}"


def test_plan_picker_place_is_single_choice(html: str) -> None:
    section = plan_section(html)
    change = section[section.index("function onPlanPickChange"):section.index("function setPlanErr")]
    assert 'data-group="place"' in section, "目的地分组要能在 DOM 里认出来"
    assert "place_collection_id" in section and 'multi:false' in section, \
        "后端 place_collection_id 是单值,UI 要标成单选"
    assert "other.checked=false" in change, "勾一个目的地应取消同组其它勾选(不静默丢弃)"
    assert '$("planPicker").addEventListener("change",onPlanPickChange)' in html, "勾选走事件委托"


def test_plan_calls_trip_plans_crud(html: str) -> None:
    section = plan_section(html)
    assert 'sendJSON("/api/trip-plans",{method:"POST"' in section, "保存方案应 POST /api/trip-plans"
    assert 'getJSON("/api/trip-plans?limit="' in section, "方案列表应 GET /api/trip-plans"
    assert 'sendJSON("/api/trip-plans/"+Number(id),{method:"DELETE"})' in section, \
        "删除方案应 DELETE /api/trip-plans/{id}"
    assert 'headers:{"Content-Type":"application/json"}' in section, "POST 要声明 JSON 请求体"
    assert "JSON.stringify(body)" in section, "请求体应序列化成 JSON(后端收裸 JSON 对象)"


def test_plan_payload_fields_match_backend(html: str) -> None:
    section = plan_section(html)
    payload = section[section.index("function planPayload"):section.index("async function postTripPlan")]
    assert "name:planNameText()" in payload and "nights:planNights()" in payload, \
        "请求体要带方案名与晚数"
    assert "if(note) body.note=note;" in payload, "备注可选:给了才带上"
    assert "body[group.field]=group.multi?ids:ids[0];" in payload, \
        "单值 place 传 id、多值 route/stay 传整数数组"
    groups = section[section.index("const PLAN_PICK_GROUPS"):section.index("function planNameText")]
    for field in PLAN_REQUEST_FIELDS:
        assert field in groups or field in payload, f"请求体缺少字段 {field}"
    for field in ("place_collection_id", "route_collection_ids", "stay_collection_ids"):
        assert f'field:"{field}"' in groups, f"勾选分组应映射到后端字段 {field}"
    used_body = referenced_fields(section, "body")
    assert used_body <= set(PLAN_REQUEST_FIELDS), \
        f"请求体出现后端不认的字段:{sorted(used_body - set(PLAN_REQUEST_FIELDS))}"


def test_plan_nights_range_matches_backend(html: str) -> None:
    assert js_constant(html, "PLAN_NIGHTS_MIN") == trip_service.MIN_NIGHTS, "晚数下限要与后端一致"
    assert js_constant(html, "PLAN_NIGHTS_MAX") == trip_service.MAX_NIGHTS, "晚数上限要与后端一致"
    assert js_constant(html, "PLAN_NIGHTS_DEFAULT") == trip_service.DEFAULT_NIGHTS, "默认晚数要一致"
    assert re.search(r'<input id="planNights" type="number" min="1" max="60"', html), \
        "晚数输入框要限 1~60(与后端 resolve_nights 同口径)"
    nights = plan_section(html)
    nights = nights[nights.index("function planNights"):nights.index("function planPickedIds")]
    assert "Math.max(PLAN_NIGHTS_MIN,Math.min(PLAN_NIGHTS_MAX,value))" in nights, "越界晚数要夹回区间"
    assert "PLAN_NIGHTS_DEFAULT" in nights, "空值/坏值退回默认晚数(不让后端 400)"


def test_plan_quote_fields_are_rendered(html: str) -> None:
    section = plan_section(html)
    used_quote = referenced_fields(section, "quote")
    for field in QUOTE_FIELDS:
        assert field in used_quote, f"前端没有消费 quote.{field}"
    assert "moneyRange(quote.total_cny_low,quote.total_cny_high)" in section, \
        "卡片要显示总花费区间 total_cny_low ~ total_cny_high"
    assert "fmtCost(quote.transport_cny)" in section, "要显示交通费构成"
    assert "KIND_BADGE[quote.kind]" in section, "估算徽标要按后端 kind 取文案"
    assert "quote.missing" in section and "已删除" in section, "missing(引用已删除)要有可见提示"
    assert "per_stay" in section and "planPerStayHtml" in section, "per_stay 要逐处展开"


def test_plan_quote_note_is_backend_verbatim(html: str) -> None:
    section = plan_section(html)
    assert "esc(quote.note||PLAN_QUOTE_NOTE_FALLBACK)" in section, "quote.note 原样展示(不改写口径)"
    assert "state.tripPlan.note" in section and '$("planApiNote")' in section, \
        "列表响应的 note(口径说明)也要展示"
    fallback = re.search(r'const PLAN_QUOTE_NOTE_FALLBACK="([^"]+)"', html)
    assert fallback, "应有 quote.note 缺失时的兜底文案常量"
    assert fallback.group(1) == trip_service.QUOTE_NOTE, "兜底文案要与后端 QUOTE_NOTE 一字不差"
    assert "仅供参考" in html and "估算" in section, "免责/估算口径必须在方案区块可见"


def test_plan_list_delete_and_recalc_are_delegated(html: str) -> None:
    section = plan_section(html)
    click = section[section.index("function onPlanListClick"):section.index("function switchFavTab")]
    assert "js-plan-del" in click and "deleteTripPlan(" in click, "删除按钮要走事件委托"
    assert "js-plan-recalc" in click and "recalcTripPlan(" in click, "按晚数重算按钮要走事件委托"
    assert '$("planList").addEventListener("click",onPlanListClick)' in html
    assert "result.trip_plans" in section, "列表应读响应的 trip_plans 数组(后端已新的在前)"
    assert "新的在前" in html, "列表要说明排序口径"
    assert "plan.updated_at" in section, "方案卡片要显示更新时间(幂等刷新才看得出来)"


def test_plan_tab_quote_contract_matches_backend(html: str, saved_plan: dict[str, Any]) -> None:
    """真跑一遍后端:前端在方案段里引用的每个字段都要在真实响应里存在。"""
    section = plan_section(html)
    quote = saved_plan["quote"]
    plan = saved_plan["trip_plan"]
    assert set(QUOTE_FIELDS) <= set(quote), f"后端 quote 缺字段:{quote}"
    assert not (referenced_fields(section, "quote") - set(quote)), \
        f"前端读了 quote 里没有的字段:{sorted(referenced_fields(section, 'quote') - set(quote))}"

    assert plan["counts"] == {"place": 1, "routes": 1, "stays": 1}
    assert not (referenced_fields(section, "plan") - set(plan)), \
        f"前端读了 trip_plan 里没有的字段:{sorted(referenced_fields(section, 'plan') - set(plan))}"
    assert not (referenced_fields(section, "counts") - set(plan["counts"])), "counts 字段对不上"

    per_stay = quote["per_stay"]
    assert per_stay and set(PER_STAY_FIELDS) <= set(per_stay[0]), f"per_stay 形状不对:{per_stay}"
    assert not (referenced_fields(section, "stay") - set(per_stay[0])), \
        f"前端读了 per_stay 里没有的字段:{sorted(referenced_fields(section, 'stay') - set(per_stay[0]))}"
    # 2 晚:住宿区间 = 均价下限/上限 × 2,交通 = 收藏快照 cost_cny 之和
    assert quote["stay_nights"] == 2 and quote["transport_cny"] == 320.5
    assert quote["total_cny_low"] == 820.5 and quote["total_cny_high"] == 1220.5
    assert quote["kind"] == trip_service.QUOTE_KIND and quote["note"] == trip_service.QUOTE_NOTE
    assert quote["missing"] == []


def test_plan_envelope_and_missing_refs_match_backend(html: str, plan_session, saved_plan: dict[str, Any]) -> None:
    """POST/GET 的信封键与「引用被删 → missing」降级口径都要被前端覆盖。"""
    section = plan_section(html)
    listed = trips_api.list_trip_plans(limit="50", session=plan_session)
    envelope = set(saved_plan) | set(listed)
    used_result = referenced_fields(section, "result")
    assert not (used_result - envelope), f"前端读了响应信封里没有的键:{sorted(used_result - envelope)}"
    assert listed["count"] == 1 and listed["trip_plans"][0]["id"] == saved_plan["trip_plan"]["id"]

    # 删掉被引用的住宿收藏:方案不连带删,报价把它列进 missing(前端要有「已删除」提示)
    assert collections_api.delete_collection(str(saved_plan["stay_id"]), session=plan_session)["deleted"]
    detail = trips_api.get_trip_plan(str(saved_plan["trip_plan"]["id"]), session=plan_session)
    assert detail["quote"]["missing"] == [saved_plan["stay_id"]], "缺行应按已删除列进 missing"
    assert detail["quote"]["per_stay"] == []
    assert "已删除" in section and "missing" in section, "前端要展示 missing 的已删除提示"
    assert 'missing.map(id=>"#"+Number(id)).join("、")' in section, "missing 要逐个 id 展示"


def test_plan_save_surfaces_idempotent_refresh(html: str) -> None:
    section = plan_section(html)
    assert "result.created===false" in section, "要区分新建/刷新(后端重名幂等)"
    assert "刷新" in section and "幂等" in section, "刷新态要有可见说明"
    assert "缺少方案名" in section, "方案名为空要就地给中文提示(不打无谓的 400)"
    assert "setPlanErr" in section and "error.message" in section, "失败要落后端中文报错文案"
    assert "disabled" in section, "保存中/没勾选时按钮要禁用"
