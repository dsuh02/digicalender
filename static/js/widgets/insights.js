/* Money insight widgets: spending, cash flow, credit, net worth trend.
 *
 * The money widgets in finance.js answer "what do I have right now". These
 * answer "what has been happening" — the view every banking app leads with, and
 * the one a wall panel is actually good at, since you walk past it daily and a
 * trend is legible at a glance in a way a table of balances is not.
 *
 * Same privacy contract as finance.js: every amount can be dotted out, a tap
 * reveals it, and it re-hides itself. Charts stay visible while hidden — the
 * SHAPE of your spending is not the secret, the amounts are — except where the
 * chart is the number, like the credit gauge.
 */

import { api, bus } from '../core/api.js';
import { areaChart, arcGauge, barChart, compactMoney, donut, seriesColor } from '../core/charts.js';
import { clear, el } from '../core/util.js';
import { money } from './finance.js';

const REVEAL_MS = 25000;

/** Local copy of finance.js's privacy helper — same contract, own timer. */
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

const dots = (s = '••••') => s;
const cash = (v, hide) => (hide ? dots() : money(v));

/** "2026-08" -> "Aug". Parsed as parts, never as a Date: new Date('2026-08')
 *  is UTC midnight, which in a negative offset renders as the month before. */
function monthLabel(ym, { long = false } = {}) {
  const NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const [y, m] = String(ym || '').split('-');
  const name = NAMES[Number(m) - 1] || ym;
  return long ? `${name} ${y}` : name;
}

function statRow(label, value, cls = '') {
  return el(`div.stat${cls}`, {}, [
    el('div.stat-k', { text: label }),
    el('div.stat-v', { text: value }),
  ]);
}

/** Shared empty state — every widget here needs the same nudge. */
function needsData(what) {
  return el('div.empty-hint', {}, [
    el('div', { text: what }),
    el('div.field-help', { text: 'Link an account under Settings › Money, then Sync.' }),
  ]);
}

/* --------------------------------------------------------------- spending */

export const SpendingWidget = {
  type: 'spending', name: 'Spending', icon: 'trend', category: 'Money',
  defaultSize: { w: 16, h: 14 }, minSize: { w: 8, h: 8 },
  settings: [
    { key: 'months', label: 'Window (months)', type: 'slider', min: 1, max: 12, default: 3 },
    { key: 'view', label: 'Show', type: 'select', default: 'donut',
      options: [
        { value: 'donut', label: 'Category donut' },
        { value: 'bars', label: 'Category bars' },
        { value: 'merchants', label: 'Top merchants' },
      ] },
    { key: 'hideAmounts', label: 'Hide amounts until tapped', type: 'toggle', default: true },
  ],
  render(host, ctx) {
    const body = el('div.insight');
    host.append(body);
    let data = null;
    const priv = privacy(host, ctx.settings);

    const load = async () => {
      try { data = await api.insights(Number(ctx.settings.months || 3)); }
      catch { data = null; }
      draw();
    };

    const draw = () => {
      clear(body);
      if (!data || !data.transaction_count) {
        body.append(needsData('No transactions yet'));
        return;
      }
      const cats = data.categories.slice(0, 6);
      if (!cats.length) {
        body.append(needsData('No spending in this window'));
        return;
      }
      const hide = priv.hidden();
      const view = ctx.settings.view || 'donut';

      // Header: this month vs last, the comparison people actually act on.
      const tm = data.this_month || {};
      const head = el('div.insight-head', {}, [
        el('div.insight-label', { text: `Spent · last ${data.months} mo` }),
        el('div.insight-value', { text: cash(data.total_spend, hide) }),
      ]);
      if (tm.delta_pct !== null && tm.delta_pct !== undefined) {
        const up = tm.delta_pct > 0;
        head.append(el(`div.insight-delta${up ? '.up' : '.down'}`, {
          // Spending UP is bad here, so the arrow is not colour-coded the way a
          // net-worth change is. Deliberate: green-up would read as good.
          text: `${up ? '▲' : '▼'} ${Math.abs(tm.delta_pct)}% vs last month`,
        }));
      }
      body.append(head);

      if (view === 'merchants') {
        const list = el('div.insight-list');
        const top = data.merchants.slice(0, 6);
        if (!top.length) { body.append(needsData('No merchants this month')); return; }
        const max = Math.max(...top.map(m => m.total));
        top.forEach((m, i) => {
          list.append(el('div.bar-row', {}, [
            el('div.bar-row-k', { text: m.name }),
            el('div.bar-row-track', {}, [
              el('div.bar-row-fill', {
                style: { width: `${Math.max(2, (m.total / max) * 100)}%`,
                         background: seriesColor(i) },
              }),
            ]),
            el('div.bar-row-v', { text: cash(m.total, hide) }),
          ]));
        });
        body.append(list);
        return;
      }

      if (view === 'bars') {
        const max = Math.max(...cats.map(c => c.total));
        const list = el('div.insight-list');
        cats.forEach((c, i) => {
          list.append(el('div.bar-row', {}, [
            el('div.bar-row-k', { text: c.label }),
            el('div.bar-row-track', {}, [
              el('div.bar-row-fill', {
                style: { width: `${Math.max(2, (c.total / max) * 100)}%`,
                         background: seriesColor(i) },
              }),
            ]),
            el('div.bar-row-v', { text: cash(c.total, hide) }),
          ]));
        });
        body.append(list);
        return;
      }

      // Donut + legend. Tapping a slice swaps the centre readout to it.
      const centre = el('div.donut-centre');
      const setCentre = (i) => {
        clear(centre);
        const c = i === null ? null : cats[i];
        centre.append(
          el('div.donut-centre-k', { text: c ? c.label : 'Total' }),
          el('div.donut-centre-v', { text: cash(c ? c.total : data.total_spend, hide) }),
          el('div.donut-centre-s', { text: c ? `${c.pct}% · ${c.count} txns` : `${cats.length} categories` }),
        );
      };
      const ring = donut({
        slices: cats.map((c, i) => ({ label: c.label, value: c.total, color: seriesColor(i) })),
        size: 130, thickness: 17,
        onHover: (i) => setCentre(i),
      });
      setCentre(null);
      body.append(el('div.donut-wrap', {}, [ring, centre]));

      const legend = el('div.legend');
      cats.forEach((c, i) => {
        legend.append(el('div.legend-item', {}, [
          el('span.legend-dot', { style: { background: seriesColor(i) } }),
          el('span.legend-k', { text: c.label }),
          el('span.legend-v', { text: hide ? dots('••') : `${c.pct}%` }),
        ]));
      });
      body.append(legend);
    };

    priv.bind(draw);
    const off = bus.on('finance_changed', load);
    load();
    return { refresh: load, destroy: () => { off(); priv.stop(); } };
  },
};

/* -------------------------------------------------------------- cash flow */

export const CashflowWidget = {
  type: 'cashflow', name: 'Cash flow', icon: 'trend', category: 'Money',
  defaultSize: { w: 18, h: 12 }, minSize: { w: 9, h: 7 },
  settings: [
    { key: 'months', label: 'Months', type: 'slider', min: 3, max: 12, default: 6 },
    { key: 'hideAmounts', label: 'Hide amounts until tapped', type: 'toggle', default: true },
  ],
  render(host, ctx) {
    const body = el('div.insight');
    host.append(body);
    let data = null;
    let picked = null;
    const priv = privacy(host, ctx.settings);

    const load = async () => {
      try { data = await api.insights(Number(ctx.settings.months || 6)); }
      catch { data = null; }
      draw();
    };

    const draw = () => {
      clear(body);
      if (!data || !data.cashflow.length) { body.append(needsData('No transactions yet')); return; }
      const hide = priv.hidden();
      const flow = data.cashflow;
      const sel = picked === null ? flow.length - 1 : picked;
      const m = flow[sel];

      body.append(el('div.insight-head', {}, [
        el('div.insight-label', { text: monthLabel(m.month, { long: true }) }),
        el('div.insight-value' + (m.net < 0 ? '.debt' : ''), {
          text: hide ? dots('••••••') : `${m.net >= 0 ? '+' : '−'}${money(Math.abs(m.net))}`,
        }),
        el('div.insight-sub', {
          text: hide ? '' : `${money(m.in)} in · ${money(m.out)} out`,
        }),
      ]));

      body.append(barChart({
        groups: flow.map(f => ({
          label: monthLabel(f.month),
          values: [f.in, f.out],
          colors: ['var(--good)', 'var(--c-4)'],
        })),
        width: 320, height: 128,
        format: hide ? () => '' : compactMoney,
        onHover: (i) => { picked = i; draw(); },
      }));

      body.append(el('div.legend', {}, [
        el('div.legend-item', {}, [
          el('span.legend-dot', { style: { background: 'var(--good)' } }),
          el('span.legend-k', { text: 'In' }),
        ]),
        el('div.legend-item', {}, [
          el('span.legend-dot', { style: { background: 'var(--c-4)' } }),
          el('span.legend-k', { text: 'Out' }),
        ]),
      ]));
    };

    priv.bind(draw);
    const off = bus.on('finance_changed', load);
    load();
    return { refresh: load, destroy: () => { off(); priv.stop(); } };
  },
};

/* ----------------------------------------------------------- credit gauge */

export const CreditWidget = {
  type: 'credit', name: 'Credit usage', icon: 'wallet', category: 'Money',
  defaultSize: { w: 10, h: 10 }, minSize: { w: 6, h: 6 },
  settings: [
    { key: 'hideAmounts', label: 'Hide amounts until tapped', type: 'toggle', default: true },
  ],
  render(host, ctx) {
    const body = el('div.insight.insight-centre');
    host.append(body);
    let summary = null;
    const priv = privacy(host, ctx.settings);

    const load = async () => {
      try { summary = (await api.finance()).summary; }
      catch { summary = null; }
      draw();
    };

    const draw = () => {
      clear(body);
      const u = summary && summary.utilization;
      if (!u) {
        body.append(needsData('No credit cards with a known limit'));
        return;
      }
      const hide = priv.hidden();
      // 30% is the threshold every credit model leans on; 10% is comfortable.
      const color = u.pct >= 50 ? 'var(--danger)'
                  : u.pct >= 30 ? 'var(--warn)'
                  : 'var(--good)';
      const gauge = arcGauge({ value: u.pct, max: 100, size: 132, thickness: 13, color });
      const centre = el('div.gauge-centre', {}, [
        el('div.gauge-v', { text: `${u.pct}%`, style: { color } }),
        el('div.gauge-k', { text: 'of limit used' }),
      ]);
      body.append(el('div.gauge-wrap', {}, [gauge, centre]));
      body.append(el('div.insight-sub', {
        text: hide ? `${u.cards} card${u.cards === 1 ? '' : 's'}`
                   : `${money(u.used)} of ${money(u.limit)} · ${u.cards} card${u.cards === 1 ? '' : 's'}`,
      }));
    };

    priv.bind(draw);
    const off = bus.on('finance_changed', load);
    load();
    return { refresh: load, destroy: () => { off(); priv.stop(); } };
  },
};

/* -------------------------------------------------------- net worth chart */

export const NetWorthChartWidget = {
  type: 'networth_chart', name: 'Net worth chart', icon: 'trend', category: 'Money',
  defaultSize: { w: 20, h: 13 }, minSize: { w: 10, h: 8 },
  settings: [
    { key: 'days', label: 'Window (days)', type: 'slider', min: 30, max: 730, default: 180 },
    { key: 'split', label: 'Assets and debts as separate lines', type: 'toggle', default: false },
    { key: 'hideAmounts', label: 'Hide amounts until tapped', type: 'toggle', default: true },
  ],
  render(host, ctx) {
    const body = el('div.insight');
    host.append(body);
    let series = [];
    let summary = null;
    let cursor = null;
    const priv = privacy(host, ctx.settings);

    const load = async () => {
      try {
        const [f, s] = await Promise.all([
          api.finance(),
          api.netWorthSeries(Number(ctx.settings.days || 180)).catch(() => []),
        ]);
        summary = f.summary; series = s || [];
      } catch { summary = null; series = []; }
      draw();
    };

    const draw = () => {
      clear(body);
      if (!summary) { body.append(needsData('No accounts yet')); return; }
      const hide = priv.hidden();

      // One reading is a dot, not a trend. Say so rather than drawing a flat
      // line that implies nothing has moved.
      if (series.length < 2) {
        body.append(el('div.insight-head', {}, [
          el('div.insight-label', { text: 'Net worth' }),
          el('div.insight-value', { text: cash(summary.net, hide) }),
        ]));
        body.append(el('p.field-help', {
          text: 'The chart appears once there are two days of readings — balances are recorded on every sync.',
        }));
        return;
      }

      const first = Number(series[0].net) || 0;
      const label = el('div.insight-label', { text: 'Net worth' });
      const value = el('div.insight-value');
      const delta = el('div.insight-delta');
      body.append(el('div.insight-head', {}, [label, value, delta]));

      // Scrubbing updates these three nodes in place. It must NOT call draw():
      // redrawing replaces the <svg> the pointer capture is attached to, and the
      // gesture dies on the first move — the same class of bug as the pull-down
      // that stopped halfway.
      const readout = (i) => {
        const at = i === null ? series.length - 1 : i;
        const shown = Number(series[at].net) || 0;
        const change = shown - first;
        label.textContent = i === null ? 'Net worth' : String(series[at].d);
        value.textContent = cash(shown, hide);
        delta.className = 'insight-delta ' + (change < 0 ? 'down' : 'up');
        delta.textContent = hide ? ''
          : `${change >= 0 ? '+' : '−'}${money(Math.abs(change))} over ${series.length} days`;
      };
      readout(null);

      body.append(areaChart({
        series: [{
          key: 'net', label: 'Net worth',
          values: series.map(p => Number(p.net) || 0),
          color: 'var(--primary)',
        }],
        labels: series.map(p => String(p.d).slice(5)),
        width: 340, height: 150,
        baseline: 0,
        format: hide ? () => '' : compactMoney,
        onHover: readout,
      }));
    };

    priv.bind(draw);
    const off = bus.on('finance_changed', load);
    load();
    return { refresh: load, destroy: () => { off(); priv.stop(); } };
  },
};
