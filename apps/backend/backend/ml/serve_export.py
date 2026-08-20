"""Export serving artifacts for the Go decision tools.

Writes ``data/derived/ml/player_history.json`` — every player's multi-season
history keyed by permanent ``code`` — from ``player_seasons.parquet``. The Go
``player_card`` tool shows this next to the projection so a one-season dip or
spike (the Salah case) is always visible to the human making the call.

Run after any ingest refresh: ``python -m backend.ml.serve_export``
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

HISTORY_COLUMNS = ["season", "team_name", "minutes", "total_points",
                   "goals_scored", "assists", "expected_goals", "expected_assists"]


def build_history(seasons: pd.DataFrame) -> dict[str, list[dict]]:
    """code (as str, JSON-object key) -> chronological season rows."""
    out: dict[str, list[dict]] = {}
    frame = seasons.sort_values(["code", "season"])
    for code, group in frame.groupby("code"):
        rows = []
        for _, r in group.iterrows():
            row = {}
            for col in HISTORY_COLUMNS:
                value = r.get(col)
                row[col] = None if pd.isna(value) else (
                    value if isinstance(value, str) else float(value))
            rows.append(row)
        out[str(int(code))] = rows
    return out


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Export player_history.json for the Go tools")
    parser.add_argument("--parquet", type=Path,
                        default=_repo_root() / "data/derived/ml/player_seasons.parquet")
    parser.add_argument("--out", type=Path,
                        default=_repo_root() / "data/derived/ml/player_history.json")
    args = parser.parse_args(argv)

    seasons = pd.read_parquet(args.parquet)
    history = build_history(seasons)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(history))
    logger.info("wrote history for %d players -> %s", len(history), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
