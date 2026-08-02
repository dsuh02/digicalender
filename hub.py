"""
Live update hub — the part that makes the panel feel like a hub rather than a
page you have to reload.

Three pieces:

  Broadcaster    fan-out of JSON events to connected SSE clients.
  Device poller  polls every enabled device on an interval and broadcasts only
                 what changed, so a plug switched from a phone lights up on the
                 wall within a couple of seconds.
  Reminder tick  turns upcoming calendar events into notifications.

Threading rules that matter here:

  * Database connections are never held across a device call. Devices are
    network I/O with real latency; parking a connection behind one is how you
    exhaust a pool. Each loop reads, releases, does I/O, then writes.
  * Every poll of a device happens off the request path, so a dead TV slows
    nothing down. The HTTP handler only ever reads the cached state.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

import devices as device_registry
import store

POLL_INTERVAL = 6.0        # seconds between device sweeps (LAN devices)
CLOUD_POLL_INTERVAL = 120.0  # cloud-backed devices: Govee's API is rate-limited
                             # (10k/day); a 6s cadence would burn 14k calls per
                             # device per day and 429 within the hour — seen live.
CLOUD_KINDS = {"govee_cloud"}
REMINDER_INTERVAL = 60.0
REMINDER_LEAD_MIN = 10     # notify this long before an event starts
FEED_INTERVAL = 900.0      # calendar subscriptions refresh every 15 minutes


class Broadcaster:
    """Tiny pub/sub. Each subscriber gets a bounded queue; a client that stops
    draining is dropped rather than allowed to grow without limit."""

    def __init__(self):
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        qq: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._subs.add(qq)
        return qq

    def unsubscribe(self, qq: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(qq)

    def publish(self, event: str, data) -> None:
        payload = (event, data)
        with self._lock:
            dead = []
            for qq in self._subs:
                try:
                    qq.put_nowait(payload)
                except queue.Full:
                    dead.append(qq)
            for qq in dead:
                self._subs.discard(qq)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._subs)


bus = Broadcaster()

# device id -> last state dict we broadcast, so we only publish real changes
_last_states: dict[str, dict] = {}
_states_lock = threading.Lock()

# device id -> monotonic time of its last poll, for rate-limited kinds.
_last_polled: dict[str, float] = {}


def snapshot() -> dict:
    with _states_lock:
        return dict(_last_states)


def state_for(device_id: str) -> dict:
    with _states_lock:
        return dict(_last_states.get(device_id, {}))


def set_state(device_id: str, state: dict, *, broadcast: bool = True) -> bool:
    """Record state; returns True if it actually changed."""
    with _states_lock:
        changed = _last_states.get(device_id) != state
        _last_states[device_id] = state
    if changed and broadcast:
        bus.publish("device_state", {"id": device_id, "state": state})
    return changed


def poll_device_now(device: dict) -> dict:
    """Poll one device and record the result. Safe to call from a request
    thread — used right after a command so the UI doesn't wait for the sweep."""
    _last_polled[device["id"]] = time.monotonic()
    adapter = device_registry.adapter_for(device)
    if adapter is None:
        return {}
    res = adapter.get_state()
    state = dict(res.state)
    state.setdefault("online", res.ok)
    if not res.ok and res.message:
        state["error"] = res.message
    set_state(device["id"], state)
    try:
        store.record_device_state(device["id"], state)
    except Exception:
        pass
    return state


def _device_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            # Read the device list, then release the connection before any I/O.
            devs = [d for d in store.list_devices() if d.get("enabled")]
        except Exception:
            devs = []

        for d in devs:
            if stop.is_set():
                break
            try:
                # Cloud-backed devices poll on their own, slower clock; a
                # command still refreshes them immediately via poll_device_now.
                if d["kind"] in CLOUD_KINDS:
                    if time.monotonic() - _last_polled.get(d["id"], 0) < CLOUD_POLL_INTERVAL:
                        continue
                    _last_polled[d["id"]] = time.monotonic()
                adapter = device_registry.adapter_for(d)
                if adapter is None:
                    continue
                res = adapter.get_state()          # network I/O, no DB held
                state = dict(res.state)
                state.setdefault("online", res.ok)
                if not res.ok and res.message:
                    state["error"] = res.message

                was_online = _last_states.get(d["id"], {}).get("online")
                if set_state(d["id"], state):
                    try:
                        store.record_device_state(d["id"], state)
                    except Exception:
                        pass
                    # Only notify on an actual transition to offline, and only
                    # once — dedupe_key stops a flapping device spamming the wall.
                    if was_online is True and state.get("online") is False:
                        store.push_notification(
                            f"{d['name']} went offline",
                            state.get("error", ""), kind="warn",
                            source="devices",
                            dedupe_key=f"offline:{d['id']}:{int(time.time() // 3600)}")
            except Exception:
                continue

        stop.wait(POLL_INTERVAL)


def _reminder_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            now = datetime.now(timezone.utc)
            soon = now + timedelta(minutes=REMINDER_LEAD_MIN)
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            events = store.list_events(now.strftime(fmt), soon.strftime(fmt))
            for ev in events:
                if ev.get("all_day"):
                    continue
                start = ev["start_utc"]
                if start < now.strftime(fmt):
                    continue          # already running
                created = store.push_notification(
                    ev["title"],
                    f"Starts at {start[11:16]} UTC" +
                    (f" · {ev['location']}" if ev.get("location") else ""),
                    kind="reminder", source="calendar",
                    dedupe_key=f"event:{ev['uid']}:{start}")
                if created:
                    bus.publish("notification", created)
        except Exception:
            pass
        stop.wait(REMINDER_INTERVAL)


PRESENCE_INTERVAL = 90.0
PRESENCE_GRACE_MIN = 20      # "home" if seen within this many minutes


def _presence_loop(stop: threading.Event) -> None:
    """Who's home, by MAC on the LAN.

    Phones sleep their radios and stop answering ARP for minutes at a time, so
    a single miss means nothing — presence is "seen within PRESENCE_GRACE_MIN",
    not "answered this instant". Approximate on purpose; it drives a greeting,
    not a lock.
    """
    from devices.discovery import _neighbour_macs, _sweep_arp
    stop.wait(30)
    while not stop.is_set():
        try:
            people = [p for p in store.list_people() if p.get("macs")]
            if people:
                _sweep_arp()                      # warm the neighbour table
                seen = {mac for _ip, mac in _neighbour_macs()}
                changed = False
                for p in people:
                    want = {str(m).lower().strip() for m in (p.get("macs") or [])}
                    if want & seen:
                        store.mark_person_seen(p["id"])
                        changed = True
                if changed:
                    bus.publish("people_changed", {})
        except Exception:
            pass
        stop.wait(PRESENCE_INTERVAL)


FINANCE_INTERVAL = 6 * 3600.0     # balances move slowly; Plaid calls are metered


def _finance_loop(stop: threading.Event) -> None:
    import finance
    stop.wait(60)
    while not stop.is_set():
        try:
            if store.list_finance_items():
                finance.sync_all()
                bus.publish("finance_changed", {})
            # Bills regenerate regardless — a manual account with a due day
            # still needs next month's date to appear as the month turns over.
            if finance.sync_bill_events():
                bus.publish("events_changed", {})
        except Exception:
            pass
        stop.wait(FINANCE_INTERVAL)


def _feed_loop(stop: threading.Event) -> None:
    """Keep calendar subscriptions fresh. First run happens shortly after boot
    so a restart never shows stale meetings for 15 minutes."""
    import feeds                      # local import: feeds -> store only
    stop.wait(20)
    while not stop.is_set():
        try:
            results = feeds.sync_all()   # network I/O; no DB conn held across it
            if any(r.get("changed") for r in results):
                bus.publish("events_changed", {})
        except Exception:
            pass
        stop.wait(FEED_INTERVAL)


_stop = threading.Event()
_threads: list[threading.Thread] = []


def start() -> None:
    if _threads:
        return
    for fn, name in ((_device_loop, "device-poller"),
                     (_reminder_loop, "reminder-ticker"),
                     (_feed_loop, "feed-sync"),
                     (_presence_loop, "presence"),
                     (_finance_loop, "finance-sync")):
        t = threading.Thread(target=fn, args=(_stop,), name=name, daemon=True)
        t.start()
        _threads.append(t)


def stop() -> None:
    _stop.set()


def sse_format(event: str, data) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
