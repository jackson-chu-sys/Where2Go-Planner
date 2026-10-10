#!/usr/bin/env python3
"""TASK-19:①携程境外 cityId/hotelId 落地页验证 ②Booking 城市页 dest_id 爬取可行性(境内外)。"""
import json, re, time, urllib.parse, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def fetch(url, timeout=15):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=UA)
        with direct.open(req, timeout=timeout) as r:
            return r.status, round(time.time()-t0,2), r.read(500000)
    except urllib.error.HTTPError as e:
        return e.code, round(time.time()-t0,2), e.read(20000)
    except Exception as e:
        return None, round(time.time()-t0,2), str(e)[:150].encode()

def title_of(body):
    if not isinstance(body, bytes): return str(body)[:100]
    html = body.decode("utf-8","replace")
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S|re.I)
    return (m.group(1).strip()[:90] if m else html[:60].replace("\n"," "))

print("## A. 携程境外落地页")
cases = [
    ("detail 境外酒店(皇后镇诺富特 2196343)", "https://hotels.ctrip.com/hotels/detail/?hotelId=2196343"),
    ("list 境外城市(cityId=3167 因特拉肯)", "https://hotels.ctrip.com/hotels/list?cityId=3167"),
    ("list 境外城市+searchWord", "https://hotels.ctrip.com/hotels/list?cityId=3167&searchWord=%E9%85%92%E5%BA%97&directSearch=1&old=1"),
]
for name, url in cases:
    st, dt, body = fetch(url)
    print(f"  [{st} {dt}s] {name}\n      {title_of(body)}")
    time.sleep(0.5)

print()
print("## B. Booking 城市页(dest_id 提取源)直连可行性")
bk_cases = [
    ("苏州(境内)", "https://www.booking.com/city/cn/suzhou.zh-cn.html"),
    ("因特拉肯(境外 ch)", "https://www.booking.com/city/ch/interlaken.zh-cn.html"),
    ("皇后镇(境外 nz)", "https://www.booking.com/city/nz/queenstown.zh-cn.html"),
    ("上海(境内)", "https://www.booking.com/city/cn/shanghai.zh-cn.html"),
]
for name, url in bk_cases:
    st, dt, body = fetch(url)
    dest = ""
    if isinstance(body, bytes):
        html = body.decode("utf-8","replace")
        m = re.search(r"dest_id=(-?\d+)", html)
        m2 = re.search(r'"b_dest_id"\s*:\s*(-?\d+)', html)
        dest = f"dest_id={m.group(1) if m else (m2.group(1) if m2 else '未找到')}"
    print(f"  [{st} {dt}s] {name} -> {dest} | {title_of(body)[:60]}")
    time.sleep(0.8)
