"""Tests for the drop-radar (backend.ml.ownership). No network — fixtures only."""

import json

from backend.ml import ownership as ow

BOOTSTRAP = {
    "elements": [
        {"id": 10, "code": 100, "web_name": "Alpha", "element_type": 4, "team": 1,
         "status": "a", "news": ""},
        {"id": 20, "code": 200, "web_name": "Beta", "element_type": 3, "team": 1,
         "status": "i", "news": "Knee injury"},
        {"id": 30, "code": 300, "web_name": "Gamma", "element_type": 2, "team": 2,
         "status": "a", "news": ""},
    ],
    "teams": [{"id": 1, "name": "Arsenal", "short_name": "ARS"},
              {"id": 2, "name": "Hull City", "short_name": "HUL"}],
}


def test_diff_detects_drop_add_move():
    prev = {10: 888888, 20: None, 30: 999}
    curr = {10: None, 20: 888888, 30: 888}
    events = ow.diff_snapshots(prev, curr, ts="20260821T1200")
    kinds = {e["element"]: e["kind"] for e in events}
    assert kinds == {10: "drop", 20: "add", 30: "move"}
    drop = next(e for e in events if e["element"] == 10)
    assert drop["from_owner"] == 888888 and drop["to_owner"] is None


def test_diff_ignores_unchanged_and_handles_new_elements():
    prev = {10: 1}
    curr = {10: 1, 20: 2}  # 20 appears mid-season, already owned
    events = ow.diff_snapshots(prev, curr, ts="t")
    assert len(events) == 1
    assert events[0] == {"ts": "t", "element": 20, "kind": "add",
                         "from_owner": None, "to_owner": 2}


def test_build_events_across_history_is_ordered():
    snaps = [
        ("t1", {10: None}),
        ("t2", {10: 5}),      # add at t2
        ("t3", {10: None}),   # drop at t3
    ]
    events = ow.build_events(snaps)
    assert [(e["ts"], e["kind"]) for e in events] == [("t2", "add"), ("t3", "drop")]


def test_free_agent_report_ranks_by_projection_and_keeps_unprojected(tmp_path):
    latest = {10: None, 20: None, 30: 42}  # Gamma owned; Alpha+Beta free
    projections = [{"code": 100, "projected_points": 120.0, "tier": 2, "confidence": "high"}]
    proj_path = tmp_path / "projections.json"
    proj_path.write_text(json.dumps(projections))

    report = ow.free_agent_report(latest, BOOTSTRAP, proj_path, top_n=10)
    assert [r["web_name"] for r in report] == ["Alpha", "Beta"]  # projected first
    assert report[0]["projected_points"] == 120.0
    assert report[1]["projected_points"] is None                 # kept, ranked last
    assert report[1]["availability"] == "i" and "Knee" in report[1]["news"]


def test_free_agent_report_without_projections_file(tmp_path):
    report = ow.free_agent_report({10: None}, BOOTSTRAP, tmp_path / "missing.json")
    assert len(report) == 1 and report[0]["projected_points"] is None


def test_annotate_events_attaches_identity():
    events = [{"ts": "t", "element": 20, "kind": "drop", "from_owner": 1, "to_owner": None}]
    out = ow.annotate_events(events, BOOTSTRAP)
    assert out[0]["web_name"] == "Beta" and out[0]["position"] == "MID" and out[0]["team"] == "ARS"


def test_free_agent_report_excludes_departed_players(tmp_path):
    bootstrap = {
        "elements": [{"id": 40, "code": 400, "web_name": "GonePlayer", "element_type": 4,
                      "team": 1, "status": "u", "news": "Has joined Como permanently"}],
        "teams": [{"id": 1, "name": "Arsenal", "short_name": "ARS"}],
    }
    report = ow.free_agent_report({40: None}, bootstrap, tmp_path / "missing.json")
    assert report == []
