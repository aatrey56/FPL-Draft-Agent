"""my_week — start/sit for the next gameweek.

Answers: *who starts, who benches, and what needs my attention before the
deadline?* Scored over the single next event. ``gw_xp`` is the match model's
``xp_gw{N}.parquet`` value when ``--scorer model`` and the player is covered,
else the heuristic (season projection / 38, mild fixture multiplier, live
availability gate); each row carries ``xp_source`` (model / heuristic / none).
A missing, unreadable or stale xP file falls back to the heuristic with a WARNING, and
the JSON records it: ``scorer`` is the scorer actually used, with
``scorer_requested``, ``xp_fallback`` and ``xp_fallback_reason`` beside it.

Role signals (see ``backend.ml.waiver``): ``role_overrides.json`` entries
replace a player's next-GW start probability (an override of 0 — e.g. a
``return_gw`` still ahead — keeps him out of the XI like an injury does) and
their ``fact`` is listed under ``attention``; a departed (status ``u``) squad
player always appears there with the ``departed`` code. Rows carry
``club_moved`` and ``expected_minutes`` from the season panel.

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
                gw_xp: pd.DataFrame | None = None,
                season_panel: pd.DataFrame | None = None) -> pd.DataFrame:
    """Player table scored over the next single gameweek (gw_xp column).

    ``gw_xp`` is the match model's next-GW frame (see ``waiver.load_gw_xp``);
    covered players take its value (``xp_source`` "model"), the rest the
    heuristic ("heuristic") or nothing ("none", no projection either).
    ``expects_model`` records whether a model frame was supplied, so the
    heuristic scorer does not warn on every player. ``season_panel`` feeds
    the role signals (``waiver.build_player_table``).
    """
    players = wv.build_player_table(bootstrap, seasons, projections_path, gw_xp=gw_xp,
                                    season_panel=season_panel)
    strengths = wv.team_strengths(seasons, bootstrap.get("teams", []))
    load1 = wv.next_fixture_load(bootstrap, strengths, n_events=1,
                                 fixtures_by_event=wv.upcoming_fixtures(bootstrap))
    team_load = {t["id"]: load1.get(t["id"], 0.0) for t in bootstrap.get("teams", [])}
    by_short = {(t.get("short_name") or t.get("name")): team_load[t["id"]]
                for t in bootstrap.get("teams", [])}
    players["gw_fixture_load"] = players["team"].map(by_short).fillna(0.0)
    players["gw_xp"] = pd.to_numeric(players["xp_next"], errors="coerce").round(1)
    players["expects_model"] = gw_xp is not None
    return players


def apply_role_overrides(players: pd.DataFrame, overrides: list[dict],
                         target_gw: int | None) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    """``waiver.apply_role_overrides`` on a ``gw_xp_table`` frame, keeping
    ``gw_xp`` in step with the overridden ``xp_next``. Returns the new frame
    and the ``overrides_applied`` / ``_unmatched`` / ``_expired`` report."""
    out, report = wv.apply_role_overrides(players, overrides, target_gw)
    out["gw_xp"] = pd.to_numeric(out["xp_next"], errors="coerce").round(1)
    return out, report


# Stable machine-readable codes for each warning, index-aligned with the
# human-readable ``warnings`` text (consumers such as the TUI match on these,
# never on the prose, which is free to change). The full set is pinned in
# testdata/my_week_warning_codes.json, which the Go tests read too.
# Declared, and emitted, most actionable first: a consumer that shows one
# warning per player (the TUI rail) must lead with "he cannot play", not with
# a note about where his number came from.
WARNING_DEPARTED = "departed"
WARNING_ROLE_OVERRIDE = "role_override"
WARNING_NO_VALUE = "no_value"
WARNING_BLANK_GW = "blank_gw"
WARNING_AVAILABILITY = "availability"
WARNING_HEURISTIC_XP = "heuristic_xp"


def player_warning_items(row: pd.Series) -> list[tuple[str, str]]:
    """Deadline-relevant flags for one squad player as (code, text) pairs,
    most actionable first; the scoring-source note (``heuristic_xp``) is
    always last."""
    items = []
    news = f" — {row['news']}" if row["news"] else ""
    if row["status"] == "u":
        items.append((WARNING_DEPARTED, f"departed — no longer in the league, drop him{news}"))
    override = row.get("role_override")
    if override is not None and not pd.isna(override):
        p_start = row.get("role_override_p_start")
        chance = "" if pd.isna(p_start) else f"p_start {float(p_start):g}"
        detail = " — ".join(part for part in (chance, override) if part)
        items.append((WARNING_ROLE_OVERRIDE, f"role override: {detail or 'no detail given'}"))
    if row["xp_source"] == "none":
        items.append((WARNING_NO_VALUE, "no value — judge manually (player_card)"))
    if row["gw_fixture_load"] == 0:
        items.append((WARNING_BLANK_GW, "blank gameweek: no fixture"))
    if row["status"] not in ("a", "u"):
        items.append((WARNING_AVAILABILITY, f"availability [{row['status']}]{news}"))
    if row["xp_source"] == "heuristic" and row["expects_model"]:
        items.append((WARNING_HEURISTIC_XP, "no model xP — heuristic"))
    return items


def player_warnings(row: pd.Series) -> list[str]:
    """Deadline-relevant flags for one squad player (human-readable text)."""
    return [text for _, text in player_warning_items(row)]


def xi_selection_value(row: pd.Series) -> float:
    """Ordering for start/sit decisions — stricter than raw gw_xp.

    Raw gw_xp cannot rank the edge cases: an injured projected player scores
    0.0 while a fit unprojected player scores NaN, so the injured one wins
    ties (the Madjo/Maddison GW1 bug). The rule made explicit:

      projected + available  ->  their gw_xp (as before)
      unprojected + available -> 0.0  (unknown, but CAN play)
      availability 0          -> -1.0 (cannot play — never start over anyone
                                       who can, projected or not)
      role override p_start 0 -> -1.0 (team news says he is out, whatever the
                                       bootstrap flag says)
    """
    if row["availability"] == 0 or row.get("role_override_p_start") == 0:
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
                "club_moved": None if pd.isna(p["club_moved"]) else bool(p["club_moved"]),
                "expected_minutes": (None if pd.isna(p["expected_minutes"])
                                     else float(p["expected_minutes"])),
                "role_override": p.get("role_override"),
                "warnings": player_warnings(p),
                "warning_codes": [code for code, _ in player_warning_items(p)],
            })
        return out

    attention = [
        {"web_name": p["web_name"], "position": p["position"],
         "warnings": [text for _, text in items],
         "warning_codes": [code for code, _ in items]}
        for _, p in squad.iterrows() if (items := player_warning_items(p))
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

    gw_xp, scorer_meta = wv.resolve_scorer(args.scorer, bootstrap, ml_dir)
    players = gw_xp_table(
        bootstrap, seasons, args.data_root / "derived/ml/projections_2627.json",
        gw_xp=gw_xp,
        season_panel=wv.load_season_panel(ml_dir / "player_gameweeks.parquet", args.season))
    players, override_report = apply_role_overrides(
        players, wv.load_role_overrides(ml_dir / "role_overrides.json"), next_event(bootstrap))
    week = build_my_week(players, element_status, entry)
    week["gw"] = next_event(bootstrap)
    week.update(override_report)
    # scorer = what actually ran; xp_fallback/_reason say why model was not used
    week.update(scorer_meta)

    fallback = f" — FALLBACK: {week['xp_fallback_reason']}" if week["xp_fallback"] else ""
    print(f"\n== MY WEEK — GW{week['gw']} best XI (xP {week['xi_gw_xp']}, "
          f"scorer {week['scorer']}{fallback}) ==")
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
    print("-- role overrides: " + "; ".join(
        f"{key.removeprefix('overrides_')} {', '.join(week[key]) or '-'}"
        for key in override_report) + " --")
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
