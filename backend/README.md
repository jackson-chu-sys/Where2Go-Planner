# backend · 阶段0 免费数据源验证层

对应任务:`tasks/TASK-001-data-source-poc.md`(ADR-006:免费、无需 key 的数据源)。

## 目录

```
backend/
├─ requirements.txt              依赖(requests;测试用 pytest)
├─ conftest.py                   pytest 路径引导
├─ test_data_sources.py          三个源的纯 mock 单测(不触网,26 个用例)
└─ data_sources/
   ├─ __init__.py                统一导出 route / geocode / reverse / nearby_places
   ├─ _common.py                 User-Agent、timeout(≤20s)、JSON 请求与中文错误
   ├─ osrm.py                    驾车路线 → {distance_km, duration_min}
   ├─ nominatim.py               正向/逆向地理编码 → {lat, lng, display_name}
   ├─ overpass.py                周边 POI 检索 → [{lat, lng, name, tags}]
   └─ verify_poc.py              真实网络端到端验证脚本(联网)
```

## 安装与运行

```bash
python3 -m venv .venv && . .venv/bin/activate      # 或用 uv venv .venv
pip install -r backend/requirements.txt

python -m pytest backend/ -q                       # 单测(mock,不触网)
python backend/test_data_sources.py                # 不装 pytest 也能跑同一套断言

python backend/data_sources/verify_poc.py          # 真实验证(需联网,约 30-60s)
cd backend && python -m data_sources.verify_poc    # 等价的包内运行方式
```

`verify_poc.py` 链路:北京 → 正向定位可核地名 → 逆向确认当前位置 → 按类别
(自然风光 100 km / 旅游景点 50 km)检索真实 POI → 取最近一个 → OSRM 驾车路线,
并对时速、绕行比做合理性校验。任一步失败会打印中文报错并以非 0 退出码结束。

## 用法示例

```python
from data_sources import geocode, nearby_places, reverse, route

origin = geocode("北京")                     # {'lat': 39.9057136, 'lng': 116.3912972, ...}
here = reverse(39.9042, 116.4074)            # 当前位置地名
places = nearby_places(origin["lat"], origin["lng"], 50_000,
                       {"tourism": "attraction"}, limit=20, require_name=True)
leg = route((origin["lng"], origin["lat"]), (places[0]["lng"], places[0]["lat"]))
# {'distance_km': 2.678, 'duration_min': 5.2}
```

## 真实 API 注意点(2026-09-08 实测)

* **OSRM**:`distance` 是米、`duration` 是秒,坐标顺序为 `lng,lat`;失败时 HTTP 仍可能
  200,需看 `code != "Ok"`。备选实例 `https://routing.openstreetmap.de/routed-car`
  实测结果一致,可用 `WHERE2GO_OSRM_ENDPOINT` 或 `endpoint=` 切换。
* **Nominatim**:响应里 `lat`/`lon` 是**字符串**;必须带可识别 `User-Agent`
  (`Where2Go-POC/0.1 (dev)`),官方政策 ≤1 次/秒,客户端已内置节流。
* **Overpass**:公共实例经常返回 `504 + HTML`("The server is probably too busy")。
  实测 `overpass-api.de` 繁忙时 `z.overpass-api.de`、`maps.mail.ru` 仍可用,因此
  客户端做了**端点链 + 重试 + 退避**降级(`used_endpoint` 可查实际服务方);
  `overpass.osm.ch` 实测无数据、`overpass.private.coffee` 数据陈旧数月,均未纳入默认链。
  另外 `around` 的结果**不按距离排序**,way/relation 需 `out center` 才有坐标,
  部分 POI 没有 `name` —— 解析层已按大圆距离重排、兼容 `center`、名字多级兜底。

## 范围

仅数据获取层。不含 HTTP 服务、前端、DB、用户系统与 LLM 调用(见任务 SCOPE)。
