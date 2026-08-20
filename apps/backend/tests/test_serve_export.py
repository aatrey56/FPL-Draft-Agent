"""Tests for serve_export (player_history.json). No network."""
import json
import pandas as pd
from backend.ml import serve_export as se


def test_build_history_orders_seasons_and_nulls(tmp_path):
    frame = pd.DataFrame([
        {"code": 7, "season": "2025-26", "team_name": "Arsenal", "minutes": 900,
         "total_points": 123, "goals_scored": 3, "assists": 1,
         "expected_goals": None, "expected_assists": 0.5},
        {"code": 7, "season": "2024-25", "team_name": "Arsenal", "minutes": 3374,
         "total_points": 344, "goals_scored": 29, "assists": 18,
         "expected_goals": 24.0, "expected_assists": 12.0},
    ])
    history = se.build_history(frame)
    rows = history["7"]
    assert [r["season"] for r in rows] == ["2024-25", "2025-26"]  # chronological
    assert rows[0]["total_points"] == 344.0
    assert rows[1]["expected_goals"] is None                      # null, not 0
    assert json.dumps(history)                                    # JSON-serializable
