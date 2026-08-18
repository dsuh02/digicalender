"""
Debt payoff projection.

The savings projection in `projection.py` answers "how much will I have at 70?".
This answers the opposite question, and the difference is not just a sign flip:

**The horizon is the ANSWER, not an input.** Savings runs to a date you choose.
Debt runs until the balance hits zero, and *when that happens* is the number you
came for. Nothing here takes a target date.

**A payment can fail to pay anything off.** If the monthly payment is less than
the monthly interest, the balance grows forever — the projection does not
converge, and saying "42 years" would be a lie of arithmetic. Debts in that
state are reported as such, by name, instead of being run to the iteration cap
and quietly rendered as a very long line.

**Paying a debt off makes every other debt faster.** When one clears, its
payment is freed and rolls into the next — the snowball effect. That rollover
is modelled, because without it the strategy choice below makes almost no
difference and the whole page would be decorative.

**Order matters, and the user picks it.** Extra money goes to the highest rate
(avalanche, cheapest) or the smallest balance (snowball, fastest first win).
Both are simulated so the cost of choosing the motivating one over the cheap one
is visible rather than argued about.

WHAT IT DOES NOT MODEL
----------------------
* **Credit-card minimums are treated as a FIXED monthly payment**, not the real
  `max(floor, % of balance)` that shrinks as you pay down. Fixed is what a
  person actually does when they decide to pay $200 a month, and a declining
  minimum would quietly stretch the payoff by years without the user asking for
  it. Say what you will pay; that is what is projected.
* **Cards are modelled with the same daily simple interest as the loans.** A
  card really compounds daily on an average daily balance, so this understates a
  revolving balance slightly. It does not understate the loans, which is where
  almost all of the money is.
* **One payment cadence for every debt.** Real accounts fall due on different
  days; the model pays them all on the same day each month. Over a payoff
  horizon that is worth days of interest, not months of duration.

Every one of those makes this arithmetic on stated assumptions, not a schedule
anyone is entitled to hold a servicer to.
"""

from __future__ import annotations

import json
from datetime import date

import loanmodel
import store

CONFIG_KEY = "finance_debt_plan"

DEBT_KINDS = ("credit", "loan")
MAX_MONTHS = 12 * 60            # 60 years: an iteration cap, not a real horizon
AVALANCHE, SNOWBALL = "avalanche", "snowball"
STRATEGIES = (AVALANCHE, SNOWBALL)

# The Auto Pay reduction is temporarily 1.00 point rather than the ordinary
# 0.25, through 30 June 2028 — so rates displayed today step UP 0.75 the day
# after. Configurable because it is a policy that can change, and because it
# applies to federal loans on Auto Pay, not to a credit card.
AUTO_PAY_STEP = date(2028, 7, 1)
AUTO_PAY_STEP_UP = 0.75
FEDERAL_SERVICERS = ("aidvantage",)

# Below this, a balance is paid off. Floating point leaves fractions of a cent
# behind, and `while balance > 0` on those never terminates.
ZERO = 0.005


# --------------------------------------------------------------------- config

def config() -> dict:
    raw = store.get_setting(CONFIG_KEY) or ""
    data = {}
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                data = loaded
        except ValueError:
            data = {}
    accounts = data.get("accounts")
    strategy = data.get("strategy")
    return {
        "extra": _clamp_float(data.get("extra"), 0.0, 1e6, 0.0),
        "strategy": strategy if strategy in STRATEGIES else AVALANCHE,
        "accounts": accounts if isinstance(accounts, dict) else {},
    }


def set_config(patch: dict) -> dict:
    cur = config()
    if "extra" in patch:
        cur["extra"] = _clamp_float(patch["extra"], 0.0, 1e6, 0.0)
    if "strategy" in patch and patch["strategy"] in STRATEGIES:
        cur["strategy"] = patch["strategy"]
    for aid, vals in (patch.get("accounts") or {}).items():
        if not isinstance(vals, dict):
            continue
        row = dict(cur["accounts"].get(aid) or {})
        if "enabled" in vals:
            row["enabled"] = bool(vals["enabled"])
        if "payment" in vals:
            row["payment"] = (None if vals["payment"] in (None, "")
                              else _clamp_float(vals["payment"], 0.0, 1e6, 0.0))
        if "apr" in vals:
            row["apr"] = (None if vals["apr"] in (None, "")
                          else _clamp_float(vals["apr"], 0.0, 100.0, 0.0))
        cur["accounts"][aid] = row
    store.set_setting(CONFIG_KEY, json.dumps(cur))
    return cur


def _clamp_float(v, lo, hi, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:                                  # NaN
        return default
    return max(lo, min(hi, f))


def _month_add(y: int, m: int, n: int) -> tuple[int, int]:
    i = (y * 12 + (m - 1)) + n
    return i // 12, i % 12 + 1


# ------------------------------------------------------------------- accounts

def _subsidized(program: str) -> bool:
    return str(program or "").upper().startswith("DLSUB")


def _statement_loans(account: dict) -> list[dict]:
    """The per-loan detail behind an account, from its newest statement.

    Present only for accounts fed by a statement import. When it is there it
    beats anything the account row can say: seventeen loans at four rates with
    their own principal, accrued interest and minimum, instead of one blended
    balance at one blended rate.
    """
    ext = account.get("external_id")
    if not ext:
        return []
    st = store.latest_loan_statement("", ext)
    if not st:
        return []
    return store.list_loan_details(st["id"])


def _debts() -> tuple[list[dict], list[dict]]:
    """Debt accounts split into (projectable, blocked).

    A debt is blocked when it has no interest rate or no payment. It is NOT
    quietly defaulted to zero: a 0% APR turns a credit card into an
    interest-free loan and reports a payoff date years too early, and a payment
    guessed from thin air is the one number the whole projection rests on.
    Blocked debts are returned so the UI can ask for exactly what is missing.
    """
    cfg = config()
    ready, blocked = [], []
    for a in store.list_finance_accounts():
        if a["kind"] not in DEBT_KINDS or a["hidden"]:
            continue
        balance = float(a["balance"] or 0)
        if balance <= ZERO:
            continue                            # already clear; nothing to plan
        acfg = cfg["accounts"].get(a["id"]) or {}
        if acfg.get("enabled") is False:
            continue

        detail = _statement_loans(a)
        apr = acfg.get("apr")
        if apr in (None, ""):
            apr = a.get("apr")
        payment = acfg.get("payment")
        if payment in (None, ""):
            payment = a.get("min_payment")

        row = {
            "account_id": a["id"], "name": a["name"],
            "institution": a.get("institution") or "", "kind": a["kind"],
            "balance": round(balance, 2),
            "apr": None if apr in (None, "") else round(float(apr), 3),
            "payment": None if payment in (None, "") else round(float(payment), 2),
            "credit_limit": a.get("credit_limit"),
            "next_due": a.get("next_due") or "",
            "detail": detail,
            "loan_count": len(detail),
            # Federal loans on Auto Pay have a scheduled rate increase; a card
            # does not. Keyed off having statement detail from a federal
            # servicer rather than guessed from the rate.
            "federal": bool(detail),
        }

        # A statement supplies the per-loan minimums, so the account is never
        # blocked for want of a payment it has already stated.
        if detail:
            row["payment"] = row["payment"] or round(
                sum(float(d["current_due"] or 0) for d in detail), 2)

        missing = []
        if row["apr"] is None and not detail:
            missing.append("apr")
        if not row["payment"]:
            missing.append("payment")
        if missing:
            row["missing"] = missing
            blocked.append(row)
        else:
            ready.append(row)
    ready.sort(key=lambda d: (-(d["apr"] or 0), d["balance"]))
    blocked.sort(key=lambda d: -d["balance"])
    return ready, blocked


def _build(ready: list[dict], step: date | None) -> tuple[list, float]:
    """Turn accounts into obligations. Returns (loans, surplus_from_overrides).

    An account with statement detail expands into one obligation per loan. An
    account without it stays a single obligation at its own rate — the honest
    shape for a credit card, and the only shape available for a loan nobody has
    uploaded a statement for.
    """
    loans, surplus = [], 0.0
    for d in ready:
        if d["detail"]:
            stated = sum(float(x["current_due"] or 0) for x in d["detail"])
            # Paying more than the bill is an OVERPAYMENT, which the servicer
            # allocates by its own rules — so the excess joins the surplus pool
            # rather than inflating each loan's minimum.
            surplus += max(0.0, (d["payment"] or 0) - stated)
            for x in d["detail"]:
                rate = float(x["rate"] or 0)
                loans.append(loanmodel.Loan(
                    key=str(d["account_id"]) + ":" + str(x["loan_ref"]),
                    name=x["loan_ref"],
                    principal=float(x["unpaid_principal"] or 0),
                    # The statement splits principal from accrued interest, so
                    # the model starts from the real split instead of assuming
                    # the whole balance is principal.
                    accrued_interest=float(x["unpaid_interest"] or 0),
                    minimum=float(x["current_due"] or 0),
                    rates=(loanmodel.auto_pay_schedule(rate, step, AUTO_PAY_STEP_UP)
                           if d["federal"] else [(None, rate)]),
                    subsidized=_subsidized(x.get("program")),
                    group=d["account_id"]))
        else:
            loans.append(loanmodel.Loan(
                key=d["account_id"], name=d["name"],
                principal=d["balance"], accrued_interest=0.0,
                minimum=d["payment"] or 0.0,
                rates=[(None, float(d["apr"] or 0))],
                subsidized=False, group=d["account_id"]))
    return loans, round(surplus, 2)


def _first_payment(ready: list[dict], today: date) -> date:
    """When the next payment lands.

    The earliest real due date still ahead of us, so the first interval charges
    the days that have actually passed since the balances were true. Falls back
    to the same day next month when nothing reported one.
    """
    dates = []
    for d in ready:
        raw = d.get("next_due") or ""
        try:
            when = date.fromisoformat(raw[:10])
        except ValueError:
            continue
        if when > today:
            dates.append(when)
    if dates:
        return min(dates)
    y, m = _month_add(today.year, today.month, 1)
    return date(y, m, min(today.day, 28))


def _monthly_interest(d: dict) -> float:
    """This month's interest at today's rates — for the underwater check."""
    if d["detail"]:
        return sum(float(x["unpaid_principal"] or 0) * float(x["rate"] or 0) / 100.0
                   for x in d["detail"]) / 12.0
    return d["balance"] * float(d["apr"] or 0) / 100.0 / 12.0


def _underwater(d: dict) -> bool:
    """True when the payment does not even cover this month's interest.

    Worth calling out on its own: it is true today, it is the reason the debt
    never clears, and unlike the payoff date it is checkable by hand.
    """
    if not d.get("payment"):
        return False
    return d["payment"] <= _monthly_interest(d) + 1e-9


# --------------------------------------------------------------------- public

def _run(ready: list[dict], extra: float, strategy: str, step, today: date) -> dict:
    loans, from_overrides = _build(ready, step)
    monthly = sum(l.minimum for l in loans) + extra + from_overrides
    return loanmodel.simulate(loans, monthly, today, _first_payment(ready, today),
                              strategy=strategy)


def plan(strategy: str | None = None, extra: float | None = None,
         auto_pay_step: bool = True) -> dict:
    """The whole payoff picture: series to draw, dates, and what extra buys."""
    cfg = config()
    strategy = strategy if strategy in STRATEGIES else cfg["strategy"]
    extra = cfg["extra"] if extra is None else _clamp_float(extra, 0.0, 1e6, 0.0)
    step = AUTO_PAY_STEP if auto_pay_step else None

    ready, blocked = _debts()
    today = date.today()
    blocked_balance = round(sum(d["balance"] for d in blocked), 2)

    if not ready:
        return {
            "months": ["%04d-%02d" % (today.year, today.month)],
            "series": [], "loans": [], "blocked": blocked, "config": cfg,
            "strategy": strategy, "extra": extra,
            "total_now": 0.0, "total_interest": 0.0, "total_paid": 0.0,
            "payoff_month": None, "debt_free": None, "stalled": [],
            "minimum_total": 0.0, "monthly_total": 0.0, "assumed": True,
            "blocked_balance": blocked_balance,
        }

    run = _run(ready, extra, strategy, step, today)
    base = _run(ready, 0.0, strategy, step, today)
    other_name = SNOWBALL if strategy == AVALANCHE else AVALANCHE
    other = _run(ready, extra, other_name, step, today)
    # What the 2028 reset itself costs, which is invisible unless both are run.
    no_step = _run(ready, extra, strategy, None, today) if step else run

    months = [h["date"][:7] for h in run["history"]]
    base_loans, from_overrides = _build(ready, step)
    minimum_total = round(sum(l.minimum for l in base_loans), 2)

    by_account = {d["account_id"]: d for d in ready}
    series = []
    for aid, d in by_account.items():
        values = [round(h["by_group"].get(aid, 0.0), 2) for h in run["history"]]
        mine = [l for l in run["loans"] if l.group == aid]
        stalled = any(l.active() for l in mine)
        cleared = [l.cleared_on for l in mine if l.cleared_on]
        payoff = None if stalled or not cleared else max(cleared)
        idx = None
        if payoff:
            key = payoff.isoformat()
            idx = next((i for i, h in enumerate(run["history"]) if h["date"] == key), None)
        series.append({
            "account_id": aid, "name": d["name"], "institution": d["institution"],
            "kind": d["kind"], "apr": d["apr"], "payment": d["payment"],
            "start_balance": d["balance"], "values": values,
            "loan_count": d["loan_count"],
            # Interest FROM HERE, not including what the statement already
            # showed as accrued — that is part of the balance, and counting it
            # as new interest would report it twice.
            "interest": round(sum(l.accrued_new for l in mine), 2),
            "opening_interest": round(sum(l.opening_interest for l in mine), 2),
            "paid": round(sum(l.paid_interest + l.paid_principal for l in mine), 2),
            "payoff_index": idx,
            "payoff_month": payoff.isoformat()[:7] if payoff else None,
            "payoff_date": payoff.isoformat() if payoff else None,
            "stalled": stalled,
            "underwater": _underwater(d),
        })
    series.sort(key=lambda s: (s["payoff_index"] is None,
                               s["payoff_index"] or 0, -s["start_balance"]))

    # Per-loan payoff, for the accounts that have real loans behind them.
    later = date(AUTO_PAY_STEP.year + 1, 1, 1)
    loans_out = []
    for l in sorted(run["loans"], key=lambda x: (x.cleared_on or date.max, x.name)):
        loans_out.append({
            "key": l.key, "name": l.name, "account_id": l.group,
            "rate": loanmodel.rate_at(l.rates, today),
            "rate_after_step": loanmodel.rate_at(l.rates, later),
            "minimum": round(l.minimum, 2),
            "start_balance": round(l.paid_principal + l.principal + l.opening_interest, 2),
            "interest": round(l.accrued_new, 2),
            "subsidized": l.subsidized,
            "payoff_date": l.cleared_on.isoformat() if l.cleared_on else None,
            "stalled": l.active(),
        })

    saved = (None if base["payoff_date"] is None or run["payoff_date"] is None
             else base["payments"] - run["payments"])

    return {
        "months": months,
        "series": series,
        "loans": loans_out,
        "blocked": blocked,
        "blocked_balance": blocked_balance,
        "config": cfg,
        "strategy": strategy,
        "extra": extra,
        "minimum_total": minimum_total,
        "monthly_total": round(minimum_total + extra + from_overrides, 2),
        "total_now": round(sum(d["balance"] for d in ready), 2),
        "total_interest": run["total_interest"],
        "total_paid": run["total_paid"],
        "payments": run["payments"],
        "final_payment": run["final_payment"],
        "payoff_month": None if not run["cleared"] else len(months) - 1,
        "debt_free": run["payoff_date"][:7] if run["payoff_date"] else None,
        "debt_free_date": run["payoff_date"],
        "stalled": sorted({l.group for l in run["loans"] if l.active()}),
        "extra_effect": {
            "months_saved": saved,
            "interest_saved": round(base["total_interest"] - run["total_interest"], 2),
            "baseline_interest": base["total_interest"],
            "baseline_payoff": base["payoff_date"],
        },
        "alternative": {
            "strategy": other_name,
            "months": other["payments"] if other["cleared"] else None,
            "interest": other["total_interest"],
            "interest_delta": round(other["total_interest"] - run["total_interest"], 2),
        },
        # The scheduled Auto Pay reset, priced. Only meaningful when a federal
        # loan is in the plan; otherwise both runs are identical and it is zero.
        "auto_pay_reset": {
            "step_date": AUTO_PAY_STEP.isoformat(),
            "step_up": AUTO_PAY_STEP_UP,
            "applies": any(d["federal"] for d in ready),
            "extra_interest": round(run["total_interest"] - no_step["total_interest"], 2),
            "months_later": (run["payments"] - no_step["payments"]
                             if run["cleared"] and no_step["cleared"] else None),
            "payoff_without": no_step["payoff_date"],
        },
        "method": {
            "basis": "daily simple interest on unpaid principal, 365.25 days",
            "allocation": ("minimums first, then surplus to the highest rate, "
                           "unsubsidised before subsidised at a tie"
                           if strategy == AVALANCHE else
                           "minimums first, then surplus to the smallest balance"),
            "first_payment": _first_payment(ready, today).isoformat(),
            "loan_level": sum(1 for d in ready if d["detail"]),
        },
        "assumed": True,        # never let the UI present this as a schedule
    }
