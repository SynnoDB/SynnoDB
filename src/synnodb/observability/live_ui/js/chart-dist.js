'use strict';

// Wall-clock distribution charts shown inside the "Time Distribution" modal:
//   - Pie of total seconds per activity type
//   - Stacked-area of cumulative seconds (or % share) over turns
//   - Bar of call counts per activity type
//
// All three pull from the same per-step `total/runtime` deltas (treating each
// step's delta as belonging to that step's `type`).

const PIE_TYPE_ORDER  = ['llm','compile','shell','validate','apply_patch','compaction','other'];
const PIE_TYPE_LABELS = {llm:'LLM', compile:'Compile', shell:'Shell',
                         validate:'Validate', apply_patch:'Patch',
                         compaction:'Compaction', other:'Other'};
const PIE_TYPE_COLORS = {llm:'#6fa8ff', compile:'#a78bfa', shell:'#4ade80',
                         validate:'#e6d96a', apply_patch:'#fb923c',
                         compaction:'#94a3b8', other:'#64748b'};

// ── Pie chart ────────────────────────────────────────────────────────────
function computeTimePieData(steps, data) {
  const totals = {};
  let prevRuntime = null;
  for (const step of steps) {
    const row = data[step] || {};
    const type = (row.type || 'other').toLowerCase();
    const runtime = Number(row['total/runtime']);
    if (!Number.isFinite(runtime)) { prevRuntime = null; continue; }
    const duration = prevRuntime != null ? runtime - prevRuntime : 0;
    prevRuntime = runtime;
    if (duration <= 0) continue;
    const key = PIE_TYPE_ORDER.includes(type) ? type : 'other';
    totals[key] = (totals[key] || 0) + duration;
  }
  const labels = [], values = [], colors = [];
  for (const k of PIE_TYPE_ORDER) {
    if (!totals[k]) continue;
    labels.push(PIE_TYPE_LABELS[k]);
    values.push(totals[k]);
    colors.push(PIE_TYPE_COLORS[k]);
  }
  return {labels, values, colors};
}

function initPieChart() {
  const ctx = document.getElementById('pc').getContext('2d');
  pieChart = new Chart(ctx, {
    type: 'doughnut',
    data: {labels: [], datasets: [{data: [], backgroundColor: [], borderColor: 'rgba(0,0,0,0)', hoverOffset: 8}]},
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: {
          position: 'right',
          labels: {color:'#e5eefc', boxWidth:14, padding:14, font:{size:12}},
        },
        tooltip: {
          backgroundColor:'#101e34', titleColor:'#e5eefc',
          bodyColor:'#93a9c8', borderColor:'#233149', borderWidth:1,
          callbacks: {
            label: item => {
              const total = item.dataset.data.reduce((a,b) => a+b, 0);
              const pct = total > 0 ? (item.parsed / total * 100).toFixed(1) : '0';
              return '  ' + fmtPieTime(item.parsed) + '  (' + pct + '%)';
            },
          },
        },
      },
    },
  });
}

function updatePieChart(steps, data) {
  if (!pieChart) initPieChart();
  const {labels, values, colors} = computeTimePieData(steps, data);
  pieChart.data.labels = labels;
  pieChart.data.datasets[0].data = values;
  pieChart.data.datasets[0].backgroundColor = colors;
  pieChart.update();
}

// ── Timeline distribution (stacked area) ─────────────────────────────────
function computeTimelineStackData(steps, data) {
  const rawCum = {};
  for (const k of PIE_TYPE_ORDER) rawCum[k] = 0;

  let prevRuntime = null;
  const turnLabels = [];
  const perTurnAbs = {}, perTurnRel = {};
  for (const k of PIE_TYPE_ORDER) { perTurnAbs[k] = []; perTurnRel[k] = []; }

  for (const step of steps) {
    const row  = data[step] || {};
    const type = (row.type || 'other').toLowerCase();
    const rt   = Number(row['total/runtime']);
    if (!Number.isFinite(rt)) { prevRuntime = null; continue; }
    const dur  = prevRuntime != null ? Math.max(0, rt - prevRuntime) : 0;
    prevRuntime = rt;

    const key = PIE_TYPE_ORDER.includes(type) ? type : 'other';
    rawCum[key] += dur;
    turnLabels.push(Number(step));

    const total = Object.values(rawCum).reduce((a, b) => a + b, 0);
    for (const k of PIE_TYPE_ORDER) {
      perTurnAbs[k].push(rawCum[k]);
      perTurnRel[k].push(total > 0 ? (rawCum[k] / total) * 100 : 0);
    }
  }

  const activeTypes = PIE_TYPE_ORDER.filter(k => rawCum[k] > 0);
  return {turnLabels, perTurnAbs, perTurnRel, activeTypes};
}

function initTimelineDistChart() {
  const ctx = document.getElementById('tc-dist').getContext('2d');
  timelineDistChart = new Chart(ctx, {
    type: 'line',
    data: {labels: [], datasets: []},
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: {mode: 'index', intersect: false},
      plugins: {
        legend: {
          position: 'top', align: 'end',
          labels: {color: '#e5eefc', boxWidth: 12, padding: 8, font: {size: 10}},
        },
        tooltip: {
          backgroundColor: '#101e34', titleColor: '#e5eefc',
          bodyColor: '#93a9c8', borderColor: '#233149', borderWidth: 1,
          callbacks: {
            title: items => 'Turn ' + items[0].label,
            label: item => {
              const v = item.parsed.y;
              const isRel = timelineDistChart._distMode === 'rel';
              return ' ' + item.dataset.label + ': ' + (isRel ? v.toFixed(1) + '%' : fmtPieTime(v));
            },
          },
        },
      },
      scales: {
        x: {
          ticks: {color: '#93a9c8', font: {size: 10}, maxTicksLimit: 20},
          grid:  {color: 'rgba(36,49,73,0.8)'},
          title: {display: true, text: 'Turn', color: '#93a9c8', font: {size: 10}},
        },
        y: {
          stacked: true, beginAtZero: true,
          ticks: {color: '#93a9c8', font: {size: 10}},
          grid:  {color: 'rgba(36,49,73,0.8)'},
          title: {display: true, text: '', color: '#93a9c8', font: {size: 10}},
        },
      },
    },
  });
}

function updateTimelineDistChart(steps, data, mode) {
  if (!timelineDistChart) initTimelineDistChart();
  timelineDistChart._distMode = mode;

  const {turnLabels, perTurnAbs, perTurnRel, activeTypes} = computeTimelineStackData(steps, data);
  const perTurn = mode === 'rel' ? perTurnRel : perTurnAbs;

  timelineDistChart.data.labels   = turnLabels;
  timelineDistChart.data.datasets = activeTypes.map((k, i) => ({
    label:           PIE_TYPE_LABELS[k],
    data:            perTurn[k],
    backgroundColor: PIE_TYPE_COLORS[k] + '88',
    borderColor:     PIE_TYPE_COLORS[k],
    borderWidth:     1,
    fill:            i === 0 ? 'origin' : '-1',
    tension:         0.3,
    pointRadius:     0,
    pointHoverRadius: 3,
  }));

  const yAxis = timelineDistChart.options.scales.y;
  yAxis.max          = mode === 'rel' ? 100 : undefined;
  yAxis.title.text   = mode === 'rel' ? '% of total time' : 'Cumulative (s)';
  yAxis.ticks.callback = mode === 'rel'
    ? v => v + '%'
    : v => { const s = +v; if (s >= 3600) return Math.round(s/3600)+'h'; if (s >= 60) return Math.round(s/60)+'m'; return Math.round(s)+'s'; };

  timelineDistChart.update();
}

// ── Call-count bar chart ─────────────────────────────────────────────────
function computeCountData(steps, data) {
  const counts = {};
  for (const step of steps) {
    const type = ((data[step] || {}).type || 'other').toLowerCase();
    const key  = PIE_TYPE_ORDER.includes(type) ? type : 'other';
    counts[key] = (counts[key] || 0) + 1;
  }
  const labels = [], values = [], colors = [];
  for (const k of PIE_TYPE_ORDER) {
    if (!counts[k]) continue;
    labels.push(PIE_TYPE_LABELS[k]);
    values.push(counts[k]);
    colors.push(PIE_TYPE_COLORS[k] + 'aa');
  }
  return {labels, values, colors};
}

function initBarChart() {
  const ctx = document.getElementById('bc').getContext('2d');
  barChart = new Chart(ctx, {
    type: 'bar',
    data: {labels: [], datasets: [{data: [], backgroundColor: [], borderWidth: 0}]},
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: {display: false},
        tooltip: {
          backgroundColor:'#101e34', titleColor:'#e5eefc',
          bodyColor:'#93a9c8', borderColor:'#233149', borderWidth:1,
          callbacks: {label: item => '  ' + item.parsed.y + ' calls'},
        },
      },
      scales: {
        x: {ticks:{color:'#93a9c8', font:{size:11}}, grid:{color:'rgba(36,49,73,0.8)'}},
        y: {
          beginAtZero: true,
          ticks:{color:'#93a9c8', font:{size:10}, stepSize: 1,
                 callback: v => Number.isInteger(v) ? v : ''},
          grid:{color:'rgba(36,49,73,0.8)'},
          title:{display:true, text:'# calls', color:'#93a9c8', font:{size:10}},
        },
      },
    },
  });
}

function updateBarChart(steps, data) {
  if (!barChart) initBarChart();
  const {labels, values, colors} = computeCountData(steps, data);
  barChart.data.labels = labels;
  barChart.data.datasets[0].data = values;
  barChart.data.datasets[0].backgroundColor = colors;
  barChart.update();
}
