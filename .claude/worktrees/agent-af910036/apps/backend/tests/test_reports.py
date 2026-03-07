"""Tests for backend.reports — weight helper and DGW fixture display."""

import sys
import types
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub heavy optional dependencies before any backend module is imported.
# ---------------------------------------------------------------------------
for _mod in ("requests", "openai", "apscheduler", "apscheduler.schedulers.background",
             "apscheduler.triggers.cron"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

_apscheduler_bg = sys.modules["apscheduler.schedulers.background"]
if not hasattr(_apscheduler_bg, "BackgroundScheduler"):
    _apscheduler_bg.BackgroundScheduler = MagicMock  # type: ignore[attr-defined]

_apscheduler_cron = sys.modules["apscheduler.triggers.cron"]
if not hasattr(_apscheduler_cron, "CronTrigger"):
    _apscheduler_cron.CronTrigger = MagicMock  # type: ignore[attr-defined]

sys.modules["openai"].OpenAI = MagicMock  # type: ignore[attr-defined]

if "zoneinfo" not in sys.modules:
    _zi = types.ModuleType("zoneinfo")
    _zi.ZoneInfo = MagicMock  # type: ignore[attr-defined]
    sys.modules["zoneinfo"] = _zi

_fastapi_mod = types.ModuleType("fastapi")
_fastapi_mod.FastAPI = MagicMock  # type: ignore[attr-defined]
_fastapi_mod.WebSocket = MagicMock  # type: ignore[attr-defined]

for _key, _stub in [
    ("fastapi", _fastapi_mod),
    ("fastapi.middleware.cors", types.ModuleType("fastapi.middleware.cors")),
    ("fastapi.staticfiles", types.ModuleType("fastapi.staticfiles")),
    ("starlette.websockets", types.ModuleType("starlette.websockets")),
]:
    if _key not in sys.modules:
        sys.modules[_key] = _stub  # type: ignore[assignment]

from backend.reports import _get_weight, render_starting_xi_md  # noqa: E402  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# _get_weight — explicit zero must NOT fall back to default
# ---------------------------------------------------------------------------

class TestGetWeight:
    def test_explicit_zero_returns_zero_not_default(self):
        """float(val or default) would return 0.35 here; _get_weight must return 0.0."""
        result = _get_weight({"fixtures": 0}, "fixtures", 0.35)
        assert result == 0.0, f"expected 0.0, got {result}"

    def test_explicit_zero_float_returns_zero(self):
        result = _get_weight({"form": 0.0}, "form", 0.25)
        assert result == 0.0

    def test_missing_key_uses_default(self):
        result = _get_weight({}, "fixtures", 0.35)
        assert result == 0.35

    def test_normal_value_returned(self):
        result = _get_weight({"xg": 0.5}, "xg", 0.15)
        assert result == 0.5

    def test_none_value_uses_default(self):
        """Explicit None behaves like absent key — falls back to default."""
        result = _get_weight({"fixtures": None}, "fixtures", 0.35)
        assert result == 0.35


# ---------------------------------------------------------------------------
# render_starting_xi_md — DGW opponent accumulation
# ---------------------------------------------------------------------------

def _make_llm_stub() -> MagicMock:
    llm = MagicMock()
    llm.available.return_value = False
    return llm


def _make_roster() -> list:
    """Return a 14-player roster (1 GK + 5 DEF + 5 MID + 3 FWD) so that
    the formation selection logic can find a valid 10-outfield arrangement.
    The GK plays for ARS, which has two fixtures in the test GW.
    """
    roster = []
    roster.append({"element": 1, "name": "GK", "team": "ARS", "position_type": 1, "role": "starter"})
    for i in range(5):
        roster.append({"element": 10 + i, "name": f"DEF{i}", "team": "CHE", "position_type": 2, "role": "starter"})
    for i in range(5):
        roster.append({"element": 20 + i, "name": f"MID{i}", "team": "LIV", "position_type": 3, "role": "starter"})
    for i in range(3):
        roster.append({"element": 30 + i, "name": f"FWD{i}", "team": "MCI", "position_type": 4, "role": "starter"})
    return roster


class TestRenderStartingXiMdDGW:
    """DGW teams must show all opponents joined with ' & ', not just the last one."""

    def _call(self, fixtures_payload):
        summary = {
            "entries": [{"entry_id": 1, "roster": _make_roster()}],
            "gameweek": 1,
        }
        return render_starting_xi_md(
            summary=summary,
            fixtures=fixtures_payload,
            entry_id=1,
            llm=_make_llm_stub(),
            form={},
            fixture_difficulty={},
            weights_in={},
        )

    def test_normal_gw_single_opponent(self):
        """Non-DGW: GK's team (ARS) has exactly one fixture."""
        fixtures = {
            "fixtures": [
                {"team_h_short": "ARS", "team_a_short": "CHE"},
            ]
        }
        _md, out = self._call(fixtures)
        gk = next((s for s in out["starters"] if s["name"] == "GK"), None)
        assert gk is not None, "GK must appear in starters"
        # ARS is HOME so opponent is CHE
        assert gk["opponent"] == "CHE", f"expected 'CHE', got '{gk['opponent']}'"
        assert gk["venue"] == "HOME"

    def test_dgw_both_opponents_shown(self):
        """DGW: GK's team (ARS) plays twice; both opponents must appear."""
        fixtures = {
            "fixtures": [
                {"team_h_short": "ARS", "team_a_short": "MCE"},  # ARS HOME vs MCE
                {"team_h_short": "TOT", "team_a_short": "ARS"},  # ARS AWAY at TOT
            ]
        }
        _md, out = self._call(fixtures)
        gk = next((s for s in out["starters"] if s["name"] == "GK"), None)
        assert gk is not None, "GK must appear in starters"
        opp = gk["opponent"]
        # Before the fix, only "TOT" appeared (second fixture overwrote first).
        assert "MCE" in opp, f"first DGW opponent 'MCE' missing from '{opp}'"
        assert "TOT" in opp, f"second DGW opponent 'TOT' missing from '{opp}'"
        assert " & " in opp, f"opponents should be joined with ' & ', got '{opp}'"

    def test_dgw_both_venues_shown(self):
        """DGW: venue string must contain both HOME and AWAY."""
        fixtures = {
            "fixtures": [
                {"team_h_short": "ARS", "team_a_short": "MCE"},
                {"team_h_short": "TOT", "team_a_short": "ARS"},
            ]
        }
        _md, out = self._call(fixtures)
        gk = next((s for s in out["starters"] if s["name"] == "GK"), None)
        assert gk is not None
        ven = gk["venue"]
        assert "HOME" in ven, f"HOME missing from venue '{ven}'"
        assert "AWAY" in ven, f"AWAY missing from venue '{ven}'"
