"""Two-stage, availability-gated expected-points model for one gameweek.

Contract: ``MATCH_MODEL_SPEC.md``. Predicts ``xP`` — a player's FPL points for
an upcoming gameweek — from the leakage-safe frame built by
``matchfeatures.build_match_frame``.

The structure follows the spec's central claim, which the baseline benchmark
confirmed: **playing time is the product, scoring is the margin.** Trailing
minutes out-ranks every points-based predictor in every position, so the model
is gated on availability rather than regressing points directly:

    xP = P(start) · E[points | started] + P(cameo) · E[points | cameo]

* **Stage 1 (``StartModel``)** answers *will they play*, from trailing start
  rate and minutes only — role, not quality — then multiplies by the live
  availability flag from bootstrap. It is deliberately a separate object with a
  narrow interface so a predicted-lineups feed (spec Tier-2) can replace it
  without Stage 2 changing.
* **Stage 2 (``PointsModel``)** answers *how many points if they do*, as a
  per-position ridge over player form and the fixture they walk into. Fitting
  per position is what makes the weighting position-specific: the same
  opponent column earns a different coefficient for a keeper than a forward.

Everything is closed-form ridge (reused from ``projection.Ridge``) — no seed,
no iteration, no new dependency, and the fitted coefficients are readable, so
``drivers`` can tell a tool *why* a player is rated.

Two modelling stances worth knowing:

* **Points are modelled per fixture, not per gameweek.** Training labels are
  divided by ``num_fixtures`` and predictions multiplied back, so a double
  gameweek is two fixtures' worth of xP and a blank is zero — rather than the
  model quietly learning an average number of fixtures.
* **Availability is a live-only signal.** The panel carries no historical
  injury flags, so the gate is inert during the backtest (factor 1.0) and only
  bites when serving against a current bootstrap. Backtest numbers therefore
  *understate* the gate's value; they do not depend on it.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from backend.ml import matcheval as me
from backend.ml.matchfeatures import build_match_frame
from backend.ml.projection import Ridge
from backend.ml.waiver import availability_factor

logger = logging.getLogger(__name__)

POSITIONS = me.POSITIONS

# Ridge strength. Selected per position, because the positions differ by an
# order of magnitude in how much data they offer: ~18 startable keepers a
# gameweek against ~115 midfielders, over the same feature count.
RIDGE_ALPHA = 100.0
ALPHA_GRID = [1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0]
# Alpha is chosen on gameweeks up to SELECTION_MAX_GW and the result is claimed
# on the ones after it. Selecting and reporting on the same gameweeks would
# make "beats the baseline" a statement about this season's noise.
SELECTION_MAX_GW = 24
# Chosen by `--backtest` on the selection window (see MODEL_ROADMAP.md). GKP
# lands on heavy shrinkage: with that little data, stage 2 adds noise and the
# honest keeper model is almost entirely "is he the number one".
SELECTED_ALPHAS = {1: 1000.0, 2: 300.0, 3: 100.0, 4: 100.0}
# Below this many training rows a position is left unfitted rather than fitted
# on noise; its rows fall back to a null prediction the caller can filter.
MIN_TRAIN_ROWS = 200
# Cameo scoring is nearly constant (an appearance point plus the rare return),
# so below this many cameo rows a mean beats a regression.
MIN_CAMEO_ROWS = 100
# First gameweek predicted by the walk-forward: earlier ones have too little
# trailing history for either stage to be fitted honestly.
MIN_TRAIN_GW = 5

# Stage 1 — role only. Deliberately excludes points: the question is whether
# the manager picks them, not whether they are good.
MINUTES_FEATURES = [
    "startrate_l3", "startrate_l5", "startrate_std",
    "mins_l1", "mins_l3", "mins_l5", "mins_std",
    "games_std",
]

# Stage 2 — player form, shared across positions.
FORM_FEATURES = [
    "pts_l1", "pts_l3", "pts_l5", "pts_std", "pts_sd_std",
    "xgi_l3", "xgi_l5", "bps_l3", "bps_l5", "ict_l3",
    "mins_l3", "mins_l5", "startrate_l5",
]
# Stage 2 — the fixture. `opp_pts_allowed_pg` is position-aware already: it is
# the points that opponent has conceded per game to this player's position.
FIXTURE_FEATURES = [
    "was_home", "opp_scored_pg", "opp_conceded_pg", "opp_pts_allowed_pg",
    "team_scored_pg", "team_conceded_pg",
]
# Position-specific additions (spec §6): keepers and defenders are paid for
# what the opponent fails to do, attackers for what they do themselves.
POSITION_FEATURES = {
    1: ["saves_l3", "saves_l5"],
    2: ["dc_l3", "dc_l5", "xg_l3", "xa_l3"],
    3: ["xg_l3", "xa_l3", "dc_l3", "dc_l5"],
    4: ["xg_l3", "xg_l5", "xa_l3"],
}
# Width of the floor/ceiling band, in trailing standard deviations of the
# player's gameweek points. One sigma — a plausible range, not a calibrated
# quantile (distributions are Phase C of the roadmap).
BAND_SIGMA = 1.0


def feature_columns(position: int) -> list[str]:
    """Stage-2 feature list for one position, order-stable and deduplicated."""
    seen: set[str] = set()
    ordered: list[str] = []
    for column in FORM_FEATURES + FIXTURE_FEATURES + POSITION_FEATURES.get(position, []):
        if column not in seen:
            seen.add(column)
            ordered.append(column)
    return ordered


def _flags(rows: pd.DataFrame, column: str) -> pd.Series:
    """A possibly-nullable boolean column as plain 0.0/1.0 floats."""
    return rows[column].astype("boolean").fillna(False).astype("float64")


def fixture_count(rows: pd.DataFrame) -> pd.Series:
    """``num_fixtures`` as a non-negative float; missing means a single fixture."""
    counts = pd.to_numeric(rows.get("num_fixtures"), errors="coerce")
    if counts is None:
        return pd.Series(1.0, index=rows.index)
    return counts.fillna(1.0).clip(lower=0.0).astype("float64")


def per_fixture_label(rows: pd.DataFrame) -> pd.Series:
    """Gameweek points spread over the fixtures that produced them.

    Clipped at one fixture: a row with a label but no recorded fixture is a
    data defect, and dividing by zero would poison the fit.
    """
    return rows["label_points"] / fixture_count(rows).clip(lower=1.0)


@dataclass
class StartModel:
    """Stage 1 — ``P(start)`` and ``P(appear)``, gated on live availability.

    Two ridge heads share the trailing-role features: one regresses ``started``,
    one ``played``. Outputs are clipped to [0, 1] and ordered so appearing is
    never less likely than starting; the gap is the cameo probability.

    Swap point for spec Tier-2: a predicted-lineups feed implements the same
    ``predict`` signature and Stage 2 is untouched.
    """

    alpha: float = RIDGE_ALPHA
    start_: Ridge | None = None
    appear_: Ridge | None = None

    def fit(self, rows: pd.DataFrame) -> "StartModel":
        self.start_ = Ridge(alpha=self.alpha, columns=list(MINUTES_FEATURES)).fit(
            rows, _flags(rows, "started"))
        self.appear_ = Ridge(alpha=self.alpha, columns=list(MINUTES_FEATURES)).fit(
            rows, _flags(rows, "played"))
        return self

    def predict(self, rows: pd.DataFrame,
                availability: pd.Series | None = None) -> pd.DataFrame:
        """Per-row ``p_start``/``p_appear``/``p_cameo``, availability applied."""
        p_start = np.clip(self.start_.predict(rows), 0.0, 1.0)
        p_appear = np.maximum(np.clip(self.appear_.predict(rows), 0.0, 1.0), p_start)
        if availability is not None:
            factor = np.clip(
                pd.to_numeric(availability, errors="coerce").fillna(1.0).to_numpy(float),
                0.0, 1.0)
            p_start, p_appear = p_start * factor, p_appear * factor
        return pd.DataFrame(
            {"p_start": p_start, "p_appear": p_appear, "p_cameo": p_appear - p_start},
            index=rows.index)


@dataclass
class PointsModel:
    """Stage 2 — points for a single fixture, conditional on how a player featured.

    Separate fits for starters and cameos: a substitute's scoring is a
    different process (an appearance point and little else), and pooling them
    drags every starter's estimate toward the bench.
    """

    columns: list[str]
    alpha: float = RIDGE_ALPHA
    min_train_rows: int = MIN_TRAIN_ROWS
    min_cameo_rows: int = MIN_CAMEO_ROWS
    started_: Ridge | None = None
    cameo_: Ridge | None = None
    cameo_mean_: float = 0.0

    def fit(self, rows: pd.DataFrame) -> "PointsModel":
        started = rows[_flags(rows, "started") > 0]
        if len(started) >= self.min_train_rows:
            self.started_ = Ridge(alpha=self.alpha, columns=list(self.columns)).fit(
                started, per_fixture_label(started))
        cameo = rows[(_flags(rows, "played") > 0) & (_flags(rows, "started") == 0)]
        cameo_label = per_fixture_label(cameo)
        self.cameo_mean_ = float(cameo_label.mean()) if len(cameo) else 0.0
        if len(cameo) >= self.min_cameo_rows:
            self.cameo_ = Ridge(alpha=self.alpha, columns=list(self.columns)).fit(
                cameo, cameo_label)
        return self

    @property
    def fitted(self) -> bool:
        return self.started_ is not None

    def expected(self, rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """``(E[points | started], E[points | cameo])`` per fixture, floored at zero.

        Negative expected points are possible arithmetic but not a useful
        prediction — a fixture cannot cost a manager less than a blank.
        """
        started = np.clip(self.started_.predict(rows), 0.0, None)
        if self.cameo_ is not None:
            cameo = np.clip(self.cameo_.predict(rows), 0.0, None)
        else:
            cameo = np.full(len(rows), max(self.cameo_mean_, 0.0))
        return started, cameo

    def drivers(self, rows: pd.DataFrame, top: int = 3) -> pd.Series:
        """Top contributing features to ``E[points | started]``, per row."""
        contributions = self.started_.contributions(rows)
        ordered = contributions.abs().to_numpy().argsort(axis=1)[:, ::-1][:, :top]
        names = np.array(contributions.columns)
        return pd.Series(
            [", ".join(names[row]) for row in ordered], index=rows.index)


@dataclass
class MatchModel:
    """Per-position pair of stages, fitted on completed gameweeks only.

    ``alpha`` is either one ridge strength for every position or a mapping from
    ``element_type`` to its own (missing positions fall back to
    ``RIDGE_ALPHA``).
    """

    alpha: float | dict[int, float] = field(
        default_factory=lambda: dict(SELECTED_ALPHAS))
    min_train_rows: int = MIN_TRAIN_ROWS
    min_cameo_rows: int = MIN_CAMEO_ROWS
    start_: dict[int, StartModel] = field(default_factory=dict)
    points_: dict[int, PointsModel] = field(default_factory=dict)
    spread_: dict[int, float] = field(default_factory=dict)

    def alpha_for(self, position: int) -> float:
        if isinstance(self.alpha, dict):
            return float(self.alpha.get(position, RIDGE_ALPHA))
        return float(self.alpha)

    def fit(self, frame: pd.DataFrame) -> "MatchModel":
        """Fit both stages per position on rows with history and a known label."""
        train = frame[frame["has_history"].astype("boolean").fillna(False)
                      & frame["label_points"].notna()]
        for position in POSITIONS:
            rows = train[train["element_type"] == position]
            if len(rows) < self.min_train_rows:
                logger.debug("position %s: %d rows, not fitted", position, len(rows))
                continue
            alpha = self.alpha_for(position)
            self.start_[position] = StartModel(alpha=alpha).fit(rows)
            points = PointsModel(feature_columns(position), alpha,
                                 self.min_train_rows, self.min_cameo_rows).fit(rows)
            if points.fitted:
                self.points_[position] = points
            # Fallback dispersion for players with no trailing variance of
            # their own (their first gameweeks): what this position typically
            # swings by.
            self.spread_[position] = float(
                pd.to_numeric(rows["pts_sd_std"], errors="coerce").median() or 0.0)
        return self

    @property
    def positions(self) -> list[int]:
        return sorted(set(self.start_) & set(self.points_))

    def predict(self, rows: pd.DataFrame,
                availability: pd.Series | None = None) -> pd.DataFrame:
        """Per-fixture expected points and its components, indexed like ``rows``.

        Rows in a position the model could not fit come back as NaN rather than
        a fabricated zero, so callers can tell "no opinion" from "predicted
        blank".
        """
        columns = ["p_start", "p_appear", "p_cameo", "xp_started", "xp_cameo",
                   "xp_fixture", "xp_spread", "drivers"]
        out = pd.DataFrame(np.nan, index=rows.index, columns=columns)
        out["drivers"] = pd.Series(pd.NA, index=rows.index, dtype="object")
        for position in self.positions:
            mask = rows["element_type"] == position
            subset = rows[mask]
            if subset.empty:
                continue
            gate = None if availability is None else availability[mask]
            chances = self.start_[position].predict(subset, gate)
            points = self.points_[position]
            started, cameo = points.expected(subset)
            out.loc[mask, chances.columns] = chances
            out.loc[mask, "xp_started"] = started
            out.loc[mask, "xp_cameo"] = cameo
            out.loc[mask, "xp_fixture"] = (
                chances["p_start"].to_numpy() * started
                + chances["p_cameo"].to_numpy() * cameo)
            spread = pd.to_numeric(subset["pts_sd_std"], errors="coerce")
            out.loc[mask, "xp_spread"] = spread.fillna(self.spread_.get(position, 0.0))
            out.loc[mask, "drivers"] = points.drivers(subset)
        return out


def _band(xp: pd.Series, spread: pd.Series, p_start: pd.Series,
          fixtures: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Floor/ceiling around ``xp``.

    Scaled by ``p_start`` (a benched player's range collapses toward zero) and
    by ``sqrt(fixtures)`` (independent fixtures add variance, not deviation).
    The floor is clipped at zero: FPL points for a gameweek cannot go negative
    in aggregate for a player who might not feature at all.
    """
    width = BAND_SIGMA * spread * p_start * np.sqrt(fixtures.clip(lower=0.0))
    return (xp - width).clip(lower=0.0), xp + width


def score_panel(model: MatchModel, rows: pd.DataFrame,
                availability: pd.Series | None = None) -> pd.DataFrame:
    """Score gameweek-level panel rows: per-fixture xP scaled by ``num_fixtures``.

    Used by the backtest, where each row is one player-gameweek carrying its own
    fixture count (a blank is zero, a double is two fixtures' worth).
    """
    out = model.predict(rows, availability)
    fixtures = fixture_count(rows)
    out["num_fixtures"] = fixtures
    out["xp"] = out["xp_fixture"] * fixtures
    out["xp_floor"], out["xp_ceiling"] = _band(
        out["xp"], out["xp_spread"], out["p_start"], fixtures)
    return out


def score_fixtures(model: MatchModel, rows: pd.DataFrame,
                   availability: pd.Series | None = None) -> pd.DataFrame:
    """Score one-row-per-fixture input and sum to one row per player.

    Used for serving, where a double gameweek is two rows with *different*
    opponents — each scored against its own fixture, then added.
    """
    scored = model.predict(rows, availability)
    scored = pd.concat([rows.reset_index(drop=True), scored.reset_index(drop=True)], axis=1)
    grouped = scored.groupby("code", sort=True)
    summed = grouped.agg(
        xp=("xp_fixture", "sum"),
        num_fixtures=("xp_fixture", "size"),
        p_start=("p_start", "max"),
        p_appear=("p_appear", "max"),
        xp_spread=("xp_spread", "first"),
        element_type=("element_type", "first"),
        team_id=("team_id", "first"),
        drivers=("drivers", "first"),
    ).reset_index()
    def _opponents(group: pd.DataFrame) -> str:
        """Readable fixture list, e.g. "vSUN, @LIV" — names when we have them."""
        names = (group["opponent_name"].fillna(group["opponent_team"])
                 if "opponent_name" in group else group["opponent_team"])
        return ", ".join(f"{'v' if home else '@'}{opponent}"
                         for home, opponent in zip(group["was_home"], names))

    summed["opponents"] = grouped.apply(_opponents, include_groups=False).to_numpy()
    summed["xp_floor"], summed["xp_ceiling"] = _band(
        summed["xp"], summed["xp_spread"], summed["p_start"], summed["num_fixtures"])
    return summed


def walk_forward(frame: pd.DataFrame, min_gw: int = MIN_TRAIN_GW,
                 alpha: float | dict[int, float] | None = None,
                 min_train_rows: int = MIN_TRAIN_ROWS) -> pd.DataFrame:
    """Refit on gameweeks 1..t, predict t+1, for every t — within each season.

    Returns ``frame``'s predicted rows with the model's columns attached. The
    features were already built to see only prior gameweeks; this loop adds the
    matching guarantee for the *labels*, so nothing the model learned from was
    unknown at the deadline it is being scored on.
    """
    alpha = dict(SELECTED_ALPHAS) if alpha is None else alpha
    predictions = []
    for season, season_frame in frame.groupby("season", sort=True):
        gameweeks = sorted(int(gw) for gw in season_frame["gw"].dropna().unique())
        for target_gw in [gw for gw in gameweeks if gw > min_gw]:
            train = season_frame[season_frame["gw"] < target_gw]
            target = season_frame[season_frame["gw"] == target_gw]
            if target.empty:
                continue
            model = MatchModel(alpha=alpha, min_train_rows=min_train_rows).fit(train)
            if not model.positions:
                continue
            scored = score_panel(model, target)
            predictions.append(pd.concat([target, scored], axis=1))
        logger.info("walk-forward: season %s, %d gameweeks predicted",
                    season, max(0, len([g for g in gameweeks if g > min_gw])))
    if not predictions:
        return pd.DataFrame(columns=list(frame.columns) + ["xp"])
    return pd.concat(predictions, ignore_index=True)


def compare(scored: pd.DataFrame, pool: str,
            fraction: float = me.TOP_FRACTION) -> pd.DataFrame:
    """Score the model against every naive baseline, per position, one pool."""
    rows = []
    predictors = {"model_xp": "xp", **me.BASELINES}
    for code, name in POSITIONS.items():
        subset = scored[scored["element_type"] == code]
        if subset.empty:
            continue
        for label, column in predictors.items():
            if column not in subset.columns:
                continue
            rows.append({
                "pool": pool, "position": name, "predictor": label,
                **me.score_predictor(subset, column, label, fraction,
                                     point_scale=True if label == "model_xp" else None),
            })
    return pd.DataFrame(rows)


def start_calibration(scored: pd.DataFrame) -> pd.DataFrame:
    """Stage 1 backtest: does ``p_start`` predict actually starting?

    Reports the Brier score (lower is better; 0.25 is a coin flip) and, in
    deciles of predicted probability, the observed start rate — the honest test
    of a probability, as distinct from a ranking.
    """
    usable = scored.dropna(subset=["p_start"]).copy()
    usable["actual"] = _flags(usable, "started")
    rows = []
    for code, name in POSITIONS.items():
        subset = usable[usable["element_type"] == code]
        if subset.empty:
            continue
        rows.append({
            "position": name,
            "brier": float(((subset["p_start"] - subset["actual"]) ** 2).mean()),
            "predicted_rate": float(subset["p_start"].mean()),
            "actual_rate": float(subset["actual"].mean()),
            "n": int(len(subset)),
        })
    return pd.DataFrame(rows)


def upcoming_fixture_rows(bootstrap: dict, gw: int, season: str) -> pd.DataFrame:
    """One stub row per (player, fixture) for an upcoming gameweek.

    The draft bootstrap carries a rolling window of future fixtures keyed by
    event. Each player gets a row per fixture their team plays that gameweek —
    zero rows for a blank, two for a double — with only the facts known before
    kickoff (opponent, venue). Outcome columns are left absent so that, once
    appended to the panel, ``build_match_frame`` computes their trailing
    features from completed gameweeks alone.
    """
    fixtures = (bootstrap.get("fixtures") or {}).get(str(gw)) or []
    teams = {int(t["id"]): t.get("short_name") or t.get("name")
             for t in bootstrap.get("teams") or []}
    per_team: dict[int, list[tuple[int, bool]]] = {}
    for fixture in fixtures:
        home, away = fixture.get("team_h"), fixture.get("team_a")
        if home is None or away is None:
            continue
        per_team.setdefault(int(home), []).append((int(away), True))
        per_team.setdefault(int(away), []).append((int(home), False))
    rows = []
    for element in bootstrap.get("elements") or []:
        team = element.get("team")
        if team is None:
            continue
        for opponent, was_home in per_team.get(int(team), []):
            rows.append({
                "code": int(element["code"]),
                "season": season,
                "gw": int(gw),
                "element_type": int(element["element_type"]),
                "team_id": int(team),
                "opponent_team": opponent,
                "opponent_name": teams.get(opponent),
                "was_home": was_home,
                "num_fixtures": len(per_team.get(int(team), [])),
                "played": False,
            })
    return pd.DataFrame(rows)


def availability_series(bootstrap: dict, codes: pd.Series) -> pd.Series:
    """Live availability factor per player code, aligned to ``codes``."""
    by_code = {int(e["code"]): availability_factor(e)
               for e in bootstrap.get("elements") or []}
    return codes.map(by_code).astype("float64").fillna(1.0)


def build_gw_xp(panel: pd.DataFrame, bootstrap: dict, gw: int, season: str,
                alpha: float | dict[int, float] | None = None,
                min_train_rows: int = MIN_TRAIN_ROWS) -> pd.DataFrame:
    """Fit on everything completed and score the upcoming gameweek.

    Appending the stub rows to the panel *before* building features is what
    keeps serving and training consistent: the same shift-and-roll code path
    produces both, so a feature can never mean one thing in the backtest and
    another on Sunday morning.
    """
    stubs = upcoming_fixture_rows(bootstrap, gw, season)
    if stubs.empty:
        raise ValueError(f"no fixtures for gameweek {gw} in the bootstrap")
    frame = build_match_frame(pd.concat([panel, stubs], ignore_index=True))
    target = frame[(frame["season"] == season) & (frame["gw"] == gw)]
    model = MatchModel(alpha=alpha or dict(SELECTED_ALPHAS),
                       min_train_rows=min_train_rows).fit(
        frame[frame["label_points"].notna()])
    if not model.positions:
        raise ValueError("no position had enough training rows to fit")
    availability = availability_series(bootstrap, target["code"])
    scored = score_fixtures(model, target, availability)
    scored["season"], scored["gw"] = season, gw
    return scored.sort_values("xp", ascending=False).reset_index(drop=True)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3].parent


def _eligible(scored: pd.DataFrame, startable_only: bool = True) -> pd.DataFrame:
    """Predicted rows that can be scored fairly: history, and optionally startable."""
    out = scored[scored["has_history"].astype("boolean").fillna(False)]
    if startable_only:
        out = out[out["is_startable"].astype("boolean").fillna(False)]
    return out


def backtest(frame: pd.DataFrame, min_gw: int = MIN_TRAIN_GW,
             grid: list[float] | None = None,
             selection_max_gw: int = SELECTION_MAX_GW,
             fraction: float = me.TOP_FRACTION,
             min_train_rows: int = MIN_TRAIN_ROWS) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """Choose the ridge strength on early gameweeks, then score the later ones.

    Returns ``(selected_alphas, selection_table, heldout_rows)``. The split is
    the point: tuning on the same gameweeks the result is quoted on would turn
    "beats the baseline" into a statement about one season's noise. Each
    position's held-out predictions come from the run at *its own* selected
    alpha, so the reported model is exactly the one this module ships.
    """
    grid = list(grid or ALPHA_GRID)
    by_code = {name: code for code, name in POSITIONS.items()}
    runs = {alpha: walk_forward(frame, min_gw=min_gw, alpha=alpha,
                                min_train_rows=min_train_rows) for alpha in grid}

    selection = []
    for alpha, scored in runs.items():
        window = _eligible(scored)
        window = window[window["gw"] <= selection_max_gw]
        report = compare(window, "selection", fraction)
        for _, row in report[report["predictor"] == "model_xp"].iterrows():
            selection.append({"alpha": alpha, "position": row["position"],
                              "spearman": row["spearman"]})
    selection_table = pd.DataFrame(selection)
    best = selection_table.loc[selection_table.groupby("position")["spearman"].idxmax()]
    selected = {by_code[row["position"]]: float(row["alpha"]) for _, row in best.iterrows()}

    heldout = pd.concat(
        [runs[alpha][(runs[alpha]["gw"] > selection_max_gw)
                     & (runs[alpha]["element_type"] == code)]
         for code, alpha in selected.items()],
        ignore_index=True)
    return selected, selection_table, heldout


def summarise(scored: pd.DataFrame, fraction: float = me.TOP_FRACTION) -> pd.DataFrame:
    """Model against the best naive baseline, per position, on one row each."""
    report = compare(scored, "startable", fraction)
    model = report[report["predictor"] == "model_xp"].set_index("position")
    others = report[report["predictor"] != "model_xp"]
    best = others.loc[others.groupby("position")["spearman"].idxmax()].set_index("position")
    return pd.DataFrame({
        "model": model["spearman"],
        "baseline": best["spearman"],
        "baseline_name": best["predictor"],
        "delta": model["spearman"] - best["spearman"],
        "model_top_frac": model["top_frac"],
        "baseline_top_frac": best["top_frac"],
        "model_mae": model["mae"],
        "gws": model["gws"],
    })


def _print_backtest(frame: pd.DataFrame, min_gw: int, fraction: float,
                    selection_max_gw: int) -> pd.DataFrame:
    selected, selection_table, heldout = backtest(
        frame, min_gw=min_gw, selection_max_gw=selection_max_gw, fraction=fraction)

    print(f"\n== ridge alpha selection (GW {min_gw + 1}-{selection_max_gw}) ==")
    print(selection_table.pivot(index="alpha", columns="position",
                                values="spearman").round(4).to_string())
    print("selected: " + ", ".join(
        f"{POSITIONS[code]}={alpha:g}" for code, alpha in sorted(selected.items())))

    startable = _eligible(heldout)
    print(f"\n== HELD OUT (GW>{selection_max_gw}), startable pool: "
          "mean per-gameweek Spearman ==")
    print(compare(startable, "startable", fraction)
          .pivot(index="predictor", columns="position", values="spearman")
          .round(3).to_string())

    print(f"\n== HELD OUT (GW>{selection_max_gw}): model vs best baseline ==")
    print(summarise(startable, fraction).round(3).to_string())

    print("\n== HELD OUT: full panel pool (context, not the decision set) ==")
    print(summarise(_eligible(heldout, startable_only=False), fraction).round(3).to_string())

    print("\n== HELD OUT: stage 1 P(start) calibration (startable pool) ==")
    print(start_calibration(startable).round(3).to_string(index=False))

    result = compare(startable, "startable", fraction)
    result["window"] = f"heldout_gw{selection_max_gw + 1}+"
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    root = _repo_root()
    parser = argparse.ArgumentParser(
        description="Match expected-points model: walk-forward backtest and per-GW xP")
    parser.add_argument("--panel", type=Path, nargs="+",
                        default=[root / "data/derived/ml/player_gameweeks.parquet"],
                        help="one or more panels; several are concatenated so a new "
                             "season can be scored with the archive behind it")
    parser.add_argument("--alpha", type=float, default=None,
                        help="one ridge strength for every position "
                             "(default: the per-position SELECTED_ALPHAS)")
    parser.add_argument("--min-gw", type=int, default=MIN_TRAIN_GW)
    parser.add_argument("--top-fraction", type=float, default=me.TOP_FRACTION)
    parser.add_argument("--backtest", action="store_true",
                        help="run the walk-forward evaluation and print the report")
    parser.add_argument("--selection-max-gw", type=int, default=SELECTION_MAX_GW,
                        help="last gameweek used to select alpha; later ones are held out")
    parser.add_argument("--gw", type=int, default=None,
                        help="build xp_gw{N}.parquet for this upcoming gameweek")
    parser.add_argument("--season", type=str, default=None,
                        help="season for --gw, e.g. 2026-27 (also picks the data root)")
    parser.add_argument("--bootstrap", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    panel = pd.concat([pd.read_parquet(path) for path in args.panel],
                      ignore_index=True)

    if args.backtest:
        frame = build_match_frame(panel)
        result = _print_backtest(frame, args.min_gw, args.top_fraction,
                                 args.selection_max_gw)
        out = args.out or root / "data/derived/ml/match_model_backtest.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(out, index=False)
        logger.info("wrote %s", out)
        return 0

    if args.gw is None:
        parser.error("choose one of --backtest or --gw")

    season = args.season
    if season is None:
        parser.error("--gw requires --season")
    bootstrap_path = args.bootstrap or (
        root / f"data/raw/{season}/bootstrap/bootstrap-static.json")
    bootstrap = json.loads(Path(bootstrap_path).read_text(encoding="utf-8"))
    scored = build_gw_xp(panel, bootstrap, args.gw, season,
                         alpha=args.alpha or dict(SELECTED_ALPHAS))
    out = args.out or root / f"data/derived/{season}/ml/xp_gw{args.gw}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    scored.to_parquet(out, index=False)
    logger.info("wrote %s (%d players)", out, len(scored))
    print(scored.head(20)[
        ["code", "element_type", "opponents", "p_start", "xp", "xp_floor",
         "xp_ceiling", "drivers"]].round(2).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
