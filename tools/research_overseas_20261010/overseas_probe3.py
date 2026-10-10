#!/usr/bin/env python3
"""TASK-19:高德境外 geocode 失败形态复现 + Photon「中文→LLM英文名」路径命中率 + trip.com suggest。"""
import json, time, urllib.parse, urllib.request

def load_key(path="/opt/data/.env", name="WHERE2GO_AMAP_KEY"):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None

AK = load_key()
UA = {"User-Agent": "Where2Go-Research/1.0"}
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def fetch(url, opener=direct, timeout=12):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=UA)
        with opener.open(req, timeout=timeout) as r:
            return r.status, round(time.time()-t0, 2), r.read(80000)
    except urllib.error.HTTPError as e:
        return e.code, round(time.time()-t0, 2), e.read(20000)
    except Exception as e:
        return None, round(time.time()-t0, 2), str(e)[:160].encode()

print("key loaded:", bool(AK))
print("=" * 70)
print("## A. 高德 geocode 境外中文(复现失败形态)")
for city in ["因特拉肯", "瑞士因特拉肯", "富良野", "皇后镇", "巴黎", "Interlaken"]:
    url = "https://restapi.amap.com/v3/geocode/geo?key=" + urllib.parse.quote(AK) + "&city=&address=" + urllib.parse.quote(city)
    st, dt, body = fetch(url)
    try:
        d = json.loads(body)
        geos = d.get("geocodes", [])
        top = geos[0] if geos else {}
        print(f"  [{st} {dt}s] {city!r:12} status={d.get('status')} count={d.get('count')} -> {top.get('formatted_address','')[:40]} location={top.get('location','')} country={top.get('country')}")
    except Exception:
        print(f"  [{st} {dt}s] {city!r} raw={body[:120]}")
    time.sleep(0.7)

print()
print("## B. 高德境外 POI 文本搜索(已知 count=0,复核)")
for kw in ["富士山", "Interlaken", "皇后镇"]:
    url = "https://restapi.amap.com/v3/place/text?key=" + urllib.parse.quote(AK) + "&offset=5&keywords=" + urllib.parse.quote(kw)
    st, dt, body = fetch(url)
    try:
        d = json.loads(body)
        pois = d.get("pois", [])
        names = [p.get("name","")[:20] for p in pois[:3]]
        print(f"  [{st} {dt}s] {kw!r} count={d.get('count')} top3={names}")
    except Exception:
        print(f"  [{st} {dt}s] {kw!r} raw={body[:120]}")
    time.sleep(0.7)

print()
print("=" * 70)
print("## C. Photon 英文名+国家限定命中率(模拟「LLM 翻译后查询」路径)")
ph_cases = [
    ("Interlaken Switzerland", 46.686, 7.863),
    ("Queenstown New Zealand", -45.031, 168.662),
    ("Furano Japan", 43.342, 142.383),
    ("Paris France", 48.856, 2.352),
    ("Chiang Mai Thailand", 18.788, 98.985),
    ("Zermatt Switzerland", 46.02, 7.749),
    ("Reykjavik Iceland", 64.146, -21.942),
]
import math
for q, lat, lng in ph_cases:
    url = "https://photon.komoot.io/api/?limit=3&q=" + urllib.parse.quote(q)
    st, dt, body = fetch(url)
    try:
        d = json.loads(body)
        f = (d.get("features") or [None])[0]
        if f:
            c = f["geometry"]["coordinates"]
            R=6371.0
            dla=math.radians(c[1]-lat); dlo=math.radians(c[0]-lng)
            a=math.sin(dla/2)**2+math.cos(math.radians(lat))*math.cos(math.radians(c[1]))*math.sin(dlo/2)**2
            err=round(2*R*math.asin(math.sqrt(a)),1)
            p=f["properties"]
            print(f"  [{st} {dt}s] {q!r:26} -> {p.get('name')} country={p.get('country')} 误差={err}km")
        else:
            print(f"  [{st} {dt}s] {q!r:26} -> 0 结果")
    except Exception:
        print(f"  [{st} {dt}s] {q!r} raw={body[:100]}")
    time.sleep(1.1)

print()
print("=" * 70)
print("## D. trip.com 搜索建议接口(境外城市 id 解析)")
# trip.com 公开 city suggest
for q in ["Interlaken", "因特拉肯", "Queenstown"]:
    url = "https://m.trip.com/restapi/soa2/22769/json/getSuggestion?keyword=" + urllib.parse.quote(q)
    st, dt, body = fetch(url)
    print(f"  [{st} {dt}s] {q!r} -> {body[:200].decode('utf-8','replace') if isinstance(body,bytes) else body}")
    time.sleep(0.6)
