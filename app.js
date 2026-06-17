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

function signalBadge(signal, reasons) {
  const s = SIGNAL_LABELS[signal] || SIGNAL_LABELS.hold;
  const dataAttr = reasons && reasons.length
    ? `data-reasons='${JSON.stringify(reasons).replace(/'/g, "&#39;")}'`
    : '';
  return `<span class="badge ${s.cls}" data-signal="${signal}" ${dataAttr}>${s.text}</span>`;
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
function showSignalModal(name, signal, reasons) {
  const s = SIGNAL_LABELS[signal] || SIGNAL_LABELS.hold;
  modalTitle.textContent = `${name} — ${s.text}`;
  let html = '<ul class="signal-reasons">';
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
      const signal = el.dataset.signal;
      const reasons = el.dataset.reasons ? JSON.parse(el.dataset.reasons) : [];
      showSignalModal(name, signal, reasons);
    });
  });
}

function renderHoldings(rows) {
  holdingsBody.innerHTML = '';
  rows.forEach(row => {
    const tr = document.createElement('tr');
    const badgeHtml = signalBadge(row.trend.signal, row.trend.signalReasons || []);
    tr.innerHTML = `
      <td><div class="name-cell"><strong>${row.name}</strong><small>${row.code}</small></div></td>
      <td>${money.format(row.quantity)}주</td>
      <td>${formatMoney(row.currentPrice)}</td>
      <td class="${row.change > 0 ? 'up' : row.change < 0 ? 'down' : 'neutral'}">${formatSignedMoney(row.change)} / ${formatPercent(row.changeRate)}</td>
      <td>${formatMoney(row.avgPrice)}</td>
      <td>${formatMoney(row.currentValue)}</td>
      <td class="${row.profit >= 0 ? 'up' : 'down'}">${formatPercent(row.profitRate)}</td>
      <td class="${row.profit >= 0 ? 'up' : 'down'}">${formatSignedMoney(row.profit)}</td>
      <td>${badgeHtml}</td>
      <td>${row.session || row.error || '-'}</td>
    `;
    holdingsBody.appendChild(tr);
  });
  attachSignalHandlers(holdingsBody);
}

function renderWatchlist(rows) {
  watchlistBody.innerHTML = '';
  rows.forEach(row => {
    const t = row.trend;
    const dayRange = (row.high && row.low)
      ? `${formatMoney(row.low)} ~ ${formatMoney(row.high)}`
      : '-';
    const tr = document.createElement('tr');
    const badgeHtml = signalBadge(t.signal, t.signalReasons || []);
    tr.innerHTML = `
      <td><div class="name-cell"><strong>${row.name}</strong><small>${row.code}</small></div></td>
      <td>${formatMoney(row.currentPrice)}</td>
      <td class="${row.change > 0 ? 'up' : row.change < 0 ? 'down' : 'neutral'}">${formatSignedMoney(row.change)} / ${formatPercent(row.changeRate)}</td>
      <td>${dayRange}<br>${rangeBar(t.rangePos)}</td>
      <td>${t.volatility}%</td>
      <td>${trendIcon(t.shortTrend)} ${t.shortTrend === 'up' ? '상승' : t.shortTrend === 'down' ? '하락' : '보합'}</td>
      <td>${badgeHtml}${t.signal !== 'hold' && t.signalReasons.length ? '<br><small>' + t.signalReasons.join(', ') + '</small>' : ''}</td>
      <td>${row.session || row.error || '-'}</td>
    `;
    watchlistBody.appendChild(tr);
  });
  attachSignalHandlers(watchlistBody);
}

function renderSummary(summary) {
  const totalValue = document.querySelector('#totalValue');
  const totalCost = document.querySelector('#totalCost');
  const totalProfit = document.querySelector('#totalProfit');
  const totalRate = document.querySelector('#totalRate');

  totalValue.textContent = formatThousand(summary.currentValue);
  totalCost.textContent = formatThousand(summary.cost);
  totalProfit.textContent = formatSignedThousand(summary.profit);
  totalRate.textContent = formatPercent(summary.profitRate);
  setSignedClass(totalProfit, summary.profit);
  setSignedClass(totalRate, summary.profitRate);
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
  } catch (error) {
    newsContainer.innerHTML = '<p class="muted">뉴스를 불러오지 못했습니다.</p>';
  }
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
  newsTimer = setInterval(loadNews, 120000);
  newsRefreshBtn.addEventListener('click', () => { loadNews(); });
}

refreshBtn.addEventListener('click', loadPortfolio);
autoBtn.addEventListener('click', () => setAutoRefresh(!autoEnabled));

initResize();
loadPortfolio();
loadNews();
setAutoRefresh(true);
setupNewsRefresh();
