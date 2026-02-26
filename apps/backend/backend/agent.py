import json
from typing import Any, Dict, List, Optional, Tuple, TypedDict
import re
from pathlib import Path

from .llm import LLMClient, try_parse_json
from .mcp_client import MCPClient
from .config import SETTINGS
from .constants import GW_PATTERN, POSITION_TYPE_LABELS
from .reports import render_league_summary_md, render_standings_md, render_matchup_md, render_lineup_efficiency_md
from .rag import get_rag_index, format_rag_docs


class AgentSession(TypedDict, total=False):
    """Typed container for per-conversation session state tracked by the Agent."""

    league_id: Optional[int]
    entry_id: Optional[int]
    entry_name: Optional[str]
    gw: Optional[int]
    last_tool: Optional[str]


SYSTEM_PROMPT = """You are a data-accurate FPL Draft assistant. You MUST call tools for any factual data.
Return ONLY a JSON object in one of these forms:
1) {"action":"tool","name":"tool_name","arguments":{...}}
2) {"action":"final","content":"..."}
If you need more data, choose action=tool. Do not guess.
If a section labeled "Memory (cached data)" is provided, you may use it as factual context without another tool call.

Available tools and when to use them:
- league_summary: League weekly summary (roster, points, bench, record, opponent per manager).
- matchup_breakdown: Points by position for each matchup (why you won/lost) in a GW.
- standings: League standings table for a gameweek.
- transactions: Per-manager waiver/free-agent/trade digest for a GW.
- lineup_efficiency: Bench points and zero-minute starters per manager.
- strength_of_schedule: Past/future opponent difficulty per manager.
- ownership_scarcity: Ownership counts by position, who owns scarce players.
- fixtures: Upcoming PL fixtures for all teams.
- fixture_difficulty: Rank fixtures by how easy/hard they are for each position.
- player_form: Rolling points/minutes/ownership for rostered players.
- waiver_targets: Ranked unrostered players for waiver adds (league-wide).
- waiver_recommendations: Personalised add/drop suggestions for a specific manager.
- manager_schedule: Upcoming or historical H2H matches for a manager.
- manager_streak: Win-streak stats for a manager.
- player_lookup: Look up a player by element id.
- manager_lookup: Look up a manager by entry id.
- league_entries: List all teams in the league.
- current_roster: Show a manager's squad (starters + bench) with names, teams, positions.
  Use for: "show my team", "my roster", "who's on my bench", "my current lineup", "squad", "my players".
- draft_picks: Full draft history — who drafted whom, in which round.
  Use for: "draft picks", "who drafted X", "draft order", "round N picks", "who did I draft".
- manager_season: GW-by-GW scores, W/D/L record, highest/lowest scoring week for a manager.
  Use for: "season stats", "season history", "how have I done", "my record", "weekly scores", "season breakdown".
- transaction_analysis: League-wide analysis of a GW's transactions: top added/dropped players, positions targeted.
  Use for: "who added", "most targeted position", "transaction analysis", "what positions did people target", "popular adds", "popular drops".
- player_gw_stats: Per-gameweek stats (points, minutes, goals, xG, xA) for a specific player.
  Use for: "Salah stats", "how many points has X scored each week", "player stats", "X points per gameweek", "weekly breakdown for X".
- head_to_head: H2H record and match history between two managers.
  Use for: "head to head", "h2h", "record against", "how many times have I beaten X".

Important routing guidance:
- For "who does X play" / "schedule" / "upcoming match" → manager_schedule.
- For "list all teams" or resolving a team name → league_entries.
- For "win streak" / "how many games in a row" → manager_streak.
- For "my roster" / "my squad" / "who's on my team" → current_roster.
- For "season record" / "season stats" / "how have I done overall" → manager_season.
- For "draft picks" / "who drafted X" → draft_picks.
- For "what positions/players did people add/drop this week" → transaction_analysis.
- For "how has player X done each week" / "player X stats by gameweek" → player_gw_stats.
- For "head to head" / "h2h" / "record against" → head_to_head.
- Never output element IDs. Always use player names.
- If required arguments are missing or ambiguous, ask a follow-up using action=final.
- Never pass null values in arguments. Omit missing fields instead.

Example tool calls:
{"action":"tool","name":"current_roster","arguments":{"league_id":14204,"entry_id":286192}}
{"action":"tool","name":"draft_picks","arguments":{"league_id":14204,"entry_id":286192}}
{"action":"tool","name":"manager_season","arguments":{"league_id":14204,"entry_id":286192}}
{"action":"tool","name":"transaction_analysis","arguments":{"league_id":14204,"gw":0}}
{"action":"tool","name":"player_gw_stats","arguments":{"player_name":"Salah"}}
{"action":"tool","name":"head_to_head","arguments":{"league_id":14204,"entry_name_a":"Team A","entry_name_b":"Team B"}}
"""


class Agent:
    # Maps intent name → list of match patterns.
    # Each pattern is either:
    #   str   – matches if the keyword is a substring of the (lowercased) text
    #   tuple – matches if ALL strings in the tuple are substrings of the text
    # The outer list is OR: the intent matches if ANY pattern matches.
    _INTENT_KEYWORDS: Dict[str, List] = {
        "draft_picks": [
            ("draft", "pick"), ("draft", "picks"), ("draft", "order"),
            ("draft", "history"), ("draft", "round"), ("draft", "drafted"),
            ("draft", "who did"),
        ],
        "player_gw_stats": [
            "stats each week", "stats per week", "weekly stats",
            "points each week", "points per gameweek", "gw points",
            "gameweek points", "weekly breakdown", "each gameweek", "per gw",
        ],
        "transaction_analysis": [
            ("transaction", "analysis"), "most targeted", "who added",
            "who dropped", ("position", "targeted"), ("position", "popular"),
            "popular adds", "popular drops",
        ],
        "head_to_head": ["head to head", "h2h", "record against"],
        "manager_season": [
            "season stats", "season history", "season record",
            "how have i done", "overall record", "weekly scores",
            "week by week", "all season", "season breakdown", "full season",
            "season summary", "my record", "this season",
        ],
        "current_roster": [
            "my team", "my roster", "my squad", "current lineup",
            "current roster", "who's on my team", "show my team",
            "show my squad", "my players", "who do i have",
            "my starting", "my bench",
        ],
        "waiver": ["waiver"],
        "streak": [("streak", "win"), ("in a row", "win")],
        "win_list": [
            ("win", "week"), ("win", "gw"), ("win", "gameweek"),
            ("won", "week"), ("won", "gw"), ("won", "gameweek"),
            ("wins", "week"), ("wins", "gw"), ("wins", "gameweek"),
        ],
        "schedule": ["schedule", ("who does", "play")],
        "fixtures": ["fixtures", "fixture list"],
        "player_form": ["player_form", "player form", "form table"],
        "standings": ["standings", "table"],
        "league_summary": ["league summary", ("summary", "league")],
        "transactions": ["transactions", "trades", ("waivers", "summary")],
        "lineup": ["lineup efficiency", "bench points"],
        "strength": ["strength of schedule", "schedule difficulty"],
        "ownership": ["ownership", "scarcity"],
        "matchup_summary": [
            (" vs ", "summary"), (" vs ", "recap"),
            (" vs. ", "summary"), (" vs. ", "recap"),
        ],
    }

    def _looks_like(self, intent: str, text: str) -> bool:
        """Return True if *text* matches any pattern for *intent*."""
        for pattern in self._INTENT_KEYWORDS.get(intent, []):
            if isinstance(pattern, tuple):
                if all(kw in text for kw in pattern):
                    return True
            else:
                if pattern in text:
                    return True
        return False

    def __init__(self, mcp: MCPClient, llm: LLMClient) -> None:
        """Initialise the agent with an MCP tool client and an LLM client.

        Args:
            mcp: Client used to list and call tools exposed by the Go MCP server.
            llm: Client used to call the OpenAI chat completions API when the
                 fast-path keyword router cannot handle a query directly.
        """
        self.mcp = mcp
        self.llm = llm
        self._element_name_cache: Optional[Dict[int, str]] = None
        self._pending_intent: Optional[str] = None
        self._pending_candidates: Optional[List[str]] = None
        self._pending_league_id: Optional[int] = None
        self._pending_text: Optional[str] = None
        self._session: AgentSession = {
            "league_id": None,
            "entry_id": None,
            "entry_name": None,
            "gw": None,
            "last_tool": None,
        }
        self._history: List[Dict[str, str]] = []
        self._history_limit = 6
        self._rag = get_rag_index()

    def run(
        self,
        user_message: str,
        max_steps: int = 6,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Process a user message and return the agent's response.

        The agent first tries a fast-path keyword router (_try_route).  If no
        route matches, it falls back to an LLM-guided tool-call loop that runs
        for up to *max_steps* iterations.

        Args:
            user_message: The raw text sent by the user.
            max_steps:    Maximum number of LLM → tool → LLM cycles before
                          giving up (default 6).
            context:      Optional dict carrying caller-supplied session hints
                          such as ``league_id``, ``entry_id``, ``entry_name``,
                          and ``gw``.

        Returns:
            A dict with at least:
              - ``"content"`` (str): the final text reply for the user.
              - ``"tool_events"`` (list): ordered log of every tool call made.
        """
        user_message = (user_message or "").strip()
        context = context or {}
        self._update_session_from_context(context)
        self._update_session_from_text(user_message)

        tools = self.mcp.list_tools()
        tool_list = "\n".join([f"- {t.name}: {t.description}" for t in tools])
        prompt_parts: List[str] = [f"Available tools:\n{tool_list}\n", self._render_session_context()]
        history_block = self._render_history()
        if history_block:
            prompt_parts.append(history_block)
        memory_block = self._get_rag_memory(user_message)
        if memory_block:
            prompt_parts.append("Memory (cached data):\n" + memory_block)
        prompt_parts.append(user_message)

        messages: List[str] = [p for p in prompt_parts if p]
        tool_events: List[Dict[str, Any]] = []

        direct = self._try_route(user_message, tool_events)
        if direct is not None:
            if user_message:
                self._append_history("user", user_message)
            self._append_history("assistant", direct)
            return {"content": direct, "tool_events": tool_events}

        if not self.llm.available():
            content = "OPENAI_API_KEY not set; unable to use LLM. Please set it and retry."
            if user_message:
                self._append_history("user", user_message)
            self._append_history("assistant", content)
            return {"content": content, "tool_events": tool_events}

        for _ in range(max_steps):
            prompt = "\n\n".join(messages)
            raw = self.llm.generate(SYSTEM_PROMPT, prompt)
            data, ok = try_parse_json(raw)
            if not ok:
                if user_message:
                    self._append_history("user", user_message)
                self._append_history("assistant", raw)
                return {"content": raw, "tool_events": tool_events}

            if data.get("action") == "final":
                content = data.get("content", "")
                if user_message:
                    self._append_history("user", user_message)
                self._append_history("assistant", content)
                return {"content": content, "tool_events": tool_events}

            # Accept legacy form where action is the tool name directly.
            if data.get("action") and data.get("action") not in ("tool", "final"):
                data = {"action": "tool", "name": data.get("action"), "arguments": data.get("arguments", {})}

            if data.get("action") == "tool":
                name = data.get("name", "")
                args = data.get("arguments", {})
                args = self._apply_defaults(name, args)
                self._note_tool_use(name, args)
                tool_events.append({"type": "tool_call", "name": name, "arguments": args})
                try:
                    result = self.mcp.call_tool(name, args)
                except Exception as exc:
                    err_msg = f"Tool error: {exc}"
                    tool_events.append({"type": "tool_error", "name": name, "error": str(exc)})
                    messages.append(err_msg)
                    if user_message:
                        self._append_history("user", user_message)
                    self._append_history("assistant", err_msg)
                    return {"content": err_msg, "tool_events": tool_events}
                tool_events.append({"type": "tool_result", "name": name, "result": result})
                if name == "league_summary" and isinstance(result, dict):
                    content = render_league_summary_md(result, self.llm)
                    if user_message:
                        self._append_history("user", user_message)
                    self._append_history("assistant", content)
                    return {"content": content, "tool_events": tool_events}
                if name == "standings" and isinstance(result, dict):
                    content = render_standings_md(result)
                    if user_message:
                        self._append_history("user", user_message)
                    self._append_history("assistant", content)
                    return {"content": content, "tool_events": tool_events}
                messages.append(f"Tool {name} result:\n{json.dumps(result)[:6000]}")
                continue

            if user_message:
                self._append_history("user", user_message)
            self._append_history("assistant", raw)
            return {"content": raw, "tool_events": tool_events}

        content = "Max steps reached."
        if user_message:
            self._append_history("user", user_message)
        self._append_history("assistant", content)
        return {"content": content, "tool_events": tool_events}

    def _apply_defaults(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Inject session context into LLM-chosen tool arguments.

        When the LLM picks a tool but omits ``league_id``, ``entry_id``, or
        ``gw``, this method fills them in from the current session state so
        that callers never have to repeat the values in every turn.

        It also normalises a few tool-specific quirks:
        - ``manager_schedule`` / ``league_entries``: flattens ``first``/``last``
          name fields into a single ``entry_name`` string.
        - All tools: skips injection of ``None`` values so that optional
          arguments are never accidentally set to null.

        Args:
            name: The tool name chosen by the LLM.
            args: The raw argument dict from the LLM response.

        Returns:
            A new dict with defaults merged in.
        """
        out = dict(args or {})
        if name in ("manager_schedule", "league_entries"):
            first = out.get("first")
            last = out.get("last")
            if first or last:
                entry_name = " ".join([p for p in [str(first or "").strip(), str(last or "").strip()] if p])
                if entry_name and not out.get("entry_name"):
                    out["entry_name"] = entry_name
                out.pop("first", None)
                out.pop("last", None)
            if isinstance(out.get("entry_name"), dict):
                name_dict = out.get("entry_name") or {}
                first = name_dict.get("first")
                last = name_dict.get("last")
                entry_name = " ".join([p for p in [str(first or "").strip(), str(last or "").strip()] if p])
                if entry_name:
                    out["entry_name"] = entry_name
                else:
                    out.pop("entry_name", None)
        try:
            league_id = int(out.get("league_id") or 0)
        except (TypeError, ValueError):
            league_id = 0
        if league_id == 0:
            league_id = self._default_league_id()
        if name in ("waiver_recommendations", "manager_schedule", "manager_streak",
                    "current_roster", "manager_season", "head_to_head"):
            if not out.get("entry_id") and self._default_entry_id():
                out["entry_id"] = self._default_entry_id()
            if not out.get("entry_name") and self._default_entry_name():
                out["entry_name"] = self._default_entry_name()
        if name in (
            "league_summary",
            "matchup_breakdown",
            "standings",
            "transactions",
            "lineup_efficiency",
            "strength_of_schedule",
            "ownership_scarcity",
            "transaction_analysis",
        ):
            out.setdefault("league_id", league_id)
            if "gw" not in out and self._default_gw() is not None:
                out["gw"] = self._default_gw()
            out.setdefault("gw", 0)
        if name in ("league_entries", "manager_schedule", "manager_streak",
                    "current_roster", "draft_picks", "manager_season",
                    "head_to_head", "transaction_analysis"):
            out.setdefault("league_id", league_id)
        if name == "fixtures":
            out.setdefault("league_id", league_id)
            if "gw" in out and "as_of_gw" not in out:
                out["as_of_gw"] = out.pop("gw")
            out.setdefault("as_of_gw", 0)
            out.setdefault("horizon", 1)
            allowed = {"league_id", "as_of_gw", "horizon"}
            out = {k: v for k, v in out.items() if k in allowed}
        if name == "fixture_difficulty":
            out.setdefault("league_id", league_id)
            if "gw" in out and "as_of_gw" not in out:
                out["as_of_gw"] = out.pop("gw")
            if "next_gw" not in out and "target_gw" in out:
                out["next_gw"] = out.pop("target_gw")
            include_raw = out.get("include_raw")
            if isinstance(include_raw, str):
                if include_raw.strip().lower() in ("true", "1", "yes"):
                    out["include_raw"] = True
                elif include_raw.strip().lower() in ("false", "0", "no"):
                    out["include_raw"] = False
            allowed = {"league_id", "as_of_gw", "next_gw", "horizon", "limit", "include_raw"}
            out = {k: v for k, v in out.items() if k in allowed}
        if name == "manager_schedule":
            out.setdefault("league_id", league_id)
            out.setdefault("horizon", 1)
            allowed = {"league_id", "entry_id", "entry_name", "gw", "horizon"}
            out = {k: v for k, v in out.items() if k in allowed}
        if name == "manager_streak":
            out.setdefault("league_id", league_id)
            allowed = {"league_id", "entry_id", "entry_name", "start_gw", "end_gw"}
            out = {k: v for k, v in out.items() if k in allowed}
        if name == "league_entries":
            out.setdefault("league_id", league_id)
            allowed = {"league_id"}
            out = {k: v for k, v in out.items() if k in allowed}
        if name == "player_form":
            out.setdefault("league_id", league_id)
            if "gw" in out and "as_of_gw" not in out:
                out["as_of_gw"] = out.pop("gw")
            out.setdefault("horizon", 5)
            out.setdefault("as_of_gw", 0)
            allowed = {"league_id", "as_of_gw", "horizon"}
            out = {k: v for k, v in out.items() if k in allowed}
        if name == "current_roster":
            out.setdefault("league_id", league_id)
            if "gw" not in out and self._default_gw() is not None:
                out["gw"] = self._default_gw()
            allowed = {"league_id", "entry_id", "entry_name", "gw"}
            out = {k: v for k, v in out.items() if k in allowed}
        if name == "draft_picks":
            out.setdefault("league_id", league_id)
            allowed = {"league_id", "entry_id", "entry_name"}
            out = {k: v for k, v in out.items() if k in allowed}
        if name == "manager_season":
            out.setdefault("league_id", league_id)
            allowed = {"league_id", "entry_id", "entry_name"}
            out = {k: v for k, v in out.items() if k in allowed}
        if name == "transaction_analysis":
            out.setdefault("league_id", league_id)
            out.setdefault("gw", 0)
            allowed = {"league_id", "gw"}
            out = {k: v for k, v in out.items() if k in allowed}
        if name == "player_gw_stats":
            if "gw" in out and "start_gw" not in out:
                out["start_gw"] = out.pop("gw")
            allowed = {"element_id", "player_name", "start_gw", "end_gw"}
            out = {k: v for k, v in out.items() if k in allowed}
        if name == "head_to_head":
            out.setdefault("league_id", league_id)
            allowed = {"league_id", "entry_id_a", "entry_name_a", "entry_id_b", "entry_name_b"}
            out = {k: v for k, v in out.items() if k in allowed}
        return out

    def _default_league_id(self) -> int:
        league_id = self._session.get("league_id")
        if league_id:
            try:
                return int(league_id)
            except Exception:
                pass
        try:
            return int(SETTINGS.league_id)
        except Exception:
            return 0

    def _default_entry_id(self) -> Optional[int]:
        entry_id = self._session.get("entry_id")
        if entry_id:
            try:
                return int(entry_id)
            except Exception:
                return None
        return None

    def _default_entry_name(self) -> Optional[str]:
        entry_name = self._session.get("entry_name")
        if entry_name:
            return str(entry_name)
        return None

    def _default_gw(self) -> Optional[int]:
        gw = self._session.get("gw")
        if gw is None:
            return None
        try:
            return int(gw)
        except Exception:
            return None

    def _update_session_from_context(self, context: Dict[str, Any]) -> None:
        league_id = context.get("league_id")
        if league_id:
            try:
                self._session["league_id"] = int(league_id)
            except Exception:
                pass
        entry_id = context.get("entry_id")
        if entry_id:
            try:
                self._session["entry_id"] = int(entry_id)
            except Exception:
                pass
        entry_name = context.get("entry_name")
        if entry_name:
            self._session["entry_name"] = str(entry_name)
        gw = context.get("gw")
        if gw is not None and gw != "":
            try:
                self._session["gw"] = int(gw)
            except Exception:
                pass

    def _update_session_from_text(self, text: str) -> None:
        league_id = self._extract_league_id(text)
        if league_id:
            self._session["league_id"] = league_id
        entry_id = self._extract_entry_id(text)
        if entry_id:
            self._session["entry_id"] = entry_id
        gw = self._extract_gw(text)
        if gw is not None:
            self._session["gw"] = gw

    def _note_tool_use(self, name: str, args: Dict[str, Any]) -> None:
        self._session["last_tool"] = name
        league_id = args.get("league_id")
        if league_id:
            try:
                self._session["league_id"] = int(league_id)
            except Exception:
                pass
        entry_id = args.get("entry_id")
        if entry_id:
            try:
                self._session["entry_id"] = int(entry_id)
            except Exception:
                pass
        entry_name = args.get("entry_name")
        if entry_name:
            self._session["entry_name"] = str(entry_name)
        gw = args.get("gw")
        if gw is None:
            gw = args.get("as_of_gw")
        if gw is not None:
            try:
                self._session["gw"] = int(gw)
            except Exception:
                pass

    def _append_history(self, role: str, content: str) -> None:
        text = (content or "").strip()
        if not text:
            return
        self._history.append({"role": role, "content": text})
        max_msgs = self._history_limit * 2
        if len(self._history) > max_msgs:
            self._history = self._history[-max_msgs:]

    def _render_history(self) -> str:
        if not self._history:
            return ""
        lines = ["Recent conversation:"]
        for item in self._history:
            role = "User" if item.get("role") == "user" else "Assistant"
            lines.append(f"{role}: {item.get('content','')}")
        return "\n".join(lines)

    def _render_session_context(self) -> str:
        parts = []
        if self._session.get("league_id"):
            parts.append(f"league_id={self._session.get('league_id')}")
        if self._session.get("entry_id"):
            parts.append(f"entry_id={self._session.get('entry_id')}")
        if self._session.get("entry_name"):
            parts.append(f"entry_name={self._session.get('entry_name')}")
        if self._session.get("gw") is not None:
            parts.append(f"gw={self._session.get('gw')}")
        if self._session.get("last_tool"):
            parts.append(f"last_tool={self._session.get('last_tool')}")
        if not parts:
            return ""
        return "Session defaults: " + ", ".join(parts)

    def _should_use_rag(self, text: str) -> bool:
        lower = text.lower()
        triggers = (
            "why",
            "changed",
            "change",
            "since",
            "difference",
            "compare",
            "what changed",
            "last week",
            "previous",
            "earlier",
        )
        return any(t in lower for t in triggers)

    def _get_rag_memory(self, text: str) -> str:
        if not self._rag or not self._should_use_rag(text):
            return ""
        docs = self._rag.search(text, k=3)
        return format_rag_docs(docs)

    # Common greetings and small-talk patterns that should get a friendly
    # response without calling any tools.
    _GREETING_PATTERNS: List[str] = [
        "hello", "hi", "hey", "howdy", "yo", "sup", "good morning",
        "good afternoon", "good evening", "what's up", "whats up",
        "how are you", "thanks", "thank you", "cheers", "bye", "goodbye",
    ]

    def _is_greeting(self, text: str) -> bool:
        """Return True if *text* is a greeting or simple small-talk phrase."""
        lower = text.lower().strip().rstrip("!?.,")
        # Exact match first (e.g. "hello" or "hey")
        if lower in self._GREETING_PATTERNS:
            return True
        # Short messages (≤4 words) that start with a greeting word
        words = lower.split()
        if len(words) <= 4 and any(lower.startswith(g) for g in self._GREETING_PATTERNS):
            return True
        return False

    def _try_route(self, user_message: str, tool_events: List[Dict[str, Any]]) -> Optional[str]:
        text = user_message.strip()
        if not text:
            return None
        pending = self._maybe_resolve_pending(text, tool_events)
        if pending is not None:
            return pending
        lower = text.lower()

        # ---- Greetings / small-talk — respond without calling any tools ----
        if self._is_greeting(lower):
            return (
                "Hey! I'm your FPL Draft assistant. "
                "Ask me about league standings, waiver picks, matchups, "
                "player stats, schedules, and more."
            )

        # ---- New tools (higher priority — check before broad existing patterns) ----
        if self._looks_like("draft_picks", lower):
            return self._handle_draft_picks(text, tool_events)
        if self._looks_like("player_gw_stats", lower):
            return self._handle_player_gw_stats(text, tool_events)
        if self._looks_like("transaction_analysis", lower):
            return self._handle_transaction_analysis(text, tool_events)
        if self._looks_like("head_to_head", lower):
            return self._handle_head_to_head(text, tool_events)
        if self._looks_like("manager_season", lower):
            return self._handle_manager_season(text, tool_events)
        if self._looks_like("current_roster", lower):
            return self._handle_current_roster(text, tool_events)

        # ---- Existing fast-path routes ----
        if self._looks_like("waiver", lower):
            return self._handle_waiver(text, tool_events)
        if self._looks_like("streak", lower):
            return self._handle_streak(text, tool_events)
        if self._looks_like("win_list", lower):
            return self._handle_wins_list(text, tool_events)
        if self._looks_like("schedule", lower):
            return self._handle_schedule(text, tool_events)
        if self._looks_like("fixtures", lower):
            return self._handle_fixtures(text, tool_events)
        if self._looks_like("player_form", lower):
            return self._handle_player_form(text, tool_events)
        if self._looks_like("standings", lower):
            return self._handle_simple_tool(text, tool_events, "standings", "Standings")
        if self._looks_like("league_summary", lower):
            return self._handle_league_summary(text, tool_events)
        if self._looks_like("transactions", lower):
            return self._handle_transactions(text, tool_events)
        if self._looks_like("lineup", lower):
            return self._handle_lineup_efficiency(text, tool_events)
        if self._looks_like("strength", lower):
            return self._handle_simple_tool(text, tool_events, "strength_of_schedule", "Strength of schedule")
        if self._looks_like("ownership", lower):
            return self._handle_simple_tool(text, tool_events, "ownership_scarcity", "Ownership scarcity")
        if self._looks_like("matchup_summary", lower):
            return self._handle_matchup_summary(text, tool_events)
        if "league entries" in lower or "all teams" in lower:
            return self._handle_simple_tool(text, tool_events, "league_entries", "League teams")

        league_id = self._extract_league_id(text) or self._default_league_id()
        if self._looks_like_team_name_only(text, league_id, tool_events):
            team_name = text.strip()
            return (
                f"What would you like to know about {team_name}? "
                "Examples: waivers, schedule, roster, season stats, win streaks, fixtures, head-to-head."
            )

        return None

    @staticmethod
    def _sanitize_error(msg: str) -> str:
        """Strip internal file paths and Go-level detail from error messages.

        The Go MCP server may include raw ``os.Open`` error strings that leak
        file-system layout (e.g.  ``open data/raw/gw/99/live.json: no such
        file or directory``).  This helper rewrites them into user-friendly
        messages while preserving the essential information.
        """
        # Pattern: "open <path>: no such file or directory"
        m = re.search(r"open\s+\S+:\s*no such file or directory", msg)
        if m:
            # Try to extract GW number from the path
            gw_m = re.search(r"/gw/(\d+)/", msg)
            if gw_m:
                return f"No data available for GW{gw_m.group(1)}. The data may not have been fetched yet."
            return "The requested data file was not found. Try refreshing your data."
        # Generic: strip anything that looks like a file-system path
        cleaned = re.sub(r"(?:open|read|stat)\s+\S+\.json:\s*", "", msg).strip()
        return cleaned or msg

    def _call_tool(self, tool_events: List[Dict[str, Any]], name: str, args: Dict[str, Any]) -> Any:
        """Call an MCP tool and return its result, or an ``{"error": ...}`` dict on failure.

        Error messages from the Go server are sanitised to remove internal file
        paths before being stored in the result.
        """
        self._note_tool_use(name, args)
        tool_events.append({"type": "tool_call", "name": name, "arguments": args})
        try:
            result = self.mcp.call_tool(name, args)
        except Exception as exc:
            err_msg = self._sanitize_error(str(exc))
            tool_events.append({"type": "tool_error", "name": name, "error": err_msg})
            return {"error": err_msg}
        # Sanitise errors that arrive inside the result dict
        if isinstance(result, dict) and "error" in result:
            result["error"] = self._sanitize_error(str(result["error"]))
        tool_events.append({"type": "tool_result", "name": name, "result": result})
        return result

    def _maybe_resolve_pending(self, text: str, tool_events: List[Dict[str, Any]]) -> Optional[str]:
        if not self._pending_intent or not self._pending_candidates or not self._pending_league_id:
            return None
        candidate = self._match_candidate(text, self._pending_candidates)
        if candidate is None:
            return f"Which team do you mean? Options: {self._format_candidates(self._pending_candidates)}"
        entry_id, entry_name = self._resolve_team_exact(self._pending_league_id, candidate, tool_events)
        if entry_id is None:
            return f"I couldn't find '{candidate}' in the league. Options: {self._format_candidates(self._pending_candidates)}"

        intent = self._pending_intent
        original_text = self._pending_text or text
        self._pending_intent = None
        self._pending_candidates = None
        self._pending_league_id = None
        self._pending_text = None

        if intent == "waiver":
            return self._handle_waiver(original_text, tool_events, entry_id_override=entry_id, team_name_override=entry_name)
        if intent == "schedule":
            return self._handle_schedule(original_text, tool_events, entry_id_override=entry_id, team_name_override=entry_name)
        if intent == "streak":
            return self._handle_streak(original_text, tool_events, entry_id_override=entry_id, team_name_override=entry_name)
        if intent == "wins":
            return self._handle_wins_list(original_text, tool_events, entry_id_override=entry_id, team_name_override=entry_name)
        if intent == "roster":
            return self._handle_current_roster(original_text, tool_events, entry_id_override=entry_id, team_name_override=entry_name)
        if intent == "season":
            return self._handle_manager_season(original_text, tool_events, entry_id_override=entry_id, team_name_override=entry_name)
        return None

    def _set_pending(self, intent: str, league_id: int, candidates: List[str], original_text: str) -> None:
        self._pending_intent = intent
        self._pending_candidates = candidates
        self._pending_league_id = league_id
        self._pending_text = original_text

    def _format_candidates(self, candidates: List[str]) -> str:
        labeled = [f"{idx + 1}) {name}" for idx, name in enumerate(candidates)]
        return " ".join(labeled)

    def _extract_league_id(self, text: str) -> Optional[int]:
        return self._extract_param("league_id", text)

    def _extract_gw(self, text: str) -> Optional[int]:
        return self._extract_param("gw", text)

    def _extract_horizon(self, text: str) -> Optional[int]:
        return self._extract_param("horizon", text)

    def _extract_entry_id(self, text: str) -> Optional[int]:
        return self._extract_param("entry_id", text)

    def _normalize_text(self, text: str) -> str:
        return re.sub(r"[^a-z0-9 ]+", " ", text.lower())

    def _resolve_team(
        self, league_id: int, text: str, tool_events: List[Dict[str, Any]]
    ) -> Tuple[Optional[int], Optional[str], Optional[List[str]]]:
        data = self._call_tool(tool_events, "league_entries", {"league_id": league_id})
        teams = data.get("teams", []) if isinstance(data, dict) else []
        msg = self._normalize_text(text).strip()
        if not msg:
            return None, None, None
        msg_tokens = msg.split()
        scored: List[Tuple[int, int, int, Dict[str, Any]]] = []

        for t in teams:
            entry_name = (t.get("entry_name") or "").strip()
            short_name = (t.get("short_name") or "").strip()
            for candidate, weight in ((entry_name, 0), (short_name, -1)):
                cand = self._normalize_text(candidate).strip()
                if not cand:
                    continue
                if cand == msg:
                    scored.append((3, len(cand.split()), len(cand), t))
                    continue
                msg_pad = f" {msg} "
                cand_pad = f" {cand} "
                if cand_pad in msg_pad:
                    scored.append((2 + weight, len(cand.split()), len(cand), t))
                    continue
                cand_tokens = cand.split()
                if len(cand_tokens) == 1 and len(cand_tokens[0]) < 4:
                    continue
                if all(tok in msg_tokens for tok in cand_tokens):
                    scored.append((1 + weight, len(cand_tokens), len(cand), t))

        if not scored:
            return None, None, None

        scored.sort(key=lambda s: (s[0], s[1], s[2]), reverse=True)
        best_score = scored[0][0]
        best = [s for s in scored if s[0] == best_score]
        unique = {b[3].get("entry_id"): b[3] for b in best}.values()
        unique = list(unique)
        if len(unique) == 1:
            return unique[0].get("entry_id"), unique[0].get("entry_name"), None
        names = [m.get("entry_name") for m in unique if m.get("entry_name")]
        return None, None, names

    def _resolve_team_exact(
        self, league_id: int, name: str, tool_events: List[Dict[str, Any]]
    ) -> Tuple[Optional[int], Optional[str]]:
        data = self._call_tool(tool_events, "league_entries", {"league_id": league_id})
        teams = data.get("teams", []) if isinstance(data, dict) else []
        target = self._normalize_text(name).strip()
        for t in teams:
            entry_name = self._normalize_text(t.get("entry_name") or "").strip()
            short_name = self._normalize_text(t.get("short_name") or "").strip()
            if target and (entry_name == target or short_name == target):
                return t.get("entry_id"), t.get("entry_name")
        return None, None

    def _match_candidate(self, text: str, candidates: List[str]) -> Optional[str]:
        msg = self._normalize_text(text).strip()
        if msg.isdigit():
            idx = int(msg)
            if 1 <= idx <= len(candidates):
                return candidates[idx - 1]
        for cand in candidates:
            if self._normalize_text(cand).strip() == msg:
                return cand
        for cand in candidates:
            cand_norm = self._normalize_text(cand).strip()
            if cand_norm and f" {cand_norm} " in f" {msg} ":
                return cand
        return None

    # ---- Intent detectors (existing) ----


    # Maps parameter name → compiled regex for extracting a numeric value from text.
    _PARAM_PATTERNS: Dict[str, Any] = {
        "league_id": re.compile(r"league\s*(?:id)?\s*[:=#]?\s*(\d{4,6})", re.IGNORECASE),
        "gw": GW_PATTERN,
        "horizon": re.compile(r"horizon\s*[:=#]?\s*(\d{1,2})", re.IGNORECASE),
        "entry_id": re.compile(r"(?:entry[_\s-]*id|entry)\s*[:=#]?\s*(\d{4,8})", re.IGNORECASE),
    }

    def _extract_param(self, param: str, text: str) -> Optional[int]:
        """Extract a named numeric parameter from *text* using *_PARAM_PATTERNS*."""
        pattern = self._PARAM_PATTERNS.get(param)
        if pattern is None:
            return None
        match = pattern.search(text)
        return int(match.group(1)) if match else None


    def _looks_like_team_name_only(self, text: str, league_id: int, tool_events: List[Dict[str, Any]]) -> bool:
        if len(text.split()) > 6:
            return False
        lowered = text.lower()
        keywords = (
            "waiver",
            "schedule",
            "fixture",
            "streak",
            "standings",
            "transactions",
            "lineup",
            "ownership",
            "trade",
            "wins",
            "roster",
            "squad",
            "draft",
            "season",
            "h2h",
        )
        if any(k in lowered for k in keywords):
            return False
        entry_id, _ = self._resolve_team_exact(league_id, text, tool_events)
        return entry_id is not None

    # ---- Handlers (new tools) ----

    def _handle_current_roster(
        self,
        text: str,
        tool_events: List[Dict[str, Any]],
        entry_id_override: Optional[int] = None,
        team_name_override: Optional[str] = None,
    ) -> str:
        league_id = self._extract_league_id(text) or self._default_league_id()
        gw = self._extract_gw(text)
        if gw is None:
            gw = self._default_gw()
        entry_id = entry_id_override or self._extract_entry_id(text) or self._default_entry_id()
        team_name = team_name_override or self._default_entry_name()

        if not entry_id:
            entry_id, team_name, multiple = self._resolve_team(league_id, text, tool_events)
            if multiple:
                self._set_pending("roster", league_id, multiple, text)
                return f"I found multiple matching teams: {self._format_candidates(multiple)} Which one do you mean?"
        if not entry_id:
            return "Which team's roster would you like? Please provide a team name or entry ID."

        args: Dict[str, Any] = {"league_id": league_id, "entry_id": entry_id}
        if gw is not None:
            args["gw"] = gw
        result = self._call_tool(tool_events, "current_roster", args)
        if not isinstance(result, dict):
            return "Roster data is unavailable right now."

        team_label = team_name or result.get("entry_name") or "your team"
        gw_val = result.get("gameweek", "")
        lines = [f"**{team_label}** — GW{gw_val} squad:"]
        starters = result.get("starters", [])
        bench = result.get("bench", [])
        lines.append("Starting XI:")
        for p in starters:
            name = p.get("name") or "Unknown"
            team = p.get("team") or ""
            pos = POSITION_TYPE_LABELS.get(p.get("position_type"), "?")
            cap = " ©" if p.get("is_captain") else (" (vc)" if p.get("is_vice_captain") else "")
            lines.append(f"  {p.get('position_slot', '')}) {name} ({team}, {pos}){cap}")
        if bench:
            lines.append("Bench:")
            for p in bench:
                name = p.get("name") or "Unknown"
                team = p.get("team") or ""
                pos = POSITION_TYPE_LABELS.get(p.get("position_type"), "?")
                lines.append(f"  {p.get('position_slot', '')}) {name} ({team}, {pos})")
        return "\n".join(lines)

    def _handle_draft_picks(self, text: str, tool_events: List[Dict[str, Any]]) -> Optional[str]:
        league_id = self._extract_league_id(text) or self._default_league_id()
        entry_id = self._extract_entry_id(text) or self._default_entry_id()
        team_name = self._default_entry_name()

        if not entry_id:
            entry_id, team_name, multiple = self._resolve_team(league_id, text, tool_events)
            if multiple:
                # Can't set pending for draft since it's not in pending handler — fall through to LLM.
                return None

        args: Dict[str, Any] = {"league_id": league_id}
        if entry_id:
            args["entry_id"] = entry_id
        result = self._call_tool(tool_events, "draft_picks", args)
        if not isinstance(result, dict):
            return "Draft history is unavailable right now."

        picks = result.get("picks", [])
        filtered_by = result.get("filtered_by") or ""
        header = f"Draft picks for **{filtered_by}**:" if filtered_by else "Draft picks (all teams):"
        lines = [header]
        for p in picks[:30]:
            manager = p.get("entry_name") or "Unknown"
            player = p.get("player_name") or "Unknown"
            team = p.get("team") or ""
            pos = POSITION_TYPE_LABELS.get(p.get("position_type"), "?")
            auto = " (auto)" if p.get("was_auto") else ""
            lines.append(f"  Rd{p.get('round')}, Pick{p.get('pick')}: {manager} → {player} ({team}, {pos}){auto}")
        if len(picks) > 30:
            lines.append(f"  ... and {len(picks) - 30} more picks.")
        return "\n".join(lines)

    def _handle_manager_season(
        self,
        text: str,
        tool_events: List[Dict[str, Any]],
        entry_id_override: Optional[int] = None,
        team_name_override: Optional[str] = None,
    ) -> str:
        league_id = self._extract_league_id(text) or self._default_league_id()
        entry_id = entry_id_override or self._extract_entry_id(text) or self._default_entry_id()
        team_name = team_name_override or self._default_entry_name()

        if not entry_id:
            entry_id, team_name, multiple = self._resolve_team(league_id, text, tool_events)
            if multiple:
                self._set_pending("season", league_id, multiple, text)
                return f"I found multiple matching teams: {self._format_candidates(multiple)} Which one do you mean?"
        if not entry_id:
            return "Which team's season history would you like? Please provide a team name or entry ID."

        args: Dict[str, Any] = {"league_id": league_id, "entry_id": entry_id}
        result = self._call_tool(tool_events, "manager_season", args)
        if not isinstance(result, dict):
            return "Season data is unavailable right now."

        team_label = team_name or result.get("entry_name") or "your team"
        rec = result.get("record", {})
        wins = rec.get("wins", 0)
        draws = rec.get("draws", 0)
        losses = rec.get("losses", 0)
        total = result.get("total_points", 0)
        avg = result.get("avg_score", 0.0)
        hi_gw = result.get("highest_scoring_gw", "?")
        hi_pts = result.get("highest_score", 0)
        lo_gw = result.get("lowest_scoring_gw", "?")
        lo_pts = result.get("lowest_score", 0)

        lines = [
            f"**{team_label}** season summary:",
            f"Record: {wins}W / {draws}D / {losses}L",
            f"Total points: {total} | Avg/week: {avg:.1f}",
            f"Highest: GW{hi_gw} ({hi_pts} pts) | Lowest: GW{lo_gw} ({lo_pts} pts)",
            "",
            "Week-by-week results:",
        ]
        for gw in result.get("gameweeks", []):
            if not gw.get("finished"):
                continue
            opp = gw.get("opponent_name") or "?"
            res = gw.get("result", "")
            score = gw.get("score", 0)
            opp_score = gw.get("opponent_score", 0)
            lines.append(f"  GW{gw.get('gameweek')}: {score}-{opp_score} vs {opp} ({res})")
        return "\n".join(lines)

    def _handle_transaction_analysis(self, text: str, tool_events: List[Dict[str, Any]]) -> str:
        league_id = self._extract_league_id(text) or self._default_league_id()
        gw = self._extract_gw(text)
        if gw is None:
            gw = self._default_gw()
        args: Dict[str, Any] = {"league_id": league_id, "gw": gw if gw is not None else 0}
        result = self._call_tool(tool_events, "transaction_analysis", args)
        if not isinstance(result, dict):
            return "Transaction analysis is unavailable right now."

        gw_val = result.get("gameweek", "?")
        total = result.get("total_transactions", 0)
        lines = [f"**Transaction analysis — GW{gw_val}** ({total} total):"]

        pos_bd = result.get("position_breakdown", {})
        if pos_bd:
            lines.append("\nPositions targeted:")
            for pos in ("GK", "DEF", "MID", "FWD"):
                bd = pos_bd.get(pos, {})
                added = bd.get("added", 0)
                dropped = bd.get("dropped", 0)
                if added or dropped:
                    lines.append(f"  {pos}: +{added} added, -{dropped} dropped")

        top_added = result.get("top_added", [])
        if top_added:
            lines.append("\nMost added players:")
            for p in top_added[:5]:
                name = p.get("player_name") or "Unknown"
                team = p.get("team") or ""
                count = p.get("count", 0)
                times = "time" if count == 1 else "times"
                lines.append(f"  {name} ({team}) — {count} {times}")

        top_dropped = result.get("top_dropped", [])
        if top_dropped:
            lines.append("\nMost dropped players:")
            for p in top_dropped[:5]:
                name = p.get("player_name") or "Unknown"
                team = p.get("team") or ""
                count = p.get("count", 0)
                times = "time" if count == 1 else "times"
                lines.append(f"  {name} ({team}) — {count} {times}")

        return "\n".join(lines)

    def _handle_player_gw_stats(self, text: str, tool_events: List[Dict[str, Any]]) -> str:
        gw_start = self._extract_gw(text)
        args: Dict[str, Any] = {}

        # Try to extract player name from text — remove common trigger phrases.
        player_text = text
        for phrase in ("stats each week", "stats per week", "weekly stats", "points each week",
                       "points per gameweek", "gw points", "gameweek points", "weekly breakdown",
                       "each gameweek", "per gw", "stats for", "points for", "how many points has",
                       "how has", "done each", "scored each"):
            player_text = re.sub(phrase, "", player_text, flags=re.IGNORECASE).strip()

        # Extract potential player name (2-3 word chunk that's not a keyword).
        words = player_text.split()
        candidate_name = " ".join([w for w in words if not re.match(r"^\d+$", w) and len(w) > 2])[:40].strip()
        if candidate_name:
            args["player_name"] = candidate_name
        if gw_start:
            args["start_gw"] = gw_start

        result = self._call_tool(tool_events, "player_gw_stats", args)
        if not isinstance(result, dict) or result.get("error"):
            err = result.get("error") if isinstance(result, dict) else None
            if err and "player not found" in err:
                return f"I couldn't find a player matching '{candidate_name}'. Try a more specific name."
            return "Player stats are unavailable right now."

        name = result.get("player_name") or "Unknown player"
        team = result.get("team") or ""
        pos = POSITION_TYPE_LABELS.get(result.get("position_type"), "?")
        total = result.get("total_points", 0)
        avg = result.get("avg_points", 0.0)
        gws = result.get("gameweeks", [])

        lines = [
            f"**{name}** ({team}, {pos}) — GW stats:",
            f"Total: {total} pts | Avg: {avg:.1f} pts/GW",
            "",
        ]
        for g in gws:
            mins = g.get("minutes", 0)
            pts = g.get("points", 0)
            goals = g.get("goals_scored", 0)
            assists = g.get("assists", 0)
            xg = g.get("expected_goals", 0.0)
            xa = g.get("expected_assists", 0.0)
            extras = []
            if goals:
                extras.append(f"{goals}G")
            if assists:
                extras.append(f"{assists}A")
            extras_str = ", ".join(extras)
            lines.append(
                f"  GW{g.get('gameweek')}: {pts}pts ({mins}min)"
                + (f" [{extras_str}]" if extras_str else "")
                + (f" xG:{xg:.2f} xA:{xa:.2f}" if xg or xa else "")
            )
        if not gws:
            lines.append("  No data found for the requested GW range.")
        return "\n".join(lines)

    def _handle_head_to_head(self, text: str, tool_events: List[Dict[str, Any]]) -> str:
        league_id = self._extract_league_id(text) or self._default_league_id()

        # Try to extract two team names around "vs", "vs.", "h2h", "against", "record against".
        team_a_name: Optional[str] = None
        team_b_name: Optional[str] = None
        m = re.search(r"(.+?)\s+(?:vs\.?|against|h2h)\s+(.+)", text, re.IGNORECASE)
        if m:
            team_a_name = m.group(1).strip()
            team_b_name = m.group(2).strip()
            # Strip common lead-in phrases from team_a.
            for phrase in ("head to head", "h2h", "record", "what is", "what's", "show me"):
                team_a_name = re.sub(rf"^{phrase}\s*", "", team_a_name, flags=re.IGNORECASE).strip()

        if not team_a_name:
            entry_id = self._default_entry_id()
            entry_name = self._default_entry_name()
            if not entry_id and not entry_name:
                return "Please specify two team names for head-to-head (e.g. 'Team A vs Team B')."
            team_a_name = entry_name or str(entry_id)

        entry_id_a: Optional[int] = None
        entry_name_a: Optional[str] = None
        if team_a_name:
            entry_id_a, entry_name_a = self._resolve_team_exact(league_id, team_a_name, tool_events)
            if not entry_id_a:
                entry_id_a, entry_name_a, _ = self._resolve_team(league_id, team_a_name, tool_events)

        entry_id_b: Optional[int] = None
        entry_name_b: Optional[str] = None
        if team_b_name:
            entry_id_b, entry_name_b = self._resolve_team_exact(league_id, team_b_name, tool_events)
            if not entry_id_b:
                entry_id_b, entry_name_b, _ = self._resolve_team(league_id, team_b_name, tool_events)

        if not entry_id_a or not entry_id_b:
            return "Couldn't resolve both teams. Please use exact team names (e.g. 'Team A vs Team B')."

        args: Dict[str, Any] = {
            "league_id": league_id,
            "entry_id_a": entry_id_a,
            "entry_id_b": entry_id_b,
        }
        result = self._call_tool(tool_events, "head_to_head", args)
        if not isinstance(result, dict):
            return "Head-to-head data is unavailable right now."

        team_a = result.get("team_a", {})
        team_b = result.get("team_b", {})
        name_a = team_a.get("entry_name") or entry_name_a or "Team A"
        name_b = team_b.get("entry_name") or entry_name_b or "Team B"
        wa, da, la = team_a.get("wins", 0), team_a.get("draws", 0), team_a.get("losses", 0)
        wb, db, lb = team_b.get("wins", 0), team_b.get("draws", 0), team_b.get("losses", 0)
        lines = [
            f"**Head-to-head: {name_a} vs {name_b}**",
            f"{name_a}: {wa}W / {da}D / {la}L",
            f"{name_b}: {wb}W / {db}D / {lb}L",
            "",
            "Matches:",
        ]
        for m_item in result.get("matches", []):
            if not m_item.get("finished"):
                continue
            sa = m_item.get("score_a", 0)
            sb = m_item.get("score_b", 0)
            res = m_item.get("result_a", "")
            lines.append(f"  GW{m_item.get('gameweek')}: {name_a} {sa} – {sb} {name_b} ({res})")
        if not result.get("matches"):
            lines.append("  No completed matches found.")
        return "\n".join(lines)

    # ---- Handlers (existing) ----

    def _handle_league_summary(self, text: str, tool_events: List[Dict[str, Any]]) -> str:
        league_id = self._extract_league_id(text) or self._default_league_id()
        gw = self._extract_gw(text)
        if gw is None:
            gw = self._default_gw()
        args: Dict[str, Any] = {"league_id": league_id}
        if gw is not None:
            args["gw"] = gw
        result = self._call_tool(tool_events, "league_summary", args)
        if not isinstance(result, dict):
            return "League summary is unavailable right now."
        return render_league_summary_md(result, self.llm)

    def _handle_lineup_efficiency(self, text: str, tool_events: List[Dict[str, Any]]) -> str:
        league_id = self._extract_league_id(text) or self._default_league_id()
        gw = self._extract_gw(text)
        if gw is None:
            gw = self._default_gw()
        entry_id = self._extract_entry_id(text) or self._default_entry_id()
        if not entry_id:
            entry_id, _, multiple = self._resolve_team(league_id, text, tool_events)
            if multiple:
                return f"I found multiple matching teams: {self._format_candidates(multiple)} Which one do you mean?"
        args: Dict[str, Any] = {"league_id": league_id, "gw": gw if gw is not None else 0}
        result = self._call_tool(tool_events, "lineup_efficiency", args)
        if not isinstance(result, dict):
            return "Lineup efficiency data is unavailable."
        return render_lineup_efficiency_md(result, entry_id or 0)

    def _handle_matchup_summary(self, text: str, tool_events: List[Dict[str, Any]]) -> str:
        league_id = self._extract_league_id(text) or self._default_league_id()
        gw = self._extract_gw(text)
        if gw is None:
            gw = self._default_gw()
        if gw is None:
            return "Which gameweek? Please include GW (e.g., GW25)."

        # Extract team names around "vs"
        match = re.search(r"(.+?)\s+vs\.?\s+(.+)", text, re.IGNORECASE)
        if not match:
            return "Please provide two team names (e.g., Glock Tua vs Luckier Than You)."

        left = match.group(1)
        right = match.group(2)
        for token in ("summary", "recap", "gameweek", "gw", "week"):
            left = re.sub(rf"\b{token}\b.*", "", left, flags=re.IGNORECASE).strip()
            right = re.sub(rf"\b{token}\b.*", "", right, flags=re.IGNORECASE).strip()

        entry_a_id, entry_a_name = self._resolve_team_exact(league_id, left, tool_events)
        if not entry_a_id:
            entry_a_id, entry_a_name, multiple = self._resolve_team(league_id, left, tool_events)
            if multiple:
                return f"I found multiple matches for '{left}': {self._format_candidates(multiple)}"
        entry_b_id, entry_b_name = self._resolve_team_exact(league_id, right, tool_events)
        if not entry_b_id:
            entry_b_id, entry_b_name, multiple = self._resolve_team(league_id, right, tool_events)
            if multiple:
                return f"I found multiple matches for '{right}': {self._format_candidates(multiple)}"

        if not entry_a_id or not entry_b_id:
            return "Couldn't resolve both teams. Please provide exact team names."

        summary = self._call_tool(tool_events, "league_summary", {"league_id": league_id, "gw": gw})
        if not isinstance(summary, dict):
            return "League summary is unavailable right now."

        entries = summary.get("entries", [])
        by_id = {e.get("entry_id"): e for e in entries}
        entry_a = by_id.get(entry_a_id)
        entry_b = by_id.get(entry_b_id)
        if not entry_a or not entry_b:
            return "Matchup not found for those teams in that GW."

        # Ensure entry_a is the row containing opponent info
        if entry_a.get("opponent_entry_id") != entry_b_id:
            if entry_b.get("opponent_entry_id") == entry_a_id:
                entry_a, entry_b = entry_b, entry_a
            else:
                return "Matchup not found for those teams in that GW."

        return render_matchup_md(summary, entry_a, entry_b)

    def _handle_waiver(
        self,
        text: str,
        tool_events: List[Dict[str, Any]],
        entry_id_override: Optional[int] = None,
        team_name_override: Optional[str] = None,
    ) -> str:
        league_id = self._extract_league_id(text) or self._default_league_id()
        gw = self._extract_gw(text)
        if gw is None:
            gw = self._default_gw()
        horizon = self._extract_horizon(text)
        entry_id = entry_id_override or self._extract_entry_id(text) or self._default_entry_id()

        team_name = team_name_override or self._default_entry_name()
        if not entry_id:
            entry_id, team_name, multiple = self._resolve_team(league_id, text, tool_events)
            if multiple:
                self._set_pending("waiver", league_id, multiple, text)
                return f"I found multiple matching teams: {self._format_candidates(multiple)} Which one do you mean?"
        if not entry_id:
            return "Which team should I run waivers for? Please provide a team name or entry ID."

        args: Dict[str, Any] = {"league_id": league_id, "entry_id": entry_id}
        if gw is not None:
            args["gw"] = gw
        if horizon is not None:
            args["horizon"] = horizon
        result = self._call_tool(tool_events, "waiver_recommendations", args)

        if not isinstance(result, dict):
            return "Waiver recommendations are unavailable right now."

        team_label = team_name or result.get("entry_name") or "your team"
        target_gw = result.get("target_gw") or gw or ""
        header = f"Top waiver adds for {team_label}"
        if target_gw:
            header += f" (GW{target_gw})"
        lines = [f"{header}:"]
        adds = result.get("top_adds", [])
        for i, add in enumerate(adds, 1):
            name = add.get("name") or "Unknown player"
            team = add.get("team") or ""
            pos = add.get("position_type")
            pos_label = POSITION_TYPE_LABELS.get(pos, "UNK")
            line = f"{i}. {name}"
            if team:
                line += f" ({team}, {pos_label})"
            else:
                line += f" ({pos_label})"
            drop = add.get("suggested_drop")
            if isinstance(drop, dict) and drop.get("name"):
                drop_team = drop.get("team") or ""
                if drop_team:
                    line += f" — drop {drop.get('name')} ({drop_team})"
                else:
                    line += f" — drop {drop.get('name')}"
            lines.append(line)
        warnings = result.get("warnings") or []
        if warnings:
            lines.append("Warnings:")
            lines.extend([f"- {w}" for w in warnings])
        return "\n".join(lines)

    def _handle_schedule(
        self,
        text: str,
        tool_events: List[Dict[str, Any]],
        entry_id_override: Optional[int] = None,
        team_name_override: Optional[str] = None,
    ) -> str:
        league_id = self._extract_league_id(text) or self._default_league_id()
        gw = self._extract_gw(text)
        if gw is None:
            gw = self._default_gw()
        horizon = self._extract_horizon(text) or 5
        entry_id = entry_id_override or self._extract_entry_id(text) or self._default_entry_id()

        team_name = team_name_override or self._default_entry_name()
        if not entry_id:
            entry_id, team_name, multiple = self._resolve_team(league_id, text, tool_events)
            if multiple:
                self._set_pending("schedule", league_id, multiple, text)
                return f"I found multiple matching teams: {self._format_candidates(multiple)} Which one do you mean?"
        if not entry_id:
            return "Which team do you want the schedule for?"

        args: Dict[str, Any] = {"league_id": league_id, "entry_id": entry_id, "horizon": horizon}
        if gw is not None:
            args["gw"] = gw
            args["horizon"] = 1
        result = self._call_tool(tool_events, "manager_schedule", args)
        if not isinstance(result, dict):
            return "Schedule data is unavailable right now."

        matches = result.get("matches", [])
        if not matches:
            return "No matches found for that request."

        entry_label = result.get("entry_name") or team_name or "That team"
        if gw is not None:
            match = matches[0]
            opp = match.get("opponent_name") or "an opponent"
            return f"{entry_label} plays {opp} in GW{match.get('gameweek')}."

        lines = [f"Upcoming matches for {entry_label}:"]
        for m in matches:
            opp = m.get("opponent_name") or "TBD"
            lines.append(f"- GW{m.get('gameweek')}: vs {opp}")
        return "\n".join(lines)

    def _handle_fixtures(self, text: str, tool_events: List[Dict[str, Any]]) -> str:
        league_id = self._extract_league_id(text) or self._default_league_id()
        gw = self._extract_gw(text)
        if gw is None:
            gw = self._default_gw()
        horizon = self._extract_horizon(text) or 5
        args: Dict[str, Any] = {"league_id": league_id, "horizon": horizon}
        if gw is not None:
            args["as_of_gw"] = gw
        result = self._call_tool(tool_events, "fixtures", args)
        if not isinstance(result, dict):
            return "Fixture data is unavailable right now."
        fixtures = result.get("fixtures", [])
        if not fixtures:
            return "No fixtures found."
        lines = [f"Upcoming fixtures (next {horizon} GWs):"]
        for f in fixtures[:20]:
            lines.append(f"- GW{f.get('event')}: {f.get('team_h_short')} vs {f.get('team_a_short')}")
        return "\n".join(lines)

    def _handle_player_form(self, text: str, tool_events: List[Dict[str, Any]]) -> str:
        league_id = self._extract_league_id(text) or self._default_league_id()
        gw = self._extract_gw(text)
        if gw is None:
            gw = self._default_gw()
        horizon = self._extract_horizon(text) or 5
        args: Dict[str, Any] = {"league_id": league_id, "horizon": horizon}
        args["as_of_gw"] = gw if gw is not None else 0
        result = self._call_tool(tool_events, "player_form", args)
        if not isinstance(result, dict):
            return "Player form data is unavailable right now."
        players = result.get("players", [])
        if not players:
            return "No player form data found."
        players = sorted(players, key=lambda p: p.get("points_per_gw", 0), reverse=True)[:10]
        lines = [f"Top 10 players by points/GW (horizon {horizon}):"]
        for p in players:
            name = p.get("name") or "Unknown player"
            team = p.get("team") or ""
            pts = p.get("points_per_gw", 0)
            if team:
                lines.append(f"- {name} ({team}): {pts:.2f} pts/GW")
            else:
                lines.append(f"- {name}: {pts:.2f} pts/GW")
        return "\n".join(lines)

    def _handle_streak(
        self,
        text: str,
        tool_events: List[Dict[str, Any]],
        entry_id_override: Optional[int] = None,
        team_name_override: Optional[str] = None,
    ) -> str:
        league_id = self._extract_league_id(text) or self._default_league_id()
        entry_id = entry_id_override or self._extract_entry_id(text) or self._default_entry_id()
        team_name = team_name_override or self._default_entry_name()
        if not entry_id:
            entry_id, team_name, multiple = self._resolve_team(league_id, text, tool_events)
            if multiple:
                self._set_pending("streak", league_id, multiple, text)
                return f"I found multiple matching teams: {self._format_candidates(multiple)} Which one do you mean?"
        if not entry_id:
            return "Which team should I check for win streaks?"
        args: Dict[str, Any] = {"league_id": league_id, "entry_id": entry_id}
        result = self._call_tool(tool_events, "manager_streak", args)
        if not isinstance(result, dict):
            return "Streak data is unavailable right now."
        entry_label = result.get("entry_name") or team_name or "That team"
        start_streak = result.get("start_win_streak", 0)
        current_streak = result.get("current_win_streak", 0)
        max_streak = result.get("max_win_streak", 0)
        return (
            f"{entry_label} win streaks: start-season streak {start_streak}, "
            f"current streak {current_streak}, max streak {max_streak}."
        )

    def _handle_wins_list(
        self,
        text: str,
        tool_events: List[Dict[str, Any]],
        entry_id_override: Optional[int] = None,
        team_name_override: Optional[str] = None,
    ) -> str:
        league_id = self._extract_league_id(text) or self._default_league_id()
        entry_id = entry_id_override or self._extract_entry_id(text) or self._default_entry_id()
        team_name = team_name_override or self._default_entry_name()
        if not entry_id:
            entry_id, team_name, multiple = self._resolve_team(league_id, text, tool_events)
            if multiple:
                self._set_pending("wins", league_id, multiple, text)
                return f"I found multiple matching teams: {self._format_candidates(multiple)} Which one do you mean?"
        if not entry_id:
            return "Which team do you want win weeks for?"

        args: Dict[str, Any] = {"league_id": league_id, "entry_id": entry_id, "gw": 1, "horizon": 38}
        result = self._call_tool(tool_events, "manager_schedule", args)
        if not isinstance(result, dict):
            return "Schedule data is unavailable right now."
        wins = [
            m.get("gameweek")
            for m in result.get("matches", [])
            if m.get("finished") and m.get("result") == "W"
        ]
        entry_label = result.get("entry_name") or team_name or "That team"
        if not wins:
            return f"{entry_label} has no completed wins yet."
        wins_list = ", ".join([f"GW{w}" for w in wins])
        return f"{entry_label} won in: {wins_list}."

    def _handle_transactions(self, text: str, tool_events: List[Dict[str, Any]]) -> str:
        league_id = self._extract_league_id(text) or self._default_league_id()
        gw = self._extract_gw(text)
        if gw is None:
            gw = self._default_gw()
        args: Dict[str, Any] = {"league_id": league_id, "gw": gw if gw is not None else 0}
        result = self._call_tool(tool_events, "transactions", args)
        if not isinstance(result, dict) or result.get("error"):
            err = result.get("error") if isinstance(result, dict) else None
            if err and "no such file or directory" in err:
                return (
                    "Transactions data is missing for that gameweek. "
                    "Refresh your local data (e.g., run the dev fetcher up to that GW) and retry."
                )
            return "Transactions data is unavailable right now."
        name_map = self._load_element_map()
        entries = result.get("entries", [])
        has_activity = any(
            (entry.get("total_in") or 0) > 0 or (entry.get("total_out") or 0) > 0 for entry in entries
        )
        gw_val = result.get("gameweek")
        lines = [f"Transactions summary (GW{gw_val}):"]
        if not has_activity:
            lines.append("No transactions recorded in the summary for this GW.")

        order_map: Dict[int, int] = {}
        if isinstance(gw_val, int) and gw_val > 1:
            standings = self._call_tool(tool_events, "standings", {"league_id": league_id, "gw": gw_val - 1})
            if isinstance(standings, dict) and standings.get("rows"):
                rows = standings.get("rows", [])
                rows_sorted = sorted(rows, key=lambda r: r.get("rank", 0), reverse=True)
                for idx, row in enumerate(rows_sorted):
                    entry_id = row.get("entry_id")
                    if entry_id is not None:
                        order_map[int(entry_id)] = idx

        def entry_sort_key(entry: Dict[str, Any]) -> Tuple[int, str]:
            entry_id = entry.get("entry_id")
            return (order_map.get(entry_id, 10**6), entry.get("entry_name") or "")

        entries_sorted = sorted(entries, key=entry_sort_key)
        for entry in entries_sorted:
            entry_name = entry.get("entry_name") or "Unknown team"
            waiver_in = entry.get("waiver_in") or []
            free_in = entry.get("free_in") or []
            trade_in = entry.get("trade_in") or []
            waiver_out = entry.get("waiver_out") or []
            free_out = entry.get("free_out") or []
            trade_out = entry.get("trade_out") or []
            lines.append(f"- {entry_name}:")
            if waiver_in or waiver_out:
                lines.append("  Waivers:")
                lines.append(
                    f"    + {', '.join([name_map.get(i, 'Unknown player') for i in waiver_in]) or 'None'}"
                )
                lines.append(
                    f"    - {', '.join([name_map.get(i, 'Unknown player') for i in waiver_out]) or 'None'}"
                )
            if free_in or free_out:
                lines.append("  Free Agents:")
                lines.append(
                    f"    + {', '.join([name_map.get(i, 'Unknown player') for i in free_in]) or 'None'}"
                )
                lines.append(
                    f"    - {', '.join([name_map.get(i, 'Unknown player') for i in free_out]) or 'None'}"
                )
            if trade_in or trade_out:
                lines.append("  Trades:")
                lines.append(
                    f"    + {', '.join([name_map.get(i, 'Unknown player') for i in trade_in]) or 'None'}"
                )
                lines.append(
                    f"    - {', '.join([name_map.get(i, 'Unknown player') for i in trade_out]) or 'None'}"
                )
            if not (waiver_in or waiver_out or free_in or free_out or trade_in or trade_out):
                lines.append("  No transactions.")
            lines.append("")
        return "\n".join(lines)

    def _handle_simple_tool(self, text: str, tool_events: List[Dict[str, Any]], tool: str, title: str) -> str:
        league_id = self._extract_league_id(text) or self._default_league_id()
        gw = self._extract_gw(text)
        if gw is None:
            gw = self._default_gw()
        args: Dict[str, Any] = {"league_id": league_id, "gw": gw if gw is not None else 0}
        result = self._call_tool(tool_events, tool, args)
        if isinstance(result, dict) and tool == "standings":
            return render_standings_md(result)
        if result:
            return f"{title} data is ready. Ask for a specific team or detail if you want a summary."
        return f"{title} data is unavailable."

    def _load_element_map(self) -> Dict[int, str]:
        if self._element_name_cache is not None:
            return self._element_name_cache
        path = Path(SETTINGS.data_dir) / "raw" / "bootstrap" / "bootstrap-static.json"
        if not path.exists():
            self._element_name_cache = {}
            return self._element_name_cache
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        out: Dict[int, str] = {}
        for e in data.get("elements", []):
            name = e.get("web_name") or (
                str(e.get("first_name", "")) + " " + str(e.get("second_name", ""))
            ).strip()
            if e.get("id"):
                out[int(e["id"])] = name
        self._element_name_cache = out
        return out
