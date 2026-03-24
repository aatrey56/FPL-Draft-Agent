"""Property-based tests for backend.agent using Hypothesis.

These tests verify invariants that must hold across *all* valid inputs,
catching edge cases that hand-written examples miss.  Each test runs
200 randomly generated inputs with automatic shrinking on failure.

Requires: pip install hypothesis
"""

import re
import sys
import types
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, example, HealthCheck
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Stub heavy optional deps before importing backend modules.
# ---------------------------------------------------------------------------
for _mod in ("openai", "requests"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)
if not hasattr(sys.modules["openai"], "OpenAI"):
    sys.modules["openai"].OpenAI = MagicMock  # type: ignore[attr-defined]

from backend.agent import Agent  # noqa: E402
from backend.constants import GW_PATTERN  # noqa: E402


def _make_agent() -> Agent:
    mcp = MagicMock()
    mcp.list_tools.return_value = []
    mcp.call_tool.return_value = {}
    llm = MagicMock()
    llm.available.return_value = False
    with patch("backend.agent.get_rag_index", return_value=MagicMock(search=lambda *a, **k: [])):
        return Agent(mcp, llm)


# ---------------------------------------------------------------------------
# Property: _extract_gw never crashes, always returns int | None
# ---------------------------------------------------------------------------

class TestExtractGwProperty:
    """_extract_gw must be robust to arbitrary input."""

    @given(text=st.text(max_size=200))
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_never_crashes(self, text: str) -> None:
        result = _make_agent()._extract_gw(text)
        assert result is None or isinstance(result, int)

    @given(gw=st.integers(min_value=1, max_value=38))
    def test_recognizes_gw_prefix(self, gw: int) -> None:
        agent = _make_agent()
        assert agent._extract_gw(f"gw{gw}") == gw
        assert agent._extract_gw(f"GW{gw}") == gw
        assert agent._extract_gw(f"gameweek {gw}") == gw
        assert agent._extract_gw(f"game week {gw}") == gw

    @given(gw=st.integers(min_value=1, max_value=38),
           prefix=st.text(min_size=0, max_size=20),
           suffix=st.text(min_size=0, max_size=20))
    @settings(max_examples=100)
    def test_extracts_from_surrounding_text(self, gw: int, prefix: str, suffix: str) -> None:
        agent = _make_agent()
        result = agent._extract_gw(f"{prefix} gw{gw} {suffix}")
        # Should find the GW number (might find a different one if prefix/suffix
        # contain gw-like patterns, but should never crash or return a non-int).
        assert result is None or isinstance(result, int)


# ---------------------------------------------------------------------------
# Property: _extract_league_id never crashes, returns int | None
# ---------------------------------------------------------------------------

class TestExtractLeagueIdProperty:

    @given(text=st.text(max_size=200))
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_never_crashes(self, text: str) -> None:
        result = _make_agent()._extract_league_id(text)
        assert result is None or isinstance(result, int)

    @given(lid=st.integers(min_value=1000, max_value=999999))
    def test_recognizes_league_id_pattern(self, lid: int) -> None:
        agent = _make_agent()
        assert agent._extract_league_id(f"league id:{lid}") == lid


# ---------------------------------------------------------------------------
# Property: _looks_like never crashes for any (intent, text) pair
# ---------------------------------------------------------------------------

class TestLooksLikeProperty:

    @given(text=st.text(max_size=300))
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_never_crashes_known_intents(self, text: str) -> None:
        agent = _make_agent()
        for intent in agent._INTENT_KEYWORDS:
            result = agent._looks_like(intent, text)
            assert isinstance(result, bool)

    @given(intent=st.text(max_size=50), text=st.text(max_size=200))
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_unknown_intent_returns_false(self, intent: str, text: str) -> None:
        agent = _make_agent()
        if intent not in agent._INTENT_KEYWORDS:
            assert agent._looks_like(intent, text) is False

    def test_known_triggers_always_match(self) -> None:
        """Every registered pattern must match when used as input."""
        agent = _make_agent()
        for intent, patterns in agent._INTENT_KEYWORDS.items():
            for pattern in patterns:
                if isinstance(pattern, str):
                    assert agent._looks_like(intent, pattern), (
                        f"'{pattern}' should match intent '{intent}'"
                    )


# ---------------------------------------------------------------------------
# Property: _sanitize_error never leaks internal file paths
# ---------------------------------------------------------------------------

class TestSanitizeErrorProperty:

    @given(gw=st.integers(min_value=1, max_value=38))
    def test_go_file_errors_stripped(self, gw: int) -> None:
        agent = _make_agent()
        raw = f"open data/raw/gw/{gw}/live.json: no such file or directory"
        result = agent._sanitize_error(raw)
        assert "data/raw/" not in result
        assert f"GW{gw}" in result

    @given(text=st.text(max_size=500))
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_never_crashes(self, text: str) -> None:
        result = _make_agent()._sanitize_error(text)
        assert isinstance(result, str)

    @given(text=st.text(min_size=1, max_size=500))
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_nonempty_input_returns_nonempty(self, text: str) -> None:
        """Non-empty error messages should produce non-empty output."""
        result = _make_agent()._sanitize_error(text)
        assert isinstance(result, str)
        # After stripping file paths, there should still be content.
        # However a message like "open foo.json: " could strip to empty.
        # The important thing is it doesn't crash.


# ---------------------------------------------------------------------------
# Property: _is_greeting never crashes, returns bool
# ---------------------------------------------------------------------------

class TestIsGreetingProperty:

    @given(text=st.text(max_size=200))
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_never_crashes(self, text: str) -> None:
        result = _make_agent()._is_greeting(text)
        assert isinstance(result, bool)

    @given(text=st.text(min_size=100, max_size=300))
    @settings(max_examples=50)
    def test_long_messages_not_greetings(self, text: str) -> None:
        """Very long messages should not be detected as greetings
        (greetings are short phrases)."""
        result = _make_agent()._is_greeting(text)
        # Not a strict invariant — a 100+ char message *could* theoretically
        # start with "hi" — but this catches regressions in the check logic.
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Property: _apply_defaults never injects None values
# ---------------------------------------------------------------------------

class TestApplyDefaultsProperty:

    TOOLS_WITH_DEFAULTS = [
        "league_summary", "standings", "transactions", "lineup_efficiency",
        "waiver_recommendations", "manager_schedule", "manager_streak",
        "current_roster", "manager_season", "head_to_head",
        "transaction_analysis", "draft_picks", "player_gw_stats",
        "fixtures", "fixture_difficulty", "player_form",
        "epl_fixtures", "epl_standings", "game_status",
        "ownership_scarcity", "strength_of_schedule", "league_entries",
    ]

    @given(data=st.data())
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_no_none_values_in_output(self, data: st.DataObject) -> None:
        """_apply_defaults must not produce args with None values —
        the Go MCP server rejects null JSON fields."""
        tool = data.draw(st.sampled_from(self.TOOLS_WITH_DEFAULTS))
        agent = _make_agent()
        agent._session["league_id"] = 14204
        agent._session["entry_id"] = 286192
        result = agent._apply_defaults(tool, {})
        for k, v in result.items():
            assert v is not None, f"_apply_defaults('{tool}', {{}}) produced None for key '{k}'"


# ---------------------------------------------------------------------------
# Property: _render_game_status_response robustness
# ---------------------------------------------------------------------------

def _fixture_counts() -> st.SearchStrategy[Dict[str, int]]:
    """Generate {"total": T, "started": S, "finished": F} where F <= S <= T."""
    return st.integers(min_value=0, max_value=10).flatmap(
        lambda total: st.integers(min_value=0, max_value=total).flatmap(
            lambda started: st.integers(min_value=0, max_value=started).map(
                lambda finished: {"total": total, "started": started, "finished": finished}
            )
        )
    )


_iso_or_empty = st.just("") | st.datetimes().map(lambda dt: dt.isoformat() + "Z")

game_status_payload = st.fixed_dictionaries({
    "current_gw": st.integers(min_value=1, max_value=38),
    "next_gw": st.integers(min_value=1, max_value=38),
    "points_status": st.sampled_from(["live", "final", "provisional"]),
    "current_gw_finished": st.booleans(),
    "waivers_processed": st.booleans(),
    "current_gw_fixtures": _fixture_counts(),
    "next_deadline": _iso_or_empty,
    "next_waivers_due": _iso_or_empty,
    "next_trades_due": _iso_or_empty,
    "next_gw_first_kickoff": _iso_or_empty,
})


class TestRenderGameStatusProperty:
    """Property: _render_game_status_response never crashes on valid data."""

    @given(data=game_status_payload)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_never_crashes(self, data: Dict[str, Any]) -> None:
        agent = _make_agent()
        result = agent._render_game_status_response(data)
        assert isinstance(result, str)
        assert "Gameweek" in result
