#!/usr/bin/env python3
import json
import mimetypes
import os
import re
import time
from datetime import datetime, timezone, timedelta
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

NOUS_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
NOUS_KEY = os.environ.get("NOUS_KEY", "sk-nous-dueimEQDyVHzxeKCOolvFyx7e0DKZzBR").strip()
NOUS_MODELS = ["stepfun/step-3.7-flash:free", "nex-agi/nex-n2-pro:free", "openai/gpt-oss-120b:free"]

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
        for item in items[:limit * 5]:
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

US_MARKET_FILE = BASE_DIR / "us_market.json"

CHAT_MSG_LIMIT = 20

_chat_history_cache = None

US_INDICES = [
    ("다우존스", "DJI@DJI"),
    ("S&P 500", "SPI@SPX"),
    ("나스닥", "NAS@IXIC"),
]

def fetch_us_market_realtime():
    """네이버 금융에서 미국 증시 실시간 데이터를 가져온다."""
    result = {"date": "", "indices": [], "marketStatus": "closed", "highlights": [], "summary": ""}
    
    for name, symbol in US_INDICES:
        try:
            url = f"https://finance.naver.com/world/sise.naver?symbol={symbol}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read().decode("euc-kr", errors="replace")
            
            # Extract current value from no_today section
            today_match = re.search(r'<p class="no_today">(.*?)</p>', html, re.DOTALL)
            if today_match:
                content = today_match.group(1)
                parts = re.findall(r'class="(no\d+|shim|jum)">([^<]*)</span>', content)
                value_str = ""
                for cls, val in parts:
                    if cls.startswith("no"):
                        value_str += val
                    elif cls == "shim":
                        value_str += val
                    elif cls == "jum":
                        value_str += val
                value = float(value_str.replace(",", ""))
                
                # Extract change and rate from no_exday section
                exday_match = re.search(r'class="no_exday"[^>]*>(.*?)</p>', html, re.DOTALL)
                change = 0
                rate = 0
                if exday_match:
                    exday_content = exday_match.group(1)
                    # Extract all no_up/no_down em sections
                    em_sections = re.findall(r'<em class="(no_up|no_down)"[^>]*>(.*?)</em>', exday_content, re.DOTALL)
                    if len(em_sections) >= 2:
                        # First section: change value
                        _, change_section = em_sections[0]
                        change_parts = re.findall(r'class="(no\d+|jum)">([^<]*)</span>', change_section)
                        change_str = ''
                        for tag, val in change_parts:
                            if tag.startswith('no'):
                                change_str += val
                            elif tag == 'jum':
                                change_str += val
                        change = float(change_str.replace(",", ""))
                        # Check direction from class name
                        if em_sections[0][0] == "no_down":
                            change = -change
                        
                        # Second section: rate
                        _, rate_section = em_sections[1]
                        rate_parts = re.findall(r'class="(no\d+|jum)">([^<]*)</span>', rate_section)
                        rate_str = ''
                        for tag, val in rate_parts:
                            if tag.startswith('no'):
                                rate_str += val
                            elif tag == 'jum':
                                rate_str += val
                        rate = float(rate_str.replace(",", ""))
                        # Check direction from class name
                        if em_sections[1][0] == "no_down":
                            rate = -rate
                
                result["indices"].append({
                    "name": name,
                    "value": value,
                    "change": change,
                    "rate": rate
                })
        except Exception as e:
            print(f"[fetch_us_market_realtime] {name} error: {e}", flush=True)
    
    # Determine market status based on US time
    utc_now = datetime.now(timezone.utc)
    us_tz = timezone(timedelta(hours=-4))
    us_now = utc_now.astimezone(us_tz)
    us_hour = us_now.hour
    us_min = us_now.minute
    us_time = us_hour * 60 + us_min
    market_open = 9 * 60 + 30
    market_close = 16 * 60
    
    if market_open <= us_time <= market_close:
        result["marketStatus"] = "open"
    elif us_time < market_open:
        result["marketStatus"] = "pre_open"
    else:
        result["marketStatus"] = "closed"
    
    result["date"] = us_now.strftime("%Y-%m-%d")
    
    # Try to load highlights and summary from local file as fallback
    try:
        with US_MARKET_FILE.open("r", encoding="utf-8") as f:
            file_data = json.load(f)
            result["highlights"] = file_data.get("highlights", [])
            result["summary"] = file_data.get("summary", "")
    except Exception:
        pass
    
    return result

def load_us_market():
    try:
        data = fetch_us_market_realtime()
        return data
    except Exception:
        return {"marketStatus": "closed"}

KOSPI_INDEX_URL = "https://finance.naver.com/sise/sise_index.naver?code=KOSPI"
KOSDAQ_INDEX_URL = "https://finance.naver.com/sise/sise_index.naver?code=KOSDAQ"

def fetch_kospi_kosdaq():
    """네이버 금융에서 코스피/코스닥 실시간 지수를 가져온다."""
    result = {"date": "", "indices": [], "marketStatus": "closed"}
    
    for name, url in [("코스피", KOSPI_INDEX_URL), ("코스닥", KOSDAQ_INDEX_URL)]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            
            # 지수 추출: <em id="now_value">8,394.65</em>
            value_match = re.search(r'id="now_value"[^>]*>([\d,]+\.\d+)', html)
            
            # 변동값/변동률 추출: <span class="fluc" id="change_value_and_rate"><span>16.56</span> -0.20%
            fluc_match = re.search(r'class="fluc"[^>]*><span>([\d,]+\.\d+)</span>\s*([+-]?[\d.]+)%', html)
            
            if value_match:
                value = float(value_match.group(1).replace(",", ""))
                change = 0
                rate = 0
                
                if fluc_match:
                    change = float(fluc_match.group(1).replace(",", ""))
                    rate = float(fluc_match.group(2))
                
                result["indices"].append({
                    "name": name,
                    "value": value,
                    "change": change,
                    "rate": rate
                })
        except Exception as e:
            print(f"[fetch_kospi_kosdaq] {name} error: {e}", flush=True)
    
    # 장 상태 및 날짜 설정
    now = datetime.now(timezone(timedelta(hours=9)))
    current_hour = now.hour
    current_minute = now.minute
    current_time = current_hour * 60 + current_minute
    
    # 한국 주식시장 시간: 09:00 ~ 15:30
    market_open = 9 * 60  # 09:00
    market_close = 15 * 60 + 30  # 15:30
    
    if current_time < market_open:
        # 장 시작 전
        result["marketStatus"] = "pre_open"
        result["date"] = now.strftime("%Y-%m-%d")
    elif current_time >= market_open and current_time <= market_close:
        # 장 운영 중
        result["marketStatus"] = "open"
        result["date"] = now.strftime("%Y-%m-%d")
    else:
        # 장 마감 후
        result["marketStatus"] = "closed"
        result["date"] = now.strftime("%Y-%m-%d")
    
    return result

def build_us_market_context():
    data = load_us_market()
    if not data:
        return ""
    lines = []
    lines.append("🇺🇸 미국증시 (전일 마감)")
    if data.get("date"):
        lines.append(f"• 날짜: {data['date']}")
    if data.get("indices"):
        for idx in data["indices"]:
            sign = "+" if idx["rate"] > 0 else ""
            lines.append(f"• {idx['name']}: {idx['value']:,.2f} ({sign}{idx['rate']:.2f}%)")
    if data.get("summary"):
        lines.append(f"• 요약: {data['summary']}")
    if data.get("highlights"):
        lines.append("• 주요 특징:")
        for h in data["highlights"]:
            lines.append(f"  - {h}")
    return "\n".join(lines)

def build_kospi_kosdaq_context():
    data = fetch_kospi_kosdaq()
    if not data or not data.get("indices"):
        return ""
    lines = []
    lines.append("📈 코스피/코스닥")
    if data.get("date"):
        status = data.get("marketStatus", "closed")
        status_text = "장 운영 중" if status == "open" else ("장 시작 전" if status == "pre_open" else "마감")
        lines.append(f"• 날짜: {data['date']} ({status_text})")
    if data.get("indices"):
        for idx in data["indices"]:
            sign = "+" if idx["rate"] > 0 else ""
            lines.append(f"• {idx['name']}: {idx['value']:,.2f} ({sign}{idx['rate']:.2f}%)")
    return "\n".join(lines)

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
        kst = timezone(timedelta(hours=9))
        today_str = datetime.now(kst).strftime('%Y-%m-%d')
        today_trades = [t for t in portfolio["trades"] if t.get("date") == today_str]
        past_trades = [t for t in portfolio["trades"] if t.get("date") != today_str]
        if today_trades:
            lines.append("📋 오늘의 거래내역")
            for t in today_trades:
                emoji = "🟢" if t["type"] == "buy" else "🔴"
                action = "매도" if t["type"] == "sell" else "매수"
                lines.append(f"  {emoji} {t['name']} {t['quantity']}주 {action} @ {t['price']:,.0f}원 ({t.get('note','')})")
            lines.append("")
        if past_trades:
            lines.append("📋 과거 거래내역")
            for t in past_trades:
                emoji = "🟢" if t["type"] == "buy" else "🔴"
                action = "매도" if t["type"] == "sell" else "매수"
                lines.append(f"  {emoji} [{t.get('date','')}] {t['name']} {t['quantity']}주 {action} @ {t['price']:,.0f}원 ({t.get('note','')})")
            lines.append("")
    if news:
        lines.append("📰 최근 뉴스 (최신순)")
        for n in news:
            if n.get("articles"):
                for article in n["articles"][:3]:
                    title = article["title"][:80]
                    url = article.get("url", "")
                    source = article.get("source", "")
                    pub_date = article.get("pubDate", "")
                    lines.append(f"• [{n['name']}] {title}")
                    if url:
                        lines.append(f"  URL: {url}")
                    if source:
                        lines.append(f"  출처: {source}")
        lines.append("")
    return "\n".join(lines)

def chat_from_zai(messages):
    payload = {
        "model": "glm-5",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 2000,
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
        "max_tokens": 2000,
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

def chat_from_nous(messages, model=None):
    if not NOUS_KEY:
        return {"error": "NOUS_KEY not set"}
    if not model:
        model = NOUS_MODELS[0]
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1500,
    }
    headers = {
        "Authorization": f"Bearer {NOUS_KEY}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        NOUS_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        result = json.loads(raw)
        msg = result["choices"][0]["message"]
        content = msg.get("content") or ""
        if not content:
            return {"error": "empty content"}
        return {"reply": content.strip(), "_source": "nous"}
    except Exception as exc:
        return {"error": str(exc)}

def call_llm(messages):
    result = chat_from_zai(messages)
    if "error" not in result:
        return result
    for m in NOUS_MODELS:
        result = chat_from_nous(messages, model=m)
        if "error" not in result:
            return result
    if OPENROUTER_KEY:
        for m in OPENROUTER_MODELS:
            result = chat_from_openrouter(messages, model=m)
            if "error" not in result:
                return result
    return {"reply": "죄송합니다. 현재 AI 서비스에 일시적인 문제가 있습니다. 잠시 후 다시 시도해 주세요.", "_source": "fallback"}

MCP_SEARCH_URL = "https://api.z.ai/api/mcp/web_search_prime/mcp"
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "").strip()

def is_irrelevant_result(url, text):
    """Filter out obviously irrelevant results."""
    url_lower = url.lower()
    text_lower = text.lower()
    irrelevant_domains = [
        "whatsapp.com", "wa.me", "web.whatsapp.com",
        "facebook.com", "fb.com", "instagram.com", "twitter.com", "x.com",
        "tiktok.com", "youtube.com", "netflix.com",
        "apple.com", "microsoft.com", "google.com/support",
        "amazon.com", "ebay.com",
    ]
    for domain in irrelevant_domains:
        if domain in url_lower:
            return True
    if "wikipedia.org" in url_lower:
        stock_kw = ["stock", "주식", "kospi", "kosdaq", "vi", "volatility",
                     "finance", "market", "invest", "trading", "배당",
                     "상장", "공매도", "액면", "변동", "호재", "악재"]
        if not any(kw in url_lower + text_lower for kw in stock_kw):
            return True
    if text and len(text) < 20:
        return True
    return False

def search_web_ddg(query):
    try:
        from duckduckgo_search import DDGS
        queries = [
            f"{query} 한국 주식 2026",
            f"{query} 증시 전망",
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

def search_web_brave(query):
    """Brave Search API — news + web 검색"""
    results = []
    seen_urls = set()
    headers = {
        "X-Subscription-Token": BRAVE_API_KEY,
        "Accept": "application/json",
    }
    if not BRAVE_API_KEY:
        return results
    try:
        encoded = urllib.parse.quote(query[:200])
        url = f"https://api.search.brave.com/res/v1/news/search?q={encoded}&freshness=pw&count=5"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for r in data.get("results", []):
            r_url = r.get("url", "")
            if r_url and r_url not in seen_urls and not is_irrelevant_result(r_url, r.get("title", "")):
                seen_urls.add(r_url)
                desc = r.get("description", "") or r.get("title", "")
                results.append({"text": desc[:2000], "url": r_url})
    except Exception as e:
        print(f"[search_web] Brave news error: {e}")
    try:
        encoded = urllib.parse.quote(query[:200])
        url = f"https://api.search.brave.com/res/v1/web/search?q={encoded}&freshness=pw&count=5"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for r in data.get("web", {}).get("results", []):
            r_url = r.get("url", "")
            if r_url and r_url not in seen_urls and not is_irrelevant_result(r_url, r.get("description", "")):
                seen_urls.add(r_url)
                desc = r.get("description", "") or r.get("title", "")
                results.append({"text": desc[:2000], "url": r_url})
    except Exception as e:
        print(f"[search_web] Brave web error: {e}")
    return results[:8]

def search_web(query):
    """검색 비활성화 (빠른 응답)"""
    return []

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


def get_next_trading_day(now_kst):
    """현재 시각 기준 다음 거래일(주말 제외)과 요일을 반환."""
    kst = timezone(timedelta(hours=9))
    next_day = now_kst + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][next_day.weekday()]
    return next_day.strftime('%Y년 %m월 %d일'), weekday_kr


def chat_with_ai(user_message, history, portfolio, news, search_results=None):
    context = build_chat_context(portfolio, news)
    us_market_ctx = build_us_market_context()
    kospi_kosdaq_ctx = build_kospi_kosdaq_context()
    if search_results is None:
        search_results = search_web(user_message)
    phase_label, trade_status, now_kst = get_market_status()
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][now_kst.weekday()]
    next_trade_date, next_trade_weekday = get_next_trading_day(now_kst)
    today_str = now_kst.strftime('%Y년 %m월 %d일')

    # 간소화된 시스템 프롬프트
    system_prompt = f"""Stock Manager AI입니다.
오늘: {today_str} ({weekday_kr}) {now_kst.strftime('%H:%M')}
장: {phase_label} | 다음 거래일: {next_trade_date}

규칙:
1. 위 뉴스 데이터를 사용하세요. 검색 결과의 오래된 뉴스를 사용하지 마세요.
2. 모든 수치는 위 데이터에서만 가져오세요.
3. 간결하게 답변하세요.

{context}
"""
    if us_market_ctx:
        system_prompt += f"\n{us_market_ctx}\n"
    if kospi_kosdaq_ctx:
        system_prompt += f"\n{kospi_kosdaq_ctx}\n"

    messages = [{"role": "system", "content": system_prompt}]
    sliced = history[-10:] if history else []
    for h in sliced:
        messages.append({"role": h["role"], "content": h["content"][:200]})
    messages.append({"role": "user", "content": user_message})
    result = call_llm(messages)
    reply = result["reply"]
    reply = re.sub(r'\n*📚\s*출처[:：][\s\S]*$', '', reply).rstrip()
    h_refs = re.findall(r'\[H(\d+)\]', reply) if sliced else []
    has_urls = search_results and any(r.get("url") for r in search_results[:3])
    if has_urls or h_refs:
        reply += "\n\n📚 출처:\n"
        if has_urls:
            for i, r in enumerate(search_results[:3], 1):
                if r.get("url"):
                    reply += f"[{i}] {r['url']}\n"
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
        if p == "/api/us-market":
            self.send_json(load_us_market())
            return
        if p == "/api/kospi-kosdaq":
            self.send_json(fetch_kospi_kosdaq())
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
