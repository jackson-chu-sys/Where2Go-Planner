#!/usr/bin/env python3
"""TASK-19:境外前端底图候选(直连):高德卫星 style=6 / 天地图(无key) / 腾讯境外。"""
import math, time, urllib.request

UA = {"User-Agent": "Mozilla/5.0"}
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def fetch(url, timeout=8):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=UA)
        with direct.open(req, timeout=timeout) as r:
            return r.status, round(time.time()-t0,2), r.read(300000)
    except Exception as e:
        return None, round(time.time()-t0,2), str(e)[:80].encode()

def tile(lat, lng, z):
    n = 2 ** z
    x = int((lng + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1/math.cos(lat_r))/math.pi)/2.0*n)
    return x, y

print("## 高德卫星影像 webst style=6(境外因特拉肯):")
for z in [10, 12, 14, 16]:
    x, y = tile(46.686, 7.863, z)
    st, dt, body = fetch(f"https://webst01.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}")
    print(f"  z={z}: status={st} bytes={len(body) if isinstance(body,bytes) else -1} {'(空白?)' if 0<len(body or b'')<500 else ''}")
    time.sleep(0.2)

print()
print("## 高德卫星+注记 style=8 vs 路网 style=7(境外 z=12):")
x, y = tile(46.686, 7.863, 12)
for style in [6, 7, 8]:
    st, dt, body = fetch(f"https://webst01.is.autonavi.com/appmaptile?style={style}&x={x}&y={y}&z={12}")
    print(f"  style={style}: status={st} bytes={len(body) if isinstance(body,bytes) else -1}")

print()
print("## 天地图(无 key,预期 403,记录行为):")
x, y = tile(46.686, 7.863, 10)
st, dt, body = fetch(f"https://t0.tianditu.gov.cn/vec_w/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=vec&STYLE=default&TILEMATRIXSET=w&FORMAT=tiles&TILECOL={x}&TILEROW={y}&TILEMATRIX=10&tk=")
print(f"  status={st} time={dt}s body={body[:100] if isinstance(body,bytes) else body}")

print()
print("## 腾讯地图瓦片(境外 z=12,腾讯用翻转y):")
x, y = tile(46.686, 7.863, 12)
yq = 2**12 - 1 - y
st, dt, body = fetch(f"https://rt2.map.gtimg.com/tile?z=12&x={x}&y={yq}&type=vector&styleid=3")
print(f"  status={st} time={dt}s bytes={len(body) if isinstance(body,bytes) else -1}")
st, dt, body = fetch(f"https://p2.map.gtimg.com/sateTiles/12/{x//16}/{y//16}/{x}_{y}.jpg")
print(f"  腾讯卫星: status={st} time={dt}s bytes={len(body) if isinstance(body,bytes) else -1}")
