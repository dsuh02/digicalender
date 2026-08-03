# DigiCalender

A touch wall hub: a configurable dashboard of resizable widgets on a fine-grained
grid — calendars, to-dos, notifications, weather, and control of the Rokus,
Govee plugs and Samsung TVs on the network.

Built for a small always-on Ubuntu box driving a 1920×1080 HDMI touch panel on
the wall, but nothing here assumes that hardware.

**No pip, no build step.** Python stdlib plus `psycopg`, which comes from apt as
`python3-psycopg`. Deliberate: the host runs Python 3.14 where wheels are still
patchy, and a wall display that won't come up because a dependency failed to
compile is worse than a hand-rolled router.

## How it fits together

```
server.py              HTTP server, hand-rolled router, JSON API, SSE stream
store.py               PostgreSQL (psycopg3) — calendar + dashboard + devices
hub.py                 live layer: SSE broadcaster, device poller, reminder ticker
weather.py             Open-Meteo client (no API key)
devices/
  base.py              DeviceAdapter contract; every method returns, never raises
  roku.py              External Control Protocol over HTTP :8060
  govee.py             two adapters — LAN (UDP) and cloud (Developer API)
  samsung.py           Tizen REST + WebSocket remote + Wake-on-LAN
  wsclient.py          minimal RFC 6455 client (Samsung needs one; pip doesn't exist here)
  discovery.py         SSDP, Govee multicast, Samsung subnet sweep
providers/             calendar sync adapters — local, Google + Microsoft scaffolds
static/
  js/core/             api, grid engine, schema-driven settings sheet, icons
  js/widgets/          the widget implementations
  css/                 theme, grid, widget styling
deploy/install.sh      packages, database, systemd units, X config — run as root
```

## Interaction model

- **Pages** sit side by side in one horizontal track. A **two-finger drag**
  moves the track under your fingers and snaps on release, tablet-style —
  rubber-banding past the ends, a flick advancing on velocity. One finger stays
  reserved for the widgets. Page dots sit along the bottom edge.
- **The top bar is hidden.** Pull down from the top edge and it slides in over
  the page (a thin bar at the top is the only hint it exists). It leaves after
  7 seconds, or the moment you tap the content below it. The padlock is a plain
  toggle, so the bar can leave while you are still editing — it used to pin
  itself open and covered the very widgets you were trying to configure.

## Theme

The palette is generated from six numbers — three **role hues** plus three
dials — so it's impossible to build an incoherent scheme:

| | |
|---|---|
| **Primary** | interaction and selection: buttons, active tab, today, toggles |
| **Secondary** | device/home state: an "on" tile's icon and dot, scene icons |
| **Tertiary** | informational accents: reminders, precipitation |
| Intensity | monochrome ↔ vivid (saturation of all three roles) |
| Brightness | near-black ↔ light theme (flips polarity past ~62) |
| Tint | the undertone of the greys |

Settings → Theme: seven presets, then hue sliders per role. Everything applies
**live** — the panel is its own preview — and persists on Save. Accent
saturation/lightness are fixed at values tuned for the panel, so a hue slider
can't produce something garish; event colours are six evenly-spaced hues derived
from the same numbers, so a calendar never clashes with the chrome. Everything
is emitted as CSS custom properties from `static/js/core/theme.js`.

## The grid

Each page declares a cell grid (48 × 32 by default) and every widget stores
`x, y, w, h` **in cells**. Pixels exist only inside `static/js/core/grid.js`.

Placement is free-form with **overlap rejected** rather than auto-packed. On a
wall display you arrange things once and want them to stay put; a layout that
reflows because a neighbour grew is infuriating on a screen you look at daily.
Dragging shows a ghost that turns red over an occupied area and snaps back if
you release there. The palette shrinks a new widget toward its minimum size to
fit the space left, and tells you when it does.

Edit mode is modal — the padlock in the top bar. Widgets are inert until you
unlock, so a passing brush can't rearrange the wall.

## Widgets

| Category | Widgets |
|---|---|
| Calendar | Month, Week, Day, Up next |
| Info | Clock, Weather, Text label |
| Productivity | To-do list, Notifications |
| Home | Device tiles, Scenes, Roku remote, Media control |
| Photos | Gallery (in-widget slideshow), Screensaver trigger |
| Money | Accounts, Net worth, Net worth chart, Upcoming bills, Spending, Cash flow, Credit usage |

Every widget declares a settings schema; the options panel is generated from it,
so adding a setting is a one-line change and never involves writing form markup.
Field types: text, textarea, number, slider, toggle, select, colour, icon,
device, devices, scene, latlon, time.

To add a widget: write the definition, export it, add it to
`static/js/widgets/index.js`. Nothing else changes.

## Devices

| Adapter | Transport | Notes |
|---|---|---|
| `roku` | HTTP :8060 (ECP) | No auth. Works on sticks, boxes and Roku TVs |
| `govee_lan` | UDP 4001/4002/4003 | **Lights only.** Needs LAN Control enabled per device |
| `govee_cloud` | Developer API | **Required for smart plugs**, which have no LAN listener |
| `samsung_tv` | Tizen WS :8002 + WoL | First connect prompts on the TV; the token is then stored |

**Roku Limited mode.** If `Settings → System → Advanced system settings →
Control by mobile apps → Network access` is set to *Limited*, the Roku answers
`/query/device-info` but returns 403 for every command — so it looks online
while every button does nothing. The adapter detects this and surfaces the exact
fix rather than failing silently. Set it to *Default* or *Permissive*.

**Govee plugs are cloud-only.** The LAN API covers a subset of Govee's RGB
lighting and plugs are not on it. That's a Govee product decision, not a
limitation here — use `govee_cloud` with an API key from the Govee Home app
(Profile → About Us → Apply for API Key).

Discovery (Settings → Devices → Scan network) probes all three concurrently.
Rokus answer only when powered; Samsung TVs stop responding entirely when off.

## Galleries, people and money

Three things the dashboard grew into, each worth a note:

- **Galleries** are folders of images under `galleries/`, one per set — **not in
  git**. The database stores paths, so deleting a widget never touches a photo.
  A gallery widget cycles in place; the screensaver takes the whole screen and
  fades between images until you touch it. Four-finger double tap plays the
  starred set from anywhere.
- **People** let one panel serve a household: per-person greeting, theme and
  pages, with shared pages visible to everyone. Deleting a person is
  `ON DELETE SET NULL`, never cascade — their pages survive them.
- **Money** links accounts through Plaid (Hosted Link, so no keyboard and no JS
  widget on the panel) or by hand, and can drop credit-card and loan due dates
  onto the calendar as all-day events. Transactions come in over
  `/transactions/sync` and drive spending-by-category, cash flow and credit
  utilisation charts, all drawn as hand-rolled SVG in `js/core/charts.js` —
  colour comes from CSS custom properties, so the palette re-themes every chart
  without a redraw. Balances are hidden behind dots by default and re-hide
  themselves 25 seconds after a tap — it is a wall display. Charts stay visible
  while amounts are hidden: the shape of your spending is not the secret.

## Configuration

Nothing about a particular home is committed. Credentials (Govee, Plaid,
calendar feeds) are entered in Settings and live in the database; `galleries/`,
`config.json` and `*.db` are gitignored. The name in the top bar is
Settings → Display → *Name shown in the top bar*.

## Install

Clone to `~/digicalender` on the display host, then:

```bash
sudo bash deploy/install.sh
```

Idempotent — re-run after `git pull`. Installs X, Chromium and Postgres, creates
the database, writes both systemd units, and starts everything.

```bash
systemctl status digicalender          # web app, port 8080
systemctl status digicalender-kiosk    # X + Chromium on the panel
journalctl -u digicalender -f
```

Reachable at `http://<host>:8080` from any LAN device or over WireGuard — so you
can add events and drive the lights from a phone.

## Local development

```bash
createdb digicalender_dev
DIGICALENDER_DSN='dbname=digicalender_dev' python3 server.py --port 8090
```

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | liveness; `kiosk.sh` polls it before launching Chromium |
| GET | `/api/stream` | SSE: device state, layout and data-change events |
| GET | `/api/dashboard` | pages + widgets + settings in one round trip |
| — | `/api/events`, `/api/todos`, `/api/notifications` | CRUD |
| — | `/api/pages`, `/api/widgets` | CRUD; `POST /api/widgets/layout` saves a drag in one transaction |
| GET/POST | `/api/devices`, `/api/devices/{id}/command` | device control |
| POST | `/api/discover` | network scan |
| — | `/api/scenes`, `/api/scenes/{id}/run` | scenes |
| GET | `/api/weather?lat=&lon=` | cached 10 min |

Times are UTC ISO-8601. The browser converts at the edges; nothing else deals in
local time.

## Design notes

- **Timestamps are TEXT, not `TIMESTAMPTZ`.** ISO-8601 UTC sorts
  lexicographically, so range scans behave identically — and all-day events are
  *floating dates* (July 4th is July 4th in any timezone). Handing those to a
  type that applies timezone conversion is exactly the bug that made them render
  across two days.
- **Database connections are never held across a device call.** Roku/Govee/
  Samsung requests are network I/O with real latency; parking a connection
  behind one is how you exhaust a pool.
- **Adapters never raise for an unreachable device.** A panel that 500s because
  a TV is unplugged is worse than one that greys the tile out.
- **Soft-deleted events** keep the tombstone a future calendar sync needs.
- `store.q()` reconnects once on a dropped connection — Postgres will restart
  under a display that runs for months.

## Connecting Google / Microsoft

Both adapters are scaffolded with `configured = False`, so `/api/sync` reports
them unavailable instead of throwing. Each module docstring has the console
steps, and `to_local_event()` — the fiddly part — is already written for both.
Use the **device-code OAuth flow**: this is a keyboard-less wall panel.

## Kiosk gotchas

Each of these cost real debugging time.

- **The kiosk must own tty1.** `seat0` has one active session and logind only
  hands input devices to it. A getty autologin on tty1 leaves X with the DRM fd
  but no input, aborting in `config_init` — video comes up fine, so the symptom
  looks unrelated. `install.sh` moves the passwordless console to tty2.
- **`--overscroll-history-navigation=0`** — without it a horizontal swipe
  triggers browser back, which is the gesture used to change pages.
- **Snap Chromium denies hidden directories in `$HOME`**, so a `--user-data-dir`
  under `~/.config` silently fails. Use the snap's own profile path.
- `pgrep chromium` finds nothing — the snap's binary is `chrome`.
- **Console noise** paints over the X session, because `console=tty0` targets
  whichever VT is in front. Quiet it with `kernel.printk`, not `pci=noaer` — if
  the chatter is correctable PCIe AER from the root port holding your only NIC,
  disabling AER on a headless Wi-Fi-only box is how you strand it.

## Not done yet

- Recurring events — the `rrule` column exists, nothing reads it.
- Google / Microsoft `pull()` / `push()`.
- Govee cloud needs an API key before plugs will respond.
- Samsung pairing is written against the Tizen protocol but has never been run
  against a powered-on TV.
