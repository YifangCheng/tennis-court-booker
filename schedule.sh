#!/usr/bin/env bash
# Schedules your Mac to:
#   1. Wake from sleep shortly before the configured site release time (via pmset)
#   2. Run the booking script at the site's pre-login time (via LaunchAgent)
#
# Usage: bash schedule.sh --site SITE_NAME
# To cancel: bash schedule.sh --uninstall

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
PLIST_PATH="$HOME/Library/LaunchAgents/com.tennis.booker.plist"
SITE_NAME=""
LOG_DIR="$SCRIPT_DIR/logs"

# ── Uninstall ────────────────────────────────────────────────────────────────
if [[ "$1" == "--uninstall" ]]; then
    echo "Uninstalling LaunchAgent …"
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    sudo pmset schedule cancelall 2>/dev/null || true
    echo "Done. LaunchAgent and scheduled wake removed."
    exit 0
fi

# ── Parse args ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --site)
            SITE_NAME="${2:-}"
            shift 2
            ;;
        *)
            echo "ERROR: Unknown argument: $1"
            echo "Usage: bash schedule.sh --site SITE_NAME"
            echo "       bash schedule.sh --uninstall"
            exit 1
            ;;
    esac
done

if [[ -z "$SITE_NAME" ]]; then
    echo "ERROR: Missing --site SITE_NAME"
    echo "Usage: bash schedule.sh --site SITE_NAME"
    exit 1
fi

# ── Validate ─────────────────────────────────────────────────────────────────
if [ ! -f "$PYTHON" ]; then
    echo "ERROR: .venv not found. Run 'bash setup.sh' first."
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "ERROR: .env not found. Run 'bash setup.sh' first and fill in your credentials."
    exit 1
fi

mkdir -p "$LOG_DIR"

CONFIG_PATH="$SCRIPT_DIR/sites/$SITE_NAME/config.json"
if [ ! -f "$CONFIG_PATH" ]; then
    echo "ERROR: config not found for site '$SITE_NAME' at $CONFIG_PATH"
    exit 1
fi

SCHEDULE_INFO=$("$PYTHON" - "$CONFIG_PATH" <<'PY'
import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

config_path = sys.argv[1]
with open(config_path) as handle:
    config = json.load(handle)

timezone = ZoneInfo(config.get("timezone", "Europe/London"))
pre_login_seconds = int(config.get("pre_login_seconds", 120))
release_hour = int(config.get("release_hour", 0))
release_minute = int(config.get("release_minute", 0))

now = datetime.now(timezone)
release = now.replace(hour=release_hour, minute=release_minute, second=0, microsecond=0)
if release <= now:
    release += timedelta(days=1)

start = release - timedelta(seconds=pre_login_seconds)
wake = start - timedelta(minutes=1)

print(wake.strftime("%m/%d/%Y"))
print(wake.strftime("%H:%M:%S"))
print(start.strftime("%H"))
print(start.strftime("%M"))
print(start.strftime("%H:%M"))
print(release.strftime("%H:%M"))
PY
)

WAKE_DATE=$(printf '%s\n' "$SCHEDULE_INFO" | sed -n '1p')
WAKE_TIME=$(printf '%s\n' "$SCHEDULE_INFO" | sed -n '2p')
START_HOUR=$(printf '%s\n' "$SCHEDULE_INFO" | sed -n '3p')
START_MINUTE=$(printf '%s\n' "$SCHEDULE_INFO" | sed -n '4p')
START_TIME=$(printf '%s\n' "$SCHEDULE_INFO" | sed -n '5p')
RELEASE_TIME=$(printf '%s\n' "$SCHEDULE_INFO" | sed -n '6p')

# ── 1. Schedule Mac wake before the configured release window ───────────────
echo "Scheduling Mac wake at $WAKE_TIME on $WAKE_DATE …"
echo "(You may be prompted for your Mac password — this is for pmset)"
sudo pmset schedule wake "$WAKE_DATE $WAKE_TIME"
echo "Wake scheduled."

# ── 2. Install LaunchAgent (runs at 23:58 every night) ───────────────────────
echo ""
echo "Installing LaunchAgent at $PLIST_PATH …"

cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.tennis.booker</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT_DIR/main.py</string>
        <string>--site</string>
        <string>$SITE_NAME</string>
    </array>

    <!-- Run daily at the site's configured pre-login time -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>   <integer>$START_HOUR</integer>
        <key>Minute</key> <integer>$START_MINUTE</integer>
    </dict>

    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>

    <key>StandardOutPath</key>
    <string>$LOG_DIR/booker.log</string>

    <key>StandardErrorPath</key>
    <string>$LOG_DIR/booker_error.log</string>

    <!-- Keep HOME for the Python process environment -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
</dict>
</plist>
PLIST

# Load the agent
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load  "$PLIST_PATH"
echo "LaunchAgent loaded."

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "=== All done! ==="
echo ""
echo "Scheduled site: $SITE_NAME"
echo ""
echo "Tonight:"
echo "  Wake: $WAKE_TIME"
echo "  Start: $START_TIME"
echo "  Release: $RELEASE_TIME"
echo ""
echo "Logs: $LOG_DIR/booker.log"
echo "Screenshots: $SCRIPT_DIR/screenshots/"
echo ""
echo "IMPORTANT — keep your Mac:"
echo "  • Plugged in to power (sleep wake only works when charging)"
echo "  • Lid can be closed — that's fine"
echo "  • Connected to Wi-Fi (screen lock is fine)"
echo ""
echo "To cancel everything: bash schedule.sh --uninstall"
