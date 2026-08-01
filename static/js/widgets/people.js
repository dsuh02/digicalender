/* Household widgets: the greeting, and the profile switcher.
 *
 * Who the panel is "for" right now is a deliberate tap, never a guess. LAN
 * presence is real but noisy — phones sleep their radios, several people are
 * home at once, and a wall display silently reconfiguring itself around a
 * wrong guess is worse than one that waits to be told. So presence decorates
 * (a dot, a "welcome home"), and the switcher decides.
 */

import { api, bus } from '../core/api.js';
import { toast } from '../core/sheet.js';
import { clear, el } from '../core/util.js';

/** The active person is app-wide state — one screen, one current profile. */
export function activePersonId() {
  return window._activePerson?.() || '';
}

function partOfDay(d = new Date()) {
  const h = d.getHours();
  if (h < 5) return 'night';
  if (h < 12) return 'morning';
  if (h < 17) return 'afternoon';
  if (h < 22) return 'evening';
  return 'night';
}

const AUTO = {
  morning: 'Good morning', afternoon: 'Good afternoon',
  evening: 'Good evening', night: 'Good night',
};

export function initials(name) {
  return (name || '?').trim().split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase();
}

export const GreetingWidget = {
  type: 'greeting', name: 'Greeting', icon: 'home', category: 'Info',
  defaultSize: { w: 16, h: 5 }, minSize: { w: 6, h: 3 },
  settings: [
    { key: 'size', label: 'Text size', type: 'slider', min: 18, max: 72, default: 34 },
    { key: 'showWho', label: 'Show who’s home', type: 'toggle', default: true,
      help: 'Needs a device MAC on each person — Settings › People' },
    { key: 'align', label: 'Alignment', type: 'select', default: 'left',
      options: [{ value: 'left', label: 'Left' }, { value: 'center', label: 'Centre' }] },
  ],
  render(host, ctx) {
    const body = el('div.greeting');
    host.append(body);
    let people = [];

    const load = async () => {
      try { people = await api.people(); } catch { people = []; }
      draw();
    };

    const draw = () => {
      clear(body);
      const s = ctx.settings;
      body.style.textAlign = s.align || 'left';
      const active = people.find(p => p.id === activePersonId());
      const home = people.filter(p => p.home);

      // A person's own line wins; {name} lets them keep the name in it.
      const line = active && active.greeting
        ? active.greeting.replace(/\{name\}/gi, active.name)
        : `${AUTO[partOfDay()]}${active ? ', ' + active.name : ''}`;

      body.append(el('div.greeting-line', {
        text: line,
        style: { fontSize: (s.size || 34) + 'px',
                 color: active && active.color ? active.color : 'var(--text)' },
      }));

      if (s.showWho !== false) {
        const others = home.filter(p => p.id !== activePersonId());
        const note = !home.length
          ? (people.some(p => (p.macs || []).length) ? 'Nobody detected at home' : '')
          : others.length
            ? `Home: ${home.map(p => p.name).join(', ')}`
            : (active && active.home ? 'Welcome home' : `Home: ${home.map(p => p.name).join(', ')}`);
        if (note) body.append(el('div.greeting-sub', { text: note }));
      }
    };

    const offP = bus.on('people_changed', load);
    const offA = bus.on('active_person', draw);
    load();
    // Crossing noon shouldn't need a reload to stop saying "good morning".
    const timer = setInterval(draw, 60000);
    return { refresh: load, destroy: () => { offP(); offA(); clearInterval(timer); } };
  },
};

export const PeopleWidget = {
  type: 'people', name: 'Who’s using this', icon: 'home', category: 'Info',
  defaultSize: { w: 16, h: 7 }, minSize: { w: 5, h: 4 },
  settings: [
    { key: 'columns', label: 'Columns', type: 'slider', min: 1, max: 8, default: 4 },
    { key: 'showShared', label: 'Include a “Shared” tile', type: 'toggle', default: true,
      help: 'Switches back to only the household pages' },
  ],
  render(host, ctx) {
    const body = el('div.people-grid');
    host.append(body);
    let people = [];

    const load = async () => {
      try { people = await api.people(); } catch { people = []; }
      draw();
    };

    const draw = () => {
      clear(body);
      body.style.gridTemplateColumns = `repeat(${Number(ctx.settings.columns || 4)}, 1fr)`;
      if (!people.length) {
        body.append(el('p.empty-hint', { text: 'Add people in Settings › People' }));
        return;
      }
      const act = activePersonId();
      if (ctx.settings.showShared !== false) {
        body.append(el('button.person' + (act ? '' : '.on'), {
          onclick: () => window._setActivePerson?.(''),
        }, [
          el('span.person-face.person-shared', {}, ['⌂']),
          el('span.person-name', { text: 'Shared' }),
        ]));
      }
      people.forEach(p => {
        const tile = el('button.person' + (p.id === act ? '.on' : ''), {
          style: p.color ? { '--c': p.color } : {},
          onclick: () => window._setActivePerson?.(p.id === act ? '' : p.id),
        }, [
          el('span.person-face', { text: p.avatar || initials(p.name) }),
          el('span.person-name', { text: p.name }),
          p.home ? el('span.person-home', { title: 'Detected at home' }) : null,
        ]);
        body.append(tile);
      });
    };

    const offP = bus.on('people_changed', load);
    const offA = bus.on('active_person', draw);
    load();
    return { refresh: load, destroy: () => { offP(); offA(); } };
  },
};
