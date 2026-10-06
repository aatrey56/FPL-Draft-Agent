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


def _events(current, nxt, current_finished, current_deadline):
    return {"current": current, "next": nxt, "data": [
        {"id": current, "finished": current_finished, "deadline_time": current_deadline},
        {"id": nxt, "finished": False, "deadline_time": "2099-01-01T00:00:00Z"}]}


def test_next_event_follows_the_event_calendar():
    between = {"events": _events(5, 6, True, "2026-09-18T17:30:00Z"),
               "fixtures": {"6": [], "7": []}}
    assert mw.next_event(between) == 6
    assert mw.next_event({"fixtures": {"4": []}}) is None   # no calendar
    assert mw.next_event({}) is None


def test_mid_gameweek_plans_the_next_gw_not_the_one_in_play(tmp_path):
    """Review regression: mid-GW the fixture map still holds the in-play GW,
    so its smallest key (6) disagreed with matchmodel.next_gameweek (7).
    my_week's gw, its fixture load and waiver's xP file must all say GW7."""
    bootstrap = {
        "teams": TEAMS,
        "events": _events(6, 7, False, "2026-10-03T10:00:00Z"),   # GW6 in play
        "fixtures": {"6": [{"team_h": 1, "team_a": 2}],             # GW6: both play
                     "7": [{"team_h": 1, "team_a": 3}]},            # GW7: Wolves blank
        "elements": [
            {"id": 1, "code": 10, "web_name": "Gunner", "element_type": 3, "team": 1, "status": "a"},
            {"id": 2, "code": 11, "web_name": "Wolf", "element_type": 3, "team": 2, "status": "a"}]}
    proj = tmp_path / "p.json"
    proj.write_text(json.dumps([{"code": 10, "projected_points": 76.0},
                                {"code": 11, "projected_points": 76.0}]))
    assert mw.next_event(bootstrap) == 7
    players = mw.gw_xp_table(bootstrap, SEASONS, proj).set_index("web_name")
    assert players.loc["Wolf", "gw_fixture_load"] == 0 and players.loc["Wolf", "gw_xp"] == 0
    assert players.loc["Gunner", "gw_fixture_load"] > 0

    # GW5 is the last finished GW, so the freshest panel (and the xP file's
    # panel_max_gw) ends there while GW6 is in play.
    bootstrap["events"]["data"].insert(
        0, {"id": 5, "finished": True, "deadline_time": "2026-09-26T10:00:00Z"})
    _model_frame([(10, 5.0)], gw=7).assign(panel_max_gw=5).to_parquet(
        tmp_path / "xp_gw7.parquet")
    frame, meta = mw.wv.resolve_scorer("model", bootstrap, tmp_path)
    assert frame is not None and meta["scorer"] == "model"


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


def test_xi_selection_value_ordering():
    """projected+fit > unprojected+fit > unavailable, regardless of gw_xp."""
    projected_fit = pd.Series({"availability": 1.0, "gw_xp": 2.5})
    unknown_fit = pd.Series({"availability": 1.0, "gw_xp": float("nan")})
    injured_projected = pd.Series({"availability": 0.0, "gw_xp": 0.0})
    injured_unknown = pd.Series({"availability": 0.0, "gw_xp": float("nan")})

    assert mw.xi_selection_value(projected_fit) == 2.5
    assert mw.xi_selection_value(unknown_fit) == 0.0
    assert mw.xi_selection_value(injured_projected) == -1.0
    assert mw.xi_selection_value(injured_unknown) == -1.0


def test_injured_players_never_start_over_fit_ones(tmp_path):
    """The Madjo/Maddison GW1 regression: with fit alternatives on the bench,
    zero-chance players must never occupy an XI slot — and a fit unprojected
    player must outrank an injured projected one."""
    elements, projections = [], []
    code = 100
    # 2 GKP, 4 DEF (all fit/projected — only nine fit projected outfielders in
    # total, so the tenth XI slot MUST come from the unknown or the injured),
    # then crafted MID and FWD rooms.
    for pos_type, count in ((1, 2), (2, 4)):
        for _ in range(count):
            elements.append({"id": code, "code": code, "web_name": f"P{code}",
                             "element_type": pos_type, "team": 1, "status": "a"})
            projections.append({"code": code, "projected_points": 150.0 - code % 100})
            code += 1
    # MIDs: three projected fit, one projected but OUT, one unprojected fit.
    for name, status, projected in (
            ("MidA", "a", True), ("MidB", "a", True), ("MidC", "a", True),
            ("MidInjured", "i", True), ("MidMystery", "a", False)):
        el = {"id": code, "code": code, "web_name": name, "element_type": 3,
              "team": 1, "status": status}
        if status == "i":
            el["chance_of_playing_next_round"] = 0
        elements.append(el)
        if projected:
            projections.append({"code": code, "projected_points": 120.0})
        code += 1
    # FWDs: two projected fit, one unprojected AND out (the Madjo case).
    for name, status, projected in (
            ("FwdA", "a", True), ("FwdB", "a", True), ("FwdMadjo", "i", False)):
        el = {"id": code, "code": code, "web_name": name, "element_type": 4,
              "team": 1, "status": status}
        if status == "i":
            el["chance_of_playing_next_round"] = 0
        elements.append(el)
        if projected:
            projections.append({"code": code, "projected_points": 110.0})
        code += 1

    bootstrap, players = _world(tmp_path, elements, projections)
    status = {"element_status": [{"element": e["id"], "owner": 42} for e in elements]}
    week = mw.build_my_week(players, status, entry_id=42)

    xi_names = {p["web_name"] for p in week["xi"]}
    assert "FwdMadjo" not in xi_names          # zero-chance never starts
    assert "MidInjured" not in xi_names        # even projected, out is out
    assert "MidMystery" in xi_names            # fit unknown beats injured anybody
    assert len(week["xi"]) == 11
    assert week["xi_gw_xp"] > 0                # total is real xP, no -1 leakage


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


# ---- match xP wiring --------------------------------------------------------

def _model_frame(rows, gw=4):
    return pd.DataFrame([{
        "code": code, "xp": xp, "p_start": 0.9, "xp_floor": 0.0, "xp_ceiling": xp + 3,
        "drivers": "d", "opponents": "vWOL", "gw": gw, "panel_max_gw": gw - 1}
        for code, xp in rows])


def _xp_world(tmp_path, gw_xp):
    bootstrap = {
        "teams": TEAMS, "fixtures": {"4": [{"team_h": 1, "team_a": 2}]},
        "elements": [
            {"id": 1, "code": 10, "web_name": "Covered", "element_type": 3, "team": 1, "status": "a"},
            {"id": 2, "code": 11, "web_name": "Heur", "element_type": 3, "team": 1, "status": "a"},
            {"id": 3, "code": 12, "web_name": "Mystery", "element_type": 3, "team": 1, "status": "a"}]}
    proj = tmp_path / "p.json"
    proj.write_text(json.dumps([{"code": 10, "projected_points": 76.0},
                                {"code": 11, "projected_points": 76.0}]))
    return mw.gw_xp_table(bootstrap, SEASONS, proj, gw_xp=gw_xp)


def test_model_xp_used_when_covered_else_heuristic_with_source(tmp_path):
    players = _xp_world(tmp_path, _model_frame([(10, 7.0)]))
    by_name = players.set_index("web_name")
    assert by_name.loc["Covered", "gw_xp"] == 7.0 and by_name.loc["Covered", "xp_source"] == "model"
    assert by_name.loc["Heur", "xp_source"] == "heuristic" and by_name.loc["Heur", "gw_xp"] > 0
    assert by_name.loc["Mystery", "xp_source"] == "none" and pd.isna(by_name.loc["Mystery", "gw_xp"])


def test_unprojected_squad_lists_only_xp_source_none_and_warnings_differ(tmp_path):
    players = _xp_world(tmp_path, _model_frame([(10, 7.0)]))
    status = {"element_status": [{"element": e, "owner": 42} for e in (1, 2, 3)]}
    week = mw.build_my_week(players, status, 42)
    assert [p["web_name"] for p in week["unprojected_squad"]] == ["Mystery"]
    warnings = {p["web_name"]: p["warnings"] for p in week["attention"]}
    assert warnings["Heur"] == ["no model xP — heuristic"]
    assert warnings["Mystery"] == ["no value — judge manually (player_card)"]
    assert all("xp_source" in row for row in week["xi"] + week["bench"])


def test_heuristic_scorer_does_not_warn_on_every_player(tmp_path):
    players = _xp_world(tmp_path, None)
    status = {"element_status": [{"element": e, "owner": 42} for e in (1, 2)]}
    assert mw.build_my_week(players, status, 42)["attention"] == []


def test_cli_writes_the_scorer_actually_used(weekly_cli_root, weekly_cli_argv, tmp_path):
    """Review regression: --scorer model with a stale xP file wrote scorer "model"."""
    xp_path = weekly_cli_root / "derived/2026-27/ml/xp_gw6.parquet"
    _model_frame([(101, 3.0)], gw=5).to_parquet(xp_path)          # built for GW5
    out = tmp_path / "out" / "my_week.json"
    assert mw.main(weekly_cli_argv(out, "model")) == 0
    doc = json.loads(out.read_text())
    assert doc["gw"] == 6
    assert doc["scorer"] == "heuristic" and doc["scorer_requested"] == "model"
    assert doc["xp_fallback"] is True and "stale" in doc["xp_fallback_reason"]

    assert mw.main(weekly_cli_argv(out, "heuristic")) == 0
    doc = json.loads(out.read_text())
    assert doc["scorer"] == "heuristic" and doc["xp_fallback"] is False


def test_cli_falls_back_on_an_unreadable_xp_file(weekly_cli_root, weekly_cli_argv, tmp_path):
    """Review regression: a corrupt xP parquet crashed my_week."""
    (weekly_cli_root / "derived/2026-27/ml/xp_gw6.parquet").write_bytes(b"\x00garbage")
    out = tmp_path / "out" / "my_week.json"
    assert mw.main(weekly_cli_argv(out, "model")) == 0
    doc = json.loads(out.read_text())
    assert doc["scorer"] == "heuristic" and doc["xp_fallback"] is True
    assert "unreadable" in doc["xp_fallback_reason"]


def test_warning_codes_are_stable_and_aligned_with_text(tmp_path):
    """The TUI matches on warning_codes; each code sits at its text's index."""
    players = _xp_world(tmp_path, _model_frame([(10, 7.0)]))
    players.loc[players["web_name"] == "Mystery", ["status", "news"]] = ["d", "Knock"]
    status = {"element_status": [{"element": e, "owner": 42} for e in (1, 2, 3)]}
    week = mw.build_my_week(players, status, 42)
    att = {p["web_name"]: p for p in week["attention"]}
    assert att["Mystery"]["warning_codes"] == [mw.WARNING_NO_VALUE, mw.WARNING_AVAILABILITY]
    assert att["Mystery"]["warnings"][1] == "availability [d] — Knock"
    assert att["Heur"]["warning_codes"] == [mw.WARNING_HEURISTIC_XP]
    for row in week["xi"] + week["bench"]:
        assert len(row["warning_codes"]) == len(row["warnings"])
