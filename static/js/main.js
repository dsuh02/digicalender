/* App shell: the pager, the auto-hiding top bar, edit mode, the widget
 * palette, device management and the theme editor.
 *
 * Interaction model:
 *   - Pages sit side by side in one horizontal track. A TWO-finger horizontal
 *     drag moves the track under your fingers and snaps to a page on release,
 *     tablet-style. One finger stays reserved for the widgets themselves.
 *   - The top bar is hidden. Pull down from the top edge and it slides in OVER
 *     the page; it leaves after 7 seconds, or the moment you tap the content
 *     below it. While editing it stays pinned — you need its buttons.
 *   - Edit mode is modal (the padlock): widgets are inert until unlocked, so a
 *     passing brush can't rearrange the wall.
 */

import { api, bus, connectStream, liveStates } from './core/api.js';
import { Grid } from './core/grid.js';
import { icon } from './core/icons.js';
import {
  buildForm, close, confirmSheet, openSheet, openWidgetSettings, toast,
} from './core/sheet.js';
import {
  DEFAULT_THEME, PRESETS, apply as applyTheme, eventPalette, getTheme,
  normalize as normalizeTheme, resolve as resolveTheme,
} from './core/theme.js';
import { clamp, clear, debounce, el } from './core/util.js';
import { initials } from './widgets/people.js';
import { CATEGORIES, WIDGETS, widgetDef } from './widgets/index.js';

const BAR_HIDE_MS = 7000;
const EDGE_PX = 32;          // how close to the top a pull-down must start

const state = {
  pages: [],                 // every page, unfiltered
  visiblePages: [],          // shared + the active person's
  people: [],
  activePerson: '',
  settings: {},
  pageIndex: 0,
  editing: false,
  grids: new Map(),          // page id -> Grid
  instances: new Map(),      // widget id -> {destroy, refresh}
};

const dom = {};

/* ------------------------------------------------------------------- boot */

async function boot() {
  applyTheme(DEFAULT_THEME);           // paint something sane before data loads

  dom.topbar = document.getElementById('topbar');
  dom.tabs = document.getElementById('tabs');
  dom.stage = document.getElementById('stage');
  dom.pager = document.getElementById('pager');
  dom.dots = document.getElementById('dots');
  dom.editBtn = document.getElementById('editBtn');
  dom.addBtn = document.getElementById('addBtn');
  dom.settingsBtn = document.getElementById('settingsBtn');
  dom.reloadBtn = document.getElementById('reloadBtn');
  dom.status = document.getElementById('status');
  dom.pageBtn = document.getElementById('pageBtn');

  dom.editBtn.addEventListener('click', toggleEdit);
  dom.addBtn.addEventListener('click', openPalette);
  dom.settingsBtn.addEventListener('click', () => openSettings());
  // Cache-busted so a reload can never re-serve the bundle it is trying to
  // escape — this button exists precisely for when something looks wrong.
  dom.reloadBtn.addEventListener('click', () => {
    toast('Reloading…');
    setTimeout(() => {
      const u = new URL(location.href);
      u.searchParams.set('r', Date.now().toString(36));
      location.replace(u.toString());
    }, 150);
  });
  dom.pageBtn.addEventListener('click', openPageManager);
  window._openSettings = openSettings;   // calendar widgets jump to the Calendars tab
  window._activePerson = () => state.activePerson;   // people widgets read/switch
  window._setActivePerson = setActivePerson;

  bus.on('connected', ok => {
    dom.status.classList.toggle('bad', !ok);
    dom.status.title = ok ? 'Live' : 'Reconnecting…';
  });
  bus.on('layout_changed', () => { if (!state.editing) reload(); });
  bus.on('people_changed', async () => {
    try { state.people = await api.people(); } catch { /* keep what we have */ }
  });

  await reload();
  connectStream();
  applyNightDim();
  setInterval(applyNightDim, 60000);
  initPager();
  initTopBar();
  initIdleDim();
  initTouchFeedback();
  initScrollbars();
  initFourFingerTap();
}

async function reload() {
  try {
    const [data, people] = await Promise.all([api.dashboard(), api.people().catch(() => [])]);
    state.pages = data.pages || [];
    state.settings = data.settings || {};
    state.people = people || [];
  } catch (e) {
    toast(`Could not load the dashboard: ${e.message}`, true);
    return;
  }
  state.activePerson = state.settings.active_person || '';
  // A profile that was deleted must not leave the panel showing nothing.
  if (state.activePerson && !state.people.some(p => p.id === state.activePerson)) {
    state.activePerson = '';
  }
  applySavedTheme();
  computeVisiblePages();
  renderTabs();
  renderAllPages();
}

/** Shared pages, plus the active person's own. */
function computeVisiblePages() {
  state.visiblePages = state.pages.filter(
    p => !p.person_id || p.person_id === state.activePerson);
  state.pageIndex = clamp(state.pageIndex, 0, Math.max(0, state.visiblePages.length - 1));
}

function applySavedTheme() {
  // A person's theme overrides the household one while they're active, so the
  // panel looks like theirs the moment they tap in.
  const person = state.people.find(p => p.id === state.activePerson);
  if (person && person.theme) {
    applyTheme({ ...DEFAULT_THEME, ...person.theme });
    return;
  }
  try {
    const raw = state.settings.theme;
    applyTheme(raw ? { ...DEFAULT_THEME, ...JSON.parse(raw) } : DEFAULT_THEME);
  } catch {
    applyTheme(DEFAULT_THEME);
  }
}

async function setActivePerson(id) {
  if (state.activePerson === id) return;
  state.activePerson = id || '';
  state.pageIndex = 0;
  applySavedTheme();
  computeVisiblePages();
  renderTabs();
  renderAllPages();
  bus.emit('active_person', state.activePerson);
  try { state.settings = await api.saveSettings({ active_person: state.activePerson }); }
  catch { /* the switch already happened on screen; persistence is a bonus */ }
}

/* ------------------------------------------------------------------ pages */

function renderTabs() {
  clear(dom.tabs);
  state.visiblePages.forEach((p, i) => {
    dom.tabs.append(el('button.tab', {
      text: p.name,
      'aria-selected': String(i === state.pageIndex),
      onclick: () => setPage(i, true),
    }));
  });
}

function renderDots() {
  clear(dom.dots);
  if (state.visiblePages.length < 2) return;
  state.visiblePages.forEach((_, i) => {
    dom.dots.append(el('span.dot' + (i === state.pageIndex ? '.on' : '')));
  });
}

function teardownWidgets() {
  for (const inst of state.instances.values()) {
    try { inst.destroy?.(); } catch { /* one bad widget must not block the rest */ }
  }
  state.instances.clear();
  state.grids.clear();
}

/**
 * Every page is mounted at once so a swipe reveals real content, not a
 * skeleton. A handful of pages of widgets is well within budget; widgets that
 * poll do so on timers measured in minutes.
 */
function renderAllPages() {
  teardownWidgets();
  clear(dom.pager);

  const saveLayout = debounce(async (layout) => {
    try { await api.saveLayout(layout); } catch (e) { toast(e.message, true); }
  }, 400);

  for (const page of state.visiblePages) {
    const pageEl = el('div.page');
    const host = el('div.grid-host');
    pageEl.append(host);
    dom.pager.append(pageEl);

    const grid = new Grid(host, page, {
      onChange: saveLayout,
      onEdit: w => editWidget(w),
      onDelete: w => confirmSheet('Remove widget?',
        `“${widgetDef(w.type)?.name || w.type}” will be removed from this page.`,
        async () => {
          try {
            await api.deleteWidget(w.id);
            state.instances.get(w.id)?.destroy?.();
            state.instances.delete(w.id);
            grid.remove(w.id);
            page.widgets = page.widgets.filter(x => x.id !== w.id);
          } catch (e) { toast(e.message, true); }
        }),
    });
    grid.setEditing(state.editing);
    state.grids.set(page.id, grid);
    for (const w of page.widgets) mountWidget(grid, w);
  }

  renderDots();
  setPage(state.pageIndex, false);
}

function mountWidget(grid, w) {
  const def = widgetDef(w.type);
  const content = el('div.w-content');
  grid.add(w, content);
  if (!def) {
    content.append(el('p.empty-hint', { text: `Unknown widget: ${w.type}` }));
    return;
  }
  try {
    state.instances.set(w.id, def.render(content, { widget: w, settings: w.settings || {}, bus }) || {});
  } catch (e) {
    console.error(`widget ${w.type} failed`, e);
    content.append(el('p.empty-hint', { text: `${def.name} failed to load` }));
  }
}

function remountWidget(w) {
  const grid = state.grids.get(w.page_id);
  if (!grid) return;
  state.instances.get(w.id)?.destroy?.();
  state.instances.delete(w.id);
  grid.remove(w.id);
  mountWidget(grid, w);
}

function currentPage() {
  return state.visiblePages[state.pageIndex];
}

function currentGrid() {
  const p = currentPage();
  return p ? state.grids.get(p.id) : null;
}

function setPage(i, animate) {
  state.pageIndex = clamp(i, 0, Math.max(0, state.visiblePages.length - 1));
  dom.pager.classList.toggle('animating', !!animate);
  dom.pager.classList.remove('dragging');
  dom.pager.style.transform = `translateX(${-state.pageIndex * 100}%)`;
  renderTabs();
  renderDots();
}

/* ------------------------------------------------- two-finger page swiping */

function initPager() {
  const pts = new Map();     // pointerId -> {x, y}
  let drag = null;

  const avgX = () => {
    let s = 0;
    for (const p of pts.values()) s += p.x;
    return s / pts.size;
  };

  // Same native-gesture problem as the bar pull: once two fingers move, the
  // browser tries to claim a pan/zoom and pointercancels our drag. While a
  // page drag is live, the touch sequence is ours.
  dom.stage.addEventListener('touchmove', e => {
    if (drag) e.preventDefault();
  }, { passive: false, capture: true });

  dom.stage.addEventListener('pointerdown', e => {
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    // A three- or four-finger gesture passes through "two" on its way down;
    // abandon the page drag rather than letting the track lurch sideways.
    if (pts.size > 2 && drag) {
      drag = null;
      dom.pager.classList.remove('dragging');
      setPage(state.pageIndex, true);
    }
    // Two fingers anywhere on the stage picks the track up — like a tablet.
    if (pts.size === 2 && !state.editing && !drag) {
      drag = {
        start: avgX(),
        last: avgX(),
        lastT: performance.now(),
        v: 0,
        base: -state.pageIndex * dom.stage.clientWidth,
      };
      dom.pager.classList.remove('animating');
      dom.pager.classList.add('dragging');
    }
  }, true);

  dom.stage.addEventListener('pointermove', e => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (!drag || pts.size < 2) return;

    const now = performance.now();
    const a = avgX();
    drag.v = (a - drag.last) / Math.max(1, now - drag.lastT);   // px per ms
    drag.last = a;
    drag.lastT = now;

    let off = drag.base + (a - drag.start);
    // Rubber-band past the ends rather than stopping dead.
    const min = -(state.visiblePages.length - 1) * dom.stage.clientWidth;
    if (off > 0) off *= 0.30;
    if (off < min) off = min + (off - min) * 0.30;
    dom.pager.style.transform = `translateX(${off}px)`;
  }, true);

  const release = e => {
    pts.delete(e.pointerId);
    if (!drag || pts.size >= 2) return;

    const w = dom.stage.clientWidth;
    const moved = drag.last - drag.start;
    const off = drag.base + moved;
    // Nearest page wins; a decisive flick advances even on a short travel.
    let idx = Math.round(-off / w);
    if (idx === state.pageIndex && Math.abs(drag.v) > 0.35 && Math.abs(moved) > 24) {
      idx += drag.v < 0 ? 1 : -1;
    }
    drag = null;
    setPage(idx, true);
  };
  dom.stage.addEventListener('pointerup', release, true);
  dom.stage.addEventListener('pointercancel', release, true);
}

/* --------------------------------------------------------- auto-hiding bar */

function initTopBar() {
  const bar = dom.topbar;
  let hideTimer = null;
  let pull = null;
  const barH = () => bar.offsetHeight || 62;

  // Edit mode does NOT pin the bar: it overlays the page, so pinning it hides
  // exactly the top-row widgets you unlocked to edit. Edit state and bar
  // visibility are independent — the lock button is a pure toggle, and the bar
  // hides on the same timer/tap rules whether you're editing or not. Pull it
  // down again whenever you need its buttons.
  const arm = () => {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideBar, BAR_HIDE_MS);
  };
  window._showBar = showBar;   // toggleEdit re-arms through this

  function showBar() {
    bar.classList.add('shown');
    document.body.classList.add('bar-open');
    arm();
  }
  function hideBar() {
    clearTimeout(hideTimer);
    bar.classList.remove('shown');
    document.body.classList.remove('bar-open');
  }
  window._hideBarNow = hideBar;

  // Any interaction with the bar itself buys another 7 seconds.
  bar.addEventListener('pointerdown', arm);

  const beginPull = (e) => {
    if (pull || bar.classList.contains('shown')) return;
    pull = { id: e.pointerId, y0: e.clientY, dy: 0, t0: Date.now() };
    bar.classList.add('dragging');
  };
  const movePull = (e) => {
    if (!pull || e.pointerId !== pull.id) return;
    // The bar rides the finger down, exactly like a notification shade.
    pull.dy = Math.max(0, e.clientY - pull.y0);
    bar.style.transform = `translateY(${Math.min(0, -barH() + pull.dy)}px)`;
  };
  const endPull = (e) => {
    if (!pull || e.pointerId !== pull.id) return;
    // On pointercancel e.clientY can be stale — trust the last move we saw.
    const dy = Math.max(pull.dy || 0, e.type === 'pointerup' ? e.clientY - pull.y0 : 0);
    const p = pull;
    pull = null;
    bar.classList.remove('dragging');
    bar.style.transform = '';
    // A third of the bar is intent enough; 45% felt like it needed convincing.
    if (dy > barH() * 0.33) showBar();
    else {
      bar.classList.remove('shown');
      document.body.classList.remove('bar-open');
      // A touch that landed on the strip but never moved was a TAP meant for
      // whatever sits underneath — pass it on instead of eating it.
      if (e.currentTarget === strip && dy < 8 && Date.now() - p.t0 < 400) {
        strip.style.pointerEvents = 'none';
        document.elementFromPoint(e.clientX, e.clientY)?.click();
        strip.style.pointerEvents = '';
      }
    }
  };

  // Primary path: the edge strip. Its touch-action:none guarantees the browser
  // never claims the gesture, and pointer capture keeps every move coming to
  // it for the whole pull, wherever the finger wanders.
  const strip = document.getElementById('edgeCatch');
  strip.addEventListener('pointerdown', e => {
    try { strip.setPointerCapture(e.pointerId); } catch { /* mouse on old UA */ }
    beginPull(e);
  });
  strip.addEventListener('pointermove', movePull);
  strip.addEventListener('pointerup', endPull);
  strip.addEventListener('pointercancel', endPull);

  // Secondary path: pointers that start on the stage inside the edge band
  // (mice, tests, and any touch the strip missed).
  dom.stage.addEventListener('pointerdown', e => {
    if (bar.classList.contains('shown')) {
      // A tap on the content below the header dismisses it.
      hideBar();
      return;
    }
    // In edit mode a drag that starts on a widget's grip/chrome near the top
    // is a widget drag, not a request for the bar.
    if (e.clientY <= EDGE_PX &&
        !e.target.closest('.w-grip, .w-chrome, .w-resize')) {
      beginPull(e);
    }
  }, true);

  // Belt for the stage path on real touch: a downward drag gets claimed by the
  // browser as a scroll after ~15px of slop, which pointercancels us halfway.
  // Cancelling the native touchmove while a pull is live keeps the gesture
  // ours for its whole length.
  dom.stage.addEventListener('touchmove', e => {
    if (pull) e.preventDefault();
  }, { passive: false, capture: true });

  dom.stage.addEventListener('pointermove', movePull, true);
  dom.stage.addEventListener('pointerup', endPull, true);
  dom.stage.addEventListener('pointercancel', endPull, true);
}

/* -------------------------------------------------------------- edit mode */

function toggleEdit() {
  state.editing = !state.editing;
  for (const g of state.grids.values()) g.setEditing(state.editing);
  document.body.classList.toggle('editing', state.editing);
  clear(dom.editBtn);
  dom.editBtn.append(icon(state.editing ? 'unlock' : 'lock', 22));
  dom.editBtn.setAttribute('aria-pressed', String(state.editing));
  dom.addBtn.hidden = !state.editing;
  dom.pageBtn.hidden = !state.editing;
  window._showBar?.();                   // keep the bar up briefly, timer running
  if (state.editing) {
    toast('Edit mode — drag to move, resize from the corner. Pull down for the bar.');
  }
}

async function editWidget(w) {
  const def = widgetDef(w.type);
  if (!def) return;
  await openWidgetSettings(def, w, async (values) => {
    try {
      const updated = await api.updateWidget(w.id, { settings: values });
      Object.assign(w, { settings: updated.settings });
      remountWidget(w);
    } catch (e) { toast(e.message, true); }
  });
}

function openPalette() {
  const page = currentPage();
  const grid = currentGrid();
  if (!page || !grid) return;
  const body = el('div.palette');
  for (const cat of CATEGORIES) {
    body.append(el('h3.form-section', { text: cat }));
    const row = el('div.palette-row');
    WIDGETS.filter(w => w.category === cat).forEach(def => {
      row.append(el('button.palette-item', {
        onclick: async () => {
          close();
          const size = def.defaultSize || { w: 12, h: 10 };
          const slot = grid.findSlot(size.w, size.h, def.minSize);
          if (!slot) {
            toast('This page is full — free some space or add a new page', true);
            return;
          }
          if (slot.w < size.w || slot.h < size.h) {
            toast(`Placed smaller (${slot.w}×${slot.h} cells) to fit the space left`);
          }
          try {
            const w = await api.createWidget({
              page_id: page.id, type: def.type,
              x: slot.x, y: slot.y, w: slot.w, h: slot.h, settings: {},
            });
            page.widgets.push(w);
            mountWidget(grid, w);
            if (!state.editing) toggleEdit();
          } catch (e) { toast(e.message, true); }
        },
      }, [icon(def.icon, 26), el('span', { text: def.name })]));
    });
    body.append(row);
  }
  openSheet({ title: 'Add a widget', body, wide: true, actions: [{ label: 'Close', onClick: close }] });
}

function openPageManager() {
  const page = currentPage();
  if (!page) return;
  const name = el('input.input', { type: 'text', value: page.name });
  const cols = el('input.input', { type: 'number', min: 8, max: 200, value: page.cols });
  const rows = el('input.input', { type: 'number', min: 8, max: 200, value: page.rows });

  // Who this page belongs to. Shared pages are always on screen; a person's
  // pages appear only while they're the active profile.
  const owner = el('select.input');
  owner.append(el('option', { value: '', text: 'Shared — everyone sees it' }));
  state.people.forEach(p => {
    const o = el('option', { value: p.id, text: `${p.name} only` });
    if (p.id === (page.person_id || '')) o.selected = true;
    owner.append(o);
  });

  const body = el('div.form', {}, [
    el('label.field', {}, [el('span.field-label', { text: 'Page name' }), name]),
    el('label.field', {}, [
      el('span.field-label', { text: 'Belongs to' }), owner,
      el('span.field-help', {
        text: state.people.length
          ? 'A person’s page shows only while their profile is active.'
          : 'Add household members under Settings › People to give pages an owner.',
      }),
    ]),
    el('div.grid2', {}, [
      el('label.field', {}, [el('span.field-label', { text: 'Grid columns' }), cols]),
      el('label.field', {}, [el('span.field-label', { text: 'Grid rows' }), rows]),
    ]),
    el('p.sheet-note', {
      text: 'More cells means finer positioning. Widgets keep their cell coordinates, so shrinking the grid can push things off-screen.',
    }),
    el('button.btn', {
      text: '+ New page',
      onclick: async () => {
        close();
        try {
          await api.createPage({ name: 'New page', position: state.pages.length });
          await reload();
          setPage(state.pages.length - 1, true);
        } catch (e) { toast(e.message, true); }
      },
    }),
  ]);

  const actions = [];
  if (state.pages.length > 1) {
    actions.push({
      label: 'Delete page', kind: 'danger', onClick: () => {
        close();
        confirmSheet('Delete this page?', `“${page.name}” and its widgets will be removed.`,
          async () => {
            try {
              await api.deletePage(page.id);
              state.pageIndex = 0;
              await reload();
            } catch (e) { toast(e.message, true); }
          });
      },
    });
  }
  actions.push({ label: 'Cancel', onClick: close });
  actions.push({
    label: 'Save', kind: 'primary', onClick: async () => {
      try {
        await api.updatePage(page.id, {
          name: name.value.trim() || 'Page',
          cols: Number(cols.value), rows: Number(rows.value),
          person_id: owner.value || null,
        });
        close();
        await reload();
      } catch (e) { toast(e.message, true); }
    },
  });
  openSheet({ title: 'Page settings', body, actions });
}

/* --------------------------------------------------------------- settings */

async function openSettings(initialTab = 'Theme') {
  const body = el('div');
  const tabs = el('div.subtabs');
  const panel = el('div.subpanel');
  body.append(tabs, panel);

  const views = { Theme: renderThemeTab, People: renderPeople,
                  Calendars: renderCalendars, Money: renderMoney,
                  Galleries: renderGalleries, Devices: renderDevices,
                  Display: renderDisplay };
  let active = views[initialTab] ? initialTab : 'Theme';
  const paint = async () => {
    clear(tabs);
    Object.keys(views).forEach(k => tabs.append(el('button.subtab', {
      text: k, 'aria-selected': String(k === active),
      onclick: async () => { active = k; await paint(); },
    })));
    clear(panel);
    panel.append(await views[active]());
  };
  await paint();
  openSheet({
    title: 'Settings', body, wide: true,
    actions: [{ label: 'Close', onClick: () => { close(); applySavedTheme(); } }],
  });
}

/* The theme editor. Everything applies LIVE as you drag — the whole panel is
   the preview — and only persists when you press Save. */
async function renderThemeTab() {
  let draft = getTheme();

  const wrap = el('div');

  // Presets: one tap to a known-good scheme, then tune from there.
  const presetRow = el('div.theme-presets');
  const paintPresets = () => {
    clear(presetRow);
    for (const p of PRESETS) {
      const v = resolveTheme(p.theme);
      const isCurrent = JSON.stringify(normalizeTheme(p.theme)) === JSON.stringify(draft);
      presetRow.append(el('button.preset', {
        'aria-pressed': String(isCurrent),
        onclick: () => { draft = normalizeTheme(p.theme); applyTheme(draft); refresh(); },
      }, [
        el('div.preset-chips', {}, [
          el('span.preset-chip', { style: { background: v['--bg'], borderColor: v['--line-strong'] } }),
          el('span.preset-chip', { style: { background: v['--primary'] } }),
          el('span.preset-chip', { style: { background: v['--secondary'] } }),
          el('span.preset-chip', { style: { background: v['--tertiary'] } }),
        ]),
        el('span', { text: p.name }),
      ]));
    }
  };

  const ROLES = [
    ['primary', 'Primary', 'Buttons, selection, today, toggles — everything you interact with'],
    ['secondary', 'Secondary', 'Devices and scenes — anything about the home being on'],
    ['tertiary', 'Tertiary', 'Reminders, precipitation, highlights — informational colour'],
  ];
  const DIALS = [
    ['intensity', 'Colour intensity', 0, 100, 'Left is monochrome, right is vivid'],
    ['brightness', 'Background brightness', 0, 100, 'Slide far right for a light theme'],
    ['tint', 'Neutral tint', 0, 359, 'The undertone of the greys'],
  ];

  const controls = el('div');
  const buildControls = () => {
    clear(controls);
    for (const [key, label, where] of ROLES) {
      const sw = el('span.role-swatch');
      const slider = el('input.hue-slider', { type: 'range', min: 0, max: 359, value: draft[key] });
      const row = el('div.hue-row', {}, [sw, slider]);
      const setSwatch = () => {
        const v = resolveTheme(draft);
        const c = v['--' + key];
        sw.style.background = c;
        slider.style.setProperty('--role-color', c);
      };
      slider.addEventListener('input', () => {
        draft[key] = Number(slider.value);
        applyTheme(draft);
        setSwatch();
        paintPresets();
      });
      setSwatch();
      controls.append(el('div.field', {}, [
        el('span.field-label', { text: label }),
        row,
        el('span.role-where', { text: where }),
      ]));
    }
    for (const [key, label, min, max, help] of DIALS) {
      const out = el('span.slider-value', { text: String(draft[key]) });
      const slider = el('input.slider', { type: 'range', min, max, step: 1, value: draft[key] });
      slider.addEventListener('input', () => {
        draft[key] = Number(slider.value);
        out.textContent = slider.value;
        applyTheme(draft);
        paintPresets();
      });
      controls.append(el('div.field', {}, [
        el('span.field-label', { text: label }),
        el('div.slider-row', {}, [slider, out]),
        help ? el('span.field-help', { text: help }) : null,
      ]));
    }
  };

  const refresh = () => { paintPresets(); buildControls(); };
  refresh();

  wrap.append(
    el('h3.form-section', { text: 'Presets' }),
    presetRow,
    el('h3.form-section', { text: 'Roles' }),
    controls,
    el('div.dev-actions', { style: { marginTop: '16px' } }, [
      el('button.btn.btn-primary', {
        text: 'Save theme',
        onclick: async () => {
          try {
            state.settings = await api.saveSettings({ theme: JSON.stringify(draft) });
            toast('Theme saved');
          } catch (e) { toast(e.message, true); }
        },
      }),
      el('button.btn', {
        text: 'Reset to default',
        onclick: () => { draft = { ...DEFAULT_THEME }; applyTheme(draft); refresh(); },
      }),
    ]),
  );
  return wrap;
}

/* Calendar subscriptions: add by URL, recolour, sync, delete. Visibility also
   lives here, but its everyday home is the layers button on any calendar
   widget — reachable without unlocking anything. */
async function renderCalendars() {
  const wrap = el('div');
  let feedList = [];
  try { feedList = await api.feeds(); } catch (e) { return el('p.sheet-note', { text: e.message }); }

  const list = el('div.dev-list');
  if (!feedList.length) {
    list.append(el('p.sheet-note', {
      text: 'No calendar subscriptions yet. Add a Google or Outlook calendar by its address — nothing is baked in.',
    }));
  }
  for (const f of feedList) {
    list.append(el('div.dev-row', {}, [
      el('span.src-dot', { style: f.color ? { background: f.color } : {} }),
      el('div.dev-main', {}, [
        el('div.dev-name', { text: f.name + (f.visible ? '' : '  (hidden)') }),
        el('div.dev-meta', {
          text: (f.status || 'never synced') +
                (f.last_sync ? ` · ${new Date(f.last_sync).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}` : ''),
        }),
        el('div.dev-meta.src-url', { text: f.url }),
      ]),
      el('button.btn.btn-small', {
        text: 'Sync', onclick: async (e) => {
          const btn = e.currentTarget;
          btn.disabled = true;
          try {
            const r = await api.syncFeed(f.id);
            toast(r.ok ? `${f.name}: ${r.message}` : r.message, !r.ok);
          } catch (err) { toast(err.message, true); }
          openSettings('Calendars');
        },
      }),
      el('button.btn.btn-small', { text: 'Edit', onclick: () => editFeed(f) }),
    ]));
  }

  wrap.append(
    el('div.dev-actions', {}, [
      el('button.btn.btn-primary', {
        text: '+ Add calendar',
        onclick: () => editFeed(null, feedList.length),
      }),
      feedList.length ? el('button.btn', {
        text: 'Sync all', onclick: async (e) => {
          e.currentTarget.disabled = true;
          try { await api.syncFeeds(); toast('Synced'); } catch (err) { toast(err.message, true); }
          openSettings('Calendars');
        },
      }) : null,
    ]),
    list,
    el('p.sheet-note', {
      text: 'Accepted addresses: an ICS/webcal link, an Outlook published-calendar link, or a Google Calendar embed link (converted automatically — the Google calendar must be public, or paste its "Secret address in iCal format" instead). Feeds refresh every 15 minutes.',
    }),
  );
  return wrap;
}

async function editFeed(feed, feedCount = 0) {
  const isNew = !feed;
  // Every source must land with a colour of its own, even when the form is
  // never touched — an untouched swatch row used to save '' and every event
  // fell back to the same theme primary. Rotate through the palette by how
  // many feeds already exist.
  const palette = eventPalette(getTheme());
  const suggested = palette[feedCount % palette.length].value;
  const { node, values } = await buildForm([
    { key: 'url', label: 'Calendar address', type: 'textarea',
      placeholder: 'https://…/calendar.ics  or a Google embed link',
      help: isNew ? '' : 'Leave unchanged unless the address moved' },
    { key: 'name', label: 'Name', type: 'text', placeholder: 'Work, Family…',
      help: 'Optional — the feed’s own name is used when blank' },
    { key: 'exclude', label: 'Hide events containing', type: 'text',
      placeholder: 'canceled, tentative',
      help: 'Comma-separated, case-insensitive, matched against event titles' },
    { key: 'color', label: 'Colour', type: 'color' },
  ], feed ? { url: feed.url, name: feed.name, exclude: feed.exclude || '',
              color: feed.color || suggested }
          : { color: suggested });

  let busy = false;
  const actions = [];
  if (!isNew) {
    actions.push({
      label: 'Delete', kind: 'danger', onClick: () => {
        close();
        confirmSheet('Remove this calendar?',
          `“${feed.name}” and all of its events will be removed from the panel. The source calendar is untouched.`,
          async () => {
            try { await api.deleteFeed(feed.id); toast('Removed'); } catch (e) { toast(e.message, true); }
            openSettings('Calendars');
          });
      },
    });
  }
  actions.push({ label: 'Cancel', onClick: () => { close(); openSettings('Calendars'); } });
  actions.push({
    label: isNew ? 'Add' : 'Save', kind: 'primary', onClick: async () => {
      if (busy) return;
      busy = true;
      try {
        if (isNew) {
          if (!String(values.url || '').trim()) { toast('Paste a calendar address first', true); busy = false; return; }
          toast('Fetching the calendar…');
          const r = await api.createFeed({
            url: values.url, name: values.name, color: values.color,
            exclude: values.exclude || '',
          });
          toast(`Imported ${r.imported} events${r.warnings ? ` (${r.warnings} skipped)` : ''}`);
        } else {
          const patch = { name: values.name, color: values.color };
          if ((values.exclude || '') !== (feed.exclude || '')) patch.exclude = values.exclude || '';
          if (String(values.url || '').trim() && values.url !== feed.url) patch.url = values.url;
          await api.updateFeed(feed.id, patch);
          toast('Saved');
        }
        close();
        openSettings('Calendars');
      } catch (e) {
        toast(e.message, true);
      }
      busy = false;
    },
  });
  openSheet({ title: isNew ? 'Add calendar' : 'Edit calendar', body: node, actions });
}

/* Money: Plaid-linked institutions plus anything typed by hand. */
async function renderMoney() {
  const wrap = el('div');
  let data = { accounts: [], items: [], summary: null, configured: false, env: 'sandbox' };
  try { data = await api.finance(); } catch (e) { return el('p.sheet-note', { text: e.message }); }

  const linked = el('div.dev-list');
  const paintLinked = () => {
    clear(linked);
    if (!data.items.length) {
      linked.append(el('p.sheet-note', { text: 'No institutions linked yet.' }));
    }
    data.items.forEach(it => {
      const n = data.accounts.filter(a => a.item_id === it.id).length;
      linked.append(el('div.dev-row', {}, [
        el('div.dev-main', {}, [
          el('div.dev-name', { text: it.institution || 'Institution' }),
          el('div.dev-meta', {
            text: `${n} account${n === 1 ? '' : 's'} · ${it.status || 'not synced yet'}`,
          }),
        ]),
        el('button.btn.btn-small', {
          text: 'Unlink', onclick: () => {
            close();
            confirmSheet('Unlink this institution?',
              `Its accounts disappear from the panel and access is revoked at Plaid. Anything you typed by hand is untouched.`,
              async () => {
                try { await api.deleteFinanceItem(it.id); } catch (e) { toast(e.message, true); }
                openSettings('Money');
              });
          },
        }),
      ]));
    });
  };
  paintLinked();

  const accountList = el('div.dev-list');
  const paintAccounts = () => {
    clear(accountList);
    if (!data.accounts.length) {
      accountList.append(el('p.sheet-note', { text: 'No accounts yet.' }));
    }
    data.accounts.forEach(a => accountList.append(el('div.dev-row', {}, [
      el('span.fin-dot', { style: a.color ? { backgroundColor: a.color } : {} }),
      el('div.dev-main', {}, [
        el('div.dev-name', { text: a.name + (a.hidden ? '  (hidden)' : '') }),
        el('div.dev-meta', {
          text: [a.kind, a.institution || null, a.item_id ? 'via Plaid' : 'manual',
                 a.due_day ? `due ${a.due_day}` : null].filter(Boolean).join(' · '),
        }),
      ]),
      el('div.fin-amt', { text: moneyFmt(a.balance) }),
      el('button.btn.btn-small', { text: 'Edit', onclick: () => editFinanceAccount(a) }),
    ])));
  };
  paintAccounts();

  // Plaid credentials. The client secret is a bearer credential; it lives in
  // the local database and is only ever sent to Plaid.
  const cid = el('input.input', { type: 'text', placeholder: 'Plaid client_id',
                                  value: state.settings.plaid_client_id || '' });
  const sec = el('input.input', { type: 'password', placeholder: 'Plaid secret',
                                  value: state.settings.plaid_secret || '' });
  const envSel = el('select.input');
  [['sandbox', 'Sandbox — fake data, works instantly'],
   ['production', 'Production — real banks (needs Plaid approval)']].forEach(([v, l]) => {
    const o = el('option', { value: v, text: l });
    if ((state.settings.plaid_env || 'sandbox') === v) o.selected = true;
    envSel.append(o);
  });

  const linkStatus = el('p.sheet-note');
  let polling = null;

  const startLink = async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    clearInterval(polling);
    linkStatus.textContent = 'Asking Plaid for a link session…';
    let session;
    try { session = await api.startLink(); }
    catch (err) { linkStatus.textContent = err.message; btn.disabled = false; return; }

    clear(linkStatus);
    linkStatus.append(
      el('div', { text: 'Open this on a phone or laptop — the panel has no keyboard:' }),
      el('a.link-url', { href: session.url, target: '_blank', rel: 'noreferrer', text: session.url }),
      el('div.field-help', { text: 'Waiting for you to finish… this page updates by itself.' }),
    );

    const started = Date.now();
    polling = setInterval(async () => {
      if (Date.now() - started > 15 * 60000) {       // Link tokens expire
        clearInterval(polling);
        linkStatus.textContent = 'That link session expired — start another.';
        btn.disabled = false;
        return;
      }
      try {
        const r = await api.pollLink(session.link_token);
        if (r.ready) {
          clearInterval(polling);
          toast(`Linked ${r.item.institution || 'institution'} — ${r.sync.message}`);
          openSettings('Money');
        }
      } catch { /* keep polling; transient errors are normal mid-flow */ }
    }, 3000);
  };

  wrap.append(
    el('h3.form-section', { text: 'Linked institutions' }),
    linked,
    el('div.dev-actions', {}, [
      el('button.btn.btn-primary', {
        text: '+ Link an institution', disabled: !data.configured, onclick: startLink,
      }),
      el('button.btn', {
        text: 'Sync now', disabled: !data.items.length,
        onclick: async (e) => {
          e.currentTarget.disabled = true;
          try { const r = await api.syncFinance(); toast(`Synced · ${r.bill_events} bill dates`); }
          catch (err) { toast(err.message, true); }
          openSettings('Money');
        },
      }),
    ]),
    linkStatus,
    !data.configured ? el('p.sheet-note.warn', {
      text: 'Add your Plaid credentials below first.',
    }) : null,

    el('h3.form-section', { text: 'Accounts' }),
    accountList,
    el('div.dev-actions', {}, [
      el('button.btn', { text: '+ Add a manual account', onclick: () => editFinanceAccount(null) }),
    ]),
    el('p.sheet-note', {
      text: 'Add anything Plaid can’t reach by hand — balances you type are tracked over time the same way, and a due day puts it on the calendar.',
    }),

    el('h3.form-section', { text: 'Plaid credentials' }),
    el('label.field', {}, [el('span.field-label', { text: 'Client ID' }), cid]),
    el('label.field', {}, [el('span.field-label', { text: 'Secret' }), sec]),
    el('label.field', {}, [
      el('span.field-label', { text: 'Environment' }), envSel,
      el('span.field-help', {
        text: 'Sandbox works the moment you sign up — use user_good / pass_good to test. Production needs Plaid to approve your account.',
      }),
    ]),
    el('div.dev-actions', {}, [
      el('button.btn.btn-primary', {
        text: 'Save credentials',
        onclick: async () => {
          try {
            state.settings = await api.saveSettings({
              plaid_client_id: cid.value.trim(),
              plaid_secret: sec.value.trim(),
              plaid_env: envSel.value,
            });
            toast('Saved');
            openSettings('Money');
          } catch (e) { toast(e.message, true); }
        },
      }),
    ]),
    el('p.sheet-note', {
      text: 'Credentials and Plaid access tokens are stored in this machine’s local database and are only ever sent to Plaid.',
    }),
  );
  return wrap;
}

function moneyFmt(n) {
  return Number(n || 0).toLocaleString(undefined,
    { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
}

async function editFinanceAccount(a) {
  const isNew = !a;
  const KINDS = [['checking', 'Checking'], ['savings', 'Savings / HYSA'],
                 ['credit', 'Credit card'], ['loan', 'Loan'],
                 ['investment', 'Investment'], ['retirement', 'Retirement (401k / IRA)'],
                 ['other', 'Other']];
  const { node, values } = await buildForm([
    { key: 'name', label: 'Name', type: 'text', placeholder: 'Ally HYSA' },
    { key: 'institution', label: 'Institution', type: 'text' },
    { key: 'kind', label: 'Type', type: 'select', default: 'savings',
      options: KINDS.map(([v, l]) => ({ value: v, label: l })) },
    { key: 'balance', label: 'Balance', type: 'number', step: '0.01',
      help: a?.item_id ? 'Synced from Plaid — editing here will be overwritten on the next sync'
                       : 'Enter debts as a positive number; the panel shows them as owed' },
    { key: 'due_day', label: 'Payment due day of month', type: 'number', min: 0, max: 31,
      help: 'Cards and loans only. Puts a due date on the calendar. 0 = none.' },
    { key: 'min_payment', label: 'Minimum payment', type: 'number', step: '0.01' },
    { key: 'apr', label: 'APR %', type: 'number', step: '0.01' },
    { key: 'color', label: 'Colour', type: 'color' },
  ], a ? { name: a.name, institution: a.institution, kind: a.kind, balance: a.balance,
           due_day: a.due_day || 0, min_payment: a.min_payment || '', apr: a.apr || '',
           color: a.color } : { kind: 'savings', balance: 0, due_day: 0 });

  const actions = [];
  if (!isNew) {
    actions.push({
      label: 'Delete', kind: 'danger', onClick: () => {
        close();
        confirmSheet('Delete this account?',
          a.item_id
            ? `“${a.name}” comes from Plaid and will reappear on the next sync — unlink the institution instead if you want it gone.`
            : `“${a.name}” and its balance history are removed.`,
          async () => {
            try { await api.deleteFinanceAccount(a.id); } catch (e) { toast(e.message, true); }
            openSettings('Money');
          });
      },
    });
  }
  actions.push({ label: 'Cancel', onClick: () => { close(); openSettings('Money'); } });
  actions.push({
    label: 'Save', kind: 'primary', onClick: async () => {
      const payload = {
        name: (values.name || '').trim(),
        institution: values.institution || '',
        kind: values.kind || 'savings',
        balance: Number(values.balance || 0),
        due_day: Number(values.due_day || 0) || null,
        min_payment: values.min_payment === '' ? null : Number(values.min_payment),
        apr: values.apr === '' ? null : Number(values.apr),
        color: values.color || '',
      };
      if (!payload.name) return toast('Give the account a name', true);
      try {
        if (isNew) await api.createFinanceAccount(payload);
        else await api.updateFinanceAccount(a.id, payload);
        close();
        openSettings('Money');
      } catch (e) { toast(e.message, true); }
    },
  });

  openSheet({ title: isNew ? 'Add account' : `Edit ${a.name}`, body: node, actions });
}

/* Household members: a greeting, a look, and pages of their own. */
async function renderPeople() {
  const wrap = el('div');
  let people = [];
  try { people = await api.people(); } catch (e) { return el('p.sheet-note', { text: e.message }); }
  state.people = people;

  const list = el('div.dev-list');
  if (!people.length) {
    list.append(el('p.sheet-note', {
      text: 'No one added yet. Each person gets their own greeting, colour and pages — and the panel switches between them with a tap.',
    }));
  }
  people.forEach((p, i) => {
    const pageCount = state.pages.filter(pg => pg.person_id === p.id).length;
    list.append(el('div.dev-row', {}, [
      el('span.person-face.person-face-sm', {
        style: p.color ? { '--c': p.color } : {},
        text: p.avatar || initials(p.name),
      }),
      el('div.dev-main', {}, [
        el('div.dev-name', { text: p.name + (p.id === state.activePerson ? '  · active' : '') }),
        el('div.dev-meta', {
          text: [
            pageCount ? `${pageCount} page${pageCount === 1 ? '' : 's'}` : 'no pages yet',
            (p.macs || []).length ? (p.home ? 'home now' : 'away') : 'no device linked',
            p.theme ? 'own theme' : null,
          ].filter(Boolean).join(' · '),
        }),
      ]),
      el('button.btn.btn-small', {
        text: '▲', 'aria-label': 'Move up', disabled: i === 0,
        onclick: async () => {
          const ids = people.map(x => x.id);
          [ids[i - 1], ids[i]] = [ids[i], ids[i - 1]];
          try { await api.orderPeople(ids); } catch (e) { toast(e.message, true); }
          openSettings('People');
        },
      }),
      el('button.btn.btn-small', {
        text: p.id === state.activePerson ? 'Active' : 'Switch to',
        disabled: p.id === state.activePerson,
        onclick: async () => { close(); await setActivePerson(p.id); },
      }),
      el('button.btn.btn-small', { text: 'Edit', onclick: () => editPerson(p) }),
    ]));
  });

  const nameInput = el('input.input', { type: 'text', placeholder: 'Name — e.g. Dan, Amaya' });
  wrap.append(
    list,
    el('div.dev-actions', { style: { marginTop: '14px' } }, [
      nameInput,
      el('button.btn.btn-primary', {
        text: '+ Add person',
        onclick: async () => {
          const name = nameInput.value.trim();
          if (!name) return toast('Give them a name', true);
          try {
            const p = await api.createPerson({ name });
            state.people.push(p);
            editPerson(p);
          } catch (e) { toast(e.message, true); }
        },
      }),
    ]),
    el('p.sheet-note', {
      text: 'Add a “Who’s using this” widget to switch profiles from the wall, and a “Greeting” widget for the welcome line. Pages get an owner in Page settings (⋯ in edit mode).',
    }),
  );
  return wrap;
}

async function editPerson(p) {
  const { node, values } = await buildForm([
    { key: 'name', label: 'Name', type: 'text' },
    { key: 'avatar', label: 'Avatar', type: 'text',
      placeholder: initials(p.name),
      help: 'An emoji or a couple of letters — blank uses their initials' },
    { key: 'greeting', label: 'Custom greeting', type: 'text',
      placeholder: 'Hey {name}, let’s go',
      help: 'Blank gives a time-aware greeting. {name} inserts their name.' },
    { key: 'color', label: 'Colour', type: 'color' },
  ], { name: p.name, avatar: p.avatar || '', greeting: p.greeting || '',
       color: p.color || '' });

  // Devices: presence is optional, and typing a MAC from memory is nobody's
  // idea of setup — pick from what's on the network right now.
  let macs = [...(p.macs || [])];
  const macList = el('div.dev-list');
  const paintMacs = () => {
    clear(macList);
    if (!macs.length) {
      macList.append(el('p.sheet-note', { text: 'No device linked — presence stays off for them.' }));
    }
    macs.forEach((mc, i) => macList.append(el('div.dev-row', {}, [
      el('div.dev-main', {}, [el('div.dev-meta', { text: mc })]),
      el('button.btn.btn-small', {
        text: 'Remove', onclick: () => { macs.splice(i, 1); paintMacs(); },
      }),
    ])));
  };
  paintMacs();

  const scanBtn = el('button.btn', {
    text: 'Scan network for their phone',
    onclick: async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true; btn.textContent = 'Scanning…';
      try {
        const hosts = await api.lanHosts();
        const body = el('div', {}, [
          el('p.sheet-note', {
            text: 'Devices seen on the network right now. Phones show up when awake — if theirs is missing, unlock it and scan again.',
          }),
          ...hosts.map(h => el('div.dev-row', {}, [
            el('div.dev-main', {}, [
              el('div.dev-name', { text: h.mac }),
              el('div.dev-meta', { text: h.ip + (h.claimed_by ? ` · already ${h.claimed_by}` : '') }),
            ]),
            el('button.btn.btn-small.btn-primary', {
              text: 'This one', disabled: macs.includes(h.mac),
              onclick: () => {
                if (!macs.includes(h.mac)) macs.push(h.mac);
                paintMacs();
                close();
                openPersonSheet();
              },
            }),
          ])),
        ]);
        openSheet({ title: 'Pick their device', body, wide: true,
                    actions: [{ label: 'Cancel', onClick: () => { close(); openPersonSheet(); } }] });
      } catch (err) { toast(err.message, true); }
      btn.disabled = false; btn.textContent = 'Scan network for their phone';
    },
  });

  const themeNote = el('p.sheet-note', {
    text: p.theme ? 'Using their own theme.' : 'Using the household theme.',
  });

  const body = el('div', {}, [
    node,
    el('h3.form-section', { text: 'Presence (optional)' }),
    macList,
    el('div.dev-actions', {}, [scanBtn]),
    el('h3.form-section', { text: 'Theme' }),
    themeNote,
    el('div.dev-actions', {}, [
      el('button.btn', {
        text: 'Use the current theme for them',
        onclick: async () => {
          try {
            await api.updatePerson(p.id, { theme: getTheme() });
            p.theme = getTheme();
            themeNote.textContent = 'Using their own theme.';
            toast('Saved — switch to them to see it');
          } catch (e) { toast(e.message, true); }
        },
      }),
      p.theme ? el('button.btn', {
        text: 'Clear', onclick: async () => {
          try {
            await api.updatePerson(p.id, { theme: null });
            p.theme = null;
            themeNote.textContent = 'Using the household theme.';
          } catch (e) { toast(e.message, true); }
        },
      }) : null,
    ]),
  ]);

  function openPersonSheet() {
    openSheet({
      title: `Edit ${p.name}`,
      body, wide: true,
      actions: [
        {
          label: 'Remove', kind: 'danger', onClick: () => {
            close();
            confirmSheet('Remove this person?',
              `${p.name} disappears from the switcher. Any pages of theirs become shared — nothing is deleted.`,
              async () => {
                try { await api.deletePerson(p.id); } catch (e) { toast(e.message, true); }
                if (state.activePerson === p.id) await setActivePerson('');
                await reload();
                openSettings('People');
              });
          },
        },
        { label: 'Cancel', onClick: () => { close(); openSettings('People'); } },
        {
          label: 'Save', kind: 'primary', onClick: async () => {
            try {
              await api.updatePerson(p.id, {
                name: (values.name || p.name).trim(),
                avatar: values.avatar || '',
                greeting: values.greeting || '',
                color: values.color || '',
                macs,
              });
              close();
              await reload();
              openSettings('People');
            } catch (e) { toast(e.message, true); }
          },
        },
      ],
    });
  }
  openPersonSheet();
}

/* Gallery sets: named folders of images on disk. Deleting a set (or a widget
   showing it) never deletes files; only the per-image ✕ does, deliberately. */
async function renderGalleries() {
  const wrap = el('div');
  let sets = [];
  try { sets = await api.galleries(); } catch (e) { return el('p.sheet-note', { text: e.message }); }

  const list = el('div.dev-list');
  if (!sets.length) {
    list.append(el('p.sheet-note', {
      text: 'No gallery sets yet. Each set is a folder of images under the app’s galleries/ directory — add one and upload photos from this panel or any phone on the network.',
    }));
  }
  sets.forEach((g, i) => {
    const starred = state.settings.starred_gallery === g.id
      || (!state.settings.starred_gallery && i === 0);
    list.append(el('div.dev-row', {}, [
      g.cover_id
        ? el('img.gset-cover', { src: api.imageUrl(g.cover_id), alt: '' })
        : el('span.gset-cover.gset-cover-empty', {}, [icon('image', 20)]),
      el('div.dev-main', {}, [
        el('div.dev-name', { text: g.name }),
        el('div.dev-meta', {
          text: `${g.image_count} image${g.image_count === 1 ? '' : 's'} · galleries/${g.dirname}/`
                + (starred ? ' · four-finger double tap plays this' : ''),
        }),
      ]),
      el('button.btn.btn-small.star-btn' + (starred ? '.on' : ''), {
        text: starred ? '★' : '☆', title: 'Play this on a four-finger double tap',
        onclick: async () => {
          try {
            state.settings = await api.saveSettings({ starred_gallery: g.id });
            toast(`★ ${g.name} — four-finger double tap plays it`);
          } catch (e) { toast(e.message, true); }
          openSettings('Galleries');
        },
      }),
      el('button.btn.btn-small', {
        text: '▲', 'aria-label': 'Move up', disabled: i === 0,
        onclick: async () => {
          const ids = sets.map(s => s.id);
          [ids[i - 1], ids[i]] = [ids[i], ids[i - 1]];
          try { await api.orderGalleries(ids); } catch (e) { toast(e.message, true); }
          openSettings('Galleries');
        },
      }),
      el('button.btn.btn-small', {
        text: '▼', 'aria-label': 'Move down', disabled: i === sets.length - 1,
        onclick: async () => {
          const ids = sets.map(s => s.id);
          [ids[i], ids[i + 1]] = [ids[i + 1], ids[i]];
          try { await api.orderGalleries(ids); } catch (e) { toast(e.message, true); }
          openSettings('Galleries');
        },
      }),
      el('button.btn.btn-small', {
        text: 'Play', onclick: () => {
          close();
          import('./widgets/gallery.js').then(m =>
            m.startScreensaver(g.id, { interval: 20, fit: 'contain' }));
        },
      }),
      el('button.btn.btn-small', { text: 'Edit', onclick: () => editGallery(g) }),
    ]));
  });

  const nameInput = el('input.input', { type: 'text', placeholder: 'New set name — e.g. Family, Travel' });
  wrap.append(
    list,
    el('div.dev-actions', { style: { marginTop: '14px' } }, [
      nameInput,
      el('button.btn.btn-primary', {
        text: '+ Create set',
        onclick: async () => {
          const name = nameInput.value.trim();
          if (!name) return toast('Give the set a name', true);
          try {
            const r = await api.createGallery(name);
            toast(r.adopted ? `Created — adopted ${r.adopted} images already in the folder`
                            : 'Created');
            editGallery(r.gallery);
          } catch (e) { toast(e.message, true); }
        },
      }),
    ]),
  );
  return wrap;
}

async function editGallery(g) {
  let images = [];
  try { images = await api.galleryImages(g.id); } catch (e) { toast(e.message, true); return; }

  const nameInput = el('input.input', { type: 'text', value: g.name });
  const grid = el('div.gthumbs');
  let armed = null;    // image id whose ✕ is primed — second tap deletes

  const paint = () => {
    clear(grid);
    images.forEach((im, i) => {
      const primed = armed === im.id;
      grid.append(el('div.gthumb', {}, [
        el('img', { src: api.imageUrl(im.id), alt: '', loading: 'lazy' }),
        el('div.gthumb-bar', {}, [
          el('button.gthumb-btn', {
            text: '◀', 'aria-label': 'Move earlier', disabled: i === 0,
            onclick: async () => {
              [images[i - 1], images[i]] = [images[i], images[i - 1]];
              paint();
              try { await api.orderGalleryImages(g.id, images.map(x => x.id)); }
              catch (e) { toast(e.message, true); }
            },
          }),
          el('button.gthumb-btn' + (primed ? '.gthumb-danger' : ''), {
            text: primed ? 'sure?' : '✕',
            'aria-label': 'Delete image',
            onclick: async () => {
              if (!primed) {
                armed = im.id;
                paint();
                setTimeout(() => { if (armed === im.id) { armed = null; paint(); } }, 2500);
                return;
              }
              armed = null;
              try {
                await api.deleteGalleryImage(g.id, im.id);
                images = images.filter(x => x.id !== im.id);
              } catch (e) { toast(e.message, true); }
              paint();
            },
          }),
          el('button.gthumb-btn', {
            text: '▶', 'aria-label': 'Move later', disabled: i === images.length - 1,
            onclick: async () => {
              [images[i], images[i + 1]] = [images[i + 1], images[i]];
              paint();
              try { await api.orderGalleryImages(g.id, images.map(x => x.id)); }
              catch (e) { toast(e.message, true); }
            },
          }),
        ]),
      ]));
    });
    if (!images.length) {
      grid.append(el('p.sheet-note', { text: 'Empty — add images below.' }));
    }
  };
  paint();

  // Hidden file input: works from the panel, and from a phone browser on the
  // LAN it opens the photo picker — the easiest way to fill a set.
  const picker = el('input', {
    type: 'file', accept: 'image/*', multiple: true, style: { display: 'none' },
  });
  picker.addEventListener('change', async () => {
    const files = [...picker.files];
    picker.value = '';
    let done = 0;
    for (const f of files) {
      toast(`Uploading ${done + 1}/${files.length}…`);
      try {
        const img = await api.uploadGalleryImage(g.id, f);
        if (img) images.push(img);
        done += 1;
      } catch (e) { toast(`${f.name}: ${e.message}`, true); }
    }
    if (done) toast(`Added ${done} image${done === 1 ? '' : 's'}`);
    paint();
  });

  const body = el('div.form', {}, [
    el('label.field', {}, [el('span.field-label', { text: 'Set name' }), nameInput]),
    el('div.field', {}, [
      el('span.field-label', { text: 'Images — ◀ ▶ reorder, ✕ deletes the file' }),
      grid,
      el('div.dev-actions', {}, [
        el('button.btn', { text: '+ Add images', onclick: () => picker.click() }),
        el('button.btn', {
          text: 'Play', onclick: () => {
            close();
            import('./widgets/gallery.js').then(m =>
              m.startScreensaver(g.id, { interval: 20, fit: 'contain' }));
          },
        }),
      ]),
      picker,
    ]),
  ]);

  openSheet({
    title: `Edit “${g.name}”`,
    body, wide: true,
    actions: [
      {
        label: 'Delete set', kind: 'danger', onClick: () => {
          close();
          confirmSheet('Delete this set?',
            `“${g.name}” disappears from the app. The folder galleries/${g.dirname}/ and every image in it stay on the PC — recreate a set with the same name to adopt them back.`,
            async () => {
              try { await api.deleteGallery(g.id); } catch (e) { toast(e.message, true); }
              openSettings('Galleries');
            });
        },
      },
      { label: 'Done', kind: 'primary', onClick: async () => {
          const name = nameInput.value.trim();
          if (name && name !== g.name) {
            try { await api.updateGallery(g.id, { name }); } catch (e) { toast(e.message, true); }
          }
          close();
          openSettings('Galleries');
        } },
    ],
  });
}

async function renderDevices() {
  const wrap = el('div');
  let devices = [], kinds = [];
  try { [devices, kinds] = await Promise.all([api.devices(), api.deviceKinds()]); }
  catch (e) { return el('p.sheet-note', { text: e.message }); }

  const list = el('div.dev-list');
  const paint = () => {
    clear(list);
    if (!devices.length) list.append(el('p.sheet-note', { text: 'No devices yet.' }));
    devices.forEach(d => {
      const st = liveStates.get(d.id) || d.state || {};
      list.append(el('div.dev-row', {}, [
        el('div.dev-main', {}, [
          el('div.dev-name', { text: d.name }),
          el('div.dev-meta', {
            text: `${d.kind}${d.room ? ' · ' + d.room : ''}${d.config?.ip ? ' · ' + d.config.ip : ''}`,
          }),
        ]),
        el('span.dev-dot' + (st.online === false ? '.bad' : st.online ? '.good' : ''), {
          title: st.online === false ? (st.error || 'offline') : 'online',
        }),
        el('button.btn.btn-small', { text: 'Edit', onclick: () => editDevice(d, kinds) }),
      ]));
    });
  };
  paint();

  // Shared Govee key: pasted once, used by every govee_cloud device that has
  // no key of its own, and by Scan to list the account's devices.
  const keyInput = el('input.input', {
    type: 'password', placeholder: 'Govee API key',
    value: state.settings.govee_api_key || '',
  });
  const keyRow = el('div.field', {}, [
    el('span.field-label', { text: 'Govee API key (shared)' }),
    el('div.dev-actions', {}, [
      keyInput,
      el('button.btn', {
        text: 'Save key', onclick: async () => {
          try {
            state.settings = await api.saveSettings({ govee_api_key: keyInput.value.trim() });
            toast(keyInput.value.trim() ? 'Key saved — Scan will now list your Govee account'
                                        : 'Key cleared');
          } catch (e) { toast(e.message, true); }
        },
      }),
    ]),
    el('span.field-help', {
      text: 'Govee Home app > Profile > About Us > Apply for API Key (arrives by email). Needed for smart plugs — they have no local control.',
    }),
  ]);

  wrap.append(
    el('div.dev-actions', {}, [
      el('button.btn.btn-primary', { text: '+ Add device', onclick: () => editDevice(null, kinds) }),
      el('button.btn', {
        text: 'Scan network', onclick: async (e) => {
          const btn = e.currentTarget;
          btn.disabled = true; btn.textContent = 'Scanning…';
          try {
            const found = await api.discover(true);
            showDiscovered(found, kinds);
          } catch (err) { toast(err.message, true); }
          btn.disabled = false; btn.textContent = 'Scan network';
        },
      }),
    ]),
    list,
    keyRow,
  );
  return wrap;
}

function showDiscovered(found, kinds) {
  const all = Object.entries(found).flatMap(([kind, items]) =>
    items.map(it => ({ ...it, kind: it.kind || kind })));
  const body = el('div');
  if (!all.length) {
    body.append(el('p.sheet-note', {
      text: 'Nothing found. Rokus answer only when powered; Samsung TVs stop responding entirely when off; Govee plugs have no LAN API and must be added with a cloud API key.',
    }));
  }
  all.forEach(d => {
    // A cloud-listing failure is information, not an addable device.
    if (d.error) {
      body.append(el('p.sheet-note.warn', { text: d.error }));
      return;
    }
    const bits = [d.kind, d.ip, d.model,
                  d.device && !d.model ? d.device : null].filter(Boolean).join(' · ');
    body.append(el('div.dev-row', {}, [
      el('div.dev-main', {}, [
        el('div.dev-name', { text: d.name }),
        el('div.dev-meta', { text: bits }),
        d.needs_key ? el('div.dev-meta', {
          text: 'On the network, but cloud-only — save a Govee API key and rescan to control it',
        }) : null,
      ]),
      el('button.btn.btn-small.btn-primary', {
        text: 'Add', onclick: async (e) => {
          e.currentTarget.disabled = true;
          const config = {};
          if (d.ip) config.ip = d.ip;
          if (d.mac) config.mac = d.mac;
          if (d.sku) config.sku = d.sku;
          if (d.is_tv) config.is_tv = true;
          if (d.device) config.device = d.device;   // govee cloud id (MAC form)
          if (d.model) config.model = d.model;
          try {
            await api.createDevice({ name: d.name, kind: d.kind, config });
            toast(`${d.name} added`);
          } catch (err) { toast(err.message, true); }
        },
      }),
    ]));
  });
  openSheet({ title: 'Discovered devices', body, wide: true,
              actions: [{ label: 'Done', onClick: () => { close(); openSettings(); } }] });
}

async function editDevice(device, kinds) {
  const isNew = !device;
  let kind = device?.kind || kinds[0]?.kind;

  const kindSel = el('select.input');
  kinds.forEach(k => {
    const o = el('option', { value: k.kind, text: k.label });
    if (k.kind === kind) o.selected = true;
    kindSel.append(o);
  });

  const name = el('input.input', { type: 'text', value: device?.name || '' });
  const room = el('input.input', { type: 'text', value: device?.room || '' });
  const configHost = el('div');
  let form = null;

  const rebuildConfig = async () => {
    const def = kinds.find(k => k.kind === kind);
    clear(configHost);
    form = await buildForm(
      (def?.config_fields || []).map(f => ({ ...f, key: f.name })),
      device?.config || {});
    configHost.append(form.node);
  };
  kindSel.addEventListener('change', async () => { kind = kindSel.value; await rebuildConfig(); });
  await rebuildConfig();

  const body = el('div.form', {}, [
    el('label.field', {}, [el('span.field-label', { text: 'Name' }), name]),
    el('label.field', {}, [el('span.field-label', { text: 'Type' }), kindSel]),
    el('label.field', {}, [el('span.field-label', { text: 'Room' }), room]),
    el('h3.form-section', { text: 'Connection' }),
    configHost,
  ]);

  const actions = [];
  if (!isNew) {
    actions.push({
      label: 'Delete', kind: 'danger', onClick: () => {
        close();
        confirmSheet('Delete device?', `“${device.name}” will be removed.`, async () => {
          try { await api.deleteDevice(device.id); openSettings(); } catch (e) { toast(e.message, true); }
        });
      },
    });
  }
  actions.push({ label: 'Cancel', onClick: () => { close(); openSettings(); } });
  actions.push({
    label: 'Save', kind: 'primary', onClick: async () => {
      const payload = { name: name.value.trim(), kind, room: room.value.trim(), config: form.values };
      if (!payload.name) return toast('Give the device a name', true);
      try {
        if (isNew) await api.createDevice(payload);
        else await api.updateDevice(device.id, payload);
        close();
        toast('Saved');
        openSettings();
      } catch (e) { toast(e.message, true); }
    },
  });

  openSheet({ title: isNew ? 'Add device' : 'Edit device', body, actions });
}

async function renderDisplay() {
  const s = state.settings;
  const { node, values } = await buildForm([
    { section: 'Dim when idle' },
    { key: 'idle_dim', label: 'Dim after inactivity', type: 'toggle',
      default: (s.idle_dim ?? 'true') !== 'false',
      help: 'Any touch wakes the screen; the waking touch presses nothing' },
    { key: 'idle_stage1_min', label: 'Dim after (minutes)', type: 'slider',
      min: 1, max: 120, default: Number(s.idle_stage1_min || 15) },
    { key: 'idle_stage1_level', label: 'Dim to (%)', type: 'slider',
      min: 2, max: 50, default: Number(s.idle_stage1_level ?? 8) },
    { key: 'idle_stage2_min', label: 'Deep-dim after (minutes)', type: 'slider',
      min: 5, max: 480, default: Number(s.idle_stage2_min || 60) },
    { key: 'idle_stage2_level', label: 'Deep-dim to (%)', type: 'slider',
      min: 0, max: 20, default: Number(s.idle_stage2_level ?? 1) },
    { key: 'idle_display_sleep', label: 'Then sleep the display', type: 'toggle',
      default: (s.idle_display_sleep ?? 'true') !== 'false',
      help: 'Real backlight-off (DPMS) — the panel is black, and a touch wakes it' },
    { key: 'idle_stage3_min', label: 'Sleep after (minutes)', type: 'slider',
      min: 10, max: 720, default: Number(s.idle_stage3_min || 120) },
    { section: 'Touch' },
    { key: 'touch_ripples', label: 'Show touch circles', type: 'toggle',
      default: (s.touch_ripples ?? 'true') !== 'false',
      help: 'Faint tinted rings under fingers; the panel never shows a cursor' },
    { section: 'Night schedule' },
    { key: 'night_dim', label: 'Dim overnight', type: 'toggle',
      default: s.night_dim === 'true', help: 'Softens the panel so it is not a lamp at 3am' },
    { key: 'night_start', label: 'Dim from', type: 'time', default: s.night_start || '22:00' },
    { key: 'night_end', label: 'Dim until', type: 'time', default: s.night_end || '07:00' },
    { key: 'night_level', label: 'Dim level (%)', type: 'slider', min: 10, max: 90,
      default: Number(s.night_level || 45) },
  ], {});
  return el('div', {}, [
    node,
    el('p.sheet-note', {
      text: 'The two dim sources are independent; whichever is darker wins.',
    }),
    el('button.btn.btn-primary', {
      text: 'Save display settings',
      onclick: async () => {
        try {
          state.settings = await api.saveSettings({
            idle_dim: String(!!values.idle_dim),
            idle_stage1_min: String(values.idle_stage1_min ?? 15),
            idle_stage1_level: String(values.idle_stage1_level ?? 8),
            idle_stage2_min: String(values.idle_stage2_min ?? 60),
            idle_stage2_level: String(values.idle_stage2_level ?? 1),
            idle_display_sleep: String(!!values.idle_display_sleep),
            idle_stage3_min: String(values.idle_stage3_min ?? 120),
            touch_ripples: String(!!values.touch_ripples),
            night_dim: String(!!values.night_dim),
            night_start: values.night_start || '22:00',
            night_end: values.night_end || '07:00',
            night_level: String(values.night_level ?? 45),
          });
          applyNightDim();
          tickIdle();
          toast('Saved');
        } catch (e) { toast(e.message, true); }
      },
    }),
  ]);
}

/* -------------------------------------------------------------- dimming */

/* Two independent dim sources — the night schedule and idle time — and the
   darker one wins. Each computes its own level; applyDim() combines. */

const idle = { last: Date.now(), level: 1 };

function applyDim() {
  const eff = Math.max(0, Math.min(state.nightLevel ?? 1, idle.level));
  const app = document.getElementById('app');
  const cur = parseFloat(document.documentElement.style.getPropertyValue('--dim') || '1') || 1;
  // Waking should feel immediate; drifting darker should be unnoticeable.
  app.style.transition = eff >= cur ? 'filter .3s ease' : 'filter 2.5s ease';
  document.documentElement.style.setProperty('--dim', String(eff));
}

function applyNightDim() {
  const s = state.settings || {};
  let level = 1;
  if (s.night_dim === 'true') {
    const toMin = t => {
      const [h, m] = String(t || '0:0').split(':').map(Number);
      return (h || 0) * 60 + (m || 0);
    };
    const now = new Date();
    const cur = now.getHours() * 60 + now.getMinutes();
    const a = toMin(s.night_start || '22:00'), b = toMin(s.night_end || '07:00');
    const inWindow = a <= b ? (cur >= a && cur < b) : (cur >= a || cur < b);
    if (inWindow) level = Number(s.night_level || 45) / 100;
  }
  state.nightLevel = level;
  applyDim();
}

function idleCfg() {
  const s = state.settings || {};
  return {
    on: (s.idle_dim ?? 'true') !== 'false',          // on unless switched off
    m1: Number(s.idle_stage1_min || 15),
    l1: Number(s.idle_stage1_level ?? 8) / 100,
    m2: Number(s.idle_stage2_min || 60),
    l2: Number(s.idle_stage2_level ?? 1) / 100,
    sleep: (s.idle_display_sleep ?? 'true') !== 'false',
    m3: Number(s.idle_stage3_min || 120),
  };
}

/* Stage 3 is real hardware sleep (DPMS), not CSS: the backlight goes off.
   Fired once per idle period; any touch wakes X natively AND we force it back
   on explicitly, because a wake that depends on one mechanism will one day
   find its exception. */
async function setDisplayPower(on) {
  if (idle.screenOff === !on) return;
  idle.screenOff = !on;
  try { await api.display(on); } catch { /* not fatal — dimming still holds */ }
}

function setIdleLevel(v) {
  if (idle.level === v) return;
  idle.level = v;
  // While dimmed, the wake overlay owns the first touch.
  document.getElementById('wakeCatch').hidden = v === 1;
  applyDim();
}

function tickIdle() {
  // A running fullscreen screensaver IS the display's purpose right now:
  // hold dim and sleep off, but leave the idle clock alone — when the show
  // ends (touch or its 3-hour cap) the stages apply immediately after.
  if (document.getElementById('screensaver')) return setIdleLevel(1);
  const c = idleCfg();
  if (!c.on) { setDisplayPower(true); return setIdleLevel(1); }
  const mins = (Date.now() - idle.last) / 60000;
  setIdleLevel(mins >= c.m2 ? c.l2 : mins >= c.m1 ? c.l1 : 1);
  if (c.sleep && mins >= c.m3) setDisplayPower(false);
}

function initIdleDim() {
  const wake = document.getElementById('wakeCatch');
  const mark = () => {
    idle.last = Date.now();
    if (idle.screenOff) setDisplayPower(true);
    if (idle.level !== 1) setIdleLevel(1);
  };
  // Any touch or key anywhere is activity. Capture phase, so this runs before
  // the wake overlay swallows the event.
  document.addEventListener('pointerdown', mark, true);
  document.addEventListener('keydown', mark, true);
  // The waking touch itself must not press anything underneath.
  for (const t of ['pointerdown', 'pointerup', 'click']) {
    wake.addEventListener(t, e => { e.stopPropagation(); e.preventDefault(); });
  }
  setInterval(tickIdle, 10000);
  window._idle = { state: idle, tick: tickIdle };    // reachable for testing
}

/* ---------------------------------------------------------- four-finger tap */

/** The starred set, or the first one if none is starred yet. */
async function playStarredGallery() {
  let sets = [];
  try { sets = await api.galleries(); } catch { sets = []; }
  if (!sets.length) return toast('No gallery sets yet — add one in Settings › Galleries', true);
  const starred = state.settings.starred_gallery;
  const pick = sets.find(s => s.id === starred) || sets[0];
  const { startScreensaver } = await import('./widgets/gallery.js');
  startScreensaver(pick.id, { interval: 20, fit: 'contain' });
}

/**
 * Four fingers, tapped twice, anywhere — plays the starred gallery.
 *
 * Counts the PEAK number of simultaneous touches per gesture rather than the
 * count at any instant: four fingers never land or lift together, so an
 * exact-match test would almost never fire. A gesture ends when the last
 * finger lifts; two qualifying gestures inside the window trigger.
 */
function initFourFingerTap() {
  const down = new Set();
  let peak = 0;
  let lastTap = 0;
  const WINDOW_MS = 700;

  document.addEventListener('pointerdown', e => {
    if (e.pointerType !== 'touch') return;
    // On a dimmed screen the first touch is a wake, not a gesture.
    if (!document.getElementById('wakeCatch').hidden) return;
    down.add(e.pointerId);
    peak = Math.max(peak, down.size);
  }, true);

  const lift = e => {
    if (e.pointerType !== 'touch' || !down.has(e.pointerId)) return;
    down.delete(e.pointerId);
    if (down.size) return;                       // still mid-gesture
    const wasFour = peak >= 4;
    peak = 0;
    if (!wasFour) { lastTap = 0; return; }       // a non-four gesture breaks the pair
    const now = Date.now();
    if (now - lastTap < WINDOW_MS) {
      lastTap = 0;
      playStarredGallery();
    } else {
      lastTap = now;
    }
  };
  document.addEventListener('pointerup', lift, true);
  document.addEventListener('pointercancel', lift, true);

  window._fourFinger = { play: playStarredGallery };   // reachable for testing
}

/* -------------------------------------------------------------- scrollbars */

/* Thumbs are transparent by default (app.css); the element that is actually
   scrolling gets .scrolling-now for the duration plus a beat. Hover can't
   drive this on a touch panel — actual scroll motion is the only honest
   signal. Capture phase because scroll events don't bubble. */
function initScrollbars() {
  const timers = new WeakMap();
  document.addEventListener('scroll', e => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    t.classList.add('scrolling-now');
    clearTimeout(timers.get(t));
    timers.set(t, setTimeout(() => t.classList.remove('scrolling-now'), 700));
  }, true);
}

/* ---------------------------------------------------------- touch feedback */

/* Faint tinted circles under fingers. Touch pointers only — a mouse already
   shows where it is, and the X server runs -nocursor so the panel never draws
   an arrow. Lives outside #app so the ring stays visible on a dimmed screen,
   confirming the wake tap registered. */
function initTouchFeedback() {
  const dots = new Map();   // pointerId -> element

  const allowed = () => (state.settings.touch_ripples ?? 'true') !== 'false';

  document.addEventListener('pointerdown', e => {
    if (e.pointerType !== 'touch' || !allowed()) return;
    const pulse = el('div.touch-pulse', {
      style: { left: e.clientX + 'px', top: e.clientY + 'px' },
    });
    pulse.addEventListener('animationend', () => pulse.remove());
    document.body.append(pulse);

    const dot = el('div.touch-dot', {
      style: { left: e.clientX + 'px', top: e.clientY + 'px' },
    });
    document.body.append(dot);
    dots.set(e.pointerId, dot);
  }, true);

  document.addEventListener('pointermove', e => {
    const dot = dots.get(e.pointerId);
    if (dot) {
      dot.style.left = e.clientX + 'px';
      dot.style.top = e.clientY + 'px';
    }
  }, true);

  const lift = e => {
    const dot = dots.get(e.pointerId);
    if (dot) {
      dots.delete(e.pointerId);
      dot.classList.add('lifting');
      setTimeout(() => dot.remove(), 200);
    }
  };
  document.addEventListener('pointerup', lift, true);
  document.addEventListener('pointercancel', lift, true);
}

document.addEventListener('DOMContentLoaded', boot);
