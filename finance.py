"""
Money: syncing Plaid items into accounts, and turning due dates into calendar
events.

Kinds are normalised from Plaid's (type, subtype) pair into the handful this
app displays, because "depository/savings" and "investment/401k" are the
distinctions that matter on a wall, not Plaid's full taxonomy.

Bills become real events under a reserved calendar source named "Bills", which
means they inherit everything calendars already do: colour, the visibility
toggle in the layers sheet, the agenda, and reminder notifications. Nothing in
the calendar code needed to learn about money.
"""

from __future__ import annotations

import calendar as _cal
from datetime import date, timedelta

import plaid
import store

# Plaid (type, subtype) -> our kind. Subtype wins where it's meaningful.
_SUBTYPE_KIND = {
    "checking": "checking", "savings": "savings", "hsa": "savings",
    "money market": "savings", "cd": "savings",
    "credit card": "credit", "paypal": "checking",
    "student": "loan", "mortgage": "loan", "auto": "loan",
    "line of credit": "credit", "home equity": "loan", "loan": "loan",
    "401k": "retirement", "403b": "retirement", "457b": "retirement",
    "ira": "retirement", "roth": "retirement", "roth 401k": "retirement",
    "sep ira": "retirement", "simple ira": "retirement", "pension": "retirement",
    "brokerage": "investment", "hsa investment": "investment",
    "529": "investment", "mutual fund": "investment", "non-taxable brokerage account": "investment",
}
_TYPE_KIND = {"depository": "checking", "credit": "credit", "loan": "loan",
              "investment": "investment", "brokerage": "investment"}

DEBT_KINDS = {"credit", "loan"}

KIND_LABEL = {
    "checking": "Checking", "savings": "Savings", "credit": "Credit cards",
    "loan": "Loans", "investment": "Investments", "retirement": "Retirement",
    "other": "Other",
}


def classify(acct_type: str, subtype: str, name: str = "") -> str:
    """Subtype first (it carries 401k/roth/student), then the broad type."""
    sub = (subtype or "").lower()
    typ = (acct_type or "").lower()
    return _SUBTYPE_KIND.get(sub) or _TYPE_KIND.get(typ) or "other"


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def sync_item(item: dict) -> dict:
    """Pull balances (and liabilities/holdings where supported) for one item."""
    token = item.get("access_token")
    if not token:
        return {"id": item["id"], "ok": False, "message": "no access token"}

    try:
        bal = plaid.accounts_balance(token)
    except plaid.PlaidError as e:
        store.update_finance_item(item["id"], {"status": f"error: {e.code or e.message}"})
        return {"id": item["id"], "ok": False, "message": e.message, "code": e.code}

    institution = item.get("institution") or ""
    if not institution:
        institution = plaid.institution_name((bal.get("item") or {}).get("institution_id", ""))
        if institution:
            store.update_finance_item(item["id"], {"institution": institution})

    # Optional detail: absent products are normal, not failures.
    liab, inv = {}, {}
    try:
        liab = plaid.liabilities(token)
    except plaid.PlaidError:
        pass
    try:
        inv = plaid.investments(token)
    except plaid.PlaidError:
        pass

    extra = {}
    for group, key in (("credit", "credit"), ("student", "student"), ("mortgage", "mortgage")):
        for row in (liab.get("liabilities") or {}).get(group, []) or []:
            aid = row.get("account_id")
            if not aid:
                continue
            e = extra.setdefault(aid, {})
            if key == "credit":
                e["min_payment"] = _num(row.get("minimum_payment_amount"))
                e["next_due"] = row.get("next_payment_due_date")
                # Cards carry several APRs (purchase, cash advance, balance
                # transfer); show the purchase rate when it's identifiable.
                aprs = row.get("aprs") or []
                purchase = next((x for x in aprs
                                 if str(x.get("apr_type", "")).startswith("purchase")), None)
                pick = purchase or (aprs[0] if aprs else None)
                if pick:
                    e["apr"] = _num(pick.get("apr_percentage"))
            else:
                e["min_payment"] = _num(row.get("minimum_payment_amount"))
                e["next_due"] = row.get("next_payment_due_date")
                e["apr"] = _num(row.get("interest_rate_percentage"))

    # Investment holdings give a truer figure than the raw account balance for
    # brokerages that report cash only.
    holdings_total = {}
    for h in (inv.get("holdings") or []):
        aid = h.get("account_id")
        if aid:
            holdings_total[aid] = holdings_total.get(aid, 0) + (_num(h.get("institution_value")) or 0)

    n = 0
    for a in bal.get("accounts") or []:
        ext = a.get("account_id")
        balances = a.get("balances") or {}
        kind = classify(a.get("type", ""), a.get("subtype", ""), a.get("name", ""))
        amount = _num(balances.get("current")) or 0.0
        if kind in ("investment", "retirement") and holdings_total.get(ext):
            amount = holdings_total[ext]
        e = extra.get(ext, {})
        fields = {
            "name": a.get("name") or "Account",
            "official_name": a.get("official_name") or "",
            "institution": institution,
            "kind": kind,
            "subtype": a.get("subtype") or "",
            "mask": a.get("mask") or "",
            "balance": amount,
            "available": _num(balances.get("available")),
            "credit_limit": _num(balances.get("limit")),
            "apr": e.get("apr"),
            "min_payment": e.get("min_payment"),
            "next_due": e.get("next_due"),
        }
        existing = store.find_finance_account(item["id"], ext)
        if existing:
            # Never overwrite a name the user has renamed by hand.
            if existing["name"] != (a.get("name") or "Account"):
                fields.pop("name", None)
            store.update_finance_account(existing["id"], fields)
        else:
            store.create_finance_account({**fields, "item_id": item["id"], "external_id": ext})
        n += 1

    store.update_finance_item(item["id"], {"status": f"ok: {n} accounts",
                                           "last_sync": store.now_iso()})
    return {"id": item["id"], "ok": True, "message": f"{n} accounts",
            "institution": institution, "count": n}


def sync_all() -> list[dict]:
    out = []
    for it in store.list_finance_items():
        try:
            out.append(sync_item(store.get_finance_item(it["id"])))
        except Exception as e:
            out.append({"id": it["id"], "ok": False, "message": str(e)})
    return out


# ------------------------------------------------------------------ summary

def summary() -> dict:
    accounts = [a for a in store.list_finance_accounts() if not a["hidden"]]
    assets = sum(a["balance"] for a in accounts if a["kind"] not in DEBT_KINDS)
    debts = sum(a["balance"] for a in accounts if a["kind"] in DEBT_KINDS)
    groups = {}
    for a in accounts:
        groups.setdefault(a["kind"], []).append(a)
    return {
        "assets": round(assets, 2),
        "debts": round(debts, 2),
        "net": round(assets - debts, 2),
        "by_kind": {k: round(sum(x["balance"] for x in v), 2) for k, v in groups.items()},
        "count": len(accounts),
    }


# ------------------------------------------------------- bills as calendar

BILLS_NAME = "Bills"
HORIZON_MONTHS = 12


def _bills_account() -> dict:
    for a in store.list_accounts(provider="bills"):
        return a
    return store.create_account({
        "provider": "bills", "display_name": BILLS_NAME, "color": "",
        "token_json": {"generated": True},
    })


def _due_dates(acct: dict, months: int = HORIZON_MONTHS) -> list[date]:
    """Every upcoming due date for an account, from an explicit next_due or a
    day-of-month. Clamped to the month's length so a 31st lands on the 30th in
    November rather than vanishing."""
    day = acct.get("due_day")
    if not day and acct.get("next_due"):
        try:
            day = int(str(acct["next_due"])[8:10])
        except (ValueError, IndexError):
            day = None
    if not day:
        return []
    day = max(1, min(int(day), 31))
    today = date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(months):
        last = _cal.monthrange(y, m)[1]
        d = date(y, m, min(day, last))
        if d >= today - timedelta(days=3):
            out.append(d)
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def sync_bill_events() -> int:
    """Regenerate the Bills calendar from accounts that have a due date."""
    src = _bills_account()
    events = []
    for a in store.list_finance_accounts():
        if a["hidden"] or a["kind"] not in DEBT_KINDS:
            continue
        for d in _due_dates(a):
            bits = [a["name"]]
            if a.get("min_payment"):
                bits.append(f"min ${a['min_payment']:,.0f}")
            events.append({
                "external_id": f"bill:{a['id']}:{d.isoformat()}",
                "title": " · ".join(bits) + " due",
                "description": (f"{a['institution']} · balance "
                                f"${abs(a['balance']):,.2f}").strip(" ·"),
                "location": "",
                "start_utc": f"{d.isoformat()}T00:00:00Z",
                "end_utc": f"{(d + timedelta(days=1)).isoformat()}T00:00:00Z",
                "all_day": True,
                "color": src.get("color") or None,
            })
    return store.replace_feed_events(src["id"], events)
