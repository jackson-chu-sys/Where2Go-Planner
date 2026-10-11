# TASK-23 契约:前端双轨地图(Leaflet 境外轨)

> 依赖 TASK-22(境外 places/routes/stays 的 region 参数全通,验收 done 后才开工)。
> 本契约**逐字执行**。只读清单(≤5 处,读完立即写码):
> ①`backend/app/static/index.html`(现状全文,地图层=高德 JS API 2.0,TASK-9d);
> ②`git show a8d252a:backend/app/static/index.html`(TASK-7a 时代的 **Leaflet 版本**,pin/环圈/popup 组件考古源;若该 commit 已是高德版,用 `git log --oneline -- backend/app/static/index.html` 找 2026-09-30 前最后一个 Leaflet 版本);
> ③`backend/test_frontend_routes.py`(静态断言 + node --check 套路);
> ④`docs/OVERSEAS-PLAN.md` §1.1(瓦片三线实测)/§4.1(坐标系口径);
> ⑤`docs/TASK-21-CONTRACT.md` §1.4(前端 region 开关现状)。
> 写码硬约定见 AGENTS.md(前端改 index.html **不需要**重启 uvicorn;任何 key 不得写进 index.html)。

## 0. 实测依据(2026-10-10/11,勿重测)

- 瓦片直连可达性(境内出口,无代理):**tile.openstreetmap.de 0.78s ✓ / tile.openstreetmap.fr 0.75s ✓ / basemaps.cartocdn.com 2.0s ✓**(注意主机名是 basemaps.cartocdn.com,a.basemaps 子域不通);tile.openstreetmap.org 官方主站 **DNS 污染不可用**;maps.wikimedia.org 超时不可用。
- 高德 JS API 对境外坐标:底图空白(webrd 境外 z≥10 全 179B 空白瓦片)但 pin/环圈**仍可画**(JS API 本身不依赖瓦片内容);WGS-84 坐标在高德地图上会有 GCJ-02 偏移(境外偏移量几百米级)——所以境外轨必须切 Leaflet(无偏移,WGS-84 直用)。
- Leaflet 1.9.4 CDN(unpkg/带 SRI)在项目阶段1 用过,考古版本里有现成的加载与降级提示写法;Leaflet 库 CDN 若不可达,降级提示照考古版本口径。

## 1. 落地契约(仅 index.html + test_frontend_routes.py,后端零改动)

### 1.1 地图引擎双轨

- 新增 `state.mapEngine`("amap"|"leaflet"),启动缺省 "amap"(现状零变化)。
- `/api/geocode` 响应 `region=overseas`(或用户勾选 #regionOverride)→ 切 "leaflet":
  - **懒加载 Leaflet**(首次进境外才注入 CDN `<script>`+`<link>`,1.9.4,带 SRI,照考古版本;加载失败给状态栏提示「地图组件加载失败,请检查网络」,列表/收藏等其余功能不受影响);
  - 地图容器复用现有 `#map` DOM(高德实例 destroy 后初始化 L.map,或双容器互斥显隐——二选一,以改动最小为准,但**同一时刻只渲染一个引擎**);
  - 瓦片三线:`https://{s}.tile.openstreetmap.de/{z}/{x}/{y}.png`(主,subdomains a/b/c)→ 加载错误率切换 `https://{s}.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png`(备)→ `https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png`(兜底);attribution 必带 `© OpenStreetMap contributors`(CC-BY-SA 义务);maxZoom=18;
  - **切换回境内**(region=domestic)→ 高德实例照旧(TASK-9d 代码零改动),Leaflet 容器隐藏。
- 渲染函数双轨:环圈(L.circle 外实内虚+起点圆,半径/颜色口径与高德版一致)、pin(L.marker + L.divIcon 自绘,分类色/emoji 复用现有 pinHtml/pinStyle 函数——它们与引擎无关,只输出 HTML)、popup(L.popup 承载现有 popupHtml() 输出,「📄 查看详情」/收藏/路线按钮事件委托照旧生效)、fitView(L.featureGroup(...).getBounds() → map.fitBounds)、路线折线(L.polyline:驾车实线/铁路虚线/飞机弧线,坐标 [lat,lng] 顺序注意 Leaflet 与高德相反)。
- **坐标口径(硬约束)**:境外轨所有坐标是 WGS-84(/api/places 境外行 tags.coord_system 已标注),**禁止任何 GCJ-02 转换**;高德轨维持现状(GCJ-02)。两轨数据同库不混用(region 决定用哪批行)。
- 状态栏:region=overseas 时显示「境外模式 · OSM/Leaflet · WGS-84」;TASK-21 加的「地图切换见后续版本」过渡提示移除(本条就是那个后续版本)。
- 定位按钮(我的位置)境外轨:逆地理走现状 /api/geocode/reverse(后端已双轨),坐标 WGS-84 直用。

### 1.2 既有功能零回归清单(逐项自验)

境内(缺省):地图渲染/pin/环圈/InfoWindow/popup、路线面板+画线、住宿区块、收藏弹层三 tab、AI 行程、详情弹窗 #placeModal、加载更多、搜索/定位、补简介——**全部走高德轨现状代码,行为零变化**。境外:列表/收藏/路线面板/住宿卡片照常(数据来自 region=overseas 接口),地图层切 Leaflet。

## 2. 测试契约(≥12 静态断言 + node --check;交互验证=执行器 browser QA)

test_frontend_routes.py 追加 TASK-23 段:
1. Leaflet 懒加载注入(CDN URL 含 leaflet@1.9.4、SRI integrity 属性在、首次境外才加载的守卫函数在);
2. 瓦片三线 URL 全在(osm.de/osm.fr/basemaps.cartocdn)+ attribution 含 OpenStreetMap;
3. state.mapEngine 双轨切换函数、region=overseas → leaflet / domestic → amap 分支;
4. L.circle/L.marker/L.popup/L.polyline/fitBounds 调用面在;divIcon 自绘复用 pinHtml;
5. **index.html 内不得出现 32 位 key 样态字符串**(既有断言保留)+ 无任何新增前端 key;
6. GCJ-02 转换函数不得出现在境外渲染路径(断言境外分支不调用 gcj/wgs 转换字样);
7. 状态栏「境外模式」文案;TASK-21 过渡提示移除;
8. 高德轨现状函数(AMap.Marker/AMap.Circle/AMap.InfoWindow/setFitView)零删改断言(字符串仍在);
9. node --check 通过;
10. bookLinksForStay/placeModal/收藏面板与引擎无关的函数零改动(既有断言全绿即证)。

## 3. 验收与禁止项

- pytest 全绿(基线=TASK-22 后实际数字,只增不减);commit 带 `TASK-23`;只 commit 不 push。
- **禁止**:改任何后端 Python;删高德轨代码(境内现状零变化是硬验收);新增前端 key;引入 Leaflet 之外的新库。
- 执行器真机 QA(browser_exec,受信任点击):①境内回归:上海 0_50 → 高德地图/pin/InfoWindow/路线画线 0 error;②境外全流程:搜「Interlaken」→ region=overseas → Leaflet 渲染(瓦片 ≥1 块加载)→ 环圈+pin → 点 pin 出 popup(名称/分类/距离)→「📄 查看详情」弹窗 → 路线面板(OSRM 驾车卡片)+ 地图折线 → 住宿卡片 → 收藏 → 全程 window.__errs 0 条;③切回「上海」→ 高德轨恢复;④瓦片主线模拟失败(改 hosts 不可行,用 JS 断言 error 监听与切换函数存在即可,真切换逻辑由静态断言+代码审查覆盖)。
