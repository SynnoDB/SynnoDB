'use strict';

// Section colours match _SPAN_PALETTE in plot_timeline.py.
const SECT_KEY_ORDER = ['storage','plan','pin','trace','implement','impl',
                         'base','gen','optim','make_mt','add_mt','mt'];
const SECT_HEX = {
  storage:'#3b6ef5', plan:'#3b6ef5',
  implement:'#22c55e', impl:'#22c55e', base:'#22c55e', gen:'#22c55e',
  pin:'#c084fc', trace:'#c084fc',
  optim:'#fb923c',
  make_mt:'#6d3bf0', add_mt:'#6d3bf0', mt:'#6d3bf0',
  _:'#64748b',
};

function sectKey(desc) {
  if (!desc) return '_';
  const d = desc.toLowerCase();
  for (const k of SECT_KEY_ORDER) { if (d.includes(k)) return k; }
  return '_';
}

function sectRgba(desc, alpha) {
  const hex = SECT_HEX[sectKey(desc)] || SECT_HEX._;
  const r = parseInt(hex.slice(1,3),16),
        g = parseInt(hex.slice(3,5),16),
        b = parseInt(hex.slice(5,7),16);
  return `rgba(${r},${g},${b},${alpha})`;
}

// Walk steps into runs of the same current_prompt_descriptor.
function getSections(steps, data) {
  const sections = [];
  let cur = null;
  steps.forEach((s, i) => {
    const desc = (data[s] || {}).current_prompt_descriptor || null;
    if (desc !== null) {
      if (!cur || cur.desc !== desc) {
        if (cur) sections.push(cur);
        cur = {desc, startIdx:i, endIdx:i};
      } else {
        cur.endIdx = i;
      }
    } else if (cur) {
      cur.endIdx = i;
    }
  });
  if (cur) sections.push(cur);
  return sections;
}

// ── Stage spans (coarse pipeline stage, from the per-row `stage` field) ──
// Every metric row is stamped with its Stage.name (see RunStatsCollector), so we
// can annotate which pipeline stage each stretch of the timeline belongs to
// without reverse-engineering it from prompt descriptors. Height of the labelled
// ribbon drawn at the top of the chart area; also shifts the finer-grained
// section labels down so the two don't overlap.
const STAGE_BAND_H = 18;

// Friendly label + colour per Stage.name. Colours echo the section palette so a
// stage and the descriptors nested under it read as the same family. Unknown
// stages fall back to the raw name and the neutral slate.
const STAGE_META = {
  createStoragePlan:  {label:'Storage Plan', hex:'#3b6ef5'},
  createBaseImpl:     {label:'Base Impl',    hex:'#22c55e'},
  runOptimLoop:       {label:'Optimize',     hex:'#fb923c'},
  addMultiThreading:  {label:'Multithread',  hex:'#6d3bf0'},
  checkSfCorrectness: {label:'Check SF',     hex:'#64748b'},
};

function stageMeta(name) {
  return STAGE_META[name] || {label:name || '', hex:'#64748b'};
}

function hexRgba(hex, alpha) {
  const r = parseInt(hex.slice(1,3),16),
        g = parseInt(hex.slice(3,5),16),
        b = parseInt(hex.slice(5,7),16);
  return `rgba(${r},${g},${b},${alpha})`;
}

// Walk steps into runs of the same `stage` field (null extends the current run,
// mirroring getSections) so consecutive rows of one stage collapse into a span.
function getStageSpans(steps, data) {
  const spans = [];
  let cur = null;
  steps.forEach((s, i) => {
    const stage = (data[s] || {}).stage || null;
    if (stage !== null) {
      if (!cur || cur.stage !== stage) {
        if (cur) spans.push(cur);
        cur = {stage, startIdx:i, endIdx:i};
      } else {
        cur.endIdx = i;
      }
    } else if (cur) {
      cur.endIdx = i;
    }
  });
  if (cur) spans.push(cur);
  return spans;
}

// Highlight a section across the timeline chart, sidebar, and log entries.
function setHoveredSection(desc, first, last) {
  if (desc === hoveredDesc) return;
  hoveredDesc = desc;
  if (chart) chart.draw();
  document.querySelectorAll('.pl-item[data-desc]').forEach(item => {
    item.classList.toggle('pl-hovered', item.dataset.desc === desc);
  });
  // The log is virtualized — only mounted rows exist in the DOM — so hand the
  // highlight range to log.js, which also applies it to rows as they mount.
  applyLogHover(first, last, desc != null && first != null);
}

// ── Chart.js plugin: section background fills + dividers + labels ────────
const sectionBgPlugin = {
  id:'sectionBg',
  beforeDraw(chart) {
    const {ctx, chartArea:ca, scales} = chart;
    if (!ca || !scales.x) return;
    const secs = chart.options.plugins.sectionBg?.sections ?? [];
    const points = chart.options.plugins.sectionBg?.points ?? [];
    ctx.save();
    for (const sec of secs) {
      const startBounds = getSegmentBounds(points, sec.startIdx);
      const endBounds   = getSegmentBounds(points, sec.endIdx);
      if (!startBounds || !endBounds) continue;
      const x0 = scales.x.getPixelForValue(startBounds.left);
      const x1 = scales.x.getPixelForValue(endBounds.right);
      const l  = Math.max(ca.left,  Math.min(x0, x1));
      const r  = Math.min(ca.right, Math.max(x0, x1));
      if (r <= l) continue;

      const alpha = hoveredDesc && sec.desc === hoveredDesc ? 0.32 : 0.13;
      ctx.fillStyle = sectRgba(sec.desc, alpha);
      ctx.fillRect(l, ca.top, r - l, ca.height);

      if (sec.startIdx > 0 && x0 > ca.left + 2) {
        ctx.strokeStyle = sectRgba(sec.desc, 0.55);
        ctx.lineWidth = 1.5;
        ctx.setLineDash([4, 3]);
        ctx.beginPath(); ctx.moveTo(x0, ca.top); ctx.lineTo(x0, ca.bottom); ctx.stroke();
        ctx.setLineDash([]);
      }

      const label = sec.desc || '';
      if (label && (r - l) > 30) {
        ctx.fillStyle = sectRgba(sec.desc, 0.85);
        ctx.font = 'bold 10px system-ui, sans-serif';
        const txt = label.length > 18 ? label.slice(0,16) + '…' : label;
        // Drop below the stage ribbon (when present) so the two don't collide.
        const hasStages = (chart.options.plugins.stageBand?.spans ?? []).length > 0;
        ctx.fillText(txt, l + 4, ca.top + 13 + (hasStages ? STAGE_BAND_H : 0));
      }
    }
    ctx.restore();
  },
};

// ── Chart.js plugin: stage ribbon + solid boundary dividers ──────────────
// A coarse header strip above the finer section fills: one labelled band per
// pipeline stage plus a solid full-height divider at each stage boundary, so
// it's obvious at a glance which stage any point on the timeline belongs to.
// Resolve a span to its clamped [l, r] pixel bounds within the chart area,
// plus the raw start pixel x0 used for the boundary divider. Returns null when
// the span is off-screen or degenerate.
function stageSpanBounds(chart, span, points) {
  const {chartArea:ca, scales} = chart;
  const startBounds = getSegmentBounds(points, span.startIdx);
  const endBounds   = getSegmentBounds(points, span.endIdx);
  if (!startBounds || !endBounds) return null;
  const x0 = scales.x.getPixelForValue(startBounds.left);
  const x1 = scales.x.getPixelForValue(endBounds.right);
  const l  = Math.max(ca.left,  Math.min(x0, x1));
  const r  = Math.min(ca.right, Math.max(x0, x1));
  if (r <= l) return null;
  return {l, r, x0};
}

const stageBandPlugin = {
  id:'stageBand',
  // Full-height boundary dividers stay behind the grid and data series.
  beforeDraw(chart) {
    const {ctx, chartArea:ca, scales} = chart;
    if (!ca || !scales.x) return;
    const spans  = chart.options.plugins.stageBand?.spans ?? [];
    const points = chart.options.plugins.stageBand?.points ?? [];
    if (!spans.length) return;
    ctx.save();
    for (const span of spans) {
      const b = stageSpanBounds(chart, span, points);
      if (!b) continue;
      // Solid full-height boundary divider at the stage start (more prominent
      // than the dashed per-descriptor dividers), skipping the very first edge.
      if (span.startIdx > 0 && b.x0 > ca.left + 2) {
        const meta = stageMeta(span.stage);
        ctx.strokeStyle = hexRgba(meta.hex, 0.85);
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(b.x0, ca.top); ctx.lineTo(b.x0, ca.bottom); ctx.stroke();
      }
    }
    ctx.restore();
  },
  // The ribbon header + label paint last so they sit on top of the grid lines.
  afterDatasetsDraw(chart) {
    const {ctx, chartArea:ca, scales} = chart;
    if (!ca || !scales.x) return;
    const spans  = chart.options.plugins.stageBand?.spans ?? [];
    const points = chart.options.plugins.stageBand?.points ?? [];
    if (!spans.length) return;
    ctx.save();
    for (const span of spans) {
      const b = stageSpanBounds(chart, span, points);
      if (!b) continue;
      const {l, r} = b;

      const meta = stageMeta(span.stage);
      // Solid ribbon strip across the top of the plot for this stage.
      ctx.fillStyle = hexRgba(meta.hex, 0.9);
      ctx.fillRect(l, ca.top, r - l, STAGE_BAND_H);

      // Centre the stage label within the visible portion of the ribbon.
      if ((r - l) > 24) {
        ctx.fillStyle = '#ffffff';
        ctx.font = 'bold 11px system-ui, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        const txt = meta.label.length > 22 ? meta.label.slice(0,20) + '…' : meta.label;
        ctx.fillText(txt, (l + r) / 2, ca.top + STAGE_BAND_H / 2);
      }
    }
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    ctx.restore();
  },
};

Chart.register(sectionBgPlugin, stageBandPlugin);
