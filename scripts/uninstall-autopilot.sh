#!/usr/bin/env bash
# Remove the macOS autopilot agents. Usage: make autopilot-off
set -euo pipefail
UID_N="$(id -u)"
for label in com.fplcopilot.server com.fplcopilot.refresh; do
  launchctl bootout "gui/$UID_N/$label" 2>/dev/null && echo "stopped $label" || echo "$label not loaded"
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
echo "Autopilot off."
