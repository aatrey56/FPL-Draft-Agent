"""player_values — per-player model values for the Go ``trade_check`` tool.

The Go server reads local JSON only (no parquet), so this step exports the
values waiver_plan/my_week already compute for EVERY player — not just the
user's squad and the free agents — to
``data/derived/<season>/ml/player_values.json``:

* ``xp_next`` / ``xp_source`` — next-GW xP (match model ``xp_gw{N}``, else
  the ``ros/38`` heuristic, else ``none``); same table as waiver_plan
  (``waiver.build_player_table``) with ``role_overrides.json`` applied.
* ``xp_h3`` / ``xp_h3_source`` — next-3-GW xP (``xp_horizon_gw{N}``'s
  ``xp_h3`` when fresh, else the heuristic).
* ``ros_adj`` (and raw ``ros_points``) — role-adjusted rest-of-season value.

Top-level ``gw`` is the bootstrap's next gameweek the values are for;
``panel_max_gw`` the last finished GW in the match model's panel (None
without model xP). trade_check treats the file as stale when ``gw`` differs
from the bootstrap's next gameweek. Scorer metadata (``scorer``,
``xp_fallback``/``_reason``, ``horizon_fallback``/``_reason``) records what
actually ran, exactly as in waiver_plan.json; ``horizon_events`` lists the
events summed into ``xp_h3`` (None without the horizon file; fewer than 3
near season end), which trade_check uses to label the span honestly.

No league or entry id is needed: values are league-independent.

Run after waiver/my_week:
``python -m backend.ml.player_values --season 2026-27 [--out PATH]``
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from backend.ml import jsonutil, paths
from backend.ml import waiver as wv

logger = logging.getLogger(__name__)

# Per-player fields exported, in output order.
PLAYER_FIELDS = ["code", "element", "web_name", "position", "team", "status",
                 "xp_next", "xp_source", "xp_h3", "xp_h3_source",
                 "ros_points", "ros_adj", "p_start"]


def _clean(value: Any) -> Any:
    """NaN/NA -> None; numpy scalars -> Python scalars."""
    if value is None:
        return None
    if not isinstance(value, (str, bool)) and pd.isna(value):
        return None
    return value.item() if hasattr(value, "item") else value


def build_player_values(players: pd.DataFrame, *, season: str, gw: int | None,
                        panel_max_gw: int | None, meta: dict[str, Any],
                        generated_at: str) -> dict[str, Any]:
    """The player_values document from a waiver player table.

    ``players`` is ``waiver.build_player_table`` output (after
    ``apply_role_overrides``); ``next3_xp``/``next3_source`` are exported as
    ``xp_h3``/``xp_h3_source``. Rows are sorted by ``code`` so the output is
    deterministic for a given input.
    """
    frame = players.rename(columns={"next3_xp": "xp_h3", "next3_source": "xp_h3_source"})
    frame = frame.sort_values("code", kind="stable")
    rows = [{field: _clean(row.get(field)) for field in PLAYER_FIELDS}
            for row in frame.to_dict("records")]
    return {"season": season, "gw": gw, "panel_max_gw": panel_max_gw,
            "generated_at": generated_at, **meta, "players": rows}


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + rename (never half-written)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _panel_max_gw(gw_xp: pd.DataFrame | None) -> int | None:
    if gw_xp is None or gw_xp.empty or "panel_max_gw" not in gw_xp:
        return None
    return int(gw_xp["panel_max_gw"].max())


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Export player_values.json for trade_check")
    parser.add_argument("--season", default="2026-27")
    parser.add_argument("--scorer", choices=wv.SCORERS, default="model",
                        help="next-GW xP source (default model, heuristic fallback)")
    parser.add_argument("--data-root", type=Path, default=_repo_root() / "data")
    parser.add_argument("--out", type=Path, default=None,
                        help="output JSON (default <data-root>/derived/<season>/ml/player_values.json)")
    args = parser.parse_args(argv)

    try:
        projections_path = paths.projections_path(args.season, args.data_root)
        prior_season = paths.prior_season(args.season)
    except (FileNotFoundError, ValueError) as exc:
        parser.exit(2, f"{parser.prog}: error: {exc}\n")
    raw_root = paths.raw_root(args.season, args.data_root)
    bootstrap = json.loads((raw_root / "bootstrap/bootstrap-static.json").read_text(encoding="utf-8"))
    seasons = pd.read_parquet(paths.seasons_table_path(args.data_root))
    ml_dir = paths.derived_root(args.season, args.data_root) / "ml"

    gw_xp, scorer_meta = wv.resolve_scorer(args.scorer, bootstrap, ml_dir)
    horizon_xp, horizon_meta = wv.resolve_horizon("3", gw_xp, bootstrap, ml_dir)
    gw = wv.next_event(bootstrap)
    players = wv.build_player_table(
        bootstrap, seasons, projections_path, gw_xp=gw_xp, horizon_xp=horizon_xp,
        season_panel=wv.load_season_panel(ml_dir / "player_gameweeks.parquet", args.season),
        prior_season=prior_season)
    players, _ = wv.apply_role_overrides(
        players, wv.load_role_overrides(ml_dir / "role_overrides.json"), gw,
        wv.event_deadlines(bootstrap), horizon_xp)

    meta = {**scorer_meta, **{k: horizon_meta[k] for k in
                              ("horizon_fallback", "horizon_fallback_reason", "horizon_events")}}
    doc = build_player_values(players, season=args.season, gw=gw,
                              panel_max_gw=_panel_max_gw(gw_xp), meta=meta,
                              generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    out = args.out or ml_dir / "player_values.json"
    write_atomic(out, jsonutil.dumps_strict(doc, indent=1))
    logger.info("wrote %d players (gw %s, scorer %s) -> %s",
                len(doc["players"]), gw, doc["scorer"], out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
