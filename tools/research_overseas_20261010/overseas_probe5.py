#!/usr/bin/env python3
"""TASK-19:Booking 外链拼法对比(直连 curl,看 SSR 结果)+ trip.com 境外城市页 SEO 拼法。"""
import re, time, urllib.parse, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def fetch(url, timeout=18):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=UA)
        with direct.open(req, timeout=timeout) as r:
            return r.status, round(time.time()-t0,2), r.read(400000)
    except urllib.error.HTTPError as e:
        return e.code, round(time.time()-t0,2), e.read(20000)
    except Exception as e:
        return None, round(time.time()-t0,2), str(e)[:150].encode()

print("## Booking searchresults 拼法对比(昆山柏轩酒店 / 上海皇冠假日)")
bk_cases = [
    ("现状: ss=酒店名,城市名", "https://www.booking.com/searchresults.zh-cn.html?ss=%E6%98%86%E5%B1%B1%E6%9F%8F%E8%BD%A9%E9%85%92%E5%BA%97,%E6%98%86%E5%B1%B1&group_adults=2&no_rooms=1"),
    ("A: ss=纯酒店名", "https://www.booking.com/searchresults.zh-cn.html?ss=%E6%98%86%E5%B1%B1%E6%9F%8F%E8%BD%A9%E9%85%92%E5%BA%97&group_adults=2&no_rooms=1"),
    ("B: ss=城市名(昆山)", "https://www.booking.com/searchresults.zh-cn.html?ss=%E6%98%86%E5%B1%B1&group_adults=2&no_rooms=1"),
    ("C: 英文酒店名", "https://www.booking.com/searchresults.zh-cn.html?ss=Kunshan%20Baixuan%20Hotel&group_adults=2&no_rooms=1"),
    ("D: 上海皇冠假日现状", "https://www.booking.com/searchresults.zh-cn.html?ss=%E4%B8%8A%E6%B5%B7%E5%86%B0%E9%9B%AA%E4%B8%96%E7%95%8C%E7%9A%87%E5%86%A0%E5%81%87%E6%97%A5%E9%85%92%E5%BA%97,%E4%B8%8A%E6%B5%B7&group_adults=2&no_rooms=1"),
    ("E: 上海皇冠假日纯名", "https://www.booking.com/searchresults.zh-cn.html?ss=%E4%B8%8A%E6%B5%B7%E5%86%B0%E9%9B%AA%E4%B8%96%E7%95%8C%E7%9A%87%E5%86%A0%E5%81%87%E6%97%A5%E9%85%92%E5%BA%97&group_adults=2&no_rooms=1"),
]
for name, url in bk_cases:
    st, dt, body = fetch(url)
    info = ""
    if isinstance(body, bytes):
        html = body.decode("utf-8", "replace")
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S|re.I)
        title = m.group(1).strip()[:80] if m else ""
        # 结果计数线索
        m2 = re.search(r'(\d[\d,]*)\s*(?:家|properties|results)', html)
        cnt = m2.group(0) if m2 else ""
        has_hotel = "柏轩" in html or "Baixuan" in html or "皇冠假日" in html or "Crowne Plaza" in html
        info = f"title={title} | cnt_hint={cnt} | 含目标酒店名={has_hotel}"
    else:
        info = str(body)[:100]
    print(f"  [{st} {dt}s] {name}\n      {info}")
    time.sleep(1)

print()
print("## trip.com 境外城市 SEO 页拼法(酒店 deep-link 候选)")
trip_cases = [
    ("SEO interlaken 酒店列表", "https://www.trip.com/hotels/interlaken-hotel"),
    ("SEO interlaken trailing slash", "https://www.trip.com/hotels/interlaken-hotel/"),
    ("SEO 苏黎世", "https://www.trip.com/hotels/zurich-hotel"),
    ("SEO 苏黎世 slash", "https://www.trip.com/hotels/zurich-hotel/"),
    ("list city=1258(猜)", "https://www.trip.com/hotels/list?city=1258"),
]
for name, url in trip_cases:
    st, dt, body = fetch(url)
    info = ""
    if isinstance(body, bytes):
        html = body.decode("utf-8", "replace")
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S|re.I)
        title = m.group(1).strip()[:80] if m else body[:80].decode("utf-8","replace").replace("\n"," ")
        info = f"title={title}"
    print(f"  [{st} {dt}s] {name}: {info}")
    time.sleep(0.5)
