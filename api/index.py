import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, Response, request, send_from_directory

app = Flask(__name__, static_folder=None)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = BASE_DIR / "data.json"
CHAT_HISTORY_FILE = BASE_DIR / "chat_history.json"
US_MARKET_FILE = BASE_DIR / "us_market.json"

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
            payload = json.loads(resp.read().decode("utf-8"))
            result = payload.get("result")
            if result is None:
                return None
            return json.loads(result)
    except Exception as exc:
        import sys
        print(f"[kv_get] key={key} error={exc}", file=sys.stderr, flush=True)
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
            payload = json.loads(resp.read().decode("utf-8"))
            ok = payload.get("result") == "OK" and resp.status == 200
            if not ok:
                import sys
                print(f"[kv_set] key={key} unexpected payload={payload}", file=sys.stderr, flush=True)
            return ok
    except Exception as exc:
        import sys
        print(f"[kv_set] key={key} error={exc}", file=sys.stderr, flush=True)
        return False
NAVER_REALTIME_URL = "https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"

history: dict[str, deque] = {}
MAX_HISTORY = 12

ZAI_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
ZAI_KEY = "136d90754ebd453999f4a4cc4547b638.LUXSKaxDozJgFHLQ"

NOUS_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
NOUS_KEY = os.environ.get("NOUS_KEY", "sk-nous-dueimEQDyVHzxeKCOolvFyx7e0DKZzBR").strip()
NOUS_MODELS = ["stepfun/step-3.7-flash:free", "nex-agi/nex-n2-pro:free", "openai/gpt-oss-120b:free"]

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "").strip()
OPENROUTER_MODELS = ["nex-agi/nex-n2-pro:free", "openai/gpt-oss-120b:free"]

OPENCODE_URL = "https://opencode.ai/zen/go/v1/chat/completions"
OPENCODE_KEY = os.environ.get("OPENCODE_KEY", "").strip()
OPENCODE_MODEL = "glm-5.2"

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
    global _PORTFOLIO_CACHE, _PORTFOLIO_CACHE_TS
    now = time.time()
    if _PORTFOLIO_CACHE is not None and (now - _PORTFOLIO_CACHE_TS) < _PORTFOLIO_CACHE_TTL:
        return _PORTFOLIO_CACHE

    config = load_config()
    all_codes = [h["code"] for h in config["holdings"]]
    all_codes += [w["code"] for w in config.get("watchlist", [])]

    # Parallel fetch_quote: previously N sequential HTTP calls (~200ms each).
    # With ~10 codes this cuts the "포트폴리오 로딩" phase from ~2s to ~0.3s.
    seen: set[str] = set()
    unique_codes = [c for c in all_codes if not (c in seen or seen.add(c))]
    with ThreadPoolExecutor(max_workers=min(8, max(2, len(unique_codes)))) as ex:
        quote_results = list(ex.map(fetch_quote, unique_codes))
    quotes = {item["code"]: item for item in quote_results if item and item.get("code")}

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
        sell_fee = current_value * 0.00215
        realized_profit = profit - sell_fee
        realized_profit_rate = (realized_profit / cost * 100) if cost else 0
        holdings_rows.append({
            **build_item(quote),
            "quantity": quantity,
            "avgPrice": avg_price,
            "cost": cost,
            "currentValue": current_value,
            "profit": profit,
            "profitRate": profit_rate,
            "sellFee": sell_fee,
            "realizedProfit": realized_profit,
            "realizedProfitRate": realized_profit_rate,
        })
    watchlist_rows = []
    for watch in config.get("watchlist", []):
        quote = quotes.get(watch["code"], {"code": watch["code"], "error": "호가 데이터 없음"})
        watchlist_rows.append(build_item(quote))
    total_cost = sum(row["cost"] for row in holdings_rows)
    total_current = sum(row["currentValue"] for row in holdings_rows)
    total_profit = total_current - total_cost
    total_profit_rate = (total_profit / total_cost * 100) if total_cost else 0
    total_sell_fee = total_current * 0.00215
    total_realized_profit = total_profit - total_sell_fee
    total_realized_profit_rate = (total_realized_profit / total_cost * 100) if total_cost else 0
    result = {
        "currency": config.get("currency", "KRW"),
        "refreshSeconds": config.get("refreshSeconds", 10),
        "generatedAt": int(time.time() * 1000),
        "summary": {
            "currentValue": total_current,
            "cost": total_cost,
            "profit": total_profit,
            "profitRate": total_profit_rate,
            "sellFee": total_sell_fee,
            "realizedProfit": total_realized_profit,
            "realizedProfitRate": total_realized_profit_rate,
        },
        "holdings": holdings_rows,
        "watchlist": watchlist_rows,
        "trades": config.get("trades", []),
    }
    _PORTFOLIO_CACHE = result
    _PORTFOLIO_CACHE_TS = now
    return result

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

# ──────────────────────────────────────────
# 기술적 지표 계산 함수
# ──────────────────────────────────────────
import statistics

def calc_sma(data, period):
    result = [None] * len(data)
    for i in range(period - 1, len(data)):
        result[i] = sum(data[i - period + 1:i + 1]) / period
    return result

def calc_ema(data, period):
    result = [None] * len(data)
    if len(data) < period:
        return result
    k = 2 / (period + 1)
    result[period - 1] = sum(data[:period]) / period
    for i in range(period, len(data)):
        result[i] = data[i] * k + result[i - 1] * (1 - k)
    return result

def calc_rsi(closes, period=14):
    result = [None] * len(closes)
    if len(closes) < period + 1:
        return result
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - (100 / (1 + rs))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i + 1] = 100 - (100 / (1 + rs))
    return result

def calc_macd(closes, fast=12, slow=26, signal=9):
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    macd_line = [None] * len(closes)
    for i in range(len(closes)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]
    valid_macd = [v for v in macd_line if v is not None]
    signal_line = [None] * len(closes)
    if len(valid_macd) >= signal:
        start_idx = next(i for i, v in enumerate(macd_line) if v is not None)
        ema_vals = calc_ema(valid_macd, signal)
        for j, v in enumerate(ema_vals):
            if v is not None:
                signal_line[start_idx + j] = v
    histogram = [None] * len(closes)
    for i in range(len(closes)):
        if macd_line[i] is not None and signal_line[i] is not None:
            histogram[i] = macd_line[i] - signal_line[i]
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}

def calc_bollinger_bands(closes, period=20, num_std=2):
    middle = calc_sma(closes, period)
    upper = [None] * len(closes)
    lower = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        std = statistics.stdev(window) if len(window) > 1 else 0
        upper[i] = middle[i] + num_std * std
        lower[i] = middle[i] - num_std * std
    return {"upper": upper, "middle": middle, "lower": lower}

def calc_stochastic(highs, lows, closes, k_period=14, d_period=3):
    k_values = [None] * len(closes)
    for i in range(k_period - 1, len(closes)):
        window_high = max(highs[i - k_period + 1:i + 1])
        window_low = min(lows[i - k_period + 1:i + 1])
        if window_high == window_low:
            k_values[i] = 50.0
        else:
            k_values[i] = ((closes[i] - window_low) / (window_high - window_low)) * 100
    d_values = [None] * len(closes)
    for i in range(k_period - 1 + d_period - 1, len(closes)):
        valid_k = [v for v in k_values[i - d_period + 1:i + 1] if v is not None]
        if valid_k:
            d_values[i] = sum(valid_k) / len(valid_k)
    return {"k": k_values, "d": d_values}

def calc_atr(highs, lows, closes, period=14):
    result = [None] * len(closes)
    if len(closes) < 2:
        return result
    tr_list = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    if len(tr_list) >= period:
        result[period - 1] = sum(tr_list[:period]) / period
        for i in range(period, len(tr_list)):
            result[i] = (result[i-1] * (period - 1) + tr_list[i]) / period
    return result

_tech_cache = {}
_tech_cache_time = {}
TECH_CACHE_TTL = 300

def calc_tech_indicators(code):
    import time
    now = time.time()
    cached = _tech_cache.get(code)
    if cached and now - _tech_cache_time.get(code, 0) < TECH_CACHE_TTL:
        return cached
    chart = fetch_chart_data(code, days=120)
    candles = chart.get("candles", [])
    if len(candles) < 30:
        return {"indicators": {}, "signals": [], "signalScore": 0, "techSignal": "hold"}
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    ma5 = calc_sma(closes, 5)
    ma20 = calc_sma(closes, 20)
    ma60 = calc_sma(closes, 60)
    ma120 = calc_sma(closes, 120) if len(closes) >= 120 else [None] * len(closes)
    rsi14 = calc_rsi(closes, 14)
    macd_data = calc_macd(closes, 12, 26, 9)
    bb = calc_bollinger_bands(closes, 20, 2.0)
    stoch = calc_stochastic(highs, lows, closes, 14, 3)
    atr14 = calc_atr(highs, lows, closes, 14)
    cur = closes[-1]
    c_ma5, c_ma20, c_ma60 = ma5[-1], ma20[-1], ma60[-1]
    c_ma120 = ma120[-1] if ma120 else None
    c_rsi = rsi14[-1]
    c_macd = macd_data["macd"][-1]
    c_signal = macd_data["signal"][-1]
    c_hist = macd_data["histogram"][-1]
    c_bb_u = bb["upper"][-1]
    c_bb_m = bb["middle"][-1]
    c_bb_l = bb["lower"][-1]
    c_stoch_k = stoch["k"][-1]
    c_stoch_d = stoch["d"][-1]
    c_atr = atr14[-1]
    prev_macd = macd_data["macd"][-2] if len(macd_data["macd"]) > 1 else None
    prev_signal = macd_data["signal"][-2] if len(macd_data["signal"]) > 1 else None
    prev_stoch_k = stoch["k"][-2] if len(stoch["k"]) > 1 else None
    prev_stoch_d = stoch["d"][-2] if len(stoch["d"]) > 1 else None
    signals = []
    score = 0
    if c_ma5 and c_ma20 and c_ma60:
        if c_ma5 > c_ma20 > c_ma60:
            signals.append("정배열 (상승추세)")
            score += 20
        elif c_ma5 < c_ma20 < c_ma60:
            signals.append("역배열 (하락추세)")
            score -= 20
    if c_ma5 and c_ma20 and ma5[-2] and ma20[-2]:
        if ma5[-2] < ma20[-2] and c_ma5 > c_ma20:
            signals.append("MA5/20 골든크로스")
            score += 15
        elif ma5[-2] > ma20[-2] and c_ma5 < c_ma20:
            signals.append("MA5/20 데드크로스")
            score -= 15
    if c_rsi is not None:
        if c_rsi > 70:
            signals.append(f"RSI 과매수 ({c_rsi:.1f})")
            score -= 15
        elif c_rsi < 30:
            signals.append(f"RSI 과매도 ({c_rsi:.1f})")
            score += 15
        elif c_rsi > 60:
            signals.append(f"RSI 강세 ({c_rsi:.1f})")
            score += 5
        elif c_rsi < 40:
            signals.append(f"RSI 약세 ({c_rsi:.1f})")
            score -= 5
    if c_macd is not None and c_signal is not None:
        if prev_macd is not None and prev_signal is not None:
            if prev_macd < prev_signal and c_macd > c_signal:
                signals.append("MACD 골든크로스")
                score += 20
            elif prev_macd > prev_signal and c_macd < c_signal:
                signals.append("MACD 데드크로스")
                score -= 20
        if c_hist is not None:
            if c_hist > 0:
                signals.append("MACD 히스토그램 양전환")
                score += 5
            else:
                signals.append("MACD 히스토그램 음전환")
                score -= 5
    if c_bb_u and c_bb_l:
        bb_pos = (cur - c_bb_l) / (c_bb_u - c_bb_l) * 100
        if cur > c_bb_u:
            signals.append("볼린저 상단 돌파")
            score -= 10
        elif cur < c_bb_l:
            signals.append("볼린저 하단 이탈")
            score += 10
        elif bb_pos > 80:
            signals.append(f"볼린저 상단 접근 ({bb_pos:.0f}%)")
            score -= 5
        elif bb_pos < 20:
            signals.append(f"볼린저 하단 접근 ({bb_pos:.0f}%)")
            score += 5
    if c_stoch_k is not None and c_stoch_d is not None:
        if c_stoch_k > 80:
            signals.append(f"스토캐스틱 과매수 ({c_stoch_k:.1f})")
            score -= 10
        elif c_stoch_k < 20:
            signals.append(f"스토캐스틱 과매도 ({c_stoch_k:.1f})")
            score += 10
        if prev_stoch_k is not None and prev_stoch_d is not None:
            if prev_stoch_k < prev_stoch_d and c_stoch_k > c_stoch_d:
                signals.append("스토캐스틱 골든크로스")
                score += 10
            elif prev_stoch_k > prev_stoch_d and c_stoch_k < c_stoch_d:
                signals.append("스토캐스틱 데드크로스")
                score -= 10
    # 거래량 분석
    if len(candles) >= 10:
        current_vol = candles[-1].get("volume", 0) or 0
        recent_vols = [c.get("volume", 0) or 0 for c in candles[-20:]]
        avg_vol_20 = sum(recent_vols) / len(recent_vols) if recent_vols else 0
        if avg_vol_20 > 0 and current_vol > 0:
            vol_ratio = current_vol / avg_vol_20
            if vol_ratio >= 3.0:
                signals.append(f"거래량 {vol_ratio:.1f}배 폭증")
                score += 5 if cur > (candles[-2].get("close", cur) if len(candles) >= 2 else cur) else -5
            elif vol_ratio >= 2.0:
                signals.append(f"거래량 {vol_ratio:.1f}배 급증")
                score += 3 if cur > (candles[-2].get("close", cur) if len(candles) >= 2 else cur) else -3
            elif vol_ratio <= 0.3:
                signals.append(f"거래량 {vol_ratio:.1f}배 급감")
            elif vol_ratio <= 0.5:
                signals.append(f"거래량 {vol_ratio:.1f}배 감소")
    if c_ma20:
        pvm = (cur - c_ma20) / c_ma20 * 100
        if pvm > 5:
            signals.append(f"MA20 대비 +{pvm:.1f}% (과열)")
            score -= 5
        elif pvm < -5:
            signals.append(f"MA20 대비 {pvm:.1f}% (과침)")
            score += 5
    tech_signal = "hold"
    if score >= 30:
        tech_signal = "strong_buy"
    elif score >= 15:
        tech_signal = "buy"
    elif score <= -30:
        tech_signal = "strong_sell"
    elif score <= -15:
        tech_signal = "sell"
    result = {
        "indicators": {
            "ma5": round(c_ma5, 0) if c_ma5 else None,
            "ma20": round(c_ma20, 0) if c_ma20 else None,
            "ma60": round(c_ma60, 0) if c_ma60 else None,
            "ma120": round(c_ma120, 0) if c_ma120 else None,
            "rsi14": round(c_rsi, 2) if c_rsi else None,
            "macd": {"macd": round(c_macd, 2) if c_macd else None, "signal": round(c_signal, 2) if c_signal else None, "histogram": round(c_hist, 2) if c_hist else None},
            "bollinger": {"upper": round(c_bb_u, 0) if c_bb_u else None, "middle": round(c_bb_m, 0) if c_bb_m else None, "lower": round(c_bb_l, 0) if c_bb_l else None},
            "stochastic": {"k": round(c_stoch_k, 2) if c_stoch_k else None, "d": round(c_stoch_d, 2) if c_stoch_d else None},
            "atr14": round(c_atr, 0) if c_atr else None,
        },
        "signals": signals,
        "signalScore": score,
        "techSignal": tech_signal,
        "currentPrice": cur,
        "dataPoints": len(candles),
    }
    _tech_cache[code] = result
    _tech_cache_time[code] = now
    return result

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

def signal_from_nous(name, code, quote, articles, model=None):
    if not NOUS_KEY:
        return {"error": "NOUS_KEY not set"}
    if not model:
        model = NOUS_MODELS[0]
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
        "max_tokens": 1000,
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
        result = json.loads(raw)
        msg = result["choices"][0]["message"]
        content = msg.get("content") or ""
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
                "_source": "nous",
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
        for n_model in NOUS_MODELS:
            n_result = signal_from_nous(item["name"], code, quote, articles, model=n_model)
            if "error" not in n_result:
                result = n_result
                break
        else:
            for or_model in OPENROUTER_MODELS:
                or_result = signal_from_openrouter(item["name"], code, quote, articles, model=or_model)
                if "error" not in or_result:
                    result = or_result
                    break
            else:
                result = keyword_signal(item["name"], code, quote, articles)
                result["_fallback"] = True
    elif "error" in result:
        for n_model in NOUS_MODELS:
            n_result = signal_from_nous(item["name"], code, quote, articles, model=n_model)
            if "error" not in n_result:
                result = n_result
                break
        else:
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

    # 기술적 지표 점수를 AI 분석에 반영
    tech = calc_tech_indicators(code)
    tech_signal = tech.get("techSignal", "hold")
    tech_score = tech.get("signalScore", 0)
    tech_signals = tech.get("signals", [])
    ai_signal = result.get("signal", "hold")

    if tech_signal != "hold":
        reasons = result.get("reasons", [])
        if ai_signal == "hold":
            result["signal"] = tech_signal
            reasons.insert(0, f"기술적 지표: {tech_signal} (점수: {tech_score})")
        elif ai_signal != tech_signal:
            if tech_score <= -30 and ai_signal in ("buy", "strong_buy"):
                result["signal"] = "hold"
                reasons.insert(0, f"기술적 지표 반대로 매수 보류 (점수: {tech_score})")
            elif tech_score >= 30 and ai_signal in ("sell", "strong_sell"):
                result["signal"] = "hold"
                reasons.insert(0, f"기술적 지표 반대로 매도 보류 (점수: {tech_score})")
            elif tech_score <= -15 and ai_signal == "hold":
                result["signal"] = tech_signal
                reasons.insert(0, f"기술적 지표: {tech_signal} (점수: {tech_score})")
            elif tech_score >= 15 and ai_signal == "hold":
                result["signal"] = tech_signal
                reasons.insert(0, f"기술적 지표: {tech_signal} (점수: {tech_score})")
        for ts in tech_signals[:3]:
            if ts not in reasons:
                reasons.append(ts)
        result["reasons"] = reasons
    result["techSignalScore"] = tech_score

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

@app.route("/api/us-market")
def api_us_market():
    return Response(
        json.dumps(load_us_market(), ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )

@app.route("/api/kospi-kosdaq")
def api_kospi_kosdaq():
    return Response(
        json.dumps(fetch_kospi_kosdaq(), ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )

@app.route("/api/us-market-news")
def api_us_market_news():
    return Response(
        json.dumps({"articles": get_us_market_news()}, ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )

@app.route("/api/kr-market-news")
def api_kr_market_news():
    return Response(
        json.dumps({"articles": get_kr_market_news()}, ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
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
    chart_data = fetch_chart_data(code)
    try:
        tech = calc_tech_indicators(code)
        chart_data["techIndicators"] = tech.get("indicators", {})
        chart_data["techSignals"] = tech.get("signals", [])
        chart_data["techSignalScore"] = tech.get("signalScore", 0)
        chart_data["techSignal"] = tech.get("techSignal", "hold")
    except Exception as e:
        chart_data["techIndicators"] = {}
        chart_data["techSignals"] = []
        chart_data["techSignalScore"] = 0
        chart_data["techSignal"] = "hold"
    candles = chart_data.get("candles", [])
    if len(candles) >= 5:
        closes = [c["close"] for c in candles]
        chart_data["maArrays"] = {
            "ma5": calc_sma(closes, 5),
            "ma20": calc_sma(closes, 20),
            "ma60": calc_sma(closes, 60),
            "ma120": calc_sma(closes, 120) if len(closes) >= 120 else [None] * len(candles),
        }
    return Response(
        json.dumps(chart_data, ensure_ascii=False),
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


def _normalize_sessions_data(data):
    """Migrate legacy formats (raw list) into the {sessions, current} envelope."""
    if isinstance(data, list):
        sid = f"sess_{int(time.time() * 1000)}"
        now_str = datetime.now().strftime("%Y-%m-%d")
        now_t = datetime.now().strftime("%H:%M")
        sessions = {
            sid: {
                "id": sid,
                "createdAt": int(time.time() * 1000),
                "date": now_str,
                "time": now_t,
                "messages": data,
            }
        }
        return {"sessions": sessions, "current": sid}
    if isinstance(data, dict) and "sessions" in data:
        return data
    return {"sessions": {}, "current": None}


def load_chat_sessions():
    """Always read fresh from Redis. No in-memory cache (serverless-safe)."""
    kv_data = kv_get("chat_sessions")
    if kv_data is not None:
        normalized = _normalize_sessions_data(kv_data)
        if normalized is not kv_data and normalized.get("sessions"):
            save_chat_sessions(normalized)
        return normalized
    # Fallback to filesystem (local dev / Redis unavailable)
    try:
        with CHAT_HISTORY_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return _normalize_sessions_data(data)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"sessions": {}, "current": None}

def save_chat_sessions(sessions_data):
    """Persist to Redis (primary) and filesystem (local fallback). Returns Redis ok."""
    if not isinstance(sessions_data, dict) or "sessions" not in sessions_data:
        return False
    # Also write to filesystem (works locally, may fail on Vercel)
    try:
        with CHAT_HISTORY_FILE.open("w", encoding="utf-8") as f:
            json.dump(sessions_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    # Primary: Upstash Redis
    if REDIS_URL and REDIS_TOKEN:
        return kv_set("chat_sessions", sessions_data)
    return True  # local-only mode (no Redis configured)

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
        elif sess.get("createdAt", 0) and timestamp_ms - sess["createdAt"] < SESSION_TIMEOUT_MS:
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
    """Return sessions for the sidebar. Empty (no-message) sessions are hidden
    unless they are the current session (so the UI can still show 'New chat' state)."""
    data = load_chat_sessions()
    current_id = data.get("current")
    result = []
    for sid, sess in data.get("sessions", {}).items():
        msgs = sess.get("messages", [])
        is_current = sid == current_id
        # Hide empty sessions that are NOT the current one (fresh "new chat" placeholder).
        if not msgs and not is_current:
            continue
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
            "isCurrent": is_current,
        })
    result.sort(key=lambda s: s["createdAt"], reverse=True)
    return result

US_INDICES = [
    ("다우존스", "DJI@DJI"),
    ("S&P 500", "SPI@SPX"),
    ("나스닥", "NAS@IXIC"),
    ("필라델피아 반도체", "SOX@SOX"),
]

_us_analysis_cache = {"highlights": [], "summary": "", "_date": ""}

def generate_us_market_analysis(indices, us_now):
    today = us_now.strftime("%Y-%m-%d")
    if _us_analysis_cache["_date"] == today and _us_analysis_cache["summary"]:
        return _us_analysis_cache["highlights"], _us_analysis_cache["summary"]

    idx_text = ""
    for idx in indices:
        sign = "+" if idx["rate"] > 0 else ""
        idx_text += f"{idx['name']}: {idx['value']:,.2f} ({sign}{idx['rate']:.2f}%)\n"

    news = get_us_market_news()
    news_text = ""
    if news:
        news_text = "\n".join(f"- {a['title']}" for a in news[:5])

    prompt = (
        f"미국 증시 마감 분석입니다.\n\n"
        f"[지수]\n{idx_text}\n"
        f"[최신 뉴스]\n{news_text}\n\n"
        f"위 정보를 바탕으로 JSON만 응답하세요:\n"
        f'{{"summary":"2~3문장 요약 (시장 흐름과 주요 이슈)","highlights":["하이라이트1","하이라이트2","하이라이트3"]}}\n'
        f"규칙: summary는 간결하게, highlights는 3개, 한국어로 작성."
    )
    messages = [{"role": "user", "content": prompt}]
    try:
        result = call_llm(messages)
        content = result.get("reply", "")
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            highlights = parsed.get("highlights", [])[:3]
            summary = parsed.get("summary", "")
            if highlights and summary:
                _us_analysis_cache.update({"highlights": highlights, "summary": summary, "_date": today})
                return highlights, summary
    except Exception as e:
        print(f"[generate_us_market_analysis] LLM error: {e}", flush=True)

    highlights = _fallback_highlights(indices)
    summary = _fallback_summary(indices)
    _us_analysis_cache.update({"highlights": highlights, "summary": summary, "_date": today})
    return highlights, summary


def _fallback_highlights(indices):
    items = []
    for idx in indices:
        if idx["rate"] > 0.5:
            items.append(f"{idx['name']} {idx['rate']:+.2f}% 상승")
        elif idx["rate"] < -0.5:
            items.append(f"{idx['name']} {idx['rate']:+.2f}% 하락")
    return items[:3] or ["지수 변동 없음"]


def _fallback_summary(indices):
    ups = sum(1 for i in indices if i["rate"] > 0)
    downs = sum(1 for i in indices if i["rate"] < 0)
    nasdaq = next((i for i in indices if "나스닥" in i["name"]), None)
    if ups == 3:
        desc = "3대 지수 동반 상승"
    elif downs == 3:
        desc = "3대 지수 동반 하락"
    else:
        desc = "3대 지수 혼조세"
    parts = [desc + " 마감."]
    if nasdaq:
        sign = "상승" if nasdaq["rate"] > 0 else "하락"
        parts.append(f"나스닥 {abs(nasdaq['rate']):.2f}% {sign}.")
    return " ".join(parts)

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
    
    highlights, summary = generate_us_market_analysis(result["indices"], us_now)
    result["highlights"] = highlights
    result["summary"] = summary
    
    return result

def load_us_market():
    try:
        return fetch_us_market_realtime()
    except Exception:
        return {"marketStatus": "closed"}

def fetch_us_market_news(limit=5):
    """미국증시 관련 최신 뉴스를 가져온다 (24시간 이내만)."""
    try:
        query = "미국증시 뉴욕증시"
        encoded = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=ko&gl=KR&ceid=KR:ko"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        items = root.findall(".//item")
        articles = []
        cutoff = time.time() - 86400  # 24시간 전
        for item in items[:limit * 5]:
            title_el = item.find("title")
            link_el = item.find("link")
            source_el = item.find("source")
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
                if pub_ts < cutoff:
                    continue
                articles.append({
                    "title": re.sub(r"\s+", " ", title_el.text).strip(),
                    "url": link_el.text.strip() if link_el is not None and link_el.text else "#",
                    "source": source_el.text.strip() if source_el is not None and source_el.text else "",
                    "pubDate": pub_str,
                    "_ts": pub_ts,
                })
        articles.sort(key=lambda x: x.get("_ts", 0), reverse=True)
        for a in articles:
            a.pop("_ts", None)
        return articles[:limit]
    except Exception as exc:
        print(f"[fetch_us_market_news] error: {exc}", flush=True)
        return []

_us_market_news_cache = []
_us_market_news_cache_time = 0

def get_us_market_news():
    """캐시된 미국증시 뉴스를 가져온다 (5분 캐시)."""
    global _us_market_news_cache, _us_market_news_cache_time
    now = time.time()
    if _us_market_news_cache and now - _us_market_news_cache_time < 300:
        return _us_market_news_cache
    _us_market_news_cache = fetch_us_market_news(limit=5)
    _us_market_news_cache_time = now
    return _us_market_news_cache

def fetch_kr_market_news(limit=5):
    """한국증시 관련 최신 뉴스를 가져온다."""
    try:
        queries = ["코스피 코스닥 오늘", "코스피 하락 급락", "외국인 매도 코스피", "코스피 전망"]
        all_articles = []
        seen_titles = set()
        for query in queries:
            encoded = urllib.parse.quote(query)
            url = f"https://news.google.com/rss/search?q={encoded}&hl=ko&gl=KR&ceid=KR:ko"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    raw = resp.read()
                root = ET.fromstring(raw)
                items = root.findall(".//item")
                for item in items[:10]:
                    title_el = item.find("title")
                    link_el = item.find("link")
                    source_el = item.find("source")
                    pub_el = item.find("pubDate")
                    if title_el is not None and title_el.text:
                        title = re.sub(r"\s+", " ", title_el.text).strip()
                        if title in seen_titles:
                            continue
                        seen_titles.add(title)
                        pub_str = pub_el.text.strip() if pub_el is not None and pub_el.text else ""
                        pub_ts = 0
                        if pub_str:
                            try:
                                from email.utils import parsedate_to_datetime
                                pub_ts = parsedate_to_datetime(pub_str).timestamp()
                            except Exception:
                                pass
                        all_articles.append({
                            "title": title,
                            "url": link_el.text.strip() if link_el is not None and link_el.text else "#",
                            "source": source_el.text.strip() if source_el is not None and source_el.text else "",
                            "pubDate": pub_str,
                            "_ts": pub_ts,
                        })
            except Exception as e:
                print(f"[fetch_kr_market_news] query '{query}' error: {e}", flush=True)
        all_articles.sort(key=lambda x: x.get("_ts", 0), reverse=True)
        for a in all_articles:
            a.pop("_ts", None)
        return all_articles[:limit]
    except Exception as exc:
        print(f"[fetch_kr_market_news] error: {exc}", flush=True)
        return []

_kr_market_news_cache = []
_kr_market_news_cache_time = 0

def get_kr_market_news():
    """캐시된 한국증시 뉴스를 가져온다 (5분 캐시)."""
    global _kr_market_news_cache, _kr_market_news_cache_time
    now = time.time()
    if _kr_market_news_cache and now - _kr_market_news_cache_time < 300:
        return _kr_market_news_cache
    _kr_market_news_cache = fetch_kr_market_news(limit=5)
    _kr_market_news_cache_time = now
    return _kr_market_news_cache

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
        result["marketStatus"] = "pre_open"
        result["date"] = now.strftime("%Y-%m-%d")
    elif current_time >= market_open and current_time <= market_close:
        result["marketStatus"] = "open"
        result["date"] = now.strftime("%Y-%m-%d")
    else:
        result["marketStatus"] = "closed"
        result["date"] = now.strftime("%Y-%m-%d")

    kr_news = get_kr_market_news()
    result["highlights"] = [a["title"] for a in kr_news[:3]] if kr_news else []
    result["summary"] = _kr_summary_from_indices(result["indices"])
    
    return result


def _kr_summary_from_indices(indices):
    if not indices:
        return ""
    parts = []
    ups = sum(1 for i in indices if i["rate"] > 0)
    downs = sum(1 for i in indices if i["rate"] < 0)
    if ups == len(indices):
        parts.append("코스피·코스닥 동반 상승")
    elif downs == len(indices):
        parts.append("코스피·코스닥 동반 하락")
    else:
        parts.append("코스피·코스닥 혼조세")
    for idx in indices:
        sign = "상승" if idx["rate"] > 0 else "하락" if idx["rate"] < 0 else "보합"
        parts.append(f"{idx['name']} {abs(idx['rate']):.2f}% {sign}")
    return ". ".join(parts) + "."

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
            sign = "▲" if idx["rate"] > 0 else "▼" if idx["rate"] < 0 else ""
            lines.append(f"• {idx['name']}: {idx['value']:,.2f} {sign}{abs(idx['rate']):.2f}%")
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
            sign = "▲" if idx["rate"] > 0 else "▼" if idx["rate"] < 0 else ""
            lines.append(f"• {idx['name']}: {idx['value']:,.2f} {sign}{abs(idx['rate']):.2f}%")
    return "\n".join(lines)

def build_chat_context(portfolio, news):
    lines = []
    summary = portfolio["summary"]
    lines.append(f"총평가 {summary['currentValue']:,.0f}원 수익 {summary['profit']:+,.0f}원({summary['profitRate']:+.1f}%)")
    
    # 종목별 뉴스 컨텍스트
    if news:
        news_lines = ["[종목별 최신 뉴스]"]
        for item in news[:10]:
            name = item.get("name", "")
            articles = item.get("articles", [])
            if articles:
                news_lines.append(f"• {name}:")
                for a in articles[:2]:
                    title = a.get("title", "")
                    if title:
                        news_lines.append(f"  - {title}")
        if len(news_lines) > 1:
            lines.extend(news_lines)
    
    if portfolio.get("holdings"):
        for h in portfolio["holdings"]:
            if not h.get("error"):
                code = h.get("code", "")
                name = h["name"]
                lines.append(f"• {name}: {h['quantity']}주 {h['currentPrice']:,.0f}원 ({h['profitRate']:+.1f}%)")
                
                # 기술적 분석 데이터 추가
                try:
                    tech = calc_tech_indicators(code)
                    indicators = tech.get("indicators", {})
                    signals = tech.get("signals", [])
                    signal_score = tech.get("signalScore", 0)
                    tech_signal = tech.get("techSignal", "hold")
                    
                    signal_labels = {
                        'strong_buy': '강력매수', 'buy': '매수', 'hold': '관망',
                        'sell': '매도', 'strong_sell': '강력매도'
                    }
                    
                    tech_lines = [f"  [기술적분석] 종합: {signal_labels.get(tech_signal, '관망')} (점수: {signal_score})"]
                    
                    if indicators.get("rsi14") is not None:
                        rsi = indicators["rsi14"]
                        rsi_status = "과매수" if rsi > 70 else "강세" if rsi > 60 else "약세" if rsi < 40 else "과매도" if rsi < 30 else "중립"
                        tech_lines.append(f"    RSI(14): {rsi:.1f} ({rsi_status})")
                    
                    if indicators.get("macd"):
                        macd = indicators["macd"]
                        macd_signal = "상승모멘텀" if macd.get("macd", 0) > macd.get("signal", 0) else "하락모멘텀"
                        tech_lines.append(f"    MACD: {macd.get('macd', 0):.0f} (시그널: {macd.get('signal', 0):.0f}) - {macd_signal}")
                    
                    if indicators.get("bollinger"):
                        bb = indicators["bollinger"]
                        if h.get("currentPrice") and bb.get("upper") and bb.get("lower"):
                            bb_pos = (h["currentPrice"] - bb["lower"]) / (bb["upper"] - bb["lower"]) * 100
                            tech_lines.append(f"    볼린저밴드: 위치 {bb_pos:.0f}%")
                    
                    if indicators.get("stochastic"):
                        stoch = indicators["stochastic"]
                        stoch_status = "과매수" if stoch.get("k", 0) > 80 else "과매도" if stoch.get("k", 0) < 20 else "중립"
                        tech_lines.append(f"    스토캐스틱: %K {stoch.get('k', 0):.1f} (%D {stoch.get('d', 0):.1f}) - {stoch_status}")
                    
                    if indicators.get("ma5") and indicators.get("ma20") and indicators.get("ma60"):
                        tech_lines.append(f"    이동평균선: MA5 {indicators['ma5']:,.0f} / MA20 {indicators['ma20']:,.0f} / MA60 {indicators['ma60']:,.0f}")
                    
                    if signals:
                        tech_lines.append(f"    시그널: {', '.join(signals[:5])}")
                    
                    lines.extend(tech_lines)
                except Exception:
                    pass
    
    # 관심종목 기술적 분석 데이터 추가
    if portfolio.get("watchlist"):
        watchlist_lines = ["\n[관심종목 기술적 분석]"]
        for w in portfolio["watchlist"]:
            if not w.get("error"):
                code = w.get("code", "")
                name = w["name"]
                current_price = w.get("currentPrice")
                if current_price:
                    watchlist_lines.append(f"• {name} ({code}): {current_price:,.0f}원")
                    
                    try:
                        tech = calc_tech_indicators(code)
                        indicators = tech.get("indicators", {})
                        signals = tech.get("signals", [])
                        signal_score = tech.get("signalScore", 0)
                        tech_signal = tech.get("techSignal", "hold")
                        
                        signal_labels = {
                            'strong_buy': '강력매수', 'buy': '매수', 'hold': '관망',
                            'sell': '매도', 'strong_sell': '강력매도'
                        }
                        
                        watchlist_lines.append(f"  [기술적분석] 종합: {signal_labels.get(tech_signal, '관망')} (점수: {signal_score})")
                        
                        if indicators.get("rsi14") is not None:
                            rsi = indicators["rsi14"]
                            rsi_status = "과매수" if rsi > 70 else "강세" if rsi > 60 else "약세" if rsi < 40 else "과매도" if rsi < 30 else "중립"
                            watchlist_lines.append(f"    RSI(14): {rsi:.1f} ({rsi_status})")
                        
                        if indicators.get("macd"):
                            macd = indicators["macd"]
                            macd_signal = "상승모멘텀" if macd.get("macd", 0) > macd.get("signal", 0) else "하락모멘텀"
                            watchlist_lines.append(f"    MACD: {macd.get('macd', 0):.0f} (시그널: {macd.get('signal', 0):.0f}) - {macd_signal}")
                        
                        if indicators.get("bollinger"):
                            bb = indicators["bollinger"]
                            if current_price and bb.get("upper") and bb.get("lower"):
                                bb_pos = (current_price - bb["lower"]) / (bb["upper"] - bb["lower"]) * 100
                                watchlist_lines.append(f"    볼린저밴드: 위치 {bb_pos:.0f}%")
                        
                        if indicators.get("stochastic"):
                            stoch = indicators["stochastic"]
                            stoch_status = "과매수" if stoch.get("k", 0) > 80 else "과매도" if stoch.get("k", 0) < 20 else "중립"
                            watchlist_lines.append(f"    스토캐스틱: %K {stoch.get('k', 0):.1f} (%D {stoch.get('d', 0):.1f}) - {stoch_status}")
                        
                        if indicators.get("ma5") and indicators.get("ma20") and indicators.get("ma60"):
                            watchlist_lines.append(f"    이동평균선: MA5 {indicators['ma5']:,.0f} / MA20 {indicators['ma20']:,.0f} / MA60 {indicators['ma60']:,.0f}")
                        
                        if current_price and indicators.get("ma20"):
                            price_vs_ma20 = (current_price - indicators["ma20"]) / indicators["ma20"] * 100
                            watchlist_lines.append(f"    MA20 대비: {price_vs_ma20:+.1f}%")
                        
                        if signals:
                            watchlist_lines.append(f"    시그널: {', '.join(signals[:5])}")
                        
                    except Exception:
                        pass
        lines.extend(watchlist_lines)
    
    return "\n".join(lines)


def _strip_thinking_artifacts(text):
    """Remove model thinking/reasoning artifacts that leak into content."""
    if not text:
        return text
    lines = text.split("\n")
    cleaned = []
    skip_patterns = (
        "사용자가 ", "먼저 ", "아니, ", "그 다음 ", "Wait, ", "먼저 ",
        "근데 ", "아, ", "음, ", "자, ", "그래서 ", "일단 ",
        "이렇게?", "이렇게 구조화", "이렇게 해야지", "이렇게 하자",
        "정리하면", "생각해보자", "분석해보니", "우선,",
        "요약하면", "결론적으로", "핵심은",
    )
    for line in lines:
        stripped = line.strip()
        if any(stripped.startswith(p) for p in skip_patterns):
            continue
        cleaned.append(line)
    result = "\n".join(cleaned).strip()
    if not result:
        return text
    return result


def _extract_final_answer(reply):
    """Extract final_answer from JSON-structured LLM response robustly.

    Never returns reasoning/intermediate content to the user.
    """
    if not reply:
        return ""

    candidate = reply.strip()

    # 1) Try parsing the whole reply as JSON (well-formed case).
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict) and "final_answer" in parsed:
            fa = parsed.get("final_answer")
            if isinstance(fa, str) and fa.strip():
                return _strip_thinking_artifacts(fa).strip()
            if isinstance(fa, list):
                return _strip_thinking_artifacts("\n".join(str(x) for x in fa)).strip()
    except (json.JSONDecodeError, ValueError):
        pass

    # 2) Brace-matching scan for the outermost object with final_answer.
    stack = []
    for i, ch in enumerate(candidate):
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            if stack:
                continue
            block = candidate[start: i + 1]
            try:
                parsed = json.loads(block)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict) and "final_answer" in parsed:
                fa = parsed.get("final_answer")
                if isinstance(fa, str) and fa.strip():
                    return _strip_thinking_artifacts(fa).strip()
                if isinstance(fa, list):
                    return _strip_thinking_artifacts("\n".join(str(x) for x in fa)).strip()

    # 3) Fallback: strip thinking artifacts from the raw reply.
    return _strip_thinking_artifacts(reply).strip()


def chat_from_zai(messages):
    payload = {
        "model": "glm-5",
        "messages": messages,
        "thinking": {"type": "disabled"},
        "temperature": 0.7,
        "max_tokens": 2000,
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
        return {"reply": _strip_thinking_artifacts(content).strip(), "_source": "zai"}
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
        return {"reply": _strip_thinking_artifacts(content).strip(), "_source": "openrouter"}
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
        return {"reply": _strip_thinking_artifacts(content).strip(), "_source": "nous"}
    except Exception as exc:
        return {"error": str(exc)}

def chat_from_opencode(messages):
    if not OPENCODE_KEY:
        return {"error": "OPENCODE_KEY not set"}
    payload = {
        "model": OPENCODE_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 2500,
        "thinking": {"type": "disabled"},
    }
    headers = {
        "Authorization": f"Bearer {OPENCODE_KEY}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        OPENCODE_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
        result = json.loads(raw)
        choice = result["choices"][0]
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        finish = choice.get("finish_reason")
        # GLM-5.2 sometimes emits only reasoning_content when reasoning isn't disabled.
        # Never expose reasoning_content; require a real content payload.
        if not content:
            return {"error": f"empty content (finish={finish})"}
        return {"reply": _strip_thinking_artifacts(content).strip(), "_source": "opencode"}
    except Exception as exc:
        return {"error": str(exc)}

def call_llm(messages):
    # Primary: OpenCode GLM-5.2 (JSON 응답, thinking disabled)
    if OPENCODE_KEY:
        result = chat_from_opencode(messages)
        if "error" not in result:
            return result
        print(f"[call_llm] opencode failed: {result.get('error')}", file=sys.stderr)
    # fallback: zai glm-5
    if ZAI_KEY:
        payload = {
            "model": "glm-5",
            "messages": messages,
            "thinking": {"type": "disabled"},
            "temperature": 0.7,
            "max_tokens": 2500,
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
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
            result = json.loads(raw)
            content = result["choices"][0]["message"]["content"]
            if content:
                return {"reply": _strip_thinking_artifacts(content).strip(), "_source": "zai"}
            print("[call_llm] zai empty content", file=sys.stderr)
        except Exception as exc:
            print(f"[call_llm] zai failed: {exc}", file=sys.stderr)
    # Secondary: OpenCode GLM-5.1 (JSON 응답)
    if OPENCODE_KEY:
        result = chat_from_opencode(messages)
        if "error" not in result:
            return result
        print(f"[call_llm] opencode failed: {result.get('error')}", file=sys.stderr)
    # fallback: nous
    for m in NOUS_MODELS:
        result = chat_from_nous(messages, model=m)
        if "error" not in result:
            return result
        print(f"[call_llm] nous({m}) failed: {result.get('error')}", file=sys.stderr)
    if OPENROUTER_KEY:
        for m in OPENROUTER_MODELS:
            result = chat_from_openrouter(messages, model=m)
            if "error" not in result:
                return result
            print(f"[call_llm] openrouter({m}) failed: {result.get('error')}", file=sys.stderr)
    print("[call_llm] all providers exhausted", file=sys.stderr)
    return {"reply": "죄송합니다. 현재 AI 서비스에 일시적인 문제가 있습니다. 잠시 후 다시 시도해 주세요.", "_source": "fallback"}

MCP_SEARCH_URL = "https://api.z.ai/api/mcp/web_search_prime/mcp"
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "").strip()

# Brave Search rate limit: 1 request per second (Free plan)
# Track last call timestamp globally to enforce >=1.1s gap between requests.
_BRAVE_MIN_INTERVAL = 1.1
_last_brave_call_ts = 0.0

# Short-lived in-memory search cache (per-instance, Fluid Compute reuse)
# Reduces Brave calls for repeated queries within a session.
_search_cache: dict[str, dict] = {}
_SEARCH_CACHE_TTL = 60  # seconds

# Portfolio cache: avoids repeated fetch_quote() calls during chat + polling overlap.
# Background /api/portfolio polling runs every 10s; chat also calls build_portfolio(),
# so we cache for 3s to dedupe without serving stale data.
_PORTFOLIO_CACHE: dict | None = None
_PORTFOLIO_CACHE_TS: float = 0.0
_PORTFOLIO_CACHE_TTL: float = 3.0


def _enforce_brave_rate_limit():
    """Block until at least _BRAVE_MIN_INTERVAL seconds passed since the last Brave call."""
    global _last_brave_call_ts
    now = time.time()
    wait = _BRAVE_MIN_INTERVAL - (now - _last_brave_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_brave_call_ts = time.time()


def _cache_get(key):
    entry = _search_cache.get(key)
    if not entry:
        return None
    if time.time() - entry.get("_ts", 0) > _SEARCH_CACHE_TTL:
        _search_cache.pop(key, None)
        return None
    return entry.get("results")


def _cache_put(key, results):
    _search_cache[key] = {"results": results, "_ts": time.time()}
    # Bound cache size to avoid memory growth on long-lived instances
    if len(_search_cache) > 50:
        oldest = sorted(_search_cache.items(), key=lambda kv: kv[1].get("_ts", 0))
        for k, _ in oldest[:10]:
            _search_cache.pop(k, None)

def is_irrelevant_result(url, text):
    """Filter out obviously irrelevant results."""
    url_lower = url.lower()
    text_lower = text.lower()
    # Irrelevant domains (messaging, social, generic)
    irrelevant_domains = [
        "whatsapp.com", "wa.me", "web.whatsapp.com",
        "facebook.com", "fb.com", "instagram.com", "twitter.com", "x.com",
        "tiktok.com", "youtube.com", "netflix.com",
        "apple.com", "microsoft.com", "google.com/support",
        "amazon.com", "ebay.com",
    ]
    # Korean / global community forums and UGC sites — personal opinions,
    # rumors, and unsourced discussion. Excluded for factual reliability.
    community_domains = [
        # 한국 커뮤니티
        "ruliweb.com", "dcinside.com", "fmkorea.com", "ilbe.com",
        "theqoo.net", "inven.co.kr", "natepan.com", "pan.nate.com",
        "82cook.com", "todayhumor.co.kr", "dogdrip.net", "ygosu.com",
        "slrclub.com", "instiz.net", "bobaedream.co.kr", "clien.net",
        "hithub.kr", "hwayon.kr", "mmovo.com", "tokbbang.com",
        "babotemps.com", "eaty.com", "teamblind.co.kr", "teamblind.com",
        "qoou.net", "msholic.net", "wigo.kr", "sosg.net", "item.co.kr",
        # 글로벌 UGC / 포럼
        "reddit.com", "quora.com", "4chan.org", "discord.com",
        "medium.com", "substack.com", " disq.us", "tumblr.com",
        "pinterest.com", "vk.com", "weibo.com", "xiaohongshu.com",
        "douban.com", "zhihu.com", "threads.net",
    ]
    for domain in irrelevant_domains + community_domains:
        if domain in url_lower:
            return True
    # URL path patterns typical of community/bbs/forum software
    community_patterns = [
        "/community/", "/bbs/", "/board/", "/forum/",
        "/view.php?id=", "/zboard.php", "/read.php?board=",
        "/r/",  # reddit subreddit paths
    ]
    for pat in community_patterns:
        if pat in url_lower:
            return True
    # Skip generic encyclopedia/dictionary pages unrelated to stocks
    if "wikipedia.org" in url_lower:
        stock_kw = ["stock", "주식", "kospi", "kosdaq", "vi", "volatility",
                     "finance", "market", "invest", "trading", "배당",
                     "상장", "공매도", "액면", "변동", "호재", "악재"]
        if not any(kw in url_lower + text_lower for kw in stock_kw):
            return True
    # Skip results with no Korean/financial content
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
        print(f"[search_web] Brave web: {len(results)} results (total)")
    except urllib.error.HTTPError as e:
        print(f"[search_web] Brave web HTTP error: {e.code} {e.reason}")
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
    next_day = now_kst + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][next_day.weekday()]
    return next_day.strftime('%Y년 %m월 %d일'), weekday_kr


def chat_with_ai(user_message, history, portfolio, news, search_results=None):
    context = build_chat_context(portfolio, news)
    us_market_ctx = build_us_market_context()
    kospi_kosdaq_ctx = build_kospi_kosdaq_context()
    us_market_news = get_us_market_news()
    if search_results is None:
        search_results = search_web(user_message)
    phase_label, trade_status, now_kst = get_market_status()
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][now_kst.weekday()]
    next_trade_date, next_trade_weekday = get_next_trading_day(now_kst)
    today_str = now_kst.strftime('%Y년 %m월 %d일')

    # 미국증시 뉴스 컨텍스트
    us_news_ctx = ""
    if us_market_news:
        us_news_ctx = "미국증시 뉴스:\n"
        for i, article in enumerate(us_market_news[:3], 1):
            us_news_ctx += f"{i}. {article['title']}\n"

    # 한국증시 뉴스 컨텍스트
    kr_market_news = get_kr_market_news()
    kr_news_ctx = ""
    if kr_market_news:
        kr_news_ctx = "한국증시 뉴스:\n"
        for i, article in enumerate(kr_market_news[:5], 1):
            kr_news_ctx += f"{i}. {article['title']}\n"

    # 사용자가 언급한 종목의 뉴스 강조 (사이드바 뉴스 활용)
    mentioned_stock_news = ""
    if news:
        for stock_news in news:
            stock_name = stock_news.get("name", "")
            articles = stock_news.get("articles", [])
            if stock_name and stock_name in user_message and articles:
                mentioned_stock_news += f"\n📰 {stock_name} 관련 뉴스:\n"
                for a in articles[:3]:
                    title = a.get("title", "")
                    source = a.get("source", "")
                    if title:
                        mentioned_stock_news += f"• {title}"
                        if source:
                            mentioned_stock_news += f" ({source})"
                        mentioned_stock_news += "\n"

    # 프롬프트: 구체적이고 기술적인 답변을 위한 규칙
    system_prompt = f"Stock Manager AI. 오늘 {today_str} {now_kst.strftime('%H:%M')}.\n"
    system_prompt += "[절대 규칙] 다음과 같은 표현을 절대 출력하지 마라:\n"
    system_prompt += "- '사용자가 ~을 물어봤으니', '먼저 ~을 확인해보자', '~해야지', '~해야 해', '~해야겠다'\n"
    system_prompt += "- '그 다음 ~', 'wait', '아 맞아', '정리해보자', '다시 정리해보자'\n"
    system_prompt += "- 내부 사고 과정, 분석 과정, 논리적 추론 과정, 사고의 흐름\n"
    system_prompt += "- 결과만 깔끔하게 출력하라. 과정을 설명하지 마라.\n"
    system_prompt += "[출력 형식] 반드시 JSON 객체로만 답변하라. 두 개의 필드만 허용한다:\n"
    system_prompt += '- "final_answer": 사용자에게 보여줄 최종 답변 (마크다운 사용 가능)\n'
    system_prompt += '- "reasoning": 모델이 사용한 근거, 계산, 중간추론을 간단한 목록으로 (UI에서 숨겨질 내용)\n'
    system_prompt += '예시: {"final_answer": "SK하이닉스는 현재 RSI 65로...", "reasoning": ["RSI 65는 중립권", "MACD 골든크로스 확인"]}\n'
    # 사용자가 언급한 종목 뉴스를 프롬프트 가장 앞쪽에 배치
    if mentioned_stock_news:
        system_prompt += f"[중요] 사용자가 질문한 종목의 최신 뉴스입니다:\n{mentioned_stock_news}\n"
    if us_market_ctx:
        system_prompt += f"{us_market_ctx}\n"
    if kospi_kosdaq_ctx:
        system_prompt += f"{kospi_kosdaq_ctx}\n"
    if kr_news_ctx:
        system_prompt += f"{kr_news_ctx}\n"
    if us_news_ctx:
        system_prompt += f"{us_news_ctx}\n"
    system_prompt += f"{context}\n"
    system_prompt += "## 답변 스타일 규칙\n"
    system_prompt += "- 기술적 지표(RSI, MACD, 볼린저밴드, 스토캐스틱, 이동평균선 등)를 구체적인 수치와 함께 반드시 인용할 것\n"
    system_prompt += "- 현재가, 전일종가, 등락률, 거래량 등 수치 데이터를 근거로 제시할 것\n"
    system_prompt += "- 뉴스 내용을 인용할 때는 출처와 함께 구체적으로 언급할 것\n"
    system_prompt += "- 결론은 2~3문단으로 작성하고, 각 문단마다 다른 관점(기술적/뉴스/시장심리)에서 분석할 것\n"
    system_prompt += "- 매매 시그널(매수/매도/관망)을 명확히 제시하고, 목표가와 손절가를 수치로 제시할 것\n"
    system_prompt += "- 불확실성은 '~할 수 있습니다', '~가능성이 있습니다'와 같이 표현할 것\n"
    system_prompt += "- 한문단으로 끝내지 말고, 구조화된 답변(기술적 분석, 뉴스 영향, 시장 심리, 종합 판단)을 제공할 것\n\n"

    messages = [{"role": "system", "content": system_prompt}]
    sliced = history[-5:] if history else []
    for h in sliced:
        messages.append({"role": h["role"], "content": h["content"][:150]})
    messages.append({"role": "user", "content": user_message})
    result = call_llm(messages)
    reply = result.get("reply") or ""

    # JSON 응답에서 final_answer 추출 (reasoning 노출 방지)
    reply = _extract_final_answer(reply)

    reply = re.sub(r'\n{3,}', '\n\n', reply)
    reply = re.sub(r'\n*📚\s*출처[:：][\s\S]*$', '', reply).rstrip()
    h_refs = re.findall(r'\[H(\d+)\]', reply) if sliced else []
    has_urls = us_market_news and any(a.get("url") for a in us_market_news[:3])
    if has_urls or h_refs:
        reply += "\n\n📚 출처:\n"
        if has_urls:
            for i, article in enumerate(us_market_news[:3], 1):
                if article.get("url"):
                    reply += f"[{i}] {article['url']}\n"
    reply = reply.rstrip()
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
    now_ms = int(time.time() * 1000)
    sid = f"sess_{now_ms}"
    kst = timezone(timedelta(hours=9))
    now_dt = datetime.fromtimestamp(now_ms / 1000, tz=kst)
    data = load_chat_sessions()
    data.setdefault("sessions", {})[sid] = {
        "id": sid, "createdAt": now_ms,
        "date": now_dt.strftime("%Y-%m-%d"),
        "time": now_dt.strftime("%H:%M"),
        "messages": [],
    }
    data["current"] = sid
    ok = save_chat_sessions(data)
    return Response(
        json.dumps({"sessionId": sid, "saved": ok}, ensure_ascii=False),
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

@app.route("/api/chat/session/<session_id>", methods=["DELETE", "OPTIONS"])
def api_delete_chat_session(session_id):
    if request.method == "OPTIONS":
        resp = Response()
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp
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
        # Resolve the target session ONCE so user msg + assistant msg land in the same session.
        sid, _ = get_or_create_session(now_ms)
        history = get_session_messages(sid)
        history.append({"role": "user", "content": message, "timestamp": now_ms})
        add_message_to_session("user", message, timestamp_ms=now_ms)
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
        add_message_to_session("assistant", reply, timestamp_ms=now_ms)
        updated_history = get_session_messages(sid)
        yield "event: result\ndata: " + json.dumps({"reply": reply, "history": updated_history, "sessionId": sid}, ensure_ascii=False) + "\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "X-Accel-Buffering": "no",
        },
    )
