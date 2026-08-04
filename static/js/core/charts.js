/* SVG chart primitives.
 *
 * Hand-rolled rather than pulled from a library, for the same reason the server
 * has no pip dependencies: there is no build step here, and a wall display that
 * won't render because a CDN is unreachable is worse than a few hundred lines of
 * path arithmetic.
 *
 * **These charts render at measured pixel size, not into a fixed viewBox.**
 * That is the whole design. A widget on this grid can be 240×200 or 1900×1080
 * and any aspect ratio in between, and a fixed viewBox scales the drawing as one
 * unit — so the axis labels balloon in a big box, turn to mush in a small one,
 * and the plot letterboxes the moment the box is not the shape the viewBox
 * assumed. Measuring costs a ResizeObserver and a frame; it buys a chart that is
 * legible at every size, which is the point.
 *
 * Consequences worth knowing:
 *   - One SVG user unit == one CSS pixel. Font sizes are real pixels.
 *   - Detail is a function of size: tick counts, label thinning, and whether
 *     there are axes at all are all decided from the measured box.
 *   - Colour is always a CSS custom property, never a literal, so a palette
 *     change re-themes every chart with no redraw.
 *   - Touch targets are separate from ink: a 2px line is unhittable with a
 *     finger, so interactive charts lay invisible hit areas over the top.
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

function svgRoot(w, h, cls) {
  // viewBox matches the measured box exactly, so nothing is scaled and text
  // renders at the size it says it is.
  return n('svg', { viewBox: `0 0 ${w} ${h}`, width: w, height: h, class: cls });
}

/**
 * Render `draw(width, height)` into `host`, re-rendering when its size changes.
 *
 * Returns a teardown function. Resize bursts (a widget being dragged) are
 * coalesced into one render per frame, and identical sizes are skipped, so a
 * drag that does not change the box costs nothing.
 */
export function autoSize(host, draw) {
  let frame = null;
  let last = '';
  const render = () => {
    frame = null;
    const w = Math.floor(host.clientWidth);
    const h = Math.floor(host.clientHeight);
    if (w < 2 || h < 2) return;                 // display:none, or mid-layout
    const key = `${w}x${h}`;
    if (key === last) return;
    last = key;
    while (host.firstChild) host.removeChild(host.firstChild);
    const node = draw(w, h);
    if (node) host.append(node);
  };
  // rAF, so measurement happens after layout rather than inside the observer
  // callback, where clientWidth can still be the pre-resize value.
  const ro = new ResizeObserver(() => {
    if (frame) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(render);
  });
  ro.observe(host);
  render();
  return () => {
    ro.disconnect();
    if (frame) cancelAnimationFrame(frame);
  };
}

/**
 * Report the host's size on change, WITHOUT touching its children.
 *
 * autoSize() clears and redraws, which is right for static charts and fatal for
 * a stateful one: it would delete the node holding pointer capture. Anything
 * that owns its own DOM across resizes wants this instead.
 */
export function observeSize(host, onSize) {
  let frame = null;
  let last = '';
  const measure = () => {
    frame = null;
    const w = Math.floor(host.clientWidth);
    const h = Math.floor(host.clientHeight);
    if (w < 2 || h < 2) return;
    const key = `${w}x${h}`;
    if (key === last) return;
    last = key;
    onSize(w, h);
  };
  const ro = new ResizeObserver(() => {
    if (frame) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(measure);
  });
  ro.observe(host);
  measure();
  return () => { ro.disconnect(); if (frame) cancelAnimationFrame(frame); };
}

/** Force the next autoSize render even if the box has not changed. */
export function invalidate(host) {
  host.__chartKey = '';
}

/* ------------------------------------------------------------------ scale */

/**
 * Catmull-Rom → cubic bezier. Financial series look wrong with hard corners.
 *
 * `bounds` is not optional decoration: Catmull-Rom control points are
 * extrapolated from the NEIGHBOURING points, so a spike makes the curve
 * overshoot past the highest value — and in a tall narrow box that overshoot
 * leaves the plot area entirely and paints over the axis. Clamping each control
 * point to the band keeps the curve inside without flattening it.
 */
function smoothPath(pts, tension = 0.5, bounds = null) {
  if (pts.length < 2) return '';
  if (pts.length === 2) return `M${pts[0][0]},${pts[0][1]} L${pts[1][0]},${pts[1][1]}`;
  const clampY = bounds
    ? (v) => Math.max(bounds.y0, Math.min(bounds.y1, v))
    : (v) => v;
  const clampX = bounds
    ? (v) => Math.max(bounds.x0, Math.min(bounds.x1, v))
    : (v) => v;
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[i + 2] || p2;
    const c1x = clampX(p1[0] + ((p2[0] - p0[0]) / 6) * tension);
    const c1y = clampY(p1[1] + ((p2[1] - p0[1]) / 6) * tension);
    const c2x = clampX(p2[0] - ((p3[0] - p1[0]) / 6) * tension);
    const c2y = clampY(p2[1] - ((p3[1] - p1[1]) / 6) * tension);
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

/**
 * What this box can carry.
 *
 * Every adaptive decision lives here rather than being sprinkled through the
 * chart functions, so "what does a short wide box do" has one answer to read.
 */
export function fit(w, h) {
  // Type scales with the box and is floored at 5px rather than 9px. 5px is not
  // comfortable, and it is not meant to be: the widget's zoom (core/scale.js)
  // is what makes small boxes legible, and this floor exists only so a chart
  // squeezed into a couple of cells still DRAWS its axis instead of dropping
  // it. Rendering something small is the user's call; deciding for them that
  // they get nothing is not.
  const font = Math.max(5, Math.min(15, Math.round(Math.min(w, h) / 16)));
  // Axes appear as soon as there is physically room to put a number and a line
  // in the box. Everything below that is still drawn, just tight.
  const axis = w >= 70 && h >= 42;
  const yTicks = h < 90 ? 2 : h < 130 ? 2 : h < 220 ? 3 : h < 340 ? 4 : 5;
  return {
    font, axis, yTicks,
    dense: w >= 420 && h >= 240,
    tiny: w < 140 || h < 70,
    pad: Math.max(2, Math.round(font * 0.4)),
  };
}

/** Rough text width. No DOM measurement — this runs per label, per frame. */
const textWidth = (s, font) => String(s).length * font * 0.58;

/* ------------------------------------------------------------------ area */

/**
 * Area/line chart for one or more series over a shared x axis.
 * `width`/`height` are CSS pixels, normally from autoSize().
 */
export function areaChart(opts) {
  const {
    series = [], labels = [], width = 320, height = 140,
    showAxis = null, smooth = true, baseline = null,
    onHover = null, format = compactMoney,
  } = opts;

  const F = fit(width, height);
  const live = series.filter(s => (s.values || []).some(v => Number.isFinite(v)));
  const svg = svgRoot(width, height, 'chart chart-area');
  if (!live.length) return svg;

  const axis = showAxis === null ? F.axis : (showAxis && F.axis);

  const all = live.flatMap(s => s.values).filter(Number.isFinite);
  let lo = Math.min(...all);
  let hi = Math.max(...all);
  // Always show the zero line for money: a net worth chart that crops the axis
  // exaggerates every wobble into a cliff.
  if (baseline !== null) { lo = Math.min(lo, baseline); hi = Math.max(hi, baseline); }
  if (lo === hi) { lo -= 1; hi += 1; }
  const step = niceStep(hi - lo, F.yTicks);
  lo = Math.floor(lo / step) * step;
  hi = Math.ceil(hi / step) * step;

  // Gutter sized to the widest label this axis will actually print, so a chart
  // of millions is not clipped and a chart of tens is not padded for nothing.
  let padL = F.pad;
  if (axis) {
    let widest = 0;
    for (let v = lo; v <= hi + 1e-9; v += step) {
      widest = Math.max(widest, textWidth(format(v), F.font));
    }
    padL = Math.min(width * 0.34, widest + F.font * 0.7);
  }
  const padR = F.pad + 1;
  const padT = Math.round(F.font * 0.7);
  const padB = axis && labels.length ? F.font + F.pad + 2 : F.pad;
  const pw = Math.max(1, width - padL - padR);
  const ph = Math.max(1, height - padT - padB);

  const len = Math.max(...live.map(s => s.values.length));
  const X = i => padL + (len === 1 ? pw / 2 : (i / (len - 1)) * pw);
  const Y = v => padT + ph - ((v - lo) / (hi - lo)) * ph;

  if (axis) {
    for (let v = lo; v <= hi + 1e-9; v += step) {
      const y = Y(v);
      svg.append(n('line', {
        x1: padL, x2: width - padR, y1: y.toFixed(2), y2: y.toFixed(2),
        class: Math.abs(v) < 1e-9 ? 'chart-grid chart-zero' : 'chart-grid',
      }));
      svg.append(n('text', {
        x: (padL - F.pad).toFixed(2), y: (y + F.font * 0.34).toFixed(2),
        class: 'chart-tick chart-tick-y', 'text-anchor': 'end', 'font-size': F.font,
      }, document.createTextNode(format(v))));
    }
  }

  live.forEach((s, si) => {
    const color = s.color || seriesColor(si);
    const pts = s.values.map((v, i) => [X(i), Y(Number.isFinite(v) ? v : lo)]);
    const band = { x0: padL, x1: width - padR, y0: padT, y1: padT + ph };
    const d = smooth ? smoothPath(pts, 0.5, band)
                     : `M${pts.map(p => `${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(' L')}`;
    if (s.fill !== false) {
      const gid = `g${Math.abs(hashish((s.key || String(si)) + width + height))}`;
      svg.append(n('defs', {}, [
        n('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 }, [
          n('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': 0.28 }),
          n('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': 0 }),
        ]),
      ]));
      svg.append(n('path', {
        d: `${d} L${X(len - 1).toFixed(2)},${(padT + ph).toFixed(2)} L${X(0).toFixed(2)},${(padT + ph).toFixed(2)} Z`,
        fill: `url(#${gid})`, stroke: 'none',
      }));
    }
    svg.append(n('path', { d, fill: 'none', stroke: color, class: 'chart-line' }));
  });

  // x labels, thinned by MEASURED width so they never collide at any size
  if (axis && labels.length) {
    const need = Math.max(...labels.map(t => textWidth(t, F.font))) + F.font;
    const every = Math.max(1, Math.ceil(labels.length / Math.max(2, Math.floor(pw / need))));
    labels.forEach((t, i) => {
      if (i % every !== 0 && i !== labels.length - 1) return;
      // Drop a penultimate label that would touch the last one.
      if (i !== labels.length - 1 && X(labels.length - 1) - X(i) < need) return;
      svg.append(n('text', {
        x: X(i).toFixed(2), y: (height - F.pad).toFixed(2),
        class: 'chart-tick chart-tick-x', 'font-size': F.font,
        'text-anchor': i === 0 ? 'start' : i === labels.length - 1 ? 'end' : 'middle',
      }, document.createTextNode(t)));
    });
  }

  if (onHover) {
    const cursor = n('line', { class: 'chart-cursor', y1: padT, y2: padT + ph, x1: 0, x2: 0, opacity: 0 });
    const dot = n('circle', { class: 'chart-dot', r: Math.max(3, F.font * 0.34), cx: 0, cy: 0, opacity: 0 });
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
    groups = [], width = 320, height = 140, showAxis = null,
    format = compactMoney, onHover = null, signed = false,
  } = opts;

  const F = fit(width, height);
  const svg = svgRoot(width, height, 'chart chart-bars');
  if (!groups.length) return svg;
  const axis = showAxis === null ? F.axis : (showAxis && F.axis);

  const all = groups.flatMap(g => g.values).filter(Number.isFinite);
  let hi = Math.max(0, ...all);
  let lo = signed ? Math.min(0, ...all) : 0;
  if (hi === lo) hi = lo + 1;
  const step = niceStep(hi - lo, Math.min(3, F.yTicks));
  hi = Math.ceil(hi / step) * step;
  lo = Math.floor(lo / step) * step;

  let padL = F.pad;
  if (axis) {
    let widest = 0;
    for (let v = lo; v <= hi + 1e-9; v += step) {
      widest = Math.max(widest, textWidth(format(v), F.font));
    }
    padL = Math.min(width * 0.34, widest + F.font * 0.7);
  }
  const padR = F.pad + 1;
  const padT = Math.round(F.font * 0.7);
  // Group labels are dropped rather than overlapped when the slots get narrow.
  const slotW = (width - padL - padR) / groups.length;
  const labelFits = slotW > Math.max(...groups.map(g => textWidth(g.label, F.font))) + 3;
  const showLabels = axis && labelFits;
  const padB = showLabels ? F.font + F.pad + 2 : F.pad;
  const pw = Math.max(1, width - padL - padR);
  const ph = Math.max(1, height - padT - padB);
  const Y = v => padT + ph - ((v - lo) / (hi - lo)) * ph;

  if (axis) {
    for (let v = lo; v <= hi + 1e-9; v += step) {
      const y = Y(v);
      svg.append(n('line', {
        x1: padL, x2: width - padR, y1: y.toFixed(2), y2: y.toFixed(2),
        class: Math.abs(v) < 1e-9 ? 'chart-grid chart-zero' : 'chart-grid',
      }));
      svg.append(n('text', {
        x: (padL - F.pad).toFixed(2), y: (y + F.font * 0.34).toFixed(2),
        class: 'chart-tick chart-tick-y', 'text-anchor': 'end', 'font-size': F.font,
      }, document.createTextNode(format(v))));
    }
  }

  const slot = pw / groups.length;
  const per = Math.max(1, groups[0].values.length);
  const gap = Math.min(slot * 0.16, F.font * 0.5);
  const inner = Math.max(0.5, Math.min(1.5, slot * 0.03));
  const bw = Math.max(1.5, (slot - gap * 2 - inner * (per - 1)) / per);

  groups.forEach((g, gi) => {
    g.values.forEach((v, vi) => {
      // Skip zero outright. The minimum height below keeps a small-but-real
      // amount visible, and applying it to zero would paint a sliver on the
      // baseline that reads as a rendering artefact rather than "no activity".
      if (!Number.isFinite(v) || v === 0) return;
      const x = padL + gi * slot + gap + vi * (bw + inner);
      const y0 = Y(0), y1 = Y(v);
      svg.append(n('rect', {
        x: x.toFixed(2), y: Math.min(y0, y1).toFixed(2),
        width: bw.toFixed(2), height: Math.max(1, Math.abs(y1 - y0)).toFixed(2),
        rx: Math.min(3, bw / 2.5), fill: (g.colors || [])[vi] || seriesColor(vi),
        class: 'chart-bar',
      }));
    });
    if (showLabels) {
      svg.append(n('text', {
        x: (padL + gi * slot + slot / 2).toFixed(2), y: (height - F.pad).toFixed(2),
        class: 'chart-tick chart-tick-x', 'text-anchor': 'middle', 'font-size': F.font,
      }, document.createTextNode(g.label)));
    }
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
 * Donut with a free centre for a total. Fills the box it is given: `size` is
 * derived from the smaller dimension so it never overflows a lopsided widget.
 */
export function donut(opts) {
  const {
    slices = [], width = 120, height = 120, onHover = null, gap = 0.018,
    thickness = null,
  } = opts;
  const size = Math.max(24, Math.min(width, height));
  const svg = svgRoot(width, height, 'chart chart-donut');
  const total = slices.reduce((a, s) => a + Math.max(0, s.value || 0), 0);
  // Ring thickness is a fraction of the radius, so a big donut does not become
  // a thin wire and a small one does not close up into a disc.
  const th = thickness ?? Math.max(6, Math.min(size * 0.22, 40));
  const r = (size - th) / 2;
  const cx = width / 2, cy = height / 2;

  if (total <= 0) {
    svg.append(n('circle', {
      cx, cy, r, class: 'chart-donut-empty', fill: 'none', 'stroke-width': th,
    }));
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
      d: `M${x0.toFixed(2)},${y0.toFixed(2)} A${r.toFixed(2)},${r.toFixed(2)} 0 ${large} 1 ${x1.toFixed(2)},${y1.toFixed(2)}`,
      fill: 'none', stroke: s.color || seriesColor(i),
      'stroke-width': th, 'stroke-linecap': 'butt', class: 'chart-arc',
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
    value = 0, max = 100, width = 110, height = 110, thickness = null,
    color = 'var(--primary)', track = true, sweep = 1.5 * Math.PI,
  } = opts;
  const size = Math.max(24, Math.min(width, height));
  const svg = svgRoot(width, height, 'chart chart-gauge');
  const th = thickness ?? Math.max(5, Math.min(size * 0.11, 22));
  const r = (size - th) / 2;
  const cx = width / 2, cy = height / 2;
  const start = Math.PI * 0.75;
  const frac = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;

  const arc = (from, to, cls, stroke) => {
    if (to - from < 1e-4) return null;
    const x0 = cx + r * Math.cos(from), y0 = cy + r * Math.sin(from);
    const x1 = cx + r * Math.cos(to), y1 = cy + r * Math.sin(to);
    return n('path', {
      d: `M${x0.toFixed(2)},${y0.toFixed(2)} A${r.toFixed(2)},${r.toFixed(2)} 0 ${(to - from) > Math.PI ? 1 : 0} 1 ${x1.toFixed(2)},${y1.toFixed(2)}`,
      fill: 'none', stroke, 'stroke-width': th, 'stroke-linecap': 'round', class: cls,
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
  const svg = svgRoot(width, height, 'chart chart-spark');
  if (vals.length < 2) return svg;
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = (hi - lo) || 1;
  const inset = 1.5;
  const pts = vals.map((v, i) => [
    (i / (vals.length - 1)) * width,
    height - inset - ((v - lo) / span) * (height - inset * 2),
  ]);
  const d = smoothPath(pts, 0.4, { x0: 0, x1: width, y0: inset, y1: height - inset });
  if (fill) {
    svg.append(n('path', {
      d: `${d} L${width},${height} L0,${height} Z`,
      fill: color, opacity: 0.14, stroke: 'none',
    }));
  }
  svg.append(n('path', { d, fill: 'none', stroke: color, class: 'chart-line' }));
  return svg;
}
