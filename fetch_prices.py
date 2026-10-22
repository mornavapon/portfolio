#!/usr/bin/env python3
"""ดึงราคาหุ้นและอัตราแลกเปลี่ยนจาก Yahoo Finance แล้วเขียนลง prices.json

อ่านรายชื่อจาก symbols.json:
  {"symbols": ["PTT.BK", "AAPL", "GC=F"], "currencies": ["USD", "EUR", "VND"]}

- Yahoo มักตอบ 429 (Too Many Requests) ให้เครื่องของ GitHub จึงใช้ curl_cffi (เลียนแบบ Chrome)
  ถ้าติดตั้งไม่ได้จะใช้ urllib ธรรมดาแทน และถ้าโดน 429 จะรอแล้วลองใหม่
- ถ้าโดนบล็อกติดๆ กัน 3 ตัว จะหยุดรอบนี้ทันที (ราคาเดิมยังอยู่ รอบหน้าลองใหม่เอง)
- ตัวไหนดึงไม่สำเร็จ จะคงราคาเดิมของตัวนั้นไว้ ไม่ทำให้ทั้งไฟล์พัง
- ถ้าราคาไม่เปลี่ยนจากรอบก่อน จะไม่เขียนไฟล์ (GitHub จะได้ไม่ commit ซ้ำ)
- อัตราแลกเปลี่ยนทั้งหมดเก็บเป็น "1 หน่วยสกุลนั้น = กี่บาท"
"""
import json
import random
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
# สกุลเงินที่ Yahoo รายงานเป็นหน่วยย่อย (เช่น เพนนี) -> (สกุลหลัก, ตัวหาร)
MINOR_UNITS = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0),
               "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}
MAX_ATTEMPTS = 3          # ลองต่อตัวกี่ครั้ง
BACKOFF = [6, 15]         # วินาทีที่รอก่อนลองใหม่ (ครั้งที่ 1, 2)
STOP_AFTER_BLOCKED = 3    # โดนบล็อกติดกันกี่ตัวแล้วหยุดรอบนี้


class HttpErr(Exception):
    def __init__(self, code, msg=""):
        super().__init__(f"HTTP {code} {msg}".strip())
        self.code = code


class Blocked(Exception):
    """Yahoo ปฏิเสธ (429/อื่นๆ) ต่อเนื่อง ไม่ใช่ความผิดของชื่อหุ้น"""


_session = None  # None = ยังไม่ลอง, False = ใช้ curl_cffi ไม่ได้


def get_session():
    """คืน session ของ curl_cffi (เลียนแบบ Chrome) หรือ None ถ้าใช้ไม่ได้"""
    global _session
    if _session is False:
        return None
    if _session is not None:
        return _session
    try:
        from curl_cffi import requests as cr
    except ImportError:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "curl_cffi"],
                           check=True, timeout=240)
            from curl_cffi import requests as cr
        except Exception as e:  # noqa: BLE001
            print("ใช้ curl_cffi ไม่ได้ จะใช้ urllib แทน:", e, file=sys.stderr)
            _session = False
            return None
    try:
        s = cr.Session(impersonate="chrome")
        try:
            s.get("https://fc.yahoo.com", timeout=15)  # ขอ cookie เหมือนเบราว์เซอร์ (ตอบ 404 ได้ ไม่เป็นไร)
        except Exception:  # noqa: BLE001
            pass
        _session = s
        return s
    except Exception as e:  # noqa: BLE001
        print("เริ่ม curl_cffi ไม่ได้ จะใช้ urllib แทน:", e, file=sys.stderr)
        _session = False
        return None


def http_json(host, path):
    url = "https://" + host + path
    s = get_session()
    if s is not None:
        r = s.get(url, timeout=25)
        if r.status_code != 200:
            raise HttpErr(r.status_code)
        return r.json()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise HttpErr(e.code, e.reason) from None


def fetch_chart(symbol):
    path = "/v8/finance/chart/" + urllib.parse.quote(symbol, safe="") + "?interval=1d&range=5d"
    last = None
    for attempt in range(MAX_ATTEMPTS):
        host = HOSTS[attempt % len(HOSTS)]
        try:
            return http_json(host, path)
        except HttpErr as e:
            last = e
            if e.code == 404:
                raise RuntimeError(f"{symbol}: Yahoo ไม่รู้จักชื่อนี้ (404) ตรวจการสะกดหรือ .BK/.PA") from None
        except Exception as e:  # noqa: BLE001
            last = e
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)] + random.uniform(0, 2))
    raise Blocked(f"{symbol}: {last}")


def parse_quote(data):
    """คืน dict ราคาหนึ่งตัว จากผลลัพธ์ chart API"""
    res = data["chart"]["result"][0]
    meta = res["meta"]
    price = meta.get("regularMarketPrice")
    if price is None:
        raise ValueError("no regularMarketPrice")
    closes = []
    try:
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
    except (KeyError, IndexError, TypeError):
        pass
    # ราคาปิดก่อนหน้าของรอบการซื้อขายล่าสุด = แท่งรองสุดท้าย
    prev = closes[-2] if len(closes) >= 2 else None
    cur = meta.get("currency") or "USD"
    if cur in MINOR_UNITS:
        cur, div = MINOR_UNITS[cur]
        price = price / div
        prev = prev / div if prev is not None else None
    q = {"price": round(float(price), 6), "currency": cur}
    if prev is not None:
        q["prevClose"] = round(float(prev), 6)
    t = meta.get("regularMarketTime")
    if t:
        q["t"] = int(t)
    return q


def fx_usdthb():
    d = fetch_chart("USDTHB=X")["chart"]["result"][0]["meta"]
    return float(d["regularMarketPrice"])


def fx_to_thb(cur, usdthb):
    """คืนอัตรา 1 cur = กี่บาท"""
    if cur == "THB":
        return 1.0
    if cur == "USD":
        return usdthb
    try:
        d = fetch_chart(f"{cur}THB=X")["chart"]["result"][0]["meta"]
        return float(d["regularMarketPrice"])
    except Exception as e:  # noqa: BLE001
        print("fx direct failed", cur, e, file=sys.stderr)
    # สำรอง: คำนวณผ่านดอลลาร์ (USD/THB หารด้วย USD/cur)
    d = fetch_chart(f"USD{cur}=X")["chart"]["result"][0]["meta"]
    return usdthb / float(d["regularMarketPrice"])


def pause():
    time.sleep(random.uniform(0.7, 1.4))


def main():
    cfg = json.loads((ROOT / "symbols.json").read_text(encoding="utf-8"))
    symbols = []
    for s in cfg.get("symbols", []):
        s = str(s).strip()
        if s and s not in symbols:
            symbols.append(s)
    want_cur = {"USD"} | {str(c).strip().upper() for c in cfg.get("currencies", []) if str(c).strip()}

    out_path = ROOT / "prices.json"
    old = {}
    if out_path.exists():
        try:
            old = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            old = {}
    old_q = old.get("quotes", {})
    old_fx = old.get("fx", {})

    quotes, failed, fresh = {}, [], set()
    blocked_run = 0
    stopped = False
    for s in symbols:
        if stopped:
            failed.append(s)
            if s in old_q:
                quotes[s] = old_q[s]
            continue
        try:
            quotes[s] = parse_quote(fetch_chart(s))
            fresh.add(s)
            blocked_run = 0
        except Blocked as e:
            print("BLOCKED", e, file=sys.stderr)
            failed.append(s)
            if s in old_q:
                quotes[s] = old_q[s]
            blocked_run += 1
            if blocked_run >= STOP_AFTER_BLOCKED:
                print(f"โดน Yahoo ปฏิเสธติดกัน {blocked_run} ตัว หยุดรอบนี้ (ราคาเดิมยังอยู่ รอบหน้าลองใหม่)",
                      file=sys.stderr)
                stopped = True
        except Exception as e:  # noqa: BLE001
            print("FAIL", s, e, file=sys.stderr)
            failed.append(s)
            if s in old_q:
                quotes[s] = old_q[s]
        pause()

    if symbols and not fresh:
        print("ดึงราคาไม่ได้เลยสักตัวในรอบนี้ ไม่เขียนทับไฟล์เดิม", file=sys.stderr)
        # รอบแรกที่ยังไม่เคยได้ราคาเลย ให้ขึ้นแดงเพื่อให้รู้ว่ามีปัญหา
        # แต่ถ้าเคยได้แล้ว ถือว่าชั่วคราว ไม่ต้องแจ้งเตือนซ้ำทุก 30 นาที
        sys.exit(0 if old_q else 1)

    for q in quotes.values():
        want_cur.add(q["currency"])
    want_cur.discard("THB")

    fx = {"THB": 1.0}
    usdthb = None
    if not stopped:
        try:
            usdthb = fx_usdthb()
        except Exception as e:  # noqa: BLE001
            print("FAIL USDTHB", e, file=sys.stderr)
    if not usdthb:
        usdthb = old_fx.get("USD")
    if usdthb:
        fx["USD"] = round(usdthb, 6)
    for cur in sorted(want_cur - {"USD"}):
        try:
            if stopped or not usdthb:
                raise RuntimeError("ข้ามรอบนี้")
            fx[cur] = round(fx_to_thb(cur, usdthb), 8)
            pause()
        except Exception as e:  # noqa: BLE001
            print("FAIL fx", cur, e, file=sys.stderr)
            if cur in old_fx:
                fx[cur] = old_fx[cur]

    if old.get("quotes") == quotes and old.get("fx") == fx:
        print("ราคาไม่เปลี่ยน ไม่เขียนไฟล์")
        return

    result = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fx": fx,
        "quotes": quotes,
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"เขียน prices.json แล้ว: ได้ราคาใหม่ {len(fresh)}/{len(symbols)} ตัว, fx={sorted(fx)}")
    if failed:
        print("ตัวที่ดึงไม่ได้รอบนี้:", ", ".join(failed))


if __name__ == "__main__":
    main()
