"""pytest configuration: shared fixtures for the weekly waiver / my_week CLIs."""
import json
from pathlib import Path

import pandas as pd
import pytest

WEEKLY_SEASON = "2026-27"
WEEKLY_LEAGUE, WEEKLY_ENTRY = 7, 42


@pytest.fixture
def weekly_cli_root(tmp_path: Path) -> Path:
    """A minimal data root the waiver / my_week CLIs can run against offline.

    Season 2026-27, GW5 finished and GW6 next (draft bootstrap shape), one
    GW6 fixture, my squad = {MyFWD}, free agents = {FreeFWD, Promoted}. No
    xP file is written — tests add ``derived/2026-27/ml/xp_gw6.parquet``.
    """
    raw = tmp_path / "raw" / WEEKLY_SEASON
    bootstrap = {
        "teams": [{"id": 1, "name": "Arsenal", "short_name": "ARS"},
                  {"id": 2, "name": "Wolves", "short_name": "WOL"}],
        "events": {"current": 5, "next": 6, "data": [
            {"id": 5, "finished": True, "deadline_time": "2026-09-18T17:30:00Z"},
            {"id": 6, "finished": False, "deadline_time": "2099-10-10T10:00:00Z"}]},
        "fixtures": {"6": [{"team_h": 1, "team_a": 2}]},
        "elements": [
            {"id": 1, "code": 101, "web_name": "MyFWD", "element_type": 4, "team": 2, "status": "a"},
            {"id": 2, "code": 102, "web_name": "FreeFWD", "element_type": 4, "team": 1, "status": "a"},
            {"id": 3, "code": 103, "web_name": "Promoted", "element_type": 4, "team": 1, "status": "a"}],
    }
    (raw / "bootstrap").mkdir(parents=True)
    (raw / "bootstrap/bootstrap-static.json").write_text(json.dumps(bootstrap))
    league = raw / "league" / str(WEEKLY_LEAGUE)
    league.mkdir(parents=True)
    (league / "element-status.json").write_text(json.dumps({"element_status": [
        {"element": 1, "owner": WEEKLY_ENTRY}, {"element": 2, "owner": None},
        {"element": 3, "owner": None}]}))
    derived = tmp_path / "derived/ml"
    derived.mkdir(parents=True)
    pd.DataFrame([
        {"season": "2025-26", "team_name": "Arsenal", "total_points": 2000},
        {"season": "2025-26", "team_name": "Wolves", "total_points": 1200},
    ]).to_parquet(derived / "player_seasons.parquet")
    (derived / "projections_2627.json").write_text(json.dumps([
        {"code": 101, "projected_points": 60.0}, {"code": 102, "projected_points": 120.0}]))
    (tmp_path / "derived" / WEEKLY_SEASON / "ml").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def weekly_cli_argv(weekly_cli_root: Path):
    """argv builder for ``waiver.main`` / ``myweek.main`` on ``weekly_cli_root``."""
    def argv(out: Path, scorer: str) -> list[str]:
        return ["--season", WEEKLY_SEASON, "--league", str(WEEKLY_LEAGUE),
                "--entry", str(WEEKLY_ENTRY), "--data-root", str(weekly_cli_root),
                "--out", str(out), "--scorer", scorer]
    return argv
