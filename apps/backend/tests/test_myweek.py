"""Tests for my_week (backend.ml.myweek). No network — fixtures only."""

import json
from pathlib import Path

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


SHARED_WARNING_CODES = Path(__file__).resolve().parents[3] / "testdata/my_week_warning_codes.json"


def test_warning_codes_match_the_fixture_shared_with_go():
    """The Go TUI and tool note read the same file, so a renamed or added
    code fails both suites instead of drifting silently."""
    shared = json.loads(SHARED_WARNING_CODES.read_text())["warning_codes"]
    assert [mw.WARNING_DEPARTED, mw.WARNING_NO_VALUE, mw.WARNING_ROLE_OVERRIDE,
            mw.WARNING_BLANK_GW, mw.WARNING_AVAILABILITY, mw.WARNING_HEURISTIC_XP] == shared
    declared = {value for name, value in vars(mw).items() if name.startswith("WARNING_")}
    assert declared == set(shared)


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


def test_actionable_warnings_come_before_the_scoring_source_note(tmp_path):
    """Review regression: for a projected player in a blank gameweek the
    heuristic_xp note came first, and the TUI rail shows only the first
    warning — so "no fixture" was hidden behind "heuristic xP"."""
    elements = [
        {"id": 1, "code": 10, "web_name": "Covered", "element_type": 3, "team": 1, "status": "a"},
        {"id": 2, "code": 11, "web_name": "Blanked", "element_type": 3, "team": 2, "status": "a"},
        {"id": 3, "code": 12, "web_name": "BlankedHurt", "element_type": 3, "team": 2,
         "status": "i", "news": "Knee"}]
    bootstrap = {"teams": TEAMS, "fixtures": {"4": [{"team_h": 1, "team_a": 1}]},  # team 2 blanks
                 "elements": elements}
    proj = tmp_path / "p.json"
    proj.write_text(json.dumps([{"code": code, "projected_points": 76.0} for code in (10, 11, 12)]))
    players = mw.gw_xp_table(bootstrap, SEASONS, proj, gw_xp=_model_frame([(10, 7.0)]))
    status = {"element_status": [{"element": e, "owner": 42} for e in (1, 2, 3)]}
    att = {p["web_name"]: p for p in mw.build_my_week(players, status, 42)["attention"]}
    assert "Covered" not in att
    assert att["Blanked"]["warning_codes"] == [mw.WARNING_BLANK_GW, mw.WARNING_HEURISTIC_XP]
    assert att["Blanked"]["warnings"][0] == "blank gameweek: no fixture"
    assert att["BlankedHurt"]["warning_codes"] == [
        mw.WARNING_BLANK_GW, mw.WARNING_AVAILABILITY, mw.WARNING_HEURISTIC_XP]


def test_scoring_source_note_alone_is_still_reported(tmp_path):
    """Edge case: with nothing more actionable, heuristic_xp is the only warning."""
    players = _xp_world(tmp_path, _model_frame([(10, 7.0)]))
    heur = players[players["web_name"] == "Heur"].iloc[0]
    assert [code for code, _ in mw.player_warning_items(heur)] == [mw.WARNING_HEURISTIC_XP]


# ---- role signals: departed players and role_overrides.json -----------------

def _role_squad(tmp_path, gw_xp=None):
    """Three MIDs I own: Covered, Heur and Mystery (see ``_xp_world``)."""
    players = _xp_world(tmp_path, gw_xp)
    status = {"element_status": [{"element": e, "owner": 42} for e in (1, 2, 3)]}
    return players, status


def test_departed_squad_player_always_needs_attention(tmp_path):
    players, status = _role_squad(tmp_path)
    players.loc[players["web_name"] == "Heur", ["status", "news", "availability"]] = [
        "u", "Has joined Elsewhere FC", 0.0]
    week = mw.build_my_week(players, status, 42)
    att = {p["web_name"]: p for p in week["attention"]}
    assert att["Heur"]["warning_codes"] == [mw.WARNING_DEPARTED]     # not doubled as availability
    assert att["Heur"]["warnings"] == [
        "departed — no longer in the league, drop him — Has joined Elsewhere FC"]


# Explicit lifetime: these tests are about the my_week rendering, not staleness.
FRESH = {"valid_through_gw": 38}


def test_role_override_fact_is_listed_and_drives_gw_xp(tmp_path):
    frame = _model_frame([(10, 7.0)]).assign(xp_started=8.0, xp_cameo=1.0, num_fixtures=1)
    players, status = _role_squad(tmp_path, frame)
    overrides = [{"player": "Covered", "team": "ARS", "p_start": 0.25, "fact": "4th-choice now", **FRESH}]
    players, report = mw.apply_role_overrides(players, overrides, target_gw=4)
    assert report["overrides_applied"] == ["Covered"]
    week = mw.build_my_week(players, status, 42)
    covered = next(p for p in week["xi"] + week["bench"] if p["web_name"] == "Covered")
    assert covered["gw_xp"] == 2.0 and covered["p_start"] == 0.25      # 0.25 x 8.0
    assert covered["role_override"] == "4th-choice now"
    assert covered["warning_codes"] == [mw.WARNING_ROLE_OVERRIDE]
    assert covered["warnings"] == ["role override: p_start 0.25 — 4th-choice now"]
    att = {p["web_name"]: p for p in week["attention"]}
    assert att["Covered"]["warnings"] == covered["warnings"]
    others = [p for p in week["xi"] + week["bench"] if p["web_name"] != "Covered"]
    assert all(p["role_override"] is None for p in others)


def test_role_override_warning_text_for_fact_only_and_empty_entries(tmp_path):
    players, status = _role_squad(tmp_path)
    overrides = [{"player": "Covered", "team": "ARS", "fact": "new manager", **FRESH},
                 {"player": "Heur", "team": "ARS", **FRESH}]
    players, _ = mw.apply_role_overrides(players, overrides, target_gw=4)
    att = {p["web_name"]: p["warnings"][0] for p in mw.build_my_week(players, status, 42)["attention"]}
    assert att["Covered"] == "role override: new manager"
    assert att["Heur"] == "role override: no detail given"


def test_no_value_leads_an_override_on_an_unprojected_player(tmp_path):
    """The TUI rail shows the first warning: "unprojected", not the fact."""
    players = _xp_world(tmp_path, _model_frame([(10, 7.0)]))
    status = {"element_status": [{"element": e, "owner": 42} for e in (1, 2, 3)]}
    overrides = [{"player": "Mystery", "team": "ARS", "p_start": 0.7, "fact": "new signing",
                  **FRESH}]
    players, report = mw.apply_role_overrides(players, overrides, target_gw=4)
    assert report["overrides_applied"] == ["Mystery"]
    att = {p["web_name"]: p for p in mw.build_my_week(players, status, 42)["attention"]}
    assert att["Mystery"]["warning_codes"] == [mw.WARNING_NO_VALUE, mw.WARNING_ROLE_OVERRIDE]
    assert att["Mystery"]["warnings"] == ["no value — judge manually (player_card)",
                                          "role override: p_start 0.7 — new signing"]


def test_override_ruling_a_player_out_keeps_him_off_the_xi(tmp_path):
    """Regression (the Maddison case): flagged out in role_overrides.json but
    status "a" in the bootstrap, he took an XI slot from a fit player."""
    elements, projections = [], []
    for code, pos_type in enumerate([1, 1] + [2] * 5 + [3] * 5 + [4] * 3, start=100):
        elements.append({"id": code, "code": code, "web_name": f"P{code}",
                         "element_type": pos_type, "team": 1, "status": "a"})
        projections.append({"code": code, "projected_points": 190.0 - code % 100})
    _, players = _world(tmp_path, elements, projections)
    status = {"element_status": [{"element": e["id"], "owner": 42} for e in elements]}
    best_mid = "P107"                                   # highest-projected MID
    assert best_mid in {p["web_name"] for p in mw.build_my_week(players, status, 42)["xi"]}

    overrides = [{"player": best_mid, "team": "ARS", "p_start": 0.0, "return_gw": 9,
                  "fact": "shoulder fracture"}]
    players, _ = mw.apply_role_overrides(players, overrides, target_gw=4)
    week = mw.build_my_week(players, status, 42)
    assert best_mid not in {p["web_name"] for p in week["xi"]}
    benched = next(p for p in week["bench"] if p["web_name"] == best_mid)
    assert benched["gw_xp"] == 0.0
    assert benched["warnings"] == ["role override: p_start 0 — shoulder fracture"]


def test_xi_selection_value_treats_a_zero_override_as_unavailable():
    fit = pd.Series({"availability": 1.0, "gw_xp": 0.0, "role_override_p_start": 0.0})
    doubt = pd.Series({"availability": 1.0, "gw_xp": 1.5, "role_override_p_start": 0.3})
    plain = pd.Series({"availability": 1.0, "gw_xp": 1.5, "role_override_p_start": float("nan")})
    assert mw.xi_selection_value(fit) == -1.0
    assert mw.xi_selection_value(doubt) == 1.5 and mw.xi_selection_value(plain) == 1.5


def test_rows_carry_minutes_and_club_move_and_tolerate_no_overrides(tmp_path):
    bootstrap = {
        "teams": TEAMS, "fixtures": {"4": [{"team_h": 1, "team_a": 2}]},
        "elements": [{"id": 1, "code": 10, "web_name": "Mover", "element_type": 3,
                      "team": 1, "status": "a"}]}
    proj = tmp_path / "p.json"
    proj.write_text(json.dumps([{"code": 10, "projected_points": 76.0}]))
    seasons = pd.concat([SEASONS, pd.DataFrame([
        {"season": "2025-26", "code": 10, "team_name": "Wolves", "total_points": 0}])])
    panel = pd.DataFrame([{"season": "2026-27", "code": 10, "gw": gw, "minutes": 30}
                          for gw in (1, 2, 3)])
    players = mw.gw_xp_table(bootstrap, seasons, proj, season_panel=panel)
    week = mw.build_my_week(players, {"element_status": [{"element": 1, "owner": 42}]}, 42)
    row = (week["xi"] + week["bench"])[0]
    assert row["club_moved"] is True and row["expected_minutes"] == 30.0
    assert row["role_override"] is None and row["warnings"] == []
    json.dumps(week)                                    # plain JSON types only


def test_cli_writes_override_report_and_attention(weekly_cli_root, weekly_cli_argv, tmp_path, capsys):
    ml_dir = weekly_cli_root / "derived/2026-27/ml"
    ml_dir.mkdir(parents=True, exist_ok=True)
    (ml_dir / "role_overrides.json").write_text(json.dumps({"overrides": [
        {"player": "MyFWD", "team": "WOL", "p_start": 0.0, "return_gw": 9, "fact": "out"},
        {"player": "Ghost", "team": "ARS", "p_start": 0.5}]}))
    out = tmp_path / "my_week.json"
    assert mw.main(weekly_cli_argv(out, "heuristic")) == 0
    doc = json.loads(out.read_text())
    assert doc["overrides_applied"] == ["MyFWD"] and doc["overrides_unmatched"] == ["Ghost"]
    assert doc["overrides_expired"] == []
    assert doc["attention"][0]["warning_codes"] == [mw.WARNING_ROLE_OVERRIDE]
    assert "role overrides: applied MyFWD; unmatched Ghost; expired -" in capsys.readouterr().out


def test_my_week_reports_formation_bench_order_and_if_out(tmp_path):
    elements, projections = [], []
    code = 100
    for pos_type, count in ((1, 2), (2, 5), (3, 5), (4, 3)):
        for _ in range(count):
            elements.append({"id": code, "code": code, "web_name": f"P{code}",
                             "element_type": pos_type, "team": 1, "status": "a"})
            projections.append({"code": code, "projected_points": 190.0 - code % 100})
            code += 1
    elements[12]["status"] = "d"                  # a starting FWD is doubtful
    projections[12]["projected_points"] = 300.0   # ...but good enough to start
    _, players = _world(tmp_path, elements, projections)
    status = {"element_status": [{"element": e["id"], "owner": 42} for e in elements]}
    week = mw.build_my_week(players, status, entry_id=42)

    assert week["formation"] and sum(map(int, week["formation"].split("-"))) == 10
    assert week["bench"][0]["position"] == "GKP"
    assert [p["bench_slot"] for p in week["bench"]] == [1, 2, 3, 4]
    assert week["bench_order"] == [p["web_name"] for p in week["bench"]]
    assert "P112" in {p["web_name"] for p in week["xi"]}
    assert [a["web_name"] for a in week["if_out"]] == ["P112"]
