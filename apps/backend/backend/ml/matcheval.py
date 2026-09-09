"""Walk-forward benchmark for per-gameweek points predictions.

Establishes the bar a match model has to clear. Every predictor is scored the
same way: for each gameweek, rank the eligible players by the prediction, then
compare against what actually happened.

Two stances worth knowing:

* **The pool matters more than the metric.** Most panel rows are players who
  were never going to feature; predicting 0 for them is free accuracy. The
  ``startable`` pool (recent minutes — see ``matchfeatures``) is the set a
  manager actually chooses between, and it is the honest place to compare
  predictors. Both pools are reported so the gap is visible.
* **FPL's own ``ep_next`` is not available historically.** It exists only in
  the live bootstrap, and no archive (including the public vaastav mirror)
  carries it per gameweek, so the spec's "beat ep_next" gate cannot be scored
  retrospectively. Until weekly snapshots accumulate, the bar is the best
  naive predictor here — which is a harder target than it sounds.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from backend.ml.matchfeatures import build_match_frame

logger = logging.getLogger(__name__)

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
# Gameweeks before this have too little trailing history to rank fairly.
MIN_EVAL_GW = 6
# Top-N precision is taken as a fraction of each gameweek's pool, so the
# metric means the same thing for 30 keepers as for 300 midfielders.
TOP_FRACTION = 0.2
MIN_TOP_N = 3

# Predictor name -> the frame column holding that prediction.
BASELINES = {
    "last_gw": "pts_l1",
    "mean_l3": "pts_l3",
    "mean_l5": "pts_l5",
    "season_mean": "pts_std",
    "minutes_only": "mins_l3",
}
# Predictors on the points scale, where absolute error is meaningful. A minutes
# predictor ranks players without pretending to be a points estimate, so its
# MAE would be a category error and is reported as NaN instead.
POINT_SCALE = {"last_gw", "mean_l3", "mean_l5", "season_mean"}


def _spearman(a: pd.Series, b: pd.Series) -> float:
    """Rank correlation, NaN when a series is constant or too short."""
    pair = pd.DataFrame({"a": a, "b": b}).dropna()
    if len(pair) < 3 or pair["a"].nunique() < 2 or pair["b"].nunique() < 2:
        return float("nan")
    # rank-then-Pearson is Spearman, and avoids a scipy dependency.
    return float(pair["a"].rank().corr(pair["b"].rank()))


def _top_n_precision(group: pd.DataFrame, predictor: str, fraction: float) -> float:
    """Share of the gameweek's true top slice that the predicted top slice finds."""
    n = max(MIN_TOP_N, int(round(len(group) * fraction)))
    n = min(n, len(group))
    if n == 0:
        return float("nan")
    predicted = set(group.nlargest(n, predictor).index)
    actual = set(group.nlargest(n, "label_points").index)
    return len(predicted & actual) / n


def score_predictor(frame: pd.DataFrame, predictor: str, name: str,
                    fraction: float = TOP_FRACTION,
                    point_scale: bool | None = None) -> dict:
    """Score one predictor per gameweek, then average across gameweeks.

    Averaging per gameweek (rather than pooling every row) keeps the metric
    aligned with the decision: each week you rank that week's options.

    ``point_scale`` overrides the ``POINT_SCALE`` membership test for callers
    whose predictor is not one of the named baselines — a model that predicts
    points should report MAE; a ranking-only predictor must not.
    """
    on_point_scale = name in POINT_SCALE if point_scale is None else point_scale
    spearmans, precisions, errors = [], [], []
    for _, group in frame.groupby("gw", sort=True):
        usable = group.dropna(subset=[predictor, "label_points"])
        if usable.empty:
            continue
        spearmans.append(_spearman(usable[predictor], usable["label_points"]))
        precisions.append(_top_n_precision(usable, predictor, fraction))
        if on_point_scale:
            errors.append((usable[predictor] - usable["label_points"]).abs().mean())
    series = pd.Series(spearmans, dtype="float64")
    return {
        "spearman": float(series.mean()),
        "top_frac": float(pd.Series(precisions, dtype="float64").mean()),
        "mae": float(pd.Series(errors, dtype="float64").mean()),
        "gws": int(series.notna().sum()),
    }


def report(frame: pd.DataFrame, pool: str, fraction: float = TOP_FRACTION) -> pd.DataFrame:
    """Per-position scores for every baseline over one evaluation pool."""
    rows = []
    for code, name in POSITIONS.items():
        subset = frame[frame["element_type"] == code]
        if subset.empty:
            continue
        for label, column in BASELINES.items():
            scores = score_predictor(subset, column, label, fraction)
            rows.append({"pool": pool, "position": name, "predictor": label, **scores})
    return pd.DataFrame(rows)


def eligible(frame: pd.DataFrame, startable_only: bool, min_gw: int = MIN_EVAL_GW) -> pd.DataFrame:
    """Rows that can be fairly predicted: enough history, optionally startable."""
    out = frame[(frame["gw"] >= min_gw) & frame["has_history"]]
    if startable_only:
        out = out[out["is_startable"]]
    return out.reset_index(drop=True)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3].parent


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Walk-forward baseline benchmark for per-GW points")
    parser.add_argument("--panel", type=Path, default=None)
    parser.add_argument("--min-gw", type=int, default=MIN_EVAL_GW)
    parser.add_argument("--top-fraction", type=float, default=TOP_FRACTION)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    panel_path = args.panel or _repo_root() / "data/derived/ml/player_gameweeks.parquet"
    frame = build_match_frame(pd.read_parquet(panel_path))

    reports = [
        report(eligible(frame, startable_only=True, min_gw=args.min_gw), "startable", args.top_fraction),
        report(eligible(frame, startable_only=False, min_gw=args.min_gw), "all", args.top_fraction),
    ]
    result = pd.concat(reports, ignore_index=True)

    for pool in ("startable", "all"):
        print(f"\n== {pool} pool (GW>={args.min_gw}) ==")
        view = result[result["pool"] == pool].pivot(
            index="predictor", columns="position", values="spearman"
        )
        print(view.round(3).to_string())

    print("\n== best point-scale baseline MAE (startable pool) ==")
    scale = result[(result["pool"] == "startable") & result["mae"].notna()]
    print(scale.loc[scale.groupby("position")["mae"].idxmin()][
        ["position", "predictor", "mae", "spearman"]].round(3).to_string(index=False))

    print("\n== best ranking baseline per position (startable pool) ==")
    startable = result[result["pool"] == "startable"]
    best = startable.loc[startable.groupby("position")["spearman"].idxmax()]
    print(best[["position", "predictor", "spearman", "top_frac", "mae"]].round(3).to_string(index=False))

    out = args.out or _repo_root() / "data/derived/ml/match_baselines.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    logger.info("wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
