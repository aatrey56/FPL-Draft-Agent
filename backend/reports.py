import json
import os
from datetime import datetime
from typing import Any, Dict, Tuple

from zoneinfo import ZoneInfo

from .config import SETTINGS
from .llm import LLMClient
from .mcp_client import MCPClient


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _report_paths(gw: int, name: str) -> Tuple[str, str]:
    folder = os.path.join(SETTINGS.reports_dir, f"gw_{gw}")
    _ensure_dir(folder)
    md = os.path.join(folder, f"{name}.md")
    js = os.path.join(folder, f"{name}.json")
    return md, js


def _now_local() -> str:
    tz = ZoneInfo(SETTINGS.timezone)
    return datetime.now(tz=tz).isoformat(timespec="seconds")


def generate_waiver_report(mcp: MCPClient, llm: LLMClient, league_id: int, entry_id: int, gw: int) -> Dict[str, Any]:
    report = mcp.call_tool(
        "waiver_recommendations",
        {
            "league_id": league_id,
            "entry_id": entry_id,
            "gw": gw,
            "horizon": 5,
            "weight_fixtures": 0.35,
            "weight_form": 0.25,
            "weight_total_points": 0.25,
            "weight_xg": 0.15,
            "limit": 5,
        },
    )
    md = render_waiver_md(report, llm)
    return {"json": report, "md": md}


def generate_league_summary(mcp: MCPClient, llm: LLMClient, league_id: int, gw: int) -> Dict[str, Any]:
    summary = mcp.call_tool("league_summary", {"league_id": league_id, "gw": gw})
    if not isinstance(summary, dict):
        return {"json": {"error": str(summary)}, "md": f"# League Summary Error\n\n{summary}\n"}
    md = render_league_summary_md(summary, llm)
    return {"json": summary, "md": md}


def generate_trades_report(mcp: MCPClient, llm: LLMClient, league_id: int, gw: int) -> Dict[str, Any]:
    tx = mcp.call_tool("transactions", {"league_id": league_id, "gw": gw})
    if not isinstance(tx, dict):
        return {"json": {"error": str(tx)}, "md": f"# Transactions Error\n\n{tx}\n"}
    md = render_trades_md(tx, llm)
    return {"json": tx, "md": md}


def generate_starting_xi_report(mcp: MCPClient, llm: LLMClient, league_id: int, entry_id: int, gw: int) -> Dict[str, Any]:
    summary = mcp.call_tool("league_summary", {"league_id": league_id, "gw": gw})
    try:
        fixtures = mcp.call_tool("fixtures", {"league_id": league_id, "as_of_gw": gw + 1, "horizon": 1})
    except Exception:
        fixtures = load_bootstrap_fixtures(gw + 1)
    if not isinstance(fixtures, dict):
        fixtures = load_bootstrap_fixtures(gw + 1)
    if not isinstance(summary, dict) or not isinstance(fixtures, dict):
        return {"json": {"error": "missing summary/fixtures"}, "md": "# Starting XI Error\n\nMissing summary or fixtures.\n"}
    md, out = render_starting_xi_md(summary, fixtures, entry_id, llm)
    return {"json": out, "md": md}


def save_report(gw: int, name: str, content: Dict[str, Any]) -> Dict[str, str]:
    md_path, json_path = _report_paths(gw, name)
    payload = {
        "generated_at": _now_local(),
        "data": content["json"],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(content["md"])
    return {"md": md_path, "json": json_path}


def render_waiver_md(report: Dict[str, Any], llm: LLMClient) -> str:
    if not llm.available():
        return _simple_waiver_md(report)
    prompt = (
        "Create a concise waiver report in Markdown with top adds and drop candidates by position. "
        "Include one-line reasons per add and surface any warnings. Use the JSON data below.\n\n"
        + json.dumps(report)
    )
    text = llm.generate("You are a precise fantasy football analyst.", prompt)
    return text if text else _simple_waiver_md(report)


def _simple_waiver_md(report: Dict[str, Any]) -> str:
    lines = [f"# Waiver Report GW{report.get('target_gw')}"]
    lines.append("")
    warnings = report.get("warnings") or []
    if warnings:
        lines.append("## Warnings")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")
    lines.append("## Top Adds")
    for a in report.get("top_adds", []):
        reasons = "; ".join(a.get("reasons", []))
        prev = ""
        if a.get("previous_owners"):
            prev = f" | Prev owners: {', '.join(a['previous_owners'])}"
        lines.append(f"- {a['name']} ({a['team']}): {reasons}{prev}")
    lines.append("")
    lines.append("## Drop Candidates")
    drops_by_pos = report.get("drop_candidates_by_position") or {}
    if drops_by_pos:
        for pos in ["GK", "DEF", "MID", "FWD"]:
            group = drops_by_pos.get(pos, [])
            if not group:
                continue
            lines.append(f"### {pos}")
            for d in group:
                lines.append(f"- {d['name']} ({d['team']}): {d['reason']}")
    else:
        for d in report.get("drop_candidates", []):
            lines.append(f"- {d['name']} ({d['team']}): {d['reason']}")
    lines.append("")
    return "\n".join(lines)


def render_league_summary_md(summary: Dict[str, Any], llm: LLMClient) -> str:
    if not llm.available():
        return _simple_league_md(summary)
    prompt = "Summarize this league summary in Markdown: matchups and key notes.\n\n" + json.dumps(summary)
    text = llm.generate("You are a fantasy football recap writer.", prompt)
    return text if text else _simple_league_md(summary)


def _simple_league_md(summary: Dict[str, Any]) -> str:
    lines = [f"# League Summary GW{summary.get('gameweek')}"]
    lines.append("")
    for e in summary.get("entries", []):
        lines.append(f"- {e['entry_name']} {e['score_for']}-{e['score_against']} vs {e['opponent_name']} ({e['result']})")
    lines.append("")
    return "\n".join(lines)


def render_trades_md(tx: Dict[str, Any], llm: LLMClient) -> str:
    if not llm.available():
        return _simple_trades_md(tx)
    prompt = "Summarize recent trades/waivers in Markdown, note implications.\n\n" + json.dumps(tx)
    text = llm.generate("You are a fantasy football analyst.", prompt)
    return text if text else _simple_trades_md(tx)


def _simple_trades_md(tx: Dict[str, Any]) -> str:
    lines = ["# Transactions Summary"]
    lines.append("")
    lines.append("No trade recommendation model configured. Recent transactions:")
    for entry in tx.get("entries", []):
        if entry.get("total_in", 0) == 0 and entry.get("total_out", 0) == 0:
            continue
        lines.append(f"- {entry['entry_name']}: +{entry['total_in']} / -{entry['total_out']}")
    lines.append("")
    return "\n".join(lines)


def render_starting_xi_md(summary: Dict[str, Any], fixtures: Dict[str, Any], entry_id: int, llm: LLMClient) -> Tuple[str, Dict[str, Any]]:
    roster = []
    opponent_by_team = {}
    venue_by_team = {}
    for f in fixtures.get("fixtures", []):
        opponent_by_team[f["team_h_short"]] = f["team_a_short"]
        venue_by_team[f["team_h_short"]] = "HOME"
        opponent_by_team[f["team_a_short"]] = f["team_h_short"]
        venue_by_team[f["team_a_short"]] = "AWAY"
    for e in summary.get("entries", []):
        if e.get("entry_id") == entry_id:
            roster = e.get("roster", [])
            break
    # simple fixture-based sort: HOME first, then AWAY
    scored = []
    for r in roster:
        team = r["team"]
        venue = venue_by_team.get(team, "NA")
        score = 1 if venue == "HOME" else 0.5 if venue == "AWAY" else 0
        scored.append((score, r, opponent_by_team.get(team, "TBD"), venue))
    scored.sort(key=lambda x: x[0], reverse=True)
    starters = scored[:11]
    out = {
        "entry_id": entry_id,
        "starters": [
            {"name": s[1]["name"], "team": s[1]["team"], "opponent": s[2], "venue": s[3]}
            for s in starters
        ],
        "method": "Simple fixture-only: HOME > AWAY > no fixture.",
    }
    lines = ["# Starting XI Recommendations (Fixture-Only)"]
    lines.append("")
    for s in out["starters"]:
        lines.append(f"- {s['name']} ({s['team']}) vs {s['opponent']} ({s['venue']})")
    lines.append("")
    return "\n".join(lines), out


def load_bootstrap_fixtures(gw: int) -> Dict[str, Any]:
    path = "data/raw/bootstrap/bootstrap-static.json"
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    fixtures = []
    teams = {t["id"]: t["short_name"] for t in data.get("teams", [])}
    gw_key = str(gw)
    for f in data.get("fixtures", {}).get(gw_key, []):
        fixtures.append(
            {
                "fixture_id": f.get("id"),
                "event": f.get("event"),
                "team_h": f.get("team_h"),
                "team_a": f.get("team_a"),
                "team_h_short": teams.get(f.get("team_h"), ""),
                "team_a_short": teams.get(f.get("team_a"), ""),
                "kickoff_utc": f.get("kickoff_time"),
                "finished": f.get("finished", False),
                "started": f.get("started", False),
            }
        )
    return {"fixtures": fixtures}
