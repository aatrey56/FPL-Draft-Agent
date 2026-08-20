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
