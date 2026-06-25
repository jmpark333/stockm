import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
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
CHAT_HISTORY_FILE = BASE_DIR / "chat_history.json"

# Upstash Redis (REST API)
REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")


def kv_get(key):
    if not REDIS_URL or not REDIS_TOKEN:
        return None
    try:
        url = f"{REDIS_URL}/get/{urllib.parse.quote(key)}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {REDIS_TOKEN}",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode("utf-8")).get("result")
            if result is None:
                return None
            return json.loads(result)
    except Exception:
        return None


def kv_set(key, value):
    if not REDIS_URL or not REDIS_TOKEN:
        return False
    try:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        url = f"{REDIS_URL}/set/{urllib.parse.quote(key)}"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {REDIS_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False
NAVER_REALTIME_URL = "https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"

history: dict[str, deque] = {}
MAX_HISTORY = 12

ZAI_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
ZAI_KEY = "136d90754ebd453999f4a4cc4547b638.LUXSKaxDozJgFHLQ"

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

def _decode_naver_response(raw: bytes) -> str:
    """네이버 API 응답을 안전하게 디코딩. UTF-8 우선, EUC-KR/CP949 fallback."""
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_quote(code):
    url = NAVER_REALTIME_URL.format(code=code)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read()
        text = _decode_naver_response(raw)
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
        "model": "glm-5",
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
        with urllib.request.urlopen(req, timeout=30) as resp:
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

    sentiment_lower = news_sentiment.lower() if news_sentiment else ""
    all_text = f"{sentiment_lower} {reasons_text}".lower()

    positive_keywords = ["긍정", "positive", "매수", "상승", "기대", "호재", "강세", "우호", "성장", "호조", "돌파", "신고가", "순매수", "목표가", "추가상승"]
    negative_keywords = ["부정", "negative", "매도", "우려", "악재", "약세", "적자", "침체", "경고", "위기"]

    is_positive = any(kw in all_text for kw in positive_keywords)
    is_negative = any(kw in all_text for kw in negative_keywords)

    if is_positive and not is_negative:
        if signal in ("strong_sell", "sell"):
            return "hold"
        return signal

    if is_negative and not is_positive:
        if signal in ("strong_buy", "buy"):
            return "hold"
        return signal

    if is_positive and is_negative:
        pos_count = sum(1 for kw in positive_keywords if kw in all_text)
        neg_count = sum(1 for kw in negative_keywords if kw in all_text)
        if pos_count > neg_count:
            if signal in ("strong_sell", "sell"):
                return "hold"
            return signal
        elif neg_count > pos_count:
            if signal in ("strong_buy", "buy"):
                return "hold"
            return signal

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

@app.route("/api/chart")
def api_chart():
    code = request.args.get("code")
    if not code:
        return Response(json.dumps({"error": "code parameter required"}), status=400, mimetype="application/json")
    return Response(
        json.dumps(fetch_chart_data(code), ensure_ascii=False),
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

# ──────────────────────────────────────────
# Stock Manager AI Chat — Session-Based
# ──────────────────────────────────────────

CHAT_MSG_LIMIT = 20
SESSION_TIMEOUT_MS = 3600000  # 60 min gap → new session

_chat_sessions_cache = None

def load_chat_sessions():
    global _chat_sessions_cache
    if _chat_sessions_cache is not None:
        return _chat_sessions_cache
    # Try Vercel KV first
    kv_data = kv_get("chat_sessions")
    if kv_data is not None:
        if isinstance(kv_data, list):
            sessions = {}
            sid = f"sess_{int(time.time() * 1000)}"
            now_str = datetime.now().strftime("%Y-%m-%d")
            now_t = datetime.now().strftime("%H:%M")
            sessions[sid] = {
                "id": sid, "createdAt": int(time.time() * 1000),
                "date": now_str, "time": now_t, "messages": kv_data,
            }
            kv_data = {"sessions": sessions, "current": sid}
            save_chat_sessions(kv_data)
        _chat_sessions_cache = kv_data
        return _chat_sessions_cache
    # Fallback to filesystem
    try:
        with CHAT_HISTORY_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                sessions = {}
                sid = f"sess_{int(time.time() * 1000)}"
                now_str = datetime.now().strftime("%Y-%m-%d")
                now_t = datetime.now().strftime("%H:%M")
                sessions[sid] = {
                    "id": sid, "createdAt": int(time.time() * 1000),
                    "date": now_str, "time": now_t, "messages": data,
                }
                data = {"sessions": sessions, "current": sid}
                save_chat_sessions(data)
            _chat_sessions_cache = data
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    _chat_sessions_cache = {"sessions": {}, "current": None}
    return _chat_sessions_cache

def save_chat_sessions(sessions_data):
    global _chat_sessions_cache
    _chat_sessions_cache = sessions_data
    # Try Upstash Redis first
    if REDIS_URL and REDIS_TOKEN:
        kv_set("chat_sessions", sessions_data)
    # Also write to filesystem (works locally, may fail on Vercel)
    try:
        with CHAT_HISTORY_FILE.open("w", encoding="utf-8") as f:
            json.dump(sessions_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def get_or_create_session(timestamp_ms=None):
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    data = load_chat_sessions()
    current_id = data.get("current")
    kst = timezone(timedelta(hours=9))
    now_dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=kst)
    date_str = now_dt.strftime("%Y-%m-%d")
    time_str = now_dt.strftime("%H:%M")

    if current_id and current_id in data.get("sessions", {}):
        sess = data["sessions"][current_id]
        last_msg = sess["messages"][-1] if sess["messages"] else None
        if last_msg:
            last_ts = last_msg.get("timestamp", 0)
            if timestamp_ms - last_ts < SESSION_TIMEOUT_MS:
                return current_id, data
    # Create new session
    sid = f"sess_{timestamp_ms}"
    data.setdefault("sessions", {})[sid] = {
        "id": sid, "createdAt": timestamp_ms,
        "date": date_str, "time": time_str, "messages": [],
    }
    data["current"] = sid
    save_chat_sessions(data)
    return sid, data

def add_message_to_session(role, content, timestamp_ms=None):
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    sid, data = get_or_create_session(timestamp_ms)
    sess = data["sessions"][sid]
    last_msg = sess["messages"][-1] if sess["messages"] else None
    if last_msg and last_msg.get("role") == role and last_msg.get("content") == content:
        return sid
    sess["messages"].append({
        "role": role, "content": content, "timestamp": timestamp_ms,
    })
    save_chat_sessions(data)
    return sid

def get_session_messages(session_id=None):
    data = load_chat_sessions()
    if session_id:
        sess = data.get("sessions", {}).get(session_id)
        return sess["messages"] if sess else []
    current_id = data.get("current")
    if current_id and current_id in data.get("sessions", {}):
        return data["sessions"][current_id]["messages"]
    return []

def delete_session(session_id):
    data = load_chat_sessions()
    sessions = data.get("sessions", {})
    if session_id not in sessions:
        return False
    del sessions[session_id]
    if data.get("current") == session_id:
        data["current"] = None
    save_chat_sessions(data)
    return True

def list_sessions():
    data = load_chat_sessions()
    current_id = data.get("current")
    result = []
    for sid, sess in data.get("sessions", {}).items():
        msgs = sess.get("messages", [])
        preview = ""
        if msgs:
            first = next((m for m in msgs if m["role"] == "user"), msgs[0])
            preview = first["content"][:60]
        result.append({
            "id": sid,
            "date": sess.get("date", ""),
            "time": sess.get("time", ""),
            "createdAt": sess.get("createdAt", 0),
            "messageCount": len(msgs),
            "preview": preview,
            "isCurrent": sid == current_id,
        })
    result.sort(key=lambda s: s["createdAt"], reverse=True)
    return result

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
        "thinking": {"type": "disabled"},
        "temperature": 0.7,
        "max_tokens": 1000,
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
    # Skip generic encyclopedia/dictionary pages unrelated to stocks
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

def get_market_status() -> tuple[str, str, datetime]:
    """현재 KST 시각과 토스증권 국내주식 장 상태를 반환.

    Returns:
        (phase_label, trade_status, now_kst)
    """
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    if now_kst.weekday() >= 5:
        return ("클로즈(주말)", "거래 불가 — 다음 거래일 프리마켓 08:00부터 주문 가능", now_kst)
    hhmm = now_kst.strftime("%H:%M")
    if "08:00" <= hhmm < "08:50":
        return ("프리마켓(장전)", "실시간 거래 가능 (토스증권)", now_kst)
    if "08:50" <= hhmm < "09:00":
        return ("프리마켓 마감~정규장 개시 전", "거래 불가 — 09:00 정규장 개시 대기", now_kst)
    if "09:00" <= hhmm < "15:20":
        return ("메인마켓(정규장)", "실시간 거래 가능 (가장 유동성 높음)", now_kst)
    if "15:20" <= hhmm < "15:30":
        return ("정규장 마감~시가단일가 준비", "거래 불가 — 15:30 시가단일가 대기", now_kst)
    if "15:30" <= hhmm < "15:40":
        return ("시가단일가 마감임박", "단일가 주문만 가능 (토스증권)", now_kst)
    if "15:40" <= hhmm < "20:00":
        return ("애프터마켓(장후)", "실시간 거래 가능 (유동성 낮음, 슬리피지 주의)", now_kst)
    return ("클로즈(장 마감)", "거래 불가 — 다음 거래일 프리마켓 08:00부터 주문 가능", now_kst)


def chat_with_ai(user_message, history, portfolio, news, search_results=None):
    context = build_chat_context(portfolio, news)
    if search_results is None:
        search_results = search_web(user_message)
    phase_label, trade_status, now_kst = get_market_status()
    weekday = now_kst.weekday()
    system_prompt = (
        "당신은 전문 주식 투자 어드바이저 'Stock Manager AI'입니다. "
        "사용자의 포트폴리오 정보와 시장 데이터를 바탕으로 투자 조언을 제공합니다.\n\n"
        f"📅 오늘 날짜: {now_kst.strftime('%Y년 %m월 %d일')} ({['월','화','수','목','금','토','일'][weekday]}요일)\n"
        f"🕒 현재 시각 (KST): {now_kst.strftime('%H시 %M분')}\n"
        f"📈 현재 장 상태: **{phase_label}** — {trade_status}\n\n"
        "【 토스증권 국내주식 장 운영 시간 】\n"
        "- 프리마켓(장전): 08:00~08:50 — 실시간 거래 가능\n"
        "- 메인마켓(정규장): 09:00~15:20 — 실시간 거래 가능\n"
        "- 시가단일가 마감임박: 15:30~15:40 — 단일가 주문만 가능\n"
        "- 애프터마켓(장후): 15:40~20:00 — 실시간 거래 가능\n"
        "- 클로즈(장 마감): 20:00~익일 08:00 — 거래 불가\n\n"
        "⚠️ 중요: 매매 추천 시 반드시 위 '현재 장 상태'를 기준으로 안내하세요. "
        "거래 불가 상태면 '다음 거래일 프리마켓 08:00 시작 후 주문' 형태로 안내하고, "
        "애프터마켓/프리마켓은 유동성이 낮아 슬리피지 위험이 크다는 점을 반드시 명시하세요.\n\n"
        "【 현재 포트폴리오 상태 】\n"
        f"{context}\n"
        "【 대시보드 지표 산출 방식 】\n"
        "사용자가 대시보드 지표의 의미나 계산 방법을 물으면 아래 기준으로 설명하세요.\n"
        "• 일중 범위 위치(rangePos): (현재가 - 저가) / (고가 - 저가) × 100. "
        "0이면 저가권(바닥), 100이면 고가권(천장). 50 미만은 상대적 저가 구간, 50 초과는 상대적 고가 구간.\n"
        "• 일중 변동성(volatility): (고가 - 저가) / 전일종가 × 100. "
        "예: 3이면 전일종가 대비 고가-저가 간 폭이 3%라는 의미. 높을수록 가격 변동이 큼.\n"
        "• 갭(gap): (시가 - 전일종가) / 전일종가 × 100. "
        "양수면 시가가 전일종가보다 높게 시작(갭업), 음수면 낮게 시작(갭다운).\n"
        "• 시가 대비 변동(changeFromOpen): (현재가 - 시가) / 시가 × 100. "
        "시가 이후의 방향성을 보여줌.\n"
        "• 단기 추세(shortTrend): 메모리에 저장된 최근 가격 히스토리(최대 30개)의 "
        "첫 번째 대비 마지막 가격 변화율 기준. +0.1% 이상이면 'up', -0.1% 이하면 'down', 그 외 'flat'.\n"
        "• AI 시그널 생성 흐름: ① 뉴스 수집 → ② LLM(ZAI/OpenRouter) 분석으로 원시 시그널 생성 → "
        "③ 키워드 분석(fallback) → ④ validate_signal 검증. "
        "LLM은 뉴스 제목+현재가 정보를 기반으로 strong_buy/buy/hold/sell/strong_sell + 신뢰도(0-100) + 사유를 JSON으로 반환.\n"
        "• 시그널 검증(validate_signal): "
        "뉴스 감성이 긍정+부정 혼재 시 상쇄되어 hold로 조정. "
        "가격 변동률(%chng)과 시그널이 불일치하면(예: +5% 상승 중 buy 시그널) 변동률 기준으로 재분류: "
        "+5% 초과→strong_sell, +3% 초과→sell, -5% 미만→strong_buy, -3% 미만→buy.\n"
        "• 뉴스 키워드 분석(fallback): POSITIVE_KW(21개, 예: 호조/상승/증가/성장)와 "
        "NEGATIVE_KW(20개, 예: 하락/감소/악화/부진)로 뉴스 텍스트 스캔. "
        "긍정 키워드 수 - 부정 키워드 수 = net. net≥2→buy, net≤-2→sell, 그 외→hold.\n"
        "• 포트폴리오 요약: 총 현재가치 = ∑(현재가 × 수량), 총 원가 = ∑(평단가 × 수량), "
        "총 수익 = 총 현재가치 - 총 원가, 수익률(%) = 총 수익 / 총 원가 × 100.\n\n"
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
        "이전 대화 내용을 인용할 때는 [H숫자] 형식으로 출처를 표시하세요. "
        "[H3]은 인덱스 3번 메시지를 의미합니다. "
        "답변에는 반드시 출처 번호 [1][2]와 [H...]를 함께 표시하고, 답변 하단에 📚 출처: 섹션을 추가하세요. "
        "검색 결과와 대화 내역 모두에 충분한 정보가 없으면 솔직히 '알 수 없습니다'라고 답변하세요.\n"
        "9. 🚫 출처 할루네이션 금지: 검색 결과(search_results)가 0건이거나 비어 있으면, "
        "답변에 절대 [1][2] 같은 출처 번호를 만들지 마세요. "
        "반드시 실제로 제공된 search_results 안의 URL만 인용하고, 없으면 '최신 실시간 데이터 확인이 필요합니다'라고 솔직히 답변하세요."
    )
    messages = [{"role": "system", "content": system_prompt}]
    sliced = history[-CHAT_MSG_LIMIT:] if history else []
    previous = sliced[:-1] if len(sliced) > 1 else []
    first_h_idx = max(0, len(history) - CHAT_MSG_LIMIT) if history else 0
    if previous:
        h_ref = "【 이전 대화 참조 (인용 시 [H...] 사용) 】\n"
        for i, h in enumerate(previous, 1):
            display_idx = first_h_idx + i
            role_label = "사용자" if h["role"] == "user" else "어드바이저"
            preview = h["content"][:150].replace("\n", " ")
            h_ref += f"[H{display_idx}] ({role_label}): {preview}\n"
        messages.append({"role": "system", "content": h_ref})
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
    h_refs = re.findall(r'\[H(\d+)\]', reply) if previous else []
    has_urls = search_results and any(r.get("url") for r in search_results)
    if has_urls or h_refs:
        reply += "\n\n📚 출처:\n"
        if search_results:
            for i, r in enumerate(search_results, 1):
                if r.get("url"):
                    reply += f"[{i}] {r['url']}\n"
        if h_refs:
            for hn in sorted(set(h_refs), key=int):
                display_idx = int(hn)
                zero_idx = display_idx - 1
                if first_h_idx <= zero_idx < first_h_idx + len(previous):
                    h = previous[zero_idx - first_h_idx]
                    role_label = "사용자" if h["role"] == "user" else "어드바이저"
                    preview = h["content"][:60].replace("\n", " ")
                    reply += f"[H{hn}] {role_label}: \"{preview}\"\n"
    return reply

@app.route("/api/chat/history")
def api_chat_history():
    messages = get_session_messages()
    return Response(
        json.dumps({"history": messages}, ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )

@app.route("/api/chat/new-session", methods=["POST"])
def api_new_session():
    global _chat_sessions_cache
    _chat_sessions_cache = None
    now_ms = int(time.time() * 1000)
    sid = f"sess_{now_ms}"
    kst = timezone(timedelta(hours=9))
    now_dt = datetime.fromtimestamp(now_ms / 1000, tz=kst)
    data = load_chat_sessions()
    data["sessions"][sid] = {
        "id": sid, "createdAt": now_ms,
        "date": now_dt.strftime("%Y-%m-%d"),
        "time": now_dt.strftime("%H:%M"),
        "messages": [],
    }
    data["current"] = sid
    save_chat_sessions(data)
    return Response(
        json.dumps({"sessionId": sid}, ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )

@app.route("/api/chat/sessions")
def api_chat_sessions():
    return Response(
        json.dumps({"sessions": list_sessions()}, ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )

@app.route("/api/chat/session/<session_id>")
def api_chat_session(session_id):
    messages = get_session_messages(session_id)
    if not messages:
        return Response(
            json.dumps({"error": "session not found"}, ensure_ascii=False),
            status=404, mimetype="application/json",
        )
    return Response(
        json.dumps({"history": messages, "sessionId": session_id}, ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )

@app.route("/api/chat/session/<session_id>", methods=["DELETE"])
def api_delete_chat_session(session_id):
    if delete_session(session_id):
        return Response(
            json.dumps({"ok": True}, ensure_ascii=False),
            mimetype="application/json",
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )
    return Response(
        json.dumps({"error": "session not found"}, ensure_ascii=False),
        status=404, mimetype="application/json",
    )

@app.route("/api/chat", methods=["POST", "OPTIONS"])
def api_chat():
    if request.method == "OPTIONS":
        resp = Response()
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp
    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    if not message:
        return Response(
            json.dumps({"error": "message required"}),
            status=400,
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    def generate():
        now_ms = int(time.time() * 1000)
        history = get_session_messages()
        history.append({"role": "user", "content": message, "timestamp": now_ms})
        add_message_to_session("user", message)
        yield "event: status\ndata: " + json.dumps({"phase": "loading"}, ensure_ascii=False) + "\n\n"
        try:
            portfolio = build_portfolio()
            news = build_news()
        except Exception:
            portfolio = {"summary": {"currentValue": 0, "cost": 0, "profit": 0, "profitRate": 0}, "holdings": [], "watchlist": []}
            news = []
        yield "event: status\ndata: " + json.dumps({"phase": "searching"}, ensure_ascii=False) + "\n\n"
        search_results = search_web(message)
        yield "event: status\ndata: " + json.dumps({"phase": "analyzing"}, ensure_ascii=False) + "\n\n"
        reply = chat_with_ai(message, history, portfolio, news, search_results=search_results)
        history.append({"role": "assistant", "content": reply, "timestamp": now_ms})
        add_message_to_session("assistant", reply)
        updated_history = get_session_messages()
        yield "event: result\ndata: " + json.dumps({"reply": reply, "history": updated_history}, ensure_ascii=False) + "\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "X-Accel-Buffering": "no",
        },
    )
