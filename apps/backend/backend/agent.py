import json
from typing import Any, Dict, List, Optional, Tuple
import re
from pathlib import Path

from .llm import LLMClient, try_parse_json
from .mcp_client import MCPClient
from .config import SETTINGS
from .reports import render_league_summary_md, render_standings_md, render_matchup_md, render_lineup_efficiency_md
from .rag import get_rag_index, format_rag_docs


SYSTEM_PROMPT = """You are a data-accurate assistant. You MUST call tools for any factual data.
Return ONLY a JSON object in one of these forms:
1) {"action":"tool","name":"tool_name","arguments":{...}}
2) {"action":"final","content":"..."}
If you need more data, choose action=tool. Do not guess.
If a section labeled "Memory (cached data)" is provided, you may use it as factual context without another tool call.

Important tool-routing guidance:
- For questions like "who does X play", "schedule", or "matchup", use manager_schedule (NOT league_summary).
- For "list all teams in my league" or to resolve a team name to entry_id, use league_entries.
- For win streak questions (e.g., "how many games in a row did X win"), use manager_streak.
- For roster/player lists for a team, use league_summary with league_id and gw.
- Never output element IDs. Always use player names. If only IDs are available, call player_lookup or map from bootstrap.
- If required arguments are missing or ambiguous (e.g., team name), ask a follow-up using action=final.
- Never pass null values in arguments. Omit missing fields instead.

Example tool call:
{"action":"tool","name":"league_summary","arguments":{"league_id":14204,"gw":0}}
"""


class Agent:
    def __init__(self, mcp: MCPClient, llm: LLMClient) -> None:
        self.mcp = mcp
        self.llm = llm
        self._element_name_cache: Optional[Dict[int, str]] = None
        self._pending_intent: Optional[str] = None
        self._pending_candidates: Optional[List[str]] = None
        self._pending_league_id: Optional[int] = None
        self._pending_text: Optional[str] = None
        self._session: Dict[str, Any] = {
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
        max_steps: int = 4,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
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
        league_id = int(out.get("league_id", 0)) if out.get("league_id") is not None else 0
        if league_id == 0:
            league_id = self._default_league_id()
        if name in ("waiver_recommendations", "manager_schedule", "manager_streak"):
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
        ):
            out.setdefault("league_id", league_id)
            if "gw" not in out and self._default_gw() is not None:
                out["gw"] = self._default_gw()
            out.setdefault("gw", 0)
        if name in ("league_entries", "manager_schedule", "manager_streak"):
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
        return out

    def _default_league_id(self) -> int:
        league_id = self._session.get("league_id")
        if league_id:
            try:
                return int(league_id)
            except Exception:
                return 14204
        return 14204

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

    def _try_route(self, user_message: str, tool_events: List[Dict[str, Any]]) -> Optional[str]:
        text = user_message.strip()
        if not text:
            return None
        pending = self._maybe_resolve_pending(text, tool_events)
        if pending is not None:
            return pending
        lower = text.lower()

        if self._looks_like_waiver(lower):
            return self._handle_waiver(text, tool_events)
        if self._looks_like_streak(lower):
            return self._handle_streak(text, tool_events)
        if self._looks_like_win_list(lower):
            return self._handle_wins_list(text, tool_events)
        if self._looks_like_schedule(lower):
            return self._handle_schedule(text, tool_events)
        if self._looks_like_fixtures(lower):
            return self._handle_fixtures(text, tool_events)
        if self._looks_like_player_form(lower):
            return self._handle_player_form(text, tool_events)
        if self._looks_like_standings(lower):
            return self._handle_simple_tool(text, tool_events, "standings", "Standings")
        if self._looks_like_league_summary(lower):
            return self._handle_league_summary(text, tool_events)
        if self._looks_like_transactions(lower):
            return self._handle_transactions(text, tool_events)
        if self._looks_like_lineup(lower):
            return self._handle_lineup_efficiency(text, tool_events)
        if self._looks_like_strength(lower):
            return self._handle_simple_tool(text, tool_events, "strength_of_schedule", "Strength of schedule")
        if self._looks_like_ownership(lower):
            return self._handle_simple_tool(text, tool_events, "ownership_scarcity", "Ownership scarcity")
        if self._looks_like_matchup_summary(lower):
            return self._handle_matchup_summary(text, tool_events)
        if "league entries" in lower or "all teams" in lower:
            return self._handle_simple_tool(text, tool_events, "league_entries", "League teams")

        league_id = self._extract_league_id(text) or self._default_league_id()
        if self._looks_like_team_name_only(text, league_id, tool_events):
            team_name = text.strip()
            return (
                f"What would you like to know about {team_name}? "
                "Examples: waivers, schedule, win streaks, fixtures."
            )

        return None

    def _call_tool(self, tool_events: List[Dict[str, Any]], name: str, args: Dict[str, Any]) -> Any:
        self._note_tool_use(name, args)
        tool_events.append({"type": "tool_call", "name": name, "arguments": args})
        try:
            result = self.mcp.call_tool(name, args)
        except Exception as exc:
            err_msg = str(exc)
            tool_events.append({"type": "tool_error", "name": name, "error": err_msg})
            return {"error": err_msg}
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
        match = re.search(r"league\s*(?:id)?\s*[:=#]?\s*(\d{4,6})", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    def _extract_gw(self, text: str) -> Optional[int]:
        match = re.search(r"(?:gw|gameweek|game\s*week|week)\s*[:=#]?\s*(\d{1,2})", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    def _extract_horizon(self, text: str) -> Optional[int]:
        match = re.search(r"horizon\s*[:=#]?\s*(\d{1,2})", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    def _extract_entry_id(self, text: str) -> Optional[int]:
        match = re.search(r"(?:entry[_\s-]*id|entry)\s*[:=#]?\s*(\d{4,8})", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

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

    def _looks_like_waiver(self, text: str) -> bool:
        return "waiver" in text or "waivers" in text or "waiver rec" in text

    def _looks_like_schedule(self, text: str) -> bool:
        return "who does" in text and "play" in text or "schedule" in text

    def _looks_like_fixtures(self, text: str) -> bool:
        return "fixtures" in text or "fixture list" in text

    def _looks_like_player_form(self, text: str) -> bool:
        return "player_form" in text or "player form" in text or "form table" in text

    def _looks_like_streak(self, text: str) -> bool:
        return ("streak" in text or "in a row" in text) and "win" in text

    def _looks_like_win_list(self, text: str) -> bool:
        if "win" not in text and "won" not in text and "wins" not in text:
            return False
        return "week" in text or "gw" in text or "gameweek" in text

    def _looks_like_standings(self, text: str) -> bool:
        return "standings" in text or "table" in text

    def _looks_like_league_summary(self, text: str) -> bool:
        return "league summary" in text or ("summary" in text and "league" in text)

    def _looks_like_transactions(self, text: str) -> bool:
        return "transactions" in text or "waivers" in text and "summary" in text or "trades" in text

    def _looks_like_lineup(self, text: str) -> bool:
        return "lineup efficiency" in text or "bench points" in text or "bench" in text

    def _looks_like_strength(self, text: str) -> bool:
        return "strength of schedule" in text or "schedule difficulty" in text

    def _looks_like_ownership(self, text: str) -> bool:
        return "ownership" in text or "scarcity" in text

    def _looks_like_matchup_summary(self, text: str) -> bool:
        return (" vs " in text or " vs. " in text) and ("summary" in text or "recap" in text)

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
        )
        if any(k in lowered for k in keywords):
            return False
        entry_id, _ = self._resolve_team_exact(league_id, text, tool_events)
        return entry_id is not None

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
        match = re.search(r"(.+?)\\s+vs\\.?\\s+(.+)", text, re.IGNORECASE)
        if not match:
            return "Please provide two team names (e.g., Glock Tua vs Luckier Than You)."

        left = match.group(1)
        right = match.group(2)
        for token in ("summary", "recap", "gameweek", "gw", "week"):
            left = re.sub(rf"\\b{token}\\b.*", "", left, flags=re.IGNORECASE).strip()
            right = re.sub(rf"\\b{token}\\b.*", "", right, flags=re.IGNORECASE).strip()

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
            # Swap if necessary
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
            pos_label = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}.get(pos, "UNK")
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
            add_ids = list(waiver_in) + list(free_in) + list(trade_in)
            out_ids = list(waiver_out) + list(free_out) + list(trade_out)
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
