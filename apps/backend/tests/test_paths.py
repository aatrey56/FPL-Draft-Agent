"""Tests for season-aware data paths (backend.ml.paths). tmp_path only."""

import pandas as pd
import pytest

from backend.ml import paths
from backend.ml import waiver as wv


@pytest.mark.parametrize("season, prior", [("2026-27", "2025-26"), ("2099-00", "2098-99"),
                                           ("2000-01", "1999-00")])
def test_prior_season_arithmetic(season, prior):
    assert paths.prior_season(season) == prior


@pytest.mark.parametrize("label", ["2026-28", "2026/27", "26-27", ""])
def test_prior_season_rejects_malformed_labels(label):
    with pytest.raises(ValueError, match="not a season label"):
        paths.prior_season(label)


def test_layout_helpers_follow_data_root(tmp_path):
    assert paths.raw_root("2027-28", tmp_path) == tmp_path / "raw/2027-28"
    assert paths.derived_root("2027-28", tmp_path) == tmp_path / "derived/2027-28"
    assert paths.archive_derived(tmp_path) == tmp_path / "derived"
    assert paths.seasons_table_path(tmp_path) == tmp_path / "derived/ml/player_seasons.parquet"


def test_unknown_season_without_projection_raises_naming_path(tmp_path):
    # the flat 2026-27 file must not leak into another season
    (tmp_path / "derived/ml").mkdir(parents=True)
    (tmp_path / "derived/ml/projections_2627.json").write_text("[]")
    expected = tmp_path / "derived/2027-28/ml/projections.json"
    with pytest.raises(FileNotFoundError, match=str(expected)):
        paths.projections_path("2027-28", tmp_path)


def test_per_season_projection_wins_when_present(tmp_path):
    nested = tmp_path / "derived/2027-28/ml/projections.json"
    nested.parent.mkdir(parents=True)
    nested.write_text("[]")
    assert paths.projections_path("2027-28", tmp_path) == nested


def test_2026_27_resolves_to_flat_preseason_file(tmp_path):
    # regression: 2026-27 has no per-season projection and must keep reading
    # the flat preseason file the pre-S6 mains hardcoded
    assert paths.projections_path("2026-27", tmp_path) == \
        tmp_path / "derived/ml/projections_2627.json"


def test_team_strengths_reads_given_prior_season_with_promoted_prior():
    seasons = pd.DataFrame([
        {"season": "2025-26", "team_name": "Arsenal", "total_points": 2000},
        {"season": "2025-26", "team_name": "Wolves", "total_points": 1200},
        {"season": "2026-27", "team_name": "Arsenal", "total_points": 1800},
        {"season": "2026-27", "team_name": "Hull City", "total_points": 900},
    ])
    teams = [{"id": 1, "name": "Arsenal"}, {"id": 2, "name": "Hull City"},
             {"id": 3, "name": "Wolves"}]
    strengths = wv.team_strengths(seasons, teams, paths.prior_season("2027-28"))
    assert strengths[1] == 1800 and strengths[2] == 900
    assert strengths[3] == pytest.approx(0.9 * 900)  # absent in 2026-27: promoted prior
    # default stays the 2026-27 prior season
    assert wv.team_strengths(seasons, teams)[1] == 2000
