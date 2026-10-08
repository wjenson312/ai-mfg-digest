#!/bin/bash
# Weekly digest runner -- this is what the scheduler calls, not the .py directly.
#
# Why a wrapper: a scheduled job starts with almost no environment. No PATH to
# your python, no working directory, no shell profile. Everything the run needs
# is made explicit here.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR" || exit 1

# Absolute path to python -- the single most common reason a scheduled job
# fails. Your shell's `python3` is usually NOT /usr/bin/python3, and the system
# one does not have anthropic/requests/pydantic installed. install_schedule.sh
# writes the correct path into .python_path; this falls back to a search.
if [ -n "${PYTHON_BIN:-}" ]; then
  PYTHON="$PYTHON_BIN"
elif [ -f "$PROJECT_DIR/.python_path" ]; then
  PYTHON="$(cat "$PROJECT_DIR/.python_path")"
else
  PYTHON="$(command -v python3 || echo /usr/bin/python3)"
fi

LOG="$PROJECT_DIR/run.log"

# Fail loudly and early if this interpreter can't import the dependencies,
# rather than producing a confusing traceback at 10am.
if ! "$PYTHON" -c "import anthropic, requests, pydantic" 2>/dev/null; then
  echo "[$(date)] ERROR: $PYTHON is missing dependencies. Run:" >> "$LOG"
  echo "    $PYTHON -m pip install anthropic requests pydantic markdown" >> "$LOG"
  exit 1
fi

{
  echo ""
  echo "================================================================"
  echo "Run started $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "================================================================"
} >> "$LOG"

# --send is what makes this actually email. Drop it to generate the digest
# without sending (useful while you're still tuning).
"$PYTHON" digest_agent.py --send >> "$LOG" 2>&1
STATUS=$?

if [ $STATUS -eq 0 ]; then
  echo "Run finished OK $(date '+%H:%M:%S')" >> "$LOG"
else
  echo "Run FAILED (exit $STATUS) $(date '+%H:%M:%S')" >> "$LOG"
  # Surface failures in macOS Notification Center so a silent Tuesday is noticed.
  if command -v osascript >/dev/null 2>&1; then
    osascript -e 'display notification "The weekly digest failed. Check run.log." with title "AI x Mfg Digest"' 2>/dev/null
  fi
fi

exit $STATUS
