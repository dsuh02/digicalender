"""
Loan-level payoff simulation, to the servicer's own arithmetic.

`debt.py` projects an account as one balance at one rate compounded monthly.
That is fine for a card, and wrong for a federal student loan portfolio, where
seventeen loans at four rates share one payment and the servicer decides which
of them the surplus touches. This module does it the way the loans actually
work:

**Daily simple interest on unpaid principal, over actual calendar days, divided
by 365.25.** Direct Loans are daily-interest loans. Interest accrues on
principal only — it does not compound unless it capitalises, which is a distinct
event this model does not invent.

**Interest first, then principal.** Every dollar that reaches a loan clears
accrued interest before it touches the balance. This is why a payment can be
made in full and move the balance by almost nothing.

**Minimums, then surplus by MOHELA's hierarchy.** Each active loan takes up to
its listed payment; whatever is left over — the borrower's extra, plus the
payments of loans already cleared — goes to the highest rate, preferring
UNSUBSIDISED at a rate tie (a subsidised loan is the cheaper one to leave
alive), splitting equally at a full tie, capped at what each loan actually owes,
with the residue redistributed.

**The rate is a schedule, not a number.** The Auto Pay reduction is temporarily
1.00 point instead of the ordinary 0.25 through 30 June 2028, so today's rates
step UP 0.75 after that date. A projection at today's rate quietly promises a
payoff that the loans will not deliver.

That last point is not an assumption taken on faith here: the archived
statements show it directly. June's statement lists 4.740 / 5.250 / 6.280 and
July's lists 3.990 / 4.500 / 5.530 — the same loans, exactly 0.75 apart, in the
month the temporary benefit began. The schedule is inferred from policy and
CONFIRMED by the borrower's own documents.

Everything here is still arithmetic on stated assumptions. It does not model
capitalisation, forbearance, plan changes, forgiveness, IDR subsidies, fees,
returned payments, or a servicer recalculating the required payment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

# Direct Loans divide by 365.25, not 365 or 360. Over a decade the difference is
# real money, and this is the servicer's published divisor.
DAYS_PER_YEAR = 365.25

# Sub-cent noise from float arithmetic. A loan inside this is paid.
EPS = 0.005

# A hard bound on iteration, not a horizon. A debt that has not cleared by here
# is reported as never clearing rather than given a date.
MAX_PAYMENTS = 12 * 60


@dataclass
class Loan:
    """One obligation. `rates` is [(effective_from|None, annual_rate_pct), ...]."""
    key: str
    principal: float
    accrued_interest: float = 0.0
    minimum: float = 0.0
    rates: list = field(default_factory=list)
    subsidized: bool = False
    group: str = ""                     # which account this rolls up into
    name: str = ""

    # running totals
    paid_interest: float = 0.0
    paid_principal: float = 0.0
    # Interest accrued DURING the simulation, separate from `paid_interest`,
    # which also clears whatever was already on the books when it started.
    # Only this one belongs in "interest from here": the opening accrued
    # interest is part of the balance being paid off, and counting it as new
    # interest reports it twice.
    accrued_new: float = 0.0
    opening_interest: float = 0.0
    cleared_on: date | None = None

    def owed(self) -> float:
        return self.principal + self.accrued_interest

    def active(self) -> bool:
        return self.owed() > EPS


def rate_at(rates: list, when: date) -> float:
    """The annual rate in force on a date."""
    best = 0.0
    for start, rate in sorted(rates, key=lambda r: r[0] or date.min):
        if start is None or start <= when:
            best = rate
    return best


def accrue(principal: float, rates: list, d0: date, d1: date) -> float:
    """Simple daily interest from d0 to d1, split across any rate change.

    An interval that straddles a rate change is charged at BOTH rates for the
    days each was in force. Applying whichever rate happened to be in effect on
    the payment date would misprice the month the benefit expires.
    """
    if d1 <= d0 or principal <= 0:
        return 0.0
    pts = sorted(rates, key=lambda r: r[0] or date.min)
    total = 0.0
    for i, (start, rate) in enumerate(pts):
        seg_start = start or date.min
        seg_end = pts[i + 1][0] if i + 1 < len(pts) and pts[i + 1][0] else date.max
        lo = max(d0, seg_start)
        hi = min(d1, seg_end)
        if hi > lo:
            total += principal * (rate / 100.0) * (hi - lo).days / DAYS_PER_YEAR
    return total


def _target_tier(room_left: list[Loan], when: date, strategy: str) -> list[Loan]:
    """Which loans the next surplus dollar goes to.

    `avalanche` is the servicer's own published rule — highest rate, preferring
    unsubsidised at a rate tie, split equally at a full tie. `snowball` is the
    borrower's override: smallest balance first, to clear a whole loan sooner.
    """
    if strategy == "snowball":
        low = min(l.owed() for l in room_left)
        return [l for l in room_left if l.owed() - low < 0.01]

    top = max(rate_at(l.rates, when) for l in room_left)
    tier = [l for l in room_left if abs(rate_at(l.rates, when) - top) < 1e-9]
    # Unsubsidised first at a rate tie: a subsidised loan is the cheaper one to
    # leave running, so the surplus should not go there while an unsubsidised
    # loan at the same rate is still alive.
    unsub = [l for l in tier if not l.subsidized]
    if unsub and len(unsub) < len(tier):
        tier = unsub
    return tier


def allocate(loans: list[Loan], budget: float, when: date,
             strategy: str = "avalanche") -> dict:
    """Split one payment across loans. Returns {key: amount}.

    Minimums first, then the surplus. The surplus loop recomputes the target set
    each pass, so when the leading loans are satisfied the residue falls through
    to the next instead of being lost.
    """
    alloc = {l.key: 0.0 for l in loans}
    live = [l for l in loans if l.active()]

    for l in live:
        if budget <= EPS:
            break
        room = l.owed() - alloc[l.key]
        take = min(l.minimum, room, budget)
        if take > 0:
            alloc[l.key] += take
            budget -= take

    while budget > EPS:
        room_left = [l for l in live if l.owed() - alloc[l.key] > EPS]
        if not room_left:
            break
        tier = _target_tier(room_left, when, strategy)
        share = budget / len(tier)
        moved = 0.0
        for l in tier:
            room = l.owed() - alloc[l.key]
            take = min(share, room)
            alloc[l.key] += take
            moved += take
        budget -= moved
        if moved <= EPS:
            break                        # nothing could absorb it; stop
    return alloc


def payment_dates(first: date, count: int) -> list[date]:
    """The same day-of-month each month, clamped into short months."""
    out = []
    y, m, day = first.year, first.month, first.day
    for i in range(count):
        yy = y + (m - 1 + i) // 12
        mm = (m - 1 + i) % 12 + 1
        d = day
        while d > 1:
            try:
                out.append(date(yy, mm, d))
                break
            except ValueError:
                d -= 1                   # the 31st of a 30-day month
        else:
            out.append(date(yy, mm, 1))
    return out


def simulate(loans: list[Loan], monthly: float, start: date, first_payment: date,
             max_payments: int = MAX_PAYMENTS, strategy: str = "avalanche") -> dict:
    """Run to payoff. Returns per-loan results and a monthly series.

    `start` is the date the balances are true as of; interest accrues from there
    to the first payment, so a projection run mid-cycle charges the days that
    have already passed rather than pretending the clock starts at the payment.
    """
    when = start
    for l in loans:
        l.opening_interest = l.accrued_interest
    schedule = []
    history = [{"date": start.isoformat(),
                "total": round(sum(l.owed() for l in loans), 2),
                "by_group": _by_group(loans)}]
    total_paid = 0.0
    total_interest = 0.0

    for n, pay_date in enumerate(payment_dates(first_payment, max_payments)):
        live = [l for l in loans if l.active()]
        if not live:
            break

        for l in live:
            got = accrue(l.principal, l.rates, when, pay_date)
            l.accrued_interest += got
            l.accrued_new += got
            total_interest += got

        due = sum(l.owed() for l in live)
        budget = min(monthly, due)
        if budget <= EPS:
            break                        # a payment of nothing never clears

        alloc = allocate(loans, budget, pay_date, strategy)
        applied = 0.0
        for l in live:
            amount = alloc.get(l.key, 0.0)
            if amount <= 0:
                continue
            to_interest = min(amount, l.accrued_interest)
            l.accrued_interest -= to_interest
            l.paid_interest += to_interest
            to_principal = amount - to_interest
            l.principal -= to_principal
            l.paid_principal += to_principal
            applied += amount
            if l.owed() <= EPS:
                # Sweep the crumbs so a hundredth of a cent does not keep a
                # cleared loan alive for another month.
                l.principal = 0.0
                l.accrued_interest = 0.0
                if l.cleared_on is None:
                    l.cleared_on = pay_date

        total_paid += applied
        schedule.append({"n": n + 1, "date": pay_date.isoformat(),
                         "paid": round(applied, 2),
                         "balance": round(sum(l.owed() for l in loans), 2)})
        history.append({"date": pay_date.isoformat(),
                        "total": round(sum(l.owed() for l in loans), 2),
                        "by_group": _by_group(loans)})
        when = pay_date

        # A payment that cannot outrun the interest never clears the loan, and a
        # month where the balance did not move proves it — with no other loan
        # left to free up a payment, nothing about later months differs.
        if applied <= EPS:
            break

    outstanding = [l for l in loans if l.active()]
    return {
        "loans": loans,
        "schedule": schedule,
        "history": history,
        "payments": len(schedule),
        "final_payment": schedule[-1]["paid"] if schedule else 0.0,
        "payoff_date": None if outstanding else (schedule[-1]["date"] if schedule else None),
        "cleared": not outstanding,
        "outstanding": [l.key for l in outstanding],
        "total_paid": round(total_paid, 2),
        "total_interest": round(total_interest, 2),
    }


def _by_group(loans: list[Loan]) -> dict:
    out: dict[str, float] = {}
    for l in loans:
        out[l.group] = round(out.get(l.group, 0.0) + l.owed(), 2)
    return out


def auto_pay_schedule(displayed_rate: float, step_date: date | None,
                      step_up: float = 0.75) -> list:
    """Today's rate, then the scheduled increase.

    Federal Student Aid raised the Auto Pay reduction from 0.25 to 1.00 point
    temporarily, through 30 June 2028. A rate shown today therefore has 1.00
    taken off it, and reverts to 0.25 off — 0.75 higher — the day after.
    """
    if not step_date:
        return [(None, displayed_rate)]
    return [(None, displayed_rate), (step_date, displayed_rate + step_up)]
