"""
Long-term savings projection.

Answers one question for retirement accounts: given what is in them now, what
goes in each month, and an assumed rate of return, what do they look like every
month between today and the year you turn 70?

Three deliberate choices:

**Monthly compounding, contribution applied at period end.** Real accounts
compound daily and contributions land on payday, but the difference over
decades is a rounding error next to the error in guessing the rate of return.
Monthly keeps the series small enough to pan and zoom on a panel at 60fps —
about 500 points per account for a person in their late twenties.

**Growth is per-account, not global.** A Roth of index funds and a 401k in a
target-date fund do not earn the same, and averaging them into one rate hides
exactly the comparison the chart exists to make.

**Nothing here is a forecast.** It is arithmetic on assumptions the user typed.
The API returns those assumptions alongside the numbers so the UI can keep
saying so; a projection that looks authoritative is worse than no projection.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import plaid
import store

CONFIG_KEY = "finance_projection"

DEFAULT_GROWTH = 7.0        # long-run nominal, before inflation
DEFAULT_TARGET_AGE = 70
MAX_MONTHS = 12 * 60        # 60 years; a bound on the series, not a real limit

PROJECTED_KINDS = ("retirement", "investment")


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
    return {
        "birth_year": _int_or_none(data.get("birth_year")),
        "target_age": _clamp_int(data.get("target_age"), 30, 100, DEFAULT_TARGET_AGE),
        "default_growth": _clamp_float(data.get("default_growth"), -20.0, 30.0, DEFAULT_GROWTH),
        "accounts": accounts if isinstance(accounts, dict) else {},
    }


def set_config(patch: dict) -> dict:
    cur = config()
    if "birth_year" in patch:
        cur["birth_year"] = _int_or_none(patch["birth_year"])
    if "target_age" in patch:
        cur["target_age"] = _clamp_int(patch["target_age"], 30, 100, DEFAULT_TARGET_AGE)
    if "default_growth" in patch:
        cur["default_growth"] = _clamp_float(patch["default_growth"], -20.0, 30.0, DEFAULT_GROWTH)
    if isinstance(patch.get("accounts"), dict):
        for aid, cfg in patch["accounts"].items():
            if not isinstance(cfg, dict):
                continue
            entry = dict(cur["accounts"].get(aid) or {})
            if "monthly" in cfg:
                entry["monthly"] = _clamp_float(cfg["monthly"], 0.0, 1e6, 0.0)
            if "growth" in cfg:
                # None means "follow the default", which is not the same as 0%.
                entry["growth"] = (None if cfg["growth"] in (None, "")
                                   else _clamp_float(cfg["growth"], -20.0, 30.0, DEFAULT_GROWTH))
            if "enabled" in cfg:
                entry["enabled"] = bool(cfg["enabled"])
            cur["accounts"][str(aid)[:64]] = entry
    store.set_setting(CONFIG_KEY, json.dumps(cur))
    return cur


def _int_or_none(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if 1900 <= n <= date.today().year else None


def _clamp_int(v, lo, hi, default):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _clamp_float(v, lo, hi, default):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    if n != n:                      # NaN
        return default
    return max(lo, min(hi, n))


# ---------------------------------------------------------------- month axis

def _month_add(y: int, m: int, n: int) -> tuple[int, int]:
    total = (y * 12 + (m - 1)) + n
    return total // 12, total % 12 + 1


def _months_between(start: tuple[int, int], end: tuple[int, int]) -> int:
    return (end[0] * 12 + end[1] - 1) - (start[0] * 12 + start[1] - 1)


# -------------------------------------------------------------- contributions

def contributions_to_date(accounts: list[dict]) -> dict:
    """Actual money paid IN per account, from Plaid's investment activity.

    Best effort by design. The product may not be enabled, the institution may
    not report it, and a 401k opened before the item was linked has history
    Plaid never saw — so a missing or low number here means "not known", never
    "you contributed nothing". The UI has to say so.
    """
    by_item: dict[str, list[dict]] = {}
    for a in accounts:
        if a.get("item_id"):
            by_item.setdefault(a["item_id"], []).append(a)

    out: dict[str, dict] = {}
    end = date.today()
    start = end - timedelta(days=365 * 5)
    for item_id, group in by_item.items():
        item = store.get_finance_item(item_id)
        if not item or not item.get("access_token"):
            continue
        try:
            res = plaid.investments_transactions(
                item["access_token"], start.isoformat(), end.isoformat())
        except plaid.PlaidError:
            continue                       # product not enabled, or unsupported

        ext_to_id = {a["external_id"]: a["id"] for a in group if a.get("external_id")}
        for t in res.get("investment_transactions") or []:
            aid = ext_to_id.get(t.get("account_id"))
            if not aid:
                continue
            # ONLY an explicit contribution subtype counts.
            #
            # There used to be a fallback here for "type == cash and amount < 0",
            # meant to catch institutions that leave subtype blank. It caught
            # Fidelity's DIVIDEND REINVESTMENTS instead — which are exactly that
            # shape — and turned 3 real contributions into 16, inflating the
            # count fivefold. A loose rule that silently mislabels data is worse
            # than missing an institution that does not report cleanly.
            #
            # Verified against both live providers: Guideline sends
            # (buy, contribution) "Payroll contribution"; Fidelity sends
            # (cash, contribution) "CASH CONTRIBUTION CURRENT YEAR".
            if str(t.get("subtype") or "") not in ("contribution", "deposit"):
                continue
            amount = abs(float(t.get("amount") or 0))
            if amount <= 0:
                continue
            rec = out.setdefault(aid, {"total": 0.0, "count": 0, "first": None,
                                       "last": None, "recent": []})
            rec["total"] += amount
            rec["count"] += 1
            d = t.get("date")
            if d:
                rec["first"] = min(rec["first"] or d, d)
                rec["last"] = max(rec["last"] or d, d)
                rec["recent"].append({"date": d, "amount": round(amount, 2)})

    cutoff = (date.today() - timedelta(days=365)).isoformat()
    for rec in out.values():
        rec["total"] = round(rec["total"], 2)
        rec["recent"].sort(key=lambda r: r["date"], reverse=True)

        # The trailing year, NOT the lifetime average, is what someone should
        # type into a contributions field. A lifetime figure smears a raise or a
        # newly-opened account across months that never had one — the Roth here
        # reads $27/mo lifetime off three lumpy deposits, which is not a rate
        # anybody chose.
        recent12 = [r for r in rec["recent"] if r["date"] >= cutoff]
        rec["last12_total"] = round(sum(r["amount"] for r in recent12), 2)
        rec["last12_count"] = len(recent12)
        if recent12:
            months = max(1, _months_between(
                (int(recent12[-1]["date"][:4]), int(recent12[-1]["date"][5:7])),
                (int(recent12[0]["date"][:4]), int(recent12[0]["date"][5:7]))) + 1)
            rec["monthly_avg"] = round(rec["last12_total"] / months, 2)
            rec["months_observed"] = months
        else:
            rec["monthly_avg"] = None
            rec["months_observed"] = 0
        rec["recent"] = rec["recent"][:6]
    return out


# ------------------------------------------------------------------ projection

def project(include_contributions: bool = False) -> dict:
    """Monthly balance per account from this month to the target age."""
    cfg = config()
    accounts = [a for a in store.list_finance_accounts()
                if a["kind"] in PROJECTED_KINDS and not a["hidden"]]
    accounts.sort(key=lambda a: (a["kind"], a["name"]))

    today = date.today()
    start = (today.year, today.month)

    if cfg["birth_year"]:
        end_year = cfg["birth_year"] + cfg["target_age"]
        n_months = _months_between(start, (end_year, today.month))
        # Someone already past the target age still gets a chart, just a short
        # one, rather than an empty axis and no explanation.
        n_months = max(12, min(MAX_MONTHS, n_months))
    else:
        n_months = 12 * 40

    months = []
    y, m = start
    for i in range(n_months + 1):
        months.append(f"{y:04d}-{m:02d}")
        y, m = _month_add(y, m, 1)

    contrib = contributions_to_date(accounts) if include_contributions else {}

    series = []
    for a in accounts:
        acfg = cfg["accounts"].get(a["id"]) or {}
        if acfg.get("enabled") is False:
            continue
        monthly = _clamp_float(acfg.get("monthly"), 0.0, 1e6, 0.0)
        growth = acfg.get("growth")
        rate = (cfg["default_growth"] if growth in (None, "")
                else _clamp_float(growth, -20.0, 30.0, cfg["default_growth"]))
        step = rate / 100.0 / 12.0

        balance = float(a["balance"] or 0)
        values = [round(balance, 2)]
        paid_in = 0.0
        for _ in range(n_months):
            balance = balance * (1 + step) + monthly
            paid_in += monthly
            values.append(round(balance, 2))

        series.append({
            "account_id": a["id"],
            "name": a["name"],
            "institution": a.get("institution") or "",
            "kind": a["kind"],
            "start_balance": round(float(a["balance"] or 0), 2),
            "monthly": monthly,
            "growth": rate,
            "values": values,
            "future_contributions": round(paid_in, 2),
            "end_balance": values[-1],
            "contributed_to_date": contrib.get(a["id"]),
        })

    return {
        "months": months,
        "series": series,
        "config": cfg,
        "current_age": (today.year - cfg["birth_year"]) if cfg["birth_year"] else None,
        "end_age": cfg["target_age"] if cfg["birth_year"] else None,
        "horizon_years": round(n_months / 12, 1),
        "assumed": True,       # never let the UI present this as a forecast
        "total_now": round(sum(s["start_balance"] for s in series), 2),
        "total_end": round(sum(s["end_balance"] for s in series), 2),
    }
