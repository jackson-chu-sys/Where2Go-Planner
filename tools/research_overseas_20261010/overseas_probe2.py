#!/usr/bin/env python3
"""TASK-19 第二轮:直连(禁代理) vs 走代理 对比 + Nominatim 中文境外命中 + trip.com 境外拼法。"""
import json, os, time, math, re, urllib.parse, urllib.request

print("env proxies:", {k: v for k, v in os.environ.items() if "proxy" in k.lower()})

direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
UA = {"User-Agent": "Where2Go-Research/1.0 (contact: local-test)"}

def fetch(url, opener=None, timeout=12):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=UA)
        op = opener or urllib.request.build_opener()
        with op.open(req, timeout=timeout) as r:
            body = r.read(60000)
            return r.status, round(time.time()-t0, 2), body
    except urllib.error.HTTPError as e:
        return e.code, round(time.time()-t0, 2), e.read(20000)
    except Exception as e:
        return None, round(time.time()-t0, 2), str(e)[:160].encode()

def hav(a_lat, a_lng, b_lat, b_lng):
    R = 6371.0
    dlat = math.radians(b_lat-a_lat); dlng = math.radians(b_lng-a_lng)
    x = math.sin(dlat/2)**2 + math.cos(math.radians(a_lat))*math.cos(math.radians(b_lat))*math.sin(dlng/2)**2
    return round(2*R*math.asin(math.sqrt(x)), 1)

print("=" * 70)
print("## A. 强制直连(禁代理)可达性")
targets = [
    ("Google geocode", "https://maps.googleapis.com/maps/api/geocode/json?address=Interlaken"),
    ("Google JS", "https://maps.googleapis.com/maps/api/js?v=3.exp&key=***"),
    ("Google 瓦片", "https://khms1.googleapis.com/kh?v=973&hl=zh-CN&x=0&y=0&z=1"),
    ("Azure atlas", "https://atlas.microsoft.com/"),
    ("Photon", "https://photon.komoot.io/api/?limit=1&q=Interlaken"),
    ("Nominatim", "https://nominatim.openstreetmap.org/search?q=Interlaken&format=json&limit=1"),
    ("OSM 瓦片", "https://tile.openstreetmap.org/1/0/0.png"),
    ("trip.com", "https://www.trip.com/"),
    ("Airbnb", "https://www.airbnb.com/s/Interlaken--Switzerland/homes"),
    ("Booking", "https://www.booking.com/searchresults.zh-cn.html?ss=Interlaken"),
]
for name, url in targets:
    st, dt, body = fetch(url, opener=direct, timeout=10)
    note = ""
    if isinstance(body, bytes):
        note = body[:80].decode("utf-8", "replace").replace("\n", " ")
    print(f"  直连 {name}: status={st} time={dt}s {note}")

print()
print("=" * 70)
print("## B. Nominatim 中文境外地名命中(accept-language=zh-CN)")
nom_cases = [
    ("因特拉肯", 46.686, 7.863),
    ("瑞士因特拉肯", 46.686, 7.863),
    ("皇后镇", -45.031, 168.662),
    ("富良野", 43.342, 142.383),
    ("富士山", 35.36, 138.73),
    ("巴黎", 48.856, 2.352),
    ("清迈", 18.788, 98.985),
    ("Interlaken", 46.686, 7.863),
]
for q, lat, lng in nom_cases:
    url = "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=3&accept-language=zh-CN&q=" + urllib.parse.quote(q)
    st, dt, body = fetch(url, timeout=12)
    try:
        arr = json.loads(body)
        if arr:
            t = arr[0]
            err = hav(lat, lng, float(t["lat"]), float(t["lon"]))
            print(f"  [{st} {dt}s] q={q!r:12} -> {t.get('display_name','')[:60]} | 误差={err}km")
        else:
            print(f"  [{st} {dt}s] q={q!r:12} -> 0 结果")
    except Exception as e:
        print(f"  [{st} {dt}s] q={q!r:12} -> 解析失败 {body[:100]}")
    time.sleep(1.1)

print()
print("## B2. Nominatim 中文+国家限定(countrycodes)")
for q, cc, lat, lng in [("因特拉肯", "ch", 46.686, 7.863), ("皇后镇", "nz", -45.031, 168.662), ("巴黎", "fr", 48.856, 2.352)]:
    url = f"https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&accept-language=zh-CN&countrycodes={cc}&q=" + urllib.parse.quote(q)
    st, dt, body = fetch(url, timeout=12)
    try:
        arr = json.loads(body)
        if arr:
            t = arr[0]
            err = hav(lat, lng, float(t["lat"]), float(t["lon"]))
            print(f"  [{st} {dt}s] q={q!r}+cc={cc} -> {t.get('display_name','')[:60]} 误差={err}km")
        else:
            print(f"  [{st} {dt}s] q={q!r}+cc={cc} -> 0 结果")
    except Exception:
        print(f"  [{st} {dt}s] q={q!r}+cc={cc} -> {body[:100]}")
    time.sleep(1.1)

print()
print("=" * 70)
print("## C. trip.com 境外拼法实测")
trip_probes = [
    ("SEO 城市页 interlaken", "https://www.trip.com/hotels/interlaken-hotel/"),
    ("SEO 城市页 中文?", "https://www.trip.com/hotels/interlaken-hotel/?locale=zh-CN"),
    ("搜索页 keyword", "https://www.trip.com/hotels/list?keyword=%E5%9B%A0%E7%89%B9%E6%8B%89%E8%82%AF"),
    ("搜索页 city(苏黎世试探 id)", "https://www.trip.com/hotels/list?city=1258"),
]
for name, url in trip_probes:
    st, dt, body = fetch(url, timeout=15)
    title = ""
    if isinstance(body, bytes):
        m = re.search(rb"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        title = m.group(1).decode("utf-8", "replace").strip()[:90] if m else body[:90].decode("utf-8","replace").replace("\n"," ")
    print(f"  {name}: status={st} time={dt}s title={title}")
    time.sleep(0.5)
