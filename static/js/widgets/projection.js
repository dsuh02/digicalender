/* Long-term savings projection.
 *
 * Retirement accounts compounded forward to a target age, drawn either as
 * independent lines (compare the accounts) or a stacked area (see the combined
 * total). Both modes share one chart instance and therefore the same gestures:
 * one finger scrubs and reads out every account at that month, two fingers pan
 * and pinch-zoom.
 *
 * The numbers are arithmetic on assumptions the user typed, not a forecast, and
 * the widget says so where it cannot be missed. A projection that looks
 * authoritative is worse than no projection — the rate of return is a guess and
 * every dollar downstream of it inherits that.
 */

import { api, bus } from '../core/api.js';
import { observeSize, seriesColor } from '../core/charts.js';
import { icon } from '../core/icons.js';
import { close, openSheet, toast } from '../core/sheet.js';
import { compactMoney, timeChart } from '../core/timechart.js';
import { clear, el } from '../core/util.js';
import { money } from './finance.js';

const REVEAL_MS = 25000;
const dots = (s = '••••') => s;

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function monthLabel(ym) {
  if (!ym) return '';
  const [y, m] = ym.split('-');
  return `${MONTH_NAMES[Number(m) - 1] || ''} ${y}`;
}

/** Age at a given month, when a birth year is known. */
function ageAt(ym, birthYear) {
  if (!birthYear || !ym) return null;
  return Number(ym.slice(0, 4)) - birthYear;
}

export const ProjectionWidget = {
  type: 'projection', name: 'Savings projection', icon: 'trend', category: 'Money',
  defaultSize: { w: 24, h: 16 }, minSize: { w: 9, h: 8 },
  settings: [
    { key: 'mode', label: 'Chart style', type: 'select', default: 'lines',
      options: [
        { value: 'lines', label: 'Independent lines' },
        { value: 'stacked', label: 'Stacked area' },
      ] },
    { key: 'hideAmounts', label: 'Hide amounts until tapped', type: 'toggle', default: true },
  ],

  render(host, ctx) {
    const body = el('div.proj');
    host.append(body);

    let data = null;
    let chart = null;
    let stopSize = null;
    let mode = ctx.settings.mode === 'stacked' ? 'stacked' : 'lines';

    // Privacy is local rather than shared with finance.js because this widget
    // must NOT toggle on every tap — a tap here is a scrub.
    let revealed = false;
    let revealTimer = null;
    const hidden = () => ctx.settings.hideAmounts !== false && !revealed;
    const cash = v => (hidden() ? dots() : money(v));

    const load = async () => {
      try { data = await api.projection(); }
      catch { data = null; }
      draw();
    };

    /* ------------------------------------------------------------ header */

    const title = el('div.insight-label', { text: 'Projected savings' });
    const value = el('div.insight-value');
    const sub = el('div.insight-sub');
    const legend = el('div.proj-legend');

    /** Fills the header from a scrub index, or the end of the series. */
    const readout = (idx) => {
      if (!data || !data.series.length) return;
      const last = data.months.length - 1;
      const i = idx === null || idx === undefined ? last : idx;
      const ym = data.months[i];
      const age = ageAt(ym, data.config.birth_year);
      let total = 0;
      for (const s of data.series) total += Number(s.values[i]) || 0;

      title.textContent = idx === null || idx === undefined
        ? (data.end_age ? `Projected at ${data.end_age}` : 'Projected total')
        : monthLabel(ym) + (age !== null ? ` · age ${age}` : '');
      value.textContent = cash(total);
      sub.textContent = i === 0 ? 'today' : (hidden() ? '' : `${data.series.length} accounts`);

      clear(legend);
      data.series.forEach((s, k) => {
        const v = Number(s.values[i]) || 0;
        legend.append(el('div.proj-legend-item', {}, [
          el('span.legend-dot', { style: { background: s.color || seriesColor(k) } }),
          el('span.proj-legend-k', { text: s.name }),
          el('span.proj-legend-v', { text: cash(v) }),
        ]));
      });
    };

    /* ------------------------------------------------------------- chrome */

    const modeBtn = el('button.proj-btn', {
      type: 'button', title: 'Switch between lines and stacked area',
      onclick: (e) => {
        e.stopPropagation();
        mode = mode === 'lines' ? 'stacked' : 'lines';
        modeBtn.textContent = mode === 'lines' ? 'Lines' : 'Stacked';
        if (chart) chart.setMode(mode);
      },
    });
    const resetBtn = el('button.proj-btn', {
      type: 'button', text: 'Reset', title: 'Show the whole horizon again',
      onclick: (e) => { e.stopPropagation(); if (chart) chart.reset(); },
    });
    const cfgBtn = el('button.proj-btn.proj-btn-icon', {
      type: 'button', 'aria-label': 'Contributions', title: 'Contributions and assumptions',
      onclick: (e) => { e.stopPropagation(); openProjectionSetup(load); },
    }, [icon('settings', 14)]);
    const eyeBtn = el('button.proj-btn', {
      type: 'button', text: 'Show', title: 'Reveal amounts',
      onclick: (e) => {
        e.stopPropagation();
        if (ctx.settings.hideAmounts === false) return;
        revealed = !revealed;
        clearTimeout(revealTimer);
        if (revealed) revealTimer = setTimeout(() => { revealed = false; refresh(); }, REVEAL_MS);
        refresh();
      },
    });

    const refresh = () => {
      eyeBtn.textContent = revealed ? 'Hide' : 'Show';
      readout(null);
    };

    /* --------------------------------------------------------------- draw */

    const draw = () => {
      if (stopSize) { stopSize(); stopSize = null; }
      if (chart) { chart.destroy(); chart = null; }
      clear(body);

      if (!data || !data.series.length) {
        body.append(el('div.empty-hint', {}, [
          el('div', { text: 'No retirement or investment accounts' }),
          el('div.field-help', {
            text: 'Link a 401k, IRA or brokerage under Settings › Money. Then set your monthly contributions here.',
          }),
          el('button.btn.btn-small', {
            text: 'Contributions…', onclick: (e) => { e.stopPropagation(); openProjectionSetup(load); },
          }),
        ]));
        return;
      }

      data.series.forEach((s, k) => { s.color = s.color || seriesColor(k); });

      modeBtn.textContent = mode === 'lines' ? 'Lines' : 'Stacked';
      eyeBtn.textContent = revealed ? 'Hide' : 'Show';

      body.append(
        el('div.proj-head', {}, [
          el('div.proj-head-main', {}, [title, value, sub]),
          el('div.proj-actions', {}, [
            modeBtn, resetBtn, ctx.settings.hideAmounts === false ? null : eyeBtn, cfgBtn,
          ].filter(Boolean)),
        ]),
      );

      const plot = el('div.chart-host.proj-plot');
      body.append(plot, legend);

      // Built once, then only resized/updated — the chart owns pointer capture
      // and must not be rebuilt underneath a live gesture.
      chart = timeChart({
        months: data.months,
        series: data.series.map(s => ({
          key: s.account_id, name: s.name, values: s.values, color: s.color,
        })),
        mode,
        // Axis labels disappear with the amounts; the SHAPE stays readable.
        format: v => (hidden() ? '' : compactMoney(v)),
        onReadout: readout,
      });
      plot.append(chart.node);
      stopSize = observeSize(plot, (w, h) => chart.setSize(w, h));

      readout(null);

      body.append(el('div.proj-note', {
        text: data.config.birth_year
          ? `Assumes ${data.config.default_growth}% average annual return to age ${data.config.end_age || data.config.target_age}. Not a forecast.`
          : `Assumes ${data.config.default_growth}% average annual return over ${data.horizon_years} years. Set your birth year to project to a specific age.`,
      }));
    };

    const off = bus.on('finance_changed', load);
    load();
    return {
      refresh: load,
      destroy: () => {
        off();
        clearTimeout(revealTimer);
        if (stopSize) stopSize();
        if (chart) chart.destroy();
      },
    };
  },
};

/* ------------------------------------------------------ contributions sheet */

/**
 * Per-account monthly contribution and growth rate, plus birth year.
 *
 * Reachable in normal mode from the widget: changing what you put in each month
 * is the whole point of the chart, not a layout concern.
 */
export async function openProjectionSetup(onSaved) {
  let data;
  try { data = await api.projection({ contributions: true }); }
  catch (e) { toast(e.message, true); return; }

  const cfg = data.config;
  const body = el('div');
  const patch = { accounts: {} };

  const birth = el('input.input', {
    type: 'number', inputmode: 'numeric', placeholder: 'e.g. 1998',
    value: cfg.birth_year || '',
  });
  const growth = el('input.input', {
    type: 'number', step: '0.1', value: cfg.default_growth,
  });
  const target = el('input.input', {
    type: 'number', inputmode: 'numeric', value: cfg.target_age,
  });

  body.append(
    el('h3.form-section', { text: 'Horizon' }),
    el('label.field', {}, [
      el('span.field-label', { text: 'Birth year' }),
      birth,
      el('span.field-help', { text: 'Used to project to a specific age. Optional.' }),
    ]),
    el('label.field', {}, [
      el('span.field-label', { text: 'Project until age' }),
      target,
    ]),
    el('label.field', {}, [
      el('span.field-label', { text: 'Average annual return (%)' }),
      growth,
      el('span.field-help', {
        text: 'A guess, and the number every projected dollar depends on. 7% is a common long-run nominal figure for a stock-heavy portfolio.',
      }),
    ]),
  );

  if (!data.series.length) {
    body.append(el('p.sheet-note', {
      text: 'No retirement or investment accounts are linked yet, so there is nothing to project.',
    }));
  } else {
    body.append(el('h3.form-section', { text: 'Monthly contributions' }));
  }

  for (const s of data.series) {
    const monthly = el('input.input', {
      type: 'number', step: '10', value: s.monthly || '', placeholder: '0',
    });
    const rate = el('input.input', {
      type: 'number', step: '0.1', placeholder: `${cfg.default_growth} (default)`,
      value: s.growth === cfg.default_growth ? '' : s.growth,
    });
    monthly.addEventListener('input', () => {
      patch.accounts[s.account_id] = {
        ...(patch.accounts[s.account_id] || {}),
        monthly: Number(monthly.value) || 0,
      };
    });
    rate.addEventListener('input', () => {
      patch.accounts[s.account_id] = {
        ...(patch.accounts[s.account_id] || {}),
        growth: rate.value === '' ? null : Number(rate.value),
      };
    });

    // What Plaid actually saw going in. Absent for most 401k providers, so it
    // is offered as a hint, never presented as the truth.
    const seen = s.contributed_to_date;
    const hint = seen && seen.monthly_avg
      ? `Plaid shows ${money(seen.total)} paid in across ${seen.count} transactions — about ${money(seen.monthly_avg)}/mo`
      : 'No contribution history available from this institution — enter what you put in each month.';

    const row = el('div.proj-cfg', {}, [
      el('div.proj-cfg-head', {}, [
        el('span.proj-cfg-name', { text: s.name }),
        el('span.proj-cfg-bal', { text: money(s.start_balance) }),
      ]),
      el('div.proj-cfg-fields', {}, [
        el('label.field', {}, [el('span.field-label', { text: 'Per month' }), monthly]),
        el('label.field', {}, [el('span.field-label', { text: 'Return %' }), rate]),
      ]),
      el('div.field-help', { text: `${s.institution || ''}${s.institution ? ' · ' : ''}${hint}` }),
    ]);
    if (seen && seen.monthly_avg) {
      row.append(el('button.btn.btn-small', {
        text: `Use ${money(seen.monthly_avg)}/mo`,
        onclick: (e) => {
          e.preventDefault();
          monthly.value = seen.monthly_avg;
          monthly.dispatchEvent(new Event('input'));
        },
      }));
    }
    body.append(row);
  }

  openSheet({
    title: 'Contributions',
    body,
    actions: [
      { label: 'Cancel', onClick: close },
      {
        label: 'Save', kind: 'primary', onClick: async () => {
          patch.birth_year = birth.value === '' ? null : Number(birth.value);
          patch.target_age = Number(target.value) || cfg.target_age;
          patch.default_growth = growth.value === '' ? cfg.default_growth : Number(growth.value);
          try {
            await api.saveProjection(patch);
            close();
            if (onSaved) onSaved();
          } catch (e) { toast(e.message, true); }
        },
      },
    ],
  });
}
