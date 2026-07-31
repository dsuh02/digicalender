"""
iCalendar (RFC 5545) parsing and recurrence expansion — stdlib only.

Scope is what real Google/Outlook subscription feeds actually contain, not the
whole RFC:

  - VEVENT with DTSTART/DTEND (DATE and DATE-TIME, Z / TZID / floating)
  - RRULE: DAILY, WEEKLY (BYDAY), MONTHLY (BYMONTHDAY or ordinal BYDAY like
    "2TU"/"-1FR"), YEARLY — with INTERVAL, COUNT, UNTIL
  - EXDATE, and RECURRENCE-ID overrides (Outlook uses these for every edited
    instance of a recurring meeting)
  - STATUS:CANCELLED

Recurrence is expanded in the event's own timezone so a 9am meeting stays 9am
across DST, then each occurrence is converted to UTC. All-day events stay
floating dates end to end — they are emitted as "YYYY-MM-DDT00:00:00Z" strings
verbatim, matching how the store and the front end treat them.

Outlook publishes Windows timezone names ("Pacific Standard Time"); a small
alias table maps the common ones onto IANA zones for zoneinfo.
"""

from __future__ import annotations

import re
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MAX_OCCURRENCES = 1000          # per recurring event, hard stop
FETCH_TIMEOUT = 20.0
MAX_FEED_BYTES = 8 * 1024 * 1024

# Windows -> IANA for the zones that actually show up in Outlook feeds.
WINDOWS_TZ = {
    "Pacific Standard Time": "America/Los_Angeles",
    "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix",
    "Central Standard Time": "America/Chicago",
    "Eastern Standard Time": "America/New_York",
    "US Eastern Standard Time": "America/New_York",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Alaskan Standard Time": "America/Anchorage",
    "UTC": "UTC",
    "Coordinated Universal Time": "UTC",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris",
    "Central Europe Standard Time": "Europe/Warsaw",
    "Central European Standard Time": "Europe/Warsaw",
    "India Standard Time": "Asia/Kolkata",
    "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "SE Asia Standard Time": "Asia/Bangkok",
    "Singapore Standard Time": "Asia/Singapore",
    "Arabian Standard Time": "Asia/Dubai",
    "Israel Standard Time": "Asia/Jerusalem",
}

_LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def resolve_tz(tzid: str | None):
    if not tzid:
        return _LOCAL_TZ
    tzid = tzid.strip().strip('"')
    try:
        return ZoneInfo(tzid)
    except Exception:
        pass
    mapped = WINDOWS_TZ.get(tzid)
    if mapped:
        try:
            return ZoneInfo(mapped)
        except Exception:
            pass
    # Better a wrong-but-consistent zone than a crash on an exotic TZID.
    return timezone.utc


# ------------------------------------------------------------------ fetching

def fetch(url: str, etag: str | None = None) -> tuple[str | None, str, str]:
    """Returns (text, new_etag, error). text is None on 304 or error."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "DigiCalender/1.0 (wall panel; ics subscriber)",
        "Accept": "text/calendar, text/plain, */*",
    })
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            body = r.read(MAX_FEED_BYTES + 1)
            if len(body) > MAX_FEED_BYTES:
                return None, "", "feed is larger than 8 MB"
            new_etag = r.headers.get("ETag", "") or ""
            return body.decode("utf-8", errors="replace"), new_etag, ""
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag or "", ""
        if e.code in (401, 403):
            return None, "", (f"HTTP {e.code} — the calendar is private. For Google, either make it "
                              "public or paste the 'Secret address in iCal format' from that "
                              "calendar's settings.")
        if e.code == 404:
            return None, "", ("HTTP 404 — no public feed at this address. For Google calendars that "
                              "usually means the calendar isn't public; paste the 'Secret address in "
                              "iCal format' from Google Calendar > Settings > Integrate calendar.")
        return None, "", f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return None, "", f"could not fetch the feed: {e}"


# ------------------------------------------------------------------- parsing

def _unfold(text: str) -> list[str]:
    """RFC folding: a line starting with space/tab continues the previous one."""
    out: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _split_prop(line: str) -> tuple[str, dict, str] | None:
    """NAME;PARAM=V;PARAM="q":VALUE — the first ':' outside quotes splits."""
    depth_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            depth_q = not depth_q
        elif ch == ':' and not depth_q:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None
    parts = []
    buf, q = "", False
    for ch in head:
        if ch == '"':
            q = not q
            buf += ch
        elif ch == ';' and not q:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    name = parts[0].upper()
    params = {}
    for p in parts[1:]:
        if '=' in p:
            k, v = p.split('=', 1)
            params[k.upper()] = v.strip('"')
    return name, params, value


_UNESCAPE = {r"\n": "\n", r"\N": "\n", r"\,": ",", r"\;": ";", r"\\": "\\"}


def _unescape(v: str) -> str:
    return re.sub(r"\\[nN,;\\]", lambda m: _UNESCAPE[m.group(0)], v)


def parse(text: str) -> tuple[list[dict], str]:
    """Returns (raw VEVENT dicts, calendar name). Each event dict carries the
    parsed properties it needs downstream; times still in their native form."""
    lines = _unfold(text)
    calname = ""
    events: list[dict] = []
    cur: dict | None = None
    for line in lines:
        prop = _split_prop(line)
        if not prop:
            continue
        name, params, value = prop
        if name == "BEGIN" and value.strip().upper() == "VEVENT":
            cur = {"EXDATE": []}
            continue
        if name == "END" and value.strip().upper() == "VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
            continue
        if cur is None:
            if name == "X-WR-CALNAME":
                calname = _unescape(value).strip()
            continue
        if name == "EXDATE":
            for v in value.split(","):
                cur["EXDATE"].append((v.strip(), params))
        elif name in ("DTSTART", "DTEND", "RECURRENCE-ID", "SUMMARY", "DESCRIPTION",
                      "LOCATION", "UID", "RRULE", "STATUS", "DURATION", "TRANSP"):
            cur[name] = (value, params)
    return events, calname


# -------------------------------------------------------------- time parsing

def _parse_when(value: str, params: dict):
    """Returns ('date', date) or ('dt', aware datetime)."""
    v = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        return "date", date(int(v[:4]), int(v[4:6]), int(v[6:8]))
    m = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", v)
    if not m:
        raise ValueError(f"unparseable date-time: {value!r}")
    d, t, z = m.groups()
    naive = datetime(int(d[:4]), int(d[4:6]), int(d[6:8]),
                     int(t[:2]), int(t[2:4]), int(t[4:6]))
    if z:
        return "dt", naive.replace(tzinfo=timezone.utc)
    return "dt", naive.replace(tzinfo=resolve_tz(params.get("TZID")))


def _to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_iso(d: date) -> str:
    # Floating date, stored verbatim — never timezone-converted.
    return f"{d.isoformat()}T00:00:00Z"


def _parse_duration(value: str) -> timedelta:
    m = re.fullmatch(r"([+-]?)P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?",
                     value.strip())
    if not m:
        return timedelta(0)
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x) if x else 0 for x in m.groups()[1:])
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def _parse_rrule(value: str) -> dict:
    out = {}
    for part in value.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.upper()] = v
    return out


# ----------------------------------------------------------------- expansion

def _ordinal_byday_in_month(year: int, month: int, weekday: int, ordinal: int):
    """The Nth <weekday> of a month (negative = from the end), or None."""
    days = []
    d = date(year, month, 1)
    while d.month == month:
        if d.weekday() == weekday:
            days.append(d)
        d += timedelta(days=1)
    try:
        return days[ordinal - 1] if ordinal > 0 else days[ordinal]
    except IndexError:
        return None


def _expand_rrule(kind, start, rrule: dict, win_lo, win_hi):
    """Yield occurrence starts (naive wall datetimes, or dates for all-day),
    honouring COUNT/UNTIL. Counting starts from DTSTART even when the window
    begins later — COUNT is defined from the first instance, not from today."""
    freq = rrule.get("FREQ", "").upper()
    interval = max(1, int(rrule.get("INTERVAL", 1) or 1))
    count = int(rrule["COUNT"]) if rrule.get("COUNT") else None

    until = None
    if rrule.get("UNTIL"):
        ukind, uval = _parse_when(rrule["UNTIL"], {})
        if kind == "date":
            until = uval if ukind == "date" else uval.date()
        else:
            until = uval if ukind == "dt" else datetime.combine(
                uval, datetime.max.time(), tzinfo=timezone.utc)

    emitted = 0
    step_i = 0

    def ok(occ):
        nonlocal emitted
        if count is not None and emitted >= count:
            return "stop"
        if until is not None:
            u = until
            if kind == "dt":
                # Compare instants; occ is naive wall time in the event's tz.
                if occ.replace(tzinfo=start.tzinfo) > u:
                    return "stop"
            elif occ > u:
                return "stop"
        emitted += 1
        return "yield"

    if kind == "date":
        cursor = start
    else:
        cursor = start.replace(tzinfo=None)      # expand on wall-clock time
    base_tz = getattr(start, "tzinfo", None)

    if freq == "DAILY":
        occ = cursor
        while step_i < MAX_OCCURRENCES:
            step_i += 1
            state = ok(occ)
            if state == "stop":
                return
            yield occ
            if (kind == "date" and occ > win_hi) or (kind == "dt" and occ.date() > win_hi):
                return
            occ = occ + timedelta(days=interval)

    elif freq == "WEEKLY":
        bydays = [WEEKDAYS[d.strip()[-2:]] for d in rrule.get("BYDAY", "").split(",")
                  if d.strip()[-2:] in WEEKDAYS] or [cursor.weekday()]
        bydays.sort()
        # Anchor to the Monday of DTSTART's week, then walk week blocks.
        week0 = cursor - timedelta(days=cursor.weekday())
        w = 0
        while step_i < MAX_OCCURRENCES:
            base = week0 + timedelta(weeks=w * interval)
            for wd in bydays:
                occ = base + timedelta(days=wd)
                if occ < cursor:
                    continue
                step_i += 1
                if ok(occ) == "stop":
                    return
                yield occ
                if step_i >= MAX_OCCURRENCES:
                    return
            probe = base.date() if kind == "dt" else base
            if probe > win_hi:
                return
            w += 1

    elif freq == "MONTHLY":
        byday = rrule.get("BYDAY", "")
        bymonthday = rrule.get("BYMONTHDAY", "")
        y, mo = (cursor.year, cursor.month)
        m_ord = re.fullmatch(r"(-?\d+)([A-Z]{2})", byday.strip()) if byday else None
        while step_i < MAX_OCCURRENCES:
            step_i += 1
            occ_date = None
            if m_ord:
                occ_date = _ordinal_byday_in_month(
                    y, mo, WEEKDAYS.get(m_ord.group(2), 0), int(m_ord.group(1)))
            else:
                day = int(bymonthday) if bymonthday else \
                    (cursor.day if kind == "date" else cursor.day)
                try:
                    occ_date = date(y, mo, day)
                except ValueError:
                    occ_date = None                 # e.g. the 31st of a short month
            if occ_date is not None:
                occ = occ_date if kind == "date" else datetime.combine(
                    occ_date, cursor.time())
                if (kind == "date" and occ >= cursor) or (kind == "dt" and occ >= cursor):
                    if ok(occ) == "stop":
                        return
                    yield occ
                if occ_date > win_hi:
                    return
            mo += interval
            y += (mo - 1) // 12
            mo = (mo - 1) % 12 + 1

    elif freq == "YEARLY":
        y = cursor.year
        while step_i < MAX_OCCURRENCES:
            step_i += 1
            try:
                occ_date = date(y, cursor.month, cursor.day)
            except ValueError:
                occ_date = None                     # Feb 29
            if occ_date is not None:
                occ = occ_date if kind == "date" else datetime.combine(
                    occ_date, cursor.time())
                if ok(occ) == "stop":
                    return
                yield occ
                if occ_date > win_hi:
                    return
            y += interval

    else:
        yield cursor
    _ = base_tz  # kept for clarity; conversion happens in the caller


# ------------------------------------------------------------------- import

def to_events(text: str, *, window_days_back: int = 60,
              window_days_forward: int = 400) -> tuple[list[dict], str, list[str]]:
    """Parse a feed and expand it into flat event dicts ready for the store:
    {external_id, title, description, location, start_utc, end_utc, all_day}.

    Returns (events, calendar_name, warnings).
    """
    raw, calname = parse(text)
    warnings: list[str] = []
    today = date.today()
    win_lo = today - timedelta(days=window_days_back)
    win_hi = today + timedelta(days=window_days_forward)

    # Group by UID: one master (optionally recurring) + instance overrides.
    by_uid: dict[str, dict] = {}
    for ev in raw:
        uid = (ev.get("UID") or ("", {}))[0] or f"no-uid-{id(ev)}"
        slot = by_uid.setdefault(uid, {"master": None, "overrides": []})
        if "RECURRENCE-ID" in ev:
            slot["overrides"].append(ev)
        else:
            slot["master"] = ev

    out: list[dict] = []
    seen_ids: set[str] = set()

    def push(uid, kind, start, end, ev):
        if kind == "date":
            start_s, end_s = _date_iso(start), _date_iso(end)
            all_day = True
        else:
            start_s, end_s = _to_utc_iso(start), _to_utc_iso(end)
            all_day = False
        ext = f"{uid}|{start_s}"
        if ext in seen_ids:
            return
        seen_ids.add(ext)
        out.append({
            "external_id": ext,
            "title": _unescape((ev.get("SUMMARY") or ("", {}))[0]).strip() or "(untitled)",
            "description": _unescape((ev.get("DESCRIPTION") or ("", {}))[0])[:4000],
            "location": _unescape((ev.get("LOCATION") or ("", {}))[0])[:500],
            "start_utc": start_s,
            "end_utc": end_s,
            "all_day": all_day,
        })

    for uid, slot in by_uid.items():
        master = slot["master"]
        overrides = slot["overrides"]

        # Index overrides by the instant they replace.
        override_at: dict[str, dict] = {}
        for ov in overrides:
            try:
                k, v = _parse_when(*ov["RECURRENCE-ID"])
                key = _date_iso(v) if k == "date" else _to_utc_iso(v)
                override_at[key] = ov
            except Exception:
                warnings.append(f"unparseable RECURRENCE-ID on {uid}")

        if master is None:
            # Orphan overrides (Outlook does this): emit them standalone.
            for ov in overrides:
                if (ov.get("STATUS") or ("", {}))[0].upper() == "CANCELLED":
                    continue
                try:
                    _emit_single(uid, ov, push)
                except Exception as e:
                    warnings.append(f"{uid}: {e}")
            continue

        if (master.get("STATUS") or ("", {}))[0].upper() == "CANCELLED":
            continue

        try:
            skind, sval = _parse_when(*master["DTSTART"])
        except Exception as e:
            warnings.append(f"{uid}: bad DTSTART ({e})")
            continue

        # Duration: DTEND > DURATION > RFC defaults.
        if "DTEND" in master:
            try:
                ekind, eval_ = _parse_when(*master["DTEND"])
                dur = (eval_ - sval) if skind == ekind else None
            except Exception:
                dur = None
        elif "DURATION" in master:
            dur = _parse_duration(master["DURATION"][0])
        else:
            dur = None
        if dur is None:
            dur = timedelta(days=1) if skind == "date" else timedelta(0)

        rrule = _parse_rrule(master["RRULE"][0]) if "RRULE" in master else None

        # EXDATEs, normalised to the same keying as overrides.
        exdates: set[str] = set()
        for v, params in master.get("EXDATE", []):
            try:
                k, val = _parse_when(v, params)
                exdates.add(_date_iso(val) if k == "date" else _to_utc_iso(val))
            except Exception:
                warnings.append(f"unparseable EXDATE on {uid}")

        if not rrule:
            key = _date_iso(sval) if skind == "date" else _to_utc_iso(sval)
            ov = override_at.get(key)
            src = ov or master
            if (src.get("STATUS") or ("", {}))[0].upper() != "CANCELLED" and key not in exdates:
                if ov:
                    _emit_single(uid, ov, push)
                else:
                    push(uid, skind, sval, sval + dur, master)
            # Any overrides that didn't match still represent real meetings.
            for k2, ov2 in override_at.items():
                if k2 != key and (ov2.get("STATUS") or ("", {}))[0].upper() != "CANCELLED":
                    try:
                        _emit_single(uid, ov2, push)
                    except Exception as e:
                        warnings.append(f"{uid}: {e}")
            continue

        tzinfo = getattr(sval, "tzinfo", None)
        for occ in _expand_rrule(skind, sval, rrule, win_lo, win_hi):
            if skind == "date":
                occ_start = occ
                key = _date_iso(occ)
                in_window = win_lo <= occ <= win_hi
            else:
                occ_start = occ.replace(tzinfo=tzinfo)
                key = _to_utc_iso(occ_start)
                in_window = win_lo <= occ_start.date() <= win_hi

            if key in exdates:
                continue
            ov = override_at.pop(key, None)
            if ov is not None:
                if (ov.get("STATUS") or ("", {}))[0].upper() != "CANCELLED":
                    try:
                        _emit_single(uid, ov, push)
                    except Exception as e:
                        warnings.append(f"{uid}: {e}")
                continue
            if in_window:
                push(uid, skind, occ_start, occ_start + dur, master)

        # Overrides moved OUTSIDE the original series (rescheduled instances).
        for ov in override_at.values():
            if (ov.get("STATUS") or ("", {}))[0].upper() != "CANCELLED":
                try:
                    _emit_single(uid, ov, push)
                except Exception as e:
                    warnings.append(f"{uid}: {e}")

    return out, calname, warnings


def _emit_single(uid, ev, push):
    kind, start = _parse_when(*ev["DTSTART"])
    if "DTEND" in ev:
        ekind, end = _parse_when(*ev["DTEND"])
        if ekind != kind:
            end = start + (timedelta(days=1) if kind == "date" else timedelta(0))
    elif "DURATION" in ev:
        end = start + _parse_duration(ev["DURATION"][0])
    else:
        end = start + (timedelta(days=1) if kind == "date" else timedelta(0))
    push(uid, kind, start, end, ev)
