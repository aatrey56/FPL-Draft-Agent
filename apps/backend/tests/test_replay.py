"""Tests for the waiver replay harness (backend.ml.replay). tmp_path fixtures only."""

import json

import pandas as pd
import pytest

from backend.ml import replay as rp
from backend.ml import waiver as wv
from test_waiver import SEASONS, _players_fixture

ME, OTHER, LEAGUE = 42, 99, 7
T2 = "2026-08-27T17:30:00Z"            # waivers_time of GW2
X, Y, Z, W, MY_FWD, OTHERS_PLAYER = 20, 21, 22, 23, 11, 30
TEAMS = [{"id": 1, "name": "Arsenal", "short_name": "ARS"},
         {"id": 2, "name": "Wolves", "short_name": "WOL"},
         {"id": 3, "name": "Hull City", "short_name": "HUL"}]
SCHEDULE = {2: [(1, 3)], 3: [(1, 2)], 4: [(2, 3)]}   # (home, away) per GW


def _live(event, points, minutes=90, fixtures=None):
    fixtures = SCHEDULE[event] if fixtures is None else fixtures
    return {
        "elements": {str(e): {"stats": {"total_points": p, "minutes": minutes if p else 0}}
                     for e, p in points.items()},
        "fixtures": [{"team_h": h, "team_a": a} for h, a in fixtures],
    }


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _snapshot(x_owner=None, with_w=False):
    rows = [
        {"element": MY_FWD, "owner": ME, "status": "a"},
        {"element": X, "owner": x_owner, "status": "a"},
        {"element": Y, "owner": None, "status": "a"},
        {"element": Z, "owner": None, "status": "a"},
        {"element": OTHERS_PLAYER, "owner": OTHER, "status": "a"},
    ]
    if with_w:
        rows.append({"element": W, "owner": None, "status": "a"})
    return {"element_status": rows}


def build_world(tmp_path, *, x_status="a", live3=True, live4=True, transactions=(),
                bootstrap_fixtures=None, w_status=None, w_gw1_pts=0):
    """data_root with GW1-4 live files, projections, season table, snapshots.

    GW1: Z is the top scorer (std_points/form3 pick Z). X is the model's
    favourite (highest projection) and hauls 20 in GW3. Optional free agent
    W (projection 200, current bootstrap status ``w_status``, ``w_gw1_pts``
    points in GW1 and never again) is absent unless w_status is given.
    """
    raw = tmp_path / "raw/2026-27"
    elements = [
        {"id": MY_FWD, "code": 1011, "web_name": "MyFwd", "element_type": 4, "team": 2},
        {"id": X, "code": 1020, "web_name": "Xavier", "element_type": 4, "team": 1,
         "status": x_status, "news": "out" if x_status != "a" else "",
         "chance_of_playing_next_round": 0 if x_status != "a" else None, "form": "99"},
        {"id": Y, "code": 1021, "web_name": "Yorke", "element_type": 4, "team": 1},
        {"id": Z, "code": 1022, "web_name": "Zeta", "element_type": 4, "team": 3},
        {"id": OTHERS_PLAYER, "code": 1030, "web_name": "Theirs", "element_type": 3, "team": 1},
    ]
    if w_status:
        elements.append({"id": W, "code": 1023, "web_name": "Wex", "element_type": 4,
                         "team": 1, "status": w_status})
    waivers = {1: "2026-08-20T17:30:00Z", 2: T2, 3: "2026-09-03T17:30:00Z",
               4: "2026-09-11T12:30:00Z", 5: "2026-09-17T17:30:00Z"}
    _write(raw / "bootstrap/bootstrap-static.json", {
        "teams": TEAMS, "elements": elements,
        "events": {"data": [{"id": k, "waivers_time": v} for k, v in waivers.items()]},
        "fixtures": bootstrap_fixtures if bootstrap_fixtures is not None else {
            "3": [{"team_h": h, "team_a": a} for h, a in SCHEDULE[3]],
            "4": [{"team_h": h, "team_a": a} for h, a in SCHEDULE[4]]},
    })
    gw1 = {Z: 9, X: 2, Y: 1, **({W: w_gw1_pts} if w_gw1_pts else {})}
    _write(raw / "gw/1/live.json", _live(1, gw1, fixtures=[(1, 2)]))
    _write(raw / "gw/2/live.json", _live(2, {X: 3, Y: 6, MY_FWD: 1}))
    if live3:
        _write(raw / "gw/3/live.json", _live(3, {X: 20}))
    if live4:
        _write(raw / "gw/4/live.json", _live(4, {X: 2}))
    league = raw / "league" / str(LEAGUE)
    _write(league / "element_status_history/20260827T1700.json", _snapshot(with_w=bool(w_status)))
    _write(league / "element_status_history/20260828T0100.json",
           _snapshot(x_owner=OTHER, with_w=bool(w_status)))
    _write(league / "transactions.json", {"transactions": list(transactions)})
    derived = tmp_path / "derived/ml"
    derived.mkdir(parents=True)
    pd.DataFrame([
        {"season": "2025-26", "team_name": "Arsenal", "total_points": 2000},
        {"season": "2025-26", "team_name": "Wolves", "total_points": 1200},
    ]).to_parquet(derived / "player_seasons.parquet")
    _write(derived / "projections_2627.json", [
        {"code": 1011, "projected_points": 80.0}, {"code": 1020, "projected_points": 150.0},
        {"code": 1021, "projected_points": 100.0}, {"code": 1022, "projected_points": 60.0},
        {"code": 1023, "projected_points": 200.0}])
    return tmp_path


def _claim(entry, element_in, kind="w", result="a", event=2, element_out=MY_FWD):
    return {"entry": entry, "event": event, "kind": kind, "result": result,
            "element_in": element_in, "element_out": element_out}


def _picks(doc, gw=2):
    return {r["strategy"]: (r["add"], r["drop"], r["won"]) for r in doc["rows"]
            if r["gw"] == gw and r["strategy"] != "me"}


def _row(doc, strategy, gw=2):
    return next(r for r in doc["rows"] if r["gw"] == gw and r["strategy"] == strategy)


# ---- leakage tests (brief section 6) ----------------------------------------

def test_leak1_future_haul_does_not_change_picks(tmp_path):
    """X hauls 20 in GW3: with or without gw/3/live.json the picks are identical."""
    with_future = rp.run(build_world(tmp_path / "a", live3=True), "2026-27", LEAGUE, ME, [2])
    without = rp.run(build_world(tmp_path / "b", live3=False), "2026-27", LEAGUE, ME, [2])
    assert _picks(with_future) == _picks(without)
    assert _picks(with_future)["waiver_plan"][0] == "Xavier"
    assert _picks(with_future)["std_points"][0] == "Zeta"
    # only the realized gw3 score sees the haul
    assert _row(with_future, "waiver_plan")["gw3_gain"] == 3 - 1 + 20 - 0 + 2 - 0
    assert _row(without, "waiver_plan")["gw3_gain"] is None


def test_leak2_snapshot_choice(tmp_path):
    world = build_world(tmp_path)
    history = world / "raw/2026-27/league" / str(LEAGUE) / "element_status_history"
    _write(history / "20260827T1200.json", _snapshot(x_owner=OTHER))
    stem, data = rp.asof_snapshot(history, T2)
    assert stem == "20260827T1700"                       # latest <= T_N, not the 1200 one
    assert next(r for r in data["element_status"] if r["element"] == X)["owner"] is None
    assert rp.asof_snapshot(history, "2026-08-27T17:00:00Z")[0] == "20260827T1700"  # inclusive
    assert rp.asof_snapshot(history, "2026-08-28T01:00:00Z")[0] == "20260828T0100"
    with pytest.raises(ValueError):
        rp.asof_snapshot(history, "2026-08-27T11:59:00Z")  # nothing at or before
    # X is owned in the later snapshot yet still in the replay pool at GW2
    doc = rp.run(world, "2026-27", LEAGUE, ME, [2])
    assert _picks(doc)["waiver_plan"][0] == "Xavier"


def test_leak3_status_neutralised(tmp_path):
    world = build_world(tmp_path, x_status="i")
    bootstrap = json.loads((world / "raw/2026-27/bootstrap/bootstrap-static.json").read_text())
    neutral = rp.neutralize_bootstrap(bootstrap)
    el = next(e for e in neutral["elements"] if e["id"] == X)
    assert (el["status"], el["news"], el["chance_of_playing_next_round"]) == ("a", "", None)
    assert "form" not in el and "fixtures" not in neutral
    projections = world / "derived/ml/projections_2627.json"
    fixtures = {"2": [{"team_h": 1, "team_a": 3}]}
    gated = wv.build_player_table(bootstrap, SEASONS, projections, fixtures_by_event=fixtures)
    flat = wv.build_player_table(bootstrap, SEASONS, projections, fixtures_by_event=fixtures,
                                 neutral_availability=True)
    assert gated[gated["element"] == X].iloc[0]["availability"] == 0.0   # default still gates
    assert flat[flat["element"] == X].iloc[0]["availability"] == 1.0
    doc = rp.run(world, "2026-27", LEAGUE, ME, [2])
    assert _picks(doc)["waiver_plan"][0] == "Xavier"                     # injured X recommendable
    assert doc["availability_mode"] == "neutral+departed"
    assert "neutral+departed" in rp.format_table(doc)


def test_departed_with_no_prior_minutes_is_excluded(tmp_path):
    """(a) status "u" and 0 minutes in every GW < N: never recommended."""
    world = build_world(tmp_path, w_status="u")                  # W: 0 minutes ever
    bootstrap = json.loads((world / "raw/2026-27/bootstrap/bootstrap-static.json").read_text())
    raw = world / "raw/2026-27"
    assert rp.departed_before(bootstrap, raw, 2) == {W}
    el = next(e for e in rp.neutralize_bootstrap(bootstrap, {W})["elements"] if e["id"] == W)
    assert el["status"] == "u"
    doc = rp.run(world, "2026-27", LEAGUE, ME, [2])
    assert all(r["add"] != "Wex" for r in doc["rows"])
    assert _picks(doc)["waiver_plan"][0] == "Xavier"             # next-best, not W (proj 200)


def test_departed_squad_player_has_zero_next3_and_is_not_in_xi(tmp_path):
    """A departed player in MY squad keeps status "u" in replay: availability 0,
    so next3_xp is 0 and the best XI leaves him out."""
    spec = [(1, 1)] + [(i, 2) for i in range(2, 6)] + [(i, 3) for i in range(6, 10)] \
        + [(i, 4) for i in range(10, 13)]                      # GK, 4 DEF, 4 MID, 3 FWD
    elements = [{"id": i, "code": 100 + i, "web_name": f"P{i}", "element_type": pos,
                 "team": 1, "status": "u" if i == 12 else "a"} for i, pos in spec]
    bootstrap = {"teams": TEAMS, "elements": elements}
    projections = tmp_path / "p.json"
    projections.write_text(json.dumps(
        [{"code": 100 + i, "projected_points": 120.0} for i, _ in spec]))
    status = {"element_status": [{"element": i, "owner": ME} for i, _ in spec]}
    fixtures = {"2": [{"team_h": 1, "team_a": 2}]}
    neutral = rp.neutralize_bootstrap(bootstrap, {12})
    result = wv.plan(neutral, status, SEASONS, projections, ME,
                     fixtures_by_event=fixtures, neutral_availability=True)
    players = result["players"].set_index("element")
    assert players.loc[12, "next3_xp"] == 0.0 and players.loc[11, "next3_xp"] > 0
    xi, _ = wv.best_xi(result["squad"])
    assert 12 not in set(xi["element"])


def test_departed_with_prior_minutes_stays_eligible(tmp_path):
    """(b) status "u" but played before N: he left later, so he stays eligible."""
    world = build_world(tmp_path, w_status="u", w_gw1_pts=5)
    bootstrap = json.loads((world / "raw/2026-27/bootstrap/bootstrap-static.json").read_text())
    assert rp.departed_before(bootstrap, world / "raw/2026-27", 2) == set()
    doc = rp.run(world, "2026-27", LEAGUE, ME, [2])
    assert _picks(doc)["waiver_plan"][0] == "Wex"


@pytest.mark.parametrize("status", ["i", "d"])
def test_injured_or_doubtful_still_neutralised(tmp_path, status):
    """(c) only "u" is kept out; "i"/"d" are neutralised to available."""
    world = build_world(tmp_path, w_status=status)
    bootstrap = json.loads((world / "raw/2026-27/bootstrap/bootstrap-static.json").read_text())
    assert rp.departed_before(bootstrap, world / "raw/2026-27", 2) == set()
    el = next(e for e in rp.neutralize_bootstrap(bootstrap)["elements"] if e["id"] == W)
    assert el["status"] == "a"
    doc = rp.run(world, "2026-27", LEAGUE, ME, [2])
    assert _picks(doc)["waiver_plan"][0] == "Wex"


def test_leak4_fixtures_as_of(tmp_path):
    bootstrap_only_6_to_8 = {"6": [{"team_h": 1, "team_a": 2}], "7": [], "8": []}
    world = build_world(tmp_path, bootstrap_fixtures=bootstrap_only_6_to_8)
    raw = world / "raw/2026-27"
    # live GW3 has only 1 v 2: team 3 (Hull) is blank that week
    bootstrap = json.loads((raw / "bootstrap/bootstrap-static.json").read_text())
    fixtures = rp.asof_fixtures(bootstrap, raw, 2)
    assert sorted(fixtures) == ["2", "3", "4"]            # live.json, never the 6-8 bootstrap keys
    assert fixtures["2"] == [{"team_h": 1, "team_a": 3}]
    strengths = wv.team_strengths(SEASONS, TEAMS)
    load = wv.next_fixture_load(rp.neutralize_bootstrap(bootstrap), strengths,
                                fixtures_by_event=fixtures)
    expected = (wv.fixture_multiplier(strengths[1], strengths, is_home=False)   # GW2 at Arsenal
                + wv.fixture_multiplier(strengths[2], strengths, is_home=False))  # GW4 at Wolves
    assert load[3] == pytest.approx(expected)             # blank in GW3: two fixtures only
    assert load[1] > 0 and load[2] > 0


def test_leak5_contested_claim_falls_back(tmp_path):
    lost = rp.run(build_world(tmp_path / "a", transactions=[_claim(OTHER, X)]),
                  "2026-27", LEAGUE, ME, [2])
    row = _row(lost, "waiver_plan")
    assert (row["add"], row["won"]) == ("Yorke", "fallback")      # rank-2 scored
    assert row["lost_to"] == [{"add": "Xavier", "by": "other"}]
    assert _row(lost, "std_points")["won"] == "y"                  # Zeta uncontested
    won = rp.run(build_world(tmp_path / "b", transactions=[_claim(ME, X)]),
                 "2026-27", LEAGUE, ME, [2])
    assert (_row(won, "waiver_plan")["add"], _row(won, "waiver_plan")["won"]) == ("Xavier", "y")


def test_leak5_exhausted_when_every_candidate_lost():
    candidates = [(X, MY_FWD), (Y, MY_FWD)]
    assert rp.select_pick(candidates, {X, Y}) == (None, "n", [X, Y])
    assert rp.select_pick([], set()) == (None, "-", [])


def test_leak6_plan_regression_matches_pre_seam_recommendations(tmp_path):
    """Golden: origin/main's recommend() on the _players_fixture world returned
    exactly these (add, drop) pairs in this order, before the plan() seam."""
    _, status = _players_fixture(tmp_path)
    golden = [("SeasonStar", "MyWeakFWD"), ("Streamer", "MyWeakFWD")]
    # rebuild the same world through plan(): same elements/fixtures as _players_fixture
    fixture_bootstrap = {
        "teams": TEAMS,
        "fixtures": {"1": [{"team_h": 1, "team_a": 2}], "2": [{"team_h": 2, "team_a": 1}],
                     "3": [{"team_h": 1, "team_a": 3}]},
        "elements": [
            {"id": 10, "code": 100, "web_name": "MyGK", "element_type": 1, "team": 1, "status": "a"},
            {"id": 11, "code": 101, "web_name": "MyWeakFWD", "element_type": 4, "team": 2, "status": "a"},
            {"id": 20, "code": 200, "web_name": "SeasonStar", "element_type": 4, "team": 1, "status": "a"},
            {"id": 21, "code": 201, "web_name": "Streamer", "element_type": 4, "team": 1, "status": "a"},
            {"id": 22, "code": 202, "web_name": "Departed", "element_type": 4, "team": 1, "status": "u"},
        ]}
    result = wv.plan(fixture_bootstrap, status, SEASONS, tmp_path / "projections.json", 42)
    assert [(r["add"], r["drop"]) for r in result["recommendations"]] == golden
    star = next(r for r in result["recommendations"] if r["add"] == "SeasonStar")
    assert (star["add_element"], star["drop_element"]) == (20, 11)
    assert set(result) == {"players", "squad", "xi_next3_xp", "recommendations", "unprojected_squad"}


def test_leak7_dnp_and_missing_are_zero_not_nan_and_gw3_null_for_late_gws():
    live = {4: {X: 5}, 5: {X: 5}, 6: None}                # Y absent = DNP; GW6 not fetched
    scored = rp.score_pick(X, Y, 4, live)
    assert scored["drop_pts"] == 0 and scored["gw_gain"] == 5.0
    assert not pd.isna(scored["gw_gain"])
    assert scored["gw3_gain"] is None                     # N=4: GW6 does not exist
    assert rp.score_pick(X, 999, 5, {5: {X: 5}, 6: None, 7: None})["gw3_gain"] is None
    full = rp.score_pick(X, Y, 2, {2: {X: 4}, 3: {X: 1}, 4: {}})
    assert full["gw3_gain"] == 5.0                        # all three present
    assert rp.score_pick(999, Y, 2, {2: {}, 3: {}, 4: {}})["gw_gain"] == 0.0   # unknown add


def test_leak7_gw_points_never_nan(tmp_path):
    raw = tmp_path / "raw"
    _write(raw / "gw/2/live.json", {"elements": {"1": {"stats": {"total_points": None}}}})
    assert rp.gw_points(raw, 2) == {1: 0}
    assert rp.gw_points(raw, 3) is None


# ---- baselines and the "me" row --------------------------------------------

def test_baseline_form3_window_and_tie_break():
    elements = {i: {"element_type": 4} for i in (1, 2, 3, 4, 5)}
    history = {1: {3: 10}, 2: {4: 1}, 3: {4: 1}, 4: {4: 1}}
    # event 5: form3 looks at GW2-4 only (3 scores only in GW1), std_points sees all of it
    assert rp.baseline_pick("form3", 5, [3, 4], [1, 2], elements, history)[0] == (4, 1)
    assert rp.baseline_pick("std_points", 5, [3, 4], [1, 2], elements, history)[0] == (3, 1)
    with pytest.raises(ValueError):
        rp.baseline_pick("std_points", 3, [3], [1], elements, {3: {}})   # reads GW >= N


def test_me_row_no_move_is_zero_and_two_moves_are_summed(tmp_path):
    world = build_world(tmp_path, transactions=[
        _claim(ME, X, event=3, element_out=MY_FWD),                       # GW3: one waiver claim
        _claim(ME, Y, kind="f", event=4, element_out=MY_FWD),              # GW4: f + w
        _claim(ME, Z, event=4, element_out=OTHERS_PLAYER),
        _claim(ME, Z, result="di", event=2),                              # denied: ignored
        _claim(OTHER, Y, event=2)])                                       # not mine: ignored
    doc = rp.run(world, "2026-27", LEAGUE, ME, [2, 3, 4])
    none = _row(doc, "me", 2)
    assert (none["add"], none["moves"], none["gw_gain"]) == ("-", 0, 0.0)
    one = _row(doc, "me", 3)
    assert (one["add"], one["moves"], one["gw_gain"]) == ("Xavier", 1, 20.0)
    two = rp.score_actual([(X, MY_FWD), (Y, OTHERS_PLAYER)], 2, {
        X: {"web_name": "Xavier", "element_type": 4}, Y: {"web_name": "Yorke", "element_type": 4},
        MY_FWD: {"web_name": "MyFwd", "element_type": 4},
        OTHERS_PLAYER: {"web_name": "Theirs", "element_type": 3}},
        {2: {X: 3, Y: 6, MY_FWD: 1}, 3: {X: 20}, 4: {X: 2}}, {X: 90, Y: 90})
    assert two["moves"] == 2 and two["add"] == "Xavier+Yorke"
    assert two["gw_gain"] == (3 - 1) + (6 - 0)
    assert two["gw3_gain"] == (3 - 1 + 20 + 2) + (6 - 0)
    assert len(rp.my_moves(world_transactions(world), 4, ME)) == 2        # w + f both counted


def world_transactions(world):
    path = world / "raw/2026-27/league" / str(LEAGUE) / "transactions.json"
    return json.loads(path.read_text())["transactions"]


def test_output_has_no_ids_and_prints_five_rows_per_gw(tmp_path):
    doc = rp.run(build_world(tmp_path), "2026-27", LEAGUE, ME, [2])
    assert [r["strategy"] for r in doc["rows"]] == list(rp.STRATEGIES)
    assert "entry" not in json.dumps(doc["rows"])
    assert all(r["won"] in ("y", "n", "fallback", "-") for r in doc["rows"])
    assert "no_change" in rp.format_table(doc) and rp.CAVEAT in rp.format_table(doc)
