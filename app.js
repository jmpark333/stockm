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
const newsUpdatedAt = document.querySelector('#newsUpdatedAt');
const aiUpdatedAt = document.querySelector('#aiUpdatedAt');
const chartModal = document.querySelector('#chartModal');
const chartModalTitle = document.querySelector('#chartModalTitle');
const chartModalClose = document.querySelector('#chartModalClose');
const tvChartContainer = document.querySelector('#tvChartContainer');

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

let lwChart = null;
let lwChartReady = false;

function loadLightweightCharts() {
  return new Promise((resolve, reject) => {
    if (window.LightweightCharts) { resolve(); return; }
    const s = document.createElement('script');
    s.src = 'https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js';
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('Failed to load lightweight-charts'));
    document.head.appendChild(s);
  });
}

function showChartModal(name, code) {
  chartModalTitle.textContent = `${name} 주가 차트`;
  if (lwChart) { try { lwChart.remove(); } catch(e){} lwChart = null; }
  tvChartContainer.innerHTML = '<p style="text-align:center;padding:40px;color:var(--muted)">차트 로딩 중...</p>';
  chartModal.hidden = false;

  loadLightweightCharts().then(() => {
    return fetch(`/api/chart?code=${code}`).then(r => r.json());
  }).then(data => {
    const candles = data.candles || [];
    if (!candles.length) {
      tvChartContainer.innerHTML = '<p style="text-align:center;padding:40px;color:var(--muted)">차트 데이터를 불러올 수 없습니다.</p>';
      return;
    }

    tvChartContainer.innerHTML = '';
    const chartOptions = {
      layout: {
        background: { color: '#131722' },
        textColor: '#d1d4dc',
        fontSize: 12,
      },
      grid: {
        vertLines: { color: 'rgba(42,46,63,0.5)' },
        horzLines: { color: 'rgba(42,46,63,0.5)' },
      },
      crosshair: { mode: 0 },
      rightPriceScale: { borderColor: 'rgba(42,46,63,1)' },
      timeScale: {
        borderColor: 'rgba(42,46,63,1)',
        timeVisible: false,
      },
      localization: {
        priceFormatter: p => Math.round(p).toLocaleString('ko-KR') + '원',
        locale: 'ko-KR',
      },
    };
    lwChart = LightweightCharts.createChart(tvChartContainer, chartOptions);

    const candleSeries = lwChart.addCandlestickSeries({
      upColor: '#ef5350',
      downColor: '#26a69a',
      borderUpColor: '#ef5350',
      borderDownColor: '#26a69a',
      wickUpColor: '#ef5350',
      wickDownColor: '#26a69a',
    });

    const volumeSeries = lwChart.addHistogramSeries({
      color: 'rgba(76,175,80,0.3)',
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    });
    lwChart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.85, bottom: 0 },
    });

    const candleData = candles.map(c => ({
      time: c.time,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));
    const volumeData = candles.map(c => ({
      time: c.time,
      value: c.volume,
      color: c.close >= c.open ? 'rgba(239,83,80,0.3)' : 'rgba(38,166,154,0.3)',
    }));

    candleSeries.setData(candleData);
    volumeSeries.setData(volumeData);

    lwChart.timeScale().fitContent();

    const resizeObserver = new ResizeObserver(entries => {
      if (entries.length > 0 && lwChart) {
        const { width, height } = entries[0].contentRect;
        lwChart.applyOptions({ width: Math.floor(width), height: Math.floor(height) });
      }
    });
    resizeObserver.observe(tvChartContainer);
  }).catch(err => {
    tvChartContainer.innerHTML = '<p style="text-align:center;padding:40px;color:var(--muted)">차트 로드 실패: ' + err.message + '</p>';
  });
}

function closeChartModal() {
  chartModal.hidden = true;
  if (lwChart) { try { lwChart.remove(); } catch(e){} lwChart = null; }
  tvChartContainer.innerHTML = '';
}

chartModalClose.addEventListener('click', closeChartModal);
chartModal.addEventListener('click', e => {
  if (e.target === chartModal) closeChartModal();
});

/* Water-Average Calculator Modal */
const calcModal = document.querySelector('#calcModal');
const calcModalTitle = document.querySelector('#calcModalTitle');
const calcModalClose = document.querySelector('#calcModalClose');
const calcName = document.querySelector('#calcName');
const calcQty = document.querySelector('#calcQty');
const calcAvgPrice = document.querySelector('#calcAvgPrice');
const calcCurPrice = document.querySelector('#calcCurPrice');
const calcCurValue = document.querySelector('#calcCurValue');
const calcCurProfit = document.querySelector('#calcCurProfit');
const calcAddPrice = document.querySelector('#calcAddPrice');
const calcAddQty = document.querySelector('#calcAddQty');
const calcAddCost = document.querySelector('#calcAddCost');
const calcNewAvg = document.querySelector('#calcNewAvg');
const calcNewQty = document.querySelector('#calcNewQty');
const calcNewTotalCost = document.querySelector('#calcNewTotalCost');
const calcNewValue = document.querySelector('#calcNewValue');
const calcNewProfit = document.querySelector('#calcNewProfit');
const calcAvgDiff = document.querySelector('#calcAvgDiff');
const calcAvgDiffRate = document.querySelector('#calcAvgDiffRate');

let calcState = { avgPrice: 0, quantity: 0, currentPrice: 0, totalCost: 0, profit: 0 };
let calcMode = 'buy';

function updateCalcResult() {
  const addP = parseFloat(calcAddPrice.value) || 0;
  const addQ = parseInt(calcAddQty.value) || 0;
  const diffRow = document.querySelector('#calcDiffRow');
  const diffEl = document.querySelector('#calcDiff');

  if (addP <= 0 || addQ <= 0) {
    calcAddCost.textContent = '0원';
    calcNewAvg.textContent = '-';
    calcNewQty.textContent = '-';
    calcNewTotalCost.textContent = '-';
    calcNewValue.textContent = '-';
    calcNewProfit.textContent = '-';
    calcNewProfit.className = '';
    calcAvgDiff.textContent = '-';
    calcAvgDiff.className = '';
    calcAvgDiffRate.textContent = '-';
    calcAvgDiffRate.className = '';
    diffRow.hidden = true;
    return;
  }

  if (calcMode === 'sell') {
    const sellQty = Math.min(addQ, calcState.quantity);
    const addCost = 0;
    const newQty = calcState.quantity - sellQty;
    const newTotalCost = calcState.avgPrice * newQty;
    const newValue = calcState.currentPrice * newQty;
    const newProfit = (calcState.currentPrice - calcState.avgPrice) * newQty;
    const avgDiff = 0;
    const avgDiffRate = 0;
    const sellProfit = (addP - calcState.avgPrice) * sellQty;

    calcAddCost.textContent = formatSignedMoney(sellProfit);
    calcAddCost.className = sellProfit >= 0 ? 'up' : 'down';
    calcNewAvg.textContent = formatMoney(calcState.avgPrice);
    calcNewQty.textContent = newQty.toLocaleString('ko-KR') + '주';
    calcNewTotalCost.textContent = formatMoney(newTotalCost);
    calcNewValue.textContent = formatMoney(newValue);
    calcNewProfit.textContent = formatSignedMoney(newProfit);
    calcNewProfit.className = newProfit >= 0 ? 'up' : 'down';
    calcAvgDiff.textContent = formatSignedMoney(avgDiff);
    calcAvgDiff.className = 'neutral';
    calcAvgDiffRate.textContent = formatPercent(avgDiffRate);
    calcAvgDiffRate.className = 'neutral';

    diffRow.hidden = false;
    diffEl.textContent = formatSignedMoney(sellProfit);
    diffEl.className = sellProfit >= 0 ? 'up' : 'down';
  } else {
    const addCost = addP * addQ;
    const newQty = calcState.quantity + addQ;
    const newTotalCost = calcState.totalCost + addCost;
    const newAvg = newQty > 0 ? newTotalCost / newQty : 0;
    const newValue = calcState.currentPrice * newQty;
    const newProfit = (calcState.currentPrice - newAvg) * newQty;
    const avgDiff = newAvg - calcState.avgPrice;
    const avgDiffRate = calcState.avgPrice > 0 ? (avgDiff / calcState.avgPrice) * 100 : 0;

    calcAddCost.textContent = formatMoney(addCost);
    calcNewAvg.textContent = formatMoney(newAvg);
    calcNewQty.textContent = newQty.toLocaleString('ko-KR') + '주';
    calcNewTotalCost.textContent = formatMoney(newTotalCost);
    calcNewValue.textContent = formatMoney(newValue);
    calcNewProfit.textContent = formatSignedMoney(newProfit);
    calcNewProfit.className = newProfit >= 0 ? 'up' : 'down';
    calcAvgDiff.textContent = formatSignedMoney(avgDiff);
    calcAvgDiff.className = avgDiff >= 0 ? 'down' : 'up';
    calcAvgDiffRate.textContent = formatPercent(avgDiffRate);
    calcAvgDiffRate.className = avgDiff >= 0 ? 'down' : 'up';

    diffRow.hidden = false;
    const profitDiff = newProfit - calcState.profit;
    diffEl.textContent = formatSignedMoney(profitDiff);
    diffEl.className = profitDiff >= 0 ? 'up' : 'down';
  }
}

function showCalcModal(name, avgPrice, quantity, currentPrice) {
  const totalCost = avgPrice * quantity;
  const currentValue = currentPrice * quantity;
  const profit = currentValue - totalCost;

  calcState = { avgPrice, quantity, currentPrice, totalCost, profit };

  calcModalTitle.textContent = `물타기 계산기 — ${name}`;
  calcName.textContent = name;
  calcQty.textContent = quantity.toLocaleString('ko-KR') + '주';
  calcAvgPrice.textContent = formatMoney(avgPrice);
  calcCurPrice.textContent = formatMoney(currentPrice);
  calcCurValue.textContent = formatMoney(currentValue);
  calcCurProfit.textContent = formatSignedMoney(profit);
  calcCurProfit.className = profit >= 0 ? 'up' : 'down';

  calcAddPrice.value = '';
  calcAddQty.value = '';
  calcAddPrice.placeholder = formatMoney(currentPrice).replace('원', '');
  calcAddCost.textContent = '0원';
  calcNewAvg.textContent = '-';
  calcNewQty.textContent = '-';
  calcNewTotalCost.textContent = '-';
  calcNewValue.textContent = '-';
  calcNewProfit.textContent = '-';
  calcNewProfit.className = '';
  calcAvgDiff.textContent = '-';
  calcAvgDiff.className = '';
  calcAvgDiffRate.textContent = '-';
  calcAvgDiffRate.className = '';

  calcMode = 'buy';
  document.querySelectorAll('.calc-tab').forEach(t => t.classList.remove('active'));
  document.querySelector('.calc-tab[data-type="buy"]').classList.add('active');
  document.querySelector('#calcPriceLabel').textContent = '추가 매수가';
  document.querySelector('#calcQtyLabel').textContent = '추가 매수량';
  document.querySelector('#calcCostLabel').textContent = '추가 투자금';
  document.querySelector('#calcAvgLabel').textContent = '새 평균단가';
  document.querySelector('#calcQtyResultLabel').textContent = '새 총 수량';
  document.querySelector('#calcTotalCostLabel').textContent = '새 총 투자금';
  document.querySelector('#calcValueLabel').textContent = '새 평가금';
  document.querySelector('#calcProfitLabel').textContent = '새 수익금';
  document.querySelector('#calcDiffLabel').textContent = '평균단가 변화';
  document.querySelector('#calcDiffRateLabel').textContent = '평균단가 변화율';
  document.querySelector('#calcResultLabel').textContent = '수익금 변동';

  calcModal.hidden = false;
  calcAddPrice.focus();
}

function closeCalcModal() {
  calcModal.hidden = true;
}

calcModalClose.addEventListener('click', closeCalcModal);
calcModal.addEventListener('click', e => {
  if (e.target === calcModal) closeCalcModal();
});

calcAddPrice.addEventListener('input', updateCalcResult);
calcAddQty.addEventListener('input', updateCalcResult);

document.querySelectorAll('.calc-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.calc-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    calcMode = tab.dataset.type;
    const priceLabel = document.querySelector('#calcPriceLabel');
    const qtyLabel = document.querySelector('#calcQtyLabel');
    const costLabel = document.querySelector('#calcCostLabel');
    const avgLabel = document.querySelector('#calcAvgLabel');
    const qtyResultLabel = document.querySelector('#calcQtyResultLabel');
    const totalCostLabel = document.querySelector('#calcTotalCostLabel');
    const valueLabel = document.querySelector('#calcValueLabel');
    const profitLabel = document.querySelector('#calcProfitLabel');
    const diffLabel = document.querySelector('#calcDiffLabel');
    const diffRateLabel = document.querySelector('#calcDiffRateLabel');
    const resultLabel = document.querySelector('#calcResultLabel');

    if (calcMode === 'sell') {
      priceLabel.textContent = '매도가';
      qtyLabel.textContent = '매도량';
      costLabel.textContent = '매도 수익';
      avgLabel.textContent = '평균단가 (변경없음)';
      qtyResultLabel.textContent = '매도 후 수량';
      totalCostLabel.textContent = '매도 후 투자금';
      valueLabel.textContent = '매도 후 평가금';
      profitLabel.textContent = '매도 후 수익금';
      diffLabel.textContent = '평균단가 변화';
      diffRateLabel.textContent = '평균단가 변화율';
      resultLabel.textContent = '매도 수익금';
      calcAddPrice.placeholder = formatMoney(calcState.currentPrice).replace('원', '');
      calcAddQty.placeholder = calcState.quantity + '주';
    } else {
      priceLabel.textContent = '추가 매수가';
      qtyLabel.textContent = '추가 매수량';
      costLabel.textContent = '추가 투자금';
      avgLabel.textContent = '새 평균단가';
      qtyResultLabel.textContent = '새 총 수량';
      totalCostLabel.textContent = '새 총 투자금';
      valueLabel.textContent = '새 평가금';
      profitLabel.textContent = '새 수익금';
      diffLabel.textContent = '평균단가 변화';
      diffRateLabel.textContent = '평균단가 변화율';
      resultLabel.textContent = '수익금 변동';
      calcAddPrice.placeholder = formatMoney(calcState.currentPrice).replace('원', '');
      calcAddQty.placeholder = '';
    }
    updateCalcResult();
  });
});

modalClose.addEventListener('click', closeSignalModal);
signalModal.addEventListener('click', e => {
  if (e.target === signalModal) closeSignalModal();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeChartModal();
    closeCalcModal();
    closeSignalModal();
    toggleChat(false);
  }
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

function attachChartHandlers(container) {
  container.querySelectorAll('.clickable').forEach(el => {
    el.style.cursor = 'pointer';
    el.addEventListener('click', () => {
      const code = el.dataset.code;
      const name = el.dataset.name;
      if (code && name) showChartModal(name, code);
    });
  });
}

function attachCalcHandlers(container) {
  container.querySelectorAll('.avg-price-cell').forEach(el => {
    el.style.cursor = 'pointer';
    el.addEventListener('click', () => {
      const name = el.dataset.name;
      const avgPrice = parseFloat(el.dataset.avg) || 0;
      const quantity = parseInt(el.dataset.qty) || 0;
      const currentPrice = parseFloat(el.dataset.price) || 0;
      showCalcModal(name, avgPrice, quantity, currentPrice);
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
      <td><div class="name-cell"><strong class="clickable" data-code="${row.code}" data-name="${row.name}">${row.name}</strong><small>${row.code}</small></div></td>
      <td>${money.format(row.quantity)}주</td>
      <td class="clickable" data-code="${row.code}" data-name="${row.name}">${formatMoney(row.currentPrice)}</td>
      <td class="${row.change > 0 ? 'up' : row.change < 0 ? 'down' : 'neutral'}">${formatSignedMoney(row.change)} / ${formatPercent(row.changeRate)}</td>
      <td class="avg-price-cell" data-code="${row.code}" data-name="${row.name}" data-avg="${row.avgPrice}" data-qty="${row.quantity}" data-price="${row.currentPrice}">${formatMoney(row.avgPrice)}</td>
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
  attachChartHandlers(holdingsBody);
  attachCalcHandlers(holdingsBody);
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
      <td><div class="name-cell"><strong class="clickable" data-code="${row.code}" data-name="${row.name}">${row.name}</strong><small>${row.code}</small></div></td>
      <td class="clickable" data-code="${row.code}" data-name="${row.name}">${formatMoney(row.currentPrice)}</td>
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
  attachChartHandlers(watchlistBody);
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

/* KOSPI/KOSDAQ Section */
const kospiKosdaqBody = document.querySelector('#kospiKosdaqBody');
const kospiKosdaqDate = document.querySelector('#kospiKosdaqDate');

async function loadKospiKosdaq() {
  try {
    const res = await fetch('/api/kospi-kosdaq', { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderKospiKosdaq(data);
  } catch (e) {
    if (kospiKosdaqBody) kospiKosdaqBody.innerHTML = '<p class="muted">코스피/코스닥 데이터를 불러올 수 없습니다.</p>';
  }
}

function renderKospiKosdaq(data) {
  if (!kospiKosdaqBody || !data) return;

  let html = '';
  if (data.date) {
    if (kospiKosdaqDate) kospiKosdaqDate.textContent = `${data.date} 마감`;
  }

  if (data.indices && data.indices.length) {
    data.indices.forEach(idx => {
      const isUp = idx.rate > 0;
      const isDown = idx.rate < 0;
      const sign = isUp ? '+' : '';
      const cls = isUp ? 'up' : isDown ? 'down' : 'neutral';
      html += `<div class="us-index-row">
        <span class="us-index-name">${idx.name}</span>
        <span>
          <span class="us-index-value">${idx.value.toLocaleString('ko-KR')}</span>
          <span class="us-index-change ${cls}">${sign}${idx.rate.toFixed(2)}%</span>
        </span>
      </div>`;
    });
  }

  kospiKosdaqBody.innerHTML = html;
}

/* US Market Section */
const usMarketBody = document.querySelector('#usMarketBody');
const usMarketDate = document.querySelector('#usMarketDate');

async function loadUSMarket() {
  try {
    const res = await fetch('/api/us-market', { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderUSMarket(data);
  } catch (e) {
    if (usMarketBody) usMarketBody.innerHTML = '<p class="muted">미국증시 데이터를 불러올 수 없습니다.</p>';
  }
}

function renderUSMarket(data) {
  if (!usMarketBody || !data) return;

  let html = '';
  if (data.date) {
    if (usMarketDate) usMarketDate.textContent = `${data.date} 마감`;
  }

  if (data.indices && data.indices.length) {
    data.indices.forEach(idx => {
      const isUp = idx.change > 0;
      const isDown = idx.change < 0;
      const sign = isUp ? '+' : '';
      const cls = isUp ? 'up' : isDown ? 'down' : 'neutral';
      html += `<div class="us-index-row">
        <span class="us-index-name">${idx.name}</span>
        <span>
          <span class="us-index-value">${idx.value.toLocaleString('ko-KR')}</span>
          <span class="us-index-change ${cls}">${sign}${idx.change.toFixed(2)}%</span>
        </span>
      </div>`;
    });
  }

  if (data.summary) {
    html += `<div class="us-summary-box">${data.summary}</div>`;
  }

  if (data.highlights && data.highlights.length) {
    html += '<div class="us-highlights">';
    data.highlights.forEach(h => {
      html += `<div class="us-highlight-item">• ${h}</div>`;
    });
    html += '</div>';
  }

  usMarketBody.innerHTML = html;
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
  if (aiUpdatedAt) aiUpdatedAt.textContent = `AI 분석: ${new Date().toLocaleTimeString('ko-KR')}`;
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
    if (newsUpdatedAt) newsUpdatedAt.textContent = `최종 갱신: ${new Date().toLocaleTimeString('ko-KR')}`;
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
loadKospiKosdaq();
loadUSMarket();
setAutoRefresh(true);
setupNewsRefresh();

/* ──────────────────────────────────────────
   Stock Manager AI Chat
   ────────────────────────────────────────── */
const chatFab = document.querySelector('#chatFab');
const chatPopup = document.querySelector('#chatPopup');
const chatClose = document.querySelector('#chatClose');
const chatMessages = document.querySelector('#chatMessages');
const chatInput = document.querySelector('#chatInput');
const chatSend = document.querySelector('#chatSend');
const chatSessionsBtn = document.querySelector('#chatSessionsBtn');
const chatNewBtn = document.querySelector('#chatNewBtn');
const chatSessionLabel = document.querySelector('#chatSessionLabel');
const CHAT_STORAGE_KEY = 'stock_chat_history';

let chatHistory = [];
let chatSessions = [];
let chatViewingSession = null; // null = current session, id = viewing archived

async function loadChatHistory() {
  try {
    const res = await fetch('/api/chat/history', { cache: 'no-store' });
    if (res.ok) {
      const data = await res.json();
      if (data.history) {
        chatHistory = data.history;
        syncLocalStorage();
        renderChatMessages();
        loadChatSessions();
        restoreChatState();
        return;
      }
    }
  } catch (e) {}
  try {
    const saved = localStorage.getItem(CHAT_STORAGE_KEY);
    if (saved) chatHistory = JSON.parse(saved);
  } catch (e) {
    chatHistory = [];
  }
  renderChatMessages();
  loadChatSessions();
  restoreChatState();
}

async function loadChatSessions() {
  try {
    const res = await fetch('/api/chat/sessions', { cache: 'no-store' });
    if (res.ok) {
      const data = await res.json();
      chatSessions = data.sessions || [];
      updateSessionLabel();
    }
  } catch (e) {}
}

async function loadSessionMessages(sessionId) {
  try {
    const res = await fetch(`/api/chat/session/${sessionId}`, { cache: 'no-store' });
    if (res.ok) {
      const data = await res.json();
      chatHistory = data.history || [];
      chatViewingSession = sessionId;
      renderChatMessages();
    }
  } catch (e) {}
}

function switchToCurrentSession() {
  chatViewingSession = null;
  loadChatHistory();
}

function updateSessionLabel() {
  if (!chatSessionLabel) return;
  if (chatViewingSession) {
    const sess = chatSessions.find(s => s.id === chatViewingSession);
    if (sess) {
      chatSessionLabel.textContent = `${sess.date} ${sess.time}`;
    } else {
      chatSessionLabel.textContent = '과거 대화';
    }
  } else {
    chatSessionLabel.textContent = '오늘';
  }
}

function showChatSessions() {
  const msgs = chatMessages;
  msgs.querySelectorAll('.chat-msg').forEach(el => el.remove());
  msgs.querySelectorAll('.chat-session-list').forEach(el => el.remove());
  const welcome = msgs.querySelector('.chat-welcome');
  if (welcome) welcome.remove();

  const container = document.createElement('div');
  container.className = 'chat-session-list';

  const header = document.createElement('div');
  header.className = 'chat-sessions-header';
  header.innerHTML = '<span>📋 대화 목록</span>';
  const backBtn = document.createElement('button');
  backBtn.className = 'chat-sessions-back';
  backBtn.textContent = '← 현재 대화';
  backBtn.addEventListener('click', switchToCurrentSession);
  header.appendChild(backBtn);
  container.appendChild(header);

  if (!chatSessions.length) {
    const empty = document.createElement('div');
    empty.className = 'chat-sessions-empty';
    empty.textContent = '저장된 대화가 없습니다.';
    container.appendChild(empty);
  } else {
    for (const sess of chatSessions) {
      const item = document.createElement('div');
      item.className = 'chat-session-item';
      if (sess.isCurrent && !chatViewingSession) item.classList.add('active');
      if (chatViewingSession === sess.id) item.classList.add('active');

      const dateRow = document.createElement('div');
      dateRow.style.display = 'flex';
      dateRow.style.alignItems = 'center';
      const dateEl = document.createElement('span');
      dateEl.className = 'sess-date';
      dateEl.textContent = sess.date;
      dateRow.appendChild(dateEl);
      const timeEl = document.createElement('span');
      timeEl.className = 'sess-time';
      timeEl.textContent = ` ${sess.time}`;
      dateRow.appendChild(timeEl);
      if (sess.isCurrent) {
        const badge = document.createElement('span');
        badge.className = 'sess-badge';
        badge.textContent = '현재';
        dateRow.appendChild(badge);
      }
      if (!sess.isCurrent) {
        const delBtn = document.createElement('button');
        delBtn.className = 'sess-delete-btn';
        delBtn.textContent = '✕';
        delBtn.title = '삭제';
        delBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!confirm('이 대화를 삭제하시겠습니까?')) return;
          try {
            const res = await fetch(`/api/chat/session/${sess.id}`, { method: 'DELETE' });
            if (!res.ok) {
              const err = await res.json().catch(() => ({}));
              console.error('세션 삭제 실패:', res.status, err);
              alert('삭제에 실패했습니다.');
              return;
            }
            chatSessions = chatSessions.filter(s => s.id !== sess.id);
            if (chatViewingSession === sess.id) {
              chatViewingSession = null;
              switchToCurrentSession();
            }
            showChatSessions();
          } catch (err) {
            console.error('세션 삭제 실패:', err);
            alert('삭제 중 오류가 발생했습니다.');
          }
        });
        dateRow.appendChild(delBtn);
      }
      item.appendChild(dateRow);

      const preview = document.createElement('div');
      preview.className = 'sess-preview';
      preview.textContent = sess.preview || '(메시지 없음)';
      item.appendChild(preview);

      const meta = document.createElement('div');
      meta.className = 'sess-meta';
      meta.textContent = `메시지 ${sess.messageCount}개`;
      item.appendChild(meta);

      item.addEventListener('click', () => {
        if (sess.isCurrent) {
          switchToCurrentSession();
        } else {
          loadSessionMessages(sess.id);
        }
      });
      container.appendChild(item);
    }
  }

  msgs.appendChild(container);
  msgs.scrollTop = 0;
}

function syncLocalStorage() {
  try {
    localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(chatHistory.slice(-50)));
  } catch (e) {}
}

function addChatMessage(role, content) {
  chatHistory.push({ role, content, timestamp: Date.now() });
  syncLocalStorage();
  renderChatMessages();
}

function renderMessageContent(text) {
  const escaped = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  // Extract markdown tables first and replace with placeholders so that
  // URL/citation/bold/newline conversions do not corrupt table HTML.
  const tables = [];
  let working = escaped.replace(
    /(?:^|\n)((?:\|[^\n]*\|[ \t]*\n)+\|[\s:|-]+\|[^\n]*)/g,
    (full, tableBlock) => {
      const html = markdownTableToHtml(tableBlock);
      const placeholder = `__CHAT_TABLE_${tables.length}__`;
      tables.push(html);
      return `\n${placeholder}\n`;
    }
  );

  // Build citation map: [N] → URL from 출처 section
  const citationMap = {};
  const citeRe = /\[(\d+)\][\s\S]*?(https?:\/\/[^\s<>)"']+)/g;
  let m;
  while ((m = citeRe.exec(working)) !== null) {
    citationMap[m[1]] = m[2];
  }

  const LINK_STYLE = 'color:#58a6ff;text-decoration:underline;font-weight:700';

  // Step 1: raw URLs → <a> (FIRST, prevents href="" double-wrapping)
  let result = working.replace(
    /(https?:\/\/[^\s<>)"']+)/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer" style="color:#58a6ff;text-decoration:underline">$1</a>'
  );

  // Step 2: [N] markers → citation links (AFTER URLs are already linked)
  result = result.replace(
    /\[(\d+)\]/g,
    (_, num) => {
      const url = citationMap[num];
      return url
        ? `<a href="${url}" target="_blank" rel="noopener noreferrer" style="${LINK_STYLE}" class="chat-citation-link">[${num}]</a>`
        : `[${num}]`;
    }
  );

  // Step 3: [Hn] → scroll-to-history link (1-based → 0-based)
  const H_STYLE = 'color:#ffa858;text-decoration:underline;font-weight:700;cursor:pointer';
  result = result.replace(
    /\[H(\d+)\]/g,
    (_, num) =>
      `<a href="#" onclick="event.preventDefault();scrollToHistoryMsg(${parseInt(num) - 1});return false;" style="${H_STYLE}">[H${num}]</a>`
  );

  // Step 4: **bold** → <strong>
  result = result.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

  // Step 5: newlines → <br>
  result = result.replace(/\n/g, '<br>');

  // Step 6: restore table HTML (after all other conversions)
  tables.forEach((html, i) => {
    result = result.replace(`__CHAT_TABLE_${i}__`, html);
  });
  return result;
}

function markdownTableToHtml(tableText) {
  const rawLines = tableText.trim().split('\n').map(l => l.trim()).filter(l => l.startsWith('|'));
  if (rawLines.length < 2) return tableText;

  const splitRow = (line) =>
    line.replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());

  // Second line must be a separator like |---|:---:|---:|
  const sep = rawLines[1];
  if (!/^\|?[\s:|-]+\|?$/.test(sep) || !sep.includes('-')) return tableText;

  const headers = splitRow(rawLines[0]);
  const rows = rawLines.slice(2).map(splitRow);

  const LINK_STYLE = 'color:#58a6ff;text-decoration:underline';
  const numClass = (cell) => {
    const m = cell.match(/([+\-])?\s*[\d.,]+\s*%/);
    if (!m) return '';
    return m[1] === '-' ? 'num-down' : m[1] === '+' ? 'num-up' : 'num-flat';
  };
  const renderCell = (cell) => {
    let s = cell;
    s = s.replace(/(https?:\/\/[^\s<>)"']+)/g, `<a href="$1" target="_blank" rel="noopener noreferrer" style="${LINK_STYLE}">$1</a>`);
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    return s;
  };

  // Transpose when table has many entity rows and few attribute columns:
  // e.g. 5 stocks (rows) x 4 attributes (cols) -> 4 rows x 5 cols.
  // Vertical scroll becomes short; horizontal scroll for many entities.
  let renderHeaders = headers;
  let renderRows = rows;
  const shouldTranspose = rows.length >= 3
    && headers.length >= 2
    && rows.length > headers.length;
  if (shouldTranspose) {
    renderHeaders = [headers[0] || '항목', ...rows.map(r => r[0] || '')];
    renderRows = headers.slice(1).map((attrName, i) => {
      const values = rows.map(r => r[i + 1] || '');
      return [attrName, ...values];
    });
  }

  const ncols = renderHeaders.length;
  const origNcols = headers.length;
  const wrapClasses = ['chat-table-wrap'];
  if (shouldTranspose) wrapClasses.push('chat-table-transposed');
  if (origNcols < 5) wrapClasses.push('chat-table-compact');
  let html = '<div class="' + wrapClasses.join(' ') + '"><table class="chat-table">';
  html += '<thead><tr>' + renderHeaders.map(h => `<th>${renderCell(h)}</th>`).join('') + '</tr></thead>';
  html += '<tbody>';
  renderRows.forEach(row => {
    const maxLen = Math.max(ncols, row.length);
    const cells = Array.from({length: maxLen}, (_, i) => {
      const raw = row[i] || '';
      const cls = numClass(raw);
      return `<td${cls ? ` class="${cls}"` : ''}>${renderCell(raw)}</td>`;
    }).join('');
    html += `<tr>${cells}</tr>`;
  });
  html += '</tbody></table></div>';
  return html;
}

function scrollToHistoryMsg(idx) {
  const el = document.getElementById(`chat-msg-${idx}`);
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.classList.add('chat-highlight');
    setTimeout(() => el.classList.remove('chat-highlight'), 2000);
  }
}

function replaceHistory(history) {
  chatHistory = history;
  chatViewingSession = null;
  syncLocalStorage();
  renderChatMessages();
}

function renderChatMessages() {
  const msgs = chatMessages;

  // Remove session list if visible
  msgs.querySelectorAll('.chat-session-list').forEach(el => el.remove());

  const welcome = msgs.querySelector('.chat-welcome');
  if (welcome) welcome.remove();

  msgs.querySelectorAll('.chat-msg').forEach(el => el.remove());

  updateSessionLabel();

  for (let i = 0; i < chatHistory.length; i++) {
    const msg = chatHistory[i];
    const div = document.createElement('div');
    div.className = `chat-msg ${msg.role}`;
    div.id = `chat-msg-${i}`;
    div.innerHTML = renderMessageContent(msg.content);
    msgs.appendChild(div);
  }

  if (!chatHistory.length) {
    const w = document.createElement('div');
    w.className = 'chat-welcome';
    w.innerHTML = '<p>💡 궁금한 점을 물어보세요!<br>예: "지금 포트폴리오 어떤가요?", "SK하이닉스 지금 사도 될까요?"</p>';
    msgs.appendChild(w);
  }

  msgs.scrollTop = msgs.scrollHeight;
}

function showTypingIndicator() {
  const div = document.createElement('div');
  div.className = 'chat-msg assistant typing';
  div.id = 'chatTyping';
  div.textContent = '준비중...';
  chatMessages.appendChild(div);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

function updateTypingPhase(phase) {
  const labels = {
    loading: '포트폴리오 데이터 로딩중...',
    searching: '웹 검색중...',
    analyzing: 'AI 분석중...',
  };
  const el = document.querySelector('#chatTyping');
  if (el) {
    el.textContent = labels[phase] || '분석중...';
  }
}

function hideTypingIndicator() {
  const el = document.querySelector('#chatTyping');
  if (el) el.remove();
}

const CHAT_OPEN_KEY = 'stock_chat_open';

function toggleChat(open) {
  chatPopup.classList.toggle('open', open);
  try { localStorage.setItem(CHAT_OPEN_KEY, open ? '1' : '0'); } catch (e) {}
  if (open) {
    chatInput.focus();
    if (!chatViewingSession) renderChatMessages();
  }
}

function restoreChatState() {
  try {
    const saved = localStorage.getItem(CHAT_OPEN_KEY);
    if (saved === '1') toggleChat(true);
  } catch (e) {}
}

let chatSending = false;
async function sendChatMessage(text) {
  if (!text.trim()) return;
  if (chatSend.disabled || chatSending) return;
  chatSending = true;

  // If viewing archived session, switch to current first
  if (chatViewingSession) {
    chatViewingSession = null;
    chatHistory = [];
  }

  chatSend.disabled = true;
  chatInput.value = '';

  // Add user message locally
  chatHistory.push({ role: 'user', content: text.trim(), timestamp: Date.now() });
  syncLocalStorage();
  renderChatMessages();
  showTypingIndicator();

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text.trim() }),
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let currentEvent = '';
    let done = false;

    while (!done) {
      const { done: streamDone, value } = await reader.read();
      done = streamDone;
      if (value) {
        buffer += decoder.decode(value, { stream: !done });
        const parts = buffer.split('\n');
        buffer = parts.pop() || '';
        for (const line of parts) {
          const trimmed = line.trim();
          if (trimmed.startsWith('event: ')) {
            currentEvent = trimmed.slice(7);
          } else if (trimmed.startsWith('data: ')) {
            try {
              const data = JSON.parse(trimmed.slice(6));
              if (currentEvent === 'status') {
                updateTypingPhase(data.phase);
              } else if (currentEvent === 'result') {
                hideTypingIndicator();
                if (data.history && data.history.length > 0) {
                  // Use server history as source of truth (no local add needed)
                  chatHistory = data.history;
                  syncLocalStorage();
                  renderChatMessages();
                  loadChatSessions();
                } else if (data.reply) {
                  // Fallback: add reply only if not already in history
                  const lastMsg = chatHistory[chatHistory.length - 1];
                  if (!lastMsg || lastMsg.role !== 'assistant' || lastMsg.content !== data.reply) {
                    chatHistory.push({ role: 'assistant', content: data.reply, timestamp: Date.now() });
                    syncLocalStorage();
                    renderChatMessages();
                  }
                }
              }
            } catch (e) {}
          }
        }
      }
    }
  } catch (err) {
    hideTypingIndicator();
    addChatMessage('assistant', `죄송합니다. AI 서비스와 연결할 수 없습니다. (${err.message})`);
  } finally {
    chatSending = false;
    chatSend.disabled = false;
    chatInput.focus();
  }
}

chatFab.addEventListener('click', () => toggleChat(true));

chatClose.addEventListener('click', () => toggleChat(false));

chatSessionsBtn.addEventListener('click', () => {
  loadChatSessions().then(() => showChatSessions());
});

chatNewBtn.addEventListener('click', async () => {
  try {
    const res = await fetch('/api/chat/new-session', { method: 'POST', cache: 'no-store' });
    if (res.ok) {
      chatHistory = [];
      chatViewingSession = null;
      syncLocalStorage();
      renderChatMessages();
      loadChatSessions();
    }
  } catch (e) {}
});

chatSend.addEventListener('click', () => {
  const text = chatInput.value;
  if (text.trim()) sendChatMessage(text);
});

chatInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    if (e.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    const text = chatInput.value;
    if (text.trim()) sendChatMessage(text);
  }
});

loadChatHistory();
