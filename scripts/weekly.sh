#!/usr/bin/env bash
# The weekly loop, cron-safe: fetch fresh data, rebuild decision artifacts.
# Schedule it (e.g. Mon+Tue mornings after GW lockdown) or run: make weekly
#
#   crontab example (07:00 local, Mon & Tue):
#   0 7 * * 1,2 cd $HOME/Coding/Football/FPL26/fpl-draft-mcp && bash scripts/weekly.sh >> $HOME/.fpl-weekly.log 2>&1
set -euo pipefail

# cron runs with a minimal PATH — make go (Homebrew) and uv reachable.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/local/go/bin:$PATH"

cd "$(dirname "$0")/.."
echo "=== weekly loop $(date) ==="
make fetch
make derive
echo "=== artifacts fresh — ask Claude: 'how did my week go, and what's my waiver plan?' ==="
