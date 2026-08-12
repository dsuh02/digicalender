/* Modal sheets and the schema-driven options form.
 *
 * Every widget declares a `settings` schema; this file turns that schema into
 * the options panel, so adding a setting to a widget is a one-line change and
 * never involves writing form markup. Same idea as a Home Assistant card
 * editor, minus the YAML.
 *
 * Field types: text, textarea, number, slider, toggle, select, color, icon,
 *              device, devices, scene, latlon, time.
 */

import { api } from './api.js';
import { ICON_NAMES, icon } from './icons.js';
import { eventPalette, getTheme } from './theme.js';
import { clear, el } from './util.js';

let backdrop = null;

function ensureRoot() {
  if (!backdrop) {
    backdrop = el('div.sheet-backdrop', { hidden: true });
    backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });
    document.body.append(backdrop);
  }
  return backdrop;
}

export function close() {
  if (backdrop) { backdrop.hidden = true; clear(backdrop); }
}

/** Generic sheet. `body` is a node; `actions` is a list of {label, kind, onClick}.
    `footerStart` renders bottom-left, before the spacer — provenance chips and
    the like, facts that belong in the footer without being buttons. */
export function openSheet({ title, body, actions = [], wide = false, footerStart = null }) {
  const root = ensureRoot();
  clear(root);
  const panel = el('div.sheet' + (wide ? '.sheet-wide' : ''), { role: 'dialog', 'aria-modal': 'true' }, [
    el('div.sheet-grip'),
    el('h2.sheet-title', { text: title }),
    el('div.sheet-body', {}, [body]),
    el('div.sheet-actions', {}, [
      footerStart,
      el('div.spacer'),
      ...actions.map(a => el('button.btn' + (a.kind ? `.btn-${a.kind}` : ''), {
        text: a.label, onclick: () => a.onClick?.(),
      })),
    ]),
  ]);
  root.append(panel);
  root.hidden = false;
  return panel;
}

export function confirmSheet(title, message, onYes, yesLabel = 'Delete') {
  openSheet({
    title,
    body: el('p.sheet-note', { text: message }),
    actions: [
      { label: 'Cancel', onClick: close },
      { label: yesLabel, kind: 'danger', onClick: () => { close(); onYes(); } },
    ],
  });
}

export function toast(msg, isErr = false) {
  let t = document.getElementById('toast');
  if (!t) {
    t = el('div.toast', { id: 'toast', hidden: true });
    document.body.append(t);
  }
  t.textContent = msg;
  t.classList.toggle('err', isErr);
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, isErr ? 6000 : 2000);
}

/* ------------------------------------------------------------- form fields */

function field(label, control, help) {
  return el('label.field', {}, [
    el('span.field-label', { text: label }),
    control,
    help ? el('span.field-help', { text: help }) : null,
  ]);
}

async function buildControl(f, values, onChange) {
  const val = values[f.key] ?? f.default ?? '';

  switch (f.type) {
    case 'textarea': {
      const c = el('textarea.input', { rows: 3, placeholder: f.placeholder || '' });
      c.value = val;
      c.addEventListener('input', () => onChange(f.key, c.value));
      return field(f.label, c, f.help);
    }
    case 'number': {
      const c = el('input.input', {
        type: 'number', min: f.min ?? 0, max: f.max ?? 9999, step: f.step ?? 1, value: val,
      });
      c.addEventListener('input', () => onChange(f.key, c.value === '' ? null : Number(c.value)));
      return field(f.label, c, f.help);
    }
    case 'slider': {
      const out = el('span.slider-value', { text: String(val) });
      const c = el('input.slider', {
        type: 'range', min: f.min ?? 0, max: f.max ?? 100, step: f.step ?? 1, value: val,
      });
      c.addEventListener('input', () => {
        out.textContent = c.value;
        onChange(f.key, Number(c.value));
      });
      return field(f.label, el('div.slider-row', {}, [c, out]), f.help);
    }
    case 'toggle': {
      const input = el('input.switch-input', { type: 'checkbox' });
      input.checked = !!val;
      input.addEventListener('change', () => onChange(f.key, input.checked));
      return el('label.row-toggle', {}, [
        el('span.field-label', { text: f.label }),
        input, el('span.switch'),
        f.help ? el('span.field-help', { text: f.help }) : null,
      ]);
    }
    case 'select': {
      const c = el('select.input');
      for (const o of f.options || []) {
        const opt = el('option', { value: o.value, text: o.label });
        if (String(o.value) === String(val)) opt.selected = true;
        c.append(opt);
      }
      c.addEventListener('change', () => onChange(f.key, c.value));
      return field(f.label, c, f.help);
    }
    case 'color': {
      const row = el('div.swatches');
      const palette = f.options ||
        eventPalette(getTheme()).map(c => ({ value: c.value, label: c.name }));
      let current = val || palette[0].value;
      const paint = () => [...row.children].forEach(b =>
        b.setAttribute('aria-pressed', String(b.dataset.value === current)));
      palette.forEach(c => {
        row.append(el('button.swatch', {
          type: 'button', dataset: { value: c.value },
          style: { background: c.value }, 'aria-label': c.label,
          onclick: () => { current = c.value; paint(); onChange(f.key, current); },
        }));
      });
      paint();
      return field(f.label, row, f.help);
    }
    case 'icon': {
      const row = el('div.icon-picker');
      let current = val || 'sparkles';
      const paint = () => [...row.children].forEach(b =>
        b.setAttribute('aria-pressed', String(b.dataset.name === current)));
      ICON_NAMES.forEach(n => {
        row.append(el('button.icon-choice', {
          type: 'button', dataset: { name: n }, 'aria-label': n,
          onclick: () => { current = n; paint(); onChange(f.key, current); },
        }, [icon(n, 22)]));
      });
      paint();
      return field(f.label, row, f.help);
    }
    case 'device':
    case 'devices': {
      const multi = f.type === 'devices';
      const list = el('div.picker-list');
      const chosen = new Set(multi ? (Array.isArray(val) ? val : []) : (val ? [val] : []));
      let devices = [];
      try { devices = await api.devices(); } catch { devices = []; }
      if (f.kinds) devices = devices.filter(d => f.kinds.includes(d.kind));
      if (!devices.length) {
        list.append(el('p.sheet-note', {
          text: 'No matching devices yet — add one from Settings > Devices.',
        }));
      }
      const paint = () => [...list.children].forEach(b =>
        b.dataset?.id && b.setAttribute('aria-pressed', String(chosen.has(b.dataset.id))));
      devices.forEach(d => {
        list.append(el('button.picker-item', {
          type: 'button', dataset: { id: d.id },
          onclick: () => {
            if (multi) chosen.has(d.id) ? chosen.delete(d.id) : chosen.add(d.id);
            else { chosen.clear(); chosen.add(d.id); }
            paint();
            onChange(f.key, multi ? [...chosen] : [...chosen][0] || '');
          },
        }, [
          el('span.picker-name', { text: d.name }),
          el('span.picker-meta', { text: `${d.kind}${d.room ? ' · ' + d.room : ''}` }),
        ]));
      });
      paint();
      return field(f.label, list, f.help);
    }
    case 'gallery': {
      const c = el('select.input');
      c.append(el('option', { value: '', text: '— pick a gallery set —' }));
      let sets = [];
      try { sets = await api.galleries(); } catch { sets = []; }
      sets.forEach(s => {
        const o = el('option', { value: s.id, text: `${s.name} (${s.image_count})` });
        if (s.id === val) o.selected = true;
        c.append(o);
      });
      c.addEventListener('change', () => onChange(f.key, c.value));
      return field(f.label, c, f.help);
    }
    case 'scene': {
      const c = el('select.input');
      c.append(el('option', { value: '', text: '— none —' }));
      let scenes = [];
      try { scenes = await api.scenes(); } catch { scenes = []; }
      scenes.forEach(s => {
        const o = el('option', { value: s.id, text: s.name });
        if (s.id === val) o.selected = true;
        c.append(o);
      });
      c.addEventListener('change', () => onChange(f.key, c.value));
      return field(f.label, c, f.help);
    }
    case 'latlon': {
      const lat = el('input.input', { type: 'number', step: '0.0001', placeholder: 'Latitude', value: values.lat ?? f.defaultLat ?? '' });
      const lon = el('input.input', { type: 'number', step: '0.0001', placeholder: 'Longitude', value: values.lon ?? f.defaultLon ?? '' });
      lat.addEventListener('input', () => onChange('lat', Number(lat.value)));
      lon.addEventListener('input', () => onChange('lon', Number(lon.value)));
      const locate = el('button.btn.btn-small', {
        type: 'button', text: 'Use this device',
        onclick: () => navigator.geolocation?.getCurrentPosition(pos => {
          lat.value = pos.coords.latitude.toFixed(4);
          lon.value = pos.coords.longitude.toFixed(4);
          onChange('lat', Number(lat.value));
          onChange('lon', Number(lon.value));
        }, () => toast('Location unavailable on this device', true)),
      });
      return field(f.label, el('div.latlon', {}, [lat, lon, locate]), f.help);
    }
    case 'time': {
      const c = el('input.input', { type: 'time', value: val || '' });
      c.addEventListener('input', () => onChange(f.key, c.value));
      return field(f.label, c, f.help);
    }
    default: {
      const c = el('input.input', {
        type: f.type === 'password' ? 'password' : 'text',
        placeholder: f.placeholder || '', value: val,
      });
      c.addEventListener('input', () => onChange(f.key, c.value));
      return field(f.label, c, f.help);
    }
  }
}

/**
 * Build a form from a schema. Returns {node, values} — values mutates live as
 * the user edits, so the caller just reads it on save.
 */
export async function buildForm(schema, initial = {}) {
  const values = { ...initial };
  const node = el('div.form');
  const deps = [];                       // {key, node, invert}
  const applyDeps = () => {
    for (const d of deps) {
      const on = d.invert ? !values[d.key] : !!values[d.key];
      d.node.classList.toggle('field-disabled', !on);
      d.node.querySelectorAll('input, select, textarea, button')
        .forEach(c => { c.disabled = !on; });
    }
  };
  const onChange = (k, v) => { values[k] = v; applyDeps(); };
  for (const f of schema) {
    if (f.section) {
      node.append(el('h3.form-section', { text: f.section }));
      continue;
    }
    // Seed from the field's own default when `initial` does not carry the key.
    // Without this a control RENDERS its default while `values` stays empty, so
    // a caller that saves everything writes blanks and zeroes for every field
    // the user did not happen to touch — silently erasing whatever was stored.
    if (!(f.key in values)) values[f.key] = f.default ?? '';
    const built = await buildControl(f, values, onChange);
    if (f.disabledWhen) deps.push({ key: f.disabledWhen, node: built, invert: true });
    if (f.enabledWhen) deps.push({ key: f.enabledWhen, node: built, invert: false });
    node.append(built);
  }
  applyDeps();
  return { node, values };
}

/**
 * Options every widget gets, appended to its own.
 *
 * Sizing is a property of the box, not of any one widget type, and the rule is
 * the user's to set: either the app picks a content size from the box, or they
 * pin it themselves. Nothing is ever hidden for being small — content shrinks.
 */
export const COMMON_SETTINGS = [
  { section: 'Size' },
  { key: 'autoScale', label: 'Let the app size the contents', type: 'toggle', default: true,
    help: 'Scales type, rows and graphics to the box. Turn off to set it yourself.' },
  { key: 'contentScale', label: 'Content size (%)', type: 'slider',
    min: 25, max: 250, step: 5, default: 100, disabledWhen: 'autoScale',
    help: 'Best effort to fit everything in the box at this size.' },
];

/** The widget options panel. */
export async function openWidgetSettings(def, widget, onSave) {
  const schema = [...(def.settings || []), ...COMMON_SETTINGS];
  const { node, values } = await buildForm(schema, widget.settings || {});
  openSheet({
    title: `${def.name} options`,
    body: node,
    actions: [
      { label: 'Cancel', onClick: close },
      { label: 'Save', kind: 'primary', onClick: () => { close(); onSave(values); } },
    ],
  });
}
