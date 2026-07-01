'use strict';

// Main timeline chart: tokens / LOC / cumulative speedup over turn or wall-clock,
// section background fills, time-travel cursor, and click-and-drag scrubbing.

function parseTimelineStep(step, idx) {
  const numeric = Number(step);
  return Number.isFinite(numeric) ? numeric : idx + 1;
}

function getTimelineValue(step, row, idx, mode) {
  if (mode === 'time') {
    const runtime = Number(row['total/runtime']);
    if (Number.isFinite(runtime)) return runtime;
  }
  return parseTimelineStep(step, idx);
}

function getTimelinePoints(steps, data, mode = timelineChartMode) {
  return steps.map((step, idx) => {
    const row = data[step] || {};
    const time = Number(row['total/runtime']);
    return {
      idx,
      step,
      turn: parseTimelineStep(step, idx),
      time: Number.isFinite(time) ? time : null,
      x:    getTimelineValue(step, row, idx, mode),
    };
  });
}

// ── Time-travel: pin per-query view to a past turn (null = live) ─────────
function setTimeTravelStep(step) {
  timeTravelStep = step;
  const ind = document.getElementById('timetravel-indicator');
  const btn = document.getElementById('timetravel-live-btn');
  if (ind) { ind.textContent = step != null ? 'Turn ' + step : ''; ind.hidden = step == null; }
  if (btn) btn.hidden = step == null;
  updateQueryChart(_lastSteps, _lastData);
  if (chart) chart.draw();
}

// ── Plugins ──────────────────────────────────────────────────────────────
const correctnessAlignPlugin = {
  id:'correctnessAlign',
  afterLayout(chart) {
    if (chart.canvas?.id !== 'tc') return;
    if (typeof layoutCorrectnessWithChart === 'function') {
      layoutCorrectnessWithChart(chart);
    }
  },
};

const timeTravelLinePlugin = {
  id: 'timeTravelLine',
  afterDraw(chart) {
    if (chart.canvas?.id !== 'tc') return;
    if (timeTravelStep == null) return;
    const {ctx, chartArea: ca, scales} = chart;
    if (!ca || !scales.x) return;
    const points = chart._timelinePoints ?? [];
    const pt = points.find(p => String(p.step) === String(timeTravelStep));
    if (!pt) return;
    const x = scales.x.getPixelForValue(pt.x);
    if (x < ca.left - 1 || x > ca.right + 1) return;
    ctx.save();
    ctx.strokeStyle = 'rgba(255,220,60,0.9)';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x, ca.top);
    ctx.lineTo(x, ca.bottom);
    ctx.stroke();
    ctx.fillStyle = 'rgba(255,220,60,0.9)';
    ctx.font = 'bold 10px system-ui,sans-serif';
    ctx.fillText('T' + timeTravelStep, x + 3, ca.top + 12);
    ctx.restore();
  },
};

Chart.register(correctnessAlignPlugin, timeTravelLinePlugin);

// A speedup segment is "complete" (drawn solid) only when both of its endpoints
// cover every benchmark query. Segments touching a preliminary point are dashed.
function isCompleteSpeedupSegment(seg) {
  const ds = chart?.data?.datasets?.[seg.datasetIndex]?.data ?? [];
  const a = ds[seg.p0DataIndex];
  const b = ds[seg.p1DataIndex];
  return !!(a && b && a.complete && b.complete);
}

// ── Init / update ────────────────────────────────────────────────────────
function initChart() {
  const ctx = document.getElementById('tc').getContext('2d');
  chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        { // 0 — Input Tokens (left axis)
          label: 'Input Tokens',
          yAxisID: 'yL',
          data: [],
          borderColor: '#8696b5',
          backgroundColor: 'rgba(134,150,181,0.07)',
          pointRadius: 2, pointHoverRadius: 5,
          tension: 0.25, fill: false, order: 3, spanGaps: true,
        },
        { // 1 — Code Size / LOC (right axis 1, dashed)
          label: 'Code Size (LOC)',
          yAxisID: 'yR1',
          data: [],
          borderColor: '#f97316',
          borderDash: [5, 3],
          backgroundColor: 'rgba(249,115,22,0.07)',
          pointRadius: 2, pointHoverRadius: 5,
          tension: 0.25, fill: false, order: 2, spanGaps: true,
        },
        { // 2 — Speedup (right axis 2). Dashed while preliminary (not every
          //     benchmark query implemented yet), solid once it covers them all.
          label: 'Speedup ×DuckDB',
          yAxisID: 'yR2',
          data: [],
          borderColor: '#3b6ef5',
          backgroundColor: 'rgba(59,110,245,0.15)',
          pointRadius: 3, pointHoverRadius: 6,
          tension: 0.3, fill: false, order: 1, spanGaps: true,
          segment: {
            // A segment is solid only when both endpoints include all queries;
            // any segment touching a preliminary point is dashed.
            borderDash: seg => isCompleteSpeedupSegment(seg) ? undefined : [6, 4],
          },
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: {mode:'index', intersect:false},
      onHover(event, elements) {
        const idx = elements.length ? elements[0].index : null;
        if (idx == null) { setHoveredSection(null, null, null); return; }
        const sections = chart.options.plugins.sectionBg?.sections ?? [];
        const points   = chart._timelinePoints ?? [];
        const sec = sections.find(s => idx >= s.startIdx && idx <= s.endIdx);
        if (!sec) { setHoveredSection(null, null, null); return; }
        const first = points[sec.startIdx]?.step;
        const last  = points[sec.endIdx]?.step;
        setHoveredSection(sec.desc, first != null ? +first : null, last != null ? +last : null);
      },
      plugins: {
        legend: {
          position:'top', align:'end',
          labels:{
            color:'#e5eefc', padding:14, font:{size:11},
            // Draw each entry as a short line segment (matching the series'
            // colour and dash pattern) instead of a filled rectangle.
            usePointStyle:true, pointStyle:'line', boxWidth:22, boxHeight:8,
            pointStyleWidth:22,
            // Thicken just the legend markers (leaves the chart lines untouched).
            generateLabels(chart) {
              const items = Chart.defaults.plugins.legend.labels.generateLabels(chart);
              items.forEach(it => { it.lineWidth = 3; });
              return items;
            },
          },
        },
        tooltip: {
          backgroundColor:'#101e34', titleColor:'#e5eefc',
          bodyColor:'#93a9c8', borderColor:'#233149', borderWidth:1,
          callbacks: {
            title: items => {
              const raw = items[0].raw || {};
              const turnLabel = 'Turn ' + (raw.step ?? items[0].dataIndex);
              if (timelineChartMode === 'time') {
                const timeLabel = raw.time != null ? fmtTimelineTick(raw.time) : null;
                return timeLabel ? `${timeLabel} · ${turnLabel}` : turnLabel;
              }
              return turnLabel;
            },
            label: item => {
              const v = item.parsed.y;
              if (v == null) return null;
              if (item.datasetIndex === 2) {
                const raw = item.raw || {};
                let s = ' Speedup: ' + v.toFixed(2) + '×';
                if (!raw.complete) {
                  // Preliminary: only some benchmark queries implemented so far.
                  s += raw.total != null && raw.nQueries != null
                    ? ` (preliminary — ${raw.nQueries}/${raw.total} queries)`
                    : ' (preliminary)';
                }
                return s;
              }
              return ' ' + item.dataset.label + ': ' + v.toLocaleString();
            },
          },
        },
        sectionBg: {sections:[]},
      },
      scales: {
        x: {
          type: 'linear',
          ticks:{color:'#93a9c8', maxTicksLimit:25, font:{size:10}},
          grid:{color:'rgba(36,49,73,0.8)'},
          title:{display:true, text:'Turn', color:'#93a9c8', font:{size:10}},
        },
        yL: {
          position:'left', min:0,
          grace: '12%',
          ticks:{color:'#8696b5', font:{size:10}},
          grid:{color:'rgba(36,49,73,0.8)'},
          title:{display:true, text:'Input Tokens', color:'#8696b5', font:{size:10}},
        },
        yR1: {
          position:'right', min:0,
          grace: '12%',
          ticks:{color:'#f97316', font:{size:10}},
          grid:{drawOnChartArea:false},
          title:{display:true, text:'Code Size (LOC)', color:'#f97316', font:{size:10}},
        },
        yR2: {
          position:'right', display:false, min:0,
          beginAtZero: true,
          grace: '12%',
          afterDataLimits: axis => { addAxisHeadroom(axis); },
          ticks:{color:'#3b6ef5', font:{size:10}},
          grid:{drawOnChartArea:false},
          title:{display:true, text:'Speedup ×', color:'#3b6ef5', font:{size:10}},
        },
      },
    },
  });
  document.getElementById('tc').addEventListener('mouseleave', () => {
    setHoveredSection(null, null, null);
  });

  _wireTimelineDrag();
}

// Click + drag on the timeline to set the time-travel turn. A click without
// drag also opens and scrolls to the matching log entry.
function _wireTimelineDrag() {
  const tcCanvas = document.getElementById('tc');
  let dragActive = false, dragStartX = 0, dragMoved = false;

  function nearestPoint(canvasX) {
    const points = chart._timelinePoints ?? [];
    const xScale = chart.scales?.x;
    const ca = chart.chartArea;
    if (!xScale || !ca || !points.length) return null;
    const clamped = Math.max(ca.left, Math.min(ca.right, canvasX));
    const val = xScale.getValueForPixel(clamped);
    let best = null, bestDist = Infinity;
    for (const pt of points) {
      const d = Math.abs(pt.x - val);
      if (d < bestDist) { bestDist = d; best = pt; }
    }
    return best;
  }

  tcCanvas.addEventListener('mousedown', e => {
    const rect = tcCanvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const ca = chart.chartArea;
    if (!ca || x < ca.left || x > ca.right) return;
    dragActive = true;
    dragStartX = e.clientX;
    dragMoved = false;
    const pt = nearestPoint(x);
    if (pt) setTimeTravelStep(pt.step);
  });

  window.addEventListener('mousemove', e => {
    if (!dragActive) return;
    if (Math.abs(e.clientX - dragStartX) > 4) dragMoved = true;
    const rect = tcCanvas.getBoundingClientRect();
    const pt = nearestPoint(e.clientX - rect.left);
    if (pt) setTimeTravelStep(pt.step);
  });

  window.addEventListener('mouseup', () => {
    if (!dragActive) return;
    const wasDrag = dragMoved;
    dragActive = false;
    if (!wasDrag && timeTravelStep != null) {
      const entry = document.querySelector(`details.log-entry[data-step="${timeTravelStep}"]`);
      if (entry) {
        entry.open = true;
        entry.scrollIntoView({behavior:'smooth', block:'center'});
        entry.classList.add('log-flash');
        setTimeout(() => entry.classList.remove('log-flash'), 1200);
      }
    }
  });
}

function updateChart(steps, data) {
  if (!chart) return;
  const points   = getTimelinePoints(steps, data);
  const tokens   = steps.map(s => (data[s] || {})['input_tokens'] ?? null);
  const loc      = steps.map(s => (data[s] || {})['code/loc']     ?? null);
  const speedup  = computeSpeedupSeries(steps, data);
  const sections = getSections(steps, data);
  const hasSpeedup = speedup.some(s => s.value != null);

  chart._timelinePoints = points;
  chart.data.labels           = steps.map(String);
  chart.data.datasets[0].data = points.map((p, i) => ({x: p.x, y: tokens[i],  step: p.step, time: p.time}));
  chart.data.datasets[1].data = points.map((p, i) => ({x: p.x, y: loc[i],     step: p.step, time: p.time}));
  chart.data.datasets[2].data = points.map((p, i) => ({
    x: p.x, y: speedup[i].value, step: p.step, time: p.time,
    complete: speedup[i].complete, nQueries: speedup[i].nQueries, total: speedup[i].total,
  }));
  chart.options.plugins.sectionBg.sections = sections;
  chart.options.plugins.sectionBg.points   = points;
  chart.options.scales.yR2.display         = hasSpeedup;
  chart.options.scales.yR2.min             = 0;
  chart.options.scales.x.title.text        = timelineChartMode === 'time' ? 'Elapsed Time' : 'Turn';
  chart.options.scales.x.min               = points.length ? points[0].x : 0;
  chart.options.scales.x.max               = points.length ? points[points.length - 1].x : 1;
  chart.options.scales.x.ticks.callback    = timelineChartMode === 'time'
    ? value => fmtTimelineTick(value)
    : value => Number.isInteger(Number(value)) ? String(value) : '';
  chart.update();
}
