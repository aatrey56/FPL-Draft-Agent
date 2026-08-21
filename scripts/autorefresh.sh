#!/usr/bin/env bash
# Autopilot tick: full fetch + derive. Run by launchd every ~15 minutes so
# gw_live, waiver_plan and my_week are always fresh without manual commands.
# Safe to run concurrently-scheduled: a lock directory skips overlapping runs.
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/local/go/bin:$PATH"
cd "$(dirname "$0")/.."

LOCK=".autorefresh.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  # A previous tick is still running (or crashed <30 min ago) — skip this one.
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then rmdir "$LOCK"; fi
  exit 0
fi
trap 'rmdir "$LOCK"' EXIT

echo "=== autorefresh $(date) ==="
make fetch
make derive
