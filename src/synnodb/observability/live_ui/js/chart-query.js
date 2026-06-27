'use strict';

// Per-query bar chart: speedup vs DuckDB or absolute runtimes side-by-side.

const querySpeedupReferencePlugin = {
  id: 'querySpeedupReference',
  afterDraw(chart) {
    if (chart.canvas?.id !== 'qc') return;
    if (!chart.options.plugins?.querySpeedupReference?.enabled) return;

    const {ctx, chartArea, scales} = chart;
    const yScale = scales.y;
    if (!chartArea || !yScale) return;

    const y = yScale.getPixelForValue(1);
    if (!Number.isFinite(y) || y < chartArea.top || y > chartArea.bottom) return;

    ctx.save();
    ctx.strokeStyle = '#E4572E';
    ctx.lineWidth = 2;
    ctx.setLineDash([8, 5]);
    ctx.beginPath();
    ctx.moveTo(chartArea.left, y);
    ctx.lineTo(chartArea.right, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();
  },
};

// Inline legend at the top-right of the bar chart, including the speedup-=-1
// reference line when active. Built inline so it can stay tight against the
// chart area without taking layout height.
const queryInlineLegendPlugin = {
  id: 'queryInlineLegend',
  afterDraw(chart) {
    if (chart.canvas?.id !== 'qc') return;
    const {ctx, chartArea: ca} = chart;
    if (!ca) return;
    const items = [];
    chart.data.datasets.forEach((ds, i) => {
      if (!chart.isDatasetVisible(i)) return;
      items.push({
        text: ds.label,
        fill: Array.isArray(ds.backgroundColor) ? ds.backgroundColor[0] : ds.backgroundColor,
        stroke: Array.isArray(ds.borderColor) ? ds.borderColor[0] : ds.borderColor,
        dash: [],
      });
    });
    if (chart.options.plugins?.querySpeedupReference?.enabled) {
      items.push({text: 'Speedup = 1', fill: 'rgba(0,0,0,0)', stroke: '#E4572E', dash: [8, 5]});
    }
    if (!items.length) return;
    ctx.save();
    ctx.font = '10px system-ui, sans-serif';
    const BOX = 12, H = 8, GAP = 4, SEP = 10;
    const totalW = items.reduce((w, item, i) =>
      w + BOX + GAP + ctx.measureText(item.text).width + (i < items.length - 1 ? SEP : 0), 0);
    let x = ca.right - totalW - 8;
    const yBase = ca.top + 12;
    for (const item of items) {
      const tw = ctx.measureText(item.text).width;
      if (item.dash.length) {
        ctx.save();
        ctx.strokeStyle = item.stroke;
        ctx.lineWidth = 2;
        ctx.setLineDash(item.dash);
        ctx.beginPath(); ctx.moveTo(x, yBase - H/2); ctx.lineTo(x + BOX, yBase - H/2); ctx.stroke();
        ctx.restore();
      } else {
        ctx.fillStyle = item.fill;
        ctx.fillRect(x, yBase - H, BOX, H);
        ctx.strokeStyle = item.stroke;
        ctx.lineWidth = 1;
        ctx.strokeRect(x, yBase - H, BOX, H);
      }
      ctx.fillStyle = '#cdd5e0';
      ctx.fillText(item.text, x + BOX + GAP, yBase);
      x += BOX + GAP + tw + SEP;
    }
    ctx.restore();
  },
};

Chart.register(querySpeedupReferencePlugin);

function initQueryChart() {
  const ctx = document.getElementById('qc').getContext('2d');
  queryChart = new Chart(ctx, {
    type: 'bar',
    plugins: [queryInlineLegendPlugin],
    data: {
      labels: [],
      datasets: [
        {
          label: 'Speedup ×DuckDB',
          data: [],
          backgroundColor: 'rgba(57,140,175,0.65)',
          borderColor: '#398caf',
          borderWidth: 1,
        },
        {
          label: 'Bespoke (ms)',
          data: [],
          backgroundColor: 'rgba(85,168,104,0.65)',
          borderColor: '#55A868',
          borderWidth: 1,
          hidden: true,
        },
        {
          label: 'DuckDB baseline (ms)',
          data: [],
          backgroundColor: 'rgba(218,139,195,0.55)',
          borderColor: '#DA8BC3',
          borderWidth: 1,
          hidden: true,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      layout: {padding: {top: 0, right: 0, bottom: 0, left: 0}},
      plugins: {
        legend: {display: false},
        tooltip: {
          backgroundColor:'#1c2333', titleColor:'#cdd5e0',
          bodyColor:'#9090b0', borderColor:'#30374a', borderWidth:1,
          callbacks: {
            label: item => {
              const v = item.parsed.y;
              if (v == null) return null;
              return queryChartMode === 'speedup'
                ? ' Speedup: ' + v.toFixed(2) + '×'
                : ' ' + item.dataset.label + ': ' + v.toFixed(1) + ' ms';
            },
          },
        },
        querySpeedupReference: {enabled: true},
      },
      scales: {
        x: {
          ticks: {color:'#7280a0', font:{size:10}},
          grid:  {color:'rgba(48,55,74,0.8)'},
          title: {display:false, text:'Query', color:'#7280a0', font:{size:10}},
        },
        y: {
          min: 0,
          beginAtZero: true,
          grace: '12%',
          afterDataLimits: axis => { addAxisHeadroom(axis); },
          ticks: {color:'#7280a0', font:{size:10}},
          grid:  {color:'rgba(48,55,74,0.8)'},
          title: {display:true, text:'Speedup ×', color:'#7280a0', font:{size:10}},
        },
      },
    },
  });
}

function formatScaleFactor(sf) {
  if (!Number.isFinite(sf)) return String(sf);
  if (Number.isInteger(sf)) return String(sf);
  return (Math.round(sf * 100) / 100).toString();
}

// Rebuild the scale-factor selector when the set of observed SFs changes, and
// keep the active button in sync with the effective SF (user pick or fallback).
function updateScaleFactorButtons(steps, data) {
  const container = document.getElementById('qc-sf-toggle');
  if (!container) return;
  const sfs = getAvailableScaleFactors(steps, data);
  if (sfs.length <= 1) {
    container.hidden = true;
    container.innerHTML = '';
    container.dataset.sfs = '';
    return;
  }
  container.hidden = false;
  const wanted = sfs.map(String).join(',');
  if (container.dataset.sfs !== wanted) {
    container.innerHTML = '';
    for (const sf of sfs) {
      const btn = document.createElement('button');
      btn.className = 'sf-btn';
      btn.dataset.sf = String(sf);
      btn.textContent = 'SF ' + formatScaleFactor(sf);
      container.appendChild(btn);
    }
    container.dataset.sfs = wanted;
  }
  const effective = getEffectiveScaleFactor(steps, data);
  for (const btn of container.querySelectorAll('.sf-btn')) {
    const active = effective != null && Math.abs(Number(btn.dataset.sf) - effective) < 1e-12;
    btn.classList.toggle('active', active);
  }
}

// When time-travel is active, freeze the displayed values at that turn but
// keep the query set (= bar layout) the same as live so columns don't dance.
function updateQueryChart(steps, data) {
  if (!queryChart) return;
  const latestQueries = getQueryRuntimes(steps, data);
  const isSpeedup = queryChartMode === 'speedup';

  if (!latestQueries.length) {
    // No benchmark rows in the current data — clear the chart so we don't
    // keep showing bars from a previously loaded run after a source switch.
    queryChart.data.labels             = [];
    queryChart.data.datasets[0].data   = [];
    queryChart.data.datasets[1].data   = [];
    queryChart.data.datasets[2].data   = [];
    queryChart.data.datasets[0].hidden = !isSpeedup;
    queryChart.data.datasets[1].hidden =  isSpeedup;
    queryChart.data.datasets[2].hidden =  true;
    queryChart.options.plugins.querySpeedupReference.enabled = isSpeedup;
    queryChart.options.scales.y.min = 0;
    queryChart.options.scales.y.max = undefined;
    queryChart.options.scales.y.title.text = isSpeedup ? 'Speedup ×' : 'Runtime (ms)';
    queryChart.update();
    return;
  }

  const filteredSteps = timeTravelStep != null
    ? steps.filter(s => +s <= +timeTravelStep)
    : steps;
  const selectedQueriesById = new Map(getQueryRuntimes(filteredSteps, data).map(q => [q.id, q]));
  const queries = latestQueries.map(q => {
    const selected = selectedQueriesById.get(q.id);
    return {id: q.id, duck: selected?.duck ?? null, impl: selected?.impl ?? null};
  });

  queryChart.data.labels             = queries.map(q => 'Q' + q.id);
  queryChart.data.datasets[0].data   = queries.map(q =>
    (q.duck != null && q.impl != null && q.impl > 0) ? q.duck / q.impl : null);
  queryChart.data.datasets[1].data   = queries.map(q => q.impl);
  queryChart.data.datasets[2].data   = queries.map(q => q.duck);
  queryChart.data.datasets[0].hidden = !isSpeedup;
  queryChart.data.datasets[1].hidden =  isSpeedup;
  queryChart.data.datasets[2].hidden =  isSpeedup || queries.every(q => q.duck == null);
  queryChart.options.plugins.querySpeedupReference.enabled = isSpeedup;
  queryChart.options.scales.y.min = 0;
  queryChart.options.scales.y.max = getQueryAxisMax(latestQueries, isSpeedup);
  queryChart.options.scales.y.title.text = isSpeedup ? 'Speedup ×' : 'Runtime (ms)';
  queryChart.update();
}
