import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path

from flask import Flask, Response, request, send_from_directory

app = Flask(__name__, static_folder=None)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = BASE_DIR / "data.json"
NAVER_REALTIME_URL = "https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"

history: dict[str, deque] = {}
MAX_HISTORY = 12

ZAI_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
ZAI_KEY = "136d90754ebd453999f4a4cc4547b638.LUXSKaxDozJgFHLQ"

ai_cache: dict[str, dict] = {}
AI_CACHE_TTL = 300

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
        
        nv = item.get("nv") or item.get("sv")
        pcv = item.get("pcv")
        cv = item.get("cv")
        cr = item.get("cr")
        
        if nv and pcv:
            calculated_change = nv - pcv
            if calculated_change != 0:
                cv = calculated_change
                cr = round(calculated_change / pcv * 100, 2) if pcv else cr
        
        return {
            "code": code,
            "name": item.get("nm"),
            "currentPrice": nv,
            "previousClose": pcv,
            "change": cv,
            "changeRate": cr,
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
    return {
        "code": quote.get("code"),
        "name": quote.get("name"),
        "currentPrice": quote.get("currentPrice"),
        "previousClose": quote.get("previousClose"),
        "change": quote.get("change"),
        "changeRate": quote.get("changeRate"),
        "session": quote.get("session"),
        "high": quote.get("high"),
        "low": quote.get("low"),
        "open": quote.get("open"),
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

def signal_from_zai(name, code, quote, articles):
    titles = "\n".join(a.get("title", "") for a in articles[:6])
    cp = quote.get("currentPrice")
    pc = quote.get("previousClose")
    chg = quote.get("change")
    chg_rate = quote.get("changeRate")
    
    if cp and pc:
        if cp > pc:
            trend_desc = f"상승 중 (+{chg}원, +{chg_rate}%)"
        elif cp < pc:
            trend_desc = f"하락 중 ({chg}원, {chg_rate}%)"
        else:
            trend_desc = "보합 (변동 없음)"
    else:
        trend_desc = f"{chg}원 ({chg_rate}%)"
    
    prompt = (
        f"주식 분석 요청:\n"
        f"종목: {name} ({code})\n"
        f"현재가: {cp}원\n"
        f"전일종가: {pc}원\n"
        f"현재 추세: {trend_desc}\n"
        f"고가: {quote.get('high')}원 / 저가: {quote.get('low')}원\n"
        f"\n최근 뉴스:\n{titles}\n"
        f"\n중요: 현재가가 전일종가보다 높으면 상승, 낮으면 하락입니다.\n"
        f"JSON만 응답하세요:\n"
        f'{{"signal":"strong_buy|buy|hold|sell|strong_sell","confidence":0-100,"reasons":["이유1","이유2","이유3"],"newsSentiment":"한줄 요약"}}'
    )
    payload = {
        "model": "glm-4.5",
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "disabled"},
        "temperature": 0.3,
        "max_tokens": 600,
    }
    headers = {
        "Authorization": f"Bearer {ZAI_KEY}",
        "Content-Type": "application/json",
        "Accept-Language": "en-US,en",
    }
    req = urllib.request.Request(
        ZAI_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
        result = json.loads(raw)
        content = result["choices"][0]["message"]["content"]
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            return {
                "signal": parsed.get("signal", "hold"),
                "confidence": parsed.get("confidence", 0),
                "reasons": parsed.get("reasons", []),
                "newsSentiment": parsed.get("newsSentiment", ""),
                "_source": "zai",
            }
    except Exception as exc:
        return {"error": str(exc)}
    return {"error": "JSON 파싱 실패"}

POSITIVE_KW = ["호조", "상승", "증가", "성장", "호실적", "수혜", "기대", "긍정적", "강세", "신고가", "목표가", "매수", "반등", "턴어라운드", "개선", "흑자", "최대", "돌파", "회복", "확대", "낙관"]
NEGATIVE_KW = ["하락", "감소", "악화", "부진", "우려", "하회", "적자", "약세", "신저가", "매도", "추락", "경고", "위기", "침체", "불안", "축소", "지연", "악재", "충격", "반토막"]

def keyword_signal(name, code, quote, articles):
    cp = quote.get("currentPrice")
    pc = quote.get("previousClose")
    text = " ".join(a.get("title", "") + " " + a.get("description", "") for a in articles)
    pos = sum(1 for kw in POSITIVE_KW if kw in text)
    neg = sum(1 for kw in NEGATIVE_KW if kw in text)
    signal = "hold"
    reasons = []
    net = pos - neg
    if net >= 2:
        reasons.append(f"뉴스 긍정 ({net})")
        signal = "buy"
    elif net <= -2:
        reasons.append(f"뉴스 부정 ({net})")
        signal = "sell"
    else:
        reasons.append(f"뉴스 중립")
    sentiment = "긍정적" if net >= 2 else ("부정적" if net <= -2 else "중립")
    return {
        "signal": signal,
        "confidence": 50,
        "reasons": reasons + [f"키워드 긍정 {pos} / 부정 {neg}"],
        "newsSentiment": f"키워드: {sentiment}",
        "_source": "keyword",
    }

def validate_signal(signal, quote, news_sentiment="", reasons=None):
    cp = quote.get("currentPrice")
    pc = quote.get("previousClose")
    if not cp or not pc:
        return signal
    chg = (cp - pc) / pc * 100
    
    reasons_text = " ".join(reasons) if reasons else ""
    
    if chg > 0.5 and "하락" in reasons_text:
        reasons_text = reasons_text.replace("하락", "상승")
    elif chg < -0.5 and "상승" in reasons_text:
        reasons_text = reasons_text.replace("상승", "하락")
    
    sentiment_lower = news_sentiment.lower() if news_sentiment else ""
    all_text = f"{sentiment_lower} {reasons_text}".lower()
    
    positive_keywords = ["긍정", "positive", "매수", "상승", "기대", "호재", "강세", "우호", "성장", "호조", "돌파", "신고가", "순매수", "목표가"]
    negative_keywords = ["부정", "negative", "매도", "하락", "우려", "악재", "약세", "적자", "침체", "경고", "위기"]
    
    is_positive = any(kw in all_text for kw in positive_keywords)
    is_negative = any(kw in all_text for kw in negative_keywords)
    
    if signal == "hold":
        if is_positive and not is_negative:
            if chg >= 0:
                return "buy"
            else:
                return "hold"
        elif is_negative and not is_positive:
            if chg <= 0:
                return "sell"
            else:
                return "hold"
    
    valid_signals = {
        "strong_buy": lambda x: x < -5,
        "buy": lambda x: x < -3,
        "hold": lambda x: True,
        "sell": lambda x: x > 3,
        "strong_sell": lambda x: x > 5,
    }
    if signal in valid_signals and not valid_signals[signal](chg):
        if chg > 5: return "strong_sell"
        if chg > 3: return "sell"
        if chg < -5: return "strong_buy"
        if chg < -3: return "buy"
        return "hold"
    return signal

def handle_analyze_signal(code):
    now = time.time()
    cached = ai_cache.get(code)
    if cached and now - cached.get("_ts", 0) < AI_CACHE_TTL:
        return {k: v for k, v in cached.items() if k != "_ts"}
    config = load_config()
    item = None
    for h in config["holdings"]:
        if h["code"] == code:
            item = h
            break
    if not item:
        for w in config.get("watchlist", []):
            if w["code"] == code:
                item = w
                break
    if not item:
        return {"error": "종목을 찾을 수 없습니다"}
    quote = fetch_quote(code)
    news = fetch_news(item["name"], code, limit=6)
    articles = news.get("articles", [])
    result = signal_from_zai(item["name"], code, quote, articles)
    if "error" in result:
        result = keyword_signal(item["name"], code, quote, articles)
        result["_fallback"] = True
        news_sentiment = result.get("newsSentiment", "")
        reasons = result.get("reasons", [])
        result["signal"] = validate_signal(result.get("signal", "hold"), quote, news_sentiment, reasons)
    else:
        news_sentiment = result.get("newsSentiment", "")
        reasons = result.get("reasons", [])
        result["signal"] = validate_signal(result.get("signal", "hold"), quote, news_sentiment, reasons)
    result["news"] = articles
    result["stockName"] = item["name"]
    result["stockCode"] = code
    result["currentPrice"] = quote.get("currentPrice")
    result["previousClose"] = quote.get("previousClose")
    result["change"] = quote.get("change")
    result["changeRate"] = quote.get("changeRate")
    result["high"] = quote.get("high")
    result["low"] = quote.get("low")
    result["_ts"] = now
    ai_cache[code] = result
    return {k: v for k, v in result.items() if k != "_ts"}

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}

@app.route("/")
def index():
    return serve_static("index.html")

@app.route("/<path:filename>")
def static_files(filename):
    return serve_static(filename)

def serve_static(filename):
    filepath = BASE_DIR / filename
    if not filepath.exists() or not filepath.is_file():
        return Response("Not Found", status=404)
    ext = filepath.suffix
    content_type = STATIC_TYPES.get(ext, "application/octet-stream")
    with filepath.open("rb") as f:
        return Response(f.read(), mimetype=content_type, headers={"Cache-Control": "no-store"})

@app.route("/api/portfolio")
def api_portfolio():
    return Response(
        json.dumps(build_portfolio(), ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )

@app.route("/api/config")
def api_config():
    return Response(
        json.dumps(load_config(), ensure_ascii=False),
        mimetype="application/json",
    )

@app.route("/api/news")
def api_news():
    return Response(
        json.dumps(build_news(), ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )

@app.route("/api/analyze-signal")
def api_analyze_signal():
    code = request.args.get("code")
    if not code:
        return Response(json.dumps({"error": "code parameter required"}), status=400, mimetype="application/json")
    return Response(
        json.dumps(handle_analyze_signal(code), ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )
