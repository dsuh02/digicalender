/* Alarms on the wall.
 *
 * Every setting here belongs to ONE alarm, not to the app: its days, its
 * volume, what it plays and which Roku it plays on are columns on that alarm's
 * own row. Two alarms can wake different rooms with different music on
 * different days, and turning one off leaves the others alone.
 *
 * The switch works in NORMAL mode, deliberately — killing tomorrow's alarm at
 * bedtime must not mean unlocking the layout first. Tapping the row opens the
 * full editor; tapping the switch only toggles.
 */

import { api, bus } from '../core/api.js';
import { daySummary, nextFire, openAlarmEditor, showAlarmRun, untilText }
  from '../core/alarmui.js';
import { icon } from '../core/icons.js';
import { toast } from '../core/sheet.js';
import { clear, el } from '../core/util.js';

export const AlarmsWidget = {
  type: 'alarms', name: 'Alarms', icon: 'clock', category: 'Home',
  defaultSize: { w: 14, h: 12 }, minSize: { w: 5, h: 4 },
  settings: [
    { key: 'showNext', label: 'Show when each one next fires', type: 'toggle', default: true },
    { key: 'showDetail', label: 'Show what plays and where', type: 'toggle', default: true },
    { key: 'onlyEnabled', label: 'Hide disabled alarms', type: 'toggle', default: false },
  ],

  render(host, ctx) {
    const body = el('div.alarms');
    host.append(body);
    let data = { alarms: [], spotify_app_id: '22297' };
    let devices = [];
    let tick = null;

    const load = async () => {
      try { data = await api.alarms(); } catch { data = { alarms: [], spotify_app_id: '22297' }; }
      try { devices = await api.devices(); } catch { devices = []; }
      draw();
    };

    const rokuName = (id) => {
      const d = devices.find(x => x.id === id);
      return d ? d.name : '';
    };

    /** What this alarm plays, in as few words as fit. */
    const contentOf = (a) => {
      const uri = a.spotify_uri || '';
      if (!uri) return 'nothing set';
      const kind = (uri.split(':')[1] || 'item');
      return kind.charAt(0).toUpperCase() + kind.slice(1);
    };

    const draw = () => {
      clear(body);
      let list = data.alarms || [];
      if (ctx.settings.onlyEnabled) list = list.filter(a => a.enabled);

      if (!list.length) {
        body.append(el('div.empty-hint', {}, [
          el('div', { text: (data.alarms || []).length ? 'All alarms are off' : 'No alarms yet' }),
          el('button.btn.btn-small', {
            text: '+ New alarm',
            onclick: (e) => { e.stopPropagation(); newAlarm(); },
          }),
        ]));
        return;
      }

      const rows = el('div.alarm-list');
      for (const a of list) {
        const sw = el('input.switch-input', { type: 'checkbox' });
        sw.checked = !!a.enabled;
        sw.addEventListener('click', e => e.stopPropagation());   // don't open the editor
        sw.addEventListener('change', async () => {
          try {
            await api.updateAlarm(a.id, { enabled: sw.checked });
            a.enabled = sw.checked;
            draw();
          } catch (err) {
            sw.checked = !sw.checked;
            toast(err.message, true);
          }
        });

        const meta = [daySummary(a.days)];
        if (ctx.settings.showDetail !== false) {
          const where = rokuName(a.device_id);
          if (where) meta.push(where);
          if (a.volume != null) meta.push(`${a.volume}%`);
          meta.push(contentOf(a));
        }

        const when = ctx.settings.showNext !== false ? untilText(nextFire(a)) : '';

        const row = el(`div.alarm-row${a.enabled ? '' : '.off'}`, {}, [
          el('div.alarm-main', {}, [
            el('div.alarm-time', { text: a.at_time }),
            el('div.alarm-name', { text: a.name }),
            el('div.alarm-meta', { text: meta.join(' · ') }),
            a.last_result && a.last_result !== 'ran clean'
              ? el('div.alarm-warn', { text: a.last_result }) : null,
          ]),
          when ? el('div.alarm-next', { text: when }) : null,
          el('label.alarm-switch', {}, [sw, el('span.switch')]),
        ]);
        // The row opens the editor; the switch above stops its own events so a
        // toggle never becomes an accidental edit.
        row.addEventListener('click', () => {
          openAlarmEditor(a, data, load);
        });
        rows.append(row);
      }
      body.append(rows);

      body.append(el('div.alarm-actions', {}, [
        el('button.btn.btn-small', {
          text: '+ New', onclick: (e) => { e.stopPropagation(); newAlarm(); },
        }),
        el('button.btn.btn-small', {
          text: 'Test first', onclick: async (e) => {
            e.stopPropagation();
            const a = list[0];
            if (!a) return;
            const btn = e.currentTarget;
            btn.disabled = true; btn.textContent = 'Running…';
            try { showAlarmRun(a, await api.runAlarm(a.id)); }
            catch (err) { toast(err.message, true); }
            btn.disabled = false; btn.textContent = 'Test first';
          },
        }),
      ]));
    };

    const newAlarm = () => openAlarmEditor({
      name: 'Wake up', at_time: '07:00', days: [], enabled: true,
      wait_seconds: 13, volume: 40, app_id: data.spotify_app_id,
      spotify_uri: '', device_name: '', shuffle: false,
    }, data, load);

    // "in 7h" goes stale sitting on a wall; a minute is fine for that.
    tick = setInterval(() => { if (ctx.settings.showNext !== false) draw(); }, 60000);

    const off = bus.on('alarms_changed', load);
    load();
    return {
      refresh: load,
      destroy: () => { off(); clearInterval(tick); },
    };
  },
};
