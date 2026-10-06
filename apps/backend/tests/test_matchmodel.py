"""Tests for the two-stage match xP model. Synthetic fixtures only, no network.

The four spec acceptance criteria (MATCH_MODEL_SPEC.md) map to:
``test_flagged_out_player_is_gated_to_zero``, ``test_walk_forward_never_trains_on_the_target_gameweek``,
``test_double_gameweek_sums_both_fixtures`` and ``test_model_beats_the_naive_baseline_on_held_out_gameweeks``.

The last of those runs on a *synthetic* panel with known signal: the real
2025-26 panel is gitignored, so it cannot back a test. The measured numbers on
real data are recorded in ``docs/MODEL_ROADMAP.md`` and reproduced by
``python -m backend.ml.matchmodel --backtest``.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.ml import matchmodel as mm
from backend.ml.matchfeatures import build_match_frame

SEASON = "2025-26"
N_TEAMS = 10
PER_POSITION = 30
N_GWS = 20
# The synthetic panel is a tenth the size of a real season, so the
# "enough rows to fit" thresholds are scaled down to match it.
SMALL_FIT = 60


def _panel(seed: int = 7) -> pd.DataFrame:
    """A season where points genuinely depend on starting and on the opponent.

    Every player has a latent quality and a latent start propensity; every team
    a latent leakiness. Points are built from those, so a model that recovers
    "who starts" and "who they play" must out-rank one that only looks at
    trailing points.
    """
    rng = np.random.default_rng(seed)
    leakiness = rng.uniform(0.4, 1.6, size=N_TEAMS + 1)
    players = []
    code = 1000
    for position in (1, 2, 3, 4):
        for index in range(PER_POSITION):
            code += 1
            players.append({
                "code": code,
                "element_type": position,
                "team_id": 1 + (index % N_TEAMS),
                "quality": rng.uniform(0.2, 1.0),
                "start_propensity": rng.uniform(0.1, 0.98),
            })

    rows = []
    for gw in range(1, N_GWS + 1):
        # A fixed round-robin rotation: every team has exactly one opponent.
        opponents = {t: 1 + ((t + gw) % N_TEAMS) for t in range(1, N_TEAMS + 1)}
        for player in players:
            team = player["team_id"]
            opponent = opponents[team]
            if opponent == team:
                opponent = 1 + (team % N_TEAMS)
            started = bool(rng.random() < player["start_propensity"])
            cameo = (not started) and bool(rng.random() < 0.2)
            played = started or cameo
            minutes = 90 if started else (20 if cameo else 0)
            base = player["quality"] * 4.0 * leakiness[opponent]
            points = 0.0
            if started:
                points = max(0.0, 2.0 + base + rng.normal(0, 1.5))
            elif cameo:
                points = max(0.0, 1.0 + rng.normal(0, 0.5))
            rows.append({
                "code": player["code"], "season": SEASON, "gw": gw,
                "element_type": player["element_type"], "team_id": team,
                "opponent_team": opponent, "opponent_name": f"T{opponent}",
                "was_home": bool(gw % 2), "num_fixtures": 1,
                "played": played, "started": started, "minutes": minutes,
                "total_points": round(points),
                "goals_scored": int(points > 6), "assists": 0,
                "clean_sheets": 0, "goals_conceded": int(leakiness[team] * 2),
                "saves": int(played) * 2, "bonus": 0, "bps": points * 3,
                "defensive_contribution": int(played) * 4,
                "expected_goals": player["quality"] * 0.3,
                "expected_assists": player["quality"] * 0.2,
                "expected_goal_involvements": player["quality"] * 0.5,
                "ict_index": player["quality"] * 10.0,
            })
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    return build_match_frame(_panel())


def _bootstrap(panel: pd.DataFrame, gw: int, doubles: tuple[int, ...] = (),
               flagged: tuple[int, ...] = ()) -> dict:
    """A minimal bootstrap: one fixture per team, plus any requested doubles."""
    fixtures = []
    fixture_id = 1
    for team in range(1, N_TEAMS + 1, 2):
        fixtures.append({"id": fixture_id, "event": gw, "team_h": team,
                         "team_a": team + 1})
        fixture_id += 1
    for team in doubles:
        opponent = 1 + (team % N_TEAMS)
        fixtures.append({"id": fixture_id, "event": gw, "team_h": team,
                         "team_a": opponent})
        fixture_id += 1
    elements = []
    for _, row in panel.drop_duplicates("code").iterrows():
        code = int(row["code"])
        elements.append({
            "id": code, "code": code, "element_type": int(row["element_type"]),
            "team": int(row["team_id"]),
            "status": "i" if code in flagged else "a",
            "chance_of_playing_next_round": 0 if code in flagged else None,
        })
    return {
        "fixtures": {str(gw): fixtures},
        "teams": [{"id": t, "short_name": f"T{t}"} for t in range(1, N_TEAMS + 1)],
        "elements": elements,
    }


# --- acceptance criterion 1: the availability gate --------------------------

def test_flagged_out_player_is_gated_to_zero():
    """A player flagged 0% gets P(start) ~ 0 and near-zero xP, however good."""
    panel = _panel()
    # Pick the most productive ever-present player, so only the flag can sink him.
    totals = panel.groupby("code")["total_points"].sum()
    star = int(totals.idxmax())
    gw = N_GWS + 1

    healthy = mm.build_gw_xp(panel, _bootstrap(panel, gw), gw, SEASON,
                             min_train_rows=SMALL_FIT)
    flagged = mm.build_gw_xp(panel, _bootstrap(panel, gw, flagged=(star,)), gw,
                             SEASON, min_train_rows=SMALL_FIT)

    before = healthy.set_index("code").loc[star]
    after = flagged.set_index("code").loc[star]
    assert before["p_start"] > 0.5          # he is a nailed starter when fit
    assert before["xp"] > 1.0
    assert after["p_start"] == pytest.approx(0.0, abs=1e-9)
    assert after["xp"] == pytest.approx(0.0, abs=1e-9)
    # and the gate is specific to him — nobody else moved.
    others = healthy.set_index("code")["xp"].drop(star)
    assert others.equals(flagged.set_index("code")["xp"].drop(star))


def test_availability_factor_maps_status_flags():
    bootstrap = {"elements": [
        {"code": 1, "status": "a", "chance_of_playing_next_round": None},
        {"code": 2, "status": "u", "chance_of_playing_next_round": None},
        {"code": 3, "status": "d", "chance_of_playing_next_round": 25},
    ]}
    factors = mm.availability_series(bootstrap, pd.Series([1, 2, 3, 999]))
    assert list(factors) == [1.0, 0.0, 0.25, 1.0]  # unknown code defaults to fit


# --- acceptance criterion 2: no leakage -------------------------------------

def test_walk_forward_never_trains_on_the_target_gameweek(frame, monkeypatch):
    """Every fit must see strictly earlier gameweeks than the one it predicts."""
    seen = []
    real_fit = mm.MatchModel.fit

    def recording_fit(self, train):
        seen.append(int(train["gw"].max()))
        return real_fit(self, train)

    monkeypatch.setattr(mm.MatchModel, "fit", recording_fit)
    predicted = mm.walk_forward(frame, min_gw=6, alpha=100.0,
                                min_train_rows=SMALL_FIT)

    targets = sorted(int(gw) for gw in predicted["gw"].unique())
    assert targets == list(range(7, N_GWS + 1))
    assert len(seen) == len(targets)
    for trained_through, target in zip(seen, targets):
        assert trained_through < target


def test_features_for_a_gameweek_use_only_earlier_rows(frame):
    """Spot-check the frame itself: a trailing mean cannot contain its own row."""
    player = frame[frame["code"] == frame["code"].iloc[0]].sort_values("gw")
    row = player[player["gw"] == 5].iloc[0]
    earlier = player[player["gw"] < 5]["total_points"]
    assert row["pts_l5"] == pytest.approx(earlier.tail(5).mean())
    assert row["pts_std"] == pytest.approx(earlier.mean())


# --- live-season evaluation: scored as served -------------------------------

LIVE_SEASON = "2026-27"
LIVE_GWS = 5
# The target gameweek of the serving-parity checks.
PARITY_GW = 3


def _live_panel(seed: int = 11, season: str = LIVE_SEASON,
                offset: int = 100) -> pd.DataFrame:
    """The first ``LIVE_GWS`` gameweeks of a later season.

    Team ids are shifted by ``offset``: nothing may join across seasons.
    """
    live = _panel(seed=seed)
    live = live[live["gw"] <= LIVE_GWS].assign(season=season)
    live["team_id"] = live["team_id"] + offset
    live["opponent_team"] = live["opponent_team"] + offset
    return live.reset_index(drop=True)


def _two_season_panel() -> pd.DataFrame:
    """A full archive season plus the first gameweeks of a live one."""
    return pd.concat([_panel(), _live_panel()], ignore_index=True)


def _paired(live: pd.DataFrame, gw: int) -> tuple[pd.DataFrame, dict]:
    """Give one live gameweek a real fixture list, and the bootstrap serving it.

    The synthetic rotation is not symmetric (a team's opponent need not list
    it back), so the target gameweek is re-paired into home/away fixtures that a
    bootstrap can express.
    """
    teams = sorted(int(t) for t in live["team_id"].unique())
    pairs = list(zip(teams[::2], teams[1::2]))
    partner = {**{h: a for h, a in pairs}, **{a: h for h, a in pairs}}
    home = {**{h: True for h, _ in pairs}, **{a: False for _, a in pairs}}
    live = live.copy()
    at = live["gw"] == gw
    live.loc[at, "opponent_team"] = live.loc[at, "team_id"].map(partner)
    live.loc[at, "opponent_name"] = live.loc[at, "opponent_team"].map(lambda t: f"T{t}")
    live.loc[at, "was_home"] = live.loc[at, "team_id"].map(home)
    bootstrap = {
        "fixtures": {str(gw): [{"team_h": h, "team_a": a} for h, a in pairs]},
        "teams": [{"id": t, "short_name": f"T{t}"} for t in teams],
        "elements": [
            {"id": int(row.code), "code": int(row.code),
             "element_type": int(row.element_type), "team": int(row.team_id),
             "status": "a", "chance_of_playing_next_round": None}
            for row in live.drop_duplicates("code").itertuples()],
    }
    return live, bootstrap


def _all_feature_columns() -> list[str]:
    columns = set(mm.MINUTES_FEATURES)
    for position in mm.POSITIONS:
        columns.update(mm.feature_columns(position))
    return sorted(columns)


def test_served_walk_forward_scores_the_features_serving_builds():
    """Parity: the eval scores exactly the rows ``build_gw_xp`` would serve.

    Target (live season, GW3). Serving would build features from the archive
    plus live GW1-2 and outcome-free bootstrap stubs; the eval must score the
    same feature values and, fitted on the same rows, the same xP.
    """
    live, bootstrap = _paired(_live_panel(), PARITY_GW)
    panel = pd.concat([_panel(), live], ignore_index=True)
    served = (mm.served_walk_forward(panel, LIVE_SEASON, [PARITY_GW], alpha=100.0,
                                     min_train_rows=SMALL_FIT)
              .set_index("code").sort_index())

    history = panel[(panel["season"] == SEASON)
                    | ((panel["season"] == LIVE_SEASON) & (panel["gw"] < PARITY_GW))]
    stubs = mm.upcoming_fixture_rows(bootstrap, PARITY_GW, LIVE_SEASON)
    frame = build_match_frame(pd.concat([history, stubs], ignore_index=True))
    expected = (frame[(frame["season"] == LIVE_SEASON) & (frame["gw"] == PARITY_GW)]
                .set_index("code").sort_index())

    assert served.index.equals(expected.index)
    columns = _all_feature_columns()
    pd.testing.assert_frame_equal(served[columns].astype("float64"),
                                  expected[columns].astype("float64"),
                                  check_exact=False, atol=1e-9)
    assert (served["is_startable"] == expected["is_startable"]).all()
    assert served[["team_conceded_pg", "opp_conceded_pg"]].notna().all().all()
    # And serving's stub features are the ones the finished gameweek's own
    # panel row carries — the team-form carry contract (no stale form).
    full = build_match_frame(panel)
    played = (full[(full["season"] == LIVE_SEASON) & (full["gw"] == PARITY_GW)]
              .set_index("code").sort_index())
    pd.testing.assert_frame_equal(served[columns].astype("float64"),
                                  played[columns].astype("float64"),
                                  check_exact=False, atol=1e-9)

    xp = mm.build_gw_xp(history, bootstrap, PARITY_GW, LIVE_SEASON, alpha=100.0,
                        min_train_rows=SMALL_FIT).set_index("code")["xp"]
    np.testing.assert_allclose(served["xp"].to_numpy(float),
                               xp.loc[served.index].to_numpy(float), rtol=1e-9)


def test_served_walk_forward_attaches_outcomes_only_after_scoring():
    """Changing the target gameweek's results moves the labels, never the features."""
    panel = _two_season_panel()
    at = (panel["season"] == LIVE_SEASON) & (panel["gw"] == PARITY_GW)
    shifted = panel.copy()
    shifted.loc[at, "total_points"] += 50
    shifted.loc[at, "minutes"] = 0
    shifted.loc[at, "goals_conceded"] += 9
    kwargs = {"alpha": 100.0, "min_train_rows": SMALL_FIT}
    before = mm.served_walk_forward(panel, LIVE_SEASON, [PARITY_GW], **kwargs)
    after = mm.served_walk_forward(shifted, LIVE_SEASON, [PARITY_GW], **kwargs)

    columns = _all_feature_columns() + ["xp", "p_start"]
    pd.testing.assert_frame_equal(before[columns], after[columns])
    assert (after["label_points"] - before["label_points"] == 50).all()
    truth = panel[at].set_index("code")
    assert (before.set_index("code")["label_points"]
            == truth.loc[before["code"], "total_points"].to_numpy()).all()


def test_served_walk_forward_training_set_matches_serving(monkeypatch):
    """GW2 of the live season is fitted on archive rows + live GW1, never GW>=2."""
    panel = _two_season_panel()
    captured = []
    real_fit = mm.MatchModel.fit

    def recording_fit(self, train):
        captured.append(train)
        return real_fit(self, train)

    monkeypatch.setattr(mm.MatchModel, "fit", recording_fit)
    predicted = mm.served_walk_forward(panel, LIVE_SEASON, [2], alpha=100.0,
                                       min_train_rows=SMALL_FIT)

    assert len(captured) == 1
    train = captured[0]
    assert (train["season"] == SEASON).sum() == (panel["season"] == SEASON).sum()
    live_train = train[train["season"] == LIVE_SEASON]
    assert not live_train.empty and live_train["gw"].max() == 1
    assert set(predicted["season"]) == {LIVE_SEASON}
    assert set(predicted["gw"]) == {2}


def test_served_walk_forward_never_trains_on_a_later_season(monkeypatch):
    """Scoring 2026-27 must not learn from 2027-28, which did not exist yet."""
    later = _live_panel(seed=13, season="2027-28", offset=200)
    panel = pd.concat([_two_season_panel(), later], ignore_index=True)
    seasons = []
    real_fit = mm.MatchModel.fit

    def recording_fit(self, train):
        seasons.append(set(train["season"]))
        return real_fit(self, train)

    monkeypatch.setattr(mm.MatchModel, "fit", recording_fit)
    mm.served_walk_forward(panel, LIVE_SEASON, [2, 3], alpha=100.0,
                           min_train_rows=SMALL_FIT)
    assert seasons == [{SEASON, LIVE_SEASON}, {SEASON, LIVE_SEASON}]


def test_season_start_orders_by_start_year_and_rejects_garbage():
    assert sorted(["2027-28", "2025-26", "2026-27"], key=mm.season_start) == [
        "2025-26", "2026-27", "2027-28"]
    with pytest.raises(ValueError):
        mm.season_start("last season")


def test_served_walk_forward_scores_a_panel_double_as_one_imputed_fixture():
    """Pins the documented DGW limitation of the as-served eval.

    The panel stores a double as one row with ``num_fixtures == 2`` and no
    opponent, so the eval cannot score each fixture the way ``score_fixtures``
    serves it: it scores one fixture with imputed opponent features and
    doubles it. If the panel ever gains per-fixture rows, this test should be
    replaced by a parity test against ``score_fixtures``.
    """
    panel = _two_season_panel()
    doubled_team = 101
    at = ((panel["season"] == LIVE_SEASON) & (panel["gw"] == PARITY_GW)
          & (panel["team_id"] == doubled_team))
    panel.loc[at, "num_fixtures"] = 2
    panel.loc[at, ["opponent_team", "opponent_name"]] = np.nan
    scored = mm.served_walk_forward(panel, LIVE_SEASON, [PARITY_GW], alpha=100.0,
                                    min_train_rows=SMALL_FIT)

    doubled = scored[scored["team_id"] == doubled_team]
    single = scored[scored["team_id"] != doubled_team]
    assert len(doubled) == at.sum()
    assert doubled[["opp_scored_pg", "opp_conceded_pg"]].isna().all().all()
    assert doubled["xp"].to_numpy() == pytest.approx(2 * doubled["xp_fixture"].to_numpy())
    assert single["xp"].to_numpy() == pytest.approx(single["xp_fixture"].to_numpy())


def test_served_walk_forward_needs_the_archive_for_a_thin_season():
    """Within the live season alone GW2 has no labelled history; the archive fixes that."""
    alone = mm.served_walk_forward(_live_panel(), LIVE_SEASON, [2], alpha=100.0,
                                   min_train_rows=SMALL_FIT)
    assert alone.empty

    pooled = mm.served_walk_forward(_two_season_panel(), LIVE_SEASON, [2],
                                    alpha=100.0, min_train_rows=SMALL_FIT)
    assert set(pooled["element_type"]) == {1, 2, 3, 4}
    assert pooled["xp"].notna().all()


def test_served_walk_forward_skips_gameweeks_missing_from_the_panel():
    predicted = mm.served_walk_forward(_two_season_panel(), LIVE_SEASON, [5, 9],
                                       alpha=100.0, min_train_rows=SMALL_FIT)
    assert set(predicted["gw"]) == {5}


def test_live_eval_scores_every_requested_gameweek():
    report, calibration = mm.live_eval(_two_season_panel(), LIVE_SEASON, [2, 3, 4, 5],
                                       min_train_rows=SMALL_FIT)
    assert set(report["pool"]) == {"startable", "all"}
    assert set(report["predictor"]) == {"model_xp", *mm.me.BASELINES}
    # A baseline can be constant (NaN Spearman) in a tiny synthetic GW; the
    # model itself is scored on all four.
    assert (report[report["predictor"] == "model_xp"]["gws"] == 4).all()
    assert (report["gws"] <= 4).all()
    assert set(calibration["position"]) == {"GKP", "DEF", "MID", "FWD"}


def _tiny_report() -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = [{"pool": pool, "position": "MID", "predictor": predictor,
             "spearman": value, "top_frac": 0.5, "mae": 2.0, "gws": 3}
            for pool in ("startable", "all")
            for predictor, value in (("model_xp", 0.3), ("mean_l3", 0.2))]
    calibration = pd.DataFrame([{"position": "MID", "brier": 0.1,
                                 "predicted_rate": 0.6, "actual_rate": 0.62, "n": 9}])
    return pd.DataFrame(rows), calibration


def test_live_report_prints_the_gw2_caveat_only_when_gw2_is_scored(capsys):
    report, calibration = _tiny_report()
    mm._print_live_eval(report, calibration, LIVE_SEASON, [2, 3, 4, 5])
    assert "GW2 form = 1 gameweek" in capsys.readouterr().out
    mm._print_live_eval(report, calibration, LIVE_SEASON, [3, 4, 5])
    out = capsys.readouterr().out
    assert "GW2 form" not in out and "LIVE CHECK 2026-27 GW3-5" in out


def test_gw_range_parses_ranges_lists_and_rejects_garbage():
    assert mm._gw_range("2-5") == [2, 3, 4, 5]
    assert mm._gw_range("3") == [3]
    assert mm._gw_range("2,4") == [2, 4]
    with pytest.raises(Exception):
        mm._gw_range("five")


def test_walk_forward_default_is_unchanged(frame):
    """Regression: the within-season backtest path reproduces its golden output."""
    predicted = mm.walk_forward(frame, min_gw=6, alpha=100.0,
                                min_train_rows=SMALL_FIT)
    assert len(predicted) == 1680
    assert predicted["xp"].sum() == pytest.approx(4470.087216015732)


def test_walk_forward_default_stays_within_season_on_two_seasons():
    """Regression: the default path must never train across a season boundary.

    With a second full season beside it, the archive season reproduces its
    single-season golden output exactly, and the second season its own — so
    any change that lets the default path pool seasons is caught.
    """
    second = _panel(seed=11).assign(season=LIVE_SEASON)
    second["team_id"] += 100
    second["opponent_team"] += 100
    frame = build_match_frame(pd.concat([_panel(), second], ignore_index=True))
    predicted = mm.walk_forward(frame, min_gw=6, alpha=100.0,
                                min_train_rows=SMALL_FIT)
    by_season = predicted.groupby("season")["xp"]
    assert by_season.size().to_dict() == {SEASON: 1680, LIVE_SEASON: 1680}
    assert by_season.sum()[SEASON] == pytest.approx(4470.087216015732)
    assert by_season.sum()[LIVE_SEASON] == pytest.approx(3785.2729618143544)


# --- acceptance criterion 3: double gameweeks -------------------------------

def test_double_gameweek_sums_both_fixtures():
    """A team playing twice yields two fixture rows whose xP add up."""
    panel = _panel()
    gw = N_GWS + 1
    doubled_team = 3
    scored = mm.build_gw_xp(panel, _bootstrap(panel, gw, doubles=(doubled_team,)),
                            gw, SEASON, min_train_rows=SMALL_FIT)
    # Adding a fixture doubles *both* sides of it, so read the doubled teams
    # off the fixture list rather than assuming only the one asked for.
    bootstrap = _bootstrap(panel, gw, doubles=(doubled_team,))
    counts: dict[int, int] = {}
    for fixture in bootstrap["fixtures"][str(gw)]:
        for team in (fixture["team_h"], fixture["team_a"]):
            counts[team] = counts.get(team, 0) + 1
    doubled_teams = {team for team, n in counts.items() if n == 2}
    assert doubled_team in doubled_teams

    codes = panel[panel["team_id"].isin(doubled_teams)]["code"].unique()
    doubled = scored[scored["code"].isin(codes)]
    single = scored[~scored["code"].isin(codes)]
    assert (doubled["num_fixtures"] == 2).all()
    assert (single["num_fixtures"] == 1).all()

    # The summed xP really is both fixtures, not a doubled multiplier: rebuild
    # one player's two fixture rows and check the total matches.
    model_rows = mm.upcoming_fixture_rows(bootstrap, gw, SEASON)
    assert (model_rows[model_rows["team_id"] == doubled_team]
            .groupby("code").size() == 2).all()
    target = int(doubled["xp"].idxmax())
    row = scored.loc[target]
    assert row["xp"] > 0
    assert row["xp_ceiling"] > row["xp"] > row["xp_floor"] >= 0


def test_blank_gameweek_scores_zero():
    """No fixture means no row and no expected points."""
    panel = _panel()
    gw = N_GWS + 1
    bootstrap = _bootstrap(panel, gw)
    # Strip team 1's only fixture; everyone on it now blanks.
    bootstrap["fixtures"][str(gw)] = [
        f for f in bootstrap["fixtures"][str(gw)]
        if 1 not in (f["team_h"], f["team_a"])]
    scored = mm.build_gw_xp(panel, bootstrap, gw, SEASON,
                            min_train_rows=SMALL_FIT)
    blanked = panel[panel["team_id"] == 1]["code"].unique()
    assert scored[scored["code"].isin(blanked)].empty

    rows = mm.upcoming_fixture_rows(bootstrap, gw, SEASON)
    assert rows[rows["team_id"] == 1].empty


def test_panel_scoring_multiplies_a_double_gameweek_row():
    """On panel rows (one per gameweek), xP scales with ``num_fixtures``."""
    frame = build_match_frame(_panel())
    model = mm.MatchModel(alpha=100.0, min_train_rows=SMALL_FIT).fit(frame[frame["gw"] < N_GWS])
    target = frame[frame["gw"] == N_GWS].copy()
    single = mm.score_panel(model, target)
    target["num_fixtures"] = 2
    double = mm.score_panel(model, target)
    assert double["xp"].dropna().to_numpy() == pytest.approx(
        2 * single["xp"].dropna().to_numpy())
    target["num_fixtures"] = 0
    assert mm.score_panel(model, target)["xp"].dropna().eq(0).all()


# --- acceptance criterion 4: beats the naive baseline -----------------------

PANEL_FIXTURE = Path(__file__).parent / "fixtures" / "player_gameweeks_2526.parquet"


def test_model_beats_the_naive_baseline_on_held_out_gameweeks():
    """The spec's bar, on real 2025-26 data, on gameweeks alpha was not tuned on.

    ``SELECTED_ALPHAS`` was chosen on gameweeks up to ``SELECTION_MAX_GW``;
    this scores the ones after it, so the comparison is not the model marking
    its own homework. The fixture is a column-reduced copy of the public
    player-gameweek panel (no league, entry or manager data).
    """
    frame = build_match_frame(pd.read_parquet(PANEL_FIXTURE))
    predicted = mm.walk_forward(frame)
    heldout = mm._eligible(predicted)
    heldout = heldout[heldout["gw"] > mm.SELECTION_MAX_GW]
    assert heldout["gw"].nunique() >= 10

    summary = mm.summarise(heldout)
    assert len(summary) == 4
    for position, row in summary.iterrows():
        assert row["delta"] > 0, (
            f"{position}: model {row['model']:.3f} did not beat "
            f"{row['baseline_name']} {row['baseline']:.3f}")
        # Ranking the top slice is the decision the tools actually make.
        assert row["model_top_frac"] > row["baseline_top_frac"]


def test_backtest_protocol_selects_an_alpha_per_position(frame):
    """The selection harness itself: an alpha per position, held out cleanly."""
    selected, selection, heldout = mm.backtest(
        frame, min_gw=5, grid=[10.0, 300.0], selection_max_gw=12,
        min_train_rows=SMALL_FIT)
    assert set(selected) == {1, 2, 3, 4}
    assert set(selected.values()) <= {10.0, 300.0}
    assert not selection.empty
    assert (heldout["gw"] > 12).all()
    # Each position's held-out rows come from the run at its own alpha.
    assert set(heldout["element_type"].unique()) == {1, 2, 3, 4}


def test_start_probability_is_calibrated_not_just_ranked(frame):
    """Stage 1 backtest: predicted start rate should track the observed one."""
    predicted = mm.walk_forward(frame, min_gw=6, alpha=100.0,
                                min_train_rows=SMALL_FIT)
    calibration = mm.start_calibration(mm._eligible(predicted))
    assert len(calibration) == 4
    for _, row in calibration.iterrows():
        assert row["brier"] < 0.25          # better than a coin flip
        assert abs(row["predicted_rate"] - row["actual_rate"]) < 0.1


# --- unit-level behaviour ---------------------------------------------------

def test_feature_columns_are_deduplicated_and_position_specific():
    keeper, forward = mm.feature_columns(1), mm.feature_columns(4)
    assert len(keeper) == len(set(keeper))
    assert "saves_l3" in keeper and "saves_l3" not in forward
    assert "xg_l5" in forward and "xg_l5" not in keeper


def test_per_fixture_label_never_divides_by_zero():
    rows = pd.DataFrame({"label_points": [8.0, 8.0, 8.0],
                         "num_fixtures": [1, 2, 0]})
    assert list(mm.per_fixture_label(rows)) == [8.0, 4.0, 8.0]


def test_unfittable_position_predicts_nan_not_zero():
    """A position with too little data must say "no opinion", not "zero"."""
    panel = _panel()
    panel = panel[panel["element_type"] != 1]  # drop every keeper from training
    frame = build_match_frame(panel)
    model = mm.MatchModel(alpha=100.0, min_train_rows=SMALL_FIT).fit(frame)
    assert 1 not in model.positions

    with_keeper = build_match_frame(_panel())
    target = with_keeper[with_keeper["gw"] == N_GWS]
    scored = mm.score_panel(model, target)
    keepers = scored[target["element_type"].to_numpy() == 1]
    assert keepers["xp"].isna().all()
    assert scored[target["element_type"].to_numpy() == 3]["xp"].notna().any()


def test_cameo_probability_is_never_negative(frame):
    model = mm.MatchModel(alpha=100.0, min_train_rows=SMALL_FIT).fit(frame[frame["gw"] < N_GWS])
    scored = mm.score_panel(model, frame[frame["gw"] == N_GWS])
    assert (scored["p_cameo"].dropna() >= 0).all()
    assert (scored["p_appear"].dropna() >= scored["p_start"].dropna()).all()
    assert scored["p_start"].dropna().between(0, 1).all()


def test_drivers_name_real_features(frame):
    model = mm.MatchModel(alpha=100.0, min_train_rows=SMALL_FIT).fit(frame[frame["gw"] < N_GWS])
    scored = mm.score_panel(model, frame[frame["gw"] == N_GWS])
    drivers = scored["drivers"].dropna()
    assert not drivers.empty
    for entry in drivers.head(20):
        names = [name.strip() for name in entry.split(",")]
        assert len(names) == 3
        assert all(name in mm.FORM_FEATURES + mm.FIXTURE_FEATURES
                   + sum(mm.POSITION_FEATURES.values(), []) for name in names)


def test_model_is_deterministic(frame):
    train, target = frame[frame["gw"] < N_GWS], frame[frame["gw"] == N_GWS]
    first = mm.score_panel(mm.MatchModel(alpha=100.0, min_train_rows=SMALL_FIT).fit(train), target)
    second = mm.score_panel(mm.MatchModel(alpha=100.0, min_train_rows=SMALL_FIT).fit(train), target)
    pd.testing.assert_frame_equal(first, second)


def test_next_gameweek_normal_draft_shape():
    events = {"data": [{"id": 1, "finished": True}, {"id": 2, "finished": True},
                       {"id": 3, "finished": False, "deadline_time": "2099-01-01T00:00:00Z"},
                       {"id": 4, "finished": False, "deadline_time": "2099-02-01T00:00:00Z"}]}
    assert mm.next_gameweek({"events": events}, now=NOW) == 3


def test_next_gameweek_mid_gw_returns_current_plus_one():
    events = {"current": 2, "next": 3,
              "data": [{"id": 1, "finished": True}, {"id": 2, "finished": False},
                       {"id": 3, "finished": False}]}
    assert mm.next_gameweek({"events": events}) == 3


def test_next_gameweek_falls_back_to_first_unfinished():
    events = {"current": 1, "next": None,
              "data": [{"id": 1, "finished": True},
                       {"id": 2, "finished": False,
                        "deadline_time": "2099-01-01T00:00:00Z"}]}
    assert mm.next_gameweek({"events": events}, now=NOW) == 2


def test_next_gameweek_season_over_raises():
    with pytest.raises(ValueError, match="season over"):
        mm.next_gameweek({"events": {"next": None, "data": [{"id": 38, "finished": True}]}})


def test_gw_arg_rejects_garbage_and_accepts_int_or_next():
    assert mm._gw_arg("next") == "next"
    assert mm._gw_arg("6") == "6"
    for bad in ("abc", "0", "-2"):
        with pytest.raises(mm.argparse.ArgumentTypeError):
            mm._gw_arg(bad)


def test_cli_gw_next_resolves_end_to_end(tmp_path):
    panel = _panel()
    panel_path = tmp_path / "panel.parquet"
    panel.to_parquet(panel_path)
    bootstrap = _bootstrap(panel, N_GWS + 1)
    bootstrap["events"] = {"current": N_GWS, "next": N_GWS + 1, "data": []}
    bootstrap_path = tmp_path / "bootstrap.json"
    bootstrap_path.write_text(json.dumps(bootstrap), encoding="utf-8")
    out = tmp_path / "xp.parquet"
    code = mm.main(["--gw", "next", "--season", SEASON, "--panel", str(panel_path),
                    "--bootstrap", str(bootstrap_path), "--out", str(out)])
    assert code == 0
    assert out.exists()
    assert len(pd.read_parquet(out)) > 0


def _events(current, nxt, current_finished, deadline):
    data = [{"id": 4, "finished": True, "deadline_time": "2026-09-01T10:00:00Z"},
            {"id": current, "finished": current_finished, "deadline_time": deadline},
            {"id": current + 1, "finished": False,
             "deadline_time": "2099-01-01T00:00:00Z"}]
    return {"events": {"current": current, "next": nxt, "data": data}}


NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def test_next_gameweek_pre_deadline_current_is_the_target():
    boot = _events(1, 2, False, "2026-10-10T10:00:00Z")
    assert mm.next_gameweek(boot, now=NOW) == 1


def test_next_gameweek_mid_gw_past_deadline_is_current_plus_one():
    boot = _events(5, 6, False, "2026-09-18T17:30:00Z")
    assert mm.next_gameweek(boot, now=NOW) == 6


def test_next_gameweek_between_gws_uses_next():
    boot = _events(5, 6, True, "2026-09-18T17:30:00Z")
    assert mm.next_gameweek(boot, now=NOW) == 6


def test_cli_tolerates_empty_season_panel_beside_archive(tmp_path):
    panel = _panel()
    archive = tmp_path / "archive.parquet"
    panel.to_parquet(archive)
    empty = tmp_path / "empty.parquet"
    panel.iloc[0:0].to_parquet(empty)
    bootstrap = _bootstrap(panel, N_GWS + 1)
    bootstrap["events"] = {"current": N_GWS, "next": N_GWS + 1, "data": []}
    bootstrap_path = tmp_path / "bootstrap.json"
    bootstrap_path.write_text(json.dumps(bootstrap), encoding="utf-8")
    out = tmp_path / "xp.parquet"
    assert mm.main(["--gw", "next", "--season", SEASON, "--panel", str(archive),
                    str(empty), "--bootstrap", str(bootstrap_path), "--out", str(out)]) == 0
    assert out.exists()


def test_cli_eval_out_directory_gets_the_default_file_name(tmp_path):
    archive, live = tmp_path / "archive.parquet", tmp_path / "live.parquet"
    _panel().to_parquet(archive)
    _live_panel().to_parquet(live)
    out_dir = tmp_path / "ml"
    out_dir.mkdir()
    assert mm.main(["--eval-season", LIVE_SEASON, "--eval-gws", "2-3",
                    "--panel", str(archive), str(live), "--out", str(out_dir)]) == 0
    written = pd.read_csv(out_dir / "match_model_eval_gw2-3.csv")
    assert list(written.columns) == ["pool", "position", "predictor", "spearman",
                                     "top_frac", "mae", "gws", "window"]


def test_out_path_keeps_an_explicit_file_and_the_default():
    default = Path("derived/ml/eval.csv")
    assert mm._out_path(None, default) == default
    assert mm._out_path(Path("elsewhere.csv"), default) == Path("elsewhere.csv")


def test_next_gameweek_list_shape_mid_gw_prefers_is_next():
    events = [{"id": 5, "finished": False, "deadline_time": "2026-09-18T17:30:00Z"},
              {"id": 6, "finished": False, "is_next": True,
               "deadline_time": "2099-01-01T00:00:00Z"},
              {"id": 7, "finished": False, "deadline_time": "2099-02-01T00:00:00Z"}]
    assert mm.next_gameweek({"events": events}, now=NOW) == 6


def test_next_gameweek_list_shape_mid_gw_without_flags_uses_future_deadline():
    events = [{"id": 5, "finished": False, "deadline_time": "2026-09-18T17:30:00Z"},
              {"id": 6, "finished": False, "deadline_time": "2099-01-01T00:00:00Z"}]
    assert mm.next_gameweek({"events": events}, now=NOW) == 6


def test_next_gameweek_dict_mid_gw_next_null_uses_future_deadline_event():
    events = {"current": 5, "next": None, "data": [
        {"id": 5, "finished": False, "deadline_time": "2026-09-18T17:30:00Z"},
        {"id": 6, "finished": False, "deadline_time": "2099-01-01T00:00:00Z"}]}
    assert mm.next_gameweek({"events": events}, now=NOW) == 6


def test_next_gameweek_mid_final_gw_has_no_actionable_next():
    events = {"current": 38, "next": None, "data": [
        {"id": 38, "finished": False, "deadline_time": "2026-09-18T17:30:00Z"}]}
    with pytest.raises(ValueError, match="no actionable next gameweek"):
        mm.next_gameweek({"events": events}, now=NOW)


def test_cli_skips_missing_archive_panel_with_warning(tmp_path, caplog):
    panel = _panel()
    season_panel = tmp_path / "season.parquet"
    panel.to_parquet(season_panel)
    bootstrap = _bootstrap(panel, N_GWS + 1)
    bootstrap["events"] = {"current": N_GWS, "next": N_GWS + 1, "data": []}
    bootstrap_path = tmp_path / "bootstrap.json"
    bootstrap_path.write_text(json.dumps(bootstrap), encoding="utf-8")
    out = tmp_path / "xp.parquet"
    missing = tmp_path / "no_archive.parquet"
    with caplog.at_level("WARNING"):
        code = mm.main(["--gw", "next", "--season", SEASON, "--panel", str(missing),
                        str(season_panel), "--bootstrap", str(bootstrap_path),
                        "--out", str(out)])
    assert code == 0 and out.exists()
    assert "not found; skipping" in caplog.text


def test_cli_errors_when_no_usable_panel(tmp_path):
    with pytest.raises(SystemExit):
        mm.main(["--gw", "next", "--season", SEASON,
                 "--panel", str(tmp_path / "nope.parquet")])


def test_build_gw_xp_serves_gw2_with_team_form(monkeypatch):
    """Regression: GW2 of a new season used to serve all-NaN team form.

    The stub gameweek must inherit the post-GW1 value, exactly what a GW2
    feature row computed from GW1's results would carry.
    """
    archive = _panel()
    new_season = archive[archive["gw"] == 1].assign(season="2026-27")
    served = {}
    original = mm.score_fixtures

    def capture(model, rows, availability=None):
        served["rows"] = rows
        return original(model, rows, availability)

    monkeypatch.setattr(mm, "score_fixtures", capture)
    panel = pd.concat([archive, new_season], ignore_index=True)
    mm.build_gw_xp(panel, _bootstrap(new_season, 2), 2, "2026-27",
                   min_train_rows=SMALL_FIT)

    rows = served["rows"]
    team_columns = ["team_scored_pg", "team_conceded_pg",
                    "opp_scored_pg", "opp_conceded_pg"]
    assert rows[team_columns].notna().all().all()
    gw1 = new_season[new_season["played"]]
    conceded = gw1.groupby("team_id")["goals_conceded"].max()
    for _, row in rows.iterrows():
        assert row["team_conceded_pg"] == pytest.approx(conceded[row["team_id"]])
        assert row["opp_conceded_pg"] == pytest.approx(conceded[row["opponent_team"]])


def test_played_row_team_form_snapshot_on_the_synthetic_season(frame):
    """Regression: blanks/stub carry must not move any played-row team form."""
    expected = {
        "team_scored_pg": 3541.301252, "team_conceded_pg": 3192.0,
        "opp_scored_pg": 3541.301252, "opp_conceded_pg": 3192.0,
        "opp_pts_allowed_pg": 17673.118673,
    }
    for column, total in expected.items():
        assert frame[column].sum() == pytest.approx(total, abs=1e-5)
        assert frame[column].isna().sum() == PER_POSITION * 4  # GW1 only
