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
* **Next-GW xP** (``xp_next``) comes from the match model's
  ``xp_gw{N}.parquet`` when ``--scorer model`` and the file is fresh; players
  the model does not cover (blank GW, no row) fall back to the heuristic
  ``ros/38 x fixture load x availability``. ``xp_source`` (model / heuristic /
  none) is carried on every player and recommendation. Scale caveat: model xP
  and the heuristic fallback share one ranking; the heuristic is on a lower
  scale (``ros/38`` averages below the model's per-start expectation), so a
  heuristic-valued drop makes model-valued adds look better than they are.
* **The short-vs-long balance is explicit**: every candidate shows
  ``next1_gain`` (next-GW xP vs the drop candidate), ``next3_gain`` (heuristic
  3-GW points) AND ``season_gain`` (rest-of-season vs the same drop). Ranking
  is by ``next1_gain``. Labels: ``upgrade`` (next1 and season positive — add
  and hold), ``stream`` (helps next GW only — plan to re-drop), ``hold``
  (no next-GW gain, better over the season — patience play); holds are listed
  after every positive-next1 recommendation. With no match xP supplied (the
  heuristic scorer) ``recommend`` keeps its original ranking (``rank_by=
  "legacy"``: labels from ``next3_gain``, ordered by the larger gain) so the
  heuristic output is unchanged.

CLI: python -m backend.ml.waiver --league <id> --entry <id> [--scorer {heuristic,model}]
(env fallback: LEAGUE_ID / ENTRY_ID; ``--data-root`` and ``--out`` override the
default data dir / output path)
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
                      n_events: int = NEXT_GWS, *,
                      fixtures_by_event: dict | None = None) -> dict[int, float]:
    """team id -> summed fixture multiplier over the next n events.

    ``bootstrap['fixtures']`` is a dict keyed by event number holding that
    event's fixtures; a team appearing 0/1/2 times in an event is a blank /
    normal / double gameweek and the sum reflects it automatically.
    ``fixtures_by_event`` (same shape) overrides the bootstrap schedule — the
    replay harness uses it to supply the as-of schedule.
    """
    load: dict[int, float] = {t["id"]: 0.0 for t in bootstrap.get("teams", [])}
    if fixtures_by_event is None:
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


def next_event(bootstrap: dict) -> int | None:
    """The upcoming event number = smallest key of the next-events fixture map."""
    events = sorted(int(e) for e in (bootstrap.get("fixtures") or {}))
    return events[0] if events else None


def load_gw_xp(path: Path, expected_gw: int | None) -> pd.DataFrame | None:
    """Read the match model's ``xp_gw{N}.parquet`` for the upcoming gameweek.

    Returns None (with a WARNING, so the caller falls back to the heuristic
    loudly) when the file is missing or was built for a different gameweek
    than ``expected_gw`` (stale).
    """
    path = Path(path)
    if not path.exists():
        logger.warning("match xP file %s missing — falling back to heuristic", path)
        return None
    frame = pd.read_parquet(path)
    gws = set(frame["gw"].unique()) if "gw" in frame else set()
    if expected_gw is None or gws != {expected_gw}:
        logger.warning("match xP file %s is for gw %s, expected %s — stale, "
                       "falling back to heuristic", path, sorted(gws), expected_gw)
        return None
    return frame


SCORERS = ("heuristic", "model")
DEFAULT_SCORER = "model"


def scorer_gw_xp(scorer: str, bootstrap: dict, ml_dir: Path) -> pd.DataFrame | None:
    """The next-GW xP frame the chosen scorer uses (None = heuristic only).

    ``model`` reads ``<ml_dir>/xp_gw{N}.parquet`` for the bootstrap's next
    event; a missing or stale file is logged by ``load_gw_xp`` and the plan
    falls back to the heuristic for everyone (``xp_source`` shows it).
    """
    if scorer != "model":
        return None
    event = next_event(bootstrap)
    if event is None:
        logger.warning("no upcoming fixtures in the bootstrap — heuristic scoring")
        return None
    return load_gw_xp(Path(ml_dir) / f"xp_gw{event}.parquet", event)


def build_player_table(bootstrap: dict, seasons: pd.DataFrame,
                       projections_path: Path, *,
                       fixtures_by_event: dict | None = None,
                       neutral_availability: bool = False,
                       gw_xp: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per 26/27 element: identity, availability, xP, ROS value.

    ``fixtures_by_event`` overrides the bootstrap schedule and
    ``neutral_availability`` forces availability to 1.0, except 0.0 for
    departed (status "u") players (both used by the
    replay harness, where only the current status/news snapshot exists).

    ``gw_xp`` is the match model's one-row-per-code next-GW frame (see
    ``matchmodel.build_gw_xp``). Joined on the permanent ``code``, it supplies
    ``xp_next, p_start, xp_floor, xp_ceiling, drivers, opponents`` with
    ``xp_source == "model"``. A player it does not cover gets the heuristic
    1-GW value (``ros/38 x next-event fixture load x availability``,
    source ``heuristic``), or ``none`` when there is no projection either.
    ``next3_xp`` stays heuristic.
    """
    strengths = team_strengths(seasons, bootstrap.get("teams", []))
    load = next_fixture_load(bootstrap, strengths, fixtures_by_event=fixtures_by_event)
    load1 = next_fixture_load(bootstrap, strengths, n_events=1,
                              fixtures_by_event=fixtures_by_event)
    model_by_code: dict[int, dict] = {}
    if gw_xp is not None and not gw_xp.empty:
        model_by_code = gw_xp.drop_duplicates("code").set_index("code").to_dict("index")
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
        if neutral_availability:
            # replay mode: everyone available except departed ("u") players
            avail = 0.0 if el.get("status") == "u" else 1.0
        else:
            avail = availability_factor(el)
        per_gw = (ros / TOTAL_GWS) if ros is not None else None
        next3 = (per_gw * load.get(el.get("team"), 0.0) * avail) if per_gw is not None else None
        model = model_by_code.get(el.get("code"))
        if model is not None:
            xp_next, xp_source = float(model["xp"]), "model"
            xp_extra = {"p_start": float(model["p_start"]), "xp_floor": float(model["xp_floor"]),
                        "xp_ceiling": float(model["xp_ceiling"]),
                        "drivers": model["drivers"], "opponents": model["opponents"]}
        else:
            xp_extra = {"p_start": None, "xp_floor": None, "xp_ceiling": None,
                        "drivers": None, "opponents": None}
            if per_gw is not None:
                xp_next = per_gw * load1.get(el.get("team"), 0.0) * avail
                xp_source = "heuristic"
            else:
                xp_next, xp_source = None, "none"
        rows.append({
            "element": el["id"], "code": el.get("code"),
            "web_name": el.get("web_name"),
            "position": POSITIONS.get(el.get("element_type")),
            "team": team_names.get(el.get("team")),
            "availability": avail,
            "status": el.get("status"), "news": el.get("news") or "",
            "next3_xp": round(next3, 1) if next3 is not None else None,
            "xp_next": round(xp_next, 2) if xp_next is not None else None,
            "xp_source": xp_source, **xp_extra,
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
              top_n: int = 10, *, rank_by: str = "next1") -> list[dict[str, Any]]:
    """Ranked add/drop pairs with the short-vs-long balance made explicit.

    For each free agent, pair with the weakest same-position player in my
    squad (by ROS). ``next1_gain`` = next-GW xP of the add minus the drop,
    ``season_gain`` = ROS difference. Ranking is by ``next1_gain`` descending;
    ``hold`` recs (no next-GW gain, better over the season) come after every
    positive-next1 rec, ordered by ``season_gain``. The label encodes the
    balance so a streamer is never confused with a season upgrade.

    ``rank_by="legacy"`` keeps the pre-xP behaviour for the heuristic scorer
    (labels from ``next3_gain``, ranked by the larger of ``next3_gain`` and
    ``season_gain``); ``plan`` selects it whenever no match xP is supplied.
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

        next1_gain = _v(fa["xp_next"]) - _v(drop["xp_next"])
        next3_gain = _v(fa["next3_xp"]) - _v(drop["next3_xp"])
        season_gain = _v(fa["ros_points"]) - _v(drop["ros_points"])
        short_gain = next3_gain if rank_by == "legacy" else next1_gain
        if short_gain <= 0 and season_gain <= 0:
            continue
        if short_gain > 0 and season_gain > 0:
            label = "upgrade"       # better now AND over the season: add and hold
        elif short_gain > 0:
            label = "stream"        # helps the next GW only: plan to re-drop
        else:
            label = "hold"          # tough fixtures now, better player long-term
        recs.append({
            "add": fa["web_name"], "add_element": int(fa["element"]),
            "drop_element": int(drop["element"]),
            "add_team": fa["team"], "position": fa["position"],
            "drop": drop["web_name"],
            "next1_gain": round(next1_gain, 2),
            "next3_gain": round(next3_gain, 1),
            "season_gain": round(season_gain, 1),
            "label": label,
            "add_xp_next": fa["xp_next"], "drop_xp_next": drop["xp_next"],
            "add_p_start": fa["p_start"], "add_xp_source": fa["xp_source"],
            "drivers": fa["drivers"], "opponents": fa["opponents"],
            "add_next3_xp": fa["next3_xp"], "add_ros": fa["ros_points"],
            "drop_next3_xp": drop["next3_xp"], "drop_ros": drop["ros_points"],
            "availability": fa["status"], "news": fa["news"],
            "confidence": fa["confidence"],
        })
    if rank_by == "legacy":
        recs.sort(key=lambda r: -max(r["next3_gain"], r["season_gain"]))
    else:
        recs.sort(key=lambda r: (r["label"] == "hold", -r["next1_gain"], -r["season_gain"]))
    return recs[:top_n]


def unprojected_squad(squad: pd.DataFrame) -> list[dict[str, Any]]:
    """Squad players the model cannot value (no projection) — surfaced for
    human judgment instead of being silently treated as droppable zeros."""
    rows = squad[squad["ros_points"].isna()]
    return [{
        "web_name": p["web_name"], "position": p["position"], "team": p["team"],
        "availability": p["status"], "news": p["news"],
    } for _, p in rows.iterrows()]


def plan(bootstrap: dict, element_status: dict, seasons: pd.DataFrame,
         projections_path: Path, entry_id: int, top_n: int = 10, *,
         fixtures_by_event: dict | None = None,
         neutral_availability: bool = False,
         gw_xp: pd.DataFrame | None = None) -> dict[str, Any]:
    """Pure waiver plan: players table, my squad, best-XI xP, ranked recs.

    ``gw_xp`` (optional) is the match model's next-GW frame; see
    ``build_player_table``. Without it the heuristic scorer applies and
    ``recommend`` keeps its pre-xP ranking (``rank_by="legacy"``).

    Free agents are the element-status rows with no owner. Returns
    ``players``, ``squad`` (DataFrames), ``xi_next3_xp``, ``recommendations``
    and ``unprojected_squad``. No I/O beyond reading ``projections_path``.
    """
    players = build_player_table(
        bootstrap, seasons, projections_path,
        fixtures_by_event=fixtures_by_event, neutral_availability=neutral_availability,
        gw_xp=gw_xp)
    free = {row["element"] for row in element_status.get("element_status", [])
            if row.get("owner") is None}
    players["is_free_agent"] = players["element"].isin(free)
    squad = my_squad(players, element_status, entry_id)
    _, xi_total = best_xi(squad)
    return {
        "players": players, "squad": squad, "xi_next3_xp": xi_total,
        "recommendations": recommend(players, squad, top_n,
                                     rank_by="next1" if gw_xp is not None else "legacy"),
        "unprojected_squad": unprojected_squad(squad),
    }


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
    parser.add_argument("--scorer", choices=SCORERS, default=DEFAULT_SCORER,
                        help="next-GW xP source: match model parquet or the ros/38 heuristic")
    parser.add_argument("--data-root", type=Path, default=_repo_root() / "data")
    parser.add_argument("--out", type=Path, default=None,
                        help="output JSON (default <data-root>/derived/<season>/ml/waiver_plan.json)")
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

    result = plan(bootstrap, element_status, seasons,
                  args.data_root / "derived/ml/projections_2627.json",
                  entry, args.top,
                  gw_xp=scorer_gw_xp(args.scorer, bootstrap, ml_dir))
    squad = result["squad"]
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

    unknown = result["unprojected_squad"]
    if unknown:
        print("\n== UNPROJECTED SQUAD PLAYERS (model has no value — judge manually) ==")
        for p in unknown:
            flag = f"  [{p['availability']}] {p['news']}" if p["availability"] != "a" else ""
            print(f"  {p['position']:<4} {p['web_name']:<20} {p['team']:<4}"
                  f" use player_card for their history{flag}")

    recs = result["recommendations"]
    print(f"\n== WAIVER RECOMMENDATIONS (top {args.top}, scorer {args.scorer}) ==")
    print("   label    add                    ->  drop                next1  next3   season  src")
    for r in recs:
        flag = f"  [{r['availability']}] {r['news']}" if r["availability"] != "a" else ""
        print(f"  {r['label']:<8} {r['add']:<18}({r['position']}) -> {r['drop']:<18} "
              f"{r['next1_gain']:>+6.2f} {r['next3_gain']:>+6.1f} {r['season_gain']:>+7.1f}"
              f"  {r['add_xp_source']}{flag}")

    out = args.out or ml_dir / "waiver_plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(jsonutil.dumps_strict(
        {"scorer": args.scorer, "xi_next3_xp": xi_total, "recommendations": recs,
         "unprojected_squad": unknown}, indent=1))
    logger.info("wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
