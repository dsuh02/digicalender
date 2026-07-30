/* App shell: pages, edit mode, the widget palette, device management.
 *
 * Edit mode is modal on purpose (the lock button in the top bar). On a touch
 * panel that lives on a wall, a drag gesture that's always live means every
 * accidental brush rearranges the screen — so widgets are inert until you
 * explicitly unlock, exactly like the iOS home screen.
 */

import { api, bus, connectStream, liveStates } from './core/api.js';
import { Grid } from './core/grid.js';
import { icon } from './core/icons.js';
import {
  buildForm, close, confirmSheet, openSheet, openWidgetSettings, toast,
} from './core/sheet.js';
import { clear, debounce, el } from './core/util.js';
import { CATEGORIES, WIDGETS, widgetDef } from './widgets/index.js';

const state = {
  pages: [],
  settings: {},
  pageIndex: 0,
  editing: false,
  grid: null,
  instances: new Map(),   // widget id -> {destroy, refresh}
};

const dom = {};

/* ------------------------------------------------------------------- boot */

async function boot() {
  dom.tabs = document.getElementById('tabs');
  dom.stage = document.getElementById('stage');
  dom.editBtn = document.getElementById('editBtn');
  dom.addBtn = document.getElementById('addBtn');
  dom.settingsBtn = document.getElementById('settingsBtn');
  dom.status = document.getElementById('status');
  dom.pageBtn = document.getElementById('pageBtn');

  dom.editBtn.addEventListener('click', toggleEdit);
  dom.addBtn.addEventListener('click', openPalette);
  dom.settingsBtn.addEventListener('click', openSettings);
  dom.pageBtn.addEventListener('click', openPageManager);

  bus.on('connected', ok => {
    dom.status.classList.toggle('bad', !ok);
    dom.status.title = ok ? 'Live' : 'Reconnecting…';
  });
  // Another client (a phone) rearranged things — pick it up, unless we're the
  // one editing, in which case our own in-flight layout wins.
  bus.on('layout_changed', () => { if (!state.editing) reload(); });

  await reload();
  connectStream();
  applyNightDim();
  setInterval(applyNightDim, 60000);
  initSwipe();
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
  if (!state.pages.length) {
    dom.stage.replaceChildren(el('p.empty-hint', { text: 'No pages yet' }));
    return;
  }
  state.pageIndex = Math.min(state.pageIndex, state.pages.length - 1);
  renderTabs();
  renderPage();
}

/* ------------------------------------------------------------------ pages */

function renderTabs() {
  clear(dom.tabs);
  state.pages.forEach((p, i) => {
    dom.tabs.append(el('button.tab', {
      text: p.name,
      'aria-selected': String(i === state.pageIndex),
      onclick: () => { state.pageIndex = i; renderTabs(); renderPage(); },
    }));
  });
}

function teardownWidgets() {
  for (const inst of state.instances.values()) {
    try { inst.destroy?.(); } catch { /* a broken widget must not block the rest */ }
  }
  state.instances.clear();
}

function renderPage() {
  teardownWidgets();
  clear(dom.stage);
  const page = state.pages[state.pageIndex];
  if (!page) return;

  const host = el('div.grid-host');
  dom.stage.append(host);

  const saveLayout = debounce(async (layout) => {
    try { await api.saveLayout(layout); } catch (e) { toast(e.message, true); }
  }, 400);

  state.grid = new Grid(host, page, {
    onChange: saveLayout,
    onEdit: w => editWidget(w),
    onDelete: w => confirmSheet('Remove widget?',
      `“${widgetDef(w.type)?.name || w.type}” will be removed from this page.`,
      async () => {
        try {
          await api.deleteWidget(w.id);
          state.instances.get(w.id)?.destroy?.();
          state.instances.delete(w.id);
          state.grid.remove(w.id);
          page.widgets = page.widgets.filter(x => x.id !== w.id);
        } catch (e) { toast(e.message, true); }
      }),
  });
  state.grid.setEditing(state.editing);

  for (const w of page.widgets) mountWidget(w);
}

function mountWidget(w) {
  const def = widgetDef(w.type);
  const content = el('div.w-content');
  state.grid.add(w, content);

  if (!def) {
    content.append(el('p.empty-hint', { text: `Unknown widget: ${w.type}` }));
    return;
  }
  const ctx = { widget: w, settings: w.settings || {}, bus };
  try {
    const inst = def.render(content, ctx) || {};
    state.instances.set(w.id, inst);
  } catch (e) {
    // One broken widget must not take the whole wall down.
    console.error(`widget ${w.type} failed`, e);
    content.append(el('p.empty-hint', { text: `${def.name} failed to load` }));
  }
}

function remountWidget(w) {
  state.instances.get(w.id)?.destroy?.();
  state.instances.delete(w.id);
  state.grid.remove(w.id);
  mountWidget(w);
}

/* -------------------------------------------------------------- edit mode */

function toggleEdit() {
  state.editing = !state.editing;
  state.grid?.setEditing(state.editing);
  document.body.classList.toggle('editing', state.editing);
  clear(dom.editBtn);
  dom.editBtn.append(icon(state.editing ? 'unlock' : 'lock', 22));
  dom.editBtn.setAttribute('aria-pressed', String(state.editing));
  dom.addBtn.hidden = !state.editing;
  dom.pageBtn.hidden = !state.editing;
  if (state.editing) toast('Edit mode — drag to move, corner to resize');
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
  const page = state.pages[state.pageIndex];
  const body = el('div.palette');
  for (const cat of CATEGORIES) {
    body.append(el('h3.form-section', { text: cat }));
    const row = el('div.palette-row');
    WIDGETS.filter(w => w.category === cat).forEach(def => {
      row.append(el('button.palette-item', {
        onclick: async () => {
          close();
          const size = def.defaultSize || { w: 12, h: 10 };
          const slot = state.grid.findSlot(size.w, size.h, def.minSize);
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
            mountWidget(w);
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
  const page = state.pages[state.pageIndex];
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
          state.pageIndex = state.pages.length - 1;
          renderTabs(); renderPage();
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

async function openSettings() {
  const body = el('div');
  const tabs = el('div.subtabs');
  const panel = el('div.subpanel');
  body.append(tabs, panel);

  const views = {
    Devices: renderDevices,
    Display: renderDisplay,
  };
  let active = 'Devices';
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
  openSheet({ title: 'Settings', body, wide: true, actions: [{ label: 'Close', onClick: close }] });
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
      text: 'Nothing found. Rokus answer only when powered; Samsung TVs stop responding entirely when off; Govee plugs have no LAN API at all and must be added with a cloud API key.',
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
      const payload = {
        name: name.value.trim(), kind, room: room.value.trim(), config: form.values,
      };
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
  const wrap = el('div', {}, [
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
  return wrap;
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
  // The window normally wraps midnight, so "inside" flips depending on order.
  const inWindow = a <= b ? (cur >= a && cur < b) : (cur >= a || cur < b);
  document.documentElement.style.setProperty(
    '--dim', inWindow ? String(Number(s.night_level || 45) / 100) : '1');
}

/* ----------------------------------------------------------------- swipe */

function initSwipe() {
  let x0 = null, y0 = null, t0 = 0;
  dom.stage.addEventListener('pointerdown', e => {
    if (state.editing) return;                       // dragging widgets wins
    if (e.target.closest('.w-content button, .w-content input, .tv-scroll, .todo-list')) return;
    x0 = e.clientX; y0 = e.clientY; t0 = Date.now();
  }, { passive: true });

  dom.stage.addEventListener('pointerup', e => {
    if (x0 === null) return;
    const dx = e.clientX - x0, dy = e.clientY - y0, dt = Date.now() - t0;
    x0 = null;
    if (dt < 700 && Math.abs(dx) > 90 && Math.abs(dx) > Math.abs(dy) * 1.6) {
      const next = state.pageIndex + (dx < 0 ? 1 : -1);
      if (next >= 0 && next < state.pages.length) {
        state.pageIndex = next;
        renderTabs();
        renderPage();
      }
    }
  }, { passive: true });
}

document.addEventListener('DOMContentLoaded', boot);
