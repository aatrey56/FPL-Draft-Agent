"""squad_prefs.json never_drop list: loader, matching, expiry and the drop pick."""
import json

import pandas as pd
import pytest
from test_waiver import SEASONS, TEAMS

from backend.ml import waiver as wv


def _world(tmp_path, **plan_kwargs):
    """My squad: GK, FWDs Weak (ros 80) and Mid (ros 100). Free FWDs: Star, Extra."""
    projections = [{"code": 100, "projected_points": 120.0},
                   {"code": 101, "projected_points": 80.0},
                   {"code": 102, "projected_points": 100.0},
                   {"code": 200, "projected_points": 150.0},
                   {"code": 201, "projected_points": 130.0}]
    proj_path = tmp_path / "projections.json"
    proj_path.write_text(json.dumps(projections))
    bootstrap = {
        "teams": TEAMS, "fixtures": {"1": [{"team_h": 1, "team_a": 2}]},
        "events": {"current": 5, "next": 6, "data": []},     # plans for GW6
        "elements": [
            {"id": 10, "code": 100, "web_name": "MyGK", "element_type": 1, "team": 1, "status": "a"},
            {"id": 11, "code": 101, "web_name": "Weak", "element_type": 4, "team": 2, "status": "a"},
            {"id": 12, "code": 102, "web_name": "Mid", "element_type": 4, "team": 1, "status": "a"},
            {"id": 20, "code": 200, "web_name": "Star", "element_type": 4, "team": 1, "status": "a"},
            {"id": 21, "code": 201, "web_name": "Extra", "element_type": 4, "team": 1, "status": "a"}]}
    status = {"element_status": [
        *({"element": e, "owner": 42} for e in (10, 11, 12)),
        *({"element": e, "owner": None} for e in (20, 21))]}
    return wv.plan(bootstrap, status, SEASONS, proj_path, 42, **plan_kwargs)


def _drops(result):
    return {rec["drop"] for rec in result["recommendations"]}


def test_unprotected_baseline_drops_the_weakest(tmp_path):
    result = _world(tmp_path)
    assert _drops(result) == {"Weak"}
    assert result["never_drop_applied"] == []


def test_protected_player_is_never_dropped_and_the_next_worst_is_used(tmp_path):
    result = _world(tmp_path, never_drop=[{"player": "Weak", "team": "WOL"}])
    assert _drops(result) == {"Mid"}
    assert {rec["drop"] for rec in result["best_by_position"]["FWD"]} == {"Mid"}
    assert [c["web_name"] for c in result["drop_candidates"] if c["position"] == "FWD"] == ["Mid"]
    assert result["never_drop_applied"] == ["Weak"]
    assert result["never_drop_unmatched"] == [] and result["never_drop_expired"] == []


def test_a_position_with_every_player_protected_gets_no_swaps(tmp_path):
    result = _world(tmp_path, never_drop=[{"player": "Weak", "team": "WOL"},
                                          {"player": "Mid", "team": "ARS"}])
    assert result["recommendations"] == []
    assert result["best_by_position"]["FWD"] == []
    assert [c for c in result["drop_candidates"] if c["position"] == "FWD"] == []
    assert result["never_drop_applied"] == ["Weak", "Mid"]


def test_protection_does_not_leak_to_other_positions(tmp_path):
    result = _world(tmp_path, never_drop=[{"player": "MyGK", "team": "ARS"}])
    assert _drops(result) == {"Weak"}


def _table():
    return pd.DataFrame([
        {"code": 101, "web_name": "Weak", "team": "WOL"},
        {"code": 102, "web_name": "Mid", "team": "ARS"},
        {"code": 103, "web_name": "Mid", "team": "WOL"}])


def test_matching_by_code_wins_and_accepts_a_quoted_code():
    out, report = wv.apply_never_drop(
        _table(), [{"player": "ignored", "team": "ignored", "code": "102"}], 5)
    assert out["never_drop"].tolist() == [False, True, False]
    assert report["never_drop_applied"] == ["ignored"]


def test_matching_by_name_and_team_needs_both():
    out, report = wv.apply_never_drop(_table(), [{"player": "Mid", "team": "WOL"}], 5)
    assert out["never_drop"].tolist() == [False, False, True]
    assert report["never_drop_applied"] == ["Mid"]


def test_unmatched_and_ambiguous_entries_are_reported_not_applied():
    entries = [{"player": "Ghost", "team": "ARS"},       # nobody
               {"player": "Mid"},                        # no team: matches no row
               {"code": "abc", "player": "Bad"},         # non-numeric code
               {"player": "Weak", "team": "WOL", "until_gw": "soon"}]
    out, report = wv.apply_never_drop(_table(), entries, 5)
    assert not out["never_drop"].any()
    assert report["never_drop_unmatched"] == ["Ghost", "Mid", "Bad", "Weak"]


def test_until_gw_protects_through_that_gameweek_then_expires():
    entry = [{"player": "Weak", "team": "WOL", "until_gw": 6}]
    through, live = wv.apply_never_drop(_table(), entry, 6)
    assert through["never_drop"].tolist() == [True, False, False]
    assert live["never_drop_applied"] == ["Weak"]
    past, report = wv.apply_never_drop(_table(), entry, 7)
    assert not past["never_drop"].any()
    assert report["never_drop_expired"] == ["Weak"] and report["never_drop_applied"] == []


def test_entry_without_until_gw_never_expires():
    out, report = wv.apply_never_drop(_table(), [{"player": "Weak", "team": "WOL"}], 38)
    assert out["never_drop"].tolist() == [True, False, False]
    assert report["never_drop_expired"] == []


def test_plan_expires_an_entry_past_until_gw(tmp_path):
    expired = _world(tmp_path, never_drop=[{"player": "Weak", "team": "WOL", "until_gw": 5}])
    assert expired["never_drop_expired"] == ["Weak"]
    assert _drops(expired) == {"Weak"}      # expired -> no longer protected
    live = _world(tmp_path, never_drop=[{"player": "Weak", "team": "WOL", "until_gw": 6}])
    assert live["never_drop_applied"] == ["Weak"] and _drops(live) == {"Mid"}


def test_loader_reads_entries_and_tolerates_absent_or_bad_files(tmp_path, caplog):
    path = tmp_path / "squad_prefs.json"
    assert wv.load_squad_prefs(path) == []                       # absent: silent
    path.write_text(json.dumps({"never_drop": [{"player": "Weak", "team": "WOL"}, "junk"]}))
    assert wv.load_squad_prefs(path) == [{"player": "Weak", "team": "WOL"}]
    for bad in ("{not json", json.dumps({"never_drop": "Weak"}), json.dumps([1])):
        path.write_text(bad)
        with caplog.at_level("WARNING"):
            assert wv.load_squad_prefs(path) == []
    assert "never_drop" in caplog.text


def test_drop_order_without_the_column_is_unchanged(tmp_path):
    result = _world(tmp_path)
    squad = result["squad"].drop(columns="never_drop")
    assert len(wv.drop_order(squad[squad["position"] == "FWD"])) == 2


@pytest.mark.parametrize("key", wv.NEVER_DROP_REPORT_KEYS)
def test_plan_always_reports_every_never_drop_list(tmp_path, key):
    assert _world(tmp_path)[key] == []
