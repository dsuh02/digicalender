#!/usr/bin/env python3
"""
DigiCalender — touch wall hub.

Stdlib only apart from psycopg (installed from apt as python3-psycopg, not pip),
so there is still no build step and nothing to compile on the display host.

  python3 server.py [--host 0.0.0.0] [--port 8080]
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import re
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import alarms as alarm_engine
import devices as device_registry
import feeds
import finance
import gemini
import hub
import ics
import plaid
import projection
import providers
import spotify as spotify_api
import store
import weather

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

# Gallery sets live here, one folder per set. Gitignored: the images are the
# user's, not the app's — they outlive widgets, sets, even the repo.
GALLERY_DIR = os.path.join(HERE, "galleries")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
MAX_IMAGE_BYTES = 64 * 1024 * 1024

ID_RE = r"([A-Za-z0-9_-]{1,64})"


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "set"
    return s[:60]


def _safe_filename(name: str) -> str:
    base = os.path.basename(name or "image")
    stem, ext = os.path.splitext(base)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "image"
    return (stem[:80] + ext.lower())


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _feed_view(acct: dict) -> dict:
    """The shape the UI works with — flattens the token_json internals."""
    cfg = acct.get("token_json") or {}
    return {
        "id": acct["id"],
        "name": acct.get("display_name") or "Calendar",
        "color": acct.get("color") or "",
        "url": cfg.get("source_url") or cfg.get("url", ""),
        "fetch_url": cfg.get("url", ""),
        "visible": bool(acct.get("visible", True)),
        "enabled": bool(acct.get("enabled", True)),
        "exclude": cfg.get("exclude", ""),
        "last_sync": acct.get("last_sync"),
        "status": cfg.get("last_status", ""),
        "count": cfg.get("last_count"),
    }


# --------------------------------------------------------------- validation

def _norm_iso(value, field: str) -> str:
    """Accept anything JS's toISOString() produces; store a canonical Z form."""
    if not isinstance(value, str) or not value:
        raise ApiError(400, f"{field} is required")
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise ApiError(400, f"{field} is not a valid ISO-8601 datetime: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_event(body: dict, *, partial: bool = False) -> dict:
    out: dict = {}
    if not partial or "title" in body:
        title = (body.get("title") or "").strip()
        if not title:
            raise ApiError(400, "title is required")
        out["title"] = title[:500]
    for f in ("description", "location"):
        if f in body:
            out[f] = (body.get(f) or "").strip()[:4000]
    if not partial or "start_utc" in body:
        out["start_utc"] = _norm_iso(body.get("start_utc"), "start_utc")
    if not partial or "end_utc" in body:
        out["end_utc"] = _norm_iso(body.get("end_utc"), "end_utc")
    if "all_day" in body:
        out["all_day"] = bool(body["all_day"])
    for f in ("color", "rrule", "calendar_id", "account_id", "provider"):
        if f in body and body[f] is not None:
            out[f] = str(body[f])[:200]
    if "start_utc" in out and "end_utc" in out and out["end_utc"] < out["start_utc"]:
        raise ApiError(400, "end_utc must not be before start_utc")
    return out


def _int(body: dict, key: str, lo: int, hi: int, default: int | None = None) -> int:
    v = body.get(key, default)
    if v is None:
        raise ApiError(400, f"{key} is required")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"{key} must be a whole number")
    if not lo <= n <= hi:
        raise ApiError(400, f"{key} must be between {lo} and {hi}")
    return n


def _alarm_body(b: dict, partial: bool = False) -> dict:
    out: dict = {}
    if "at_time" in b or not partial:
        t = str(b.get("at_time") or "").strip()
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            raise ApiError(400, "at_time must be HH:MM in 24-hour time")
        out["at_time"] = t
    if "days" in b:
        days = b.get("days") or []
        if not isinstance(days, list):
            raise ApiError(400, "days must be a list of weekday numbers (Monday = 0)")
        out["days"] = sorted({int(d) for d in days if 0 <= int(d) <= 6})
    for f, cap in (("name", 60), ("app_id", 16), ("device_name", 80)):
        if f in b:
            out[f] = str(b.get(f) or "")[:cap]
    if "device_id" in b:
        out["device_id"] = b["device_id"] or None
    if "wait_seconds" in b:
        out["wait_seconds"] = _int(b, "wait_seconds", 0, 120)
    if "volume" in b:
        # NULL is meaningful: leave the speaker wherever it was.
        out["volume"] = None if b["volume"] in (None, "") else _int(b, "volume", 0, 100)
    if "spotify_uri" in b:
        try:
            out["spotify_uri"] = alarm_engine.validate_uri(str(b.get("spotify_uri") or ""))
        except ValueError as e:
            raise ApiError(400, str(e))
    for f in ("enabled", "shuffle"):
        if f in b:
            out[f] = bool(b[f])
    return out


def validate_geometry(body: dict, partial: bool = False) -> dict:
    out = {}
    for key in ("x", "y", "w", "h"):
        if key in body or not partial:
            lo = 1 if key in ("w", "h") else 0
            out[key] = _int(body, key, lo, 512)
    return out


# ------------------------------------------------------------------ handler

class Handler(BaseHTTPRequestHandler):
    server_version = "DigiCalender"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ------------------------------------------------------------ plumbing

    def _send(self, status, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # App assets must never be stale; gallery images opt into caching by
        # supplying their own Cache-Control (duplicating the header would make
        # browser behaviour a coin flip).
        if not (extra and "Cache-Control" in extra):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > 2_000_000:
            raise ApiError(413, "request body too large")
        try:
            return json.loads(self.rfile.read(n).decode())
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "body is not valid JSON")

    # ------------------------------------------------------------- routing

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")

    def do_PUT(self):
        self._route("PATCH")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/api/stream":
                return self._stream()
            if path.startswith("/api"):
                return self._api(method, path, parse_qs(parsed.query))
            if method == "GET":
                return self._static(path)
            raise ApiError(405, "method not allowed")
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "internal server error"})

    # ----------------------------------------------------------------- SSE

    def _stream(self):
        """Server-sent events. One long-lived response per client; the
        broadcaster drops any client that stops draining its queue."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        qq = hub.bus.subscribe()
        try:
            self.wfile.write(hub.sse_format("hello", {"states": hub.snapshot()}))
            self.wfile.flush()
            while True:
                try:
                    event, data = qq.get(timeout=20)
                    self.wfile.write(hub.sse_format(event, data))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.bus.unsubscribe(qq)

    # ----------------------------------------------------------------- API

    def _api(self, method, path, qs):
        # ---- meta
        if path == "/api/health":
            return self._json(200, {"ok": True, "service": "digicalender",
                                    "time_utc": store.now_iso(),
                                    "sse_clients": hub.bus.count})
        if path == "/api/providers":
            return self._json(200, {"providers": providers.describe()})
        if path == "/api/accounts":
            return self._json(200, {"accounts": store.list_accounts()})
        if path == "/api/sync" and method == "POST":
            results = [{"provider": "ics", **r} for r in feeds.sync_all()]
            for acct in store.list_accounts():
                if not acct["enabled"] or acct["provider"] == "ics":
                    continue
                try:
                    results.append(providers.get(acct["provider"], acct).sync().as_dict())
                except KeyError as e:
                    results.append({"ok": False, "message": str(e)})
            if any(r.get("changed") for r in results):
                hub.bus.publish("events_changed", {})
            if not results:
                results.append({"provider": "local", "ok": True,
                                "message": "Local calendar only — no accounts connected yet."})
            return self._json(200, {"results": results})

        # ---- calendar feed subscriptions
        if path == "/api/feeds":
            if method == "GET":
                return self._json(200, {"feeds": [_feed_view(a) for a in
                                                  store.list_accounts(provider="ics")]})
            if method == "POST":
                b = self._body()
                raw_url = (b.get("url") or "").strip()
                if not raw_url:
                    raise ApiError(400, "url is required")
                fetch_url, note = feeds.normalize_url(raw_url)
                # Validate BEFORE creating anything — a feed that can't be
                # fetched right now would only ever be a zombie row.
                text, etag, err = ics.fetch(fetch_url)
                if err:
                    raise ApiError(400, err + (f" ({note})" if note else ""))
                try:
                    events, calname, warnings = ics.to_events(text)
                except Exception as e:
                    raise ApiError(400, f"that URL fetched, but is not a usable calendar: {e}")
                name = (b.get("name") or "").strip() or calname or "Calendar"
                cfg0 = {"url": fetch_url, "source_url": raw_url,
                        "exclude": (b.get("exclude") or "").strip()[:400]}
                events, dropped = feeds.apply_exclude(events, cfg0)
                cfg0["last_status"] = f"ok: {len(events)} events" + \
                    (f" · {dropped} filtered" if dropped else "")
                cfg0["last_count"] = len(events)
                acct = store.create_account({
                    "provider": "ics", "display_name": name[:80],
                    "color": (b.get("color") or "")[:20],
                    "token_json": cfg0,
                })
                for ev in events:
                    ev["color"] = acct.get("color") or None
                store.replace_feed_events(acct["id"], events)
                store.update_account(acct["id"], {"sync_token": etag,
                                                  "last_sync": store.now_iso()})
                hub.bus.publish("events_changed", {})
                return self._json(201, {"feed": _feed_view(store.get_account(acct["id"])),
                                        "imported": len(events), "note": note,
                                        "warnings": len(warnings)})
            raise ApiError(405, "method not allowed")

        if path == "/api/feeds/sync" and method == "POST":
            results = feeds.sync_all()
            if any(r.get("changed") for r in results):
                hub.bus.publish("events_changed", {})
            return self._json(200, {"results": results})

        m = re.fullmatch(rf"/api/feeds/{ID_RE}/sync", path)
        if m and method == "POST":
            acct = store.get_account(m.group(1))
            if not acct or acct["provider"] != "ics":
                raise ApiError(404, "feed not found")
            res = feeds.sync_account(acct)
            if res.get("changed"):
                hub.bus.publish("events_changed", {})
            return self._json(200 if res["ok"] else 502, res)

        m = re.fullmatch(rf"/api/feeds/{ID_RE}", path)
        if m:
            aid = m.group(1)
            acct = store.get_account(aid)
            if not acct or acct["provider"] != "ics":
                raise ApiError(404, "feed not found")
            if method == "PATCH":
                b = self._body()
                data = {}
                needs_sync = False
                if "name" in b:
                    data["display_name"] = str(b["name"])[:80]
                if "color" in b:
                    data["color"] = str(b["color"] or "")[:20]
                for f in ("visible", "enabled"):
                    if f in b:
                        data[f] = bool(b[f])
                cfg = dict(acct.get("token_json") or {})
                if "url" in b and str(b["url"]).strip():
                    fetch_url, _ = feeds.normalize_url(str(b["url"]).strip())
                    cfg["url"] = fetch_url
                    cfg["source_url"] = str(b["url"]).strip()
                    needs_sync = True
                if "exclude" in b:
                    cfg["exclude"] = str(b["exclude"] or "").strip()[:400]
                    needs_sync = True               # filter applies at import
                if needs_sync:
                    data["token_json"] = cfg
                    data["sync_token"] = None       # force a refetch past the ETag
                acct = store.update_account(aid, data)
                if needs_sync:
                    feeds.sync_account(acct)
                    acct = store.get_account(aid)
                # Visibility and colour change what every calendar shows.
                hub.bus.publish("events_changed", {})
                return self._json(200, {"feed": _feed_view(acct)})
            if method == "DELETE":
                store.delete_account(aid)
                hub.bus.publish("events_changed", {})
                return self._json(200, {"ok": True})
            raise ApiError(405, "method not allowed")

        # ---- events
        if path == "/api/events":
            if method == "GET":
                start = (qs.get("start") or [""])[0]
                end = (qs.get("end") or [""])[0]
                if not start or not end:
                    raise ApiError(400, "start and end query params are required")
                return self._json(200, {"events": store.list_events(
                    _norm_iso(start, "start"), _norm_iso(end, "end"))})
            if method == "POST":
                ev = store.create_event(validate_event(self._body()))
                hub.bus.publish("events_changed", {"uid": ev["uid"]})
                return self._json(201, {"event": ev})
            raise ApiError(405, "method not allowed")

        m = re.fullmatch(rf"/api/events/{ID_RE}", path)
        if m:
            uid = m.group(1)
            if method == "GET":
                ev = store.get_event(uid)
                if not ev or ev["deleted"]:
                    raise ApiError(404, "event not found")
                return self._json(200, {"event": ev})
            if method == "PATCH":
                ev = store.update_event(uid, validate_event(self._body(), partial=True))
                if not ev:
                    raise ApiError(404, "event not found")
                hub.bus.publish("events_changed", {"uid": uid})
                return self._json(200, {"event": ev})
            if method == "DELETE":
                if not store.delete_event(uid):
                    raise ApiError(404, "event not found")
                hub.bus.publish("events_changed", {"uid": uid})
                return self._json(200, {"ok": True})
            raise ApiError(405, "method not allowed")

        # ---- dashboard (pages + widgets in one round trip)
        if path == "/api/dashboard" and method == "GET":
            pages = store.list_pages()
            by_page: dict[str, list] = {p["id"]: [] for p in pages}
            for w in store.list_widgets():
                by_page.setdefault(w["page_id"], []).append(w)
            for p in pages:
                p["widgets"] = by_page.get(p["id"], [])
            return self._json(200, {"pages": pages, "settings": store.all_settings()})

        if path == "/api/pages":
            if method == "GET":
                return self._json(200, {"pages": store.list_pages()})
            if method == "POST":
                b = self._body()
                owner = str(b.get("person_id") or "") or None
                if owner and not store.get_person(owner):
                    raise ApiError(400, "person_id does not exist")
                pid = store.create_page((b.get("name") or "Page").strip()[:60],
                                        _int(b, "position", 0, 100, 0),
                                        _int(b, "cols", 8, 200, 48),
                                        _int(b, "rows", 8, 200, 32),
                                        owner)
                return self._json(201, {"page": store.get_page(pid)})
            raise ApiError(405, "method not allowed")

        m = re.fullmatch(rf"/api/pages/{ID_RE}", path)
        if m:
            pid = m.group(1)
            if method == "PATCH":
                b = self._body()
                data = {}
                if "name" in b:
                    data["name"] = str(b["name"])[:60]
                for f, lo, hi in (("position", 0, 100), ("cols", 8, 200), ("rows", 8, 200)):
                    if f in b:
                        data[f] = _int(b, f, lo, hi)
                if "person_id" in b:
                    owner = str(b["person_id"] or "") or None
                    if owner and not store.get_person(owner):
                        raise ApiError(400, "person_id does not exist")
                    data["person_id"] = owner
                page = store.update_page(pid, data)
                if not page:
                    raise ApiError(404, "page not found")
                return self._json(200, {"page": page})
            if method == "DELETE":
                if len(store.list_pages()) <= 1:
                    raise ApiError(400, "the last page cannot be deleted")
                if not store.delete_page(pid):
                    raise ApiError(404, "page not found")
                return self._json(200, {"ok": True})
            raise ApiError(405, "method not allowed")

        # ---- widgets
        if path == "/api/widgets" and method == "POST":
            b = self._body()
            page_id = str(b.get("page_id") or "")
            if not store.get_page(page_id):
                raise ApiError(400, "page_id does not exist")
            wtype = str(b.get("type") or "").strip()
            if not re.fullmatch(r"[a-z0-9_]{1,40}", wtype):
                raise ApiError(400, "invalid widget type")
            g = validate_geometry(b)
            w = store.create_widget(page_id, wtype, g["x"], g["y"], g["w"], g["h"],
                                    b.get("settings") or {})
            hub.bus.publish("layout_changed", {"page_id": page_id})
            return self._json(201, {"widget": w})

        if path == "/api/widgets/layout" and method == "POST":
            items = self._body().get("widgets") or []
            if not isinstance(items, list):
                raise ApiError(400, "widgets must be a list")
            clean = []
            for it in items[:500]:
                g = validate_geometry(it)
                g["id"] = str(it.get("id", ""))
                clean.append(g)
            n = store.bulk_update_widgets(clean)
            hub.bus.publish("layout_changed", {"count": n})
            return self._json(200, {"updated": n})

        m = re.fullmatch(rf"/api/widgets/{ID_RE}", path)
        if m:
            wid = m.group(1)
            if method == "PATCH":
                b = self._body()
                data = validate_geometry(b, partial=True)
                if "settings" in b:
                    if not isinstance(b["settings"], dict):
                        raise ApiError(400, "settings must be an object")
                    data["settings"] = b["settings"]
                if "page_id" in b:
                    if not store.get_page(str(b["page_id"])):
                        raise ApiError(400, "page_id does not exist")
                    data["page_id"] = str(b["page_id"])
                w = store.update_widget(wid, data)
                if not w:
                    raise ApiError(404, "widget not found")
                hub.bus.publish("layout_changed", {"widget_id": wid})
                return self._json(200, {"widget": w})
            if method == "DELETE":
                if not store.delete_widget(wid):
                    raise ApiError(404, "widget not found")
                hub.bus.publish("layout_changed", {"widget_id": wid})
                return self._json(200, {"ok": True})
            raise ApiError(405, "method not allowed")

        # ---- todos
        if path == "/api/todos":
            if method == "GET":
                lst = (qs.get("list") or [None])[0]
                inc = (qs.get("include_done") or ["1"])[0] != "0"
                return self._json(200, {"todos": store.list_todos(lst, inc)})
            if method == "POST":
                b = self._body()
                title = (b.get("title") or "").strip()
                if not title:
                    raise ApiError(400, "title is required")
                t = store.create_todo({
                    "title": title[:300],
                    "list_name": (b.get("list_name") or "Home")[:60],
                    "notes": (b.get("notes") or "")[:2000],
                    "priority": _int(b, "priority", 0, 3, 0),
                    "due_utc": _norm_iso(b["due_utc"], "due_utc") if b.get("due_utc") else None,
                })
                hub.bus.publish("todos_changed", {"id": t["id"]})
                return self._json(201, {"todo": t})
            raise ApiError(405, "method not allowed")

        m = re.fullmatch(rf"/api/todos/{ID_RE}", path)
        if m:
            tid = m.group(1)
            if method == "PATCH":
                b = self._body()
                data = {}
                for f, cap in (("title", 300), ("notes", 2000), ("list_name", 60)):
                    if f in b:
                        data[f] = str(b[f])[:cap]
                if "done" in b:
                    data["done"] = bool(b["done"])
                if "priority" in b:
                    data["priority"] = _int(b, "priority", 0, 3)
                if "due_utc" in b:
                    data["due_utc"] = _norm_iso(b["due_utc"], "due_utc") if b["due_utc"] else None
                t = store.update_todo(tid, data)
                if not t:
                    raise ApiError(404, "todo not found")
                hub.bus.publish("todos_changed", {"id": tid})
                return self._json(200, {"todo": t})
            if method == "DELETE":
                if not store.delete_todo(tid):
                    raise ApiError(404, "todo not found")
                hub.bus.publish("todos_changed", {"id": tid})
                return self._json(200, {"ok": True})
            raise ApiError(405, "method not allowed")

        # ---- notifications
        if path == "/api/notifications":
            if method == "GET":
                limit = min(int((qs.get("limit") or ["50"])[0] or 50), 200)
                unread = (qs.get("unread") or ["0"])[0] == "1"
                return self._json(200, {"notifications": store.list_notifications(limit, unread)})
            if method == "POST":
                b = self._body()
                title = (b.get("title") or "").strip()
                if not title:
                    raise ApiError(400, "title is required")
                n = store.push_notification(title[:200], (b.get("body") or "")[:1000],
                                            (b.get("kind") or "info")[:20],
                                            (b.get("source") or "manual")[:60],
                                            b.get("dedupe_key"))
                if n:
                    hub.bus.publish("notification", n)
                return self._json(201, {"notification": n})
            if method == "DELETE":
                return self._json(200, {"cleared": store.clear_notifications()})
            raise ApiError(405, "method not allowed")

        m = re.fullmatch(rf"/api/notifications/{ID_RE}", path)
        if m:
            nid = m.group(1)
            if method == "PATCH":
                if not store.mark_notification(nid, bool(self._body().get("read", True))):
                    raise ApiError(404, "notification not found")
                return self._json(200, {"ok": True})
            if method == "DELETE":
                if not store.delete_notification(nid):
                    raise ApiError(404, "notification not found")
                return self._json(200, {"ok": True})
            raise ApiError(405, "method not allowed")

        # ---- devices
        if path == "/api/device-kinds":
            return self._json(200, {"kinds": device_registry.describe()})

        if path == "/api/devices":
            if method == "GET":
                devs = store.list_devices()
                live = hub.snapshot()
                for d in devs:
                    d["state"] = live.get(d["id"], d.get("last_state") or {})
                return self._json(200, {"devices": devs})
            if method == "POST":
                b = self._body()
                kind = str(b.get("kind") or "")
                if kind not in device_registry.REGISTRY:
                    raise ApiError(400, f"unknown device kind: {kind}")
                name = (b.get("name") or "").strip()
                if not name:
                    raise ApiError(400, "name is required")
                cfg = b.get("config") or {}
                if not isinstance(cfg, dict):
                    raise ApiError(400, "config must be an object")
                d = store.create_device({"name": name[:80], "kind": kind,
                                         "room": (b.get("room") or "")[:60],
                                         "config": cfg})
                hub.bus.publish("devices_changed", {"id": d["id"]})
                return self._json(201, {"device": d})
            raise ApiError(405, "method not allowed")

        if path == "/api/devices/states":
            return self._json(200, {"states": hub.snapshot()})

        if path == "/api/discover" and method == "POST":
            from devices.discovery import discover_all
            return self._json(200, {"found": discover_all(
                include_samsung=bool(self._body().get("samsung", True)))})

        m = re.fullmatch(rf"/api/devices/{ID_RE}/command", path)
        if m and method == "POST":
            dev = store.get_device(m.group(1))
            if not dev:
                raise ApiError(404, "device not found")
            b = self._body()
            adapter = device_registry.adapter_for(dev)
            if adapter is None:
                raise ApiError(400, f"no adapter for kind {dev['kind']}")
            res = adapter.command(str(b.get("command") or ""), b.get("params") or {})
            # Samsung hands back a pairing token on first successful connect;
            # persist it so the on-screen prompt only ever appears once.
            if res.data.get("token"):
                cfg = dict(dev.get("config") or {})
                cfg["token"] = res.data["token"]
                store.update_device(dev["id"], {"config": cfg})
                dev = store.get_device(dev["id"])
            if res.ok:
                hub.poll_device_now(dev)     # reflect the change immediately
            return self._json(200 if res.ok else 502, res.as_dict())

        m = re.fullmatch(rf"/api/devices/{ID_RE}/state", path)
        if m and method == "GET":
            dev = store.get_device(m.group(1))
            if not dev:
                raise ApiError(404, "device not found")
            return self._json(200, {"state": hub.poll_device_now(dev)})

        m = re.fullmatch(rf"/api/devices/{ID_RE}/apps", path)
        if m and method == "GET":
            dev = store.get_device(m.group(1))
            if not dev:
                raise ApiError(404, "device not found")
            adapter = device_registry.adapter_for(dev)
            if adapter is None or not hasattr(adapter, "list_apps"):
                raise ApiError(400, "this device has no app list")
            apps, err = adapter.list_apps()
            return self._json(200, {"apps": apps, "notice": err})

        m = re.fullmatch(rf"/api/devices/{ID_RE}/icon/([A-Za-z0-9]{{1,20}})", path)
        if m and method == "GET":
            dev = store.get_device(m.group(1))
            if not dev:
                raise ApiError(404, "device not found")
            adapter = device_registry.adapter_for(dev)
            got = adapter.app_icon(m.group(2)) if hasattr(adapter, "app_icon") else None
            if not got:
                raise ApiError(404, "icon unavailable")
            body, ctype = got
            # Proxied so the kiosk page stays same-origin.
            return self._send(200, body, ctype)

        m = re.fullmatch(rf"/api/devices/{ID_RE}", path)
        if m:
            did = m.group(1)
            dev = store.get_device(did)
            if not dev:
                raise ApiError(404, "device not found")
            if method == "GET":
                dev["state"] = hub.state_for(did) or dev.get("last_state") or {}
                return self._json(200, {"device": dev})
            if method == "PATCH":
                b = self._body()
                data = {}
                for f, cap in (("name", 80), ("room", 60)):
                    if f in b:
                        data[f] = str(b[f])[:cap]
                if "config" in b:
                    if not isinstance(b["config"], dict):
                        raise ApiError(400, "config must be an object")
                    data["config"] = b["config"]
                if "enabled" in b:
                    data["enabled"] = bool(b["enabled"])
                d = store.update_device(did, data)
                hub.bus.publish("devices_changed", {"id": did})
                return self._json(200, {"device": d})
            if method == "DELETE":
                store.delete_device(did)
                hub.bus.publish("devices_changed", {"id": did})
                return self._json(200, {"ok": True})
            raise ApiError(405, "method not allowed")

        # ---- scenes
        if path == "/api/scenes":
            if method == "GET":
                return self._json(200, {"scenes": store.list_scenes()})
            if method == "POST":
                b = self._body()
                name = (b.get("name") or "").strip()
                if not name:
                    raise ApiError(400, "name is required")
                s = store.create_scene({"name": name[:80],
                                        "icon": (b.get("icon") or "sparkles")[:40],
                                        "color": (b.get("color") or "")[:20],
                                        "actions": b.get("actions") or []})
                return self._json(201, {"scene": s})
            raise ApiError(405, "method not allowed")

        m = re.fullmatch(rf"/api/scenes/{ID_RE}/run", path)
        if m and method == "POST":
            scene = store.get_scene(m.group(1))
            if not scene:
                raise ApiError(404, "scene not found")
            results = []
            for act in scene.get("actions", []):
                dev = store.get_device(str(act.get("device_id", "")))
                if not dev:
                    results.append({"ok": False, "message": "device missing"})
                    continue
                adapter = device_registry.adapter_for(dev)
                if adapter is None:
                    results.append({"ok": False, "message": f"no adapter for {dev['kind']}"})
                    continue
                r = adapter.command(str(act.get("command", "")), act.get("params") or {})
                results.append({"device": dev["name"], **r.as_dict()})
                if r.ok:
                    hub.poll_device_now(dev)
            return self._json(200, {"results": results,
                                    "ok": all(r.get("ok") for r in results)})

        m = re.fullmatch(rf"/api/scenes/{ID_RE}", path)
        if m:
            sid = m.group(1)
            if method == "PATCH":
                b = self._body()
                data = {}
                for f, cap in (("name", 80), ("icon", 40), ("color", 20)):
                    if f in b:
                        data[f] = str(b[f])[:cap]
                if "actions" in b:
                    if not isinstance(b["actions"], list):
                        raise ApiError(400, "actions must be a list")
                    data["actions"] = b["actions"]
                s = store.update_scene(sid, data)
                if not s:
                    raise ApiError(404, "scene not found")
                return self._json(200, {"scene": s})
            if method == "DELETE":
                if not store.delete_scene(sid):
                    raise ApiError(404, "scene not found")
                return self._json(200, {"ok": True})
            raise ApiError(405, "method not allowed")

        # ---- weather
        if path == "/api/weather" and method == "GET":
            try:
                lat = float((qs.get("lat") or ["37.7749"])[0])
                lon = float((qs.get("lon") or ["-122.4194"])[0])
            except ValueError:
                raise ApiError(400, "lat and lon must be numbers")
            units = (qs.get("units") or ["imperial"])[0]
            try:
                days = min(int((qs.get("days") or ["5"])[0] or 5), 7)
            except ValueError:
                days = 5
            data, err = weather.fetch(lat, lon, units, days)
            if data is None:
                raise ApiError(502, err)
            return self._json(200, {"weather": data, "notice": err})

        # ---- money
        if path == "/api/finance":
            if method == "GET":
                return self._json(200, {
                    # include_hidden: the widgets need to KNOW an account is
                    # hidden so the visibility sheet can list it and switch it
                    # back on. Filtering here would make hiding irreversible
                    # from the panel.
                    "accounts": store.list_finance_accounts(include_hidden=True),
                    "items": [{k: v for k, v in it.items() if k != "access_token"}
                              for it in store.list_finance_items()],
                    "kind_colors": finance.kind_colors(),
                    "summary": finance.summary(),
                    "configured": bool((store.get_setting("plaid_client_id") or "").strip()
                                       and (store.get_setting("plaid_secret") or "").strip()),
                    "env": plaid.env_name(),
                })
            raise ApiError(405, "method not allowed")

        if path == "/api/finance/networth" and method == "GET":
            try:
                days = min(int((qs.get("days") or ["180"])[0] or 180), 1095)
            except ValueError:
                days = 180
            return self._json(200, {"series": store.finance_networth_series(days)})



        # ----------------------------------------------------------------- ai
        if path == "/api/ai" and method == "GET":
            out = {"configured": gemini.configured(), "model": gemini.model_name(),
                   "models": []}
            if out["configured"]:
                try:
                    out["models"] = gemini.list_models()
                except gemini.GeminiError as e:
                    out["error"] = e.message
            return self._json(200, out)

        if path == "/api/ai/test" and method == "POST":
            try:
                return self._json(200, gemini.check())
            except gemini.GeminiError as e:
                raise ApiError(400 if e.status in (0, 400, 403) else e.status, e.message)

        if path == "/api/ai/ask" and method == "POST":
            b = self._body()
            prompt = str(b.get("prompt") or "").strip()
            if not prompt:
                raise ApiError(400, "prompt is required")
            try:
                text = gemini.generate(
                    prompt[:8000],
                    model=str(b.get("model") or ""),
                    system=str(b.get("system") or "")[:4000],
                    max_tokens=_int(b, "max_tokens", 64, 4096, 800),
                )
            except gemini.GeminiError as e:
                raise ApiError(400 if e.status in (0, 400, 403) else e.status, e.message)
            return self._json(200, {"text": text})

        # ------------------------------------------------------------ spotify
        if path == "/api/spotify" and method == "GET":
            out = {"configured": spotify_api.configured(),
                   "connected": spotify_api.connected(),
                   "redirect_uri": spotify_api.REDIRECT_URI, "user": None, "devices": []}
            if out["connected"]:
                try:
                    me = spotify_api.me()
                    out["user"] = {"name": me.get("display_name") or me.get("id"),
                                   "product": me.get("product")}
                    out["devices"] = [{"id": d.get("id"), "name": d.get("name"),
                                       "type": d.get("type"), "active": d.get("is_active")}
                                      for d in spotify_api.devices()]
                except spotify_api.SpotifyError as e:
                    out["error"] = e.message
            return self._json(200, out)

        if path == "/api/spotify/authorize" and method == "POST":
            try:
                return self._json(200, {"url": spotify_api.authorize_url()})
            except spotify_api.SpotifyError as e:
                raise ApiError(400, e.message)

        if path == "/api/spotify/complete" and method == "POST":
            try:
                code = spotify_api.code_from_redirect(str(self._body().get("redirect") or ""))
                spotify_api.exchange_code(code)
            except spotify_api.SpotifyError as e:
                raise ApiError(400, e.message)
            hub.bus.publish("alarms_changed", {})
            return self._json(200, {"connected": True})

        if path == "/api/spotify/disconnect" and method == "POST":
            spotify_api.disconnect()
            return self._json(200, {"connected": False})

        if path == "/api/spotify/search" and method == "GET":
            term = (qs.get("q") or [""])[0].strip()
            if not term:
                raise ApiError(400, "q is required")
            try:
                res = spotify_api.search(term)
            except spotify_api.SpotifyError as e:
                raise ApiError(400, e.message)
            out = []
            for kind in ("playlists", "albums", "artists", "tracks"):
                for it in ((res.get(kind) or {}).get("items") or [])[:5]:
                    if not it:
                        continue
                    if kind in ("albums", "tracks"):
                        by = ", ".join(a.get("name", "") for a in (it.get("artists") or []))
                    elif kind == "playlists":
                        by = (it.get("owner") or {}).get("display_name") or ""
                    else:
                        by = ""
                    out.append({"uri": it.get("uri"), "name": it.get("name"),
                                "kind": kind[:-1], "by": by})
            return self._json(200, {"results": out})

        # ------------------------------------------------------------- alarms
        if path == "/api/alarms":
            if method == "GET":
                return self._json(200, {"alarms": store.list_alarms(),
                                        "spotify_app_id": alarm_engine.SPOTIFY_ROKU_APP})
            if method == "POST":
                a = store.create_alarm(_alarm_body(self._body()))
                hub.bus.publish("alarms_changed", {})
                return self._json(201, {"alarm": a})
            raise ApiError(405, "method not allowed")

        m = re.fullmatch(rf"/api/alarms/{ID_RE}", path)
        if m:
            al = store.get_alarm(m.group(1))
            if not al:
                raise ApiError(404, "alarm not found")
            if method == "PATCH":
                a = store.update_alarm(al["id"], _alarm_body(self._body(), partial=True))
                hub.bus.publish("alarms_changed", {})
                return self._json(200, {"alarm": a})
            if method == "DELETE":
                store.delete_alarm(al["id"])
                hub.bus.publish("alarms_changed", {})
                return self._json(200, {"ok": True})
            raise ApiError(405, "method not allowed")

        m = re.fullmatch(rf"/api/alarms/{ID_RE}/run", path)
        if m and method == "POST":
            al = store.get_alarm(m.group(1))
            if not al:
                raise ApiError(404, "alarm not found")
            # Inline: it takes ~40s and the caller wants the step-by-step result,
            # which is the entire point of a test button.
            res = alarm_engine.run(al)
            store.mark_alarm_fired(al["id"], al.get("last_fired") or "", res["message"])
            hub.bus.publish("alarms_changed", {})
            return self._json(200, res)

        if path == "/api/finance/projection":
            if method == "GET":
                # Contributions-to-date costs a Plaid round trip per item, so it
                # is opt-in: the chart itself does not need it to draw.
                want = (qs.get("contributions") or ["0"])[0] in ("1", "true", "yes")
                return self._json(200, projection.project(include_contributions=want))
            if method == "POST":
                return self._json(200, {"config": projection.set_config(self._body())})
            raise ApiError(405, "method not allowed")

        if path == "/api/finance/kind-colors" and method == "POST":
            b = self._body()
            colors = b.get("colors")
            if not isinstance(colors, dict):
                raise ApiError(400, "colors must be an object of kind -> colour")
            finance.set_kind_colors(colors)
            hub.bus.publish("finance_changed", {})
            return self._json(200, {"kind_colors": finance.kind_colors()})

        if path == "/api/finance/insights" and method == "GET":
            try:
                months = min(int((qs.get("months") or ["6"])[0] or 6), 24)
            except ValueError:
                months = 6
            return self._json(200, finance.insights(months))

        if path == "/api/finance/transactions" and method == "GET":
            try:
                limit = min(int((qs.get("limit") or ["25"])[0] or 25), 200)
            except ValueError:
                limit = 25
            return self._json(200, {"transactions": store.finance_recent_transactions(limit)})

        if path == "/api/finance/sync" and method == "POST":
            results = finance.sync_all()
            n = finance.sync_bill_events()
            hub.bus.publish("finance_changed", {})
            hub.bus.publish("events_changed", {})
            return self._json(200, {"results": results, "bill_events": n})

        # Plaid Hosted Link: create a session, hand back a URL the user opens
        # on a device that has a keyboard.
        if path == "/api/finance/link" and method == "POST":
            try:
                d = plaid.create_hosted_link(user_id="digicalender-household")
            except plaid.PlaidError as e:
                raise ApiError(400, e.message + (f" [{e.code}]" if e.code else ""))
            url = d.get("hosted_link_url") or ""
            if not url:
                raise ApiError(502, "Plaid did not return a hosted link URL — check that "
                                    "Hosted Link is enabled for your Plaid account.")
            return self._json(200, {"link_token": d.get("link_token"), "url": url,
                                    "expiration": d.get("expiration")})

        # Polled by the UI while the user completes the flow elsewhere.
        if path == "/api/finance/link/poll" and method == "POST":
            token = str(self._body().get("link_token") or "")
            if not token:
                raise ApiError(400, "link_token is required")
            try:
                res = plaid.get_link_results(token)
            except plaid.PlaidError as e:
                raise ApiError(400, e.message)
            public_token, inst = plaid.public_token_from_results(res)
            if not public_token:
                return self._json(200, {"ready": False})
            try:
                ex = plaid.exchange(public_token)
            except plaid.PlaidError as e:
                raise ApiError(400, e.message)

            # /link/token/get keeps returning a completed session's public_token
            # until the link token expires, so this endpoint WILL be called again
            # after it has already succeeded — by a retry, a second tab, or two
            # polls overlapping because the sync below outlasts the poll interval.
            # Identity is Plaid's item_id: exchanging twice gives two different
            # access_tokens for one item, so the token cannot be the key.
            #
            # Do NOT call /item/remove on the redundant token. It removes the
            # ITEM, not the token, and would invalidate the copy we are keeping.
            existing = store.get_finance_item_by_item_id(ex.get("item_id"))
            if existing:
                return self._json(200, {"ready": True, "duplicate": True,
                                        "item": {"id": existing["id"],
                                                 "institution": existing.get("institution") or inst},
                                        "sync": {"ok": True, "message": "already linked"}})

            item = store.create_finance_item({
                "item_id": ex.get("item_id"), "access_token": ex.get("access_token"),
                "institution": inst,
            })
            out = finance.sync_item(store.get_finance_item(item["id"]))
            if out.get("ok"):
                # Balances first, then transactions — they attach to the account
                # rows sync_item just created.
                out["transactions"] = finance.sync_transactions(
                    store.get_finance_item(item["id"]))
            finance.sync_bill_events()
            hub.bus.publish("finance_changed", {})
            hub.bus.publish("events_changed", {})
            return self._json(200, {"ready": True, "item": {"id": item["id"],
                                                            "institution": out.get("institution") or inst},
                                    "sync": out})

        m = re.fullmatch(rf"/api/finance/items/{ID_RE}", path)
        if m and method == "DELETE":
            it = store.get_finance_item(m.group(1))
            if not it:
                raise ApiError(404, "item not found")
            try:
                if it.get("access_token"):
                    plaid.item_remove(it["access_token"])   # revoke at Plaid too
            except plaid.PlaidError:
                pass                                        # local removal still proceeds
            store.delete_finance_item(it["id"])
            finance.sync_bill_events()
            hub.bus.publish("finance_changed", {})
            hub.bus.publish("events_changed", {})
            return self._json(200, {"ok": True})

        if path == "/api/finance/accounts" and method == "POST":
            b = self._body()
            name = (b.get("name") or "").strip()
            if not name:
                raise ApiError(400, "name is required")
            a = store.create_finance_account({
                "name": name[:80],
                "institution": (b.get("institution") or "")[:80],
                "kind": (b.get("kind") or "other")[:20],
                "balance": float(b.get("balance") or 0),
                "credit_limit": b.get("credit_limit"),
                "apr": b.get("apr"),
                "min_payment": b.get("min_payment"),
                "due_day": _int(b, "due_day", 1, 31) if b.get("due_day") else None,
                "color": (b.get("color") or "")[:20],
            })
            finance.sync_bill_events()
            hub.bus.publish("finance_changed", {})
            hub.bus.publish("events_changed", {})
            return self._json(201, {"account": a})

        m = re.fullmatch(rf"/api/finance/accounts/{ID_RE}", path)
        if m:
            aid = m.group(1)
            acct = store.get_finance_account(aid)
            if not acct:
                raise ApiError(404, "account not found")
            if method == "GET":
                return self._json(200, {"account": acct,
                                        "history": store.finance_history(aid)})
            if method == "PATCH":
                b = self._body()
                data = {}
                for f, cap in (("name", 80), ("institution", 80), ("kind", 20), ("color", 20)):
                    if f in b:
                        data[f] = str(b[f] or "")[:cap]
                for f in ("balance", "credit_limit", "apr", "min_payment"):
                    if f in b:
                        data[f] = float(b[f]) if b[f] not in (None, "") else None
                if "due_day" in b:
                    data["due_day"] = _int(b, "due_day", 1, 31) if b["due_day"] else None
                if "hidden" in b:
                    data["hidden"] = bool(b["hidden"])
                a = store.update_finance_account(aid, data)
                finance.sync_bill_events()
                hub.bus.publish("finance_changed", {})
                hub.bus.publish("events_changed", {})
                return self._json(200, {"account": a})
            if method == "DELETE":
                store.delete_finance_account(aid)
                finance.sync_bill_events()
                hub.bus.publish("finance_changed", {})
                hub.bus.publish("events_changed", {})
                return self._json(200, {"ok": True})
            raise ApiError(405, "method not allowed")

        # ---- household members
        if path == "/api/people":
            if method == "GET":
                grace = hub.PRESENCE_GRACE_MIN * 60
                now = datetime.now(timezone.utc)
                out = []
                for p in store.list_people():
                    home = False
                    if p.get("last_seen"):
                        try:
                            seen = datetime.strptime(p["last_seen"], "%Y-%m-%dT%H:%M:%SZ") \
                                .replace(tzinfo=timezone.utc)
                            home = (now - seen).total_seconds() <= grace
                        except ValueError:
                            home = False
                    out.append({**p, "home": home})
                return self._json(200, {"people": out})
            if method == "POST":
                b = self._body()
                name = (b.get("name") or "").strip()
                if not name:
                    raise ApiError(400, "name is required")
                p = store.create_person({
                    "name": name[:60],
                    "greeting": (b.get("greeting") or "")[:200],
                    "color": (b.get("color") or "")[:20],
                    "avatar": (b.get("avatar") or "")[:8],
                    "macs": [str(m).lower().strip() for m in (b.get("macs") or [])][:10],
                })
                hub.bus.publish("people_changed", {})
                return self._json(201, {"person": p})
            raise ApiError(405, "method not allowed")

        if path == "/api/people/order" and method == "POST":
            ids = self._body().get("ids") or []
            if not isinstance(ids, list):
                raise ApiError(400, "ids must be a list")
            n = store.reorder_people([str(i) for i in ids][:100])
            hub.bus.publish("people_changed", {})
            return self._json(200, {"updated": n})

        # Every MAC currently visible on the LAN, so a person's device can be
        # picked from a list instead of typed from memory.
        if path == "/api/people/lan" and method == "GET":
            from devices.discovery import _neighbour_macs, _sweep_arp
            _sweep_arp()
            known = {}
            for p in store.list_people():
                for m in (p.get("macs") or []):
                    known[str(m).lower()] = p["name"]
            return self._json(200, {"hosts": [
                {"ip": ip, "mac": mac, "claimed_by": known.get(mac)}
                for ip, mac in sorted(_neighbour_macs(), key=lambda t: t[0])
            ]})

        m = re.fullmatch(rf"/api/people/{ID_RE}", path)
        if m:
            pid = m.group(1)
            if not store.get_person(pid):
                raise ApiError(404, "person not found")
            if method == "PATCH":
                b = self._body()
                data = {}
                for f, cap in (("name", 60), ("greeting", 200), ("color", 20), ("avatar", 8)):
                    if f in b:
                        data[f] = str(b[f] or "")[:cap]
                if "macs" in b:
                    if not isinstance(b["macs"], list):
                        raise ApiError(400, "macs must be a list")
                    data["macs"] = [str(x).lower().strip() for x in b["macs"]][:10]
                if "theme" in b:
                    data["theme"] = b["theme"] if isinstance(b["theme"], dict) else None
                p = store.update_person(pid, data)
                hub.bus.publish("people_changed", {})
                return self._json(200, {"person": p})
            if method == "DELETE":
                store.delete_person(pid)
                hub.bus.publish("people_changed", {})
                hub.bus.publish("layout_changed", {})
                return self._json(200, {"ok": True,
                                        "note": "their pages are now shared"})
            raise ApiError(405, "method not allowed")

        # ---- galleries
        if path == "/api/galleries":
            if method == "GET":
                return self._json(200, {"galleries": store.list_galleries()})
            if method == "POST":
                b = self._body()
                name = (b.get("name") or "").strip()[:80]
                if not name:
                    raise ApiError(400, "name is required")
                slug = _slugify(name)
                if any(g["dirname"] == slug for g in store.list_galleries()):
                    raise ApiError(400, f"a set named like “{slug}” already exists")
                folder = os.path.join(GALLERY_DIR, slug)
                os.makedirs(folder, exist_ok=True)
                g = store.create_gallery(name, slug)
                # A folder that already holds images (from a deleted set, or
                # dropped in over SSH) is adopted wholesale — files are truth.
                adopted = 0
                for fn in sorted(os.listdir(folder)):
                    if os.path.splitext(fn)[1].lower() in IMAGE_EXTS:
                        store.add_gallery_image(g["id"], fn)
                        adopted += 1
                hub.bus.publish("galleries_changed", {})
                return self._json(201, {"gallery": store.get_gallery(g["id"]),
                                        "adopted": adopted})
            raise ApiError(405, "method not allowed")

        if path == "/api/galleries/order" and method == "POST":
            ids = self._body().get("ids") or []
            if not isinstance(ids, list):
                raise ApiError(400, "ids must be a list")
            n = store.reorder_galleries([str(i) for i in ids][:200])
            hub.bus.publish("galleries_changed", {})
            return self._json(200, {"updated": n})

        m = re.fullmatch(rf"/api/galleries/{ID_RE}/images/order", path)
        if m and method == "POST":
            ids = self._body().get("ids") or []
            if not isinstance(ids, list):
                raise ApiError(400, "ids must be a list")
            n = store.reorder_gallery_images(m.group(1), [str(i) for i in ids][:2000])
            hub.bus.publish("galleries_changed", {})
            return self._json(200, {"updated": n})

        m = re.fullmatch(rf"/api/galleries/{ID_RE}/images", path)
        if m:
            g = store.get_gallery(m.group(1))
            if not g:
                raise ApiError(404, "gallery not found")
            if method == "GET":
                return self._json(200, {"images": store.list_gallery_images(g["id"])})
            if method == "POST":
                # Raw body upload: one request per file, filename in a header.
                # Multipart buys nothing here and costs a parser.
                from urllib.parse import unquote
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0:
                    raise ApiError(400, "empty upload")
                if n > MAX_IMAGE_BYTES:
                    raise ApiError(413, "image is larger than 64 MB")
                fn = _safe_filename(unquote(self.headers.get("X-Filename", "image.jpg")))
                ext = os.path.splitext(fn)[1].lower()
                if ext not in IMAGE_EXTS:
                    raise ApiError(400, f"unsupported type {ext or '(none)'} — "
                                        "jpg, png, webp, gif or avif")
                folder = os.path.join(GALLERY_DIR, g["dirname"])
                os.makedirs(folder, exist_ok=True)
                stem, _ = os.path.splitext(fn)
                dest = os.path.join(folder, fn)
                serial = 1
                while os.path.exists(dest):
                    serial += 1
                    fn = f"{stem}-{serial}{ext}"
                    dest = os.path.join(folder, fn)
                remaining = n
                with open(dest, "wb") as out:
                    while remaining > 0:
                        chunk = self.rfile.read(min(1 << 20, remaining))
                        if not chunk:
                            break
                        out.write(chunk)
                        remaining -= len(chunk)
                img = store.add_gallery_image(g["id"], fn)
                hub.bus.publish("galleries_changed", {})
                return self._json(201, {"image": img})
            raise ApiError(405, "method not allowed")

        m = re.fullmatch(rf"/api/galleries/{ID_RE}/images/{ID_RE}", path)
        if m and method == "DELETE":
            g = store.get_gallery(m.group(1))
            img = store.get_gallery_image(m.group(2))
            if not g or not img or img["gallery_id"] != g["id"]:
                raise ApiError(404, "image not found")
            # An explicit per-image delete IS a request to remove the file —
            # unlike deleting sets or widgets, which never touch disk.
            store.delete_gallery_image(img["id"])
            try:
                os.remove(os.path.join(GALLERY_DIR, g["dirname"], img["filename"]))
            except OSError:
                pass
            hub.bus.publish("galleries_changed", {})
            return self._json(200, {"ok": True})

        m = re.fullmatch(rf"/api/galleries/{ID_RE}", path)
        if m:
            gid = m.group(1)
            g = store.get_gallery(gid)
            if not g:
                raise ApiError(404, "gallery not found")
            if method == "PATCH":
                b = self._body()
                data = {}
                if "name" in b:
                    data["name"] = str(b["name"]).strip()[:80]
                if "position" in b:
                    data["position"] = _int(b, "position", 0, 500)
                g = store.update_gallery(gid, data)
                hub.bus.publish("galleries_changed", {})
                return self._json(200, {"gallery": g})
            if method == "DELETE":
                store.delete_gallery(gid)
                hub.bus.publish("galleries_changed", {})
                return self._json(200, {"ok": True,
                                        "note": "the folder and its images stay on disk"})
            raise ApiError(405, "method not allowed")

        m = re.fullmatch(rf"/api/gimg/{ID_RE}", path)
        if m and method == "GET":
            img = store.get_gallery_image(m.group(1))
            g = img and store.get_gallery(img["gallery_id"])
            if not img or not g:
                raise ApiError(404, "image not found")
            full = os.path.normpath(os.path.join(GALLERY_DIR, g["dirname"], img["filename"]))
            if not full.startswith(GALLERY_DIR + os.sep) or not os.path.isfile(full):
                raise ApiError(404, "image file missing")
            ctype, _ = mimetypes.guess_type(full)
            with open(full, "rb") as fh:
                body = fh.read()
            # Immutable enough: rows are new ids on re-upload, so cache hard —
            # a slideshow must not refetch its set every loop.
            return self._send(200, body, ctype or "image/jpeg",
                              {"Cache-Control": "public, max-age=86400"})

        # ---- display power (DPMS on the panel's X server)
        if path == "/api/display" and method == "POST":
            want_on = bool(self._body().get("on", True))
            x_display = os.environ.get("DIGICALENDER_X_DISPLAY", ":0")
            try:
                # `force` works even with DPMS timers disabled (kiosk.sh turns
                # them off so nothing sleeps the panel behind our back);
                # tested against a real panel. Any input wakes X too —
                # this endpoint is the deliberate path.
                r = subprocess.run(
                    ["xset", "-display", x_display, "dpms", "force",
                     "on" if want_on else "off"],
                    capture_output=True, text=True, timeout=5)
                if r.returncode != 0:
                    return self._json(502, {"ok": False,
                                            "error": (r.stderr or "xset failed").strip()})
            except FileNotFoundError:
                return self._json(502, {"ok": False,
                                        "error": "xset is not installed on this host"})
            except subprocess.TimeoutExpired:
                return self._json(502, {"ok": False, "error": "xset timed out"})
            return self._json(200, {"ok": True, "on": want_on})

        # ---- settings
        if path == "/api/settings":
            if method == "GET":
                return self._json(200, {"settings": store.all_settings()})
            if method == "PATCH":
                b = self._body()
                if not isinstance(b, dict):
                    raise ApiError(400, "body must be an object")
                for k, v in list(b.items())[:100]:
                    store.set_setting(str(k)[:80],
                                      v if isinstance(v, str) else json.dumps(v))
                return self._json(200, {"settings": store.all_settings()})
            raise ApiError(405, "method not allowed")

        raise ApiError(404, "no such endpoint")

    # -------------------------------------------------------------- static

    def _static(self, path):
        rel = "index.html" if path == "/" else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        # Defence in depth: never serve outside static/ even if normpath is fooled.
        if not full.startswith(STATIC + os.sep) and full != STATIC:
            raise ApiError(403, "forbidden")
        if not os.path.isfile(full):
            raise ApiError(404, "not found")
        ctype, _ = mimetypes.guess_type(full)
        if full.endswith(".js"):
            ctype = "text/javascript"      # ES modules are refused without this
        with open(full, "rb") as fh:
            body = fh.read()
        self._send(200, body, ctype or "application/octet-stream")


def main() -> int:
    ap = argparse.ArgumentParser(description="DigiCalender server")
    ap.add_argument("--host", default=os.environ.get("DIGICALENDER_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("DIGICALENDER_PORT", "8080")))
    args = ap.parse_args()

    store.init_db()
    os.makedirs(GALLERY_DIR, exist_ok=True)
    hub.start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"DigiCalender on http://{args.host}:{args.port}  dsn={store.DSN}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
