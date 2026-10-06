"""Tests for the per-GW modelling frame and baseline benchmark. Fixtures only."""

import pandas as pd
import pytest

from backend.ml import matcheval as me
from backend.ml.matchfeatures import add_fixture_context, build_match_frame, team_form


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


# --- team form for gameweeks with no played row (serving stubs, blanks) -----

TEAM_COLUMNS = ["team_scored_pg", "team_conceded_pg", "opp_scored_pg", "opp_conceded_pg"]


def _team_panel(results: dict[int, dict[int, tuple[int, int, int]]],
                stub_gw: int | None = None,
                stub_fixtures: tuple[tuple[int, int], ...] = ()) -> pd.DataFrame:
    """One player per team. ``results[gw][team] = (opponent, scored, conceded)``.

    A team missing from ``results[gw]`` blanks that gameweek (a row with
    ``played=False`` and no opponent). ``stub_gw`` appends outcome-free rows
    shaped like ``matchmodel.upcoming_fixture_rows`` for ``stub_fixtures``.
    """
    teams = sorted({team for by_team in results.values() for team in by_team})
    rows = []
    for gw, by_team in sorted(results.items()):
        for team in teams:
            if team not in by_team:
                rows.append({"code": team * 10, "season": "2026-27", "gw": gw,
                             "element_type": 3, "team_id": team, "played": False,
                             "minutes": 0, "total_points": 0, "goals_scored": 0,
                             "goals_conceded": 0})
                continue
            opponent, scored, conceded = by_team[team]
            rows.append({"code": team * 10, "season": "2026-27", "gw": gw,
                         "element_type": 3, "team_id": team,
                         "opponent_team": opponent, "was_home": team < opponent,
                         "num_fixtures": 1, "played": True, "started": True,
                         "minutes": 90, "total_points": 2 + scored,
                         "goals_scored": scored, "goals_conceded": conceded})
    for home, away in stub_fixtures:
        for team, opponent in ((home, away), (away, home)):
            rows.append({"code": team * 10, "season": "2026-27", "gw": stub_gw,
                         "element_type": 3, "team_id": team,
                         "opponent_team": opponent, "was_home": team == home,
                         "num_fixtures": 1, "played": False})
    return pd.DataFrame(rows)


def _two_team_results(gws: int) -> dict[int, dict[int, tuple[int, int, int]]]:
    """Team 1 scores 1, 3, 5, ...; team 2 scores 0, 2, 4, ... against it."""
    return {gw: {1: (2, 2 * gw - 1, 2 * gw - 2), 2: (1, 2 * gw - 2, 2 * gw - 1)}
            for gw in range(1, gws + 1)}


def _row(frame: pd.DataFrame, team: int, gw: int) -> pd.Series:
    return frame[(frame["team_id"] == team) & (frame["gw"] == gw)].iloc[0]


def test_stub_gameweek_team_form_includes_the_last_completed_gameweek():
    """Serving at GW4 must see GW1-3; the GW3 training row still sees GW1-2."""
    frame = add_fixture_context(_team_panel(_two_team_results(3), stub_gw=4,
                                            stub_fixtures=((1, 2),)))
    stub = _row(frame, 1, 4)
    assert stub["team_scored_pg"] == pytest.approx((1 + 3 + 5) / 3)
    assert stub["team_conceded_pg"] == pytest.approx((0 + 2 + 4) / 3)
    assert stub["opp_scored_pg"] == pytest.approx((0 + 2 + 4) / 3)
    assert stub["opp_conceded_pg"] == pytest.approx((1 + 3 + 5) / 3)

    played = _row(frame, 1, 3)
    assert played["team_scored_pg"] == pytest.approx((1 + 3) / 2)
    assert played["team_conceded_pg"] == pytest.approx((0 + 2) / 2)
    assert played["opp_scored_pg"] == pytest.approx((0 + 2) / 2)
    assert played["opp_conceded_pg"] == pytest.approx((1 + 3) / 2)


def test_stub_at_gw2_gets_the_gw1_value_not_nan():
    frame = add_fixture_context(_team_panel(_two_team_results(1), stub_gw=2,
                                            stub_fixtures=((1, 2),)))
    stub = _row(frame, 2, 2)
    assert stub[TEAM_COLUMNS].notna().all()
    assert stub["team_scored_pg"] == pytest.approx(0)
    assert stub["team_conceded_pg"] == pytest.approx(1)
    assert stub["opp_scored_pg"] == pytest.approx(1)
    assert stub["opp_conceded_pg"] == pytest.approx(0)


def test_blank_gameweek_carries_the_last_post_gameweek_value():
    """Team 1 blanks GW3: GW3 and GW4 both see GW1-2; the GW5 stub sees 1, 2, 4."""
    results = {
        1: {1: (2, 1, 0), 2: (1, 0, 1), 3: (4, 2, 2), 4: (3, 2, 2)},
        2: {1: (2, 3, 1), 2: (1, 1, 3), 3: (4, 0, 0), 4: (3, 0, 0)},
        3: {2: (3, 1, 1), 3: (2, 1, 1)},  # teams 1 and 4 blank
        4: {1: (2, 8, 0), 2: (1, 0, 8), 3: (4, 1, 1), 4: (3, 1, 1)},
    }
    overall, _ = team_form(_team_panel(results, stub_gw=5, stub_fixtures=((1, 2),)))
    team1 = overall[overall["team_id"] == 1].set_index("gw")
    assert team1.loc[3, "team_scored_pg"] == pytest.approx((1 + 3) / 2)
    assert team1.loc[4, "team_scored_pg"] == pytest.approx((1 + 3) / 2)
    assert team1.loc[5, "team_scored_pg"] == pytest.approx((1 + 3 + 8) / 3)
    assert team1.loc[5, "team_conceded_pg"] == pytest.approx((0 + 1 + 0) / 3)


def test_played_row_team_form_is_unchanged_snapshot():
    """Regression: the fix only touches gameweeks without a played row."""
    frame = build_match_frame(_panel())
    snapshot = (
        frame[frame["code"].isin([1, 2])]
        .set_index(["code", "gw"])[TEAM_COLUMNS + ["opp_pts_allowed_pg"]]
    )
    nan = float("nan")
    expected = pd.DataFrame(
        [
            [nan, nan, nan, nan, nan], [1, 1, 0, 2, 2], [1, 1, 0, 2, 3], [1, 1, 0, 2, 4],
            [nan, nan, nan, nan, nan], [0, 2, 1, 1, 1], [0, 2, 1, 1, 1], [0, 2, 1, 1, 1],
        ],
        columns=TEAM_COLUMNS + ["opp_pts_allowed_pg"],
        index=pd.MultiIndex.from_product([[1, 2], [1, 2, 3, 4]], names=["code", "gw"]),
    )
    pd.testing.assert_frame_equal(snapshot, expected, check_dtype=False)
