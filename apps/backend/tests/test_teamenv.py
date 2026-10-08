"""Tests for team_env (backend.ml.teamenv). Fixtures only."""

import json

import pandas as pd
import pytest

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


def _promoted_panel():
    """2026-27 rows only: Hull City (promoted) hosts Arsenal in GW1."""
    return pd.DataFrame([
        {"season": "2026-27", "gw": 1, "team_id": 9, "opponent_team": 1,
         "opponent_name": "Arsenal", "was_home": True, "num_fixtures": 1,
         "element_type": 4, "total_points": 5, "expected_goals": 0.4},
        {"season": "2026-27", "gw": 1, "team_id": 1, "opponent_team": 9,
         "opponent_name": "Hull City", "was_home": False, "num_fixtures": 1,
         "element_type": 3, "total_points": 12, "expected_goals": 1.1},
    ])


def test_promoted_club_rated_in_current_season():
    env = te.build_team_env(_promoted_panel(), "2026-27")
    assert set(env) == {"Arsenal", "Hull City"}
    assert env["Hull City"]["defense"]["pts_conceded_pg_at_home"] == 12.0


def test_season_ml_dir_nests_current_seasons(tmp_path):
    assert te.season_ml_dir("2026-27", tmp_path) == tmp_path / "derived/2026-27/ml"
    assert te.season_ml_dir("2025-26", tmp_path) == tmp_path / "derived/ml"


def _write_flat_archive(data_root):
    flat = data_root / "derived/ml/team_env.json"
    flat.parent.mkdir(parents=True)
    flat.write_text('{"season": "2025-26", "teams": {"Wolves": {}}}')
    return flat


def test_main_writes_season_nested_and_leaves_archive(tmp_path):
    flat = _write_flat_archive(tmp_path)
    ml = tmp_path / "derived/2026-27/ml"
    ml.mkdir(parents=True)
    _promoted_panel().to_parquet(ml / "player_gameweeks.parquet", index=False)

    assert te.main(["--season", "2026-27", "--data-root", str(tmp_path)]) == 0

    doc = json.loads((ml / "team_env.json").read_text())
    assert doc["season"] == "2026-27"
    assert "Hull City" in doc["teams"] and "Wolves" not in doc["teams"]
    assert json.loads(flat.read_text())["teams"] == {"Wolves": {}}


def test_main_missing_season_panel_exits_without_writing(tmp_path, capsys):
    flat = _write_flat_archive(tmp_path)
    with pytest.raises(SystemExit) as exc:
        te.main(["--season", "2026-27", "--data-root", str(tmp_path)])
    assert exc.value.code == 2
    assert "no panel for season 2026-27" in capsys.readouterr().err
    assert not (tmp_path / "derived/2026-27/ml/team_env.json").exists()
    assert json.loads(flat.read_text())["season"] == "2025-26"


def test_main_panel_without_season_rows_is_an_error(tmp_path):
    ml = tmp_path / "derived/2027-28/ml"
    ml.mkdir(parents=True)
    _promoted_panel().to_parquet(ml / "player_gameweeks.parquet", index=False)
    with pytest.raises(SystemExit) as exc:
        te.main(["--season", "2027-28", "--data-root", str(tmp_path)])
    assert exc.value.code == 2
    assert not (ml / "team_env.json").exists()


def test_main_refuses_current_season_over_flat_archive(tmp_path):
    flat = _write_flat_archive(tmp_path)
    panel = tmp_path / "panel.parquet"
    _promoted_panel().to_parquet(panel, index=False)
    with pytest.raises(SystemExit) as exc:
        te.main(["--season", "2026-27", "--data-root", str(tmp_path),
                 "--panel", str(panel), "--out", str(flat)])
    assert exc.value.code == 2
    assert json.loads(flat.read_text())["season"] == "2025-26"
