# 调研报告:OTA deep-link 城市字段传法 & 微信小程序发布(2026-10-09)

> 调研方式:web_search/web_extract + 容器内 curl 实测 + 无头浏览器真机验证(携程/Booking/Airbnb 均为当日实测,标注「实测」处为真实返回)。
> 背景:Where2Go 现有住宿外链把「城市+酒店名」拼进 `keyword=`(如 `https://hotels.ctrip.com/hotels/list?keyword=昆山昆山柏轩酒店`),实测经常搜不到。

---

## A) OTA 酒店外链城市字段

### A1. 携程 hotels.ctrip.com/hotels/list

**参数体系(实测)**

| 参数 | 说明 | 实测结论 |
|---|---|---|
| `city=<id>` / `cityId=<id>` | 携程自有城市 ID(**不是**高德 adcode)。昆山 = **83**(高德 adcode 是 320583,两者无换算关系) | `?city=83&checkin=...` 返回 200,页面标题「昆山酒店」,SSR 正确渲染城市列表 |
| `keyword=<文本>` | 搜索词。**SSR 阶段不做酒店过滤** | `city=83&keyword=柏轩` 与不带 keyword 结果一样(全量 1446 家),前端 XHR 才过滤,首屏命中不稳定 |
| `cityId + searchWord + directSearch=1 + old=1` | 站内搜索框实际生成的参数组合 | **实测命中**:搜「柏轩」返回「2家」,含昆山柏轩酒店。`old=1` 是关键开关,去掉后回退全量 1446 家 |
| `checkin/checkout` | `YYYY-MM-DD`,**非必填** | 不带日期 `?city=83` 仍 200 并渲染城市页(前端默认填今天/明天) |
| `crn/curr/locale` | 房间数/币种/语言,可选 | — |

**酒店详情页(实测,最可靠)**

```
https://hotels.ctrip.com/hotels/detail/?hotelId=83790270
```
- `hotelId` 为携程酒店主键;无日期、无登录、桌面浏览器直接打开即渲染「昆山柏轩酒店(亭林园玉山广场地铁站店)」详情页(curl 200,标题正确)。

**城市/酒店 ID 从哪拿(实测)**:携程公开 suggest 接口,无需登录/cookie:
```
POST https://m.ctrip.com/restapi/soa2/21881/json/gaHotelSearchEngine
Body: {"searchType":"H","keyword":"昆山柏轩酒店","head":{...}}
→ {"type":"Hotel","id":"83790270","word":"昆山柏轩酒店(亭林园玉山广场地铁站店)","cityId":83,"cityName":"昆山"}
→ {"type":"City","id":"83","word":"昆山","cityName":"昆山"}   # searchType:"D" 查城市
```
返回还带 `gdLat/gdLon`(高德坐标,可用坐标做二次校验)。第三方 OpenCLI 文档亦确认此端点与 city ID 体系(city 83 昆山)。

**结论(携程)**:
1. 最可靠 deep-link = **`/hotels/detail/?hotelId=<id>`**,hotelId 用上述 suggest 接口在收藏时解析一次并落库(与现有 `Place.osm_id` 存高德 POI id 同思路)。
2. 次选列表页:`?cityId=<id>&searchWord=<酒店名>&directSearch=1&old=1&checkin=&checkout=`(实测可命中,但参数属未公开行为,有被改版风险)。
3. **不要**再用 `keyword=城市+酒店名` 拼接——SSR 不过滤,首屏经常是空/全量结果,即现有 bug 根因。
4. **城市代码映射表**:不需要维护静态表——suggest 接口公开、免鉴权,可在收藏/建外链时动态解析并缓存(cityId、hotelId 一起存);高德 adcode 与携程 cityId 是两套体系,不可互换。

引用:
- https://opencli.info/docs/adapters/browser/ctrip.html (suggest 端点、city ID、hotel-search 参数)
- https://hotels.ctrip.com/hotels/list?city=83&checkin=2026-10-10&checkout=2026-10-11 (实测 200)
- https://hotels.ctrip.com/hotels/detail/?hotelId=83790270 (实测 200)

### A2. Booking.com searchresults

**参数体系**:

| 参数 | 说明 |
|---|---|
| `ss=<文本>` | 搜索串(人类可读),服务端意图解析;歧义地名(多个同名城市)会**静默**选一个 |
| `dest_id=<int>` | Booking 内部目的地 ID(**自有体系**,可为负数,如巴黎 `-1456928`、曼谷 `-3414440`;中国城市为正数,苏州 = `79514`,实测自苏州城市页链接)。与 GeoNames/adcode 均无对应 |
| `dest_type=` | `city / region / country / district / landmark / airport / hotel / coordinates` |
| `checkin/checkout` | ISO 日期;出价格必填,不填也能出列表(无报价) |
| `group_adults/group_children/no_rooms/age` | 人数房间;带儿童必须给 `age` 否则弹补年龄页 |
| 地图搜索 | `latitude/longitude + bounding_box_* + map=1` 可替代 dest_id |

**dest_id 从哪拿**:
- 官方 autocomplete:`GET https://accommodations.booking.com/autocomplete.json?aid=<联盟id>&query=...`,返回 `(dest_id, dest_type)` 元组。**实测:不带有效 `aid` 返回 `{"results":[]}`**(aid 要从已登录/已过 WAF 的页面 `window.utag_data.aid` 取)。
- 城市页 `https://www.booking.com/city/cn/<拼音>.html` 的页内链接自带 `dest_id`(实测苏州页给出 `dest_type=city&dest_id=79514`)。
- **昆山实测没有独立 city 页**(`/city/cn/kunshan.zh-cn.html` 302 到国家页)——Booking 把昆山归在苏州下,只能用 `ss=昆山` 或苏州 dest_id + `ss=昆山` 细化。

**反爬注意(实测)**:searchresults 在 AWS WAF 后面,curl/无头浏览器直连吃 202 challenge;真人浏览器可正常打开,deep-link 场景(用户自己点开)不受影响,但**我方服务端不要预取该页面校验**。

**结论(Booking)**:最稳拼法 `https://www.booking.com/searchresults.zh-cn.html?ss=<城市名或酒店名>&checkin=&checkout=&group_adults=2&no_rooms=1&group_children=0`;有 dest_id 就带上 `dest_id=&dest_type=city`(消歧义)。dest_id 需要**维护映射表**(autocomplete 要 aid,不能裸调),但覆盖到地级市即可(县级市 fallback 到 ss=)。

引用:
- https://explainx.ai/skills/booking.com/search-hotels-asq6cc/search-hotels (完整参数表、autocomplete 需 aid、ss 静默歧义)
- https://extension.orizn.app/extension/integrations/booking (dest_id/dest_type 说明)
- https://sparkproxy.io/blog/how-to-scrape-booking-com-hotel-prices (参数与 URL 构造)
- https://www.booking.com/city/cn/suzhou.zh-cn.html (实测 dest_id=79514)

### A3. Airbnb /s/<slug>/homes

- slug 体系:`/s/<City>--<Region>--<Country>/homes`,连字符分词(如 `Paris--France`、`Kunshan--China`);**解析很宽容,自由文本可用**,服务端会重写为规范 slug。必须带 `/homes` 后缀(裸 `/s/<slug>` 可能 301 到 503 维护页)。
- **实测**:`https://www.airbnb.com/s/Kunshan--China/homes` curl 200,标题「Airbnb | Kunshan - Vacation Rentals & Places to Stay - Suzhou」,免登录正确解析;也支持 `?query=<文本>` 与 `ne_lat/ne_lng/sw_lat/sw_lng&search_by_map=true` 地图圈选。
- **重大前提**:Airbnb 自 **2022-07-30 起暂停中国境内房源预订**(聚焦出境游),境内城市(如昆山)搜不到可订房源,deep-link 只对**出境目的地**有价值。Where2Go 的周末境内游场景,Airbnb 外链基本无效,建议仅对境外目的地展示。

引用:
- https://explainx.ai/skills/airbnb.com/search-listings-ddgioa/search-listings (slug 规则、/homes 必须、503 坑)
- https://browserless.io/skills/airbnb.com/find-filtered-airbnb-stays (URL 构造)
- http://travel.china.com.cn/txt/2024-04/09/content_117113074.shtml (境内游暂停)
- https://cn.nytimes.com/business/20220524/airbnb-china (背景)

### A4. 三家最可靠拼法汇总(已知酒店名+城市名+坐标)

| OTA | 推荐 deep-link | 是否需城市代码映射表 |
|---|---|---|
| 携程 | `https://hotels.ctrip.com/hotels/detail/?hotelId=<id>`(收藏时用公开 suggest 接口解析 hotelId 落库);降级:`/hotels/list?cityId=<id>&searchWord=<酒店名>&directSearch=1&old=1` | 不需静态表;动态解析+缓存(cityId/hotelId 与高德 adcode 是两套体系) |
| Booking | `https://www.booking.com/searchresults.zh-cn.html?ss=<酒店名,城市名>&dest_id=<id>&dest_type=city&checkin=&checkout=&group_adults=2`(县级市无 dest_id 时只带 ss) | 建议维护地级市 dest_id 表(autocomplete 需 aid;可从城市页爬取),县级市 fallback ss |
| Airbnb | `https://www.airbnb.com/s/<City>--<Country>/homes?query=<城市名>`;**仅境外目的地展示**(境内房源已停) | 不需要(slug 宽容解析) |

坐标用途:携程 suggest 返回 `gdLat/gdLon` 可做同名酒店消歧校验;Booking 可用 `bounding_box_*` 地图搜索兜底。日期建议带上(收藏时的计划入住日),三家 checkin/checkout 都非硬性必填但带上体验更好。

---

## B) 微信小程序发布(FastAPI 后端 + 高德 JS API 2.0 单页 H5)

### B1. 个人 vs 企业主体、类目与资质

官方类目文档(实测抓取)关键结论:

- **个人主体开放类目很窄**:工具(信息查询、预约/报名、记账、天气、计算器、备忘录等,**无资质要求**)、教育信息展示、体育培训/在线健身、生活服务(家政/婚庆/丽人)、餐饮(仅餐厅排队)等。**旅游服务对个人主体只开放「出境WiFi」一项**;「旅游攻略」「资讯-信息资讯」「深度合成(AI问答)」等均在非个人(企业/个体户)清单里,个人不可选。
- **企业主体·旅游服务类目资质**:旅行社 → 需《旅行社业务经营许可证》;OTA 平台 → 需《增值电信业务经营许可证》+平台协议/审核规范/纠纷机制;住宿服务 → 营业执照含住宿或 ≥2 家合作协议+特种行业许可证;景区服务 → 无资质要求。
- **关键规避口径(官方原文)**:住宿服务类目备注写明「**如小程序内仅提供公寓/民宿/酒店推荐服务,需选择【资讯-信息资讯】类目**」——Where2Go 只做规划/推荐+跳外链(不在端内交易),对号入座应是 **「资讯-信息资讯」(无特殊资质)** 或 **「工具-信息查询」**,而不是旅游服务;不需要旅行社许可证。
- **AI 助手是合规雷区**:「深度合成-AI问答」类目要求《互联网信息服务算法备案》(自研)或第三方的算法备案+合作协议。Where2Go 调用第三方大模型 API,走企业主体时需按「使用第三方技术」口径提交材料;**个人主体没有该类目,含 AI 问答功能基本过不了审**——个人版需砍掉 AI 助手或改为纯规则推荐。
- 个体户主体:按「非个人主体」类目走,可选资讯/工具类目,认证费仅 30 元/年(见 B4)。

### B2. 技术路线

**a) 原生小程序重写前端**
- WXML/WXSS/JS 重写单页 H5(地图、面板、收藏、外链)。后端 FastAPI 零改动。
- 地图替代:**高德 JS API 2.0 不能在小程序原生环境运行**(小程序无 DOM);两条路:①小程序原生 `<map>` 组件(内核是腾讯位置服务,底图样式/坐标系同为 GCJ-02,现有高德坐标可直接用);②**高德微信小程序 SDK(amap-wx.js)**——高德官方的微信小程序插件,提供 POI 搜索/逆地理/输入提示/天气/静态图/路线规划数据接口(底层仍是 restapi.amap.com Web 服务,现有 `WHERE2GO_AMAP_KEY` 体系可复用),地图显示仍用 `<map>` 组件或静态图。注意该 SDK 文档更新停留在 2021 年,能力面窄于 JS API(无矢量底图/自定义图层),自绘 pin 交互要改用 `<map>` 的 marker/callout 体系。
- 改造量:前端 ≈ 重写(现有原生 JS 单页 → WXML 模板+小程序 API),后端接口可直接复用(fetch→wx.request),估 2~4 周。**个人主体唯一可行的完整路线**。

**b) web-view 套壳 H5**
- 官方文档明确:**「个人类型的小程序暂不支持使用 web-view」**——个人主体此路不通;企业/个体户主体可用。
- 限制:src 域名必须在 mp 后台配置为**业务域名**(需域名 ICP 备案+下载校验文件放到域名根目录);web-view 内是完整浏览器环境,**高德 JS API 2.0 可以正常跑**(现有 H5 几乎零改动);web-view 内跳外链(OTA)受限——不能在 web-view 里直接打开 ctrip.com 等非业务域名页面,OTA 跳转要改成「复制链接」(wx.setClipboardData)或 `<navigator target="miniProgram">` 跳 OTA 官方小程序。
- 改造量:最小(前端基本不动,加 JSSDK 桥接+业务域名校验文件+外链策略调整),但**必须企业/个体户主体 + 备案域名**。

**c) Taro / uni-app 跨端**
- uni-app(Vue 语法)或 Taro(React 语法)编译到微信小程序,一套代码兼顾 H5。现有前端是原生 JS 单页,迁到 uni-app ≈ 重写为 Vue 组件(改造量接近 a),但换来 H5/小程序双端同源;地图统一用 uni-app `<map>` 组件(小程序端腾讯内核,H5 端可继续高德 JS API,条件编译)。
- 适合「H5 继续迭代 + 小程序分发」的长期形态;短期 DEMO 性价比低于 a/b。

### B3. 后端要求(request 合法域名)

官方网络文档(实测抓取):
- `wx.request` 只能访问后台配置的**服务器域名**;必须 **HTTPS**(TLS ≥1.2、证书链完整、域名与证书一致、iOS 不支持自签名),**必须 ICP 备案**,不能是 IP/localhost,不支持父域名配子域名。
- 自有服务器部署需要:①一个已备案域名(ICP 备案需通过境内云服务商接入,家里 NAS/无备案服务器不行);②SSL 证书(云厂商免费 DV 证书或 Let's Encrypt 均可,满足证书链完整);③容器公网可达(现有 FastAPI 单容器加反代+证书即可,代码零改)。
- 替代:**微信云托管**可作为后端(小程序内 `callContainer` 走私有协议,**免配置合法域名**),但意味着把容器迁到腾讯云托管,偏离现有 NAS 部署。
- 高德接口:小程序端如直调 restapi.amap.com,也要把它加进 request 合法域名(高德域名已备案,可配);或全部走自己后端代理(现有 `/api/*` 已是这个形态,**推荐**,还顺带藏住 key)。
- 另:**小程序本体也要备案**——2023-09 起新注册小程序必须完成「小程序备案」(在 mp 后台走流程,引用《反电信网络诈骗法》),未备案不能上架。

### B4. 发布流程、费用、审核

- **注册**:mp.weixin.qq.com 免费注册小程序账号(个人:身份证+手机号;企业:营业执照+对公验证)。
- **微信认证(年审制,2023-12 起)**:企业 300 元/年;个人/个体户 30 元/年;政府/基金会免费。个人主体不认证也能用基础能力,但改名等需认证。官方认证页确认「除政府或部分组织外均需支付审核服务费」。
- **备案**:小程序备案免费,平台初审+管局审核约 1~2 周(各省不同)。
- **审核周期**:代码提审通常 1~7 天,常见数小时~2 天;涉「社交/时政」类需当地互联网主管机关确认约 7 天(Where2Go 不涉及)。
- **常见被拒原因**(官方「常见拒绝情形」+旅游/地图类实践):类目与实际功能不符(推荐+外链却选旅游服务被要求资质是高频坑,应按官方口径选「资讯-信息资讯」或「工具-信息查询」);功能过于简单(只有一个页面/按钮);名称简介含搜索热词或与功能无关;诱导分享/诱导跳转;位置权限未声明用途(需在隐私协议里声明 getLocation 用途);web-view 业务域名未配置;AI 生成内容未按「深度合成」要求标注/备案。

### B5. 结论:个人开发者最低成本路径

**推荐:注册个体工商户 → 企业类(非个人)主体小程序 + web-view 套壳现有 H5**。
- 成本:个体户执照(线上办理,近乎 0 成本)+ 认证 30 元/年 + 域名 ~50 元/年 + ICP 备案(免费,1~2 周)+ 免费 SSL 证书;服务器可用现有 NAS(只要有公网+备案域名接入)。
- 改造量最小:H5 几乎不动(高德 JS API 2.0 在 web-view 内正常跑);要做的只有:业务域名校验文件、OTA 外链改「复制链接/跳 OTA 小程序」、AI 内容标注、隐私协议(getLocation 用途)。
- 类目:选「资讯-信息资讯」(官方口径:仅推荐酒店/民宿选此类,无需旅游资质)。

**纯个人主体(0 认证费也能玩)的替代路径**:个人主体不能用 web-view → 必须原生重写前端(a 路线)+ 砍掉 AI 问答(个人无深度合成类目)+ 类目选「工具-信息查询」+ OTA 跳转改复制链接/跳 OTA 小程序;地图用原生 `<map>` 组件 + 高德微信小程序 SDK(amap-wx.js)做数据层。后端仍需备案域名+HTTPS(或迁微信云托管)。总成本 ≈ 0 元现金 + 2~4 周重写工作量,功能有裁剪。

---

## 引用汇总

**A(OTA)**
1. https://opencli.info/docs/adapters/browser/ctrip.html — 携程 suggest 端点/city ID/hotel-search 参数
2. https://hotels.ctrip.com/hotels/list?city=83 — 实测 city 参数
3. https://hotels.ctrip.com/hotels/detail/?hotelId=83790270 — 实测详情页
4. https://m.ctrip.com/restapi/soa2/21881/json/gaHotelSearchEngine — 实测 suggest API(昆山 cityId=83、柏轩 hotelId=83790270)
5. https://explainx.ai/skills/booking.com/search-hotels-asq6cc/search-hotels — Booking 参数全集/autocomplete aid
6. https://extension.orizn.app/extension/integrations/booking — dest_id/dest_type
7. https://www.booking.com/city/cn/suzhou.zh-cn.html — 实测苏州 dest_id=79514
8. https://explainx.ai/skills/airbnb.com/search-listings-ddgioa/search-listings — Airbnb slug 规则
9. https://www.airbnb.com/s/Kunshan--China/homes — 实测 200 正确解析
10. http://travel.china.com.cn/txt/2024-04/09/content_117113074.shtml — Airbnb 境内游暂停
11. https://cn.nytimes.com/business/20220524/airbnb-china — 同上背景

**B(微信小程序)**
1. https://developers.weixin.qq.com/miniprogram/product/material/ — 开放类目(个人/非个人/境外主体全表,住宿服务→资讯类目口径)
2. https://developers.weixin.qq.com/miniprogram/dev/component/web-view.html — web-view 个人主体不支持、业务域名
3. https://developers.weixin.qq.com/miniprogram/dev/framework/ability/network.html — 服务器域名 HTTPS/备案/证书要求、云托管免域名
4. https://developers.weixin.qq.com/miniprogram/product/record/guidelines.html — 小程序备案(2023-09 起强制)
5. https://developers.weixin.qq.com/miniprogram/product/renzheng.html — 微信认证资费(政府免费,其余付费)
6. https://developers.weixin.qq.com/miniprogram/product/reject.html — 常见拒绝情形
7. https://lbs.amap.com/api/wx/summary 、 https://lbs.amap.com/api/wx/guide/create-project/config-project — 高德微信小程序 SDK(amap-wx.js)
8. https://www.ixunke.com/article/543 — 认证年审制、个人/个体户 30 元、企业 300 元(非官方,与官方资费页互证)
9. https://www.etang.com/shangwufw/597.html — 个人主体功能限制综述(非官方)

*调研执行:2026-10-09,Asia/Shanghai。curl 实测样本存 /tmp/ota/。*
