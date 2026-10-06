"""Live track record: the match model's xP against what actually happened, per GW.

The model's own output is the accountability log (#178). Every finished
gameweek of a season is scored once it is in the panel, and the result is kept
in ``data/derived/<season>/ml/track_record.csv`` (machine-readable) and
``track_record.md`` (the human view), so "is the model any good this season?"
has a standing, per-week answer instead of a one-off backtest.

Where a gameweek's prediction comes from — the ``source`` column:

* ``live`` — ``xp_gw{N}.parquet`` exists: the forecast actually served before
  that deadline. ``make derive`` only ever writes the *next* gameweek's file,
  so a past file is a pre-deadline forecast and is never overwritten after
  the fact. A file whose modification time is later than the gameweek's
  deadline (e.g. a manual ``make xp GW=3`` run in October) is not a forecast
  and is ignored with a warning, as is a file whose ``gw`` column is not N.
* ``replay`` — no usable file: the gameweek is rebuilt as it would have been
  served (``matchmodel.served_walk_forward``): trained on the seasons that
  started earlier plus this season's gameweeks before it, and featurised from
  outcome-free stubs through ``stub_frame``, the path ``build_gw_xp`` serves
  through. Replay is **not live evidence**: it is rebuilt with today's code, and
  the availability gate is inert (the panel carries no historical injury
  flags), so its Stage-1 Brier is a minutes-only number.

Live wins over replay. A gameweek carries one source; when it is re-scored, all
of its earlier rows are replaced, so a re-run with unchanged inputs rewrites
the CSV byte-for-byte. Rows already recorded as ``live`` are kept even if the
xP file later disappears — the log does not forget live evidence.

A gameweek already in the CSV is not scored again (a replay re-fit costs tens
of seconds, and the autopilot runs derive every 15 minutes) unless
``--rescore`` is passed, it is named in ``--gws``, or it was recorded as
``replay`` and a usable live file has since appeared.

Scoring reuses ``matcheval.score_predictor`` through ``matchmodel.compare``:
per position, on the startable and full pools, for ``model_xp`` and the five
naive baselines, each restricted to the players the model covered that week so
every predictor ranks the same set. Gameweeks where no player has in-season
history (GW1 — form features are built per season) are skipped.

FPL's ``ep_next`` is not a baseline: it is null for every element in the draft
API, so there is nothing to record.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from backend.ml import matcheval as me
from backend.ml import matchmodel as mm
from backend.ml.matchfeatures import build_match_frame

logger = logging.getLogger(__name__)

LIVE, REPLAY = "live", "replay"
POOLS = ("startable", "all")
PREDICTORS = ["model_xp", *me.BASELINES]
KEY = ["gw", "pool", "position", "predictor", "source"]
METRICS = ["spearman", "top_frac", "mae", "n", "brier", "p_start_mean", "start_rate"]
COLUMNS = KEY + METRICS
# Six decimals: plenty for a rank correlation. Scores are rounded when computed
# and written in a fixed text format, so a re-run is byte-identical and the
# in-memory table equals the one read back from disk.
DECIMALS = 6
FLOAT_FORMAT = f"%.{DECIMALS}f"


class TrackRecordError(RuntimeError):
    """A gameweek could not be scored; the message names it."""


def finished_gameweeks(frame: pd.DataFrame, season: str) -> list[int]:
    """Gameweeks of ``season`` with labels and at least one player with history.

    The panel only holds finished gameweeks (derive never writes a partial one),
    so "labelled" is "finished". A gameweek with no in-season history (GW1)
    cannot be ranked by any trailing predictor and is skipped.
    """
    rows = frame[(frame["season"] == season) & frame["label_points"].notna()]
    gws = []
    for gw, group in rows.groupby("gw", sort=True):
        if group["has_history"].astype("boolean").fillna(False).any():
            gws.append(int(gw))
        else:
            logger.info("GW%d: no player has in-season history yet; skipped", gw)
    return gws


def _deadline(bootstrap: dict | None, gw: int) -> datetime | None:
    """The gameweek's deadline from a draft bootstrap, or None when unknown."""
    events = (bootstrap or {}).get("events") or []
    if isinstance(events, dict):
        events = events.get("data") or []
    for event in events:
        if int(event.get("id", -1)) == gw:
            return mm._parse_deadline(event.get("deadline_time"))
    return None


def _forecast_gws(path: Path) -> set[int]:
    """The distinct ``gw`` values in an xP file; empty when it has no ``gw`` column."""
    if "gw" not in pq.read_schema(path).names:
        return set()
    return {int(gw) for gw in pd.read_parquet(path, columns=["gw"])["gw"].dropna().unique()}


def live_xp_path(ml_dir: Path, gw: int, bootstrap: dict | None) -> Path | None:
    """``xp_gw{N}.parquet`` when it forecasts GW N and was written before the deadline.

    A file whose ``gw`` column is not exactly ``{N}`` (renamed or mis-written)
    is not GW N's forecast and is ignored with a warning.
    """
    path = ml_dir / f"xp_gw{gw}.parquet"
    if not path.exists():
        return None
    forecast = _forecast_gws(path)
    if forecast != {gw}:
        logger.warning("%s forecasts GW %s, not GW%d; not a live forecast, replaying instead",
                       path.name, sorted(forecast) or "unknown (no gw column)", gw)
        return None
    deadline = _deadline(bootstrap, gw)
    if deadline is not None:
        written = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if written > deadline:
            logger.warning("%s was written %s, after the GW%d deadline %s; "
                           "not a live forecast, replaying instead",
                           path.name, written.isoformat(), gw, deadline.isoformat())
            return None
    return path


def live_predictions(frame: pd.DataFrame, season: str, gw: int, xp_path: Path) -> pd.DataFrame:
    """The season's GW rows joined to the served forecast on ``code``.

    Only covered players are kept: a player missing from the file (signed after
    the deadline) was never ranked, so ranking the baselines over him would
    score them on a different set than the model.
    """
    served = pd.read_parquet(xp_path, columns=["code", "xp", "p_start"])
    target = frame[(frame["season"] == season) & (frame["gw"] == gw)]
    joined = target.merge(served, on="code", how="inner", validate="many_to_one")
    dropped = len(target) - len(joined)
    if dropped:
        logger.info("GW%d live: %d panel rows not in %s", gw, dropped, xp_path.name)
    return joined


def replay_predictions(panel: pd.DataFrame, season: str, gws: list[int],
                       min_train_rows: int = mm.MIN_TRAIN_ROWS) -> pd.DataFrame:
    """Rebuild ``gws`` through the serving path (``matchmodel.served_walk_forward``).

    Takes the raw ``panel``: each gameweek's features are rebuilt from stubs,
    as serving builds them. Raises ``TrackRecordError`` naming the first
    gameweek that had nothing to train on (no earlier season and no earlier
    rows of this one).
    """
    if not gws:
        return pd.DataFrame()
    scored = mm.served_walk_forward(panel, season, gws, alpha=dict(mm.SELECTED_ALPHAS),
                                    min_train_rows=min_train_rows)
    covered = set() if scored.empty else {int(gw) for gw in scored["gw"].unique()}
    missing = [gw for gw in gws if gw not in covered]
    if missing:
        raise TrackRecordError(
            f"GW{missing[0]}: no xp_gw{missing[0]}.parquet and too few training rows "
            "(earlier seasons + earlier gameweeks) to replay it")
    return scored[scored["xp"].notna()].reset_index(drop=True)


def _brier(rows: pd.DataFrame) -> dict:
    """Stage-1 P(start) against actually starting, on one position's rows."""
    usable = rows.dropna(subset=["p_start"])
    if usable.empty:
        return {"brier": math.nan, "p_start_mean": math.nan, "start_rate": math.nan}
    actual = mm._flags(usable, "started")
    return {
        "brier": float(((usable["p_start"] - actual) ** 2).mean()),
        "p_start_mean": float(usable["p_start"].mean()),
        "start_rate": float(actual.mean()),
    }


def score_gameweek(predicted: pd.DataFrame, gw: int, source: str,
                   fraction: float = me.TOP_FRACTION) -> pd.DataFrame:
    """One row per (pool, position, predictor) for a single scored gameweek.

    Every position × predictor is emitted even when a pool is thin, so the CSV
    has a fixed shape. A position with fewer than ``matcheval.MIN_TOP_N`` rows
    is reported as NaN rather than a correlation over two points.
    """
    rows = []
    for pool in POOLS:
        eligible = me.eligible(predicted, startable_only=pool == "startable", min_gw=gw)
        for code, position in mm.POSITIONS.items():
            subset = eligible[eligible["element_type"] == code]
            brier = _brier(subset) if len(subset) >= me.MIN_TOP_N else {}
            for predictor in PREDICTORS:
                column = "xp" if predictor == "model_xp" else me.BASELINES[predictor]
                if len(subset) >= me.MIN_TOP_N:
                    scores = me.score_predictor(
                        subset, column, predictor, fraction,
                        point_scale=True if predictor == "model_xp" else None)
                else:
                    scores = {"spearman": math.nan, "top_frac": math.nan, "mae": math.nan}
                row = {"gw": gw, "pool": pool, "position": position,
                       "predictor": predictor, "source": source,
                       "spearman": scores["spearman"], "top_frac": scores["top_frac"],
                       "mae": scores["mae"], "n": len(subset)}
                model_row = predictor == "model_xp"
                for metric in ("brier", "p_start_mean", "start_rate"):
                    row[metric] = brier.get(metric, math.nan) if model_row else math.nan
                rows.append(row)
    table = pd.DataFrame(rows, columns=COLUMNS)
    floats = ["spearman", "top_frac", "mae", "brier", "p_start_mean", "start_rate"]
    table[floats] = table[floats].astype("float64").round(DECIMALS)
    return table


def _sorted(table: pd.DataFrame) -> pd.DataFrame:
    """Canonical row order: gw, pool, position (GKP..FWD), predictor (model first)."""
    order = {
        "pool": {name: i for i, name in enumerate(POOLS)},
        "position": {name: i for i, name in enumerate(mm.POSITIONS.values())},
        "predictor": {name: i for i, name in enumerate(PREDICTORS)},
    }

    def sort_key(column: pd.Series) -> pd.Series:
        return column.map(order[column.name]) if column.name in order else column

    return (table.sort_values(["gw", "pool", "position", "predictor"], key=sort_key)
            .reset_index(drop=True)[COLUMNS])


def read_record(path: Path) -> pd.DataFrame:
    """The existing track record, or an empty table with the right columns."""
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(path)


def upsert(existing: pd.DataFrame, scored: pd.DataFrame) -> pd.DataFrame:
    """Replace every existing row of each re-scored gameweek, keep the rest.

    Replacing by gameweek (rather than only by the full key) is what keeps one
    source per gameweek when a replayed week later gains a live file.
    """
    if scored.empty:
        return _sorted(existing) if not existing.empty else existing
    kept = existing[~existing["gw"].isin(set(scored["gw"]))]
    frames = [frame for frame in (kept, scored) if not frame.empty]
    combined = pd.concat(frames, ignore_index=True)
    duplicated = combined.duplicated(KEY)
    if duplicated.any():
        raise TrackRecordError(f"duplicate track-record keys: {combined[duplicated][KEY]}")
    return _sorted(combined)


def write_record(table: pd.DataFrame, path: Path) -> None:
    """Write the CSV atomically in its fixed text format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    table.to_csv(tmp, index=False, float_format=FLOAT_FORMAT)
    tmp.replace(path)


def per_gw_summary(table: pd.DataFrame, pool: str = "startable") -> pd.DataFrame:
    """Per GW and position: model vs that week's best baseline."""
    rows = []
    view = table[table["pool"] == pool]
    for (gw, position), group in view.groupby(["gw", "position"], sort=False):
        model = group[group["predictor"] == "model_xp"].iloc[0]
        baselines = group[(group["predictor"] != "model_xp") & group["spearman"].notna()]
        best = baselines.loc[baselines["spearman"].idxmax()] if not baselines.empty else None
        rows.append({
            "gw": int(gw), "source": model["source"], "position": position,
            "model": model["spearman"],
            "baseline": best["spearman"] if best is not None else math.nan,
            "baseline_name": best["predictor"] if best is not None else "-",
            "delta": model["spearman"] - (best["spearman"] if best is not None else math.nan),
            "brier": model["brier"], "n": int(model["n"]),
        })
    return _order_positions(pd.DataFrame(rows), ["gw"])


def cumulative_summary(table: pd.DataFrame, pool: str = "startable") -> pd.DataFrame:
    """Mean per-GW Spearman, model vs the baseline with the best season mean."""
    view = table[table["pool"] == pool]
    rows = []
    for position, group in view.groupby("position", sort=False):
        means = group.groupby("predictor")["spearman"].mean()
        model_mean = means.get("model_xp", math.nan)
        others = means.drop("model_xp", errors="ignore").dropna()
        best_name = others.idxmax() if not others.empty else "-"
        best = others.max() if not others.empty else math.nan
        model_rows = group[group["predictor"] == "model_xp"]
        sources = model_rows["source"].value_counts()
        rows.append({
            "position": position,
            "gws": int(model_rows["gw"].nunique()),
            "live": int(sources.get(LIVE, 0)), "replay": int(sources.get(REPLAY, 0)),
            "model": model_mean, "baseline": best, "baseline_name": best_name,
            "delta": model_mean - best,
            "brier": float(model_rows["brier"].mean()),
        })
    return _order_positions(pd.DataFrame(rows), [])


def _order_positions(frame: pd.DataFrame, leading: list[str]) -> pd.DataFrame:
    if frame.empty:
        return frame
    rank = {name: i for i, name in enumerate(mm.POSITIONS.values())}
    ordered = frame.assign(_rank=frame["position"].map(rank))
    return ordered.sort_values([*leading, "_rank"]).drop(columns="_rank").reset_index(drop=True)


def _fmt(value: float) -> str:
    """Three decimals, or an em dash for a missing score."""
    return "—" if pd.isna(value) else f"{value:.3f}"


def render_markdown(table: pd.DataFrame, season: str) -> str:
    """The human view: per-GW model vs best baseline, then the season so far.

    Deterministic (no timestamps), so an unchanged record renders identically.
    """
    lines = [
        f"# Match-model track record — {season}",
        "",
        "Mean per-gameweek Spearman of predicted vs realized FPL points, startable",
        "pool (recent minutes). `live` = the `xp_gw{N}.parquet` forecast served before",
        "that deadline. `replay` = rebuilt afterwards on the serving path (earlier",
        "seasons + earlier GWs, today's code, no availability gate) — **not live",
        "evidence**. Generated by",
        "`python -m backend.ml.trackrecord`; the full table (both pools, top-20% hit",
        "rate, MAE) is `track_record.csv`.",
        "",
        "## Per gameweek",
        "",
        "| GW | source | evidence | pos | model ρ | best baseline | baseline ρ | Δ | Brier | n |",
        "|---:|---|---|---|---:|---|---:|---:|---:|---:|",
    ]
    for row in per_gw_summary(table).itertuples(index=False):
        evidence = "live forecast" if row.source == LIVE else "replay — not live evidence"
        lines.append(
            f"| {row.gw} | {row.source} | {evidence} | {row.position} | {_fmt(row.model)} "
            f"| {row.baseline_name} | {_fmt(row.baseline)} | {_fmt(row.delta)} "
            f"| {_fmt(row.brier)} | {row.n} |")
    for pool, title in (("startable", "startable pool"), ("all", "full pool (context)")):
        lines += [
            "",
            f"## Season so far — {title}",
            "",
            "| pos | GWs | live | replay | model mean ρ | best baseline | baseline mean ρ | Δ | mean Brier |",
            "|---|---:|---:|---:|---:|---|---:|---:|---:|",
        ]
        for row in cumulative_summary(table, pool).itertuples(index=False):
            lines.append(
                f"| {row.position} | {row.gws} | {row.live} | {row.replay} | {_fmt(row.model)} "
                f"| {row.baseline_name} | {_fmt(row.baseline)} | {_fmt(row.delta)} "
                f"| {_fmt(row.brier)} |")
    return "\n".join(lines) + "\n"


def run(panel: pd.DataFrame, season: str, ml_dir: Path, bootstrap: dict | None = None,
        gws: list[int] | None = None, fraction: float = me.TOP_FRACTION,
        min_train_rows: int = mm.MIN_TRAIN_ROWS, rescore: bool = False) -> pd.DataFrame:
    """Score new finished GWs (or re-score ``gws``), upsert the CSV, rewrite the markdown.

    ``panel`` is the raw archive panel(s) plus the season panel. Live weeks are
    joined to the built frame; replayed weeks are rebuilt from ``panel`` per
    gameweek. Gameweeks already recorded are skipped unless ``rescore`` is set,
    they are named in ``gws``, or a recorded replay week now has a live file.
    Returns the full, updated track record.
    """
    frame = build_match_frame(panel)
    csv_path = ml_dir / "track_record.csv"
    existing = read_record(csv_path)
    recorded = {int(gw): source for gw, source
                in existing.groupby("gw")["source"].first().items()}
    targets = finished_gameweeks(frame, season)
    if gws is not None:
        targets = [gw for gw in targets if gw in set(gws)]
    force = rescore or gws is not None

    scored, replay_gws = [], []
    for gw in targets:
        if not force and recorded.get(gw) == LIVE:
            logger.info("GW%d: already recorded (live); skipped", gw)
            continue
        path = live_xp_path(ml_dir, gw, bootstrap)
        if not force and recorded.get(gw) == REPLAY and path is None:
            logger.info("GW%d: already recorded (replay); skipped", gw)
            continue
        if path is not None:
            scored.append(score_gameweek(live_predictions(frame, season, gw, path),
                                         gw, LIVE, fraction))
        elif recorded.get(gw) == LIVE:
            logger.info("GW%d: keeping the recorded live rows (xp_gw%d.parquet is gone)", gw, gw)
        else:
            replay_gws.append(gw)
    replayed = replay_predictions(panel, season, replay_gws, min_train_rows)
    for gw in replay_gws:
        scored.append(score_gameweek(replayed[replayed["gw"] == gw], gw, REPLAY, fraction))

    new = pd.concat(scored, ignore_index=True) if scored else pd.DataFrame(columns=COLUMNS)
    table = upsert(existing, new)
    write_record(table, csv_path)
    md_path = ml_dir / "track_record.md"
    md_tmp = md_path.with_suffix(".md.tmp")
    md_tmp.write_text(render_markdown(table, season), encoding="utf-8")
    md_tmp.replace(md_path)
    logger.info("wrote %s (%d rows) and %s", csv_path, len(table), md_path)
    return table


def _load_panels(paths: list[Path]) -> pd.DataFrame:
    panels = []
    for path in paths:
        if not path.exists():
            logger.warning("panel %s not found; skipping", path)
            continue
        panel = pd.read_parquet(path)
        if not panel.empty:
            panels.append(panel)
    if not panels:
        raise TrackRecordError(f"no usable panel among {[str(p) for p in paths]}")
    return pd.concat(panels, ignore_index=True)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Score the match model's xP against realized points for every finished GW")
    parser.add_argument("--season", required=True, help="e.g. 2026-27")
    parser.add_argument("--data-root", type=Path, default=mm._repo_root() / "data",
                        help="the data/ directory (raw/ and derived/ beneath it)")
    parser.add_argument("--gws", type=mm._gw_range, default=None,
                        help="only re-score these finished gameweeks, e.g. 3 or 2-5 "
                             "(re-scored even if already recorded)")
    parser.add_argument("--rescore", action="store_true",
                        help="re-score gameweeks already in track_record.csv "
                             "(default: only new ones, or replay weeks that gained a live file)")
    args = parser.parse_args(argv)

    derived = args.data_root / "derived"
    ml_dir = derived / args.season / "ml"
    bootstrap_path = args.data_root / "raw" / args.season / "bootstrap/bootstrap-static.json"
    bootstrap = None
    if bootstrap_path.exists():
        bootstrap = json.loads(bootstrap_path.read_text(encoding="utf-8"))
    else:
        logger.warning("no bootstrap at %s; live files are trusted without a deadline check",
                       bootstrap_path)
    try:
        panel = _load_panels([derived / "ml/player_gameweeks.parquet",
                              ml_dir / "player_gameweeks.parquet"])
        table = run(panel, args.season, ml_dir, bootstrap, args.gws, rescore=args.rescore)
    except TrackRecordError as error:
        print(f"trackrecord: {error}")
        return 1

    print(f"\n== {args.season} track record: startable pool, model vs best baseline per GW ==")
    print(per_gw_summary(table).round(3).to_string(index=False))
    print("\n== season so far (startable pool) ==")
    print(cumulative_summary(table).round(3).to_string(index=False))
    print(f"\n{len(table)} rows in {ml_dir / 'track_record.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
