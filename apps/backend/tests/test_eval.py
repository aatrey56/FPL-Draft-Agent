"""Tests for the evaluation harness (backend.ml.eval).

No network: small in-memory season frames through the pure scoring functions.
"""

import pandas as pd
import pytest

from backend.ml import eval as ev


def _seasons() -> pd.DataFrame:
    """Two seasons of a few MIDs: prior points perfectly ordered like next-season."""
    rows = []
    # feature season 2023-24
    for code, pts in [(1, 200), (2, 150), (3, 100), (4, 50), (5, 20)]:
        rows.append({"season": "2023-24", "code": code, "element_type": 3,
                     "minutes": 3000, "total_points": pts, "ict_index": pts / 10,
                     "bps": pts * 2})
    # target season 2024-25 — same order, different magnitudes
    for code, pts in [(1, 220), (2, 140), (3, 110), (4, 40), (5, 30)]:
        rows.append({"season": "2024-25", "code": code, "element_type": 3,
                     "minutes": 3000, "total_points": pts, "ict_index": pts / 10,
                     "bps": pts * 2})
    return pd.DataFrame(rows)


def test_spearman_perfect_order():
    a = pd.Series([1, 2, 3, 4, 5])
    b = pd.Series([10, 20, 30, 40, 50])
    assert ev._spearman(a, b) == pytest.approx(1.0)


def test_build_pairs_no_leakage_uses_prior_features():
    """Pairs carry prev_* features from the feature season, actual from target."""
    pairs = ev.build_pairs(_seasons(), "2023-24", "2024-25")
    assert len(pairs) == 5
    assert set(pairs["position"]) == {"MID"}
    # prev_total_points is the FEATURE season value, actual_points the TARGET
    row1 = pairs[pairs["code"] == 1].iloc[0]
    assert row1["prev_total_points"] == 200
    assert row1["actual_points"] == 220


def test_baseline_scores_high_when_order_persists():
    """When prior order matches next-season order, baseline Spearman is high."""
    pairs = ev.build_pairs(_seasons(), "2023-24", "2024-25")
    table = ev.score(pairs, "prev_total_points")
    mid = table[table["position"] == "MID"].iloc[0]
    assert mid["spearman"] >= 0.9


def test_min_minutes_filter_excludes_fringe():
    """A player below the prior-minutes floor is dropped from the pairs."""
    seasons = _seasons()
    seasons.loc[seasons["code"] == 5, "minutes"] = 100  # below floor in both seasons
    pairs = ev.build_pairs(seasons, "2023-24", "2024-25")
    assert 5 not in set(pairs["code"])
