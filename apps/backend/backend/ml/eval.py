"""Evaluation harness — how well does a signal predict next-season points?

The "define accurate" deliverable (see docs/RESHAPE_PLAN.md). It backtests
player rankings against a completed season: rank players by a predictor computed
from the PRIOR season only (no leakage), then score that ranking against ACTUAL
target-season points — per position, versus the naive "last-season points"
baseline the draft board must beat.

Reads ``data/derived/ml/player_seasons.parquet`` (Phase A). No network.

Metrics per position:
* Spearman rank correlation (drafts care about order, not absolute points).
* Top-N precision (of the actual top-N scorers, how many the predictor ranked
  in its own top-N — this is what actually wins a draft).

Run: ``python -m backend.ml.eval``
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
# Minutes floor on the PRIOR season: rank players who actually featured, so the
# scores reflect draftable players rather than fringe noise.
PRIOR_MIN_MINUTES = 500
# Top-N pool size per position (DEF/MID deeper than GKP/FWD).
TOP_N = {"GKP": 10, "DEF": 20, "MID": 20, "FWD": 15}


def _spearman(a: pd.Series, b: pd.Series) -> float:
    """Spearman rank correlation of two aligned numeric series."""
    return float(a.rank().corr(b.rank()))


def build_pairs(seasons: pd.DataFrame, feature_season: str, target_season: str) -> pd.DataFrame:
    """Join prior-season features to target-season actual points, by code.

    One row per player who featured (>= PRIOR_MIN_MINUTES) in the feature season
    AND has a target-season row. Prior-season columns are prefixed ``prev_``.
    """
    feat = seasons[seasons["season"] == feature_season].copy()
    feat = feat[feat["minutes"].fillna(0) >= PRIOR_MIN_MINUTES]
    tgt = seasons[seasons["season"] == target_season][["code", "total_points"]].copy()
    tgt = tgt.rename(columns={"total_points": "actual_points"})

    prev_cols = ["total_points", "minutes", "expected_goal_involvements",
                 "ict_index", "bps", "element_type"]
    feat = feat[["code", *[c for c in prev_cols if c in feat.columns]]]
    feat = feat.rename(columns={c: f"prev_{c}" for c in prev_cols if c != "code" and c in feat.columns})

    pairs = feat.merge(tgt, on="code", how="inner")
    pairs["position"] = pairs["prev_element_type"].map(POSITIONS)
    # Minutes-aware per-90 points from the prior season (a candidate predictor).
    mins = pairs["prev_minutes"].fillna(0)
    pairs["prev_pts_per90"] = (pairs["prev_total_points"] / mins.replace(0, pd.NA)) * 90
    return pairs.dropna(subset=["position", "actual_points"])


def _top_n_precision(pairs: pd.DataFrame, predictor: str, n: int) -> float:
    """Share of the actual top-n scorers that the predictor also ranks top-n."""
    if len(pairs) < n:
        n = len(pairs)
    if n == 0:
        return float("nan")
    actual_top = set(pairs.nlargest(n, "actual_points")["code"])
    pred_top = set(pairs.nlargest(n, predictor)["code"])
    return len(actual_top & pred_top) / n


def score(pairs: pd.DataFrame, predictor: str) -> pd.DataFrame:
    """Per-position Spearman + top-N precision of a predictor vs actual points."""
    out = []
    for pos in ("GKP", "DEF", "MID", "FWD"):
        grp = pairs[pairs["position"] == pos].dropna(subset=[predictor])
        if len(grp) < 5:
            out.append({"position": pos, "n": len(grp), "spearman": float("nan"),
                        "top_n_precision": float("nan")})
            continue
        out.append({
            "position": pos,
            "n": len(grp),
            "spearman": round(_spearman(grp[predictor], grp["actual_points"]), 3),
            "top_n_precision": round(_top_n_precision(grp, predictor, TOP_N[pos]), 3),
        })
    return pd.DataFrame(out)


def run_report(seasons: pd.DataFrame, feature_season: str, target_season: str) -> dict[str, pd.DataFrame]:
    """Score the baseline and candidate predictors for one feature->target pair."""
    pairs = build_pairs(seasons, feature_season, target_season)
    predictors = {
        "baseline_prev_points": "prev_total_points",
        "candidate_prev_ict": "prev_ict_index",
        "candidate_prev_bps": "prev_bps",
        "candidate_pts_per90": "prev_pts_per90",
    }
    reports = {}
    for name, col in predictors.items():
        if col not in pairs.columns:
            logger.warning("predictor %s: column %s absent — skipping", name, col)
            continue
        reports[name] = score(pairs, col)
    return reports


def _default_parquet() -> Path:
    return Path(__file__).resolve().parents[4] / "data/derived/ml/player_seasons.parquet"


def main(argv: list[str] | None = None) -> int:
    """CLI: backtest predictors for feature->target and print a per-position report."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Next-season points prediction eval")
    parser.add_argument("--parquet", type=Path, default=_default_parquet())
    parser.add_argument("--feature-season", default="2023-24")
    parser.add_argument("--target-season", default="2024-25")
    args = parser.parse_args(argv)

    seasons = pd.read_parquet(args.parquet)
    reports = run_report(seasons, args.feature_season, args.target_season)

    print(f"\nBacktest: rank by {args.feature_season} signal -> actual {args.target_season} points")
    print(f"(players with >= {PRIOR_MIN_MINUTES} prior minutes; per position)\n")
    for name, table in reports.items():
        print(f"### {name}")
        print(table.to_string(index=False))
        print()

    base = reports.get("baseline_prev_points")
    if base is not None:
        print("Read: a candidate 'beats baseline' where its Spearman/top_n exceeds "
              "baseline_prev_points for that position.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
