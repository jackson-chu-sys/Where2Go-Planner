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

from app.api import routes as routes_api  # noqa: E402
from app.main import STATIC, app as fastapi_app  # noqa: E402
from services import routes as route_service  # noqa: E402

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
