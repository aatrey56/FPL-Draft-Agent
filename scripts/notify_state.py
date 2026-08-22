#!/usr/bin/env python3
"""Deadline + state-transition notifications for the autopilot (stdlib only).

Runs after every autorefresh tick. Reads the season bootstrap (event
deadlines) and game.json (league clock), compares against the last-seen
state in ~/.fplcopilot/notify_state.json, and emits macOS notifications
(osascript) for things a manager must not miss:

  * gameweek finished          -> "GW N final — ask Claude for gw_report + waiver plan"
  * waivers processed          -> "free agency open until lineup lock"
  * trades due   in <24h       (once per event)
  * waivers due  in <24h, <3h  (once each per event)
  * lineup lock  in <24h, <2h  (once each per event)

Pure logic lives in compute_notifications() so it can be unit-tested;
side effects (files, osascript) stay in main().
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

THRESHOLDS = {
    "trades_time": [("24h", timedelta(hours=24))],
    "waivers_time": [("24h", timedelta(hours=24)), ("3h", timedelta(hours=3))],
    "deadline_time": [("24h", timedelta(hours=24)), ("2h", timedelta(hours=2))],
}
LABELS = {"trades_time": "trades due", "waivers_time": "waivers due",
          "deadline_time": "lineup lock"}


def parse_ts(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def est(ts: datetime) -> str:
    # Fixed-offset Eastern is fine for a notification (EDT in season).
    return ts.astimezone(timezone(timedelta(hours=-4))).strftime("%a %I:%M %p") + " EST"


def compute_notifications(events: list[dict], game: dict, state: dict,
                          now: datetime) -> tuple[list[str], dict]:
    """-> (messages, new_state). state keys mark what was already announced."""
    messages: list[str] = []
    new_state = dict(state)

    current = game.get("current_event") or 0
    if game.get("current_event_finished") and state.get("finished_gw") != current:
        messages.append(f"GW{current} is final — ask Claude for gw_report and your waiver plan")
        new_state["finished_gw"] = current
    if game.get("waivers_processed") and state.get("waivers_processed_gw") != game.get("next_event"):
        messages.append(f"GW{game.get('next_event')} waivers processed — free agency open until lineup lock")
        new_state["waivers_processed_gw"] = game.get("next_event")

    for event in events:
        for field, thresholds in THRESHOLDS.items():
            ts = parse_ts(event.get(field, ""))
            if ts is None or ts <= now:
                continue
            for tag, delta in thresholds:
                key = f"{event['id']}:{field}:{tag}"
                if now >= ts - delta and key not in new_state.get("announced", []):
                    messages.append(
                        f"GW{event['id']} {LABELS[field]} {est(ts)} — "
                        f"{'under ' + tag + ' left' if tag != '24h' else 'within 24h'}")
                    new_state.setdefault("announced", []).append(key)
    return messages, new_state


def notify(message: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "FPL Co-Pilot"'],
            check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        print(f"[notify] {message}")


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    season = sys.argv[1] if len(sys.argv) > 1 else "2026-27"
    try:
        bootstrap = json.loads((repo / f"data/raw/{season}/bootstrap/bootstrap-static.json").read_text())
        game = json.loads((repo / f"data/raw/{season}/game/game.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[notify] no data yet: {exc}")
        return 0
    events = bootstrap.get("events", {}).get("data", [])

    state_path = Path.home() / ".fplcopilot/notify_state.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}

    messages, new_state = compute_notifications(events, game, state, datetime.now(timezone.utc))
    for message in messages:
        notify(message)
        print(f"[notify] {message}")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(new_state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
