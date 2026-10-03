"""Tests for Phase A.2 per-gameweek panel (backend.ml.gameweeks).

No network: small in-memory bootstrap / live.json fixtures through the pure
parse/build functions. Covers the acceptance criteria in
``backend/ml/GAMEWEEK_INGEST_SPEC.md``.
"""

import json

import pandas as pd
import pytest

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


def test_guard_blocks_new_season_write_to_flat_archive(tmp_path):
    flat = tmp_path / "data/derived/ml/player_gameweeks.parquet"
    with pytest.raises(ValueError, match="archive"):
        gameweeks.guard_archive_write(flat, "2026-27", flat)


def test_guard_allows_archive_season_and_nested_path(tmp_path):
    flat = tmp_path / "data/derived/ml/player_gameweeks.parquet"
    gameweeks.guard_archive_write(flat, "2025-26", flat)
    nested = tmp_path / "data/derived/2026-27/ml/p.parquet"
    gameweeks.guard_archive_write(nested, "2026-27", flat)


def test_guard_blocks_case_variant_of_existing_archive(tmp_path):
    flat = tmp_path / "data/derived/ml/player_gameweeks.parquet"
    flat.parent.mkdir(parents=True)
    flat.write_bytes(b"x")
    variant = tmp_path / "DATA/derived/ml/Player_Gameweeks.parquet"
    if not variant.exists():  # case-sensitive filesystem: nothing to alias
        variant = flat
    with pytest.raises(ValueError, match="archive"):
        gameweeks.guard_archive_write(variant, "2026-27", flat)


def test_guard_blocks_case_variant_of_missing_archive(tmp_path):
    flat = tmp_path / "data/derived/ml/player_gameweeks.parquet"
    variant = tmp_path / "DATA/derived/ml/PLAYER_gameweeks.parquet"
    with pytest.raises(ValueError, match="archive"):
        gameweeks.guard_archive_write(variant, "2026-27", flat)


def _write_season(root, with_partial: bool):
    """Raw tree: GW1 finished, GW2 in progress (live.json optionally present)."""
    bootstrap = dict(BOOTSTRAP)
    bootstrap["events"] = {"current": 2, "next": 3, "data": [
        {"id": 1, "finished": True}, {"id": 2, "finished": False}]}
    (root / "bootstrap").mkdir(parents=True)
    (root / "bootstrap/bootstrap-static.json").write_text(json.dumps(bootstrap))
    live = {"elements": {"430": {"stats": {"minutes": 90}, "explain": _explain(1, 90)}}}
    for gw in (1, 2) if with_partial else (1,):
        (root / "gw" / str(gw)).mkdir(parents=True)
        (root / "gw" / str(gw) / "live.json").write_text(json.dumps(live))


def test_unfinished_gameweek_is_excluded_from_panel(tmp_path):
    """Regression: a partial in-progress GW must not enter the panel."""
    with_partial, clean = tmp_path / "a", tmp_path / "b"
    _write_season(with_partial, True)
    _write_season(clean, False)
    args = ("2026-27", 38)
    got = gameweeks.ingest_local(with_partial / "gw",
                                 with_partial / "bootstrap/bootstrap-static.json", *args)
    want = gameweeks.ingest_local(clean / "gw",
                                  clean / "bootstrap/bootstrap-static.json", *args)
    assert set(got["gw"]) == {1}
    pd.testing.assert_frame_equal(got, want)


def test_main_calls_guard_and_defaults_to_nested_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(gameweeks, "_repo_root", lambda: tmp_path)
    flat = tmp_path / "data/derived/ml/player_gameweeks.parquet"
    with pytest.raises(ValueError, match="archive"):
        gameweeks.main(["--season", "2026-27", "--out", str(flat)])
    _write_season(tmp_path / "data/raw/2026-27", False)
    assert gameweeks.main(["--season", "2026-27"]) == 0
    assert (tmp_path / "data/derived/2026-27/ml/player_gameweeks.parquet").exists()
    assert not flat.exists()
