#!/usr/bin/env bash
# Pause or resume the autopilot agents without uninstalling.
# Usage: autopilot-ctl.sh stop|start   (make stop / make start)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=launchd_lib.sh
. "$HERE/launchd_lib.sh"
UID_N="$(id -u)"
AGENTS="$HOME/Library/LaunchAgents"
rc=0
for label in com.fplcopilot.server com.fplcopilot.refresh com.fplcopilot.deadline; do
  case "${1:-}" in
    stop) unload_agent "$UID_N" "$label" || rc=1 ;;
    start) load_agent "$UID_N" "$label" "$AGENTS/$label.plist" || rc=1 ;;
    *) echo "usage: $0 stop|start" >&2; exit 2 ;;
  esac
done
exit "$rc"
