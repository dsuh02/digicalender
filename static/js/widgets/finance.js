/* Money widgets: accounts, net worth, upcoming bills.
 *
 * This is a wall panel in a room other people walk through, so every widget
 * here can hide its numbers. Hidden is a real state, not a blur: the amount is
 * replaced with dots, and a tap reveals it for a while and then puts it back
 * on its own. Nobody has to remember to re-hide anything.
 */

import { api, bus } from '../core/api.js';
import { autoSize, sparkline } from '../core/charts.js';
import { icon } from '../core/icons.js';
import { close, openSheet, toast } from '../core/sheet.js';
import { eventPalette, getTheme } from '../core/theme.js';
import { clear, el, fromApi } from '../core/util.js';

const REVEAL_MS = 25000;
const DEBT_KINDS = new Set(['credit', 'loan']);
const KIND_LABEL = {
  checking: 'Checking', savings: 'Savings', credit: 'Credit cards',
  loan: 'Loans', investment: 'Investments', retirement: 'Retirement', other: 'Other',
};
const KIND_ORDER = ['checking', 'savings', 'investment', 'retirement', 'credit', 'loan', 'other'];

/* Colour is BY KIND, not per account. On a wall you want to read "the amber
 * rows are cards" at a glance; eight individually-coloured rows are eight
 * colours that mean nothing. Defaults are palette SLOTS, not hex, so they
 * re-theme with everything else — an override is only stored once you pick one.
 */
export function kindColor(kind, overrides = {}) {
  if (overrides && overrides[kind]) return overrides[kind];
  const i = KIND_ORDER.indexOf(kind);
  return `var(--c-${(i < 0 ? KIND_ORDER.length : i) % 6 + 1})`;
}

/** A per-account colour still wins if one was ever set. */
export function accountColor(a, overrides = {}) {
  return a.color || kindColor(a.kind, overrides);
}

export function money(n, { cents = false } = {}) {
  const v = Number(n || 0);
  return v.toLocaleString(undefined, {
    style: 'currency', currency: 'USD',
    minimumFractionDigits: cents ? 2 : 0, maximumFractionDigits: cents ? 2 : 0,
  });
}

/** Wires one widget's hide/reveal behaviour. */
function privacy(host, settings) {
  let revealed = false;
  let timer = null;
  const hidden = () => settings.hideAmounts !== false && !revealed;
  const bind = (redraw) => {
    host.addEventListener('click', () => {
      if (settings.hideAmounts === false) return;
      revealed = !revealed;
      clearTimeout(timer);
      if (revealed) timer = setTimeout(() => { revealed = false; redraw(); }, REVEAL_MS);
      redraw();
    });
  };
  return { hidden, bind, stop: () => clearTimeout(timer) };
}

const dots = (s = '••••••') => s;

/**
 * Show/hide accounts, and recolour a whole account type.
 *
 * Reachable in NORMAL mode from the layers button on any Accounts widget, for
 * the same reason the calendar's is: hiding a balance before people come over
 * must not require unlocking the layout.
 *
 * Grouped by kind because that is the unit colour works in, and because "hide
 * every loan" is the thing people actually want, not "hide account 4 of 9".
 */
export async function openAccountVisibility() {
  let data;
  try { data = await api.finance(); }
  catch (e) { toast(e.message, true); return; }

  const accounts = data.accounts || [];
  const overrides = { ...(data.kind_colors || {}) };
  const body = el('div');

  if (!accounts.length) {
    body.append(el('p.sheet-note', {
      text: 'No accounts yet. Link an institution under Settings › Money, or add one by hand.',
    }));
  }

  const palette = eventPalette(getTheme()).map(c => c.value);
  const kinds = KIND_ORDER.filter(k => accounts.some(a => a.kind === k));

  for (const k of kinds) {
    const group = accounts.filter(a => a.kind === k);

    // Tapping the group dot cycles the colour for the whole TYPE. Every row
    // below it, and every widget showing them, follows.
    const dot = el('button.src-dot.src-dot-btn', {
      type: 'button', 'aria-label': `Change colour of ${KIND_LABEL[k] || k}`,
      style: { backgroundColor: kindColor(k, overrides) },
      onclick: async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const at = palette.indexOf(overrides[k]);
        const next = palette[(at + 1) % palette.length];
        try {
          Object.assign(overrides, await api.saveKindColors({ ...overrides, [k]: next }));
          dot.style.backgroundColor = kindColor(k, overrides);
          rows.forEach(r => { r.dot.style.backgroundColor = kindColor(k, overrides); });
        } catch (err) { toast(err.message, true); }
      },
    });

    const shown = group.filter(a => !a.hidden).length;
    const head = el('div.src-group', {}, [
      dot,
      el('span.src-group-k', { text: KIND_LABEL[k] || k }),
      el('span.src-group-n', { text: `${shown}/${group.length} shown · tap the dot to recolour` }),
    ]);
    body.append(head);

    const rows = [];
    const list = el('div.src-list');
    for (const a of group) {
      const input = el('input.switch-input', { type: 'checkbox' });
      input.checked = !a.hidden;
      input.addEventListener('change', async () => {
        try {
          await api.updateFinanceAccount(a.id, { hidden: !input.checked });
          a.hidden = !input.checked;
          head.lastChild.textContent =
            `${group.filter(x => !x.hidden).length}/${group.length} shown · tap the dot to recolour`;
          // The server broadcasts finance_changed; every money widget redraws,
          // and net worth re-totals without the hidden accounts.
        } catch (e) {
          input.checked = !input.checked;
          toast(e.message, true);
        }
      });

      const rowDot = el('span.src-dot', { style: { backgroundColor: kindColor(k, overrides) } });
      rows.push({ dot: rowDot });
      list.append(el('label.src-row', {}, [
        rowDot,
        el('span.src-main', {}, [
          el('span.src-name', { text: a.name }),
          el('span.src-meta', {
            text: [a.institution, a.mask ? `••${a.mask}` : null,
                   money(Math.abs(a.balance))].filter(Boolean).join(' · '),
          }),
        ]),
        input, el('span.switch'),
      ]));
    }
    body.append(list);
  }

  openSheet({
    title: 'Accounts',
    body,
    actions: [
      { label: 'Manage…', onClick: () => { close(); window._openSettings?.('Money'); } },
      { label: 'Done', kind: 'primary', onClick: close },
    ],
  });
}

/* --------------------------------------------------------------- accounts */

export const AccountsWidget = {
  type: 'accounts', name: 'Accounts', icon: 'wallet', category: 'Money',
  defaultSize: { w: 16, h: 14 }, minSize: { w: 7, h: 5 },
  settings: [
    { key: 'kinds', label: 'Show', type: 'select', default: 'all',
      options: [
        { value: 'all', label: 'Everything' },
        { value: 'assets', label: 'Assets only' },
        { value: 'debts', label: 'Debts only' },
      ] },
    { key: 'group', label: 'Group by type', type: 'toggle', default: true },
    { key: 'hideAmounts', label: 'Hide amounts until tapped', type: 'toggle', default: true,
      help: 'Tap the widget to reveal for 25 seconds' },
  ],
  render(host, ctx) {
    const body = el('div.fin-list');
    host.append(el('button.fin-layers', {
      type: 'button', 'aria-label': 'Show or hide accounts', title: 'Show or hide accounts',
      onclick: (e) => { e.stopPropagation(); openAccountVisibility(); },
    }, [icon('layers', 15)]), body);
    let accounts = [];
    let colors = {};
    const priv = privacy(host, ctx.settings);

    const load = async () => {
      try {
        const f = await api.finance();
        accounts = f.accounts || [];
        colors = f.kind_colors || {};
      } catch { accounts = []; colors = {}; }
      draw();
    };

    const draw = () => {
      clear(body);
      let list = accounts.filter(a => !a.hidden);
      if (ctx.settings.kinds === 'assets') list = list.filter(a => !DEBT_KINDS.has(a.kind));
      if (ctx.settings.kinds === 'debts') list = list.filter(a => DEBT_KINDS.has(a.kind));
      if (!list.length) {
        body.append(el('p.empty-hint', { text: 'No accounts yet — Settings › Money' }));
        return;
      }
      const rows = (arr) => arr.forEach(a => {
        const debt = DEBT_KINDS.has(a.kind);
        body.append(el('div.fin-row', {}, [
          el('span.fin-dot', { style: { backgroundColor: accountColor(a, colors) } }),
          el('div.fin-main', {}, [
            el('div.fin-name', { text: a.name }),
            el('div.fin-meta', {
              text: [a.institution, a.mask ? `••${a.mask}` : null]
                .filter(Boolean).join(' · '),
            }),
          ]),
          el('div.fin-amt' + (debt ? '.debt' : ''), {
            text: priv.hidden() ? dots() : (debt ? '−' : '') + money(Math.abs(a.balance)),
          }),
        ]));
      });

      if (ctx.settings.group === false) {
        rows(list);
      } else {
        KIND_ORDER.filter(k => list.some(a => a.kind === k)).forEach(k => {
          const group = list.filter(a => a.kind === k);
          const total = group.reduce((s, a) => s + a.balance, 0);
          body.append(el('div.fin-group', {}, [
            el('span', { text: KIND_LABEL[k] || k }),
            el('span.fin-group-total', {
              text: priv.hidden() ? dots('•••') : money(Math.abs(total)),
            }),
          ]));
          rows(group);
        });
      }
    };

    priv.bind(draw);
    const off = bus.on('finance_changed', load);
    load();
    return { refresh: load, destroy: () => { off(); priv.stop(); } };
  },
};

/* -------------------------------------------------------------- net worth */

export const NetWorthWidget = {
  type: 'net_worth', name: 'Net worth', icon: 'trend', category: 'Money',
  defaultSize: { w: 14, h: 9 }, minSize: { w: 6, h: 5 },
  settings: [
    { key: 'days', label: 'Trend window (days)', type: 'slider', min: 30, max: 730, default: 180 },
    { key: 'showSplit', label: 'Show assets and debts', type: 'toggle', default: true },
    { key: 'hideAmounts', label: 'Hide amounts until tapped', type: 'toggle', default: true },
  ],
  render(host, ctx) {
    const body = el('div.networth');
    host.append(body);
    let summary = null;
    let series = [];
    let stopSize = null;
    const priv = privacy(host, ctx.settings);

    const load = async () => {
      try {
        const [f, s] = await Promise.all([
          api.finance(),
          api.netWorthSeries(Number(ctx.settings.days || 180)).catch(() => []),
        ]);
        summary = f.summary; series = s || [];
      } catch { summary = null; }
      draw();
    };


    const draw = () => {
      if (stopSize) { stopSize(); stopSize = null; }
      clear(body);
      if (!summary) {
        body.append(el('p.empty-hint', { text: 'No accounts yet — Settings › Money' }));
        return;
      }
      body.append(el('div.nw-label', { text: 'Net worth' }));
      body.append(el('div.nw-value', {
        text: priv.hidden() ? dots('••••••••') : money(summary.net),
      }));

      // A trend needs at least two days that differ; on day one there is
      // nothing honest to say, so say nothing rather than "+$0 over 1 days".
      const change = series.length >= 2 ? summary.net - Number(series[0].net) : null;
      if (change !== null && Math.round(change) !== 0 && !priv.hidden()) {
        const n = series.length;
        body.append(el('div.nw-change' + (change < 0 ? '.down' : ''), {
          text: `${change >= 0 ? '+' : '−'}${money(Math.abs(change))} over ${n} day${n === 1 ? '' : 's'}`,
        }));
      }
      // Drawn to measured pixels, not a fixed viewBox: this widget can be
      // stretched to any shape, and a stretched viewBox distorts the line and
      // the stroke with it.
      if (series.length >= 2) {
        const host = el('div.spark-host');
        body.append(host);
        stopSize = autoSize(host, (w, h) => sparkline(
          series.map(p => Number(p.net) || 0),
          { width: w, height: h, color: 'var(--primary)' },
        ));
      }
      if (ctx.settings.showSplit !== false) {
        body.append(el('div.nw-split', {}, [
          el('div', {}, [
            el('div.nw-split-k', { text: 'Assets' }),
            el('div.nw-split-v', { text: priv.hidden() ? dots('•••') : money(summary.assets) }),
          ]),
          el('div', {}, [
            el('div.nw-split-k', { text: 'Debts' }),
            el('div.nw-split-v.debt', { text: priv.hidden() ? dots('•••') : money(summary.debts) }),
          ]),
        ]));
      }
    };

    priv.bind(draw);
    const off = bus.on('finance_changed', load);
    load();
    return {
      refresh: load,
      destroy: () => { off(); priv.stop(); if (stopSize) stopSize(); },
    };
  },
};

/* ------------------------------------------------------------------ bills */

export const BillsWidget = {
  type: 'bills', name: 'Bills due', icon: 'wallet', category: 'Money',
  defaultSize: { w: 14, h: 10 }, minSize: { w: 6, h: 5 },
  settings: [
    { key: 'days', label: 'Days ahead', type: 'slider', min: 7, max: 90, default: 45 },
    { key: 'hideAmounts', label: 'Hide amounts until tapped', type: 'toggle', default: true },
  ],
  render(host, ctx) {
    const body = el('div.fin-list');
    host.append(el('button.fin-layers', {
      type: 'button', 'aria-label': 'Show or hide accounts', title: 'Show or hide accounts',
      onclick: (e) => { e.stopPropagation(); openAccountVisibility(); },
    }, [icon('layers', 15)]), body);
    let accounts = [];
    let colors = {};
    const priv = privacy(host, ctx.settings);

    const load = async () => {
      try {
        const f = await api.finance();
        accounts = f.accounts || [];
        colors = f.kind_colors || {};
      } catch { accounts = []; colors = {}; }
      draw();
    };

    /** Next occurrence of this account's due date. */
    const nextDue = (a) => {
      if (a.next_due) {
        const d = new Date(`${a.next_due}T00:00:00`);
        if (!isNaN(d)) return d;
      }
      if (!a.due_day) return null;
      const now = new Date();
      for (let m = 0; m < 3; m++) {
        const y = now.getFullYear(), mo = now.getMonth() + m;
        // Day 0 of the next month is the last day of this one — clamps a 31st
        // onto a 30-day month instead of rolling into the following one.
        const last = new Date(y, mo + 1, 0).getDate();
        const d = new Date(y, mo, Math.min(a.due_day, last));
        if (d >= new Date(now.getFullYear(), now.getMonth(), now.getDate())) return d;
      }
      return null;
    };

    const draw = () => {
      clear(body);
      const horizon = new Date();
      horizon.setDate(horizon.getDate() + Number(ctx.settings.days || 45));
      const due = accounts
        .filter(a => !a.hidden && DEBT_KINDS.has(a.kind))
        .map(a => ({ a, d: nextDue(a) }))
        .filter(x => x.d && x.d <= horizon)
        .sort((x, y) => x.d - y.d);

      if (!due.length) {
        body.append(el('p.empty-hint', { text: 'Nothing due — add a due day in Settings › Money' }));
        return;
      }
      const today = new Date(); today.setHours(0, 0, 0, 0);
      due.forEach(({ a, d }) => {
        const days = Math.round((d - today) / 86400000);
        const soon = days <= 3;
        body.append(el('div.fin-row', {}, [
          el('div.bill-when' + (soon ? '.soon' : ''), {
            text: days === 0 ? 'Today' : days === 1 ? 'Tomorrow' : `${days}d`,
          }),
          el('div.fin-main', {}, [
            el('div.fin-name', { text: a.name }),
            el('div.fin-meta', {
              text: d.toLocaleDateString([], { month: 'short', day: 'numeric' })
                + (a.apr ? ` · ${a.apr}% APR` : ''),
            }),
          ]),
          el('div.fin-amt', {
            text: priv.hidden() ? dots('••••')
              : a.min_payment ? money(a.min_payment, { cents: true }) : money(Math.abs(a.balance)),
          }),
        ]));
      });
    };

    priv.bind(draw);
    const off = bus.on('finance_changed', load);
    load();
    return { refresh: load, destroy: () => { off(); priv.stop(); } };
  },
};
