"""Tests for the season projection model (backend.ml.projection + features).

Component tests use small synthetic frames (no network, deterministic).
The backtest acceptance tests run against the real ingested parquet and are
skipped when the data directory is absent (data/ is gitignored by design and
never present in CI — unlike a missing dependency, absent local data is not a
misconfiguration).
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.ml import features as F
from backend.ml import projection as P

PARQUET = Path(__file__).resolve().parents[3] / "data/derived/ml/player_seasons.parquet"


# ---------------------------------------------------------------------------
# Component tests (synthetic, always run)
# ---------------------------------------------------------------------------

def test_ridge_recovers_linear_signal():
    table = pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]})
    target = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0, 60.0])
    model = P.Ridge(alpha=0.001, columns=["x"]).fit(table, target)
    pred = model.predict(table)
    assert P._spearman_arrays(pred, target.to_numpy()) == pytest.approx(1.0)


def test_ridge_imputes_nulls_with_training_mean_not_zero():
    table = pd.DataFrame({"x": [1.0, 2.0, 3.0, None]})
    target = pd.Series([1.0, 2.0, 3.0, 2.0])
    model = P.Ridge(alpha=1.0, columns=["x"]).fit(table, target)
    # the null row is imputed to mean(x)=2.0 -> standardized 0 -> prediction = intercept
    assert model.predict(table)[3] == pytest.approx(model.intercept_)


def test_persistence_predicts_the_column():
    table = pd.DataFrame({"total_points": [50.0, 100.0, None]})
    model = P.Persistence("total_points").fit(table, pd.Series([0, 0, 0]))
    pred = model.predict(table)
    assert pred[0] == 50.0 and pred[1] == 100.0
    assert pred[2] == pytest.approx(75.0)  # imputed with training mean


def test_two_stage_composition_is_minutes_times_pts90():
    rows = pd.DataFrame({
        "minutes": [900.0, 1800.0, 2700.0, 3400.0],
        "pts90": [3.0, 4.0, 5.0, 6.0],
        "next_total_minutes_label": [900.0, 1800.0, 2700.0, 3400.0],
        "next_minutes": [900.0, 1800.0, 2700.0, 3400.0],
        "next_pts90_label": [3.0, 4.0, 5.0, 6.0],
    })
    model = P.TwoStage(lambda: P.Persistence("minutes"), lambda: P.Persistence("pts90"))
    model.fit(rows, rows["next_total_minutes_label"])
    pred = model.predict(rows)
    expected = rows["minutes"].to_numpy() / 90 * rows["pts90"].to_numpy()
    assert np.allclose(pred, expected)


def test_finishing_skill_rewards_persistence_over_one_season_spike():
    """Equal per-90 over-performance: 3 seasons of it out-scores 1 season."""
    rows = []
    for season, code in [("2022-23", 1), ("2023-24", 1), ("2024-25", 1), ("2024-25", 2)]:
        rows.append({"code": code, "season": season, "minutes": 900,
                     "goals_scored": 5, "expected_goals": 2.0,  # +3 per 900'
                     "assists": 0, "expected_assists": 0.0, "total_points": 100})
    frame = F.add_finishing_skill(pd.DataFrame(rows))
    persistent = frame[(frame.code == 1) & (frame.season == "2024-25")].finishing_skill.iloc[0]
    spike = frame[(frame.code == 2) & (frame.season == "2024-25")].finishing_skill.iloc[0]
    assert persistent > spike > 0


def test_vor_uses_replacement_rank():
    board = pd.DataFrame({
        "position": ["FWD"] * 5,
        "projected_points": [200.0, 150.0, 120.0, 100.0, 80.0],
    })
    out = P.add_vor(board, league_size=2, starters={"FWD": 2})
    # replacement rank = 4 -> replacement level = 100
    assert out["vor"].tolist() == [100.0, 50.0, 20.0, 0.0, -20.0]


def test_tiers_split_at_natural_gaps():
    points = pd.Series([100.0, 98.0, 60.0, 58.0, 30.0, 29.0])
    tiers = P.assign_tiers(points, tier_count=3)
    assert tiers.tolist() == [1, 1, 2, 2, 3, 3]


def _selection_rows(signal_predicts: bool) -> pd.DataFrame:
    """Rows over 3 labelled seasons where `signal` tracks the target and
    `noise` does not (or vice versa)."""
    rng = [3.0, 1.0, 2.0, 5.0, 4.0, 6.0]  # fixed pseudo-noise, deterministic
    rows = []
    for season_index, season in enumerate(["2021-22", "2022-23", "2023-24"]):
        for i in range(6):
            target = float(i * 10 + season_index)
            rows.append({
                "season": season, "next_season": season,  # label season tag
                "signal": target if signal_predicts else rng[i],
                "noise": rng[i],
                "y": target,
            })
    return pd.DataFrame(rows)


def test_select_model_keeps_simple_incumbent_when_challenger_is_noise():
    rows = _selection_rows(signal_predicts=False)
    name, _, _ = P.select_model(rows, "y", [
        ("incumbent", lambda: P.Persistence("noise")),
        ("challenger", lambda: P.Ridge(alpha=1.0, columns=["noise"])),
    ])
    assert name == "incumbent"  # same information -> no displacement


def test_select_model_promotes_consistently_better_challenger():
    rows = _selection_rows(signal_predicts=True)
    name, model, _ = P.select_model(rows, "y", [
        ("incumbent", lambda: P.Persistence("noise")),
        ("challenger", lambda: P.Ridge(alpha=0.1, columns=["signal"])),
    ])
    assert name == "challenger"
    assert P._spearman_arrays(model.predict(rows), rows["y"].to_numpy()) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Acceptance backtests (real data; skipped when local data is absent — data/
# is gitignored and never in CI, so this is by-design, not a hidden failure)
# ---------------------------------------------------------------------------

needs_data = pytest.mark.skipif(not PARQUET.exists(),
                                reason="local player_seasons.parquet not present")


@needs_data
def test_backtest_2025_26_never_loses_to_baseline_any_position():
    seasons = pd.read_parquet(PARQUET)
    report = P.backtest(seasons, "2025-26")
    assert report["beats_baseline"].all(), report.to_string()


@needs_data
def test_backtest_2024_25_beats_baseline_for_gkp_def_mid():
    seasons = pd.read_parquet(PARQUET)
    report = P.backtest(seasons, "2024-25")
    passing = report[report["position"].isin(["GKP", "DEF", "MID"])]
    assert passing["beats_baseline"].all(), report.to_string()
    # FWD 2024-25 is the documented known miss (see PROJECTION_MODEL_SPEC.md);
    # if it ever starts passing, remove the exception.


@needs_data
def test_projection_output_schema_and_determinism():
    seasons = pd.read_parquet(PARQUET)
    board_a = P.project_upcoming(seasons)
    board_b = P.project_upcoming(seasons)
    pd.testing.assert_frame_equal(board_a, board_b)  # fully deterministic
    for column in ["code", "position", "projected_points", "tier", "vor",
                   "confidence", "risk_flags", "position_rank", "drivers"]:
        assert column in board_a.columns, column
    assert set(board_a["position"].unique()) == {"GKP", "DEF", "MID", "FWD"}
