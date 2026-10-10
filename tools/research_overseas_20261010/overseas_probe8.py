#!/usr/bin/env python3
"""TASK-19:高德无key栅格瓦片境外最大可用 zoom + 境外瓦片内容判别。"""
import time, urllib.request

UA = {"User-Agent": "Mozilla/5.0"}
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def fetch(url, timeout=10):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=UA)
        with direct.open(req, timeout=timeout) as r:
            return r.status, round(time.time()-t0, 2), r.read(200000)
    except Exception as e:
        return None, round(time.time()-t0, 2), str(e)[:100].encode()

# 因特拉肯 (46.686N, 7.863E) 各 zoom 的瓦片号
import math
def tile(lat, lng, z):
    n = 2 ** z
    x = int((lng + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1/math.cos(lat_r)) / math.pi) / 2.0 * n)
    return x, y

print("高德无key栅格(webrd, style=8)境外瓦片 zoom 阶梯(因特拉肯):")
for z in [6, 8, 10, 12, 14, 16]:
    x, y = tile(46.686, 7.863, z)
    url = f"https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}"
    st, dt, body = fetch(url)
    size = len(body) if isinstance(body, bytes) else -1
    # 空白瓦片通常 < 500 bytes
    print(f"  z={z:2} x={x:5} y={y:5}: status={st} time={dt}s bytes={size} {'(疑似空白)' if 0 < size < 500 else ''}")
    time.sleep(0.3)

print()
print("对照:境内(上海人民广场)同 zoom:")
for z in [14, 16]:
    x, y = tile(31.2304, 121.4737, z)
    url = f"https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}"
    st, dt, body = fetch(url)
    print(f"  z={z}: status={st} bytes={len(body) if isinstance(body,bytes) else -1}")
    time.sleep(0.3)

print()
print("OSM 官方瓦片直连复验(不同 IP 节点):")
for host in ["tile.openstreetmap.org", "a.tile.openstreetmap.org"]:
    st, dt, body = fetch(f"https://{host}/10/537/363.png", timeout=8)
    print(f"  {host}: status={st} time={dt}s bytes={len(body) if isinstance(body,bytes) else body}")
