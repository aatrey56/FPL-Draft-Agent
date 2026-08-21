"""Tests for team_env (backend.ml.teamenv). Fixtures only."""

import pandas as pd

from backend.ml import teamenv as te


def _panel():
    rows = []
    # GW1: team 1 (Arsenal) home vs team 2 (Wolves).
    # Arsenal MID scores 10 with 0.8 xG; Wolves FWD scores 2 with 0.2 xG.
    rows.append({"season": "2025-26", "gw": 1, "team_id": 1, "opponent_team": 2,
                 "opponent_name": "Wolves", "was_home": True, "num_fixtures": 1,
                 "element_type": 3, "total_points": 10, "expected_goals": 0.8})
    rows.append({"season": "2025-26", "gw": 1, "team_id": 2, "opponent_team": 1,
                 "opponent_name": "Arsenal", "was_home": False, "num_fixtures": 1,
                 "element_type": 4, "total_points": 2, "expected_goals": 0.2})
    # GW2: reverse fixture; a DGW row for Arsenal (excluded from defense splits).
    rows.append({"season": "2025-26", "gw": 2, "team_id": 1, "opponent_team": 2,
                 "opponent_name": "Wolves", "was_home": False, "num_fixtures": 2,
                 "element_type": 3, "total_points": 14, "expected_goals": 1.2})
    rows.append({"season": "2025-26", "gw": 2, "team_id": 2, "opponent_team": 1,
                 "opponent_name": "Arsenal", "was_home": True, "num_fixtures": 1,
                 "element_type": 4, "total_points": 6, "expected_goals": 0.6})
    # A blank row must be ignored entirely.
    rows.append({"season": "2025-26", "gw": 3, "team_id": 1, "opponent_team": None,
                 "opponent_name": None, "was_home": False, "num_fixtures": 0,
                 "element_type": 3, "total_points": 0, "expected_goals": 0.0})
    return pd.DataFrame(rows)


def test_attack_rates_are_per_fixture():
    env = te.build_team_env(_panel(), "2025-26")
    ars = env["Arsenal"]
    # 24 pts over 3 fixtures (1 + DGW 2); xG (0.8+1.2)/3
    assert ars["attack"]["fixtures"] == 3
    assert ars["attack"]["pts_pg"] == 8.0
    assert ars["attack"]["xg_pg"] == round(2.0 / 3, 2)


def test_defense_attribution_by_position_and_venue():
    env = te.build_team_env(_panel(), "2025-26")
    wol = env["Wolves"]
    # Wolves faced Arsenal's 10-pt MID GW1 only (GW2 Arsenal row is DGW, excluded).
    assert wol["defense"]["pts_conceded_pg"] == 10.0
    assert wol["defense"]["pts_conceded_pg_by_pos"]["MID"] == 10.0
    assert wol["defense"]["pts_conceded_pg_by_pos"]["FWD"] == 0.0
    # Arsenal was home in GW1 -> Wolves conceded those away.
    assert wol["defense"]["pts_conceded_pg_away"] == 10.0


def test_expected_environment_labels():
    env = {
        "A": {"attack": {"pts_pg": 9, "xg_pg": 2.1}, "defense": {
            "pts_conceded_pg": 8, "pts_conceded_pg_at_home": 7, "pts_conceded_pg_away": 9}},
        "B": {"attack": {"pts_pg": 7, "xg_pg": 1.5}, "defense": {
            "pts_conceded_pg": 6, "pts_conceded_pg_at_home": 5, "pts_conceded_pg_away": 7}},
    }
    fx = te.expected_environment(env, "A", "B")
    assert fx["combined_xg_pg"] == 3.6 and fx["environment"] == "shootout"
    assert te.expected_environment(env, "A", "Nope") == {}


def test_empty_season_returns_empty():
    assert te.build_team_env(_panel(), "2019-20") == {}
