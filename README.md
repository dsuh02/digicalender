# DigiCalender

A touch calendar for a wall-mounted display. Dark theme, month / week / day views, tap to create,
swipe to navigate. Runs as a local web app plus a Chromium kiosk on the attached panel.

Built for `panelhost` — an a mini PC (4 cores / 14 GB, Ubuntu 26.04) with a 1920×1080
touch panel on HDMI.

**Stdlib Python only — no pip install, no build step.** That's deliberate: the host runs Python
3.14, where third-party wheels are still patchy, and a wall display that won't come up because a
dependency failed to compile is worse than a hand-rolled router.

## Layout

```
server.py              HTTP server, hand-rolled router, JSON API, static serving
store.py               SQLite schema + CRUD (provider-aware from day one)
kiosk.sh               X session: xset/unclutter/openbox, health-wait, Chromium kiosk
providers/
  base.py              CalendarProvider interface + SyncResult
  local.py             the on-device calendar
  google.py            SCAFFOLD — see the module docstring
  microsoft.py         SCAFFOLD — see the module docstring
static/                index.html, app.css, app.js  (no framework, no CDN)
deploy/install.sh      systemd units, X config, console quieting — run as root
config.example.json    copy to config.json when connecting a provider
```

## Install

Copy the tree to `/home/panel/digicalender` on the display host, then:

```bash
sudo bash deploy/install.sh
```

Idempotent — re-run after pulling changes. It installs X + Chromium, writes both systemd units,
sets the timezone, and starts everything.

```bash
systemctl status digicalender          # web app, port 8080
systemctl status digicalender-kiosk    # X + Chromium on the panel
journalctl -u digicalender -f
```

Reachable at `http://<host>:8080` from any LAN device or over WireGuard — phones included, so you
can add events without walking to the wall.

## Local development

```bash
python3 server.py --port 8090
```

Open `http://localhost:8090`. The SQLite file is created next to `server.py` on first run;
`DIGICALENDER_DB` overrides the path.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | liveness — `kiosk.sh` polls this before launching Chromium |
| GET | `/api/events?start=ISO&end=ISO` | events **overlapping** the range |
| POST | `/api/events` | create |
| PATCH | `/api/events/{uid}` | partial update |
| DELETE | `/api/events/{uid}` | **soft** delete (tombstone kept for sync) |
| GET | `/api/providers` | adapters and whether they're configured |
| GET | `/api/accounts` | connected accounts |
| POST | `/api/sync` | run every enabled account's adapter |

Times are UTC ISO-8601 (`2026-07-30T14:00:00Z`). The browser converts at the edges; nothing else
deals in local time.

## Connecting Google / Microsoft

Both adapters are scaffolded but not connected — `configured = False`, so `/api/sync` reports them
unavailable instead of failing. Each module's docstring lists the exact console steps and API
endpoints, and `to_local_event()` (the fiddly part — mapping their event shapes onto ours) is
already written for both.

To finish either: fill `config.json`, implement `pull()` and `push()`, flip `configured = True`.
Nothing else changes — the store and UI are already provider-aware.

**Use the device-code OAuth flow for both.** This is a keyboard-less wall panel, so redirect-based
flows are the wrong shape; device code puts a short string on screen that you type in on your phone.
Google needs an OAuth client of type *TV and Limited Input device*; Microsoft needs
*Allow public client flows = Yes*.

## Design notes

- **All-day events are floating dates, not instants.** Compared and emitted by date component,
  never timezone-converted — the rule iCal, Google and Graph all use. Stored as UTC midnight
  instead, an all-day event renders across two days on any panel west of Greenwich.
- **Soft deletes.** A hard delete loses the tombstone we need to push, and the event comes back on
  the next pull from a remote.
- **`dirty = 1`** marks locally-changed rows awaiting push.
- The partial unique index on `(provider, account_id, external_id)` makes remote pulls idempotent —
  upserting the same remote event twice can't duplicate it.
- Range fetches pad ±1 day, since the server filters on UTC instants.

## Touch and kiosk gotchas

These are all load-bearing; each one cost real debugging time.

- **The kiosk must own tty1.** `seat0` has exactly one active session and logind only hands input
  devices to the active one. If a getty autologin holds tty1, X gets the DRM fd but is refused
  input devices and dies in `config_init` with signal 6 — video initialises fine, so the symptom
  looks nothing like the cause. `install.sh` moves the passwordless console to tty2.
- **`--overscroll-history-navigation=0` is required.** Without it a horizontal swipe triggers
  browser back-navigation — exactly the gesture the app uses to change period.
- **Snap Chromium denies hidden directories in `$HOME`** (snapd's `home` interface), so a
  `--user-data-dir` under `~/.config` silently fails to write. Use the snap's own profile path.
- `kiosk.sh` scrubs `exit_type: Crashed` from Preferences each launch, or Chromium shows a
  restore-tabs modal nobody can dismiss.
- `pgrep chromium` finds nothing — the snap's binary is `chrome`.
- **Console noise:** quiet it with `kernel.printk`, never `pci=noaer` or `pcie_aspm=off`. On this
  host the PCIe AER stream is 100% correctable and comes from the root port holding the *only* NIC;
  touching PCIe power management on a Wi-Fi-only box with no IPMI risks stranding it.

## Status

Running on the panel and auto-starting at boot. Not yet done:

- Recurring events — the `rrule` column exists, nothing reads it.
- Google / Microsoft `pull()` / `push()`.
- Screen scheduling — the display is always-on (`xset s off -dpms`).
- Reminders/notifications, search, drag-to-move.
