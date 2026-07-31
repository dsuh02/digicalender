#!/bin/bash
# DigiCalender installer — run as root on the display host (Ubuntu).
#
#   sudo bash deploy/install.sh
#
# Assumes the app lives at /home/panel/digicalender. Idempotent: safe to re-run
# after pulling changes (it rewrites the units and restarts).
set -euo pipefail

APP_USER="${APP_USER:-panel}"
APP_DIR="${APP_DIR:-/home/$APP_USER/digicalender}"
APP_PORT="${APP_PORT:-8080}"
TIMEZONE="${TIMEZONE:-America/Los_Angeles}"
DB_NAME="${DB_NAME:-digicalender}"

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
[ -f "$APP_DIR/server.py" ] || { echo "no server.py under $APP_DIR" >&2; exit 1; }

USER_UID="$(id -u "$APP_USER")"

echo "==> packages"
DEBIAN_FRONTEND=noninteractive apt-get update -qq
# psycopg comes from apt, not pip — the app still has no build step.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    xserver-xorg xinit openbox unclutter x11-xserver-utils curl \
    postgresql python3-psycopg
# Chromium is snap-only on modern Ubuntu.
snap list chromium >/dev/null 2>&1 || snap install chromium

echo "==> database"
# Peer auth over the unix socket: the app runs as $APP_USER and connects as
# $APP_USER, so no password exists on disk or in the repo.
systemctl enable --now postgresql
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$APP_USER'" | grep -q 1 \
  || sudo -u postgres createuser --createdb "$APP_USER"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 \
  || sudo -u postgres createdb -O "$APP_USER" "$DB_NAME"

echo "==> timezone"
timedatectl set-timezone "$TIMEZONE"

echo "==> app service"
cat > /etc/systemd/system/digicalender.service <<UNIT
[Unit]
Description=DigiCalender web app
# Postgres must be accepting connections first, and Restart=always covers the
# case where it isn't quite ready when we first try.
After=network.target postgresql.service
Wants=postgresql.service
Before=digicalender-kiosk.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=DIGICALENDER_DSN=dbname=$DB_NAME
ExecStart=/usr/bin/python3 $APP_DIR/server.py --port $APP_PORT
Restart=always
RestartSec=3
ProtectSystem=full
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
UNIT

echo "==> kiosk service"
# The kiosk MUST own tty1. seat0 has exactly one active session, and logind
# only hands input devices to the active one -- if a getty autologin (or any
# other session) holds tty1, X gets the DRM fd but is refused input devices and
# dies in config_init with signal 6. Video works, input is fatal; the symptom
# looks nothing like the cause.
cat > /etc/systemd/system/digicalender-kiosk.service <<UNIT
[Unit]
Description=DigiCalender kiosk display (X + Chromium)
After=digicalender.service systemd-user-sessions.service
Wants=digicalender.service
Conflicts=getty@tty1.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
PAMName=login
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
StandardInput=tty
StandardOutput=journal
StandardError=journal
Environment=HOME=/home/$APP_USER
Environment=XDG_RUNTIME_DIR=/run/user/$USER_UID
Environment=DIGICALENDER_URL=http://localhost:$APP_PORT
WorkingDirectory=$APP_DIR
# -nocursor: a touch panel must never draw an arrow. Server-level beats CSS
# cursor tricks — nothing (Chromium included) can bring it back.
ExecStart=/usr/bin/xinit $APP_DIR/kiosk.sh -- :0 vt1 -nolisten tcp -nocursor
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

echo "==> free tty1, move the passwordless console to tty2"
mkdir -p /etc/systemd/system/getty@tty2.service.d
cat > /etc/systemd/system/getty@tty2.service.d/override.conf <<OVR
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $APP_USER --noclear %I \$TERM
OVR
rm -rf /etc/systemd/system/getty@tty1.service.d

echo "==> allow a non-root user to start X"
# The default 'console' policy won't do: this service isn't an interactive
# console login.
cat > /etc/X11/Xwrapper.config <<'XW'
allowed_users=anybody
needs_root_rights=yes
XW

echo "==> quiet the console"
# console=tty0 targets whichever VT is in FRONT, so kernel messages paint over
# the X session. This host emits a steady stream of *correctable* PCIe AER
# events from the Wi-Fi root port (152k correctable / 0 fatal when measured).
# Raise the console threshold to KERN_ERR instead of touching PCIe: AER stays
# enabled and every event is still recorded in dmesg and the sysfs counters.
# Do NOT "fix" this with pci=noaer or pcie_aspm=off -- on a Wi-Fi-only box with
# no wired fallback and no IPMI, that risks stranding the machine.
cat > /etc/sysctl.d/99-quiet-console.conf <<'SYS'
# console, default_message, minimum, boot
kernel.printk = 3 4 1 3
SYS
sysctl -q -p /etc/sysctl.d/99-quiet-console.conf

echo "==> enable + start"
chmod +x "$APP_DIR/kiosk.sh"
systemctl daemon-reload
systemctl disable --now getty@tty1.service 2>/dev/null || true
for s in $(loginctl list-sessions --no-legend | awk '$NF=="tty1"{print $1}'); do
    loginctl terminate-session "$s" 2>/dev/null || true
done
systemctl enable --now getty@tty2.service
# enable + RESTART, not enable --now: --now is a no-op on an already-running
# unit, so a re-run after `git pull` would silently keep the old process.
systemctl enable digicalender.service
systemctl restart digicalender.service
systemctl restart digicalender-kiosk.service

sleep 10
echo
echo "app   : $(systemctl is-active digicalender.service)"
echo "kiosk : $(systemctl is-active digicalender-kiosk.service)"
curl -sS "http://localhost:$APP_PORT/api/health" && echo
