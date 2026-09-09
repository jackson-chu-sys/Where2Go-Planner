"""Where2Go 业务编排层(阶段 1)。

夹在 ``data_sources``(只管网络)与 ``app``(只管 HTTP)之间:

* :mod:`services.bands` —— 环形距离分段定义与环内过滤(haversine);
* :mod:`services.classify` —— OSM tags → 需求**四分类**归类引擎:归类优先级
  (滑雪 > 运动 > 人文美食 > 自然)、按 OSM ``(type, id)`` 跨 tag 去重、检索 tag 并集分组;
  :mod:`services.categories` 保留为它的兼容导入面(阶段1a 的旧名字);
* :mod:`services.place_loader` —— (城市, band) 抓取入库编排:命中库直接读,否则走 Overpass;
* :mod:`services.intro` —— LLM 一句话简介(Provider 可切换,按 POI 缓存在 ``Place.intro``,
  失败降级为空简介、不阻塞入库);
* :mod:`services.reclassify` —— 存量库重归类:把 ``category`` 的旧值/空值按四分类规则重算(离线)。
"""
