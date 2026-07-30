/* Todo list and notifications. */

import { api, bus } from '../core/api.js';
import { toast } from '../core/sheet.js';
import { clear, el, fromApi, relativeTime } from '../core/util.js';

export const TodoWidget = {
  type: 'todo', name: 'To-do list', icon: 'check', category: 'Productivity',
  defaultSize: { w: 12, h: 12 }, minSize: { w: 6, h: 6 },
  settings: [
    { key: 'list', label: 'List name', type: 'text', default: 'Home',
      help: 'Separate widgets can show separate lists' },
    { key: 'hideDone', label: 'Hide completed', type: 'toggle', default: false },
    { key: 'showInput', label: 'Show quick-add box', type: 'toggle', default: true },
  ],
  render(host, ctx) {
    let todos = [];
    const listEl = el('div.todo-list');
    const input = el('input.input.todo-input', {
      type: 'text', maxlength: 300, placeholder: 'Add an item…',
    });

    const add = async () => {
      const title = input.value.trim();
      if (!title) return;
      input.value = '';
      try {
        await api.createTodo({ title, list_name: ctx.settings.list || 'Home' });
        load();
      } catch (e) { toast(e.message, true); }
    };
    input.addEventListener('keydown', e => { if (e.key === 'Enter') add(); });

    const inputRow = el('div.todo-add', {}, [
      input, el('button.btn.btn-small.btn-primary', { text: 'Add', onclick: add }),
    ]);
    host.append(listEl, inputRow);

    const load = async () => {
      try { todos = await api.todos(ctx.settings.list || 'Home'); }
      catch { todos = []; }
      draw();
    };

    const draw = () => {
      clear(listEl);
      inputRow.hidden = ctx.settings.showInput === false;
      let items = todos;
      if (ctx.settings.hideDone) items = items.filter(t => !t.done);
      if (!items.length) {
        listEl.append(el('p.empty-hint', { text: 'Nothing to do' }));
        return;
      }
      for (const t of items) {
        const box = el('button.todo-check' + (t.done ? '.on' : ''), {
          'aria-label': t.done ? 'Mark not done' : 'Mark done',
          onclick: async () => {
            t.done = !t.done;                 // optimistic: touch feels instant
            draw();
            try { await api.updateTodo(t.id, { done: t.done }); }
            catch (e) { t.done = !t.done; draw(); toast(e.message, true); }
          },
        }, [t.done ? '✓' : '']);

        listEl.append(el('div.todo-item' + (t.done ? '.done' : '') +
                         (t.priority >= 2 ? '.hot' : ''), {}, [
          box,
          el('div.todo-main', {}, [
            el('div.todo-title', { text: t.title }),
            t.due_utc ? el('div.todo-due', { text: relativeTime(fromApi(t.due_utc)) || 'due' }) : null,
          ]),
          el('button.todo-del', {
            'aria-label': 'Delete', text: '✕',
            onclick: async () => {
              try { await api.deleteTodo(t.id); load(); }
              catch (e) { toast(e.message, true); }
            },
          }),
        ]));
      }
    };

    const off = bus.on('todos_changed', load);
    load();
    return { refresh: load, destroy: off };
  },
};

export const NotificationsWidget = {
  type: 'notifications', name: 'Notifications', icon: 'bell', category: 'Productivity',
  defaultSize: { w: 12, h: 12 }, minSize: { w: 6, h: 5 },
  settings: [
    { key: 'max', label: 'Maximum shown', type: 'slider', min: 3, max: 40, default: 10 },
    { key: 'kinds', label: 'Show', type: 'select', default: 'all',
      options: [
        { value: 'all', label: 'Everything' },
        { value: 'reminder', label: 'Reminders only' },
        { value: 'alert', label: 'Warnings and errors only' },
      ] },
  ],
  render(host, ctx) {
    let items = [];
    const body = el('div.notif-list');
    const head = el('div.notif-head', {}, [
      el('span.notif-count'),
      el('button.btn.btn-small', {
        text: 'Clear', onclick: async () => {
          try { await api.clearNotifications(); load(); } catch (e) { toast(e.message, true); }
        },
      }),
    ]);
    host.append(head, body);

    const load = async () => {
      try { items = await api.notifications(60); } catch { items = []; }
      draw();
    };

    const draw = () => {
      clear(body);
      const filter = ctx.settings.kinds || 'all';
      let list = items;
      if (filter === 'reminder') list = list.filter(n => n.kind === 'reminder');
      else if (filter === 'alert') list = list.filter(n => n.kind === 'warn' || n.kind === 'error');
      list = list.slice(0, Number(ctx.settings.max || 10));

      head.querySelector('.notif-count').textContent =
        list.length ? `${list.length} recent` : '';
      if (!list.length) {
        body.append(el('p.empty-hint', { text: 'All clear' }));
        return;
      }
      for (const n of list) {
        body.append(el(`div.notif.k-${n.kind}`, {}, [
          el('div.notif-dot'),
          el('div.notif-main', {}, [
            el('div.notif-title', { text: n.title }),
            n.body ? el('div.notif-body', { text: n.body }) : null,
            el('div.notif-meta', { text: `${n.source || 'system'} · ${fmtAgo(n.created_at)}` }),
          ]),
          el('button.todo-del', {
            'aria-label': 'Dismiss', text: '✕',
            onclick: async () => {
              try { await api.dismissNotification(n.id); load(); }
              catch (e) { toast(e.message, true); }
            },
          }),
        ]));
      }
    };

    const offA = bus.on('notification', load);
    load();
    const timer = setInterval(load, 60000);
    return { refresh: load, destroy: () => { offA(); clearInterval(timer); } };
  },
};

function fmtAgo(iso) {
  const secs = (Date.now() - fromApi(iso).getTime()) / 1000;
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}
