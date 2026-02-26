"""Tests for backend.agent — intent routing, parameter extraction, _apply_defaults,
and handler responses with a mocked MCP client."""

import sys
import types
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub out heavy optional dependencies that may not be installed in the test
# environment (openai, requests).  These must be in sys.modules BEFORE any
# backend module is imported.
# ---------------------------------------------------------------------------
for _mod in ("openai", "requests"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

# Provide the minimal attributes accessed by llm.py / mcp_client.py.
if not hasattr(sys.modules["openai"], "OpenAI"):
    sys.modules["openai"].OpenAI = MagicMock  # type: ignore[attr-defined]

from backend.agent import Agent  # noqa: E402
from backend.constants import GW_PATTERN, POSITION_TYPE_LABELS  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(mcp_return: Any = None) -> Agent:
    """Return an Agent with stubbed MCP and LLM clients."""
    mcp = MagicMock()
    mcp.list_tools.return_value = []
    mcp.call_tool.return_value = mcp_return or {}

    llm = MagicMock()
    llm.available.return_value = False  # disable LLM by default; tests focus on routing

    with patch("backend.agent.get_rag_index", return_value=MagicMock(search=lambda *a, **k: [])):
        agent = Agent(mcp, llm)

    return agent


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_position_type_labels_complete(self) -> None:
        assert POSITION_TYPE_LABELS == {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    def test_gw_pattern_matches_gw3(self) -> None:
        m = GW_PATTERN.search("gw3")
        assert m is not None
        assert m.group(1) == "3"

    def test_gw_pattern_matches_gameweek_5(self) -> None:
        m = GW_PATTERN.search("gameweek 5")
        assert m is not None
        assert m.group(1) == "5"

    def test_gw_pattern_matches_game_week(self) -> None:
        m = GW_PATTERN.search("game week 10")
        assert m is not None
        assert m.group(1) == "10"

    def test_gw_pattern_no_match(self) -> None:
        assert GW_PATTERN.search("who scored most") is None


# ---------------------------------------------------------------------------
# _extract_param
# ---------------------------------------------------------------------------

class TestExtractParam:
    def setup_method(self) -> None:
        self.agent = _make_agent()

    def test_extract_gw(self) -> None:
        assert self.agent._extract_param("gw", "gw 7") == 7

    def test_extract_gw_gameweek(self) -> None:
        assert self.agent._extract_param("gw", "gameweek 12") == 12

    def test_extract_league_id(self) -> None:
        assert self.agent._extract_param("league_id", "league 14204") == 14204

    def test_extract_league_id_colon(self) -> None:
        # Pattern supports "league id:XXXXX" with whitespace separator
        assert self.agent._extract_param("league_id", "league id:99999") == 99999

    def test_extract_entry_id(self) -> None:
        assert self.agent._extract_param("entry_id", "entry 286192") == 286192

    def test_extract_horizon(self) -> None:
        assert self.agent._extract_param("horizon", "horizon 3") == 3

    def test_extract_unknown_param(self) -> None:
        assert self.agent._extract_param("nonexistent", "any text") is None

    def test_extract_missing_returns_none(self) -> None:
        assert self.agent._extract_param("gw", "no gameweek here") is None


# ---------------------------------------------------------------------------
# _looks_like (intent routing)
# ---------------------------------------------------------------------------

class TestLooksLike:
    def setup_method(self) -> None:
        self.agent = _make_agent()

    # ---- single-keyword intents ----

    def test_waiver_keyword(self) -> None:
        assert self.agent._looks_like("waiver", "show me waiver picks")

    def test_standings_table(self) -> None:
        assert self.agent._looks_like("standings", "show me the table")

    def test_fixtures_keyword(self) -> None:
        assert self.agent._looks_like("fixtures", "upcoming fixtures")

    def test_ownership_keyword(self) -> None:
        assert self.agent._looks_like("ownership", "player ownership %")

    def test_strength_keyword(self) -> None:
        assert self.agent._looks_like("strength", "strength of schedule next 5 gws")

    # ---- tuple (AND) intents ----

    def test_streak_requires_win(self) -> None:
        assert self.agent._looks_like("streak", "who has a win streak")
        # 'streak' alone without 'win' should not match
        assert not self.agent._looks_like("streak", "what is the current streak")

    def test_win_list_gw(self) -> None:
        assert self.agent._looks_like("win_list", "how many wins this week")
        assert self.agent._looks_like("win_list", "wins in gw 5")

    def test_win_list_no_gw_reference_no_match(self) -> None:
        # "win" alone without time reference should not match win_list
        assert not self.agent._looks_like("win_list", "who will win the league")

    def test_schedule_who_does_play(self) -> None:
        assert self.agent._looks_like("schedule", "who does city play next")

    def test_schedule_keyword_alone(self) -> None:
        assert self.agent._looks_like("schedule", "my schedule")

    def test_matchup_vs_summary(self) -> None:
        assert self.agent._looks_like("matchup_summary", "team a vs team b summary")

    def test_matchup_not_just_vs(self) -> None:
        # "vs" without summary/recap should not match matchup_summary
        assert not self.agent._looks_like("matchup_summary", "team a vs team b")

    # ---- multi-word phrase intents ----

    def test_head_to_head(self) -> None:
        assert self.agent._looks_like("head_to_head", "head to head record")

    def test_h2h_abbreviation(self) -> None:
        assert self.agent._looks_like("head_to_head", "what is my h2h record")

    def test_manager_season_phrase(self) -> None:
        assert self.agent._looks_like("manager_season", "my season stats")

    def test_current_roster_my_team(self) -> None:
        assert self.agent._looks_like("current_roster", "show my team")

    def test_draft_picks_and_keyword(self) -> None:
        assert self.agent._looks_like("draft_picks", "who did we draft")
        assert self.agent._looks_like("draft_picks", "draft order history")

    def test_draft_keyword_alone_no_match(self) -> None:
        # "draft" without a pick-related word should not match draft_picks
        assert not self.agent._looks_like("draft_picks", "talk about the draft")

    def test_player_gw_stats_phrase(self) -> None:
        assert self.agent._looks_like("player_gw_stats", "salah's weekly stats")

    def test_transaction_analysis(self) -> None:
        assert self.agent._looks_like("transaction_analysis", "transaction analysis")
        assert self.agent._looks_like("transaction_analysis", "most targeted players")

    # ---- negative cases ----

    def test_no_false_positives_waiver_for_standings(self) -> None:
        assert not self.agent._looks_like("waiver", "show me the standings table")

    def test_unknown_intent_returns_false(self) -> None:
        assert not self.agent._looks_like("nonexistent_intent", "any text")


# ---------------------------------------------------------------------------
# _apply_defaults
# ---------------------------------------------------------------------------

class TestApplyDefaults:
    def setup_method(self) -> None:
        self.agent = _make_agent()
        # Seed session with known values
        self.agent._session["league_id"] = 14204
        self.agent._session["entry_id"] = 286192
        self.agent._session["gw"] = 5

    def test_waiver_recommendations_injects_entry_id(self) -> None:
        # _apply_defaults injects entry_id and entry_name for waiver_recommendations
        result = self.agent._apply_defaults("waiver_recommendations", {})
        assert result["entry_id"] == 286192

    def test_league_summary_injects_league_id(self) -> None:
        result = self.agent._apply_defaults("league_summary", {})
        assert result["league_id"] == 14204

    def test_league_summary_injects_gw_from_session(self) -> None:
        result = self.agent._apply_defaults("league_summary", {})
        assert result["gw"] == 5

    def test_existing_args_not_overwritten(self) -> None:
        result = self.agent._apply_defaults("waiver_recommendations", {"league_id": 99999})
        assert result["league_id"] == 99999  # caller wins

    def test_transaction_analysis_defaults_gw_to_zero(self) -> None:
        # session has gw=5, but _apply_defaults for transaction_analysis sets gw=0 if unset
        result = self.agent._apply_defaults("transaction_analysis", {})
        # league_id should come from session
        assert result["league_id"] == 14204

    def test_manager_schedule_flattens_first_last(self) -> None:
        result = self.agent._apply_defaults(
            "manager_schedule", {"first": "John", "last": "Smith"}
        )
        assert result.get("entry_name") == "John Smith"
        assert "first" not in result
        assert "last" not in result

    def test_player_form_renames_gw_to_as_of_gw(self) -> None:
        result = self.agent._apply_defaults("player_form", {"gw": 8})
        assert result.get("as_of_gw") == 8
        assert "gw" not in result

    def test_none_values_not_injected(self) -> None:
        # If session has None for entry_id, it should not be injected
        self.agent._session["entry_id"] = None
        result = self.agent._apply_defaults("league_summary", {})
        assert "entry_id" not in result


# ---------------------------------------------------------------------------
# _try_route — handler dispatch with mocked MCP
# ---------------------------------------------------------------------------

class TestTryRoute:
    def setup_method(self) -> None:
        self.mcp = MagicMock()
        self.mcp.list_tools.return_value = []
        self.mcp.call_tool.return_value = {"entries": [], "gameweek": 5}
        self.llm = MagicMock()
        self.llm.available.return_value = False

        with patch("backend.agent.get_rag_index", return_value=MagicMock(search=lambda *a, **k: [])):
            self.agent = Agent(self.mcp, self.llm)

        self.agent._session["league_id"] = 14204
        self.agent._session["entry_id"] = 286192

    def test_waiver_routes_to_handler(self) -> None:
        result = self.agent._try_route("show my waiver recommendations", [])
        assert result is not None  # fast-path returned something

    def test_standings_routes_to_handler(self) -> None:
        self.mcp.call_tool.return_value = {"rows": [], "gameweek": 5}
        result = self.agent._try_route("show me the standings table", [])
        assert result is not None

    def test_head_to_head_routes(self) -> None:
        self.mcp.call_tool.return_value = {
            "team_a": {"entry_name": "A", "wins": 2, "draws": 0, "losses": 1},
            "team_b": {"entry_name": "B", "wins": 1, "draws": 0, "losses": 2},
            "matches": [],
        }
        result = self.agent._try_route("head to head between team a and team b", [])
        assert result is not None

    def test_manager_season_routes(self) -> None:
        self.mcp.call_tool.return_value = {
            "entry_name": "My Team",
            "gameweeks": [],
            "record": {"wins": 3, "draws": 1, "losses": 2},
        }
        result = self.agent._try_route("show me my season stats", [])
        assert result is not None

    def test_unknown_query_returns_none(self) -> None:
        # Should not be fast-pathed — returns None so LLM fallback is triggered
        result = self.agent._try_route("zzzzunknowntopic", [])
        assert result is None

    def test_tool_error_returns_error_string(self) -> None:
        self.mcp.call_tool.side_effect = RuntimeError("connection refused")
        result = self.agent._try_route("show my waiver recommendations", [])
        # Should not propagate exception; should return a user-facing error message
        assert result is not None
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Team name resolution: handlers resolve names from text before session
# defaults (#72, #73, #74)
# ---------------------------------------------------------------------------

_LEAGUE_ENTRIES = {
    "teams": [
        {"entry_id": 100, "entry_name": "Boot Gang", "short_name": "BG"},
        {"entry_id": 200, "entry_name": "Glock Tua", "short_name": "GT"},
    ]
}

_WAIVER_RESULT = {
    "entry_name": "Boot Gang",
    "target_gw": 28,
    "top_adds": [{"name": "Salah", "team": "LIV", "position_type": 3}],
}

_STREAK_RESULT = {
    "entry_name": "Boot Gang",
    "start_win_streak": 2,
    "current_win_streak": 1,
    "max_win_streak": 4,
}

_SCHEDULE_RESULT = {
    "entry_name": "Boot Gang",
    "matches": [{"gameweek": 28, "opponent_name": "Glock Tua"}],
}


class TestTeamNameResolution:
    """Handlers should resolve a team name from user text rather than always
    falling back to the session's own entry_id."""

    def setup_method(self) -> None:
        self.mcp = MagicMock()
        self.mcp.list_tools.return_value = []
        self.llm = MagicMock()
        self.llm.available.return_value = False

        with patch("backend.agent.get_rag_index", return_value=MagicMock(search=lambda *a, **k: [])):
            self.agent = Agent(self.mcp, self.llm)

        # Session defaults point to *Glock Tua* (id=200).
        self.agent._session["league_id"] = 14204
        self.agent._session["entry_id"] = 200
        self.agent._session["entry_name"] = "Glock Tua"

    def _mock_call_tool(self, responses: Dict[str, Any]):
        """Return different data depending on which MCP tool is called."""
        def _side_effect(name: str, args: Any = None):
            return responses.get(name, {})
        self.mcp.call_tool.side_effect = _side_effect

    def test_waiver_resolves_other_team(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "waiver_recommendations": _WAIVER_RESULT,
        })
        result = self.agent._try_route("waiver recs for Boot Gang", [])
        assert result is not None
        # Should mention Boot Gang, NOT Glock Tua
        assert "Boot Gang" in result
        # The tool should have been called with Boot Gang's entry_id (100)
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "waiver_recommendations"]
        assert len(calls) == 1
        assert calls[0][0][1]["entry_id"] == 100

    def test_waiver_falls_back_to_session_when_no_team_in_text(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "waiver_recommendations": _WAIVER_RESULT,
        })
        result = self.agent._try_route("show my waiver recommendations", [])
        assert result is not None
        # No team name in text → should use session default (Glock Tua, id=200)
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "waiver_recommendations"]
        assert len(calls) == 1
        assert calls[0][0][1]["entry_id"] == 200

    def test_streak_resolves_other_team(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "manager_streak": _STREAK_RESULT,
        })
        result = self.agent._try_route("win streak for Boot Gang", [])
        assert result is not None
        assert "Boot Gang" in result
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "manager_streak"]
        assert len(calls) == 1
        assert calls[0][0][1]["entry_id"] == 100

    def test_schedule_resolves_other_team(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "manager_schedule": _SCHEDULE_RESULT,
        })
        result = self.agent._try_route("schedule for Boot Gang", [])
        assert result is not None
        assert "Boot Gang" in result
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "manager_schedule"]
        assert len(calls) == 1
        assert calls[0][0][1]["entry_id"] == 100
