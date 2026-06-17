#!/usr/bin/env python3
import json
import mimetypes
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data.json"
NAVER_REALTIME_URL = "https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"

history: dict[str, deque] = {}
MAX_HISTORY = 12

def load_config():
    with DATA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)

def fetch_quote(code):
    url = NAVER_REALTIME_URL.format(code=code)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read()
        text = raw.decode("euc-kr", errors="replace")
        payload = json.loads(text)
        if payload.get("resultCode") != "success":
            return {"code": code, "error": "네이버 금융 응답 실패"}
        areas = payload.get("result", {}).get("areas", [])
        datas = areas[0].get("datas", []) if areas else []
        item = datas[0] if datas else {}
        extra = item.get("nxtOverMarketPriceInfo") or {}
        return {
            "code": code,
            "name": item.get("nm"),
            "currentPrice": item.get("nv") or item.get("sv"),
            "previousClose": item.get("pcv"),
            "change": item.get("cv"),
            "changeRate": item.get("cr"),
            "session": item.get("ms"),
            "high": item.get("hv"),
            "low": item.get("lv"),
            "open": item.get("ov"),
            "updatedAt": extra.get("localTradedAt") or payload.get("time"),
        }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"code": code, "error": str(exc)}

def track_history(code, current_price):
    if code not in history:
        history[code] = deque(maxlen=MAX_HISTORY)
    if current_price is not None:
        history[code].append(current_price)

def calc_trend(quote):
    cp = quote.get("currentPrice")
    pc = quote.get("previousClose")
    hv = quote.get("high")
    lv = quote.get("low")
    op = quote.get("open")
    code = quote.get("code")

    if cp is None:
        return {"trend": "unknown", "signal": "hold", "rangePos": 50, "volatility": 0}

    track_history(code, cp)

    range_pos = 50
    if hv is not None and lv is not None and hv != lv:
        range_pos = round((cp - lv) / (hv - lv) * 100, 1)

    volatility = 0
    if pc and pc > 0 and hv and lv:
        volatility = round((hv - lv) / pc * 100, 2)

    gap = 0
    if pc and pc > 0 and op:
        gap = round((op - pc) / pc * 100, 2)

    change_from_open = 0
    if op and op > 0:
        change_from_open = round((cp - op) / op * 100, 2)

    hist = list(history.get(code, []))
    short_trend = "flat"
    if len(hist) >= 3:
        first = hist[0]
        last = hist[-1]
        if last > first * 1.001:
            short_trend = "up"
        elif last < first * 0.999:
            short_trend = "down"

    signal = "hold"
    reasons = []
    if cp and pc:
        if cp < pc * 0.97 and range_pos < 30 and short_trend != "down":
            signal = "buy"
            reasons.append("전일대비 -3% 이상 하락")
            reasons.append("일중 저가권")
        if cp > pc * 1.03 and range_pos > 70:
            signal = "sell"
            reasons.append("전일대비 +3% 이상 상승")
            reasons.append("일중 고가권")
        if cp < pc * 0.95 and range_pos < 15:
            signal = "strong_buy"
            reasons.append("전일대비 -5% 급락")
            reasons.append("일중 바닥권")
        if cp > pc * 1.05 and range_pos > 85:
            signal = "strong_sell"
            reasons.append("전일대비 +5% 급등")
            reasons.append("일중 고점권")

    return {
        "rangePos": range_pos,
        "volatility": volatility,
        "gap": gap,
        "changeFromOpen": change_from_open,
        "shortTrend": short_trend,
        "signal": signal,
        "signalReasons": reasons,
    }

def build_item(quote):
    cp = quote.get("currentPrice")
    pc = quote.get("previousClose")
    hv = quote.get("high")
    lv = quote.get("low")
    op = quote.get("open")
    return {
        "code": quote.get("code"),
        "name": quote.get("name"),
        "currentPrice": cp,
        "previousClose": pc,
        "change": quote.get("change"),
        "changeRate": quote.get("changeRate"),
        "session": quote.get("session"),
        "high": hv,
        "low": lv,
        "open": op,
        "updatedAt": quote.get("updatedAt"),
        "error": quote.get("error"),
        "trend": calc_trend(quote),
    }

def build_portfolio():
    config = load_config()
    all_codes = [h["code"] for h in config["holdings"]]
    all_codes += [w["code"] for w in config.get("watchlist", [])]
    quotes = {item["code"]: item for item in (fetch_quote(c) for c in all_codes)}

    holdings_rows = []
    for holding in config["holdings"]:
        quote = quotes.get(holding["code"], {"code": holding["code"], "error": "호가 데이터 없음"})
        quantity = int(holding["quantity"])
        avg_price = int(holding["avgPrice"])
        current_price = quote.get("currentPrice")
        if current_price is None:
            current_price = avg_price
        current_value = current_price * quantity
        cost = avg_price * quantity
        profit = current_value - cost
        profit_rate = (profit / cost * 100) if cost else 0
        holdings_rows.append({
            **build_item(quote),
            "quantity": quantity,
            "avgPrice": avg_price,
            "cost": cost,
            "currentValue": current_value,
            "profit": profit,
            "profitRate": profit_rate,
        })

    watchlist_rows = []
    for watch in config.get("watchlist", []):
        quote = quotes.get(watch["code"], {"code": watch["code"], "error": "호가 데이터 없음"})
        watchlist_rows.append(build_item(quote))

    total_cost = sum(row["cost"] for row in holdings_rows)
    total_current = sum(row["currentValue"] for row in holdings_rows)
    total_profit = total_current - total_cost
    total_profit_rate = (total_profit / total_cost * 100) if total_cost else 0

    return {
        "currency": config.get("currency", "KRW"),
        "refreshSeconds": config.get("refreshSeconds", 10),
        "generatedAt": int(time.time() * 1000),
        "summary": {
            "currentValue": total_current,
            "cost": total_cost,
            "profit": total_profit,
            "profitRate": total_profit_rate,
        },
        "holdings": holdings_rows,
        "watchlist": watchlist_rows,
    }

def fetch_news(name, code, limit=4):
    try:
        query = f"{name} 주식"
        encoded = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=ko&gl=KR&ceid=KR:ko"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        items = root.findall(".//item")
        articles = []
        for item in items[:limit]:
            title_el = item.find("title")
            link_el = item.find("link")
            source_el = item.find("source")
            desc_el = item.find("description")
            if title_el is not None and title_el.text:
                articles.append({
                    "title": re.sub(r"\s+", " ", title_el.text).strip(),
                    "url": link_el.text.strip() if link_el is not None and link_el.text else "#",
                    "source": source_el.text.strip() if source_el is not None and source_el.text else "",
                    "description": re.sub(r"\s+", " ", desc_el.text).strip()[:120] if desc_el is not None and desc_el.text else "",
                })
        return {"code": code, "name": name, "articles": articles}
    except Exception as exc:
        return {"code": code, "name": name, "articles": [], "error": str(exc)}

def build_news():
    config = load_config()
    items = []
    for h in config["holdings"]:
        items.append(fetch_news(h["name"], h["code"]))
    for w in config.get("watchlist", []):
        items.append(fetch_news(w["name"], w["code"]))
    return items

class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        if p == "/api/portfolio":
            self.send_json(build_portfolio())
            return
        if p == "/api/config":
            self.send_json(load_config())
            return
        if p == "/api/news":
            self.send_json(build_news())
            return
        path = BASE_DIR / ("index.html" if p == "/" else p.lstrip("/"))
        if not path.exists() or not path.is_file():
            self.send_error(404, "Not Found")
            return
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as f:
            self.wfile.write(f.read())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def send_json(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

def main():
    server = ThreadingHTTPServer(("127.0.0.1", 8090), Handler)
    print("Portfolio dashboard: http://127.0.0.1:8090")
    server.serve_forever()

if __name__ == "__main__":
    main()
