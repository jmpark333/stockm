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

# 실시간 급변 감지용 데이터
price_history: dict[str, deque] = {}  # {code: deque([(timestamp, price, volume), ...])}
PRICE_HISTORY_MAX = 60  # 최근 60개 데이터포인트 (약 10분)
VOLUME_HISTORY_MAX = 20  # 최근 20개 거래량 데이터

# 기술적 지표 캐시
_tech_indicators_cache: dict[str, dict] = {}
_tech_indicators_cache_time: dict[str, float] = {}
TECH_CACHE_TTL = 300  # 5분 캐시

ZAI_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
ZAI_KEY = os.environ.get("ZAI_KEY", "").strip()

KRX_API_KEY = os.environ.get("KRX_API_KEY", "D3ABD30920534A2C9616A984AB6078D1C722F0BA").strip()

NOUS_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
NOUS_KEY = os.environ.get("NOUS_KEY", "sk-nous-dueimEQDyVHzxeKCOolvFyx7e0DKZzBR").strip()
NOUS_MODELS = ["stepfun/step-3.7-flash:free"]

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
            "volume": item.get("nmv") or item.get("vlm"),  # 누적 거래량
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


def track_price_volume(code, price, volume=None):
    """실시간 가격과 거래량을 이력에 저장"""
    if code not in price_history:
        price_history[code] = deque(maxlen=PRICE_HISTORY_MAX)
    timestamp = time.time()
    price_history[code].append((timestamp, price, volume))


def detect_price_surge(code, current_price):
    """가격 급변 감지 (최근 5분 내 급등락)"""
    if code not in price_history or len(price_history[code]) < 2:
        return None
    
    now = time.time()
    recent_prices = [(t, p) for t, p, v in price_history[code] if now - t <= 300]  # 5분 이내
    
    if len(recent_prices) < 2:
        return None
    
    # 5분 전 가격과 현재 가격 비교
    old_price = recent_prices[0][1]
    new_price = recent_prices[-1][1]
    
    if old_price is None or new_price is None or old_price == 0:
        return None
    
    change_pct = (new_price - old_price) / old_price * 100
    
    # 1분 내 변동
    one_min_ago = now - 60
    one_min_prices = [(t, p) for t, p in recent_prices if t <= one_min_ago]
    one_min_change = 0
    if one_min_prices:
        one_min_old = one_min_prices[-1][1]
        if one_min_old and one_min_old > 0:
            one_min_change = (new_price - one_min_old) / one_min_old * 100
    
    signals = []
    
    # 5분 내 급락 (-2% 이상)
    if change_pct <= -2:
        signals.append({
            "type": "price_drop",
            "severity": "critical" if change_pct <= -3 else "warning",
            "message": f"5분 내 {change_pct:.1f}% 급락",
            "change_pct": round(change_pct, 2),
            "timeframe": "5min"
        })
    
    # 5분 내 급등 (+2% 이상)
    if change_pct >= 2:
        signals.append({
            "type": "price_surge",
            "severity": "critical" if change_pct >= 3 else "warning",
            "message": f"5분 내 +{change_pct:.1f}% 급등",
            "change_pct": round(change_pct, 2),
            "timeframe": "5min"
        })
    
    # 1분 내 급변 (±1% 이상)
    if abs(one_min_change) >= 1:
        signals.append({
            "type": "price_volatility",
            "severity": "warning",
            "message": f"1분 내 {one_min_change:+.1f}% 급변",
            "change_pct": round(one_min_change, 2),
            "timeframe": "1min"
        })
    
    return signals if signals else None


def detect_volume_surge(code, current_volume):
    """거래량 급증 감지"""
    if code not in price_history or len(price_history[code]) < 5:
        return None
    
    # 최근 거래량 이력
    recent_volumes = [v for _, _, v in price_history[code] if v is not None and v > 0]
    
    if len(recent_volumes) < 3:
        return None
    
    # 평균 거래량 계산 (최근 데이터 제외)
    avg_volume = sum(recent_volumes[:-1]) / len(recent_volumes[:-1]) if len(recent_volumes) > 1 else 0
    
    if avg_volume == 0 or current_volume is None:
        return None
    
    volume_ratio = current_volume / avg_volume
    
    signals = []
    
    # 거래량 200% 이상 급증
    if volume_ratio >= 2.0:
        signals.append({
            "type": "volume_surge",
            "severity": "critical" if volume_ratio >= 3.0 else "warning",
            "message": f"거래량 {volume_ratio:.1f}배 급증",
            "volume_ratio": round(volume_ratio, 2),
            "current_volume": current_volume,
            "avg_volume": round(avg_volume)
        })
    
    # 거래량 50% 이상 감소 (유동성 주의)
    if volume_ratio <= 0.5 and len(recent_volumes) > 5:
        signals.append({
            "type": "volume_drop",
            "severity": "info",
            "message": f"거래량 {volume_ratio:.1f}배 감소",
            "volume_ratio": round(volume_ratio, 2)
        })
    
    return signals if signals else None


def detect_all_signals(code, quote):
    """종목의 모든 실시간 시그널 감지"""
    all_signals = []
    
    current_price = quote.get("currentPrice")
    current_volume = quote.get("volume")
    
    # 가격 급변 감지
    price_signals = detect_price_surge(code, current_price)
    if price_signals:
        all_signals.extend(price_signals)
    
    # 거래량 급증 감지
    volume_signals = detect_volume_surge(code, current_volume)
    if volume_signals:
        all_signals.extend(volume_signals)
    
    # 가격 이력 업데이트
    track_price_volume(code, current_price, current_volume)
    
    return all_signals

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

    # 기술적 지표 가져오기 (캐시 활용)
    try:
        tech = calc_tech_indicators(code)
        tech_signals = tech.get("signals", [])
        tech_signal = tech.get("techSignal", "hold")
        signal_score = tech.get("signalScore", 0)
        indicators = tech.get("indicators", {})
    except Exception:
        tech_signals = []
        tech_signal = "hold"
        signal_score = 0
        indicators = {}

    # 기술적 지표 기반 단기추세 판단 (단순 가격 비교보다 안정적)
    short_trend = "flat"
    trend_reasons = []
    
    # 1. 이동평균선 배열 분석
    ma5 = indicators.get("ma5")
    ma20 = indicators.get("ma20")
    ma60 = indicators.get("ma60")
    
    if ma5 and ma20 and ma60:
        if ma5 > ma20 > ma60:
            short_trend = "up"
            trend_reasons.append("정배열 (MA5>MA20>MA60)")
        elif ma5 < ma20 < ma60:
            short_trend = "down"
            trend_reasons.append("역배열 (MA5<MA20<MA60)")
        elif ma5 > ma20 and ma5 > ma60:
            short_trend = "up"
            trend_reasons.append("단기 정배열 (MA5 최상위)")
        elif ma5 < ma20 and ma5 < ma60:
            short_trend = "down"
            trend_reasons.append("단기 역배열 (MA5 최하위)")
    
    # 2. RSI 기반 판단
    rsi = indicators.get("rsi14")
    if rsi is not None:
        if rsi > 60:
            if short_trend != "down":
                short_trend = "up"
                trend_reasons.append(f"RSI 강세 ({rsi:.1f})")
        elif rsi < 40:
            if short_trend != "up":
                short_trend = "down"
                trend_reasons.append(f"RSI 약세 ({rsi:.1f})")
    
    # 3. MACD 기반 판단
    macd_data = indicators.get("macd")
    if macd_data and macd_data.get("macd") is not None and macd_data.get("signal") is not None:
        if macd_data["macd"] > macd_data["signal"]:
            if short_trend != "down":
                short_trend = "up"
                trend_reasons.append("MACD 상승")
        elif macd_data["macd"] < macd_data["signal"]:
            if short_trend != "up":
                short_trend = "down"
                trend_reasons.append("MACD 하락")
    
    # 4. 기술적 시그널 점수 기반 판단
    if signal_score > 20:
        short_trend = "up"
        trend_reasons.append(f"기술적 시그널 강세 ({signal_score})")
    elif signal_score < -20:
        short_trend = "down"
        trend_reasons.append(f"기술적 시그널 약세 ({signal_score})")
    
    # 5. 실시간 가격 이력 기반 판단 (최소 3개 데이터 필요)
    hist = list(history.get(code, []))
    if len(hist) >= 3:
        # 이동평균선과 비교하여 추세 확인
        if ma5 and ma20:
            if cp > ma5 and ma5 > ma20:
                if short_trend != "down":
                    short_trend = "up"
                    trend_reasons.append("가격 > MA5 > MA20 (강한 상승)")
            elif cp < ma5 and ma5 < ma20:
                if short_trend != "up":
                    short_trend = "down"
                    trend_reasons.append("가격 < MA5 < MA20 (강한 하락)")
    
    # 기술적 지표가 없으면 가격 기반 판단 사용
    if not indicators:
        if len(hist) >= 3:
            first = hist[0]
            last = hist[-1]
            if last > first * 1.001:
                short_trend = "up"
                trend_reasons.append("가격 상승 추세")
            elif last < first * 0.999:
                short_trend = "down"
                trend_reasons.append("가격 하락 추세")

    # 기본 시그널 (가격 기반)
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
    
    # 기술적 지표가 있으면 reasons에 추가
    if tech_signals:
        reasons.extend(tech_signals[:5])  # 최대 5개까지
    
    # 기술적 시그널이 더 강하면 이를 우선
    if tech_signal != "hold" and signal == "hold":
        signal = tech_signal
    elif tech_signal != "hold":
        # 기본 시그널과 기술적 시그널이 다르면 점수 기반 판단
        if signal_score > 20 and signal in ("sell", "strong_sell"):
            signal = "hold"
            reasons.append("기술적 지표 상승 신호로 매도 보류")
        elif signal_score < -20 and signal in ("buy", "strong_buy"):
            signal = "hold"
            reasons.append("기술적 지표 하락 신호로 매수 보류")
    
    # 실시간 급변 시그널 감지
    realtime_signals = detect_all_signals(code, quote)
    if realtime_signals:
        for rs in realtime_signals:
            reasons.append(rs["message"])
        # 실시간 급변 시그널이 있으면 시그널 업데이트
        for rs in realtime_signals:
            if rs["type"] == "price_drop" and rs["severity"] == "critical":
                signal = "strong_buy" if signal not in ("sell", "strong_sell") else signal
            elif rs["type"] == "price_surge" and rs["severity"] == "critical":
                signal = "strong_sell" if signal not in ("buy", "strong_buy") else signal

    return {
        "rangePos": range_pos,
        "volatility": volatility,
        "gap": gap,
        "changeFromOpen": change_from_open,
        "shortTrend": short_trend,
        "signal": signal,
        "signalReasons": reasons,
        "techIndicators": indicators,
        "techSignals": tech_signals,
        "techSignalScore": signal_score,
        "realtimeSignals": realtime_signals,
    }

def build_item(quote):
    cp = quote.get("currentPrice")
    pc = quote.get("previousClose")
    hv = quote.get("high")
    lv = quote.get("low")
    op = quote.get("open")
    trend = calc_trend(quote)
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
        "volume": quote.get("volume"),
        "updatedAt": quote.get("updatedAt"),
        "error": quote.get("error"),
        "trend": trend,
        "realtimeSignals": trend.get("realtimeSignals", []),
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

    return {
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

def fetch_kr_market_news(limit=5):
    """한국증시 관련 최신 뉴스를 가져온다."""
    try:
        queries = ["코스피 코스닥 오늘", "코스피 하락 급락", "외국인 매도 코스피", "코스피 전망"]
        all_articles = []
        seen_titles = set()
        cutoff = time.time() - 86400
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

# 전역 변수: 뉴스 캐시
_us_market_news_cache = []
_us_market_news_cache_time = 0

_kr_market_news_cache = []
_kr_market_news_cache_time = 0

def get_us_market_news():
    """캐시된 미국증시 뉴스를 가져온다 (5분 캐시)."""
    global _us_market_news_cache, _us_market_news_cache_time
    now = time.time()
    if _us_market_news_cache and now - _us_market_news_cache_time < 300:
        return _us_market_news_cache
    _us_market_news_cache = fetch_us_market_news(limit=5)
    _us_market_news_cache_time = now
    return _us_market_news_cache

def get_kr_market_news():
    """캐시된 한국증시 뉴스를 가져온다 (5분 캐시)."""
    global _kr_market_news_cache, _kr_market_news_cache_time
    now = time.time()
    if _kr_market_news_cache and now - _kr_market_news_cache_time < 300:
        return _kr_market_news_cache
    _kr_market_news_cache = fetch_kr_market_news(limit=5)
    _kr_market_news_cache_time = now
    return _kr_market_news_cache

def build_news():
    config = load_config()
    items = []
    seen_codes = set()
    # 보유종목 + 관심종목 합쳐서 중복 제거
    all_stocks = []
    for h in config["holdings"]:
        if h["code"] not in seen_codes:
            all_stocks.append(h)
            seen_codes.add(h["code"])
    for w in config.get("watchlist", []):
        if w["code"] not in seen_codes:
            all_stocks.append(w)
            seen_codes.add(w["code"])
    # 병렬로 뉴스 가져오기
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def fetch_one(stock):
        return fetch_news(stock["name"], stock["code"])
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_one, s): s for s in all_stocks}
        for future in as_completed(futures):
            try:
                items.append(future.result())
            except Exception:
                pass
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

def calc_sma(data: list[float], period: int) -> list[float | None]:
    """단순 이동평균선 (Simple Moving Average)"""
    result = [None] * len(data)
    for i in range(period - 1, len(data)):
        result[i] = sum(data[i - period + 1:i + 1]) / period
    return result


def calc_ema(data: list[float], period: int) -> list[float | None]:
    """지수 이동평균선 (Exponential Moving Average)"""
    result = [None] * len(data)
    if len(data) < period:
        return result
    # 첫 EMA는 SMA로 시작
    result[period - 1] = sum(data[:period]) / period
    multiplier = 2 / (period + 1)
    for i in range(period, len(data)):
        result[i] = (data[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


def calc_rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """상대강도지수 (Relative Strength Index)"""
    result = [None] * len(closes)
    if len(closes) < period + 1:
        return result
    
    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
    
    # 첫 평균 계산
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - (100 / (1 + rs))
    
    # 나머지 계산 (Wilder's smoothing)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i + 1] = 100 - (100 / (1 + rs))
    
    return result


def calc_macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD (Moving Average Convergence Divergence)"""
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    
    macd_line = [None] * len(closes)
    for i in range(len(closes)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]
    
    # 시그널 라인 계산 (MACD의 EMA)
    macd_values = [v for v in macd_line if v is not None]
    signal_line = [None] * len(closes)
    if len(macd_values) >= signal:
        ema_signal = calc_ema(macd_values, signal)
        j = 0
        for i in range(len(closes)):
            if macd_line[i] is not None:
                signal_line[i] = ema_signal[j]
                j += 1
    
    # 히스토그램
    histogram = [None] * len(closes)
    for i in range(len(closes)):
        if macd_line[i] is not None and signal_line[i] is not None:
            histogram[i] = macd_line[i] - signal_line[i]
    
    return {
        "macd": macd_line,
        "signal": signal_line,
        "histogram": histogram
    }


def calc_bollinger_bands(closes: list[float], period: int = 20, std_dev: float = 2.0) -> dict:
    """볼린저 밴드 (Bollinger Bands)"""
    middle = calc_sma(closes, period)
    upper = [None] * len(closes)
    lower = [None] * len(closes)
    
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        avg = middle[i]
        if avg is not None:
            variance = sum((x - avg) ** 2 for x in window) / period
            std = variance ** 0.5
            upper[i] = avg + std_dev * std
            lower[i] = avg - std_dev * std
    
    return {
        "upper": upper,
        "middle": middle,
        "lower": lower
    }


def calc_stochastic(highs: list[float], lows: list[float], closes: list[float], 
                    k_period: int = 14, d_period: int = 3) -> dict:
    """스토캐스틱 (Stochastic Oscillator)"""
    k_values = [None] * len(closes)
    
    for i in range(k_period - 1, len(closes)):
        window_high = max(highs[i - k_period + 1:i + 1])
        window_low = min(lows[i - k_period + 1:i + 1])
        if window_high != window_low:
            k_values[i] = ((closes[i] - window_low) / (window_high - window_low)) * 100
        else:
            k_values[i] = 50.0
    
    # %D = %K의 이동평균
    d_values = [None] * len(closes)
    valid_k = [(i, v) for i, v in enumerate(k_values) if v is not None]
    for i in range(d_period - 1, len(valid_k)):
        window = [v for _, v in valid_k[i - d_period + 1:i + 1]]
        d_values[valid_k[i][0]] = sum(window) / d_period
    
    return {
        "k": k_values,
        "d": d_values
    }


def calc_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    """평균 진실 범위 (Average True Range) - 변동성 측정"""
    result = [None] * len(closes)
    if len(closes) < 2:
        return result
    
    true_ranges = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        true_ranges.append(tr)
    
    if len(true_ranges) >= period:
        result[period - 1] = sum(true_ranges[:period]) / period
        for i in range(period, len(true_ranges)):
            result[i] = (result[i - 1] * (period - 1) + true_ranges[i]) / period
    
    return result


def calc_tech_indicators(code: str) -> dict:
    """종목의 기술적 지표를 계산하여 반환"""
    now = time.time()
    cached = _tech_indicators_cache.get(code)
    if cached and now - _tech_indicators_cache_time.get(code, 0) < TECH_CACHE_TTL:
        return cached
    
    chart = fetch_chart_data(code, days=120)
    candles = chart.get("candles", [])
    if len(candles) < 30:
        return {"error": "데이터 부족", "candles": len(candles)}
    
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    
    # 이동평균선
    ma5 = calc_sma(closes, 5)
    ma20 = calc_sma(closes, 20)
    ma60 = calc_sma(closes, 60)
    ma120 = calc_sma(closes, 120) if len(closes) >= 120 else [None] * len(closes)
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    
    # RSI
    rsi14 = calc_rsi(closes, 14)
    
    # MACD
    macd_data = calc_macd(closes, 12, 26, 9)
    
    # 볼린저 밴드
    bb = calc_bollinger_bands(closes, 20, 2.0)
    
    # 스토캐스틱
    stoch = calc_stochastic(highs, lows, closes, 14, 3)
    
    # ATR
    atr14 = calc_atr(highs, lows, closes, 14)
    
    # 현재 값들
    current_close = closes[-1]
    current_ma5 = ma5[-1]
    current_ma20 = ma20[-1]
    current_ma60 = ma60[-1]
    current_ma120 = ma120[-1] if ma120 else None
    current_rsi = rsi14[-1]
    current_macd = macd_data["macd"][-1]
    current_signal = macd_data["signal"][-1]
    current_histogram = macd_data["histogram"][-1]
    current_bb_upper = bb["upper"][-1]
    current_bb_middle = bb["middle"][-1]
    current_bb_lower = bb["lower"][-1]
    current_stoch_k = stoch["k"][-1]
    current_stoch_d = stoch["d"][-1]
    current_atr = atr14[-1]
    
    # 과거 값들 (크로스오버 감지)
    prev_macd = macd_data["macd"][-2] if len(macd_data["macd"]) > 1 else None
    prev_signal = macd_data["signal"][-2] if len(macd_data["signal"]) > 1 else None
    prev_histogram = macd_data["histogram"][-2] if len(macd_data["histogram"]) > 1 else None
    prev_stoch_k = stoch["k"][-2] if len(stoch["k"]) > 1 else None
    prev_stoch_d = stoch["d"][-2] if len(stoch["d"]) > 1 else None
    
    # 기술적 시그널 분석
    signals = []
    signal_score = 0  # -100 ~ +100
    
    # 1. 이동평균선 배열 분석
    if current_ma5 and current_ma20 and current_ma60:
        if current_ma5 > current_ma20 > current_ma60:
            signals.append("정배열 (상승추세)")
            signal_score += 20
        elif current_ma5 < current_ma20 < current_ma60:
            signals.append("역배열 (하락추세)")
            signal_score -= 20
    
    # 2. 골든크로스/데드크로스
    if current_ma5 and current_ma20 and ma5[-2] and ma20[-2]:
        if ma5[-2] < ma20[-2] and current_ma5 > current_ma20:
            signals.append("MA5/20 골든크로스")
            signal_score += 15
        elif ma5[-2] > ma20[-2] and current_ma5 < current_ma20:
            signals.append("MA5/20 데드크로스")
            signal_score -= 15
    
    # 3. RSI 분석
    if current_rsi is not None:
        if current_rsi > 70:
            signals.append(f"RSI 과매수 ({current_rsi:.1f})")
            signal_score -= 15
        elif current_rsi < 30:
            signals.append(f"RSI 과매도 ({current_rsi:.1f})")
            signal_score += 15
        elif current_rsi > 60:
            signals.append(f"RSI 강세 ({current_rsi:.1f})")
            signal_score += 5
        elif current_rsi < 40:
            signals.append(f"RSI 약세 ({current_rsi:.1f})")
            signal_score -= 5
    
    # 4. MACD 분석
    if current_macd is not None and current_signal is not None:
        if prev_macd is not None and prev_signal is not None:
            if prev_macd < prev_signal and current_macd > current_signal:
                signals.append("MACD 골든크로스")
                signal_score += 20
            elif prev_macd > prev_signal and current_macd < current_signal:
                signals.append("MACD 데드크로스")
                signal_score -= 20
        if current_histogram is not None:
            if current_histogram > 0:
                signals.append("MACD 히스토그램 양전환")
                signal_score += 5
            else:
                signals.append("MACD 히스토그램 음전환")
                signal_score -= 5
    
    # 5. 볼린저 밴드 분석
    if current_bb_upper and current_bb_lower:
        bb_position = (current_close - current_bb_lower) / (current_bb_upper - current_bb_lower) * 100
        if current_close > current_bb_upper:
            signals.append("볼린저 상단 돌파")
            signal_score -= 10
        elif current_close < current_bb_lower:
            signals.append("볼린저 하단 이탈")
            signal_score += 10
        elif bb_position > 80:
            signals.append(f"볼린저 상단 접근 ({bb_position:.0f}%)")
            signal_score -= 5
        elif bb_position < 20:
            signals.append(f"볼린저 하단 접근 ({bb_position:.0f}%)")
            signal_score += 5
    
    # 6. 스토캐스틱 분석
    if current_stoch_k is not None and current_stoch_d is not None:
        if current_stoch_k > 80:
            signals.append(f"스토캐스틱 과매수 ({current_stoch_k:.1f})")
            signal_score -= 10
        elif current_stoch_k < 20:
            signals.append(f"스토캐스틱 과매도 ({current_stoch_k:.1f})")
            signal_score += 10
        if prev_stoch_k is not None and prev_stoch_d is not None:
            if prev_stoch_k < prev_stoch_d and current_stoch_k > current_stoch_d:
                signals.append("스토캐스틱 골든크로스")
                signal_score += 10
            elif prev_stoch_k > prev_stoch_d and current_stoch_k < current_stoch_d:
                signals.append("스토캐스틱 데드크로스")
                signal_score -= 10
    
    # 7. 거래량 분석
    if len(candles) >= 10:
        current_vol = candles[-1].get("volume", 0) or 0
        recent_vols = [c.get("volume", 0) or 0 for c in candles[-20:]]
        avg_vol_20 = sum(recent_vols) / len(recent_vols) if recent_vols else 0
        if avg_vol_20 > 0 and current_vol > 0:
            vol_ratio = current_vol / avg_vol_20
            if vol_ratio >= 3.0:
                signals.append(f"거래량 {vol_ratio:.1f}배 폭증")
                signal_score += 5 if current_close > candles[-2].get("close", current_close) else -5
            elif vol_ratio >= 2.0:
                signals.append(f"거래량 {vol_ratio:.1f}배 급증")
                signal_score += 3 if current_close > candles[-2].get("close", current_close) else -3
            elif vol_ratio <= 0.3:
                signals.append(f"거래량 {vol_ratio:.1f}배 급감")
            elif vol_ratio <= 0.5:
                signals.append(f"거래량 {vol_ratio:.1f}배 감소")
    
    # 8. 가격 vs 이동평균선 위치
    if current_ma20:
        price_vs_ma20 = (current_close - current_ma20) / current_ma20 * 100
        if price_vs_ma20 > 5:
            signals.append(f"MA20 대비 +{price_vs_ma20:.1f}% (과열)")
            signal_score -= 5
        elif price_vs_ma20 < -5:
            signals.append(f"MA20 대비 {price_vs_ma20:.1f}% (과침)")
            signal_score += 5
    
    # 종합 시그널 판단
    tech_signal = "hold"
    if signal_score >= 30:
        tech_signal = "strong_buy"
    elif signal_score >= 15:
        tech_signal = "buy"
    elif signal_score <= -30:
        tech_signal = "strong_sell"
    elif signal_score <= -15:
        tech_signal = "sell"
    
    result = {
        "indicators": {
            "ma5": round(current_ma5, 0) if current_ma5 else None,
            "ma20": round(current_ma20, 0) if current_ma20 else None,
            "ma60": round(current_ma60, 0) if current_ma60 else None,
            "ma120": round(current_ma120, 0) if current_ma120 else None,
            "rsi14": round(current_rsi, 2) if current_rsi else None,
            "macd": {
                "macd": round(current_macd, 2) if current_macd else None,
                "signal": round(current_signal, 2) if current_signal else None,
                "histogram": round(current_histogram, 2) if current_histogram else None,
            },
            "bollinger": {
                "upper": round(current_bb_upper, 0) if current_bb_upper else None,
                "middle": round(current_bb_middle, 0) if current_bb_middle else None,
                "lower": round(current_bb_lower, 0) if current_bb_lower else None,
            },
            "stochastic": {
                "k": round(current_stoch_k, 2) if current_stoch_k else None,
                "d": round(current_stoch_d, 2) if current_stoch_d else None,
            },
            "atr14": round(current_atr, 0) if current_atr else None,
        },
        "signals": signals,
        "signalScore": signal_score,
        "techSignal": tech_signal,
        "currentPrice": current_close,
        "dataPoints": len(candles),
    }
    
    _tech_indicators_cache[code] = result
    _tech_indicators_cache_time[code] = now
    
    return result

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
        "thinking": {"type": "disabled"},
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
        content = result["choices"][0]["message"].get("content") or ""
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
    # 기술적 지표 점수를 AI 분석에 반영
    tech = calc_tech_indicators(code)
    tech_signal = tech.get("techSignal", "hold")
    tech_score = tech.get("signalScore", 0)
    tech_signals = tech.get("signals", [])
    ai_signal = result.get("signal", "hold")

    # 기술적 시그널이 hold가 아니면 AI 분석에 반영
    if tech_signal != "hold":
        reasons = result.get("reasons", [])
        if ai_signal == "hold":
            # AI가 관망인데 기술적 시그널이 있으면 기술적 시그널 우선
            result["signal"] = tech_signal
            reasons.insert(0, f"기술적 지표: {tech_signal} (점수: {tech_score})")
        elif ai_signal != tech_signal:
            # AI와 기술적 시그널이 다르면 점수 기반 판단
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
        # 기술적 시그널 사유 추가
        for ts in tech_signals[:3]:
            if ts not in reasons:
                reasons.append(ts)
        result["reasons"] = reasons
    result["techSignalScore"] = tech_score

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
    dow = next((i for i in indices if "다우" in i["name"]), None)
    sp = next((i for i in indices if "S&P" in i["name"]), None)
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
        data = fetch_us_market_realtime()
        return data
    except Exception:
        return {"marketStatus": "closed"}

_krx_daily_cache: dict = {}
_krx_daily_cache_time: float = 0
KRX_DAILY_CACHE_TTL = 600  # 10분 캐시

def fetch_krx_daily(bas_dd: str) -> dict:
    """KRX OpenAPI에서 유가증권+코스닥 일별매매정보를 가져온다."""
    global _krx_daily_cache, _krx_daily_cache_time
    now = time.time()
    if _krx_daily_cache and now - _krx_daily_cache_time < KRX_DAILY_CACHE_TTL:
        return _krx_daily_cache

    all_rows = []
    for market, ep in [("KOSPI", "stk_bydd_trd"), ("KOSDAQ", "ksq_bydd_trd")]:
        url = f"https://data-dbg.krx.co.kr/svc/apis/sto/{ep}?basDd={bas_dd}"
        req = urllib.request.Request(url, headers={
            "AUTH_KEY": KRX_API_KEY,
            "User-Agent": "Mozilla/5.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                rows = data.get("OutBlock_1", [])
                for r in rows:
                    r["_market"] = market
                all_rows.extend(rows)
        except Exception as exc:
            print(f"[fetch_krx_daily] {market} error: {exc}", flush=True)

    result = {"basDd": bas_dd, "rows": all_rows}
    _krx_daily_cache = result
    _krx_daily_cache_time = now
    return result

def get_krx_stock(code: str, bas_dd: str = None) -> dict | None:
    """KRX에서 특정 종목의 일별 매매 데이터를 가져온다."""
    if not bas_dd:
        kst = timezone(timedelta(hours=9))
        bas_dd = datetime.now(kst).strftime("%Y%m%d")
    data = fetch_krx_daily(bas_dd)
    for row in data.get("rows", []):
        if row.get("ISU_CD") == code:
            return {
                "code": row["ISU_CD"],
                "name": row["ISU_NM"],
                "market": row.get("_market", ""),
                "close": int(row.get("TDD_CLSPRC", "0") or "0"),
                "change": int(row.get("CMPPREVDD_PRC", "0") or "0"),
                "changeRate": float(row.get("FLUC_RT", "0") or "0"),
                "open": int(row.get("TDD_OPNPRC", "0") or "0"),
                "high": int(row.get("TDD_HGPRC", "0") or "0"),
                "low": int(row.get("TDD_LWPRC", "0") or "0"),
                "volume": int(row.get("ACC_TRDVOL", "0") or "0"),
                "tradeValue": int(row.get("ACC_TRDVAL", "0") or "0"),
                "marketCap": int(row.get("MKTCAP", "0") or "0"),
                "listedShares": int(row.get("LIST_SHRS", "0") or "0"),
            }
    return None

_trader_flow_cache: dict[str, dict] = {}
_trader_flow_cache_time: dict[str, float] = {}
TRADER_FLOW_CACHE_TTL = 300  # 5분 캐시 (장 마감)
TRADER_FLOW_CACHE_TTL_MARKET = 60  # 1분 캐시 (장중)

def _is_market_open() -> bool:
    """장중 여부 확인 (한국시간 09:00~15:30, 주말 제외)"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    if now.weekday() >= 5:
        return False
    hhmm = now.hour * 60 + now.minute
    return 9 * 60 <= hhmm <= 15 * 60 + 30

def fetch_trader_flow(code: str) -> dict:
    """네이버 금융에서 외국인/기관 일별 매매 데이터를 스크래핑한다."""
    now = time.time()
    cached = _trader_flow_cache.get(code)
    ttl = TRADER_FLOW_CACHE_TTL_MARKET if _is_market_open() else TRADER_FLOW_CACHE_TTL
    if cached and now - _trader_flow_cache_time.get(code, 0) < ttl:
        return cached

    url = f"https://finance.naver.com/item/frgn.naver?code={code}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        html = _decode_naver_response(raw)
    except Exception as exc:
        return {"code": code, "rows": [], "summary": {}, "error": str(exc)}

    rows = []
    table_match = re.search(
        r'<table[^>]*summary="외국인[^"]*매매[^"]*"[^>]*>(.*?)</table>',
        html, re.DOTALL
    )
    if not table_match:
        table_match = re.search(
            r'<table[^>]*class="type2"[^>]*summary="외국인[^"]*"[^>]*>(.*?)</table>',
            html, re.DOTALL
        )

    if table_match:
        table_html = table_match.group(1)
        tr_blocks = re.split(r'<tr\b', table_html)
        for tr_html in tr_blocks:
            td_matches = re.findall(r'<td[^>]*>(.*?)</td>', tr_html, re.DOTALL)
            if len(td_matches) < 9:
                continue
            date_m = re.search(r'gray03[^>]*>([\d.]+)', td_matches[0])
            if not date_m:
                continue
            date_str = date_m.group(1)

            def parse_num(html_fragment: str) -> int | float | None:
                val_match = re.search(r'>\s*([\d,.\-+%]+)\s*<', html_fragment)
                if not val_match:
                    return None
                s = val_match.group(1).replace(',', '').replace('%', '')
                has_minus = '-' in s
                s = s.replace('+', '').replace('-', '')
                try:
                    v = float(s)
                    return -v if has_minus else v
                except ValueError:
                    return None

            close_price = parse_num(td_matches[1])
            change = parse_num(td_matches[2])
            change_rate = parse_num(td_matches[3])
            volume = parse_num(td_matches[4])
            inst_net = parse_num(td_matches[5])
            frgn_net = parse_num(td_matches[6])
            frgn_holding = parse_num(td_matches[7])
            frgn_ratio = parse_num(td_matches[8])

            rows.append({
                "date": date_str,
                "close": close_price,
                "change": change,
                "changeRate": change_rate,
                "volume": volume,
                "instNet": inst_net,
                "frgnNet": frgn_net,
                "frgnHolding": frgn_holding,
                "frgnRatio": frgn_ratio,
            })

    summary = {}
    if rows:
        recent5 = rows[:5]
        inst5 = sum(r["instNet"] for r in recent5 if r["instNet"] is not None)
        frgn5 = sum(r["frgnNet"] for r in recent5 if r["frgnNet"] is not None)
        if inst5 > 0 and frgn5 > 0:
            trend = "기관+외국인 동반매수"
        elif inst5 < 0 and frgn5 < 0:
            trend = "기관+외국인 동반매도"
        elif inst5 > 0:
            trend = "기관매수/외국인매도"
        else:
            trend = "기관매도/외국인매수"
        summary = {
            "instNet5d": inst5,
            "frgnNet5d": frgn5,
            "instNet1d": rows[0]["instNet"],
            "frgnNet1d": rows[0]["frgnNet"],
            "frgnRatio": rows[0]["frgnRatio"],
            "frgnHolding": rows[0]["frgnHolding"],
            "trend": trend,
        }

    result = {"code": code, "rows": rows[:10], "summary": summary}
    _trader_flow_cache[code] = result
    _trader_flow_cache_time[code] = now
    return result

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
    ups = sum(1 for i in indices if i["rate"] > 0)
    downs = sum(1 for i in indices if i["rate"] < 0)
    if ups == len(indices):
        desc = "코스피·코스닥 동반 상승"
    elif downs == len(indices):
        desc = "코스피·코스닥 동반 하락"
    else:
        desc = "코스피·코스닥 혼조세"
    parts = [desc]
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

    Returns the cleaned final_answer string on success, otherwise the
    original reply (with thinking artifacts stripped). Never returns
    reasoning/intermediate content to the user.
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

    # 2) Try to find the outermost JSON object that contains final_answer.
    #    Scan with a brace-matching heuristic (handles nested objects/strings).
    stack = []
    for i, ch in enumerate(candidate):
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            if stack:
                continue  # inner block, keep scanning
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
        content = result["choices"][0]["message"].get("content") or ""
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
        "max_tokens": 2500,
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
        msg = result["choices"][0]["message"]
        content = msg.get("content") or ""
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
        "max_tokens": 8000,
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
        result = json.loads(raw)
        msg = result["choices"][0]["message"]
        content = msg.get("content") or ""
        if not content:
            content = msg.get("reasoning") or ""
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
        "thinking": {"type": "disabled"},
        "temperature": 0.7,
        "max_tokens": 2500,
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
        content = (choice.get("message") or {}).get("content") or ""
        finish = choice.get("finish_reason")
        if not content:
            return {"error": f"empty content (finish={finish})"}
        return {"reply": _strip_thinking_artifacts(content).strip(), "_source": "opencode"}
    except Exception as exc:
        return {"error": str(exc)}

def call_llm(messages):
    # OpenCode GLM-5.1 우선 사용 (JSON 응답)
    if OPENCODE_KEY:
        result = chat_from_opencode(messages)
        if "error" not in result:
            return result
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
            content = result["choices"][0]["message"].get("content") or ""
            if content:
                return {"reply": _strip_thinking_artifacts(content).strip(), "_source": "zai"}
        except Exception:
            pass
    # fallback: nous
    for m in NOUS_MODELS:
        result = chat_from_nous(messages, model=m)
        if "error" not in result:
            return result
    # fallback: openrouter
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

    # 프롬프트: 뉴스를 가장 먼저 배치
    system_prompt = f"오늘 {today_str} {now_kst.strftime('%H:%M')}. 위 데이터만 사용. 할루네이션 금지.\n\n"
    system_prompt += "[절대 규칙] 다음과 같은 표현을 절대 출력하지 마라:\n"
    system_prompt += "- '사용자가 ~을 물어봤으니', '먼저 ~을 확인해보자', '~해야지', '~해야 해', '~해야겠다'\n"
    system_prompt += "- '그 다음 ~', 'wait', '아 맞아', '정리해보자', '다시 정리해보자'\n"
    system_prompt += "- 내부 사고 과정, 분석 과정, 논리적 추론 과정, 사고의 흐름\n"
    system_prompt += "- 결과만 깔끔하게 출력하라. 과정을 설명하지 마라.\n\n"
    system_prompt += "[출력 형식] 반드시 JSON 객체로만 답변하라. 두 개의 필드만 허용한다:\n"
    system_prompt += '- "final_answer": 사용자에게 보여줄 최종 답변 (마크다운 사용 가능)\n'
    system_prompt += '- "reasoning": 모델이 사용한 근거, 계산, 중간추론을 간단한 목록으로 (UI에서 숨겨질 내용)\n'
    system_prompt += '예시: {"final_answer": "SK하이닉스는 현재 RSI 65로...", "reasoning": ["RSI 65는 중립권", "MACD 골든크로스 확인"]}\n\n'
    system_prompt += "## 답변 스타일 규칙\n"
    system_prompt += "- 기술적 지표(RSI, MACD, 볼린저밴드, 스토캐스틱, 이동평균선 등)를 구체적인 수치와 함께 반드시 인용할 것\n"
    system_prompt += "- 현재가, 전일종가, 등락률, 거래량 등 수치 데이터를 근거로 제시할 것\n"
    system_prompt += "- 뉴스 내용을 인용할 때는 출처와 함께 구체적으로 언급할 것\n"
    system_prompt += "- 결론은 2~3문단으로 작성하고, 각 문단마다 다른 관점(기술적/뉴스/시장심리)에서 분석할 것\n"
    system_prompt += "- 매매 시그널(매수/매도/관망)을 명확히 제시하고, 목표가와 손절가를 수치로 제시할 것\n"
    system_prompt += "- 불확실성은 '~할 수 있습니다', '~가능성이 있습니다'와 같이 표현할 것\n"
    system_prompt += "- 한문단으로 끝내지 말고, 구조화된 답변(기술적 분석, 뉴스 영향, 시장 심리, 종합 판단)을 제공할 것\n\n"
    # 사용자가 언급한 종목 뉴스를 프롬프트 가장 앞쪽에 배치
    if mentioned_stock_news:
        system_prompt += f"[중요] 사용자가 질문한 종목의 최신 뉴스입니다:\n{mentioned_stock_news}\n"
    # 뉴스를 프롬프트 앞쪽에 배치
    if kr_news_ctx:
        system_prompt += f"{kr_news_ctx}\n"
    if us_news_ctx:
        system_prompt += f"{us_news_ctx}\n"
    system_prompt += f"{context}\n"
    # 시장 컨텍스트는 뒤쪽에 배치
    if kospi_kosdaq_ctx:
        system_prompt += f"{kospi_kosdaq_ctx}\n"
    if us_market_ctx:
        system_prompt += f"{us_market_ctx}\n"

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
            chart_data = fetch_chart_data(code)
            # 기술적 지표 추가
            try:
                tech = calc_tech_indicators(code)
                chart_data["techIndicators"] = tech.get("indicators", {})
                chart_data["techSignals"] = tech.get("signals", [])
                chart_data["techSignalScore"] = tech.get("signalScore", 0)
                chart_data["techSignal"] = tech.get("techSignal", "hold")
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                print(f"[CHART ERROR] code={code}: {tb}")
                chart_data["techIndicators"] = {}
                chart_data["techSignals"] = []
                chart_data["techSignalScore"] = 0
                chart_data["techSignal"] = "hold"
                chart_data["_techError"] = str(e)
            
            # 이동평균선 배열 데이터 추가 (차트용)
            candles = chart_data.get("candles", [])
            if len(candles) >= 5:
                closes = [c["close"] for c in candles]
                chart_data["maArrays"] = {
                    "ma5": calc_sma(closes, 5),
                    "ma20": calc_sma(closes, 20),
                    "ma60": calc_sma(closes, 60),
                    "ma120": calc_sma(closes, 120) if len(closes) >= 120 else [None] * len(candles),
                }
                # RSI 배열
                chart_data["rsiArray"] = calc_rsi(closes, 14)
            
            self.send_json(chart_data)
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
        if p == "/api/us-market-news":
            self.send_json({"articles": get_us_market_news()})
            return
        if p == "/api/kr-market-news":
            self.send_json({"articles": get_kr_market_news()})
            return
        if p == "/api/trader-flow":
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [None])[0]
            if not code:
                self.send_json({"error": "code parameter required"})
                return
            self.send_json(fetch_trader_flow(code))
            return
        if p == "/api/krx-daily":
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [None])[0]
            bas_dd = qs.get("date", [None])[0]
            if code:
                self.send_json(get_krx_stock(code, bas_dd) or {"error": "not found"})
            else:
                self.send_json(fetch_krx_daily(bas_dd or ""))
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

    def do_PUT(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        if p == "/api/reorder":
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self.send_json({"error": "invalid JSON"})
                return
            section = data.get("section", "")
            codes = data.get("codes", [])
            if not section or not codes:
                self.send_json({"error": "section and codes required"})
                return
            config = load_config()
            if section == "holdings":
                code_map = {h["code"]: h for h in config["holdings"]}
                config["holdings"] = [code_map[c] for c in codes if c in code_map]
            elif section == "watchlist":
                code_map = {w["code"]: w for w in config.get("watchlist", [])}
                config["watchlist"] = [code_map[c] for c in codes if c in code_map]
            else:
                self.send_json({"error": "invalid section"})
                return
            with DATA_FILE.open("w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            self.send_json({"ok": True})
            return
        self.send_error(404, "Not Found")

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
