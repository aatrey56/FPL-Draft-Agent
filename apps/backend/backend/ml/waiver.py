"""waiver_plan — roster-aware add/drop recommendations.

Answers the weekly question: *given MY squad and MY league's free agents, who
do I add, who do I drop, and is the move a short-term stream or a season-long
upgrade?* Reads local files only (bootstrap, element-status snapshot,
projections, the 25/26 season table) — no live API calls.

Design (v1, pre-GW1-honest):

* **Per-GW baseline** = season projection / 38 (the measured-vs-baseline model
  from ``projection.py``). Early season there is no current form to use; as the
  match xP model (MATCH_MODEL_SPEC) lands, it replaces this baseline.
* **Fixture adjustment** — opponent strength from last season's per-team player
  points totals (promoted teams get a weak prior: 90% of the minimum), mapped
  to a multiplier in [0.85, 1.15], with a small home/away nudge. Summing per
  event handles blanks (0 fixtures) and doubles (2) naturally.
* **Availability gate** — live status/news from bootstrap: available=1.0,
  doubtful=chance/100, injured/suspended=chance if stated else 0, departed=0.
* **The short-vs-long balance is explicit**: every candidate shows
  ``next3_gain`` (points over the next 3 GWs vs the drop candidate) AND
  ``season_gain`` (rest-of-season vs the same drop). Labels:
  ``upgrade`` (both positive — add and hold), ``stream`` (helps now, worse
  long-term — plan to re-drop), ``hold`` (worse now, better over the season —
  patience play). A class player with two tough fixtures shows a small/negative
  next3_gain but a big season_gain — and stays recommended as ``hold``.

CLI: python -m backend.ml.waiver --league <id> --entry <id>   (env fallback:
LEAGUE_ID / ENTRY_ID)
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

logger = logging.getLogger(__name__)

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
NEXT_GWS = 3
TOTAL_GWS = 38
# Fixture multiplier bounds and home nudge (documented, deliberately mild —
# fixtures modulate quality, they don't replace it).
FIXTURE_MULT_MIN, FIXTURE_MULT_MAX = 0.85, 1.15
HOME_NUDGE = 1.03
# Valid draft formations: (DEF, MID, FWD) with exactly 1 GKP, 10 outfielders.
FORMATIONS = [(3, 4, 3), (3, 5, 2), (4, 3, 3), (4, 4, 2), (4, 5, 1), (5, 3, 2), (5, 4, 1)]


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

def team_strengths(seasons: pd.DataFrame, teams: list[dict]) -> dict[int, float]:
    """26/27 team id -> strength score (sum of that club's player points in
    25/26). Promoted/unseen clubs get a weak prior: 90% of the minimum."""
    last = seasons[seasons["season"] == "2025-26"]
    by_name = last.groupby("team_name")["total_points"].sum().to_dict()
    floor = 0.9 * min(by_name.values()) if by_name else 0.0
    return {t["id"]: float(by_name.get(t["name"], floor)) for t in teams}


def fixture_multiplier(opponent_strength: float, strengths: dict[int, float],
                       is_home: bool) -> float:
    """Mild multiplier: weakest opponent -> ~1.15, strongest -> ~0.85."""
    values = sorted(strengths.values())
    lo, hi = values[0], values[-1]
    span = (hi - lo) or 1.0
    # opponent strong -> harder fixture -> below 1
    centered = 0.5 - (opponent_strength - lo) / span  # +0.5 weakest .. -0.5 strongest
    mult = 1.0 + centered * (FIXTURE_MULT_MAX - FIXTURE_MULT_MIN)
    mult *= HOME_NUDGE if is_home else (2 - HOME_NUDGE)
    return max(FIXTURE_MULT_MIN, min(FIXTURE_MULT_MAX, mult))


def next_fixture_load(bootstrap: dict, strengths: dict[int, float],
                      n_events: int = NEXT_GWS) -> dict[int, float]:
    """team id -> summed fixture multiplier over the next n events.

    ``bootstrap['fixtures']`` is a dict keyed by event number holding that
    event's fixtures; a team appearing 0/1/2 times in an event is a blank /
    normal / double gameweek and the sum reflects it automatically.
    """
    load: dict[int, float] = {t["id"]: 0.0 for t in bootstrap.get("teams", [])}
    fixtures_by_event = bootstrap.get("fixtures", {}) or {}
    events = sorted(int(e) for e in fixtures_by_event)[:n_events]
    for event in events:
        for fx in fixtures_by_event[str(event)]:
            home, away = fx.get("team_h"), fx.get("team_a")
            if home in load:
                load[home] += fixture_multiplier(strengths.get(away, 0.0), strengths, True)
            if away in load:
                load[away] += fixture_multiplier(strengths.get(home, 0.0), strengths, False)
    return load


def availability_factor(element: dict) -> float:
    """Live availability gate from bootstrap status/news flags."""
    status = element.get("status", "a")
    chance = element.get("chance_of_playing_next_round")
    if status == "a":
        return 1.0
    if status == "u":
        return 0.0
    if chance is not None:
        return max(0.0, min(1.0, float(chance) / 100.0))
    return 0.75 if status == "d" else 0.0


def build_player_table(bootstrap: dict, seasons: pd.DataFrame,
                       projections_path: Path) -> pd.DataFrame:
    """One row per 26/27 element: identity, availability, next-3 xP, ROS value."""
    strengths = team_strengths(seasons, bootstrap.get("teams", []))
    load = next_fixture_load(bootstrap, strengths)
    projections = {}
    if Path(projections_path).exists():
        projections = {int(r["code"]): r for r in
                       json.loads(Path(projections_path).read_text(encoding="utf-8"))}
    else:
        logger.warning("projections missing at %s — values will be null", projections_path)

    team_names = {t["id"]: t.get("short_name") or t.get("name")
                  for t in bootstrap.get("teams", [])}
    rows = []
    for el in bootstrap.get("elements", []):
        projection = projections.get(el.get("code"), {})
        ros = projection.get("projected_points")
        avail = availability_factor(el)
        per_gw = (ros / TOTAL_GWS) if ros is not None else None
        next3 = (per_gw * load.get(el.get("team"), 0.0) * avail) if per_gw is not None else None
        rows.append({
            "element": el["id"], "code": el.get("code"),
            "web_name": el.get("web_name"),
            "position": POSITIONS.get(el.get("element_type")),
            "team": team_names.get(el.get("team")),
            "availability": avail,
            "status": el.get("status"), "news": el.get("news") or "",
            "next3_xp": round(next3, 1) if next3 is not None else None,
            # ROS is deliberately NOT availability-gated: an injury gates the
            # next-3 horizon, not the season (status "u" = departed is the
            # exception and is filtered out of recommendations entirely).
            "ros_points": round(ros, 1) if ros is not None else None,
            "tier": projection.get("tier"), "confidence": projection.get("confidence"),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Squad evaluation
# ---------------------------------------------------------------------------

def my_squad(players: pd.DataFrame, element_status: dict, entry_id: int) -> pd.DataFrame:
    """My current 15, from the latest element-status snapshot."""
    mine = {row["element"] for row in element_status.get("element_status", [])
            if row.get("owner") == entry_id}
    return players[players["element"].isin(mine)].copy()


def best_xi(squad: pd.DataFrame, value_col: str = "next3_xp") -> tuple[pd.DataFrame, float]:
    """Best legal XI (1 GKP + a valid DEF/MID/FWD formation) by value_col."""
    ranked = {pos: squad[squad["position"] == pos]
              .sort_values(value_col, ascending=False, na_position="last")
              for pos in ("GKP", "DEF", "MID", "FWD")}
    best_total, best_frame = -1.0, None
    for d, m, f in FORMATIONS:
        if len(ranked["GKP"]) < 1 or len(ranked["DEF"]) < d \
                or len(ranked["MID"]) < m or len(ranked["FWD"]) < f:
            continue
        xi = pd.concat([ranked["GKP"].head(1), ranked["DEF"].head(d),
                        ranked["MID"].head(m), ranked["FWD"].head(f)])
        total = float(xi[value_col].fillna(0).sum())
        if total > best_total:
            best_total, best_frame = total, xi
    if best_frame is None:  # squad too broken for any formation — take best 11
        best_frame = squad.sort_values(value_col, ascending=False).head(11)
        best_total = float(best_frame[value_col].fillna(0).sum())
    return best_frame, round(best_total, 1)


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

def recommend(players: pd.DataFrame, squad: pd.DataFrame,
              top_n: int = 10) -> list[dict[str, Any]]:
    """Ranked add/drop pairs with the short-vs-long balance made explicit.

    For each free agent, pair with the weakest same-position player in my
    squad (by ROS). Gains are computed on both horizons; the label encodes
    the balance so a fixture-run streamer is never confused with a season
    upgrade.
    """
    # Free agents by definition have no owner, so my squad is already excluded.
    pool = players[players["is_free_agent"] & (players["status"] != "u")]
    recs: list[dict[str, Any]] = []
    for _, fa in pool.iterrows():
        # Drop candidates come only from players the model can value. An
        # unprojected teammate (under the minutes floor last season, promoted,
        # or newly signed) is NOT worth zero — it is unknown, and auto-dropping
        # a returning star on missing data is the one unrecoverable mistake.
        # Those players go in the plan's unprojected_squad section instead.
        mine = squad[(squad["position"] == fa["position"])
                     & squad["ros_points"].notna()]
        if mine.empty or pd.isna(fa["ros_points"]):
            continue  # cannot recommend a player we cannot value
        drop = mine.sort_values(["ros_points", "next3_xp"]).iloc[0]

        def _v(x):
            return 0.0 if pd.isna(x) else float(x)

        next3_gain = _v(fa["next3_xp"]) - _v(drop["next3_xp"])
        season_gain = _v(fa["ros_points"]) - _v(drop["ros_points"])
        if next3_gain <= 0 and season_gain <= 0:
            continue
        if next3_gain > 0 and season_gain > 0:
            label = "upgrade"       # better now AND over the season: add and hold
        elif next3_gain > 0:
            label = "stream"        # helps the next 3 GWs only: plan to re-drop
        else:
            label = "hold"          # tough fixtures now, better player long-term
        recs.append({
            "add": fa["web_name"], "add_team": fa["team"], "position": fa["position"],
            "drop": drop["web_name"],
            "next3_gain": round(next3_gain, 1),
            "season_gain": round(season_gain, 1),
            "label": label,
            "add_next3_xp": fa["next3_xp"], "add_ros": fa["ros_points"],
            "drop_next3_xp": drop["next3_xp"], "drop_ros": drop["ros_points"],
            "availability": fa["status"], "news": fa["news"],
            "confidence": fa["confidence"],
        })
    # Rank by the better of the two gains so both upgrade and stream value
    # surface; the label keeps them distinguishable.
    recs.sort(key=lambda r: -max(r["next3_gain"], r["season_gain"]))
    return recs[:top_n]


def unprojected_squad(squad: pd.DataFrame) -> list[dict[str, Any]]:
    """Squad players the model cannot value (no projection) — surfaced for
    human judgment instead of being silently treated as droppable zeros."""
    rows = squad[squad["ros_points"].isna()]
    return [{
        "web_name": p["web_name"], "position": p["position"], "team": p["team"],
        "availability": p["status"], "news": p["news"],
    } for _, p in rows.iterrows()]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(_repo_root() / ".env")
    parser = argparse.ArgumentParser(description="waiver_plan: roster-aware add/drop recommendations")
    parser.add_argument("--season", default="2026-27")
    parser.add_argument("--league", type=int, default=int(os.getenv("LEAGUE_ID", "0") or "0"))
    parser.add_argument("--entry", type=int, default=int(os.getenv("ENTRY_ID", "0") or "0"))
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args(argv)
    if not args.league or not args.entry:
        parser.error("--league and --entry required (or set LEAGUE_ID / ENTRY_ID)")

    raw_root = _repo_root() / "data/raw" / args.season
    bootstrap = json.loads((raw_root / "bootstrap/bootstrap-static.json").read_text(encoding="utf-8"))
    element_status = json.loads(
        (raw_root / f"league/{args.league}/element-status.json").read_text(encoding="utf-8"))
    seasons = pd.read_parquet(_repo_root() / "data/derived/ml/player_seasons.parquet")

    players = build_player_table(
        bootstrap, seasons, _repo_root() / "data/derived/ml/projections_2627.json")
    free = {row["element"] for row in element_status.get("element_status", [])
            if row.get("owner") is None}
    players["is_free_agent"] = players["element"].isin(free)

    squad = my_squad(players, element_status, args.entry)
    xi, xi_total = best_xi(squad)
    bench = squad[~squad["element"].isin(xi["element"])]

    def _fmt(x):
        return "    ?" if pd.isna(x) else f"{x:>5}"

    print(f"\n== MY SQUAD — best XI, next {NEXT_GWS} GWs (xP {xi_total}) ==")
    for _, p in xi.sort_values(["position", "next3_xp"], ascending=[True, False]).iterrows():
        flag = f"  [{p['status']}] {p['news']}" if p["status"] != "a" else ""
        print(f"  {p['position']:<4} {p['web_name']:<20} {p['team']:<4} "
              f"next3 {_fmt(p['next3_xp'])} | ROS {_fmt(p['ros_points'])}{flag}")
    print("-- bench --")
    for _, p in bench.sort_values("next3_xp", ascending=False).iterrows():
        flag = f"  [{p['status']}] {p['news']}" if p["status"] != "a" else ""
        print(f"  {p['position']:<4} {p['web_name']:<20} {p['team']:<4} "
              f"next3 {_fmt(p['next3_xp'])} | ROS {_fmt(p['ros_points'])}{flag}")

    unknown = unprojected_squad(squad)
    if unknown:
        print("\n== UNPROJECTED SQUAD PLAYERS (model has no value — judge manually) ==")
        for p in unknown:
            flag = f"  [{p['availability']}] {p['news']}" if p["availability"] != "a" else ""
            print(f"  {p['position']:<4} {p['web_name']:<20} {p['team']:<4}"
                  f" use player_card for their history{flag}")

    recs = recommend(players, squad, args.top)
    print(f"\n== WAIVER RECOMMENDATIONS (top {args.top}) ==")
    print("   label    add                    ->  drop                next3   season")
    for r in recs:
        flag = f"  [{r['availability']}] {r['news']}" if r["availability"] != "a" else ""
        print(f"  {r['label']:<8} {r['add']:<18}({r['position']}) -> {r['drop']:<18} "
              f"{r['next3_gain']:>+6.1f} {r['season_gain']:>+7.1f}{flag}")

    out = _repo_root() / "data/derived" / args.season / "ml/waiver_plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(jsonutil.dumps_strict(
        {"xi_next3_xp": xi_total, "recommendations": recs,
         "unprojected_squad": unknown}, indent=1))
    logger.info("wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
