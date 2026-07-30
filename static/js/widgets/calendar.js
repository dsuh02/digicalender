/* Calendar widgets: month, week, day, agenda — plus the shared event editor.
 *
 * Each widget owns its own cursor date and its own compact header, so you can
 * put "this week" next to "next month" on the same page and navigate them
 * independently. They share the fetch/refresh contract and the editor.
 */

import { api, bus } from '../core/api.js';
import { close, openSheet, toast } from '../core/sheet.js';
import {
  DEFAULT_COLOR, DOW, EVENT_COLORS, MONTHS, addDays, addMonths, clear,
  dateOnly, dayKey, el, eventsForDay, fmtHour, fmtTime, fromApi, layoutColumns,
  relativeTime, sameDay, shiftKey, startOfDay, startOfWeek, toApi,
} from '../core/util.js';

/* ------------------------------------------------------------ event editor */

export function openEventEditor(ev, defaults = {}, onSaved) {
  const allDay0 = ev ? ev.all_day : !!defaults.allDay;
  let start, end;
  if (ev) {
    start = fromApi(ev.start_utc);
    end = fromApi(ev.end_utc);
  } else if (defaults.at) {
    start = defaults.at;
    end = new Date(start.getTime() + 3600000);
  } else {
    const base = defaults.day || new Date();
    const now = new Date();
    start = new Date(base.getFullYear(), base.getMonth(), base.getDate(),
                     now.getHours(), now.getMinutes() < 30 ? 30 : 0);
    if (now.getMinutes() >= 30) start.setHours(start.getHours() + 1);
    end = new Date(start.getTime() + 3600000);
  }

  let color = ev?.color || DEFAULT_COLOR;
  const f = {
    title: el('input.input', { type: 'text', maxlength: 500, placeholder: "What's happening?" }),
    allDay: el('input.switch-input', { type: 'checkbox' }),
    sd: el('input.input', { type: 'date' }), st: el('input.input', { type: 'time', step: 300 }),
    ed: el('input.input', { type: 'date' }), et: el('input.input', { type: 'time', step: 300 }),
    loc: el('input.input', { type: 'text', maxlength: 500, placeholder: 'Optional' }),
    notes: el('textarea.input', { rows: 2, maxlength: 4000, placeholder: 'Optional' }),
  };
  f.title.value = ev?.title || '';
  f.loc.value = ev?.location || '';
  f.notes.value = ev?.description || '';
  f.allDay.checked = allDay0;
  const hhmm = d => `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  if (allDay0 && ev) {
    f.sd.value = dateOnly(ev.start_utc);
    f.ed.value = shiftKey(dateOnly(ev.end_utc), -1);
  } else {
    f.sd.value = dayKey(start);
    f.ed.value = dayKey(end);
  }
  f.st.value = hhmm(start);
  f.et.value = hhmm(end);

  const swatches = el('div.swatches');
  const paint = () => [...swatches.children].forEach(b =>
    b.setAttribute('aria-pressed', String(b.dataset.value === color)));
  EVENT_COLORS.forEach(c => swatches.append(el('button.swatch', {
    type: 'button', dataset: { value: c.value }, style: { background: c.value },
    'aria-label': c.name, onclick: () => { color = c.value; paint(); },
  })));
  paint();

  const timeCells = [
    el('label.field.timeonly', {}, [el('span.field-label', { text: ' ' }), f.st]),
    el('label.field.timeonly', {}, [el('span.field-label', { text: ' ' }), f.et]),
  ];
  const syncAllDay = () => timeCells.forEach(c =>
    c.style.visibility = f.allDay.checked ? 'hidden' : 'visible');
  f.allDay.addEventListener('change', syncAllDay);

  const err = el('p.form-error', { hidden: true });
  const body = el('div.form', {}, [
    el('label.field', {}, [el('span.field-label', { text: 'Title' }), f.title]),
    el('label.row-toggle', {}, [
      el('span.field-label', { text: 'All day' }), f.allDay, el('span.switch'),
    ]),
    el('div.grid2', {}, [
      el('label.field', {}, [el('span.field-label', { text: 'Starts' }), f.sd]),
      timeCells[0],
      el('label.field', {}, [el('span.field-label', { text: 'Ends' }), f.ed]),
      timeCells[1],
    ]),
    el('label.field', {}, [el('span.field-label', { text: 'Location' }), f.loc]),
    el('label.field', {}, [el('span.field-label', { text: 'Notes' }), f.notes]),
    el('div.field', {}, [el('span.field-label', { text: 'Colour' }), swatches]),
    err,
  ]);
  syncAllDay();

  const read = () => {
    const title = f.title.value.trim();
    if (!title) throw new Error('Give the event a title.');
    if (!f.sd.value || !f.ed.value) throw new Error('Pick a start and end date.');
    const common = {
      title, location: f.loc.value.trim(), description: f.notes.value.trim(),
      all_day: f.allDay.checked, color,
    };
    if (f.allDay.checked) {
      // Floating dates: emitted verbatim, never run through a timezone.
      if (f.ed.value < f.sd.value) throw new Error('The event has to end on or after it starts.');
      return { ...common, start_utc: `${f.sd.value}T00:00:00Z`,
               end_utc: `${shiftKey(f.ed.value, 1)}T00:00:00Z` };
    }
    const s = new Date(`${f.sd.value}T${f.st.value || '00:00'}:00`);
    const e2 = new Date(`${f.ed.value}T${f.et.value || '00:00'}:00`);
    if (isNaN(s) || isNaN(e2)) throw new Error('That date or time is not valid.');
    if (e2 <= s) throw new Error('The event has to end after it starts.');
    return { ...common, start_utc: toApi(s), end_utc: toApi(e2) };
  };

  const actions = [];
  if (ev) {
    actions.push({
      label: 'Delete', kind: 'danger', onClick: async () => {
        close();
        try { await api.deleteEvent(ev.uid); onSaved?.(); toast('Deleted'); }
        catch (e) { toast(e.message, true); }
      },
    });
  }
  actions.push({ label: 'Cancel', onClick: close });
  actions.push({
    label: 'Save', kind: 'primary', onClick: async () => {
      let data;
      try { data = read(); }
      catch (e) { err.textContent = e.message; err.hidden = false; return; }
      try {
        if (ev) await api.updateEvent(ev.uid, data);
        else await api.createEvent(data);
        close();
        onSaved?.();
        toast('Saved');
      } catch (e) { err.textContent = e.message; err.hidden = false; }
    },
  });

  openSheet({ title: ev ? 'Edit event' : 'New event', body, actions });
  if (!ev) setTimeout(() => f.title.focus(), 60);
}

/* -------------------------------------------------------------- scaffolding */

/** Shared shell: compact header + a body the view fills. */
function calendarShell(host, ctx, { label, onPrev, onNext, onToday, onAdd }) {
  const title = el('span.cw-title');
  const head = el('header.cw-head', {}, [
    el('button.cw-nav', { text: '‹', 'aria-label': 'Previous', onclick: onPrev }),
    el('button.cw-today', { text: 'Today', onclick: onToday }),
    el('button.cw-nav', { text: '›', 'aria-label': 'Next', onclick: onNext }),
    title,
    onAdd ? el('button.cw-add', { text: '+', 'aria-label': 'New event', onclick: onAdd }) : null,
  ]);
  const body = el('div.cw-body');
  host.append(head, body);
  return { body, setLabel: t => { title.textContent = t; } };
}

/** Wire the refresh contract: fetch on demand, and whenever events change. */
function liveCalendar(render) {
  return (host, ctx) => {
    let cursor = new Date();
    let events = [];
    const state = { get cursor() { return cursor; }, set cursor(v) { cursor = v; } };

    const load = async () => {
      const [s, e] = render.range(cursor, ctx.settings);
      try {
        // Pad a day each way: the server filters on UTC instants, so an all-day
        // event at the edge can fall outside the window by the panel's offset.
        events = await api.events(addDays(s, -1), addDays(e, 1));
      } catch { events = []; }
      draw();
    };
    const draw = () => {
      clear(host);
      const shell = calendarShell(host, ctx, {
        label: '',
        onPrev: () => { cursor = render.step(cursor, -1, ctx.settings); load(); },
        onNext: () => { cursor = render.step(cursor, 1, ctx.settings); load(); },
        onToday: () => { cursor = new Date(); load(); },
        onAdd: () => openEventEditor(null, { day: cursor }, load),
      });
      shell.setLabel(render.label(cursor, ctx.settings));
      render.draw(shell.body, { cursor, events, settings: ctx.settings, reload: load });
    };

    const off = bus.on('events_changed', load);
    load();
    return { refresh: load, destroy: off };
  };
}

const chip = (ev, reload) => {
  // JS hands CSS a colour and nothing else; how that colour is expressed
  // (a rule, a dot, a fill) is a styling decision, not a data one.
  const node = el('div.chip', {
    style: { '--c': ev.color || DEFAULT_COLOR },
    onclick: e => { e.stopPropagation(); openEventEditor(ev, {}, reload); },
  });
  if (!ev.all_day) node.append(el('span.t', { text: fmtTime(fromApi(ev.start_utc)) }));
  node.append(document.createTextNode(ev.title));
  return node;
};

/* -------------------------------------------------------------------- month */

export const MonthWidget = {
  type: 'month', name: 'Month calendar', icon: 'calendar', category: 'Calendar',
  defaultSize: { w: 24, h: 18 }, minSize: { w: 12, h: 10 },
  settings: [
    { key: 'weekStart', label: 'Week starts on', type: 'select', default: '0',
      options: [{ value: '0', label: 'Sunday' }, { value: '1', label: 'Monday' }] },
    { key: 'maxChips', label: 'Events shown per day', type: 'slider', min: 1, max: 8, default: 4 },
    { key: 'showOther', label: 'Show neighbouring months', type: 'toggle', default: true },
  ],
  render: liveCalendar({
    range: (c, s) => {
      const first = new Date(c.getFullYear(), c.getMonth(), 1);
      const g = startOfWeek(first, Number(s.weekStart || 0));
      return [g, addDays(g, 42)];
    },
    step: (c, d) => addMonths(c, d),
    label: c => `${MONTHS[c.getMonth()]} ${c.getFullYear()}`,
    draw(body, { cursor, events, settings, reload }) {
      const first = Number(settings.weekStart || 0);
      const maxChips = Number(settings.maxChips || 4);
      const gridStart = startOfWeek(new Date(cursor.getFullYear(), cursor.getMonth(), 1), first);
      const today = new Date();

      const dow = el('div.dow-row');
      for (let i = 0; i < 7; i++) dow.append(el('div', { text: DOW[(i + first) % 7] }));
      const grid = el('div.month-grid');

      for (let i = 0; i < 42; i++) {
        const day = addDays(gridStart, i);
        const other = day.getMonth() !== cursor.getMonth();
        if (other && settings.showOther === false) { grid.append(el('div.mcell.blank')); continue; }
        const cell = el('div.mcell' + (other ? '.other' : '') + (sameDay(day, today) ? '.today' : ''), {
          onclick: () => openEventEditor(null, { day }, reload),
        });
        cell.append(el('div.mnum', { text: day.getDate() }));
        const box = el('div.mevents');
        const { allDay, timed } = eventsForDay(events, day);
        const all = [...allDay, ...timed];
        all.slice(0, maxChips).forEach(ev => box.append(chip(ev, reload)));
        if (all.length > maxChips) box.append(el('div.more', { text: `+${all.length - maxChips} more` }));
        cell.append(box);
        grid.append(cell);
      }
      body.append(dow, grid);
    },
  }),
};

/* --------------------------------------------------------------- time grid */

function drawTimeGrid(body, { cursor, events, settings, reload }, nDays, firstDay) {
  const hourH = Number(settings.hourHeight || 52);
  const startHour = Number(settings.startHour ?? 0);
  const endHour = Number(settings.endHour ?? 24);
  const hours = Math.max(1, endHour - startHour);
  const rangeStart = nDays === 7 ? startOfWeek(cursor, firstDay) : startOfDay(cursor);
  const today = new Date();
  const cols = `repeat(${nDays}, 1fr)`;

  const head = el('div.tv-days', { style: { gridTemplateColumns: cols } });
  const allDayRow = el('div.tv-days', { style: { gridTemplateColumns: cols } });
  const canvas = el('div.tv-days.tv-canvas', { style: { gridTemplateColumns: cols } });
  const gutter = el('div.tv-gutter');
  for (let h = startHour; h < endHour; h++) {
    gutter.append(el('div.hour-label', { text: h === startHour ? '' : fmtHour(h), style: { height: hourH + 'px' } }));
  }

  for (let i = 0; i < nDays; i++) {
    const day = addDays(rangeStart, i);
    const isToday = sameDay(day, today);
    head.append(el('div' + (isToday ? '.is-today' : ''), {
      onclick: () => openEventEditor(null, { day }, reload),
    }, [
      el('div.tv-dow', { text: DOW[day.getDay()] }),
      el('div.tv-date', { text: day.getDate() }),
    ]));

    const { allDay, timed } = eventsForDay(events, day);
    const ad = el('div', { onclick: () => openEventEditor(null, { day, allDay: true }, reload) });
    allDay.forEach(ev => ad.append(chip(ev, reload)));
    allDayRow.append(ad);

    const col = el('div.col');
    for (let h = startHour; h < endHour; h++) {
      col.append(el('div.hour-line', { style: { height: hourH + 'px' } }));
    }
    col.addEventListener('click', e => {
      if (e.target.closest('.ev')) return;
      const y = e.clientY - col.getBoundingClientRect().top;
      const mins = Math.max(0, Math.floor(y / hourH * 2) * 30) + startHour * 60;
      const at = new Date(day.getFullYear(), day.getMonth(), day.getDate(),
                          Math.floor(mins / 60), mins % 60);
      openEventEditor(null, { at }, reload);
    });

    const dayStart = startOfDay(day).getTime() + startHour * 3600000;
    layoutColumns(timed).forEach(({ ev, s, e, col: ci, cols: nc }) => {
      const top = Math.max(0, (s - dayStart) / 3600000) * hourH;
      const bottom = Math.min(hours * 3600000, e - dayStart) / 3600000 * hourH;
      const h = Math.max(20, bottom - top - 2);
      const node = el('div.ev' + (h < 40 ? '.ev-compact' : ''), {
        style: {
          top: top + 'px', height: h + 'px',
          left: `calc(${(ci / nc) * 100}% + 2px)`, width: `calc(${(1 / nc) * 100}% - 4px)`,
          '--c': ev.color || DEFAULT_COLOR,
        },
        onclick: e2 => { e2.stopPropagation(); openEventEditor(ev, {}, reload); },
      }, [
        el('div.ev-title', { text: ev.title }),
        el('div.ev-time', { text: `${fmtTime(fromApi(ev.start_utc))} – ${fmtTime(fromApi(ev.end_utc))}` }),
      ]);
      col.append(node);
    });

    if (isToday && settings.showNow !== false) {
      const now = new Date();
      const mins = now.getHours() * 60 + now.getMinutes() - startHour * 60;
      if (mins >= 0 && mins <= hours * 60) {
        col.append(el('div.now-line', { style: { top: (mins / 60) * hourH + 'px' } }));
      }
    }
    canvas.append(col);
  }

  const scroll = el('div.tv-scroll', {}, [el('div.tv-body', {}, [gutter, canvas])]);
  body.append(
    el('div.tv-head', {}, [el('div.tv-gutter'), head]),
    el('div.tv-allday', {}, [el('div.tv-gutter.tv-allday-label', { text: 'all-day' }), allDayRow]),
    scroll,
  );
  // Open on the working day rather than midnight.
  requestAnimationFrame(() => {
    scroll.scrollTop = Math.max(0, (8 - startHour) * hourH);
  });
}

const TIME_SETTINGS = [
  { key: 'startHour', label: 'Day starts at', type: 'slider', min: 0, max: 12, default: 0 },
  { key: 'endHour', label: 'Day ends at', type: 'slider', min: 13, max: 24, default: 24 },
  { key: 'hourHeight', label: 'Hour height (px)', type: 'slider', min: 28, max: 120, default: 52 },
  { key: 'showNow', label: 'Show current-time line', type: 'toggle', default: true },
];

export const WeekWidget = {
  type: 'week', name: 'Week calendar', icon: 'calendar', category: 'Calendar',
  defaultSize: { w: 28, h: 20 }, minSize: { w: 14, h: 10 },
  settings: [
    { key: 'weekStart', label: 'Week starts on', type: 'select', default: '0',
      options: [{ value: '0', label: 'Sunday' }, { value: '1', label: 'Monday' }] },
    ...TIME_SETTINGS,
  ],
  render: liveCalendar({
    range: (c, s) => {
      const st = startOfWeek(c, Number(s.weekStart || 0));
      return [st, addDays(st, 7)];
    },
    step: (c, d) => addDays(c, 7 * d),
    label: (c, s) => {
      const st = startOfWeek(c, Number(s.weekStart || 0)), e = addDays(st, 6);
      const sm = MONTHS[st.getMonth()].slice(0, 3), em = MONTHS[e.getMonth()].slice(0, 3);
      return st.getMonth() === e.getMonth()
        ? `${sm} ${st.getDate()}–${e.getDate()}`
        : `${sm} ${st.getDate()} – ${em} ${e.getDate()}`;
    },
    draw: (body, ctx) => drawTimeGrid(body, ctx, 7, Number(ctx.settings.weekStart || 0)),
  }),
};

export const DayWidget = {
  type: 'day', name: 'Day calendar', icon: 'calendar', category: 'Calendar',
  defaultSize: { w: 12, h: 20 }, minSize: { w: 6, h: 10 },
  settings: TIME_SETTINGS,
  render: liveCalendar({
    range: c => [startOfDay(c), addDays(startOfDay(c), 1)],
    step: (c, d) => addDays(c, d),
    label: c => c.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' }),
    draw: (body, ctx) => drawTimeGrid(body, ctx, 1, 0),
  }),
};

/* ------------------------------------------------------------------ agenda */

export const AgendaWidget = {
  type: 'agenda', name: 'Up next', icon: 'list', category: 'Calendar',
  defaultSize: { w: 12, h: 14 }, minSize: { w: 6, h: 6 },
  settings: [
    { key: 'days', label: 'Days ahead', type: 'slider', min: 1, max: 30, default: 7 },
    { key: 'max', label: 'Maximum items', type: 'slider', min: 3, max: 40, default: 12 },
    { key: 'groupByDay', label: 'Group by day', type: 'toggle', default: true },
  ],
  render(host, ctx) {
    let events = [];
    const body = el('div.agenda');
    host.append(body);

    const load = async () => {
      const now = new Date();
      const days = Number(ctx.settings.days || 7);
      try { events = await api.events(addDays(now, -1), addDays(now, days + 1)); }
      catch { events = []; }
      draw();
    };

    const draw = () => {
      clear(body);
      const now = new Date();
      const max = Number(ctx.settings.max || 12);
      const horizon = addDays(startOfDay(now), Number(ctx.settings.days || 7));
      const upcoming = events
        .filter(ev => (ev.all_day
          ? dateOnly(ev.end_utc) > dayKey(now)
          : fromApi(ev.end_utc) > now))
        .filter(ev => (ev.all_day ? true : fromApi(ev.start_utc) < horizon))
        .sort((a, b) => {
          const av = a.all_day ? dateOnly(a.start_utc) : toApi(fromApi(a.start_utc)).slice(0, 10);
          const bv = b.all_day ? dateOnly(b.start_utc) : toApi(fromApi(b.start_utc)).slice(0, 10);
          if (av !== bv) return av < bv ? -1 : 1;
          if (a.all_day !== b.all_day) return a.all_day ? -1 : 1;
          return fromApi(a.start_utc) - fromApi(b.start_utc);
        })
        .slice(0, max);

      if (!upcoming.length) {
        body.append(el('p.empty-hint', { text: 'Nothing coming up' }));
        return;
      }

      let lastKey = null;
      for (const ev of upcoming) {
        const day = ev.all_day ? new Date(`${dateOnly(ev.start_utc)}T00:00:00`) : fromApi(ev.start_utc);
        const k = dayKey(day);
        if (ctx.settings.groupByDay !== false && k !== lastKey) {
          lastKey = k;
          body.append(el('div.agenda-day', {
            text: sameDay(day, now) ? 'Today'
              : sameDay(day, addDays(now, 1)) ? 'Tomorrow'
              : day.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' }),
          }));
        }
        const rel = ev.all_day ? '' : relativeTime(fromApi(ev.start_utc), now);
        body.append(el('div.agenda-item', {
          style: { '--c': ev.color || DEFAULT_COLOR },
          onclick: () => openEventEditor(ev, {}, load),
        }, [
          el('div.agenda-when', { text: ev.all_day ? 'All day' : fmtTime(fromApi(ev.start_utc)) }),
          el('div.agenda-main', {}, [
            el('div.agenda-title', { text: ev.title }),
            ev.location ? el('div.agenda-loc', { text: ev.location }) : null,
          ]),
          rel ? el('div.agenda-rel', { text: rel }) : null,
        ]));
      }
    };

    const off = bus.on('events_changed', load);
    const timer = setInterval(draw, 60000);   // keeps "in 25 min" honest
    load();
    return { refresh: load, destroy: () => { off(); clearInterval(timer); } };
  },
};
