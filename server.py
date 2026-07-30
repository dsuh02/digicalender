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
import sys
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import devices as device_registry
import hub
import providers
import store
import weather

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

ID_RE = r"([A-Za-z0-9_-]{1,64})"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


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
            results = []
            for acct in store.list_accounts():
                if not acct["enabled"]:
                    continue
                try:
                    results.append(providers.get(acct["provider"], acct).sync().as_dict())
                except KeyError as e:
                    results.append({"ok": False, "message": str(e)})
            if not results:
                results.append({"provider": "local", "ok": True,
                                "message": "Local calendar only — no accounts connected yet."})
            return self._json(200, {"results": results})

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
                pid = store.create_page((b.get("name") or "Page").strip()[:60],
                                        _int(b, "position", 0, 100, 0),
                                        _int(b, "cols", 8, 200, 48),
                                        _int(b, "rows", 8, 200, 32))
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
                                        "color": (b.get("color") or "#6c86c8")[:20],
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
