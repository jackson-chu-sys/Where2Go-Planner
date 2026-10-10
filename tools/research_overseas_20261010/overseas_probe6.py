#!/usr/bin/env python3
"""TASK-19:携程 suggest 对境外酒店的解析 + hotels.ctrip.com detail 境外可用性 + trip.com SEO 格式。"""
import json, re, time, urllib.parse, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def fetch(url, opener=direct, timeout=15, data=None, ctype=None):
    t0 = time.time()
    hdrs = dict(UA)
    if ctype: hdrs["Content-Type"] = ctype
    try:
        req = urllib.request.Request(url, headers=hdrs, data=data)
        with opener.open(req, timeout=timeout) as r:
            return r.status, round(time.time()-t0,2), r.read(200000)
    except urllib.error.HTTPError as e:
        return e.code, round(time.time()-t0,2), e.read(20000)
    except Exception as e:
        return None, round(time.time()-t0,2), str(e)[:150].encode()

print("## A. 携程 suggest(既有 TASK-17 端点)对境外查询")
for kw in ["因特拉肯", "Interlaken", "皇后镇", "富士山 酒店"]:
    payload = json.dumps({"searchType": "H", "keyword": kw, "head": {"cid": "", "ctok": "", "cver": "1.0", "lang": "01", "sid": "8888", "syscode": "09", "auth": "", "extension": []}}).encode()
    st, dt, body = fetch("https://m.ctrip.com/restapi/soa2/21881/json/gaHotelSearchEngine", data=payload, ctype="application/json")
    try:
        d = json.loads(body)
        items = d.get("responseItems") or d.get("items") or []
        flat = []
        def walk(x):
            if isinstance(x, dict):
                if x.get("type") in ("Hotel", "City") or ("word" in x and "id" in x):
                    flat.append({k: x.get(k) for k in ("type","id","word","cityId","cityName","countryName")})
                for v in x.values(): walk(v)
            elif isinstance(x, list):
                for v in x: walk(v)
        walk(d)
        print(f"  [{st} {dt}s] {kw!r} -> {flat[:4]}")
    except Exception:
        print(f"  [{st} {dt}s] {kw!r} raw={body[:150]}")
    time.sleep(0.6)

print()
print("## B. hotels.ctrip.com detail 境外可用性(用一个境外 hotelId 试探)")
# 若 A 解析出境外 hotelId 则用之;先测已知无效 id 的行为
st, dt, body = fetch("https://hotels.ctrip.com/hotels/detail/?hotelId=83790270")
html = body.decode("utf-8","replace") if isinstance(body,bytes) else ""
m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S|re.I)
print(f"  境内样例(昆山柏轩): [{st} {dt}s] title={(m.group(1).strip()[:70] if m else '')}")

print()
print("## C. trip.com SEO 页格式试探")
for url in [
    "https://www.trip.com/hotels/interlaken-hotel/detail/",
    "https://www.trip.com/hotels/w/interlaken/",
    "https://www.trip.com/hotels/list?cityName=Interlaken&city=2130",
    "https://cn.trip.com/hotels/interlaken-hotel/",
]:
    st, dt, body = fetch(url, timeout=12)
    html = body.decode("utf-8","replace") if isinstance(body,bytes) else ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S|re.I)
    print(f"  [{st} {dt}s] {url}\n      title={(m.group(1).strip()[:80] if m else html[:60])}")
    time.sleep(0.4)
