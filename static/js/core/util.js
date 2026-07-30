/* Shared helpers: DOM building, date maths, formatting.
 *
 * Time rule for the whole front end: the API speaks UTC ISO ("...Z") and the
 * screen speaks the panel's local time. Conversion happens only at the edges
 * (toApi / fromApi). All-day events are the exception — they are floating
 * dates and must never be converted; use dateOnly/shiftKey for those.
 */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** el('div.card', {onclick}, [children]) — terse DOM building without a framework. */
export function el(spec, props = {}, children = []) {
  const [tag, ...classes] = String(spec).split('.');
  const node = document.createElement(tag || 'div');
  if (classes.length) node.className = classes.join(' ');
  for (const [k, v] of Object.entries(props)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of [].concat(children)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

export const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); };

export function debounce(fn, ms = 250) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export const clamp = (n, lo, hi) => Math.max(lo, Math.min(n, hi));

// ------------------------------------------------------------------- dates

export const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
export const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'];

export const startOfDay = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
export const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
export const addMonths = (d, n) => new Date(d.getFullYear(), d.getMonth() + n, 1);
export const startOfWeek = (d, firstDay = 0) => {
  const s = startOfDay(d);
  return addDays(s, -((s.getDay() - firstDay + 7) % 7));
};
export const sameDay = (a, b) =>
  a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();

export const dayKey = d =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

/* All-day events are floating dates — "July 4th" is July 4th in any timezone,
   the same rule iCal, Google and Graph use. Compare them by date component and
   never run them through a timezone, or they straddle two days west of UTC. */
export const dateOnly = s => String(s).slice(0, 10);
export const shiftKey = (key, n) => {
  const [y, m, d] = key.split('-').map(Number);
  return dayKey(new Date(y, m - 1, d + n));
};

export const toApi = d => new Date(d.getTime() - d.getMilliseconds()).toISOString().replace(/\.\d{3}Z$/, 'Z');
export const fromApi = s => new Date(s);

export const fmtTime = (d, h12 = true) =>
  d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', hour12: h12 });
export const fmtHour = (h, h12 = true) =>
  new Date(2000, 0, 1, h).toLocaleTimeString([], { hour: 'numeric', hour12: h12 });

/** "in 25 min", "now", "2h 10m" — used by the agenda and reminders. */
export function relativeTime(target, now = new Date()) {
  const mins = Math.round((target - now) / 60000);
  if (mins <= 0 && mins > -60) return 'now';
  if (mins < 0) return '';
  if (mins < 60) return `in ${mins} min`;
  const h = Math.floor(mins / 60), m = mins % 60;
  if (h < 24) return m ? `in ${h}h ${m}m` : `in ${h}h`;
  const days = Math.round(h / 24);
  return `in ${days} day${days === 1 ? '' : 's'}`;
}

/** Events overlapping a local day, split into all-day and timed. */
export function eventsForDay(events, day) {
  const s = startOfDay(day), e = addDays(s, 1), k = dayKey(day);
  const hit = events.filter(ev => {
    if (ev.all_day) return dateOnly(ev.start_utc) <= k && k < dateOnly(ev.end_utc);
    return fromApi(ev.start_utc) < e && fromApi(ev.end_utc) > s;
  });
  return {
    allDay: hit.filter(ev => ev.all_day),
    timed: hit.filter(ev => !ev.all_day).sort((a, b) => fromApi(a.start_utc) - fromApi(b.start_utc)),
  };
}

/**
 * Lay out overlapping events side by side.
 * Sweep in start order; anything overlapping the running cluster joins it, and
 * each cluster is packed into the fewest columns that fit.
 */
export function layoutColumns(evs) {
  const items = evs.map(ev => ({
    ev,
    s: fromApi(ev.start_utc).getTime(),
    e: Math.max(fromApi(ev.end_utc).getTime(), fromApi(ev.start_utc).getTime() + 15 * 60000),
  })).sort((a, b) => a.s - b.s || b.e - a.e);

  const out = [];
  let cluster = [], clusterEnd = -Infinity;
  const flush = () => {
    if (!cluster.length) return;
    const cols = [];
    cluster.forEach(it => {
      let ci = cols.findIndex(col => col[col.length - 1].e <= it.s);
      if (ci === -1) { cols.push([it]); ci = cols.length - 1; }
      else cols[ci].push(it);
      it.col = ci;
    });
    cluster.forEach(it => out.push({ ...it, cols: cols.length }));
    cluster = []; clusterEnd = -Infinity;
  };
  items.forEach(it => {
    if (it.s >= clusterEnd) flush();
    cluster.push(it);
    clusterEnd = Math.max(clusterEnd, it.e);
  });
  flush();
  return out;
}

/* Deliberately desaturated. Full-chroma hues on a panel that's lit all day in
   a living room read as neon and are tiring to look at; these stay
   distinguishable at a glance without shouting. Colour is only ever expressed
   as a 2px rule, a dot or an icon — see widgets.css. */
export const EVENT_COLORS = [
  { name: 'blue', value: '#6c86c8' }, { name: 'green', value: '#7d9e6b' },
  { name: 'purple', value: '#907fb4' }, { name: 'amber', value: '#c2914f' },
  { name: 'rose', value: '#c07281' }, { name: 'teal', value: '#5b95a0' },
];
export const DEFAULT_COLOR = EVENT_COLORS[0].value;
