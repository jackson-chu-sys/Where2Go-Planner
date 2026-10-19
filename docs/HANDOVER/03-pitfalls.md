# 03 · 踩坑手册(动这些代码之前先读)

每条都是实测踩出来的,不是推测。**这里记录的结论都带日期与配置前提 —— 前提变了结论就可能失效,引用前先核对。**

---

## A. 高德(境内主链路)

1. **检索走 v3,不要用 v5**。v5 的 `page=2` 和 `page=1` 返回完全相同的页,翻页是坏的。
2. **单次查询最多 200 条**,`page>=9` 返回 0,`count` 字段不可信。所以 band ≥ 50 km 必须**分格走 polygon 查询**;`radius` 参数实测被截断在 50000 米。
3. **`tolls` 恒为 0、`cost` 恒为 null**。过路费只能用 `toll_distance` × 区域费率自算,别指望高德给。
4. **QPS 限制 `infocode=10021`**:单进程最小请求间隔 **0.6 秒** + 2s/5s 退避重试(≤3 次)。别把间隔调小,也别多个程序共用一张 key。
5. **`Place.osm_id` 是 Integer** → 高德 POI id 必须 `crc32` 之后入库,`osm_type="amap"` 区分来源。
6. **两把 key 平台隔离**:REST 只能用「Web 服务」key,前端地图只能用「Web端(JS API)」key。混用报 `USERKEY_PLAT_NOMATCH`(10009)。安全密钥缺失报 `INVALID_USER_SCODE`。
7. **任何 key 都不许写进 `index.html`**,前端通过 `/api/map-config` 运行时注入。
8. **高德只服务境内**。境外地名会**错误吸附到国内同名地点**(瑞士因特拉肯 → 成都双流某小区),而且 `status=1` 看着像成功。**不能用高德 geocode 结果判定境内外**。
9. **`level` 字段能区分"真行政区"和"POI 吸附"**(上海=省、崇礼=区县;因特拉肯=兴趣点、北海道=道路、东京=村庄),2026-10-18 实测 12/12 正确。**这个判据还没落码**,是已知的技术债,见 `05-status-and-todo.md`。
10. 高德「世界地图」(`showOversea: true`)是机会项:需要工单商务咨询,个人 key 能否获批未知。**没批下来之前不要动境外轨**(它只解决底图,不解决境外 POI/geocode/路线)。详见队列条目 `[TASK-WORLDMAP]`。

## B. 境外链(OSM 系)

1. **Nominatim 与 Overpass 在国内直连不通**(Nominatim `Network is unreachable`,Overpass `502`),配了 HTTP 代理后 0.69s / 30.9s 可用。所以境外功能是**部署配置问题**,不是代码问题:容器没配 `HTTPS_PROXY` 就是不能用,README 已写明。
2. **别用"部分数据"的 Overpass 实例**。`overpass.osm.ch` 国内直连最快(0.49s),但**只有瑞士数据**:皇后镇/札幌/巴黎/纽约/上海全是 n=0,只有因特拉肯有。空结果会被当成"这个分段没有 POI" = **静默错答**,比超时难查得多。端点必须用官方全球实例 `overpass-api.de`。
3. **Photon 对中文境外城市名会返回国内同名 POI**:「因特拉肯」→ 成都双流「因特拉肯大道」(street)、「巴黎」→ 高雄(house)、「皇后镇」→ 北京度假村(house)、「苏黎世」→ 天津(house)。所以**必须过行政区级判据** `photon.admin_place_verdict()`:三态(`type ∈ ADMIN_PLACE_TYPES` 且 `osm_type ∈ {R,N}` = True;字段齐但落 POI 级 = False;字段缺 = None 沿用旧口径)。判 False 时按"这条腿没答上"处理,绝不能采信。
4. **错判会写缓存 7 天**(`origin_cache`)。一次误判 domestic,TASK-25 的境外链之后**永远不再触发** —— 这是「国外目的地完全不支持中文」的真正根因。判不准时返回 `_unknown_region()`(按 domestic 处理但**不写缓存**,下次还能重试)。
5. **境内证据必须双源**:原实现只认 Nominatim 的 CN,而 Nominatim 直连不通 → 自动降级永远触发不了,勾着境外搜「上海」直接 400。现在 Phonon 的**行政区级** CN 也算境内正面证据;非行政区级 CN 仍只当该腿失败(保住「巴黎→高雄」这条防线)。
6. **`region` 参数的口径不统一,这是坑**:`/api/places` 后端会**自动判定**兜底,`/api/routes` 与 `/api/stays` **只认显式参数**。前端必须用 `isOverseasTrack()`(开关 **或** 后端判定为境外)统一透传,只认开关会让"搜境外城市但没勾开关"错走高德。
7. **境外模式下的 OTA 外链**:Booking 必须带 `latitude/longitude` 坐标圈选(纯文本 `ss=` 对境内访客 0 家);Airbnb 只在 `summary.region=overseas` 时出(境内页 h1 虚标 1000+ 实际 0 卡片);携程境外返回 `IntlCity` 类型,原代码只认 `Hotel`。

## C. 代理口径

- 唯一开关是 `data_sources/_common.build_session(user_agent, source=...)`,按数据源决定是否走代理;可用 `WHERE2GO_PROXY_<源>` 覆盖(`off` 直连 / `env` 沿用环境变量 / 其他值=代理 URL)。
- 默认:高德、Photon、OSRM、携程**直连**;Nominatim、Overpass、LLM、维基**沿用环境代理**。
- **一刀切会坏**:实测同一条 Overpass 查询走代理反而 504、住宿检索直连 1.4s vs 代理 16.7s。
- **旧结论会失效**:2026-09-27 记录过"Overpass 走代理 504,应直连",到 10-18 直连变成 502、代理反而可用,于是默认值改成了 `env`。这类结论请连日期一起读。

## D. LLM 调用与超时

1. **默认 `max_tokens=120 / timeout=20s` 是给"一句话简介"调的**。多条目 JSON、长介绍、行程规划必须按调用放大 token 与超时,否则输出被截断或读超时 → 服务层捕获异常 → **静默降级成"按距离推荐"/空介绍**,极难查。
2. **超时线按高峰期实测定,不按平均值**。同一个 prompt 平峰 12~23s,高峰 73~184s。90s / 120s 级别的线高峰期必被砍断。
3. **超时的表现不是报错,而是"永远在进行中"**:`estimate_batch` 之类把所有异常吞成空值 → 价格永远 `null` → 前端每次重开面板都重排队、一直显示"生成中"。查法:`SELECT price_kind, COUNT(*), SUM(price_estimate IS NULL) FROM stays GROUP BY price_kind` —— 有一批 `price_kind IS NULL` 就是**从没成功过**(不是慢)。
4. **后台线程路径的超时可以放宽得很随意**(不阻塞请求);只有同步路径(如 ≤5 家就地估价)才需权衡。
5. **LLM 回填必须异步**。实测冷抓墙钟的 73%~98% 是 LLM 简介,异步化之后 173s → 12.3s。别把它塞回主链路。

## E. 前端与 JS

1. **正则剥 emoji 必须用交替序列 `(?:📍|🛏️)`,绝不用字符类 `[📍🛏️]`**。字符类按 UTF-16 单元匹配会拆开代理对 → 孤立低代理 → `encodeURIComponent` 抛 `URI malformed` → **整个面板渲染崩掉**,而源码看着完全正常。
2. **验证点击必须用真实点击**。JS 合成 `dispatchEvent(new MouseEvent(...))` 对高德这类在 overlay 层代理事件的库**不触发**(冒泡逐级全是 false),只能证明"元素在",不能判断"交互坏没坏"。
3. **同色陷阱**:药丸/徽章容器自带背景色时,内层文字色要单独算对比度。真发生过价格文字与底色同色(1.0:1 完全隐形)。改法:深色字浅底(≥7:1),药丸内次要灰字也要局部加深。对比度是纯计算,写个十几行的脚本比截图更硬。
4. **长耗时操作开始时立刻清旧结果**:搜索要跑 10~60s,旧 pin 和旧列表留在页面上会被当成"已经搜完的新结果"。在**发请求之前**清视图,失败路径补空态渲染。
5. **浏览器 QA 重页面前先 stub 掉媒体加载**(`window.loadPlaceMedia=function(){}`),否则图片密集 + LLM 实调会把 CDP 页面主线程拖到超时;要确认图片真拉到了,改看 uvicorn 日志里的 200。
6. **境外轨 QA 时浏览器必须带代理**:headless 直连 unpkg/jsdelivr/cdnjs 全部 SSL 失败,Leaflet CDN 加载不出来;真实境内用户同样到不了 unpkg,所以境外轨有"地图组件加载失败"的降级路径。
7. **release / dev 双模**:`WHERE2GO_UI_MODE` 只认 `release`,其余一律 dev。CSS(`body.ui-release` 藏 `.dev-only`)与 JS(`isReleaseUI()`)双闸门;合规标注(「估算」「AI 预估」)在 release 必须保留,有测试守着。QA release 用独立实例(加环境变量前缀起 uvicorn),验完杀掉恢复。

## F. 测试

- **凡"提交后台任务 + monkeypatch 依赖"的测试,替身的生命周期必须覆盖到 wait/join 之后**。真踩过:helper 在函数返回时就复原了替身,后台 worker 惰性解析时拿到真实客户端(无 key → 抛错 → 退避),表现为"简介全空 + inflight 不清 + llm.calls=0",单跑复现率 20%,像 random flake 其实是确定性的竞态。
- 定位这类问题的正确姿势:**别靠 pytest 碰运气**,写独立脚本连跑 20 轮,把被吞的异常用 `traceback.format_exc()` 打出来,一轮就现形。
- 静态断言(字符串在文件里)+ node 真跑纯函数 + 浏览器真实点击,三层缺一不可。

## G. Git / Docker / 发布

1. **磁盘上的执行位 ≠ git 里的执行位**。`ls -l` 显示 755 但 index 记的是 `100644` 时,checkout / COPY 进镜像的文件没有执行位,容器直接 `permission denied` exit 126。查法 `git ls-files -s <file>`;修法 `git update-index --chmod=+x <file>` 后提交。
2. **PAT 缺 `workflow` scope 时,任何触碰 `.github/workflows/` 的 commit 都会被远端整推拒收**。
3. **验证凭据不要用 `ls-remote`**(公开仓库读操作不需要认证,会假通过)。用 `curl -H "Authorization: Bearer <token>" https://api.github.com/user` 验有效性,或 `git push --dry-run` 验写权限。另注意 `store` helper 在认证失败时会**自动删除凭据行**。
4. **验证 GHCR 镜像不要用 packages API**(PAT 可能没有 `read:packages` → 403)。走匿名 registry:先 `GET https://ghcr.io/token?scope=repository:<owner>/<pkg>:pull` 拿 bearer,再查 `/v2/<owner>/<pkg>/tags/list` 与 `/manifests/<tag>`。semver 模式产出的 tag **不带 v**(`0.2.0-beta`,不是 `v0.2.0-beta`)。
5. **别在容器里跑 `hermes update` 之类的自更新命令**(历史上抹过盘)。
