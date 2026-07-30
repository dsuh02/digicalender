#!/usr/bin/env python3
"""
DigiCalender — touch calendar for the wall display.

Stdlib only, on purpose: this runs on Python 3.14 where third-party wheels are
still patchy, and a wall display that fails to boot because a dependency didn't
build is worse than one with a hand-rolled router.

  python3 server.py [--host 0.0.0.0] [--port 8080]
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import providers
import store

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _norm_iso(value: str, field: str) -> str:
    """Accept anything JS's toISOString() produces, store a canonical Z form."""
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
        if len(title) > 500:
            raise ApiError(400, "title is too long (max 500)")
        out["title"] = title
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
    # Only compare when both ends are known; a PATCH may carry just one.
    if "start_utc" in out and "end_utc" in out and out["end_utc"] < out["start_utc"]:
        raise ApiError(400, "end_utc must not be before start_utc")
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "DigiCalender"
    protocol_version = "HTTP/1.1"

    # ---------------- plumbing ----------------

    def log_message(self, fmt, *args):  # quieter journal
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Kiosk reloads should never show a stale bundle.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload):
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ApiError(413, "request body too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "body is not valid JSON")

    # ---------------- routing ----------------

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

    def _route(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path.startswith("/api"):
                self._api(method, path, parse_qs(parsed.query))
            elif method == "GET":
                self._static(path)
            else:
                raise ApiError(405, "method not allowed")
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except BrokenPipeError:
            pass  # kiosk browser navigated away mid-response
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "internal server error"})

    def _api(self, method: str, path: str, q: dict):
        if path == "/api/health":
            return self._json(200, {
                "ok": True,
                "service": "digicalender",
                "time_utc": store.now_iso(),
            })

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
                    p = providers.get(acct["provider"], acct)
                    results.append(p.sync().as_dict())
                except KeyError as e:
                    results.append({"ok": False, "message": str(e)})
            if not results:
                results.append({
                    "provider": "local", "ok": True,
                    "message": "Local calendar only — no accounts connected yet.",
                })
            return self._json(200, {"results": results})

        if path == "/api/events":
            if method == "GET":
                start = (q.get("start") or [""])[0]
                end = (q.get("end") or [""])[0]
                if not start or not end:
                    raise ApiError(400, "start and end query params are required")
                return self._json(200, {
                    "events": store.list_events(_norm_iso(start, "start"),
                                                _norm_iso(end, "end"))
                })
            if method == "POST":
                data = validate_event(self._read_json())
                return self._json(201, {"event": store.create_event(data)})
            raise ApiError(405, "method not allowed")

        m = re.fullmatch(r"/api/events/([A-Za-z0-9_-]{1,64})", path)
        if m:
            uid = m.group(1)
            if method == "GET":
                ev = store.get_event(uid)
                if not ev or ev["deleted"]:
                    raise ApiError(404, "event not found")
                return self._json(200, {"event": ev})
            if method == "PATCH":
                data = validate_event(self._read_json(), partial=True)
                ev = store.update_event(uid, data)
                if not ev:
                    raise ApiError(404, "event not found")
                return self._json(200, {"event": ev})
            if method == "DELETE":
                if not store.delete_event(uid):
                    raise ApiError(404, "event not found")
                return self._json(200, {"ok": True})
            raise ApiError(405, "method not allowed")

        raise ApiError(404, "no such endpoint")

    def _static(self, path: str):
        rel = "index.html" if path == "/" else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        # Defence in depth: never serve outside static/ even if normpath is fooled.
        if not full.startswith(STATIC + os.sep) and full != STATIC:
            raise ApiError(403, "forbidden")
        if not os.path.isfile(full):
            raise ApiError(404, "not found")
        ctype, _ = mimetypes.guess_type(full)
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
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"DigiCalender serving on http://{args.host}:{args.port}  db={store.DB_PATH}",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
