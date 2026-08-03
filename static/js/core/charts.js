/* SVG chart primitives.
 *
 * Hand-rolled rather than pulled from a library, for the same reason the server
 * has no pip dependencies: there is no build step here, and a wall display that
 * won't render because a CDN is unreachable is worse than a few hundred lines of
 * path arithmetic.
 *
 * Three rules everything here follows:
 *
 * 1. **Colour comes from CSS custom properties**, never literals. A chart drawn
 *    with `var(--c-3)` re-themes itself the instant the palette changes, with no
 *    redraw and no JS listening for it.
 * 2. **Geometry is viewBox-relative.** Widgets are resized by dragging on a
 *    grid; charts that measure pixels need a ResizeObserver and still lag a
 *    frame behind. A viewBox scales for free.
 * 3. **Touch targets are separate from ink.** A 1px line is unhittable with a
 *    finger, so interactive charts lay invisible wide hit strips over the top.
 */

const NS = 'http://www.w3.org/2000/svg';

/** Series colours, in the order they look best together. */
export const SERIES = ['--c-1', '--c-2', '--c-3', '--c-4', '--c-5', '--c-6'];

export function seriesColor(i) {
  return `var(${SERIES[i % SERIES.length]})`;
}

function n(name, attrs = {}, children = []) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    node.setAttribute(k, String(v));
  }
  for (const c of [].concat(children)) if (c) node.append(c);
  return node;
}

function svgRoot(w, h, cls, { stretch = false } = {}) {
  const s = n('svg', {
    viewBox: `0 0 ${w} ${h}`,
    class: cls,
    // Charts with axis text must keep their aspect ratio or the labels shear;
    // sparklines are pure shape and look better filling the box.
    preserveAspectRatio: stretch ? 'none' : 'xMidYMid meet',
  });
  return s;
}

/** Catmull-Rom → cubic bezier. Financial series look wrong with hard corners. */
function smoothPath(pts, tension = 0.5) {
  if (pts.length < 2) return '';
  if (pts.length === 2) return `M${pts[0][0]},${pts[0][1]} L${pts[1][0]},${pts[1][1]}`;
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[i + 2] || p2;
    const c1x = p1[0] + ((p2[0] - p0[0]) / 6) * tension;
    const c1y = p1[1] + ((p2[1] - p0[1]) / 6) * tension;
    const c2x = p2[0] - ((p3[0] - p1[0]) / 6) * tension;
    const c2y = p2[1] - ((p3[1] - p1[1]) / 6) * tension;
    d += ` C${c1x.toFixed(2)},${c1y.toFixed(2)} ${c2x.toFixed(2)},${c2y.toFixed(2)} ${p2[0].toFixed(2)},${p2[1].toFixed(2)}`;
  }
  return d;
}

/** A "nice" axis step — 1/2/5 × 10^k, so gridlines land on readable numbers. */
function niceStep(span, targetTicks = 4) {
  if (!(span > 0)) return 1;
  const raw = span / targetTicks;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1;
  return step * mag;
}

export function compactMoney(v) {
  const a = Math.abs(v);
  const sign = v < 0 ? '−' : '';
  if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(a >= 1e10 ? 0 : 1)}B`;
  if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(a >= 1e7 ? 0 : 1)}M`;
  if (a >= 1e3) return `${sign}$${(a / 1e3).toFixed(a >= 1e4 ? 0 : 1)}k`;
  return `${sign}$${Math.round(a)}`;
}

/* ------------------------------------------------------------------ area */

/**
 * Area/line chart for one or more series over a shared x axis.
 *
 * opts.series: [{ key, label, values:[n], color, fill? }]
 * opts.labels: x tick labels, same length as values
 * opts.onHover(index|null) — called as a finger moves across the plot
 */
export function areaChart(opts) {
  const {
    series = [], labels = [], width = 320, height = 140,
    yTicks = 4, showAxis = true, smooth = true, baseline = null,
    onHover = null, format = compactMoney,
  } = opts;

  const live = series.filter(s => (s.values || []).some(v => Number.isFinite(v)));
  const svg = svgRoot(width, height, 'chart chart-area');
  if (!live.length) return svg;

  const padL = showAxis ? 34 : 2;
  const padR = 2;
  const padT = 6;
  const padB = showAxis && labels.length ? 16 : 4;
  const pw = width - padL - padR;
  const ph = height - padT - padB;

  const all = live.flatMap(s => s.values).filter(Number.isFinite);
  let lo = Math.min(...all);
  let hi = Math.max(...all);
  // Always show the zero line for money: a net worth chart that crops the axis
  // exaggerates every wobble into a cliff.
  if (baseline !== null) { lo = Math.min(lo, baseline); hi = Math.max(hi, baseline); }
  if (lo === hi) { lo -= 1; hi += 1; }
  const step = niceStep(hi - lo, yTicks);
  lo = Math.floor(lo / step) * step;
  hi = Math.ceil(hi / step) * step;

  const len = Math.max(...live.map(s => s.values.length));
  const X = i => padL + (len === 1 ? pw / 2 : (i / (len - 1)) * pw);
  const Y = v => padT + ph - ((v - lo) / (hi - lo)) * ph;

  // gridlines + y labels
  if (showAxis) {
    for (let v = lo; v <= hi + 1e-9; v += step) {
      const y = Y(v);
      svg.append(n('line', {
        x1: padL, x2: width - padR, y1: y.toFixed(2), y2: y.toFixed(2),
        class: Math.abs(v) < 1e-9 ? 'chart-grid chart-zero' : 'chart-grid',
      }));
      svg.append(n('text', {
        x: padL - 4, y: (y + 3).toFixed(2), class: 'chart-tick', 'text-anchor': 'end',
      }, document.createTextNode(format(v))));
    }
  }

  live.forEach((s, si) => {
    const color = s.color || seriesColor(si);
    const pts = s.values.map((v, i) => [X(i), Y(Number.isFinite(v) ? v : lo)]);
    const d = smooth ? smoothPath(pts) : `M${pts.map(p => `${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(' L')}`;
    if (s.fill !== false) {
      const gid = `g${Math.abs(hashish(s.key || String(si)))}`;
      const grad = n('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 }, [
        n('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': 0.28 }),
        n('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': 0 }),
      ]);
      svg.append(n('defs', {}, [grad]));
      svg.append(n('path', {
        d: `${d} L${X(len - 1).toFixed(2)},${(padT + ph).toFixed(2)} L${X(0).toFixed(2)},${(padT + ph).toFixed(2)} Z`,
        fill: `url(#${gid})`, stroke: 'none',
      }));
    }
    svg.append(n('path', { d, fill: 'none', stroke: color, class: 'chart-line' }));
  });

  // x labels — thinned so they never collide
  if (showAxis && labels.length) {
    const every = Math.ceil(labels.length / Math.max(2, Math.floor(pw / 42)));
    labels.forEach((t, i) => {
      if (i % every !== 0 && i !== labels.length - 1) return;
      svg.append(n('text', {
        x: X(i).toFixed(2), y: height - 4, class: 'chart-tick',
        'text-anchor': i === 0 ? 'start' : i === labels.length - 1 ? 'end' : 'middle',
      }, document.createTextNode(t)));
    });
  }

  if (onHover) {
    const cursor = n('line', { class: 'chart-cursor', y1: padT, y2: padT + ph, x1: 0, x2: 0, opacity: 0 });
    const dot = n('circle', { class: 'chart-dot', r: 3.5, cx: 0, cy: 0, opacity: 0 });
    svg.append(cursor, dot);
    const hit = n('rect', { x: padL, y: padT, width: pw, height: ph, fill: 'transparent' });
    svg.append(hit);

    const at = (clientX) => {
      const box = svg.getBoundingClientRect();
      if (!box.width) return null;
      const rel = ((clientX - box.left) / box.width) * width;
      const i = Math.round(((rel - padL) / pw) * (len - 1));
      return Math.max(0, Math.min(len - 1, i));
    };
    const show = (i) => {
      if (i === null) {
        cursor.setAttribute('opacity', 0); dot.setAttribute('opacity', 0);
        onHover(null); return;
      }
      const s0 = live[0];
      cursor.setAttribute('x1', X(i)); cursor.setAttribute('x2', X(i));
      cursor.setAttribute('opacity', 1);
      const v = s0.values[i];
      if (Number.isFinite(v)) {
        dot.setAttribute('cx', X(i)); dot.setAttribute('cy', Y(v));
        dot.setAttribute('stroke', s0.color || seriesColor(0));
        dot.setAttribute('opacity', 1);
      }
      onHover(i);
    };
    // Pointer events, not touch: one code path covers finger and mouse, and
    // capture keeps the readout alive if the finger slides off the plot.
    hit.addEventListener('pointerdown', (e) => {
      hit.setPointerCapture(e.pointerId);
      show(at(e.clientX));
      e.stopPropagation();          // don't trip the widget's privacy toggle
    });
    hit.addEventListener('pointermove', (e) => {
      if (!hit.hasPointerCapture(e.pointerId)) return;
      show(at(e.clientX));
    });
    const end = () => show(null);
    hit.addEventListener('pointerup', end);
    hit.addEventListener('pointercancel', end);
  }

  return svg;
}

function hashish(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return h;
}

/* ------------------------------------------------------------------- bars */

/**
 * Vertical bars, optionally paired (money in vs money out).
 * opts.groups: [{ label, values:[n], colors:[css] }]
 */
export function barChart(opts) {
  const {
    groups = [], width = 320, height = 140, showAxis = true,
    format = compactMoney, onHover = null, signed = false,
  } = opts;

  const svg = svgRoot(width, height, 'chart chart-bars');
  if (!groups.length) return svg;

  const padL = showAxis ? 34 : 2;
  const padR = 2, padT = 6, padB = 16;
  const pw = width - padL - padR;
  const ph = height - padT - padB;

  const all = groups.flatMap(g => g.values).filter(Number.isFinite);
  let hi = Math.max(0, ...all);
  let lo = signed ? Math.min(0, ...all) : 0;
  if (hi === lo) hi = lo + 1;
  const step = niceStep(hi - lo, 3);
  hi = Math.ceil(hi / step) * step;
  lo = Math.floor(lo / step) * step;
  const Y = v => padT + ph - ((v - lo) / (hi - lo)) * ph;

  if (showAxis) {
    for (let v = lo; v <= hi + 1e-9; v += step) {
      const y = Y(v);
      svg.append(n('line', {
        x1: padL, x2: width - padR, y1: y.toFixed(2), y2: y.toFixed(2),
        class: Math.abs(v) < 1e-9 ? 'chart-grid chart-zero' : 'chart-grid',
      }));
      svg.append(n('text', {
        x: padL - 4, y: (y + 3).toFixed(2), class: 'chart-tick', 'text-anchor': 'end',
      }, document.createTextNode(format(v))));
    }
  }

  const slot = pw / groups.length;
  const per = Math.max(1, groups[0].values.length);
  const gap = Math.min(4, slot * 0.14);
  const bw = Math.max(2, (slot - gap * 2) / per - 1.5);

  groups.forEach((g, gi) => {
    g.values.forEach((v, vi) => {
      // Skip zero outright. The minimum height below keeps a small-but-real
      // amount visible, and applying it to zero would paint a 1px sliver on the
      // baseline that reads as a rendering artefact rather than "no activity".
      if (!Number.isFinite(v) || v === 0) return;
      const x = padL + gi * slot + gap + vi * (bw + 1.5);
      const y0 = Y(0), y1 = Y(v);
      svg.append(n('rect', {
        x: x.toFixed(2), y: Math.min(y0, y1).toFixed(2),
        width: bw.toFixed(2), height: Math.max(1, Math.abs(y1 - y0)).toFixed(2),
        rx: Math.min(2.5, bw / 2), fill: (g.colors || [])[vi] || seriesColor(vi),
        class: 'chart-bar',
      }));
    });
    svg.append(n('text', {
      x: (padL + gi * slot + slot / 2).toFixed(2), y: height - 4,
      class: 'chart-tick', 'text-anchor': 'middle',
    }, document.createTextNode(g.label)));
  });

  if (onHover) {
    groups.forEach((g, gi) => {
      const hit = n('rect', {
        x: (padL + gi * slot).toFixed(2), y: padT,
        width: slot.toFixed(2), height: ph, fill: 'transparent',
      });
      hit.addEventListener('pointerdown', (e) => { onHover(gi); e.stopPropagation(); });
      svg.append(hit);
    });
  }
  return svg;
}

/* ------------------------------------------------------------------ donut */

/**
 * Donut with a free centre for a total.
 * opts.slices: [{ label, value, color }]
 */
export function donut(opts) {
  const { slices = [], size = 120, thickness = 16, onHover = null, gap = 0.018 } = opts;
  const svg = svgRoot(size, size, 'chart chart-donut');
  const total = slices.reduce((a, s) => a + Math.max(0, s.value || 0), 0);
  const r = (size - thickness) / 2;
  const cx = size / 2, cy = size / 2;

  if (total <= 0) {
    svg.append(n('circle', { cx, cy, r, class: 'chart-donut-empty', fill: 'none', 'stroke-width': thickness }));
    return svg;
  }

  let a0 = -Math.PI / 2;
  slices.forEach((s, i) => {
    const frac = Math.max(0, s.value || 0) / total;
    if (frac <= 0) return;
    const a1 = a0 + frac * Math.PI * 2;
    // A ring segment is just a stroked arc — no wedge maths, no fill rules.
    const pad = slices.length > 1 ? gap : 0;
    const s0 = a0 + pad, s1 = Math.max(a0 + pad, a1 - pad);
    const x0 = cx + r * Math.cos(s0), y0 = cy + r * Math.sin(s0);
    const x1 = cx + r * Math.cos(s1), y1 = cy + r * Math.sin(s1);
    const large = (s1 - s0) > Math.PI ? 1 : 0;
    const path = n('path', {
      d: `M${x0.toFixed(2)},${y0.toFixed(2)} A${r},${r} 0 ${large} 1 ${x1.toFixed(2)},${y1.toFixed(2)}`,
      fill: 'none', stroke: s.color || seriesColor(i),
      'stroke-width': thickness, 'stroke-linecap': 'butt', class: 'chart-arc',
    });
    if (onHover) {
      path.addEventListener('pointerdown', (e) => { onHover(i); e.stopPropagation(); });
    }
    svg.append(path);
    a0 = a1;
  });
  return svg;
}

/* ------------------------------------------------------------------ gauge */

/** 270° arc gauge — credit utilisation, savings rate, progress to a goal. */
export function arcGauge(opts) {
  const {
    value = 0, max = 100, size = 110, thickness = 11,
    color = 'var(--primary)', track = true, sweep = 1.5 * Math.PI,
  } = opts;
  const svg = svgRoot(size, size, 'chart chart-gauge');
  const r = (size - thickness) / 2;
  const cx = size / 2, cy = size / 2;
  const start = Math.PI * 0.75;
  const frac = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;

  const arc = (from, to, cls, stroke) => {
    if (to - from < 1e-4) return null;
    const x0 = cx + r * Math.cos(from), y0 = cy + r * Math.sin(from);
    const x1 = cx + r * Math.cos(to), y1 = cy + r * Math.sin(to);
    return n('path', {
      d: `M${x0.toFixed(2)},${y0.toFixed(2)} A${r},${r} 0 ${(to - from) > Math.PI ? 1 : 0} 1 ${x1.toFixed(2)},${y1.toFixed(2)}`,
      fill: 'none', stroke, 'stroke-width': thickness, 'stroke-linecap': 'round', class: cls,
    });
  };
  if (track) svg.append(arc(start, start + sweep, 'chart-gauge-track', 'var(--line)'));
  const v = arc(start, start + sweep * frac, 'chart-gauge-value', color);
  if (v) svg.append(v);
  return svg;
}

/* -------------------------------------------------------------- sparkline */

/** Tiny trend line, no axes — for account rows and stat cards. */
export function sparkline(values, opts = {}) {
  const { width = 100, height = 28, color = 'var(--primary)', fill = true } = opts;
  const vals = (values || []).filter(Number.isFinite);
  const svg = svgRoot(width, height, 'chart chart-spark', { stretch: true });
  if (vals.length < 2) return svg;
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = (hi - lo) || 1;
  const pts = vals.map((v, i) => [
    (i / (vals.length - 1)) * width,
    height - 1 - ((v - lo) / span) * (height - 2),
  ]);
  const d = smoothPath(pts, 0.4);
  if (fill) {
    svg.append(n('path', {
      d: `${d} L${width},${height} L0,${height} Z`,
      fill: color, opacity: 0.14, stroke: 'none',
    }));
  }
  svg.append(n('path', { d, fill: 'none', stroke: color, class: 'chart-line' }));
  return svg;
}
