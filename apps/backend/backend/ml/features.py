"""Feature engineering for the season projection model.

Turns ``player_seasons.parquet`` (one row per ``(code, season)``) into the
leakage-safe training table the projection model consumes: features computed
from season *S* (plus lagged history strictly ≤ *S*), labelled with season
*S+1* outcomes, joined on the permanent ``code``.

Contract: ``PROJECTION_MODEL_SPEC.md``. Key stances implemented here:

* **Per-90 rates, not totals** — totals conflate quality with volume.
  Per-90 columns are null when the season's minutes are below
  ``PER90_MIN_MINUTES`` (rates on tiny samples are noise, and nulls are
  imputed with training means downstream — never fabricated as 0).
* **Multi-year finishing skill** — the expanding, leakage-safe cumulative
  ``goals - xG`` (and ``assists - xA``) gap per 90, shrunk toward 0 by
  ``minutes / (minutes + SHRINK_K)`` so one hot season regresses while a
  persistent multi-season gap survives. Uses only seasons ≤ S with xG data
  (xG exists from 2022-23 onward; earlier seasons contribute nothing).
"""

from __future__ import annotations

import pandas as pd

# Below this many season-minutes, per-90 rates are unstable -> null (imputed later).
PER90_MIN_MINUTES = 450
# Shrinkage constant (minutes) for the finishing-skill gap: at 900 cumulative
# minutes the observed gap is halved; a full season (~3000') keeps ~77% of it.
SHRINK_K = 900
# Stats converted to per-90 rates (source column -> feature name).
PER90_SOURCES = {
    "total_points": "pts90",
    "expected_goals": "xg90",
    "expected_assists": "xa90",
    "goals_scored": "goals90",
    "assists": "assists90",
    "ict_index": "ict90",
    "bps": "bps90",
    "threat": "threat90",
    "creativity": "creativity90",
    "influence": "influence90",
    "saves": "saves90",
    "clean_sheets": "cs90",
    "goals_conceded": "gc90",
}


def add_per90(seasons: pd.DataFrame) -> pd.DataFrame:
    """Add per-90 rate columns; null when minutes < PER90_MIN_MINUTES."""
    out = seasons.copy()
    minutes = out["minutes"].astype("Float64")
    eligible = minutes >= PER90_MIN_MINUTES
    for src, name in PER90_SOURCES.items():
        if src not in out.columns:
            continue
        rate = (out[src].astype("Float64") / minutes) * 90
        out[name] = rate.where(eligible)
    return out


def add_finishing_skill(seasons: pd.DataFrame) -> pd.DataFrame:
    """Add shrunk, leakage-safe cumulative finishing-skill columns.

    For each ``(code, season)`` row: over that player's seasons ≤ S that have
    xG data, compute the cumulative per-90 over/under-performance
    ``(Σ goals − Σ xG) / Σ minutes × 90`` and shrink it by
    ``Σ minutes / (Σ minutes + SHRINK_K)``. Same for assists vs xA.
    Rows with no xG history get 0 (no evidence either way).
    """
    out = seasons.sort_values(["code", "season"]).copy()
    has_xg = out["expected_goals"].notna()
    mins = out["minutes"].fillna(0).astype(float).where(has_xg, 0.0)
    goal_gap = (
        out["goals_scored"].fillna(0).astype(float)
        - out["expected_goals"].astype(float).fillna(0)
    ).where(has_xg, 0.0)
    assist_gap = (
        out["assists"].fillna(0).astype(float)
        - out["expected_assists"].astype(float).fillna(0)
    ).where(has_xg, 0.0)

    grouped = out.assign(_m=mins, _g=goal_gap, _a=assist_gap).groupby("code")
    cum_minutes = grouped["_m"].cumsum()
    cum_goal_gap = grouped["_g"].cumsum()
    cum_assist_gap = grouped["_a"].cumsum()

    shrink = cum_minutes / (cum_minutes + SHRINK_K)
    safe_minutes = cum_minutes.replace(0, pd.NA)
    out["finishing_skill"] = ((cum_goal_gap / safe_minutes * 90) * shrink).fillna(0.0).astype(float)
    out["assist_skill"] = ((cum_assist_gap / safe_minutes * 90) * shrink).fillna(0.0).astype(float)
    return out.drop(columns=[c for c in ("_m", "_g", "_a") if c in out.columns])


def add_lags(seasons: pd.DataFrame) -> pd.DataFrame:
    """Add previous-season durability context (minutes_lag1), per player."""
    out = seasons.sort_values(["code", "season"]).copy()
    out["minutes_lag1"] = out.groupby("code")["minutes"].shift(1).astype("Float64")
    return out


def season_order(seasons: pd.DataFrame) -> list[str]:
    """Chronologically sorted season labels ('2019-20' sorts correctly)."""
    return sorted(seasons["season"].unique())


def build_feature_frame(seasons: pd.DataFrame) -> pd.DataFrame:
    """All feature columns on the raw season table (still one row per code+season)."""
    return add_lags(add_finishing_skill(add_per90(seasons)))


def build_pairs(seasons: pd.DataFrame) -> pd.DataFrame:
    """Leakage-safe training pairs: features from season S, labels from S+1.

    Only consecutive-season pairs are formed (a player absent in S+1 has no
    label and is dropped — see spec: not labelled 0). Label columns are
    ``next_total_points`` and ``next_minutes``.
    """
    feats = build_feature_frame(seasons)
    order = season_order(seasons)
    successor = {s: order[i + 1] for i, s in enumerate(order[:-1])}

    labels = seasons[["code", "season", "total_points", "minutes"]].rename(
        columns={
            "season": "next_season",
            "total_points": "next_total_points",
            "minutes": "next_minutes",
        }
    )
    feats["next_season"] = feats["season"].map(successor)
    pairs = feats.merge(labels, on=["code", "next_season"], how="inner")
    return pairs.reset_index(drop=True)
