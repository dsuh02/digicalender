"""
Wake-up routines: wake a Roku, open Spotify, start something playing.

The sequence is deliberately dumb and sequential, because every step is a
physical device waking up and none of them can be trusted to be ready when the
previous one returns:

  1. power on the Roku (ECP PowerOn)
  2. wait — the box boots, the HDMI handshake happens, the TV switches input
  3. launch the Spotify channel
  4. wait for that channel to register itself as a Spotify Connect device
  5. transfer playback to it, set the volume, start the content

Two things this CANNOT do, both verified against the hardware and the API rather
than assumed:

**Absolute volume on a Roku box.** The Ultra reports `is-tv: false` and
`supports-audio-settings: false` — it has no volume of its own, because the
sound leaves over HDMI and the TV or soundbar owns the level. ECP's volume keys
only mean something on Roku TVs. So volume here is set through Spotify Connect,
which controls the level *within* the Spotify app. If the Connect device refuses
it, the step reports that and the rest of the alarm still runs.

**Spotify's DJ.** The DJ button has no Web API, and Spotify-owned algorithmic
playlists (DJ, Daily Mix, Discover Weekly) return 404 to third-party apps.
Alarms have to point at your own playlists, albums, artists or tracks. Anything
else fails at 6am, which is the worst possible time to discover it, so
validate_uri() rejects them up front instead.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime

import spotify
import store

SPOTIFY_ROKU_APP = "22297"          # "Spotify Music" — same id on both boxes
CONNECT_WAIT_S = 25                 # how long to wait for the Connect device
CONNECT_POLL_S = 2.5

# Spotify's own algorithmic playlists. 37i9dQZF1E* is the DJ and the personalised
# mixes; they 404 for third-party apps no matter the scopes.
_SPOTIFY_OWNED = re.compile(r"37i9dQZF1E", re.I)
_URI_RE = re.compile(r"^spotify:(playlist|album|artist|track|show|episode):[A-Za-z0-9]{16,}$")


def validate_uri(uri: str) -> str:
    """Normalise a pasted Spotify link or URI, refusing what cannot be played."""
    s = (uri or "").strip()
    if not s:
        return ""
    # Accept an open.spotify.com link and convert it.
    m = re.match(r"https?://open\.spotify\.com/(?:intl-[a-z]+/)?"
                 r"(playlist|album|artist|track|show|episode)/([A-Za-z0-9]+)", s)
    if m:
        s = f"spotify:{m.group(1)}:{m.group(2)}"
    if not _URI_RE.match(s):
        raise ValueError("That is not a Spotify URI or link — expected something like "
                         "spotify:playlist:… or an open.spotify.com address")
    if _SPOTIFY_OWNED.search(s):
        raise ValueError("Spotify's own mixes (DJ, Daily Mix, Discover Weekly) cannot be "
                         "started by an app — Spotify blocks them. Use one of your own "
                         "playlists, an album, an artist or a track.")
    return s


# ------------------------------------------------------------------ running

def _adapter(device: dict):
    import devices as registry
    a = registry.adapter_for(device)
    if a is None:
        raise RuntimeError(f"no adapter for kind {device.get('kind')}")
    return a


def run(alarm: dict, *, log=None) -> dict:
    """Execute one alarm. Never raises: an alarm that half-worked should say so."""
    steps: list[dict] = []

    def step(name, fn):
        t0 = time.time()
        try:
            detail = fn() or ""
            steps.append({"step": name, "ok": True, "detail": detail,
                          "ms": int((time.time() - t0) * 1000)})
            return True
        except Exception as e:
            steps.append({"step": name, "ok": False, "detail": str(e),
                          "ms": int((time.time() - t0) * 1000)})
            return False

    device = store.get_device(alarm["device_id"]) if alarm.get("device_id") else None

    # 1 — wake the Roku
    if device:
        def power_on():
            res = _adapter(device).command("power_on", {})
            if not res.ok:
                raise RuntimeError(res.message or "the Roku did not accept PowerOn")
            return f"{device['name']} on"
        step("Wake the Roku", power_on)
    else:
        steps.append({"step": "Wake the Roku", "ok": False,
                      "detail": "no device selected", "ms": 0})

    # 2 — let it boot. This is why the whole sequence is sequential.
    wait = max(0, min(120, int(alarm.get("wait_seconds") or 13)))
    if wait:
        step(f"Wait {wait}s", lambda: (time.sleep(wait), f"{wait}s")[1])

    # 3 — open Spotify on the box
    app_id = (alarm.get("app_id") or SPOTIFY_ROKU_APP).strip()
    if device and app_id:
        def launch():
            res = _adapter(device).command("launch_app", {"app_id": app_id})
            if not res.ok:
                raise RuntimeError(res.message or "the Roku refused the launch")
            return f"channel {app_id}"
        step("Open Spotify", launch)

    uri = (alarm.get("spotify_uri") or "").strip()
    want_name = (alarm.get("device_name") or "").strip()
    if not (uri or want_name):
        return _finish(steps)

    # 4 — wait for the channel to advertise itself to Spotify Connect. It is not
    # ready the instant the Roku says it launched.
    target = {"id": None, "name": ""}

    def find_connect():
        deadline = time.time() + CONNECT_WAIT_S
        seen: list[str] = []
        while time.time() < deadline:
            try:
                found = spotify.devices()
            except spotify.SpotifyError as e:
                raise RuntimeError(e.message)
            seen = [d.get("name", "") for d in found]
            for d in found:
                if not want_name or want_name.lower() in str(d.get("name", "")).lower():
                    target["id"] = d.get("id")
                    target["name"] = d.get("name", "")
                    return f"found “{target['name']}”"
            time.sleep(CONNECT_POLL_S)
        raise RuntimeError("no matching Spotify Connect device appeared"
                           + (f" (saw: {', '.join(seen) or 'none'})" if seen else ""))

    if not step("Find it on Spotify Connect", find_connect):
        return _finish(steps)

    # 5 — hand playback to it, then set the level, then start. Volume before play
    # so the first second is not at whatever the last session left behind.
    step("Hand over playback", lambda: (spotify.transfer(target["id"], False), "ok")[1])

    if alarm.get("volume") is not None:
        def vol():
            # Not fatal. Plenty of Connect endpoints accept playback commands and
            # refuse volume, and a silent alarm is worse than a loud one.
            spotify.set_volume(int(alarm["volume"]), target["id"])
            return f"{int(alarm['volume'])}%"
        step("Set volume", vol)

    if uri:
        step("Start playing", lambda: (spotify.start(target["id"], uri,
                                                     bool(alarm.get("shuffle"))), uri)[1])
    return _finish(steps)


def _finish(steps: list[dict]) -> dict:
    ok = all(s["ok"] for s in steps)
    failed = [s for s in steps if not s["ok"]]
    return {
        "ok": ok,
        "steps": steps,
        "message": "ran clean" if ok else "; ".join(f"{s['step']}: {s['detail']}" for s in failed),
    }


# --------------------------------------------------------------- scheduler

CHECK_INTERVAL = 20.0


def due(alarm: dict, now: datetime) -> bool:
    if not alarm.get("enabled"):
        return False
    at = str(alarm.get("at_time") or "")
    if not re.match(r"^\d{2}:\d{2}$", at):
        return False
    days = alarm.get("days") or []
    if days and now.weekday() not in [int(d) for d in days]:
        return False
    today = now.strftime("%Y-%m-%d")
    if alarm.get("last_fired") == today:
        return False
    hh, mm = int(at[:2]), int(at[3:5])
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    delta = (now - target).total_seconds()
    # Fire within a two-minute window AFTER the time, never before. A restart at
    # 06:04 must not replay a 06:00 alarm — last_fired covers the same day, but
    # the window is what stops a machine that was asleep from firing at noon.
    return 0 <= delta < 120


def loop(stop: threading.Event, bus=None) -> None:
    stop.wait(20)
    while not stop.is_set():
        try:
            now = datetime.now()
            for a in store.list_alarms():
                if not due(a, now):
                    continue
                # Claim it BEFORE running. The sequence takes ~40s, longer than
                # the check interval, and a second thread must not start it again.
                store.mark_alarm_fired(a["id"], now.strftime("%Y-%m-%d"), "running…")
                res = run(a)
                store.mark_alarm_fired(a["id"], now.strftime("%Y-%m-%d"), res["message"])
                if bus:
                    bus.publish("alarms_changed", {})
                if not res["ok"]:
                    store.push_notification(
                        f"Alarm “{a['name']}” had a problem", res["message"],
                        kind="warn", source="alarms",
                        dedupe_key=f"alarm:{a['id']}:{now.strftime('%Y-%m-%d')}")
        except Exception:
            pass
        stop.wait(CHECK_INTERVAL)
