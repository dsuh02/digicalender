/* Debt payoff: the chart, the headline, and the per-debt dates.
 *
 * The savings projection asks "how much at 70?". These ask "when am I free, and
 * what did it cost?" — and the difference shows up in what leads. There is no
 * target date to configure, because the date IS the answer.
 *
 * Three widgets so a whole page can be built out of them: the chart to see the
 * shape, the headline for the one number, and the list to inspect each debt.
 * They share an engine, so they cannot disagree with each other.
 *
 * Two states matter more here than anywhere in the savings chart, and both are
 * loud rather than silent:
 *
 *   BLOCKED — a debt with no rate or no payment is not projected at all. It is
 *   not defaulted to 0%, which would invent a payoff date years too early, and
 *   its balance is reported separately so the total is never quietly short.
 *
 *   UNDERWATER — a payment smaller than the monthly interest. The balance goes
 *   UP. Saying "42 years" there would be a lie of arithmetic.
 */

import { api, bus } from '../core/api.js';
import { observeSize, seriesColor } from '../core/charts.js';
import { icon } from '../core/icons.js';
import { close, openSheet, toast } from '../core/sheet.js';
import { compactMoney, timeChart } from '../core/timechart.js';
import { clear, el } from '../core/util.js';
import { money } from './finance.js';

const REVEAL_MS = 25000;
const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function monthLabel(ym) {
  if (!ym) return '';
  const [y, m] = ym.split('-');
  return `${MONTH_NAMES[Number(m) - 1] || ''} ${y}`;
}

/** "4 years 3 months" — the unit people actually think in for a payoff. */
export function durationLabel(months) {
  if (months === null || months === undefined) return 'never';
  if (months <= 0) return 'now';
  const y = Math.floor(months / 12);
  const m = months % 12;
  if (!y) return `${m} month${m === 1 ? '' : 's'}`;
  if (!m) return `${y} year${y === 1 ? '' : 's'}`;
  return `${y}y ${m}m`;
}

/**
 * Shared privacy toggle: these widgets are the most sensitive on the wall.
 *
 * `fallback` is the widget's DECLARED default, and has to be passed in: a
 * widget placed without ever opening its settings has `hideAmounts` undefined,
 * and `undefined !== false` reads as hidden — so a widget declaring
 * `default: false` came up masked anyway.
 */
function privacy(ctx, redraw, fallback = true) {
  let revealed = false;
  let timer = null;
  const wants = () => (ctx.settings.hideAmounts === undefined
    ? fallback : ctx.settings.hideAmounts !== false);
  const hidden = () => wants() && !revealed;
  return {
    hidden,
    cash: (v, opts) => (hidden() ? '••••' : money(v, opts)),
    toggle: () => {
      if (!wants() && !revealed) return;      // masking is off; nothing to reveal
      revealed = !revealed;
      clearTimeout(timer);
      if (revealed) timer = setTimeout(() => { revealed = false; redraw(); }, REVEAL_MS);
      redraw();
    },
    masking: wants,
    label: () => (revealed ? 'Hide' : 'Show'),
    stop: () => clearTimeout(timer),
  };
}

/** The "set this up" panel every widget falls back to when there is nothing yet. */
function nothingYet(text, onSetup) {
  return el('div.empty-hint', {}, [
    el('div', { text }),
    el('div.field-help', {
      text: 'Each debt needs an interest rate and a monthly payment before it can be projected.',
    }),
    el('button.btn.btn-small', {
      text: 'Set up debts…',
      onclick: (e) => { e.stopPropagation(); openDebtSetup(onSetup); },
    }),
  ]);
}

/* --------------------------------------------------------------- the chart */

export const DebtPayoffWidget = {
  type: 'debt_payoff', name: 'Debt payoff', icon: 'trend', category: 'Money',
  defaultSize: { w: 24, h: 16 }, minSize: { w: 9, h: 8 },
  settings: [
    { key: 'mode', label: 'Chart style', type: 'select', default: 'stacked',
      options: [
        { value: 'stacked', label: 'Stacked area (total debt)' },
        { value: 'lines', label: 'Independent lines (per debt)' },
      ] },
    { key: 'hideAmounts', label: 'Hide amounts until tapped', type: 'toggle', default: true },
  ],

  render(host, ctx) {
    const body = el('div.proj');
    host.append(body);

    let data = null;
    let chart = null;
    let stopSize = null;
    let mode = ctx.settings.mode === 'lines' ? 'lines' : 'stacked';

    const priv = privacy(ctx, () => refresh(), true);

    const load = async () => {
      try { data = await api.debt(); }
      catch { data = null; }
      draw();
    };

    const title = el('div.insight-label', { text: 'Debt remaining' });
    const value = el('div.insight-value');
    const sub = el('div.insight-sub');
    const legend = el('div.proj-legend');

    /** Header from a scrub index, or from the end of the series. */
    const readout = (idx) => {
      if (!data || !data.series.length) return;
      const last = data.months.length - 1;
      const atEnd = idx === null || idx === undefined;
      const i = atEnd ? 0 : idx;               // default view is TODAY, not zero
      const ym = data.months[i];

      let total = 0;
      for (const s of data.series) total += Number(s.values[i]) || 0;

      title.textContent = atEnd
        ? 'Debt remaining'
        : `${monthLabel(ym)} · ${durationLabel(i)} from now`;
      value.textContent = priv.cash(total);
      sub.textContent = atEnd
        ? (data.debt_free
            ? `Clear ${monthLabel(data.debt_free)} · ${durationLabel(data.payoff_month)}`
            : 'Never clears at these payments')
        : '';

      clear(legend);
      data.series.forEach((s, k) => {
        const v = Number(s.values[i]) || 0;
        legend.append(el('div.proj-legend-item', {}, [
          el('span.legend-dot', { style: { background: s.color || seriesColor(k) } }),
          el('span.proj-legend-k', { text: s.name }),
          el('span.proj-legend-v', {
            text: v <= 0 ? `clear ${monthLabel(s.payoff_month) || ''}` : priv.cash(v),
          }),
        ]));
      });
      // The end of a line is a fact worth keeping visible while scrubbing.
      void last;
    };

    const modeBtn = el('button.proj-btn', {
      type: 'button', title: 'Switch between total and per-debt',
      onclick: (e) => {
        e.stopPropagation();
        mode = mode === 'lines' ? 'stacked' : 'lines';
        modeBtn.textContent = mode === 'lines' ? 'Lines' : 'Stacked';
        if (chart) chart.setMode(mode);
      },
    });
    const resetBtn = el('button.proj-btn', {
      type: 'button', text: 'Reset',
      onclick: (e) => { e.stopPropagation(); if (chart) chart.reset(); },
    });
    const cfgBtn = el('button.proj-btn.proj-btn-icon', {
      type: 'button', 'aria-label': 'Debt plan', title: 'Rates, payments and strategy',
      onclick: (e) => { e.stopPropagation(); openDebtSetup(load); },
    }, [icon('sliders', 14)]);
    const eyeBtn = el('button.proj-btn', {
      type: 'button', text: 'Show',
      onclick: (e) => { e.stopPropagation(); priv.toggle(); },
    });

    const refresh = () => {
      eyeBtn.textContent = priv.label();
      readout(null);
    };

    const draw = () => {
      if (stopSize) { stopSize(); stopSize = null; }
      if (chart) { chart.destroy(); chart = null; }
      clear(body);

      if (!data || !data.series.length) {
        body.append(nothingYet(
          data && data.blocked && data.blocked.length
            ? 'No debt can be projected yet'
            : 'No debts to project',
          load));
        return;
      }

      data.series.forEach((s, k) => { s.color = s.color || seriesColor(k); });
      modeBtn.textContent = mode === 'lines' ? 'Lines' : 'Stacked';
      eyeBtn.textContent = priv.label();

      body.append(el('div.proj-head', {}, [
        el('div.proj-head-main', {}, [title, value, sub]),
        el('div.proj-actions', {}, [
          modeBtn, resetBtn, ctx.settings.hideAmounts === false ? null : eyeBtn, cfgBtn,
        ].filter(Boolean)),
      ]));

      const plot = el('div.chart-host.proj-plot');
      body.append(plot, legend);

      chart = timeChart({
        months: data.months,
        series: data.series.map(s => ({
          key: s.account_id, name: s.name, values: s.values, color: s.color,
        })),
        mode,
        format: v => (priv.hidden() ? '' : compactMoney(v)),
        onReadout: readout,
      });
      plot.append(chart.node);
      stopSize = observeSize(plot, (w, h, k) => chart.setSize(w, h, k));

      readout(null);
      body.append(el('div.proj-note', { text: planNote(data) }));
    };

    const off = bus.on('finance_changed', load);
    load();
    return {
      refresh: load,
      destroy: () => {
        off(); priv.stop();
        if (stopSize) stopSize();
        if (chart) chart.destroy();
      },
    };
  },
};

/** The caveat line. Always names what is NOT in the chart. */
function planNote(data) {
  const bits = [];
  bits.push(`${data.strategy === 'snowball' ? 'Snowball' : 'Avalanche'}`
          + `, paying ${money(data.monthly_total)}/mo`
          + `${data.extra ? ` (${money(data.extra)} extra)` : ''}.`);
  if (data.blocked && data.blocked.length) {
    bits.push(`${money(data.blocked_balance)} across ${data.blocked.length} `
            + `debt${data.blocked.length === 1 ? '' : 's'} is NOT included — `
            + `${data.blocked.map(b => b.name).join(', ')} still needs a rate or payment.`);
  }
  if (data.stalled && data.stalled.length) {
    bits.push('Some debts never clear at these payments.');
  }
  bits.push('Assumes the rate and payment stay put. Not a schedule.');
  return bits.join(' ');
}

/* ------------------------------------------------------------ the headline */

export const DebtFreeWidget = {
  type: 'debt_free', name: 'Debt-free date', icon: 'sparkles', category: 'Money',
  defaultSize: { w: 10, h: 8 }, minSize: { w: 3, h: 3 },
  settings: [
    { key: 'hideAmounts', label: 'Hide amounts until tapped', type: 'toggle', default: false },
  ],

  render(host, ctx) {
    const body = el('div.debtfree');
    host.append(body);
    let data = null;
    let stopSize = null;
    const priv = privacy(ctx, () => draw(), false);

    const load = async () => {
      try { data = await api.debt(); } catch { data = null; }
      draw();
    };

    const draw = () => {
      if (stopSize) { stopSize(); stopSize = null; }
      clear(body);
      if (!data) return;
      if (!data.series.length) { body.append(nothingYet('No debts to project', load)); return; }

      const holder = el('div.debtfree-holder');
      body.append(holder);
      stopSize = observeSize(holder, (w, h, k) => {
        clear(holder);
        const wrap = el('div.debtfree-body');
        const never = !data.debt_free;

        wrap.append(el('div.insight-label', { text: never ? 'At these payments' : 'Debt free' }));
        wrap.append(el(`div.debtfree-date${never ? '.never' : ''}`, {
          text: never ? 'Never clears' : monthLabel(data.debt_free),
        }));
        if (!never) {
          wrap.append(el('div.insight-sub', { text: durationLabel(data.payoff_month) }));
        }

        if (h > 130 * k) {
          wrap.append(el('div.debtfree-facts', {}, [
            el('div.debtfree-fact', {}, [
              el('div.loans-fact-label', { text: 'Owed now' }),
              el('div.loans-fact-value', { text: priv.cash(data.total_now) }),
            ]),
            el('div.debtfree-fact', {}, [
              el('div.loans-fact-label', { text: 'Interest to come' }),
              el('div.loans-fact-value.warn', { text: priv.cash(data.total_interest) }),
            ]),
          ]));
        }

        // What the extra actually buys is the whole reason to type one in.
        const eff = data.extra_effect || {};
        if (h > 190 * k && data.extra > 0 && eff.months_saved) {
          wrap.append(el('div.debtfree-saved', {
            text: `${money(data.extra)}/mo extra saves ${durationLabel(eff.months_saved)}`
                + ` and ${priv.cash(eff.interest_saved)} of interest`,
          }));
        } else if (h > 190 * k && !data.extra) {
          wrap.append(el('div.debtfree-saved.muted', {
            text: 'No extra payment set — tap to see what one would save.',
          }));
        }

        if (h > 230 * k && data.blocked && data.blocked.length) {
          wrap.append(el('div.debtfree-blocked', {
            text: `${priv.cash(data.blocked_balance)} not included`
                + ` (${data.blocked.map(b => b.name).join(', ')})`,
          }));
        }
        holder.append(wrap);
      });
    };

    // With masking off there is nothing to reveal, so a tap opens the plan —
    // which is what you actually want to change when looking at these.
    host.addEventListener('click', () => {
      if (priv.masking()) priv.toggle();
      else openDebtSetup(load);
    });

    const off = bus.on('finance_changed', load);
    load();
    return { refresh: load,
             destroy: () => { off(); priv.stop(); if (stopSize) stopSize(); } };
  },
};

/* ------------------------------------------------------------- the debts */

export const DebtListWidget = {
  type: 'debt_list', name: 'Debts', icon: 'list', category: 'Money',
  defaultSize: { w: 14, h: 12 }, minSize: { w: 4, h: 4 },
  settings: [
    { key: 'sort', label: 'Order', type: 'select', default: 'payoff',
      options: [
        { value: 'payoff', label: 'Payoff order' },
        { value: 'rate', label: 'Interest rate' },
        { value: 'balance', label: 'Balance' },
      ] },
    { key: 'hideAmounts', label: 'Hide amounts until tapped', type: 'toggle', default: false },
  ],

  render(host, ctx) {
    const body = el('div.loans');
    host.append(body);
    let data = null;
    let stopSize = null;
    const priv = privacy(ctx, () => draw(), false);

    const load = async () => {
      try { data = await api.debt(); } catch { data = null; }
      draw();
    };

    const ordered = () => {
      const rows = [...(data.series || [])];
      const by = ctx.settings.sort || 'payoff';
      if (by === 'rate') rows.sort((a, b) => (b.apr || 0) - (a.apr || 0));
      else if (by === 'balance') rows.sort((a, b) => b.start_balance - a.start_balance);
      return rows;                       // 'payoff' is the engine's own order
    };

    const draw = () => {
      if (stopSize) { stopSize(); stopSize = null; }
      clear(body);
      if (!data) return;
      if (!data.series.length && !(data.blocked || []).length) {
        body.append(el('div.empty-hint', { text: 'No debts' }));
        return;
      }

      const holder = el('div.loans-holder');
      body.append(holder);
      stopSize = observeSize(holder, (w, h, k) => {
        clear(holder);
        const list = el('div.loans-list.debt-list');
        const rows = ordered();
        const blocked = data.blocked || [];
        const room = Math.max(1, Math.floor(h / (42 * k)));
        const shown = [...rows, ...blocked].slice(0, room);

        shown.forEach((s) => {
          const isBlocked = !!s.missing;
          const when = isBlocked ? 'Needs ' + s.missing.join(' + ')
                     : s.stalled ? 'Never clears'
                     : monthLabel(s.payoff_month);
          list.append(el(`div.loans-row.debt-row${isBlocked || s.stalled ? '.debt-warn' : ''}`, {}, [
            el('div.loans-row-main', {}, [
              el('div.loans-row-name', { text: s.name }),
              el('div.loans-row-note', {
                text: [s.institution,
                       s.apr == null ? null : `${Number(s.apr).toFixed(2)}%`,
                       s.payment ? `${money(s.payment)}/mo` : null].filter(Boolean).join(' · '),
              }),
            ]),
            el('div.debt-when', {}, [
              el(`div.debt-when-date${isBlocked || s.stalled ? '.warn' : ''}`, { text: when }),
              el('div.loans-row-note', {
                text: isBlocked || s.stalled ? '' : durationLabel(s.payoff_index),
              }),
            ]),
            el('div.loans-row-amount', { text: priv.cash(s.start_balance ?? s.balance) }),
          ]));
        });

        if (shown.length < rows.length + blocked.length) {
          list.append(el('div.loans-more', {
            text: `+${rows.length + blocked.length - shown.length} more`,
          }));
        }
        holder.append(list);
      });
    };

    // With masking off there is nothing to reveal, so a tap opens the plan —
    // which is what you actually want to change when looking at these.
    host.addEventListener('click', () => {
      if (priv.masking()) priv.toggle();
      else openDebtSetup(load);
    });

    const off = bus.on('finance_changed', load);
    load();
    return { refresh: load,
             destroy: () => { off(); priv.stop(); if (stopSize) stopSize(); } };
  },
};

/* ----------------------------------------------------------- the setup sheet */

/**
 * Rates, payments, extra and strategy.
 *
 * Rate and payment are pre-filled from whatever the institution reported, but
 * both are editable, because the reported minimum is not what most people
 * actually pay — and on one real card here the reported minimum was $0.00,
 * which is a payment that never clears anything.
 */
export async function openDebtSetup(onSaved) {
  let data;
  try { data = await api.debt(); }
  catch (e) { toast(e.message, true); return; }

  const cfg = data.config;
  const body = el('div');
  const patch = { accounts: {} };

  const extra = el('input.input', {
    type: 'number', step: '25', value: cfg.extra || '', placeholder: '0',
  });
  const strategy = el('select.input');
  [['avalanche', 'Avalanche — highest rate first (cheapest)'],
   ['snowball', 'Snowball — smallest balance first (fastest first win)']]
    .forEach(([v, l]) => {
      const o = el('option', { value: v, text: l });
      if (cfg.strategy === v) o.selected = true;
      strategy.append(o);
    });

  body.append(
    el('h3.form-section', { text: 'Plan' }),
    el('label.field', {}, [
      el('span.field-label', { text: 'Extra per month' }),
      extra,
      el('span.field-help', {
        text: 'On top of every payment below. It goes to one debt at a time, and rolls onto the next as each clears.',
      }),
    ]),
    el('label.field', {}, [
      el('span.field-label', { text: 'Where the extra goes' }),
      strategy,
      el('span.field-help', {
        text: data.alternative && data.alternative.interest_delta
          ? `The other order costs ${money(Math.abs(data.alternative.interest_delta))} `
            + `${data.alternative.interest_delta > 0 ? 'more' : 'less'} in interest with these debts.`
          : 'With these debts both orders work out the same.',
      }),
    ]),
  );

  const all = [...data.series, ...(data.blocked || [])];
  if (!all.length) {
    body.append(el('p.sheet-note', { text: 'No debt accounts yet.' }));
  } else {
    body.append(el('h3.form-section', { text: 'Each debt' }));
  }

  for (const s of all) {
    const id = s.account_id;
    const bal = s.start_balance ?? s.balance;
    const apr = el('input.input', {
      type: 'number', step: '0.01', value: s.apr ?? '', placeholder: 'rate %',
    });
    const pay = el('input.input', {
      type: 'number', step: '10', value: s.payment ?? '', placeholder: 'per month',
    });
    apr.addEventListener('input', () => {
      patch.accounts[id] = { ...(patch.accounts[id] || {}),
                             apr: apr.value === '' ? null : Number(apr.value) };
    });
    pay.addEventListener('input', () => {
      patch.accounts[id] = { ...(patch.accounts[id] || {}),
                             payment: pay.value === '' ? null : Number(pay.value) };
    });

    // Say what is wrong with this row, in the row, rather than in a summary
    // somewhere else that does not say which debt it means.
    let help = `${s.institution || ''}`;
    if (s.missing) {
      help = `Not projected — needs ${s.missing.join(' and ')}. `
           + `${money(bal)} is missing from every total until it has one.`;
    } else if (s.underwater) {
      help = `⚠ ${money(s.payment)}/mo does not cover the interest on ${money(bal)} `
           + `at ${Number(s.apr).toFixed(2)}% — this balance GROWS.`;
    } else if (s.stalled) {
      help = '⚠ Never clears at this payment.';
    } else if (s.payoff_month) {
      help = `Clears ${monthLabel(s.payoff_month)} · ${money(s.interest)} interest from here.`;
    }

    body.append(el('div.proj-cfg', {}, [
      el('div.proj-cfg-head', {}, [
        el('span.proj-cfg-name', { text: s.name }),
        el('span.proj-cfg-bal', { text: money(bal) }),
      ]),
      el('div.proj-cfg-fields', {}, [
        el('label.field', {}, [el('span.field-label', { text: 'Rate %' }), apr]),
        el('label.field', {}, [el('span.field-label', { text: 'Per month' }), pay]),
      ]),
      el('div.field-help', { text: help }),
    ]));
  }

  openSheet({
    title: 'Debt plan',
    body,
    actions: [
      { label: 'Cancel', onClick: close },
      {
        label: 'Save', kind: 'primary', onClick: async () => {
          patch.extra = extra.value === '' ? 0 : Number(extra.value);
          patch.strategy = strategy.value;
          try {
            await api.saveDebtPlan(patch);
            close();
            if (onSaved) onSaved();
          } catch (e) { toast(e.message, true); }
        },
      },
    ],
  });
}
