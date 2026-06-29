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
    result = {
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
        return fetch_us_market_realtime()
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
            sign = "+" if idx["change"] > 0 else ""
            lines.append(f"• {idx['name']}: {idx['value']:,.2f} ({sign}{idx['change']:.2f}%)")
    if data.get("summary"):
        lines.append(f"• 요약: {data['summary']}")
    if data.get("highlights"):
        lines.append("• 주요 특징:")
        for h in data["highlights"]:
            lines.append(f"  - {h}")
    return "\n".join(lines)

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
        "max_tokens": 4000,
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
    """Brave Search API — news + web 검색.

    Brave Free plan allows only 1 request per second. We enforce a global
    ≥1.1s gap before each Brave HTTP call (rate limiter) and sleep 1.1s
    between the news and web calls so they never collide.
    """
    results = []
    seen_urls = set()
    headers = {
        "X-Subscription-Token": BRAVE_API_KEY,
        "Accept": "application/json",
    }
    # 1) News search (최근 7일)
    try:
        _enforce_brave_rate_limit()
        encoded = urllib.parse.quote(query[:200])
        url = f"https://api.search.brave.com/res/v1/news/search?q={encoded}&freshness=pw&count=5"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for r in data.get("results", []):
            r_url = r.get("url", "")
            if r_url and r_url not in seen_urls and not is_irrelevant_result(r_url, r.get("title", "")):
                seen_urls.add(r_url)
                desc = r.get("description", "") or r.get("title", "")
                results.append({"text": desc[:2000], "url": r_url})
        print(f"[search_web] Brave news: {len(results)} results")
    except urllib.error.HTTPError as e:
        print(f"[search_web] Brave news HTTP error: {e.code} {e.reason}")
    except Exception as e:
        print(f"[search_web] Brave news error: {e}")
    # 2) Web search (최근 7일) — always wait ≥1.1s after the news call
    try:
        _enforce_brave_rate_limit()
        encoded = urllib.parse.quote(query[:200])
        url = f"https://api.search.brave.com/res/v1/web/search?q={encoded}&freshness=pw&count=5"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
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
    """Brave Search + DuckDuckGo 폴백. 결과는 60초간 메모리 캐싱."""
    if not query or not query.strip():
        return []
    cache_key = query.strip().lower()[:200]
    cached = _cache_get(cache_key)
    if cached is not None:
        print(f"[search_web] cache hit ({len(cached)} results)")
        return cached
    all_results = []
    seen_urls = set()

    def add_results(results):
        for r in results:
            url = r.get("url", "")
            if url and url not in seen_urls and not is_irrelevant_result(url, r.get("text", "")):
                seen_urls.add(url)
                all_results.append(r)

    # 1) Brave Search (news + web) — internally enforces 1.1s gap
    try:
        brave_results = search_web_brave(query)
        add_results(brave_results)
    except Exception as e:
        print(f"[search_web] Brave search error: {e}")

    # 2) DuckDuckGo 폴백 (Brave 결과가 부족할 때)
    if len(all_results) < 3:
        try:
            ddg_results = search_web_ddg(query)
            add_results(ddg_results)
        except Exception as e:
            print(f"[search_web] DuckDuckGo error: {e}")

    final = all_results[:8]
    _cache_put(cache_key, final)
    return final

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
    if search_results is None:
        search_results = search_web(user_message)
    phase_label, trade_status, now_kst = get_market_status()
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][now_kst.weekday()]
    next_trade_date, next_trade_weekday = get_next_trading_day(now_kst)
    today_str = now_kst.strftime('%Y년 %m월 %d일')
    today_iso = now_kst.strftime('%Y-%m-%d')

    # 주식/경제 관련 검색 결과만 필터링
    filtered_search = []
    if search_results:
        for r in search_results:
            text = r.get("text", "")
            stock_kw = ["주식", "증시", "코스피", "코스닥", "종목", "투자", "매매",
                        "호실적", "실적", "전망", "목표가", "상승", "하락", "급락",
                        "반도체", "메모리", "HBM", "전자", "차", "자동차",
                        "로봇", "AI", "인공지능", "배당", "수익", "손실",
                        "분석", "리포트", "애널리스트", "순매수", "기관", "외국인"]
            if any(kw in text for kw in stock_kw):
                filtered_search.append(r)
    if not filtered_search:
        filtered_search = search_results[:5] if search_results else []

    system_prompt = (
        "당신은 전문 주식 투자 어드바이저 'Stock Manager AI'입니다. "
        "사용자의 포트폴리오 정보와 시장 데이터를 바탕으로 투자 조언을 제공합니다.\n\n"
        f"📅 오늘 날짜: {today_str} ({weekday_kr}요일)\n"
        f"🕒 현재 시각 (KST): {now_kst.strftime('%H시 %M분')}\n"
        f"📈 현재 장 상태: **{phase_label}** — {trade_status}\n"
        f"📅 다음 거래일: {next_trade_date} ({next_trade_weekday}요일) — 프리마켓 08:00 개시\n\n"
        "【 절대 규칙 — 위반 금지 】\n"
        f"1. 오늘은 {today_iso}({weekday_kr}요일)입니다. 토요일/일요일은 장이 열리지 않습니다. "
        f"따라서 다음 거래일은 {next_trade_date}({next_trade_weekday})입니다. "
        "절대 토요일이나 일요일에 프리마켓/메인마켓이 있다고 말하지 마세요.\n"
        "2. ⚠️ 할루네이션(실제 없는 사실 만들어내기) 엄격히 금지: "
        "실적 발표일, 목표가, 기업 전망 등 정보가 검색 결과에 없으면 절대 만들지 마세요. "
        "예: '마이크론 실적 발표일'이 검색 결과에 없으면 '실적 발표일은 확인이 필요합니다'라고 답변하세요.\n"
        "3. ⚠️ 출처 인용 규칙: 아래 검색 결과에서 실제로 관련 있는 것만 인용하세요. "
        "검색 결과와 무관한 URL을 출처로 만들지 마세요. "
        "검색 결과가 없으면 출처 섹션을 생략하고 '최신 실시간 데이터 확인이 필요합니다'라고 답변하세요.\n"
        "4. 답변에 사용하는 모든 수치(주가, 수익률, 비율 등)는 반드시 위 포트폴리오 데이터에서 가져오세요. "
        "절대 상상으로 수치를 만들지 마세요.\n\n"
        "【 토스증권 국내주식 장 운영 시간 】\n"
        "- 프리마켓(장전): 08:00~08:50 — 실시간 거래 가능\n"
        "- 메인마켓(정규장): 09:00~15:20 — 실시간 거래 가능\n"
        "- 시가단일가 마감임박: 15:30~15:40 — 단일가 주문만 가능\n"
        "- 애프터마켓(장후): 15:40~20:00 — 실시간 거래 가능\n"
        "- 클로즈(장 마감): 20:00~익일 08:00 — 거래 불가\n\n"
        "⚠️ 중요: 매매 추천 시 반드시 위 '현재 장 상태'와 '다음 거래일'을 기준으로 안내하세요. "
        f"거래 불가 상태면 {next_trade_date} 프리마켓 08:00 형태로 안내하세요.\n\n"
        "【 현재 포트폴리오 상태 】\n"
        f"{context}\n"
    )

    if us_market_ctx:
        system_prompt += (
            "【 미국증시 동향 (전일 마감) 】\n"
            f"{us_market_ctx}\n\n"
            "⚠️ 미국증시 데이터 활용 지침: "
            "사용자가 미국증시, 미국 시장, 뉴욕 증시 관련 질문을 하거나, "
            "보유종목과 미국 시장의 연관성을 분석할 때 반드시 위 미국증시 데이터를 참고하세요. "
            "예: 애플 하락 → 삼성전자/반도체 영향, 마이크론 HBM 수요 → SK하이닉스 연관성 등\n\n"
        )

    system_prompt += (
        "【 응답 원칙 】\n"
        "1. 항상 데이터에 기반한 객관적인 조언을 제공하세요.\n"
        "2. 매수/매도/관망에 대한 명확한 의견을 제시하세요.\n"
        "3. 리스크 관리의 중요성을 강조하세요.\n"
        "4. 전문적이고 친근한 어조로 답변하세요.\n"
        "5. 한국어로 답변하세요.\n"
        "6. 답변은 완전하게 작성하세요.\n"
        "7. 필요시 포트폴리오 내 특정 종목에 대한 구체적인 분석을 제공하세요.\n"
        "8. ⚠️ 절대 상상하여 답변하지 마세요. 위 포트폴리오 데이터와 미국증시 데이터, "
        "그리고 아래 검색 결과를 모두 활용하여 답변하세요. "
        "검색 결과에 없는 정보는 '확인이 필요합니다'라고 답변하세요.\n"
        "9. 대화 내역(history)에 이전에 나눈 내용이 있다면 그 정보도 적극 활용하세요. "
        "이전 대화 내용을 인용할 때는 [H숫자] 형식으로 출처를 표시하세요.\n"
        "10. 🚫 출처 할루네이션 금지: 반드시 아래 검색 결과 안의 URL만 인용하세요. "
        "검색 결과가 없으면 출처 섹션을 생략하세요."
    )
    if us_market_ctx:
        system_prompt += (
            "【 미국증시 동향 (전일 마감) 】\n"
            f"{us_market_ctx}\n\n"
            "⚠️ 미국증시 데이터 활용 지침: "
            "사용자가 미국증시, 미국 시장, 뉴욕 증시 관련 질문을 하거나, "
            "보유종목과 미국 시장의 연관성을 분석할 때 반드시 위 미국증시 데이터를 참고하세요. "
            "예: 애플 하락 → 삼성전자/반도체 영향, 마이크론 HBM 수요 → SK하이닉스 연관성 등\n\n"
        )
    system_prompt += (
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
        "6. 답변은 완전하게 작성하세요. 절대 중략하거나 생략하지 마세요. 테이블이나 목록도 모든 항목을 빠짐없이 포함하세요.\n"
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
    if filtered_search:
        search_text = ""
        for i, r in enumerate(filtered_search, 1):
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
    is_fallback = result.get("_source") == "fallback"
    # LLM이 자체 생성한 출처 섹션 제거 (코드에서 정확한 출처 추가)
    reply = re.sub(r'\n*📚\s*출처[:：][\s\S]*$', '', reply).rstrip()
    # AI 응답이 fallback(장애 메시지)이면 출처를 붙이지 않는다.
    if is_fallback:
        return reply
    h_refs = re.findall(r'\[H(\d+)\]', reply) if previous else []
    has_urls = filtered_search and any(r.get("url") for r in filtered_search)
    if has_urls or h_refs:
        reply += "\n\n📚 출처:\n"
        if filtered_search:
            for i, r in enumerate(filtered_search, 1):
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
