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
import json
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

    # Holdings are a FALLBACK, not a better answer.
    #
    # `/accounts/balance/get` asks the institution for the balance now, but
    # holdings carry their own `institution_price_as_of` and Plaid refreshes
    # those on a much slower schedule. Observed 2026-08-18: Guideline reported
    # balance $4,501.24 against holdings priced 2026-08-03 summing to $4,066.16
    # — a real account understated by $435 for fifteen days, by a sync that
    # reported success every time. Fidelity was four days behind the same way.
    #
    # Holdings are still worth keeping for the brokerages whose account balance
    # reports cash only, which is what this was originally for. So they are used
    # when there is no balance to prefer, and not otherwise.
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
        reported = _num(balances.get("current"))
        amount = reported or 0.0
        # Only when the institution reported nothing at all, or a flat zero it
        # clearly does not mean, do the holdings stand in for it.
        if (kind in ("investment", "retirement")
                and not reported and holdings_total.get(ext)):
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


# ------------------------------------------------------------ transactions

MAX_TX_PAGES = 20          # 500/page — a hard stop, not an expected limit


def _tx_category(t: dict) -> str:
    """Plaid's personal_finance_category, falling back to the legacy list.

    PFC is the current taxonomy and is far cleaner for charting (a fixed set of
    ~16 primaries); `category` is the deprecated free-ish hierarchy. Items
    enrolled before PFC still only send the old one, so both paths are live.
    """
    pfc = t.get("personal_finance_category") or {}
    if pfc.get("primary"):
        return str(pfc["primary"])
    legacy = t.get("category") or []
    return str(legacy[0]).upper().replace(" ", "_") if legacy else ""


def sync_transactions(item: dict) -> dict:
    """Pull this item's transactions via the cursor stream.

    Resumes from the stored cursor, so a re-run costs one empty round trip
    instead of re-importing history. The cursor is only advanced once a page has
    been written — crash mid-page and the next run replays it, which the upsert
    makes harmless.
    """
    token = item.get("access_token")
    if not token:
        return {"ok": False, "message": "no access token"}

    # Map Plaid's account ids to ours once, rather than per transaction.
    accounts = {a["external_id"]: a["id"]
                for a in store.list_finance_accounts()
                if a.get("item_id") == item["id"] and a.get("external_id")}

    cursor = item.get("tx_cursor") or None
    added = modified = removed = 0
    pages = 0
    try:
        while pages < MAX_TX_PAGES:
            pages += 1
            page = plaid.transactions_sync(token, cursor)
            for group, counter in (("added", "a"), ("modified", "m")):
                for t in page.get(group) or []:
                    store.upsert_finance_transaction({
                        "item_id": item["id"],
                        "account_id": accounts.get(t.get("account_id")),
                        "external_id": t.get("transaction_id"),
                        "name": t.get("name") or "",
                        "merchant": t.get("merchant_name") or "",
                        "amount": t.get("amount") or 0,
                        "category": _tx_category(t),
                        "pending": bool(t.get("pending")),
                        "posted_on": (t.get("authorized_date") or t.get("date")
                                      or store.now_iso()[:10]),
                    })
                    if counter == "a":
                        added += 1
                    else:
                        modified += 1
            gone = [t.get("transaction_id") for t in (page.get("removed") or [])
                    if t.get("transaction_id")]
            removed += store.delete_finance_transactions(gone)

            cursor = page.get("next_cursor") or cursor
            store.update_finance_item(item["id"], {"tx_cursor": cursor})
            if not page.get("has_more"):
                break
    except plaid.PlaidError as e:
        # Transactions are an add-on: an item without the product still has
        # perfectly good balances, so this must not fail the whole sync.
        return {"ok": False, "message": e.message, "code": e.code,
                "added": added, "modified": modified, "removed": removed}

    return {"ok": True, "added": added, "modified": modified, "removed": removed,
            "message": f"{added} new, {modified} updated, {removed} removed"}


def sync_all() -> list[dict]:
    out = []
    for it in store.list_finance_items():
        try:
            res = sync_item(store.get_finance_item(it["id"]))
            # Balances first — transactions need the account rows to exist so
            # each one can be attributed to the account it happened on.
            if res.get("ok"):
                res["transactions"] = sync_transactions(store.get_finance_item(it["id"]))
            out.append(res)
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
        "utilization": _utilization(accounts),
    }


# Accounts are coloured BY KIND, not individually: on a wall you read "that
# amber row is a card" at a glance, and per-account colours would make eight
# rows eight different colours that mean nothing. Stored as overrides only —
# an absent kind falls back to a slot in the theme palette, so the defaults
# re-theme with everything else instead of being frozen hex.
KIND_COLORS_KEY = "finance_kind_colors"


def kind_colors() -> dict:
    raw = store.get_setting(KIND_COLORS_KEY) or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return {k: str(v)[:24] for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str)} if isinstance(data, dict) else {}


def set_kind_colors(colors: dict) -> dict:
    clean = {str(k)[:20]: str(v)[:24] for k, v in colors.items()
             if isinstance(k, str) and isinstance(v, str) and v.strip()}
    store.set_setting(KIND_COLORS_KEY, json.dumps(clean))
    return clean


def _utilization(accounts: list[dict]) -> dict | None:
    """Revolving credit used vs available, the single most actionable card stat.

    Only cards with a stated limit count — a card whose limit Plaid doesn't
    report would otherwise drag the ratio toward zero and make a maxed-out
    wallet look healthy.
    """
    cards = [a for a in accounts
             if a["kind"] == "credit" and (a.get("credit_limit") or 0) > 0]
    if not cards:
        return None
    used = sum(a["balance"] for a in cards)
    limit = sum(a["credit_limit"] for a in cards)
    return {"used": round(used, 2), "limit": round(limit, 2),
            "pct": round((used / limit) * 100, 1) if limit else 0.0,
            "cards": len(cards)}


# ----------------------------------------------------------------- insights

CATEGORY_LABEL = {
    "FOOD_AND_DRINK": "Food & drink",
    "GENERAL_MERCHANDISE": "Shopping",
    "TRANSPORTATION": "Transport",
    "TRAVEL": "Travel",
    "RENT_AND_UTILITIES": "Rent & utilities",
    "ENTERTAINMENT": "Entertainment",
    "PERSONAL_CARE": "Personal care",
    "MEDICAL": "Medical",
    "GENERAL_SERVICES": "Services",
    "HOME_IMPROVEMENT": "Home",
    "INCOME": "Income",
    "GOVERNMENT_AND_NON_PROFIT": "Government",
    "BANK_FEES": "Fees",
    "OTHER": "Other",
}


def category_label(code: str) -> str:
    if code in CATEGORY_LABEL:
        return CATEGORY_LABEL[code]
    return (code or "Other").replace("_", " ").title()


def insights(months: int = 6) -> dict:
    """Everything the spending/cash-flow charts need, in one round trip.

    One call rather than four, because each widget re-fetching on every SSE
    nudge would hammer the database from a panel that redraws whenever anything
    at all changes.
    """
    months = max(1, min(24, int(months or 6)))
    cats = store.finance_spend_by_category(months)
    flow = store.finance_cashflow_by_month(months)
    total = sum(float(c["total"] or 0) for c in cats)

    # This month vs last, the comparison every banking app leads with.
    #
    # Keyed on the actual calendar month, NOT flow[-1]. The series is dense now,
    # but relying on position would resurrect the bug where a current month with
    # no transactions let the previous month pose as "this month" — and made the
    # delta compare the two months before it while claiming otherwise.
    now_key = store.now_iso()[:7]
    keys = [f["month"] for f in flow]
    # Position found by month STRING, not by searching for the row: dict
    # equality would happily match a different month that happened to have
    # identical totals.
    idx = keys.index(now_key) if now_key in keys else -1
    this_month = flow[idx] if idx >= 0 else None
    prev_month = flow[idx - 1] if idx > 0 else None
    spent_now = float(this_month["money_out"] or 0) if this_month else 0.0
    spent_prev = float(prev_month["money_out"] or 0) if prev_month else None

    return {
        "months": months,
        "total_spend": round(total, 2),
        "categories": [{
            "code": c["category"],
            "label": category_label(c["category"]),
            "total": round(float(c["total"] or 0), 2),
            "count": c["n"],
            "pct": round((float(c["total"] or 0) / total) * 100, 1) if total else 0.0,
        } for c in cats],
        "cashflow": [{
            "month": f["month"],
            "in": round(float(f["money_in"] or 0), 2),
            "out": round(float(f["money_out"] or 0), 2),
            "net": round(float(f["money_in"] or 0) - float(f["money_out"] or 0), 2),
        } for f in flow],
        "this_month": {
            "spent": round(spent_now, 2),
            "prev": round(spent_prev, 2) if spent_prev is not None else None,
            "delta_pct": (round(((spent_now - spent_prev) / spent_prev) * 100, 1)
                          if spent_prev else None),
        },
        "merchants": [{
            "name": m["merchant"] or "Unknown",
            "total": round(float(m["total"] or 0), 2),
            "count": m["n"],
        } for m in store.finance_top_merchants(months, 8)],
        "transaction_count": store.count_finance_transactions(),
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
