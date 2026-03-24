"""Comprehensive handler output tests for backend.agent.

Verifies every _handle_* method with valid tool results and error conditions.
Complements test_agent.py (which covers routing/extraction) with handler
output format and rendering correctness.
"""

import sys
import types
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub heavy optional deps before importing backend modules.
# ---------------------------------------------------------------------------
for _mod in ("openai", "requests"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)
if not hasattr(sys.modules["openai"], "OpenAI"):
    sys.modules["openai"].OpenAI = MagicMock  # type: ignore[attr-defined]

from backend.agent import Agent  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _agent(tool_return: Any = None) -> Agent:
    """Agent with session defaults (league_id, entry_id) and mocked MCP."""
    mcp = MagicMock()
    mcp.list_tools.return_value = []
    mcp.call_tool.return_value = tool_return or {}
    llm = MagicMock()
    llm.available.return_value = False
    with patch("backend.agent.get_rag_index", return_value=MagicMock(search=lambda *a, **k: [])):
        agent = Agent(mcp, llm)
    agent._session["league_id"] = 14204
    agent._session["entry_id"] = 286192
    agent._session["entry_name"] = "Boot Gang"
    return agent


def _agent_with_side_effect(side_effect) -> Agent:
    """Agent whose mcp.call_tool uses a function-based side_effect."""
    mcp = MagicMock()
    mcp.list_tools.return_value = []
    mcp.call_tool.side_effect = side_effect
    llm = MagicMock()
    llm.available.return_value = False
    with patch("backend.agent.get_rag_index", return_value=MagicMock(search=lambda *a, **k: [])):
        agent = Agent(mcp, llm)
    agent._session["league_id"] = 14204
    agent._session["entry_id"] = 286192
    agent._session["entry_name"] = "Boot Gang"
    return agent


_TEAMS = [
    {"entry_id": 100, "entry_name": "Boot Gang", "short_name": "BG"},
    {"entry_id": 200, "entry_name": "Glock Tua", "short_name": "GT"},
]


# ---------------------------------------------------------------------------
# _handle_current_roster
# ---------------------------------------------------------------------------

class TestHandleCurrentRoster:

    def test_renders_starters_and_bench(self) -> None:
        a = _agent({
            "entry_name": "Boot Gang",
            "gameweek": 28,
            "starters": [
                {"name": "Salah", "team": "LIV", "position_type": 3,
                 "position_slot": 1, "is_captain": True, "is_vice_captain": False},
                {"name": "Haaland", "team": "MCI", "position_type": 4,
                 "position_slot": 2, "is_captain": False, "is_vice_captain": True},
            ],
            "bench": [
                {"name": "Saka", "team": "ARS", "position_type": 3,
                 "position_slot": 12, "is_captain": False, "is_vice_captain": False},
            ],
        })
        result = a._handle_current_roster("show my team", [])
        assert "Boot Gang" in result
        assert "GW28" in result
        assert "Salah" in result
        assert "©" in result
        assert "Haaland" in result
        assert "(vc)" in result
        assert "Bench" in result
        assert "Saka" in result

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_current_roster("show my team", [])
        assert "unavailable" in result.lower()

    def test_empty_bench_omits_section(self) -> None:
        a = _agent({
            "gameweek": 28,
            "starters": [{"name": "Salah", "team": "LIV", "position_type": 3,
                          "position_slot": 1, "is_captain": False, "is_vice_captain": False}],
            "bench": [],
        })
        result = a._handle_current_roster("show my team", [])
        assert "Bench" not in result


# ---------------------------------------------------------------------------
# _handle_draft_picks
# ---------------------------------------------------------------------------

class TestHandleDraftPicks:

    def test_renders_picks(self) -> None:
        a = _agent({
            "filtered_by": "all",
            "picks": [
                {"round": 1, "pick": 3, "entry_name": "Boot Gang",
                 "player_name": "Haaland", "team": "MCI", "position_type": 4, "was_auto": False},
                {"round": 2, "pick": 10, "entry_name": "Glock Tua",
                 "player_name": "Salah", "team": "LIV", "position_type": 3, "was_auto": True},
            ],
        })
        result = a._handle_draft_picks("draft history", [])
        assert "Haaland" in result
        assert "Salah" in result
        assert "Rd1" in result
        assert "(auto)" in result

    def test_round_filter(self) -> None:
        a = _agent({
            "filtered_by": "Boot Gang",
            "picks": [
                {"round": 1, "pick": 3, "entry_name": "Boot Gang",
                 "player_name": "Haaland", "team": "MCI", "position_type": 4, "was_auto": False},
                {"round": 2, "pick": 10, "entry_name": "Boot Gang",
                 "player_name": "Salah", "team": "LIV", "position_type": 3, "was_auto": False},
            ],
        })
        result = a._handle_draft_picks("draft picks round 1", [])
        assert "Haaland" in result
        assert "Salah" not in result
        assert "round 1" in result

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_draft_picks("draft picks", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_manager_season
# ---------------------------------------------------------------------------

class TestHandleManagerSeason:

    def test_renders_season_summary(self) -> None:
        a = _agent({
            "entry_name": "Boot Gang",
            "record": {"wins": 15, "draws": 3, "losses": 7},
            "total_points": 1450,
            "avg_score": 58.0,
            "highest_scoring_gw": 12,
            "highest_score": 85,
            "lowest_scoring_gw": 5,
            "lowest_score": 32,
            "gameweeks": [
                {"gameweek": 1, "finished": True, "result": "W",
                 "score": 65, "opponent_score": 50, "opponent_name": "Glock Tua"},
            ],
        })
        result = a._handle_manager_season("season stats", [])
        assert "Boot Gang" in result
        assert "15W" in result
        assert "3D" in result
        assert "1450" in result
        assert "58.0" in result
        assert "GW12" in result
        assert "65-50" in result

    def test_skips_unfinished_gameweeks(self) -> None:
        a = _agent({
            "record": {"wins": 0, "draws": 0, "losses": 0},
            "total_points": 0,
            "avg_score": 0,
            "highest_scoring_gw": "?",
            "highest_score": 0,
            "lowest_scoring_gw": "?",
            "lowest_score": 0,
            "gameweeks": [
                {"gameweek": 30, "finished": False, "result": "", "score": 0,
                 "opponent_score": 0, "opponent_name": "Glock Tua"},
            ],
        })
        result = a._handle_manager_season("season stats", [])
        assert "GW30" not in result

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_manager_season("season stats", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_transaction_analysis
# ---------------------------------------------------------------------------

class TestHandleTransactionAnalysis:

    def test_renders_analysis(self) -> None:
        a = _agent({
            "gameweek": 28,
            "total_transactions": 12,
            "position_breakdown": {
                "MID": {"added": 4, "dropped": 3},
                "FWD": {"added": 2, "dropped": 1},
            },
            "top_added": [{"player_name": "Palmer", "team": "CHE", "count": 3}],
            "top_dropped": [{"player_name": "Werner", "team": "TOT", "count": 2}],
        })
        result = a._handle_transaction_analysis("transaction analysis", [])
        assert "GW28" in result
        assert "12 total" in result
        assert "Palmer" in result
        assert "Werner" in result
        assert "+4 added" in result

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_transaction_analysis("transaction analysis", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_player_gw_stats
# ---------------------------------------------------------------------------

class TestHandlePlayerGwStats:

    def test_renders_stats_with_goals(self) -> None:
        a = _agent({
            "player_name": "Salah",
            "team": "LIV",
            "position_type": 3,
            "total_points": 180,
            "avg_points": 7.2,
            "gameweeks": [
                {"gameweek": 27, "minutes": 90, "points": 12,
                 "goals_scored": 1, "assists": 1,
                 "expected_goals": 0.85, "expected_assists": 0.45},
            ],
        })
        result = a._handle_player_gw_stats("Salah stats each week", [])
        assert "Salah" in result
        assert "LIV" in result
        assert "180" in result
        assert "7.2" in result
        assert "GW27" in result
        assert "1G" in result
        assert "1A" in result
        assert "xG:0.85" in result

    def test_no_goals_no_extras(self) -> None:
        a = _agent({
            "player_name": "Son",
            "team": "TOT",
            "position_type": 3,
            "total_points": 30,
            "avg_points": 3.0,
            "gameweeks": [
                {"gameweek": 27, "minutes": 90, "points": 2,
                 "goals_scored": 0, "assists": 0,
                 "expected_goals": 0.0, "expected_assists": 0.0},
            ],
        })
        result = a._handle_player_gw_stats("Son stats each week", [])
        assert "[" not in result.split("GW27")[1].split("\n")[0]  # no extras bracket

    def test_player_not_found(self) -> None:
        a = _agent({"error": "player not found: Xhaka"})
        result = a._handle_player_gw_stats("Xhaka stats each week", [])
        assert "couldn't find" in result.lower()

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_player_gw_stats("Salah stats", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_head_to_head
# ---------------------------------------------------------------------------

class TestHandleHeadToHead:

    @staticmethod
    def _h2h_side_effect(name: str, args: Dict[str, Any]) -> Any:
        if name == "league_entries":
            return {"teams": _TEAMS}
        if name == "head_to_head":
            return {
                "team_a": {"entry_name": "Boot Gang", "wins": 5, "draws": 2, "losses": 3},
                "team_b": {"entry_name": "Glock Tua", "wins": 3, "draws": 2, "losses": 5},
                "matches": [
                    {"gameweek": 1, "finished": True, "score_a": 65, "score_b": 50, "result_a": "W"},
                    {"gameweek": 5, "finished": True, "score_a": 40, "score_b": 55, "result_a": "L"},
                ],
            }
        return {}

    def test_renders_record_and_matches(self) -> None:
        a = _agent_with_side_effect(self._h2h_side_effect)
        result = a._handle_head_to_head("Boot Gang vs Glock Tua", [])
        assert "Boot Gang" in result
        assert "Glock Tua" in result
        assert "5W" in result
        assert "3W" in result
        assert "GW1" in result
        assert "65" in result

    def test_missing_teams_asks_for_format(self) -> None:
        a = _agent({})
        a._session["entry_id"] = None
        a._session["entry_name"] = None
        result = a._handle_head_to_head("head to head record", [])
        assert "team" in result.lower() or "vs" in result.lower()

    def test_unavailable_on_non_dict(self) -> None:
        def se(name, args):
            if name == "league_entries":
                return {"teams": _TEAMS}
            return "error"
        a = _agent_with_side_effect(se)
        result = a._handle_head_to_head("Boot Gang vs Glock Tua", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_waiver
# ---------------------------------------------------------------------------

class TestHandleWaiver:

    def test_renders_recommendations_with_drops(self) -> None:
        a = _agent({
            "entry_name": "Boot Gang",
            "target_gw": 28,
            "top_adds": [
                {"name": "Palmer", "team": "CHE", "position_type": 3,
                 "suggested_drop": {"name": "Werner", "team": "TOT"}},
                {"name": "Isak", "team": "NEW", "position_type": 4,
                 "suggested_drop": None},
            ],
            "warnings": ["Palmer rotated in CL midweek"],
        })
        result = a._handle_waiver("waiver recommendations", [])
        assert "Palmer" in result
        assert "Werner" in result
        assert "drop" in result.lower()
        assert "GW28" in result
        assert "Warning" in result
        assert "1." in result and "2." in result

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_waiver("waiver recs", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_streak
# ---------------------------------------------------------------------------

class TestHandleStreak:

    def test_renders_all_streaks(self) -> None:
        a = _agent({
            "entry_name": "Boot Gang",
            "start_win_streak": 3,
            "current_win_streak": 2,
            "max_win_streak": 5,
        })
        result = a._handle_streak("win streak", [])
        assert "Boot Gang" in result
        assert "start-season streak 3" in result
        assert "current streak 2" in result
        assert "max streak 5" in result

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_streak("win streak", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_wins_list
# ---------------------------------------------------------------------------

class TestHandleWinsList:

    def test_renders_win_gameweeks(self) -> None:
        a = _agent({
            "entry_name": "Boot Gang",
            "matches": [
                {"gameweek": 1, "finished": True, "result": "W"},
                {"gameweek": 2, "finished": True, "result": "L"},
                {"gameweek": 3, "finished": True, "result": "W"},
                {"gameweek": 4, "finished": False, "result": ""},
            ],
        })
        result = a._handle_wins_list("wins each week", [])
        assert "GW1" in result
        assert "GW3" in result
        assert "GW2" not in result
        assert "GW4" not in result

    def test_no_completed_wins(self) -> None:
        a = _agent({
            "entry_name": "Boot Gang",
            "matches": [
                {"gameweek": 1, "finished": True, "result": "L"},
            ],
        })
        result = a._handle_wins_list("wins each week", [])
        assert "no completed wins" in result.lower()

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_wins_list("wins each week", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_schedule
# ---------------------------------------------------------------------------

class TestHandleSchedule:

    def test_renders_upcoming_matches(self) -> None:
        a = _agent({
            "entry_name": "Boot Gang",
            "matches": [
                {"gameweek": 28, "opponent_name": "Glock Tua"},
                {"gameweek": 29, "opponent_name": "Luckier Than You"},
            ],
        })
        result = a._handle_schedule("upcoming schedule", [])
        assert "Boot Gang" in result
        assert "Glock Tua" in result
        assert "GW28" in result
        assert "GW29" in result

    def test_single_gw_shortform(self) -> None:
        a = _agent({
            "entry_name": "Boot Gang",
            "matches": [{"gameweek": 28, "opponent_name": "Glock Tua"}],
        })
        result = a._handle_schedule("who does Boot Gang play GW28", [])
        assert "plays" in result
        assert "Glock Tua" in result

    def test_no_matches(self) -> None:
        a = _agent({"entry_name": "Boot Gang", "matches": []})
        result = a._handle_schedule("schedule", [])
        assert "no matches" in result.lower()

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_schedule("schedule", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_fixtures
# ---------------------------------------------------------------------------

class TestHandleFixtures:

    def test_renders_fixtures(self) -> None:
        a = _agent({
            "fixtures": [
                {"event": 28, "team_h_short": "ARS", "team_a_short": "CHE"},
                {"event": 28, "team_h_short": "LIV", "team_a_short": "MCI"},
            ],
        })
        result = a._handle_fixtures("upcoming fixtures", [])
        assert "ARS" in result and "CHE" in result
        assert "LIV" in result and "MCI" in result
        assert "GW28" in result

    def test_no_fixtures(self) -> None:
        a = _agent({"fixtures": []})
        result = a._handle_fixtures("fixtures", [])
        assert "no fixtures" in result.lower()

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_fixtures("fixtures", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_player_form
# ---------------------------------------------------------------------------

class TestHandlePlayerForm:

    def test_renders_top_10(self) -> None:
        a = _agent({
            "players": [
                {"name": "Salah", "team": "LIV", "points_per_gw": 8.5},
                {"name": "Palmer", "team": "CHE", "points_per_gw": 7.2},
            ],
        })
        result = a._handle_player_form("player form table", [])
        assert "Salah" in result
        assert "Palmer" in result
        assert "8.50" in result
        assert "7.20" in result

    def test_no_players(self) -> None:
        a = _agent({"players": []})
        result = a._handle_player_form("player form", [])
        assert "no player form" in result.lower()

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_player_form("player form", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_lineup_efficiency
# ---------------------------------------------------------------------------

class TestHandleLineupEfficiency:

    def test_renders_efficiency_table(self) -> None:
        a = _agent({
            "gameweek": 28,
            "entries": [
                {"entry_id": 286192, "entry_name": "Boot Gang",
                 "bench_points": 15, "bench_points_played": 8,
                 "zero_minute_starter_count": 1},
            ],
        })
        result = a._handle_lineup_efficiency("bench points", [])
        assert "Boot Gang" in result
        assert "15" in result

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_lineup_efficiency("lineup efficiency", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_league_summary
# ---------------------------------------------------------------------------

class TestHandleLeagueSummary:

    def test_renders_via_reports(self) -> None:
        a = _agent({
            "gameweek": 28,
            "league_id": 14204,
            "entries": [
                {"entry_id": 100, "entry_name": "Boot Gang",
                 "opponent_entry_id": 200, "opponent_name": "Glock Tua",
                 "score_for": 65, "score_against": 50, "result": "W",
                 "roster": []},
                {"entry_id": 200, "entry_name": "Glock Tua",
                 "opponent_entry_id": 100, "opponent_name": "Boot Gang",
                 "score_for": 50, "score_against": 65, "result": "L",
                 "roster": []},
            ],
        })
        result = a._handle_league_summary("league summary", [])
        assert "GW28" in result
        assert "Boot Gang" in result

    def test_unavailable_on_error_key(self) -> None:
        a = _agent({"error": "something broke"})
        result = a._handle_league_summary("league summary", [])
        assert "unavailable" in result.lower()

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_league_summary("league summary", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_transactions
# ---------------------------------------------------------------------------

class TestHandleTransactions:

    def test_renders_with_element_names(self) -> None:
        a = _agent({
            "gameweek": 28,
            "entries": [
                {"entry_id": 100, "entry_name": "Boot Gang",
                 "total_in": 1, "total_out": 1,
                 "waiver_in": [101], "waiver_out": [202],
                 "free_in": [], "free_out": [],
                 "trade_in": [], "trade_out": []},
            ],
        })
        with patch.object(type(a), "_load_element_map", return_value={
            101: "Palmer", 202: "Werner",
        }):
            result = a._handle_transactions("transactions this week", [])
        assert "GW28" in result
        assert "Boot Gang" in result
        assert "Palmer" in result
        assert "Werner" in result
        assert "Waivers" in result

    def test_no_activity(self) -> None:
        a = _agent({
            "gameweek": 28,
            "entries": [
                {"entry_id": 100, "entry_name": "Boot Gang",
                 "total_in": 0, "total_out": 0,
                 "waiver_in": [], "waiver_out": [],
                 "free_in": [], "free_out": [],
                 "trade_in": [], "trade_out": []},
            ],
        })
        with patch.object(type(a), "_load_element_map", return_value={}):
            result = a._handle_transactions("transactions", [])
        assert "no transactions" in result.lower()

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_transactions("transactions", [])
        assert "unavailable" in result.lower()

    def test_file_not_found_error(self) -> None:
        a = _agent({"error": "no such file or directory"})
        result = a._handle_transactions("transactions", [])
        assert "missing" in result.lower() or "refresh" in result.lower()


# ---------------------------------------------------------------------------
# _handle_matchup_summary
# ---------------------------------------------------------------------------

class TestHandleMatchupSummary:

    def test_missing_gw_asks(self) -> None:
        a = _agent({})
        result = a._handle_matchup_summary("Boot Gang vs Glock Tua recap", [])
        assert "gameweek" in result.lower() or "gw" in result.lower()

    def test_missing_vs_asks(self) -> None:
        a = _agent({})
        result = a._handle_matchup_summary("GW27 matchup summary", [])
        assert "team" in result.lower() or "provide" in result.lower()


# ---------------------------------------------------------------------------
# _handle_ownership_scarcity
# ---------------------------------------------------------------------------

class TestHandleOwnershipScarcity:

    def test_renders_ownership_breakdown(self) -> None:
        a = _agent({
            "gameweek": 28,
            "owned_totals": {"gk": 10, "def": 40, "mid": 50, "fwd": 20, "total": 120},
            "unowned_totals": {"gk": 30, "def": 60, "mid": 80, "fwd": 40, "total": 210},
            "hoarders": {
                "def": [{"entry_name": "Boot Gang", "count": 7}],
            },
        })
        result = a._handle_ownership_scarcity("ownership breakdown", [])
        assert "GW28" in result
        assert "120" in result
        assert "210" in result
        assert "Boot Gang" in result
        assert "7" in result

    def test_empty_hoarders(self) -> None:
        a = _agent({
            "gameweek": 28,
            "owned_totals": {"gk": 10, "def": 40, "mid": 50, "fwd": 20, "total": 120},
            "unowned_totals": {},
            "hoarders": {},
        })
        result = a._handle_ownership_scarcity("ownership scarcity", [])
        assert "hoarder" not in result.lower()

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_ownership_scarcity("ownership scarcity", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_strength_of_schedule
# ---------------------------------------------------------------------------

class TestHandleStrengthOfSchedule:

    def test_renders_rankings_sorted(self) -> None:
        a = _agent({
            "gameweek": 28,
            "entries": [
                {"entry_name": "Glock Tua", "future_opponent_avg_rank": 5.8,
                 "future_opponents_top_half": 4, "future_opponents_bottom_half": 1},
                {"entry_name": "Boot Gang", "future_opponent_avg_rank": 3.2,
                 "future_opponents_top_half": 2, "future_opponents_bottom_half": 3},
            ],
        })
        result = a._handle_strength_of_schedule("strength of schedule", [])
        assert "Boot Gang" in result
        assert "3.2" in result
        # Boot Gang (3.2) should appear before Glock Tua (5.8)
        boot_idx = result.index("Boot Gang")
        glock_idx = result.index("Glock Tua")
        assert boot_idx < glock_idx

    def test_no_entries(self) -> None:
        a = _agent({"entries": []})
        result = a._handle_strength_of_schedule("strength of schedule", [])
        assert "no" in result.lower()

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_strength_of_schedule("strength of schedule", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_league_entries
# ---------------------------------------------------------------------------

class TestHandleLeagueEntries:

    def test_renders_team_list(self) -> None:
        a = _agent({"teams": _TEAMS})
        result = a._handle_league_entries("show all teams", [])
        assert "Boot Gang" in result
        assert "Glock Tua" in result
        assert "BG" in result
        assert "2" in result

    def test_no_teams(self) -> None:
        a = _agent({"teams": []})
        result = a._handle_league_entries("all teams", [])
        assert "no teams" in result.lower()

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_league_entries("teams", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _handle_game_status (output rendering)
# ---------------------------------------------------------------------------

class TestHandleGameStatusOutput:

    def test_live_renders_deadlines_and_fixtures(self) -> None:
        a = _agent({
            "current_gw": 28,
            "current_gw_finished": False,
            "next_gw": 29,
            "waivers_processed": True,
            "next_deadline": "2026-03-07T18:30:00Z",
            "next_waivers_due": "2026-03-06T18:30:00Z",
            "next_trades_due": "2026-03-05T18:30:00Z",
            "next_gw_first_kickoff": "2026-03-07T20:00:00Z",
            "current_gw_fixtures": {"total": 10, "started": 3, "finished": 1},
            "points_status": "live",
        })
        result = a._handle_game_status("game status", [])
        assert "Gameweek 28" in result
        assert "LIVE" in result
        assert "1 finished" in result
        assert "2 in progress" in result
        assert "7 upcoming" in result
        assert "Trades due" in result
        assert "Waivers due" in result
        assert "free agency open" in result

    def test_final_points(self) -> None:
        a = _agent({
            "current_gw": 28,
            "current_gw_finished": True,
            "next_gw": 29,
            "waivers_processed": False,
            "next_deadline": "2026-03-07T18:30:00Z",
            "next_waivers_due": "",
            "next_trades_due": "",
            "next_gw_first_kickoff": "",
            "current_gw_fixtures": {"total": 10, "started": 10, "finished": 10},
            "points_status": "final",
        })
        result = a._handle_game_status("are points final", [])
        assert "FINAL" in result
        assert "official" in result.lower()
        assert "pending" in result.lower()

    def test_unavailable_on_non_dict(self) -> None:
        a = _agent("error")
        result = a._handle_game_status("game status", [])
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# _render_game_context (LLM context injection)
# ---------------------------------------------------------------------------

class TestRenderGameContext:

    def test_terse_format_for_llm(self) -> None:
        a = _agent()
        ctx = a._render_game_context({
            "current_gw": 28,
            "next_gw": 29,
            "points_status": "live",
            "waivers_processed": True,
            "next_deadline": "2026-03-07T18:30:00Z",
            "next_waivers_due": "2026-03-06T18:30:00Z",
            "next_trades_due": "2026-03-05T18:30:00Z",
            "next_gw_first_kickoff": "2026-03-07T20:00:00Z",
        })
        assert "Game Status:" in ctx
        assert "Current GW: 28" in ctx
        assert "Points: Live" in ctx
        assert "free agency open" in ctx.lower()

    def test_empty_on_failure(self) -> None:
        a = _agent()
        a.mcp.call_tool.side_effect = Exception("connection refused")
        assert a._fetch_game_context() == ""
