"""my_week — start/sit for the next gameweek.

Answers: *who starts, who benches, and what needs my attention before the
deadline?* Scored over the single next event. ``gw_xp`` is the match model's
``xp_gw{N}.parquet`` value when ``--scorer model`` and the player is covered,
else the heuristic (season projection / 38, mild fixture multiplier, live
availability gate); each row carries ``xp_source`` (model / heuristic / none).
A missing or stale xP file falls back to the heuristic with a WARNING.

Reads local files only. CLI:
    python -m backend.ml.myweek --league <id> --entry <id> [--scorer {heuristic,model}]
(env fallback LEAGUE_ID / ENTRY_ID; writes data/derived/<season>/ml/my_week.json,
or ``--out``; ``--data-root`` overrides the data dir)
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


next_event = wv.next_event


def gw_xp_table(bootstrap: dict, seasons: pd.DataFrame,
                projections_path: Path, *,
                gw_xp: pd.DataFrame | None = None) -> pd.DataFrame:
    """Player table scored over the next single gameweek (gw_xp column).

    ``gw_xp`` is the match model's next-GW frame (see ``waiver.load_gw_xp``);
    covered players take its value (``xp_source`` "model"), the rest the
    heuristic ("heuristic") or nothing ("none", no projection either).
    ``expects_model`` records whether a model frame was supplied, so the
    heuristic scorer does not warn on every player.
    """
    players = wv.build_player_table(bootstrap, seasons, projections_path, gw_xp=gw_xp)
    strengths = wv.team_strengths(seasons, bootstrap.get("teams", []))
    load1 = wv.next_fixture_load(bootstrap, strengths, n_events=1)
    team_load = {t["id"]: load1.get(t["id"], 0.0) for t in bootstrap.get("teams", [])}
    by_short = {(t.get("short_name") or t.get("name")): team_load[t["id"]]
                for t in bootstrap.get("teams", [])}
    players["gw_fixture_load"] = players["team"].map(by_short).fillna(0.0)
    players["gw_xp"] = pd.to_numeric(players["xp_next"], errors="coerce").round(1)
    players["expects_model"] = gw_xp is not None
    return players


def player_warnings(row: pd.Series) -> list[str]:
    """Deadline-relevant flags for one squad player."""
    warnings = []
    if row["xp_source"] == "none":
        warnings.append("no value — judge manually (player_card)")
    elif row["xp_source"] == "heuristic" and row["expects_model"]:
        warnings.append("no model xP — heuristic")
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
                "xp_source": p["xp_source"],
                "p_start": None if pd.isna(p["p_start"]) else float(p["p_start"]),
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
        "unprojected_squad": wv.unprojected_squad(squad[squad["xp_source"] == "none"]),
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
    parser.add_argument("--scorer", choices=wv.SCORERS, default=wv.DEFAULT_SCORER,
                        help="gw_xp source: match model parquet or the ros/38 heuristic")
    parser.add_argument("--data-root", type=Path, default=_repo_root() / "data")
    parser.add_argument("--out", type=Path, default=None,
                        help="output JSON (default <data-root>/derived/<season>/ml/my_week.json)")
    args = parser.parse_args(argv)
    load_dotenv(args.data_root.parent / ".env")
    league = args.league or int(os.getenv("LEAGUE_ID", "0") or "0")
    entry = args.entry or int(os.getenv("ENTRY_ID", "0") or "0")
    if not league or not entry:
        parser.error("--league and --entry required (or set LEAGUE_ID / ENTRY_ID)")

    raw_root = args.data_root / "raw" / args.season
    bootstrap = json.loads((raw_root / "bootstrap/bootstrap-static.json").read_text(encoding="utf-8"))
    element_status = json.loads(
        (raw_root / f"league/{league}/element-status.json").read_text(encoding="utf-8"))
    seasons = pd.read_parquet(args.data_root / "derived/ml/player_seasons.parquet")
    ml_dir = args.data_root / "derived" / args.season / "ml"

    players = gw_xp_table(
        bootstrap, seasons, args.data_root / "derived/ml/projections_2627.json",
        gw_xp=wv.scorer_gw_xp(args.scorer, bootstrap, ml_dir))
    week = build_my_week(players, element_status, entry)
    week["gw"] = next_event(bootstrap)
    week["scorer"] = args.scorer

    print(f"\n== MY WEEK — GW{week['gw']} best XI (xP {week['xi_gw_xp']}) ==")
    for p in week["xi"]:
        flags = f"  !! {'; '.join(p['warnings'])}" if p["warnings"] else ""
        gw_xp = "?" if p["gw_xp"] is None else p["gw_xp"]
        print(f"  {p['position']:<4} {p['web_name']:<20} {p['team']:<4} xP {gw_xp} [{p['xp_source']}]{flags}")
    print("-- bench --")
    for p in week["bench"]:
        flags = f"  !! {'; '.join(p['warnings'])}" if p["warnings"] else ""
        gw_xp = "?" if p["gw_xp"] is None else p["gw_xp"]
        print(f"  {p['position']:<4} {p['web_name']:<20} {p['team']:<4} xP {gw_xp} [{p['xp_source']}]{flags}")
    if week["unprojected_squad"]:
        print(f"-- unprojected_squad: {len(week['unprojected_squad'])} --")
        for p in week["unprojected_squad"]:
            print(f"  {p['position']:<4} {p['web_name']:<20} {p['team']}")
    if week["attention"]:
        print("-- needs attention --")
        for p in week["attention"]:
            print(f"  {p['position']:<4} {p['web_name']:<20} {'; '.join(p['warnings'])}")

    out = args.out or ml_dir / "my_week.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(jsonutil.dumps_strict(week, indent=1))
    logger.info("wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
