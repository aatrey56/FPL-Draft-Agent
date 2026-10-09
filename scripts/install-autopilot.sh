#!/usr/bin/env bash
# Install the macOS autopilot: three launchd agents —
#   com.fplcopilot.server   — the MCP server, always on (restarts if it dies)
#   com.fplcopilot.refresh  — fetch + derive every 15 minutes
#   com.fplcopilot.deadline — deadline-checklist tick every 5 minutes (docs/DEADLINE_AGENT.md)
# Usage: make autopilot     (undo: make autopilot-off)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
UID_N="$(id -u)"
# shellcheck source=launchd_lib.sh
. "$(dirname "$0")/launchd_lib.sh"
mkdir -p "$AGENTS" "$HOME/.fplcopilot"

plist() { # label, program
  cat > "$AGENTS/$1.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>-lc</string><string>$2</string>
  </array>
  $3
  <key>StandardOutPath</key><string>$HOME/.fplcopilot/$1.log</string>
  <key>StandardErrorPath</key><string>$HOME/.fplcopilot/$1.log</string>
</dict></plist>
EOF
}

plist com.fplcopilot.server \
  "cd $REPO/apps/mcp-server && exec go run ./fpl-server --raw-root ../../data/raw --derived-root ../../data/derived --default-season \${SEASON:-2026-27}" \
  "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"

plist com.fplcopilot.refresh \
  "bash $REPO/scripts/autorefresh.sh" \
  "<key>StartInterval</key><integer>900</integer><key>RunAtLoad</key><true/>"

plist com.fplcopilot.deadline \
  "bash $REPO/scripts/deadline_tick.sh" \
  "<key>StartInterval</key><integer>300</integer><key>RunAtLoad</key><true/>"

rc=0
for label in com.fplcopilot.server com.fplcopilot.refresh com.fplcopilot.deadline; do
  unload_agent "$UID_N" "$label" || true
  if load_agent "$UID_N" "$label" "$AGENTS/$label.plist"; then
    echo "loaded $label"
  else
    rc=1
  fi
done

echo
echo "Autopilot on: server always running on :8080, data+artifacts refresh every 15 min,
deadline checklists every 5-min tick (set USER_TZ / AWAKE_START / AWAKE_END in .env)."
echo "Logs: ~/.fplcopilot/*.log   ·   After a git pull: make update   ·   Undo: make autopilot-off"
exit "$rc"
