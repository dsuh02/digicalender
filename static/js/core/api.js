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

  display: on => req('/api/display', { method: 'POST', body: JSON.stringify({ on }) }),

  // money
  finance: () => req('/api/finance'),
  netWorthSeries: (days = 180) => req(`/api/finance/networth?days=${days}`).then(r => r.series),
  insights: (months = 6) => req(`/api/finance/insights?months=${months}`),
  transactions: (limit = 25) => req(`/api/finance/transactions?limit=${limit}`).then(r => r.transactions),
  syncFinance: () => req('/api/finance/sync', { method: 'POST' }),
  startLink: () => req('/api/finance/link', { method: 'POST' }),
  pollLink: link_token => req('/api/finance/link/poll', { method: 'POST', body: JSON.stringify({ link_token }) }),
  deleteFinanceItem: id => req(`/api/finance/items/${id}`, { method: 'DELETE' }),
  projection: ({ contributions = false } = {}) =>
    req(`/api/finance/projection${contributions ? '?contributions=1' : ''}`),
  saveProjection: patch => req('/api/finance/projection', {
    method: 'POST', body: JSON.stringify(patch) }).then(r => r.config),
  saveKindColors: colors => req('/api/finance/kind-colors', {
    method: 'POST', body: JSON.stringify({ colors }) }).then(r => r.kind_colors),
  createFinanceAccount: d => req('/api/finance/accounts', { method: 'POST', body: JSON.stringify(d) }).then(r => r.account),
  updateFinanceAccount: (id, d) => req(`/api/finance/accounts/${id}`, { method: 'PATCH', body: JSON.stringify(d) }).then(r => r.account),
  deleteFinanceAccount: id => req(`/api/finance/accounts/${id}`, { method: 'DELETE' }),

  // mail
  mail: () => req('/api/mail'),
  createMailAccount: d => req('/api/mail', { method: 'POST', body: JSON.stringify(d) }).then(r => r.account),
  updateMailAccount: (id, d) => req(`/api/mail/accounts/${id}`, { method: 'PATCH', body: JSON.stringify(d) }).then(r => r.account),
  deleteMailAccount: id => req(`/api/mail/accounts/${id}`, { method: 'DELETE' }),
  checkMailAccount: id => req(`/api/mail/accounts/${id}/check`, { method: 'POST' }),
  mailMessages: (limit = 40, unread = false) =>
    req(`/api/mail/messages?limit=${limit}${unread ? '&unread=1' : ''}`).then(r => r.messages),

  // pipeline
  pipelineState: () => req('/api/pipeline'),
  runPipeline: reason => req('/api/pipeline/run', {
    method: 'POST', body: JSON.stringify({ reason }) }),

  // ai
  ai: () => req('/api/ai'),
  aiTest: () => req('/api/ai/test', { method: 'POST' }),
  aiAsk: (prompt, opts = {}) => req('/api/ai/ask', {
    method: 'POST', body: JSON.stringify({ prompt, ...opts }) }).then(r => r.text),

  // spotify + alarms
  spotify: () => req('/api/spotify'),
  spotifyAuthorize: () => req('/api/spotify/authorize', { method: 'POST' }).then(r => r.url),
  spotifyComplete: redirect => req('/api/spotify/complete', {
    method: 'POST', body: JSON.stringify({ redirect }) }),
  spotifyDisconnect: () => req('/api/spotify/disconnect', { method: 'POST' }),
  spotifyPlaylists: () => req('/api/spotify/playlists'),
  spotifySearch: q => req(`/api/spotify/search?q=${encodeURIComponent(q)}`).then(r => r.results),
  alarms: () => req('/api/alarms'),
  createAlarm: d => req('/api/alarms', { method: 'POST', body: JSON.stringify(d) }).then(r => r.alarm),
  updateAlarm: (id, d) => req(`/api/alarms/${id}`, { method: 'PATCH', body: JSON.stringify(d) }).then(r => r.alarm),
  deleteAlarm: id => req(`/api/alarms/${id}`, { method: 'DELETE' }),
  runAlarm: id => req(`/api/alarms/${id}/run`, { method: 'POST' }),

  // household members
  people: () => req('/api/people').then(r => r.people),
  createPerson: d => req('/api/people', { method: 'POST', body: JSON.stringify(d) }).then(r => r.person),
  updatePerson: (id, d) => req(`/api/people/${id}`, { method: 'PATCH', body: JSON.stringify(d) }).then(r => r.person),
  deletePerson: id => req(`/api/people/${id}`, { method: 'DELETE' }),
  orderPeople: ids => req('/api/people/order', { method: 'POST', body: JSON.stringify({ ids }) }),
  lanHosts: () => req('/api/people/lan').then(r => r.hosts),

  // gallery sets
  galleries: () => req('/api/galleries').then(r => r.galleries),
  createGallery: name => req('/api/galleries', { method: 'POST', body: JSON.stringify({ name }) }),
  updateGallery: (id, d) => req(`/api/galleries/${id}`, { method: 'PATCH', body: JSON.stringify(d) }).then(r => r.gallery),
  deleteGallery: id => req(`/api/galleries/${id}`, { method: 'DELETE' }),
  orderGalleries: ids => req('/api/galleries/order', { method: 'POST', body: JSON.stringify({ ids }) }),
  galleryImages: id => req(`/api/galleries/${id}/images`).then(r => r.images),
  orderGalleryImages: (id, ids) =>
    req(`/api/galleries/${id}/images/order`, { method: 'POST', body: JSON.stringify({ ids }) }),
  deleteGalleryImage: (gid, iid) => req(`/api/galleries/${gid}/images/${iid}`, { method: 'DELETE' }),
  imageUrl: iid => `/api/gimg/${iid}`,
  uploadGalleryImage: async (gid, file) => {
    const res = await fetch(`/api/galleries/${gid}/images`, {
      method: 'POST',
      headers: {
        'Content-Type': file.type || 'application/octet-stream',
        'X-Filename': encodeURIComponent(file.name || 'image.jpg'),
      },
      body: file,
    });
    const body = await res.json().catch(() => null);
    if (!res.ok) throw new Error((body && body.error) || `upload failed (${res.status})`);
    return body.image;
  },

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
                        'layout_changed', 'devices_changed', 'galleries_changed',
                        'people_changed', 'finance_changed']) {
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
