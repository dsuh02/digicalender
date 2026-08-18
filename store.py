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
from datetime import datetime, timedelta, timezone

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
-- NOTE: the unique index on item_id is deliberately NOT here. SCHEMA runs
-- before any migration, so creating it here raises UniqueViolation on a
-- database that still holds duplicates — and the app can never boot to run the
-- migration that would clean them up. See _dedupe_finance_items().

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

-- ------------------------------------------------------------ loan statements
--
-- Servicers that have left Plaid still send a monthly PDF, and that statement
-- is the only record of these loans there is. One row per statement, one
-- loan_details row per loan on it.
--
-- This is an APPEND-ONLY ARCHIVE, not a mirror of a live balance. Each row is
-- what the servicer said on one date and stays true forever, which is why the
-- per-loan figures are stored per statement rather than being overwritten on a
-- table of current loans: the month-over-month series is the entire value of
-- having them. Watching one loan's interest rate move between two statements is
-- something no "current balance" table can answer.
--
-- The unique index is on (servicer, account_number, statement_date) because a
-- month is the natural key here. Re-uploading the same PDF, or a corrected one
-- for a month already held, replaces that month rather than adding a second.
CREATE TABLE IF NOT EXISTS loan_statements (
    id                 TEXT PRIMARY KEY,
    account_id         TEXT REFERENCES finance_accounts(id) ON DELETE SET NULL,
    servicer           TEXT NOT NULL DEFAULT 'aidvantage',
    institution        TEXT NOT NULL DEFAULT '',
    account_number     TEXT NOT NULL,
    statement_date     TEXT NOT NULL,
    period_start       TEXT NOT NULL DEFAULT '',
    period_end         TEXT NOT NULL DEFAULT '',
    due_date           TEXT NOT NULL DEFAULT '',
    current_balance    DOUBLE PRECISION NOT NULL DEFAULT 0,
    unpaid_principal   DOUBLE PRECISION NOT NULL DEFAULT 0,
    unpaid_interest    DOUBLE PRECISION NOT NULL DEFAULT 0,
    original_principal DOUBLE PRECISION NOT NULL DEFAULT 0,
    current_due        DOUBLE PRECISION NOT NULL DEFAULT 0,
    past_due           DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_due          DOUBLE PRECISION NOT NULL DEFAULT 0,
    paid_since_last    DOUBLE PRECISION NOT NULL DEFAULT 0,
    applied_interest   DOUBLE PRECISION NOT NULL DEFAULT 0,
    applied_principal  DOUBLE PRECISION NOT NULL DEFAULT 0,
    autopay            BOOLEAN NOT NULL DEFAULT FALSE,
    autopay_amount     DOUBLE PRECISION,
    autopay_date       TEXT NOT NULL DEFAULT '',
    source_name        TEXT NOT NULL DEFAULT '',
    source_sha256      TEXT NOT NULL DEFAULT '',
    imported_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lstmt_month
    ON loan_statements(servicer, account_number, statement_date);
CREATE INDEX IF NOT EXISTS idx_lstmt_date ON loan_statements(statement_date DESC);

CREATE TABLE IF NOT EXISTS loan_details (
    id                   TEXT PRIMARY KEY,
    statement_id         TEXT NOT NULL REFERENCES loan_statements(id) ON DELETE CASCADE,
    loan_ref             TEXT NOT NULL,
    position             INTEGER NOT NULL DEFAULT 0,
    program              TEXT NOT NULL DEFAULT '',
    rate                 DOUBLE PRECISION,
    rate_type            TEXT NOT NULL DEFAULT '',
    opened_on            TEXT NOT NULL DEFAULT '',
    current_balance      DOUBLE PRECISION NOT NULL DEFAULT 0,
    unpaid_principal     DOUBLE PRECISION NOT NULL DEFAULT 0,
    unpaid_interest      DOUBLE PRECISION NOT NULL DEFAULT 0,
    original_principal   DOUBLE PRECISION NOT NULL DEFAULT 0,
    capitalized_interest DOUBLE PRECISION NOT NULL DEFAULT 0,
    principal_reduction  DOUBLE PRECISION NOT NULL DEFAULT 0,
    life_payments        DOUBLE PRECISION NOT NULL DEFAULT 0,
    principal_paid       DOUBLE PRECISION NOT NULL DEFAULT 0,
    interest_paid        DOUBLE PRECISION NOT NULL DEFAULT 0,
    payments_received    DOUBLE PRECISION NOT NULL DEFAULT 0,
    applied_interest     DOUBLE PRECISION NOT NULL DEFAULT 0,
    applied_principal    DOUBLE PRECISION NOT NULL DEFAULT 0,
    returned_check_fee   DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_payment_on      TEXT NOT NULL DEFAULT '',
    current_due          DOUBLE PRECISION NOT NULL DEFAULT 0,
    past_due             DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_due            DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ldet_loan
    ON loan_details(statement_id, loan_ref);

-- ---------------------------------------------------------------- pipeline
--
-- Background work that produces data other work depends on.
--
-- The rules this schema exists to enforce, in order of how expensive they are
-- to retrofit:
--
-- 1. **A node's inputs and outputs are declared, and the dependency graph is
--    DERIVED from those declarations.** There is no second place listing "when
--    A finishes, run B" — such a list inevitably drifts from what the code
--    actually reads, and then work runs against data that is not there yet.
--
-- 2. **Artifacts are versioned and append-only.** Readiness is "every input I
--    declared has a version newer than the one I last consumed", which is a
--    question this table can answer directly. Overwriting in place would make
--    staleness unknowable.
--
-- 3. **Reads are recorded, not just writes.** Without this, "did that run on
--    stale input?" can only be answered by reading source code and guessing.
--    With it, it is a join.
--
-- 4. **One row per runnable node, enforced by the database.** A SELECT that
--    checks "is this already queued?" followed by an INSERT has a window
--    between them. The partial unique index below closes it, so correctness
--    does not depend on how many workers happen to be running.

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id          TEXT PRIMARY KEY,
    reason      TEXT NOT NULL DEFAULT '',   -- who asked for this, in words
    budget      INTEGER NOT NULL DEFAULT 50,
    spent       INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | done | exhausted | cancelled
    started_at  TEXT NOT NULL,
    ended_at    TEXT
);

-- Every artifact ever produced. Never updated, only appended: version N+1 is a
-- new row, so "what did this consumer actually see?" stays answerable forever.
CREATE TABLE IF NOT EXISTS pipeline_artifacts (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,              -- the declared contract name
    version     INTEGER NOT NULL,
    produced_by TEXT NOT NULL,              -- node id
    run_id      TEXT REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_kind_version
    ON pipeline_artifacts(kind, version);
CREATE INDEX IF NOT EXISTS idx_artifact_latest ON pipeline_artifacts(kind, version DESC);

CREATE TABLE IF NOT EXISTS pipeline_tasks (
    id            TEXT PRIMARY KEY,
    run_id        TEXT REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    node_id       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|skipped
    reason        TEXT NOT NULL DEFAULT '',   -- WHY this was admitted; never blank
    attempt       INTEGER NOT NULL DEFAULT 1,
    error         TEXT NOT NULL DEFAULT '',
    started_at    TEXT,
    ended_at      TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ptask_run ON pipeline_tasks(run_id, status);
-- THE choke point. One live task per node, enforced here rather than by a
-- check-then-insert in application code, so it holds under any concurrency.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ptask_live_unique
    ON pipeline_tasks(node_id) WHERE status IN ('pending', 'running');

-- What each task actually consumed. The reason "did this run on stale data?"
-- is a query here and not an archaeology exercise.
CREATE TABLE IF NOT EXISTS pipeline_reads (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES pipeline_tasks(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    version     INTEGER,                    -- NULL = declared input was absent
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_preads_task ON pipeline_reads(task_id);

-- Mail accounts. The secret is an app password (IMAP) — Gmail keeps those as
-- the explicit exception to its OAuth mandate, and imaplib is stdlib, so this
-- needs no dependency and no browser round trip.
CREATE TABLE IF NOT EXISTS mail_accounts (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT 'Mail',
    kind        TEXT NOT NULL DEFAULT 'imap',
    host        TEXT NOT NULL DEFAULT 'imap.gmail.com',
    port        INTEGER NOT NULL DEFAULT 993,
    username    TEXT NOT NULL DEFAULT '',
    secret      TEXT NOT NULL DEFAULT '',
    folder      TEXT NOT NULL DEFAULT 'INBOX',
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    color       TEXT NOT NULL DEFAULT '',
    last_sync   TEXT,
    last_status TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- ENVELOPES ONLY — no bodies, ever.
--
-- This panel hangs in a room other people walk through, and it is not a mail
-- client. Storing subjects and senders is enough for every use here; storing
-- bodies would multiply the blast radius of any compromise for no gain. Flags
-- stay authoritative on the server, so `unread` here is a cached observation,
-- not the truth.
CREATE TABLE IF NOT EXISTS mail_messages (
    id           TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL REFERENCES mail_accounts(id) ON DELETE CASCADE,
    uid          TEXT NOT NULL,
    message_id   TEXT NOT NULL DEFAULT '',
    from_name    TEXT NOT NULL DEFAULT '',
    from_addr    TEXT NOT NULL DEFAULT '',
    subject      TEXT NOT NULL DEFAULT '',
    snippet      TEXT NOT NULL DEFAULT '',
    received_at  TEXT NOT NULL,
    unread       BOOLEAN NOT NULL DEFAULT TRUE,
    flagged      BOOLEAN NOT NULL DEFAULT FALSE,
    to_me        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mailmsg_uid ON mail_messages(account_id, uid);
CREATE INDEX IF NOT EXISTS idx_mailmsg_recent ON mail_messages(received_at DESC);

-- Lyrics answers, hits AND misses. LRCLIB is a free volunteer service; asking
-- it for the same track every few seconds because a widget re-rendered would be
-- rude, and most misses are permanent anyway.
CREATE TABLE IF NOT EXISTS lyrics_cache (
    id         TEXT PRIMARY KEY,
    key        TEXT NOT NULL UNIQUE,
    payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TEXT NOT NULL
);

-- Wake-up routines: a time, some days, and a sequence to run on a device.
--
-- `last_fired` is a LOCAL DATE STRING, not a timestamp. The scheduler asks
-- "has this already gone off today?", and a date is the honest shape for that
-- question — it also makes a restart mid-morning idempotent instead of
-- re-firing the alarm.
--
-- `days` is a JSON array of weekday numbers, Monday=0, matching date.weekday().
CREATE TABLE IF NOT EXISTS alarms (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT 'Alarm',
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    at_time       TEXT NOT NULL,                 -- 'HH:MM', local
    days          JSONB NOT NULL DEFAULT '[]'::jsonb,
    device_id     TEXT REFERENCES devices(id) ON DELETE SET NULL,
    app_id        TEXT NOT NULL DEFAULT '',      -- Roku channel id
    wait_seconds  INTEGER NOT NULL DEFAULT 13,
    volume        INTEGER,                       -- Spotify Connect %, NULL = leave alone
    spotify_uri   TEXT NOT NULL DEFAULT '',
    device_name   TEXT NOT NULL DEFAULT '',      -- Connect target, matched by name
    shuffle       BOOLEAN NOT NULL DEFAULT FALSE,
    last_fired    TEXT,
    last_result   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alarms_live ON alarms(enabled, at_time);

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


SCHEMA_VERSION = 11


def _dedupe_finance_items() -> None:
    """Collapse duplicate Plaid items, then enforce uniqueness.

    Order matters and is the whole point: the unique index cannot be created
    while duplicates exist, so the DELETE must come first. Safe to call on a
    fresh, empty database — both statements are then no-ops.
    """
    q("""DELETE FROM finance_items a USING finance_items b
          WHERE a.item_id IS NOT NULL AND a.item_id = b.item_id
            AND (a.created_at > b.created_at
                 OR (a.created_at = b.created_at AND a.id > b.id))""")
    q("""CREATE UNIQUE INDEX IF NOT EXISTS idx_fitem_itemid
            ON finance_items(item_id) WHERE item_id IS NOT NULL""")


def init_db() -> None:
    with conn().cursor() as cur:
        cur.execute(SCHEMA)
    row = q("SELECT version FROM schema_version LIMIT 1", fetch="one")
    if row is None:
        q("INSERT INTO schema_version (version) VALUES (%s)", (SCHEMA_VERSION,))
        _dedupe_finance_items()          # no-op here; creates the index
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
        _dedupe_finance_items()
        # v7 also adds finance_transactions (created by SCHEMA above) and the
        # per-item transaction cursor. ADD COLUMN is needed for the cursor
        # because finance_items already exists on any v6 database.
        q("ALTER TABLE finance_items ADD COLUMN IF NOT EXISTS tx_cursor TEXT")
        q("UPDATE schema_version SET version = 7")
    if row["version"] < 8:
        # v8: alarms — created by SCHEMA above; only the version moves.
        q("UPDATE schema_version SET version = 8")
    if row["version"] < 9:
        # v9: pipeline (runs/artifacts/tasks/reads) + mail — all created by
        # SCHEMA above; only the version moves.
        q("UPDATE schema_version SET version = 9")
    if row["version"] < 10:
        # v10: lyrics cache — created by SCHEMA above; only the version moves.
        q("UPDATE schema_version SET version = 10")
    if row["version"] < 11:
        # v11: loan statements + per-loan detail — created by SCHEMA above;
        # only the version moves.
        q("UPDATE schema_version SET version = 11")


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


def create_finance_account(data: dict, history: bool = True) -> dict:
    """`history=False` for callers that supply their own dated points.

    A statement import knows the date its balance belongs to; the automatic
    point here is stamped `now`, and the two together would put an extra reading
    on the chart dated today with an old statement's balance on it.
    """
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
    if history:
        add_finance_history(aid, float(data.get("balance") or 0))
    return get_finance_account(aid)


def update_finance_account(aid: str, data: dict, history: bool = True) -> dict | None:
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
    if history and "balance" in data \
            and float(data["balance"] or 0) != float(prev["balance"] or 0):
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
    # generate_series, not GROUP BY alone: a month with no transactions must
    # come back as a zero row, not vanish. Dropping it silently shifts the bar
    # chart's x axis and makes the newest month with data masquerade as the
    # current one.
    return q("""
        WITH span AS (
            SELECT to_char(generate_series(
                       date_trunc('month', now() - (%s || ' months')::interval),
                       date_trunc('month', now()),
                       interval '1 month'), 'YYYY-MM') AS month
        )
        SELECT s.month,
               COALESCE(SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END), 0) AS money_in,
               COALESCE(SUM(CASE WHEN t.amount > 0 THEN  t.amount ELSE 0 END), 0) AS money_out
        FROM span s
        LEFT JOIN finance_transactions t
               ON substring(t.posted_on, 1, 7) = s.month
              AND t.category <> ALL(%s)
        GROUP BY s.month ORDER BY s.month
    """, (months - 1, NON_SPEND), fetch="all")


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


# --------------------------------------------------------- loan statements

LOAN_STATEMENT_FIELDS = (
    "account_id", "servicer", "institution", "account_number", "statement_date",
    "period_start", "period_end", "due_date", "current_balance",
    "unpaid_principal", "unpaid_interest", "original_principal", "current_due",
    "past_due", "total_due", "paid_since_last", "applied_interest",
    "applied_principal", "autopay", "autopay_amount", "autopay_date",
    "source_name", "source_sha256")

LOAN_DETAIL_FIELDS = (
    "loan_ref", "position", "program", "rate", "rate_type", "opened_on",
    "current_balance", "unpaid_principal", "unpaid_interest",
    "original_principal", "capitalized_interest", "principal_reduction",
    "life_payments", "principal_paid", "interest_paid", "payments_received",
    "applied_interest", "applied_principal", "returned_check_fee",
    "last_payment_on", "current_due", "past_due", "total_due")

_TEXT_LOAN_FIELDS = {"loan_ref", "program", "rate_type", "opened_on",
                     "last_payment_on"}


def create_loan_statement(data: dict) -> dict:
    """Insert one statement and all of its loans, together.

    Both writes go in one EXPLICIT transaction. The connection is autocommit, so
    a cursor block alone would commit the statement row and then, if the loans
    failed, leave a balance with nothing behind it — and every later import
    would happily stack on top of that.
    """
    sid = new_uid()
    cols = ", ".join(LOAN_STATEMENT_FIELDS)
    marks = ", ".join(["%s"] * len(LOAN_STATEMENT_FIELDS))
    c = conn()
    with c.transaction(), c.cursor() as cur:
        cur.execute(
            f"INSERT INTO loan_statements (id, {cols}, imported_at) "
            f"VALUES (%s, {marks}, %s)",
            [sid] + [data.get(f) for f in LOAN_STATEMENT_FIELDS] + [now_iso()])
        dcols = ", ".join(LOAN_DETAIL_FIELDS)
        dmarks = ", ".join(["%s"] * len(LOAN_DETAIL_FIELDS))
        for n, loan in enumerate(data.get("loans") or []):
            vals = []
            for f in LOAN_DETAIL_FIELDS:
                v = loan.get(f)
                if f == "position":
                    v = n
                elif f in _TEXT_LOAN_FIELDS:
                    v = v or ""
                elif f != "rate":
                    v = float(v or 0)   # rate stays NULL when unstated
                vals.append(v)
            cur.execute(
                f"INSERT INTO loan_details (id, statement_id, {dcols}) "
                f"VALUES (%s, %s, {dmarks})", [new_uid(), sid] + vals)
    return get_loan_statement(sid)


def get_loan_statement(sid: str) -> dict | None:
    return q("SELECT * FROM loan_statements WHERE id = %s", (sid,), fetch="one")


def find_loan_statement(servicer: str, account_number: str,
                        statement_date: str) -> dict | None:
    return q("""SELECT * FROM loan_statements
                WHERE servicer = %s AND account_number = %s
                  AND statement_date = %s""",
             (servicer, account_number, statement_date), fetch="one")


def list_loan_statements(servicer: str = "", account_number: str = "",
                         limit: int = 240) -> list[dict]:
    where, args = [], []
    if servicer:
        where.append("servicer = %s")
        args.append(servicer)
    if account_number:
        where.append("account_number = %s")
        args.append(account_number)
    sql = "SELECT * FROM loan_statements"
    if where:
        sql += " WHERE " + " AND ".join(where)
    args.append(limit)
    return q(sql + " ORDER BY statement_date DESC LIMIT %s", args, fetch="all")


def latest_loan_statement(servicer: str = "",
                          account_number: str = "") -> dict | None:
    rows = list_loan_statements(servicer, account_number, limit=1)
    return rows[0] if rows else None


def latest_loan_statement_date(account_number: str) -> str | None:
    row = q("""SELECT max(statement_date) AS d FROM loan_statements
               WHERE account_number = %s""", (account_number,), fetch="one")
    return (row or {}).get("d")


def list_loan_details(sid: str) -> list[dict]:
    return q("SELECT * FROM loan_details WHERE statement_id = %s ORDER BY position",
             (sid,), fetch="all")


def loan_series(account_number: str, loan_ref: str = "") -> list[dict]:
    """One loan's figures across every statement held, oldest first.

    With no `loan_ref` this is the whole account's month-by-month history, which
    is what the charts draw.
    """
    if loan_ref:
        return q("""SELECT s.statement_date, d.*
                    FROM loan_details d JOIN loan_statements s
                      ON d.statement_id = s.id
                    WHERE s.account_number = %s AND d.loan_ref = %s
                    ORDER BY s.statement_date""",
                 (account_number, loan_ref), fetch="all")
    return q("""SELECT statement_date, current_balance, unpaid_principal,
                       unpaid_interest, current_due, paid_since_last,
                       applied_interest, applied_principal
                FROM loan_statements WHERE account_number = %s
                ORDER BY statement_date""", (account_number,), fetch="all")


def delete_loan_statement(sid: str) -> bool:
    return q("DELETE FROM loan_statements WHERE id = %s", (sid,)) > 0


def add_finance_history_at(aid: str, balance: float, at: str) -> None:
    """A history point stamped with a date you choose, not with now.

    Importing a back catalogue of statements is exactly the case `now_iso()`
    gets wrong: six months of balances would all land at this afternoon, and the
    balance chart would show a vertical line instead of six months of paydown.
    Replaces any existing point at the same instant so a re-import does not
    double up.
    """
    q("DELETE FROM finance_history WHERE account_id = %s AND at = %s", (aid, at))
    q("INSERT INTO finance_history (id, account_id, balance, at) VALUES (%s,%s,%s,%s)",
      (new_uid(), aid, float(balance), at))


# ------------------------------------------------------------------ alarms

ALARM_FIELDS = ("name", "enabled", "at_time", "days", "device_id", "app_id",
                "wait_seconds", "volume", "spotify_uri", "device_name", "shuffle")


def list_alarms() -> list[dict]:
    return q("SELECT * FROM alarms ORDER BY at_time, created_at", fetch="all")


def get_alarm(aid: str) -> dict | None:
    return q("SELECT * FROM alarms WHERE id = %s", (aid,), fetch="one")


def create_alarm(data: dict) -> dict:
    aid = new_uid()
    q("""INSERT INTO alarms (id, name, enabled, at_time, days, device_id, app_id,
             wait_seconds, volume, spotify_uri, device_name, shuffle, created_at)
         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
      (aid, data.get("name") or "Alarm", bool(data.get("enabled", True)),
       data["at_time"], Jsonb(data.get("days") or []), data.get("device_id"),
       data.get("app_id", "") or "", int(data.get("wait_seconds") or 13),
       data.get("volume"), data.get("spotify_uri", "") or "",
       data.get("device_name", "") or "", bool(data.get("shuffle")), now_iso()))
    return get_alarm(aid)


def update_alarm(aid: str, data: dict) -> dict | None:
    sets, vals = [], []
    for f in ALARM_FIELDS:
        if f in data:
            sets.append(f"{f} = %s")
            vals.append(Jsonb(data[f]) if f == "days" else data[f])
    if not sets:
        return get_alarm(aid)
    vals.append(aid)
    q(f"UPDATE alarms SET {', '.join(sets)} WHERE id = %s", vals)
    return get_alarm(aid)


def delete_alarm(aid: str) -> bool:
    return q("DELETE FROM alarms WHERE id = %s", (aid,)) > 0


def mark_alarm_fired(aid: str, day: str, result: str) -> None:
    q("UPDATE alarms SET last_fired = %s, last_result = %s WHERE id = %s",
      (day, result[:300], aid))


# ---------------------------------------------------------------- pipeline

def seconds_since(iso: str, now: float | None = None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    ref = now if now is not None else datetime.now(timezone.utc).timestamp()
    return ref - then.timestamp()


def create_pipeline_run(reason: str, budget: int = 50) -> dict:
    rid = new_uid()
    q("""INSERT INTO pipeline_runs (id, reason, budget, spent, status, started_at)
         VALUES (%s,%s,%s,0,'running',%s)""",
      (rid, reason[:200], int(budget), now_iso()))
    return get_pipeline_run(rid)


def get_pipeline_run(rid: str) -> dict | None:
    return q("SELECT * FROM pipeline_runs WHERE id = %s", (rid,), fetch="one")


def list_pipeline_runs(limit: int = 20) -> list[dict]:
    return q("SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT %s",
             (limit,), fetch="all")


def finish_pipeline_run(rid: str, status: str) -> None:
    q("UPDATE pipeline_runs SET status = %s, ended_at = %s WHERE id = %s",
      (status, now_iso(), rid))


def spend_pipeline_budget(rid: str, cost: int) -> None:
    q("UPDATE pipeline_runs SET spent = spent + %s WHERE id = %s", (int(cost), rid))


def create_pipeline_task(run_id: str, node_id: str, reason: str) -> dict | None:
    """Insert a task, or return None if one is already live for this node.

    The partial unique index does the deciding, and catching its violation is
    the entire point: a check-then-insert leaves a window where two callers both
    see "nothing queued" and both insert.
    """
    tid = new_uid()
    try:
        q("""INSERT INTO pipeline_tasks (id, run_id, node_id, status, reason, created_at)
             VALUES (%s,%s,%s,'pending',%s,%s)""",
          (tid, run_id, node_id, reason[:300], now_iso()))
    except psycopg.errors.UniqueViolation:
        conn().rollback()
        return None
    except psycopg.Error:
        conn().rollback()
        raise
    return get_pipeline_task(tid)


def get_pipeline_task(tid: str) -> dict | None:
    return q("SELECT * FROM pipeline_tasks WHERE id = %s", (tid,), fetch="one")


def pipeline_live_task(node_id: str) -> dict | None:
    return q("""SELECT * FROM pipeline_tasks
                WHERE node_id = %s AND status IN ('pending','running') LIMIT 1""",
             (node_id,), fetch="one")


def pipeline_last_success(node_id: str) -> dict | None:
    return q("""SELECT * FROM pipeline_tasks
                WHERE node_id = %s AND status = 'done'
                ORDER BY ended_at DESC LIMIT 1""", (node_id,), fetch="one")


def start_pipeline_task(tid: str) -> None:
    q("UPDATE pipeline_tasks SET status = 'running', started_at = %s WHERE id = %s",
      (now_iso(), tid))


def finish_pipeline_task(tid: str, status: str, error: str = "") -> None:
    q("""UPDATE pipeline_tasks SET status = %s, error = %s, ended_at = %s
         WHERE id = %s""", (status, (error or "")[:2000], now_iso(), tid))


def list_pipeline_tasks(run_id: str) -> list[dict]:
    return q("SELECT * FROM pipeline_tasks WHERE run_id = %s ORDER BY created_at",
             (run_id,), fetch="all")


def create_pipeline_artifact(kind: str, produced_by: str, run_id: str | None,
                             payload: dict) -> dict:
    """Append version N+1. Never updates in place — a consumer that recorded
    version 3 must still be able to prove what version 3 contained."""
    aid = new_uid()
    nxt = q("SELECT COALESCE(MAX(version), 0) + 1 AS v FROM pipeline_artifacts WHERE kind = %s",
            (kind,), fetch="one")["v"]
    q("""INSERT INTO pipeline_artifacts (id, kind, version, produced_by, run_id,
             payload, created_at)
         VALUES (%s,%s,%s,%s,%s,%s,%s)""",
      (aid, kind, nxt, produced_by, run_id, Jsonb(payload or {}), now_iso()))
    return q("SELECT * FROM pipeline_artifacts WHERE id = %s", (aid,), fetch="one")


def pipeline_latest_artifact(kind: str) -> dict | None:
    return q("""SELECT * FROM pipeline_artifacts WHERE kind = %s
                ORDER BY version DESC LIMIT 1""", (kind,), fetch="one")


def record_pipeline_read(task_id: str, kind: str, version: int | None) -> None:
    q("""INSERT INTO pipeline_reads (id, task_id, kind, version, created_at)
         VALUES (%s,%s,%s,%s,%s)""", (new_uid(), task_id, kind, version, now_iso()))


def pipeline_input_watermark(node_id: str, kinds: list[str]) -> dict:
    """Highest version of each kind that EXISTED when this node last succeeded.

    Deliberately not "what it read". A node that declares an input and then
    ignores it would otherwise have no recorded version, look permanently
    stale, and re-run forever. The watermark answers "has anything appeared
    since I last ran?", which is the actual readiness question.

    The read log stays separate and serves a different purpose: proving what a
    task really consumed, so staleness is auditable.
    """
    last = pipeline_last_success(node_id)
    if not last or not kinds:
        return {}
    at = last.get("started_at") or last.get("created_at")
    rows = q("""SELECT kind, MAX(version) AS version FROM pipeline_artifacts
                WHERE kind = ANY(%s) AND created_at <= %s
                GROUP BY kind""", (list(kinds), at), fetch="all")
    return {r["kind"]: r["version"] for r in rows}


def pipeline_ran_in_run(run_id: str, node_id: str) -> dict | None:
    """Has this node already been attempted in this run? One shot per run."""
    return q("""SELECT * FROM pipeline_tasks
                WHERE run_id = %s AND node_id = %s
                  AND status IN ('done','failed','skipped','running')
                LIMIT 1""", (run_id, node_id), fetch="one")


def pipeline_stale_reads(limit: int = 50) -> list[dict]:
    """Tasks that consumed an input already superseded by the time they ran.

    This is the query the reads table exists for. Without it, "did that run on
    stale data?" can only be answered by reading source and inferring.
    """
    return q("""
        SELECT t.node_id, t.id AS task_id, r.kind, r.version AS read_version,
               a.version AS newest_at_the_time, t.started_at
        FROM pipeline_reads r
        JOIN pipeline_tasks t ON t.id = r.task_id
        JOIN LATERAL (
            SELECT MAX(version) AS version FROM pipeline_artifacts pa
            WHERE pa.kind = r.kind AND pa.created_at <= COALESCE(t.started_at, t.created_at)
        ) a ON TRUE
        WHERE r.version IS NOT NULL AND a.version > r.version
        ORDER BY t.started_at DESC LIMIT %s""", (limit,), fetch="all")


# -------------------------------------------------------------------- mail

MAIL_FIELDS = ("name", "kind", "host", "port", "username", "secret", "folder",
               "enabled", "color")


def list_mail_accounts() -> list[dict]:
    return q("SELECT * FROM mail_accounts ORDER BY created_at", fetch="all")


def get_mail_account(aid: str) -> dict | None:
    return q("SELECT * FROM mail_accounts WHERE id = %s", (aid,), fetch="one")


def create_mail_account(data: dict) -> dict:
    aid = new_uid()
    q("""INSERT INTO mail_accounts (id, name, kind, host, port, username, secret,
             folder, enabled, color, created_at)
         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
      (aid, data.get("name") or "Mail", data.get("kind", "imap"),
       data.get("host") or "imap.gmail.com", int(data.get("port") or 993),
       data.get("username", "") or "", data.get("secret", "") or "",
       data.get("folder") or "INBOX", bool(data.get("enabled", True)),
       data.get("color", "") or "", now_iso()))
    return get_mail_account(aid)


def update_mail_account(aid: str, data: dict) -> dict | None:
    sets, vals = [], []
    for f in MAIL_FIELDS:
        if f in data:
            sets.append(f"{f} = %s")
            vals.append(data[f])
    if not sets:
        return get_mail_account(aid)
    vals.append(aid)
    q(f"UPDATE mail_accounts SET {', '.join(sets)} WHERE id = %s", vals)
    return get_mail_account(aid)


def delete_mail_account(aid: str) -> bool:
    return q("DELETE FROM mail_accounts WHERE id = %s", (aid,)) > 0


def mark_mail_synced(aid: str, status: str) -> None:
    q("UPDATE mail_accounts SET last_sync = %s, last_status = %s WHERE id = %s",
      (now_iso(), status[:200], aid))


def upsert_mail_message(data: dict) -> None:
    q("""INSERT INTO mail_messages (id, account_id, uid, message_id, from_name,
             from_addr, subject, snippet, received_at, unread, flagged, to_me, created_at)
         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
         ON CONFLICT (account_id, uid) DO UPDATE SET
             unread = EXCLUDED.unread, flagged = EXCLUDED.flagged,
             subject = EXCLUDED.subject, snippet = EXCLUDED.snippet""",
      (new_uid(), data["account_id"], str(data["uid"]), data.get("message_id", "")[:300],
       data.get("from_name", "")[:200], data.get("from_addr", "")[:200],
       data.get("subject", "")[:400], data.get("snippet", "")[:400],
       data["received_at"], bool(data.get("unread", True)),
       bool(data.get("flagged")), bool(data.get("to_me")), now_iso()))


def list_mail_messages(limit: int = 50, unread_only: bool = False) -> list[dict]:
    sql = """SELECT m.*, a.name AS account_name, a.color AS account_color
             FROM mail_messages m JOIN mail_accounts a ON a.id = m.account_id"""
    if unread_only:
        sql += " WHERE m.unread"
    return q(sql + " ORDER BY m.received_at DESC LIMIT %s", (limit,), fetch="all")


def prune_mail_messages(days: int = 30) -> int:
    """Rolling window. This is an index for a wall panel, not an archive."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    return q("DELETE FROM mail_messages WHERE received_at < %s", (cutoff,))


# ------------------------------------------------------------------ lyrics

def get_lyrics(key: str, max_age_days: int = 90) -> dict | None:
    row = q("""SELECT payload FROM lyrics_cache
                WHERE key = %s AND created_at > %s""",
            (key, (datetime.now(timezone.utc) - timedelta(days=int(max_age_days)))
             .strftime("%Y-%m-%dT%H:%M:%SZ")), fetch="one")
    return row["payload"] if row else None


def put_lyrics(key: str, payload: dict) -> None:
    q("""INSERT INTO lyrics_cache (id, key, payload, created_at)
         VALUES (%s,%s,%s,%s)
         ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload,
                                         created_at = EXCLUDED.created_at""",
      (new_uid(), key, Jsonb(payload or {}), now_iso()))


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
