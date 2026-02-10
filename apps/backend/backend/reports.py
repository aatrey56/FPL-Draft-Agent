import json
import os
from datetime import datetime
from typing import Any, Dict, Tuple, Optional

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


def _report_rel(path: str) -> str:
    try:
        rel = os.path.relpath(path, SETTINGS.reports_dir)
    except ValueError:
        return path
    return rel.replace(os.sep, "/")


def _now_local() -> str:
    tz = ZoneInfo(SETTINGS.timezone)
    return datetime.now(tz=tz).isoformat(timespec="seconds")


def generate_waiver_report(
    mcp: MCPClient,
    llm: LLMClient,
    league_id: int,
    entry_id: int,
    gw: int,
    weights: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    weights = weights or {}
    w_fix = float(weights.get("fixtures", 0.35) or 0.35)
    w_form = float(weights.get("form", 0.25) or 0.25)
    w_total = float(weights.get("total", 0.25) or 0.25)
    w_xg = float(weights.get("xg", 0.15) or 0.15)
    report = mcp.call_tool(
        "waiver_recommendations",
        {
            "league_id": league_id,
            "entry_id": entry_id,
            "gw": gw,
            "horizon": 5,
            "weight_fixtures": w_fix,
            "weight_form": w_form,
            "weight_total_points": w_total,
            "weight_xg": w_xg,
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


def generate_starting_xi_report(
    mcp: MCPClient,
    llm: LLMClient,
    league_id: int,
    entry_id: int,
    gw: int,
    weights: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    summary = mcp.call_tool("league_summary", {"league_id": league_id, "gw": gw})

    try:
        form = mcp.call_tool("player_form", {"league_id": league_id, "as_of_gw": gw, "horizon": 5})
    except Exception:
        form = {}
    if not isinstance(form, dict):
        form = {}

    try:
        next_gw = gw + 1 if gw else 0
        fixture_difficulty = mcp.call_tool(
            "fixture_difficulty",
            {
                "league_id": league_id,
                "as_of_gw": gw,
                "next_gw": next_gw,
                "horizon": 5,
                "limit": 40,
                "include_raw": True,
            },
        )
    except Exception:
        fixture_difficulty = {}
    if not isinstance(fixture_difficulty, dict):
        fixture_difficulty = {}

    next_gw = fixture_difficulty.get("next_gw") or (gw + 1 if gw else 0)
    try:
        fixtures = mcp.call_tool("fixtures", {"league_id": league_id, "as_of_gw": next_gw, "horizon": 1})
    except Exception:
        fixtures = load_bootstrap_fixtures(next_gw)
    if not isinstance(fixtures, dict):
        fixtures = load_bootstrap_fixtures(next_gw)

    if not isinstance(summary, dict) or not isinstance(fixtures, dict):
        return {"json": {"error": "missing summary/fixtures"}, "md": "# Starting XI Error\n\nMissing summary or fixtures.\n"}
    md, out = render_starting_xi_md(summary, fixtures, entry_id, llm, form, fixture_difficulty, weights or {})
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
    return {"md": _report_rel(md_path), "json": _report_rel(json_path)}


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
    lines.append("## Scoring")
    lines.append(
        f"Weighted score = {report.get('weight_fixtures', 0):.2f}*fixture_norm + "
        f"{report.get('weight_form', 0):.2f}*form_norm + "
        f"{report.get('weight_total_points', 0):.2f}*total_norm + "
        f"{report.get('weight_xg', 0):.2f}*xg_norm"
    )
    lines.append(
        f"Fixture score blend = {report.get('fixture_season_weight', 0):.2f} season / "
        f"{report.get('fixture_recent_weight', 0):.2f} recent (home/away by position)."
    )
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
    return _simple_league_md(summary)


def _load_points_map(league_id: int, entry_id: int, gw: int) -> Dict[int, float]:
    path = os.path.join(
        SETTINGS.data_dir,
        "derived",
        "points",
        str(league_id),
        "entry",
        str(entry_id),
        "gw",
        f"{gw}.json",
    )
    if not os.path.exists(path):
        return {}
    try:
        payload = json.loads(open(path, "r", encoding="utf-8").read())
    except Exception:
        return {}
    out: Dict[int, float] = {}
    for p in payload.get("players", []):
        try:
            out[int(p.get("element"))] = float(p.get("total", 0) or 0)
        except Exception:
            continue
    return out


def _best_starter(entry: Dict[str, Any], points: Dict[int, float]) -> str:
    starters = [r for r in entry.get("roster", []) if r.get("role") == "starter"]
    best_name = ""
    best_pos = ""
    best_pts = None
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    for r in starters:
        elem = r.get("element")
        if elem is None:
            continue
        pts = points.get(int(elem))
        if pts is None:
            continue
        if best_pts is None or pts > best_pts:
            best_pts = pts
            best_name = r.get("name") or ""
            best_pos = pos_map.get(r.get("position_type"), "")
    if best_pts is None:
        return "n/a"
    if best_pos:
        return f"{best_name} [{best_pos}] ({best_pts:g})"
    return f"{best_name} ({best_pts:g})"


def _simple_league_md(summary: Dict[str, Any]) -> str:
    lines = [f"# League Summary GW{summary.get('gameweek')}"]
    lines.append("")
    lines.append("| Matchup | Score | Result | Best Starters |")
    lines.append("|---|---:|:---:|:---|")

    entries = summary.get("entries", [])
    by_id = {e.get("entry_id"): e for e in entries}
    gw = int(summary.get("gameweek", 0) or 0)
    seen = set()
    for e in entries:
        entry_id = e.get("entry_id")
        opp_id = e.get("opponent_entry_id")
        if not entry_id or not opp_id:
            continue
        key = tuple(sorted((entry_id, opp_id)))
        if key in seen:
            continue
        seen.add(key)
        opp = by_id.get(opp_id, {})
        team_a = e.get("entry_name", "Unknown")
        team_b = e.get("opponent_name", opp.get("entry_name", "Unknown"))
        score = f"{e.get('score_for', 0)}–{e.get('score_against', 0)}"
        result = f"{e.get('result', '')}/{opp.get('result', '')}".strip("/")
        points_a = _load_points_map(summary.get("league_id", 0), entry_id, gw)
        points_b = _load_points_map(summary.get("league_id", 0), opp_id, gw)
        best_a = _best_starter(e, points_a)
        best_b = _best_starter(opp, points_b)
        best = f"{team_a}: {best_a}; {team_b}: {best_b}"
        lines.append(f"| {team_a} vs {team_b} | {score} | {result} | {best} |")
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


def render_standings_md(standings: Dict[str, Any]) -> str:
    lines = [f"# Standings GW{standings.get('gameweek')}"]
    lines.append("")
    lines.append("| Rank | Team | W-D-L | MPts | PF | PA |")
    lines.append("|---:|---|:---:|---:|---:|---:|")
    for row in standings.get("rows", []):
        w = row.get("wins", 0)
        d = row.get("draws", 0)
        l = row.get("losses", 0)
        lines.append(
            f"| {row.get('rank', '')} | {row.get('entry_name', '')} | {w}-{d}-{l} | "
            f"{row.get('match_points', '')} | {row.get('points_for', '')} | {row.get('points_against', '')} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_lineup_efficiency_md(summary: Dict[str, Any], entry_id: int = 0) -> str:
    gw = summary.get("gameweek")
    lines = [f"# Lineup Efficiency GW{gw}"]
    lines.append("")
    entries = summary.get("entries", [])
    if entry_id:
        entries = [e for e in entries if e.get("entry_id") == entry_id]
    if not entries:
        return "\n".join(lines + ["No lineup efficiency data found."])
    lines.append("| Team | Bench Pts | Bench Pts Played | Zero-Min Starters |")
    lines.append("|---|---:|---:|---:|")
    for e in entries:
        lines.append(
            f"| {e.get('entry_name','')} | {e.get('bench_points',0)} | {e.get('bench_points_played',0)} | {e.get('zero_minute_starter_count',0)} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_matchup_md(summary: Dict[str, Any], entry_a: Dict[str, Any], entry_b: Dict[str, Any]) -> str:
    league_id = int(summary.get("league_id", 0) or 0)
    gw = int(summary.get("gameweek", 0) or 0)
    points_a = _load_points_map(league_id, entry_a.get("entry_id", 0), gw)
    points_b = _load_points_map(league_id, entry_b.get("entry_id", 0), gw)
    best_a = _best_starter(entry_a, points_a)
    best_b = _best_starter(entry_b, points_b)
    score = f"{entry_a.get('score_for', 0)}–{entry_a.get('score_against', 0)}"
    result = f"{entry_a.get('result', '')}/{entry_b.get('result', '')}".strip("/")
    a_points = entry_a.get("points", {})
    b_points = entry_b.get("points", {})
    lines = [f"# Matchup Summary GW{gw}"]
    lines.append("")
    lines.append(f"**{entry_a.get('entry_name')} vs {entry_b.get('entry_name')}**")
    lines.append("")
    lines.append("| Item | Value |")
    lines.append("|---|---|")
    lines.append(f"| Score | {score} |")
    lines.append(f"| Result | {result} |")
    lines.append(f"| Best Starters | {entry_a.get('entry_name')}: {best_a}; {entry_b.get('entry_name')}: {best_b} |")
    lines.append(
        f"| Starters/Bench | {entry_a.get('entry_name')}: {a_points.get('starters', 0)}/{a_points.get('bench', 0)}; "
        f"{entry_b.get('entry_name')}: {b_points.get('starters', 0)}/{b_points.get('bench', 0)} |"
    )
    lines.append("")
    return "\n".join(lines)


def render_starting_xi_md(
    summary: Dict[str, Any],
    fixtures: Dict[str, Any],
    entry_id: int,
    llm: LLMClient,
    form: Dict[str, Any],
    fixture_difficulty: Dict[str, Any],
    weights_in: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
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

    form_map = {}
    for p in form.get("players", []):
        form_map[p.get("element")] = p

    xgi_map = load_bootstrap_xgi()

    fixture_map: Dict[int, Dict[str, Dict[str, Any]]] = {1: {}, 2: {}, 3: {}, 4: {}}
    positions = fixture_difficulty.get("positions", {}) if isinstance(fixture_difficulty, dict) else {}
    pos_map = {"GK": 1, "DEF": 2, "MID": 3, "FWD": 4}
    for label, rows in positions.items():
        pos = pos_map.get(label)
        if not pos or not isinstance(rows, list):
            continue
        for row in rows:
            team_short = row.get("team_short")
            if not team_short:
                continue
            if team_short not in opponent_by_team:
                opponent_by_team[team_short] = row.get("opponent_short") or "TBD"
            if team_short not in venue_by_team:
                venue_by_team[team_short] = row.get("venue") or "NA"
            fixture_map[pos][team_short] = {
                "score": row.get("score") if row.get("score") is not None else 0.0,
                "opponent": row.get("opponent_short") or opponent_by_team.get(team_short, "TBD"),
                "venue": row.get("venue") or venue_by_team.get(team_short, "NA"),
            }

    def norm(value: float, min_val: float, max_val: float) -> float:
        if max_val <= min_val:
            return 0.0
        return (value - min_val) / (max_val - min_val)

    scored_players = []
    raw_fixture = []
    raw_form = []
    raw_xgi = []
    raw_minutes = []
    for r in roster:
        element = r.get("element")
        team = r.get("team")
        pos = r.get("position_type")
        form_row = form_map.get(element, {})
        fixture_row = fixture_map.get(pos, {}).get(team, {})
        fixture_score = fixture_row.get("score", 0.0) or 0.0
        form_score = float(form_row.get("points_per_gw", 0) or 0)
        minutes_score = float(form_row.get("minutes_per_gw", 0) or 0)
        xgi_score = float(xgi_map.get(element, 0.0) or 0.0)
        raw_fixture.append(fixture_score)
        raw_form.append(form_score)
        raw_xgi.append(xgi_score)
        raw_minutes.append(minutes_score)
        scored_players.append(
            {
                "element": element,
                "name": r.get("name"),
                "team": team,
                "position_type": pos,
                "opponent": fixture_row.get("opponent") or opponent_by_team.get(team, "TBD"),
                "venue": fixture_row.get("venue") or venue_by_team.get(team, "NA"),
                "fixture_score": fixture_score,
                "form_score": form_score,
                "xgi_score": xgi_score,
                "minutes_score": minutes_score,
            }
        )

    def min_max(vals: list) -> Tuple[float, float]:
        if not vals:
            return 0.0, 0.0
        return min(vals), max(vals)

    f_min, f_max = min_max(raw_fixture)
    form_min, form_max = min_max(raw_form)
    xgi_min, xgi_max = min_max(raw_xgi)
    min_min, min_max_v = min_max(raw_minutes)

    w_fix = float(weights_in.get("fixtures", 0.35) or 0.35)
    w_form = float(weights_in.get("form", 0.30) or 0.30)
    w_xgi = float(weights_in.get("xgi", 0.25) or 0.25)
    w_min = float(weights_in.get("minutes", 0.10) or 0.10)
    weight_sum = w_fix + w_form + w_xgi + w_min
    if weight_sum <= 0:
        weight_sum = 1.0
    weights = {
        "fixtures": w_fix / weight_sum,
        "form": w_form / weight_sum,
        "xgi": w_xgi / weight_sum,
        "minutes": w_min / weight_sum,
    }
    for p in scored_players:
        p["fixture_norm"] = norm(p["fixture_score"], f_min, f_max)
        p["form_norm"] = norm(p["form_score"], form_min, form_max)
        p["xgi_norm"] = norm(p["xgi_score"], xgi_min, xgi_max)
        p["minutes_norm"] = norm(p["minutes_score"], min_min, min_max_v)
        p["total_score"] = (
            weights["fixtures"] * p["fixture_norm"]
            + weights["form"] * p["form_norm"]
            + weights["xgi"] * p["xgi_norm"]
            + weights["minutes"] * p["minutes_norm"]
        )

    gks = [p for p in scored_players if p["position_type"] == 1]
    defs = sorted([p for p in scored_players if p["position_type"] == 2], key=lambda x: x["total_score"], reverse=True)
    mids = sorted([p for p in scored_players if p["position_type"] == 3], key=lambda x: x["total_score"], reverse=True)
    fwds = sorted([p for p in scored_players if p["position_type"] == 4], key=lambda x: x["total_score"], reverse=True)

    gk = max(gks, key=lambda x: x["total_score"]) if gks else None
    formations = []
    for d in range(3, 6):
        for m in range(2, 6):
            for f in range(1, 4):
                if d + m + f == 10:
                    formations.append((d, m, f))
    best = None
    for d, m, f in formations:
        if len(defs) < d or len(mids) < m or len(fwds) < f:
            continue
        lineup = defs[:d] + mids[:m] + fwds[:f]
        score = sum(p["total_score"] for p in lineup)
        if best is None or score > best["score"]:
            best = {"def": d, "mid": m, "fwd": f, "players": lineup, "score": score}

    warnings = []
    starters = []
    if gk:
        starters.append(gk)
    else:
        warnings.append("No goalkeeper found on roster; lineup may be invalid.")
    if best:
        starters.extend(best["players"])
    else:
        warnings.append("No valid formation found with current roster constraints.")

    starters = sorted(starters, key=lambda x: (x["position_type"], -x["total_score"]))
    starters = starters[:11]
    if len(starters) != 11:
        warnings.append("Could not fill exactly 11 starters with the required position constraints.")

    out = {
        "entry_id": entry_id,
        "formation": {"DEF": best["def"], "MID": best["mid"], "FWD": best["fwd"]} if best else None,
        "weights": weights,
        "scoring_formula": "score = w_fix*fixture_norm + w_form*form_norm + w_xgi*xgi_norm + w_minutes*minutes_norm (min-max normalized within roster)",
        "warnings": warnings,
        "starters": [
            {
                "name": s["name"],
                "team": s["team"],
                "position_type": s["position_type"],
                "opponent": s["opponent"],
                "venue": s["venue"],
                "score": round(s["total_score"], 4),
                "components": {
                    "fixture": round(s["fixture_norm"], 3),
                    "form": round(s["form_norm"], 3),
                    "xgi": round(s["xgi_norm"], 3),
                    "minutes": round(s["minutes_norm"], 3),
                },
            }
            for s in starters
        ],
        "method": "Weighted score using fixture difficulty, recent form, expected goal involvements, and minutes.",
    }

    lines = ["# Starting XI Recommendations"]
    lines.append("")
    if best:
        lines.append(f"Formation: {best['def']}-{best['mid']}-{best['fwd']}")
        lines.append("")
    lines.append("Scoring:")
    lines.append(
        f"score = {weights['fixtures']:.2f}*fixture_norm + {weights['form']:.2f}*form_norm + "
        f"{weights['xgi']:.2f}*xgi_norm + {weights['minutes']:.2f}*minutes_norm"
    )
    lines.append("")
    if warnings:
        lines.append("Warnings:")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")
    for s in out["starters"]:
        pos_label = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}.get(s["position_type"], "UNK")
        lines.append(
            f"- {s['name']} ({s['team']}, {pos_label}) vs {s['opponent']} ({s['venue']}) "
            f"| score {s['score']}"
        )
    lines.append("")
    return "\n".join(lines), out


def load_bootstrap_fixtures(gw: int) -> Dict[str, Any]:
    path = os.path.join(SETTINGS.data_dir, "raw", "bootstrap", "bootstrap-static.json")
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


def load_bootstrap_xgi() -> Dict[int, float]:
    path = os.path.join(SETTINGS.data_dir, "raw", "bootstrap", "bootstrap-static.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    out = {}
    for e in data.get("elements", []):
        element_id = e.get("id")
        if element_id is None:
            continue
        out[int(element_id)] = float(e.get("expected_goal_involvements", 0) or 0.0)
    return out
