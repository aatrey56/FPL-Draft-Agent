"""Tests for waiver_plan (backend.ml.waiver). No network — fixtures only."""

import json
import os

import pandas as pd
import pytest
from test_matchmodel import N_GWS, SMALL_FIT
from test_matchmodel import SEASON as PANEL_SEASON
from test_matchmodel import _bootstrap as _panel_bootstrap
from test_matchmodel import _panel as _synthetic_panel

from backend.ml import jsonutil
from backend.ml import matchmodel as mm
from backend.ml import waiver as wv

TEAMS = [{"id": 1, "name": "Arsenal", "short_name": "ARS"},
         {"id": 2, "name": "Wolves", "short_name": "WOL"},
         {"id": 3, "name": "Hull City", "short_name": "HUL"}]  # promoted: no 25/26 rows

SEASONS = pd.DataFrame([
    {"season": "2025-26", "team_name": "Arsenal", "total_points": 2000},
    {"season": "2025-26", "team_name": "Wolves", "total_points": 1200},
])


def test_team_strengths_with_promoted_prior():
    strengths = wv.team_strengths(SEASONS, TEAMS)
    assert strengths[1] == 2000 and strengths[2] == 1200
    assert strengths[3] == pytest.approx(0.9 * 1200)  # promoted prior below the floor


def test_fixture_multiplier_direction_and_bounds():
    strengths = {1: 2000.0, 2: 1200.0, 3: 1080.0}
    weak = wv.fixture_multiplier(1080.0, strengths, is_home=True)
    strong = wv.fixture_multiplier(2000.0, strengths, is_home=True)
    assert weak > 1.0 > strong                       # easy fixture boosts, hard reduces
    for mult in (weak, strong):
        assert wv.FIXTURE_MULT_MIN <= mult <= wv.FIXTURE_MULT_MAX
    assert wv.fixture_multiplier(1080.0, strengths, True) > \
        wv.fixture_multiplier(1080.0, strengths, False)  # home > away


def test_next_fixture_load_handles_blank_and_double():
    bootstrap = {"teams": TEAMS, "fixtures": {
        "1": [{"team_h": 1, "team_a": 2}],
        "2": [{"team_h": 2, "team_a": 3}, {"team_h": 3, "team_a": 2}],  # DGW for 2 and 3
        "3": [],                                                        # blank for all
    }}
    strengths = wv.team_strengths(SEASONS, TEAMS)
    load = wv.next_fixture_load(bootstrap, strengths)
    assert load[1] > 0                       # one fixture
    assert load[2] > load[1]                 # three fixtures beats one
    per_fix_max = wv.FIXTURE_MULT_MAX
    assert load[2] <= 3 * per_fix_max


def test_availability_factor_gates():
    assert wv.availability_factor({"status": "a"}) == 1.0
    assert wv.availability_factor({"status": "u"}) == 0.0
    assert wv.availability_factor({"status": "d", "chance_of_playing_next_round": 50}) == 0.5
    assert wv.availability_factor({"status": "d"}) == 0.75
    assert wv.availability_factor({"status": "i"}) == 0.0


def _players_fixture(tmp_path):
    """Small world: my squad has a weak FWD; the market has three FWDs."""
    bootstrap = {
        "teams": TEAMS,
        "fixtures": {"1": [{"team_h": 1, "team_a": 2}], "2": [{"team_h": 2, "team_a": 1}],
                     "3": [{"team_h": 1, "team_a": 3}]},
        "elements": [
            # my players (owned by 42)
            {"id": 10, "code": 100, "web_name": "MyGK", "element_type": 1, "team": 1, "status": "a"},
            {"id": 11, "code": 101, "web_name": "MyWeakFWD", "element_type": 4, "team": 2, "status": "a"},
            # market
            {"id": 20, "code": 200, "web_name": "SeasonStar", "element_type": 4, "team": 1, "status": "a"},
            {"id": 21, "code": 201, "web_name": "Streamer", "element_type": 4, "team": 1, "status": "a"},
            {"id": 22, "code": 202, "web_name": "Departed", "element_type": 4, "team": 1, "status": "u"},
        ],
    }
    projections = [
        {"code": 100, "projected_points": 120.0},
        {"code": 101, "projected_points": 80.0},    # my weak FWD: ~2.1/GW
        {"code": 200, "projected_points": 150.0},   # better now AND all season
        {"code": 201, "projected_points": 60.0},    # worse season, but... (see test)
        {"code": 202, "projected_points": 200.0},   # departed — must never appear
    ]
    proj_path = tmp_path / "projections.json"
    proj_path.write_text(json.dumps(projections))
    players = wv.build_player_table(bootstrap, SEASONS, proj_path)
    status = {"element_status": [
        {"element": 10, "owner": 42}, {"element": 11, "owner": 42},
        {"element": 20, "owner": None}, {"element": 21, "owner": None},
        {"element": 22, "owner": None},
    ]}
    players["is_free_agent"] = players["element"].isin({20, 21, 22})
    return players, status


def test_recommend_labels_upgrade_and_excludes_departed(tmp_path):
    players, status = _players_fixture(tmp_path)
    squad = wv.my_squad(players, status, entry_id=42)
    assert len(squad) == 2
    recs = wv.recommend(players, squad)
    names = [r["add"] for r in recs]
    assert "SeasonStar" in names and "Departed" not in names
    star = next(r for r in recs if r["add"] == "SeasonStar")
    assert star["label"] == "upgrade"           # both horizons positive
    assert star["drop"] == "MyWeakFWD"
    assert star["season_gain"] == pytest.approx(150.0 - 80.0, abs=0.2)


def test_recommend_stream_label_when_only_short_term(tmp_path):
    players, status = _players_fixture(tmp_path)
    # Boost Streamer's next3 above MyWeakFWD's by injuring my FWD short-term:
    # simulate via availability (my FWD 0 chance next rounds -> next3 0)
    players.loc[players["web_name"] == "MyWeakFWD", "next3_xp"] = 0.5
    squad = wv.my_squad(players, status, entry_id=42)
    recs = wv.recommend(players, squad)
    streamer = next(r for r in recs if r["add"] == "Streamer")
    assert streamer["next3_gain"] > 0 and streamer["season_gain"] < 0
    assert streamer["label"] == "stream"


def test_best_xi_respects_formation():
    rows = []
    for i in range(2):
        rows.append({"element": i, "position": "GKP", "next3_xp": 10 - i})
    for i in range(5):
        rows.append({"element": 10 + i, "position": "DEF", "next3_xp": 20 - i})
        rows.append({"element": 20 + i, "position": "MID", "next3_xp": 30 - i})
    for i in range(3):
        rows.append({"element": 30 + i, "position": "FWD", "next3_xp": 15 - i})
    squad = pd.DataFrame(rows)
    xi, total = wv.best_xi(squad)
    assert len(xi) == 11
    counts = xi["position"].value_counts()
    assert counts["GKP"] == 1
    assert (counts.get("DEF", 0), counts.get("MID", 0), counts.get("FWD", 0)) in \
        [(d, m, f) for d, m, f in wv.FORMATIONS]
    assert total > 0


def test_unprojected_free_agents_are_never_recommended(tmp_path):
    """Real-run regression: NaN projections must not produce nan 'hold' recs."""
    players, status = _players_fixture(tmp_path)
    players.loc[players["web_name"] == "Streamer", ["ros_points", "next3_xp"]] = float("nan")
    squad = wv.my_squad(players, status, entry_id=42)
    recs = wv.recommend(players, squad)
    assert all(r["add"] != "Streamer" for r in recs)
    assert all(not pd.isna(r["season_gain"]) for r in recs)


def test_unprojected_teammate_is_never_the_drop_candidate(tmp_path):
    """The Maddison case: a squad player with no projection (long injury last
    season / new signing) must never be auto-dropped, and must be surfaced in
    unprojected_squad for human judgment instead."""
    bootstrap = {
        "teams": TEAMS,
        "fixtures": {"1": [{"team_h": 1, "team_a": 2}]},
        "elements": [
            {"id": 11, "code": 101, "web_name": "MyWeakFWD", "element_type": 4, "team": 2, "status": "a"},
            {"id": 12, "code": 102, "web_name": "MyMysteryFWD", "element_type": 4, "team": 1, "status": "a"},
            {"id": 20, "code": 200, "web_name": "SeasonStar", "element_type": 4, "team": 1, "status": "a"},
        ],
    }
    proj = tmp_path / "p.json"
    proj.write_text(json.dumps([
        {"code": 101, "projected_points": 80.0},
        {"code": 200, "projected_points": 150.0},   # MyMysteryFWD: no projection
    ]))
    players = wv.build_player_table(bootstrap, SEASONS, proj)
    status = {"element_status": [
        {"element": 11, "owner": 42}, {"element": 12, "owner": 42},
        {"element": 20, "owner": None},
    ]}
    players["is_free_agent"] = players["element"].isin({20})
    squad = wv.my_squad(players, status, entry_id=42)

    recs = wv.recommend(players, squad)
    star = next(r for r in recs if r["add"] == "SeasonStar")
    assert star["drop"] == "MyWeakFWD"          # never the unknown
    assert all(r["drop"] != "MyMysteryFWD" for r in recs)
    assert star["season_gain"] == pytest.approx(150.0 - 80.0, abs=0.2)  # real gain, not vs 0

    unknown = wv.unprojected_squad(squad)
    assert [p["web_name"] for p in unknown] == ["MyMysteryFWD"]


def test_injury_gates_next3_but_not_season_value(tmp_path):
    """Real-run regression: an injured player's ROS survives; next3 goes to 0."""
    bootstrap = {
        "teams": TEAMS,
        "fixtures": {"1": [{"team_h": 1, "team_a": 2}]},
        "elements": [{"id": 50, "code": 500, "web_name": "InjuredStar",
                      "element_type": 2, "team": 1, "status": "i",
                      "chance_of_playing_next_round": 0,
                      "news": "Groin injury"}],
    }
    proj = tmp_path / "p.json"
    proj.write_text(json.dumps([{"code": 500, "projected_points": 140.0}]))
    table = wv.build_player_table(bootstrap, SEASONS, proj)
    row = table.iloc[0]
    assert row["next3_xp"] == 0.0          # gated now
    assert row["ros_points"] == 140.0      # season value intact


# ---------------------------------------------------------------------------
# Match xP wiring (xp_next / xp_source / next1_gain)
# ---------------------------------------------------------------------------

def _gw_xp(rows, gw=1):
    """Match-model next-GW frame with the columns build_player_table reads."""
    return pd.DataFrame([{
        "code": code, "xp": xp, "p_start": 0.9, "xp_floor": xp - 2.0, "xp_ceiling": xp + 3.0,
        "drivers": "pts_std", "opponents": "vWOL", "gw": gw, "panel_max_gw": gw - 1}
        for code, xp in rows])


def _xp_table(tmp_path, gw_xp, horizon_xp=None):
    """Player table over a small world, with projections for 101/200/201 only."""
    projections = [{"code": 101, "projected_points": 80.0},
                   {"code": 200, "projected_points": 150.0},
                   {"code": 201, "projected_points": 60.0}]
    proj_path = tmp_path / "projections.json"
    proj_path.write_text(json.dumps(projections))
    bootstrap = {
        "teams": TEAMS, "fixtures": {"1": [{"team_h": 1, "team_a": 2}]},
        "elements": [
            {"id": 11, "code": 101, "web_name": "MyWeakFWD", "element_type": 4, "team": 2, "status": "a"},
            {"id": 20, "code": 200, "web_name": "SeasonStar", "element_type": 4, "team": 1, "status": "a"},
            {"id": 21, "code": 201, "web_name": "Streamer", "element_type": 4, "team": 1, "status": "a"},
            {"id": 99, "code": 999, "web_name": "NoProj", "element_type": 4, "team": 1, "status": "a"}]}
    return wv.build_player_table(bootstrap, SEASONS, proj_path, gw_xp=gw_xp,
                                 horizon_xp=horizon_xp)


def test_covered_player_takes_model_xp_and_uncovered_falls_back(tmp_path):
    table = _xp_table(tmp_path, _gw_xp([(200, 6.5)]))
    star = table[table["web_name"] == "SeasonStar"].iloc[0]
    assert star["xp_source"] == "model" and star["xp_next"] == pytest.approx(6.5)
    assert star["p_start"] == pytest.approx(0.9) and star["opponents"] == "vWOL"
    streamer = table[table["web_name"] == "Streamer"].iloc[0]
    assert streamer["xp_source"] == "heuristic"
    assert streamer["xp_next"] > 0 and pd.isna(streamer["p_start"])   # ros 60/38 x load
    assert table[table["web_name"] == "NoProj"].iloc[0]["xp_source"] == "none"


def test_no_gw_xp_is_all_heuristic(tmp_path):
    table = _xp_table(tmp_path, None)
    assert set(table["xp_source"]) <= {"heuristic", "none"}


def test_load_gw_xp_missing_and_stale_return_none_with_warning(tmp_path, caplog):
    assert wv.load_gw_xp(tmp_path / "xp_gw6.parquet", 6) is None
    path = tmp_path / "xp_gw5.parquet"
    _gw_xp([(200, 6.5)], gw=5).to_parquet(path)
    with caplog.at_level("WARNING"):
        assert wv.load_gw_xp(path, 6) is None
    assert "stale" in caplog.text
    assert wv.load_gw_xp(path, 5) is not None


NEXT_GW6 = {"events": {"current": 5, "next": 6, "data": [
    {"id": 5, "finished": True, "deadline_time": "2026-09-18T17:30:00Z"},
    {"id": 6, "finished": False, "deadline_time": "2099-10-10T10:00:00Z"}]},
    "fixtures": {"6": []}}


def test_resolve_scorer_heuristic_never_reads_the_file(tmp_path):
    bootstrap = NEXT_GW6
    _gw_xp([(200, 6.5)], gw=6).to_parquet(tmp_path / "xp_gw6.parquet")
    frame, meta = wv.resolve_scorer("heuristic", bootstrap, tmp_path)
    assert frame is None and meta["scorer"] == "heuristic" and not meta["xp_fallback"]
    frame, meta = wv.resolve_scorer("model", bootstrap, tmp_path)
    assert frame is not None and meta == {"scorer": "model", "scorer_requested": "model",
                                          "xp_fallback": False, "xp_fallback_reason": None}


def test_resolve_scorer_reports_fallback_when_file_missing_or_stale(tmp_path):
    bootstrap = NEXT_GW6
    frame, meta = wv.resolve_scorer("model", bootstrap, tmp_path)
    assert frame is None
    assert meta["scorer"] == "heuristic" and meta["scorer_requested"] == "model"
    assert meta["xp_fallback"] and "missing" in meta["xp_fallback_reason"]
    _gw_xp([(200, 6.5)], gw=5).to_parquet(tmp_path / "xp_gw6.parquet")
    frame, meta = wv.resolve_scorer("model", bootstrap, tmp_path)
    assert frame is None and "stale" in meta["xp_fallback_reason"]


def test_cli_writes_the_scorer_actually_used(weekly_cli_root, weekly_cli_argv, tmp_path):
    """Review regression: --scorer model with no xP file wrote scorer "model"."""
    out = tmp_path / "out" / "waiver_plan.json"
    assert wv.main(weekly_cli_argv(out, "model")) == 0
    doc = json.loads(out.read_text())
    assert doc["scorer"] == "heuristic" and doc["scorer_requested"] == "model"
    assert doc["xp_fallback"] is True and "xp_gw6.parquet missing" in doc["xp_fallback_reason"]
    assert {r["add_xp_source"] for r in doc["recommendations"]} <= {"heuristic"}

    xp_path = weekly_cli_root / "derived/2026-27/ml/xp_gw6.parquet"
    _gw_xp([(102, 6.0), (101, 1.0)], gw=6).to_parquet(xp_path)
    assert wv.main(weekly_cli_argv(out, "model")) == 0
    doc = json.loads(out.read_text())
    assert doc["scorer"] == "model" and doc["xp_fallback"] is False
    assert doc["xp_fallback_reason"] is None


def test_next1_gain_ranks_and_hold_follows_stream(tmp_path):
    players, status = _players_fixture(tmp_path)
    # SeasonStar: next-GW worse than my FWD but far better ROS (hold);
    # Streamer: next-GW better, ROS worse (stream).
    players.loc[players["web_name"] == "MyWeakFWD", "xp_next"] = 2.0
    players.loc[players["web_name"] == "SeasonStar", "xp_next"] = 1.0
    players.loc[players["web_name"] == "Streamer", "xp_next"] = 2.5
    squad = wv.my_squad(players, status, entry_id=42)
    recs = wv.recommend(players, squad)
    assert [r["add"] for r in recs] == ["Streamer", "SeasonStar"]
    assert [r["label"] for r in recs] == ["stream", "hold"]
    assert recs[0]["next1_gain"] == pytest.approx(0.5)
    assert recs[1]["next1_gain"] < 0 < recs[1]["season_gain"]


def test_hold_never_ranks_above_a_positive_next1_rec(tmp_path):
    players, status = _players_fixture(tmp_path)
    players.loc[players["web_name"] == "MyWeakFWD", "xp_next"] = 2.0
    players.loc[players["web_name"] == "SeasonStar", "xp_next"] = 1.0   # hold, +70 ROS
    players.loc[players["web_name"] == "Streamer", "xp_next"] = 2.01    # tiny positive next1
    squad = wv.my_squad(players, status, entry_id=42)
    labels = [r["label"] for r in wv.recommend(players, squad)]
    assert labels.index("hold") > labels.index("stream")


def test_legacy_ranking_orders_by_larger_of_next3_and_season_gain(tmp_path):
    players, status = _players_fixture(tmp_path)
    squad = wv.my_squad(players, status, entry_id=42)
    legacy = wv.recommend(players, squad, rank_by="legacy")
    gains = [max(r["next3_gain"], r["season_gain"]) for r in legacy]
    assert gains == sorted(gains, reverse=True)


def test_default_scorer_is_model_and_heuristic_stays_available():
    assert wv.DEFAULT_SCORER == "model" and "heuristic" in wv.SCORERS


def _model_only_world(tmp_path, xp_rows, horizon_xp=None):
    """MyWeakFWD (projected, heuristic) in my squad; NoProj and Streamer free."""
    table = _xp_table(tmp_path, _gw_xp(xp_rows), horizon_xp)
    status = {"element_status": [{"element": 11, "owner": 42},
                                 {"element": 21, "owner": None},
                                 {"element": 99, "owner": None}]}
    table["is_free_agent"] = table["element"].isin({21, 99})
    return table, wv.my_squad(table, status, entry_id=42)


def test_model_valued_free_agent_without_ros_is_ranked_as_stream(tmp_path):
    """Review regression: promoted-club players (no ROS projection) were
    skipped even when the match model rated them highly."""
    table, squad = _model_only_world(tmp_path, [(999, 6.0)])
    recs = wv.recommend(table, squad)
    noproj = next(r for r in recs if r["add"] == "NoProj")
    assert noproj["add_xp_source"] == "model" and pd.isna(noproj["add_ros"])
    assert noproj["season_gain"] is None and noproj["season_unknown"] is True
    assert noproj["add_ros_unknown"] is True and noproj["drop_ros_unknown"] is False
    assert noproj["label"] == "stream"
    assert noproj["next1_gain"] > 0 and noproj["drop"] == "MyWeakFWD"
    assert recs[0]["add"] == "NoProj"          # ranked on next1_gain like anyone else


def test_season_unknown_is_false_and_gain_numeric_for_projected_adds(tmp_path):
    table, squad = _model_only_world(tmp_path, [(999, 6.0), (201, 5.0)])
    recs = wv.recommend(table, squad)
    projected = [r for r in recs if r["add"] != "NoProj"]
    assert projected and all(r["season_unknown"] is False for r in projected)
    assert all(isinstance(r["season_gain"], float) for r in projected)


def test_unknown_season_gain_orders_like_zero_among_equal_next1(tmp_path):
    """Ranking is unchanged by the null: an unknown season gain sorts as 0,
    below an equal-next1 add with a positive season gain."""
    table, squad = _model_only_world(tmp_path, [(999, 6.0), (201, 6.0)])
    table.loc[table["web_name"] == "Streamer", ["ros_points", "ros_adj"]] = 200.0
    recs = wv.recommend(table, squad)
    assert [r["add"] for r in recs][:2] == ["Streamer", "NoProj"]
    assert recs[1]["season_gain"] is None and recs[0]["season_gain"] > 0


def test_season_unknown_rec_survives_the_json_round_trip(tmp_path):
    table, squad = _model_only_world(tmp_path, [(999, 6.0)])
    doc = json.loads(wv.jsonutil.dumps_strict(wv.recommend(table, squad)))   # the CLI's writer
    noproj = next(r for r in doc if r["add"] == "NoProj")
    assert noproj["season_gain"] is None and noproj["season_unknown"] is True


def test_next3_gain_is_null_for_an_add_without_a_ros_projection(tmp_path):
    """Review regression: a model-only add has no heuristic 3-GW value, but
    its null counted as 0 and next3_gain came out as minus the drop's next3."""
    table, squad = _model_only_world(tmp_path, [(999, 6.0), (201, 5.0)])
    recs = wv.recommend(table, squad)
    noproj = next(r for r in recs if r["add"] == "NoProj")
    assert pd.isna(noproj["add_next3_xp"]) and noproj["drop_next3_xp"] > 0
    assert noproj["next3_gain"] is None
    streamer = next(r for r in recs if r["add"] == "Streamer")     # projected: a number
    assert streamer["next3_gain"] == pytest.approx(
        streamer["add_next3_xp"] - streamer["drop_next3_xp"], abs=0.05)
    doc = json.loads(wv.jsonutil.dumps_strict(recs))                # the CLI's writer
    assert next(r for r in doc if r["add"] == "NoProj")["next3_gain"] is None


def test_cli_prints_a_question_mark_for_an_unknown_next3_gain(
        weekly_cli_root, weekly_cli_argv, tmp_path, capsys):
    xp_path = weekly_cli_root / "derived/2026-27/ml/xp_gw6.parquet"
    _gw_xp([(103, 6.0), (102, 5.0), (101, 1.0)], gw=6).to_parquet(xp_path)
    out = tmp_path / "out" / "waiver_plan.json"
    assert wv.main(weekly_cli_argv(out, "model")) == 0
    recs = {r["add"]: r for r in json.loads(out.read_text())["recommendations"]}
    assert recs["Promoted"]["next3_gain"] is None and recs["Promoted"]["add_next3_xp"] is None
    assert isinstance(recs["FreeFWD"]["next3_gain"], float)
    lines = {line.split()[1]: line.split()
             for line in capsys.readouterr().out.splitlines() if "(FWD) ->" in line}
    # columns: label, add, (pos), ->, drop, next1, next3, season, src
    assert lines["Promoted"][6:8] == ["?", "?"]
    assert lines["FreeFWD"][6].startswith(("+", "-"))


def test_model_only_free_agent_with_no_next1_gain_is_not_recommended(tmp_path):
    table, squad = _model_only_world(tmp_path, [(999, 0.1)])
    assert all(r["add"] != "NoProj" for r in wv.recommend(table, squad))


def test_unprojected_free_agent_stays_skipped_without_model_value(tmp_path):
    table, squad = _model_only_world(tmp_path, [(200, 6.0)])   # NoProj not covered
    assert all(r["add"] != "NoProj" for r in wv.recommend(table, squad))
    table, squad = _model_only_world(tmp_path, [(999, 6.0)])
    legacy = wv.recommend(table, squad, rank_by="legacy")      # heuristic scorer
    assert all(r["add"] != "NoProj" for r in legacy)


def test_load_gw_xp_rejects_a_file_trained_on_an_unrefreshed_panel(tmp_path, caplog):
    """Review regression: a failed panel rebuild let xP train on a stale panel
    while its gw still matched. panel_max_gw must be the last finished GW."""
    path = tmp_path / "xp_gw6.parquet"
    _gw_xp([(200, 6.5)], gw=6).assign(panel_max_gw=4).to_parquet(path)
    with caplog.at_level("WARNING"):
        assert wv.load_gw_xp(path, 6) is None
    assert "panel not refreshed" in caplog.text
    frame, reason = wv.read_gw_xp(path, 6)
    assert frame is None and "through gw [4], but the last finished gw is 5" in reason


MID_GW6 = {"events": {"current": 5, "next": 6, "data": [
    {"id": 4, "finished": True, "deadline_time": "2026-09-11T17:30:00Z"},
    {"id": 5, "finished": False, "deadline_time": "2026-09-18T17:30:00Z"},
    {"id": 6, "finished": False, "deadline_time": "2099-10-10T10:00:00Z"}]},
    "fixtures": {"5": [], "6": []}}


def test_resolve_scorer_serves_model_xp_mid_gameweek(tmp_path):
    """Review regression: with GW5 in play and GW6 next, the freshest panel
    ends at GW4. The old rule (panel_max_gw == N-1) rejected every xP file
    from the deadline until GW5 finished — exactly when matchday derive runs."""
    _gw_xp([(200, 6.5)], gw=6).assign(panel_max_gw=4).to_parquet(tmp_path / "xp_gw6.parquet")
    frame, meta = wv.resolve_scorer("model", MID_GW6, tmp_path)
    assert frame is not None and meta["scorer"] == "model" and not meta["xp_fallback"]


def test_resolve_scorer_rejects_a_panel_behind_the_last_finished_gameweek(tmp_path):
    """Mid-GW, a panel through GW3 when GW4 is finished is genuinely stale."""
    _gw_xp([(200, 6.5)], gw=6).assign(panel_max_gw=3).to_parquet(tmp_path / "xp_gw6.parquet")
    frame, meta = wv.resolve_scorer("model", MID_GW6, tmp_path)
    assert frame is None and meta["xp_fallback"]
    assert meta["xp_fallback_reason"] == (
        "match xP file xp_gw6.parquet was trained on a panel through gw [3], "
        "but the last finished gw is 4 (panel not refreshed; stale)")


def test_resolve_scorer_rejects_a_mid_gameweek_file_once_the_gameweek_finishes(tmp_path):
    """The file built mid-GW (panel through 4) goes stale when GW5 finishes."""
    _gw_xp([(200, 6.5)], gw=6).assign(panel_max_gw=4).to_parquet(tmp_path / "xp_gw6.parquet")
    frame, meta = wv.resolve_scorer("model", NEXT_GW6, tmp_path)
    assert frame is None and "last finished gw is 5" in meta["xp_fallback_reason"]


@pytest.mark.parametrize("content", [b"not a parquet file", b""])
def test_resolve_scorer_falls_back_on_an_unreadable_xp_file(tmp_path, content):
    """Review regression: a corrupt or truncated parquet raised out of
    read_gw_xp and crashed waiver_plan / my_week instead of falling back."""
    (tmp_path / "xp_gw6.parquet").write_bytes(content)
    frame, meta = wv.resolve_scorer("model", NEXT_GW6, tmp_path)
    assert frame is None and meta["scorer"] == "heuristic" and meta["xp_fallback"]
    assert meta["xp_fallback_reason"].startswith("match xP file xp_gw6.parquet is unreadable")
    assert wv.load_gw_xp(tmp_path / "xp_gw6.parquet", 6) is None


def test_cli_falls_back_on_an_unreadable_xp_file(weekly_cli_root, weekly_cli_argv, tmp_path):
    (weekly_cli_root / "derived/2026-27/ml/xp_gw6.parquet").write_bytes(b"\x00garbage")
    out = tmp_path / "out" / "waiver_plan.json"
    assert wv.main(weekly_cli_argv(out, "model")) == 0
    doc = json.loads(out.read_text())
    assert doc["scorer"] == "heuristic" and doc["xp_fallback"] is True
    assert "unreadable" in doc["xp_fallback_reason"]


def test_load_gw_xp_rejects_a_file_without_panel_max_gw(tmp_path):
    path = tmp_path / "xp_gw6.parquet"
    _gw_xp([(200, 6.5)], gw=6).drop(columns="panel_max_gw").to_parquet(path)
    frame, reason = wv.read_gw_xp(path, 6)
    assert frame is None and "no panel_max_gw" in reason


def test_load_gw_xp_accepts_gw1_with_an_empty_season_panel(tmp_path):
    path = tmp_path / "xp_gw1.parquet"
    _gw_xp([(200, 6.5)], gw=1).to_parquet(path)              # panel_max_gw 0
    assert wv.load_gw_xp(path, 1) is not None


def test_resolve_scorer_without_an_event_calendar_falls_back():
    frame, meta = wv.resolve_scorer("model", {"fixtures": {"6": []}}, "/nonexistent")
    assert frame is None and meta["xp_fallback"]
    assert meta["xp_fallback_reason"] == "no upcoming gameweek in the bootstrap"


def test_upcoming_fixtures_drops_the_in_play_gameweek():
    bootstrap = {"events": {"current": 6, "next": 7, "data": [
        {"id": 6, "finished": False, "deadline_time": "2026-10-03T10:00:00Z"},
        {"id": 7, "finished": False, "deadline_time": "2099-10-17T10:00:00Z"}]},
        "fixtures": {"6": ["a"], "7": ["b"], "8": ["c"]}}
    assert wv.upcoming_fixtures(bootstrap) == {"7": ["b"], "8": ["c"]}
    assert wv.upcoming_fixtures({"fixtures": {"6": ["a"]}}) == {"6": ["a"]}


def test_unprojected_squad_means_no_projection_and_no_model_xp(tmp_path):
    """waiver_plan and my_week share one definition of "no value". A squad
    player the model covers is not listed, but is still never the drop."""
    table = _xp_table(tmp_path, _gw_xp([(999, 4.0)]))       # NoProj: model only
    status = {"element_status": [{"element": 11, "owner": 42}, {"element": 99, "owner": 42},
                                 {"element": 20, "owner": None}]}
    table["is_free_agent"] = table["element"].isin({20})
    squad = wv.my_squad(table, status, entry_id=42)
    assert wv.unprojected_squad(squad) == []
    assert all(r["drop"] == "MyWeakFWD" for r in wv.recommend(table, squad))

    table = _xp_table(tmp_path, _gw_xp([(200, 4.0)]))       # NoProj: no value at all
    squad = wv.my_squad(table, status, entry_id=42)
    assert [p["web_name"] for p in wv.unprojected_squad(squad)] == ["NoProj"]


def test_unfitted_position_is_not_served_as_model_xp(tmp_path, caplog):
    """Review regression: a panel too thin to fit one position still yields
    rows for its players (xp 0, p_start NaN) and they were served as
    xp_source "model" — a fabricated zero. They take the heuristic instead."""
    full = _synthetic_panel()
    gw = N_GWS + 1
    bootstrap = _panel_bootstrap(full, gw)          # every keeper has a fixture stub
    bootstrap["teams"] = [{**team, "name": team["short_name"]} for team in bootstrap["teams"]]
    for element in bootstrap["elements"]:
        element["web_name"] = f"P{element['code']}"
    no_keepers = full[full["element_type"] != 1]    # the keeper model cannot be fitted
    gw_xp = mm.build_gw_xp(no_keepers, bootstrap, gw, PANEL_SEASON, min_train_rows=SMALL_FIT)
    keeper_rows = gw_xp[gw_xp["element_type"] == 1]
    assert len(keeper_rows) and (keeper_rows["xp"] == 0).all()
    assert keeper_rows["p_start"].isna().all()

    keepers = sorted(int(code) for code in keeper_rows["code"])
    outfielder = int(gw_xp.loc[gw_xp["element_type"] == 3, "code"].iloc[0])
    proj_path = tmp_path / "projections.json"
    proj_path.write_text(json.dumps([{"code": keepers[0], "projected_points": 114.0},
                                     {"code": outfielder, "projected_points": 114.0}]))
    with caplog.at_level("WARNING"):
        table = wv.build_player_table(bootstrap, SEASONS, proj_path, gw_xp=gw_xp).set_index("code")
    assert f"no prediction for {len(keepers)} of {len(gw_xp)} players" in caplog.text

    assert set(table.loc[keepers, "xp_source"]) == {"heuristic", "none"}
    projected = table.loc[keepers[0]]
    assert projected["xp_source"] == "heuristic" and projected["xp_next"] > 0
    assert pd.isna(projected["p_start"]) and pd.isna(projected["drivers"])
    unprojected = table.loc[keepers[1]]
    assert unprojected["xp_source"] == "none" and pd.isna(unprojected["xp_next"])
    assert table.loc[outfielder, "xp_source"] == "model"
    assert table.loc[outfielder, "p_start"] == pytest.approx(
        gw_xp.set_index("code").loc[outfielder, "p_start"])


def test_usable_gw_xp_keeps_a_predicted_blank_and_drops_no_opinion_rows():
    frame = _gw_xp([(200, 0.0), (201, 4.0), (999, 0.0)])
    frame.loc[frame["code"] == 201, "xp"] = float("nan")        # no xP at all
    frame.loc[frame["code"] == 999, "p_start"] = float("nan")   # unfitted position
    assert list(wv.usable_gw_xp(frame)["code"]) == [200]        # xp 0 with a p_start stays


# ---------------------------------------------------------------------------
# Role signals: club_moved / expected_minutes / ros_adj
# ---------------------------------------------------------------------------

ROLE_SEASONS = pd.DataFrame([
    {"season": "2025-26", "code": 300, "team_name": "Wolves", "total_points": 1200},
    {"season": "2025-26", "code": 301, "team_name": "Arsenal", "total_points": 2000},
    {"season": "2024-25", "code": 302, "team_name": "Wolves", "total_points": 900},
])


def _panel(minutes_by_code: dict[int, list[int]]) -> pd.DataFrame:
    """Season panel: one row per (code, gw); ``None`` minutes = no row that GW."""
    return pd.DataFrame([
        {"season": "2026-27", "code": code, "gw": gw, "minutes": minutes}
        for code, per_gw in minutes_by_code.items()
        for gw, minutes in enumerate(per_gw, start=1) if minutes is not None])


def _role_table(tmp_path, season_panel, *, gw_xp=None, extra_elements=()):
    """ClubMover (Wolves -> Arsenal, proj 175), Stayer (Arsenal), Newcomer."""
    projections = [{"code": 300, "projected_points": 175.0},
                   {"code": 301, "projected_points": 100.0},
                   {"code": 302, "projected_points": 90.0}]
    proj_path = tmp_path / "projections.json"
    proj_path.write_text(json.dumps(projections))
    bootstrap = {
        "teams": TEAMS, "fixtures": {"1": [{"team_h": 1, "team_a": 2}]},
        "elements": [
            {"id": 30, "code": 300, "web_name": "ClubMover", "element_type": 2, "team": 1, "status": "a"},
            {"id": 31, "code": 301, "web_name": "Stayer", "element_type": 2, "team": 1, "status": "a"},
            {"id": 32, "code": 302, "web_name": "Newcomer", "element_type": 2, "team": 2, "status": "a"},
            *extra_elements]}
    table = wv.build_player_table(bootstrap, ROLE_SEASONS, proj_path, gw_xp=gw_xp,
                                  season_panel=season_panel)
    return table.set_index("web_name")


def test_role_factor_math():
    assert wv.role_factor(True, 0.0) == wv.ROLE_FLOOR            # 0 min -> floor
    assert wv.role_factor(True, 30.0) == pytest.approx(0.5)      # linear in between
    assert wv.role_factor(True, 60.0) == 1.0
    assert wv.role_factor(True, 90.0) == 1.0                     # 60+ min -> full
    assert wv.role_factor(False, 0.0) == 1.0                     # not moved: never scaled
    assert wv.role_factor(None, 0.0) == 1.0                      # no prior club: unknown
    assert wv.role_factor(True, None) == 1.0                     # no minutes yet: unknown
    assert wv.role_factor(True, 0.0, "a") == wv.ROLE_FLOOR       # available: scaled
    for status in ("d", "i", "s", None):                         # absence explains the minutes
        assert wv.role_factor(True, 0.0, status) == 1.0, status


def test_injured_or_doubtful_club_mover_keeps_his_full_projection(tmp_path):
    """Regression (live GW6): flagged club-movers were scaled for minutes
    their injury cost them (Struijk ROS 108 -> 35, Wilson 105 -> 51)."""
    panel = _panel({300: [0, 0, 0, 0, 0], 303: [0, 0, 0, 0, 0]})
    hurt = [{"id": 33, "code": 303, "web_name": "HurtMover", "element_type": 2, "team": 1,
             "status": "d", "chance_of_playing_next_round": 50, "news": "Knock"}]
    seasons = pd.concat([ROLE_SEASONS, pd.DataFrame([
        {"season": "2025-26", "code": 303, "team_name": "Wolves", "total_points": 140}])],
        ignore_index=True)
    projections = tmp_path / "projections.json"
    projections.write_text(json.dumps([{"code": 300, "projected_points": 175.0},
                                       {"code": 303, "projected_points": 140.0}]))
    bootstrap = {"teams": TEAMS, "fixtures": {"1": [{"team_h": 1, "team_a": 2}]}, "elements": [
        {"id": 30, "code": 300, "web_name": "ClubMover", "element_type": 2, "team": 1, "status": "a"},
        *hurt]}
    table = wv.build_player_table(bootstrap, seasons, projections,
                                  season_panel=panel).set_index("web_name")
    assert bool(table.loc["HurtMover", "club_moved"]) is True
    assert table.loc["HurtMover", "role_factor"] == 1.0
    assert table.loc["HurtMover", "ros_adj"] == 140.0
    assert table.loc["HurtMover", "availability"] == 0.5          # the gate still applies
    assert table.loc["ClubMover", "role_factor"] == wv.ROLE_FLOOR  # available: unchanged rule


def test_club_mover_with_no_minutes_gets_the_floor(tmp_path):
    """The synthetic check from the spec: proj 175, 0 minutes -> ros_adj 26.2."""
    table = _role_table(tmp_path, _panel({300: [0, 0, 0, 0, 0], 301: [0, 0, 0, 0, 0]}))
    mover = table.loc["ClubMover"]
    assert bool(mover["club_moved"]) is True
    assert mover["expected_minutes"] == 0.0 and mover["minutes_season"] == 0
    assert mover["role_factor"] == wv.ROLE_FLOOR
    assert mover["ros_adj"] == 26.2 and mover["ros_points"] == 175.0   # raw ROS kept
    stayer = table.loc["Stayer"]                                       # benched, not moved
    assert bool(stayer["club_moved"]) is False
    assert stayer["role_factor"] == 1.0 and stayer["ros_adj"] == 100.0


def test_club_mover_playing_sixty_plus_keeps_his_projection(tmp_path):
    table = _role_table(tmp_path, _panel({300: [90, 90, 60, 90, 75]}))
    mover = table.loc["ClubMover"]
    assert mover["role_factor"] == 1.0 and mover["ros_adj"] == 175.0
    assert mover["minutes_season"] == 405


def test_club_moved_is_none_without_a_prior_season_row(tmp_path):
    table = _role_table(tmp_path, _panel({302: [0, 0]}))
    newcomer = table.loc["Newcomer"]             # only a 2024-25 row: no prior club
    assert newcomer["club_moved"] is None
    assert newcomer["role_factor"] == 1.0 and newcomer["ros_adj"] == 90.0


def test_expected_minutes_uses_the_last_five_gameweeks_and_missing_rows_are_zero(tmp_path):
    # ClubMover: no row in GW2 and GW6; 7 gameweeks in the panel -> window GW3-7.
    panel = _panel({300: [90, None, 90, 90, 30, None, 90], 301: [90] * 7})
    table = _role_table(tmp_path, panel)
    mover = table.loc["ClubMover"]
    assert mover["expected_minutes"] == pytest.approx((90 + 90 + 30 + 0 + 90) / 5)
    assert mover["minutes_season"] == 390
    assert table.loc["Newcomer", "expected_minutes"] == 0.0   # no row at all = 0 minutes
    assert table.loc["Newcomer", "minutes_season"] == 0


def test_no_panel_rows_means_unknown_minutes_and_factor_one(tmp_path):
    for season_panel in (None, _panel({})):
        mover = _role_table(tmp_path, season_panel).loc["ClubMover"]
        assert pd.isna(mover["expected_minutes"]) and pd.isna(mover["minutes_season"])
        assert mover["role_factor"] == 1.0 and mover["ros_adj"] == 175.0


def test_heuristic_per_gw_baseline_uses_the_role_adjusted_projection(tmp_path):
    benched = _role_table(tmp_path, _panel({300: [0, 0, 0]})).loc["ClubMover"]
    unknown = _role_table(tmp_path, None).loc["ClubMover"]
    assert benched["xp_source"] == "heuristic"
    assert benched["xp_next"] == pytest.approx(unknown["xp_next"] * wv.ROLE_FLOOR, abs=0.01)
    assert benched["next3_xp"] < unknown["next3_xp"]


def test_departed_player_has_zero_ros_adj_even_without_a_projection(tmp_path):
    gone = [{"id": 33, "code": 303, "web_name": "GoneNoProj", "element_type": 2, "team": 1, "status": "u"},
            {"id": 34, "code": 301, "web_name": "GoneStayer", "element_type": 2, "team": 1, "status": "u"}]
    table = _role_table(tmp_path, None, extra_elements=gone)
    assert table.loc["GoneNoProj", "ros_adj"] == 0.0 and pd.isna(table.loc["GoneNoProj", "ros_points"])
    assert table.loc["GoneNoProj", "xp_source"] == "none"       # still not "valued"
    assert table.loc["GoneStayer", "ros_adj"] == 0.0 and table.loc["GoneStayer", "ros_points"] == 100.0


def test_seasons_table_without_codes_flags_nobody(tmp_path):
    proj_path = tmp_path / "projections.json"
    proj_path.write_text("[]")
    bootstrap = {"teams": TEAMS, "fixtures": {}, "elements": [
        {"id": 1, "code": 300, "web_name": "A", "element_type": 2, "team": 1, "status": "a"}]}
    table = wv.build_player_table(bootstrap, SEASONS, proj_path)   # SEASONS has no code column
    assert table.loc[0, "club_moved"] is None
    # a table mixing coded and code-less rows flags only the coded player
    mixed = pd.concat([SEASONS, ROLE_SEASONS], ignore_index=True)
    assert wv.club_moves(bootstrap, mixed) == {300: True}


# ---------------------------------------------------------------------------
# Drop pick: departed first, then role-adjusted ROS
# ---------------------------------------------------------------------------

def _drop_world(tmp_path, *, departed_status="u", season_panel=None, gw_xp=None,
                departed_projection=160.0, **plan_kwargs):
    """My DEFs: Departed (high ROS), Solid, Weak, plus an unprojected Unknown.
    Free DEFs: FreeA, FreeB and FreeMover (moved clubs, big projection)."""
    projections = [{"code": 400, "projected_points": departed_projection},
                   {"code": 401, "projected_points": 120.0},
                   {"code": 402, "projected_points": 70.0},
                   {"code": 410, "projected_points": 110.0},
                   {"code": 411, "projected_points": 90.0},
                   {"code": 300, "projected_points": 175.0}]
    projections = [p for p in projections if p["projected_points"] is not None]
    proj_path = tmp_path / "projections.json"
    proj_path.write_text(json.dumps(projections))
    bootstrap = {
        "teams": TEAMS, "fixtures": {"1": [{"team_h": 1, "team_a": 2}]},
        "elements": [
            {"id": 40, "code": 400, "web_name": "Departed", "element_type": 2, "team": 1,
             "status": departed_status},
            {"id": 41, "code": 401, "web_name": "Solid", "element_type": 2, "team": 1, "status": "a"},
            {"id": 42, "code": 402, "web_name": "Weak", "element_type": 2, "team": 2, "status": "a"},
            {"id": 43, "code": 403, "web_name": "Unknown", "element_type": 2, "team": 2, "status": "a"},
            {"id": 50, "code": 410, "web_name": "FreeA", "element_type": 2, "team": 1, "status": "a"},
            {"id": 51, "code": 411, "web_name": "FreeB", "element_type": 2, "team": 2, "status": "a"},
            {"id": 52, "code": 300, "web_name": "FreeMover", "element_type": 2, "team": 1, "status": "a"}]}
    status = {"element_status": [
        *({"element": e, "owner": 42} for e in (40, 41, 42, 43)),
        *({"element": e, "owner": None} for e in (50, 51, 52))]}
    return wv.plan(bootstrap, status, ROLE_SEASONS, proj_path, 42, gw_xp=gw_xp,
                   season_panel=season_panel, **plan_kwargs)


@pytest.mark.parametrize("gw_xp", [None, _gw_xp([(410, 5.0), (411, 4.0), (300, 3.0), (402, 1.0)])],
                         ids=["heuristic", "model"])
def test_departed_squad_player_is_the_drop_for_every_rec_at_his_position(tmp_path, gw_xp):
    """Regression: the drop was the lowest raw ROS, so a departed player with a
    big projection was kept and a playing teammate dropped instead."""
    result = _drop_world(tmp_path, gw_xp=gw_xp)
    recs = result["recommendations"]
    assert len(recs) == 3
    assert {r["drop"] for r in recs} == {"Departed"}
    assert all(r["drop_status"] == "u" and r["drop_ros_adj"] == 0.0 for r in recs)
    # dropping a departed player for anyone projected is a season gain, not a "stream"
    free_a = next(r for r in recs if r["add"] == "FreeA")
    assert free_a["season_gain"] == pytest.approx(110.0) and free_a["label"] == "upgrade"
    assert free_a["drop_ros"] == 160.0                       # raw projection still shown


def test_departed_squad_player_without_a_projection_is_still_the_drop(tmp_path):
    result = _drop_world(tmp_path, departed_projection=None)
    assert {r["drop"] for r in result["recommendations"]} == {"Departed"}
    assert "Unknown" not in {c["web_name"] for c in result["drop_candidates"]}


def test_without_a_departed_player_the_drop_is_the_lowest_ros_adj(tmp_path):
    result = _drop_world(tmp_path, departed_status="a")
    assert {r["drop"] for r in result["recommendations"]} == {"Weak"}


def test_drop_candidates_lists_three_per_position_in_drop_order(tmp_path):
    result = _drop_world(tmp_path)
    candidates = [c for c in result["drop_candidates"] if c["position"] == "DEF"]
    assert [c["web_name"] for c in candidates] == ["Departed", "Weak", "Solid"]
    assert candidates[0] == {
        "web_name": "Departed", "position": "DEF", "team": "ARS", "status": "u",
        "ros_points": 160.0, "ros_adj": 0.0, "xp_next": 0.0, "next3_xp": 0.0,
        "club_moved": None, "expected_minutes": None}
    json.loads(jsonutil.dumps_strict(result["drop_candidates"]))     # strict-JSON safe


def test_season_gain_and_rec_fields_use_the_role_adjusted_projection(tmp_path):
    """A club-mover with a 175 projection and no minutes is not a season upgrade."""
    panel = _panel({300: [0, 0, 0], 410: [90, 90, 90]})
    gw_xp = _gw_xp([(410, 5.0), (411, 4.0), (300, 3.0), (402, 1.0)])
    recs = _drop_world(tmp_path, departed_status="a", season_panel=panel,
                       gw_xp=gw_xp)["recommendations"]
    mover = next(r for r in recs if r["add"] == "FreeMover")
    assert mover["club_moved"] is True and mover["add_expected_minutes"] == 0.0
    assert mover["add_minutes_season"] == 0 and mover["add_role_factor"] == wv.ROLE_FLOOR
    assert mover["add_ros"] == 175.0 and mover["add_ros_adj"] == 26.2
    assert mover["season_gain"] == pytest.approx(26.2 - 70.0, abs=0.05)
    assert mover["label"] == "stream"                # was an "upgrade" on raw ROS
    newcomer = next(r for r in recs if r["add"] == "FreeA")     # no prior-season row
    assert newcomer["club_moved"] is None and newcomer["add_expected_minutes"] == 90.0
    assert newcomer["add_role_factor"] == 1.0 and newcomer["add_ros_adj"] == 110.0
    json.loads(jsonutil.dumps_strict(recs))


def test_rec_role_fields_are_plain_json_types_when_every_flag_is_known(tmp_path):
    """Edge: with a prior club for everyone, club_moved is a bool column
    (numpy scalars) — the artifact must still be strict JSON."""
    seasons = pd.concat([ROLE_SEASONS, pd.DataFrame([
        {"season": "2025-26", "code": code, "team_name": "Arsenal", "total_points": 0}
        for code in (400, 401, 402, 403, 410, 411)])], ignore_index=True)
    proj_path = tmp_path / "projections.json"
    proj_path.write_text(json.dumps([{"code": 402, "projected_points": 70.0},
                                     {"code": 410, "projected_points": 110.0}]))
    bootstrap = {"teams": TEAMS, "fixtures": {"1": [{"team_h": 1, "team_a": 2}]}, "elements": [
        {"id": 42, "code": 402, "web_name": "Weak", "element_type": 2, "team": 2, "status": "a"},
        {"id": 50, "code": 410, "web_name": "FreeA", "element_type": 2, "team": 1, "status": "a"}]}
    status = {"element_status": [{"element": 42, "owner": 42}, {"element": 50, "owner": None}]}
    result = wv.plan(bootstrap, status, seasons, proj_path, 42,
                     season_panel=_panel({402: [90], 410: [45]}))
    assert result["players"]["club_moved"].dtype == bool
    rec = result["recommendations"][0]
    assert rec["club_moved"] is False and type(rec["add_minutes_season"]) is int
    assert result["drop_candidates"][0]["club_moved"] is True       # Weak: Arsenal -> Wolves
    json.loads(jsonutil.dumps_strict({k: result[k] for k in ("recommendations", "drop_candidates")}))


def test_heuristic_ranking_no_longer_leads_with_a_benched_club_mover(tmp_path):
    """Regression (the Senesi case): on the heuristic scorer a transferred
    player with a big projection and no minutes was the rank-1 add."""
    blind = _drop_world(tmp_path, departed_status="a")["recommendations"]
    assert blind[0]["add"] == "FreeMover"            # no panel: club-move blind
    panel = _panel({300: [0, 0, 0], 410: [90, 90, 90]})
    seen = _drop_world(tmp_path, departed_status="a", season_panel=panel)["recommendations"]
    assert seen[0]["add"] == "FreeA"
    assert "FreeMover" not in [r["add"] for r in seen]   # 26.2 ROS: no gain on any horizon


# ---------------------------------------------------------------------------
# role_overrides.json
# ---------------------------------------------------------------------------

def _override_table(tmp_path, horizon_xp=None):
    """SeasonStar: model xp 6.5 (p_start 0.9, xp_started 7.0). Streamer: heuristic."""
    gw_xp = _gw_xp([(200, 6.5)]).assign(xp_started=7.0, xp_cameo=1.5, num_fixtures=1)
    return _xp_table(tmp_path, gw_xp, horizon_xp)


def _by_name(table):
    return table.set_index("web_name")


# An explicit lifetime for entries whose test is not about staleness (an
# undated entry with no calendar is stale — see the staleness tests below).
FRESH = {"valid_through_gw": 38}


def _report(**names):
    """An ``apply_role_overrides`` report: every key empty except ``names``."""
    return {key: names.get(key.removeprefix("overrides_"), [])
            for key in wv.OVERRIDE_REPORT_KEYS}


def test_override_replaces_xp_next_with_p_override_times_xp_started(tmp_path):
    table = _override_table(tmp_path)
    assert _by_name(table).loc["SeasonStar", "xp_started"] == 7.0
    overrides = [{"player": "SeasonStar", "team": "ARS", "p_start": 0.4, "fact": "rotation risk", **FRESH}]
    out, report = wv.apply_role_overrides(table, overrides, target_gw=1)
    star = _by_name(out).loc["SeasonStar"]
    assert star["xp_next"] == pytest.approx(0.4 * 7.0)        # cameo term dropped
    assert star["p_start"] == 0.4 and star["role_override_p_start"] == 0.4
    assert star["role_override"] == "rotation risk"
    assert pd.isna(star["xp_floor"]) and pd.isna(star["xp_ceiling"])   # stale band cleared
    assert report == _report(applied=["SeasonStar"])
    # the input table is not mutated, and nobody else moves
    assert _by_name(table).loc["SeasonStar", "xp_next"] == pytest.approx(6.5)
    untouched = _by_name(out).loc["Streamer"]
    assert untouched["role_override"] is None
    assert untouched["xp_next"] == _by_name(table).loc["Streamer", "xp_next"]


@pytest.mark.parametrize("p_override, expected", [(0.0, 0.0), (0.5, 0.5 * 6.5 / 0.8)])
def test_override_without_xp_started_zeroes_or_rescales_the_model_value(
        tmp_path, p_override, expected):
    """A pre-S3 xP file has no xp_started: a ruled-out player must still be
    worth 0 (regression: he kept his model xP and could be recommended), and a
    partial override rescales the model value by p_override / model p_start."""
    table = _override_table(tmp_path).assign(xp_started=float("nan"))
    star = _by_name(table).loc["SeasonStar"]
    assert star["xp_next"] == pytest.approx(6.5)
    table.loc[table["web_name"] == "SeasonStar", "p_start"] = 0.8
    out, report = wv.apply_role_overrides(
        table, [{"player": "SeasonStar", "team": "ARS", "p_start": p_override, **FRESH}],
        target_gw=1)
    assert _by_name(out).loc["SeasonStar", "xp_next"] == pytest.approx(expected, abs=0.006)
    assert _by_name(out).loc["SeasonStar", "p_start"] == p_override
    assert report == _report(applied=["SeasonStar"])


def test_legacy_xp_file_without_xp_started_rescales_the_model_value(tmp_path):
    """Regression through the real loading path: an xP file written before
    xp_started existed must keep the model row's conditional missing, so a
    partial override rescales the model value (6.5 × 0.5 / 0.9) instead of
    silently switching to the heuristic while still labelled "model"."""
    table = _xp_table(tmp_path, _gw_xp([(200, 6.5)]))        # no xp_started column
    star = _by_name(table).loc["SeasonStar"]
    assert star["xp_source"] == "model" and pd.isna(star["xp_started"])
    out, _ = wv.apply_role_overrides(
        table, [{"player": "SeasonStar", "team": "ARS", "p_start": 0.5, **FRESH}],
        target_gw=1)
    assert _by_name(out).loc["SeasonStar", "xp_next"] == pytest.approx(
        6.5 * 0.5 / 0.9, abs=0.006)
    assert _by_name(out).loc["SeasonStar", "xp_source"] == "model"


def test_override_in_a_double_gameweek_uses_the_summed_conditional(tmp_path):
    """The frame's xp_started is already the sum over both fixtures (7.5 + 4.5):
    it is used as is, never multiplied by num_fixtures again."""
    gw_xp = _gw_xp([(200, 10.0)]).assign(xp_started=12.0, xp_cameo=2.5, num_fixtures=2)
    table = _xp_table(tmp_path, gw_xp)
    assert _by_name(table).loc["SeasonStar", "xp_started"] == 12.0
    out, _ = wv.apply_role_overrides(
        table, [{"player": "SeasonStar", "team": "ARS", "p_start": 0.5, **FRESH}], target_gw=1)
    assert _by_name(out).loc["SeasonStar", "xp_next"] == pytest.approx(6.0)


def test_override_on_a_heuristic_player_uses_the_full_availability_heuristic(tmp_path):
    table = _override_table(tmp_path)
    streamer = _by_name(table).loc["Streamer"]
    assert streamer["xp_source"] == "heuristic"
    assert streamer["xp_started"] == pytest.approx(streamer["xp_next"])   # status "a"
    out, _ = wv.apply_role_overrides(
        table, [{"player": "Streamer", "team": "ARS", "p_start": 0.5, **FRESH}], target_gw=1)
    assert _by_name(out).loc["Streamer", "xp_next"] == pytest.approx(
        0.5 * streamer["xp_started"], abs=0.006)


def test_unmatched_override_is_reported_without_raising(tmp_path):
    table = _override_table(tmp_path)
    overrides = [{"player": "Nobody", "team": "ARS", "p_start": 0.1, **FRESH},
                 {"player": "SeasonStar", "team": "WOL", "p_start": 0.1, **FRESH},   # wrong team
                 {"player": "Streamer", "team": "ARS", "p_start": "soon", **FRESH}]  # not a number
    out, report = wv.apply_role_overrides(table, overrides, target_gw=1)
    assert report["overrides_unmatched"] == ["Nobody", "SeasonStar", "Streamer"]
    assert report["overrides_applied"] == []
    assert out["role_override"].isna().all()
    assert out["xp_next"].equals(table["xp_next"])


def test_return_gw_in_the_future_zeroes_xp_next(tmp_path):
    table = _override_table(tmp_path)
    overrides = [{"player": "SeasonStar", "team": "ARS", "p_start": 0.8, "return_gw": 9,
                  "fact": "out until GW9"}]
    out, report = wv.apply_role_overrides(table, overrides, target_gw=6)
    star = _by_name(out).loc["SeasonStar"]
    assert star["xp_next"] == 0.0 and star["p_start"] == 0.0
    assert report["overrides_applied"] == ["SeasonStar"]


@pytest.mark.parametrize("return_gw", [6, 4])
def test_override_expires_once_the_return_gameweek_arrives(tmp_path, return_gw):
    """The Maddison case: ``p_start 0, return_gw 4`` must not zero him in GW6."""
    table = _override_table(tmp_path)
    overrides = [{"player": "SeasonStar", "team": "ARS", "p_start": 0.0, "return_gw": return_gw}]
    out, report = wv.apply_role_overrides(table, overrides, target_gw=6)
    assert _by_name(out).loc["SeasonStar", "xp_next"] == pytest.approx(6.5)
    assert report == _report(expired=["SeasonStar"])


@pytest.mark.parametrize("status, chance", [("i", None), ("u", None), ("s", None), ("d", 0)])
def test_override_never_lifts_the_availability_gate(tmp_path, status, chance):
    """Regression (live GW6): Mateta, status i with a stale p_start 0.60, went
    0.00 -> 1.87; Watkins (status u) 0 -> 0.09. An availability-0 player is
    blocked: xp_next and p_start stay as the gate left them."""
    projections = tmp_path / "projections.json"
    projections.write_text(json.dumps([{"code": 200, "projected_points": 150.0}]))
    bootstrap = {"teams": TEAMS, "fixtures": {"1": [{"team_h": 1, "team_a": 2}]}, "elements": [
        {"id": 20, "code": 200, "web_name": "Crocked", "element_type": 4, "team": 1,
         "status": status, "chance_of_playing_next_round": chance}]}
    gw_xp = _gw_xp([(200, 0.0)]).assign(p_start=0.0, xp_started=3.1, xp_cameo=1.0,
                                         num_fixtures=1)
    table = wv.build_player_table(bootstrap, SEASONS, projections, gw_xp=gw_xp)
    assert table.loc[0, "availability"] == 0.0
    overrides = [{"player": "Crocked", "team": "ARS", "p_start": 0.6, "fact": "contract war", **FRESH}]
    out, report = wv.apply_role_overrides(table, overrides, target_gw=1)
    assert report == _report(blocked=["Crocked"])
    row = out.iloc[0]
    assert row["xp_next"] == 0.0 and row["p_start"] == 0.0
    assert row["role_override"] is None and pd.isna(row["role_override_p_start"])


def test_doubtful_player_with_a_nonzero_chance_is_still_overridden(tmp_path):
    table = _override_table(tmp_path)
    table.loc[table["web_name"] == "SeasonStar", "availability"] = 0.25
    out, report = wv.apply_role_overrides(
        table, [{"player": "SeasonStar", "team": "ARS", "p_start": 0.5, **FRESH}], target_gw=1)
    assert report == _report(applied=["SeasonStar"])
    assert _by_name(out).loc["SeasonStar", "xp_next"] == pytest.approx(3.5)


def test_override_prefers_code_over_name_when_names_collide(tmp_path):
    table = _override_table(tmp_path)
    table.loc[table["web_name"] == "Streamer", "web_name"] = "SeasonStar"   # same name + team
    by_name = [{"player": "SeasonStar", "team": "ARS", "p_start": 0.2, **FRESH}]
    _, report = wv.apply_role_overrides(table, by_name, target_gw=1)
    assert report["overrides_unmatched"] == ["SeasonStar"]                  # ambiguous: skipped
    by_code = [{**by_name[0], "code": 200}]
    out, report = wv.apply_role_overrides(table, by_code, target_gw=1)
    assert report["overrides_applied"] == ["SeasonStar"]
    assert out.loc[out["code"] == 200, "xp_next"].iloc[0] == pytest.approx(0.2 * 7.0)
    assert out.loc[out["code"] == 201, "role_override"].iloc[0] is None


@pytest.mark.parametrize("code", ["200", " 200 ", 200.0])
def test_override_code_given_as_a_string_still_matches(tmp_path, code):
    table = _override_table(tmp_path)
    entry = {"player": "Renamed", "team": "XXX", "code": code, "p_start": 0.2, **FRESH}
    out, report = wv.apply_role_overrides(table, [entry], target_gw=1)
    assert report == _report(applied=["Renamed"])
    assert out.loc[out["code"] == 200, "xp_next"].iloc[0] == pytest.approx(0.2 * 7.0)


def test_override_with_a_non_numeric_code_is_unmatched(tmp_path):
    table = _override_table(tmp_path)
    entry = {"player": "SeasonStar", "team": "ARS", "code": "abc", "p_start": 0.2, **FRESH}
    out, report = wv.apply_role_overrides(table, [entry], target_gw=1)
    assert report == _report(unmatched=["SeasonStar"])
    assert out["role_override"].isna().all()


@pytest.mark.parametrize("code", [200.9, True, float("nan"), [200]])
def test_override_with_a_non_integral_code_is_unmatched_not_truncated(tmp_path, code):
    """Regression: ``int(200.9)`` used to match code 200; override codes now
    go through ``_strict_int`` like never_drop entries (research ones too)."""
    table = _override_table(tmp_path)
    for origin in ({}, {"override_origin": "research"}):
        entry = {"player": "SeasonStar", "team": "ARS", "code": code, "p_start": 0.2,
                 **FRESH, **origin}
        out, report = wv.apply_role_overrides(table, [entry], target_gw=1)
        name = "research:SeasonStar" if origin else "SeasonStar"
        assert report == _report(unmatched=[name])
        assert out["role_override"].isna().all()


def test_fact_only_override_and_unvalued_player_keep_their_xp(tmp_path):
    table = _override_table(tmp_path)
    overrides = [{"player": "SeasonStar", "team": "ARS", "fact": "new manager", **FRESH},
                 {"player": "NoProj", "team": "ARS", "p_start": 0.0, **FRESH}]
    out, report = wv.apply_role_overrides(table, overrides, target_gw=1)
    rows = _by_name(out)
    assert rows.loc["SeasonStar", "xp_next"] == pytest.approx(6.5)
    assert rows.loc["SeasonStar", "role_override"] == "new manager"
    assert pd.isna(rows.loc["NoProj", "xp_next"])            # no value to scale
    assert rows.loc["NoProj", "role_override_p_start"] == 0.0
    assert report["overrides_applied"] == ["SeasonStar", "NoProj"]


# Staleness: live role_overrides.json written around GW2 (``updated``
# 2026-08-27, no per-entry dates) kept changing GW6 xP every week.
DEADLINES = {gw: pd.Timestamp(day, tz="UTC").to_pydatetime() for gw, day in
             {2: "2026-08-28 17:30", 3: "2026-09-04 17:30", 4: "2026-09-12 12:30",
              5: "2026-09-18 17:30", 6: "2026-10-10 10:00"}.items()}


def test_event_deadlines_reads_the_draft_bootstrap_shape():
    bootstrap = {"events": {"current": 5, "next": 6, "data": [
        {"id": 5, "deadline_time": "2026-09-18T17:30:00Z"},
        {"id": 6, "deadline_time": "2026-10-10T10:00:00Z"},
        {"id": 7, "deadline_time": None}]}}
    assert wv.event_deadlines(bootstrap) == {5: DEADLINES[5], 6: DEADLINES[6]}
    assert wv.event_deadlines({}) == {}


@pytest.mark.parametrize("as_of, window_end", [
    ("2026-08-27", 2),                    # written before the GW2 deadline -> GW2 only
    ("2026-08-28", 2),                    # date only = 00:00 UTC: the deadline-day fact counts
    ("2026-08-28T18:00:00Z", 3),          # after the GW2 deadline -> describes GW3
    ("2026-10-11", None),                 # no later deadline in the calendar
    ("not a date", None), (None, None),
])
def test_override_window_end_is_the_first_deadline_after_as_of(as_of, window_end):
    assert wv.override_window_end(as_of, DEADLINES) == window_end


def test_undated_entry_written_before_an_earlier_deadline_is_stale(tmp_path):
    """Regression: an undated GW2 entry must not touch GW6 (the live file)."""
    table = _override_table(tmp_path)
    overrides = [{"player": "SeasonStar", "team": "ARS", "p_start": 0.6, "as_of": "2026-08-27"}]
    out, report = wv.apply_role_overrides(table, overrides, target_gw=6, deadlines=DEADLINES)
    assert report == _report(stale=["SeasonStar"])
    star = _by_name(out).loc["SeasonStar"]
    assert star["xp_next"] == pytest.approx(6.5) and star["role_override"] is None


def test_fresh_undated_entry_applies_to_the_next_deadline_only(tmp_path):
    table = _override_table(tmp_path)
    overrides = [{"player": "SeasonStar", "team": "ARS", "p_start": 0.4,
                  "as_of": "2026-10-06T09:00:00Z"}]
    out, report = wv.apply_role_overrides(table, overrides, target_gw=6, deadlines=DEADLINES)
    assert report == _report(applied=["SeasonStar"])
    assert _by_name(out).loc["SeasonStar", "xp_next"] == pytest.approx(0.4 * 7.0)
    _, report = wv.apply_role_overrides(table, overrides, target_gw=7, deadlines=DEADLINES)
    assert report == _report(stale=["SeasonStar"])
    # no calendar or no as_of: the entry cannot be shown to be current
    _, report = wv.apply_role_overrides(table, overrides, target_gw=6)
    assert report == _report(stale=["SeasonStar"])


def test_valid_through_gw_is_honoured_whatever_the_as_of(tmp_path):
    table = _override_table(tmp_path)
    entry = {"player": "SeasonStar", "team": "ARS", "p_start": 0.4, "as_of": "2026-08-27",
             "valid_through_gw": 8}
    for target_gw, expected in ((6, _report(applied=["SeasonStar"])),
                                (8, _report(applied=["SeasonStar"])),
                                (9, _report(stale=["SeasonStar"]))):
        _, report = wv.apply_role_overrides(table, [entry], target_gw, deadlines=DEADLINES)
        assert report == expected, target_gw
    _, report = wv.apply_role_overrides(table, [{**entry, "valid_through_gw": "soon"}], 6)
    assert report == _report(unmatched=["SeasonStar"])


def test_return_gw_entries_are_not_aged_by_their_as_of(tmp_path):
    """A dated absence keeps its own lifetime: out until return_gw, then expired."""
    table = _override_table(tmp_path)
    entry = {"player": "SeasonStar", "team": "ARS", "p_start": 0.0, "return_gw": 9,
             "as_of": "2026-08-27"}
    _, report = wv.apply_role_overrides(table, [entry], target_gw=6, deadlines=DEADLINES)
    assert report == _report(applied=["SeasonStar"])


def test_load_role_overrides_dates_entries_from_the_file(tmp_path):
    path = tmp_path / "role_overrides.json"
    path.write_text(json.dumps({"updated": "2026-08-27", "overrides": [
        {"player": "A", "team": "ARS"}, {"player": "B", "team": "ARS", "as_of": "2026-10-05"}]}))
    assert [e["as_of"] for e in wv.load_role_overrides(path)] == ["2026-08-27", "2026-10-05"]
    path.write_text(json.dumps({"as_of": "2026-09-01", "updated": "2026-08-27",
                                "overrides": [{"player": "A", "team": "ARS"}]}))
    assert wv.load_role_overrides(path)[0]["as_of"] == "2026-09-01"     # as_of beats updated
    path.write_text(json.dumps({"overrides": [{"player": "A", "team": "ARS"}]}))
    os.utime(path, (1790000000, 1790000000))                             # 2026-09-21 UTC
    as_of = wv.load_role_overrides(path)[0]["as_of"]
    assert wv._parse_as_of(as_of) == pd.Timestamp(1790000000, unit="s", tz="UTC")
    assert wv.override_window_end(as_of, DEADLINES) == 6


def test_load_role_overrides_tolerates_missing_and_malformed_files(tmp_path, caplog):
    assert wv.load_role_overrides(tmp_path / "absent.json") == []
    good = tmp_path / "role_overrides.json"
    good.write_text(json.dumps({"updated": "x", "overrides": [{"player": "A", "team": "ARS"}, "junk"]}))
    assert wv.load_role_overrides(good) == [{"player": "A", "team": "ARS", "as_of": "x"}]
    for content in ("{not json", "[1, 2]", '{"overrides": {"player": "A"}}'):
        bad = tmp_path / "bad.json"
        bad.write_text(content)
        with caplog.at_level("WARNING"):
            assert wv.load_role_overrides(bad) == []
    assert "ignored" in caplog.text


def test_cli_prints_the_three_sections_and_writes_role_fields(weekly_cli_root, weekly_cli_argv,
                                                              tmp_path, capsys):
    ml_dir = weekly_cli_root / "derived/2026-27/ml"
    ml_dir.mkdir(parents=True, exist_ok=True)
    (ml_dir / "role_overrides.json").write_text(json.dumps({"overrides": [
        {"player": "FreeFWD", "team": "ARS", "p_start": 0.0, "return_gw": 9, "fact": "out"},
        {"player": "Ghost", "team": "ARS", "p_start": 0.5},
        {"player": "MyFWD", "team": "WOL", "p_start": 0.0, "return_gw": 3},
        {"player": "FreeFWD", "team": "ARS", "p_start": 0.9, "as_of": "2026-08-27"}]}))
    pd.DataFrame([{"season": "2026-27", "code": 101, "gw": gw, "minutes": 90}
                  for gw in (4, 5)]).to_parquet(ml_dir / "player_gameweeks.parquet")
    out = tmp_path / "waiver_plan.json"
    assert wv.main(weekly_cli_argv(out, "heuristic")) == 0
    doc = json.loads(out.read_text())
    assert doc["overrides_applied"] == ["FreeFWD"]
    assert doc["overrides_unmatched"] == ["Ghost"] and doc["overrides_expired"] == ["MyFWD"]
    assert doc["overrides_stale"] == ["FreeFWD"] and doc["overrides_blocked"] == []
    assert doc["minutes_through_gw"] == 5
    assert [c["web_name"] for c in doc["drop_candidates"]] == ["MyFWD"]
    assert doc["drop_candidates"][0]["expected_minutes"] == 90.0
    printed = capsys.readouterr().out
    assert printed.index("overrides_applied: FreeFWD") < printed.index("== DROP CANDIDATES") \
        < printed.index("== WAIVER RECOMMENDATIONS")


def test_cli_runs_without_a_season_panel_or_overrides_file(weekly_cli_root, weekly_cli_argv,
                                                           tmp_path, caplog):
    out = tmp_path / "waiver_plan.json"
    with caplog.at_level("WARNING"):
        assert wv.main(weekly_cli_argv(out, "heuristic")) == 0
    doc = json.loads(out.read_text())
    assert doc["minutes_through_gw"] is None and doc["overrides_applied"] == []
    assert doc["drop_candidates"][0]["expected_minutes"] is None
    assert "club-move role signals off" in caplog.text


def _ruled_out_world(tmp_path, status, chance=None, horizon_xp=None):
    """Streamer (ros 60 < MyWeakFWD's 80) valued by a cached model row at 6.5
    xP, now with ``status``/``chance`` in the CURRENT bootstrap."""
    projections = [{"code": 101, "projected_points": 80.0},
                   {"code": 201, "projected_points": 60.0}]
    proj_path = tmp_path / "projections.json"
    proj_path.write_text(json.dumps(projections))
    bootstrap = {
        "teams": TEAMS, "fixtures": {"1": [{"team_h": 1, "team_a": 2}]},
        "elements": [
            {"id": 11, "code": 101, "web_name": "MyWeakFWD", "element_type": 4, "team": 2,
             "status": "a"},
            {"id": 21, "code": 201, "web_name": "Streamer", "element_type": 4, "team": 1,
             "status": status, "chance_of_playing_next_round": chance}]}
    status_rows = {"element_status": [{"element": 11, "owner": 42},
                                      {"element": 21, "owner": None}]}
    return wv.plan(bootstrap, status_rows, SEASONS, proj_path, 42,
                   gw_xp=_gw_xp([(101, 1.0), (201, 6.5)]), horizon_xp=horizon_xp)


def test_model_xp_of_a_player_now_ruled_out_is_zeroed_and_never_recommended(tmp_path):
    """Regression: a cached xP file built before an injury kept a positive
    xp_next and recommended the injured player as a stream."""
    result = _ruled_out_world(tmp_path, "i")
    hurt = result["players"].set_index("web_name").loc["Streamer"]
    assert hurt["xp_source"] == "model" and hurt["xp_reconciled"]
    assert hurt["xp_next"] == 0.0 and hurt["p_start"] == 0.0
    assert result["recommendations"] == []
    assert result["xp_reconciled"] == 1
    mine = result["players"].set_index("web_name").loc["MyWeakFWD"]
    assert not mine["xp_reconciled"] and mine["xp_next"] == pytest.approx(1.0)


@pytest.mark.parametrize("status,chance,reconciled", [
    ("d", 0, True),       # doubtful with a stated 0% chance is ruled out
    ("s", None, True),    # suspended, no chance stated
    ("d", 50, False),     # partial doubt: model value kept as served
    ("a", None, False),
])
def test_reconciliation_only_applies_at_zero_availability(tmp_path, status, chance, reconciled):
    result = _ruled_out_world(tmp_path, status, chance)
    row = result["players"].set_index("web_name").loc["Streamer"]
    assert bool(row["xp_reconciled"]) is reconciled
    assert bool(row["xp_next"] == 0.0) is reconciled
    assert bool(result["recommendations"]) is not reconciled


# --- horizons: gw1 / gw3 / ros -------------------------------------------------

def _horizon_xp(per_code, gw=1, *, started=None, fitted=True):
    """A ``matchmodel.build_horizon_xp``-shaped frame: ``per_code`` maps a code
    to its xP in events gw, gw+1, ... (0.0 = a blank). ``started`` maps a code
    to the matching ``xp_started`` values (default: the xP itself)."""
    rows = []
    for code, values in per_code.items():
        for offset, xp in enumerate(values):
            rows.append({
                "code": code, "season": "2026-27", "gw": gw, "event": gw + offset,
                "num_fixtures": int(xp > 0), "opponents": "vWOL" if xp > 0 else "",
                "p_start": 0.9 if xp > 0 else float("nan"), "xp": float(xp),
                "xp_started": float((started or {}).get(code, values)[offset]),
                "fitted": fitted, "xp_h1": float(values[0]), "xp_h3": float(sum(values)),
                "events_covered": len(values), "panel_max_gw": gw - 1})
    return pd.DataFrame(rows)


def test_covered_player_takes_the_model_horizon_for_next3(tmp_path):
    horizon = _horizon_xp({200: [6.5, 0.0, 5.25], 999: [3.0, 3.0, 3.0]})
    table = _by_name(_xp_table(tmp_path, _gw_xp([(200, 6.5)]), horizon))
    assert table.loc["SeasonStar", "next3_source"] == "model"
    assert table.loc["SeasonStar", "next3_xp"] == pytest.approx(11.75)   # 6.5 + blank + 5.25
    assert table.loc["NoProj", "next3_source"] == "model"                 # no projection needed
    assert table.loc["NoProj", "next3_xp"] == pytest.approx(9.0)
    streamer = table.loc["Streamer"]                                      # not in the frame
    assert streamer["next3_source"] == "heuristic" and streamer["next3_xp"] > 0


def test_without_a_horizon_frame_next3_is_the_heuristic_as_before(tmp_path):
    with_model = _by_name(_xp_table(tmp_path, _gw_xp([(200, 6.5)])))
    heuristic = _by_name(_xp_table(tmp_path, None))
    assert (with_model["next3_xp"].dropna() == heuristic["next3_xp"].dropna()).all()
    assert with_model.loc["SeasonStar", "next3_source"] == "heuristic"
    assert with_model.loc["NoProj", "next3_source"] == "none"
    assert pd.isna(with_model.loc["NoProj", "next3_xp"])


def test_unfitted_horizon_rows_are_not_served_as_model_values(tmp_path):
    """Edge: a position the model could not fit has xp 0.0 in the horizon file
    ("no opinion"); the heuristic must value those players, not that zero."""
    horizon = _horizon_xp({200: [0.0, 0.0, 0.0]}, fitted=False)
    table = _by_name(_xp_table(tmp_path, _gw_xp([(201, 5.0)]), horizon))
    assert table.loc["SeasonStar", "next3_source"] == "heuristic"
    assert table.loc["SeasonStar", "next3_xp"] > 0
    assert wv.usable_horizon_xp(horizon).empty and wv.usable_horizon_xp(None).empty


def test_rank_key_maps_the_horizon_and_falls_back_safely():
    gw_xp, horizon = _gw_xp([(200, 6.5)]), _horizon_xp({200: [6.5, 6.0, 5.0]})
    assert wv.rank_key("1", gw_xp, horizon) == "next1"
    assert wv.rank_key("3", gw_xp, horizon) == "next3"
    assert wv.rank_key("ros", gw_xp, horizon) == "ros"
    assert wv.rank_key("3", gw_xp, None) == "next1"          # no horizon file: next-GW ranking
    assert wv.rank_key("ros", gw_xp, None) == "ros"
    for horizon_arg in wv.HORIZONS:                           # heuristic scorer: frozen baseline
        assert wv.rank_key(horizon_arg, None, None) == "legacy"
    with pytest.raises(ValueError, match="unknown horizon"):
        wv.rank_key("5", gw_xp, horizon)
    assert wv.DEFAULT_HORIZON == "3"


def _three_way_world(tmp_path):
    """SeasonStar: best next GW and all season, but two blanks ahead.
    Streamer: three kind fixtures, a worse season. Drop = MyWeakFWD."""
    players, status = _players_fixture(tmp_path)
    for name, xp_next, next3 in (("MyWeakFWD", 2.0, 6.0), ("SeasonStar", 2.5, 2.5),
                                 ("Streamer", 2.2, 7.5)):
        players.loc[players["web_name"] == name, ["xp_next", "next3_xp"]] = [xp_next, next3]
    players["next3_source"] = "model"
    return players, wv.my_squad(players, status, entry_id=42)


def test_each_horizon_ranks_and_labels_on_its_own_gain(tmp_path):
    players, squad = _three_way_world(tmp_path)

    def ranked(rank_by):
        return [(r["add"], r["label"]) for r in wv.recommend(players, squad, rank_by=rank_by)]

    assert ranked("next1") == [("SeasonStar", "upgrade"), ("Streamer", "stream")]
    assert ranked("next3") == [("Streamer", "stream"), ("SeasonStar", "hold")]
    assert ranked("ros") == [("SeasonStar", "hold"), ("Streamer", "stream")]   # holds not demoted
    with pytest.raises(ValueError, match="unknown rank_by"):
        wv.recommend(players, squad, rank_by="next5")


def test_every_rec_carries_gains_for_all_three_horizons(tmp_path):
    players, squad = _three_way_world(tmp_path)
    for rank_by in ("legacy", "next1", "next3", "ros"):
        for rec in wv.recommend(players, squad, rank_by=rank_by):
            assert rec["gains"] == {"gw1": rec["next1_gain"], "gw3": rec["next3_gain"],
                                    "ros": rec["season_gain"]}, rank_by
            assert rec["add_next3_source"] == "model"
    star = next(r for r in wv.recommend(players, squad, rank_by="next3")
                if r["add"] == "SeasonStar")
    assert star["gains"] == {"gw1": 0.5, "gw3": -3.5, "ros": 70.0}
    json.loads(jsonutil.dumps_strict(wv.recommend(players, squad, rank_by="next3")))


def test_drop_order_follows_the_ranking_horizon_with_ros_as_tiebreak():
    squad = pd.DataFrame([
        {"web_name": "LowRos", "status": "a", "ros_points": 50.0, "ros_adj": 50.0,
         "xp_next": 3.0, "next3_xp": 9.0},
        {"web_name": "LowNext3", "status": "a", "ros_points": 150.0, "ros_adj": 150.0,
         "xp_next": 4.0, "next3_xp": 2.0},
        {"web_name": "LowNext1", "status": "a", "ros_points": 120.0, "ros_adj": 120.0,
         "xp_next": 1.0, "next3_xp": 6.0},
        {"web_name": "TieLowerRos", "status": "a", "ros_points": 90.0, "ros_adj": 90.0,
         "xp_next": 1.0, "next3_xp": 7.0}])

    def first(rank_by):
        return list(wv.drop_order(squad, rank_by)["web_name"])

    assert first("legacy")[0] == "LowRos"
    assert first("ros")[0] == "LowRos"
    assert first("next3")[0] == "LowNext3"
    assert first("next1")[:2] == ["TieLowerRos", "LowNext1"]     # equal xp_next: lower ROS first
    with pytest.raises(ValueError, match="unknown rank_by"):
        wv.drop_order(squad, "next5")


def test_drop_order_puts_players_the_horizon_cannot_value_last():
    """Edge: a ROS-only player (no 3-GW value) is droppable, after the valued."""
    squad = pd.DataFrame([
        {"web_name": "NoNext3", "status": "a", "ros_points": 10.0, "ros_adj": 10.0,
         "xp_next": None, "next3_xp": None},
        {"web_name": "Valued", "status": "a", "ros_points": 200.0, "ros_adj": 200.0,
         "xp_next": 6.0, "next3_xp": 18.0}])
    assert list(wv.drop_order(squad, "next3")["web_name"]) == ["Valued", "NoNext3"]


# ---------------------------------------------------------------------------
# Drop pick on the model's value; diversified top N; best_by_position
# ---------------------------------------------------------------------------

# Unknown (code 403) has no ROS projection; the model values him at 0 xP.
_UNKNOWN_AT_ZERO = [(410, 5.0), (411, 4.0), (300, 3.0), (402, 1.0), (403, 0.0)]


def test_unprojected_zero_xp_squad_player_is_now_the_drop(tmp_path):
    """Regression (Madjo): a squad player with no ROS but a 0 model xP was never
    offered as a drop; the drop was the lowest-ROS teammate instead."""
    result = _drop_world(tmp_path, departed_status="a", gw_xp=_gw_xp(_UNKNOWN_AT_ZERO))
    assert result["rank_by"] == "next1"
    recs = result["recommendations"]
    assert recs and {r["drop"] for r in recs} == {"Unknown"}
    free_a = next(r for r in recs if r["add"] == "FreeA")
    assert free_a["next1_gain"] == pytest.approx(5.0)
    # his season value is unknown, so the season gain is too — never "add - 0"
    assert free_a["drop_ros_unknown"] is True and free_a["season_unknown"] is True
    assert free_a["add_ros_unknown"] is False      # FreeA is projected: the drop is the gap
    assert free_a["season_gain"] is None and free_a["label"] == "stream"
    assert free_a["drop_xp_source"] == "model"
    defs = [c["web_name"] for c in result["drop_candidates"] if c["position"] == "DEF"]
    assert defs[0] == "Unknown"
    json.loads(jsonutil.dumps_strict(recs))


def test_next3_gain_is_null_when_the_drop_has_no_three_gw_value(tmp_path):
    """Regression: a drop with model next-GW xP but neither ROS nor a model
    horizon has next3_xp null; next3_gain was "the add minus 0" (the add's
    whole 3-GW value shown as improvement). It is unknown."""
    result = _drop_world(tmp_path, departed_status="a", gw_xp=_gw_xp(_UNKNOWN_AT_ZERO))
    free_a = next(r for r in result["recommendations"] if r["add"] == "FreeA")
    assert free_a["drop"] == "Unknown" and pd.isna(free_a["drop_next3_xp"])
    assert free_a["add_next3_xp"] is not None
    assert free_a["next3_gain"] is None and free_a["gains"]["gw3"] is None
    assert free_a["label"] == "stream"          # read from next1_gain, unaffected
    json.loads(jsonutil.dumps_strict(result["recommendations"]))


def test_departed_drop_without_a_three_gw_value_counts_as_zero(tmp_path):
    """Edge: a departed drop's value really is 0, so his gain stays numeric."""
    result = _drop_world(tmp_path, departed_projection=None,
                         gw_xp=_gw_xp([(410, 5.0), (411, 4.0), (300, 3.0), (402, 1.0)]))
    free_a = next(r for r in result["recommendations"] if r["add"] == "FreeA")
    assert free_a["drop"] == "Departed" and free_a["drop_status"] == "u"
    assert free_a["next3_gain"] == pytest.approx(free_a["add_next3_xp"])


def test_drop_on_the_three_gw_value_under_the_next3_ranking(tmp_path):
    horizon = _horizon_xp({410: [5.0, 5.0, 5.0], 411: [4.0, 4.0, 4.0], 300: [3.0, 3.0, 3.0],
                           402: [1.0, 1.0, 1.0], 401: [0.5, 0.0, 0.0], 403: [2.0, 2.0, 2.0]})
    gw_xp = _gw_xp([*_UNKNOWN_AT_ZERO[:-1], (401, 0.5), (403, 2.0)])
    result = _drop_world(tmp_path, departed_status="a", gw_xp=gw_xp, horizon_xp=horizon)
    assert result["rank_by"] == "next3"
    # Solid has the best ROS (120) but the worst 3 GWs (0.5)
    assert {r["drop"] for r in result["recommendations"]} == {"Solid"}


def test_squad_player_with_no_model_value_and_no_ros_is_never_the_drop(tmp_path):
    """Edge: no model row and no projection = unknown, not zero — unchanged."""
    result = _drop_world(tmp_path, departed_status="a",
                         gw_xp=_gw_xp([(410, 5.0), (411, 4.0), (300, 3.0), (402, 1.0)]))
    assert {r["drop"] for r in result["recommendations"]} == {"Weak"}
    assert "Unknown" not in {c["web_name"] for c in result["drop_candidates"]}
    assert [p["web_name"] for p in result["unprojected_squad"]] == ["Unknown"]


def test_blank_gw_player_with_a_model_horizon_is_a_drop_not_unprojected(tmp_path):
    """Regression: a no-ROS player blanking next GW has xp_source "none" but a
    model 3-GW value; under next3 he was the drop AND listed as "no value,
    never auto-dropped". He is now only the drop."""
    horizon = _horizon_xp({410: [5.0, 5.0, 5.0], 411: [4.0, 4.0, 4.0], 300: [3.0, 3.0, 3.0],
                           401: [3.0, 3.0, 3.0], 402: [1.0, 1.0, 1.0], 403: [0.0, 0.2, 0.2]})
    gw_xp = _gw_xp([(410, 5.0), (411, 4.0), (300, 3.0), (401, 3.0), (402, 1.0)])
    result = _drop_world(tmp_path, departed_status="a", gw_xp=gw_xp, horizon_xp=horizon)
    unknown = result["squad"].set_index("web_name").loc["Unknown"]
    assert unknown["xp_source"] == "none" and unknown["next3_source"] == "model"
    assert result["rank_by"] == "next3"
    assert {r["drop"] for r in result["recommendations"]} == {"Unknown"}
    assert result["unprojected_squad"] == []


def test_unprojected_squad_is_the_complement_of_the_drop_pool():
    """Edge: under legacy only a ROS projection values a player; under the
    model rankings any model value does. Every non-departed squad player is
    either in drop_order or in unprojected_squad, never both."""
    squad = pd.DataFrame([
        {"web_name": "Horizon", "position": "DEF", "team": "AAA", "status": "a", "news": "",
         "ros_points": None, "ros_adj": None, "xp_next": None, "next3_xp": 0.4},
        {"web_name": "Nothing", "position": "DEF", "team": "AAA", "status": "a", "news": "",
         "ros_points": None, "ros_adj": None, "xp_next": None, "next3_xp": None},
        {"web_name": "Projected", "position": "DEF", "team": "AAA", "status": "a", "news": "",
         "ros_points": 80.0, "ros_adj": 80.0, "xp_next": 2.0, "next3_xp": 6.0}])
    for rank_by in ("next1", "next3", "ros", "legacy"):
        dropped = set(wv.drop_order(squad, rank_by)["web_name"])
        listed = {p["web_name"] for p in wv.unprojected_squad(squad, rank_by)}
        assert not dropped & listed and dropped | listed == set(squad["web_name"]), rank_by
    assert {p["web_name"] for p in wv.unprojected_squad(squad, "next3")} == {"Nothing"}
    assert {p["web_name"] for p in wv.unprojected_squad(squad, "legacy")} == {"Horizon", "Nothing"}
    with pytest.raises(ValueError, match="unknown rank_by"):
        wv.unprojected_squad(squad, "next5")


def test_departed_player_is_still_dropped_before_a_zero_xp_teammate(tmp_path):
    result = _drop_world(tmp_path, gw_xp=_gw_xp(_UNKNOWN_AT_ZERO))
    assert {r["drop"] for r in result["recommendations"]} == {"Departed"}


def test_heuristic_scorer_never_drops_a_player_without_a_projection(tmp_path):
    """The heuristic pick is frozen: a model-valued, unprojected player (only
    possible in a hand-built table) stays out of the legacy drop order."""
    squad = pd.DataFrame([
        {"web_name": "ModelOnly", "status": "a", "ros_points": None, "ros_adj": None,
         "xp_next": 0.0, "next3_xp": 0.0},
        {"web_name": "Projected", "status": "a", "ros_points": 80.0, "ros_adj": 80.0,
         "xp_next": 2.0, "next3_xp": 6.0}])
    assert list(wv.drop_order(squad, "legacy")["web_name"]) == ["Projected"]
    assert list(wv.drop_order(squad, "next1")["web_name"]) == ["ModelOnly", "Projected"]


def _rec(add, drop, position="MID"):
    return {"add": add, "drop_element": drop, "position": position}


def test_diversify_caps_each_drop_and_backfills_in_rank_order():
    ranked = [_rec("m1", 1), _rec("m2", 1), _rec("m3", 1), _rec("m4", 1), _rec("m5", 1),
              _rec("d1", 2, "DEF"), _rec("f1", 3, "FWD")]
    out = [r["add"] for r in wv.diversify(ranked, top_n=5, per_drop=3)]
    assert out == ["m1", "m2", "m3", "d1", "f1"]                  # rank 1 kept, m4/m5 held
    # too few other drops: held recs backfill after the capped picks
    assert [r["add"] for r in wv.diversify(ranked, top_n=6, per_drop=3)] == [
        "m1", "m2", "m3", "d1", "f1", "m4"]
    assert wv.diversify([], top_n=10) == []


def test_recommend_diversifies_the_model_ranking_but_not_the_heuristic(monkeypatch):
    """Regression: every top-10 rec replaced one MID. The model rankings now
    cap a drop at MAX_RECS_PER_DROP; the heuristic (legacy) stays a plain top N."""
    ranked = [*(_rec(f"m{i}", 1) for i in range(5)), _rec("d1", 2, "DEF")]
    monkeypatch.setattr(wv, "ranked_recs", lambda players, squad, rank_by: ranked)
    assert [r["add"] for r in wv.recommend(None, None, 4, rank_by="legacy")] == [
        "m0", "m1", "m2", "m3"]
    for rank_by in ("next1", "next3", "ros"):
        assert [r["add"] for r in wv.recommend(None, None, 4, rank_by=rank_by)] == [
            "m0", "m1", "m2", "d1"]


def test_best_by_position_covers_every_position_with_candidates(tmp_path):
    result = _drop_world(tmp_path, departed_status="a", gw_xp=_gw_xp(_UNKNOWN_AT_ZERO))
    best = result["best_by_position"]
    assert list(best) == ["GKP", "DEF", "MID", "FWD"]
    assert best["GKP"] == best["MID"] == best["FWD"] == []        # no candidates there
    assert [r["add"] for r in best["DEF"]] == ["FreeA", "FreeB", "FreeMover"]
    assert all(r["drop"] == "Unknown" for r in best["DEF"])
    json.loads(jsonutil.dumps_strict(best))


def test_best_by_position_keeps_the_top_few_per_position():
    ranked = [_rec("m1", 1), _rec("m2", 1), _rec("d1", 2, "DEF"), _rec("m3", 1),
              _rec("m4", 1), _rec("f1", 3, "FWD")]
    best = wv.best_by_position(ranked, per_position=2)
    assert {pos: [r["add"] for r in recs] for pos, recs in best.items()} == {
        "GKP": [], "DEF": ["d1"], "MID": ["m1", "m2"], "FWD": ["f1"]}


def test_model_only_add_has_a_gw3_gain_once_the_horizon_values_him(tmp_path):
    """The promoted-club starter: no ROS projection, but the horizon knows his
    three fixtures — gw3 is a number, ros stays unknown, and he is a stream."""
    horizon = _horizon_xp({999: [6.0, 5.0, 4.0]})
    table, squad = _model_only_world(tmp_path, [(999, 6.0), (201, 5.0)], horizon)
    noproj = next(r for r in wv.recommend(table, squad, rank_by="next3") if r["add"] == "NoProj")
    assert noproj["add_next3_xp"] == pytest.approx(15.0) and noproj["add_next3_source"] == "model"
    assert noproj["next3_gain"] == pytest.approx(15.0 - noproj["drop_next3_xp"], abs=0.01)
    assert noproj["season_unknown"] is True and noproj["gains"]["ros"] is None
    assert noproj["label"] == "stream"


def test_free_agent_blanking_next_gw_is_ranked_by_the_horizon_only(tmp_path):
    """Edge: no xp_gw row (his club blanks in GW N) and no projection — the
    3-GW ranking still sees his next two fixtures; the next-GW one does not."""
    horizon = _horizon_xp({999: [0.0, 5.0, 5.0]})
    table, squad = _model_only_world(tmp_path, [(201, 5.0)], horizon)
    assert _by_name(table).loc["NoProj", "xp_source"] == "none"
    by_three = [r["add"] for r in wv.recommend(table, squad, rank_by="next3")]
    assert "NoProj" in by_three
    assert all(r["add"] != "NoProj" for r in wv.recommend(table, squad, rank_by="next1"))


def test_plan_ranks_on_next1_without_a_horizon_frame_and_next3_with_one(tmp_path):
    """FreeA has the best next GW, FreeB the best three (FreeA blanks twice)."""
    gw_xp = _gw_xp([(410, 5.0), (411, 4.0), (300, 3.0), (402, 1.0)])
    horizon = _horizon_xp({410: [5.0, 0.0, 0.0], 411: [4.0, 4.0, 4.0], 300: [3.0, 3.0, 3.0],
                           402: [1.0, 1.0, 1.0], 401: [2.0, 2.0, 2.0]})

    def plan(**kwargs):
        result = _drop_world(tmp_path, departed_status="a", **kwargs)
        return result["rank_by"], result["recommendations"][0]["add"]

    assert plan(gw_xp=gw_xp) == ("next1", "FreeA")                     # as before the horizon
    assert plan(gw_xp=gw_xp, horizon_xp=horizon) == ("next3", "FreeB")
    assert plan(gw_xp=gw_xp, horizon_xp=horizon, horizon="1") == ("next1", "FreeA")
    # heuristic scorer: the horizon frame is ignored, values and ranking alike
    heuristic = _drop_world(tmp_path, departed_status="a", horizon_xp=horizon)
    assert heuristic["rank_by"] == "legacy"
    assert set(heuristic["players"]["next3_source"]) <= {"heuristic", "none"}
    assert heuristic["recommendations"] == _drop_world(
        tmp_path, departed_status="a")["recommendations"]


# --- role overrides over the horizon ----------------------------------------------

OVERRIDE_HORIZON = {"per_code": {200: [6.5, 6.0, 5.0]}, "started": {200: [7.0, 6.6, 5.6]}}


def _horizon_override(tmp_path, entry):
    """SeasonStar's next3_xp after ``entry`` is applied for GW6 (horizon 6-8)."""
    horizon = _horizon_xp(OVERRIDE_HORIZON["per_code"], gw=6, started=OVERRIDE_HORIZON["started"])
    table = _override_table(tmp_path, horizon)
    assert _by_name(table).loc["SeasonStar", "next3_xp"] == pytest.approx(17.5)
    out, report = wv.apply_role_overrides(
        table, [{"player": "SeasonStar", "team": "ARS", **entry}], target_gw=6,
        deadlines=DEADLINES, horizon_xp=horizon)
    return _by_name(out).loc["SeasonStar"], report


def test_undated_override_changes_only_the_next_gameweek_of_the_horizon(tmp_path):
    star, report = _horizon_override(tmp_path, {"p_start": 0.4, "as_of": "2026-10-06T09:00:00Z"})
    assert report == _report(applied=["SeasonStar"])
    assert star["xp_next"] == pytest.approx(0.4 * 7.0)
    assert star["next3_xp"] == pytest.approx(0.4 * 7.0 + 6.0 + 5.0)     # GW7, GW8 untouched


def test_valid_through_gw_extends_the_override_across_the_horizon(tmp_path):
    star, _ = _horizon_override(tmp_path, {"p_start": 0.4, "valid_through_gw": 7})
    assert star["next3_xp"] == pytest.approx(0.4 * 7.0 + 0.4 * 6.6 + 5.0)


@pytest.mark.parametrize("return_gw, expected", [(8, 5.0), (7, 6.0 + 5.0), (9, 0.0)])
def test_return_gw_zeroes_the_horizon_events_before_the_return(tmp_path, return_gw, expected):
    star, _ = _horizon_override(tmp_path, {"p_start": 0.0, "return_gw": return_gw})
    assert star["xp_next"] == 0.0
    assert star["next3_xp"] == pytest.approx(expected)


def test_override_that_does_not_apply_leaves_the_horizon_alone(tmp_path):
    star, report = _horizon_override(tmp_path, {"p_start": 0.4, "as_of": "2026-08-27"})
    assert report == _report(stale=["SeasonStar"]) and star["next3_xp"] == pytest.approx(17.5)
    star, _ = _horizon_override(tmp_path, {"fact": "new manager", "valid_through_gw": 8})
    assert star["next3_xp"] == pytest.approx(17.5)            # fact-only: nothing to re-weight


def test_override_leaves_a_heuristic_next3_untouched(tmp_path):
    """Streamer has no horizon rows: his 3-GW value has no per-event parts."""
    horizon = _horizon_xp(OVERRIDE_HORIZON["per_code"], gw=6)
    table = _override_table(tmp_path, horizon)
    before = _by_name(table).loc["Streamer", "next3_xp"]
    out, report = wv.apply_role_overrides(
        table, [{"player": "Streamer", "team": "ARS", "p_start": 0.0, **FRESH}], target_gw=6,
        deadlines=DEADLINES, horizon_xp=horizon)
    assert report == _report(applied=["Streamer"])
    assert _by_name(out).loc["Streamer", "xp_next"] == 0.0
    assert _by_name(out).loc["Streamer", "next3_xp"] == before


def test_blocked_override_does_not_touch_the_horizon(tmp_path):
    horizon = _horizon_xp({200: [0.0, 0.0, 0.0]}, gw=6, started={200: [7.0, 6.6, 5.6]})
    table = _override_table(tmp_path, horizon)
    table.loc[table["web_name"] == "SeasonStar", "availability"] = 0.0
    out, report = wv.apply_role_overrides(
        table, [{"player": "SeasonStar", "team": "ARS", "p_start": 0.9, **FRESH}], target_gw=6,
        deadlines=DEADLINES, horizon_xp=horizon)
    assert report == _report(blocked=["SeasonStar"])
    assert _by_name(out).loc["SeasonStar", "next3_xp"] == 0.0


# --- the horizon file: freshness and fallback ----------------------------------------

def _weekly_bootstrap(root):
    return json.loads((root / "raw/2026-27/bootstrap/bootstrap-static.json").read_text())


def test_resolve_horizon_reads_a_fresh_file_and_reports_its_events(weekly_cli_root):
    ml_dir = weekly_cli_root / "derived/2026-27/ml"
    gw_xp = _gw_xp([(102, 6.0), (101, 1.0)], gw=6)
    _horizon_xp({102: [6.0, 5.0, 4.0], 101: [1.0, 1.0, 1.0]}, gw=6).to_parquet(
        ml_dir / "xp_horizon_gw6.parquet")
    frame, meta = wv.resolve_horizon("3", gw_xp, _weekly_bootstrap(weekly_cli_root), ml_dir)
    assert frame is not None and len(frame) == 6
    assert meta == {"horizon_requested": "3", "horizon_fallback": False,
                    "horizon_fallback_reason": None, "horizon_events": [6, 7, 8]}


def test_resolve_horizon_is_not_read_for_the_heuristic_scorer(weekly_cli_root):
    ml_dir = weekly_cli_root / "derived/2026-27/ml"
    (ml_dir / "xp_horizon_gw6.parquet").write_bytes(b"not a parquet file")
    frame, meta = wv.resolve_horizon("3", None, _weekly_bootstrap(weekly_cli_root), ml_dir)
    assert frame is None and meta["horizon_fallback"] is False
    assert meta["horizon_requested"] == "3" and meta["horizon_events"] is None


@pytest.mark.parametrize("build, reason", [
    (None, "xp_horizon_gw6.parquet missing"),
    (lambda: _horizon_xp({102: [6.0, 5.0, 4.0]}, gw=5), "is for gw [5], expected 6"),
    (lambda: _horizon_xp({102: [6.0, 5.0, 4.0]}, gw=6).assign(panel_max_gw=4),
     "trained on a panel through gw [4]"),
    (lambda: _horizon_xp({102: [5.5, 5.0, 4.0]}, gw=6), "disagrees with xp_gw6"),
    (lambda: _horizon_xp({102: [6.0, 5.0, 4.0]}, gw=6).drop(columns=["fitted", "xp_h3"]),
     "lacks ['fitted', 'xp_h3']"),
])
def test_resolve_horizon_falls_back_with_a_reason(weekly_cli_root, caplog, build, reason):
    ml_dir = weekly_cli_root / "derived/2026-27/ml"
    if build is not None:
        build().to_parquet(ml_dir / "xp_horizon_gw6.parquet")
    with caplog.at_level("WARNING"):
        frame, meta = wv.resolve_horizon("3", _gw_xp([(102, 6.0)], gw=6),
                                         _weekly_bootstrap(weekly_cli_root), ml_dir)
    assert frame is None and meta["horizon_fallback"] is True
    assert reason in meta["horizon_fallback_reason"]
    assert "horizon xP file" in meta["horizon_fallback_reason"]
    assert "3-GW values fall back to the heuristic" in caplog.text


def _write_weekly_xp(root):
    """xp_gw6 + xp_horizon_gw6 for the CLI world: FreeFWD has the better next
    GW, Promoted (no projection) the better three."""
    ml_dir = root / "derived/2026-27/ml"
    _gw_xp([(101, 1.0), (102, 6.0), (103, 5.0)], gw=6).to_parquet(ml_dir / "xp_gw6.parquet")
    _horizon_xp({101: [1.0, 1.0, 1.0], 102: [6.0, 0.0, 0.0], 103: [5.0, 5.0, 5.0]},
                gw=6).to_parquet(ml_dir / "xp_horizon_gw6.parquet")


def test_cli_ranks_on_the_requested_horizon_and_writes_all_three_gains(
        weekly_cli_root, weekly_cli_argv, tmp_path, capsys):
    _write_weekly_xp(weekly_cli_root)
    out = tmp_path / "waiver_plan.json"
    assert wv.main(weekly_cli_argv(out, "model")) == 0                 # default --horizon 3
    doc = json.loads(out.read_text())
    assert doc["rank_by"] == "next3" and doc["horizon_requested"] == "3"
    assert doc["horizon_fallback"] is False and doc["horizon_events"] == [6, 7, 8]
    assert [r["add"] for r in doc["recommendations"]] == ["Promoted", "FreeFWD"]
    for rec in doc["recommendations"]:
        assert set(rec["gains"]) == {"gw1", "gw3", "ros"}
        assert rec["gains"]["gw1"] == rec["next1_gain"] and rec["gains"]["gw3"] == rec["next3_gain"]
        assert rec["gains"]["ros"] == rec["season_gain"] and rec["add_next3_source"] == "model"
    promoted = doc["recommendations"][0]
    assert promoted["gains"] == {"gw1": 4.0, "gw3": 12.0, "ros": None}
    assert "ranked by next3" in capsys.readouterr().out

    assert wv.main([*weekly_cli_argv(out, "model"), "--horizon", "1"]) == 0
    doc = json.loads(out.read_text())
    assert doc["rank_by"] == "next1" and doc["horizon_requested"] == "1"
    assert [r["add"] for r in doc["recommendations"]] == ["FreeFWD", "Promoted"]
    assert doc["recommendations"][1]["gains"]["gw3"] == 12.0           # still reported

    assert wv.main([*weekly_cli_argv(out, "model"), "--horizon", "ros"]) == 0
    assert json.loads(out.read_text())["rank_by"] == "ros"


def test_cli_without_the_horizon_file_keeps_the_next_gw_ranking(
        weekly_cli_root, weekly_cli_argv, tmp_path, capsys):
    """The horizon build failing must not cost the weekly run its model
    ranking: gw1 stays model-valued, gw3 says it is heuristic, and why."""
    _write_weekly_xp(weekly_cli_root)
    (weekly_cli_root / "derived/2026-27/ml/xp_horizon_gw6.parquet").unlink()
    out = tmp_path / "waiver_plan.json"
    assert wv.main(weekly_cli_argv(out, "model")) == 0
    doc = json.loads(out.read_text())
    assert doc["scorer"] == "model" and doc["rank_by"] == "next1"
    assert doc["horizon_fallback"] is True and doc["horizon_events"] is None
    assert "xp_horizon_gw6.parquet missing" in doc["horizon_fallback_reason"]
    recs = {r["add"]: r for r in doc["recommendations"]}
    assert recs["FreeFWD"]["add_next3_source"] == "heuristic"
    assert recs["Promoted"]["gains"]["gw3"] is None and recs["Promoted"]["add_next3_source"] == "none"
    assert "HORIZON FALLBACK" in capsys.readouterr().out


def test_cli_heuristic_scorer_ignores_the_horizon(weekly_cli_root, weekly_cli_argv, tmp_path):
    """Regression: the heuristic ranking is the frozen replay baseline — no
    --horizon value and no model file may move it."""
    _write_weekly_xp(weekly_cli_root)
    docs = []
    for horizon in wv.HORIZONS:
        out = tmp_path / f"waiver_plan_{horizon}.json"
        assert wv.main([*weekly_cli_argv(out, "heuristic"), "--horizon", horizon]) == 0
        docs.append(json.loads(out.read_text()))
    assert all(doc["rank_by"] == "legacy" and doc["horizon_fallback"] is False for doc in docs)
    assert all(doc["recommendations"] == docs[0]["recommendations"] for doc in docs)
    assert {r["add_next3_source"] for r in docs[0]["recommendations"]} == {"heuristic"}
    assert all(isinstance(r["next3_gain"], float) and round(r["next3_gain"], 1) == r["next3_gain"]
               for r in docs[0]["recommendations"])


def test_model_horizon_of_a_player_now_ruled_out_is_zeroed(tmp_path):
    """The 3-GW horizon file has the same stale Stage-1 gate as xp_gw: a
    model-based next3_xp is reconciled to 0 too, and the 3-GW ranking never
    recommends him."""
    horizon = _horizon_xp({101: [1.0, 1.0, 1.0], 201: [6.5, 6.0, 6.0]})
    result = _ruled_out_world(tmp_path, "i", horizon_xp=horizon)
    hurt = _by_name(result["players"]).loc["Streamer"]
    assert hurt["next3_source"] == "model" and hurt["next3_xp"] == 0.0
    assert hurt["xp_reconciled"] and result["xp_reconciled"] == 1
    assert result["recommendations"] == []
    fit = _by_name(_ruled_out_world(tmp_path, "a", horizon_xp=horizon)["players"])
    assert fit.loc["Streamer", "next3_xp"] == pytest.approx(18.5)


def test_player_table_carries_p_appear_and_an_override_sets_it(tmp_path):
    gw_xp = _gw_xp([(200, 6.5)]).assign(xp_started=7.0, xp_cameo=1.5, num_fixtures=1,
                                         p_appear=0.95)
    table = _xp_table(tmp_path, gw_xp)
    assert _by_name(table).loc["SeasonStar", "p_appear"] == 0.95
    assert pd.isna(_by_name(table).loc["Streamer", "p_appear"])     # heuristic row
    overrides = [{"player": "SeasonStar", "team": "ARS", "p_start": 0.4, **FRESH}]
    out, _ = wv.apply_role_overrides(table, overrides, target_gw=1)
    assert _by_name(out).loc["SeasonStar", "p_appear"] == 0.4


def test_player_table_p_appear_is_nan_for_an_xp_file_without_it(tmp_path):
    table = _override_table(tmp_path)
    assert pd.isna(_by_name(table).loc["SeasonStar", "p_appear"])


# ---------------------------------------------------------------------------
# role_overrides.research.json (research agent) — lower precedence, tagged
# ---------------------------------------------------------------------------

def _research(**entry):
    """A research entry as ``load_research_overrides`` returns it."""
    return {"player": "SeasonStar", "team": "ARS", "fact": "research: fit again [high]",
            "override_origin": "research", **entry}


def test_manual_override_beats_research_for_the_same_player(tmp_path):
    table = _override_table(tmp_path)
    manual = {"player": "SeasonStar", "team": "ARS", "p_start": 0.4, "fact": "rotation", **FRESH}
    out, report = wv.apply_role_overrides(table, [manual, _research(p_start=0.9, **FRESH)],
                                          target_gw=1)
    star = _by_name(out).loc["SeasonStar"]
    assert star["p_start"] == 0.4 and star["role_override"] == "rotation"
    assert report == _report(applied=["SeasonStar"], superseded=["research:SeasonStar"])


def test_research_override_applies_tagged_when_no_manual_entry(tmp_path):
    table = _override_table(tmp_path)
    out, report = wv.apply_role_overrides(table, [_research(p_start=0.9, **FRESH)], target_gw=1)
    star = _by_name(out).loc["SeasonStar"]
    assert star["p_start"] == 0.9 and star["xp_next"] == pytest.approx(0.9 * 7.0)
    assert star["role_override"].startswith("research:")
    assert report == _report(applied=["research:SeasonStar"])


def test_expired_or_stale_research_is_ignored(tmp_path):
    table = _override_table(tmp_path)
    entries = [_research(p_start=0.0, return_gw=1), _research(p_start=0.2, valid_through_gw=0)]
    out, report = wv.apply_role_overrides(table, entries, target_gw=1)
    assert _by_name(out).loc["SeasonStar", "role_override"] is None
    assert report == _report(expired=["research:SeasonStar"], stale=["research:SeasonStar"])


def test_stale_manual_entry_does_not_shadow_fresh_research(tmp_path):
    table = _override_table(tmp_path)
    stale_manual = {"player": "SeasonStar", "team": "ARS", "p_start": 0.1, "valid_through_gw": 0}
    out, report = wv.apply_role_overrides(table, [stale_manual, _research(p_start=0.8, **FRESH)],
                                          target_gw=1)
    assert _by_name(out).loc["SeasonStar", "p_start"] == 0.8
    assert report == _report(stale=["SeasonStar"], applied=["research:SeasonStar"])


def test_load_all_role_overrides_reads_manual_then_tagged_research(tmp_path):
    (tmp_path / wv.MANUAL_OVERRIDES_FILE).write_text(json.dumps(
        {"updated": "2026-10-01", "overrides": [{"player": "A", "team": "ARS", "fact": "hand"}]}))
    (tmp_path / wv.RESEARCH_OVERRIDES_FILE).write_text(json.dumps(
        {"updated": "2026-10-02", "overrides": [{"player": "B", "team": "ARS", "fact": "knock"},
                                                {"player": "C", "team": "ARS", "fact": "research: x"}]}))
    entries = wv.load_all_role_overrides(tmp_path)
    assert [e["player"] for e in entries] == ["A", "B", "C"]
    assert "override_origin" not in entries[0] and entries[0]["fact"] == "hand"
    assert [e["fact"] for e in entries[1:]] == ["research: knock", "research: x"]
    assert all(e["override_origin"] == "research" for e in entries[1:])
    assert wv.load_all_role_overrides(tmp_path / "absent") == []


def test_override_lifetime_and_matching_helpers_never_raise():
    assert wv.override_lifetime({"valid_through_gw": "x"}, 1, None) == "invalid"
    assert wv.override_lifetime({"valid_through_gw": 3}, 2, None) == "live"
    assert wv.override_matches({"code": "200"}, 200, "Other", "WOL")
    assert not wv.override_matches({"code": "abc"}, 200, "SeasonStar", "ARS")
    assert wv.override_matches({"player": "SeasonStar", "team": "ARS"}, None, "SeasonStar", "ARS")
    assert wv.override_matches({"code": 200.0}, 200, "Other", "WOL")
    assert not wv.override_matches({"code": 200.9}, 200, "SeasonStar", "ARS")
    assert not wv.override_matches({"code": True}, 1, "SeasonStar", "ARS")
    assert not wv.override_matches({"player": ["SeasonStar"], "team": "ARS"}, None,
                                   "SeasonStar", "ARS")
    assert wv.override_code({"code": " 200 "}) == 200 and wv.override_code({}) is None


def test_cli_tags_research_overrides_in_waiver_plan(weekly_cli_root, weekly_cli_argv, tmp_path):
    ml_dir = weekly_cli_root / "derived/2026-27/ml"
    (ml_dir / wv.RESEARCH_OVERRIDES_FILE).write_text(json.dumps({"overrides": [
        {"player": "FreeFWD", "team": "ARS", "code": 102, "p_start": 0.7, "valid_through_gw": 6,
         "fact": "research: back in training [med]", "source": "research"}]}))
    out = tmp_path / "waiver_plan.json"
    assert wv.main(weekly_cli_argv(out, "heuristic")) == 0
    doc = json.loads(out.read_text())
    assert doc["overrides_applied"] == ["research:FreeFWD"] and doc["overrides_superseded"] == []
    # never_drop reporting sits beside the research override keys.
    assert all(doc[key] == [] for key in wv.NEVER_DROP_REPORT_KEYS)
    rec = next(r for r in doc["recommendations"] if r["add"] == "FreeFWD")
    assert rec["add_role_override"] == "research: back in training [med]"
