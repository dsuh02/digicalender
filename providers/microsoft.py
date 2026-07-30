"""
Microsoft / Outlook Calendar adapter (Graph API) — SCAFFOLD, not yet connected.

What's left to make this live:

1. Azure portal → Entra ID → App registrations → new registration.
   Supported account types: "Personal Microsoft accounts" if this is an
   @outlook.com / @hotmail.com calendar; add work/school if you also want the
   Persist account.
2. Authentication → Allow public client flows = **Yes** (needed for device code).
3. API permissions → Microsoft Graph → delegated → Calendars.ReadWrite,
   offline_access.
4. Put the client id (and tenant, or "consumers") in config.json.
5. Implement _device_authorize() and the calls marked TODO below.

Like Google, use the **device code flow** — this box is a keyboard-less wall
display, so redirect-based OAuth is awkward.

API shape (Graph v1.0):
  list:   GET    https://graph.microsoft.com/v1.0/me/calendarView
                 ?startDateTime=&endDateTime=
  delta:  GET    https://graph.microsoft.com/v1.0/me/calendarView/delta
  create: POST   https://graph.microsoft.com/v1.0/me/events
  patch:  PATCH  https://graph.microsoft.com/v1.0/me/events/{id}
  delete: DELETE https://graph.microsoft.com/v1.0/me/events/{id}

Graph's delta endpoint returns @odata.deltaLink — store it in
accounts.sync_token for cheap incremental pulls.
"""

from __future__ import annotations

from .base import NotConfiguredProvider

MS_DEVICE_CODE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"
MS_TOKEN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_API = "https://graph.microsoft.com/v1.0"
SCOPES = "offline_access Calendars.ReadWrite"


def to_local_event(m: dict, account_id: str, color: str | None) -> dict:
    """Map a Graph event onto the local schema.

    Graph returns naive ISO strings plus a separate timeZone field; when
    isAllDay is set the times are midnight boundaries in that zone.
    """
    start = m.get("start", {})
    end = m.get("end", {})
    all_day = bool(m.get("isAllDay"))

    def _z(v: dict) -> str:
        dt = v.get("dateTime", "")
        if not dt:
            return ""
        # Graph omits the trailing Z even when timeZone is UTC.
        return dt if dt.endswith("Z") else dt.split(".")[0] + "Z"

    return {
        "provider": "microsoft",
        "account_id": account_id,
        "calendar_id": m.get("calendar", {}).get("id"),
        "external_id": m.get("id"),
        "etag": m.get("@odata.etag"),
        "title": m.get("subject") or "(no title)",
        "description": (m.get("bodyPreview") or "")[:2000],
        "location": (m.get("location") or {}).get("displayName", "") or "",
        "start_utc": _z(start),
        "end_utc": _z(end),
        "all_day": all_day,
        "color": color,
        "rrule": None,  # Graph models recurrence as an object, not an RRULE string
    }


class MicrosoftProvider(NotConfiguredProvider):
    name = "microsoft"
    label = "Microsoft / Outlook"
    configured = False  # flip to True once credentials + methods are in place

    # TODO: _device_authorize()  -> device-code flow, store refresh token
    # TODO: pull()               -> calendarView/delta, upsert via store
    # TODO: push()               -> POST/PATCH/DELETE for events where dirty = 1
