/* Student loans, from uploaded statements.
 *
 * The servicer left Plaid, so this data arrives one PDF a month rather than as
 * a live feed. That changes what the widget should show. A synced account gets
 * a current balance and little else worth saying; a statement archive knows the
 * exact composition of the debt — seventeen loans, four interest rates, and how
 * much of every payment reached principal instead of interest.
 *
 * That last figure is the one this leads with. A payment of $374 that moves the
 * balance by $93 is the single most useful thing these statements say, and it
 * is invisible on a balance line alone.
 */

import { api, bus } from '../core/api.js';
import { autoSize } from '../core/charts.js';
import { icon } from '../core/icons.js';
import { toast } from '../core/sheet.js';
import { clear, el } from '../core/util.js';
import { money } from './finance.js';

const REVEAL_MS = 25000;

const pct = (n) => `${(Number(n) || 0).toFixed(1)}%`;

/** A statement date as "Aug 2026" — the month is the unit these arrive in. */
function monthLabel(iso) {
  if (!iso) return '';
  const [y, m] = iso.split('-').map(Number);
  return new Date(y, (m || 1) - 1, 1)
    .toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
}

/**
 * Split one payment into the part that reduced the debt and the part that did
 * not. Returned as a fraction so the caller can draw it at any width.
 */
export function paymentSplit(st) {
  const toInterest = Number(st?.applied_interest || 0);
  const toPrincipal = Number(st?.applied_principal || 0);
  const total = toInterest + toPrincipal;
  return {
    toInterest, toPrincipal, total,
    principalShare: total > 0 ? toPrincipal / total : 0,
  };
}

/**
 * Group loans by the rate they carry.
 *
 * Rate is the axis that matters for paying them off, and seventeen loans across
 * three rates is three decisions, not seventeen. Sorted highest first, because
 * that is the order any extra payment should go in.
 */
export function byRate(loans) {
  const groups = new Map();
  for (const l of loans || []) {
    const key = l.rate == null ? 'n/a' : Number(l.rate).toFixed(3);
    const g = groups.get(key) || { rate: l.rate, balance: 0, count: 0, loans: [] };
    g.balance += Number(l.current_balance || 0);
    g.count += 1;
    g.loans.push(l);
    groups.set(key, g);
  }
  return [...groups.values()].sort((a, b) => (b.rate || 0) - (a.rate || 0));
}

/** Change between the two most recent statements, or null with fewer than two. */
export function trend(series) {
  if (!series || series.length < 2) return null;
  const a = series[series.length - 2];
  const b = series[series.length - 1];
  return {
    delta: Number(b.current_balance || 0) - Number(a.current_balance || 0),
    from: a.statement_date, to: b.statement_date,
  };
}

export const LoansWidget = {
  type: 'student_loans',
  name: 'Student loans',
  icon: 'wallet',
  category: 'Money',
  defaultSize: { w: 12, h: 12 },
  minSize: { w: 3, h: 3 },
  settings: [
    { key: 'view', label: 'Show', type: 'select', default: 'summary',
      options: [
        { value: 'summary', label: 'Balance and payment split' },
        { value: 'rates', label: 'Grouped by interest rate' },
        { value: 'loans', label: 'Every loan' },
      ] },
    { key: 'hideAmounts', label: 'Hide amounts until tapped', type: 'toggle', default: false },
  ],

  render(host, ctx) {
    const body = el('div.loans');
    host.append(body);

    let data = null;
    let error = '';
    let revealed = false;
    let revealTimer = null;
    let stopSize = null;

    const hidden = () => ctx.settings.hideAmounts === true && !revealed;
    const amount = (n, opts) => (hidden() ? '••••' : money(n, opts));

    const load = async () => {
      try {
        data = await api.loans();
        error = '';
      } catch (e) {
        error = e.message;
      }
      draw();
    };

    const draw = () => {
      if (stopSize) { stopSize(); stopSize = null; }
      clear(body);

      if (error) { body.append(el('div.empty-hint', { text: error })); return; }
      if (!data) return;
      if (!data.latest) {
        body.append(el('div.empty-hint', {
          text: 'No statements yet — add one in Settings → Money → Loan statements.',
        }));
        return;
      }

      const st = data.latest;
      const loans = data.loans || [];
      const holder = el('div.loans-holder');
      body.append(holder);

      // Everything below is drawn against the measured box, so the widget shows
      // fewer rows at a small size rather than clipping the ones it has.
      stopSize = autoSize(holder, (w, h, k) => {
        const view = ctx.settings.view || 'summary';
        const wrap = el('div.loans-body');

        wrap.append(el('div.loans-head', {}, [
          el('div.loans-total', { text: amount(st.current_balance) }),
          el('div.loans-sub', {
            text: `${loans.length} loans · ${monthLabel(st.statement_date)}`,
          }),
        ]));

        const t = trend(data.series);
        if (t && h > 90 * k) {
          const down = t.delta < 0;
          wrap.append(el(`div.loans-trend${down ? '.down' : '.up'}`, {
            text: `${down ? '▼' : '▲'} ${amount(Math.abs(t.delta), { cents: true })} `
                + `since ${monthLabel(t.from)}`,
          }));
        }

        if (view === 'summary') wrap.append(...summaryRows(st, h, k));
        else if (view === 'rates') wrap.append(rateList(loans, h, k));
        else wrap.append(loanList(loans, h, k));

        return wrap;
      });
    };

    /* The payment breakdown: one bar, two numbers. */
    const summaryRows = (st, h, k) => {
      const out = [];
      const s = paymentSplit(st);
      if (s.total > 0 && h > 130 * k) {
        const bar = el('div.loans-split', {}, [
          el('div.loans-split-principal', {
            style: `flex: ${Math.max(s.principalShare, 0.001)}`,
          }),
          el('div.loans-split-interest', {
            style: `flex: ${Math.max(1 - s.principalShare, 0.001)}`,
          }),
        ]);
        out.push(el('div.loans-section', {}, [
          el('div.loans-label', {
            text: `Last payment ${amount(s.total, { cents: true })}`,
          }),
          bar,
          el('div.loans-legend', {}, [
            el('span.loans-key.principal', {
              text: `${amount(s.toPrincipal, { cents: true })} principal`,
            }),
            el('span.loans-key.interest', {
              text: `${amount(s.toInterest, { cents: true })} interest`,
            }),
          ]),
        ]));
      }
      if (h > 200 * k) {
        out.push(el('div.loans-facts', {}, [
          fact('Principal', amount(st.unpaid_principal)),
          fact('Interest', amount(st.unpaid_interest)),
          fact('Next due', amount(st.current_due, { cents: true })),
          fact('Due date', (st.due_date || '').slice(5) || '—'),
        ]));
      }
      if (st.autopay && h > 240 * k) {
        out.push(el('div.loans-autopay', {
          text: `Auto pay ${amount(st.autopay_amount, { cents: true })}`
              + `${st.autopay_date ? ` on ${st.autopay_date.slice(5)}` : ''}`,
        }));
      }
      return out;
    };

    const fact = (label, value) => el('div.loans-fact', {}, [
      el('div.loans-fact-label', { text: label }),
      el('div.loans-fact-value', { text: value }),
    ]);

    const rateList = (loans, h, k) => {
      const groups = byRate(loans);
      const room = Math.max(1, Math.floor((h - 90 * k) / (34 * k)));
      const list = el('div.loans-list');
      const total = groups.reduce((n, g) => n + g.balance, 0) || 1;
      groups.slice(0, room).forEach((g) => {
        list.append(el('div.loans-row', {}, [
          el('div.loans-row-main', {}, [
            el('div.loans-row-name', {
              text: g.rate == null ? 'Rate unstated' : pct(g.rate),
            }),
            el('div.loans-row-note', {
              text: `${g.count} loan${g.count === 1 ? '' : 's'}`,
            }),
          ]),
          el('div.loans-bar', {}, [
            el('div.loans-bar-fill', { style: `width:${(g.balance / total) * 100}%` }),
          ]),
          el('div.loans-row-amount', { text: amount(g.balance) }),
        ]));
      });
      if (groups.length > room) {
        list.append(el('div.loans-more', { text: `+${groups.length - room} more` }));
      }
      return list;
    };

    const loanList = (loans, h, k) => {
      const room = Math.max(1, Math.floor((h - 90 * k) / (30 * k)));
      const list = el('div.loans-list');
      // Biggest first: a list of seventeen is only useful if the top of it is
      // the part that matters.
      const sorted = [...loans].sort(
        (a, b) => Number(b.current_balance || 0) - Number(a.current_balance || 0));
      sorted.slice(0, room).forEach((l) => {
        list.append(el('div.loans-row', {}, [
          el('div.loans-row-main', {}, [
            el('div.loans-row-name', { text: l.loan_ref }),
            el('div.loans-row-note', {
              text: `${l.program || ''}${l.rate == null ? '' : ` · ${pct(l.rate)}`}`,
            }),
          ]),
          el('div.loans-row-amount', { text: amount(l.current_balance) }),
        ]));
      });
      if (sorted.length > room) {
        list.append(el('div.loans-more', { text: `+${sorted.length - room} more` }));
      }
      return list;
    };

    host.addEventListener('click', () => {
      if (ctx.settings.hideAmounts !== true) return;
      revealed = !revealed;
      clearTimeout(revealTimer);
      if (revealed) {
        revealTimer = setTimeout(() => { revealed = false; draw(); }, REVEAL_MS);
      }
      draw();
    });

    load();
    const off = bus.on('finance_changed', load);
    return {
      refresh: load,
      destroy: () => {
        off();
        clearTimeout(revealTimer);
        if (stopSize) stopSize();
      },
    };
  },
};

/* ------------------------------------------------------------------ upload */

/**
 * The upload control for Settings → Money.
 *
 * Uploads are sequential, not parallel. Each import replaces its month and then
 * re-points the finance account at the newest statement held; running several
 * at once makes "newest" a race, and the account could end up showing whichever
 * PDF happened to finish last rather than the most recent month.
 */
export function statementUploader(onDone) {
  const input = el('input', {
    type: 'file', accept: 'application/pdf,.pdf', multiple: true,
    style: 'display:none',
  });
  const status = el('div.upload-status');
  const button = el('button.btn.btn-primary', {}, [icon('plus'), 'Add statements']);

  button.addEventListener('click', () => input.click());
  input.addEventListener('change', async () => {
    const files = [...input.files];
    input.value = '';                       // so the same file can be re-picked
    if (!files.length) return;

    button.disabled = true;
    const failures = [];
    let added = 0;
    let replaced = 0;
    for (let i = 0; i < files.length; i++) {
      status.textContent = `Reading ${files[i].name} (${i + 1} of ${files.length})…`;
      try {
        const r = await api.uploadStatement(files[i]);
        if (r.replaced) replaced++; else added++;
      } catch (e) {
        failures.push(`${files[i].name}: ${e.message}`);
      }
    }
    button.disabled = false;

    // Failures are listed in full rather than counted. A statement is rejected
    // because specific figures did not reconcile, and that message is the only
    // way to tell a wrong file from a parser that needs fixing.
    status.textContent = '';
    if (failures.length) {
      status.append(...failures.map(f => el('div.upload-error', { text: f })));
    }
    const ok = added + replaced;
    if (ok) {
      toast(`Imported ${ok} statement${ok === 1 ? '' : 's'}`
          + `${replaced ? ` (${replaced} replaced)` : ''}`);
      if (onDone) onDone();
    } else if (!failures.length) {
      toast('Nothing imported', true);
    }
  });

  return el('div.uploader', {}, [button, input, status]);
}
