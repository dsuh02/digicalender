/* The alarm editor and run-report sheets.
 *
 * In core rather than main.js because the Alarms WIDGET needs them too, and a
 * widget importing main.js would be a cycle — main.js imports the widget
 * registry. Refresh is a callback for the same reason: the settings panel wants
 * to repaint itself, a widget wants to reload its own list, and neither should
 * know about the other.
 *
 * Every field here belongs to ONE alarm. Days, volume, what plays and which
 * Roku it plays on are columns on that alarm's row — two alarms can wake
 * different rooms with different music on different days.
 */

import { api } from './api.js';
import { close, confirmSheet, openSheet, toast } from './sheet.js';
import { clear, el } from './util.js';

export const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/** "Mon Tue Wed" / "every day" / "weekdays" — short enough for a widget row. */
export function daySummary(days) {
  const d = [...(days || [])].sort((a, b) => a - b);
  if (!d.length) return 'every day';
  if (d.length === 7) return 'every day';
  if (d.join() === '0,1,2,3,4') return 'weekdays';
  if (d.join() === '5,6') return 'weekends';
  return d.map(i => DAY_LABELS[i]).join(' ');
}

/** Milliseconds until this alarm next fires, or null if it never will. */
export function nextFire(alarm, from = new Date()) {
  if (!alarm || alarm.enabled === false) return null;
  const m = /^(\d{2}):(\d{2})$/.exec(alarm.at_time || '');
  if (!m) return null;
  const days = (alarm.days && alarm.days.length) ? alarm.days.map(Number) : [0, 1, 2, 3, 4, 5, 6];
  for (let ahead = 0; ahead <= 7; ahead++) {
    const d = new Date(from);
    d.setDate(d.getDate() + ahead);
    d.setHours(Number(m[1]), Number(m[2]), 0, 0);
    // JS weeks start on Sunday; the stored days start on Monday.
    const dow = (d.getDay() + 6) % 7;
    if (!days.includes(dow)) continue;
    if (d.getTime() > from.getTime()) return d.getTime() - from.getTime();
  }
  return null;
}

export function untilText(ms) {
  if (ms == null) return '';
  const mins = Math.round(ms / 60000);
  if (mins < 60) return `in ${mins} min`;
  const h = Math.floor(mins / 60), mm = mins % 60;
  if (h < 24) return `in ${h}h${mm ? ` ${mm}m` : ''}`;
  return `in ${Math.round(h / 24)}d`;
}

export function showAlarmRun(alarm, res) {
  const body = el('div');
  body.append(el('p.sheet-note', { text: res.ok ? 'Ran clean.' : 'Some steps failed.' }));
  const list = el('div.dev-list');
  for (const s of res.steps || []) {
    list.append(el('div.dev-row', {}, [
      el('span.src-dot', { style: { background: s.ok ? 'var(--good)' : 'var(--danger)' } }),
      el('div.dev-main', {}, [
        el('div.dev-name', { text: s.step }),
        el('div.dev-meta', { text: `${s.detail || (s.ok ? 'ok' : 'failed')} · ${s.ms}ms` }),
      ]),
    ]));
  }
  body.append(list);
  openSheet({ title: `${alarm.name}`, body, actions: [{ label: 'Done', kind: 'primary', onClick: close }] });
}

export async function openAlarmEditor(alarm, data, onSaved) {
  const isNew = !alarm.id;
  const devices = (await api.devices().catch(() => [])).filter(d => d.kind === 'roku');

  const body = el('div');
  const name = el('input.input', { type: 'text', value: alarm.name || '' });
  const at = el('input.input', { type: 'time', value: alarm.at_time || '07:00' });
  const wait = el('input.input', { type: 'number', min: 0, max: 120, value: alarm.wait_seconds ?? 13 });
  const vol = el('input.input', { type: 'number', min: 0, max: 100,
                                  value: alarm.volume == null ? '' : alarm.volume });
  const uri = el('input.input', { type: 'text', value: alarm.spotify_uri || '',
                                  placeholder: 'spotify:playlist:… or an open.spotify.com link' });
  const connectName = el('input.input', {
    type: 'text', value: alarm.device_name || '',
    placeholder: 'defaults to the Roku above',
  });
  if (!alarm.device_name) connectName.dataset.auto = '1';
  connectName.addEventListener('input', () => { connectName.dataset.auto = '0'; });
  const enabled = el('input.switch-input', { type: 'checkbox' });
  enabled.checked = alarm.enabled !== false;
  const shuffle = el('input.switch-input', { type: 'checkbox' });
  shuffle.checked = !!alarm.shuffle;

  const devSel = el('select.input');
  devSel.append(el('option', { value: '', text: '— pick a Roku —' }));
  devices.forEach(d => {
    const o = el('option', { value: d.id, text: `${d.name} (${d.config?.ip || '?'})` });
    if (d.id === alarm.device_id) o.selected = true;
    devSel.append(o);
  });
  // Keep the Connect target in step with the Roku unless it has been typed
  // over. Leaving these two to drift apart means launching Spotify on one
  // device and searching for another.
  const syncConnectName = () => {
    const chosen = devices.find(d => d.id === devSel.value);
    if (!chosen) return;
    if (!connectName.value.trim() || connectName.dataset.auto === '1') {
      connectName.value = chosen.name;
      connectName.dataset.auto = '1';
    }
  };
  devSel.addEventListener('change', syncConnectName);

  const dayRow = el('div.day-row');
  const dayBtns = DAY_LABELS.map((lbl, i) => {
    const on = (alarm.days || []).includes(i);
    const b = el('button.day-btn', { type: 'button', text: lbl, 'aria-pressed': String(on) });
    b.addEventListener('click', (e) => {
      e.preventDefault();
      b.setAttribute('aria-pressed', b.getAttribute('aria-pressed') === 'true' ? 'false' : 'true');
    });
    dayRow.append(b);
    return b;
  });

  /* Your own playlists, most recently played first — because picking what to
     wake up to should be a tap, not a paste. Search stays underneath for
     anything not already in the library. */
  const mine = el('div.pl-list');
  const mineNote = el('div.field-help', { text: 'Loading your playlists…' });

  const markChosen = () => {
    const cur = uri.value.trim();
    mine.querySelectorAll('.pl-row').forEach(r => {
      r.classList.toggle('chosen', r.dataset.uri === cur);
    });
  };

  const loadMine = async () => {
    try {
      const lib = await api.spotifyPlaylists();
      clear(mine);
      mineNote.textContent = lib.recency
        ? `${lib.count} playlists, most recently played first.`
        : `${lib.count} playlists, in Spotify's own order — re-authorise to sort by what you actually play.`;
      lib.playlists.forEach((pl) => {
        const row = el('button.pl-row', {
          type: 'button', dataset: { uri: pl.uri },
          onclick: (e) => {
            e.preventDefault();
            uri.value = pl.uri;
            uri.dispatchEvent(new Event('input'));
            markChosen();
          },
        }, [
          el('span.pl-name', { text: pl.name }),
          el('span.pl-meta', {
            text: [pl.recent_rank != null ? 'recent' : '', pl.owner].filter(Boolean).join(' · '),
          }),
        ]);
        mine.append(row);
      });
      markChosen();
    } catch (e) {
      mineNote.textContent = e.message;
    }
  };

  /* Search, for anything not already in the library. */
  const searchBox = el('input.input', { type: 'text', placeholder: 'Search all of Spotify…' });
  const results = el('div.dev-list');
  searchBox.addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    clear(results);
    try {
      const rows = await api.spotifySearch(searchBox.value);
      if (!rows.length) results.append(el('p.sheet-note', { text: 'Nothing found.' }));
      rows.forEach(r => results.append(el('div.dev-row', {}, [
        el('div.dev-main', {}, [
          el('div.dev-name', { text: r.name }),
          el('div.dev-meta', { text: `${r.kind}${r.by ? ` · ${r.by}` : ''}` }),
        ]),
        el('button.btn.btn-small', {
          text: 'Use', onclick: (ev) => { ev.preventDefault(); uri.value = r.uri; toast(r.name); },
        }),
      ])));
    } catch (err) { results.append(el('p.sheet-note', { text: err.message })); }
  });

  body.append(
    el('label.field', {}, [el('span.field-label', { text: 'Name' }), name]),
    el('label.field', {}, [el('span.field-label', { text: 'Time' }), at]),
    el('div.field', {}, [
      el('span.field-label', { text: 'Days' }), dayRow,
      el('span.field-help', { text: 'None selected means every day.' }),
    ]),
    el('label.row-toggle', {}, [
      el('span.field-label', { text: 'Enabled' }), enabled, el('span.switch'),
    ]),

    el('h3.form-section', { text: 'Sequence' }),
    el('label.field', {}, [el('span.field-label', { text: 'Roku' }), devSel]),
    el('label.field', {}, [
      el('span.field-label', { text: 'Wait before opening Spotify (seconds)' }), wait,
      el('span.field-help', { text: 'Time for the box to boot and the TV to switch input.' }),
    ]),
    el('label.field', {}, [
      el('span.field-label', { text: 'Spotify Connect device name' }), connectName,
      el('span.field-help', {
        text: 'Matched by name once the Roku app registers itself. Leave blank and it follows the Roku chosen above.',
      }),
    ]),
    el('label.field', {}, [
      el('span.field-label', { text: 'Volume (%)' }), vol,
      el('span.field-help', {
        text: 'Set inside Spotify. A Roku box has no volume of its own — the TV or soundbar owns the real level, so set that once by hand and use this for fine control. Leave blank to change nothing.',
      }),
    ]),
    el('label.row-toggle', {}, [
      el('span.field-label', { text: 'Shuffle' }), shuffle, el('span.switch'),
    ]),

    el('h3.form-section', { text: 'What to play' }),
    mineNote,
    mine,
    el('label.field', {}, [
      el('span.field-label', { text: 'Spotify link or URI' }), uri,
      el('span.field-help', {
        text: 'Your own playlists, albums, artists and tracks. Spotify blocks apps from starting DJ, Daily Mix and Discover Weekly — those cannot be automated by anyone.',
      }),
    ]),
    el('label.field', {}, [el('span.field-label', { text: 'Search' }), searchBox]),
    results,
  );

  // The failure mode this prevents: an alarm that wakes the TV, opens Spotify,
  // and sits in silence — behaving exactly as configured.
  const noMusic = el('p.sheet-note.warn-note', {
    text: 'This alarm has no music set — it will wake the Roku and open Spotify, but play nothing.',
  });
  const refreshNoMusic = () => {
    noMusic.hidden = !!uri.value.trim();
    markChosen();
  };
  uri.addEventListener('input', refreshNoMusic);
  refreshNoMusic();
  body.append(noMusic);
  loadMine();

  const collect = () => ({
    name: name.value.trim() || 'Alarm',
    at_time: at.value,
    days: dayBtns.map((b, i) => (b.getAttribute('aria-pressed') === 'true' ? i : -1)).filter(i => i >= 0),
    enabled: enabled.checked,
    device_id: devSel.value || null,
    app_id: alarm.app_id || data.spotify_app_id,
    wait_seconds: Number(wait.value) || 0,
    volume: vol.value === '' ? null : Number(vol.value),
    spotify_uri: uri.value.trim(),
    device_name: connectName.value.trim(),
    shuffle: shuffle.checked,
  });

  const actions = [
    { label: 'Cancel', onClick: close },
    {
      label: 'Save', kind: 'primary', onClick: async () => {
        try {
          if (isNew) await api.createAlarm(collect());
          else await api.updateAlarm(alarm.id, collect());
          close();
          if (onSaved) onSaved();
        } catch (e) { toast(e.message, true); }
      },
    },
  ];
  if (!isNew) {
    actions.unshift({
      label: 'Delete', onClick: () => {
        close();
        confirmSheet('Delete this alarm?', `“${alarm.name}” will stop running.`, async () => {
          try { await api.deleteAlarm(alarm.id); } catch (e) { toast(e.message, true); }
          if (onSaved) onSaved();
        });
      },
    });
  }
  openSheet({ title: isNew ? 'New alarm' : 'Edit alarm', body, actions });
}
