"""my_week — start/sit for the next gameweek.

Answers: *who starts, who benches, and what needs my attention before the
deadline?* Same honest v1 scaffold as waiver_plan (season projection / 38,
mild fixture multiplier, live availability gate) but scored over the single
next event only. The match xP model (MATCH_MODEL_SPEC) replaces the scoring
core once in-season data accumulates; the XI selection and warnings stay.

Reads local files only. CLI:
    python -m backend.ml.myweek --league <id> --entry <id>
(env fallback LEAGUE_ID / ENTRY_ID; writes data/derived/<season>/ml/my_week.json)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

from backend.ml import jsonutil
from backend.ml import waiver as wv

logger = logging.getLogger(__name__)


def next_event(bootstrap: dict) -> int | None:
    """The upcoming event number = smallest key of the next-events fixture map."""
    events = sorted(int(e) for e in (bootstrap.get("fixtures") or {}))
    return events[0] if events else None


def gw_xp_table(bootstrap: dict, seasons: pd.DataFrame,
                projections_path: Path) -> pd.DataFrame:
    """Player table scored over the next single gameweek (gw_xp column)."""
    players = wv.build_player_table(bootstrap, seasons, projections_path)
    strengths = wv.team_strengths(seasons, bootstrap.get("teams", []))
    load1 = wv.next_fixture_load(bootstrap, strengths, n_events=1)
    team_load = {t["id"]: load1.get(t["id"], 0.0) for t in bootstrap.get("teams", [])}
    by_short = {(t.get("short_name") or t.get("name")): team_load[t["id"]]
                for t in bootstrap.get("teams", [])}
    per_gw = pd.to_numeric(players["ros_points"], errors="coerce") / wv.TOTAL_GWS
    players["gw_fixture_load"] = players["team"].map(by_short).fillna(0.0)
    players["gw_xp"] = (per_gw * players["gw_fixture_load"]
                        * players["availability"]).round(1)
    return players


def player_warnings(row: pd.Series) -> list[str]:
    """Deadline-relevant flags for one squad player."""
    warnings = []
    if pd.isna(row["ros_points"]):
        warnings.append("no projection — judge manually (player_card)")
    if row["gw_fixture_load"] == 0:
        warnings.append("blank gameweek: no fixture")
    if row["status"] != "a":
        news = f" — {row['news']}" if row["news"] else ""
        warnings.append(f"availability [{row['status']}]{news}")
    return warnings


def xi_selection_value(row: pd.Series) -> float:
    """Ordering for start/sit decisions — stricter than raw gw_xp.

    Raw gw_xp cannot rank the edge cases: an injured projected player scores
    0.0 while a fit unprojected player scores NaN, so the injured one wins
    ties (the Madjo/Maddison GW1 bug). The rule made explicit:

      projected + available  ->  their gw_xp (as before)
      unprojected + available -> 0.0  (unknown, but CAN play)
      availability 0          -> -1.0 (cannot play — never start over anyone
                                       who can, projected or not)
    """
    if row["availability"] == 0:
        return -1.0
    if pd.isna(row["gw_xp"]):
        return 0.0
    return float(row["gw_xp"])


def build_my_week(players: pd.DataFrame, element_status: dict,
                  entry_id: int) -> dict[str, Any]:
    """XI + bench + attention list for my squad, scored on gw_xp."""
    squad = wv.my_squad(players, element_status, entry_id)
    squad["xi_value"] = squad.apply(xi_selection_value, axis=1)
    xi, _ = wv.best_xi(squad, value_col="xi_value")
    # Report real expected points, not the selection ordering (which carries
    # a -1 penalty for unavailable players).
    xi_total = round(float(pd.to_numeric(xi["gw_xp"], errors="coerce")
                           .fillna(0).clip(lower=0).sum()), 1)
    bench = squad[~squad["element"].isin(xi["element"])]

    def rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
        out = []
        order = frame.sort_values(["position", "gw_xp"], ascending=[True, False])
        for _, p in order.iterrows():
            out.append({
                "web_name": p["web_name"], "position": p["position"],
                "team": p["team"],
                "gw_xp": None if pd.isna(p["gw_xp"]) else float(p["gw_xp"]),
                "ros_points": None if pd.isna(p["ros_points"]) else float(p["ros_points"]),
                "warnings": player_warnings(p),
            })
        return out

    attention = [
        {"web_name": p["web_name"], "position": p["position"], "warnings": w}
        for _, p in squad.iterrows() if (w := player_warnings(p))
    ]
    return {
        "xi": rows(xi), "bench": rows(bench), "xi_gw_xp": xi_total,
        "attention": attention,
        "unprojected_squad": wv.unprojected_squad(squad),
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(_repo_root() / ".env")
    parser = argparse.ArgumentParser(description="my_week: start/sit for the next gameweek")
    parser.add_argument("--season", default="2026-27")
    parser.add_argument("--league", type=int, default=int(os.getenv("LEAGUE_ID", "0") or "0"))
    parser.add_argument("--entry", type=int, default=int(os.getenv("ENTRY_ID", "0") or "0"))
    args = parser.parse_args(argv)
    if not args.league or not args.entry:
        parser.error("--league and --entry required (or set LEAGUE_ID / ENTRY_ID)")

    raw_root = _repo_root() / "data/raw" / args.season
    bootstrap = json.loads((raw_root / "bootstrap/bootstrap-static.json").read_text(encoding="utf-8"))
    element_status = json.loads(
        (raw_root / f"league/{args.league}/element-status.json").read_text(encoding="utf-8"))
    seasons = pd.read_parquet(_repo_root() / "data/derived/ml/player_seasons.parquet")

    players = gw_xp_table(
        bootstrap, seasons, _repo_root() / "data/derived/ml/projections_2627.json")
    week = build_my_week(players, element_status, args.entry)
    week["gw"] = next_event(bootstrap)

    print(f"\n== MY WEEK — GW{week['gw']} best XI (xP {week['xi_gw_xp']}) ==")
    for p in week["xi"]:
        flags = f"  !! {'; '.join(p['warnings'])}" if p["warnings"] else ""
        gw_xp = "?" if p["gw_xp"] is None else p["gw_xp"]
        print(f"  {p['position']:<4} {p['web_name']:<20} {p['team']:<4} xP {gw_xp}{flags}")
    print("-- bench --")
    for p in week["bench"]:
        flags = f"  !! {'; '.join(p['warnings'])}" if p["warnings"] else ""
        gw_xp = "?" if p["gw_xp"] is None else p["gw_xp"]
        print(f"  {p['position']:<4} {p['web_name']:<20} {p['team']:<4} xP {gw_xp}{flags}")
    if week["attention"]:
        print("-- needs attention --")
        for p in week["attention"]:
            print(f"  {p['position']:<4} {p['web_name']:<20} {'; '.join(p['warnings'])}")

    out = _repo_root() / "data/derived" / args.season / "ml/my_week.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(jsonutil.dumps_strict(week, indent=1))
    logger.info("wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
