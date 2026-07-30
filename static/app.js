/* DigiCalender front-end.
 *
 * No framework and no build step: this is served off a small stdlib Python
 * server onto a kiosk Chromium, and a build pipeline would be one more thing
 * that can break a wall display at 3am.
 *
 * Times: the API speaks UTC ISO ("...Z"); everything on screen is the panel's
 * local time. Conversion happens only at the two edges (toApi / fromApi).
 */
'use strict';

// ---------------------------------------------------------------- constants

const COLORS = [
  { name: 'blue',   value: '#7aa2f7' },
  { name: 'green',  value: '#9ece6a' },
  { name: 'purple', value: '#bb9af7' },
  { name: 'orange', value: '#ff9e64' },
  { name: 'pink',   value: '#f7768e' },
  { name: 'teal',   value: '#2ac3de' },
];
const DEFAULT_COLOR = COLORS[0].value;
const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                'August', 'September', 'October', 'November', 'December'];
const HOUR_H = 62;          // must match --hour-h in app.css
const MAX_CHIPS = 4;        // month cell before "+N more"

// ---------------------------------------------------------------- date utils

const startOfDay = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const addDays    = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const addMonths  = (d, n) => new Date(d.getFullYear(), d.getMonth() + n, 1);
const startOfWeek= d => addDays(startOfDay(d), -d.getDay());
const sameDay    = (a, b) => a.getFullYear() === b.getFullYear()
                          && a.getMonth() === b.getMonth()
                          && a.getDate() === b.getDate();
const dayKey     = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

/* All-day events are *floating dates*, not instants — same rule Google, Graph
   and iCal use. "July 4th" is July 4th regardless of the panel's timezone, so
   these are compared by their date component and never converted. Treating
   them as UTC instants makes them straddle two local days west of Greenwich. */
const dateOnly   = s => s.slice(0, 10);
const shiftKey   = (key, n) => {
  const [y, m, d] = key.split('-').map(Number);
  return dayKey(new Date(y, m - 1, d + n));
};

/** Local Date -> API UTC string. */
const toApi = d => new Date(d.getTime() - d.getMilliseconds()).toISOString().replace(/\.\d{3}Z$/, 'Z');
/** API UTC string -> local Date. */
const fromApi = s => new Date(s);

const fmtTime = d => d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
const fmtHour = h => {
  const d = new Date(2000, 0, 1, h);
  return d.toLocaleTimeString([], { hour: 'numeric' });
};

// ---------------------------------------------------------------- api client

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  let body = null;
  try { body = await res.json(); } catch { /* empty body is fine on some paths */ }
  if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
  return body;
}

const API = {
  list: (start, end) =>
    api(`/api/events?start=${encodeURIComponent(toApi(start))}&end=${encodeURIComponent(toApi(end))}`)
      .then(r => r.events),
  create: data => api('/api/events', { method: 'POST', body: JSON.stringify(data) }).then(r => r.event),
  update: (uid, data) => api(`/api/events/${uid}`, { method: 'PATCH', body: JSON.stringify(data) }).then(r => r.event),
  remove: uid => api(`/api/events/${uid}`, { method: 'DELETE' }),
};

// ---------------------------------------------------------------- state

const state = {
  view: localStorage.getItem('dc.view') || 'month',
  cursor: new Date(),
  events: [],
  editing: null,      // uid being edited, or null for a new event
  color: DEFAULT_COLOR,
  busy: false,
};

const $ = id => document.getElementById(id);

// ---------------------------------------------------------------- range

/** [start, end) covering everything the current view needs. */
function viewRange() {
  if (state.view === 'month') {
    const first = new Date(state.cursor.getFullYear(), state.cursor.getMonth(), 1);
    const gridStart = startOfWeek(first);
    return [gridStart, addDays(gridStart, 42)];
  }
  if (state.view === 'week') {
    const s = startOfWeek(state.cursor);
    return [s, addDays(s, 7)];
  }
  const s = startOfDay(state.cursor);
  return [s, addDays(s, 1)];
}

function periodLabel() {
  const c = state.cursor;
  if (state.view === 'month') return `${MONTHS[c.getMonth()]} ${c.getFullYear()}`;
  if (state.view === 'week') {
    const s = startOfWeek(c), e = addDays(s, 6);
    const sm = MONTHS[s.getMonth()].slice(0, 3), em = MONTHS[e.getMonth()].slice(0, 3);
    return s.getMonth() === e.getMonth()
      ? `${sm} ${s.getDate()} – ${e.getDate()}, ${e.getFullYear()}`
      : `${sm} ${s.getDate()} – ${em} ${e.getDate()}, ${e.getFullYear()}`;
  }
  return c.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
}

// ---------------------------------------------------------------- loading

async function load() {
  const [s, e] = viewRange();
  try {
    // Pad by a day each way: the server filters on UTC instants, so an all-day
    // event sitting on the edge of the window can fall outside it by the
    // panel's UTC offset. Cheap to over-fetch, and the views filter anyway.
    state.events = await API.list(addDays(s, -1), addDays(e, 1));
  } catch (err) {
    toast(`Could not load events: ${err.message}`, true);
    state.events = [];
  }
  render();
}

/** Events overlapping a given local day, split into all-day and timed. */
function eventsForDay(day) {
  const s = startOfDay(day), e = addDays(s, 1);
  const k = dayKey(day);
  const hit = state.events.filter(ev => {
    if (ev.all_day) {
      // Half-open date range: end is the day *after* the last day shown.
      return dateOnly(ev.start_utc) <= k && k < dateOnly(ev.end_utc);
    }
    const es = fromApi(ev.start_utc), ee = fromApi(ev.end_utc);
    return es < e && ee > s;
  });
  return {
    allDay: hit.filter(ev => ev.all_day),
    timed: hit.filter(ev => !ev.all_day)
           .sort((a, b) => fromApi(a.start_utc) - fromApi(b.start_utc)),
  };
}

// ---------------------------------------------------------------- render

function render() {
  $('periodLabel').textContent = periodLabel();
  document.querySelectorAll('.seg').forEach(b =>
    b.setAttribute('aria-selected', String(b.dataset.view === state.view)));
  ['Month', 'Week', 'Day'].forEach(v =>
    $('view' + v).hidden = state.view !== v.toLowerCase());

  if (state.view === 'month') renderMonth();
  else if (state.view === 'week') renderTime($('weekHead'), $('weekAllDay'), $('weekGutter'), $('weekCanvas'), 7, $('weekScroll'));
  else renderTime($('dayHead'), $('dayAllDay'), $('dayGutter'), $('dayCanvas'), 1, $('dayScroll'));
}

function chip(ev) {
  const el = document.createElement('div');
  el.className = 'chip';
  const c = ev.color || DEFAULT_COLOR;
  el.style.borderLeftColor = c;
  el.style.background = `color-mix(in srgb, ${c} 20%, transparent)`;
  if (ev.all_day) {
    el.textContent = ev.title;
  } else {
    const t = document.createElement('span');
    t.className = 't';
    t.textContent = fmtTime(fromApi(ev.start_utc));
    el.appendChild(t);
    el.appendChild(document.createTextNode(ev.title));
  }
  el.addEventListener('click', e => { e.stopPropagation(); openEditor(ev); });
  return el;
}

function renderMonth() {
  const dow = $('monthDow');
  if (!dow.childElementCount) {
    DOW.forEach(d => { const el = document.createElement('div'); el.textContent = d; dow.appendChild(el); });
  }

  const grid = $('monthGrid');
  grid.replaceChildren();
  const [gridStart] = viewRange();
  const today = new Date();
  const month = state.cursor.getMonth();

  for (let i = 0; i < 42; i++) {
    const day = addDays(gridStart, i);
    const cell = document.createElement('div');
    cell.className = 'mcell';
    if (day.getMonth() !== month) cell.classList.add('other');
    if (sameDay(day, today)) cell.classList.add('today');

    const num = document.createElement('div');
    num.className = 'mnum';
    num.textContent = day.getDate();
    cell.appendChild(num);

    const box = document.createElement('div');
    box.className = 'mevents';
    const { allDay, timed } = eventsForDay(day);
    const all = [...allDay, ...timed];
    all.slice(0, MAX_CHIPS).forEach(ev => box.appendChild(chip(ev)));
    if (all.length > MAX_CHIPS) {
      const more = document.createElement('div');
      more.className = 'more';
      more.textContent = `+${all.length - MAX_CHIPS} more`;
      box.appendChild(more);
    }
    cell.appendChild(box);

    // Tap empty space in a cell -> jump to that day.
    cell.addEventListener('click', () => { state.cursor = day; setView('day'); });
    grid.appendChild(cell);
  }
}

/**
 * Lay out overlapping events side by side.
 * Sweep the day in start order; anything that overlaps the running cluster
 * joins it, and each cluster's members are packed into the fewest columns.
 */
function layoutColumns(evs) {
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

function renderTime(headEl, allDayEl, gutterEl, canvasEl, nDays, scrollEl) {
  const [rangeStart] = viewRange();
  const today = new Date();
  const cols = `repeat(${nDays}, 1fr)`;
  headEl.style.gridTemplateColumns = cols;
  allDayEl.style.gridTemplateColumns = cols;
  canvasEl.style.gridTemplateColumns = cols;

  headEl.replaceChildren();
  allDayEl.replaceChildren();
  canvasEl.replaceChildren();

  if (!gutterEl.childElementCount) {
    for (let h = 0; h < 24; h++) {
      const l = document.createElement('div');
      l.className = 'hour-label';
      l.textContent = h === 0 ? '' : fmtHour(h);
      gutterEl.appendChild(l);
    }
  }

  for (let i = 0; i < nDays; i++) {
    const day = addDays(rangeStart, i);
    const isToday = sameDay(day, today);

    // header
    const h = document.createElement('div');
    if (isToday) h.classList.add('is-today');
    h.innerHTML = `<div class="tv-dow">${DOW[day.getDay()]}</div><div class="tv-date">${day.getDate()}</div>`;
    h.addEventListener('click', () => { state.cursor = day; setView('day'); });
    headEl.appendChild(h);

    const { allDay, timed } = eventsForDay(day);

    // all-day strip
    const ad = document.createElement('div');
    allDay.forEach(ev => ad.appendChild(chip(ev)));
    ad.addEventListener('click', () => openEditor(null, { day, allDay: true }));
    allDayEl.appendChild(ad);

    // timed column
    const col = document.createElement('div');
    col.className = 'col';
    for (let x = 0; x < 24; x++) {
      const line = document.createElement('div');
      line.className = 'hour-line';
      col.appendChild(line);
    }

    // Tap an empty slot -> new event starting at that half hour.
    col.addEventListener('click', e => {
      if (e.target.closest('.ev')) return;
      const y = e.clientY - col.getBoundingClientRect().top;
      const mins = Math.max(0, Math.min(23.5 * 60, Math.floor(y / HOUR_H * 2) * 30));
      const at = new Date(day.getFullYear(), day.getMonth(), day.getDate(),
                          Math.floor(mins / 60), mins % 60);
      openEditor(null, { at });
    });

    const dayStart = startOfDay(day).getTime();
    layoutColumns(timed).forEach(({ ev, s, e, col: ci, cols: nc }) => {
      const top = Math.max(0, (s - dayStart) / 3600000) * HOUR_H;
      const bottom = Math.min(24 * 3600000, e - dayStart) / 3600000 * HOUR_H;
      const el = document.createElement('div');
      const h = Math.max(26, bottom - top - 2);
      // A 30-minute slot can't fit two lines; drop the time and keep the title.
      el.className = h < 44 ? 'ev ev-compact' : 'ev';
      const c = ev.color || DEFAULT_COLOR;
      el.style.top = `${top}px`;
      el.style.height = `${h}px`;
      el.style.left = `calc(${(ci / nc) * 100}% + 3px)`;
      el.style.width = `calc(${(1 / nc) * 100}% - 6px)`;
      el.style.borderLeftColor = c;
      el.style.background = `color-mix(in srgb, ${c} 26%, var(--bg-elev))`;
      el.innerHTML = `<div class="ev-title"></div><div class="ev-time"></div>`;
      el.querySelector('.ev-title').textContent = ev.title;
      el.querySelector('.ev-time').textContent =
        `${fmtTime(fromApi(ev.start_utc))} – ${fmtTime(fromApi(ev.end_utc))}`;
      el.addEventListener('click', evt => { evt.stopPropagation(); openEditor(ev); });
      col.appendChild(el);
    });

    if (isToday) {
      const now = new Date();
      const line = document.createElement('div');
      line.className = 'now-line';
      line.style.top = `${(now.getHours() + now.getMinutes() / 60) * HOUR_H}px`;
      col.appendChild(line);
    }

    canvasEl.appendChild(col);
  }

  // Open on the working day, not on midnight.
  if (scrollEl && !scrollEl.dataset.scrolled) {
    scrollEl.scrollTop = Math.max(0, 7.5 * HOUR_H);
    scrollEl.dataset.scrolled = '1';
  }
}

// ---------------------------------------------------------------- navigation

function setView(v) {
  state.view = v;
  localStorage.setItem('dc.view', v);
  load();
}

function step(dir) {
  if (state.view === 'month') state.cursor = addMonths(state.cursor, dir);
  else if (state.view === 'week') state.cursor = addDays(state.cursor, 7 * dir);
  else state.cursor = addDays(state.cursor, dir);

  const stage = $('stage');
  stage.classList.remove('slide-left', 'slide-right');
  void stage.offsetWidth;                       // restart the animation
  stage.classList.add(dir > 0 ? 'slide-left' : 'slide-right');
  load();
}

// ---------------------------------------------------------------- editor

function openEditor(ev, defaults = {}) {
  state.editing = ev ? ev.uid : null;
  $('sheetTitle').textContent = ev ? 'Edit event' : 'New event';
  $('deleteBtn').hidden = !ev;
  $('formError').hidden = true;

  let start, end, allDay;
  if (ev) {
    start = fromApi(ev.start_utc);
    end = fromApi(ev.end_utc);
    allDay = ev.all_day;
    $('fTitle').value = ev.title;
    $('fLocation').value = ev.location || '';
    $('fNotes').value = ev.description || '';
    state.color = ev.color || DEFAULT_COLOR;
  } else {
    allDay = !!defaults.allDay;
    if (defaults.at) {
      start = defaults.at;
    } else {
      const base = defaults.day || state.cursor;
      const now = new Date();
      // Default to the next half hour, on whichever day is in view.
      start = new Date(base.getFullYear(), base.getMonth(), base.getDate(),
                       now.getHours(), now.getMinutes() < 30 ? 30 : 0);
      if (now.getMinutes() >= 30) start.setHours(start.getHours() + 1);
    }
    end = new Date(start.getTime() + 60 * 60000);
    $('fTitle').value = '';
    $('fLocation').value = '';
    $('fNotes').value = '';
    state.color = DEFAULT_COLOR;
  }

  $('fAllDay').checked = allDay;
  if (allDay && ev) {
    // Read the floating dates straight off the strings; the picker shows the
    // last day inclusively, the stored end is exclusive.
    $('fStartDate').value = dateOnly(ev.start_utc);
    $('fEndDate').value = shiftKey(dateOnly(ev.end_utc), -1);
  } else {
    $('fStartDate').value = dayKey(start);
    $('fEndDate').value = dayKey(end);
  }
  $('fStartTime').value = `${String(start.getHours()).padStart(2, '0')}:${String(start.getMinutes()).padStart(2, '0')}`;
  $('fEndTime').value = `${String(end.getHours()).padStart(2, '0')}:${String(end.getMinutes()).padStart(2, '0')}`;

  renderSwatches();
  syncAllDay();
  $('sheetBackdrop').hidden = false;
  if (!ev) setTimeout(() => $('fTitle').focus(), 60);
}

function closeEditor() {
  $('sheetBackdrop').hidden = true;
  state.editing = null;
}

function renderSwatches() {
  const box = $('fColors');
  box.replaceChildren();
  COLORS.forEach(c => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'swatch';
    b.style.background = c.value;
    b.setAttribute('aria-pressed', String(c.value === state.color));
    b.setAttribute('aria-label', c.name);
    b.addEventListener('click', () => { state.color = c.value; renderSwatches(); });
    box.appendChild(b);
  });
}

function syncAllDay() {
  const on = $('fAllDay').checked;
  document.querySelectorAll('[data-timeonly]').forEach(el => {
    el.style.visibility = on ? 'hidden' : 'visible';
  });
}

function readForm() {
  const allDay = $('fAllDay').checked;
  const sd = $('fStartDate').value, ed = $('fEndDate').value;
  if (!sd || !ed) throw new Error('Pick a start and end date.');
  const title = $('fTitle').value.trim();
  if (!title) throw new Error('Give the event a title.');

  const common = {
    title,
    location: $('fLocation').value.trim(),
    description: $('fNotes').value.trim(),
    all_day: allDay,
    color: state.color,
  };

  if (allDay) {
    // Floating dates — emitted verbatim, never run through a timezone.
    if (ed < sd) throw new Error('The event has to end on or after it starts.');
    return { ...common, start_utc: `${sd}T00:00:00Z`, end_utc: `${shiftKey(ed, 1)}T00:00:00Z` };
  }

  const st = $('fStartTime').value || '00:00';
  const et = $('fEndTime').value || '00:00';
  const start = new Date(`${sd}T${st}:00`);
  const end = new Date(`${ed}T${et}:00`);
  if (isNaN(start) || isNaN(end)) throw new Error('That date or time is not valid.');
  if (end <= start) throw new Error('The event has to end after it starts.');

  return { ...common, start_utc: toApi(start), end_utc: toApi(end) };
}

async function save(e) {
  e.preventDefault();
  if (state.busy) return;
  let data;
  try {
    data = readForm();
  } catch (err) {
    const box = $('formError');
    box.textContent = err.message;
    box.hidden = false;
    return;
  }
  state.busy = true;
  $('saveBtn').disabled = true;
  try {
    if (state.editing) await API.update(state.editing, data);
    else await API.create(data);
    closeEditor();
    await load();
    toast('Saved');
  } catch (err) {
    const box = $('formError');
    box.textContent = err.message;
    box.hidden = false;
  } finally {
    state.busy = false;
    $('saveBtn').disabled = false;
  }
}

async function removeCurrent() {
  if (!state.editing) return;
  try {
    await API.remove(state.editing);
    closeEditor();
    await load();
    toast('Deleted');
  } catch (err) {
    toast(err.message, true);
  }
}

// ---------------------------------------------------------------- toast

let toastTimer;
function toast(msg, isErr = false) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.toggle('err', isErr);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, isErr ? 5000 : 1800);
}

// ---------------------------------------------------------------- swipe

function initSwipe() {
  const stage = $('stage');
  let x0 = null, y0 = null, t0 = 0;

  stage.addEventListener('pointerdown', e => {
    if (e.target.closest('.ev, .chip, .sheet')) return;
    x0 = e.clientX; y0 = e.clientY; t0 = Date.now();
  }, { passive: true });

  stage.addEventListener('pointerup', e => {
    if (x0 === null) return;
    const dx = e.clientX - x0, dy = e.clientY - y0, dt = Date.now() - t0;
    x0 = null;
    // Horizontal, decisive, and not a vertical scroll of the time grid.
    if (dt < 700 && Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy) * 1.6) {
      step(dx < 0 ? 1 : -1);
    }
  }, { passive: true });

  stage.addEventListener('pointercancel', () => { x0 = null; }, { passive: true });
}

// ---------------------------------------------------------------- clock

function tickClock() {
  const now = new Date();
  $('clockTime').textContent = now.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  $('clockDate').textContent = now.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
}

// ---------------------------------------------------------------- boot

function init() {
  $('prevBtn').addEventListener('click', () => step(-1));
  $('nextBtn').addEventListener('click', () => step(1));
  $('todayBtn').addEventListener('click', () => { state.cursor = new Date(); load(); });
  document.querySelectorAll('.seg').forEach(b =>
    b.addEventListener('click', () => setView(b.dataset.view)));

  $('addBtn').addEventListener('click', () => openEditor(null));
  $('cancelBtn').addEventListener('click', closeEditor);
  $('deleteBtn').addEventListener('click', removeCurrent);
  $('eventForm').addEventListener('submit', save);
  $('fAllDay').addEventListener('change', syncAllDay);

  // Tap the dimmed area to dismiss, but not taps inside the sheet.
  $('sheetBackdrop').addEventListener('click', e => {
    if (e.target === $('sheetBackdrop')) closeEditor();
  });

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeEditor();
    else if (e.key === 'ArrowLeft' && $('sheetBackdrop').hidden) step(-1);
    else if (e.key === 'ArrowRight' && $('sheetBackdrop').hidden) step(1);
  });

  // A wall display gets no clicks for days; keep it honest on its own.
  tickClock();
  setInterval(tickClock, 10000);
  setInterval(() => { if ($('sheetBackdrop').hidden) load(); }, 60000);

  initSwipe();
  load();
}

document.addEventListener('DOMContentLoaded', init);
