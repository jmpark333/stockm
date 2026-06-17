const money = new Intl.NumberFormat('ko-KR');
const percent = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 2 });
const refreshBtn = document.querySelector('#refreshBtn');
const autoBtn = document.querySelector('#autoBtn');
const errorBox = document.querySelector('#errorBox');
const holdingsBody = document.querySelector('#holdingsBody');
const watchlistBody = document.querySelector('#watchlistBody');
const statusPill = document.querySelector('#statusPill');
const sourceText = document.querySelector('#sourceText');
const newsContainer = document.querySelector('#newsContainer');
const newsRefreshBtn = document.querySelector('#newsRefreshBtn');
const signalModal = document.querySelector('#signalModal');
const modalTitle = document.querySelector('#modalTitle');
const modalBody = document.querySelector('#modalBody');
const modalClose = document.querySelector('#modalClose');

let autoTimer = null;
let autoEnabled = true;
let newsTimer = null;

const aiResults = new Map();
let profitHistory = [];

/* Sidebar Resize */
const sidebar = document.querySelector('#sidebar');
const resizeHandle = document.querySelector('#resizeHandle');
const sidebarToggle = document.querySelector('#sidebarToggle');
const sidebarClose = document.querySelector('#sidebarCloseBtn');
const sidebarOverlay = document.querySelector('#sidebarOverlay');
let isResizing = false;

function toggleSidebar(open) {
  sidebar.classList.toggle('open', open);
  sidebarOverlay.classList.toggle('show', open);
  document.body.style.overflow = open ? 'hidden' : '';
}

sidebarToggle.addEventListener('click', () => toggleSidebar(true));
sidebarClose.addEventListener('click', () => toggleSidebar(false));
sidebarOverlay.addEventListener('click', () => toggleSidebar(false));

function initResize() {
  const saved = localStorage.getItem('sidebarWidth');
  if (saved) {
    sidebar.style.setProperty('--sidebar-width', saved + 'px');
  }
  resizeHandle.addEventListener('mousedown', (e) => {
    isResizing = true;
    resizeHandle.classList.add('active');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });
  document.addEventListener('mousemove', (e) => {
    if (!isResizing) return;
    const width = Math.min(500, Math.max(240, e.clientX));
    sidebar.style.setProperty('--sidebar-width', width + 'px');
    localStorage.setItem('sidebarWidth', width);
  });
  document.addEventListener('mouseup', () => {
    if (!isResizing) return;
    isResizing = false;
    resizeHandle.classList.remove('active');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });
}

function formatMoney(value) {
  return `${money.format(Math.round(value || 0))}원`;
}

function formatThousand(value) {
  return `${money.format(Math.round((value || 0) / 1000))}천원`;
}

function formatSignedThousand(value) {
  const prefix = value > 0 ? '+' : '';
  return `${prefix}${money.format(Math.round(Math.abs(value || 0) / 1000))}천원`;
}

function formatSignedMoney(value) {
  const prefix = value > 0 ? '+' : '';
  return `${prefix}${money.format(Math.round(value || 0))}원`;
}

function formatPercent(value) {
  const prefix = value > 0 ? '+' : '';
  return `${prefix}${percent.format(value || 0)}%`;
}

function setSignedClass(element, value) {
  element.classList.remove('up', 'down', 'neutral');
  if (value > 0) element.classList.add('up');
  if (value < 0) element.classList.add('down');
  if (value === 0) element.classList.add('neutral');
}

function setError(message) {
  if (!message) {
    errorBox.hidden = true;
    errorBox.textContent = '';
    return;
  }
  errorBox.hidden = false;
  errorBox.textContent = message;
}

const SIGNAL_LABELS = {
  strong_buy: { text: '🔴 강력매수', cls: 'strong-buy' },
  buy: { text: '🟠 매수', cls: 'buy' },
  hold: { text: '⚪ 관망', cls: 'hold' },
  sell: { text: '🟡 매도', cls: 'sell' },
  strong_sell: { text: '🟢 강력매도', cls: 'strong-sell' },
};

function signalBadge(signal, reasons, opts) {
  opts = opts || {};
  const s = SIGNAL_LABELS[signal] || SIGNAL_LABELS.hold;
  const prefix = opts.isAI ? 'AI ' : '';
  const dataAttr = reasons && reasons.length
    ? `data-reasons='${JSON.stringify(reasons).replace(/'/g, "&#39;")}'`
    : '';
  const nsAttr = opts.newsSentiment ? ` data-news-sentiment="${opts.newsSentiment.replace(/"/g, '&quot;')}"` : '';
  const aiAttr = opts.isAI ? ' data-ai="1"' : '';
  return `<span class="badge ${s.cls}" data-signal="${signal}"${dataAttr}${nsAttr}${aiAttr}>${prefix}${s.text}</span>`;
}

function trendIcon(trend) {
  if (trend === 'up') return '<span class="up">▲</span>';
  if (trend === 'down') return '<span class="down">▼</span>';
  return '<span class="neutral">―</span>';
}

function rangeBar(pos) {
  const pct = Math.max(0, Math.min(100, pos));
  const color = pct > 66 ? '#ff4d5e' : pct > 33 ? '#fbbf24' : '#2dd4bf';
  return `<div class="range-bar"><div class="range-fill" style="width:${pct}%;background:${color}"></div></div><small>${pct}%</small>`;
}

/* Signal detail modal */
function showSignalModal(name, signal, reasons, aiAnalysis) {
  const s = SIGNAL_LABELS[signal] || SIGNAL_LABELS.hold;
  modalTitle.textContent = `${name} — ${s.text}`;
  let html = '';

  if (aiAnalysis && aiAnalysis.currentPrice) {
    const cp = aiAnalysis.currentPrice;
    const pc = aiAnalysis.previousClose;
    const chg = aiAnalysis.change;
    const chgRate = aiAnalysis.changeRate;
    const high = aiAnalysis.high;
    const low = aiAnalysis.low;
    const chgClass = chg > 0 ? 'up' : chg < 0 ? 'down' : 'neutral';
    html += '<div class="stock-info">';
    html += `<div class="stock-price"><strong>${formatMoney(cp)}</strong></div>`;
    html += `<div class="stock-change ${chgClass}">${formatSignedMoney(chg)} / ${formatPercent(chgRate)}</div>`;
    if (high && low) {
      html += `<div class="stock-range">고가: ${formatMoney(high)} / 저가: ${formatMoney(low)}</div>`;
    }
    html += '</div>';
  }

  html += '<h3>📊 시그널 분석</h3>';
  if (aiAnalysis && aiAnalysis.reasons && aiAnalysis.reasons.length) {
    html += '<ul class="signal-reasons">';
    aiAnalysis.reasons.forEach(r => html += `<li>${r}</li>`);
    html += '</ul>';
    if (aiAnalysis.newsSentiment) {
      html += `<p style="margin-top:8px;font-size:13px;color:var(--muted)">뉴스 감성: ${aiAnalysis.newsSentiment}</p>`;
    }
  } else {
    html += '<ul class="signal-reasons">';
    if (reasons && reasons.length) {
      reasons.forEach(r => { html += `<li>${r}</li>`; });
    } else {
      html += '<li class="muted">특이사항 없음</li>';
    }
    html += '</ul>';
    html += '<div class="signal-guide">';
    if (signal === 'strong_buy' || signal === 'buy') {
      html += '<p>📈 <strong>매수 고려</strong>: 현재 하락 구간으로 추가 하락 시 분할 매수 전략을 고려하세요.</p>';
    } else if (signal === 'strong_sell' || signal === 'sell') {
      html += '<p>📉 <strong>매도 고려</strong>: 현재 상승 구간으로 차익실현을 고려하세요.</p>';
    } else {
      html += '<p>⏸️ <strong>관망</strong>: 현재 특별한 시그널이 없는 구간입니다. 추세를 지켜보세요.</p>';
    }
    html += '</div>';
  }

  if (aiAnalysis && aiAnalysis.news && aiAnalysis.news.length) {
    html += '<div class="news-links">';
    html += '<h3>📰 관련 뉴스</h3>';
    html += '<div class="news-list">';
    aiAnalysis.news.forEach(a => {
      const src = a.source ? `<span class="news-source">${a.source}</span>` : '';
      html += `<a class="news-item" href="${a.url}" target="_blank" rel="noopener">
        <div class="news-title">${a.title}</div>
        <div class="news-meta">${src}</div>
      </a>`;
    });
    html += '</div>';
    html += `<a class="news-more" href="https://finance.naver.com/item/news.naver?code=${aiAnalysis.stockCode}" target="_blank" rel="noopener">네이버 금융 뉴스 보기 →</a>`;
    html += '</div>';
  }

  modalBody.innerHTML = html;
  signalModal.hidden = false;
}

function closeSignalModal() {
  signalModal.hidden = true;
}

modalClose.addEventListener('click', closeSignalModal);
signalModal.addEventListener('click', e => {
  if (e.target === signalModal) closeSignalModal();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeSignalModal();
});

/* Attach signal click handlers */
function attachSignalHandlers(container) {
  container.querySelectorAll('.badge[data-signal]').forEach(el => {
    el.style.cursor = 'pointer';
    el.addEventListener('click', () => {
      const row = el.closest('tr');
      if (!row) return;
      const name = row.querySelector('.name-cell strong')?.textContent || '';
      const code = row.querySelector('.name-cell small')?.textContent || '';
      const signal = el.dataset.signal;
      const reasons = el.dataset.reasons ? JSON.parse(el.dataset.reasons) : [];
      const ai = el.dataset.ai;
      const cached = aiResults.get(code);
      const aiAnalysis = cached ? {
        reasons: cached.reasons || [],
        newsSentiment: cached.newsSentiment || '',
        news: cached.news || [],
        stockName: cached.stockName || name,
        stockCode: cached.stockCode || code,
        currentPrice: cached.currentPrice,
        previousClose: cached.previousClose,
        change: cached.change,
        changeRate: cached.changeRate,
        high: cached.high,
        low: cached.low,
      } : (ai ? { reasons, newsSentiment: el.dataset.newsSentiment || '' } : null);
      showSignalModal(name, signal, ai ? [] : reasons, aiAnalysis);
    });
  });
}

/* Attach AI analysis button handlers */
function attachAIHandlers(container) {
  container.querySelectorAll('.ai-btn:not(.done)').forEach(btn => {
    btn.addEventListener('click', async () => {
      const code = btn.dataset.code;
      const name = btn.dataset.name;
      btn.textContent = '⏳';
      btn.disabled = true;
      let data;
      try {
        const res = await fetch(`/api/analyze-signal?code=${code}`, { cache: 'no-store' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        data = await res.json();
        if (!data.signal) throw new Error(data.error || '분석 실패');
      } catch (err) {
        btn.textContent = 'AI';
        btn.disabled = false;
        setError(`${name} AI 분석 실패: ${err.message}`);
        return;
      }
      aiResults.set(code, {
        signal: data.signal,
        reasons: data.reasons || [],
        newsSentiment: data.newsSentiment || '',
        news: data.news || [],
        stockName: data.stockName || name,
        stockCode: data.stockCode || code,
        currentPrice: data.currentPrice,
        previousClose: data.previousClose,
        change: data.change,
        changeRate: data.changeRate,
        high: data.high,
        low: data.low,
      });
      const td = btn.closest('td');
      const badge = td.querySelector('.badge');
      const s = SIGNAL_LABELS[data.signal] || SIGNAL_LABELS.hold;
      badge.className = `badge ${s.cls}`;
      badge.textContent = `AI ${s.text}`;
      badge.dataset.signal = data.signal;
      badge.dataset.reasons = JSON.stringify(data.reasons || []);
      badge.dataset.ai = '1';
      if (data.newsSentiment) {
        badge.dataset.newsSentiment = data.newsSentiment;
      }
      btn.textContent = '✓';
      btn.classList.add('done');
      setError('');
      showSignalModal(name, data.signal, data.reasons || [], {
        reasons: data.reasons || [],
        newsSentiment: data.newsSentiment || '',
        news: data.news || [],
        stockName: data.stockName || name,
        stockCode: data.stockCode || code,
        currentPrice: data.currentPrice,
        previousClose: data.previousClose,
        change: data.change,
        changeRate: data.changeRate,
        high: data.high,
        low: data.low,
      });
    });
  });
}

function renderHoldings(rows) {
  holdingsBody.innerHTML = '';
  rows.forEach(row => {
    const tr = document.createElement('tr');
    const cached = aiResults.get(row.code);
    const badgeHtml = cached
      ? signalBadge(cached.signal, cached.reasons, { isAI: true, newsSentiment: cached.newsSentiment })
      : `<span class="badge hold">AI 분석중</span>`;
    const aiBtnHtml = cached
      ? `<button class="ai-btn done" data-code="${row.code}" data-name="${row.name}">✓</button>`
      : `<button class="ai-btn" data-code="${row.code}" data-name="${row.name}" title="AI 뉴스 분석">AI</button>`;
    tr.innerHTML = `
      <td><div class="name-cell"><strong>${row.name}</strong><small>${row.code}</small></div></td>
      <td>${money.format(row.quantity)}주</td>
      <td>${formatMoney(row.currentPrice)}</td>
      <td class="${row.change > 0 ? 'up' : row.change < 0 ? 'down' : 'neutral'}">${formatSignedMoney(row.change)} / ${formatPercent(row.changeRate)}</td>
      <td>${formatMoney(row.avgPrice)}</td>
      <td>${formatMoney(row.currentValue)}</td>
      <td class="${row.profit >= 0 ? 'up' : 'down'}">${formatPercent(row.profitRate)}</td>
      <td class="${row.profit >= 0 ? 'up' : 'down'}">${formatSignedMoney(row.profit)}</td>
      <td>${badgeHtml} ${aiBtnHtml}</td>
      <td>${row.session || row.error || '-'}</td>
    `;
    holdingsBody.appendChild(tr);
  });
  attachSignalHandlers(holdingsBody);
  attachAIHandlers(holdingsBody);
}

function renderWatchlist(rows) {
  watchlistBody.innerHTML = '';
  rows.forEach(row => {
    const t = row.trend;
    const dayRange = (row.high && row.low)
      ? `${formatMoney(row.low)} ~ ${formatMoney(row.high)}`
      : '-';
    const tr = document.createElement('tr');
    const cached = aiResults.get(row.code);
    const badgeHtml = cached
      ? signalBadge(cached.signal, cached.reasons, { isAI: true, newsSentiment: cached.newsSentiment })
      : `<span class="badge hold">AI 분석중</span>`;
    const aiBtnHtml = cached
      ? `<button class="ai-btn done" data-code="${row.code}" data-name="${row.name}">✓</button>`
      : `<button class="ai-btn" data-code="${row.code}" data-name="${row.name}" title="AI 뉴스 분석">AI</button>`;
    tr.innerHTML = `
      <td><div class="name-cell"><strong>${row.name}</strong><small>${row.code}</small></div></td>
      <td>${formatMoney(row.currentPrice)}</td>
      <td class="${row.change > 0 ? 'up' : row.change < 0 ? 'down' : 'neutral'}">${formatSignedMoney(row.change)} / ${formatPercent(row.changeRate)}</td>
      <td>${dayRange}<br>${rangeBar(t.rangePos)}</td>
      <td>${t.volatility}%</td>
      <td>${trendIcon(t.shortTrend)} ${t.shortTrend === 'up' ? '상승' : t.shortTrend === 'down' ? '하락' : '보합'}</td>
      <td>${badgeHtml} ${aiBtnHtml}</td>
      <td>${row.session || row.error || '-'}</td>
    `;
    watchlistBody.appendChild(tr);
  });
  attachSignalHandlers(watchlistBody);
  attachAIHandlers(watchlistBody);
}

function renderSummary(summary) {
  const totalValue = document.querySelector('#totalValue');
  const totalCost = document.querySelector('#totalCost');
  const totalProfit = document.querySelector('#totalProfit');
  const totalRate = document.querySelector('#totalRate');
  const profitTrend = document.querySelector('#profitTrend');

  totalValue.textContent = formatThousand(summary.currentValue);
  totalCost.textContent = formatThousand(summary.cost);
  totalProfit.textContent = formatSignedThousand(summary.profit);
  totalRate.textContent = formatPercent(summary.profitRate);
  setSignedClass(totalProfit, summary.profit);
  setSignedClass(totalRate, summary.profitRate);

  profitHistory.push(summary.profit);
  if (profitHistory.length > 12) profitHistory.shift();
  let trend = 'flat';
  if (profitHistory.length >= 3) {
    const first = profitHistory[0];
    const last = profitHistory[profitHistory.length - 1];
    if (last > first * 1.0005) trend = 'up';
    else if (last < first * 0.9995) trend = 'down';
  }
  if (profitTrend) {
    profitTrend.textContent = trend === 'up' ? '▲' : trend === 'down' ? '▼' : '―';
    profitTrend.className = 'trend-icon ' + trend;
  }
}

function renderNews(newsItems) {
  newsContainer.innerHTML = '';
  if (!newsItems || !newsItems.length) {
    newsContainer.innerHTML = '<p class="muted">뉴스를 불러올 수 없습니다.</p>';
    return;
  }
  newsItems.forEach(item => {
    const section = document.createElement('div');
    section.className = 'news-section';
    let html = `<div class="news-stock"><strong>${item.name}</strong> <small>${item.code}</small></div>`;
    if (item.articles && item.articles.length) {
      html += '<div class="news-list">';
      item.articles.forEach(a => {
        const src = a.source ? `<span class="news-source">${a.source}</span>` : '';
        html += `<a class="news-item" href="${a.url}" target="_blank" rel="noopener">
          <div class="news-title">${a.title}</div>
          <div class="news-meta">${src}</div>
        </a>`;
      });
      html += '</div>';
    } else {
      html += '<p class="muted">최근 뉴스 없음</p>';
    }
    html += `<a class="news-more" href="https://finance.naver.com/item/news.naver?code=${item.code}" target="_blank" rel="noopener">네이버 금융 뉴스 보기 →</a>`;
    section.innerHTML = html;
    newsContainer.appendChild(section);
  });
}

async function analyzeAllStocks(holdings, watchlist) {
  const allCodes = [
    ...holdings.map(h => ({ code: h.code, name: h.name })),
    ...watchlist.map(w => ({ code: w.code, name: w.name }))
  ];
  
  for (const item of allCodes) {
    if (aiResults.has(item.code)) continue;
    try {
      const res = await fetch(`/api/analyze-signal?code=${item.code}`, { cache: 'no-store' });
      if (!res.ok) continue;
      const data = await res.json();
      if (!data.signal) continue;
      aiResults.set(item.code, {
        signal: data.signal,
        reasons: data.reasons || [],
        newsSentiment: data.newsSentiment || '',
        news: data.news || [],
        stockName: data.stockName || item.name,
        stockCode: data.stockCode || item.code,
        currentPrice: data.currentPrice,
        previousClose: data.previousClose,
        change: data.change,
        changeRate: data.changeRate,
        high: data.high,
        low: data.low,
      });
      updateSignalBadge(item.code);
    } catch (err) {
      continue;
    }
  }
}

function updateSignalBadge(code) {
  const cached = aiResults.get(code);
  if (!cached) return;
  const s = SIGNAL_LABELS[cached.signal] || SIGNAL_LABELS.hold;
  const badgeHtml = signalBadge(cached.signal, cached.reasons, { isAI: true, newsSentiment: cached.newsSentiment });
  
  document.querySelectorAll('tr').forEach(tr => {
    const small = tr.querySelector('.name-cell small');
    if (small && small.textContent === code) {
      const badge = tr.querySelector('.badge');
      if (badge) {
        const temp = document.createElement('div');
        temp.innerHTML = badgeHtml;
        badge.replaceWith(temp.firstChild);
      }
      const aiBtn = tr.querySelector('.ai-btn');
      if (aiBtn) {
        aiBtn.textContent = '✓';
        aiBtn.classList.add('done');
      }
    }
  });
}

async function loadPortfolio() {
  refreshBtn.disabled = true;
  statusPill.textContent = '갱신 중';
  setError('');
  try {
    const response = await fetch('/api/portfolio', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderSummary(data.summary);
    renderHoldings(data.holdings);
    renderWatchlist(data.watchlist);
    sourceText.textContent = `네이버 금융 polling API · ${data.refreshSeconds}초 자동 갱신`;
    statusPill.textContent = `정상 · ${new Date().toLocaleTimeString('ko-KR')}`;
    analyzeAllStocks(data.holdings, data.watchlist);
  } catch (error) {
    statusPill.textContent = '오류';
    setError(`데이터를 불러오지 못했습니다. 서버가 실행 중인지 확인하세요. ${error.message}`);
  } finally {
    refreshBtn.disabled = false;
  }
}

async function loadNews() {
  try {
    const response = await fetch('/api/news', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderNews(data);
    refreshAISignals();
  } catch (error) {
    newsContainer.innerHTML = '<p class="muted">뉴스를 불러오지 못했습니다.</p>';
  }
}

function refreshAISignals() {
  aiResults.clear();
  document.querySelectorAll('.ai-btn.done').forEach(btn => {
    btn.textContent = 'AI';
    btn.classList.remove('done');
  });
  document.querySelectorAll('.badge').forEach(badge => {
    if (badge.dataset.ai) {
      badge.textContent = 'AI 분석중';
      badge.className = 'badge hold';
      delete badge.dataset.signal;
      delete badge.dataset.reasons;
      delete badge.dataset.ai;
    }
  });
  fetch('/api/portfolio', { cache: 'no-store' })
    .then(res => res.json())
    .then(data => {
      analyzeAllStocks(data.holdings, data.watchlist);
    })
    .catch(() => {});
}

function setAutoRefresh(enabled) {
  autoEnabled = enabled;
  autoBtn.setAttribute('aria-pressed', String(enabled));
  autoBtn.textContent = enabled ? '자동 갱신 ON' : '자동 갱신 OFF';
  if (autoTimer) {
    clearInterval(autoTimer);
    autoTimer = null;
  }
  if (enabled) {
    autoTimer = setInterval(loadPortfolio, 10000);
  }
}

function setupNewsRefresh() {
  if (newsTimer) clearInterval(newsTimer);
  newsTimer = setInterval(loadNews, 600000);
  newsRefreshBtn.addEventListener('click', () => { loadNews(); });
}

refreshBtn.addEventListener('click', loadPortfolio);
autoBtn.addEventListener('click', () => setAutoRefresh(!autoEnabled));

initResize();
loadPortfolio();
loadNews();
setAutoRefresh(true);
setupNewsRefresh();
