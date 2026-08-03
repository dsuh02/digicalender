"""
PostgreSQL storage for DigiCalender (psycopg3).

Two halves:
  - the calendar (events, accounts), provider-aware so Google/Microsoft sync
    can be added without a migration;
  - the dashboard (pages, widgets, todos, notifications, devices, scenes),
    which turns this from a calendar into a wall hub.

Connection: peer auth over the unix socket. The app runs as the same OS user
it connects as, so there is no password on disk or in the repo. Override with
DIGICALENDER_DSN if you ever move the database off-box.

Two deliberate choices worth knowing before you edit this file:

**Timestamps are TEXT, not TIMESTAMPTZ.** Everything is ISO-8601 UTC
("2026-07-30T14:00:00Z"), which sorts lexicographically, so range scans and
indexes behave exactly as they would on a real timestamp type. The reason is
all-day events: they are *floating dates* (July 4th is July 4th in any
timezone), and handing them to a type that applies timezone conversion is
precisely the bug that made them render across two days. TEXT keeps the wire
format, the storage format, and the comparison semantics identical.

**Connections are scoped to database work only.** Never hold one across a
device call — Roku/Govee/Samsung requests are network I/O with real latency,
and parking a connection behind them is how you exhaust a pool.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DSN = os.environ.get("DIGICALENDER_DSN", "dbname=digicalender")

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

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
    all_day      BOOLEAN NOT NULL DEFAULT FALSE,
    color        TEXT,
    rrule        TEXT,
    updated_at   TEXT NOT NULL,
    deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    dirty        BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_events_range ON events(deleted, start_utc, end_utc);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_external
    ON events(provider, account_id, external_id)
    WHERE external_id IS NOT NULL;

-- Calendar sources. provider='ics' rows are feed subscriptions whose config
-- (url, last status) lives in token_json. `enabled` gates syncing; `visible`
-- gates rendering — hiding a calendar must not stop it staying current.
CREATE TABLE IF NOT EXISTS accounts (
    id           TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    color        TEXT NOT NULL DEFAULT '',
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    visible      BOOLEAN NOT NULL DEFAULT TRUE,
    token_json   JSONB,
    sync_token   TEXT,
    last_sync    TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Household members. Declared before `pages`, which references it.
-- `macs` drive optional presence detection on the LAN; `theme` overrides the
-- global scheme while that person is the active profile.
CREATE TABLE IF NOT EXISTS people (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    greeting   TEXT NOT NULL DEFAULT '',
    color      TEXT NOT NULL DEFAULT '',
    avatar     TEXT NOT NULL DEFAULT '',
    macs       JSONB NOT NULL DEFAULT '[]'::jsonb,
    theme      JSONB,
    settings   JSONB NOT NULL DEFAULT '{}'::jsonb,
    position   INTEGER NOT NULL DEFAULT 0,
    last_seen  TEXT,
    created_at TEXT NOT NULL
);

-- A page is one screen of the dashboard; swipe left/right moves between them.
-- person_id NULL = shared with the whole household. ON DELETE SET NULL, never
-- CASCADE: removing a person must not destroy the pages they built.
CREATE TABLE IF NOT EXISTS pages (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    cols       INTEGER NOT NULL DEFAULT 48,
    rows       INTEGER NOT NULL DEFAULT 32,
    person_id  TEXT REFERENCES people(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

-- x/y/w/h are in grid CELLS, not pixels. The grid is the coordinate system.
CREATE TABLE IF NOT EXISTS widgets (
    id         TEXT PRIMARY KEY,
    page_id    TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    type       TEXT NOT NULL,
    x          INTEGER NOT NULL,
    y          INTEGER NOT NULL,
    w          INTEGER NOT NULL,
    h          INTEGER NOT NULL,
    z          INTEGER NOT NULL DEFAULT 0,
    settings   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_widgets_page ON widgets(page_id);

CREATE TABLE IF NOT EXISTS todos (
    id         TEXT PRIMARY KEY,
    list_name  TEXT NOT NULL DEFAULT 'Home',
    title      TEXT NOT NULL,
    notes      TEXT NOT NULL DEFAULT '',
    done       BOOLEAN NOT NULL DEFAULT FALSE,
    priority   INTEGER NOT NULL DEFAULT 0,
    due_utc    TEXT,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_todos_list ON todos(list_name, done, position);

CREATE TABLE IF NOT EXISTS notifications (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'info',   -- info | warn | error | reminder
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    dedupe_key TEXT UNIQUE,
    read       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_created ON notifications(read, created_at DESC);

-- config holds adapter-specific fields (ip, mac, model, token, api key...).
CREATE TABLE IF NOT EXISTS devices (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,   -- adapter: roku | govee_lan | govee_cloud | samsung_tv ...
    room       TEXT NOT NULL DEFAULT '',
    config     JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    last_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_seen  TEXT,
    created_at TEXT NOT NULL
);

-- One row per linked institution (a Plaid Item). access_token is a bearer
-- credential: this database is peer-auth and local-only, and it never leaves
-- the box except in calls to Plaid.
CREATE TABLE IF NOT EXISTS finance_items (
    id           TEXT PRIMARY KEY,
    provider     TEXT NOT NULL DEFAULT 'plaid',
    item_id      TEXT,
    access_token TEXT,
    institution  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT '',
    last_sync    TEXT,
    tx_cursor    TEXT,
    created_at   TEXT NOT NULL
);
-- One row per Plaid item, enforced by the database. The link flow is polled,
-- and a slow exchange used to let concurrent polls insert the same item several
-- times over — which silently multiplied every balance and every net worth.
CREATE UNIQUE INDEX IF NOT EXISTS idx_fitem_itemid
    ON finance_items(item_id) WHERE item_id IS NOT NULL;

-- Balances are DOUBLE PRECISION, not NUMERIC: this is a display surface, and
-- floats keep the JSON boundary free of Decimal special-casing. Do not do
-- arithmetic here you would want to reconcile against a statement.
CREATE TABLE IF NOT EXISTS finance_accounts (
    id            TEXT PRIMARY KEY,
    item_id       TEXT REFERENCES finance_items(id) ON DELETE CASCADE,
    external_id   TEXT,
    name          TEXT NOT NULL,
    official_name TEXT NOT NULL DEFAULT '',
    institution   TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'other',
    subtype       TEXT NOT NULL DEFAULT '',
    mask          TEXT NOT NULL DEFAULT '',
    balance       DOUBLE PRECISION NOT NULL DEFAULT 0,
    available     DOUBLE PRECISION,
    credit_limit  DOUBLE PRECISION,
    apr           DOUBLE PRECISION,
    min_payment   DOUBLE PRECISION,
    due_day       INTEGER,
    next_due      TEXT,
    color         TEXT NOT NULL DEFAULT '',
    hidden        BOOLEAN NOT NULL DEFAULT FALSE,
    position      INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_facct_external
    ON finance_accounts(item_id, external_id) WHERE external_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS finance_history (
    id         TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES finance_accounts(id) ON DELETE CASCADE,
    balance    DOUBLE PRECISION NOT NULL,
    at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fhist ON finance_history(account_id, at);

-- Transactions, for the spending and cash-flow views.
--
-- `posted_on` is a DATE STRING (YYYY-MM-DD), not a timestamp — for the same
-- reason all-day events are: a purchase happened on a calendar day, and running
-- it through a timezone is how a Sunday coffee lands in Saturday's total.
--
-- **Plaid's sign convention is inverted from intuition**: a POSITIVE amount is
-- money leaving the account. Stored exactly as Plaid sends it so the raw data
-- stays faithful; every read site flips it, via finance.spending().
CREATE TABLE IF NOT EXISTS finance_transactions (
    id          TEXT PRIMARY KEY,
    item_id     TEXT REFERENCES finance_items(id) ON DELETE CASCADE,
    account_id  TEXT REFERENCES finance_accounts(id) ON DELETE CASCADE,
    external_id TEXT,
    name        TEXT NOT NULL DEFAULT '',
    merchant    TEXT NOT NULL DEFAULT '',
    amount      DOUBLE PRECISION NOT NULL DEFAULT 0,
    category    TEXT NOT NULL DEFAULT '',
    pending     BOOLEAN NOT NULL DEFAULT FALSE,
    posted_on   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ftx_external
    ON finance_transactions(item_id, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ftx_date ON finance_transactions(posted_on DESC);

-- Gallery sets: one row per set, one folder per set under galleries/ on disk.
-- The DB stores names + order; the FILES are the user's and outlive both the
-- widgets showing them and (deliberately) the set row itself.
CREATE TABLE IF NOT EXISTS galleries (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    dirname    TEXT NOT NULL UNIQUE,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gallery_images (
    id         TEXT PRIMARY KEY,
    gallery_id TEXT NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
    filename   TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (gallery_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_gimages_gallery ON gallery_images(gallery_id, position);

-- actions = [{device_id, command, params}], run in order on one tap.
CREATE TABLE IF NOT EXISTS scenes (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    icon       TEXT NOT NULL DEFAULT 'sparkles',
    color      TEXT NOT NULL DEFAULT '',
    actions    JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TEXT NOT NULL
);
"""


def conn() -> psycopg.Connection:
    """One connection per thread. ThreadingHTTPServer gives each request its own
    thread, and a psycopg connection is not safe to share across threads."""
    c = getattr(_local, "conn", None)
    if c is None or c.closed:
        c = psycopg.connect(DSN, row_factory=dict_row, autocommit=True)
        _local.conn = c
    return c


def q(sql: str, params: tuple | list = (), *, fetch: str | None = None):
    """Run a statement, retrying once on a dropped connection.

    Postgres restarts (upgrades, a reboot mid-session) otherwise leave the
    thread-local connection stale and the panel dead until the service is
    bounced. One transparent reconnect keeps the wall display up.
    """
    for attempt in (1, 2):
        try:
            with conn().cursor() as cur:
                cur.execute(sql, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return cur.rowcount
        except (psycopg.OperationalError, psycopg.InterfaceError):
            try:
                if getattr(_local, "conn", None):
                    _local.conn.close()
            except Exception:
                pass
            _local.conn = None
            if attempt == 2:
                raise
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_uid() -> str:
    return uuid.uuid4().hex


SCHEMA_VERSION = 7


def init_db() -> None:
    with conn().cursor() as cur:
        cur.execute(SCHEMA)
    row = q("SELECT version FROM schema_version LIMIT 1", fetch="one")
    if row is None:
        q("INSERT INTO schema_version (version) VALUES (%s)", (SCHEMA_VERSION,))
        seed_default_dashboard()
        return
    if row["version"] < 3:
        # v3: per-source visibility for calendar feeds.
        q("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS visible BOOLEAN NOT NULL DEFAULT TRUE")
        q("UPDATE schema_version SET version = 3")
    if row["version"] < 4:
        # v4: gallery tables — created by SCHEMA above; only the version moves.
        q("UPDATE schema_version SET version = 4")
    if row["version"] < 5:
        # v5: household members, and page ownership. ON DELETE SET NULL, not
        # CASCADE: removing a person must never silently destroy the pages
        # they built — those become shared instead.
        q("""ALTER TABLE pages ADD COLUMN IF NOT EXISTS person_id TEXT
             REFERENCES people(id) ON DELETE SET NULL""")
        q("UPDATE schema_version SET version = 5")
    if row["version"] < 6:
        # v6: finance tables — created by SCHEMA above; only the version moves.
        q("UPDATE schema_version SET version = 6")
    if row["version"] < 7:
        # v7: one row per Plaid item. Concurrent polls of the link flow could
        # exchange the same public_token repeatedly, and every exchange inserted
        # another copy of the same institution — 10 rows for 2 real banks, with
        # every balance counted once per copy.
        #
        # Collapse to the earliest row per item_id first; the unique index below
        # cannot be created while duplicates exist. Accounts, history and bill
        # events cascade from the rows that go.
        q("""DELETE FROM finance_items a USING finance_items b
              WHERE a.item_id IS NOT NULL AND a.item_id = b.item_id
                AND (a.created_at > b.created_at
                     OR (a.created_at = b.created_at AND a.id > b.id))""")
        q("""CREATE UNIQUE INDEX IF NOT EXISTS idx_fitem_itemid
                ON finance_items(item_id) WHERE item_id IS NOT NULL""")
        # v7 also adds finance_transactions (created by SCHEMA above) and the
        # per-item transaction cursor. ADD COLUMN is needed for the cursor
        # because finance_items already exists on any v6 database.
        q("ALTER TABLE finance_items ADD COLUMN IF NOT EXISTS tx_cursor TEXT")
        q("UPDATE schema_version SET version = 7")


# --------------------------------------------------------------------- events

def list_events(start_utc: str, end_utc: str) -> list[dict]:
    """Every event overlapping [start, end) — overlap, not containment, so a
    multi-day event shows in a window that only touches its middle.

    Filtered by source visibility here rather than in each caller, so hiding a
    calendar hides it everywhere at once: month, week, day, agenda, reminders.
    Local events have no account row and are always visible.
    """
    return q("""SELECT e.* FROM events e
                LEFT JOIN accounts a ON e.account_id = a.id
                WHERE e.deleted = FALSE
                  AND (e.account_id IS NULL OR a.id IS NULL OR a.visible)
                  AND e.start_utc < %s AND e.end_utc > %s
                ORDER BY e.all_day DESC, e.start_utc ASC""",
             (end_utc, start_utc), fetch="all")


def get_event(uid: str) -> dict | None:
    return q("SELECT * FROM events WHERE uid = %s", (uid,), fetch="one")


def create_event(data: dict) -> dict:
    uid = data.get("uid") or new_uid()
    q("""INSERT INTO events (uid, provider, account_id, calendar_id, external_id,
             etag, title, description, location, start_utc, end_utc, all_day,
             color, rrule, updated_at, deleted, dirty)
         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s)""",
      (uid, data.get("provider", "local"), data.get("account_id"),
       data.get("calendar_id"), data.get("external_id"), data.get("etag"),
       data["title"], data.get("description", "") or "",
       data.get("location", "") or "", data["start_utc"], data["end_utc"],
       bool(data.get("all_day")), data.get("color"), data.get("rrule"),
       now_iso(), data.get("provider", "local") == "local"))
    return get_event(uid)


MUTABLE_EVENT = ("title", "description", "location", "start_utc", "end_utc",
                 "all_day", "color", "rrule", "calendar_id", "account_id", "provider")


def update_event(uid: str, data: dict) -> dict | None:
    if not get_event(uid):
        return None
    sets, vals = [], []
    for f in MUTABLE_EVENT:
        if f in data:
            sets.append(f"{f} = %s")
            vals.append(bool(data[f]) if f == "all_day" else data[f])
    if not sets:
        return get_event(uid)
    sets += ["updated_at = %s", "dirty = TRUE"]
    vals += [now_iso(), uid]
    q(f"UPDATE events SET {', '.join(sets)} WHERE uid = %s", vals)
    return get_event(uid)


def delete_event(uid: str) -> bool:
    """Soft delete — a hard delete loses the tombstone we need to push, and the
    event returns on the next pull from a remote."""
    return q("UPDATE events SET deleted = TRUE, dirty = TRUE, updated_at = %s WHERE uid = %s",
             (now_iso(), uid)) > 0


def list_accounts(provider: str | None = None) -> list[dict]:
    if provider:
        return q("SELECT * FROM accounts WHERE provider = %s ORDER BY display_name",
                 (provider,), fetch="all")
    return q("SELECT * FROM accounts ORDER BY provider, display_name", fetch="all")


def get_account(aid: str) -> dict | None:
    return q("SELECT * FROM accounts WHERE id = %s", (aid,), fetch="one")


def create_account(data: dict) -> dict:
    aid = data.get("id") or new_uid()
    q("""INSERT INTO accounts (id, provider, display_name, color, enabled,
             visible, token_json, sync_token, last_sync)
         VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,NULL)""",
      (aid, data["provider"], data.get("display_name", "") or "",
       data.get("color", "") or "", data.get("enabled") is not False,
       data.get("visible") is not False, Jsonb(data.get("token_json") or {})))
    return get_account(aid)


def update_account(aid: str, data: dict) -> dict | None:
    if not get_account(aid):
        return None
    sets, vals = [], []
    for f in ("display_name", "color", "sync_token", "last_sync"):
        if f in data:
            sets.append(f"{f} = %s")
            vals.append(data[f])
    for f in ("enabled", "visible"):
        if f in data:
            sets.append(f"{f} = %s")
            vals.append(bool(data[f]))
    if "token_json" in data:
        sets.append("token_json = %s")
        vals.append(Jsonb(data["token_json"]))
    if not sets:
        return get_account(aid)
    vals.append(aid)
    q(f"UPDATE accounts SET {', '.join(sets)} WHERE id = %s", vals)
    # A source's colour tints all of its mirrored events.
    if "color" in data:
        q("UPDATE events SET color = %s WHERE account_id = %s", (data["color"] or None, aid))
    return get_account(aid)


def delete_account(aid: str) -> bool:
    """Feed events are read-only mirrors — remove them with their source."""
    q("DELETE FROM events WHERE account_id = %s", (aid,))
    return q("DELETE FROM accounts WHERE id = %s", (aid,)) > 0


def replace_feed_events(account_id: str, events: list[dict]) -> int:
    """One transaction: drop the account's mirror and re-insert the feed's
    current truth. Hard delete on purpose — tombstones protect local edits
    being pushed upstream, and a subscription pushes nothing."""
    c = conn()
    n = 0
    with c.transaction(), c.cursor() as cur:
        cur.execute("DELETE FROM events WHERE account_id = %s", (account_id,))
        for ev in events:
            cur.execute(
                """INSERT INTO events (uid, provider, account_id, calendar_id,
                       external_id, etag, title, description, location,
                       start_utc, end_utc, all_day, color, rrule, updated_at,
                       deleted, dirty)
                   VALUES (%s,'ics',%s,NULL,%s,NULL,%s,%s,%s,%s,%s,%s,%s,NULL,%s,
                           FALSE,FALSE)
                   ON CONFLICT DO NOTHING""",
                (new_uid(), account_id, ev["external_id"], ev["title"],
                 ev.get("description", ""), ev.get("location", ""),
                 ev["start_utc"], ev["end_utc"], bool(ev.get("all_day")),
                 ev.get("color"), now_iso()))
            n += cur.rowcount
    return n


# ---------------------------------------------------------------- dashboard

def seed_default_dashboard() -> None:
    """First boot gets a usable screen rather than an empty grid."""
    if q("SELECT 1 FROM pages LIMIT 1", fetch="one"):
        return
    home = create_page("Home", position=0)
    hub = create_page("Hub", position=1)
    # 48x32 grid: calendar dominant on the left, glanceable column on the right.
    for w in (
        dict(type="clock",         x=32, y=0,  w=16, h=6),
        dict(type="month",         x=0,  y=0,  w=32, h=20),
        dict(type="agenda",        x=32, y=6,  w=16, h=14),
        dict(type="todo",          x=0,  y=20, w=16, h=12),
        dict(type="weather",       x=16, y=20, w=16, h=12),
        dict(type="notifications", x=32, y=20, w=16, h=12),
    ):
        create_widget(home, w.pop("type"), **w)
    for w in (
        dict(type="label",       x=0,  y=0,  w=48, h=3, settings={"text": "Home control"}),
        dict(type="device_grid", x=0,  y=3,  w=28, h=14),
        dict(type="scenes",      x=28, y=3,  w=20, h=14),
        dict(type="roku_remote", x=0,  y=17, w=16, h=15),
        dict(type="media",       x=16, y=17, w=32, h=15),
    ):
        create_widget(hub, w.pop("type"), **w)


def list_pages() -> list[dict]:
    return q("SELECT * FROM pages ORDER BY position, created_at", fetch="all")


def get_page(pid: str) -> dict | None:
    return q("SELECT * FROM pages WHERE id = %s", (pid,), fetch="one")


def create_page(name: str, position: int = 0, cols: int = 48, rows: int = 32,
                person_id: str | None = None) -> str:
    pid = new_uid()
    q("""INSERT INTO pages (id, name, position, cols, rows, person_id, created_at)
         VALUES (%s,%s,%s,%s,%s,%s,%s)""",
      (pid, name, position, cols, rows, person_id, now_iso()))
    return pid


def update_page(pid: str, data: dict) -> dict | None:
    if not get_page(pid):
        return None
    sets, vals = [], []
    for f in ("name", "position", "cols", "rows", "person_id"):
        if f in data:
            sets.append(f"{f} = %s")
            vals.append(data[f])
    if not sets:
        return get_page(pid)
    vals.append(pid)
    q(f"UPDATE pages SET {', '.join(sets)} WHERE id = %s", vals)
    return get_page(pid)


def delete_page(pid: str) -> bool:
    return q("DELETE FROM pages WHERE id = %s", (pid,)) > 0


def list_widgets(page_id: str | None = None) -> list[dict]:
    if page_id:
        return q("SELECT * FROM widgets WHERE page_id = %s ORDER BY z, created_at",
                 (page_id,), fetch="all")
    return q("SELECT * FROM widgets ORDER BY page_id, z, created_at", fetch="all")


def get_widget(wid: str) -> dict | None:
    return q("SELECT * FROM widgets WHERE id = %s", (wid,), fetch="one")


def create_widget(page_id: str, wtype: str, x: int, y: int, w: int, h: int,
                  settings: dict | None = None, z: int = 0) -> dict:
    wid = new_uid()
    q("""INSERT INTO widgets (id, page_id, type, x, y, w, h, z, settings, created_at)
         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
      (wid, page_id, wtype, x, y, w, h, z, Jsonb(settings or {}), now_iso()))
    return get_widget(wid)


def update_widget(wid: str, data: dict) -> dict | None:
    if not get_widget(wid):
        return None
    sets, vals = [], []
    for f in ("x", "y", "w", "h", "z", "type", "page_id"):
        if f in data:
            sets.append(f"{f} = %s")
            vals.append(data[f])
    if "settings" in data:
        sets.append("settings = %s")
        vals.append(Jsonb(data["settings"]))
    if not sets:
        return get_widget(wid)
    vals.append(wid)
    q(f"UPDATE widgets SET {', '.join(sets)} WHERE id = %s", vals)
    return get_widget(wid)


def bulk_update_widgets(items: list[dict]) -> int:
    """A layout save moves many widgets at once — one transaction, not N, so a
    half-applied drag can't survive a crash."""
    n = 0
    c = conn()
    with c.transaction(), c.cursor() as cur:
        for it in items:
            if not it.get("id"):
                continue
            cur.execute("UPDATE widgets SET x=%s, y=%s, w=%s, h=%s WHERE id=%s",
                        (it["x"], it["y"], it["w"], it["h"], it["id"]))
            n += cur.rowcount
    return n


def delete_widget(wid: str) -> bool:
    return q("DELETE FROM widgets WHERE id = %s", (wid,)) > 0


# -------------------------------------------------------------------- todos

def list_todos(list_name: str | None = None, include_done: bool = True) -> list[dict]:
    sql, where, vals = "SELECT * FROM todos", [], []
    if list_name:
        where.append("list_name = %s")
        vals.append(list_name)
    if not include_done:
        where.append("done = FALSE")
    if where:
        sql += " WHERE " + " AND ".join(where)
    return q(sql + " ORDER BY done, position, created_at", vals, fetch="all")


def get_todo(tid: str) -> dict | None:
    return q("SELECT * FROM todos WHERE id = %s", (tid,), fetch="one")


def create_todo(data: dict) -> dict:
    tid = new_uid()
    ts = now_iso()
    lst = data.get("list_name") or "Home"
    row = q("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM todos WHERE list_name = %s",
            (lst,), fetch="one")
    q("""INSERT INTO todos (id, list_name, title, notes, done, priority,
             due_utc, position, created_at, updated_at)
         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
      (tid, lst, data["title"], data.get("notes", "") or "", bool(data.get("done")),
       int(data.get("priority", 0)), data.get("due_utc"), row["p"], ts, ts))
    return get_todo(tid)


def update_todo(tid: str, data: dict) -> dict | None:
    if not get_todo(tid):
        return None
    sets, vals = [], []
    for f in ("title", "notes", "priority", "due_utc", "list_name", "position"):
        if f in data:
            sets.append(f"{f} = %s")
            vals.append(data[f])
    if "done" in data:
        sets.append("done = %s")
        vals.append(bool(data["done"]))
    if not sets:
        return get_todo(tid)
    sets.append("updated_at = %s")
    vals += [now_iso(), tid]
    q(f"UPDATE todos SET {', '.join(sets)} WHERE id = %s", vals)
    return get_todo(tid)


def delete_todo(tid: str) -> bool:
    return q("DELETE FROM todos WHERE id = %s", (tid,)) > 0


# ------------------------------------------------------------- notifications

def list_notifications(limit: int = 50, unread_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM notifications"
    if unread_only:
        sql += " WHERE read = FALSE"
    return q(sql + " ORDER BY created_at DESC LIMIT %s", (limit,), fetch="all")


def push_notification(title: str, body: str = "", kind: str = "info",
                      source: str = "", dedupe_key: str | None = None) -> dict | None:
    """Returns None when dedupe_key already exists, so callers can fire this on
    a timer without spamming the panel."""
    nid = new_uid()
    n = q("""INSERT INTO notifications (id, kind, title, body, source,
                 dedupe_key, read, created_at)
             VALUES (%s,%s,%s,%s,%s,%s,FALSE,%s)
             ON CONFLICT (dedupe_key) DO NOTHING""",
          (nid, kind, title, body, source, dedupe_key, now_iso()))
    if not n:
        return None
    return q("SELECT * FROM notifications WHERE id = %s", (nid,), fetch="one")


def mark_notification(nid: str, read: bool = True) -> bool:
    return q("UPDATE notifications SET read = %s WHERE id = %s", (bool(read), nid)) > 0


def delete_notification(nid: str) -> bool:
    return q("DELETE FROM notifications WHERE id = %s", (nid,)) > 0


def clear_notifications() -> int:
    return q("DELETE FROM notifications")


# ------------------------------------------------------------------ devices

def list_devices() -> list[dict]:
    return q("SELECT * FROM devices ORDER BY room, name", fetch="all")


def get_device(did: str) -> dict | None:
    return q("SELECT * FROM devices WHERE id = %s", (did,), fetch="one")


def create_device(data: dict) -> dict:
    did = data.get("id") or new_uid()
    q("""INSERT INTO devices (id, name, kind, room, config, enabled,
             last_state, last_seen, created_at)
         VALUES (%s,%s,%s,%s,%s,%s,'{}'::jsonb,NULL,%s)""",
      (did, data["name"], data["kind"], data.get("room", "") or "",
       Jsonb(data.get("config", {})), data.get("enabled") is not False, now_iso()))
    return get_device(did)


def update_device(did: str, data: dict) -> dict | None:
    if not get_device(did):
        return None
    sets, vals = [], []
    for f in ("name", "kind", "room"):
        if f in data:
            sets.append(f"{f} = %s")
            vals.append(data[f])
    if "config" in data:
        sets.append("config = %s")
        vals.append(Jsonb(data["config"]))
    if "enabled" in data:
        sets.append("enabled = %s")
        vals.append(bool(data["enabled"]))
    if not sets:
        return get_device(did)
    vals.append(did)
    q(f"UPDATE devices SET {', '.join(sets)} WHERE id = %s", vals)
    return get_device(did)


def record_device_state(did: str, state: dict) -> None:
    q("UPDATE devices SET last_state = %s, last_seen = %s WHERE id = %s",
      (Jsonb(state), now_iso(), did))


def delete_device(did: str) -> bool:
    return q("DELETE FROM devices WHERE id = %s", (did,)) > 0


# ------------------------------------------------------------------- scenes

def list_scenes() -> list[dict]:
    return q("SELECT * FROM scenes ORDER BY name", fetch="all")


def get_scene(sid: str) -> dict | None:
    return q("SELECT * FROM scenes WHERE id = %s", (sid,), fetch="one")


def create_scene(data: dict) -> dict:
    sid = new_uid()
    q("INSERT INTO scenes (id, name, icon, color, actions, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
      (sid, data["name"], data.get("icon", "sparkles"), data.get("color") or "",
       Jsonb(data.get("actions", [])), now_iso()))
    return get_scene(sid)


def update_scene(sid: str, data: dict) -> dict | None:
    if not get_scene(sid):
        return None
    sets, vals = [], []
    for f in ("name", "icon", "color"):
        if f in data:
            sets.append(f"{f} = %s")
            vals.append(data[f])
    if "actions" in data:
        sets.append("actions = %s")
        vals.append(Jsonb(data["actions"]))
    if not sets:
        return get_scene(sid)
    vals.append(sid)
    q(f"UPDATE scenes SET {', '.join(sets)} WHERE id = %s", vals)
    return get_scene(sid)


def delete_scene(sid: str) -> bool:
    return q("DELETE FROM scenes WHERE id = %s", (sid,)) > 0


# ----------------------------------------------------------------- finance

def list_finance_items() -> list[dict]:
    return q("SELECT * FROM finance_items ORDER BY created_at", fetch="all")


def get_finance_item(iid: str) -> dict | None:
    return q("SELECT * FROM finance_items WHERE id = %s", (iid,), fetch="one")


def get_finance_item_by_item_id(item_id: str) -> dict | None:
    """Look an item up by Plaid's id, so linking is idempotent.

    Exchanging a public_token twice yields two different access_tokens for the
    SAME item_id — so the Plaid id is the only reliable identity here, and the
    only thing that can tell a genuine second institution from the same one
    arriving twice.
    """
    if not item_id:
        return None
    return q("SELECT * FROM finance_items WHERE item_id = %s ORDER BY created_at LIMIT 1",
             (item_id,), fetch="one")


def create_finance_item(data: dict) -> dict:
    iid = new_uid()
    q("""INSERT INTO finance_items (id, provider, item_id, access_token,
             institution, status, created_at)
         VALUES (%s,%s,%s,%s,%s,%s,%s)""",
      (iid, data.get("provider", "plaid"), data.get("item_id"),
       data.get("access_token"), data.get("institution", "") or "",
       data.get("status", "") or "", now_iso()))
    return get_finance_item(iid)


def update_finance_item(iid: str, data: dict) -> dict | None:
    sets, vals = [], []
    for f in ("institution", "status", "last_sync", "access_token", "item_id"):
        if f in data:
            sets.append(f"{f} = %s")
            vals.append(data[f])
    if not sets:
        return get_finance_item(iid)
    vals.append(iid)
    q(f"UPDATE finance_items SET {', '.join(sets)} WHERE id = %s", vals)
    return get_finance_item(iid)


def delete_finance_item(iid: str) -> bool:
    return q("DELETE FROM finance_items WHERE id = %s", (iid,)) > 0


def list_finance_accounts(include_hidden: bool = True) -> list[dict]:
    sql = "SELECT * FROM finance_accounts"
    if not include_hidden:
        sql += " WHERE hidden = FALSE"
    return q(sql + " ORDER BY position, institution, name", fetch="all")


def get_finance_account(aid: str) -> dict | None:
    return q("SELECT * FROM finance_accounts WHERE id = %s", (aid,), fetch="one")


def find_finance_account(item_id: str, external_id: str) -> dict | None:
    return q("""SELECT * FROM finance_accounts
                WHERE item_id = %s AND external_id = %s""",
             (item_id, external_id), fetch="one")


FINANCE_FIELDS = ("name", "official_name", "institution", "kind", "subtype", "mask",
                  "balance", "available", "credit_limit", "apr", "min_payment",
                  "due_day", "next_due", "color", "position")


def create_finance_account(data: dict) -> dict:
    aid = new_uid()
    ts = now_iso()
    q("""INSERT INTO finance_accounts (id, item_id, external_id, name, official_name,
             institution, kind, subtype, mask, balance, available, credit_limit,
             apr, min_payment, due_day, next_due, color, hidden, position,
             updated_at, created_at)
         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s,%s,%s)""",
      (aid, data.get("item_id"), data.get("external_id"), data["name"],
       data.get("official_name", "") or "", data.get("institution", "") or "",
       data.get("kind", "other"), data.get("subtype", "") or "",
       data.get("mask", "") or "", float(data.get("balance") or 0),
       data.get("available"), data.get("credit_limit"), data.get("apr"),
       data.get("min_payment"), data.get("due_day"), data.get("next_due"),
       data.get("color", "") or "", int(data.get("position") or 0), ts, ts))
    add_finance_history(aid, float(data.get("balance") or 0))
    return get_finance_account(aid)


def update_finance_account(aid: str, data: dict) -> dict | None:
    prev = get_finance_account(aid)
    if not prev:
        return None
    sets, vals = [], []
    for f in FINANCE_FIELDS:
        if f in data:
            sets.append(f"{f} = %s")
            vals.append(data[f])
    if "hidden" in data:
        sets.append("hidden = %s")
        vals.append(bool(data["hidden"]))
    if not sets:
        return prev
    sets.append("updated_at = %s")
    vals += [now_iso(), aid]
    q(f"UPDATE finance_accounts SET {', '.join(sets)} WHERE id = %s", vals)
    # History only on a real move, so a rename doesn't fake a data point.
    if "balance" in data and float(data["balance"] or 0) != float(prev["balance"] or 0):
        add_finance_history(aid, float(data["balance"] or 0))
    return get_finance_account(aid)


def delete_finance_account(aid: str) -> bool:
    return q("DELETE FROM finance_accounts WHERE id = %s", (aid,)) > 0


def add_finance_history(aid: str, balance: float) -> None:
    q("INSERT INTO finance_history (id, account_id, balance, at) VALUES (%s,%s,%s,%s)",
      (new_uid(), aid, float(balance), now_iso()))


def finance_history(aid: str, limit: int = 400) -> list[dict]:
    return q("""SELECT balance, at FROM finance_history
                WHERE account_id = %s ORDER BY at DESC LIMIT %s""",
             (aid, limit), fetch="all")


def finance_networth_series(days: int = 180) -> list[dict]:
    """Daily net worth: last reading per account per day, assets minus debts.

    Carried forward with a window function so a day where only one account was
    updated still totals every account, rather than dipping to near-zero.
    """
    return q("""
        WITH days AS (
            SELECT DISTINCT substring(at, 1, 10) AS d FROM finance_history
            WHERE substring(at, 1, 10) >= to_char(now() - (%s || ' days')::interval, 'YYYY-MM-DD')
        ),
        acct_day AS (
            SELECT a.id, a.kind, d.d,
                   (SELECT h.balance FROM finance_history h
                    WHERE h.account_id = a.id AND substring(h.at, 1, 10) <= d.d
                    ORDER BY h.at DESC LIMIT 1) AS bal
            FROM finance_accounts a CROSS JOIN days d
            WHERE a.hidden = FALSE
        )
        SELECT d,
               SUM(CASE WHEN kind IN ('credit','loan') THEN -COALESCE(bal,0)
                        ELSE COALESCE(bal,0) END) AS net
        FROM acct_day GROUP BY d ORDER BY d
    """, (days,), fetch="all")


# ------------------------------------------------------------ transactions

# Money moving between your OWN accounts is not spending, and neither is paying
# a card — the purchases on that card were already counted. Leaving these in
# double-counts every dollar and makes the spending chart useless.
NON_SPEND = ["TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS"]


def upsert_finance_transaction(data: dict) -> None:
    """Insert or update one transaction, keyed on Plaid's id.

    Plaid re-sends a transaction when it settles (pending -> posted, and the
    amount can change), so this has to be an upsert or every purchase is
    counted twice: once pending, once final.
    """
    q("""INSERT INTO finance_transactions
             (id, item_id, account_id, external_id, name, merchant, amount,
              category, pending, posted_on, created_at)
         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
         ON CONFLICT (item_id, external_id) WHERE external_id IS NOT NULL
         DO UPDATE SET name = EXCLUDED.name, merchant = EXCLUDED.merchant,
                       amount = EXCLUDED.amount, category = EXCLUDED.category,
                       pending = EXCLUDED.pending, posted_on = EXCLUDED.posted_on,
                       account_id = EXCLUDED.account_id""",
      (new_uid(), data.get("item_id"), data.get("account_id"), data.get("external_id"),
       (data.get("name") or "")[:300], (data.get("merchant") or "")[:200],
       float(data.get("amount") or 0), (data.get("category") or "")[:80],
       bool(data.get("pending")), data["posted_on"], now_iso()))


def delete_finance_transactions(external_ids: list[str]) -> int:
    if not external_ids:
        return 0
    return q("DELETE FROM finance_transactions WHERE external_id = ANY(%s)",
             (list(external_ids),))


def count_finance_transactions() -> int:
    return q("SELECT count(*) AS n FROM finance_transactions", fetch="one")["n"]


def finance_spend_by_category(months: int = 6) -> list[dict]:
    """Outflow per category over a window. Amounts flipped to positive-is-spend."""
    return q("""
        SELECT COALESCE(NULLIF(category, ''), 'OTHER') AS category,
               SUM(amount) AS total, count(*) AS n
        FROM finance_transactions
        WHERE amount > 0
          AND category <> ALL(%s)
          AND posted_on >= to_char(
                date_trunc('month', now() - (%s || ' months')::interval), 'YYYY-MM-DD')
        GROUP BY 1 ORDER BY total DESC
    """, (NON_SPEND, months - 1), fetch="all")


def finance_cashflow_by_month(months: int = 6) -> list[dict]:
    """Money in vs money out per calendar month.

    Transfers are excluded from BOTH directions — moving $500 to savings is not
    income and not spending, and counting it inflates each side equally.
    """
    return q("""
        SELECT substring(posted_on, 1, 7) AS month,
               SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS money_in,
               SUM(CASE WHEN amount > 0 THEN  amount ELSE 0 END) AS money_out
        FROM finance_transactions
        WHERE category <> ALL(%s)
          AND posted_on >= to_char(
                date_trunc('month', now() - (%s || ' months')::interval), 'YYYY-MM-DD')
        GROUP BY 1 ORDER BY 1
    """, (NON_SPEND, months - 1), fetch="all")


def finance_top_merchants(months: int = 1, limit: int = 8) -> list[dict]:
    return q("""
        SELECT COALESCE(NULLIF(merchant, ''), name) AS merchant,
               SUM(amount) AS total, count(*) AS n
        FROM finance_transactions
        WHERE amount > 0
          AND category <> ALL(%s)
          AND posted_on >= to_char(
                date_trunc('month', now() - (%s || ' months')::interval), 'YYYY-MM-DD')
        GROUP BY 1 HAVING SUM(amount) > 0
        ORDER BY total DESC LIMIT %s
    """, (NON_SPEND, months - 1, limit), fetch="all")


def finance_recent_transactions(limit: int = 25) -> list[dict]:
    return q("""SELECT t.*, a.name AS account_name, a.institution
                FROM finance_transactions t
                LEFT JOIN finance_accounts a ON t.account_id = a.id
                ORDER BY t.posted_on DESC, t.created_at DESC LIMIT %s""",
             (limit,), fetch="all")


# ------------------------------------------------------------------ people

def list_people() -> list[dict]:
    return q("SELECT * FROM people ORDER BY position, created_at", fetch="all")


def get_person(pid: str) -> dict | None:
    return q("SELECT * FROM people WHERE id = %s", (pid,), fetch="one")


def create_person(data: dict) -> dict:
    pid = new_uid()
    pos = q("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM people", fetch="one")["p"]
    q("""INSERT INTO people (id, name, greeting, color, avatar, macs, theme,
             settings, position, created_at)
         VALUES (%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb,%s,%s)""",
      (pid, data["name"], data.get("greeting", "") or "", data.get("color", "") or "",
       data.get("avatar", "") or "", Jsonb(data.get("macs") or []),
       Jsonb(data["theme"]) if data.get("theme") else None, pos, now_iso()))
    return get_person(pid)


def update_person(pid: str, data: dict) -> dict | None:
    if not get_person(pid):
        return None
    sets, vals = [], []
    for f in ("name", "greeting", "color", "avatar", "position", "last_seen"):
        if f in data:
            sets.append(f"{f} = %s")
            vals.append(data[f])
    for f in ("macs", "settings"):
        if f in data:
            sets.append(f"{f} = %s")
            vals.append(Jsonb(data[f]))
    if "theme" in data:
        sets.append("theme = %s")
        vals.append(Jsonb(data["theme"]) if data["theme"] else None)
    if not sets:
        return get_person(pid)
    vals.append(pid)
    q(f"UPDATE people SET {', '.join(sets)} WHERE id = %s", vals)
    return get_person(pid)


def delete_person(pid: str) -> bool:
    """Their pages survive as shared pages — see the FK's ON DELETE SET NULL."""
    return q("DELETE FROM people WHERE id = %s", (pid,)) > 0


def reorder_people(ids: list[str]) -> int:
    n = 0
    for i, pid in enumerate(ids):
        n += q("UPDATE people SET position = %s WHERE id = %s", (i, pid))
    return n


def mark_person_seen(pid: str) -> None:
    q("UPDATE people SET last_seen = %s WHERE id = %s", (now_iso(), pid))


# ---------------------------------------------------------------- galleries

def list_galleries() -> list[dict]:
    return q("""SELECT g.*, COUNT(i.id) AS image_count,
                       (SELECT id FROM gallery_images
                        WHERE gallery_id = g.id ORDER BY position, created_at LIMIT 1) AS cover_id
                FROM galleries g LEFT JOIN gallery_images i ON i.gallery_id = g.id
                GROUP BY g.id ORDER BY g.position, g.created_at""", fetch="all")


def get_gallery(gid: str) -> dict | None:
    return q("SELECT * FROM galleries WHERE id = %s", (gid,), fetch="one")


def create_gallery(name: str, dirname: str) -> dict:
    gid = new_uid()
    pos = q("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM galleries", fetch="one")["p"]
    q("INSERT INTO galleries (id, name, dirname, position, created_at) VALUES (%s,%s,%s,%s,%s)",
      (gid, name, dirname, pos, now_iso()))
    return get_gallery(gid)


def update_gallery(gid: str, data: dict) -> dict | None:
    if not get_gallery(gid):
        return None
    sets, vals = [], []
    for f in ("name", "position"):
        if f in data:
            sets.append(f"{f} = %s")
            vals.append(data[f])
    if sets:
        vals.append(gid)
        q(f"UPDATE galleries SET {', '.join(sets)} WHERE id = %s", vals)
    return get_gallery(gid)


def delete_gallery(gid: str) -> bool:
    """Rows only — the folder and its files stay on disk, on purpose."""
    return q("DELETE FROM galleries WHERE id = %s", (gid,)) > 0


def reorder_galleries(ids: list[str]) -> int:
    n = 0
    for i, gid in enumerate(ids):
        n += q("UPDATE galleries SET position = %s WHERE id = %s", (i, gid))
    return n


def list_gallery_images(gid: str) -> list[dict]:
    return q("""SELECT * FROM gallery_images WHERE gallery_id = %s
                ORDER BY position, created_at""", (gid,), fetch="all")


def get_gallery_image(img_id: str) -> dict | None:
    return q("SELECT * FROM gallery_images WHERE id = %s", (img_id,), fetch="one")


def add_gallery_image(gid: str, filename: str) -> dict:
    iid = new_uid()
    pos = q("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM gallery_images WHERE gallery_id = %s",
            (gid,), fetch="one")["p"]
    q("""INSERT INTO gallery_images (id, gallery_id, filename, position, created_at)
         VALUES (%s,%s,%s,%s,%s)
         ON CONFLICT (gallery_id, filename) DO NOTHING""",
      (iid, gid, filename, pos, now_iso()))
    row = q("SELECT * FROM gallery_images WHERE gallery_id = %s AND filename = %s",
            (gid, filename), fetch="one")
    return row


def delete_gallery_image(img_id: str) -> bool:
    return q("DELETE FROM gallery_images WHERE id = %s", (img_id,)) > 0


def reorder_gallery_images(gid: str, ids: list[str]) -> int:
    n = 0
    for i, iid in enumerate(ids):
        n += q("UPDATE gallery_images SET position = %s WHERE id = %s AND gallery_id = %s",
               (i, iid, gid))
    return n


# ----------------------------------------------------------------- settings

def get_setting(key: str, default: str | None = None) -> str | None:
    row = q("SELECT value FROM settings WHERE key = %s", (key,), fetch="one")
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    q("INSERT INTO settings (key, value) VALUES (%s, %s) "
      "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))


def all_settings() -> dict:
    return {r["key"]: r["value"] for r in q("SELECT key, value FROM settings", fetch="all")}
