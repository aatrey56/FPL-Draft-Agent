"""team_env — per-team match-environment ratings from the GW panel.

The game-environment insight: a 0-0 between two locked defenses starves every
player of points; a 5-4 shootout showers them. This module measures, per team
and per season, how many FPL points a team *generates* and how many it
*concedes* — overall, by opponent position, and home/away — from the per-GW
panel (``player_gameweeks.parquet``).

v1 consumers: an MCP tool (``team_env``) so Claude can reason about fixture
environment explicitly. The match xP model (MATCH_MODEL_SPEC) consumes these
as features and, once validated against FPL's ``ep_next``, replaces the
cruder team-strength multiplier inside waiver_plan / my_week.

Method notes:
* Rates are per-fixture (a DGW row counts as two fixtures in attack rates).
* Points-conceded attribution needs a single opponent, so only clean
  single-fixture rows (``num_fixtures == 1``) feed the defense splits —
  documented, and fine: 25/26 has 409 DGW rows out of 29,747.
* Keys are team *names* (stable across seasons), not per-season ids.

CLI: python -m backend.ml.teamenv   (writes data/derived/ml/team_env.json)
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from backend.ml import jsonutil

logger = logging.getLogger(__name__)

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def team_names(panel: pd.DataFrame) -> dict[int, str]:
    """Per-season team id -> name, recovered from the opponent columns."""
    pairs = panel[["opponent_team", "opponent_name"]].dropna().drop_duplicates()
    return {int(t): str(n) for t, n in pairs.itertuples(index=False)}


def build_team_env(panel: pd.DataFrame, season: str) -> dict[str, dict[str, Any]]:
    """Team name -> attack/defense environment rates for one season."""
    df = panel[(panel["season"] == season) & (panel["num_fixtures"] > 0)].copy()
    if df.empty:
        return {}
    names = team_names(df)

    env: dict[str, dict[str, Any]] = {}
    for team_id, name in names.items():
        mine = df[df["team_id"] == team_id]
        fixtures = float(mine.groupby("gw")["num_fixtures"].first().sum()) or 1.0
        attack = {
            "fixtures": int(fixtures),
            "pts_pg": round(float(mine["total_points"].sum()) / fixtures, 2),
            "xg_pg": round(float(mine["expected_goals"].sum()) / fixtures, 2),
        }

        # Defense: what opponents' players did against this team (single-fixture
        # rows only, so the opponent attribution is unambiguous).
        faced = df[(df["opponent_team"] == team_id) & (df["num_fixtures"] == 1)]
        d_fixtures = float(faced["gw"].nunique()) or 1.0
        by_pos = {
            label: round(float(faced[faced["element_type"] == et]["total_points"].sum()) / d_fixtures, 2)
            for et, label in POSITIONS.items()
        }
        home_faced = faced[~faced["was_home"].astype(bool)]   # opponents away = this team at home
        away_faced = faced[faced["was_home"].astype(bool)]
        defense = {
            "pts_conceded_pg": round(float(faced["total_points"].sum()) / d_fixtures, 2),
            "pts_conceded_pg_by_pos": by_pos,
            "xg_conceded_pg": round(float(faced["expected_goals"].sum()) / d_fixtures, 2),
            "pts_conceded_pg_at_home": round(
                float(home_faced["total_points"].sum()) / (float(home_faced["gw"].nunique()) or 1.0), 2),
            "pts_conceded_pg_away": round(
                float(away_faced["total_points"].sum()) / (float(away_faced["gw"].nunique()) or 1.0), 2),
        }
        env[name] = {"attack": attack, "defense": defense}
    return env


def expected_environment(env: dict[str, dict[str, Any]], home: str, away: str) -> dict[str, Any]:
    """Crude fixture-environment estimate for one matchup: combined xG and the
    points each side's positions can expect to harvest. Not a prediction —
    a context signal (shootout vs stalemate)."""
    h, a = env.get(home), env.get(away)
    if not h or not a:
        return {}
    combined_xg = round(h["attack"]["xg_pg"] + a["attack"]["xg_pg"], 2)
    return {
        "home": home, "away": away,
        "combined_xg_pg": combined_xg,
        "environment": "shootout" if combined_xg >= 3.4 else
                       ("stalemate" if combined_xg <= 2.2 else "average"),
        "home_attack_vs_away_defense": {
            "attack_pts_pg": h["attack"]["pts_pg"],
            "opponent_concedes_pg": a["defense"]["pts_conceded_pg_away"],
        },
        "away_attack_vs_home_defense": {
            "attack_pts_pg": a["attack"]["pts_pg"],
            "opponent_concedes_pg": h["defense"]["pts_conceded_pg_at_home"],
        },
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="team_env: per-team match-environment ratings")
    parser.add_argument("--season", default="2025-26",
                        help="panel season to rate (25/26 until the 26/27 panel accumulates)")
    parser.add_argument("--panel", type=Path,
                        default=_repo_root() / "data/derived/ml/player_gameweeks.parquet")
    parser.add_argument("--out", type=Path,
                        default=_repo_root() / "data/derived/ml/team_env.json")
    args = parser.parse_args(argv)

    panel = pd.read_parquet(args.panel)
    env = build_team_env(panel, args.season)
    payload = {"season": args.season, "teams": env}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(jsonutil.dumps_strict(payload, indent=1))
    logger.info("wrote environment for %d teams -> %s", len(env), args.out)

    ranked = sorted(env.items(), key=lambda kv: -kv[1]["attack"]["xg_pg"])
    print(f"\n== TEAM ENVIRONMENT ({args.season}) — attack xG/g | pts conceded/g ==")
    for name, e in ranked:
        print(f"  {name:<18} xG {e['attack']['xg_pg']:>5} | concedes {e['defense']['pts_conceded_pg']:>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
