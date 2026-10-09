#!/usr/bin/env bash
# Remove the macOS autopilot agents. Usage: make autopilot-off
# Exits 1 if any agent is still loaded after the bounded bootout wait; that
# agent's plist is kept so the install state stays truthful and a re-run retries.
set -euo pipefail
UID_N="$(id -u)"
# shellcheck source=launchd_lib.sh
. "$(dirname "$0")/launchd_lib.sh"
rc=0
for label in com.fplcopilot.server com.fplcopilot.refresh com.fplcopilot.deadline; do
  if launchctl print "gui/$UID_N/$label" >/dev/null 2>&1; then
    if unload_agent "$UID_N" "$label"; then
      echo "stopped $label"
    else
      echo "error: $label is still loaded; kept its plist (re-run make autopilot-off)" >&2
      rc=1
      continue
    fi
  else
    echo "$label not loaded"
  fi
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
if [ "$rc" -eq 0 ]; then
  echo "Autopilot off."
else
  echo "Autopilot only partly removed." >&2
fi
exit "$rc"
