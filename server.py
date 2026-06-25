#!/usr/bin/env python3
import json
import mimetypes
import os
import re
import time
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data.json"
CHAT_HISTORY_FILE = BASE_DIR / "chat_history.json"
NAVER_REALTIME_URL = "https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"

history: dict[str, deque] = {}
MAX_HISTORY = 12

ZAI_URL = "https://api.z.ai/api/coding/paas/v4"
ZAI_KEY = "136d90754ebd999f4a4cc4547b638.LUXSKaxDozJgFHLQ"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "").strip()
OPENROUTER_MODELS = ["nex-agi/nex-n2-pro:free", "openai/gpt-oss-120b:free"]

ai_cache: dict[str, dict] = {}
AI_CACHE_TTL = 300

def load_config():
    with DATA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)

def fetch_previous_close(code):
    """네이버 증권 메인 페이지에서 정확한 전일 종가를 가져온다.
    실시간 API의 pcv는 장 마감 후 오늘 종가로 덮어씌워지는 버그가 있어 별도 스크랩이 필요."""
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        match = re.search(r"전일가\s*([\d,]+)", html)
        if match:
            return int(match.group(1).replace(",", ""))
    except Exception:
        pass
    return None

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

        # 실시간 API pcv는 장 마감 후 오늘 종가로 덮어씌워지므로
        # 웹페이지에서 정확한 전일 종가를 가져와 사용
        real_prev_close = fetch_previous_close(code)
        if real_prev_close:
            pcv = real_prev_close

        # 프리마켓/애프터마켓: overPrice가 실시간가, nv는 stale
        over_price = None
        session_type = extra.get("tradingSessionType")
        if session_type in ("PRE_MARKET", "AFTER_MARKET") and extra.get("overPrice"):
            over_price = int(extra["overPrice"].replace(",", ""))

        # 실제 현재가 결정: overPrice > nv 순서
        current = over_price or nv

        if current and pcv:
            cv = current - pcv
            cr = round(cv / pcv * 100, 2)

        return {
            "code": code,
            "name": item.get("nm"),
            "currentPrice": current,
            "previousClose": pcv,
            "change": cv,
            "changeRate": cr,
            "session": item.get("ms"),
            "high": item.get("hv"),
            "low": item.get("lv"),
            "open": item.get("ov"),
            "afterMarketPrice": over_price,
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
        "trades": config.get("trades", []),
    }

def fetch_news(name, code, limit=4):
    try:
        query = f"{name} when:1d"
        encoded = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=ko&gl=KR&ceid=KR:ko"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        items = root.findall(".//item")
        articles = []
        for item in items[:limit * 3]:
            title_el = item.find("title")
            link_el = item.find("link")
            source_el = item.find("source")
            desc_el = item.find("description")
            pub_el = item.find("pubDate")
            if title_el is not None and title_el.text:
                pub_str = pub_el.text.strip() if pub_el is not None and pub_el.text else ""
                pub_ts = 0
                if pub_str:
                    try:
                        from email.utils import parsedate_to_datetime
                        pub_ts = parsedate_to_datetime(pub_str).timestamp()
                    except Exception:
                        pass
                articles.append({
                    "title": re.sub(r"\s+", " ", title_el.text).strip(),
                    "url": link_el.text.strip() if link_el is not None and link_el.text else "#",
                    "source": source_el.text.strip() if source_el is not None and source_el.text else "",
                    "description": re.sub(r"\s+", " ", desc_el.text).strip()[:120] if desc_el is not None and desc_el.text else "",
                    "pubDate": pub_str,
                    "_ts": pub_ts,
                })
        articles.sort(key=lambda x: x.get("_ts", 0), reverse=True)
        for a in articles:
            a.pop("_ts", None)
        return {"code": code, "name": name, "articles": articles[:limit]}
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

def fetch_chart_data(code, days=120):
    try:
        from datetime import datetime, timedelta
        end = datetime.now()
        start = end - timedelta(days=days)
        s = start.strftime("%Y%m%d")
        e = end.strftime("%Y%m%d")
        url = f"https://api.finance.naver.com/siseJson.naver?symbol={code}&requestType=1&startTime={s}&endTime={e}&timeframe=day"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": f"https://finance.naver.com/item/main.naver?code={code}"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        import ast
        parsed = ast.literal_eval(raw.strip())
        candles = []
        for row in parsed[1:]:
            if len(row) >= 5 and row[0] != "날짜":
                date_str = str(row[0])
                candles.append({
                    "time": date_str[:4] + "-" + date_str[4:6] + "-" + date_str[6:8],
                    "open": row[1],
                    "high": row[2],
                    "low": row[3],
                    "close": row[4],
                    "volume": row[5] if len(row) > 5 else 0,
                })
        return {"code": code, "candles": candles}
    except Exception as exc:
        return {"code": code, "candles": [], "error": str(exc)}

def signal_from_zai(name, code, quote, articles):
    titles = "\n".join(a.get("title", "") for a in articles[:6])
    prompt = (
        f"주식 분석 요청:\n"
        f"종목: {name} ({code})\n"
        f"현재가: {quote.get('currentPrice')}원\n"
        f"전일종가: {quote.get('previousClose')}원\n"
        f"변동: {quote.get('change')}원 ({quote.get('changeRate')}%)\n"
        f"고가: {quote.get('high')}원 / 저가: {quote.get('low')}원\n"
        f"\n최근 뉴스:\n{titles}\n"
        f"\nJSON만 응답하세요:\n"
        f'{{"signal":"strong_buy|buy|hold|sell|strong_sell","confidence":0-100,"reasons":["이유1","이유2","이유3"],"newsSentiment":"한줄 요약"}}'
    )
    payload = {
        "model": "glm-5",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 600,
    }
    headers = {
        "Authorization": f"Bearer {ZAI_KEY}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        ZAI_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
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
    except urllib.error.HTTPError as exc:
        if exc.code in (429, 401, 403):
            return {"error": "rate_limited"}
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": str(exc)}
    return {"error": "JSON 파싱 실패"}

def signal_from_openrouter(name, code, quote, articles, model=None):
    if not OPENROUTER_KEY:
        return {"error": "OPENROUTER_KEY not set"}
    if not model:
        model = OPENROUTER_MODELS[0]
    titles = "\n".join(a.get("title", "") for a in articles[:6])
    prompt = (
        f"주식 분석 요청:\n"
        f"종목: {name} ({code})\n"
        f"현재가: {quote.get('currentPrice')}원\n"
        f"전일종가: {quote.get('previousClose')}원\n"
        f"변동: {quote.get('change')}원 ({quote.get('changeRate')}%)\n"
        f"고가: {quote.get('high')}원 / 저가: {quote.get('low')}원\n"
        f"\n최근 뉴스:\n{titles}\n"
        f"\nJSON만 응답하세요:\n"
        f'{{"signal":"strong_buy|buy|hold|sell|strong_sell","confidence":0-100,"reasons":["이유1","이유2","이유3"],"newsSentiment":"한줄 요약"}}'
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 600,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://stock-dashboard.vercel.app",
        "X-Title": "Stock Dashboard",
    }
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
        result = json.loads(raw)
        content = result["choices"][0]["message"]["content"]
        if not content:
            return {"error": "empty content"}
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            return {
                "signal": parsed.get("signal", "hold"),
                "confidence": parsed.get("confidence", 0),
                "reasons": parsed.get("reasons", []),
                "newsSentiment": parsed.get("newsSentiment", ""),
                "_source": "openrouter",
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
    if cp and pc:
        chg = (cp - pc) / pc * 100
        if chg < -3: signal = "strong_buy"
        elif chg < -1: signal = "buy"
        elif chg > 3: signal = "strong_sell"
        elif chg > 1: signal = "sell"
    net = pos - neg
    if net >= 2:
        reasons.append(f"뉴스 긍정 ({net})")
        if signal == "hold": signal = "buy"
    elif net <= -2:
        reasons.append(f"뉴스 부정 ({net})")
        if signal == "hold": signal = "sell"
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
    if result.get("error") == "rate_limited":
        for or_model in OPENROUTER_MODELS:
            or_result = signal_from_openrouter(item["name"], code, quote, articles, model=or_model)
            if "error" not in or_result:
                result = or_result
                break
        else:
            result = keyword_signal(item["name"], code, quote, articles)
            result["_fallback"] = True
    elif "error" in result:
        for or_model in OPENROUTER_MODELS:
            or_result = signal_from_openrouter(item["name"], code, quote, articles, model=or_model)
            if "error" not in or_result:
                result = or_result
                break
        else:
            result = keyword_signal(item["name"], code, quote, articles)
            result["_fallback"] = True
    result["_ts"] = now
    ai_cache[code] = result
    return {k: v for k, v in result.items() if k != "_ts"}

# ──────────────────────────────────────────
# Stock Manager AI Chat
# ──────────────────────────────────────────

CHAT_MSG_LIMIT = 20

_chat_history_cache = None

def load_chat_history():
    global _chat_history_cache
    if _chat_history_cache is not None:
        return _chat_history_cache
    try:
        with CHAT_HISTORY_FILE.open("r", encoding="utf-8") as f:
            _chat_history_cache = json.load(f)
            return _chat_history_cache
    except (FileNotFoundError, json.JSONDecodeError):
        _chat_history_cache = []
        return _chat_history_cache

def save_chat_history(history):
    global _chat_history_cache
    _chat_history_cache = history
    try:
        with CHAT_HISTORY_FILE.open("w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def build_chat_context(portfolio, news):
    lines = []
    summary = portfolio["summary"]
    lines.append("📊 포트폴리오 현황")
    lines.append(f"• 총 평가금: {summary['currentValue']:,.0f}원")
    lines.append(f"• 총 투자원금: {summary['cost']:,.0f}원")
    lines.append(f"• 총 손익: {summary['profit']:+,.0f}원 ({summary['profitRate']:+.2f}%)")
    lines.append("")
    if portfolio.get("holdings"):
        lines.append("📦 보유종목")
        for h in portfolio["holdings"]:
            err = h.get("error")
            if err:
                lines.append(f"• {h['name']}({h['code']}): 데이터 없음")
            else:
                profit_emoji = "🔴" if h["profitRate"] > 0 else ("🔵" if h["profitRate"] < 0 else "⚪")
                lines.append(f"• {h['name']}({h['code']}): {h['quantity']}주")
                lines.append(f"  평균 {h['avgPrice']:,.0f}원 → 현재 {h['currentPrice']:,.0f}원")
                lines.append(f"  {profit_emoji} 수익 {h['profit']:+,.0f}원 ({h['profitRate']:+.2f}%) | 시그널: {h['trend']['signal']}")
        lines.append("")
    if portfolio.get("watchlist"):
        lines.append("👀 관심종목")
        for w in portfolio["watchlist"]:
            err = w.get("error")
            if err:
                lines.append(f"• {w['name']}({w['code']}): 데이터 없음")
            else:
                emoji = "📈" if w["changeRate"] > 0 else ("📉" if w["changeRate"] < 0 else "📊")
                lines.append(f"• {emoji} {w['name']}({w['code']}): 현재 {w['currentPrice']:,.0f}원 ({w['changeRate']:+.2f}%) | 추세: {w['trend']['shortTrend']}")
        lines.append("")
    if portfolio.get("trades"):
        lines.append("📋 오늘의 거래내역")
        for t in portfolio["trades"]:
            emoji = "🟢" if t["type"] == "buy" else "🔴"
            action = "매도" if t["type"] == "sell" else "매수"
            lines.append(f"  {emoji} {t['name']} {t['quantity']}주 {action} @ {t['price']:,.0f}원 ({t.get('note','')})")
        lines.append("")
    if news:
        lines.append("📰 최근 뉴스")
        for n in news:
            if n.get("articles"):
                for article in n["articles"][:2]:
                    title = article["title"][:80]
                    lines.append(f"• [{n['name']}] {title}")
        lines.append("")
    return "\n".join(lines)

def chat_from_zai(messages):
    payload = {
        "model": "glm-5",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1000,
    }
    headers = {
        "Authorization": f"Bearer {ZAI_KEY}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        ZAI_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        result = json.loads(raw)
        content = result["choices"][0]["message"]["content"]
        return {"reply": content.strip(), "_source": "zai"}
    except urllib.error.HTTPError as exc:
        if exc.code in (429, 401, 403):
            return {"error": "rate_limited"}
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": str(exc)}

def chat_from_openrouter(messages, model=None):
    if not OPENROUTER_KEY:
        return {"error": "OPENROUTER_KEY not set"}
    if not model:
        model = OPENROUTER_MODELS[0]
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1000,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://stock-dashboard.vercel.app",
        "X-Title": "Stock Dashboard",
    }
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        result = json.loads(raw)
        content = result["choices"][0]["message"]["content"]
        if not content:
            return {"error": "empty content"}
        return {"reply": content.strip(), "_source": "openrouter"}
    except Exception as exc:
        return {"error": str(exc)}

def call_llm(messages):
    result = chat_from_zai(messages)
    if "error" not in result:
        return result
    if OPENROUTER_KEY:
        for m in OPENROUTER_MODELS:
            result = chat_from_openrouter(messages, model=m)
            if "error" not in result:
                return result
    return {"reply": "죄송합니다. 현재 AI 서비스에 일시적인 문제가 있습니다. 잠시 후 다시 시도해 주세요.", "_source": "fallback"}

MCP_SEARCH_URL = "https://api.z.ai/api/mcp/web_search_prime/mcp"

def is_irrelevant_result(url, text):
    """Filter out obviously irrelevant results like generic Wikipedia."""
    url_lower = url.lower()
    if "wikipedia.org" in url_lower:
        if not any(kw in url_lower + text.lower() for kw in ["stock", "주식", "kospi", "kosdaq", "vi", "volatility",
                                                              "finance", "market", "invest", "trading", "배당",
                                                              "상장", "공매도", "액면", "변동", "호재", "악재"]):
            return True
    return False

def search_web_ddg(query):
    try:
        from duckduckgo_search import DDGS
        queries = [
            query[:180],
            f"주식 {query[:150]}",
        ]
        seen_urls = set()
        results = []
        for sq in queries:
            try:
                with DDGS() as ddgs:
                    raw = list(ddgs.text(sq, max_results=5))
                    for r in raw:
                        body = r.get("body", "")
                        url = r.get("href", "")
                        if not body or url in seen_urls:
                            continue
                        if is_irrelevant_result(url, body):
                            continue
                        seen_urls.add(url)
                        results.append({"text": body[:2000], "url": url})
            except Exception:
                continue
        return results[:8]
    except Exception as e:
        print(f"[search_web] DuckDuckGo error: {e}")
        return []

def search_web(query):
    """Z.AI MCP + DuckDuckGo — topic-agnostic search merger"""
    if not query or not query.strip():
        return []
    all_results = []
    seen_urls = set()

    def add_results(results):
        for r in results:
            url = r.get("url", "")
            if url and url not in seen_urls and not is_irrelevant_result(url, r.get("text", "")):
                seen_urls.add(url)
                all_results.append(r)

    # 1) Z.AI MCP
    headers = {
        "Authorization": f"Bearer {ZAI_KEY}",
        "Content-Type": "application/json",
    }
    batch = [
        {
            "jsonrpc": "2.0", "id": "1", "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "stock-dashboard", "version": "1.0.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0", "id": "2", "method": "tools/call",
            "params": {
                "name": "webSearchPrime",
                "arguments": {"search_query": query[:200]},
            },
        },
    ]
    try:
        req = urllib.request.Request(
            MCP_SEARCH_URL,
            data=json.dumps(batch, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        items = data if isinstance(data, list) else [data]
        for item in items:
            content_list = None
            if isinstance(item, dict) and "result" in item:
                content_list = item["result"].get("content", [])
            elif isinstance(item, dict) and "content" in item:
                content_list = item["content"]
            if content_list:
                for c in content_list:
                    if c.get("type") == "text":
                        text = c.get("text", "")
                        url = ""
                        annotations = c.get("annotations")
                        if isinstance(annotations, dict):
                            url = annotations.get("source", "")
                        resource = c.get("resource")
                        if isinstance(resource, dict):
                            url = resource.get("uri", url)
                        if text and url:
                            add_results([{"text": text[:2000], "url": url}])
        print(f"[search_web] Z.AI MCP: {len(all_results)} results")
    except Exception as e:
        print(f"[search_web] Z.AI MCP error: {e}")

    # 2) DuckDuckGo
    try:
        ddg_results = search_web_ddg(query)
        add_results(ddg_results)
    except Exception as e:
        print(f"[search_web] DuckDuckGo error: {e}")

    return all_results[:8]

def chat_with_ai(user_message, history, portfolio, news, search_results=None):
    context = build_chat_context(portfolio, news)
    if search_results is None:
        search_results = search_web(user_message)
    system_prompt = (
        "당신은 전문 주식 투자 어드바이저 'Stock Manager AI'입니다. "
        "사용자의 포트폴리오 정보와 시장 데이터를 바탕으로 투자 조언을 제공합니다.\n\n"
        f"📅 오늘 날짜: {datetime.now().strftime('%Y년 %m월 %d일')}\n\n"
        "【 현재 포트폴리오 상태 】\n"
        f"{context}\n"
        "【 응답 원칙 】\n"
        "1. 항상 데이터에 기반한 객관적인 조언을 제공하세요.\n"
        "2. 매수/매도/관망에 대한 명확한 의견을 제시하세요.\n"
        "3. 리스크 관리의 중요성을 강조하세요.\n"
        "4. 전문적이고 친근한 어조로 답변하세요.\n"
        "5. 한국어로 답변하세요.\n"
        "6. 답변은 800자 이내로 간결하게 작성하세요.\n"
        "7. 필요시 포트폴리오 내 특정 종목에 대한 구체적인 분석을 제공하세요.\n"
        "8. ⚠️ 절대 상상하여 답변하지 마세요. 제공된 대화 내역과 아래 웹 검색 결과를 모두 활용하여 답변하세요. "
        "대화 내역(history)에 이전에 나눈 내용이 있다면 그 정보도 적극 활용하세요. "
        "답변에는 반드시 출처 번호 [1][2]를 표시하고, 답변 하단에 📚 출처: 섹션을 추가하세요. "
        "검색 결과와 대화 내역 모두에 충분한 정보가 없으면 솔직히 '알 수 없습니다'라고 답변하세요."
    )
    messages = [{"role": "system", "content": system_prompt}]
    sliced = history[-CHAT_MSG_LIMIT:] if history else []
    for h in sliced:
        messages.append({"role": h["role"], "content": h["content"]})
    if search_results:
        search_text = ""
        for i, r in enumerate(search_results, 1):
            text = r.get("text", "")
            url = r.get("url", "")
            search_text += f"[{i}] {text}\n"
            if url:
                search_text += f"    출처: {url}\n\n"
        sf = f"/tmp/stock_search_{int(time.time())}.txt"
        try:
            with open(sf, "w", encoding="utf-8") as f:
                f.write(search_text)
        except Exception:
            sf = "(메모리)"
        search_block = (
            f"아래는 웹 검색 결과 파일({sf})의 내용입니다:\n\n{search_text}"
        )
        messages.append({"role": "user", "content": search_block})
        messages.append({"role": "assistant", "content": "파일을 읽었습니다. 출처 번호 [1][2]를 표기하여 답변하겠습니다."})
    messages.append({"role": "user", "content": user_message})
    result = call_llm(messages)
    reply = result["reply"]
    if search_results:
        urls = [(i, r.get("url", "")) for i, r in enumerate(search_results, 1) if r.get("url")]
        if urls:
            reply += "\n\n📚 출처:\n"
            for i, url in urls:
                reply += f"[{i}] {url}\n"
    return reply


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
        if p == "/api/chart":
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [None])[0]
            if not code:
                self.send_json({"error": "code parameter required"})
                return
            self.send_json(fetch_chart_data(code))
            return
        if p == "/api/analyze-signal":
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [None])[0]
            if not code:
                self.send_json({"error": "code parameter required"})
                return
            self.send_json(handle_analyze_signal(code))
            return
        if p == "/api/chat/history":
            self.send_json({"history": load_chat_history()})
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

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        if p == "/api/chat":
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self.send_json({"error": "invalid JSON"})
                return
            message = data.get("message", "").strip()
            if not message:
                self.send_json({"error": "message required"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            def sse(event, data):
                payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()

            history = load_chat_history()
            history.append({"role": "user", "content": message, "timestamp": int(time.time() * 1000)})
            sse("status", {"phase": "loading"})
            try:
                portfolio = build_portfolio()
                news = build_news()
            except Exception:
                portfolio = {"summary": {"currentValue": 0, "cost": 0, "profit": 0, "profitRate": 0}, "holdings": [], "watchlist": []}
                news = []
            sse("status", {"phase": "searching"})
            search_results = search_web(message)
            sse("status", {"phase": "analyzing"})
            reply = chat_with_ai(message, history, portfolio, news, search_results=search_results)
            history.append({"role": "assistant", "content": reply, "timestamp": int(time.time() * 1000)})
            save_chat_history(history)
            sse("result", {"reply": reply, "history": history})
            return
        self.send_error(405, "Method Not Allowed")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
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
