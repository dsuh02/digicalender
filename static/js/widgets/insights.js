/* Money insight widgets: spending, cash flow, credit, net worth trend.
 *
 * The money widgets in finance.js answer "what do I have right now". These
 * answer "what has been happening" — the view every banking app leads with, and
 * the one a wall panel is actually good at, since you walk past it daily and a
 * trend is legible at a glance in a way a table of balances is not.
 *
 * **Every widget here is sized by measurement, not assumption.** A box on this
 * grid can be 240×200 or 1900×1080 at any aspect ratio, so the plot area is
 * measured with autoSize() and the chart is drawn to those exact pixels.
 *
 * Nothing is ever dropped for being small. An earlier version shed axes, then
 * labels, then the chart itself below fixed thresholds; that overrode the size
 * the user had chosen and made boxes look broken rather than dense. Content
 * shrinks instead, and core/scale.js gives the user the final say over how far.
 *
 * Same privacy contract as finance.js: every amount can be dotted out, a tap
 * reveals it, and it re-hides itself. Charts stay visible while hidden — the
 * SHAPE of your spending is not the secret, the amounts are — except where the
 * chart IS the number, like the credit gauge.
 */

import { api, bus } from '../core/api.js';
import { areaChart, arcGauge, autoSize, barChart, compactMoney, donut, fit, seriesColor }
  from '../core/charts.js';
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
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function monthLabel(ym, { long = false } = {}) {
  const [y, m] = String(ym || '').split('-');
  const name = MONTHS[Number(m) - 1] || ym;
  return long ? `${name} ${y}` : name;
}

/** Shared empty state — every widget here needs the same nudge. */
function needsData(what) {
  return el('div.empty-hint', {}, [
    el('div', { text: what }),
    el('div.field-help', { text: 'Link an account under Settings › Money, then Sync.' }),
  ]);
}

/**
 * Standard body: a header that always fits, and a plot that takes what is left.
 * Returns the plot host, which is what autoSize() should observe.
 */
function plotBody(body) {
  const plot = el('div.chart-host');
  body.append(plot);
  return plot;
}

/* --------------------------------------------------------------- spending */

export const SpendingWidget = {
  type: 'spending', name: 'Spending', icon: 'trend', category: 'Money',
  defaultSize: { w: 16, h: 14 }, minSize: { w: 6, h: 5 },
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
    let stopSize = null;
    const priv = privacy(host, ctx.settings);

    const load = async () => {
      try { data = await api.insights(Number(ctx.settings.months || 3)); }
      catch { data = null; }
      draw();
    };

    const draw = () => {
      if (stopSize) { stopSize(); stopSize = null; }
      clear(body);
      if (!data || !data.transaction_count) { body.append(needsData('No transactions yet')); return; }
      const cats = data.categories.slice(0, 6);
      if (!cats.length) { body.append(needsData('No spending in this window')); return; }

      const hide = priv.hidden();
      const view = ctx.settings.view || 'donut';
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

      /* --- list views: plain DOM, already fluid, just needs to scroll --- */
      if (view === 'merchants' || view === 'bars') {
        const rows = view === 'merchants'
          ? data.merchants.slice(0, 8).map(m => ({ k: m.name, v: m.total }))
          : cats.map(c => ({ k: c.label, v: c.total }));
        if (!rows.length) { body.append(needsData('Nothing in this window')); return; }
        const max = Math.max(...rows.map(r => r.v));
        const list = el('div.insight-list');
        rows.forEach((r, i) => {
          list.append(el('div.bar-row', {}, [
            el('div.bar-row-k', { text: r.k }),
            el('div.bar-row-track', {}, [
              el('div.bar-row-fill', {
                style: { width: `${Math.max(2, (r.v / max) * 100)}%`, background: seriesColor(i) },
              }),
            ]),
            el('div.bar-row-v', { text: cash(r.v, hide) }),
          ]));
        });
        body.append(list);
        return;
      }

      /* --- donut: ring + legend, laid out by whatever space exists --- */
      const plot = plotBody(body);
      const centre = el('div.donut-centre');
      const legend = el('div.legend');
      const setCentre = (i) => {
        clear(centre);
        const c = i === null ? null : cats[i];
        centre.append(
          el('div.donut-centre-k', { text: c ? c.label : 'Total' }),
          el('div.donut-centre-v', { text: cash(c ? c.total : data.total_spend, hide) }),
          el('div.donut-centre-s', {
            text: c ? `${c.pct}% · ${c.count} txns` : `${cats.length} categories`,
          }),
        );
      };

      stopSize = autoSize(plot, (w, h, k) => {
        const F = fit(w, h, k);
        // Legend beside the ring in a wide box, under it otherwise. It is
        // always shown — dropping it in a squarish box was the app overruling
        // the size the user picked.
        const side = w > h * 1.25;
        const stacked = !side;
        const wrap = el(`div.donut-layout${side ? '.side' : ''}${stacked ? '.stacked' : ''}`);

        const ringW = side ? Math.max(24, Math.min(h, w * 0.5)) : w;
        const ringH = stacked ? Math.max(24, h * 0.6) : h;
        const ring = donut({
          slices: cats.map((c, i) => ({ label: c.label, value: c.total, color: seriesColor(i) })),
          width: ringW, height: ringH,
          onHover: setCentre,
        });
        const ringBox = el('div.donut-wrap', {}, [ring, centre]);
        ringBox.style.width = `${ringW}px`;
        ringBox.style.height = `${ringH}px`;
        wrap.append(ringBox);

        {
          clear(legend);
          cats.forEach((c, i) => {
            legend.append(el('div.legend-item', {}, [
              el('span.legend-dot', { style: { background: seriesColor(i) } }),
              el('span.legend-k', { text: c.label }),
              el('span.legend-v', { text: hide ? dots('••') : `${c.pct}%` }),
            ]));
          });
          wrap.append(legend);
        }
        setCentre(null);
        return wrap;
      });
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

/* -------------------------------------------------------------- cash flow */

export const CashflowWidget = {
  type: 'cashflow', name: 'Cash flow', icon: 'trend', category: 'Money',
  defaultSize: { w: 18, h: 12 }, minSize: { w: 6, h: 5 },
  settings: [
    { key: 'months', label: 'Months', type: 'slider', min: 3, max: 12, default: 6 },
    { key: 'hideAmounts', label: 'Hide amounts until tapped', type: 'toggle', default: true },
  ],
  render(host, ctx) {
    const body = el('div.insight');
    host.append(body);
    let data = null;
    let picked = null;
    let stopSize = null;
    const priv = privacy(host, ctx.settings);

    const load = async () => {
      try { data = await api.insights(Number(ctx.settings.months || 6)); }
      catch { data = null; }
      draw();
    };

    const draw = () => {
      if (stopSize) { stopSize(); stopSize = null; }
      clear(body);
      if (!data || !data.cashflow.length) { body.append(needsData('No transactions yet')); return; }
      const hide = priv.hidden();
      const flow = data.cashflow;
      const sel = picked === null ? flow.length - 1 : Math.min(picked, flow.length - 1);
      const m = flow[sel];

      body.append(el('div.insight-head', {}, [
        el('div.insight-label', { text: monthLabel(m.month, { long: true }) }),
        el('div.insight-value' + (m.net < 0 ? '.debt' : ''), {
          text: hide ? dots('••••••') : `${m.net >= 0 ? '+' : '−'}${money(Math.abs(m.net))}`,
        }),
        el('div.insight-sub', { text: hide ? '' : `${money(m.in)} in · ${money(m.out)} out` }),
      ]));

      const plot = plotBody(body);
      stopSize = autoSize(plot, (w, h, k) => {
        // Narrow box: show only the most recent months rather than shaving the
        // bars down to threads nobody can compare.
        const perMonth = 26 * k;
        const room = Math.max(2, Math.floor(w / perMonth));
        const shown = flow.slice(Math.max(0, flow.length - room));
        const offset = flow.length - shown.length;
        return barChart({
          groups: shown.map(f => ({
            label: monthLabel(f.month),
            values: [f.in, f.out],
            colors: ['var(--good)', 'var(--c-4)'],
          })),
          width: w, height: h, scale: k,
          format: hide ? () => '' : compactMoney,
          onHover: (i) => { picked = offset + i; draw(); },
        });
      });

      // The legend is only worth its vertical space once the plot has some.
      if (host.clientHeight > 150) {
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

/* ----------------------------------------------------------- credit gauge */

export const CreditWidget = {
  type: 'credit', name: 'Credit usage', icon: 'wallet', category: 'Money',
  defaultSize: { w: 10, h: 10 }, minSize: { w: 4, h: 4 },
  settings: [
    { key: 'hideAmounts', label: 'Hide amounts until tapped', type: 'toggle', default: true },
  ],
  render(host, ctx) {
    const body = el('div.insight.insight-centre');
    host.append(body);
    let summary = null;
    let stopSize = null;
    const priv = privacy(host, ctx.settings);

    const load = async () => {
      try { summary = (await api.finance()).summary; }
      catch { summary = null; }
      draw();
    };

    const draw = () => {
      if (stopSize) { stopSize(); stopSize = null; }
      clear(body);
      const u = summary && summary.utilization;
      if (!u) { body.append(needsData('No credit cards with a known limit')); return; }
      const hide = priv.hidden();
      // 30% is the threshold every credit model leans on; 50% is trouble.
      const color = u.pct >= 50 ? 'var(--danger)'
                  : u.pct >= 30 ? 'var(--warn)'
                  : 'var(--good)';

      const plot = plotBody(body);
      stopSize = autoSize(plot, (w, h, k) => {
        // The arc is always drawn. It used to be dropped below 96px, which is
        // exactly the kind of decision that belongs to whoever sized the box.
        const gauge = arcGauge({ value: u.pct, max: 100, width: w, height: h, color });
        const centre = el('div.gauge-centre', {}, [
          el('div.gauge-v', { text: `${u.pct}%`, style: { color } }),
          el('div.gauge-k', { text: 'of limit used' }),
        ]);
        return el('div.gauge-wrap', {}, [gauge, centre]);
      });

      body.append(el('div.insight-sub', {
        text: hide ? `${u.cards} card${u.cards === 1 ? '' : 's'}`
                   : `${money(u.used)} of ${money(u.limit)} · ${u.cards} card${u.cards === 1 ? '' : 's'}`,
      }));
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

/* -------------------------------------------------------- net worth chart */

export const NetWorthChartWidget = {
  type: 'networth_chart', name: 'Net worth chart', icon: 'trend', category: 'Money',
  defaultSize: { w: 20, h: 13 }, minSize: { w: 6, h: 4 },
  settings: [
    { key: 'days', label: 'Window (days)', type: 'slider', min: 30, max: 730, default: 180 },
    { key: 'hideAmounts', label: 'Hide amounts until tapped', type: 'toggle', default: true },
  ],
  render(host, ctx) {
    const body = el('div.insight');
    host.append(body);
    let series = [];
    let summary = null;
    let stopSize = null;
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
      if (stopSize) { stopSize(); stopSize = null; }
      clear(body);
      if (!summary) { body.append(needsData('No accounts yet')); return; }
      const hide = priv.hidden();

      const label = el('div.insight-label', { text: 'Net worth' });
      const value = el('div.insight-value');
      const delta = el('div.insight-delta');
      body.append(el('div.insight-head', {}, [label, value, delta]));

      // One reading is a dot, not a trend. Say so rather than drawing a flat
      // line that implies nothing has moved.
      if (series.length < 2) {
        value.textContent = cash(summary.net, hide);
        body.append(el('p.field-help', {
          text: 'The chart appears once there are two days of readings — balances are recorded on every sync.',
        }));
        return;
      }

      const first = Number(series[0].net) || 0;
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

      const plot = plotBody(body);
      stopSize = autoSize(plot, (w, h, k) => areaChart({
        series: [{
          key: 'net', label: 'Net worth',
          values: series.map(p => Number(p.net) || 0),
          color: 'var(--primary)',
        }],
        labels: series.map(p => String(p.d).slice(5)),
        width: w, height: h, scale: k,
        baseline: 0,
        format: hide ? () => '' : compactMoney,
        onHover: readout,
      }));
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
