"""
Mail over IMAP.

**Envelopes only — bodies are never fetched or stored.** This panel is not a
mail client; it hangs in a room other people walk through, and everything it
needs (who, what about, when, unread) lives in the envelope. Fetching bodies
would multiply the consequences of any compromise for no benefit here.

`imaplib` is stdlib, so this adds no dependency — which is also why IMAP wins
over the Gmail API for this particular job. Gmail's device-code flow does not
support any Gmail scope, so the API route would need a browser round trip *and*
an OAuth app whose refresh tokens expire every seven days unless it goes
through restricted-scope verification. An app password has none of those
properties. It does require 2-Step Verification on the account and IMAP enabled
in Gmail's settings.

⚠️ Read-only, deliberately: the connection uses `select(readonly=True)`, so
looking at the mailbox here can never mark anything as read in the real one.
"""

from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import re
from datetime import datetime, timezone

import store

TIMEOUT = 25.0
DEFAULT_FETCH = 60          # newest N per account, per sync
SNIPPET_MAX = 240


class MailError(Exception):
    pass


def _decode(raw) -> str:
    """MIME-decode a header into something displayable.

    Subjects arrive RFC 2047-encoded, often in several chunks with different
    charsets, and any of them may be mislabelled — so every part decodes
    defensively rather than trusting the declared charset.
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    out = []
    for part, charset in email.header.decode_header(str(raw)):
        if isinstance(part, bytes):
            try:
                out.append(part.decode(charset or "utf-8", "replace"))
            except (LookupError, TypeError):
                out.append(part.decode("utf-8", "replace"))
        else:
            out.append(part)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _received_at(msg) -> str:
    """The Date header as UTC ISO-8601, falling back to now.

    A missing or unparseable Date is common in spam and in some automated
    senders; sorting must not blow up over it.
    """
    try:
        dt = email.utils.parsedate_to_datetime(msg.get("Date"))
        if dt is None:
            raise ValueError
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(account: dict) -> imaplib.IMAP4_SSL:
    host = account.get("host") or "imap.gmail.com"
    port = int(account.get("port") or 993)
    user = (account.get("username") or "").strip()
    secret = (account.get("secret") or "").strip()
    if not user or not secret:
        raise MailError("username and app password are required")
    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=TIMEOUT)
    except Exception as e:
        raise MailError(f"could not reach {host}:{port} — {e}")
    try:
        # Gmail rejects app passwords containing the spaces it displays them
        # with; stripping them is the difference between "works" and an
        # AUTHENTICATIONFAILED that looks like a wrong password.
        conn.login(user, secret.replace(" ", ""))
    except imaplib.IMAP4.error as e:
        detail = str(e)
        if "AUTHENTICATIONFAILED" in detail.upper():
            detail = ("rejected the login — for Gmail this must be an App Password "
                      "(2-Step Verification on), not the account password")
        raise MailError(detail)
    return conn


def check(account: dict) -> dict:
    """Prove the credentials and folder work, without storing anything."""
    conn = _connect(account)
    try:
        folder = account.get("folder") or "INBOX"
        typ, data = conn.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            raise MailError(f"cannot open folder {folder!r}")
        total = int(data[0] or 0)
        typ, unseen = conn.search(None, "UNSEEN")
        n_unseen = len(unseen[0].split()) if typ == "OK" and unseen and unseen[0] else 0
        return {"ok": True, "folder": folder, "total": total, "unread": n_unseen}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def fetch(account: dict, limit: int = DEFAULT_FETCH) -> dict:
    """Newest `limit` envelopes from one account into the local index."""
    conn = _connect(account)
    stored = 0
    unread = 0
    try:
        folder = account.get("folder") or "INBOX"
        typ, _ = conn.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            raise MailError(f"cannot open folder {folder!r}")

        typ, data = conn.search(None, "ALL")
        if typ != "OK":
            raise MailError("search failed")
        uids = (data[0] or b"").split()
        if not uids:
            return {"ok": True, "stored": 0, "unread": 0}
        uids = uids[-int(limit):]

        me = (account.get("username") or "").lower()
        # Headers only. BODY.PEEK never sets \\Seen — BODY would.
        spec = "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID TO CC)])"
        for uid in reversed(uids):
            typ, parts = conn.fetch(uid, spec)
            if typ != "OK" or not parts:
                continue
            raw, flags = b"", ""
            for part in parts:
                if isinstance(part, tuple):
                    raw = part[1]
                    flags = part[0].decode("utf-8", "replace")
                elif isinstance(part, bytes):
                    flags += part.decode("utf-8", "replace")
            if not raw:
                continue
            msg = email.message_from_bytes(raw)

            name, addr = email.utils.parseaddr(_decode(msg.get("From")))
            recipients = f"{_decode(msg.get('To'))} {_decode(msg.get('Cc'))}".lower()
            is_unread = "\\Seen" not in flags
            subject = _decode(msg.get("Subject")) or "(no subject)"

            store.upsert_mail_message({
                "account_id": account["id"],
                "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                "message_id": _decode(msg.get("Message-ID")),
                "from_name": name or addr,
                "from_addr": addr,
                "subject": subject,
                # No body is fetched, so the "snippet" is the subject. Kept as
                # its own column so a future source can fill it without a
                # migration or a change of meaning.
                "snippet": subject[:SNIPPET_MAX],
                "received_at": _received_at(msg),
                "unread": is_unread,
                "flagged": "\\Flagged" in flags,
                # Addressed to you directly, rather than a list you are on —
                # the cheapest useful signal for "this probably wants a reply".
                "to_me": bool(me and me in recipients),
            })
            stored += 1
            if is_unread:
                unread += 1
        return {"ok": True, "stored": stored, "unread": unread}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def sync_all(limit: int = DEFAULT_FETCH) -> dict:
    """Every enabled account. One failure does not stop the others."""
    results = []
    for acct in store.list_mail_accounts():
        if not acct.get("enabled"):
            continue
        try:
            res = fetch(acct, limit)
            store.mark_mail_synced(acct["id"], f"ok: {res['stored']} messages")
            results.append({"account": acct["name"], **res})
        except MailError as e:
            store.mark_mail_synced(acct["id"], f"error: {e}")
            results.append({"account": acct["name"], "ok": False, "error": str(e)})
        except Exception as e:                      # never let one account abort a sync
            store.mark_mail_synced(acct["id"], f"error: {e}")
            results.append({"account": acct["name"], "ok": False, "error": str(e)})
    store.prune_mail_messages(30)
    return {"results": results}


def summary() -> dict:
    """The shape downstream work consumes. Counts and headlines, never bodies."""
    msgs = store.list_mail_messages(limit=200)
    unread = [m for m in msgs if m["unread"]]
    needs_reply = [m for m in unread if m["to_me"]]
    by_account: dict[str, int] = {}
    for m in unread:
        by_account[m["account_name"]] = by_account.get(m["account_name"], 0) + 1
    return {
        "total_indexed": len(msgs),
        "unread": len(unread),
        "unread_to_me": len(needs_reply),
        "by_account": by_account,
        "headlines": [{
            "from": m["from_name"], "subject": m["subject"],
            "account": m["account_name"], "received_at": m["received_at"],
            "to_me": m["to_me"],
        } for m in unread[:12]],
    }
