'use strict';

// Section colours match _SPAN_PALETTE in plot_timeline.py.
const SECT_KEY_ORDER = ['storage','plan','pin','trace','implement','impl',
                         'base','gen','optim','make_mt','add_mt','mt'];
const SECT_HEX = {
  storage:'#4C72B0', plan:'#4C72B0',
  implement:'#55A868', impl:'#55A868', base:'#55A868', gen:'#55A868',
  pin:'#DA8BC3', trace:'#DA8BC3',
  optim:'#DD8452',
  make_mt:'#8172B3', add_mt:'#8172B3', mt:'#8172B3',
  _:'#8C8C8C',
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

// Highlight a section across the timeline chart, sidebar, and log entries.
function setHoveredSection(desc, first, last) {
  if (desc === hoveredDesc) return;
  hoveredDesc = desc;
  if (chart) chart.draw();
  document.querySelectorAll('.pl-item[data-desc]').forEach(item => {
    item.classList.toggle('pl-hovered', item.dataset.desc === desc);
  });
  document.querySelectorAll('details.log-entry').forEach(entry => {
    const step = +entry.dataset.step;
    entry.classList.toggle('log-highlighted',
      desc != null && first != null && step >= first && step <= last);
  });
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
        ctx.fillText(txt, l + 4, ca.top + 13);
      }
    }
    ctx.restore();
  },
};

Chart.register(sectionBgPlugin);
