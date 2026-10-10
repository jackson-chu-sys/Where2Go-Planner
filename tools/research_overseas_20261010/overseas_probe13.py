#!/usr/bin/env python3
"""TASK-19:境内外判定实测——Photon/Nominatim 逆地理对境内坐标的 country 字段。"""
import json, time, urllib.parse, urllib.request

UA = {"User-Agent": "Where2Go-Research/1.0"}
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
proxied = urllib.request.build_opener()

def fetch(url, opener=direct, timeout=10):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=UA)
        with opener.open(req, timeout=timeout) as r:
            return r.status, round(time.time()-t0,2), r.read(20000)
    except Exception as e:
        return None, round(time.time()-t0,2), str(e)[:100].encode()

print("## Photon reverse 境内坐标(上海人民广场 31.2304,121.4737):")
st, dt, body = fetch("https://photon.komoot.io/reverse?lat=31.2304&lon=121.4737")
try:
    d = json.loads(body)
    for f in d.get("features", [])[:2]:
        p = f["properties"]
        print(f"  [{st} {dt}s] name={p.get('name')} country={p.get('country')} state={p.get('state')} city={p.get('city')}")
except Exception:
    print(f"  [{st} {dt}s] {body[:150]}")

print()
print("## Photon reverse 边境/争议坐标(漠河 53.47,122.34 中国最北):")
st, dt, body = fetch("https://photon.komoot.io/reverse?lat=53.47&lon=122.34")
try:
    d = json.loads(body)
    for f in d.get("features", [])[:1]:
        p = f["properties"]
        print(f"  [{st} {dt}s] name={p.get('name')} country={p.get('country')}")
except Exception:
    print(f"  [{st} {dt}s] {body[:150]}")

print()
print("## Photon 正向『中文境内城市』命中(判定另一路径:先 geocode 再看 country):")
for q in ["上海", "成都", "昆山"]:
    st, dt, body = fetch("https://photon.komoot.io/api/?limit=1&q=" + urllib.parse.quote(q))
    try:
        d = json.loads(body)
        f = (d.get("features") or [{}])[0]
        p = f.get("properties", {})
        print(f"  [{st} {dt}s] {q} -> name={p.get('name')} country={p.get('country')}")
    except Exception:
        print(f"  [{st} {dt}s] {q} raw={body[:80]}")
    time.sleep(1.1)
