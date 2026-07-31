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
import { CATEGORIES, WIDGETS, widgetDef } from './widgets/index.js';

const BAR_HIDE_MS = 7000;
const EDGE_PX = 32;          // how close to the top a pull-down must start

const state = {
  pages: [],
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
  dom.status = document.getElementById('status');
  dom.pageBtn = document.getElementById('pageBtn');

  dom.editBtn.addEventListener('click', toggleEdit);
  dom.addBtn.addEventListener('click', openPalette);
  dom.settingsBtn.addEventListener('click', () => openSettings());
  dom.pageBtn.addEventListener('click', openPageManager);
  window._openSettings = openSettings;   // calendar widgets jump to the Calendars tab

  bus.on('connected', ok => {
    dom.status.classList.toggle('bad', !ok);
    dom.status.title = ok ? 'Live' : 'Reconnecting…';
  });
  bus.on('layout_changed', () => { if (!state.editing) reload(); });

  await reload();
  connectStream();
  applyNightDim();
  setInterval(applyNightDim, 60000);
  initPager();
  initTopBar();
}

async function reload() {
  try {
    const data = await api.dashboard();
    state.pages = data.pages || [];
    state.settings = data.settings || {};
  } catch (e) {
    toast(`Could not load the dashboard: ${e.message}`, true);
    return;
  }
  applySavedTheme();
  state.pageIndex = clamp(state.pageIndex, 0, Math.max(0, state.pages.length - 1));
  renderTabs();
  renderAllPages();
}

function applySavedTheme() {
  try {
    const raw = state.settings.theme;
    applyTheme(raw ? { ...DEFAULT_THEME, ...JSON.parse(raw) } : DEFAULT_THEME);
  } catch {
    applyTheme(DEFAULT_THEME);
  }
}

/* ------------------------------------------------------------------ pages */

function renderTabs() {
  clear(dom.tabs);
  state.pages.forEach((p, i) => {
    dom.tabs.append(el('button.tab', {
      text: p.name,
      'aria-selected': String(i === state.pageIndex),
      onclick: () => setPage(i, true),
    }));
  });
}

function renderDots() {
  clear(dom.dots);
  if (state.pages.length < 2) return;
  state.pages.forEach((_, i) => {
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

  for (const page of state.pages) {
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
  return state.pages[state.pageIndex];
}

function currentGrid() {
  const p = currentPage();
  return p ? state.grids.get(p.id) : null;
}

function setPage(i, animate) {
  state.pageIndex = clamp(i, 0, Math.max(0, state.pages.length - 1));
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
    const min = -(state.pages.length - 1) * dom.stage.clientWidth;
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

  const body = el('div.form', {}, [
    el('label.field', {}, [el('span.field-label', { text: 'Page name' }), name]),
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

  const views = { Theme: renderThemeTab, Calendars: renderCalendars,
                  Devices: renderDevices, Display: renderDisplay };
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
      el('button.btn.btn-primary', { text: '+ Add calendar', onclick: () => editFeed(null) }),
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

async function editFeed(feed) {
  const isNew = !feed;
  const { node, values } = await buildForm([
    { key: 'url', label: 'Calendar address', type: 'textarea',
      placeholder: 'https://…/calendar.ics  or a Google embed link',
      help: isNew ? '' : 'Leave unchanged unless the address moved' },
    { key: 'name', label: 'Name', type: 'text', placeholder: 'Work, Family…',
      help: 'Optional — the feed’s own name is used when blank' },
    { key: 'color', label: 'Colour', type: 'color' },
  ], feed ? { url: feed.url, name: feed.name, color: feed.color } : {});

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
          const r = await api.createFeed({ url: values.url, name: values.name, color: values.color });
          toast(`Imported ${r.imported} events${r.warnings ? ` (${r.warnings} skipped)` : ''}`);
        } else {
          const patch = { name: values.name, color: values.color };
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
  all.forEach(d => body.append(el('div.dev-row', {}, [
    el('div.dev-main', {}, [
      el('div.dev-name', { text: d.name }),
      el('div.dev-meta', { text: `${d.kind}${d.ip ? ' · ' + d.ip : ''}${d.model ? ' · ' + d.model : ''}` }),
    ]),
    el('button.btn.btn-small.btn-primary', {
      text: 'Add', onclick: async (e) => {
        e.currentTarget.disabled = true;
        const config = {};
        if (d.ip) config.ip = d.ip;
        if (d.mac) config.mac = d.mac;
        if (d.sku) config.sku = d.sku;
        if (d.is_tv) config.is_tv = true;
        try {
          await api.createDevice({ name: d.name, kind: d.kind, config });
          toast(`${d.name} added`);
        } catch (err) { toast(err.message, true); }
      },
    }),
  ])));
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
    { key: 'night_dim', label: 'Dim overnight', type: 'toggle',
      default: s.night_dim === 'true', help: 'Softens the panel so it is not a lamp at 3am' },
    { key: 'night_start', label: 'Dim from', type: 'time', default: s.night_start || '22:00' },
    { key: 'night_end', label: 'Dim until', type: 'time', default: s.night_end || '07:00' },
    { key: 'night_level', label: 'Dim level (%)', type: 'slider', min: 10, max: 90,
      default: Number(s.night_level || 45) },
  ], {});
  return el('div', {}, [
    node,
    el('button.btn.btn-primary', {
      text: 'Save display settings',
      onclick: async () => {
        try {
          state.settings = await api.saveSettings({
            night_dim: String(!!values.night_dim),
            night_start: values.night_start || '22:00',
            night_end: values.night_end || '07:00',
            night_level: String(values.night_level ?? 45),
          });
          applyNightDim();
          toast('Saved');
        } catch (e) { toast(e.message, true); }
      },
    }),
  ]);
}

/* ------------------------------------------------------------- night dim */

function applyNightDim() {
  const s = state.settings || {};
  if (s.night_dim !== 'true') {
    document.documentElement.style.removeProperty('--dim');
    return;
  }
  const toMin = t => {
    const [h, m] = String(t || '0:0').split(':').map(Number);
    return (h || 0) * 60 + (m || 0);
  };
  const now = new Date();
  const cur = now.getHours() * 60 + now.getMinutes();
  const a = toMin(s.night_start || '22:00'), b = toMin(s.night_end || '07:00');
  const inWindow = a <= b ? (cur >= a && cur < b) : (cur >= a || cur < b);
  document.documentElement.style.setProperty(
    '--dim', inWindow ? String(Number(s.night_level || 45) / 100) : '1');
}

document.addEventListener('DOMContentLoaded', boot);
