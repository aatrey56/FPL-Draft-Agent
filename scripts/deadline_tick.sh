#!/usr/bin/env bash
# Deadline-agent tick: launchd runs this every 5 minutes (make autopilot).
# It is idempotent — it only does work inside a deadline's research/delivery
# window (see docs/DEADLINE_AGENT.md) and otherwise exits in well under a second.
# Non-Mac / cron users can schedule it directly:
#   */5 * * * * bash $HOME/path/to/fpl-draft-mcp/scripts/deadline_tick.sh >> $HOME/.fplcopilot/deadline.log 2>&1
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/local/go/bin:$PATH"
cd "$(dirname "$0")/../apps/backend"
exec uv run python -m backend.ml.deadline_agent tick --season "${SEASON:-2026-27}"
