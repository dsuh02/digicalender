"""
SQLite storage for DigiCalender.

Schema is deliberately provider-aware from day one so that Google / Microsoft
sync can be switched on later without a migration: every event carries the
provider it came from, the remote id/etag, and dirty/deleted flags for
two-way sync bookkeeping.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "DIGICALENDER_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "digicalender.db"),
)

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    uid          TEXT PRIMARY KEY,
    provider     TEXT NOT NULL DEFAULT 'local',
    account_id   TEXT,
    calendar_id  TEXT,
    external_id  TEXT,
    etag         TEXT,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    location     TEXT NOT NULL DEFAULT '',
    start_utc    TEXT NOT NULL,
    end_utc      TEXT NOT NULL,
    all_day      INTEGER NOT NULL DEFAULT 0,
    color        TEXT,
    rrule        TEXT,
    updated_at   TEXT NOT NULL,
    deleted      INTEGER NOT NULL DEFAULT 0,
    dirty        INTEGER NOT NULL DEFAULT 0
);

-- Range queries hit this constantly (every view change is a range query).
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_utc);
CREATE INDEX IF NOT EXISTS idx_events_range ON events(deleted, start_utc, end_utc);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_external
    ON events(provider, account_id, external_id)
    WHERE external_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS accounts (
    id           TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    color        TEXT NOT NULL DEFAULT '#7aa2f7',
    enabled      INTEGER NOT NULL DEFAULT 1,
    token_json   TEXT,
    sync_token   TEXT,
    last_sync    TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def conn() -> sqlite3.Connection:
    """One connection per thread — ThreadingHTTPServer hands each request its own."""
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=10000")
        _local.conn = c
    return c


def init_db() -> None:
    c = conn()
    c.executescript(SCHEMA)
    c.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_uid() -> str:
    return uuid.uuid4().hex


EVENT_FIELDS = (
    "uid", "provider", "account_id", "calendar_id", "external_id", "etag",
    "title", "description", "location", "start_utc", "end_utc", "all_day",
    "color", "rrule", "updated_at", "deleted", "dirty",
)


def row_to_event(row: sqlite3.Row) -> dict:
    e = {k: row[k] for k in EVENT_FIELDS}
    e["all_day"] = bool(e["all_day"])
    e["deleted"] = bool(e["deleted"])
    e["dirty"] = bool(e["dirty"])
    return e


def list_events(start_utc: str, end_utc: str) -> list[dict]:
    """Every event that overlaps [start, end). Overlap, not containment —
    a multi-day event must show up in a window that only touches its middle."""
    cur = conn().execute(
        """
        SELECT * FROM events
        WHERE deleted = 0
          AND start_utc < ?
          AND end_utc   > ?
        ORDER BY all_day DESC, start_utc ASC
        """,
        (end_utc, start_utc),
    )
    return [row_to_event(r) for r in cur.fetchall()]


def get_event(uid: str) -> dict | None:
    cur = conn().execute("SELECT * FROM events WHERE uid = ?", (uid,))
    row = cur.fetchone()
    return row_to_event(row) if row else None


def create_event(data: dict) -> dict:
    uid = data.get("uid") or new_uid()
    ts = now_iso()
    c = conn()
    c.execute(
        """
        INSERT INTO events (uid, provider, account_id, calendar_id, external_id,
                            etag, title, description, location, start_utc,
                            end_utc, all_day, color, rrule, updated_at,
                            deleted, dirty)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
        """,
        (
            uid,
            data.get("provider", "local"),
            data.get("account_id"),
            data.get("calendar_id"),
            data.get("external_id"),
            data.get("etag"),
            data["title"],
            data.get("description", "") or "",
            data.get("location", "") or "",
            data["start_utc"],
            data["end_utc"],
            1 if data.get("all_day") else 0,
            data.get("color"),
            data.get("rrule"),
            ts,
            # Locally-created events start dirty so a future sync pushes them up.
            1 if data.get("provider", "local") == "local" else 0,
        ),
    )
    c.commit()
    return get_event(uid)


MUTABLE = ("title", "description", "location", "start_utc", "end_utc",
           "all_day", "color", "rrule", "calendar_id", "account_id", "provider")


def update_event(uid: str, data: dict) -> dict | None:
    existing = get_event(uid)
    if not existing:
        return None
    sets, vals = [], []
    for f in MUTABLE:
        if f in data:
            sets.append(f"{f} = ?")
            vals.append(1 if f == "all_day" and data[f] else
                        0 if f == "all_day" else data[f])
    if not sets:
        return existing
    sets.append("updated_at = ?")
    vals.append(now_iso())
    sets.append("dirty = 1")
    vals.append(uid)
    c = conn()
    c.execute(f"UPDATE events SET {', '.join(sets)} WHERE uid = ?", vals)
    c.commit()
    return get_event(uid)


def delete_event(uid: str) -> bool:
    """Soft delete — a hard delete would resurrect the event on the next pull
    from a remote provider, and loses the tombstone we need to push."""
    c = conn()
    cur = c.execute(
        "UPDATE events SET deleted = 1, dirty = 1, updated_at = ? WHERE uid = ?",
        (now_iso(), uid),
    )
    c.commit()
    return cur.rowcount > 0


def list_accounts() -> list[dict]:
    cur = conn().execute(
        "SELECT id, provider, display_name, color, enabled, last_sync FROM accounts"
    )
    out = []
    for r in cur.fetchall():
        d = dict(r)
        d["enabled"] = bool(d["enabled"])
        out.append(d)
    return out


def get_setting(key: str, default: str | None = None) -> str | None:
    cur = conn().execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    c = conn()
    c.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    c.commit()
