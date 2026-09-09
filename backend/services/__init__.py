"""Where2Go 业务编排层(阶段 1)。

夹在 ``data_sources``(只管网络)与 ``app``(只管 HTTP)之间:

* :mod:`services.bands` —— 环形距离分段定义与环内过滤(haversine);
* :mod:`services.categories` —— OSM tags → 需求分类的**简化**归类(TASK-1b 会替换);
* :mod:`services.place_loader` —— (城市, band) 抓取入库编排:命中库直接读,否则走 Overpass。
"""
