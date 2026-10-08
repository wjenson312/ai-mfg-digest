#!/bin/bash
# Installs the Tuesday 10am schedule (macOS launchd).
# Run this ONCE, after .env is filled in and --check-smtp passes.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.aimfgdigest.weekly"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "Project: $PROJECT_DIR"

# Refuse to schedule a job that cannot possibly work.
if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo "ERROR: no .env file. Copy .env.example to .env and fill it in first." >&2
  exit 1
fi
if ! grep -qE '^ANTHROPIC_API_KEY=.+' "$PROJECT_DIR/.env"; then
  echo "ERROR: ANTHROPIC_API_KEY is empty in .env." >&2
  exit 1
fi
if ! grep -qE '^SMTP_HOST=.+' "$PROJECT_DIR/.env"; then
  echo "ERROR: SMTP_HOST is empty in .env. Run --check-smtp once it's set." >&2
  exit 1
fi

# Pin the interpreter that actually has the packages. A scheduled job does not
# inherit your shell's PATH, and the system /usr/bin/python3 almost certainly
# lacks `anthropic` -- this is the #1 cause of a job that "silently stopped".
PYTHON="$(command -v python3)"
if ! "$PYTHON" -c "import anthropic, requests, pydantic" 2>/dev/null; then
  echo "ERROR: $PYTHON cannot import the dependencies. Install them first:" >&2
  echo "    $PYTHON -m pip install anthropic requests pydantic markdown" >&2
  exit 1
fi
echo "$PYTHON" > "$PROJECT_DIR/.python_path"
echo "Python:  $PYTHON"

mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    "$PROJECT_DIR/com.aimfgdigest.weekly.plist" > "$PLIST_DEST"

# Replace any previous version of the job.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"

echo ""
echo "Scheduled: every Tuesday at 10:00."
echo "  installed:  $PLIST_DEST"
echo "  logs:       $PROJECT_DIR/run.log"
echo ""
echo "Useful commands:"
echo "  launchctl print gui/$(id -u)/$LABEL     # is it loaded?"
echo "  launchctl kickstart -k gui/$(id -u)/$LABEL   # run it right now (SENDS EMAIL)"
echo "  launchctl bootout gui/$(id -u)/$LABEL   # cancel the schedule"
