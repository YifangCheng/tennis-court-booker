#!/usr/bin/env bash
# Schedules your Mac to:
#   1. Wake from sleep shortly before the site's booking-open time (via pmset)
#   2. Run the booking script at the site's pre-login time (via LaunchAgent)
#
# Usage: bash schedule.sh --site SITE_NAME [--account ACCOUNT_NAME] [--venue VENUE_SLUG] [--time HH:MM] [--court N]
# To cancel: bash schedule.sh --uninstall

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
CAFFEINATE="/usr/bin/caffeinate"
RUNNER="$SCRIPT_DIR/run_scheduled.sh"
SITE_NAME=""
ACCOUNT_NAME=""
VENUE_NAME=""
BOOKING_TIME=""
COURT_NUMBER=""
LOG_DIR="$SCRIPT_DIR/logs"

# ── Uninstall ────────────────────────────────────────────────────────────────
if [[ "$1" == "--uninstall" ]]; then
    echo "Uninstalling LaunchAgents …"
    for plist in "$HOME"/Library/LaunchAgents/com.tennis.booker*.plist; do
        [ -e "$plist" ] || continue
        launchctl unload "$plist" 2>/dev/null || true
        rm -f "$plist"
    done
    sudo pmset schedule cancelall 2>/dev/null || true
    echo "Done. LaunchAgents and scheduled wake removed."
    exit 0
fi

# ── Parse args ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --site)
            SITE_NAME="${2:-}"
            shift 2
            ;;
        --account)
            ACCOUNT_NAME="${2:-}"
            shift 2
            ;;
        --venue)
            VENUE_NAME="${2:-}"
            shift 2
            ;;
        --time)
            BOOKING_TIME="${2:-}"
            shift 2
            ;;
        --court)
            COURT_NUMBER="${2:-}"
            shift 2
            ;;
        *)
            echo "ERROR: Unknown argument: $1"
            echo "Usage: bash schedule.sh --site SITE_NAME [--account ACCOUNT_NAME] [--venue VENUE_SLUG] [--time HH:MM] [--court N]"
            echo "       bash schedule.sh --uninstall"
            exit 1
            ;;
    esac
done

if [[ -z "$SITE_NAME" ]]; then
    echo "ERROR: Missing --site SITE_NAME"
    echo "Usage: bash schedule.sh --site SITE_NAME [--account ACCOUNT_NAME] [--venue VENUE_SLUG] [--time HH:MM] [--court N]"
    exit 1
fi

LABEL_SUFFIX="$SITE_NAME"
LOG_SUFFIX="$SITE_NAME"
LOG_NAME_SUFFIX="$SITE_NAME"
PROGRAM_ACCOUNT_ARGS=""
PROGRAM_VENUE_ARGS=""
PROGRAM_TIME_ARGS=""
PROGRAM_COURT_ARGS=""
if [[ -n "$ACCOUNT_NAME" ]]; then
    SAFE_ACCOUNT_NAME="${ACCOUNT_NAME//[^A-Za-z0-9_-]/_}"
    LABEL_SUFFIX="${SITE_NAME}.${SAFE_ACCOUNT_NAME}"
    LOG_SUFFIX="${SITE_NAME}_${SAFE_ACCOUNT_NAME}"
    LOG_NAME_SUFFIX="${SITE_NAME}_${SAFE_ACCOUNT_NAME}"
    PROGRAM_ACCOUNT_ARGS=$(cat <<EOF
        <string>--account</string>
        <string>$ACCOUNT_NAME</string>
EOF
)
fi

if [[ -n "$VENUE_NAME" ]]; then
    SAFE_VENUE_NAME="${VENUE_NAME//[^A-Za-z0-9_-]/_}"
    LABEL_SUFFIX="${LABEL_SUFFIX}.${SAFE_VENUE_NAME}"
    LOG_SUFFIX="${LOG_SUFFIX}_${SAFE_VENUE_NAME}"
    LOG_NAME_SUFFIX="${LOG_NAME_SUFFIX}_${SAFE_VENUE_NAME}"
    PROGRAM_VENUE_ARGS=$(cat <<EOF
        <string>--venue</string>
        <string>$VENUE_NAME</string>
EOF
)
fi

if [[ -n "$BOOKING_TIME" ]]; then
    SAFE_BOOKING_TIME="${BOOKING_TIME//:/-}"
    LABEL_SUFFIX="${LABEL_SUFFIX}.${SAFE_BOOKING_TIME}"
    LOG_SUFFIX="${LOG_SUFFIX}_${SAFE_BOOKING_TIME}"
    PROGRAM_TIME_ARGS=$(cat <<EOF
        <string>--time</string>
        <string>$BOOKING_TIME</string>
EOF
)
fi

if [[ -n "$COURT_NUMBER" ]]; then
    LABEL_SUFFIX="${LABEL_SUFFIX}.court-${COURT_NUMBER}"
    LOG_SUFFIX="${LOG_SUFFIX}_court-${COURT_NUMBER}"
    PROGRAM_COURT_ARGS=$(cat <<EOF
        <string>--court</string>
        <string>$COURT_NUMBER</string>
EOF
)
fi

PLIST_PATH="$HOME/Library/LaunchAgents/com.tennis.booker.${LABEL_SUFFIX}.plist"
LOG_PATTERN="$LOG_DIR/booker_${LOG_NAME_SUFFIX}_YYYYMMDD.log"

# ── Validate ─────────────────────────────────────────────────────────────────
if [ ! -f "$PYTHON" ]; then
    echo "ERROR: .venv not found. Run 'bash setup.sh' first."
    exit 1
fi

if [ ! -f "$RUNNER" ]; then
    echo "ERROR: scheduled runner not found at $RUNNER"
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
booking_open_time = config.get("booking_open_time", "00:00")
release_hour, release_minute = [int(part) for part in booking_open_time.split(":", 1)]

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

# ── 1. Schedule Mac wake before the booking-open window ─────────────────────
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
    <string>com.tennis.booker.$LABEL_SUFFIX</string>

    <key>ProgramArguments</key>
    <array>
        <string>$RUNNER</string>
        <string>--site</string>
        <string>$SITE_NAME</string>
$PROGRAM_ACCOUNT_ARGS
$PROGRAM_VENUE_ARGS
$PROGRAM_TIME_ARGS
$PROGRAM_COURT_ARGS
    </array>

    <!-- Run daily at the site's configured pre-login time -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>   <integer>$START_HOUR</integer>
        <key>Minute</key> <integer>$START_MINUTE</integer>
    </dict>

    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>

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
if [[ -n "$ACCOUNT_NAME" ]]; then
    echo "Scheduled account: $ACCOUNT_NAME"
fi
if [[ -n "$VENUE_NAME" ]]; then
    echo "Scheduled venue: $VENUE_NAME"
fi
if [[ -n "$BOOKING_TIME" ]]; then
    echo "Scheduled booking time: $BOOKING_TIME"
fi
if [[ -n "$COURT_NUMBER" ]]; then
    echo "Scheduled court: $COURT_NUMBER"
fi
echo ""
echo "Tonight:"
echo "  Wake: $WAKE_TIME"
echo "  Start: $START_TIME"
echo "  Booking Opens: $RELEASE_TIME"
echo ""
echo "Logs: $LOG_PATTERN"
echo "Screenshots: $SCRIPT_DIR/screenshots/"
echo ""
echo "IMPORTANT — keep your Mac:"
echo "  • Plugged in to power (sleep wake only works when charging)"
echo "  • Lid can be closed — that's fine"
echo "  • Connected to Wi-Fi (screen lock is fine)"
echo ""
echo "To cancel everything: bash schedule.sh --uninstall"
