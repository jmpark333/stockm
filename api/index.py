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
                    clean = re.sub(r'<[^>]+>', '', exday_content)
                    clean = re.sub(r'\s+', ' ', clean).strip()
                    nums = re.findall(r'[\d,]+\.?\d*', clean)
                    if len(nums) >= 2:
                        change = float(nums[0].replace(",", ""))
                        rate = float(nums[1])
                        # Check if it's negative (minus sign)
                        if "-" in clean and "+" not in clean:
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
