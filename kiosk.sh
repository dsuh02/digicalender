#!/bin/sh
# Kiosk session for the wall display. Launched by xinit from
# digicalender-kiosk.service; everything here runs inside the X session.
set -eu

URL="${DIGICALENDER_URL:-http://localhost:8080}"

# A wall calendar must never sleep or blank.
xset s off
xset s noblank
xset -dpms

# No pointer on a touch panel — hide it immediately and keep it hidden.
unclutter -idle 0 -root &

# Minimal WM. Chromium is happier with something owning the root window,
# and it makes the kiosk window get focus reliably.
openbox &

# Wait for the app to answer before showing anything, so the panel never
# flashes a Chromium error page on a cold boot where X wins the race.
i=0
while [ "$i" -lt 60 ]; do
    if curl -sf -o /dev/null "$URL/api/health"; then break; fi
    i=$((i + 1))
    sleep 1
done

# Use the snap's own profile dir. A --user-data-dir elsewhere in $HOME would be
# refused: snapd's `home` interface denies hidden directories, so a profile
# under ~/.config never gets written and Chromium dies on startup.
PREFS="$HOME/snap/chromium/common/chromium/Default/Preferences"

# Chromium remembers that it was killed and offers to restore tabs on next
# start, which on a kiosk means a modal nobody can dismiss. Scrub the flags
# that trigger it before every launch.
if [ -f "$PREFS" ]; then
    sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/; s/"exited_cleanly":false/"exited_cleanly":true/' "$PREFS" || true
fi

exec chromium \
    --kiosk "$URL" \
    --start-fullscreen \
    --touch-events=enabled \
    --enable-features=OverlayScrollbar \
    --disable-pinch \
    --overscroll-history-navigation=0 \
    --disable-features=TranslateUI,Translate \
    --noerrdialogs \
    --disable-infobars \
    --no-first-run \
    --fast \
    --fast-start \
    --disable-session-crashed-bubble \
    --disable-restore-session-state \
    --hide-crash-restore-bubble \
    --password-store=basic \
    --check-for-update-interval=31536000 \
    --autoplay-policy=no-user-gesture-required
