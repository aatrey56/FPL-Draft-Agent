#!/usr/bin/env bash
# Near-live matchday loop: live points every 60s, full refresh + derive every
# 10 minutes (keeps waiver/my_week artifacts and non-live files current too).
# Usage: make matchday   (Ctrl-C when the day's fixtures end)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "near-live matchday loop: live points every 60s, full refresh every 10 min (Ctrl-C to stop)"
i=0
while true; do
  if [ $((i % 10)) -eq 0 ]; then
    make fetch derive || echo "full refresh failed — retrying next cycle"
  else
    make livefetch || echo "live fetch failed — retrying next cycle"
  fi
  i=$((i + 1))
  sleep 60
done
