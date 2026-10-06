"""Tests for waiver_plan (backend.ml.waiver). No network — fixtures only."""

import json

import pandas as pd
import pytest

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


def _xp_table(tmp_path, gw_xp):
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
    return wv.build_player_table(bootstrap, SEASONS, proj_path, gw_xp=gw_xp)


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


def _model_only_world(tmp_path, xp_rows):
    """MyWeakFWD (projected, heuristic) in my squad; NoProj and Streamer free."""
    table = _xp_table(tmp_path, _gw_xp(xp_rows))
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
    table.loc[table["web_name"] == "Streamer", "ros_points"] = 200.0
    recs = wv.recommend(table, squad)
    assert [r["add"] for r in recs][:2] == ["Streamer", "NoProj"]
    assert recs[1]["season_gain"] is None and recs[0]["season_gain"] > 0


def test_season_unknown_rec_survives_the_json_round_trip(tmp_path):
    table, squad = _model_only_world(tmp_path, [(999, 6.0)])
    doc = json.loads(wv.jsonutil.dumps_strict(wv.recommend(table, squad)))   # the CLI's writer
    noproj = next(r for r in doc if r["add"] == "NoProj")
    assert noproj["season_gain"] is None and noproj["season_unknown"] is True


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
    while its gw still matched. panel_max_gw must be N-1."""
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
