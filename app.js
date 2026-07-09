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

// 기술적 분석 상세 모달
const techDetailModal = document.querySelector('#techDetailModal');
const techDetailTitle = document.querySelector('#techDetailTitle');
const techDetailClose = document.querySelector('#techDetailClose');
const techDetailBody = document.querySelector('#techDetailBody');

let autoTimer = null;
let autoEnabled = true;
let newsTimer = null;
let lastSidebarRefresh = 0;
const SIDEBAR_REFRESH_INTERVAL = 300000; // 5 minutes

let watchlistExpanded = false;
let watchlistLoaded = false;

const aiResults = new Map();
let profitHistory = [];

let reorderLockUntil = 0;

let holdingsSort = { key: null, dir: 'asc' };
let holdingsData = [];
let lastHoldingsRows = null;

const HOLDINGS_ORDER_KEY = 'stock_holdings_order_v2';
const WATCHLIST_ORDER_KEY = 'stock_watchlist_order_v2';

function sortBySavedOrder(items, storageKey) {
  const saved = localStorage.getItem(storageKey);
  if (!saved) return items;
  try {
    const order = JSON.parse(saved);
    if (!Array.isArray(order) || order.length === 0) return items;
    const map = new Map(items.map(i => [i.code, i]));
    const result = [];
    for (const code of order) {
      if (map.has(code)) {
        result.push(map.get(code));
        map.delete(code);
      }
    }
    map.forEach(v => result.push(v));
    return result;
  } catch { return items; }
}

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

function showChartModal(name, code, avgPrice) {
  chartModalTitle.textContent = `${name} 주가 차트`;
  if (lwChart) { try { lwChart.remove(); } catch(e){} lwChart = null; }
  tvChartContainer.innerHTML = '<p style="text-align:center;padding:40px;color:var(--muted)">차트 로딩 중...</p>';
  document.querySelector('#techIndicators').innerHTML = '<p class="muted">기술적 지표 로딩 중...</p>';
  document.querySelector('#techSignals').innerHTML = '';
  document.querySelector('#techSignalBadge').textContent = '-';
  document.querySelector('#techSignalBadge').className = 'tech-signal-badge';
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
      lastValueVisible: false,
      priceLineVisible: false,
    });
    lwChart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.85, bottom: 0 },
      borderVisible: false,
      ticksVisible: false,
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

    // 이동평균선 추가
    if (data.maArrays) {
      const maArrays = data.maArrays;
      const addMALine = (values, color, title) => {
        const lineData = candles.map((c, i) => ({
          time: c.time,
          value: values[i],
        })).filter(d => d.value !== null && d.value !== undefined);
        if (lineData.length > 0) {
          const series = lwChart.addLineSeries({
            color: color,
            lineWidth: 1,
            lineStyle: 2,
            priceLineVisible: false,
            lastValueVisible: false,
          });
          series.setData(lineData);
        }
      };
      
      if (maArrays.ma5) addMALine(maArrays.ma5, '#f59e0b', 'MA5');
      if (maArrays.ma20) addMALine(maArrays.ma20, '#60a5fa', 'MA20');
      if (maArrays.ma60) addMALine(maArrays.ma60, '#a78bfa', 'MA60');
      if (maArrays.ma120) addMALine(maArrays.ma120, '#f472b6', 'MA120');
    }

    if (avgPrice > 0) {
      candleSeries.createPriceLine({
        price: avgPrice,
        color: '#f59e0b',
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: '평균단가',
      });
    }

    lwChart.timeScale().fitContent();

    const resizeObserver = new ResizeObserver(entries => {
      if (entries.length > 0 && lwChart) {
        const { width, height } = entries[0].contentRect;
        lwChart.applyOptions({ width: Math.floor(width), height: Math.floor(height) });
      }
    });
    resizeObserver.observe(tvChartContainer);
    
    // 기술적 지표 표시
    renderTechPanel(data);
  }).catch(err => {
    tvChartContainer.innerHTML = '<p style="text-align:center;padding:40px;color:var(--muted)">차트 로드 실패: ' + err.message + '</p>';
  });
}

function closeChartModal() {
  chartModal.hidden = true;
  if (lwChart) { try { lwChart.remove(); } catch(e){} lwChart = null; }
  tvChartContainer.innerHTML = '';
}

function renderTechPanel(data) {
  const techIndicators = document.querySelector('#techIndicators');
  const techSignals = document.querySelector('#techSignals');
  const techSignalBadge = document.querySelector('#techSignalBadge');
  
  if (!data.techIndicators) {
    techIndicators.innerHTML = '<p class="muted">기술적 지표를 사용할 수 없습니다.</p>';
    return;
  }
  
  const indicators = data.techIndicators;
  const currentPrice = (data.candles && data.candles.length) ? data.candles[data.candles.length - 1].close : 0;
  
  // 시그널 배지
  const techSignal = data.techSignal || 'hold';
  const signalLabels = {
    'strong_buy': '강력매수',
    'buy': '매수',
    'hold': '관망',
    'sell': '매도',
    'strong_sell': '강력매도'
  };
  techSignalBadge.textContent = signalLabels[techSignal] || '관망';
  techSignalBadge.className = `tech-signal-badge ${techSignal}`;
  
  // 지표 표시
  let html = '';
  
  // 이동평균선 섹션
  html += '<div class="tech-indicator-section">';
  html += '<div class="tech-indicator-section-title">이동평균선</div>';
  
  const maItems = [
    { label: 'MA5', value: indicators.ma5 },
    { label: 'MA20', value: indicators.ma20 },
    { label: 'MA60', value: indicators.ma60 },
    { label: 'MA120', value: indicators.ma120 },
  ];
  
  maItems.forEach(item => {
    if (item.value !== null && item.value !== undefined) {
      const diff = currentPrice ? ((currentPrice - item.value) / item.value * 100) : 0;
      const cls = diff > 0 ? 'up' : diff < 0 ? 'down' : 'neutral';
      html += `<div class="tech-indicator-row">
        <span class="tech-indicator-label">${item.label}</span>
        <span class="tech-indicator-value ${cls}">${money.format(Math.round(item.value))}원 (${diff > 0 ? '+' : ''}${diff.toFixed(1)}%)</span>
      </div>`;
    }
  });
  html += '</div>';
  
  // RSI
  if (indicators.rsi14 !== null && indicators.rsi14 !== undefined) {
    const rsiCls = indicators.rsi14 > 70 ? 'up' : indicators.rsi14 < 30 ? 'down' : 'neutral';
    const rsiLabel = indicators.rsi14 > 70 ? '과매수' : indicators.rsi14 < 30 ? '과매도' : '중립';
    html += '<div class="tech-indicator-section">';
    html += '<div class="tech-indicator-section-title">모멘텀</div>';
    html += `<div class="tech-indicator-row">
      <span class="tech-indicator-label">RSI(14)</span>
      <span class="tech-indicator-value ${rsiCls}">${indicators.rsi14.toFixed(1)} (${rsiLabel})</span>
    </div>`;
    html += '</div>';
  }
  
  // MACD
  if (indicators.macd) {
    const macd = indicators.macd;
    html += '<div class="tech-indicator-section">';
    html += '<div class="tech-indicator-section-title">MACD</div>';
    if (macd.macd !== null && macd.macd !== undefined) {
      const macdCls = macd.histogram > 0 ? 'up' : macd.histogram < 0 ? 'down' : 'neutral';
      html += `<div class="tech-indicator-row">
        <span class="tech-indicator-label">MACD</span>
        <span class="tech-indicator-value ${macdCls}">${macd.macd.toFixed(2)}</span>
      </div>`;
    }
    if (macd.signal !== null && macd.signal !== undefined) {
      html += `<div class="tech-indicator-row">
        <span class="tech-indicator-label">시그널</span>
        <span class="tech-indicator-value">${macd.signal.toFixed(2)}</span>
      </div>`;
    }
    if (macd.histogram !== null && macd.histogram !== undefined) {
      const histCls = macd.histogram > 0 ? 'up' : 'down';
      html += `<div class="tech-indicator-row">
        <span class="tech-indicator-label">히스토그램</span>
        <span class="tech-indicator-value ${histCls}">${macd.histogram.toFixed(2)}</span>
      </div>`;
    }
    html += '</div>';
  }
  
  // 볼린저 밴드
  if (indicators.bollinger) {
    const bb = indicators.bollinger;
    html += '<div class="tech-indicator-section">';
    html += '<div class="tech-indicator-section-title">볼린저 밴드</div>';
    if (bb.upper) {
      html += `<div class="tech-indicator-row">
        <span class="tech-indicator-label">상단</span>
        <span class="tech-indicator-value up">${money.format(Math.round(bb.upper))}원</span>
      </div>`;
    }
    if (bb.middle) {
      html += `<div class="tech-indicator-row">
        <span class="tech-indicator-label">중간</span>
        <span class="tech-indicator-value neutral">${money.format(Math.round(bb.middle))}원</span>
      </div>`;
    }
    if (bb.lower) {
      html += `<div class="tech-indicator-row">
        <span class="tech-indicator-label">하단</span>
        <span class="tech-indicator-value down">${money.format(Math.round(bb.lower))}원</span>
      </div>`;
    }
    html += '</div>';
  }
  
  // 스토캐스틱
  if (indicators.stochastic) {
    const stoch = indicators.stochastic;
    html += '<div class="tech-indicator-section">';
    html += '<div class="tech-indicator-section-title">스토캐스틱</div>';
    if (stoch.k !== null && stoch.k !== undefined) {
      const stochCls = stoch.k > 80 ? 'up' : stoch.k < 20 ? 'down' : 'neutral';
      html += `<div class="tech-indicator-row">
        <span class="tech-indicator-label">%K</span>
        <span class="tech-indicator-value ${stochCls}">${stoch.k.toFixed(1)}</span>
      </div>`;
    }
    if (stoch.d !== null && stoch.d !== undefined) {
      html += `<div class="tech-indicator-row">
        <span class="tech-indicator-label">%D</span>
        <span class="tech-indicator-value">${stoch.d.toFixed(1)}</span>
      </div>`;
    }
    html += '</div>';
  }
  
  // 거래량 요약
  const candles = data.candles || [];
  if (candles.length >= 10) {
    const currentVol = candles[candles.length - 1].volume || 0;
    const recent20 = candles.slice(-20);
    const avgVol20 = recent20.reduce((s, c) => s + (c.volume || 0), 0) / recent20.length;
    const volRatio = avgVol20 > 0 ? currentVol / avgVol20 : 1;
    const volCls = volRatio >= 2.0 ? 'up' : volRatio <= 0.5 ? 'down' : 'neutral';
    
    html += '<div class="tech-indicator-section">';
    html += '<div class="tech-indicator-section-title">거래량</div>';
    html += `<div class="tech-indicator-row">
      <span class="tech-indicator-label">현재/평균</span>
      <span class="tech-indicator-value ${volCls}">${volRatio.toFixed(1)}배</span>
    </div>`;
    html += '</div>';
  }
  
  techIndicators.innerHTML = html;
  
  // 기술적 시그널 표시
  const signals = data.techSignals || [];
  if (signals.length > 0) {
    let signalHtml = '<div class="tech-indicator-section">';
    signalHtml += '<div class="tech-indicator-section-title">매매 시그널</div>';
    signals.forEach(signal => {
      const cls = signal.includes('매수') || signal.includes('상승') || signal.includes('골든') || signal.includes('과매도') ? 'positive' : 
                  signal.includes('매도') || signal.includes('하락') || signal.includes('데드') || signal.includes('과매수') ? 'negative' : '';
      signalHtml += `<div class="tech-signal-item ${cls}">• ${signal}</div>`;
    });
    signalHtml += '</div>';
    techSignals.innerHTML = signalHtml;
  } else {
    techSignals.innerHTML = '<div class="tech-indicator-section"><div class="tech-indicator-section-title">매매 시그널</div><div class="tech-signal-item">특이사항 없음</div></div>';
  }
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

// 기술적 분석 상세 모달 이벤트
techDetailClose.addEventListener('click', closeTechDetailModal);
techDetailModal.addEventListener('click', e => {
  if (e.target === techDetailModal) closeTechDetailModal();
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeChartModal();
    closeCalcModal();
    closeSignalModal();
    closeTechDetailModal();
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

/* 기술적 분석 상세 모달 */
function showTechDetailModal(name, code, trendData) {
  techDetailTitle.textContent = `${name} 기술적 분석`;
  techDetailBody.innerHTML = '<p class="muted">기술적 지표를 불러오는 중...</p>';
  techDetailModal.hidden = false;
  
  // 차트 API에서 기술적 지표 가져오기
  fetch(`/api/chart?code=${code}`)
    .then(r => r.json())
    .then(data => {
      renderTechDetailContent(name, code, trendData, data);
    })
    .catch(err => {
      techDetailBody.innerHTML = `<p class="muted">기술적 지표를 불러올 수 없습니다: ${err.message}</p>`;
    });
}

function closeTechDetailModal() {
  techDetailModal.hidden = true;
}

function renderTechDetailContent(name, code, trendData, chartData) {
  const indicators = chartData.techIndicators || {};
  const signals = chartData.techSignals || [];
  const signalScore = chartData.techSignalScore || 0;
  const techSignal = chartData.techSignal || 'hold';
  const realtimeSignals = trendData.realtimeSignals || [];
  
  let html = '';
  
  // 종합 시그널 섹션
  html += '<div class="tech-detail-section">';
  html += '<div class="tech-detail-section-title">📊 종합 시그널</div>';
  
  const signalLabels = {
    'strong_buy': { text: '🔴 강력매수', cls: 'positive' },
    'buy': { text: '🟠 매수', cls: 'positive' },
    'hold': { text: '⚪ 관망', cls: 'neutral' },
    'sell': { text: '🟡 매도', cls: 'negative' },
    'strong_sell': { text: '🟢 강력매도', cls: 'negative' }
  };
  const signalInfo = signalLabels[techSignal] || signalLabels.hold;
  
  html += `<div class="tech-detail-signal">`;
  html += `<div class="tech-detail-signal-title ${signalInfo.cls}">${signalInfo.text}</div>`;
  html += `<div class="tech-detail-signal-desc">기술적 지표 종합 판단 결과</div>`;
  html += `</div>`;
  
  // 시그널 점수 바
  html += '<div class="tech-detail-signal-score">';
  html += '<span style="font-size:12px;color:var(--muted)">매도</span>';
  html += '<div class="tech-detail-signal-bar">';
  const fillWidth = Math.min(100, Math.max(0, (signalScore + 100) / 2));
  const fillCls = signalScore > 0 ? 'positive' : 'negative';
  html += `<div class="tech-detail-signal-fill ${fillCls}" style="width:${fillWidth}%"></div>`;
  html += '</div>';
  html += `<span class="tech-detail-signal-value ${signalScore > 0 ? 'up' : signalScore < 0 ? 'down' : 'neutral'}">${signalScore > 0 ? '+' : ''}${signalScore}</span>`;
  html += '<span style="font-size:12px;color:var(--muted)">매수</span>';
  html += '</div>';
  html += '</div>';
  
  // 기술적 지표 섹션
  html += '<div class="tech-detail-section">';
  
  // 기술적 지표 요약 생성
  const currentPrice = chartData.candles?.length ? chartData.candles[chartData.candles.length - 1].close : null;
  let techSummaryParts = [];
  
  // 단기 추세 (MA5, RSI, 스토캐스틱)
  const shortBullish = [];
  const shortBearish = [];
  if (indicators.ma5 && currentPrice) {
    if (currentPrice > indicators.ma5) shortBullish.push('MA5 위');
    else shortBearish.push('MA5 아래');
  }
  if (indicators.rsi14 !== null && indicators.rsi14 !== undefined) {
    if (indicators.rsi14 > 70) shortBearish.push('RSI 과매수');
    else if (indicators.rsi14 > 60) shortBullish.push('RSI 강세');
    else if (indicators.rsi14 < 30) shortBullish.push('RSI 과매도(반등)');
    else if (indicators.rsi14 < 40) shortBearish.push('RSI 약세');
  }
  if (indicators.stochastic) {
    if (indicators.stochastic.k > 80) shortBearish.push('스토캐스틱 과매수');
    else if (indicators.stochastic.k < 20) shortBullish.push('스토캐스틱 과매도(반등)');
    else if (indicators.stochastic.k > indicators.stochastic.d) shortBullish.push('스토캐스틱 상승모멘텀');
    else shortBearish.push('스토캐스틱 하락모멘텀');
  }
  
  // 중기 추세 (MA20, MACD, 볼린저)
  const midBullish = [];
  const midBearish = [];
  if (indicators.ma20 && currentPrice) {
    if (currentPrice > indicators.ma20) midBullish.push('MA20 위');
    else midBearish.push('MA20 아래');
  }
  if (indicators.macd) {
    if (indicators.macd.macd > indicators.macd.signal) midBullish.push('MACD 상승');
    else midBearish.push('MACD 하락');
  }
  if (indicators.bollinger && currentPrice && indicators.bollinger.upper && indicators.bollinger.lower) {
    const bbPos = ((currentPrice - indicators.bollinger.lower) / (indicators.bollinger.upper - indicators.bollinger.lower) * 100);
    if (bbPos > 80) midBearish.push('볼린저 상단접근');
    else if (bbPos < 20) midBullish.push('볼린저 하단접근');
  }
  
  // 장기 추세 (MA60)
  if (indicators.ma60 && currentPrice) {
    if (currentPrice > indicators.ma60) midBullish.push('MA60 위');
    else midBearish.push('MA60 아래');
  }
  
  // 요약 문장 조합
  const shortScore = shortBullish.length - shortBearish.length;
  const midScore = midBullish.length - midBearish.length;
  const longTrend = indicators.ma60 && currentPrice ? (currentPrice > indicators.ma60 ? '상승' : '하락') : '불명';
  
  let techSummary;
  if (shortScore > 0 && midScore > 0 && longTrend === '상승') techSummary = '단기·중기·장기 모두 상승 추세';
  else if (shortScore > 0 && midScore > 0 && longTrend === '하락') techSummary = '단기·중기 반등이나 장기 하락 추세 지속 주의';
  else if (shortScore > 0 && midScore <= 0 && longTrend === '상승') techSummary = '단기 반등이나 중기 조정, 장기 상승 추세 유지';
  else if (shortScore > 0 && midScore <= 0 && longTrend === '하락') techSummary = '단기 반등신호 있으나 중장기 하락 추세 지속 주의';
  else if (shortScore <= 0 && midScore > 0 && longTrend === '상승') techSummary = '단기 조정이나 중장기 상승 추세 유지';
  else if (shortScore <= 0 && midScore > 0 && longTrend === '하락') techSummary = '단기 약세이나 중기 반등, 장기 하락 전환 주의';
  else if (shortScore <= 0 && midScore <= 0 && longTrend === '상승') techSummary = '단기·중기 조정이나 장기 상승 추세 유지';
  else if (shortScore <= 0 && midScore <= 0 && longTrend === '하락') techSummary = '단기·중기·장기 모두 하락 추세';
  else techSummary = '뚜렷한 추세 방향 없음';
  
  html += `<div class="tech-detail-section-title">📈 기술적 지표 <span style="font-size:12px;font-weight:500;color:var(--muted);margin-left:8px">${techSummary}</span></div>`;
  html += '<div class="tech-detail-grid">';
  if (indicators.ma5) {
    const diff5 = currentPrice ? ((currentPrice - indicators.ma5) / indicators.ma5 * 100) : 0;
    const ma5Meaning = diff5 > 0 ? '주가가 MA5 위 (단기 강세)' : '주가가 MA5 아래 (단기 약세)';
    html += `<div class="tech-detail-item">
      <div class="tech-detail-item-label">MA5 (5일선)</div>
      <div class="tech-detail-item-value ${diff5 > 0 ? 'up' : 'down'}">${money.format(Math.round(indicators.ma5))}원</div>
      <div class="tech-detail-item-sub">${diff5 > 0 ? '+' : ''}${diff5.toFixed(1)}% — ${ma5Meaning}</div>
    </div>`;
  }
  if (indicators.ma20) {
    const diff20 = currentPrice ? ((currentPrice - indicators.ma20) / indicators.ma20 * 100) : 0;
    const ma20Meaning = diff20 > 5 ? '과열권 — 조정 가능' : diff20 < -5 ? '과침권 — 반등 가능' : diff20 > 0 ? '주가가 MA20 위 (중기 강세)' : '주가가 MA20 아래 (중기 약세)';
    html += `<div class="tech-detail-item">
      <div class="tech-detail-item-label">MA20 (20일선)</div>
      <div class="tech-detail-item-value ${diff20 > 0 ? 'up' : 'down'}">${money.format(Math.round(indicators.ma20))}원</div>
      <div class="tech-detail-item-sub">${diff20 > 0 ? '+' : ''}${diff20.toFixed(1)}% — ${ma20Meaning}</div>
    </div>`;
  }
  if (indicators.ma60) {
    const diff60 = currentPrice ? ((currentPrice - indicators.ma60) / indicators.ma60 * 100) : 0;
    const ma60Meaning = diff60 > 10 ? '장기 상승 추세 강화' : diff60 < -10 ? '장기 하락 추세 심화' : diff60 > 0 ? '주가가 MA60 위 (장기 강세)' : '주가가 MA60 아래 (장기 약세)';
    html += `<div class="tech-detail-item">
      <div class="tech-detail-item-label">MA60 (60일선)</div>
      <div class="tech-detail-item-value ${diff60 > 0 ? 'up' : 'down'}">${money.format(Math.round(indicators.ma60))}원</div>
      <div class="tech-detail-item-sub">${diff60 > 0 ? '+' : ''}${diff60.toFixed(1)}% — ${ma60Meaning}</div>
    </div>`;
  }
  
  // RSI
  if (indicators.rsi14 !== null && indicators.rsi14 !== undefined) {
    const rsiCls = indicators.rsi14 > 70 ? 'up' : indicators.rsi14 < 30 ? 'down' : 'neutral';
    let rsiLabel, rsiMeaning;
    if (indicators.rsi14 > 70) {
      rsiLabel = '과매수';
      rsiMeaning = '상승 과잉 — 하락 전환 가능';
    } else if (indicators.rsi14 > 60) {
      rsiLabel = '강세';
      rsiMeaning = '상승 모멘텀 유지 중';
    } else if (indicators.rsi14 < 30) {
      rsiLabel = '과매도';
      rsiMeaning = '하락 과잉 — 반등 기대';
    } else if (indicators.rsi14 < 40) {
      rsiLabel = '약세';
      rsiMeaning = '하락 모멘텀 지속 중';
    } else {
      rsiLabel = '중립';
      rsiMeaning = '뚜렷한 방향 없음';
    }
    html += `<div class="tech-detail-item">
      <div class="tech-detail-item-label">RSI (14일)</div>
      <div class="tech-detail-item-value ${rsiCls}">${indicators.rsi14.toFixed(1)}</div>
      <div class="tech-detail-item-sub">${rsiLabel} — ${rsiMeaning}</div>
    </div>`;
  }
  
  // MACD
  if (indicators.macd) {
    const macd = indicators.macd;
    if (macd.macd !== null && macd.macd !== undefined) {
      const macdCls = macd.histogram > 0 ? 'up' : 'down';
      let macdMeaning;
      if (macd.macd > macd.signal) {
        macdMeaning = 'MACD > 시그널 — 상승 모멘텀';
      } else {
        macdMeaning = 'MACD < 시그널 — 하락 모멘텀';
      }
      html += `<div class="tech-detail-item">
        <div class="tech-detail-item-label">MACD</div>
        <div class="tech-detail-item-value ${macdCls}">${macd.macd.toFixed(0)}</div>
        <div class="tech-detail-item-sub">시그널: ${macd.signal?.toFixed(0) || '-'} — ${macdMeaning}</div>
      </div>`;
    }
  }
  
  // 볼린저 밴드
  if (indicators.bollinger) {
    const bb = indicators.bollinger;
    let bbMeaning = '';
    if (currentPrice && bb.upper && bb.lower) {
      const bbRange = bb.upper - bb.lower;
      const bbPos = ((currentPrice - bb.lower) / bbRange * 100).toFixed(0);
      if (currentPrice > bb.upper) {
        bbMeaning = '상단 돌파 — 과열/과매수';
      } else if (currentPrice < bb.lower) {
        bbMeaning = '하단 이탈 — 과매도/반등 기대';
      } else if (bbPos > 80) {
        bbMeaning = `상단 접근 (${bbPos}%) — 매도 압력`;
      } else if (bbPos < 20) {
        bbMeaning = `하단 접근 (${bbPos}%) — 매수 기회`;
      } else {
        bbMeaning = `밴드 내 위치 (${bbPos}%)`;
      }
    }
    html += `<div class="tech-detail-item">
      <div class="tech-detail-item-label">볼린저 밴드</div>
      <div class="tech-detail-item-value neutral">중간: ${bb.middle ? money.format(Math.round(bb.middle)) : '-'}</div>
      <div class="tech-detail-item-sub">상단: ${bb.upper ? money.format(Math.round(bb.upper)) : '-'} / 하단: ${bb.lower ? money.format(Math.round(bb.lower)) : '-'} — ${bbMeaning}</div>
    </div>`;
  }
  
  // 스토캐스틱
  if (indicators.stochastic) {
    const stoch = indicators.stochastic;
    const stochCls = stoch.k > 80 ? 'up' : stoch.k < 20 ? 'down' : 'neutral';
    let stochMeaning;
    if (stoch.k > 80) {
      stochMeaning = '과매수 — 하락 전환 가능';
    } else if (stoch.k < 20) {
      stochMeaning = '과매도 — 반등 기대';
    } else if (stoch.k > stoch.d) {
      stochMeaning = '%K > %D — 단기 상승 모멘텀';
    } else {
      stochMeaning = '%K < %D — 단기 하락 모멘텀';
    }
    html += `<div class="tech-detail-item">
      <div class="tech-detail-item-label">스토캐스틱</div>
      <div class="tech-detail-item-value ${stochCls}">%K: ${stoch.k?.toFixed(1) || '-'}</div>
      <div class="tech-detail-item-sub">%D: ${stoch.d?.toFixed(1) || '-'} — ${stochMeaning}</div>
    </div>`;
  }
  
  html += '</div></div>';
  
  // 거래량 분석 섹션
  const candles = chartData.candles || [];
  if (candles.length >= 10) {
    const currentVol = candles[candles.length - 1].volume || 0;
    const recent20 = candles.slice(-20);
    const avgVol20 = recent20.reduce((s, c) => s + (c.volume || 0), 0) / recent20.length;
    const volRatio = avgVol20 > 0 ? currentVol / avgVol20 : 1;
    
    // 5일 거래량 추세
    const recent5 = candles.slice(-5);
    const recent5Prev = candles.slice(-10, -5);
    const avgVol5 = recent5.reduce((s, c) => s + (c.volume || 0), 0) / recent5.length;
    const avgVol5Prev = recent5Prev.length ? recent5Prev.reduce((s, c) => s + (c.volume || 0), 0) / recent5Prev.length : avgVol5;
    const volTrend = avgVol5Prev > 0 ? ((avgVol5 - avgVol5Prev) / avgVol5Prev * 100) : 0;
    
    // 가격-거래량 관계 (요약용)
    const priceChange5 = candles.length >= 6 ? ((candles[candles.length - 1].close - candles[candles.length - 6].close) / candles[candles.length - 6].close * 100) : 0;
    
    // 요약 문장 생성
    let volSummary;
    const priceDir = priceChange5 > 1 ? '상승' : priceChange5 < -1 ? '하락' : '보합';
    const priceDirSuffix = priceChange5 > 1 ? '속' : priceChange5 < -1 ? '속' : '속';
    const volDir = volTrend > 10 ? '증가' : volTrend < -10 ? '감소' : '보합';
    
    if (priceDir === '보합' && volDir === '감소') volSummary = '가격보합속 거래량감소로 관망세 심화';
    else if (priceDir === '보합' && volDir === '증가') volSummary = '가격보합속 거래량증가로 방향 탐색 중';
    else if (priceDir === '상승' && volDir === '증가') volSummary = '가격상승속 거래량증가로 상승세 강화';
    else if (priceDir === '상승' && volDir === '감소') volSummary = '가격상승속 거래량감소로 상승세 약화, 하락전환 가능성 주의';
    else if (priceDir === '하락' && volDir === '증가') volSummary = '가격하락속 거래량증가로 하락세 강화, 추가하락 가능';
    else if (priceDir === '하락' && volDir === '감소') volSummary = '가격하락속 거래량감소로 하락세 약화, 반등 가능성';
    else if (priceDir === '상승') volSummary = '가격상승 지속 중, 거래량 변화 뚜렷하지 않음';
    else if (priceDir === '하락') volSummary = '가격하락 지속 중, 거래량 변화 뚜렷하지 않음';
    else volSummary = '뚜렷한 가격-거래량 변화 없음';
    
    const summaryCls = (priceDir === '상승' && volDir === '감소') || (priceDir === '하락' && volDir === '증가') ? 'down' : 
                       (priceDir === '상승' && volDir === '증가') || (priceDir === '하락' && volDir === '감소') ? 'up' : 'neutral';
    
    html += '<div class="tech-detail-section">';
    html += `<div class="tech-detail-section-title">📊 거래량 분석 <span style="font-size:12px;font-weight:500;color:var(--muted);margin-left:8px">${volSummary}</span></div>`;
    html += '<div class="tech-detail-grid">';
    
    const volCls = volRatio >= 2.0 ? 'up' : volRatio <= 0.5 ? 'down' : 'neutral';
    
    html += `<div class="tech-detail-item">
      <div class="tech-detail-item-label">현재 거래량</div>
      <div class="tech-detail-item-value ${volCls}">${money.format(Math.round(currentVol))}주</div>
      <div class="tech-detail-item-sub">20일 평균 대비 ${volRatio.toFixed(1)}배</div>
    </div>`;
    
    html += `<div class="tech-detail-item">
      <div class="tech-detail-item-label">20일 평균 거래량</div>
      <div class="tech-detail-item-value neutral">${money.format(Math.round(avgVol20))}주</div>
      <div class="tech-detail-item-sub">거래량 기준선</div>
    </div>`;
    
    const trendCls = volTrend > 10 ? 'up' : volTrend < -10 ? 'down' : 'neutral';
    
    html += `<div class="tech-detail-item">
      <div class="tech-detail-item-label">5일 거래량 추세</div>
      <div class="tech-detail-item-value ${trendCls}">${volTrend > 0 ? '+' : ''}${volTrend.toFixed(1)}%</div>
      <div class="tech-detail-item-sub">최근 거래량 변화 추이</div>
    </div>`;
    
    html += `<div class="tech-detail-item">
      <div class="tech-detail-item-label">가격-거래량 관계</div>
      <div class="tech-detail-item-value neutral">5일 가격 ${priceChange5 > 0 ? '+' : ''}${priceChange5.toFixed(1)}%</div>
      <div class="tech-detail-item-sub">${priceDir === '상승' ? '가격↑' : priceDir === '하락' ? '가격↓' : '가격-'} + 거래량${volDir === '증가' ? '↑' : volDir === '감소' ? '↓' : '-'}</div>
    </div>`;
    
    html += '</div></div>';
  }
  
  // 시그널 분석 섹션
  html += '<div class="tech-detail-section">';

  // 시그널 요약
  let signalSummary = '특이사항 없음';
  if (signals.length > 0) {
    let posCount = 0, negCount = 0;
    signals.forEach(s => {
      if (s.includes('매수') || s.includes('상승') || s.includes('골든') || s.includes('과매도')) posCount++;
      if (s.includes('매도') || s.includes('하락') || s.includes('데드') || s.includes('과매수')) negCount++;
    });
    if (posCount > negCount) signalSummary = `매수 신호 ${posCount}건 우세 — 상승 모멘텀 기대`;
    else if (negCount > posCount) signalSummary = `매도 신호 ${negCount}건 우세 — 하락 주의`;
    else signalSummary = `매수·매도 신호 혼재 — 관망 추천`;
  }
  html += `<div class="tech-detail-section-title">🎯 시그널 분석 <span style="font-size:12px;font-weight:500;color:var(--muted);margin-left:8px">${signalSummary}</span></div>`;
  
  // 시그널 설명 매핑
  const signalDescriptions = {
    '정배열': '단기 이동평균선이 장기선 위에 있어 상승 추세가 강함',
    '역배열': '단기 이동평균선이 장기선 아래에 있어 하락 추세가 강함',
    'MACD 골든크로스': 'MACD선이 시그널선을 아래에서 위로 돌파 — 매수 신호',
    'MACD 데드크로스': 'MACD선이 시그널선을 위에서 아래로 돌파 — 매도 신호',
    'MACD 히스토그램 양전환': 'MACD와 시그널 차이가 양전환 — 상승 모멘텀 시작',
    'MACD 히스토그램 음전환': 'MACD와 시그널 차이가 음전환 — 하락 모멘텀 시작',
    'RSI 과매수': 'RSI 70 이상 — 상승 과잉, 하락 전환 가능',
    'RSI 과매도': 'RSI 30 이하 — 하락 과잉, 반등 기대',
    'RSI 강세': 'RSI 60-70 — 상승 모멘텀 유지 중',
    'RSI 약세': 'RSI 30-40 — 하락 모멘텀 지속 중',
    'RSI 중립': 'RSI 40-60 — 뚜렷한 방향 없음',
    '볼린저 상단 돌파': '주가가 볼린저 밴드 상단 돌파 — 과열 상태',
    '볼린저 하단 이탈': '주가가 볼린저 밴드 하단 이탈 — 과매도 상태',
    '볼린저 상단 접근': '주가가 볼린저 밴드 상단에 접근 중 — 매도 압력 가능',
    '볼린저 하단 접근': '주가가 볼린저 밴드 하단에 접근 중 — 매수 기회 가능',
    '스토캐스틱 과매수': '%K 80 이상 — 단기 과매수, 조정 가능',
    '스토캐스틱 과매도': '%K 20 이하 — 단기 과매도, 반등 기대',
    '스토캐스틱 골든크로스': '%K가 %D를 아래에서 위로 돌파 — 단기 매수 신호',
    '스토캐스틱 데드크로스': '%K가 %D를 위에서 아래로 돌파 — 단기 매도 신호',
    '거래량': '거래량 변화 관련 시그널',
    'MA20 대비': '20일 이동평균선 대비 주가가 과도하게 벗어남 — 과열 또는 과침 상태',
    '거래량 폭증': '평균 대비 3배 이상 거래량 증가 — 중요 변수 발생',
    '거래량 급증': '평균 대비 2배 이상 거래량 증가 — 추세 전환 신호',
    '거래량 급감': '평균 대비 0.3배 이하 거래량 감소 — 유동성 주의',
    '거래량 감소': '평균 대비 0.5배 이하 거래량 감소 — 관망세 심화',
  };
  
  if (signals.length > 0) {
    signals.forEach(signal => {
      const cls = signal.includes('매수') || signal.includes('상승') || signal.includes('골든') || signal.includes('과매도') ? 'positive' : 
                  signal.includes('매도') || signal.includes('하락') || signal.includes('데드') || signal.includes('과매수') ? 'negative' : 'neutral';
      
      // 설명 찾기 (숫자가 포함된 시그널도 매칭)
      let description = '';
      for (const [key, desc] of Object.entries(signalDescriptions)) {
        if (signal.includes(key)) {
          description = desc;
          break;
        }
      }
      
      html += `<div class="tech-detail-signal">
        <div class="tech-detail-signal-title ${cls}">• ${signal}</div>
        ${description ? `<div class="tech-detail-signal-desc">${description}</div>` : ''}
      </div>`;
    });
  } else {
    html += '<div class="tech-detail-signal"><div class="tech-detail-signal-title neutral">특이사항 없음</div></div>';
  }
  
  // 실시간 시그널
  if (realtimeSignals.length > 0) {
    html += '<div style="margin-top:12px">';
    html += '<div class="tech-detail-signal-title" style="margin-bottom:8px">⚡ 실시간 시그널</div>';
    realtimeSignals.forEach(sig => {
      const cls = sig.type === 'price_drop' || sig.type === 'volume_drop' ? 'negative' : 
                  sig.type === 'price_surge' || sig.type === 'volume_surge' ? 'positive' : 'neutral';
      html += `<div class="tech-detail-signal">
        <div class="tech-detail-signal-title ${cls}">${sig.message}</div>
        <div class="tech-detail-signal-desc">심각도: ${sig.severity === 'critical' ? '치명적' : '경고'}</div>
      </div>`;
    });
    html += '</div>';
  }
  
  html += '</div>';
  
  // 단기 추세 분석 섹션
  html += '<div class="tech-detail-section">';
  html += '<div class="tech-detail-section-title">📉 단기 추세 분석</div>';
  
  // 추세 전환 단계별 표시
  const trendPhase = trendData.trendPhase || '보합';
  const trendConfidence = trendData.trendConfidence || 0;
  const phaseLabels = {
    '상승시작': { text: '🟢 상승시작', cls: 'positive', desc: '내리다가 이제 오르기 시작' },
    '상승지속': { text: '🟢 상승지속', cls: 'positive', desc: '계속 올라가는 중' },
    '상승세약화': { text: '🟡 상승세약화', cls: 'neutral', desc: '오르긴 하는데 속도 둔화' },
    '하락시작': { text: '🔴 하락시작', cls: 'negative', desc: '오르다가 이제 내리기 시작' },
    '하락지속': { text: '🔴 하락지속', cls: 'negative', desc: '계속 내려가는 중' },
    '하락세약화': { text: '🟡 하락세약화', cls: 'neutral', desc: '내리긴 하는데 속도 둔화' },
    '바닥반등': { text: '🔵 바닥반등', cls: 'positive', desc: '내려가다가 바닥 찍고 반등' },
    '천장반락': { text: '🟠 천장반락', cls: 'negative', desc: '올라가다가 꼭짓점 찍고 조정' },
    '보합': { text: '⚪ 보합', cls: 'neutral', desc: '뚜렷한 추세 방향 없음' },
  };
  const phaseInfo = phaseLabels[trendPhase] || phaseLabels['보합'];
  
  html += `<div class="tech-detail-signal">`;
  html += `<div class="tech-detail-signal-title ${phaseInfo.cls}">${phaseInfo.text}</div>`;
  html += `<div class="tech-detail-signal-desc">${phaseInfo.desc}</div>`;
  html += `<div class="tech-detail-signal-desc" style="font-size:11px;color:var(--muted)">신뢰도: ${trendConfidence}%</div>`;
  html += `</div>`;
  
  // 추세 판단 근거 (signalReasons에서 가격 기반 이유 표시)
  const signalReasons = trendData.signalReasons || [];
  if (signalReasons.length > 0) {
    html += '<div class="tech-detail-indicators">';
    html += '<div class="tech-detail-indicator-title">추세 판단 근거</div>';
    signalReasons.slice(0, 3).forEach(reason => {
      html += `<div class="tech-detail-indicator-row">
        <span class="tech-detail-indicator-label" style="font-size:12px">•</span>
        <span class="tech-detail-indicator-value neutral" style="font-size:12px;font-weight:400">${reason}</span>
      </div>`;
    });
    html += '</div>';
  }
  
  // 기술적 지표 상태 (보조 참고)
  const trendIndicators = trendData.techIndicators || {};
  const trendSignalScore = trendData.techSignalScore || 0;
  
  html += '<div class="tech-detail-indicators" style="margin-top:8px">';
  html += '<div class="tech-detail-indicator-title">기술적 지표 (보조 참고)</div>';
  
  // 이동평균선 상태
  if (trendIndicators.ma5 && trendIndicators.ma20 && trendIndicators.ma60) {
    const maStatus = trendIndicators.ma5 > trendIndicators.ma20 && trendIndicators.ma20 > trendIndicators.ma60 
      ? '정배열 (상승추세)' 
      : trendIndicators.ma5 < trendIndicators.ma20 && trendIndicators.ma20 < trendIndicators.ma60 
        ? '역배열 (하락추세)' 
        : '혼조';
    const maClass = trendIndicators.ma5 > trendIndicators.ma20 ? 'positive' : 'negative';
    html += `<div class="tech-detail-indicator-row">
      <span class="tech-detail-indicator-label">이동평균선 배열</span>
      <span class="tech-detail-indicator-value ${maClass}">${maStatus}</span>
    </div>`;
  }
  
  // RSI 상태
  if (trendIndicators.rsi14 !== null && trendIndicators.rsi14 !== undefined) {
    const rsiClass = trendIndicators.rsi14 > 60 ? 'positive' : trendIndicators.rsi14 < 40 ? 'negative' : 'neutral';
    const rsiStatus = trendIndicators.rsi14 > 60 ? '강세' : trendIndicators.rsi14 < 40 ? '약세' : '중립';
    html += `<div class="tech-detail-indicator-row">
      <span class="tech-detail-indicator-label">RSI(14)</span>
      <span class="tech-detail-indicator-value ${rsiClass}">${trendIndicators.rsi14.toFixed(1)} (${rsiStatus})</span>
    </div>`;
  }
  
  // MACD 상태
  if (trendIndicators.macd && trendIndicators.macd.macd !== null && trendIndicators.macd.signal !== null) {
    const macdClass = trendIndicators.macd.macd > trendIndicators.macd.signal ? 'positive' : 'negative';
    const macdStatus = trendIndicators.macd.macd > trendIndicators.macd.signal ? '상승 모멘텀' : '하락 모멘텀';
    html += `<div class="tech-detail-indicator-row">
      <span class="tech-detail-indicator-label">MACD</span>
      <span class="tech-detail-indicator-value ${macdClass}">${macdStatus}</span>
    </div>`;
  }
  
  // 기술적 시그널 점수
  const scoreClass = trendSignalScore > 0 ? 'positive' : trendSignalScore < 0 ? 'negative' : 'neutral';
  const scoreStatus = trendSignalScore > 20 ? '강세' : trendSignalScore < -20 ? '약세' : '중립';
  html += `<div class="tech-detail-indicator-row">
    <span class="tech-detail-indicator-label">기술적 시그널 점수</span>
    <span class="tech-detail-indicator-value ${scoreClass}">${trendSignalScore > 0 ? '+' : ''}${trendSignalScore} (${scoreStatus})</span>
  </div>`;
  
  html += '</div></div>';
  
  techDetailBody.innerHTML = html;
}

/* Attach trend click handlers (보합/상승/하락 클릭) */
function attachTrendHandlers(container) {
  container.querySelectorAll('.trend-cell').forEach(el => {
    el.style.cursor = 'pointer';
    el.addEventListener('click', () => {
      const row = el.closest('tr');
      if (!row) return;
      const name = row.querySelector('.name-cell strong')?.textContent || '';
      const code = row.querySelector('.name-cell small')?.textContent || '';
      const trendData = JSON.parse(el.dataset.trend || '{}');
      showTechDetailModal(name, code, trendData);
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
      const avgPrice = parseFloat(el.dataset.avg) || 0;
      if (code && name) showChartModal(name, code, avgPrice);
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

function sortHoldingsData(data) {
  if (!holdingsSort.key) return data;
  
  const sorted = [...data];
  sorted.sort((a, b) => {
    let valA, valB;
    
    switch (holdingsSort.key) {
      case 'avgPrice':
        valA = a.avgPrice || 0;
        valB = b.avgPrice || 0;
        break;
      case 'profitRate':
        valA = a.realizedProfitRate || 0;
        valB = b.realizedProfitRate || 0;
        break;
      case 'profit':
        valA = a.realizedProfit || 0;
        valB = b.realizedProfit || 0;
        break;
      default:
        return 0;
    }
    
    if (valA < valB) return holdingsSort.dir === 'asc' ? -1 : 1;
    if (valA > valB) return holdingsSort.dir === 'asc' ? 1 : -1;
    return 0;
  });
  
  return sorted;
}

function updateSortIcons() {
  document.querySelectorAll('#holdingsTable th.sortable').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.sort === holdingsSort.key) {
      th.classList.add(holdingsSort.dir === 'asc' ? 'sort-asc' : 'sort-desc');
    }
  });
}

// 추세 시각화 전역 상수
const PHASE_COLORS = {
  '상승시작': '#22c55e', '상승지속': '#16a34a', '상승세약화': '#eab308',
  '하락시작': '#ef4444', '하락지속': '#dc2626', '하락세약화': '#f59e0b',
  '바닥반등': '#3b82f6', '천장반락': '#f97316', '보합': '#94a3b8'
};
const PHASE_LABELS = {
  '상승시작': '↗ 상승시작', '상승지속': '↑ 상승지속', '상승세약화': '⇀ 상승세약화',
  '하락시작': '↘ 하락시작', '하락지속': '↓ 하락지속', '하락세약화': '⇀ 하락세약화',
  '바닥반등': '⤴ 바닥반등', '천장반락': '⤵ 천장반락', '보합': '― 보합'
};

function makeMidTrendHtml(trendData) {
  const midTrend = trendData.midTrend || '보합';
  const midTrendReasons = trendData.midTrendReasons || [];
  const midCumulativeChange = trendData.midCumulativeChange || 0;
  
  const trendLabel = PHASE_LABELS[midTrend] || midTrend;
  const trendColor = PHASE_COLORS[midTrend] || '#94a3b8';
  
  const chgText = midTrend.includes('지속') && midCumulativeChange 
    ? ` (${midCumulativeChange > 0 ? '+' : ''}${midCumulativeChange}%)` 
    : '';
  
  const mainReason = midTrendReasons.length > 0 ? midTrendReasons[0] : '';
  
  return `<span style="font-size:11px;font-weight:600;color:${trendColor}">${trendLabel}${chgText}</span>
    ${mainReason ? `<br><small style="opacity:0.6;font-size:10px">${mainReason}</small>` : ''}`;
}

function makeLongTrendHtml(trendData) {
  const longTrend = trendData.longTrend || '보합';
  const longTrendReasons = trendData.longTrendReasons || [];
  const longCumulativeChange = trendData.longCumulativeChange || 0;
  
  const trendLabel = PHASE_LABELS[longTrend] || longTrend;
  const trendColor = PHASE_COLORS[longTrend] || '#94a3b8';
  
  const chgText = longTrend.includes('지속') && longCumulativeChange 
    ? ` (${longCumulativeChange > 0 ? '+' : ''}${longCumulativeChange}%)` 
    : '';
  
  const mainReason = longTrendReasons.length > 0 ? longTrendReasons[0] : '';
  
  return `<span style="font-size:11px;font-weight:600;color:${trendColor}">${trendLabel}${chgText}</span>
    ${mainReason ? `<br><small style="opacity:0.6;font-size:10px">${mainReason}</small>` : ''}`;
}

function renderHoldings(rows) {
  lastHoldingsRows = rows;
  const sortedRows = sortHoldingsData(rows);
  holdingsBody.innerHTML = '';
  sortedRows.forEach(row => {
    const tr = document.createElement('tr');
    
    // 실시간 시그널 표시
    let realtimeHtml = '';
    if (row.realtimeSignals && row.realtimeSignals.length > 0) {
      realtimeHtml = '<div class="realtime-signals">';
      row.realtimeSignals.forEach(sig => {
        const cls = sig.type === 'price_drop' || sig.type === 'volume_drop' ? 'down' : 
                    sig.type === 'price_surge' || sig.type === 'volume_surge' ? 'up' : 'neutral';
        const severityCls = sig.severity === 'critical' ? 'critical' : '';
        realtimeHtml += `<span class="realtime-signal ${cls} ${severityCls}" title="${sig.message}">⚡ ${sig.message}</span>`;
      });
      realtimeHtml += '</div>';
    }
    
    // 단기추세 (추세 전환 감지)
    const t = row.trend || {};
    const trendPhase = t.trendPhase || '보합';
    const trendDataAttr = `data-trend='${JSON.stringify(t)}'`;
    const trendLabel = PHASE_LABELS[trendPhase] || trendPhase;
    const trendColor = PHASE_COLORS[trendPhase] || '#94a3b8';
    
    // 추세 근거 요약
    const trendReasons = t.signalReasons || [];
    const trendSummary = trendReasons.length > 0 ? trendReasons[0] : '';
    
    tr.innerHTML = `
      <td><div class="name-cell"><strong class="clickable" data-code="${row.code}" data-name="${row.name}" data-avg="${row.avgPrice}">${row.name}</strong><small>${row.code}</small></div></td>
      <td>${money.format(row.quantity)}주</td>
      <td class="clickable" data-code="${row.code}" data-name="${row.name}" data-avg="${row.avgPrice}">${formatMoney(row.currentPrice)}</td>
      <td class="${row.change > 0 ? 'up' : row.change < 0 ? 'down' : 'neutral'}">${formatSignedMoney(row.change)} / ${formatPercent(row.changeRate)}</td>
      <td class="avg-price-cell" data-code="${row.code}" data-name="${row.name}" data-avg="${row.avgPrice}" data-qty="${row.quantity}" data-price="${row.currentPrice}">${formatMoney(row.avgPrice)}</td>
      <td class="${row.realizedProfit >= 0 ? 'up' : 'down'}">${formatPercent(row.realizedProfitRate)}</td>
      <td class="${row.realizedProfit >= 0 ? 'up' : 'down'}">${formatSignedMoney(row.realizedProfit)}<br><small style="opacity:0.6">(비용 ${formatMoney(Math.round(row.sellFee))})</small></td>
      <td class="trend-cell trend-clickable" ${trendDataAttr}><span style="font-size:11px;font-weight:600;color:${trendColor}">${trendLabel}</span>${trendSummary ? `<br><small style="opacity:0.6;font-size:10px">${trendSummary}</small>` : ''}</td>
      <td>${makeMidTrendHtml(t)}</td>
      <td>${makeLongTrendHtml(t)}</td>
    `;
    holdingsBody.appendChild(tr);
    makeDraggable(tr, holdingsBody);
  });
  attachChartHandlers(holdingsBody);
  attachCalcHandlers(holdingsBody);
  attachTrendHandlers(holdingsBody);
  updateSortIcons();
}

function renderWatchlist(rows) {
  watchlistBody.innerHTML = '';
  rows.forEach(row => {
    const t = row.trend;
    const dayRange = (row.high && row.low)
      ? `${formatMoney(row.low)} ~ ${formatMoney(row.high)}`
      : '-';
    const tr = document.createElement('tr');
    
    // 실시간 시그널 표시
    let realtimeHtml = '';
    if (row.realtimeSignals && row.realtimeSignals.length > 0) {
      realtimeHtml = '<div class="realtime-signals">';
      row.realtimeSignals.forEach(sig => {
        const cls = sig.type === 'price_drop' || sig.type === 'volume_drop' ? 'down' : 
                    sig.type === 'price_surge' || sig.type === 'volume_surge' ? 'up' : 'neutral';
        const severityCls = sig.severity === 'critical' ? 'critical' : '';
        realtimeHtml += `<span class="realtime-signal ${cls} ${severityCls}" title="${sig.message}">⚡ ${sig.message}</span>`;
      });
      realtimeHtml += '</div>';
    }
    
    // 단기추세 (추세 전환 감지)
    const trendPhase = t.trendPhase || '보합';
    const trendDataAttr = `data-trend='${JSON.stringify(t)}'`;
    const trendLabel2 = PHASE_LABELS[trendPhase] || trendPhase;
    const trendColor2 = PHASE_COLORS[trendPhase] || '#94a3b8';
    // 추세 근거 요약
    const trendReasons = t.signalReasons || [];
    const trendSummary = trendReasons.length > 0 ? trendReasons[0] : '';
    
    tr.innerHTML = `
      <td><div class="name-cell"><strong class="clickable" data-code="${row.code}" data-name="${row.name}">${row.name}</strong><small>${row.code}</small></div></td>
      <td class="clickable" data-code="${row.code}" data-name="${row.name}">${formatMoney(row.currentPrice)}</td>
      <td class="${row.change > 0 ? 'up' : row.change < 0 ? 'down' : 'neutral'}">${formatSignedMoney(row.change)} / ${formatPercent(row.changeRate)}</td>
      <td>${dayRange}<br>${rangeBar(t.rangePos)}</td>
      <td>${t.volatility}%</td>
      <td class="trend-cell trend-clickable" ${trendDataAttr}><span style="font-size:11px;font-weight:600;color:${trendColor2}">${trendLabel2}</span>${trendSummary ? `<br><small style="opacity:0.6;font-size:10px">${trendSummary}</small>` : ''}</td>
      <td>${makeMidTrendHtml(t)}</td>
      <td>${makeLongTrendHtml(t)}</td>
    `;
    watchlistBody.appendChild(tr);
    makeDraggable(tr, watchlistBody);
  });
  attachChartHandlers(watchlistBody);
  attachTrendHandlers(watchlistBody);
}

/* ────────────────────────────────────────── */
/* 드래그 앤 드롭 순서 조정                     */
/* ────────────────────────────────────────── */
let dragSrcRow = null;
let dragSrcBody = null;

function makeDraggable(tr, tbody) {
  tr.draggable = true;
  tr.style.cursor = 'grab';

  tr.addEventListener('dragstart', (e) => {
    dragSrcRow = tr;
    dragSrcBody = tbody;
    tr.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', '');
  });

  tr.addEventListener('dragend', () => {
    tr.classList.remove('dragging');
    document.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
    dragSrcRow = null;
    dragSrcBody = null;
  });

  tr.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    if (dragSrcBody === tbody) {
      tr.classList.add('drag-over');
    }
  });

  tr.addEventListener('dragleave', () => {
    tr.classList.remove('drag-over');
  });

  tr.addEventListener('drop', (e) => {
    e.preventDefault();
    tr.classList.remove('drag-over');
    if (!dragSrcRow || dragSrcBody !== tbody || dragSrcRow === tr) return;

    const rows = [...tbody.querySelectorAll('tr')];
    const fromIdx = rows.indexOf(dragSrcRow);
    const toIdx = rows.indexOf(tr);
    if (fromIdx < toIdx) {
      tbody.insertBefore(dragSrcRow, tr.nextSibling);
    } else {
      tbody.insertBefore(dragSrcRow, tr);
    }

    const section = tbody.id === 'holdingsBody' ? 'holdings' : 'watchlist';
    const codes = [...tbody.querySelectorAll('tr')].map(r => {
      const nameEl = r.querySelector('.name-cell strong') || r.querySelector('strong');
      return nameEl?.dataset?.code || '';
    }).filter(Boolean);

    const storageKey = section === 'holdings' ? HOLDINGS_ORDER_KEY : WATCHLIST_ORDER_KEY;
    localStorage.setItem(storageKey, JSON.stringify(codes));

    fetch('/api/reorder', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ section, codes }),
    }).catch(() => {});
    reorderLockUntil = Date.now() + 5000;
  });
}

function renderSummary(summary) {
  const totalValue = document.querySelector('#totalValue');
  const totalCost = document.querySelector('#totalCost');
  const totalProfit = document.querySelector('#totalProfit');
  const totalRate = document.querySelector('#totalRate');
  const profitTrend = document.querySelector('#profitTrend');

  totalValue.textContent = formatThousand(summary.currentValue);
  totalCost.textContent = formatThousand(summary.cost);
  totalProfit.innerHTML = `${formatSignedThousand(summary.realizedProfit)}<br><small style="opacity:0.6">(비용 ${formatMoney(Math.round(summary.sellFee))})</small>`;
  totalRate.textContent = formatPercent(summary.realizedProfitRate);
  setSignedClass(totalProfit, summary.realizedProfit);
  setSignedClass(totalRate, summary.realizedProfitRate);

  profitHistory.push(summary.realizedProfit);
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
    if (kospiKosdaqDate) {
      const statusText = data.marketStatus === 'open' ? '장 운영 중 · 실시간' :
                         data.marketStatus === 'pre_open' ? '장 시작 전' : '마감';
      kospiKosdaqDate.textContent = `${data.date} ${statusText}`;
    }
  }

  if (data.marketStatus === 'open') {
    html += '<div class="market-open-indicator">🔴 실시간 시황</div>';
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
  } else {
    html += '<p class="muted">지수 데이터를 불러올 수 없습니다.</p>';
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

function getUSMarketStatus() {
  const now = new Date();
  const utcHours = now.getUTCHours();
  const utcMinutes = now.getUTCMinutes();
  const utcTime = utcHours * 60 + utcMinutes;
  
  // 한국시간 UTC+9에서 미국 동부시간 UTC-4(EDT) 또는 UTC-5(EST)로 변환
  // 현재 6월이면 EDT(UTC-4) 사용
  const kstOffset = 9 * 60;
  const edtOffset = -4 * 60;
  const usTime = (utcTime + kstOffset + edtOffset + 24 * 60) % (24 * 60);
  
  // 미국 주식시장 시간: 09:30 ~ 16:00 (EDT)
  const marketOpen = 9 * 60 + 30;
  const marketClose = 16 * 60;
  
  if (usTime >= marketOpen && usTime <= marketClose) {
    return 'open';
  } else if (usTime < marketOpen) {
    return 'pre_open';
  } else {
    return 'closed';
  }
}

function renderUSMarket(data) {
  if (!usMarketBody || !data) return;

  let html = '';
  const marketStatus = data.marketStatus || getUSMarketStatus();
  if (data.date) {
    if (usMarketDate) {
      const statusText = marketStatus === 'open' ? '장 운영 중 · 실시간' :
                         marketStatus === 'pre_open' ? '장 시작 전' : '마감';
      usMarketDate.textContent = `${data.date} ${statusText}`;
    }
  }

  if (marketStatus === 'open') {
    html += '<div class="market-open-indicator">🔴 실시간 시황</div>';
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
          <span class="us-index-change ${cls}">${sign}${idx.rate.toFixed(2)}%</span>
        </span>
      </div>`;
    });
  } else {
    html += '<p class="muted">지수 데이터를 불러올 수 없습니다.</p>';
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
    const url = watchlistExpanded ? '/api/portfolio' : '/api/portfolio?section=holdings';
    const response = await fetch(url, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderSummary(data.summary);
    if (Date.now() < reorderLockUntil) {
      statusPill.textContent = `정상 · ${new Date().toLocaleTimeString('ko-KR')}`;
    } else {
      renderHoldings(sortBySavedOrder(data.holdings, HOLDINGS_ORDER_KEY));
      if (watchlistExpanded && data.watchlist) {
        renderWatchlist(sortBySavedOrder(data.watchlist, WATCHLIST_ORDER_KEY));
      }
    }
    sourceText.textContent = `네이버 금융 polling API · ${data.refreshSeconds}초 자동 갱신`;
    statusPill.textContent = `정상 · ${new Date().toLocaleTimeString('ko-KR')}`;

    const now = Date.now();
    if (now - lastSidebarRefresh > SIDEBAR_REFRESH_INTERVAL) {
      lastSidebarRefresh = now;
      loadKospiKosdaq();
      loadUSMarket();
      loadKrMarketNews();
      loadUSMarketNews();
      loadNews();
      if (watchlistExpanded) loadTraderFlow();
    }
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
  // AI 분석 비활성화
}

function isMarketOpen() {
  const now = new Date();
  const h = now.getHours();
  const m = now.getMinutes();
  const day = now.getDay();
  const weekday = day >= 1 && day <= 5;
  if (!weekday) return false;
  const t = h * 60 + m;
  return t >= 540 && t <= 930;  // 09:00 ~ 15:30
}

function isOffHours() {
  const now = new Date();
  const h = now.getHours();
  const m = now.getMinutes();
  const t = h * 60 + m;
  return t >= 1205 || t < 475;  // 20:05 ~ 07:55
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
    autoTimer = setInterval(() => {
      if (isOffHours()) return;
      loadPortfolio();
    }, 5000);
  }
}

function setupNewsRefresh() {
  if (newsTimer) clearInterval(newsTimer);
  newsTimer = setInterval(() => {
    if (isOffHours()) {
      loadNews();
      loadKospiKosdaq();
      loadUSMarket();
      loadKrMarketNews();
      loadUSMarketNews();
    }
  }, 300000);  // 장 마감 시 5분마다 뉴스 갱신
  newsRefreshBtn.addEventListener('click', () => { loadKospiKosdaq(); loadUSMarket(); loadKrMarketNews(); loadUSMarketNews(); });
}

/* ────────────────────────────────────────── */
/* 외국인·기관 매매현황                        */
/* ────────────────────────────────────────── */
const traderFlowBody = document.querySelector('#traderFlowBody');
const traderFlowUpdatedAt = document.querySelector('#traderFlowUpdatedAt');
const traderFlowRefreshBtn = document.querySelector('#traderFlowRefreshBtn');

function formatNet(value) {
  if (value === null || value === undefined) return '-';
  const abs = Math.abs(Math.round(value));
  const formatted = abs >= 10000 ? (abs / 10000).toFixed(0) + '만' : money.format(abs);
  return (value > 0 ? '+' : value < 0 ? '-' : '') + formatted + '주';
}

function renderTraderFlowCard(item) {
  const name = item.name || item.code;
  const s = item.summary || {};
  const inst1d = s.instNet1d;
  const frgn1d = s.frgnNet1d;
  const inst5d = s.instNet5d;
  const frgn5d = s.frgnNet5d;
  const frgnRatio = s.frgnRatio;
  const trend = s.trend || '';

  let trendCls = 'mixed';
  if (trend.includes('동반매수')) trendCls = 'both-buy';
  else if (trend.includes('동반매도')) trendCls = 'both-sell';

  const recentRows = (item.rows || []).slice(0, 5);

  return `
    <div class="trader-flow-card" data-code="${item.code}">
      <div class="trader-flow-card-head">
        <span class="stock-name">${name} <small style="color:var(--muted);font-weight:400">${item.code}</small></span>
        ${trend ? `<span class="trend-badge ${trendCls}">${trend}</span>` : ''}
      </div>
      <div class="trader-flow-summary">
        <div class="summary-item">
          <div class="summary-label">기관 1일</div>
          <div class="summary-value ${inst1d > 0 ? 'up' : inst1d < 0 ? 'down' : ''}">${formatNet(inst1d)}</div>
        </div>
        <div class="summary-item">
          <div class="summary-label">외국인 1일</div>
          <div class="summary-value ${frgn1d > 0 ? 'up' : frgn1d < 0 ? 'down' : ''}">${formatNet(frgn1d)}</div>
        </div>
        <div class="summary-item">
          <div class="summary-label">기관 5일</div>
          <div class="summary-value ${inst5d > 0 ? 'up' : inst5d < 0 ? 'down' : ''}">${formatNet(inst5d)}</div>
        </div>
        <div class="summary-item">
          <div class="summary-label">외국인 5일</div>
          <div class="summary-value ${frgn5d > 0 ? 'up' : frgn5d < 0 ? 'down' : ''}">${formatNet(frgn5d)}</div>
        </div>
      </div>
      <div class="trader-flow-detail" id="trader-detail-${item.code}">
        <table>
          <thead>
            <tr>
              <th>날짜</th>
              <th>종가</th>
              <th>기관 순매매</th>
              <th>외국인 순매매</th>
              <th>외국인 비중</th>
            </tr>
          </thead>
          <tbody>
            ${recentRows.map(r => `
              <tr>
                <td>${r.date}</td>
                <td>${r.close ? money.format(Math.round(r.close)) + '원' : '-'}</td>
                <td class="${r.instNet > 0 ? 'up' : r.instNet < 0 ? 'down' : ''}">${formatNet(r.instNet)}</td>
                <td class="${r.frgnNet > 0 ? 'up' : r.frgnNet < 0 ? 'down' : ''}">${formatNet(r.frgnNet)}</td>
                <td>${r.frgnRatio != null ? r.frgnRatio.toFixed(1) + '%' : '-'}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

function renderTraderFlow(items) {
  if (!items || !items.length) {
    traderFlowBody.innerHTML = '<p class="muted">데이터가 없습니다.</p>';
    return;
  }
  traderFlowBody.innerHTML = items.map(renderTraderFlowCard).join('');

  traderFlowBody.querySelectorAll('.trader-flow-card').forEach(card => {
    card.addEventListener('click', () => {
      const code = card.dataset.code;
      const detail = document.getElementById(`trader-detail-${code}`);
      if (detail) detail.classList.toggle('open');
    });
  });

  traderFlowUpdatedAt.textContent = new Date().toLocaleTimeString('ko-KR');
}

async function loadTraderFlow() {
  try {
    const portfolioRes = await fetch('/api/portfolio', { cache: 'no-store' });
    if (!portfolioRes.ok) return;
    const portfolio = await portfolioRes.json();

    const codes = new Set();
    (portfolio.holdings || []).forEach(h => codes.add(h.code));
    (portfolio.watchlist || []).forEach(w => codes.add(w.code));

    const nameMap = {};
    (portfolio.holdings || []).forEach(h => nameMap[h.code] = h.name);
    (portfolio.watchlist || []).forEach(w => nameMap[w.code] = w.name);

    const results = [];
    const fetches = [...codes].map(code =>
      fetch(`/api/trader-flow?code=${code}`, { cache: 'no-store' })
        .then(r => r.json())
        .then(data => {
          data.name = nameMap[code] || code;
          results.push(data);
        })
        .catch(() => {})
    );
    await Promise.all(fetches);

    results.sort((a, b) => {
      const ai = a.summary?.instNet5d || 0;
      const bi = b.summary?.instNet5d || 0;
      const af = a.summary?.frgnNet5d || 0;
      const bf = b.summary?.frgnNet5d || 0;
      return (bi + bf) - (ai + af);
    });

    renderTraderFlow(results);
  } catch (err) {
    traderFlowBody.innerHTML = `<p class="muted">매매동향을 불러오지 못했습니다: ${err.message}</p>`;
  }
}

traderFlowRefreshBtn.addEventListener('click', loadTraderFlow);

/* 장중 자동 갱신 (1분) */
let traderFlowTimer = null;
function setupTraderFlowAutoRefresh() {
  if (traderFlowTimer) clearInterval(traderFlowTimer);
  traderFlowTimer = setInterval(() => {
    const now = new Date();
    const hour = now.getHours();
    const min = now.getMinutes();
    const day = now.getDay();
    const isWeekday = day >= 1 && day <= 5;
    const isMarketOpen = isWeekday && ((hour === 9 && min >= 0) || (hour >= 10 && hour < 15) || (hour === 15 && min <= 30));
    if (isMarketOpen) {
      loadTraderFlow();
    }
  }, 60000);
}

refreshBtn.addEventListener('click', loadPortfolio);
autoBtn.addEventListener('click', () => setAutoRefresh(!autoEnabled));

/* Watchlist toggle (lazy load) */
const watchlistToggle = document.querySelector('#watchlistToggle');
const watchlistWrap = document.querySelector('#watchlistWrap');

watchlistToggle.addEventListener('click', async () => {
  watchlistExpanded = !watchlistExpanded;
  watchlistToggle.textContent = watchlistExpanded ? '접기' : '펼치기';
  watchlistWrap.style.display = watchlistExpanded ? '' : 'none';
  if (watchlistExpanded && !watchlistLoaded) {
    watchlistBody.innerHTML = '<tr><td colspan="7" class="loading">관심종목 데이터를 불러오는 중...</td></tr>';
    try {
      const res = await fetch('/api/portfolio', { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      renderWatchlist(sortBySavedOrder(data.watchlist, WATCHLIST_ORDER_KEY));
      watchlistLoaded = true;
    } catch (e) {
      watchlistBody.innerHTML = '<tr><td colspan="7" class="muted" style="text-align:center">관심종목을 불러올 수 없습니다.</td></tr>';
    }
  }
});

/* Korean Market News Section */
const krMarketNewsBody = document.querySelector('#krMarketNewsBody');
const krMarketNewsDate = document.querySelector('#krMarketNewsDate');
const krMarketNewsMore = document.querySelector('#krMarketNewsMore');

async function loadKrMarketNews() {
  try {
    const res = await fetch('/api/kr-market-news?limit=5', { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderKrMarketNews(data);
  } catch (e) {
    if (krMarketNewsBody) krMarketNewsBody.innerHTML = '<p class="muted">한국증시 뉴스를 불러올 수 없습니다.</p>';
  }
}

function renderKrMarketNews(data) {
  if (!krMarketNewsBody || !data) return;

  let html = '';
  if (krMarketNewsDate) {
    krMarketNewsDate.textContent = `최종 갱신: ${new Date().toLocaleTimeString('ko-KR')}`;
  }

  if (data.articles && data.articles.length) {
    data.articles.forEach(article => {
      html += `<a class="news-item" href="${article.url}" target="_blank" rel="noopener">
        <div class="news-title">${article.title}</div>
        ${article.source ? `<div class="news-meta">${article.source}</div>` : ''}
      </a>`;
    });
    if (krMarketNewsMore) krMarketNewsMore.style.display = 'block';
  } else {
    html = '<p class="muted">최신 뉴스 없음</p>';
    if (krMarketNewsMore) krMarketNewsMore.style.display = 'none';
  }

  krMarketNewsBody.innerHTML = html;
}

/* US Market News Section */
const usMarketNewsBody = document.querySelector('#usMarketNewsBody');
const usMarketNewsDate = document.querySelector('#usMarketNewsDate');
const usMarketNewsMore = document.querySelector('#usMarketNewsMore');

async function loadUSMarketNews() {
  try {
    const res = await fetch('/api/us-market-news?limit=5', { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderUSMarketNews(data);
  } catch (e) {
    if (usMarketNewsBody) usMarketNewsBody.innerHTML = '<p class="muted">미국증시 뉴스를 불러올 수 없습니다.</p>';
  }
}

function renderUSMarketNews(data) {
  if (!usMarketNewsBody || !data) return;

  let html = '';
  if (usMarketNewsDate) {
    usMarketNewsDate.textContent = `최종 갱신: ${new Date().toLocaleTimeString('ko-KR')}`;
  }

  if (data.articles && data.articles.length) {
    data.articles.forEach(article => {
      html += `<a class="news-item" href="${article.url}" target="_blank" rel="noopener">
        <div class="news-title">${article.title}</div>
        ${article.source ? `<div class="news-meta">${article.source}</div>` : ''}
      </a>`;
    });
    if (usMarketNewsMore) usMarketNewsMore.style.display = 'block';
  } else {
    html = '<p class="muted">최신 뉴스 없음</p>';
    if (usMarketNewsMore) usMarketNewsMore.style.display = 'none';
  }

  usMarketNewsBody.innerHTML = html;
}

initResize();

document.querySelectorAll('#holdingsTable th.sortable').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.sort;
    if (holdingsSort.key === key) {
      holdingsSort.dir = holdingsSort.dir === 'asc' ? 'desc' : 'asc';
    } else {
      holdingsSort.key = key;
      holdingsSort.dir = 'asc';
    }
    if (lastHoldingsRows) renderHoldings(lastHoldingsRows);
  });
});

loadPortfolio();
loadNews();
loadKospiKosdaq();
loadUSMarket();
loadKrMarketNews();
loadUSMarketNews();
setAutoRefresh(true);
setupNewsRefresh();
setupTraderFlowAutoRefresh();

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
