#!/usr/bin/env bash
# Remove the macOS autopilot agents. Usage: make autopilot-off
set -euo pipefail
UID_N="$(id -u)"
# shellcheck source=launchd_lib.sh
. "$(dirname "$0")/launchd_lib.sh"
for label in com.fplcopilot.server com.fplcopilot.refresh com.fplcopilot.deadline; do
  if launchctl print "gui/$UID_N/$label" >/dev/null 2>&1; then
    unload_agent "$UID_N" "$label" && echo "stopped $label"
  else
    echo "$label not loaded"
  fi
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
echo "Autopilot off."
