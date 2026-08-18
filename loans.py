"""
Aidvantage monthly statements, read from the PDF and reconciled before storage.

Aidvantage stopped supporting Plaid, so there is no API to sync from — the
monthly statement is the only machine-readable record of these loans that
exists. Everything here turns that PDF into rows.

WHY THIS RECONCILES INSTEAD OF JUST PARSING
-------------------------------------------
A federal loan statement is a wide table, and when a borrower has more than ten
loans it SPILLS ONTO A SECOND PAGE. A parser that reads the first "Loan
Information" block and stops looks completely successful — it returns ten loans,
every number well-formed — while under-reporting the balance by forty percent.
Nothing about the output would look wrong.

So the parse is not trusted on its own. The statement states the same figures in
three independent places:

  * per-loan columns, which must sum to
  * the "Total" column of the loan table, which must equal
  * the Account Summary panel on page one.

`parse()` checks all three agree and raises if they do not. A statement that
does not reconcile is not imported at all — a wrong balance shown confidently is
worse than no balance, because there is nothing about it to notice.

The one number that cannot be cross-checked is the payment due date, which
appears only in the summary panel; it is verified for shape but not for value.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date

import pdftext
import store
from store import new_uid, now_iso

SERVICER = "aidvantage"
INSTITUTION = "Aidvantage"

# How far a value may sit from a column's centre before the column assignment is
# considered unsafe. Loan columns are ~47pt apart, so half a pitch is the point
# past which "nearest column" stops being an obvious answer.
_COL_TOLERANCE = 22.0

# Footnote markers and section numbering sit in the label gutter alongside the
# label itself; they carry no meaning here and are trimmed off both ends.
_LABEL_JUNK = " \t‡†*¹²³§#0123456789."

MONEY, DATE, TEXT, RATE = "money", "date", "text", "rate"

# label -> (field, type). Matched on the WHOLE normalised label, never a prefix:
# "Past Due Amount" and "Pay Past Due Amount by 08/29/26" are different rows, and
# a prefix match would silently read the late-fee date into the balance.
_ROWS = {
    "info": {
        "Current Balance": ("current_balance", MONEY),
        "Unpaid Interest": ("unpaid_interest", MONEY),
        "Unpaid Principal": ("unpaid_principal", MONEY),
        "Original Principal": ("original_principal", MONEY),
        "Capitalized Interest": ("capitalized_interest", MONEY),
        "Principal Reduction": ("principal_reduction", MONEY),
        "Life of Loan Payments": ("life_payments", MONEY),
        "Total Principal Paid": ("principal_paid", MONEY),
        "Total Interest Paid": ("interest_paid", MONEY),
    },
    "period": {
        "Payments Received": ("payments_received", MONEY),
        "Last Payment Effective Date": ("last_payment_on", DATE),
        "Applied to Interest": ("applied_interest", MONEY),
        "Applied to Principal": ("applied_principal", MONEY),
        "Returned Check Fee": ("returned_check_fee", MONEY),
    },
    "details": {
        "Loan Date": ("opened_on", DATE),
        "Loan Program": ("program", TEXT),
        "Interest Rate": ("rate", RATE),
        "(F-Fixed, V-Variable)": ("rate_type", TEXT),
        "Total Payment Due": ("total_due", MONEY),
        "Past Due Amount": ("past_due", MONEY),
        "Current Amount Due": ("current_due", MONEY),
    },
}

_HEADERS = (
    (re.compile(r"^Loan Information as of\s+(\d{1,2}/\d{1,2}/\d{2,4})"), "info"),
    (re.compile(r"^Billing Period Summary\s+(\d{1,2}/\d{1,2}/\d{2,4})"
                r"\s+to\s+(\d{1,2}/\d{1,2}/\d{2,4})"), "period"),
    (re.compile(r"^Loan Details\s*$"), "details"),
)

_LOAN_REF = re.compile(r"^\d{1,3}-\d{1,3}$")
_MONEY = re.compile(r"^\(?-?\$?\s*[\d,]+\.\d{2}\)?$")
_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")

# Money that is genuinely a total should never be this large; a value past it
# means columns were misread rather than that someone owes it.
_SANITY_MAX = 100_000_000.0


class StatementError(Exception):
    """The PDF is not a statement this parser can read, or it does not add up."""


# ------------------------------------------------------------------- values

def _money(raw: str) -> float:
    s = raw.strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").strip()
    try:
        v = float(s)
    except ValueError:
        raise StatementError(f"expected an amount, got {raw!r}")
    if abs(v) > _SANITY_MAX:
        raise StatementError(f"implausible amount {raw!r}")
    return -v if neg else v


def _date(raw: str) -> str:
    m = _DATE.match(raw.strip())
    if not m:
        raise StatementError(f"expected a date, got {raw!r}")
    mo, dy, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # Two-digit years are unambiguous here: federal direct loans postdate 2000
    # and no statement predates the servicer, so every year is 20xx.
    if yr < 100:
        yr += 2000
    try:
        return date(yr, mo, dy).isoformat()
    except ValueError:
        raise StatementError(f"impossible date {raw!r}")


def _rate(raw: str) -> float:
    try:
        return float(raw.strip().rstrip("%"))
    except ValueError:
        raise StatementError(f"expected an interest rate, got {raw!r}")


_CAST = {MONEY: _money, DATE: _date, RATE: _rate, TEXT: lambda s: s.strip()}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().strip(_LABEL_JUNK).strip()


# ------------------------------------------------------------------ the grid

def _sections(page: list[dict]) -> list[dict]:
    """Loan-table sections on one page, each with its column layout.

    A section is only real if a row of loan IDs follows its heading. The
    disclosures page carries phrases like "Loan Details" in running prose, and
    requiring the ID row is what tells the table apart from the sentence.
    """
    rs = pdftext.rows(page)
    marks: list[tuple[int, str, tuple]] = []
    for i, r in enumerate(rs):
        txt = pdftext.text_of(r)
        for pat, kind in _HEADERS:
            m = pat.match(txt)
            if m:
                marks.append((i, kind, m.groups()))
                break

    out: list[dict] = []
    for n, (start, kind, groups) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(rs)
        body = rs[start + 1:end]

        head_at, cols = None, []
        for j, r in enumerate(body):
            found = [(it["x"], it["text"].strip()) for it in r["items"]
                     if _LOAN_REF.match(it["text"].strip())
                     or it["text"].strip() == "Total"]
            if len(found) >= 2:
                head_at, cols = j, sorted(found)
                break
        if head_at is None:
            continue                    # prose, not a table

        out.append({"kind": kind, "groups": groups, "cols": cols,
                    "rows": body[head_at + 1:]})
    return out


def _read_section(sec: dict) -> tuple[dict, dict]:
    """One section as ({loan_ref: {field: value}}, {field: total})."""
    vocab = _ROWS[sec["kind"]]
    cols = sec["cols"]
    # The label gutter ends where the first data column begins. Derived from the
    # header rather than hardcoded, so a statement laid out at a different width
    # still splits in the right place.
    gutter = cols[0][0] - _COL_TOLERANCE

    per: dict[str, dict] = {}
    totals: dict[str, object] = {}
    for r in sec["rows"]:
        label = _norm(" ".join(i["text"] for i in r["items"] if i["x"] < gutter))
        target = vocab.get(label)
        if not target:
            continue                    # footnotes, disclosures, unused rows
        field, kind = target
        cast = _CAST[kind]
        for it in r["items"]:
            if it["x"] < gutter:
                continue
            val = it["text"].strip()
            if not val:
                continue
            cx, name = min(cols, key=lambda c: abs(c[0] - it["x"]))
            if abs(cx - it["x"]) > _COL_TOLERANCE:
                raise StatementError(
                    f"{label!r}: value {val!r} does not line up with any loan "
                    f"column — the table layout is not one this parser knows")
            if name == "Total":
                totals[field] = cast(val)
            else:
                per.setdefault(name, {})[field] = cast(val)
    return per, totals


def _summary(page: list[dict]) -> dict:
    """The Account Summary panel, plus the auto-pay sentence, from page one.

    The panel is located by its own heading instead of by a fixed coordinate,
    because the left half of that page is the detachable payment stub and its
    rows interleave with the panel's at the same heights. Anchoring on the
    heading is what keeps "Total Payment Due" from the stub out of the summary.
    """
    heading = next((i for i in page if i["text"].strip() == "Account Summary"), None)
    if heading is None:
        raise StatementError("no Account Summary panel — is this an Aidvantage "
                             "statement?")
    left = heading["x"] - 10
    value_at = heading["x"] + 150

    panel = [i for i in page if i["x"] >= left and i["y"] < heading["y"] + 5]
    out: dict[str, object] = {}
    for r in pdftext.rows(panel):
        label = _norm(" ".join(i["text"] for i in r["items"] if i["x"] < value_at))
        vals = [i["text"].strip() for i in r["items"] if i["x"] >= value_at]
        if not vals:
            continue
        raw = vals[-1]
        if label == "Account Number":
            out["account_number"] = raw
        elif label == "Statement Date" and _DATE.match(raw):
            out["statement_date"] = _date(raw)
        elif label == "Current Amount Due Date" and _DATE.match(raw):
            out["due_date"] = _date(raw)
        elif label == "Unpaid Principal" and _MONEY.match(raw):
            out["unpaid_principal"] = _money(raw)
        elif label == "Payments Since Last Bill" and _MONEY.match(raw):
            out["paid_since_last"] = _money(raw)
        elif label == "Past Due Amount (Pay Now)" and _MONEY.match(raw):
            out["past_due"] = _money(raw)
        elif label == "Current Amount Due" and _MONEY.match(raw):
            out["current_due"] = _money(raw)
        elif label == "Billing Group":
            out["billing_group"] = raw

    prose = pdftext.page_text(page)
    m = re.search(r"\$\s*([\d,]+\.\d{2})\s+will be\s+debited.*?by\s+"
                  r"(\d{1,2}/\d{1,2}/\d{2,4})", prose, re.S)
    if m:
        out["autopay"] = True
        out["autopay_amount"] = _money(m.group(1))
        out["autopay_date"] = _date(m.group(2))
    else:
        out["autopay"] = bool(re.search(r"ENROLLED IN AUTO ?PAY", prose, re.I))
    return out


# ------------------------------------------------------------------- reconcile

def _close(a: float, b: float) -> bool:
    """Equal to the cent. Statements are exact; anything else is a misread."""
    return abs(round(a, 2) - round(b, 2)) <= 0.01


def _reconcile(loans: list[dict], totals: dict, summary: dict) -> list[str]:
    """Every cross-check the statement makes possible. Returns failures."""
    bad: list[str] = []

    def cmp(what: str, got: float, want: float) -> None:
        if not _close(got, want):
            bad.append(f"{what}: loans sum to {got:,.2f} but the statement "
                       f"says {want:,.2f}")

    for field, name in (("current_balance", "current balance"),
                        ("unpaid_principal", "unpaid principal"),
                        ("unpaid_interest", "unpaid interest"),
                        ("original_principal", "original principal"),
                        ("payments_received", "payments received"),
                        ("current_due", "current amount due"),
                        ("past_due", "past due"),
                        ("total_due", "total payment due")):
        if field in totals:
            cmp(name, sum(l.get(field) or 0 for l in loans), float(totals[field]))

    # The summary panel is written by a different part of the statement than the
    # loan grid, so agreeing with it is a genuinely independent check.
    for field, key, name in (("unpaid_principal", "unpaid_principal", "unpaid principal"),
                             ("current_due", "current_due", "current amount due"),
                             ("past_due", "past_due", "past due"),
                             ("payments_received", "paid_since_last", "payments received")):
        if key in summary:
            cmp(f"{name} (summary panel)",
                sum(l.get(field) or 0 for l in loans), float(summary[key]))

    for l in loans:
        ref = l.get("loan_ref")
        bal, prin, intr = (l.get("current_balance"), l.get("unpaid_principal"),
                           l.get("unpaid_interest"))
        if None not in (bal, prin, intr) and not _close(bal, prin + intr):
            bad.append(f"loan {ref}: balance {bal:,.2f} is not principal "
                       f"{prin:,.2f} plus interest {intr:,.2f}")
    return bad


# ---------------------------------------------------------------------- parse

_PER_LOAN_REQUIRED = ("current_balance", "unpaid_principal", "unpaid_interest")


def parse(data: bytes, source_name: str = "") -> dict:
    """One Aidvantage statement PDF as a dict, or raise `StatementError`."""
    try:
        pages = pdftext.extract(data)
    except pdftext.PdfError as e:
        raise StatementError(str(e))
    if not pages or not any(pages):
        raise StatementError("no text found — this looks like a scanned PDF, "
                             "which cannot be read without OCR")

    summary = _summary(pages[0])

    merged: dict[str, dict] = {}
    totals: dict[str, object] = {}
    order: list[str] = []
    stamps: dict[str, tuple] = {}
    for page in pages:
        for sec in _sections(page):
            per, tot = _read_section(sec)
            stamps.setdefault(sec["kind"], sec["groups"])
            totals.update(tot)
            for ref, fields in per.items():
                if ref not in merged:
                    merged[ref] = {}
                    order.append(ref)
                merged[ref].update(fields)

    if not merged:
        raise StatementError("no loan table found — is this an Aidvantage "
                             "monthly statement?")

    # Sort by the servicer's own loan numbering rather than by page position, so
    # a statement whose table spills across pages still lists 1-9 before 1-10.
    def key(ref: str) -> tuple:
        return tuple(int(p) for p in ref.split("-"))

    order.sort(key=key)
    loans = [{"loan_ref": ref, **merged[ref]} for ref in order]

    for l in loans:
        missing = [f for f in _PER_LOAN_REQUIRED if l.get(f) is None]
        if missing:
            raise StatementError(f"loan {l['loan_ref']} is missing "
                                 f"{', '.join(missing)}")

    statement_date = summary.get("statement_date")
    if not statement_date and "info" in stamps:
        statement_date = _date(stamps["info"][0])
    if not statement_date:
        raise StatementError("could not find the statement date")
    if "info" in stamps and _date(stamps["info"][0]) != statement_date:
        raise StatementError("the summary panel and the loan table disagree "
                             "about the statement date")

    period = stamps.get("period") or ("", "")
    problems = _reconcile(loans, totals, summary)
    if problems:
        raise StatementError("this statement does not add up, so nothing was "
                             "imported:\n  - " + "\n  - ".join(problems))

    def total(field: str) -> float:
        if field in totals:
            return float(totals[field])
        return round(sum(l.get(field) or 0 for l in loans), 2)

    return {
        "servicer": SERVICER,
        "institution": INSTITUTION,
        "account_number": str(summary.get("account_number") or ""),
        "statement_date": statement_date,
        "period_start": _date(period[0]) if period[0] else "",
        "period_end": _date(period[1]) if period[1] else "",
        "due_date": summary.get("due_date") or "",
        "current_balance": total("current_balance"),
        "unpaid_principal": total("unpaid_principal"),
        "unpaid_interest": total("unpaid_interest"),
        "original_principal": total("original_principal"),
        "current_due": total("current_due"),
        "past_due": total("past_due"),
        "total_due": total("total_due"),
        "paid_since_last": total("payments_received"),
        "applied_interest": total("applied_interest"),
        "applied_principal": total("applied_principal"),
        "autopay": bool(summary.get("autopay")),
        "autopay_amount": summary.get("autopay_amount"),
        "autopay_date": summary.get("autopay_date") or "",
        "loans": loans,
        "source_name": source_name,
        "source_sha256": hashlib.sha256(data).hexdigest(),
    }


# --------------------------------------------------------------------- import

def _weighted_rate(loans: list[dict]) -> float | None:
    """Balance-weighted average rate — the only average that means anything."""
    bal = sum(l.get("current_balance") or 0 for l in loans)
    if bal <= 0:
        return None
    rated = [l for l in loans if l.get("rate") is not None]
    if not rated:
        return None
    return round(sum((l["current_balance"] or 0) * l["rate"] for l in rated) / bal, 3)


def _sync_account(st: dict) -> str | None:
    """Mirror the newest statement onto a finance account, for net worth.

    One account for the servicer, not one per loan: seventeen rows of a hundred
    dollars each would bury every other account in the net-worth list while
    saying nothing the loans widget does not say better.

    Only the NEWEST statement writes here. Importing an older PDF afterwards
    still stores its detail and its history point, but must not roll the visible
    balance backwards to a figure that has since been paid down.
    """
    number = st["account_number"]
    # Keyed through item_id, which carries a unique index — so two uploads
    # racing each other cannot create a second copy of the same account.
    item = store.get_finance_item_by_item_id(f"{SERVICER}:{number}")
    if not item:
        item = store.create_finance_item({
            "provider": SERVICER, "item_id": f"{SERVICER}:{number}",
            "institution": INSTITUTION, "status": "manual"})
    acct = store.find_finance_account(item["id"], number)
    fields = {
        "item_id": item["id"], "external_id": number,
        "name": "Student loans", "official_name": "Aidvantage federal loans",
        "institution": INSTITUTION, "kind": "loan", "subtype": "student",
        # "9163028236-1" is an account number and a BILLING GROUP. The mask has
        # to come from the account number alone: slicing the whole string gives
        # "36-1", and stripping the dash first glues the group digit on and
        # gives "2361" — neither is the last four digits of anything real.
        "mask": re.sub(r"\D", "", number.split("-")[0])[-4:],
        "balance": st["current_balance"],
        "apr": _weighted_rate(st["loans"]), "min_payment": st["current_due"],
        "next_due": st["due_date"] or None,
    }
    # No automatic history from either call: import_statement writes the point
    # itself, stamped with the statement's own date rather than with now.
    if not acct:
        acct = store.create_finance_account(fields, history=False)
    elif st["statement_date"] >= (store.latest_loan_statement_date(number) or ""):
        store.update_finance_account(acct["id"], fields, history=False)
    return acct["id"]


def import_statement(data: bytes, source_name: str = "") -> dict:
    """Parse, reconcile and store one statement. Re-importing replaces it.

    Replacement rather than skip-if-present: a corrected statement for a month
    already loaded should win, and since the whole month is one atomic block
    there is no partial state to reconcile against.
    """
    st = parse(data, source_name)
    existing = store.find_loan_statement(st["servicer"], st["account_number"],
                                         st["statement_date"])
    replaced = bool(existing)
    if existing:
        store.delete_loan_statement(existing["id"])

    st["account_id"] = _sync_account(st)
    row = store.create_loan_statement(st)

    # History is stamped with the STATEMENT date, not now: three statements
    # imported this afternoon are three months of balance, not three points
    # today, and the balance chart is unreadable if they pile up.
    if st.get("account_id"):
        store.add_finance_history_at(st["account_id"], st["current_balance"],
                                     st["statement_date"] + "T12:00:00Z")
    return {"statement": row, "loans": store.list_loan_details(row["id"]),
            "replaced": replaced}


def history(account_number: str = "") -> list[dict]:
    return store.list_loan_statements(SERVICER, account_number)
