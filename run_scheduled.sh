#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
CAFFEINATE="/usr/bin/caffeinate"
LOG_DIR="$SCRIPT_DIR/logs"

SITE_NAME=""
ACCOUNT_NAME=""
VENUE_NAME=""

ARGS=("$@")

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
        *)
            shift
            ;;
    esac
done

if [[ -z "$SITE_NAME" ]]; then
    echo "ERROR: Missing --site SITE_NAME" >&2
    exit 1
fi

if [[ ! -f "$PYTHON" ]]; then
    echo "ERROR: .venv not found. Run 'bash setup.sh' first." >&2
    exit 1
fi

CONFIG_PATH="$SCRIPT_DIR/sites/$SITE_NAME/config.json"
TIMEZONE="$("$PYTHON" - "$CONFIG_PATH" <<'PY'
import json
import sys

with open(sys.argv[1]) as handle:
    config = json.load(handle)

print(config.get("timezone", "Europe/London"))
PY
)"

DATE_STAMP="$(TZ="$TIMEZONE" date +%Y%m%d)"
SAFE_SITE_NAME="${SITE_NAME//[^A-Za-z0-9_-]/_}"
LOG_SUFFIX="$SAFE_SITE_NAME"

if [[ -n "$ACCOUNT_NAME" ]]; then
    SAFE_ACCOUNT_NAME="${ACCOUNT_NAME//[^A-Za-z0-9_-]/_}"
    LOG_SUFFIX="${LOG_SUFFIX}_${SAFE_ACCOUNT_NAME}"
fi

if [[ -n "$VENUE_NAME" ]]; then
    SAFE_VENUE_NAME="${VENUE_NAME//[^A-Za-z0-9_-]/_}"
    LOG_SUFFIX="${LOG_SUFFIX}_${SAFE_VENUE_NAME}"
fi

mkdir -p "$LOG_DIR"

STDOUT_LOG="$LOG_DIR/booker_${LOG_SUFFIX}_${DATE_STAMP}.log"
STDERR_LOG="$LOG_DIR/booker_${LOG_SUFFIX}_${DATE_STAMP}_error.log"

exec "$CAFFEINATE" -dimsu "$PYTHON" "$SCRIPT_DIR/main.py" "${ARGS[@]}" >>"$STDOUT_LOG" 2>>"$STDERR_LOG"
