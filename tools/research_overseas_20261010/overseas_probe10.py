#!/usr/bin/env python3
"""TASK-19:Booking 城市页 dest_id 服务端提取可行性(带 cookie/无头 UA 变体)。"""
import re, time, urllib.parse, urllib.request, json

direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def fetch(url, headers, timeout=15):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=headers)
        with direct.open(req, timeout=timeout) as r:
            return r.status, round(time.time()-t0,2), r.read(800000)
    except urllib.error.HTTPError as e:
        return e.code, round(time.time()-t0,2), e.read(30000)
    except Exception as e:
        return None, round(time.time()-t0,2), str(e)[:100].encode()

UA_BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
              "Accept-Language": "zh-CN,zh;q=0.9",
              "Cookie": "bkng_ses_activated=1; chosen_locale=zh-cn; bkng_prefs_consent_level=2; selected_currency=CNY"}

print("## 城市页 dest_id(curl+consent cookie):")
for name, url in [
    ("Interlaken", "https://www.booking.com/city/ch/interlaken.zh-cn.html"),
    ("Suzhou", "https://www.booking.com/city/cn/suzhou.zh-cn.html"),
    ("Queenstown", "https://www.booking.com/city/nz/queenstown.zh-cn.html"),
    ("Shanghai", "https://www.booking.com/city/cn/shanghai.zh-cn.html"),
]:
    st, dt, body = fetch(url, UA_BROWSER)
    dest = "?"
    if isinstance(body, bytes):
        html = body.decode("utf-8","replace")
        m = re.search(r"dest_id[=\"':]+\s*(-?\d+)", html) or re.search(r'"b_dest_id"\s*:\s*(-?\d+)', html)
        if m: dest = m.group(1)
        else:
            m2 = re.search(r'dest_id=(-?\d+)', html)
            if m2: dest = m2.group(1)
        ttl = re.search(r"<title[^>]*>(.*?)</title>", html, re.S|re.I)
        ttl = ttl.group(1).strip()[:50] if ttl else ""
    else:
        ttl = str(body)[:60]
    print(f"  [{st} {dt}s] {name}: dest_id={dest} title={ttl}")
    time.sleep(0.7)

print()
print("## 备选:booking destinations 内部接口(直连):")
for url in [
    "https://www.booking.com/destinations/search?query=Interlaken&locale=zh-cn",
    "https://www.booking.com/searchresults.zh-cn.html?ss=Interlaken&nflt=dest%3D-2552548",
]:
    st, dt, body = fetch(url, UA_BROWSER)
    info = body[:200].decode("utf-8","replace").replace("\n"," ") if isinstance(body,bytes) else str(body)
    m = re.search(r"dest_id[=\"':]+\s*(-?\d+)", info) if isinstance(body,bytes) else None
    print(f"  [{st} {dt}s] {url.split('?')[0][-40:]}: {info[:150]}")
    time.sleep(0.5)
