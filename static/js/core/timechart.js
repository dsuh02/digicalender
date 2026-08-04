/* Interactive multi-series time chart: pan, pinch-zoom, and a scrub readout.
 *
 * Separate from charts.js, which draws static shapes and rebuilds them on every
 * change. That model cannot work here for one hard reason: **rebuilding the SVG
 * destroys the element the pointer capture is attached to**, so the gesture dies
 * on the first move. Everything below is built ONCE and then mutated in place —
 * paths get new `d` attributes, tick nodes are pooled and re-labelled, and no
 * node is created or removed while a finger is down.
 *
 * Gesture model, chosen so nothing fights the page pager:
 *   1 finger  — scrub. Reads out every series at that instant.
 *   2 fingers — pan and pinch-zoom together, anchored so the date under the
 *               midpoint of your fingers stays under it.
 *   wheel     — zoom about the cursor (mouse only; the panel has no wheel).
 *
 * The pager also uses a two-finger drag to change pages, so the plot claims its
 * gestures with touch-action:none and stops propagation. Inside the chart, two
 * fingers mean the chart.
 *
 * Two display modes over the same data and the same interactions:
 *   'lines'   — one line per account, compared against each other.
 *   'stacked' — areas summed bottom-up, showing the combined total.
 */

const NS = 'http://www.w3.org/2000/svg';
const MIN_SPAN = 6;              // months; below this the axis is meaningless

function n(name, attrs = {}) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  return node;
}

function niceStep(span, target = 4) {
  if (!(span > 0)) return 1;
  const raw = span / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  return (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
}

export function compactMoney(v) {
  const a = Math.abs(v);
  const sign = v < 0 ? '−' : '';
  if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(a >= 1e10 ? 0 : 1)}B`;
  if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(a >= 1e7 ? 0 : 1)}M`;
  if (a >= 1e3) return `${sign}$${(a / 1e3).toFixed(a >= 1e4 ? 0 : 1)}k`;
  return `${sign}$${Math.round(a)}`;
}

/** Keep exactly `count` children of `tag` under `parent`, reusing what exists. */
function pool(parent, store, tag, count, attrs = {}) {
  while (store.length < count) {
    const node = n(tag, attrs);
    parent.append(node);
    store.push(node);
  }
  for (let i = 0; i < store.length; i++) {
    store[i].setAttribute('opacity', i < count ? '1' : '0');
  }
  return store;
}

/**
 * opts:
 *   months   ['2026-08', ...]
 *   series   [{ key, name, values:[n], color }]
 *   mode     'lines' | 'stacked'
 *   onReadout(index|null)   — index into months, or null when the finger lifts
 *   onView(i0, i1)          — visible range changed
 */
export function timeChart(opts = {}) {
  let months = opts.months || [];
  let series = opts.series || [];
  let mode = opts.mode === 'stacked' ? 'stacked' : 'lines';
  const format = opts.format || compactMoney;
  const onReadout = opts.onReadout || (() => {});
  const onView = opts.onView || (() => {});

  let W = 320, H = 200, K = 1;
  let i0 = 0, i1 = Math.max(1, months.length - 1);

  const svg = n('svg', { class: 'chart tchart' });
  const gGrid = n('g', { class: 'tchart-grid' });
  const gArea = n('g', { class: 'tchart-areas' });
  const gLine = n('g', { class: 'tchart-lines' });
  const gTickY = n('g', { class: 'tchart-ticks-y' });
  const gTickX = n('g', { class: 'tchart-ticks-x' });
  const cursor = n('line', { class: 'chart-cursor', opacity: 0 });
  const gDots = n('g', { class: 'tchart-dots' });
  const hit = n('rect', { fill: 'transparent' });
  svg.append(gGrid, gArea, gLine, gTickY, gTickX, cursor, gDots, hit);

  const gridPool = [], tickYPool = [], tickXPool = [], dotPool = [];
  let areaPaths = [], linePaths = [];

  /* Series paths are the one thing rebuilt when the DATA changes — never while
     a gesture is running, because setData is only called from a load. */
  function buildSeriesNodes() {
    while (gArea.firstChild) gArea.removeChild(gArea.firstChild);
    while (gLine.firstChild) gLine.removeChild(gLine.firstChild);
    areaPaths = series.map((s) => {
      const p = n('path', { class: 'tchart-area', fill: s.color, opacity: 0.16, stroke: 'none' });
      gArea.append(p);
      return p;
    });
    linePaths = series.map((s) => {
      const p = n('path', { class: 'tchart-line chart-line', fill: 'none', stroke: s.color });
      gLine.append(p);
      return p;
    });
  }
  buildSeriesNodes();

  /* ------------------------------------------------------------ geometry */

  let padL = 40, padR = 4, padT = 8, padB = 18, pw = 1, ph = 1, font = 10;

  function layout() {
    // Same multiplier the rest of the widget uses, so the axis never drifts out
    // of step with the labels beside it.
    font = Math.max(5, Math.min(15, Math.round(Math.min(W, H) / 16)) * K);
    padT = Math.round(font * 0.7);
    padR = Math.max(3, Math.round(font * 0.4));
    padB = font + 6;
    const yMax = maxVisible();
    padL = Math.min(W * 0.34, format(yMax).length * font * 0.58 + font * 0.8);
    pw = Math.max(1, W - padL - padR);
    ph = Math.max(1, H - padT - padB);
  }

  /** Highest value drawn in the current window — stacked sums, lines don't. */
  function maxVisible() {
    let hi = 0;
    const a = Math.max(0, Math.floor(i0)), b = Math.min(months.length - 1, Math.ceil(i1));
    for (let i = a; i <= b; i++) {
      if (mode === 'stacked') {
        let sum = 0;
        for (const s of series) sum += Number(s.values[i]) || 0;
        if (sum > hi) hi = sum;
      } else {
        for (const s of series) {
          const v = Number(s.values[i]) || 0;
          if (v > hi) hi = v;
        }
      }
    }
    return hi > 0 ? hi : 1;
  }

  const X = (i) => padL + ((i - i0) / Math.max(1e-6, i1 - i0)) * pw;
  let yScale = (v) => padT + ph - (v / 1) * ph;

  /* --------------------------------------------------------------- render */

  function render() {
    layout();
    const hi = maxVisible();
    const step = niceStep(hi, Math.max(2, Math.min(5, Math.floor(ph / 44))));
    const top = Math.ceil(hi / step) * step || step;
    yScale = (v) => padT + ph - (v / top) * ph;

    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('width', W);
    svg.setAttribute('height', H);
    hit.setAttribute('x', padL); hit.setAttribute('y', padT);
    hit.setAttribute('width', pw); hit.setAttribute('height', ph);
    cursor.setAttribute('y1', padT); cursor.setAttribute('y2', padT + ph);

    // gridlines + y labels
    const rows = Math.floor(top / step) + 1;
    pool(gGrid, gridPool, 'line', rows, { class: 'chart-grid' });
    pool(gTickY, tickYPool, 'text', rows, { class: 'chart-tick chart-tick-y', 'text-anchor': 'end' });
    for (let r = 0; r < rows; r++) {
      const v = r * step;
      const y = yScale(v).toFixed(2);
      const g = gridPool[r];
      g.setAttribute('x1', padL); g.setAttribute('x2', W - padR);
      g.setAttribute('y1', y); g.setAttribute('y2', y);
      const t = tickYPool[r];
      t.setAttribute('x', (padL - Math.max(2, font * 0.4)).toFixed(2));
      t.setAttribute('y', (+y + font * 0.34).toFixed(2));
      t.setAttribute('font-size', font);
      t.textContent = format(v);
    }

    // x labels: pick whole years that fit, so the axis reads 2030, 2035, …
    const labels = yearTicks();
    pool(gTickX, tickXPool, 'text', labels.length, { class: 'chart-tick chart-tick-x', 'text-anchor': 'middle' });
    labels.forEach((L, k) => {
      const t = tickXPool[k];
      t.setAttribute('x', X(L.i).toFixed(2));
      t.setAttribute('y', (H - 4).toFixed(2));
      t.setAttribute('font-size', font);
      t.textContent = L.text;
    });

    // series
    const a = Math.max(0, Math.floor(i0) - 1);
    const b = Math.min(months.length - 1, Math.ceil(i1) + 1);
    const base = new Array(b - a + 1).fill(0);

    series.forEach((s, si) => {
      let d = '';
      const pts = [];
      for (let i = a; i <= b; i++) {
        const raw = Number(s.values[i]) || 0;
        const v = mode === 'stacked' ? base[i - a] + raw : raw;
        if (mode === 'stacked') base[i - a] = v;
        pts.push([X(i), yScale(v)]);
      }
      d = 'M' + pts.map(p => `${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(' L');
      linePaths[si].setAttribute('d', d);

      if (mode === 'stacked') {
        // Close along the layer beneath, so each band is its own contribution
        // rather than an opaque block hiding the ones below it.
        const under = [];
        for (let i = b; i >= a; i--) {
          const belowVal = base[i - a] - (Number(s.values[i]) || 0);
          under.push([X(i), yScale(belowVal)]);
        }
        areaPaths[si].setAttribute('d',
          d + ' L' + under.map(p => `${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(' L') + ' Z');
        areaPaths[si].setAttribute('opacity', 0.55);
      } else {
        areaPaths[si].setAttribute('d',
          `${d} L${X(b).toFixed(2)},${(padT + ph).toFixed(2)} L${X(a).toFixed(2)},${(padT + ph).toFixed(2)} Z`);
        areaPaths[si].setAttribute('opacity', 0.14);
      }
    });

    pool(gDots, dotPool, 'circle', series.length, { class: 'chart-dot' });
    dotPool.forEach((dot, k) => {
      dot.setAttribute('r', Math.max(3, font * 0.32));
      if (series[k]) dot.setAttribute('stroke', series[k].color);
      if (k >= series.length) dot.setAttribute('opacity', 0);
    });
    if (readIndex === null) hideCursor();
    else showCursor(readIndex);

    onView(i0, i1);
  }

  /** Year boundaries inside the window, thinned to what fits. */
  function yearTicks() {
    if (!months.length) return [];
    const out = [];
    const a = Math.max(0, Math.floor(i0)), b = Math.min(months.length - 1, Math.ceil(i1));
    for (let i = a; i <= b; i++) {
      if (months[i].endsWith('-01') || i === a) out.push({ i, text: months[i].slice(0, 4) });
    }
    if (!out.length) out.push({ i: a, text: months[a].slice(0, 4) });
    const need = font * 0.58 * 4 + font * 1.6;
    const room = Math.max(2, Math.floor(pw / need));
    const every = Math.ceil(out.length / room);
    const thinned = out.filter((_, k) => k % every === 0);
    // Months, not years, once the window is short enough for years to be
    // useless (a 9-month view labelled "2031 2031" tells you nothing).
    if ((i1 - i0) <= 24) {
      const NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
      const ms = [];
      for (let i = a; i <= b; i++) {
        ms.push({ i, text: `${NAMES[Number(months[i].slice(5, 7)) - 1]} ’${months[i].slice(2, 4)}` });
      }
      const e2 = Math.ceil(ms.length / Math.max(2, Math.floor(pw / (font * 0.58 * 7))));
      return ms.filter((_, k) => k % e2 === 0);
    }
    return thinned;
  }

  /* ------------------------------------------------------------- readout */

  let readIndex = null;

  function showCursor(idx) {
    const x = X(idx);
    cursor.setAttribute('x1', x); cursor.setAttribute('x2', x);
    cursor.setAttribute('opacity', 1);
    let stack = 0;
    series.forEach((s, k) => {
      const raw = Number(s.values[idx]) || 0;
      const v = mode === 'stacked' ? (stack += raw) : raw;
      const dot = dotPool[k];
      if (!dot) return;
      dot.setAttribute('cx', x);
      dot.setAttribute('cy', yScale(v));
      dot.setAttribute('opacity', 1);
    });
  }

  function hideCursor() {
    cursor.setAttribute('opacity', 0);
    dotPool.forEach(d => d.setAttribute('opacity', 0));
  }

  function indexAt(clientX) {
    const box = svg.getBoundingClientRect();
    if (!box.width) return null;
    const px = ((clientX - box.left) / box.width) * W;
    const f = (px - padL) / pw;
    const i = Math.round(i0 + f * (i1 - i0));
    return Math.max(0, Math.min(months.length - 1, i));
  }

  /* ------------------------------------------------------------ gestures */

  const pointers = new Map();
  let pinch = null;

  function localX(clientX) {
    const box = svg.getBoundingClientRect();
    return box.width ? ((clientX - box.left) / box.width) * W : 0;
  }

  function beginPinch() {
    const [p, q] = [...pointers.values()];
    const mid = (localX(p.x) + localX(q.x)) / 2;
    const dist = Math.abs(localX(p.x) - localX(q.x)) || 1;
    pinch = { dist, mid, i0, i1, anchor: i0 + ((mid - padL) / pw) * (i1 - i0) };
    // A pinch is not a scrub: drop the readout so the two never fight.
    readIndex = null;
    hideCursor();
    onReadout(null);
  }

  function applyPinch() {
    const [p, q] = [...pointers.values()];
    const mid = (localX(p.x) + localX(q.x)) / 2;
    const dist = Math.abs(localX(p.x) - localX(q.x)) || 1;

    const total = Math.max(1, months.length - 1);
    let span = (pinch.i1 - pinch.i0) * (pinch.dist / dist);
    span = Math.max(MIN_SPAN, Math.min(total, span));

    // Keep the date that was under the fingers' midpoint under it as the
    // fingers move AND spread — that is what makes a pinch feel physical
    // rather than like a zoom button.
    const f = Math.max(0, Math.min(1, (mid - padL) / pw));
    let a = pinch.anchor - f * span;
    a = Math.max(0, Math.min(total - span, a));
    i0 = a;
    i1 = a + span;
    render();
  }

  hit.addEventListener('pointerdown', (e) => {
    e.stopPropagation();          // the pager must not see this as a page swipe
    hit.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 1) {
      readIndex = indexAt(e.clientX);
      showCursor(readIndex);
      onReadout(readIndex);
    } else if (pointers.size === 2) {
      beginPinch();
    }
  });

  hit.addEventListener('pointermove', (e) => {
    if (!pointers.has(e.pointerId)) return;
    e.stopPropagation();
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size >= 2 && pinch) {
      applyPinch();
    } else if (pointers.size === 1 && readIndex !== null) {
      readIndex = indexAt(e.clientX);
      showCursor(readIndex);
      onReadout(readIndex);
    }
  });

  function endPointer(e) {
    if (!pointers.has(e.pointerId)) return;
    pointers.delete(e.pointerId);
    try { hit.releasePointerCapture(e.pointerId); } catch { /* already gone */ }
    if (pointers.size < 2) pinch = null;
    // Lifting one finger of a pinch must NOT silently become a scrub — the
    // remaining finger is mid-gesture and would yank the readout around.
    if (pointers.size === 0) {
      readIndex = null;
      hideCursor();
      onReadout(null);
    }
  }
  hit.addEventListener('pointerup', endPointer);
  hit.addEventListener('pointercancel', endPointer);
  hit.addEventListener('lostpointercapture', endPointer);

  // Mouse/trackpad only; the panel has no wheel, but this is how the chart is
  // testable on a laptop.
  hit.addEventListener('wheel', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const total = Math.max(1, months.length - 1);
    const f = Math.max(0, Math.min(1, (localX(e.clientX) - padL) / pw));
    const anchor = i0 + f * (i1 - i0);
    let span = (i1 - i0) * (e.deltaY > 0 ? 1.15 : 0.87);
    span = Math.max(MIN_SPAN, Math.min(total, span));
    let a = Math.max(0, Math.min(total - span, anchor - f * span));
    i0 = a; i1 = a + span;
    render();
  }, { passive: false });

  /* ----------------------------------------------------------------- API */

  return {
    node: svg,
    setSize(w, h, k = K) { W = w; H = h; K = (k > 0 ? k : 1); render(); },
    setMode(m) {
      mode = m === 'stacked' ? 'stacked' : 'lines';
      render();
    },
    getMode: () => mode,
    setData(newMonths, newSeries, { keepView = false } = {}) {
      const total = Math.max(1, newMonths.length - 1);
      months = newMonths;
      series = newSeries;
      buildSeriesNodes();
      if (!keepView || i1 <= i0) { i0 = 0; i1 = total; }
      else { i1 = Math.min(i1, total); i0 = Math.max(0, Math.min(i0, i1 - MIN_SPAN)); }
      render();
    },
    reset() {
      i0 = 0; i1 = Math.max(1, months.length - 1);
      readIndex = null; hideCursor(); onReadout(null);
      render();
    },
    zoomTo(a, b) {
      const total = Math.max(1, months.length - 1);
      const span = Math.max(MIN_SPAN, Math.min(total, b - a));
      i0 = Math.max(0, Math.min(total - span, a));
      i1 = i0 + span;
      render();
    },
    view: () => ({ i0, i1 }),
    destroy() { pointers.clear(); pinch = null; },
  };
}
