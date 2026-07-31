/* API client + the live event bus.
 *
 * Every widget reads through here rather than fetching directly, so shared
 * resources (devices, todos, events) are fetched once per tick instead of once
 * per widget — a dashboard with six calendar widgets should still make one
 * events request.
 */

import { toApi } from './util.js';

async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  let body = null;
  try { body = await res.json(); } catch { /* some endpoints return no body */ }
  if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
  return body;
}

export const api = {
  health: () => req('/api/health'),

  // calendar
  events: (start, end) =>
    req(`/api/events?start=${encodeURIComponent(toApi(start))}&end=${encodeURIComponent(toApi(end))}`)
      .then(r => r.events),
  createEvent: d => req('/api/events', { method: 'POST', body: JSON.stringify(d) }).then(r => r.event),
  updateEvent: (uid, d) => req(`/api/events/${uid}`, { method: 'PATCH', body: JSON.stringify(d) }).then(r => r.event),
  deleteEvent: uid => req(`/api/events/${uid}`, { method: 'DELETE' }),

  // dashboard
  dashboard: () => req('/api/dashboard'),
  createPage: d => req('/api/pages', { method: 'POST', body: JSON.stringify(d) }).then(r => r.page),
  updatePage: (id, d) => req(`/api/pages/${id}`, { method: 'PATCH', body: JSON.stringify(d) }).then(r => r.page),
  deletePage: id => req(`/api/pages/${id}`, { method: 'DELETE' }),

  createWidget: d => req('/api/widgets', { method: 'POST', body: JSON.stringify(d) }).then(r => r.widget),
  updateWidget: (id, d) => req(`/api/widgets/${id}`, { method: 'PATCH', body: JSON.stringify(d) }).then(r => r.widget),
  deleteWidget: id => req(`/api/widgets/${id}`, { method: 'DELETE' }),
  saveLayout: widgets => req('/api/widgets/layout', { method: 'POST', body: JSON.stringify({ widgets }) }),

  // todos
  todos: (list) => req('/api/todos' + (list ? `?list=${encodeURIComponent(list)}` : '')).then(r => r.todos),
  createTodo: d => req('/api/todos', { method: 'POST', body: JSON.stringify(d) }).then(r => r.todo),
  updateTodo: (id, d) => req(`/api/todos/${id}`, { method: 'PATCH', body: JSON.stringify(d) }).then(r => r.todo),
  deleteTodo: id => req(`/api/todos/${id}`, { method: 'DELETE' }),

  // notifications
  notifications: (limit = 30) => req(`/api/notifications?limit=${limit}`).then(r => r.notifications),
  dismissNotification: id => req(`/api/notifications/${id}`, { method: 'DELETE' }),
  clearNotifications: () => req('/api/notifications', { method: 'DELETE' }),

  // devices
  deviceKinds: () => req('/api/device-kinds').then(r => r.kinds),
  devices: () => req('/api/devices').then(r => r.devices),
  createDevice: d => req('/api/devices', { method: 'POST', body: JSON.stringify(d) }).then(r => r.device),
  updateDevice: (id, d) => req(`/api/devices/${id}`, { method: 'PATCH', body: JSON.stringify(d) }).then(r => r.device),
  deleteDevice: id => req(`/api/devices/${id}`, { method: 'DELETE' }),
  deviceState: id => req(`/api/devices/${id}/state`).then(r => r.state),
  deviceApps: id => req(`/api/devices/${id}/apps`),
  command: (id, command, params = {}) =>
    req(`/api/devices/${id}/command`, { method: 'POST', body: JSON.stringify({ command, params }) }),
  discover: (samsung = true) =>
    req('/api/discover', { method: 'POST', body: JSON.stringify({ samsung }) }).then(r => r.found),

  // scenes
  scenes: () => req('/api/scenes').then(r => r.scenes),
  createScene: d => req('/api/scenes', { method: 'POST', body: JSON.stringify(d) }).then(r => r.scene),
  updateScene: (id, d) => req(`/api/scenes/${id}`, { method: 'PATCH', body: JSON.stringify(d) }).then(r => r.scene),
  deleteScene: id => req(`/api/scenes/${id}`, { method: 'DELETE' }),
  runScene: id => req(`/api/scenes/${id}/run`, { method: 'POST' }),

  // calendar feed subscriptions
  feeds: () => req('/api/feeds').then(r => r.feeds),
  createFeed: d => req('/api/feeds', { method: 'POST', body: JSON.stringify(d) }),
  updateFeed: (id, d) => req(`/api/feeds/${id}`, { method: 'PATCH', body: JSON.stringify(d) }).then(r => r.feed),
  deleteFeed: id => req(`/api/feeds/${id}`, { method: 'DELETE' }),
  syncFeed: id => req(`/api/feeds/${id}/sync`, { method: 'POST' }),
  syncFeeds: () => req('/api/feeds/sync', { method: 'POST' }).then(r => r.results),

  weather: (lat, lon, units = 'imperial', days = 5) =>
    req(`/api/weather?lat=${lat}&lon=${lon}&units=${units}&days=${days}`),

  settings: () => req('/api/settings').then(r => r.settings),
  saveSettings: d => req('/api/settings', { method: 'PATCH', body: JSON.stringify(d) }).then(r => r.settings),
};

/* --------------------------------------------------------------- event bus */

class Bus extends EventTarget {
  emit(name, detail) { this.dispatchEvent(new CustomEvent(name, { detail })); }
  on(name, fn) {
    const h = e => fn(e.detail);
    this.addEventListener(name, h);
    return () => this.removeEventListener(name, h);
  }
}
export const bus = new Bus();

/* Live device state, kept current by SSE and readable synchronously by widgets. */
export const liveStates = new Map();

/**
 * Subscribe to the server stream. Reconnects with backoff — a wall panel runs
 * for months and the server will restart under it at some point.
 */
export function connectStream() {
  let retry = 1000;
  const open = () => {
    const es = new EventSource('/api/stream');

    es.addEventListener('hello', e => {
      retry = 1000;
      const { states } = JSON.parse(e.data);
      Object.entries(states || {}).forEach(([id, s]) => liveStates.set(id, s));
      bus.emit('devices_state', null);
      bus.emit('connected', true);
    });
    es.addEventListener('device_state', e => {
      const { id, state } = JSON.parse(e.data);
      liveStates.set(id, state);
      bus.emit('devices_state', id);
    });
    for (const name of ['events_changed', 'todos_changed', 'notification',
                        'layout_changed', 'devices_changed']) {
      es.addEventListener(name, e => bus.emit(name, JSON.parse(e.data || '{}')));
    }

    es.onerror = () => {
      es.close();
      bus.emit('connected', false);
      setTimeout(open, retry);
      retry = Math.min(retry * 2, 30000);
    };
  };
  open();
}
