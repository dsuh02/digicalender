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
* **Interest is simple monthly accrual on the whole balance.** Cards compound
  daily on an average balance and student loans accrue daily on principal only.
  Over a payoff horizon the gap is small next to the error in the payment you
  typed.
* **Multi-loan servicer allocation.** The Aidvantage account is seventeen loans
  at four rates, and the servicer prorates payments across them by its own
  rules. It is projected as one balance at the balance-weighted rate, which
  tracks total paydown closely but will not match any single loan.

Every one of those makes this arithmetic on stated assumptions, not a schedule
anyone is entitled to hold a servicer to.
"""

from __future__ import annotations

import json
from datetime import date

import store

CONFIG_KEY = "finance_debt_plan"

DEBT_KINDS = ("credit", "loan")
MAX_MONTHS = 12 * 60            # 60 years: an iteration cap, not a real horizon
AVALANCHE, SNOWBALL = "avalanche", "snowball"
STRATEGIES = (AVALANCHE, SNOWBALL)

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
        }

        missing = []
        if row["apr"] is None:
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


def _order(active: list[dict], strategy: str) -> list[dict]:
    """Who gets the extra money first."""
    if strategy == SNOWBALL:
        return sorted(active, key=lambda d: d["_bal"])          # quickest win
    return sorted(active, key=lambda d: -d["_apr"])             # cheapest total


# ---------------------------------------------------------------- simulation

def simulate(debts: list[dict], extra: float, strategy: str) -> dict:
    """Run the payoff month by month. Returns per-debt series and totals.

    Each month, in this order: accrue interest, pay every debt its own payment,
    then hand the surplus to whichever debt the strategy targets. The surplus is
    the user's extra PLUS the payments of debts already cleared — that rollover
    is what makes later debts fall faster than earlier ones.
    """
    state = [{**d, "_bal": d["balance"], "_apr": (d["apr"] or 0) / 100.0 / 12.0,
              "_pay": d["payment"] or 0.0, "_interest": 0.0,
              "_paid": 0.0, "_off": None, "values": [round(d["balance"], 2)]}
             for d in debts]

    months_run = 0
    for step in range(MAX_MONTHS):
        active = [d for d in state if d["_bal"] > ZERO]
        if not active:
            break

        # Freed payments join the pool; a cleared debt keeps contributing.
        pool = extra + sum(d["_pay"] for d in state if d["_bal"] <= ZERO)

        for d in active:
            interest = d["_bal"] * d["_apr"]
            d["_bal"] += interest
            d["_interest"] += interest
            pay = min(d["_pay"], d["_bal"])
            d["_bal"] -= pay
            d["_paid"] += pay

        for d in _order([d for d in state if d["_bal"] > ZERO], strategy):
            if pool <= ZERO:
                break
            pay = min(pool, d["_bal"])
            d["_bal"] -= pay
            d["_paid"] += pay
            pool -= pay

        cleared = False
        for d in state:
            if d["_bal"] <= ZERO:
                d["_bal"] = 0.0
                if d["_off"] is None:
                    d["_off"] = step + 1
                    cleared = True
            d["values"].append(round(d["_bal"], 2))
        months_run = step + 1

        # Nothing went down and nothing was cleared: the only event that can
        # change these dynamics is a debt being paid off, and none can be. The
        # balances diverge from here, so stop rather than compounding for sixty
        # years — that produced a $209 BILLION line and a chart axis to match.
        shrank = any(d["_bal"] < d["values"][-2] - ZERO for d in active)
        if not shrank and not cleared:
            break

    # Anything still owing after the cap does not converge. Reported as such
    # rather than as a payoff sixty years out, which would be a made-up date.
    stalled = [d for d in state if d["_bal"] > ZERO]
    return {
        "state": state,
        "months_run": months_run,
        "stalled": [d["account_id"] for d in stalled],
        "total_interest": round(sum(d["_interest"] for d in state), 2),
        "total_paid": round(sum(d["_paid"] for d in state), 2),
        "payoff_month": None if stalled else months_run,
    }


def _underwater(d: dict) -> bool:
    """True when this month's payment does not even cover this month's interest.

    Worth calling out on its own: it is true today, it is the reason the debt
    never clears, and unlike the payoff date it is checkable by hand.
    """
    if not d.get("apr") or not d.get("payment"):
        return False
    return d["payment"] <= d["balance"] * (d["apr"] / 100.0 / 12.0) + 1e-9


# --------------------------------------------------------------------- public

def plan(strategy: str | None = None, extra: float | None = None) -> dict:
    """The whole payoff picture: series to draw, dates, and what extra buys."""
    cfg = config()
    strategy = strategy if strategy in STRATEGIES else cfg["strategy"]
    extra = cfg["extra"] if extra is None else _clamp_float(extra, 0.0, 1e6, 0.0)

    ready, blocked = _debts()
    today = date.today()

    if not ready:
        return {
            "months": [f"{today.year:04d}-{today.month:02d}"],
            "series": [], "blocked": blocked, "config": cfg,
            "strategy": strategy, "extra": extra,
            "total_now": 0.0, "total_interest": 0.0, "total_paid": 0.0,
            "payoff_month": None, "debt_free": None, "stalled": [],
            "minimum_total": 0.0, "assumed": True,
            "blocked_balance": round(sum(d["balance"] for d in blocked), 2),
        }

    run = simulate(ready, extra, strategy)
    # The comparison that makes the extra worth typing: same debts, no extra.
    base = simulate(ready, 0.0, strategy)
    # And the road not taken, so the strategy choice is priced rather than
    # asserted. Snowball is compared against avalanche at the same extra.
    other_name = SNOWBALL if strategy == AVALANCHE else AVALANCHE
    other = simulate(ready, extra, other_name)

    n = run["months_run"]
    months = []
    y, m = today.year, today.month
    for _ in range(n + 1):
        months.append(f"{y:04d}-{m:02d}")
        y, m = _month_add(y, m, 1)

    def month_of(idx):
        if idx is None or idx > n:
            return None
        return months[idx]

    series = []
    for d in run["state"]:
        # Every series is padded to the same length so the chart's months axis
        # lines up; a debt cleared in year two is a flat zero after it, not a
        # short line the chart has to guess the end of.
        vals = d["values"] + [0.0] * (n + 1 - len(d["values"]))
        src = next(x for x in ready if x["account_id"] == d["account_id"])
        series.append({
            "account_id": d["account_id"], "name": d["name"],
            "institution": d["institution"], "kind": d["kind"],
            "apr": d["apr"], "payment": d["payment"],
            "start_balance": d["balance"], "values": vals[:n + 1],
            "interest": round(d["_interest"], 2),
            "paid": round(d["_paid"], 2),
            "payoff_index": d["_off"],
            "payoff_month": month_of(d["_off"]),
            "stalled": d["account_id"] in run["stalled"],
            "underwater": _underwater(src),
        })
    # Payoff order, soonest first; the ones that never clear go last.
    series.sort(key=lambda s: (s["payoff_index"] is None,
                               s["payoff_index"] or 0, -s["start_balance"]))

    saved_months = (None if base["payoff_month"] is None or run["payoff_month"] is None
                    else base["payoff_month"] - run["payoff_month"])

    return {
        "months": months,
        "series": series,
        "blocked": blocked,
        "blocked_balance": round(sum(d["balance"] for d in blocked), 2),
        "config": cfg,
        "strategy": strategy,
        "extra": extra,
        "minimum_total": round(sum(d["payment"] or 0 for d in ready), 2),
        "monthly_total": round(sum(d["payment"] or 0 for d in ready) + extra, 2),
        "total_now": round(sum(d["balance"] for d in ready), 2),
        "total_interest": run["total_interest"],
        "total_paid": run["total_paid"],
        "payoff_month": run["payoff_month"],
        "debt_free": month_of(run["payoff_month"]),
        "stalled": run["stalled"],
        "extra_effect": {
            "months_saved": saved_months,
            "interest_saved": round(base["total_interest"] - run["total_interest"], 2),
            "baseline_interest": base["total_interest"],
            "baseline_payoff": base["payoff_month"],
        },
        "alternative": {
            "strategy": other_name,
            "months": other["payoff_month"],
            "interest": other["total_interest"],
            "interest_delta": round(other["total_interest"] - run["total_interest"], 2),
        },
        "assumed": True,        # never let the UI present this as a schedule
    }
