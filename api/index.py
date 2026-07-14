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

# Storage: Upstash Redis > Supabase > Vercel Blob (fallback)
REDIS_URL = os.environ.get("KV_REST_API_URL", os.environ.get("UPSTASH_REDIS_REST_URL", "")).strip()
REDIS_TOKEN = os.environ.get("KV_REST_API_TOKEN", os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")).strip()
def _first_nonempty(*vals):
    for v in vals:
        v = (v or "").strip()
        if v:
            return v
    return ""

SUPABASE_URL = _first_nonempty(
    os.environ.get("SUPABASE_URL"),
    os.environ.get("NEXT_PUBLIC_SUPABASE_URL"),
)
SUPABASE_KEY = _first_nonempty(
    os.environ.get("SUPABASE_SECRET_KEY"),
    os.environ.get("SUPABASE_PUBLISHABLE_KEY"),
    os.environ.get("SUPABASE_ANON_KEY"),
    os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY"),
    os.environ.get("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY"),
)
BLOB_STORE_ID = os.environ.get("BLOB_STORE_ID", "").strip()
BLOB_TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip()

if REDIS_URL and REDIS_TOKEN:
    STORAGE_BACKEND = "redis"
elif SUPABASE_URL and SUPABASE_KEY:
    STORAGE_BACKEND = "supabase"
elif BLOB_STORE_ID and BLOB_TOKEN:
    STORAGE_BACKEND = "blob"
else:
    STORAGE_BACKEND = "none"
print(f"[storage] backend={STORAGE_BACKEND} supabase_url={'set' if SUPABASE_URL else 'unset'} key={'set' if SUPABASE_KEY else 'unset'}", file=sys.stderr, flush=True)


def kv_get(key):
    if STORAGE_BACKEND == "redis":
        return _redis_get(key)
    elif STORAGE_BACKEND == "supabase":
        return _supabase_get(key)
    elif STORAGE_BACKEND == "blob":
        return _blob_get(key)
    return None


def kv_set(key, value):
    if STORAGE_BACKEND == "redis":
        return _redis_set(key, value)
    elif STORAGE_BACKEND == "supabase":
        return _supabase_set(key, value)
    elif STORAGE_BACKEND == "blob":
        return _blob_set(key, value)
    return False


def _redis_get(key):
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
        print(f"[redis_get] key={key} error={exc}", file=sys.stderr, flush=True)
        return None


def _redis_set(key, value):
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
                print(f"[redis_set] key={key} unexpected payload={payload}", file=sys.stderr, flush=True)
            return ok
    except Exception as exc:
        print(f"[redis_set] key={key} error={exc}", file=sys.stderr, flush=True)
        return False


_SUPABASE_ERR = ""
_KV_CACHE = {}  # key -> (value, expiry_ts)
_KV_CACHE_TTL = 60  # seconds — short cache to dedupe rapid repeat calls

def _kv_cache_get(key):
    entry = _KV_CACHE.get(key)
    if entry and entry[1] > time.time():
        return entry[0]
    return None

def _kv_cache_set(key, value):
    _KV_CACHE[key] = (value, time.time() + _KV_CACHE_TTL)

def _supabase_get(key):
    global _SUPABASE_ERR
    cached = _kv_cache_get(key)
    if cached is not None:
        return cached
    try:
        url = f"{SUPABASE_URL}/rest/v1/kv_store?key=eq.{urllib.parse.quote(key, safe='')}&select=value"
        req = urllib.request.Request(url, headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
            val = rows[0]["value"] if rows else None
            if val is not None:
                _kv_cache_set(key, val)
            return val
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        _SUPABASE_ERR = f"HTTP {exc.code}: {body}"
        print(f"[supabase_get] key={key} {_SUPABASE_ERR}", file=sys.stderr, flush=True)
        return None
    except Exception as exc:
        _SUPABASE_ERR = str(exc)
        print(f"[supabase_get] key={key} error={exc}", file=sys.stderr, flush=True)
        return None


def _supabase_set(key, value):
    global _SUPABASE_ERR
    _kv_cache_set(key, value)  # optimistically update cache
    try:
        url = f"{SUPABASE_URL}/rest/v1/kv_store"
        body = json.dumps({"key": key, "value": value}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            ok = resp.status in (200, 201)
            if not ok:
                _SUPABASE_ERR = f"status={resp.status}"
            return ok
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        _SUPABASE_ERR = f"HTTP {exc.code}: {body}"
        print(f"[supabase_set] key={key} {_SUPABASE_ERR}", file=sys.stderr, flush=True)
        return False
    except Exception as exc:
        _SUPABASE_ERR = str(exc)
        print(f"[supabase_set] key={key} error={exc}", file=sys.stderr, flush=True)
        return False


def _blob_store_id_normalized():
    """Strip 'store_' prefix — CDN URL and API header require bare ID."""
    sid = BLOB_STORE_ID
    return sid[len("store_"):] if sid.startswith("store_") else sid


BLOB_STORE_ID_NORM = _blob_store_id_normalized()


def _blob_get(key):
    """GET blob content from Vercel Blob private storage (CDN URL)."""
    try:
        safe_key = urllib.parse.quote(key, safe="")
        url = f"https://{BLOB_STORE_ID_NORM}.private.blob.vercel-storage.com/{safe_key}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {BLOB_TOKEN}",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        print(f"[blob_get] key={key} error={exc}", file=sys.stderr, flush=True)
        return None
    except Exception as exc:
        print(f"[blob_get] key={key} error={exc}", file=sys.stderr, flush=True)
        return None


def _blob_set(key, value):
    """PUT blob content to Vercel Blob private storage (control-plane API)."""
    try:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        params = urllib.parse.urlencode({"pathname": key})
        url = f"https://vercel.com/api/blob?{params}"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {BLOB_TOKEN}",
                "Content-Type": "application/json",
                "x-vercel-blob-store-id": BLOB_STORE_ID_NORM,
                "x-api-version": "12",
                "x-allow-overwrite": "1",
                "x-vercel-blob-access": "private",
            },
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = resp.status in (200, 201)
            print(f"[blob_set] key={key} status={resp.status} ok={ok}", file=sys.stderr, flush=True)
            return ok
    except Exception as exc:
        print(f"[blob_set] key={key} error={exc}", file=sys.stderr, flush=True)
        return False
NAVER_REALTIME_URL = "https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"

history: dict[str, deque] = {}
MAX_HISTORY = 12

# 실시간 급변 감지용 데이터
price_history: dict[str, deque] = {}  # {code: deque([(timestamp, price, volume), ...])}
PRICE_HISTORY_MAX = 60

ZAI_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
ZAI_KEY = os.environ.get("ZAI_KEY", "").strip()

KRX_API_KEY = os.environ.get("KRX_API_KEY", "D3ABD30920534A2C9616A984AB6078D1C722F0BA").strip()

NOUS_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
NOUS_KEY = os.environ.get("NOUS_KEY", "sk-nous-dueimEQDyVHzxeKCOolvFyx7e0DKZzBR").strip()
NOUS_MODELS = ["stepfun/step-3.7-flash:free"]

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "").strip()
OPENROUTER_MODELS = ["nex-agi/nex-n2-pro:free", "openai/gpt-oss-120b:free"]

OPENCODE_URL = os.environ.get("OPENCODE_URL", "https://opencode.ai/zen/v1/chat/completions").strip()
OPENCODE_KEY = os.environ.get("OPENCODE_KEY", "").strip()
OPENCODE_MODEL = os.environ.get("OPENCODE_MODEL", "glm-5.2").strip()

# AI 의견 전용 (사용자 제공 키)
AI_OPINION_URL = "https://opencode.ai/zen/v1/chat/completions"
AI_OPINION_KEY = "sk-pero02gJQKOUNxVQ4c5tJWNZptId3KnoohgMITzWYXC5vJqRZpWLCwkLxXeyMv9b"
AI_OPINION_MODEL = "big-pickle"

# Toss Open API (거래량 비교용)
TOSS_CLIENT_ID = "tsck_live_Goy8p8nYIN9mrCNxLyi2lC"
TOSS_CLIENT_SECRET = "tssk_live_31UK7Gq3cHvuhZjUdIwMrg13xernaAJ4flisZ6wGfEGA"
TOSS_TOKEN_URL = "https://openapi.tossinvest.com/oauth2/token"
TOSS_CANDLES_URL = "https://openapi.tossinvest.com/api/v1/candles"
_toss_token = None
_toss_token_ts = 0

def _get_toss_token():
    global _toss_token, _toss_token_ts
    now = time.time()
    if _toss_token and now - _toss_token_ts < 3600:
        return _toss_token
    data = urllib.parse.urlencode({
        'grant_type': 'client_credentials',
        'client_id': TOSS_CLIENT_ID,
        'client_secret': TOSS_CLIENT_SECRET
    }).encode()
    req = urllib.request.Request(TOSS_TOKEN_URL, data=data, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            _toss_token = result.get('access_token')
            _toss_token_ts = now
            return _toss_token
    except Exception:
        return None

def fetch_yesterday_volume(code):
    token = _get_toss_token()
    if not token:
        return None
    url = f'{TOSS_CANDLES_URL}?symbol={code}&interval=1d&count=2'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode())
            candles = result.get('result', {}).get('candles', [])
            if len(candles) >= 2:
                return candles[1].get('volume')
    except Exception:
        pass
    return None

ai_cache: dict[str, dict] = {}
AI_CACHE_TTL = 300

def load_config():
    saved = kv_get("stock_dashboard:config")
    if saved:
        return saved
    with DATA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)

def save_config(config):
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    kv_set("stock_dashboard:config", config)

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

def fetch_previous_volume(code):
    """네이버 금융 과거 시세에서 전일 거래량을 가져온다."""
    url = f"https://finance.naver.com/item/sise_day.naver?code={code}&page=1"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
        for row in rows:
            cols = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            if cols and len(cols) >= 7:
                date_text = re.sub(r'<[^>]+>', '', cols[0]).strip()
                volume_text = re.sub(r'<[^>]+>', '', cols[6]).strip()
                # 오늘 날짜가 아니면 전일 데이터
                today = time.strftime("%Y.%m.%d")
                if date_text and volume_text and date_text != today:
                    return int(volume_text.replace(",", ""))
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
        
        # 프리마켓 거래량: aq가 0이면 nxtOverMarketPriceInfo에서 가져오기
        volume = item.get("aq")
        if (volume is None or volume == 0) and extra.get("accumulatedTradingVolumeRaw"):
            try:
                volume = int(extra["accumulatedTradingVolumeRaw"].replace(",", ""))
            except (ValueError, TypeError):
                pass
        
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
            "volume": volume,  # 거래량
            "afterMarketPrice": over_price,
            "updatedAt": extra.get("localTradedAt") or payload.get("time"),
        }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"code": code, "error": str(exc)}

def save_short_trend_history(code, trend_phase):
    """단기추세 결과를 저장 (최근 10개)."""
    key = f"short_trend_history:{code}"
    saved = kv_get(key)
    history = saved if isinstance(saved, list) else []
    
    history.append({"phase": trend_phase, "count": 1})
    
    if len(history) > 10:
        history = history[-10:]
    
    kv_set(key, history)


def detect_mid_term_trend(code, current_price):
    """중기 추세를 판단한다. 단기추세 결과 기반.
    
    전환 조건: 방향 전환 시 최소 2회 이상 연속된 신호 필요
    반환: (phase, confidence, reasons, cumulative_change, up_count, down_count, neutral_count)
    """
    key = f"short_trend_history:{code}"
    saved = kv_get(key)
    
    if not saved or not isinstance(saved, list) or len(saved) < 3:
        return "보합", 0, ["데이터 수집 중 (3개 필요)"], 0, 0, 0, 0
    
    # 상승/하락/보합 카운트
    up_count = 0
    down_count = 0
    neutral_count = 0
    total = 0
    
    up_phases = ("상승시작", "상승지속", "상승세약화", "바닥반등")
    down_phases = ("하락시작", "하락지속", "하락세약화", "천장반락")
    
    # 최근 추세 분석 (마지막 3개 기준)
    recent_phases = [item.get("phase", "보합") for item in saved[-3:]]
    
    for item in saved:
        phase = item.get("phase", "보합")
        count = item.get("count", 1)
        total += count
        
        if phase in up_phases:
            up_count += count
        elif phase in down_phases:
            down_count += count
        else:
            neutral_count += count
    
    if total == 0:
        return "보합", 0, [], 0, 0, 0, 0
    
    up_ratio = up_count / total
    down_ratio = down_count / total
    
    reasons = [f"상승 {up_count}회 / 하락 {down_count}회 / 보합 {neutral_count}회"]
    
    # 판단 로직
    prev_key = f"mid_trend_phase:{code}"
    prev_data = kv_get(prev_key)
    prev_phase = prev_data.get("phase") if prev_data else None
    prev_start_price = prev_data.get("start_price", current_price) if prev_data else current_price
    consec = 1
    start_price = current_price
    
    # 상승 추세 판정
    if up_ratio >= 0.6 and up_ratio > down_ratio + 0.1:
        if prev_phase in ("상승시작", "상승지속", "상승세약화"):
            consec = (prev_data.get("consec", 1) if prev_data else 1) + 1
            start_price = prev_start_price
            result = ("상승지속", 60, reasons + [f"{consec}회 연속 상승 ({up_ratio:.0%})"])
        elif prev_phase in ("하락시작", "하락지속", "하락세약화"):
            up_recent = sum(1 for p in recent_phases if p in up_phases)
            if up_recent >= 2:
                result = ("상승시작", 55, reasons + [f"전환 (최근3 중 상승 {up_recent}개)"])
            else:
                result = ("하락세약화", 50, reasons + ["전환 조건 미충족"])
                direction = "down"
                kv_set(prev_key, {"phase": result[0], "consec": 1, "ts": int(time.time()), "price": current_price, "direction": direction, "start_price": current_price})
                return result + (0, up_count, down_count, neutral_count)
        else:
            result = ("상승시작", 50, reasons + [f"상승 우세 ({up_ratio:.0%})"])
        direction = "up"
    # 하락 추세 판정
    elif down_ratio >= 0.6 and down_ratio > up_ratio + 0.1:
        if prev_phase in ("하락시작", "하락지속", "하락세약화"):
            consec = (prev_data.get("consec", 1) if prev_data else 1) + 1
            start_price = prev_start_price
            result = ("하락지속", 60, reasons + [f"{consec}회 연속 하락 ({down_ratio:.0%})"])
        elif prev_phase in ("상승시작", "상승지속", "상승세약화"):
            down_recent = sum(1 for p in recent_phases if p in down_phases)
            if down_recent >= 2:
                result = ("하락시작", 55, reasons + [f"전환 (최근3 중 하락 {down_recent}개)"])
            else:
                result = ("상승세약화", 50, reasons + ["전환 조건 미충족"])
                direction = "up"
                kv_set(prev_key, {"phase": result[0], "consec": 1, "ts": int(time.time()), "price": current_price, "direction": direction, "start_price": current_price})
                return result + (0, up_count, down_count, neutral_count)
        else:
            result = ("하락시작", 50, reasons + [f"하락 우세 ({down_ratio:.0%})"])
        direction = "down"
    else:
        result = ("보합", 30, reasons + ["뚜렷한 방향 없음"])
        direction = None
    
    # 누적 변동률 계산
    cumulative_chg = 0
    if start_price and start_price > 0:
        cumulative_chg = round((current_price - start_price) / start_price * 100, 2)
    
    kv_set(prev_key, {
        "phase": result[0],
        "consec": consec,
        "ts": int(time.time()),
        "price": current_price,
        "direction": direction,
        "start_price": start_price,
    })
    
    return result + (cumulative_chg, up_count, down_count, neutral_count)


def save_mid_term_trend_history(code, mid_trend_phase, up_count, down_count, neutral_count):
    """중기추세 결과를 저장 (최근 10개)."""
    key = f"mid_trend_history:{code}"
    saved = kv_get(key)
    history = saved if isinstance(saved, list) else []
    
    history.append({
        "phase": mid_trend_phase,
        "count": 1,
        "up": up_count,
        "down": down_count,
        "neutral": neutral_count,
    })
    
    if len(history) > 10:
        history = history[-10:]
    
    kv_set(key, history)


def detect_long_term_trend(code, current_price):
    """장기 추세를 판단한다. 중기추세 결과 기반.
    
    중기추세가 저장한 상승/하락/보합 횟수를 직접 종합하여 판단.
    반환: (phase, confidence, reasons, cumulative_change)
    """
    key = f"mid_trend_history:{code}"
    saved = kv_get(key)
    
    if not saved or not isinstance(saved, list) or len(saved) < 3:
        return "보합", 0, ["데이터 수집 중 (3개 필요)"], 0
    
    # 중기추세 결과에서 상승/하락/보합 횟수를 직접 합산
    up_count = 0
    down_count = 0
    neutral_count = 0
    total = 0
    
    up_phases = ("상승시작", "상승지속", "상승세약화", "바닥반등")
    down_phases = ("하락시작", "하락지속", "하락세약화", "천장반락")
    
    for item in saved:
        count = item.get("count", 1)
        total += count
        # 중기추세 단계(상승/하락/보합) 자체의 횟수를 셈
        phase = item.get("phase", "보합")
        if phase in up_phases:
            up_count += count
        elif phase in down_phases:
            down_count += count
        else:
            neutral_count += count
    
    if total == 0:
        return "보합", 0, [], 0
    
    reasons = [f"상승 {up_count}회 / 하락 {down_count}회 / 보합 {neutral_count}회"]
    
    # 판단 로직 — 장기추세는 상승/하락 횟수 비율차이로 판단
    prev_key = f"long_trend_phase:{code}"
    prev_data = kv_get(prev_key)
    prev_phase = prev_data.get("phase") if prev_data else None
    prev_start_price = prev_data.get("start_price", current_price) if prev_data else current_price
    consec = 1
    start_price = current_price
    
    # 상승 추세 판정: 상승이 하락의 2배 이상
    if up_count >= down_count * 2 and up_count >= 3:
        if prev_phase in ("상승시작", "상승지속", "상승세약화"):
            consec = (prev_data.get("consec", 1) if prev_data else 1) + 1
            start_price = prev_start_price
            result = ("상승지속", 60, reasons + [f"{consec}회 연속 상승"])
        else:
            result = ("상승시작", 55, reasons + [f"상승 우세"])
        direction = "up"
    # 하락 추세 판정: 하락이 상승의 2배 이상
    elif down_count >= up_count * 2 and down_count >= 3:
        if prev_phase in ("하락시작", "하락지속", "하락세약화"):
            consec = (prev_data.get("consec", 1) if prev_data else 1) + 1
            start_price = prev_start_price
            result = ("하락지속", 60, reasons + [f"{consec}회 연속 하락"])
        else:
            result = ("하락시작", 55, reasons + [f"하락 우세"])
        direction = "down"
    else:
        result = ("보합", 30, reasons + ["뚜렷한 방향 없음"])
        direction = None
    
    # 누적 변동률 계산
    cumulative_chg = 0
    if start_price and start_price > 0:
        cumulative_chg = round((current_price - start_price) / start_price * 100, 2)
    
    kv_set(prev_key, {
        "phase": result[0],
        "consec": consec,
        "ts": int(time.time()),
        "price": current_price,
        "direction": direction,
        "start_price": start_price,
    })
    
    return result + (cumulative_chg,)


def detect_trend_phase(code, current_price, previous_close, open_price):
    """추세 전환 단계를 감지한다. 실시간 가격 변화 기반.
    
    핵심 규칙:
    - 상승 중 하락 발생 → 즉시 하락추세 전환 (상승세약화 아님)
    - 하락 중 상승 발생 → 즉시 상승추세 전환 (하락세약화 아님)
    - 보합일 때만 세약화 유지
    """
    if current_price is None or previous_close is None or previous_close == 0:
        return "보합", 0, ["데이터 부족"]
    
    prev_key = f"trend_phase:{code}"
    prev_data = kv_get(prev_key)
    prev_phase = prev_data.get("phase") if prev_data else None
    prev_price = prev_data.get("price", 0) if prev_data else 0
    prev_consec = prev_data.get("consec", 1) if prev_data else 1
    prev_direction = prev_data.get("direction") if prev_data else None
    
    if prev_price > 0:
        price_chg = ((current_price - prev_price) / prev_price * 100)
    else:
        price_chg = 0
    
    day_chg = ((current_price - previous_close) / previous_close * 100)
    
    # 첫 요청: 전일 대비로 판단
    if prev_price == 0:
        if day_chg > 2:
            return "상승시작", 50, [f"오늘 +{day_chg:.1f}% 상승"]
        elif day_chg < -2:
            return "하락시작", 50, [f"오늘 {day_chg:.1f}% 하락"]
        elif day_chg > 0.5:
            return "상승세약화", 40, [f"오늘 +{day_chg:.1f}% 상승"]
        elif day_chg < -0.5:
            return "하락세약화", 40, [f"오늘 {day_chg:.1f}% 하락"]
        else:
            return "보합", 30, [f"오늘 보합 ({day_chg:+.1f}%)"]
    
    is_rising = price_chg > 0.05
    is_falling = price_chg < -0.05
    is_strong_rising = price_chg > 0.5
    is_strong_falling = price_chg < -0.5
    is_flat = not is_rising and not is_falling
    
    # ── 하락 추세에서의 전환 ──
    down_phases = ("하락시작", "하락지속", "하락세약화")
    
    if prev_phase in down_phases:
        # 강한 반등 → 바닥반등
        if is_strong_rising:
            return "바닥반등", 75, [f"반등 (+{price_chg:.2f}%)"]
        
        # 상승 발생 → 상승추세 전환 (하락세약화 아님!)
        if is_rising:
            if prev_phase == "하락세약화":
                return "상승시작", 55, [f"반등 후 상승 전환 ({price_chg:+.2f}%)"]
            return "상승시작", 50, [f"하락→상승 전환 ({price_chg:+.2f}%)"]
        
        # 하락 지속
        if is_falling:
            if prev_phase == "하락세약화":
                return "하락지속", 70, [f"반등 실패 재하락 ({price_chg:+.2f}%)"]
            return "하락지속", 70, [f"{prev_consec + 1}회 연속 하락 ({price_chg:+.2f}%)"]
        
        # 보합 → 하락세약화 유지
        if is_flat:
            return "하락세약화", 50, [f"하락 후 보합 ({price_chg:+.2f}%)"]
    
    # ── 상승 추세에서의 전환 ──
    up_phases = ("상승시작", "상승지속", "상승세약화")
    
    if prev_phase in up_phases:
        # 강한 조정 → 천장반락
        if is_strong_falling:
            return "천장반락", 75, [f"조정 ({price_chg:+.2f}%)"]
        
        # 하락 발생 → 하락추세 전환 (상승세약화 아님!)
        if is_falling:
            if prev_phase == "상승세약화":
                return "하락시작", 55, [f"조정 후 하락 전환 ({price_chg:+.2f}%)"]
            return "하락시작", 50, [f"상승→하락 전환 ({price_chg:+.2f}%)"]
        
        # 상승 지속
        if is_rising:
            if prev_phase == "상승세약화":
                return "상승지속", 70, [f"조정 후 재상승 ({price_chg:+.2f}%)"]
            return "상승지속", 70, [f"{prev_consec + 1}회 연속 상승 ({price_chg:+.2f}%)"]
        
        # 보합 → 상승세약화 유지
        if is_flat:
            return "상승세약화", 50, [f"상승 후 보합 ({price_chg:+.2f}%)"]
    
    # ── 바닥반등에서의 전환 ──
    if prev_phase == "바닥반등":
        if is_rising:
            return "상승시작", 55, [f"반등 후 상승 ({price_chg:+.2f}%)"]
        if is_falling:
            return "하락지속", 60, [f"반등 실패 재하락 ({price_chg:+.2f}%)"]
        return "보합", 40, [f"반등 후 보합 ({price_chg:+.2f}%)"]
    
    # ── 천장반락에서의 전환 ──
    if prev_phase == "천장반락":
        if is_falling:
            return "하락시작", 55, [f"조정 후 하락 ({price_chg:+.2f}%)"]
        if is_rising:
            return "상승지속", 60, [f"조정 후 재상승 ({price_chg:+.2f}%)"]
        return "보합", 40, [f"조정 후 보합 ({price_chg:+.2f}%)"]
    
    # ── 보합/신규 추세 시작 ──
    if is_rising:
        return "상승시작", 50, [f"상승 ({price_chg:+.2f}%)"]
    elif is_falling:
        return "하락시작", 50, [f"하락 ({price_chg:+.2f}%)"]
    
    return "보합", 30, [f"보합 ({price_chg:+.2f}%)"]


# 요청 내 추세 캐시 (같은 종목은 같은 추세 보장)
_trend_cache: dict[str, tuple] = {}

def clear_trend_cache():
    global _trend_cache
    _trend_cache = {}


def track_history(code, current_price):
    if current_price is None:
        return
    
    # Redis에서 기존 이력 로드
    key = f"stock_history:{code}"
    saved = kv_get(key)
    if saved and isinstance(saved, list):
        history[code] = deque(saved[-MAX_HISTORY:], maxlen=MAX_HISTORY)
    elif code not in history:
        history[code] = deque(maxlen=MAX_HISTORY)
    
    # 같은 가격이면 중복 추가 방지
    if history[code] and history[code][-1] == current_price:
        return
    
    history[code].append(current_price)
    
    # Redis에 저장
    kv_set(key, list(history[code]))


def track_price_volume(code, price, volume=None):
    """실시간 가격과 거래량을 이력에 저장"""
    if code not in price_history:
        price_history[code] = deque(maxlen=PRICE_HISTORY_MAX)
    timestamp = time.time()
    price_history[code].append((timestamp, price, volume))


def calc_trend(quote, mode="buy", holding=None):
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

    # ── 추세 전환 감지 (캐시 활용) ──
    day_chg = ((cp - pc) / pc * 100) if cp and pc and pc > 0 else 0
    
    if code in _trend_cache:
        trend_phase, trend_confidence, trend_reasons = _trend_cache[code]
    else:
        trend_phase, trend_confidence, trend_reasons = detect_trend_phase(code, cp, pc, op)
        _trend_cache[code] = (trend_phase, trend_confidence, trend_reasons)
        
        # 현재 추세를 Redis에 저장
        consec = 1
        now_ts = int(time.time())
        direction = None
        start_price = cp  # 기본값: 현재 가격
        
        if trend_phase in ("하락시작", "하락지속", "하락세약화"):
            direction = "down"
        elif trend_phase in ("상승시작", "상승지속", "상승세약화"):
            direction = "up"
        
        prev_data = kv_get(f"trend_phase:{code}")
        if prev_data:
            prev_phase = prev_data.get("phase", "")
            prev_consec = prev_data.get("consec", 1)
            prev_direction = prev_data.get("direction")
            prev_start_price = prev_data.get("start_price", cp)
            
            # 하락세약화에서 하락지속으로 복귀 시 consec 유지, start_price 유지
            if trend_phase == "하락지속" and prev_phase == "하락세약화" and prev_direction == "down":
                consec = prev_consec + 1
                start_price = prev_start_price
            # 상승세약화에서 상승지속으로 복귀 시 consec 유지, start_price 유지
            elif trend_phase == "상승지속" and prev_phase == "상승세약화" and prev_direction == "up":
                consec = prev_consec + 1
                start_price = prev_start_price
            # 같은 방향이면 +1, start_price 유지
            elif direction == prev_direction and direction is not None:
                consec = prev_consec + 1
                start_price = prev_start_price
            else:
                consec = 1
        
        kv_set(f"trend_phase:{code}", {
            "phase": trend_phase,
            "day_chg": round(day_chg, 2),
            "consec": consec,
            "ts": now_ts,
            "price": cp,
            "direction": direction,
            "start_price": start_price,
        })
        
        # 단기추세 결과를 중기추세 이력에 저장
        save_short_trend_history(code, trend_phase)
    
    # 추세 단계를 short_trend로 변환
    if trend_phase in ("상승시작", "상승지속", "바닥반등"):
        short_trend = "up"
    elif trend_phase in ("하락시작", "하락지속", "천장반락"):
        short_trend = "down"
    else:
        short_trend = "flat"

    # 기본 시그널 (가격 기반)
    signal = "hold"
    reasons = list(trend_reasons)  # 추세 판단 근거를 먼저 포함
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
    
    if tech_signals:
        reasons.extend(tech_signals[:5])
    
    if tech_signal != "hold" and signal == "hold":
        signal = tech_signal
    elif tech_signal != "hold":
        if signal_score > 20 and signal in ("sell", "strong_sell"):
            signal = "hold"
            reasons.append("기술적 지표 상승 신호로 매도 보류")
        elif signal_score < -20 and signal in ("buy", "strong_buy"):
            signal = "hold"
            reasons.append("기술적 지표 하락 신호로 매수 보류")

    # 중기 추세 (단기추세 10개 종합 기반) — 캐시 사용
    mid_cache_key = f"mid_{code}"
    if mid_cache_key in _trend_cache:
        mid_trend_phase, mid_trend_confidence, mid_trend_reasons, mid_cumulative_chg, mid_up, mid_down, mid_neutral = _trend_cache[mid_cache_key]
    else:
        mid_trend_phase, mid_trend_confidence, mid_trend_reasons, mid_cumulative_chg, mid_up, mid_down, mid_neutral = detect_mid_term_trend(code, cp)
        _trend_cache[mid_cache_key] = (mid_trend_phase, mid_trend_confidence, mid_trend_reasons, mid_cumulative_chg, mid_up, mid_down, mid_neutral)
        # 중기추세 결과 저장 → 장기추세 분석용
        save_mid_term_trend_history(code, mid_trend_phase, mid_up, mid_down, mid_neutral)
    
    # 장기 추세 (중기추세 10개 종합 기반) — 캐시 사용
    long_cache_key = f"long_{code}"
    if long_cache_key in _trend_cache:
        long_trend_phase, long_trend_confidence, long_trend_reasons, long_cumulative_chg = _trend_cache[long_cache_key]
    else:
        long_trend_phase, long_trend_confidence, long_trend_reasons, long_cumulative_chg = detect_long_term_trend(code, cp)
        _trend_cache[long_cache_key] = (long_trend_phase, long_trend_confidence, long_trend_reasons, long_cumulative_chg)

    # 단기 추세 누적 변동률
    short_cumulative_chg = 0
    trend_data = kv_get(f"trend_phase:{code}")
    if trend_data and trend_data.get("start_price") and cp:
        start_price = trend_data["start_price"]
        if start_price > 0:
            short_cumulative_chg = round((cp - start_price) / start_price * 100, 2)

    return {
        "rangePos": range_pos,
        "volatility": volatility,
        "gap": gap,
        "changeFromOpen": change_from_open,
        "shortTrend": short_trend,
        "trendPhase": trend_phase,
        "trendConfidence": trend_confidence,
        "cumulativeChange": short_cumulative_chg,
        "midTrend": mid_trend_phase,
        "midTrendReasons": mid_trend_reasons,
        "midCumulativeChange": mid_cumulative_chg,
        "longTrend": long_trend_phase,
        "longTrendReasons": long_trend_reasons,
        "longCumulativeChange": long_cumulative_chg,
        "signal": signal,
        "signalReasons": reasons,
        "techIndicators": indicators,
        "techSignals": tech_signals,
        "techSignalScore": signal_score,
        "buySellScore": calc_buy_sell_score(code, quote, {
            "techSignalScore": signal_score,
            "shortTrend": short_trend,
            "midTrend": mid_trend_phase,
            "longTrend": long_trend_phase,
            "rangePos": range_pos,
        }, mode=mode, holding=holding),
    }

def build_item(quote, mode="buy", holding=None):
    cp = quote.get("currentPrice")
    hv = quote.get("high")
    lv = quote.get("low")
    amp = quote.get("afterMarketPrice")
    code = quote.get("code")
    
    # 애프터마켓 가격이 있으면 고가/저가에 반영
    if amp and amp > 0:
        if hv is None or amp > hv:
            hv = amp
        if lv is None or amp < lv:
            lv = amp
    
    # Redis에서 이전 최저가/최고가 로드 (절대 높아지지 않도록)
    range_key = f"price_range:{code}"
    saved_range = kv_get(range_key)
    if saved_range:
        saved_low = saved_range.get("low", 0)
        saved_high = saved_range.get("high", 0)
        # 최저가는 절대 높아지면 안 됨
        if saved_low > 0 and lv is not None and lv > saved_low:
            lv = saved_low
        # 최고가는 절대 낮아지면 안 됨
        if saved_high > 0 and hv is not None and hv < saved_high:
            hv = saved_high
    
    # 현재 고가/저가를 Redis에 저장
    kv_set(range_key, {"low": lv, "high": hv})
    
    # 조정된 고가/저가를 quote에 반영
    adjusted_quote = {**quote, "high": hv, "low": lv}
    
    return {
        "code": quote.get("code"),
        "name": quote.get("name"),
        "currentPrice": cp,
        "previousClose": quote.get("previousClose"),
        "change": quote.get("change"),
        "changeRate": quote.get("changeRate"),
        "session": quote.get("session"),
        "high": hv,
        "low": lv,
        "open": quote.get("open"),
        "volume": quote.get("volume"),
        "yesterdayVolume": quote.get("yesterdayVolume"),
        "afterMarketPrice": amp,
        "updatedAt": quote.get("updatedAt"),
        "error": quote.get("error"),
        "trend": calc_trend(adjusted_quote, mode=mode, holding=holding),
    }


def generate_stock_summary(item):
    """종목별 AI 요약 생성 (규칙 기반, 2줄)"""
    trend = item.get("trend", {})
    short_phase = trend.get("trendPhase", "보합")
    mid_trend = trend.get("midTrend", "보합")
    long_trend = trend.get("longTrend", "보합")
    change_rate = item.get("changeRate", 0) or 0
    profit_rate = item.get("realizedProfitRate") or item.get("profitRate") or 0
    
    # 추세 요약 — 보합 포함 모든 추세 표시
    trend_parts = []
    trend_parts.append(f"단기{short_phase}")
    trend_parts.append(f"중기{mid_trend}")
    trend_parts.append(f"장기{long_trend}")
    trend_desc = ", ".join(trend_parts)
    
    # 가격 및 수익률 요약
    if change_rate > 0:
        price_desc = f"전일대비 +{change_rate:.1f}% 상승 중"
    elif change_rate < 0:
        price_desc = f"전일대비 {change_rate:.1f}% 하락 중"
    else:
        price_desc = "보합 유지"
    
    # 종합 의견
    up_signals = sum(1 for t in [short_phase, mid_trend, long_trend] if "상승" in t)
    down_signals = sum(1 for t in [short_phase, mid_trend, long_trend] if "하락" in t)
    
    if up_signals > down_signals:
        opinion = "상승 우세 → 긍정적"
    elif down_signals > up_signals:
        opinion = "하락 우세 → 주의 필요"
    else:
        opinion = "혼조세 → 관망"
    
    return f"{trend_desc} | {price_desc}\n{opinion}"


def build_portfolio(section=None):
    global _PORTFOLIO_CACHE, _PORTFOLIO_CACHE_TS
    clear_trend_cache()  # 요청 시작 시 추세 캐시 초기화
    now = time.time()
    if _PORTFOLIO_CACHE is not None and (now - _PORTFOLIO_CACHE_TS) < _PORTFOLIO_CACHE_TTL:
        return _PORTFOLIO_CACHE

    config = load_config()
    if section == "holdings":
        all_codes = [h["code"] for h in config["holdings"]]
    else:
        all_codes = [h["code"] for h in config["holdings"]]
        all_codes += [w["code"] for w in config.get("watchlist", [])]

    # 전일 거래량 캐싱 (하루에 한번만 스크래핑)
    prev_vol_cache = kv_get("prev_volume_cache") or {}
    today_str = time.strftime("%Y%m%d")
    codes_needing_vol = [c for c in all_codes if c not in prev_vol_cache or prev_vol_cache[c].get("date") != today_str]
    if codes_needing_vol:
        for code in codes_needing_vol:
            vol = fetch_previous_volume(code)
            if vol:
                prev_vol_cache[code] = {"date": today_str, "volume": vol}
        kv_set("prev_volume_cache", prev_vol_cache)

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
            **build_item(quote, mode="sell", holding={"avgPrice": avg_price, "quantity": quantity}),
            "quantity": quantity,
            "avgPrice": avg_price,
            "cost": cost,
            "currentValue": current_value,
            "profit": profit,
            "profitRate": profit_rate,
            "sellFee": sell_fee,
            "realizedProfit": realized_profit,
            "realizedProfitRate": realized_profit_rate,
            "previousVolume": prev_vol_cache.get(holding["code"], {}).get("volume"),
        })
    watchlist_rows = []
    if section != "holdings":
        for watch in config.get("watchlist", []):
            quote = quotes.get(watch["code"], {"code": watch["code"], "error": "호가 데이터 없음"})
            item = build_item(quote, mode="buy")
            item["previousVolume"] = prev_vol_cache.get(watch["code"], {}).get("volume")
            watchlist_rows.append(item)
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


def calc_buy_sell_score(code, quote, trend, mode="buy", holding=None):
    """매수/매도 종합 점수를 계산한다. 0 ~ 100.
    mode="buy": 관심종목 매수점수 (높을수록 매수 적기)
    mode="sell": 보유종목 매도점수 (높을수록 매도 적기)
    holding: {"avgPrice": float, "quantity": int} (sell 모드에서 사용)
    """
    score = 50
    factors = []

    # 1. 기술적 지표 점수 (35%)
    tech_score = trend.get("techSignalScore", 0)
    if mode == "sell":
        tech_contrib = tech_score * 0.35
        score += tech_contrib
        if tech_score > 30:
            factors.append(f"과매수 진입(+{tech_score:.0f})")
        elif tech_score < -30:
            factors.append(f"과매도 구간({tech_score:.0f})")
        elif tech_score > 15:
            factors.append(f"기술적 강세(+{tech_score:.0f})")
    else:
        tech_contrib = tech_score * 0.35
        score += tech_contrib
        if tech_score > 20:
            factors.append(f"기술적지표 강세(+{tech_score:.0f})")
        elif tech_score < -20:
            factors.append(f"기술적지표 약세({tech_score:.0f})")

    # 2. 추세 정렬 점수 (25%)
    trend_score = 0
    short = trend.get("shortTrend", "flat")
    mid = trend.get("midTrend", "보합")
    long_t = trend.get("longTrend", "보합")

    if mode == "sell":
        if short == "up":
            trend_score += 12
        elif short == "down":
            trend_score -= 10
        if "상승" in mid:
            trend_score += 8
        elif "하락" in mid:
            trend_score -= 8
        if "상승" in long_t:
            trend_score += 5
        elif "하락" in long_t:
            trend_score -= 5
        directions = [short, "up" if "상승" in mid else "down" if "하락" in mid else "flat",
                      "up" if "상승" in long_t else "down" if "하락" in long_t else "flat"]
        unique = set(d for d in directions if d != "flat")
        if len(unique) == 1 and "상승" in mid:
            trend_score = int(trend_score * 1.2)
            factors.append("추세 3개 동반 상승 → 이익실현 기회")
    else:
        if short == "up":
            trend_score += 15
        elif short == "down":
            trend_score -= 15
        if "상승" in mid:
            trend_score += 10
        elif "하락" in mid:
            trend_score -= 10
        if "상승" in long_t:
            trend_score += 5
        elif "하락" in long_t:
            trend_score -= 5
        directions = [short, "up" if "상승" in mid else "down" if "하락" in mid else "flat",
                      "up" if "상승" in long_t else "down" if "하락" in long_t else "flat"]
        unique = set(d for d in directions if d != "flat")
        if len(unique) == 1:
            trend_score = int(trend_score * 1.3)
            factors.append("추세 3개 동반" + (" 상승" in mid and "상승" or " 하락"))

    score += trend_score * 0.25

    # 3. 가격 모멘텀 점수 (25%)
    cp = quote.get("currentPrice")
    pc = quote.get("previousClose")
    op = quote.get("open")
    hv = quote.get("high")
    lv = quote.get("low")
    momentum = 0

    if cp and pc and pc > 0:
        day_chg = (cp - pc) / pc * 100
        if mode == "sell":
            if day_chg > 3:
                momentum += 15
                factors.append(f"일간 +{day_chg:.1f}% 급등 → 이익실현")
            elif day_chg > 1:
                momentum += 8
            elif day_chg < -3:
                momentum -= 5
                factors.append(f"일간 {day_chg:.1f}% 급락")
            elif day_chg < -1:
                momentum -= 3
        else:
            if day_chg > 3:
                momentum += 15
                factors.append(f"일간 +{day_chg:.1f}% 상승")
            elif day_chg > 1:
                momentum += 8
            elif day_chg < -3:
                momentum -= 15
                factors.append(f"일간 {day_chg:.1f}% 하락")
            elif day_chg < -1:
                momentum -= 8

    if cp and pc and op and pc > 0:
        gap = (op - pc) / pc * 100
        if mode == "sell":
            if gap > 2:
                momentum += 5
                factors.append(f"갭업 +{gap:.1f}% → 차익실현 기회")
            elif gap < -2:
                momentum -= 5
                factors.append(f"갭다운 {gap:.1f}%")
        else:
            if gap > 2:
                momentum += 5
                factors.append(f"갭업 +{gap:.1f}%")
            elif gap < -2:
                momentum -= 5
                factors.append(f"갭다운 {gap:.1f}%")

    range_pos = trend.get("rangePos", 50)
    if mode == "sell":
        if range_pos > 80:
            momentum += 8
            factors.append("일중 고점권 → 매도 유리")
        elif range_pos < 20:
            momentum -= 8
            factors.append("일중 저점권 → 매도 불리")
    else:
        if range_pos > 80:
            momentum -= 8
            factors.append("일중 고점권")
        elif range_pos < 20:
            momentum += 8
            factors.append("일중 저점권")

    score += momentum * 0.25

    # 4. 거래량 분석 (15%)
    vol = quote.get("volume")
    if vol and vol > 0 and cp and pc:
        price_up = cp > pc
        if mode == "sell":
            if vol > 1000000:
                if price_up:
                    score += 8 * 0.15
                    factors.append("거래량 증가+상승 → 이익실현 수요")
                else:
                    score -= 10 * 0.15
                    factors.append("거래량 증가+하락 → 매도 압력")
            elif vol < 100000:
                score -= 3 * 0.15
        else:
            if vol > 1000000:
                if price_up:
                    score += 10 * 0.15
                    factors.append("거래량 증가+상승")
                else:
                    score -= 10 * 0.15
                    factors.append("거래량 증가+하락")
            elif vol < 100000:
                score -= 3 * 0.15

    # 5. 매도 전용: 수익률 보정 (10%)
    if mode == "sell" and holding:
        avg_p = holding.get("avgPrice", 0)
        if avg_p and avg_p > 0 and cp:
            profit_rate = (cp - avg_p) / avg_p * 100
            if profit_rate > 30:
                score += 15
                factors.append(f"수익률 +{profit_rate:.1f}% → 적극매도")
            elif profit_rate > 15:
                score += 10
                factors.append(f"수익률 +{profit_rate:.1f}% → 이익실현")
            elif profit_rate > 5:
                score += 5
                factors.append(f"수익률 +{profit_rate:.1f}%")
            elif profit_rate > 0:
                score += 2
            elif profit_rate < -10:
                score -= 10
                factors.append(f"손실 {profit_rate:.1f}% → 홀드")
            elif profit_rate < -5:
                score -= 5
                factors.append(f"손실 {profit_rate:.1f}%")

    score = max(0, min(100, round(score)))

    if mode == "sell":
        if score >= 80:
            label, grade = "적극매도", "strong_sell"
        elif score >= 65:
            label, grade = "매도", "sell"
        elif score >= 55:
            label, grade = "관망(매도)", "lean_sell"
        elif score > 45:
            label, grade = "관망", "hold"
        elif score > 35:
            label, grade = "관망(보유)", "lean_buy"
        elif score > 20:
            label, grade = "보유", "buy"
        else:
            label, grade = "적극보유", "strong_buy"
    else:
        if score >= 80:
            label, grade = "적극매수", "strong_buy"
        elif score >= 65:
            label, grade = "매수", "buy"
        elif score >= 55:
            label, grade = "관망(매수)", "lean_buy"
        elif score > 45:
            label, grade = "관망", "hold"
        elif score > 35:
            label, grade = "관망(보류)", "lean_sell"
        elif score > 20:
            label, grade = "매수불리", "sell"
        else:
            label, grade = "매수매우불리", "strong_sell"

    return {"score": score, "label": label, "grade": grade, "mode": mode, "factors": factors[:6]}


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

@app.route("/api/debug-storage")
def _debug_storage():
    return Response(json.dumps({"backend": STORAGE_BACKEND}, ensure_ascii=False), mimetype="application/json")


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
    section = request.args.get("section")
    return Response(
        json.dumps(build_portfolio(section=section), ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )

@app.route("/api/stock-summaries")
def api_stock_summaries():
    """종목별 AI 요약 생성"""
    config = load_config()
    news = build_news()
    news_map = {n["code"]: n.get("articles", []) for n in news}
    
    summaries = {}
    # 보유종목 + 관심종목
    all_items = []
    for h in config["holdings"]:
        all_items.append({"code": h["code"], "name": h["name"], "type": "holding"})
    for w in config.get("watchlist", []):
        all_items.append({"code": w["code"], "name": w["name"], "type": "watchlist"})
    
    # 기존 portfolio 캐시에서 추세 데이터 사용
    portfolio = build_portfolio()
    holdings_map = {h["code"]: h for h in portfolio.get("holdings", [])}
    watchlist_map = {w["code"]: w for w in portfolio.get("watchlist", [])}
    
    for item in all_items:
        code = item["code"]
        name = item["name"]
        
        # 추세 데이터 가져오기
        if code in holdings_map:
            data = holdings_map[code]
        elif code in watchlist_map:
            data = watchlist_map[code]
        else:
            continue
        
        trend = data.get("trend", {})
        short_phase = trend.get("trendPhase", "보합")
        mid_trend = trend.get("midTrend", "보합")
        long_trend = trend.get("longTrend", "보합")
        mid_reasons = trend.get("midTrendReasons", [])
        long_reasons = trend.get("longTrendReasons", [])
        change_rate = data.get("changeRate", 0) or 0
        profit_rate = data.get("realizedProfitRate") or data.get("profitRate") or 0
        current_price = data.get("currentPrice", 0)
        
        # 뉴스 헤드라인
        articles = news_map.get(code, [])
        news_headline = articles[0]["title"] if articles else ""
        
        # 추세 요약 — 보합 포함 모든 추세 표시
        trend_parts = []
        trend_parts.append(f"단기 {short_phase}")
        trend_parts.append(f"중기 {mid_trend}")
        trend_parts.append(f"장기 {long_trend}")
        trend_desc = ", ".join(trend_parts)
        
        # 가격 요약
        if change_rate > 0:
            price_desc = f"전일대비 +{change_rate:.1f}% 상승 중"
        elif change_rate < 0:
            price_desc = f"전일대비 {change_rate:.1f}% 하락 중"
        else:
            price_desc = "보합 유지"
        
        # 종합 의견
        up_signals = sum(1 for t in [short_phase, mid_trend, long_trend] if "상승" in t)
        down_signals = sum(1 for t in [short_phase, mid_trend, long_trend] if "하락" in t)
        
        if up_signals > down_signals:
            opinion = "상승 우세 → 긍정적"
        elif down_signals > up_signals:
            opinion = "하락 우세 → 주의 필요"
        else:
            opinion = "혼조세 → 관망"
        
        # 뉴스가 있으면 의견에 반영
        if news_headline:
            opinion += f" | 📰 {news_headline[:40]}..."
        
        summary_text = f"{trend_desc} | {price_desc}\n{opinion}"
        summaries[code] = summary_text
    
    return Response(
        json.dumps(summaries, ensure_ascii=False),
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

@app.route("/api/trader-flow")
def api_trader_flow():
    code = request.args.get("code", "")
    if not code:
        return Response(
            json.dumps({"error": "code parameter required"}, ensure_ascii=False),
            mimetype="application/json",
            status=400,
        )
    return Response(
        json.dumps(fetch_trader_flow(code), ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )

@app.route("/api/krx-daily")
def api_krx_daily():
    code = request.args.get("code", "")
    bas_dd = request.args.get("date", "")
    if code:
        result = get_krx_stock(code, bas_dd) or {"error": "not found"}
    else:
        result = fetch_krx_daily(bas_dd)
    return Response(
        json.dumps(result, ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )

@app.route("/api/reorder", methods=["PUT"])
def api_reorder():
    data = request.get_json(force=True)
    section = data.get("section", "")
    codes = data.get("codes", [])
    if not section or not codes:
        return Response(json.dumps({"error": "section and codes required"}, ensure_ascii=False), mimetype="application/json", status=400)
    config = load_config()
    if section == "holdings":
        code_map = {h["code"]: h for h in config["holdings"]}
        config["holdings"] = [code_map[c] for c in codes if c in code_map]
    elif section == "watchlist":
        code_map = {w["code"]: w for w in config.get("watchlist", [])}
        config["watchlist"] = [code_map[c] for c in codes if c in code_map]
    else:
        return Response(json.dumps({"error": "invalid section"}, ensure_ascii=False), mimetype="application/json", status=400)
    save_config(config)
    global _PORTFOLIO_CACHE, _PORTFOLIO_CACHE_TS
    _PORTFOLIO_CACHE = None
    _PORTFOLIO_CACHE_TS = 0.0
    return Response(json.dumps({"ok": True}, ensure_ascii=False), mimetype="application/json", headers={"Access-Control-Allow-Origin": "*"})

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


def call_llm_for_ai_opinion(messages):
    """AI 의견 전용 LLM 호출 (big-pickle 모델 사용)"""
    import requests as _requests
    payload = {
        "model": AI_OPINION_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1500,
    }
    headers = {
        "Authorization": f"Bearer {AI_OPINION_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "StockDashboard/1.0",
    }
    try:
        resp = _requests.post(AI_OPINION_URL, json=payload, headers=headers, timeout=60)
        if resp.status_code != 200:
            return {"reply": f"AI 분석 실패: HTTP {resp.status_code}"}
        result = resp.json()
        choice = result["choices"][0]
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        if not content:
            reasoning = msg.get("reasoning") or ""
            if reasoning:
                content = reasoning
        if not content:
            return {"reply": "AI 분석 실패: 빈 응답"}
        return {"reply": _strip_thinking_artifacts(content).strip()}
    except Exception as exc:
        return {"reply": f"AI 분석 실패: {exc}"}


@app.route("/api/ai-opinion")
def api_ai_opinion():
    code = request.args.get("code")
    mode = request.args.get("mode", "buy")
    if not code:
        return Response(json.dumps({"error": "code parameter required"}), status=400, mimetype="application/json")
    result = get_ai_opinion(code, mode)
    return Response(
        json.dumps(result, ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )


def get_ai_opinion(code, mode="buy"):
    """AI에게 특정 종목의 매수/매도 의견을 요청한다."""
    try:
        quote = fetch_quote(code)
    except Exception:
        quote = {"code": code, "error": "호가 데이터 없음"}

    name = quote.get("name", code)
    cp = quote.get("currentPrice", 0)
    chg = quote.get("change", 0)
    chg_rate = quote.get("changeRate", 0)
    hv = quote.get("high", 0)
    lv = quote.get("low", 0)
    vol = quote.get("volume") or 0

    # 기술적 지표
    tech_ctx = ""
    tech = None
    try:
        tech = calc_tech_indicators(code)
        ind = tech.get("indicators", {})
        sigs = tech.get("signals", [])
        sig_score = tech.get("signalScore", 0)
        sig_label = tech.get("techSignal", "hold")
        signal_names = {"strong_buy": "강력매수", "buy": "매수", "hold": "관망", "sell": "매도", "strong_sell": "강력매도"}

        tech_ctx = f"기술적 종합: {signal_names.get(sig_label, '관망')} (점수: {sig_score})\n"
        if ind.get("rsi14") is not None:
            rsi = ind["rsi14"]
            rsi_s = "과매수" if rsi > 70 else "강세" if rsi > 60 else "약세" if rsi < 40 else "과매도" if rsi < 30 else "중립"
            tech_ctx += f"RSI(14): {rsi:.1f} ({rsi_s})\n"
        if ind.get("macd"):
            m = ind["macd"]
            macd_v = m.get("macd", 0)
            sig_v = m.get("signal", 0)
            tech_ctx += f"MACD: {macd_v:.2f} (시그널: {sig_v:.2f}) {'상승모멘텀' if macd_v > sig_v else '하락모멘텀'}\n"
        if ind.get("bollinger"):
            bb = ind["bollinger"]
            if cp and bb.get("upper") and bb.get("lower"):
                bb_pos = (cp - bb["lower"]) / (bb["upper"] - bb["lower"]) * 100
                tech_ctx += f"볼린저밴드 위치: {bb_pos:.0f}% (상단: {bb.get('upper', 0):,.0f}원, 하단: {bb.get('lower', 0):,.0f}원)\n"
        if ind.get("stochastic"):
            st = ind["stochastic"]
            tech_ctx += f"스토캐스틱: %K={st.get('k', 0):.1f} %D={st.get('d', 0):.1f}\n"
        if sigs:
            tech_ctx += f"기술적 시그널: {', '.join(sigs[:5])}\n"
    except Exception:
        tech_ctx = "기술적 지표 없음\n"

    # 뉴스
    news_ctx = ""
    try:
        news_items = build_news()
        for item in news_items:
            if item.get("code") == code or item.get("name") == name:
                articles = item.get("articles", [])
                if articles:
                    news_ctx = f"{name} 관련 뉴스:\n"
                    for a in articles[:5]:
                        title = a.get("title", "")
                        src = a.get("source", "")
                        if title:
                            news_ctx += f"• {title}"
                            if src:
                                news_ctx += f" ({src})"
                            news_ctx += "\n"
                break
    except Exception:
        pass

    # 시장 컨텍스트
    market_ctx = ""
    try:
        us_ctx = build_us_market_context()
        if us_ctx:
            market_ctx += us_ctx + "\n"
        kr_ctx = build_kospi_kosdaq_context()
        if kr_ctx:
            market_ctx += kr_ctx + "\n"
    except Exception:
        pass

    # 추세 데이터
    trend = None
    trend_ctx = ""
    try:
        trend = calc_trend(quote, mode=mode)
        trend_phase = trend.get("trendPhase", "보합")
        mid_trend = trend.get("midTrend", "보합")
        long_trend = trend.get("longTrend", "보합")
        trend_ctx = f"단기 추세: {trend_phase}, 중기 추세: {mid_trend}, 장기 추세: {long_trend}\n"
        signal_reasons = trend.get("signalReasons", [])
        if signal_reasons:
            trend_ctx += f"추세 근거: {', '.join(signal_reasons[:3])}\n"
    except Exception:
        pass

    # 매수/매도 점수
    score_ctx = ""
    try:
        trend_for_score = trend or {}
        bss = calc_buy_sell_score(code, quote, {
            "techSignalScore": tech.get("signalScore", 0) if tech else 0,
            "shortTrend": trend_for_score.get("shortTrend", "flat"),
            "midTrend": trend_for_score.get("midTrend", "보합"),
            "longTrend": trend_for_score.get("longTrend", "보합"),
            "rangePos": trend_for_score.get("rangePos", 50),
        }, mode=mode)
        score_ctx = f"{'매도' if mode == 'sell' else '매수'} 점수: {bss.get('score', 50)}/100 ({bss.get('label', '관망')})\n"
        factors = bss.get("factors", [])
        if factors:
            score_ctx += f"점수 근거: {', '.join(factors)}\n"
    except Exception:
        pass

    # 프롬프트 구성
    mode_text = "매도" if mode == "sell" else "매수"

    system_prompt = f"""{name}({code}) 종목 분석. JSON만 출력.

opinion: 적극매수/매수/분할매수/관망/분할매도/매도/적극매도/손절 중 택1
reason: 분석 근거 3줄

예시 출력:
{{"opinion": "분할매수", "reason": "RSI 32 과매도. 볼린저 하단 접근. 소량 분할매수 고려."}}

데이터:
현재가 {cp:,}원, 전일 {chg:+,}원({chg_rate:+.2f}%)
고가 {hv:,}원/저가 {lv:,}원, 거래량 {vol:,}주
기술적분석: {tech_ctx}
추세: {trend_ctx}
매매점수: {score_ctx}
뉴스: {news_ctx if news_ctx else "없음"}
시장: {market_ctx if market_ctx else "없음"}

한글로만 답변. JSON만 출력."""

    messages = [{"role": "system", "content": system_prompt}]
    result = call_llm_for_ai_opinion(messages)
    reply = result.get("reply", "")

    # JSON 파싱 시도
    opinion_data = {"opinion": "관망", "reason": reply}
    try:
        # 1. 전체 문자열에서 JSON 파싱 시도
        parsed = json.loads(reply)
        opinion_data["opinion"] = parsed.get("opinion", "관망")
        opinion_data["reason"] = parsed.get("reason", reply)
    except Exception:
        try:
            # 2. markdown 코드 블록 제거 후 파싱 시도
            cleaned = re.sub(r'```json\s*', '', reply)
            cleaned = re.sub(r'```\s*$', '', cleaned.strip())
            cleaned = re.sub(r'```\s*', '', cleaned)
            parsed = json.loads(cleaned)
            opinion_data["opinion"] = parsed.get("opinion", "관망")
            opinion_data["reason"] = parsed.get("reason", reply)
        except Exception:
            try:
                # 3. 중괄호 매칭으로 JSON 블록 추출
                start = reply.find('{')
                end = reply.rfind('}')
                if start != -1 and end != -1 and end > start:
                    json_str = reply[start:end+1]
                    parsed = json.loads(json_str)
                    opinion_data["opinion"] = parsed.get("opinion", "관망")
                    opinion_data["reason"] = parsed.get("reason", reply)
            except Exception:
                pass

    opinion_data["stockName"] = name
    opinion_data["stockCode"] = code
    opinion_data["mode"] = mode
    opinion_data["currentPrice"] = cp
    return opinion_data

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

_krx_daily_cache: dict = {}
_krx_daily_cache_time: float = 0
KRX_DAILY_CACHE_TTL = 600

def fetch_krx_daily(bas_dd: str) -> dict:
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

KOSPI_INDEX_URL = "https://finance.naver.com/sise/sise_index.naver?code=KOSPI"
KOSDAQ_INDEX_URL = "https://finance.naver.com/sise/sise_index.naver?code=KOSDAQ"

_trader_flow_cache: dict = {}
_trader_flow_cache_time: dict = {}
TRADER_FLOW_CACHE_TTL = 300
TRADER_FLOW_CACHE_TTL_MARKET = 60

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
        for enc in ("utf-8", "euc-kr", "cp949"):
            try:
                html = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            html = raw.decode("utf-8", errors="replace")
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

            def parse_num(html_fragment):
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
        summary = {
            "instNet5d": inst5,
            "frgnNet5d": frgn5,
            "instNet1d": rows[0]["instNet"],
            "frgnNet1d": rows[0]["frgnNet"],
            "frgnRatio": rows[0]["frgnRatio"],
            "frgnHolding": rows[0]["frgnHolding"],
            "trend": (
                "기관+외국인 동반매수" if inst5 > 0 and frgn5 > 0
                else "기관+외국인 동반매도" if inst5 < 0 and frgn5 < 0
                else "기관매수/외국인매도" if inst5 > 0
                else "기관매도/외국인매수"
            ),
        }

    result = {"code": code, "rows": rows[:10], "summary": summary}
    _trader_flow_cache[code] = result
    _trader_flow_cache_time[code] = now
    return result

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
        "temperature": 0.3,
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
    
    # 종목별 요약 요청 처리
    if "요약" in user_message or "summary" in user_message.lower():
        system_prompt += "[종목별 요약 요청]\n"
        system_prompt += "사용자가 종목별 요약을 요청했습니다. 각 종목에 대해 2줄로 요약하세요.\n"
        system_prompt += "형식:\n"
        system_prompt += "**[종목명]** (현재가, 수익률)\n"
        system_prompt += "• 추세: 단기/중기/장기 추세 상태\n"
        system_prompt += "• 의견: 한 줄 요약\n\n"
        system_prompt += "예시:\n"
        system_prompt += "**삼성전자** (276,000원, -8.27%)\n"
        system_prompt += "• 추세: 단기 상승세약화, 중기 하락지속, 장기 하락지속\n"
        system_prompt += "• 의견: 하락 추세 지속 중이나 단기 반등 가능성 있음\n\n"

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
