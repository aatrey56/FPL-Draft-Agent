"""Tests for Phase A historical ingestion (backend.ml.history).

No network: every test feeds small in-memory CSV / JSON fixtures through the
pure parse/build functions. Covers the acceptance criteria in
``backend/ml/HISTORY_INGEST_SPEC.md``.
"""

import csv
import io

import pandas as pd
import pytest

from backend.ml import history

TEAMS_CSV = "id,name\n1,Arsenal\n13,Man City\n"


def _players_csv(rows: list[dict], columns: list[str]) -> str:
    """Render dict rows as a vaastav-style players_raw.csv string."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def test_code_is_stable_cross_season_join_key():
    """A player keeps one `code` across seasons even as `id` is reassigned."""
    cols = ["code", "id", "web_name", "team", "minutes", "total_points", "expected_goals"]
    s2324 = _players_csv(
        [{"code": 223094, "id": 351, "web_name": "Haaland", "team": 13,
          "minutes": 2736, "total_points": 181, "expected_goals": 21.9}],
        cols,
    )
    s2425 = _players_csv(
        [{"code": 223094, "id": 360, "web_name": "Haaland", "team": 13,
          "minutes": 2900, "total_points": 200, "expected_goals": 24.0}],
        cols,
    )
    rows = (
        history.parse_vaastav_players(s2324, TEAMS_CSV, "2023-24")
        + history.parse_vaastav_players(s2425, TEAMS_CSV, "2024-25")
    )
    frame = history.build_dataframe(rows)

    haaland = frame[frame["code"] == 223094]
    assert len(haaland) == 2
    assert haaland["season"].nunique() == 2
    assert haaland["code"].nunique() == 1
    assert set(haaland["season_id"]) == {351, 360}


def test_duplicate_code_season_raises():
    """Two rows for the same (code, season) is a hard error, not a silent dup."""
    cols = ["code", "id", "web_name", "team"]
    dup = _players_csv(
        [{"code": 1, "id": 10, "web_name": "A", "team": 1},
         {"code": 1, "id": 11, "web_name": "A", "team": 1}],
        cols,
    )
    rows = history.parse_vaastav_players(dup, TEAMS_CSV, "2024-25")
    with pytest.raises(ValueError, match="duplicate"):
        history.build_dataframe(rows)


def test_xg_is_null_not_zero_pre_2022_23():
    """Seasons lacking the expected_goals column get null, never 0."""
    cols = ["code", "id", "web_name", "team", "minutes", "total_points"]  # no expected_goals
    text = _players_csv(
        [{"code": 5, "id": 5, "web_name": "OldGuy", "team": 1, "minutes": 900, "total_points": 50}],
        cols,
    )
    frame = history.build_dataframe(history.parse_vaastav_players(text, TEAMS_CSV, "2019-20"))

    assert frame["expected_goals"].isna().all()
    assert not (frame["expected_goals"] == 0).any()


def test_numeric_coercion_handles_empty_and_garbage():
    """Empty / non-numeric cells coerce to null without crashing; '12.0' -> 12."""
    cols = ["code", "id", "web_name", "team", "minutes", "ict_index", "total_points"]
    text = _players_csv(
        [{"code": 7, "id": 7, "web_name": "X", "team": 1,
          "minutes": "", "ict_index": "garbage", "total_points": "12.0"}],
        cols,
    )
    frame = history.build_dataframe(history.parse_vaastav_players(text, TEAMS_CSV, "2024-25"))

    assert pd.isna(frame.loc[0, "minutes"])
    assert pd.isna(frame.loc[0, "ict_index"])
    assert frame.loc[0, "total_points"] == 12


def test_local_bootstrap_maps_team_name_and_nulls_now_cost():
    """The draft bootstrap has no price, so now_cost is null; team_name resolves."""
    bootstrap = {
        "elements": [{"code": 223094, "id": 430, "web_name": "Haaland",
                      "element_type": 4, "team": 13, "minutes": 2953,
                      "total_points": 239, "expected_goals": "25.50"}],
        "teams": [{"id": 13, "name": "Man City", "short_name": "MCI"}],
    }
    frame = history.build_dataframe(history.parse_local_bootstrap(bootstrap, "2025-26"))

    assert frame.loc[0, "team_name"] == "Man City"
    assert pd.isna(frame.loc[0, "now_cost"])
    assert frame.loc[0, "expected_goals"] == 25.5
