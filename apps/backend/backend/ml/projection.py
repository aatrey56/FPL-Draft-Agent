"""Season projection model — the draft board's brain.

Predicts next-season FPL total points per player, per the contract in
``PROJECTION_MODEL_SPEC.md``:

* **Two-stage, minutes first**: projection = E[minutes]/90 x E[pts/90].
  Availability is modelled separately from per-game quality so a nailed
  3000-minute player is never out-ranked by a brilliant 1500-minute one
  purely on rate stats. (Diagnosis on 2024-25 shows minutes is the dominant
  error source: with actual minutes, predicted pts/90 ranks players at
  Spearman 0.92-0.98.)
* **Position-specific, honestly selected**: for each position and stage, a
  small candidate zoo (ridge variants at several regularization strengths,
  plus naive persistence — "next = this season's value") is scored on an
  internal validation pair (the last labelled season INSIDE the training
  window) and the winner is refit on all training pairs. Naive persistence
  is a legitimate candidate: where a position's signal is pure persistence
  (small pools, sticky roles), the selector picks it instead of a noisy fit.
  Selection never sees the held-out test season.
* **Explainable and deterministic**: closed-form ridge (numpy) — no seed, no
  randomness; ties break toward the simpler candidate. Coefficients give the
  per-player top drivers used by draft_board's one-line rationale.
* **xG as stabilizer**: attacking quality enters via per-90 xG/xA; persistent
  over/under-finishing via the shrunk multi-year finishing-skill gap.

CLI: ``python -m backend.ml.projection``           -> walk-forward backtest report
     ``python -m backend.ml.projection --project``  -> also writes projections for
     the upcoming season (parquet + JSON for the Go tools).
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from backend.ml import features as F

logger = logging.getLogger(__name__)

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
MAX_SEASON_MINUTES = 38 * 90
# Stage-2 label floor: next-season pts/90 is only a meaningful target when the
# player actually featured next season.
NEXT_MINUTES_FLOOR = 450
# Pool floor for evaluation/projection: rank players who featured this season.
PRIOR_MINUTES_FLOOR = 500

# Candidate feature sets. Stage 1 models durability; stage 2 models per-90
# quality (position-specific per the verified 25/26 correlation findings).
STAGE1_FEATURE_SETS: dict[str, list[str]] = {
    "durability": ["minutes", "minutes_lag1", "starts"],
    "durability+form": ["minutes", "minutes_lag1", "starts", "pts90", "bps90"],
}
STAGE2_FEATURE_SETS: dict[str, dict[str, list[str]]] = {
    "GKP": {
        "slim": ["pts90", "saves90", "cs90"],
        "full": ["pts90", "saves90", "cs90", "gc90", "bps90"],
    },
    "DEF": {
        "slim": ["pts90", "cs90", "xg90", "xa90"],
        "full": ["pts90", "cs90", "gc90", "xg90", "xa90", "threat90", "ict90",
                 "bps90", "finishing_skill"],
    },
    "MID": {
        "slim": ["pts90", "xg90", "xa90"],
        "full": ["pts90", "xg90", "xa90", "ict90", "creativity90", "threat90",
                 "bps90", "finishing_skill", "assist_skill"],
    },
    "FWD": {
        "slim": ["pts90", "xg90", "xa90"],
        "full": ["pts90", "xg90", "xa90", "threat90", "ict90", "bps90",
                 "finishing_skill"],
    },
}
# Descending: stronger regularization = simpler; listed first so the
# one-standard-error rule prefers it on ties.
RIDGE_ALPHAS = [100.0, 30.0, 10.0, 3.0, 1.0, 0.3]

# Draft config: league size and typical starters per position (configurable).
DEFAULT_LEAGUE_SIZE = 10
DEFAULT_STARTERS = {"GKP": 1, "DEF": 4, "MID": 4, "FWD": 2}
TIER_COUNT = 5


def _numeric_matrix(table: pd.DataFrame, columns: list[str]) -> np.ndarray:
    """Writable float matrix with nulls as NaN (handles nullable dtypes)."""
    frame = table[columns].apply(pd.to_numeric, errors="coerce").astype(float)
    return frame.to_numpy(dtype=float, copy=True)


@dataclass
class Ridge:
    """Closed-form ridge regression on standardized features.

    Deterministic and dependency-free. Nulls are imputed with training-column
    means (never 0 — a null per-90 means "unknown", not "zero output"); a
    column that is entirely null in training imputes to 0 and, having zero
    variance, gets no weight.
    """

    alpha: float = 1.0
    columns: list[str] = field(default_factory=list)
    mean_: np.ndarray | None = None
    std_: np.ndarray | None = None
    impute_: np.ndarray | None = None
    coef_: np.ndarray | None = None
    intercept_: float = 0.0

    def _standardize(self, raw: np.ndarray) -> np.ndarray:
        idx = np.where(np.isnan(raw))
        if idx[0].size:
            raw[idx] = np.take(self.impute_, idx[1])
        return (raw - self.mean_) / self.std_

    def fit(self, table: pd.DataFrame, target: pd.Series) -> "Ridge":
        raw = _numeric_matrix(table, self.columns)
        y = np.asarray(pd.to_numeric(target, errors="coerce"), dtype=float)
        self.impute_ = np.array([
            float(np.nanmean(col)) if not np.all(np.isnan(col)) else 0.0
            for col in raw.T
        ])
        idx = np.where(np.isnan(raw))
        if idx[0].size:
            raw[idx] = np.take(self.impute_, idx[1])
        self.mean_ = raw.mean(axis=0)
        std = raw.std(axis=0)
        self.std_ = np.where(std < 1e-9, 1.0, std)
        x = (raw - self.mean_) / self.std_
        self.intercept_ = float(y.mean())
        gram = x.T @ x + self.alpha * np.eye(x.shape[1])
        self.coef_ = np.linalg.solve(gram, x.T @ (y - self.intercept_))
        return self

    def predict(self, table: pd.DataFrame) -> np.ndarray:
        return self._standardize(_numeric_matrix(table, self.columns)) @ self.coef_ \
            + self.intercept_

    def contributions(self, table: pd.DataFrame) -> pd.DataFrame:
        """Per-row, per-feature contribution (coef x standardized value)."""
        x = self._standardize(_numeric_matrix(table, self.columns))
        return pd.DataFrame(x * self.coef_, columns=self.columns, index=table.index)


@dataclass
class Persistence:
    """Naive candidate: next-season value = this season's value of one column.

    Nulls are imputed with the training mean of that column, keeping ranking
    behaviour defined for players missing the stat.
    """

    column: str
    impute_: float = 0.0

    def fit(self, table: pd.DataFrame, target: pd.Series) -> "Persistence":
        values = pd.to_numeric(table[self.column], errors="coerce")
        self.impute_ = float(values.mean()) if values.notna().any() else 0.0
        return self

    def predict(self, table: pd.DataFrame) -> np.ndarray:
        values = pd.to_numeric(table[self.column], errors="coerce")
        return values.fillna(self.impute_).to_numpy(dtype=float)


def _spearman_arrays(a: np.ndarray, b: np.ndarray) -> float:
    sa = pd.Series(np.asarray(a, dtype=float)).rank()
    sb = pd.Series(np.asarray(b, dtype=float)).rank()
    value = sa.corr(sb)
    return float(value) if pd.notna(value) else 0.0


def select_model(rows: pd.DataFrame, target_col: str,
                 candidates: list[tuple[str, object]]) -> tuple[str, object, object]:
    """Choose a candidate by multi-fold walk-forward validation.

    Folds: for every labelled season s inside ``rows`` (except the first),
    train on pairs labelled before s and score Spearman on pairs labelled s;
    a candidate's score is the average across folds. This keeps selection
    leakage-free (only data inside the training window) while averaging away
    single-season noise. The winner is refit on all rows.

    ``candidates`` holds (name, factory) where factory() returns a fresh
    unfitted model. Deterministic: ties keep the earlier candidate, so list
    simpler candidates first. Returns (name, fitted_model, factory).
    """
    fold_seasons = sorted(rows["next_season"].unique())
    folds = []
    for season in fold_seasons[1:]:
        train = rows[rows["next_season"] < season]
        validate = rows[rows["next_season"] == season]
        if len(train) >= 5 and len(validate) >= 5:
            folds.append((train, validate))

    per_fold: list[tuple[str, object, list[float]]] = []
    for name, factory in candidates:
        scores = []
        for train, validate in folds:
            model = factory()
            model.fit(train, train[target_col])
            scores.append(_spearman_arrays(
                model.predict(validate), validate[target_col].to_numpy(dtype=float)))
        per_fold.append((name, factory, scores))

    # Paired displacement rule: candidates are ordered simplest-first and the
    # simplest is the incumbent. A more complex challenger displaces the
    # incumbent only when its mean PER-FOLD advantage exceeds the standard
    # error of that advantage (a paired comparison — shared season-difficulty
    # noise cancels, unlike comparing raw averages). This lets genuine signal
    # win on large pools while keeping small, noisy pools (e.g. FWD, ~30
    # players/season) anchored to the honest simple model.
    best_name, best_factory, best_scores = per_fold[0]
    for name, factory, scores in per_fold[1:]:
        if not best_scores:
            break
        diffs = np.array(scores) - np.array(best_scores)
        mean_gain = float(diffs.mean())
        se_gain = float(diffs.std(ddof=1) / np.sqrt(len(diffs))) if len(diffs) > 1 else 0.0
        wins_majority = (diffs > 0).sum() > len(diffs) / 2
        if mean_gain > se_gain and wins_majority:
            best_name, best_factory, best_scores = name, factory, scores

    winner = best_factory()
    winner.fit(rows, rows[target_col])
    logger.debug("selected %s (avg %.3f, %d folds)", best_name,
                 float(np.mean(best_scores)) if best_scores else 0.0, len(folds))
    return best_name, winner, best_factory


class TwoStage:
    """Combined-level candidate: E[minutes]/90 x E[pts/90] from fixed stage configs."""

    def __init__(self, minutes_factory, quality_factory):
        self._minutes_factory = minutes_factory
        self._quality_factory = quality_factory
        self.minutes_model = None
        self.quality_model = None

    def fit(self, rows: pd.DataFrame, target: pd.Series) -> "TwoStage":
        # target is next_total_points; the stages train on their own labels.
        self.minutes_model = self._minutes_factory()
        self.minutes_model.fit(rows, rows["next_total_minutes_label"])
        quality_rows = rows[rows["next_minutes"].astype(float) >= NEXT_MINUTES_FLOOR]
        self.quality_model = self._quality_factory()
        self.quality_model.fit(quality_rows, quality_rows["next_pts90_label"])
        return self

    def predict(self, table: pd.DataFrame) -> np.ndarray:
        minutes = np.clip(self.minutes_model.predict(table), 0, MAX_SEASON_MINUTES)
        return minutes / 90 * self.quality_model.predict(table)


# Direct (one-stage) candidates predict next_total_points straight from a mix of
# volume and rate features — the structure the raw baseline belongs to.
DIRECT_FEATURE_SETS: dict[str, list[str]] = {
    "direct-slim": ["total_points", "minutes", "pts90"],
    "direct-full": ["total_points", "minutes", "minutes_lag1", "pts90",
                    "xg90", "xa90", "ict90", "bps90", "finishing_skill"],
}


@dataclass
class PositionModel:
    """Per-position model with a two-level honest selection.

    Level 1 (stages): choose the best minutes model and the best pts/90 model
    from ridge variants + persistence, by multi-fold internal validation.
    Level 2 (structure): let three structures compete on predicting
    next_total_points itself — the two-stage composition, direct ridge on
    volume+rate features, and the raw baseline (persistence of total points).
    Whatever validates best per position is the model. Including the baseline
    as a candidate makes "beats the baseline" the selection's floor rather
    than a hope.
    """

    position: str
    minutes_choice: str = ""
    quality_choice: str = ""
    structure_choice: str = ""
    model: object = None
    _minutes_factory: object = None
    _quality_factory: object = None

    def _candidates_stage1(self) -> list[tuple[str, object]]:
        cands = [("persistence:minutes", lambda: Persistence("minutes"))]
        for set_name, cols in STAGE1_FEATURE_SETS.items():
            for alpha in RIDGE_ALPHAS:
                cands.append((f"ridge:{set_name}:a{alpha}",
                              lambda a=alpha, c=cols: Ridge(alpha=a, columns=c)))
        return cands

    def _candidates_stage2(self) -> list[tuple[str, object]]:
        cands = [("persistence:pts90", lambda: Persistence("pts90"))]
        for set_name, cols in STAGE2_FEATURE_SETS[self.position].items():
            for alpha in RIDGE_ALPHAS:
                cands.append((f"ridge:{set_name}:a{alpha}",
                              lambda a=alpha, c=cols: Ridge(alpha=a, columns=c)))
        return cands

    def _candidates_structure(self) -> list[tuple[str, object]]:
        cands = [
            ("baseline:total_points", lambda: Persistence("total_points")),
            # Single-feature ridge = same ranking as ICT persistence but
            # calibrated in POINTS units (projected_points feeds VOR/tiers).
            ("rank:ict_index", lambda: Ridge(alpha=1.0, columns=["ict_index"])),
        ]
        for set_name, cols in DIRECT_FEATURE_SETS.items():
            for alpha in RIDGE_ALPHAS:
                cands.append((f"ridge:{set_name}:a{alpha}",
                              lambda a=alpha, c=cols: Ridge(alpha=a, columns=c)))
        cands.append(("two-stage",
                      lambda: TwoStage(self._minutes_factory, self._quality_factory)))
        return cands

    def fit(self, pairs: pd.DataFrame) -> "PositionModel":
        rows = pairs[pairs["position"] == self.position]

        self.minutes_choice, _, self._minutes_factory = select_model(
            rows, "next_total_minutes_label", self._candidates_stage1()
        )
        quality_rows = rows[rows["next_minutes"].astype(float) >= NEXT_MINUTES_FLOOR]
        self.quality_choice, _, self._quality_factory = select_model(
            quality_rows, "next_pts90_label", self._candidates_stage2()
        )
        self.structure_choice, self.model, _ = select_model(
            rows, "next_total_points", self._candidates_structure()
        )
        return self

    def predict(self, table: pd.DataFrame) -> pd.DataFrame:
        out = table[["code", "web_name", "season"]].copy()
        out["position"] = self.position
        projected = np.asarray(self.model.predict(table), dtype=float)
        out["projected_points"] = np.round(projected, 1)
        if isinstance(self.model, TwoStage):
            minutes = np.clip(self.model.minutes_model.predict(table), 0, MAX_SEASON_MINUTES)
            out["pred_minutes"] = np.round(minutes, 0)
            out["pred_pts90"] = np.round(self.model.quality_model.predict(table), 3)
        else:
            out["pred_minutes"] = np.nan
            out["pred_pts90"] = np.nan
        out["drivers"] = self._drivers(table)
        return out

    def _drivers(self, table: pd.DataFrame) -> list[str]:
        """Top-3 contribution drivers (ridge) or the persistence/stage source."""
        model = self.model.quality_model if isinstance(self.model, TwoStage) else self.model
        if isinstance(model, Persistence):
            return [f"persistence:{model.column}"] * len(table)
        contrib = model.contributions(table)
        ranked = contrib.abs().rank(axis=1, ascending=False, method="first")
        return [
            ", ".join(
                f"{col}{'+' if contrib.at[i, col] >= 0 else '-'}"
                for col in contrib.columns
                if ranked.at[i, col] <= 3
            )
            for i in contrib.index
        ]


def prepare_pairs(seasons: pd.DataFrame) -> pd.DataFrame:
    """Feature/label table with position names and stage labels attached."""
    pairs = F.build_pairs(seasons)
    pairs["position"] = pairs["element_type"].map(POSITIONS)
    pairs = pairs.dropna(subset=["position"])
    pairs["next_total_minutes_label"] = pairs["next_minutes"].astype(float)
    next_minutes = pairs["next_minutes"].astype(float)
    pairs["next_pts90_label"] = np.where(
        next_minutes > 0,
        pairs["next_total_points"].astype(float) / next_minutes.clip(lower=1) * 90,
        0.0,
    )
    return pairs


def fit_models(train_pairs: pd.DataFrame) -> dict[str, PositionModel]:
    """Fit the per-position two-stage models (with internal model selection)."""
    return {pos: PositionModel(pos).fit(train_pairs) for pos in POSITIONS.values()}


# ---------------------------------------------------------------------------
# Draft outputs: tiers, VOR, confidence, risk flags
# ---------------------------------------------------------------------------

def assign_tiers(points: pd.Series, tier_count: int = TIER_COUNT) -> pd.Series:
    """Tier 1..N within a position by the largest natural gaps (deterministic)."""
    order = points.sort_values(ascending=False)
    if len(order) <= tier_count:
        return pd.Series(range(1, len(order) + 1), index=order.index).reindex(points.index)
    gaps = order.diff(-1).iloc[:-1]  # drop from each player to the next
    cut_positions = sorted(gaps.nlargest(tier_count - 1).index.map(order.index.get_loc))
    tiers = pd.Series(1, index=order.index)
    for tier_number, cut in enumerate(cut_positions, start=2):
        tiers.iloc[cut + 1:] = tier_number
    return tiers.reindex(points.index)


def add_vor(board: pd.DataFrame, league_size: int = DEFAULT_LEAGUE_SIZE,
            starters: dict[str, int] | None = None) -> pd.DataFrame:
    """Value over replacement: projected points minus the replacement-level
    player at that position (rank = league_size x starters at the position)."""
    starters = starters or DEFAULT_STARTERS
    out = board.copy()
    out["vor"] = 0.0
    for pos, group in out.groupby("position"):
        rank = league_size * starters.get(pos, 1)
        ordered = group["projected_points"].sort_values(ascending=False)
        replacement = ordered.iloc[min(rank, len(ordered)) - 1]
        out.loc[group.index, "vor"] = (group["projected_points"] - replacement).round(1)
    return out


def add_confidence_and_flags(board: pd.DataFrame, feature_frame: pd.DataFrame) -> pd.DataFrame:
    """Confidence from history depth; risk flags from durability signals."""
    history = (
        feature_frame[feature_frame["minutes"].fillna(0) > 0]
        .groupby("code")["season"].nunique().rename("seasons_played")
    )
    out = board.merge(history, on="code", how="left")
    out["seasons_played"] = out["seasons_played"].fillna(0).astype(int)
    out["confidence"] = np.select(
        [out["seasons_played"] >= 3, out["seasons_played"] == 2],
        ["high", "medium"], default="low",
    )
    latest_season = F.season_order(feature_frame)[-1]
    latest = feature_frame[feature_frame["season"] == latest_season].set_index("code")
    flags = []
    for code in out["code"]:
        row = latest.loc[code]
        row_flags = []
        minutes = float(row["minutes"] if pd.notna(row["minutes"]) else 0)
        lag = row["minutes_lag1"]
        if minutes < 1500:
            row_flags.append("low_minutes")
        if pd.notna(lag) and float(lag) > 0 and minutes < 0.6 * float(lag):
            row_flags.append("minutes_decline")
        if pd.isna(lag):
            row_flags.append("first_pl_season")
        flags.append(",".join(row_flags))
    out["risk_flags"] = flags
    return out


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

def top_n_precision(table: pd.DataFrame, pred_col: str, actual_col: str, n: int) -> float:
    n = min(n, len(table))
    if n == 0:
        return float("nan")
    actual = set(table.nlargest(n, actual_col)["code"])
    predicted = set(table.nlargest(n, pred_col)["code"])
    return len(actual & predicted) / n


def backtest(seasons: pd.DataFrame, target_season: str) -> pd.DataFrame:
    """Walk-forward: train (and internally select) on pairs labelled before the
    target season, predict the target, compare to the previous-season-points
    baseline on the same pool. The target season is never seen in selection."""
    pairs = prepare_pairs(seasons)
    train = pairs[pairs["next_season"] < target_season]
    test = pairs[pairs["next_season"] == target_season]
    test = test[test["minutes"].fillna(0) >= PRIOR_MINUTES_FLOOR]
    models = fit_models(train)

    top_n = {"GKP": 10, "DEF": 20, "MID": 20, "FWD": 15}
    rows = []
    for pos, model in models.items():
        pool = test[test["position"] == pos]
        if len(pool) < 5:
            continue
        pred = model.predict(pool)
        merged = pool[["code", "total_points", "next_total_points"]].join(
            pred[["projected_points"]]
        )
        actual = merged["next_total_points"].to_numpy(dtype=float)
        rows.append({
            "position": pos,
            "n": len(merged),
            "model": model.structure_choice,
            "model_spearman": round(_spearman_arrays(
                merged["projected_points"].to_numpy(dtype=float), actual), 3),
            "baseline_spearman": round(_spearman_arrays(
                merged["total_points"].to_numpy(dtype=float), actual), 3),
            "model_top_n": round(top_n_precision(merged, "projected_points",
                                                 "next_total_points", top_n[pos]), 3),
            "baseline_top_n": round(top_n_precision(merged, "total_points",
                                                    "next_total_points", top_n[pos]), 3),
            "model_mae": round(float(
                (merged["projected_points"] - merged["next_total_points"]).abs().mean()), 1),
        })
    report = pd.DataFrame(rows)
    report["beats_baseline"] = report["model_spearman"] >= report["baseline_spearman"]
    return report


# ---------------------------------------------------------------------------
# Projection for the upcoming season
# ---------------------------------------------------------------------------

def project_upcoming(seasons: pd.DataFrame,
                     league_size: int = DEFAULT_LEAGUE_SIZE) -> pd.DataFrame:
    """Fit on all pairs, project next season from the latest season's features."""
    pairs = prepare_pairs(seasons)
    models = fit_models(pairs)
    feature_frame = F.build_feature_frame(seasons)
    latest_season = F.season_order(seasons)[-1]
    latest = feature_frame[feature_frame["season"] == latest_season]
    latest = latest[latest["minutes"].fillna(0) >= PRIOR_MINUTES_FLOOR]
    latest = latest.assign(position=latest["element_type"].map(POSITIONS)).dropna(
        subset=["position"]
    )

    boards = []
    for pos, model in models.items():
        pool = latest[latest["position"] == pos]
        if pool.empty:
            continue
        board = model.predict(pool)
        board["tier"] = assign_tiers(board["projected_points"])
        boards.append(board)
    combined = pd.concat(boards, ignore_index=True)
    combined = add_vor(combined, league_size=league_size)
    combined = add_confidence_and_flags(combined, feature_frame)
    combined["position_rank"] = combined.groupby("position")["projected_points"] \
        .rank(ascending=False, method="first").astype(int)
    return combined.sort_values(["position", "position_rank"]).reset_index(drop=True)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Season projection model")
    parser.add_argument("--parquet", type=Path,
                        default=_repo_root() / "data/derived/ml/player_seasons.parquet")
    parser.add_argument("--target-season", default="2024-25",
                        help="held-out season for the walk-forward backtest")
    parser.add_argument("--project", action="store_true",
                        help="also write projections for the upcoming season")
    parser.add_argument("--out-dir", type=Path,
                        default=_repo_root() / "data/derived/ml")
    args = parser.parse_args(argv)

    seasons = pd.read_parquet(args.parquet)
    report = backtest(seasons, args.target_season)
    print(f"\nWalk-forward backtest -> actual {args.target_season}:")
    print(report.to_string(index=False))

    if args.project:
        board = project_upcoming(seasons)
        out_parquet = args.out_dir / "projections_2627.parquet"
        board.to_parquet(out_parquet, index=False)
        out_json = args.out_dir / "projections_2627.json"
        out_json.write_text(json.dumps(board.to_dict(orient="records"), indent=1))
        logger.info("wrote %d projections -> %s (+ .json for Go tools)",
                    len(board), out_parquet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
