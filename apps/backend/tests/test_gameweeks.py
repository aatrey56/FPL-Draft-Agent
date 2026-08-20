"""Tests for Phase A.2 per-gameweek panel (backend.ml.gameweeks).

No network: small in-memory bootstrap / live.json fixtures through the pure
parse/build functions. Covers the acceptance criteria in
``backend/ml/GAMEWEEK_INGEST_SPEC.md``.
"""

import pandas as pd

from backend.ml import gameweeks

# Two teams, three players: a GK on team 13 and a MID on team 1.
BOOTSTRAP = {
    "elements": [
        {"id": 430, "code": 223094, "element_type": 4, "team": 13},  # Haaland
        {"id": 5, "code": 900, "element_type": 3, "team": 1},         # a midfielder
    ],
    "teams": [
        {"id": 1, "name": "Arsenal", "short_name": "ARS"},
        {"id": 13, "name": "Man City", "short_name": "MCI"},
    ],
}


def _explain(fixture_id: int, minutes: int) -> list:
    """One explain entry: [stat_breakdown, fixture_id]."""
    return [[[{"stat": "minutes", "points": 2, "value": minutes}], fixture_id]]


def _maps():
    return gameweeks.build_id_maps(BOOTSTRAP)


def test_id_maps_to_permanent_code():
    """A live.json element id resolves to its permanent code."""
    live = {
        "fixtures": [{"id": 9, "team_h": 13, "team_a": 1}],
        "elements": {"430": {"stats": {"minutes": 90, "total_points": 13, "starts": 1},
                             "explain": _explain(9, 90)}},
    }
    rows = gameweeks.parse_gw_live(live, gw=1, season="2025-26", id_maps=_maps())
    assert len(rows) == 1
    assert rows[0]["code"] == 223094
    assert rows[0]["total_points"] == 13


def test_single_fixture_opponent_and_home():
    """A normal (1-fixture) GW derives opponent + was_home correctly."""
    live = {
        "fixtures": [{"id": 9, "team_h": 13, "team_a": 1}],  # City home vs Arsenal
        "elements": {"430": {"stats": {"minutes": 90, "starts": 1}, "explain": _explain(9, 90)}},
    }
    row = gameweeks.parse_gw_live(live, 1, "2025-26", _maps())[0]
    assert row["num_fixtures"] == 1
    assert row["was_home"] is True
    assert row["opponent_team"] == 1
    assert row["opponent_name"] == "Arsenal"
    assert row["played"] is True and row["started"] is True


def test_double_gameweek_nulls_opponent():
    """A DGW (2 fixtures) sums stats and leaves opponent / was_home null."""
    live = {
        "fixtures": [{"id": 9, "team_h": 13, "team_a": 1}, {"id": 10, "team_h": 2, "team_a": 13}],
        "elements": {"430": {"stats": {"minutes": 180},
                             "explain": _explain(9, 90) + _explain(10, 90)}},
    }
    row = gameweeks.parse_gw_live(live, 1, "2025-26", _maps())[0]
    assert row["num_fixtures"] == 2
    assert row["opponent_team"] is None
    assert row["was_home"] is None


def test_blank_gameweek_no_fixture():
    """A blank GW (0 fixtures, 0 minutes) is played=False, num_fixtures=0."""
    live = {
        "fixtures": [],
        "elements": {"5": {"stats": {"minutes": 0}, "explain": []}},
    }
    row = gameweeks.parse_gw_live(live, 30, "2025-26", _maps())[0]
    assert row["num_fixtures"] == 0
    assert row["played"] is False
    assert row["opponent_team"] is None


def test_no_duplicate_code_season_gw():
    """(code, season, gw) must be unique across the assembled frame."""
    live = {
        "fixtures": [{"id": 9, "team_h": 13, "team_a": 1}],
        "elements": {"430": {"stats": {"minutes": 90}, "explain": _explain(9, 90)}},
    }
    rows = (
        gameweeks.parse_gw_live(live, 1, "2025-26", _maps())
        + gameweeks.parse_gw_live(live, 2, "2025-26", _maps())
    )
    frame = gameweeks.build_dataframe(rows)
    assert len(frame) == 2
    assert not frame.duplicated(subset=["code", "season", "gw"]).any()


def test_unmappable_element_dropped():
    """An element id absent from the bootstrap (no code) is skipped."""
    live = {
        "fixtures": [{"id": 9, "team_h": 13, "team_a": 1}],
        "elements": {"9999": {"stats": {"minutes": 90}, "explain": _explain(9, 90)}},
    }
    rows = gameweeks.parse_gw_live(live, 1, "2025-26", _maps())
    assert rows == []
