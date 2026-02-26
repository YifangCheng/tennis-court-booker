#!/usr/bin/env bash
# Schedules your Mac to:
#   1. Wake from sleep at 23:57 tonight (via pmset)
#   2. Run the booking script at 23:58 (via LaunchAgent)
#
# Usage: bash schedule.sh
# To cancel: bash schedule.sh --uninstall

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/venv/bin/python"
PLIST_PATH="$HOME/Library/LaunchAgents/com.tennis.booker.plist"

# ── Uninstall ────────────────────────────────────────────────────────────────
if [[ "$1" == "--uninstall" ]]; then
    echo "Uninstalling LaunchAgent …"
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    sudo pmset schedule cancelall 2>/dev/null || true
    echo "Done. LaunchAgent and scheduled wake removed."
    exit 0
fi

# ── Validate ─────────────────────────────────────────────────────────────────
if [ ! -f "$PYTHON" ]; then
    echo "ERROR: venv not found. Run 'bash setup.sh' first."
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "ERROR: .env not found. Run 'bash setup.sh' first and fill in your credentials."
    exit 1
fi

# ── 1. Schedule Mac wake at 23:57 tonight ───────────────────────────────────
TONIGHT=$(date "+%m/%d/%Y")
echo "Scheduling Mac wake at 23:57:00 on $TONIGHT …"
echo "(You may be prompted for your Mac password — this is for pmset)"
sudo pmset schedule wake "$TONIGHT 23:57:00"
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
        <string>$SCRIPT_DIR/booker.py</string>
    </array>

    <!-- Run at 23:58 every night -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>   <integer>23</integer>
        <key>Minute</key> <integer>58</integer>
    </dict>

    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>

    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/booker.log</string>

    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/booker_error.log</string>

    <!-- Keep HOME so python-dotenv can find the .env file -->
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
echo "Tonight:"
echo "  23:57 — Mac wakes from sleep"
echo "  23:58 — Booking script starts, logs in, pre-loads the page"
echo "  00:00 — Script fires the booking at exactly midnight"
echo ""
echo "Logs: $SCRIPT_DIR/booker.log"
echo "Screenshots: $SCRIPT_DIR/screenshots/"
echo ""
echo "IMPORTANT — keep your Mac:"
echo "  • Plugged in to power (sleep wake only works when charging)"
echo "  • Lid can be closed — that's fine"
echo "  • Connected to Wi-Fi (screen lock is fine)"
echo ""
echo "To cancel everything: bash schedule.sh --uninstall"
