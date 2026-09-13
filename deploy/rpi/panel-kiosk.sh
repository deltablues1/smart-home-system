#!/bin/sh
# Wall panel: Chromium showing the Jarvis dashboard, nothing else.
#
# Lives in the user's home and is started from ~/.config/labwc/autostart, so it
# needs no root and is undone by deleting one file.
#
# The profile is its own directory rather than the default one: the panel stays
# logged into Home Assistant across reboots without entangling that session with
# any browsing done on this Pi by hand.

URL="${PANEL_URL:-http://homeassistant.local:8123/jarvis-dom/pregled}"
# Chromium only grants getUserMedia in a secure context, and the panel loads
# Home Assistant over plain HTTP on a LAN address. Without this the dashboard's
# "Pitaj Jarvisa" button opens Assist with a microphone that can never start.
# Scoped to this one origin, and it needs the --user-data-dir set below to
# persist. Derived from URL so changing PANEL_URL keeps the two in step.
ORIGIN="$(printf '%s' "$URL" | cut -d/ -f1-3)"
# The panel is 1024x600, and Home Assistant's section columns have a minimum
# width that lets only two of them fit at 1:1 -- which leaves a third of the
# screen empty and pushes the rest below the fold. Scaling down gives the page
# a 1280x750 viewport: three columns, and a quarter more height. Text stays
# legible at arm's length; raise PANEL_SCALE toward 1 if it does not.
SCALE="${PANEL_SCALE:-0.8}"
PROFILE="$HOME/.config/chromium-panel"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"

# Chromium refuses to start again if it thinks it crashed, and a wall panel
# cannot answer a dialogue. Clearing these two makes the restart silent.
if [ -d "$PROFILE/Default" ]; then
    sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/' \
        "$PROFILE/Default/Preferences" 2>/dev/null || true
fi

# Only ever one panel browser.
pkill -f "user-data-dir=$PROFILE" 2>/dev/null
sleep 2

exec /usr/bin/chromium \
    --ozone-platform=wayland \
    --enable-wayland-ime \
    --kiosk \
    --user-data-dir="$PROFILE" \
    --unsafely-treat-insecure-origin-as-secure="$ORIGIN" \
    --force-device-scale-factor="$SCALE" \
    --password-store=basic \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-features=Translate,TranslateUI \
    --check-for-update-interval=31536000 \
    --overscroll-history-navigation=0 \
    --disable-pinch \
    --autoplay-policy=no-user-gesture-required \
    "$URL"
