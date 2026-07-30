/* Glanceable widgets: clock, label, weather. */

import { api } from '../core/api.js';
import { icon } from '../core/icons.js';
import { clear, el } from '../core/util.js';

export const ClockWidget = {
  type: 'clock', name: 'Clock', icon: 'clock', category: 'Info',
  defaultSize: { w: 12, h: 6 }, minSize: { w: 5, h: 3 },
  settings: [
    { key: 'h12', label: '12-hour clock', type: 'toggle', default: true },
    { key: 'seconds', label: 'Show seconds', type: 'toggle', default: false },
    { key: 'showDate', label: 'Show date', type: 'toggle', default: true },
    { key: 'align', label: 'Alignment', type: 'select', default: 'left',
      options: [{ value: 'left', label: 'Left' }, { value: 'center', label: 'Centre' }] },
  ],
  render(host, ctx) {
    const time = el('div.clock-time');
    const date = el('div.clock-date');
    const wrap = el('div.clock', { style: { textAlign: ctx.settings.align || 'left' } }, [time, date]);
    host.append(wrap);

    const tick = () => {
      const now = new Date();
      const h12 = ctx.settings.h12 !== false;
      time.textContent = now.toLocaleTimeString([], {
        hour: 'numeric', minute: '2-digit',
        second: ctx.settings.seconds ? '2-digit' : undefined, hour12: h12,
      });
      date.hidden = ctx.settings.showDate === false;
      date.textContent = now.toLocaleDateString([], {
        weekday: 'long', month: 'long', day: 'numeric',
      });
    };
    tick();
    // A seconds display needs a 1s tick; otherwise 10s is plenty and cheaper.
    const timer = setInterval(tick, ctx.settings.seconds ? 1000 : 10000);
    return { refresh: tick, destroy: () => clearInterval(timer) };
  },
};

export const LabelWidget = {
  type: 'label', name: 'Text label', icon: 'text', category: 'Info',
  defaultSize: { w: 12, h: 3 }, minSize: { w: 3, h: 2 },
  settings: [
    { key: 'text', label: 'Text', type: 'text', default: 'Label' },
    { key: 'size', label: 'Size', type: 'slider', min: 14, max: 72, default: 28 },
    { key: 'align', label: 'Alignment', type: 'select', default: 'left',
      options: [{ value: 'left', label: 'Left' }, { value: 'center', label: 'Centre' },
                { value: 'right', label: 'Right' }] },
    { key: 'color', label: 'Colour', type: 'color', default: '#e8ebf2' },
    { key: 'divider', label: 'Show divider line', type: 'toggle', default: false },
  ],
  render(host, ctx) {
    const draw = () => {
      clear(host);
      const s = ctx.settings;
      host.append(el('div.label-widget' + (s.divider ? '.with-divider' : ''), {
        text: s.text || 'Label',
        style: {
          fontSize: (s.size || 28) + 'px',
          textAlign: s.align || 'left',
          color: s.color || 'var(--text)',
        },
      }));
    };
    draw();
    return { refresh: draw };
  },
};

export const WeatherWidget = {
  type: 'weather', name: 'Weather', icon: 'cloud-sun', category: 'Info',
  defaultSize: { w: 12, h: 12 }, minSize: { w: 6, h: 5 },
  settings: [
    { key: 'location', label: 'Location', type: 'latlon',
      help: 'Defaults to San Francisco until you set one' },
    { key: 'units', label: 'Units', type: 'select', default: 'imperial',
      options: [{ value: 'imperial', label: '°F, mph' }, { value: 'metric', label: '°C, km/h' }] },
    { key: 'days', label: 'Forecast days', type: 'slider', min: 0, max: 7, default: 4 },
    { key: 'showHourly', label: 'Show hourly strip', type: 'toggle', default: true },
  ],
  render(host, ctx) {
    const body = el('div.weather');
    host.append(body);

    const load = async () => {
      const s = ctx.settings;
      const lat = Number(s.lat ?? 37.7749);
      const lon = Number(s.lon ?? -122.4194);
      try {
        const { weather: w, notice } = await api.weather(
          lat, lon, s.units || 'imperial', Math.max(1, Number(s.days ?? 4)));
        draw(w, notice);
      } catch (e) {
        clear(body);
        body.append(el('p.empty-hint', { text: e.message }));
      }
    };

    const draw = (w, notice) => {
      clear(body);
      const u = '°' + w.temp_unit;
      const cur = w.current;
      body.append(el('div.wx-now', {}, [
        el('div.wx-icon', {}, [icon(cur.icon, 54)]),
        el('div.wx-main', {}, [
          el('div.wx-temp', { text: `${Math.round(cur.temp)}${u}` }),
          el('div.wx-label', { text: cur.label }),
          el('div.wx-sub', {
            text: `Feels ${Math.round(cur.feels_like)}${u} · ${cur.humidity}% · ${Math.round(cur.wind)} ${w.units === 'metric' ? 'km/h' : 'mph'}`,
          }),
        ]),
      ]));

      if (ctx.settings.showHourly !== false && w.hourly?.length) {
        const strip = el('div.wx-hourly');
        const now = new Date();
        w.hourly
          .filter(h => new Date(h.time) >= now)
          .slice(0, 8)
          .forEach(h => strip.append(el('div.wx-hour', {}, [
            el('div.wx-hour-t', { text: new Date(h.time).toLocaleTimeString([], { hour: 'numeric' }) }),
            icon(h.icon, 20),
            el('div.wx-hour-temp', { text: `${Math.round(h.temp)}°` }),
          ])));
        body.append(strip);
      }

      const nDays = Number(ctx.settings.days ?? 4);
      if (nDays > 0 && w.daily?.length) {
        const days = el('div.wx-days');
        w.daily.slice(0, nDays).forEach((d, i) => days.append(el('div.wx-day', {}, [
          el('div.wx-day-name', {
            text: i === 0 ? 'Today'
              : new Date(`${d.date}T00:00:00`).toLocaleDateString([], { weekday: 'short' }),
          }),
          icon(d.icon, 22),
          el('div.wx-day-temps', {}, [
            el('span.hi', { text: `${Math.round(d.high)}°` }),
            el('span.lo', { text: `${Math.round(d.low)}°` }),
          ]),
          d.precip > 15 ? el('div.wx-precip', { text: `${d.precip}%` }) : null,
        ])));
        body.append(days);
      }

      if (notice) body.append(el('div.wx-notice', { text: notice }));
    };

    load();
    const timer = setInterval(load, 10 * 60000);
    return { refresh: load, destroy: () => clearInterval(timer) };
  },
};
