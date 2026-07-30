"""
Google Calendar adapter — SCAFFOLD, not yet connected.

What's left to make this live:

1. Google Cloud console → new project → enable "Google Calendar API".
2. Create OAuth 2.0 credentials of type **TV and Limited Input device**. That
   flow matters here: this box is a wall display with no keyboard, so the
   normal redirect-to-localhost flow is painful. The device flow shows a short
   code on screen that you type in on your phone.
3. Put the client id/secret in config.json (see config.example.json).
4. Implement _device_authorize() and the two API calls marked TODO below.

Scopes needed: https://www.googleapis.com/auth/calendar

API shape (v3):
  list:   GET  https://www.googleapis.com/calendar/v3/calendars/{calId}/events
               ?timeMin=&timeMax=&singleEvents=true&syncToken=
  insert: POST https://www.googleapis.com/calendar/v3/calendars/{calId}/events
  patch:  PATCH .../events/{eventId}
  delete: DELETE .../events/{eventId}

Google returns incremental changes via `nextSyncToken`; store it in
accounts.sync_token so later pulls are cheap deltas rather than full scans.

Everything here uses urllib so the app keeps its zero-dependency property.
"""

from __future__ import annotations

from .base import NotConfiguredProvider

GOOGLE_AUTH_DEVICE = "https://oauth2.googleapis.com/device/code"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_API = "https://www.googleapis.com/calendar/v3"
SCOPES = "https://www.googleapis.com/auth/calendar"


def to_local_event(g: dict, account_id: str, color: str | None) -> dict:
    """Map a Google event resource onto the local schema.

    Written now because it's the fiddly part and it's the same either way:
    Google uses {'date': 'YYYY-MM-DD'} for all-day events and
    {'dateTime': ISO8601, 'timeZone': ...} for timed ones.
    """
    start = g.get("start", {})
    end = g.get("end", {})
    all_day = "date" in start
    if all_day:
        start_utc = f"{start['date']}T00:00:00Z"
        end_utc = f"{end.get('date', start['date'])}T00:00:00Z"
    else:
        start_utc = start.get("dateTime", "")
        end_utc = end.get("dateTime", "")
    return {
        "provider": "google",
        "account_id": account_id,
        "calendar_id": g.get("organizer", {}).get("email"),
        "external_id": g.get("id"),
        "etag": g.get("etag"),
        "title": g.get("summary") or "(no title)",
        "description": g.get("description", "") or "",
        "location": g.get("location", "") or "",
        "start_utc": start_utc,
        "end_utc": end_utc,
        "all_day": all_day,
        "color": color,
        "rrule": (g.get("recurrence") or [None])[0],
    }


class GoogleProvider(NotConfiguredProvider):
    name = "google"
    label = "Google Calendar"
    configured = False  # flip to True once credentials + methods are in place

    # TODO: _device_authorize()  -> device-code flow, store refresh token
    # TODO: pull()               -> GET events with syncToken, upsert via store
    # TODO: push()               -> POST/PATCH/DELETE for events where dirty = 1
