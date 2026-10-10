#!/usr/bin/env python3
"""TASK-19 境外数据源可达性 + Photon 中文地名命中实测(直连,不走代理)。"""
import json, time, urllib.parse, urllib.request, socket, ssl

UA = {"User-Agent": "Where2Go-Research/1.0 (contact: local-test)"}
TIMEOUT = 12

def fetch(url, timeout=TIMEOUT):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(60000)
            return r.status, round(time.time()-t0, 2), body
    except urllib.error.HTTPError as e:
        return e.code, round(time.time()-t0, 2), e.read(20000)
    except Exception as e:
        return None, round(time.time()-t0, 2), str(e)[:200].encode()

def tcp_probe(host, port=443, timeout=8):
    t0 = time.time()
    try:
        ip = socket.gethostbyname(host)
        with socket.create_connection((ip, port), timeout=timeout):
            return f"TCP OK {ip} {round(time.time()-t0,2)}s"
    except Exception as e:
        return f"TCP FAIL {type(e).__name__} {round(time.time()-t0,2)}s"

print("=" * 70)
print("## 1. Photon 境外地名命中(中文+英文)")
cases = [
    ("因特拉肯", 46.686, 7.863, "瑞士"),
    ("Interlaken", 46.686, 7.863, "瑞士"),
    ("瑞士因特拉肯", 46.686, 7.863, "瑞士"),
    ("皇后镇", -45.031, 168.662, "新西兰"),
    ("Queenstown", -45.031, 168.662, "新西兰"),
    ("富良野", 43.342, 142.383, "日本"),
    ("Furano", 43.342, 142.383, "日本"),
    ("富士山", 35.36, 138.73, "日本"),
    ("Mount Fuji", 35.36, 138.73, "日本"),
    ("巴黎", 48.856, 2.352, "法国"),
    ("Paris", 48.856, 2.352, "法国"),
    ("清迈", 18.788, 98.985, "泰国"),
]
for q, lat, lng, country in cases:
    url = "https://photon.komoot.io/api/?limit=3&q=" + urllib.parse.quote(q)
    st, dt, body = fetch(url)
    try:
        data = json.loads(body)
        feats = data.get("features", [])
        if feats:
            p = feats[0]["properties"]
            c = feats[0]["geometry"]["coordinates"]
            # haversine 误差
            import math
            R = 6371.0
            dlat = math.radians(c[1]-lat); dlng = math.radians(c[0]-lng)
            a = math.sin(dlat/2)**2 + math.cos(math.radians(lat))*math.cos(math.radians(c[1]))*math.sin(dlng/2)**2
            err = round(2*R*math.asin(math.sqrt(a)), 1)
            print(f"  [{st} {dt}s] q={q!r:14} -> {p.get('name')} | city={p.get('city')} state={p.get('state')} country={p.get('country')} | ({round(c[1],4)},{round(c[0],4)}) 误差={err}km")
        else:
            print(f"  [{st} {dt}s] q={q!r:14} -> 0 结果")
    except Exception as e:
        print(f"  [{st} {dt}s] q={q!r:14} -> 解析失败 {e}: {body[:120]}")
    time.sleep(1.1)  # 1 req/s 礼貌节流

print()
print("## 1b. Photon 境外逆地理(因特拉肯坐标 -> country 字段)")
st, dt, body = fetch("https://photon.komoot.io/reverse?lat=46.686&lon=7.863")
try:
    d = json.loads(body)
    for f in d.get("features", [])[:1]:
        p = f["properties"]
        print(f"  [{st} {dt}s] -> name={p.get('name')} country={p.get('country')} state={p.get('state')}")
except Exception as e:
    print(f"  [{st} {dt}s] 解析失败: {body[:200]}")

print()
print("=" * 70)
print("## 2. Nominatim 直连复验(已知需代理)")
st, dt, body = fetch("https://nominatim.openstreetmap.org/search?q=Interlaken&format=json&limit=1", timeout=10)
print(f"  nominatim direct: status={st} time={dt}s body={body[:100]}")

print()
print("=" * 70)
print("## 3. Google Maps 直连可达性(产品无代理环境)")
probes = [
    ("maps.googleapis.com geocode(无key)", "https://maps.googleapis.com/maps/api/geocode/json?address=Interlaken"),
    ("maps.googleapis.com JS API", "https://maps.googleapis.com/maps/api/js?v=3.exp&key=***"),
    ("maps.gstatic.com 静态资源", "https://maps.gstatic.com/maps-api-v3/api/js/40/0/init.js"),
    ("khms1.googleapis.com 瓦片", "https://khms1.googleapis.com/kh?v=973&hl=zh-CN&x=0&y=0&z=1"),
]
for name, url in probes:
    st, dt, body = fetch(url, timeout=10)
    snippet = body[:150].decode("utf-8", "replace").replace("\n", " ") if isinstance(body, bytes) else str(body)
    print(f"  {name}: status={st} time={dt}s body={snippet}")

print()
print("## 3b. Google TCP 层探测")
for h in ["maps.googleapis.com", "maps.gstatic.com", "khms1.googleapis.com", "tile.googleapis.com"]:
    print(f"  {h}: {tcp_probe(h)}")

print()
print("=" * 70)
print("## 4. Azure Maps 直连可达性(无 key,只看网络层)")
az_probes = [
    ("atlas.microsoft.com 根", "https://atlas.microsoft.com/"),
    ("示例账户 atlas 子域", "https://sample-account.atlas.microsoft.com/maps/tile?api-version=2.0&style=satellite&zoom=2&x=1&y=1"),
    ("search.azuremapscn.com(中国区)", "https://search.azuremapscn.com/"),
]
for name, url in az_probes:
    st, dt, body = fetch(url, timeout=10)
    snippet = body[:120].decode("utf-8", "replace").replace("\n", " ") if isinstance(body, bytes) else str(body)
    print(f"  {name}: status={st} time={dt}s body={snippet}")
print()
print("## 4b. Azure TCP 层探测")
for h in ["atlas.microsoft.com", "search.azuremapscn.com", "account.atlas.azure.com"]:
    print(f"  {h}: {tcp_probe(h)}")

print()
print("=" * 70)
print("## 5. OSM 生态直连(底图瓦片候选)")
osm_probes = [
    ("tile.openstreetmap.org 瓦片", "https://tile.openstreetmap.org/1/0/0.png"),
    ("webrd01.is.autonavi.com 高德无key栅格(备用)", "https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x=1&y=1&z=2"),
]
for name, url in osm_probes:
    st, dt, body = fetch(url, timeout=10)
    print(f"  {name}: status={st} time={dt}s bytes={len(body) if isinstance(body, bytes) else body[:80]}")

print()
print("=" * 70)
print("## 6. Airbnb / trip.com / Booking 直连(curl 层)")
ota_probes = [
    ("Airbnb 境外 slug", "https://www.airbnb.com/s/Interlaken--Switzerland/homes"),
    ("trip.com 首页", "https://www.trip.com/"),
    ("trip.com 酒店搜索页(境外城市)", "https://www.trip.com/hotels/list?city=1274&cityName=Interlaken"),
    ("Booking searchresults(无头预期被WAF)", "https://www.booking.com/searchresults.zh-cn.html?ss=%E6%98%86%E5%B1%B1%E6%9F%8F%E8%BD%A9%E9%85%92%E5%BA%97"),
    ("Booking autocomplete(无aid)", "https://accommodations.booking.com/autocomplete.json?query=%E6%98%86%E5%B1%B1"),
]
for name, url in ota_probes:
    st, dt, body = fetch(url, timeout=15)
    if isinstance(body, bytes):
        # 抓 <title>
        import re
        m = re.search(rb"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        title = m.group(1).decode("utf-8", "replace").strip()[:100] if m else body[:100].decode("utf-8", "replace").replace("\n", " ")
    else:
        title = str(body)
    print(f"  {name}: status={st} time={dt}s title/snippet={title}")
    time.sleep(0.5)
