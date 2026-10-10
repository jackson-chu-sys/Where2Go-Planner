#!/usr/bin/env python3
"""TASK-19 第4轮:境外 POI/路线候选源(Overpass/OSRM/Carto)+ Azure 走代理确认服务活着。"""
import json, time, urllib.parse, urllib.request

UA = {"User-Agent": "Where2Go-Research/1.0"}
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
proxied = urllib.request.build_opener()  # 走 env 代理

def fetch(url, opener=direct, timeout=20, data=None):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=UA, data=data)
        with opener.open(req, timeout=timeout) as r:
            return r.status, round(time.time()-t0, 2), r.read(200000)
    except urllib.error.HTTPError as e:
        return e.code, round(time.time()-t0, 2), e.read(20000)
    except Exception as e:
        return None, round(time.time()-t0, 2), str(e)[:160].encode()

print("## A. Overpass 境外 POI(因特拉肯 5km 圆,自然+景点)")
q = """[out:json][timeout:60];
( nwr["natural"](around:5000,46.686,7.863); nwr["tourism"~"attraction|viewpoint|museum"](around:5000,46.686,7.863); );
out center 30;"""
for label, opener in [("直连", direct), ("代理", proxied)]:
    st, dt, body = fetch("https://overpass-api.de/api/interpreter", opener=opener, timeout=90,
                         data=("data=" + urllib.parse.quote(q)).encode())
    try:
        d = json.loads(body)
        els = d.get("elements", [])
        names = [e.get("tags", {}).get("name", "?") for e in els[:5]]
        print(f"  {label}: status={st} time={dt}s elements={len(els)} names={names}")
    except Exception:
        print(f"  {label}: status={st} time={dt}s raw={body[:150]}")

print()
print("## B. OSRM demo 境外驾车(因特拉肯->格林德瓦)")
for label, opener in [("直连", direct), ("代理", proxied)]:
    url = "https://router.project-osrm.org/route/v1/driving/7.863,46.686;8.036,46.624?overview=false"
    st, dt, body = fetch(url, opener=opener, timeout=20)
    try:
        d = json.loads(body)
        r = d.get("routes", [{}])[0]
        print(f"  {label}: status={st} time={dt}s dist={round(r.get('distance',0)/1000,1)}km dur={round(r.get('duration',0)/60)}min")
    except Exception:
        print(f"  {label}: status={st} time={dt}s raw={body[:150]}")

print()
print("## C. 前端底图候选瓦片(直连)")
tiles = [
    ("OSM 标准", "https://tile.openstreetmap.org/8/133/89.png"),
    ("Carto light", "https://a.basemaps.cartocdn.com/light_all/8/133/89.png"),
    ("高德无key栅格(webrd)", "https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x=133&y=89&z=8"),
    ("高德境外区域瓦片(z=6 瑞士)", "https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x=33&y=22&z=6"),
]
for name, url in tiles:
    st, dt, body = fetch(url, timeout=12)
    size = len(body) if isinstance(body, bytes) else -1
    ctype = "?"
    print(f"  直连 {name}: status={st} time={dt}s bytes={size}")

print()
print("## D. Azure Maps(走代理确认服务活着 + 中国区域名)")
for label, opener in [("代理", proxied), ("直连", direct)]:
    st, dt, body = fetch("https://atlas.microsoft.com/", opener=opener, timeout=15)
    print(f"  {label} atlas.microsoft.com: status={st} time={dt}s")
    st2, dt2, body2 = fetch("https://search.azuremapscn.com/", opener=opener, timeout=15)
    print(f"  {label} search.azuremapscn.com: status={st2} time={dt2}s {str(body2[:80])}")

print()
print("## E. DNS 解析对比(直连失败是 DNS 还是路由)")
import socket
for h in ["maps.googleapis.com", "atlas.microsoft.com", "tile.openstreetmap.org", "nominatim.openstreetmap.org", "overpass-api.de"]:
    try:
        print(f"  {h} -> {socket.gethostbyname(h)}")
    except Exception as e:
        print(f"  {h} -> DNS FAIL {e}")
