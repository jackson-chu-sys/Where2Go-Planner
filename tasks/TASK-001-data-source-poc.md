# TASK-001 · POC: 免费数据源验证层

## 目标
验证 Where2Go 阶段0选定的**免费、无需 key 数据源**可用,并封装成可复用的 Python 数据源层。这是产品 backend/ 的第一块地基。

## 范围(SCOPE)
只做数据获取层 + 测试 + 一条真实验证脚本。**不含** HTTP 服务、前端、DB、用户系统、LLM 调用。

## 需实现(backend/data_sources/ 下)
1. `osrm.py` — OSRM 驾车路线规划
   - `route(start_lnglat, end_lnglat) -> {distance_km, duration_min}`
   - 默认端点 `https://router.project-osrm.org`,可切换 `https://routing.openstreetmap.de/routed-car`
2. `nominatim.py` — OSM 地理编码
   - `geocode(query) -> {lat, lng, display_name}`(正向:城市/地名→坐标)
   - `reverse(lat, lng) -> {lat, lng, display_name}`(逆:坐标→地名,用于“当前位置”)
   - 必须带 User-Agent,遵守礼貌请求
3. `overpass.py` — Overpass POI 检索(目的地库冷启动)
   - `nearby_places(lat, lng, radius_m, tags) -> list[{lat,lng,name,tags}]`
   - 默认端点 `https://overpass-api.de/api/interpreter`
4. `__init__.py` — 统一导出
5. `verify_poc.py` — **真实网络端到端验证脚本**(北京起点→按类别找周边目的地→取最近一个→驾车路线)。逐源打印结果,可 `python verify_poc.py` 直接跑。设 `timeout` 与失败时给出明确中文报错。

## 技术约束
- 用 `requests` 库;依赖写 `backend/requirements.txt`
- 纯 Python 3,无框架;结构清晰、有类型注解与简短 docstring
- User-Agent 设置:`Where2Go-POC/0.1 (dev)`;所有请求设合理 timeout(≤20s)
- OSRM/Nominatim/Overpass 均需真实直连可达(本环境可直连,不走代理)

## 测试策略
- `backend/test_data_sources.py`:对三个源做**单测**(mock 网络,断言 URL/参数/解析逻辑)
- 真实网络的可达性验证交给 `verify_poc.py`(本任务验收执行它,不放进单测)
- 允许运行 `python -m pytest backend/` 全部通过;不依赖 pytest 则写 `if __name__` 直接可跑的断言

## 验收标准(AC)
- AC1: `verify_poc.py` 对北京(39.9042,116.4074)能:正向定位到可核地名、按类别(自然风光/旅游景点类 tag)在 50-100km 内返回≥3 个真实 POI、并对最近一个成功返回驾车 {distance_km, duration_min} 且数值合理(非 0、范围相符)
- AC2: 三个源在纯单测(mock)下全部通过
- AC3: 代码在 `backend/` 下,依赖在 `requirements.txt`,不破坏仓库根现有 .md 文档

## 执行注意
- 别动仓库根的其他文件(01/02 .md、需求文档等)
- 完成后 `git add backend/ tasks/TASK-001-data-source-poc.md && git commit`(不要 push,由控制面处理)
- 汇报:改了哪些文件、verify_poc.py 实际输出、单测结果
