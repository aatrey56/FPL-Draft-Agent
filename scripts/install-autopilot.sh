#!/usr/bin/env bash
# Install the macOS autopilot: two launchd agents —
#   com.fplcopilot.server   — the MCP server, always on (restarts if it dies)
#   com.fplcopilot.refresh  — fetch + derive every 15 minutes
# Usage: make autopilot     (undo: make autopilot-off)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
UID_N="$(id -u)"
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

for label in com.fplcopilot.server com.fplcopilot.refresh; do
  launchctl bootout "gui/$UID_N/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_N" "$AGENTS/$label.plist"
  echo "loaded $label"
done

echo
echo "Autopilot on: server always running on :8080, data+artifacts refresh every 15 min."
echo "Logs: ~/.fplcopilot/*.log   ·   After a git pull: make update   ·   Undo: make autopilot-off"
