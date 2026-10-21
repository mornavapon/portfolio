#!/usr/bin/env python3
"""ดึงราคาหุ้นและอัตราแลกเปลี่ยนจาก Yahoo Finance แล้วเขียนลง prices.json

อ่านรายชื่อจาก symbols.json:
  {"symbols": ["PTT.BK", "AAPL", "GC=F"], "currencies": ["USD", "EUR", "VND"]}

- ตัวไหนดึงไม่สำเร็จ จะคงราคาเดิมของตัวนั้นไว้ (ไม่ทำให้ทั้งไฟล์พัง)
- ถ้าราคาไม่เปลี่ยนจากรอบก่อน จะไม่เขียนไฟล์ (GitHub จะได้ไม่ commit ซ้ำ)
- อัตราแลกเปลี่ยนทั้งหมดเก็บเป็น "1 หน่วยสกุลนั้น = กี่บาท"
"""
import json
import sys
import time
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


def http_json(host, path):
    req = urllib.request.Request("https://" + host + path, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def fetch_chart(symbol):
    path = "/v8/finance/chart/" + urllib.parse.quote(symbol, safe="") + "?interval=1d&range=5d"
    last_err = None
    for host in HOSTS:
        for _ in range(2):
            try:
                return http_json(host, path)
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(1.5)
    raise RuntimeError(f"{symbol}: {last_err}")


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


def fx_to_thb(cur, usdthb):
    """คืนอัตรา 1 cur = กี่บาท"""
    if cur == "THB":
        return 1.0, None
    if cur == "USD":
        return usdthb, None
    try:
        d = fetch_chart(f"{cur}THB=X")["chart"]["result"][0]["meta"]
        return float(d["regularMarketPrice"]), int(d.get("regularMarketTime") or 0) or None
    except Exception as e:  # noqa: BLE001
        print("fx direct failed", cur, e, file=sys.stderr)
    # สำรอง: คำนวณผ่านดอลลาร์ (USD/THB หารด้วย USD/cur)
    d = fetch_chart(f"USD{cur}=X")["chart"]["result"][0]["meta"]
    usdcur = float(d["regularMarketPrice"])
    return usdthb / usdcur, int(d.get("regularMarketTime") or 0) or None


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

    quotes, failed = {}, []
    for s in symbols:
        try:
            quotes[s] = parse_quote(fetch_chart(s))
        except Exception as e:  # noqa: BLE001
            print("FAIL", s, e, file=sys.stderr)
            failed.append(s)
            if s in old_q:
                quotes[s] = old_q[s]
        time.sleep(0.4)

    for q in quotes.values():
        want_cur.add(q["currency"])
    want_cur.discard("THB")

    fx = {"THB": 1.0}
    usdthb = None
    try:
        usdthb, _ = fx_to_thb_usd()
    except Exception as e:  # noqa: BLE001
        print("FAIL USDTHB", e, file=sys.stderr)
        usdthb = old_fx.get("USD")
    if usdthb:
        fx["USD"] = round(usdthb, 6)
    for cur in sorted(want_cur - {"USD"}):
        try:
            if not usdthb:
                raise RuntimeError("no USDTHB")
            rate, _ = fx_to_thb(cur, usdthb)
            fx[cur] = round(rate, 8)
        except Exception as e:  # noqa: BLE001
            print("FAIL fx", cur, e, file=sys.stderr)
            if cur in old_fx:
                fx[cur] = old_fx[cur]

    if symbols and not any(s in quotes for s in symbols):
        print("ดึงราคาไม่ได้เลยสักตัว ไม่เขียนทับไฟล์เดิม", file=sys.stderr)
        sys.exit(1)

    if old.get("quotes") == quotes and old.get("fx") == fx:
        print("ราคาไม่เปลี่ยน ไม่เขียนไฟล์")
        return

    result = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fx": fx,
        "quotes": quotes,
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    ok = len(symbols) - len(failed)
    print(f"เขียน prices.json แล้ว: ราคา {ok}/{len(symbols)} ตัว, fx={sorted(fx)}")
    if failed:
        print("ตัวที่ดึงไม่ได้:", ", ".join(failed))


def fx_to_thb_usd():
    d = fetch_chart("USDTHB=X")["chart"]["result"][0]["meta"]
    return float(d["regularMarketPrice"]), int(d.get("regularMarketTime") or 0) or None


if __name__ == "__main__":
    main()
