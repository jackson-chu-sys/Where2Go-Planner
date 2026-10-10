#!/usr/bin/env python3
"""TASK-19:Azure 中国区端点直连 + 高德境外卫星瓦片内容鉴别 + DNS 污染证据。"""
import socket, time, urllib.request

UA = {"User-Agent": "Mozilla/5.0"}
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def fetch(url, timeout=8):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=UA)
        with direct.open(req, timeout=timeout) as r:
            return r.status, round(time.time()-t0,2), r.read(100000)
    except Exception as e:
        return None, round(time.time()-t0,2), str(e)[:120].encode()

print("## Azure Maps 中国区(21Vianet)端点直连:")
hosts = ["atlas.azuremapscn.com", "search.azuremapscn.com", "route.azuremapscn.com", "account.azuremapscn.com"]
for h in hosts:
    try:
        ip = socket.gethostbyname(h)
        dns = f"DNS={ip}"
    except Exception as e:
        dns = f"DNS FAIL {e}"
    st, dt, body = fetch(f"https://{h}/")
    print(f"  {h}: {dns} | HTTP status={st} time={dt}s")

print()
print("## DNS 污染证据(直连解析):")
for h in ["maps.googleapis.com", "tile.openstreetmap.org", "nominatim.openstreetmap.org", "atlas.microsoft.com", "photon.komoot.io", "overpass-api.de"]:
    try:
        print(f"  {h} -> {socket.gethostbyname(h)}")
    except Exception as e:
        print(f"  {h} -> FAIL {e}")

print()
print("## 高德境外瓦片内容鉴别(4235 bytes 是否同一张图):")
import hashlib
def tile(lat, lng, z):
    import math
    n = 2 ** z
    x = int((lng + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1/math.cos(lat_r))/math.pi)/2.0*n)
    return x, y
for place, lat, lng in [("因特拉肯", 46.686, 7.863), ("皇后镇", -45.031, 168.662)]:
    x, y = tile(lat, lng, 14)
    st, dt, body = fetch(f"https://webst01.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z=14")
    if isinstance(body, bytes):
        print(f"  {place} style=6 z14: {len(body)}B sha1={hashlib.sha1(body).hexdigest()[:12]} png={body[:4]==b'\\x89PNG'}")
    time.sleep(0.3)
