"""Tests for the player_values export (backend.ml.player_values). No network."""

import json

import pandas as pd

from backend.ml import jsonutil
from backend.ml import player_values as pv
from backend.ml import waiver as wv

META = {"scorer": "model", "xp_fallback": False, "xp_fallback_reason": None}


def _players() -> pd.DataFrame:
    return pd.DataFrame([
        {"code": 20, "element": 2, "web_name": "Beta", "position": "MID", "team": "WOL",
         "status": "a", "xp_next": 4.1, "xp_source": "model", "next3_xp": 11.2,
         "next3_source": "model", "ros_points": 120.0, "ros_adj": 110.0, "p_start": 0.9,
         "news": "ignored"},
        {"code": 10, "element": 1, "web_name": "Alpha", "position": "FWD", "team": "ARS",
         "status": "i", "xp_next": None, "xp_source": "none", "next3_xp": float("nan"),
         "next3_source": "none", "ros_points": float("nan"), "ros_adj": float("nan"),
         "p_start": float("nan")},
    ])


def test_build_player_values_renames_sorts_and_keeps_metadata():
    doc = pv.build_player_values(_players(), season="2026-27", gw=6, panel_max_gw=5,
                                 meta=META, generated_at="2026-10-07T00:00:00+00:00")
    assert doc["gw"] == 6 and doc["panel_max_gw"] == 5 and doc["scorer"] == "model"
    assert [p["web_name"] for p in doc["players"]] == ["Alpha", "Beta"]  # sorted by code
    beta = doc["players"][1]
    assert beta["xp_h3"] == 11.2 and beta["xp_h3_source"] == "model"
    assert beta["ros_adj"] == 110.0 and "news" not in beta
    assert set(beta) == set(pv.PLAYER_FIELDS)


def test_unvalued_player_exports_nulls_and_strict_json(tmp_path):
    doc = pv.build_player_values(_players(), season="2026-27", gw=6, panel_max_gw=None,
                                 meta=META, generated_at="x")
    alpha = doc["players"][0]
    assert alpha["xp_next"] is None and alpha["xp_h3"] is None and alpha["ros_adj"] is None
    out = tmp_path / "ml" / "player_values.json"
    pv.write_atomic(out, jsonutil.dumps_strict(doc))
    loaded = json.loads(out.read_text())  # strict: no NaN literals
    assert loaded["players"][0]["p_start"] is None
    assert not any(p.name.endswith(".tmp") for p in out.parent.iterdir())


def test_panel_max_gw_without_model_is_none():
    assert pv._panel_max_gw(None) is None
    assert pv._panel_max_gw(pd.DataFrame({"panel_max_gw": [5, 5]})) == 5


def test_horizon_events_reach_the_document():
    meta = {**META, "horizon_fallback": False, "horizon_fallback_reason": None,
            "horizon_events": [37, 38]}
    doc = pv.build_player_values(_players(), season="2026-27", gw=37, panel_max_gw=36,
                                 meta=meta, generated_at="x")
    assert doc["horizon_events"] == [37, 38]
    loaded = json.loads(jsonutil.dumps_strict(doc))
    assert loaded["horizon_events"] == [37, 38]


def test_cli_reports_research_overrides(weekly_cli_root, tmp_path):
    ml_dir = weekly_cli_root / "derived/2026-27/ml"
    (ml_dir / "role_overrides.research.json").write_text(json.dumps({"overrides": [
        {"player": "FreeFWD", "team": "ARS", "code": 102, "p_start": 0.3, "valid_through_gw": 6,
         "fact": "research: rotation risk [high]", "source": "research"}]}))
    out = tmp_path / "player_values.json"
    assert pv.main(["--season", "2026-27", "--data-root", str(weekly_cli_root),
                    "--out", str(out), "--scorer", "heuristic"]) == 0
    doc = json.loads(out.read_text())
    assert doc["overrides_applied"] == ["research:FreeFWD"]
    assert set(wv.OVERRIDE_REPORT_KEYS) <= set(doc)
    assert next(p for p in doc["players"] if p["web_name"] == "FreeFWD")["p_start"] == 0.3
