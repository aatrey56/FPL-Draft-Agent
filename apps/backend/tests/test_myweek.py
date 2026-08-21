"""Tests for my_week (backend.ml.myweek). No network — fixtures only."""

import json

import pandas as pd

from backend.ml import myweek as mw

TEAMS = [{"id": 1, "name": "Arsenal", "short_name": "ARS"},
         {"id": 2, "name": "Wolves", "short_name": "WOL"}]

SEASONS = pd.DataFrame([
    {"season": "2025-26", "team_name": "Arsenal", "total_points": 2000},
    {"season": "2025-26", "team_name": "Wolves", "total_points": 1200},
])


def _world(tmp_path, elements, projections):
    bootstrap = {
        "teams": TEAMS,
        "fixtures": {"4": [{"team_h": 1, "team_a": 2}], "5": [], "6": []},
        "elements": elements,
    }
    proj = tmp_path / "p.json"
    proj.write_text(json.dumps(projections))
    return bootstrap, mw.gw_xp_table(bootstrap, SEASONS, proj)


def test_next_event_is_smallest_fixture_key():
    bootstrap = {"fixtures": {"4": [], "5": [], "6": []}}
    assert mw.next_event(bootstrap) == 4
    assert mw.next_event({"fixtures": {}}) is None
    assert mw.next_event({}) is None


def test_gw_xp_scores_single_event_and_availability(tmp_path):
    elements = [
        {"id": 1, "code": 10, "web_name": "Fit", "element_type": 3, "team": 1, "status": "a"},
        {"id": 2, "code": 11, "web_name": "Out", "element_type": 3, "team": 1, "status": "i",
         "chance_of_playing_next_round": 0},
    ]
    _, players = _world(tmp_path, elements, [
        {"code": 10, "projected_points": 152.0}, {"code": 11, "projected_points": 152.0}])
    fit = players[players["web_name"] == "Fit"].iloc[0]
    out = players[players["web_name"] == "Out"].iloc[0]
    assert fit["gw_xp"] > 0
    assert out["gw_xp"] == 0.0                       # injured: gated this week
    assert out["ros_points"] == 152.0                # season value intact


def test_build_my_week_xi_bench_and_attention(tmp_path):
    elements = []
    projections = []
    code = 100
    # 2 GKP, 5 DEF, 5 MID, 3 FWD = a full 15 with a clear quality gradient
    for pos_type, count in ((1, 2), (2, 5), (3, 5), (4, 3)):
        for i in range(count):
            eid = code
            elements.append({"id": eid, "code": code,
                             "web_name": f"P{code}", "element_type": pos_type,
                             "team": 1, "status": "a"})
            projections.append({"code": code, "projected_points": 190.0 - code % 100})
            code += 1
    # One mystery man: no projection row at all
    projections = [p for p in projections if p["code"] != 114]

    bootstrap, players = _world(tmp_path, elements, projections)
    status = {"element_status": [{"element": e["id"], "owner": 42} for e in elements]}
    week = mw.build_my_week(players, status, entry_id=42)

    assert len(week["xi"]) == 11 and len(week["bench"]) == 4
    assert week["xi_gw_xp"] > 0
    assert [p["web_name"] for p in week["unprojected_squad"]] == ["P114"]
    flagged = {p["web_name"] for p in week["attention"]}
    assert "P114" in flagged                          # unprojected needs a human call
    xi_names = {p["web_name"] for p in week["xi"]}
    assert "P114" not in xi_names                     # unknown never auto-started


def test_blank_gameweek_flags_players(tmp_path):
    elements = [{"id": 1, "code": 10, "web_name": "Blanked", "element_type": 3,
                 "team": 2, "status": "a"}]
    bootstrap = {
        "teams": TEAMS,
        "fixtures": {"4": [{"team_h": 1, "team_a": 1}]},  # no fixture for team 2
        "elements": elements,
    }
    proj = tmp_path / "p.json"
    proj.write_text(json.dumps([{"code": 10, "projected_points": 100.0}]))
    players = mw.gw_xp_table(bootstrap, SEASONS, proj)
    row = players.iloc[0]
    assert row["gw_xp"] == 0.0
    assert "blank gameweek: no fixture" in mw.player_warnings(row)
