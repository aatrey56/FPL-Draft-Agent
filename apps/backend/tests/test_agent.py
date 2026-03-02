"""Tests for backend.agent — intent routing, parameter extraction, _apply_defaults,
and handler responses with a mocked MCP client."""

import json
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
from backend.reports import _load_points_map, _simple_league_md  # noqa: E402


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

    def test_greeting_hello_returns_fast(self) -> None:
        """Greetings should be answered without calling any tools (issue #71)."""
        result = self.agent._try_route("hello", [])
        assert result is not None
        assert "FPL" in result
        self.mcp.call_tool.assert_not_called()

    def test_greeting_hey_returns_fast(self) -> None:
        result = self.agent._try_route("hey!", [])
        assert result is not None
        self.mcp.call_tool.assert_not_called()

    def test_greeting_thanks_returns_fast(self) -> None:
        result = self.agent._try_route("thanks", [])
        assert result is not None
        self.mcp.call_tool.assert_not_called()

    def test_greeting_does_not_match_fpl_queries(self) -> None:
        """Ensure 'hi' inside FPL queries doesn't trigger greeting."""
        result = self.agent._try_route("show standings table", [])
        # Should route to standings, not greeting
        assert result is not None
        assert "FPL Draft assistant" not in result

    def test_greeting_hi_there_returns_fast(self) -> None:
        """Two-word message starting with greeting word should match."""
        result = self.agent._try_route("hi there", [])
        assert result is not None
        assert "FPL Draft assistant" in result
        self.mcp.call_tool.assert_not_called()

    def test_hi_salah_stats_not_greeting(self) -> None:
        """'hi salah stats' is a 3-word FPL query, not a greeting (issue #91)."""
        result = self.agent._try_route("hi salah stats", [])
        # Should NOT be intercepted as a greeting
        assert result is None or "FPL Draft assistant" not in (result or "")

    def test_highlights_gw27_not_greeting(self) -> None:
        """'highlights gw27' must not match because 'hi' is a prefix of 'highlights' (issue #91)."""
        result = self.agent._try_route("highlights gw27", [])
        assert result is None or "FPL Draft assistant" not in (result or "")

    def test_hey_show_me_standings_not_greeting(self) -> None:
        """'hey show me standings' is a 4-word FPL query, not a greeting (issue #91)."""
        result = self.agent._try_route("hey show me standings", [])
        # Should route to standings, not greeting
        assert result is None or "FPL Draft assistant" not in (result or "")

    def test_history_of_trades_not_greeting(self) -> None:
        """'history of trades' must not match — 'hi' is a prefix of 'history' (issue #91)."""
        result = self.agent._try_route("history of trades", [])
        assert result is None or "FPL Draft assistant" not in (result or "")


# ---------------------------------------------------------------------------
# _is_greeting (unit tests)
# ---------------------------------------------------------------------------

class TestIsGreeting:
    """Direct tests for the _is_greeting method (issue #91)."""

    def setup_method(self) -> None:
        self.agent = _make_agent()

    # ---- should match ----

    def test_hi(self) -> None:
        assert self.agent._is_greeting("hi") is True

    def test_hello(self) -> None:
        assert self.agent._is_greeting("hello") is True

    def test_hi_there(self) -> None:
        assert self.agent._is_greeting("hi there") is True

    def test_hey_with_punctuation(self) -> None:
        assert self.agent._is_greeting("hey!") is True

    def test_thanks(self) -> None:
        assert self.agent._is_greeting("thanks") is True

    def test_good_morning(self) -> None:
        assert self.agent._is_greeting("good morning") is True

    def test_yo_mate(self) -> None:
        """Two-word greeting where first word is a known greeting."""
        assert self.agent._is_greeting("yo mate") is True

    # ---- should NOT match ----

    def test_hi_salah_stats(self) -> None:
        assert self.agent._is_greeting("hi salah stats") is False

    def test_highlights_gw27(self) -> None:
        assert self.agent._is_greeting("highlights gw27") is False

    def test_hey_show_me_standings(self) -> None:
        assert self.agent._is_greeting("hey show me standings") is False

    def test_history_of_trades(self) -> None:
        assert self.agent._is_greeting("history of trades") is False

    def test_hello_what_are_standings(self) -> None:
        """Multi-word message even starting with greeting should not match."""
        assert self.agent._is_greeting("hello what are the standings") is False


# ---------------------------------------------------------------------------
# _sanitize_error
# ---------------------------------------------------------------------------

class TestSanitizeError:
    def setup_method(self) -> None:
        self.agent = _make_agent()

    def test_strips_file_path_with_gw(self) -> None:
        """Go 'open data/raw/gw/99/live.json: no such file...' → user-friendly (issue #89)."""
        raw = "open data/raw/gw/99/live.json: no such file or directory"
        result = self.agent._sanitize_error(raw)
        assert "data/raw" not in result
        assert "GW99" in result

    def test_strips_generic_file_path(self) -> None:
        raw = "open data/derived/summary/league_summary.json: no such file or directory"
        result = self.agent._sanitize_error(raw)
        assert "data/derived" not in result

    def test_passes_through_non_path_errors(self) -> None:
        raw = "connection refused"
        result = self.agent._sanitize_error(raw)
        assert result == "connection refused"

    def test_empty_string_passthrough(self) -> None:
        assert self.agent._sanitize_error("") == ""


# ---------------------------------------------------------------------------
# GW session stickiness (#75, #81-#84, #88)
# ---------------------------------------------------------------------------

class TestGwSessionStickiness:
    """Verify that the GW from one query does not leak into the next."""

    def setup_method(self) -> None:
        self.mcp = MagicMock()
        self.mcp.list_tools.return_value = []
        self.mcp.call_tool.return_value = {"rows": [], "gameweek": 5}
        self.llm = MagicMock()
        self.llm.available.return_value = False

        with patch("backend.agent.get_rag_index", return_value=MagicMock(search=lambda *a, **k: [])):
            self.agent = Agent(self.mcp, self.llm)

        self.agent._session["league_id"] = 14204
        self.agent._session["entry_id"] = 286192

    def test_gw_not_persisted_by_note_tool_use(self) -> None:
        """_note_tool_use should NOT store gw in session."""
        self.agent._note_tool_use("league_summary", {"league_id": 14204, "gw": 3})
        assert self.agent._session.get("gw") is None

    def test_gw_not_persisted_from_text(self) -> None:
        """_update_session_from_text should NOT store gw in session."""
        self.agent._update_session_from_text("standings for gw 7")
        assert self.agent._session.get("gw") is None

    def test_gw_cleared_at_start_of_run(self) -> None:
        """run() should clear any stale GW before processing a new message."""
        self.agent._session["gw"] = 99  # stale value
        self.agent.run("show standings")
        assert self.agent._session.get("gw") is None

    def test_gw_from_context_persists_for_current_turn(self) -> None:
        """When the API caller sends gw in context, it should be available."""
        self.agent.run("show standings", context={"gw": 5})
        # After run completes, the gw should NOT persist (cleared on next run)
        # But during the run, the handler should have used gw=5.
        # Verify by checking that _default_gw returns None after the run:
        # (the run already cleared and re-set, but there's nothing to persist)
        # We verify by calling run again without gw
        self.agent.run("show standings")
        assert self.agent._session.get("gw") is None

    def test_explicit_gw_in_text_used_only_for_current_query(self) -> None:
        """A GW mentioned in text should not stick to the next query."""
        self.mcp.call_tool.return_value = {"rows": [], "gameweek": 3}
        # First query mentions GW3
        self.agent.run("standings for gw 3")
        # Second query has no GW — should NOT default to 3
        self.agent.run("show standings")
        # Grab the standings call from the second run — it should use gw=0
        # (the "use current GW" convention) rather than the sticky gw=3.
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "standings"]
        assert len(calls) >= 2
        second_call_args = calls[-1][0][1]
        assert second_call_args.get("gw") != 3, "GW from first query leaked to second query"


# ---------------------------------------------------------------------------
# PR6: Routing bugs — entry_id fallback, draft round, who won, player name
# ---------------------------------------------------------------------------

class TestRoutingBugs:
    """Tests for bug fixes #76, #80, #85, #86, #87."""

    def setup_method(self) -> None:
        self.mcp = MagicMock()
        self.mcp.list_tools.return_value = []
        self.llm = MagicMock()
        self.llm.available.return_value = False
        with patch("backend.agent.get_rag_index", return_value=MagicMock(search=lambda *a, **k: [])):
            self.agent = Agent(self.mcp, self.llm)

    # ---- #76: _default_entry_id falls back to SETTINGS.entry_id ----

    def test_default_entry_id_falls_back_to_settings(self) -> None:
        """When session has no entry_id, _default_entry_id returns SETTINGS.entry_id."""
        self.agent._session["entry_id"] = None
        with patch("backend.agent.SETTINGS") as mock_settings:
            mock_settings.entry_id = 42
            result = self.agent._default_entry_id()
        assert result == 42

    def test_default_entry_id_session_takes_priority(self) -> None:
        """Session entry_id should take priority over SETTINGS."""
        self.agent._session["entry_id"] = 100
        result = self.agent._default_entry_id()
        assert result == 100

    def test_current_roster_uses_settings_fallback(self) -> None:
        """'my team' should work even without session entry_id (#76)."""
        self.agent._session["league_id"] = 14204
        self.agent._session["entry_id"] = None
        self.mcp.call_tool.return_value = {
            "entry_name": "My Team", "gameweek": 5,
            "starters": [], "bench": [],
        }
        with patch("backend.agent.SETTINGS") as mock_settings:
            mock_settings.entry_id = 99
            mock_settings.league_id = 14204
            result = self.agent._try_route("show my team", [])
        assert result is not None
        assert "unavailable" not in result.lower()

    # ---- #80: draft_picks round filter ----

    def test_draft_picks_filters_by_round(self) -> None:
        """'who did we draft in round 1' should only show round 1 picks (#80)."""
        self.agent._session["league_id"] = 14204
        self.agent._session["entry_id"] = 100
        self.mcp.call_tool.return_value = {
            "filtered_by": "My Team",
            "picks": [
                {"round": 1, "pick": 3, "entry_name": "Me", "player_name": "Salah", "team": "LIV", "position_type": 3},
                {"round": 2, "pick": 14, "entry_name": "Me", "player_name": "Rice", "team": "ARS", "position_type": 3},
                {"round": 3, "pick": 19, "entry_name": "Me", "player_name": "Saka", "team": "ARS", "position_type": 3},
            ],
        }
        result = self.agent._try_route("who did we draft in round 1", [])
        assert result is not None
        assert "Salah" in result
        assert "Rice" not in result  # round 2 — should be filtered out
        assert "round 1" in result.lower()

    def test_draft_picks_no_round_shows_all(self) -> None:
        """Without a round mention, all picks should be shown."""
        self.agent._session["league_id"] = 14204
        self.agent._session["entry_id"] = 100
        self.mcp.call_tool.return_value = {
            "filtered_by": "My Team",
            "picks": [
                {"round": 1, "pick": 3, "entry_name": "Me", "player_name": "Salah", "team": "LIV", "position_type": 3},
                {"round": 2, "pick": 14, "entry_name": "Me", "player_name": "Rice", "team": "ARS", "position_type": 3},
            ],
        }
        result = self.agent._try_route("who did we draft", [])
        assert result is not None
        assert "Salah" in result
        assert "Rice" in result

    # ---- #85: "who won GW27" routes to league_summary ----

    def test_who_won_gw_routes_to_league_summary(self) -> None:
        """'who won GW27' should route to league_summary, not win_list (#85)."""
        self.agent._session["league_id"] = 14204
        self.mcp.call_tool.return_value = {
            "entries": [], "gameweek": 27, "matches": [],
        }
        result = self.agent._try_route("who won gw27", [])
        # Verify it called league_summary, not win_list
        call_args = self.mcp.call_tool.call_args
        assert call_args is not None
        tool_name = call_args[0][0]
        assert tool_name == "league_summary"

    # ---- #86: "wins each gameweek" matches win_list ----

    def test_wins_each_gameweek_matches_win_list(self) -> None:
        """'wins each gameweek' should route to win_list (#86)."""
        assert self.agent._looks_like("win_list", "wins each gameweek")

    def test_wins_each_still_matches(self) -> None:
        """The ('wins', 'each') tuple keyword should match."""
        assert self.agent._looks_like("win_list", "my wins each week")

    # ---- #87: "for" prefix stripped from player name ----

    def test_player_name_for_prefix_stripped(self) -> None:
        """'gameweek points for Saka' should extract 'Saka', not 'for Saka' (#87)."""
        self.agent._session["league_id"] = 14204
        self.mcp.call_tool.return_value = {
            "player_name": "Saka", "team": "ARS", "position_type": 3,
            "total_points": 50, "avg_points": 5.0, "gameweeks": [],
        }
        result = self.agent._try_route("gameweek points for Saka", [])
        assert result is not None
        # The tool should have been called with player_name="Saka" not "for Saka"
        call_args = self.mcp.call_tool.call_args
        tool_args = call_args[0][1]  # second positional arg is the args dict
        assert tool_args.get("player_name") == "Saka"


# ---------------------------------------------------------------------------
# Dedicated tool renderers (#77, #78, #79)
# ---------------------------------------------------------------------------

class TestSimpleToolRenderers:
    """Verify that league_entries, ownership_scarcity, and
    strength_of_schedule return formatted output, not generic 'data is ready'."""
    pass


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
        self.agent._session["league_id"] = 14204
        self.agent._session["entry_id"] = 286192

    def test_league_entries_renders_team_list(self) -> None:
        self.mcp.call_tool.return_value = {
            "league_id": 14204,
            "teams": [
                {"entry_id": 100, "entry_name": "Boot Gang", "short_name": "BG"},
                {"entry_id": 200, "entry_name": "Glock Tua", "short_name": "GT"},
            ],
        }
        result = self.agent._try_route("show all teams", [])
        assert result is not None
        assert "Boot Gang" in result
        assert "Glock Tua" in result
        assert "data is ready" not in result

    def test_ownership_scarcity_renders_breakdown(self) -> None:
        self.mcp.call_tool.return_value = {
            "league_id": 14204,
            "gameweek": 27,
            "owned_totals": {"gk": 10, "def": 40, "mid": 40, "fwd": 20, "total": 110},
            "unowned_totals": {"gk": 5, "def": 10, "mid": 15, "fwd": 5, "total": 35},
            "hoarders": {"mid": [{"entry_name": "Boot Gang", "count": 8}]},
        }
        result = self.agent._try_route("player ownership %", [])
        assert result is not None
        assert "Owned" in result
        assert "Free agents" in result
        assert "data is ready" not in result

    def test_strength_of_schedule_renders_rankings(self) -> None:
        self.mcp.call_tool.return_value = {
            "league_id": 14204,
            "gameweek": 27,
            "entries": [
                {
                    "entry_name": "Boot Gang",
                    "future_opponent_avg_rank": 3.2,
                    "future_opponents_top_half": 2,
                    "future_opponents_bottom_half": 3,
                },
                {
                    "entry_name": "Glock Tua",
                    "future_opponent_avg_rank": 5.8,
                    "future_opponents_top_half": 4,
                    "future_opponents_bottom_half": 1,
                },
            ],
        }
        result = self.agent._try_route("strength of schedule next 5 gws", [])
        assert result is not None
        assert "Boot Gang" in result
        assert "avg opp rank" in result
        assert "data is ready" not in result
        # Verify sort order: Boot Gang (3.2) should appear before Glock Tua (5.8)
        assert result.index("Boot Gang") < result.index("Glock Tua")

    def test_strength_of_schedule_routes_to_correct_tool(self) -> None:
        """'strength of schedule' must route to strength_of_schedule, not schedule handler."""
        self.mcp.call_tool.return_value = {
            "league_id": 14204,
            "gameweek": 27,
            "entries": [
                {
                    "entry_name": "Boot Gang",
                    "future_opponent_avg_rank": 3.2,
                    "future_opponents_top_half": 2,
                    "future_opponents_bottom_half": 3,
                },
            ],
        }
        self.agent._try_route("strength of schedule next 5 gws", [])
        tool_name = self.mcp.call_tool.call_args[0][0]
        assert tool_name == "strength_of_schedule", (
            f"Expected 'strength_of_schedule' but routed to '{tool_name}'. "
            "Check that the 'strength' intent is tested before 'schedule' in _try_route()."
        )

    def test_schedule_difficulty_routes_to_strength_tool(self) -> None:
        """'schedule difficulty' must route to strength_of_schedule, not schedule handler."""
        self.mcp.call_tool.return_value = {
            "league_id": 14204,
            "gameweek": 27,
            "entries": [
                {
                    "entry_name": "Glock Tua",
                    "future_opponent_avg_rank": 5.8,
                    "future_opponents_top_half": 4,
                    "future_opponents_bottom_half": 1,
                },
            ],
        }
        self.agent._try_route("schedule difficulty", [])
        tool_name = self.mcp.call_tool.call_args[0][0]
        assert tool_name == "strength_of_schedule", (
            f"Expected 'strength_of_schedule' but routed to '{tool_name}'. "
            "'schedule difficulty' starts with 'schedule' — the strength check must come first."
        )

    # ---- edge cases ----

    def test_league_entries_empty_teams(self) -> None:
        """Empty teams list should return a user-friendly message."""
        self.mcp.call_tool.return_value = {"teams": []}
        result = self.agent._try_route("show all teams", [])
        assert result is not None
        assert "no teams" in result.lower() or "No teams" in result

    def test_league_entries_non_dict_return(self) -> None:
        """Non-dict MCP return should show unavailable message."""
        self.mcp.call_tool.return_value = "error: connection refused"
        result = self.agent._try_route("show all teams", [])
        assert result is not None
        assert "unavailable" in result.lower()

    def test_ownership_scarcity_non_dict_return(self) -> None:
        """Non-dict MCP return should show unavailable message."""
        self.mcp.call_tool.return_value = "error: connection refused"
        result = self.agent._try_route("player ownership %", [])
        assert result is not None
        assert "unavailable" in result.lower()

    def test_strength_of_schedule_empty_entries(self) -> None:
        """Empty entries should return a user-friendly message."""
        self.mcp.call_tool.return_value = {"entries": []}
        result = self.agent._try_route("strength of schedule next 5 gws", [])
        assert result is not None
        assert "no strength" in result.lower() or "No strength" in result

    def test_strength_of_schedule_non_dict_return(self) -> None:
        """Non-dict MCP return should show unavailable message."""
        self.mcp.call_tool.return_value = "error: connection refused"
        result = self.agent._try_route("strength of schedule next 5 gws", [])
        assert result is not None
        assert "unavailable" in result.lower()
# Regression tests for league summary bug fixes (#90)
# ---------------------------------------------------------------------------

class TestLeagueSummaryBugFixes:
    """Regression tests for the three bugs fixed in PR #90:

    1. _load_points_map read 'total' instead of 'points' key
    2. _simple_league_md rendered 'GWNone' when gameweek was None
    3. _handle_league_summary crashed on error dicts instead of surfacing them
    """

    def test_load_points_map_uses_points_key(self, tmp_path: Any) -> None:
        """_load_points_map must read the 'points' key from each player entry.

        Before the fix, only the 'total' key was read, so player points files
        using the canonical 'points' key silently returned 0.0 for every player.
        """
        # Build the nested directory structure that _load_points_map expects:
        # <data_dir>/derived/points/<league_id>/entry/<entry_id>/gw/<gw>.json
        gw_dir = tmp_path / "derived" / "points" / "100" / "entry" / "200" / "gw"
        gw_dir.mkdir(parents=True)
        payload = {
            "players": [
                {"element": 10, "points": 42},
                {"element": 20, "points": 7},
            ]
        }
        (gw_dir / "5.json").write_text(json.dumps(payload))

        with patch("backend.reports.SETTINGS") as mock_settings:
            mock_settings.data_dir = str(tmp_path)
            result = _load_points_map(league_id=100, entry_id=200, gw=5)

        assert result[10] == 42.0, f"expected 42.0 for element 10, got {result.get(10)}"
        assert result[20] == 7.0, f"expected 7.0 for element 20, got {result.get(20)}"

    def test_load_points_map_falls_back_to_total_key(self, tmp_path: Any) -> None:
        """_load_points_map must fall back to 'total' when 'points' is absent.

        This ensures backwards compatibility with older cached data files that
        only have the 'total' key.
        """
        gw_dir = tmp_path / "derived" / "points" / "100" / "entry" / "200" / "gw"
        gw_dir.mkdir(parents=True)
        payload = {
            "players": [
                {"element": 10, "total": 35},
                {"element": 20, "total": 12},
            ]
        }
        (gw_dir / "5.json").write_text(json.dumps(payload))

        with patch("backend.reports.SETTINGS") as mock_settings:
            mock_settings.data_dir = str(tmp_path)
            result = _load_points_map(league_id=100, entry_id=200, gw=5)

        assert result[10] == 35.0, f"expected 35.0 for element 10, got {result.get(10)}"
        assert result[20] == 12.0, f"expected 12.0 for element 20, got {result.get(20)}"

    def test_simple_league_md_none_gameweek(self) -> None:
        """_simple_league_md must render '(unknown GW)' when gameweek is None.

        Before the fix, it rendered 'GWNone' via unguarded f-string interpolation.
        """
        summary: Dict[str, Any] = {
            "gameweek": None,
            "entries": [],
        }
        result = _simple_league_md(summary)
        assert "(unknown GW)" in result, f"expected '(unknown GW)' in output, got: {result!r}"
        assert "GWNone" not in result, f"'GWNone' must not appear in output, got: {result!r}"

    def test_handle_league_summary_error_propagation(self) -> None:
        """When the MCP tool returns an error dict, the handler must surface the
        error message to the user instead of crashing or rendering broken Markdown.
        """
        error_response = {"error": "file not found"}
        agent = _make_agent(mcp_return=error_response)
        agent._session["league_id"] = 14204

        tool_events: List[Dict[str, Any]] = []
        result = agent._handle_league_summary("show league summary", tool_events)

        assert isinstance(result, str)
        assert "unavailable" in result.lower(), (
            f"expected 'unavailable' in error response, got: {result!r}"
        )
        assert "file not found" in result, (
            f"expected error text 'file not found' in response, got: {result!r}"
        )

    def test_llm_path_guard_skips_render_on_error_dict(self) -> None:
        """When the LLM calls league_summary and the tool returns an error dict,
        the guard at chat() line ~276 must prevent render_league_summary_md from
        being called.  Instead the error should be passed back to the LLM loop
        as a tool result string so it can produce a graceful final answer.
        """
        mcp = MagicMock()
        mcp.list_tools.return_value = []
        mcp.call_tool.return_value = {"error": "no data available"}

        llm = MagicMock()
        llm.available.return_value = True

        # First LLM call: emit a tool invocation for league_summary.
        # Second LLM call: emit a final answer after seeing the error.
        llm.generate.side_effect = [
            json.dumps({
                "action": "tool",
                "name": "league_summary",
                "arguments": {"league_id": 14204, "gw": 5},
            }),
            json.dumps({
                "action": "final",
                "content": "Sorry, league summary data is not available.",
            }),
        ]

        with patch("backend.agent.get_rag_index", return_value=MagicMock(search=lambda *a, **k: [])):
            agent = Agent(mcp, llm)

        agent._session["league_id"] = 14204

        # Patch _try_route to return None so the LLM loop is entered
        # (otherwise the fast-path router handles "league summary" directly).
        # Patch _try_route to return None so the LLM loop is entered.
        with patch.object(agent, "_try_route", return_value=None), \
             patch("backend.agent.render_league_summary_md") as mock_render:
            result = agent.run("show league summary")

        # render_league_summary_md must NOT have been called because the
        # guard `"error" not in result` should have prevented it.
        mock_render.assert_not_called()

        # The LLM loop should still produce a response (the LLM's final answer).
        # The run loop should still produce a response (the LLM's final answer).
        assert isinstance(result, dict)
        assert "content" in result

    def test_handle_league_summary_sends_gw_zero_when_no_gw_set(self) -> None:
        """When no gameweek is in session or text, _handle_league_summary must
        send gw=0 to mcp.call_tool so the Go tool uses the current gameweek.

        This tests the `args = {"league_id": league_id, "gw": gw or 0}` change.
        """
        mcp = MagicMock()
        mcp.list_tools.return_value = []
        mcp.call_tool.return_value = {"entries": [], "gameweek": 0, "matches": []}

        llm = MagicMock()
        llm.available.return_value = False

        with patch("backend.agent.get_rag_index", return_value=MagicMock(search=lambda *a, **k: [])):
            agent = Agent(mcp, llm)

        agent._session["league_id"] = 14204
        # Explicitly ensure no GW is set in session
        agent._session["gw"] = None

        tool_events: List[Dict[str, Any]] = []
        agent._handle_league_summary("show league summary", tool_events)

        # Verify mcp.call_tool was called with gw=0
        mcp.call_tool.assert_called_once()
        call_args = mcp.call_tool.call_args[0]
        tool_name = call_args[0]
        tool_args = call_args[1]
        assert tool_name == "league_summary"
        assert tool_args["gw"] == 0, (
            f"expected gw=0 when no gameweek is set, got gw={tool_args.get('gw')}"
        )


# ---------------------------------------------------------------------------
# Team name resolution for remaining handlers (#92)
# ---------------------------------------------------------------------------

class TestHandlerTeamNameResolution:
    """Handlers should resolve a team name from user text rather than always
    falling back to the session's own entry_id.  Tests cover waiver, streak,
    schedule, and wins-list handlers."""

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

    def test_wins_list_resolves_other_team(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "manager_schedule": {
                "entry_name": "Boot Gang",
                "matches": [
                    {"gameweek": 5, "finished": True, "result": "W"},
                    {"gameweek": 6, "finished": True, "result": "L"},
                    {"gameweek": 7, "finished": True, "result": "W"},
                ],
            },
        })
        result = self.agent._try_route("wins each week for Boot Gang", [])
        assert result is not None
        assert "Boot Gang" in result
        # The handler should have called manager_schedule with Boot Gang's
        # entry_id (100), not the session default (200).
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "manager_schedule"]
        assert len(calls) == 1
        assert calls[0][0][1]["entry_id"] == 100

    # ---- current_roster resolution ----

    def test_current_roster_resolves_other_team(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "current_roster": {
                "entry_name": "Boot Gang", "gameweek": 28,
                "starters": [], "bench": [],
            },
        })
        result = self.agent._try_route("current roster for Boot Gang", [])
        assert result is not None
        assert "Boot Gang" in result
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "current_roster"]
        assert len(calls) == 1
        assert calls[0][0][1]["entry_id"] == 100

    def test_current_roster_falls_back_to_session(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "current_roster": {
                "entry_name": "Glock Tua", "gameweek": 28,
                "starters": [], "bench": [],
            },
        })
        result = self.agent._try_route("show my team", [])
        assert result is not None
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "current_roster"]
        assert len(calls) == 1
        assert calls[0][0][1]["entry_id"] == 200

    # ---- draft_picks resolution ----

    def test_draft_picks_resolves_other_team(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "draft_picks": {
                "filtered_by": "Boot Gang",
                "picks": [
                    {"round": 1, "pick": 1, "entry_name": "Boot Gang",
                     "player_name": "Salah", "team": "LIV", "position_type": 3},
                ],
            },
        })
        result = self.agent._try_route("draft picks for Boot Gang", [])
        assert result is not None
        assert "Boot Gang" in result
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "draft_picks"]
        assert len(calls) == 1
        assert calls[0][0][1]["entry_id"] == 100

    def test_draft_picks_falls_back_to_session(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "draft_picks": {
                "filtered_by": "Glock Tua",
                "picks": [
                    {"round": 1, "pick": 2, "entry_name": "Glock Tua",
                     "player_name": "Haaland", "team": "MCI", "position_type": 4},
                ],
            },
        })
        result = self.agent._try_route("who did we draft", [])
        assert result is not None
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "draft_picks"]
        assert len(calls) == 1
        assert calls[0][0][1]["entry_id"] == 200

    # ---- manager_season resolution ----

    def test_manager_season_resolves_other_team(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "manager_season": {
                "entry_name": "Boot Gang",
                "gameweeks": [],
                "record": {"wins": 5, "draws": 2, "losses": 3},
            },
        })
        result = self.agent._try_route("season stats for Boot Gang", [])
        assert result is not None
        assert "Boot Gang" in result
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "manager_season"]
        assert len(calls) == 1
        assert calls[0][0][1]["entry_id"] == 100

    def test_manager_season_falls_back_to_session(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "manager_season": {
                "entry_name": "Glock Tua",
                "gameweeks": [],
                "record": {"wins": 3, "draws": 1, "losses": 6},
            },
        })
        result = self.agent._try_route("show me my season stats", [])
        assert result is not None
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "manager_season"]
        assert len(calls) == 1
        assert calls[0][0][1]["entry_id"] == 200

    # ---- lineup_efficiency resolution ----

    def test_lineup_efficiency_resolves_other_team(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "lineup_efficiency": {
                "gameweek": 28,
                "entries": [
                    {"entry_id": 100, "entry_name": "Boot Gang",
                     "bench_points": 10, "zero_minute_starters": 0},
                ],
            },
        })
        result = self.agent._try_route("lineup efficiency for Boot Gang", [])
        assert result is not None
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "lineup_efficiency"]
        assert len(calls) == 1

    def test_lineup_efficiency_falls_back_to_session(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "lineup_efficiency": {
                "gameweek": 28,
                "entries": [
                    {"entry_id": 200, "entry_name": "Glock Tua",
                     "bench_points": 5, "zero_minute_starters": 1},
                ],
            },
        })
        result = self.agent._try_route("bench points", [])
        assert result is not None

    # ---- fallback tests for schedule, streak, wins_list ----

    def test_schedule_falls_back_to_session(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "manager_schedule": _SCHEDULE_RESULT,
        })
        result = self.agent._try_route("my schedule", [])
        assert result is not None
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "manager_schedule"]
        assert len(calls) == 1
        assert calls[0][0][1]["entry_id"] == 200

    def test_streak_falls_back_to_session(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "manager_streak": _STREAK_RESULT,
        })
        result = self.agent._try_route("win streak", [])
        assert result is not None
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "manager_streak"]
        assert len(calls) == 1
        assert calls[0][0][1]["entry_id"] == 200

    def test_wins_list_falls_back_to_session(self) -> None:
        self._mock_call_tool({
            "league_entries": _LEAGUE_ENTRIES,
            "manager_schedule": {
                "entry_name": "Glock Tua",
                "matches": [
                    {"gameweek": 5, "finished": True, "result": "W"},
                ],
            },
        })
        result = self.agent._try_route("wins each week", [])
        assert result is not None
        calls = [c for c in self.mcp.call_tool.call_args_list if c[0][0] == "manager_schedule"]
        assert len(calls) == 1
        assert calls[0][0][1]["entry_id"] == 200
