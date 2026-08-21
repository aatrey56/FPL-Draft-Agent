#!/usr/bin/env bash
# Matchday refresh loop: keep gw_live fresh while games are on.
# Usage: make matchday   (or: bash scripts/matchday.sh [interval_seconds])
# Ctrl-C to stop when the day's fixtures finish.
set -euo pipefail
INTERVAL="${1:-300}"

cd "$(dirname "$0")/.."
echo "matchday loop: refreshing every ${INTERVAL}s (Ctrl-C to stop)"
while true; do
  make fetch || echo "fetch failed — retrying next cycle"
  date
  sleep "$INTERVAL"
done
