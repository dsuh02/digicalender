"""
Calendar subscriptions (ICS feeds).

A feed is an `accounts` row with provider='ics'. Its config lives in
token_json: {url, source_url, last_status, last_count}. Feed events are
read-only mirrors, so syncing is replace-on-refresh: hard-delete the account's
events and re-insert what the feed says now. No tombstones needed — we own
nothing upstream.

URL normalisation accepts what people actually paste:
  - a real .ics URL (Outlook "publish calendar" links, Google secret address)
  - webcal://  ->  https://
  - a Google Calendar EMBED page url — not a feed, but every Google calendar
    has a public ICS twin at /calendar/ical/<src>/public/basic.ics, so the src
    parameter is lifted out and converted. That twin only answers if the
    calendar is public; the fetch error explains the fix when it isn't.
"""

from __future__ import annotations

import urllib.parse

import ics
import store


def normalize_url(raw: str) -> tuple[str, str]:
    """Returns (fetch_url, note). note is a human hint when we transformed it."""
    url = (raw or "").strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]

    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return url, ""

    if "calendar.google.com" in parsed.netloc and "/embed" in parsed.path:
        qs = urllib.parse.parse_qs(parsed.query)
        src = (qs.get("src") or [""])[0]
        if src:
            fetch_url = ("https://calendar.google.com/calendar/ical/"
                         f"{urllib.parse.quote(src)}/public/basic.ics")
            return fetch_url, ("converted the Google embed page to its ICS feed — "
                               "this works when the calendar is public")
    return url, ""


def _exclude_terms(cfg: dict) -> list[str]:
    return [t.strip().casefold()
            for t in (cfg.get("exclude") or "").split(",") if t.strip()]


def apply_exclude(events: list[dict], cfg: dict) -> tuple[list[dict], int]:
    """Drop events whose TITLE contains any configured substring
    (case-insensitive). Applied at import, so filtered events never reach the
    database and every count the UI shows stays honest."""
    terms = _exclude_terms(cfg)
    if not terms:
        return events, 0
    kept = [ev for ev in events
            if not any(t in ev["title"].casefold() for t in terms)]
    return kept, len(events) - len(kept)


def sync_account(account: dict) -> dict:
    """Fetch + parse + replace one feed's events. Returns a status dict and
    persists last_status/last_count on the account either way."""
    cfg = dict(account.get("token_json") or {})
    url = cfg.get("url", "")
    if not url:
        return _finish(account, cfg, ok=False, message="no URL configured")

    etag = account.get("sync_token") or None
    text, new_etag, err = ics.fetch(url, etag)
    if err:
        return _finish(account, cfg, ok=False, message=err)
    if text is None:                      # 304 — nothing changed upstream
        return _finish(account, cfg, ok=True,
                       message="not modified", count=cfg.get("last_count"),
                       changed=False)

    try:
        events, calname, warnings = ics.to_events(text)
    except Exception as e:
        return _finish(account, cfg, ok=False, message=f"could not parse the feed: {e}")

    # Re-read the account AFTER the network fetch: a recolour or filter change
    # landing while a slow feed was downloading used to be silently reverted
    # when the import stamped values captured before the fetch began.
    fresh = store.get_account(account["id"]) or account
    cfg = dict(fresh.get("token_json") or cfg)
    color = fresh.get("color") or None

    events, dropped = apply_exclude(events, cfg)
    for ev in events:
        ev["provider"] = "ics"
        ev["account_id"] = account["id"]
        ev["color"] = color

    n = store.replace_feed_events(account["id"], events)
    store.update_account(account["id"], {"sync_token": new_etag,
                                         "last_sync": store.now_iso()})
    msg = f"{n} events"
    if dropped:
        msg += f" · {dropped} filtered"
    if warnings:
        msg += f" · {len(warnings)} items skipped"
    return _finish(account, cfg, ok=True, message=msg, count=n,
                   calname=calname, changed=True)


def _finish(account, cfg, *, ok, message, count=None, calname="", changed=False):
    cfg["last_status"] = ("ok: " if ok else "error: ") + message
    if count is not None:
        cfg["last_count"] = count
    store.update_account(account["id"], {"token_json": cfg})
    return {"id": account["id"], "ok": ok, "message": message,
            "count": count, "calendar_name": calname, "changed": changed}


def sync_all() -> list[dict]:
    results = []
    for acct in store.list_accounts(provider="ics"):
        if not acct.get("enabled", True):
            continue
        try:
            results.append(sync_account(store.get_account(acct["id"])))
        except Exception as e:
            results.append({"id": acct["id"], "ok": False, "message": str(e)})
    return results
