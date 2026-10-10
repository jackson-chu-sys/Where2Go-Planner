#!/usr/bin/env python3
"""TASK-19:境外底图瓦片镜像直连可达性(wikimedia/osm.de/osm.fr/cartocdn 备线)。"""
import math, time, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (research; contact local)"}
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def fetch(url, timeout=8):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=UA)
        with direct.open(req, timeout=timeout) as r:
            return r.status, round(time.time()-t0,2), r.read(300000)
    except Exception as e:
        return None, round(time.time()-t0,2), str(e)[:90].encode()

def tile(lat, lng, z):
    n = 2 ** z
    x = int((lng + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1/math.cos(lat_r))/math.pi)/2.0*n)
    return x, y

z = 12
x, y = tile(46.686, 7.863, z)  # Interlaken
mirrors = [
    ("wikimedia maps", f"https://maps.wikimedia.org/osm-intl/{z}/{x}/{y}.png"),
    ("openstreetmap.de", f"https://tile.openstreetmap.de/{z}/{x}/{y}.png"),
    ("openstreetmap.fr", f"https://a.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png"),
    ("cartocdn light", f"https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"),
    ("carto maps (basemaps.carto.com)", f"https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"),
    ("osm tile 官方(重测)", f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
    ("高德矢量(对照,境内可用境外空白)", f"https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}"),
]
for name, url in mirrors:
    st, dt, body = fetch(url)
    size = len(body) if isinstance(body, bytes) else -1
    png = isinstance(body, bytes) and body[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xdb")
    print(f"  {name}: status={st} time={dt}s bytes={size} 图像={'Y' if png else 'N'}")
    time.sleep(0.4)
