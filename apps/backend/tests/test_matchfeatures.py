"""Tests for the per-GW modelling frame and baseline benchmark. Fixtures only."""

import pandas as pd
import pytest

from backend.ml import matcheval as me
from backend.ml.matchfeatures import build_match_frame, team_form


def _panel() -> pd.DataFrame:
    """Three players, two clubs, four gameweeks — enough to catch leakage."""
    rows = []
    for gw in range(1, 5):
        rows.append({
            "code": 1, "season": "2025-26", "gw": gw, "element_type": 3,
            "team_id": 1, "opponent_team": 2, "was_home": True, "num_fixtures": 1,
            "played": True, "started": True, "minutes": 90,
            "total_points": gw * 2, "goals_scored": 1, "assists": 0,
            "goals_conceded": 1, "saves": 0, "bonus": 0, "bps": 20,
            "defensive_contribution": 3, "expected_goals": 0.5,
            "expected_assists": 0.2, "expected_goal_involvements": 0.7,
            "ict_index": 10.0,
        })
        rows.append({
            "code": 2, "season": "2025-26", "gw": gw, "element_type": 4,
            "team_id": 2, "opponent_team": 1, "was_home": False, "num_fixtures": 1,
            "played": True, "started": True, "minutes": 90,
            "total_points": 1, "goals_scored": 0, "assists": 0,
            "goals_conceded": 2, "saves": 0, "bonus": 0, "bps": 5,
            "defensive_contribution": 1, "expected_goals": 0.1,
            "expected_assists": 0.1, "expected_goal_involvements": 0.2,
            "ict_index": 2.0,
        })
        rows.append({
            "code": 3, "season": "2025-26", "gw": gw, "element_type": 2,
            "team_id": 1, "opponent_team": 2, "was_home": True, "num_fixtures": 1,
            "played": True, "started": True, "minutes": 90,
            "total_points": 5 - gw, "goals_scored": 0, "assists": 0,
            "goals_conceded": 1, "saves": 0, "bonus": 0, "bps": 12,
            "defensive_contribution": 8, "expected_goals": 0.0,
            "expected_assists": 0.1, "expected_goal_involvements": 0.1,
            "ict_index": 4.0,
        })
    return pd.DataFrame(rows)


def test_trailing_features_never_see_their_own_gameweek():
    """The leakage guard: GW3's form must be built from GW1-2 only."""
    frame = build_match_frame(_panel())
    gw3 = frame[(frame["code"] == 1) & (frame["gw"] == 3)].iloc[0]
    assert gw3["pts_l1"] == 4          # GW2's points, not GW3's 6
    assert gw3["pts_l3"] == pytest.approx((2 + 4) / 2)
    assert gw3["label_points"] == 6    # the label is this gameweek

    gw1 = frame[(frame["code"] == 1) & (frame["gw"] == 1)].iloc[0]
    assert pd.isna(gw1["pts_l1"])      # nothing before the first gameweek
    assert not gw1["has_history"]


def test_team_form_is_shifted_and_position_aware():
    overall, by_position = team_form(_panel())
    gw3 = overall[(overall["team_id"] == 1) & (overall["gw"] == 3)].iloc[0]
    assert gw3["team_conceded_pg"] == pytest.approx(1.0)

    # Team 2 has conceded to one MID (code 1) each gameweek: 2, 4 before GW3.
    allowed = by_position[
        (by_position["team_id"] == 2)
        & (by_position["element_type"] == 3)
        & (by_position["gw"] == 3)
    ].iloc[0]
    assert allowed["opp_pts_allowed_pg"] == pytest.approx((2 + 4) / 2)


def test_startable_pool_excludes_players_without_recent_minutes():
    panel = _panel()
    panel.loc[panel["code"] == 2, "minutes"] = 0
    panel.loc[panel["code"] == 2, "played"] = False
    frame = build_match_frame(panel)
    gw4 = frame[frame["gw"] == 4].set_index("code")
    assert bool(gw4.loc[1, "is_startable"])
    assert not bool(gw4.loc[2, "is_startable"])


def test_perfect_predictor_scores_a_clean_sweep():
    """Sanity floor for the scorer: predicting the label exactly is a 1.0."""
    frame = build_match_frame(_panel())
    frame["oracle"] = frame["label_points"]
    scores = me.score_predictor(frame, "oracle", "oracle")
    assert scores["spearman"] == pytest.approx(1.0)
    assert scores["top_frac"] == pytest.approx(1.0)


def test_minutes_predictor_reports_no_points_mae():
    """A ranking-only predictor must not claim a points-scale error."""
    frame = build_match_frame(_panel())
    scores = me.score_predictor(frame, "mins_l3", "minutes_only")
    assert pd.isna(scores["mae"])
