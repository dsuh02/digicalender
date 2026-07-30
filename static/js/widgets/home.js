/* Smart-home widgets: device tiles, Roku remote, media transport, scenes.
 *
 * Interaction model borrowed from the consumer hubs that get this right —
 * Apple Home and the Nest Hub. A tile is one big target that does the obvious
 * thing on tap (toggle), tints itself when the device is on, greys out when
 * unreachable, and hides its detail behind a long-press rather than cluttering
 * the face of it. Nothing here needs a second tap to confirm.
 */

import { api, bus, liveStates } from '../core/api.js';
import { icon } from '../core/icons.js';
import { close, openSheet, toast } from '../core/sheet.js';
import { clear, el } from '../core/util.js';

const KIND_ICON = {
  roku: 'tv', samsung_tv: 'tv', govee_lan: 'bulb', govee_cloud: 'plug',
};

/** Long-press without breaking normal taps — 500ms, cancelled by movement. */
function onLongPress(node, fn) {
  let timer = null, moved = false;
  const cancel = () => { clearTimeout(timer); timer = null; };
  node.addEventListener('pointerdown', () => {
    moved = false;
    timer = setTimeout(() => { if (!moved) { node._suppressClick = true; fn(); } }, 500);
  });
  node.addEventListener('pointermove', () => { moved = true; cancel(); });
  node.addEventListener('pointerup', cancel);
  node.addEventListener('pointercancel', cancel);
}

async function runCommand(device, command, params = {}) {
  try {
    const res = await api.command(device.id, command, params);
    if (!res.ok) toast(res.message || 'Command failed', true);
    return res.ok;
  } catch (e) {
    toast(e.message, true);
    return false;
  }
}

function deviceDetail(device, state) {
  const rows = Object.entries(state || {})
    .filter(([k]) => !['limited_hint'].includes(k))
    .map(([k, v]) => el('div.kv', {}, [
      el('span.kv-k', { text: k }),
      el('span.kv-v', { text: typeof v === 'object' ? JSON.stringify(v) : String(v) }),
    ]));
  const body = el('div', {}, [
    el('div.detail-grid', {}, rows.length ? rows : [el('p.sheet-note', { text: 'No state reported yet.' })]),
    state?.limited_hint ? el('p.sheet-note.warn', { text: state.limited_hint }) : null,
  ]);

  const actions = [{ label: 'Close', onClick: close }];
  const caps = [];
  if (device.kind === 'roku' || device.kind === 'samsung_tv') {
    caps.push(['Vol −', 'volume_down'], ['Mute', 'mute'], ['Vol +', 'volume_up']);
  }
  const quick = el('div.quick-row', {}, caps.map(([label, cmd]) =>
    el('button.btn', { text: label, onclick: () => runCommand(device, cmd) })));
  if (caps.length) body.append(quick);

  openSheet({ title: device.name, body, actions });
}

function tile(device, opts = {}) {
  const state = liveStates.get(device.id) || device.state || {};
  const online = state.online !== false;
  const on = !!state.on;
  const accent = opts.color || (device.kind.startsWith('govee') ? '#ff9e64' : '#7aa2f7');

  const node = el('button.tile' + (on ? '.on' : '') + (online ? '' : '.offline'), {
    style: on ? { '--tile-accent': accent } : {},
    onclick: () => {
      if (node._suppressClick) { node._suppressClick = false; return; }
      if (!online) { deviceDetail(device, state); return; }
      runCommand(device, opts.tapCommand || 'toggle');
    },
  }, [
    el('div.tile-icon', {}, [icon(opts.icon || KIND_ICON[device.kind] || 'plug', 26)]),
    el('div.tile-name', { text: device.name }),
    el('div.tile-state', {
      text: !online ? 'Unreachable'
        : state.limited ? 'Limited mode'
        : on ? (state.active_app || 'On') : 'Off',
    }),
    device.room ? el('div.tile-room', { text: device.room }) : null,
  ]);
  onLongPress(node, () => deviceDetail(device, state));
  return node;
}

export const DeviceGridWidget = {
  type: 'device_grid', name: 'Device tiles', icon: 'grid', category: 'Home',
  defaultSize: { w: 16, h: 12 }, minSize: { w: 6, h: 5 },
  settings: [
    { key: 'deviceIds', label: 'Devices', type: 'devices',
      help: 'Leave empty to show every device' },
    { key: 'room', label: 'Only this room', type: 'text',
      help: 'Optional filter, matched exactly' },
    { key: 'columns', label: 'Columns', type: 'slider', min: 1, max: 8, default: 3 },
    { key: 'accent', label: 'On colour', type: 'color', default: '#7aa2f7' },
  ],
  render(host, ctx) {
    let devices = [];
    const body = el('div.tile-grid');
    host.append(body);

    const load = async () => {
      try { devices = await api.devices(); } catch { devices = []; }
      draw();
    };

    const draw = () => {
      clear(body);
      body.style.gridTemplateColumns = `repeat(${Number(ctx.settings.columns || 3)}, 1fr)`;
      let list = devices;
      const ids = ctx.settings.deviceIds;
      if (Array.isArray(ids) && ids.length) list = list.filter(d => ids.includes(d.id));
      if (ctx.settings.room) list = list.filter(d => d.room === ctx.settings.room);
      if (!list.length) {
        body.append(el('p.empty-hint', { text: 'No devices — add some in Settings → Devices' }));
        return;
      }
      list.forEach(d => body.append(tile(d, { color: ctx.settings.accent })));
    };

    const offState = bus.on('devices_state', draw);
    const offList = bus.on('devices_changed', load);
    load();
    return { refresh: load, destroy: () => { offState(); offList(); } };
  },
};

/* -------------------------------------------------------------- Roku remote */

const DPAD = [
  [null, { k: 'Up', t: '▲' }, null],
  [{ k: 'Left', t: '◀' }, { k: 'Select', t: 'OK', cls: 'ok' }, { k: 'Right', t: '▶' }],
  [null, { k: 'Down', t: '▼' }, null],
];

export const RokuRemoteWidget = {
  type: 'roku_remote', name: 'Roku remote', icon: 'remote', category: 'Home',
  defaultSize: { w: 10, h: 16 }, minSize: { w: 7, h: 12 },
  settings: [
    { key: 'deviceId', label: 'Roku', type: 'device', kinds: ['roku'] },
    { key: 'showVolume', label: 'Show volume keys', type: 'toggle', default: true,
      help: 'Only works on Roku TVs; sticks pass volume to the TV over HDMI' },
    { key: 'showApps', label: 'Show channel shortcuts', type: 'toggle', default: true },
  ],
  render(host, ctx) {
    let device = null;
    const body = el('div.remote');
    host.append(body);

    const press = (key) => device && runCommand(device, 'key', { key });

    const load = async () => {
      const id = ctx.settings.deviceId;
      if (!id) { device = null; draw(); return; }
      try { device = (await api.devices()).find(d => d.id === id) || null; }
      catch { device = null; }
      draw();
      if (device && ctx.settings.showApps !== false) loadApps();
    };

    const loadApps = async () => {
      try {
        const { apps, notice } = await api.deviceApps(device.id);
        const strip = body.querySelector('.remote-apps');
        if (!strip) return;
        clear(strip);
        if (notice) {
          strip.append(el('div.remote-note', { text: notice }));
          return;
        }
        apps.slice(0, 12).forEach(a => strip.append(el('button.app-btn', {
          title: a.name, onclick: () => runCommand(device, 'launch', { app_id: a.id }),
        }, [
          el('img.app-icon', { src: `/api/devices/${device.id}/icon/${a.id}`, alt: a.name,
                               loading: 'lazy' }),
        ])));
      } catch { /* leave the strip empty */ }
    };

    const draw = () => {
      clear(body);
      if (!device) {
        body.append(el('p.empty-hint', { text: 'Pick a Roku in this widget’s options' }));
        return;
      }
      const state = liveStates.get(device.id) || {};
      body.append(el('div.remote-head', {}, [
        el('span.remote-name', { text: device.name }),
        el('span.remote-status' + (state.online === false ? '.bad' : ''), {
          text: state.online === false ? 'offline' : (state.active_app || state.power || ''),
        }),
      ]));

      if (state.limited) {
        body.append(el('div.remote-note', { text: state.limited_hint }));
      }

      body.append(el('div.remote-row', {}, [
        el('button.rbtn', { text: '⏻', title: 'Power', onclick: () => runCommand(device, 'power_toggle') }),
        el('button.rbtn', { text: '⌂', title: 'Home', onclick: () => press('Home') }),
        el('button.rbtn', { text: '↩', title: 'Back', onclick: () => press('Back') }),
      ]));

      const pad = el('div.dpad');
      DPAD.forEach(row => row.forEach(cell => {
        pad.append(cell
          ? el('button.dbtn' + (cell.cls ? '.' + cell.cls : ''), {
              text: cell.t, onclick: () => press(cell.k) })
          : el('span'));
      }));
      body.append(pad);

      body.append(el('div.remote-row', {}, [
        el('button.rbtn', { text: '⏮', title: 'Rewind', onclick: () => press('Rev') }),
        el('button.rbtn', { text: '⏯', title: 'Play/pause', onclick: () => press('Play') }),
        el('button.rbtn', { text: '⏭', title: 'Forward', onclick: () => press('Fwd') }),
      ]));

      if (ctx.settings.showVolume !== false) {
        body.append(el('div.remote-row', {}, [
          el('button.rbtn', { text: '🔉', title: 'Volume down', onclick: () => runCommand(device, 'volume_down') }),
          el('button.rbtn', { text: '🔇', title: 'Mute', onclick: () => runCommand(device, 'mute') }),
          el('button.rbtn', { text: '🔊', title: 'Volume up', onclick: () => runCommand(device, 'volume_up') }),
        ]));
      }
      if (ctx.settings.showApps !== false) body.append(el('div.remote-apps'));
    };

    const off = bus.on('devices_state', id => { if (!id || id === ctx.settings.deviceId) draw(); });
    load();
    return { refresh: load, destroy: off };
  },
};

/* -------------------------------------------------------------------- media */

export const MediaWidget = {
  type: 'media', name: 'Media control', icon: 'speaker', category: 'Home',
  defaultSize: { w: 16, h: 10 }, minSize: { w: 8, h: 6 },
  settings: [
    { key: 'deviceIds', label: 'Devices', type: 'devices',
      kinds: ['roku', 'samsung_tv'],
      help: 'Transport and volume for each; handy for a TV plus a soundbar' },
  ],
  render(host, ctx) {
    let devices = [];
    const body = el('div.media');
    host.append(body);

    const load = async () => {
      try {
        const all = await api.devices();
        const ids = ctx.settings.deviceIds;
        devices = (Array.isArray(ids) && ids.length)
          ? all.filter(d => ids.includes(d.id))
          : all.filter(d => ['roku', 'samsung_tv'].includes(d.kind));
      } catch { devices = []; }
      draw();
    };

    const draw = () => {
      clear(body);
      if (!devices.length) {
        body.append(el('p.empty-hint', { text: 'No media devices selected' }));
        return;
      }
      devices.forEach(d => {
        const st = liveStates.get(d.id) || {};
        const offline = st.online === false;
        body.append(el('div.media-row' + (offline ? '.offline' : ''), {}, [
          el('div.media-info', {}, [
            el('div.media-name', {}, [icon(KIND_ICON[d.kind] || 'tv', 18), d.name]),
            el('div.media-sub', {
              text: offline ? 'unreachable'
                : st.active_app ? st.active_app
                : st.on ? 'on' : 'standby',
            }),
          ]),
          el('div.media-controls', {}, [
            el('button.rbtn', { text: '⏻', title: 'Power', onclick: () => runCommand(d, 'toggle') }),
            el('button.rbtn', { text: '⏯', title: 'Play/pause', onclick: () => runCommand(d, 'play_pause') }),
            el('button.rbtn', { text: '🔉', onclick: () => runCommand(d, 'volume_down') }),
            el('button.rbtn', { text: '🔇', onclick: () => runCommand(d, 'mute') }),
            el('button.rbtn', { text: '🔊', onclick: () => runCommand(d, 'volume_up') }),
          ]),
        ]));
      });
    };

    const offState = bus.on('devices_state', draw);
    const offList = bus.on('devices_changed', load);
    load();
    return { refresh: load, destroy: () => { offState(); offList(); } };
  },
};

/* ------------------------------------------------------------------- scenes */

export const ScenesWidget = {
  type: 'scenes', name: 'Scenes', icon: 'sparkles', category: 'Home',
  defaultSize: { w: 12, h: 10 }, minSize: { w: 5, h: 4 },
  settings: [
    { key: 'columns', label: 'Columns', type: 'slider', min: 1, max: 6, default: 2 },
    { key: 'showEditor', label: 'Allow editing here', type: 'toggle', default: true },
  ],
  render(host, ctx) {
    let scenes = [];
    const body = el('div.scene-grid');
    host.append(body);

    const load = async () => {
      try { scenes = await api.scenes(); } catch { scenes = []; }
      draw();
    };

    const draw = () => {
      clear(body);
      body.style.gridTemplateColumns = `repeat(${Number(ctx.settings.columns || 2)}, 1fr)`;
      scenes.forEach(s => {
        const btn = el('button.scene', { style: { '--scene': s.color || '#bb9af7' },
          onclick: async () => {
            if (btn._suppressClick) { btn._suppressClick = false; return; }
            btn.classList.add('running');
            try {
              const res = await api.runScene(s.id);
              toast(res.ok ? `${s.name} ✓`
                : res.results.find(r => !r.ok)?.message || 'Some steps failed', !res.ok);
            } catch (e) { toast(e.message, true); }
            btn.classList.remove('running');
          },
        }, [icon(s.icon || 'sparkles', 26), el('span.scene-name', { text: s.name })]);
        if (ctx.settings.showEditor !== false) onLongPress(btn, () => editScene(s, load));
        body.append(btn);
      });
      if (ctx.settings.showEditor !== false) {
        body.append(el('button.scene.scene-new', {
          onclick: () => editScene(null, load),
        }, [icon('plus', 26), el('span.scene-name', { text: 'New scene' })]));
      }
      if (!scenes.length && ctx.settings.showEditor === false) {
        body.append(el('p.empty-hint', { text: 'No scenes yet' }));
      }
    };

    load();
    return { refresh: load, destroy: () => {} };
  },
};

/** Scene editor: name, look, and an ordered list of device commands. */
async function editScene(scene, onDone) {
  let devices = [];
  try { devices = await api.devices(); } catch { devices = []; }
  const actions = [...(scene?.actions || [])];

  const name = el('input.input', { type: 'text', value: scene?.name || '', placeholder: 'Movie night' });
  const list = el('div.action-list');

  const COMMANDS = [
    ['power_on', 'Turn on'], ['power_off', 'Turn off'], ['toggle', 'Toggle'],
    ['play_pause', 'Play / pause'], ['mute', 'Mute'],
    ['volume_up', 'Volume up'], ['volume_down', 'Volume down'],
  ];

  const drawActions = () => {
    clear(list);
    if (!actions.length) list.append(el('p.sheet-note', { text: 'No steps yet.' }));
    actions.forEach((a, i) => {
      const devSel = el('select.input');
      devices.forEach(d => {
        const o = el('option', { value: d.id, text: `${d.name}${d.room ? ' · ' + d.room : ''}` });
        if (d.id === a.device_id) o.selected = true;
        devSel.append(o);
      });
      devSel.addEventListener('change', () => { a.device_id = devSel.value; });

      const cmdSel = el('select.input');
      COMMANDS.forEach(([v, l]) => {
        const o = el('option', { value: v, text: l });
        if (v === a.command) o.selected = true;
        cmdSel.append(o);
      });
      cmdSel.addEventListener('change', () => { a.command = cmdSel.value; });

      list.append(el('div.action-row', {}, [
        el('span.action-num', { text: String(i + 1) }), devSel, cmdSel,
        el('button.todo-del', { text: '✕', onclick: () => { actions.splice(i, 1); drawActions(); } }),
      ]));
    });
  };
  drawActions();

  const body = el('div.form', {}, [
    el('label.field', {}, [el('span.field-label', { text: 'Name' }), name]),
    el('div.field', {}, [
      el('span.field-label', { text: 'Steps' }), list,
      el('button.btn.btn-small', {
        text: '+ Add step',
        onclick: () => {
          if (!devices.length) return toast('Add a device first', true);
          actions.push({ device_id: devices[0].id, command: 'toggle', params: {} });
          drawActions();
        },
      }),
    ]),
  ]);

  const sheetActions = [];
  if (scene) {
    sheetActions.push({
      label: 'Delete', kind: 'danger', onClick: async () => {
        close();
        try { await api.deleteScene(scene.id); onDone(); } catch (e) { toast(e.message, true); }
      },
    });
  }
  sheetActions.push({ label: 'Cancel', onClick: close });
  sheetActions.push({
    label: 'Save', kind: 'primary', onClick: async () => {
      const payload = { name: name.value.trim() || 'Scene', actions };
      if (!payload.name) return;
      try {
        if (scene) await api.updateScene(scene.id, payload);
        else await api.createScene(payload);
        close(); onDone(); toast('Saved');
      } catch (e) { toast(e.message, true); }
    },
  });

  openSheet({ title: scene ? 'Edit scene' : 'New scene', body, actions: sheetActions });
}
