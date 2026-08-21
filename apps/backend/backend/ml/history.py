"""Phase A — multi-season historical ingestion.

Builds the canonical cross-season player table that all next-season modeling
(Phases B/C) is built on: **one row per ``(code, season)``**, written to
``data/derived/ml/player_seasons.parquet``.

Contract: see ``HISTORY_INGEST_SPEC.md`` in this directory. Key facts (verified
empirically, do not re-assume):

* Cross-season identity uses the permanent ``code`` field, never the per-season
  ``id`` (which FPL reassigns every year).
* ``expected_goals`` / ``expected_assists`` / ``starts`` only exist from 2022-23
  onward; earlier seasons get ``null`` (never ``0`` — a zero would corrupt the
  per-90 math in Phase B).
* The 2025-26 source is the local **draft** bootstrap, which has no ``now_cost``
  (Draft FPL has no prices), so ``now_cost`` is ``null`` for that season only.

Sources:
    * vaastav/Fantasy-Premier-League ``players_raw.csv`` + ``teams.csv`` per
      season for 2019-20..2024-25 (fetched over HTTPS, cached on disk).
    * local ``data/raw/bootstrap/bootstrap-static.json`` for 2025-26.

No live FPL API calls. Network access is limited to the cached vaastav fetch.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
from pathlib import Path
from typing import Any

import pandas as pd
import requests

logger = logging.getLogger(__name__)

VAASTAV_BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
DEFAULT_HISTORICAL_SEASONS = ["2019-20", "2020-21", "2021-22", "2022-23", "2023-24", "2024-25"]
LOCAL_SEASON = "2025-26"

# Canonical columns that must be coerced to nullable integers.
INT_FIELDS = [
    "code", "season_id", "element_type", "team_id", "minutes", "starts",
    "total_points", "goals_scored", "assists", "clean_sheets", "goals_conceded",
    "saves", "bonus", "bps", "now_cost",
]
# Canonical columns that must be coerced to floats.
FLOAT_FIELDS = [
    "ict_index", "influence", "creativity", "threat",
    "expected_goals", "expected_assists",
]
# Full canonical column order for the output table.
CANONICAL_COLUMNS = [
    "code", "season", "season_id", "web_name", "element_type", "team_id",
    "team_name", "minutes", "starts", "total_points", "goals_scored", "assists",
    "clean_sheets", "goals_conceded", "saves", "bonus", "bps", "ict_index",
    "influence", "creativity", "threat", "expected_goals", "expected_assists",
    "now_cost",
]
# Source field name (identical in vaastav CSV and local JSON) -> canonical name.
# A source key absent for a given season simply leaves the canonical column null.
SOURCE_TO_CANONICAL = {
    "code": "code", "id": "season_id", "web_name": "web_name",
    "element_type": "element_type", "team": "team_id", "minutes": "minutes",
    "starts": "starts", "total_points": "total_points",
    "goals_scored": "goals_scored", "assists": "assists",
    "clean_sheets": "clean_sheets", "goals_conceded": "goals_conceded",
    "saves": "saves", "bonus": "bonus", "bps": "bps", "ict_index": "ict_index",
    "influence": "influence", "creativity": "creativity", "threat": "threat",
    "expected_goals": "expected_goals", "expected_assists": "expected_assists",
    "now_cost": "now_cost",
}


def _coerce_int(value: Any) -> int | None:
    """Coerce a raw cell to int, returning None for empty/garbage/missing."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))  # tolerate "12" and "12.0"
    except ValueError:
        return None


def _coerce_float(value: Any) -> float | None:
    """Coerce a raw cell to float, returning None for empty/garbage/missing."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return None if math.isnan(result) else result
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _canonical_row(src: dict, season: str, team_names: dict[int, str]) -> dict:
    """Map one source record (CSV row or JSON element) to a canonical row.

    Missing source keys stay ``None`` so absent columns become nulls rather than
    zeros. ``team_name`` is resolved from that season's team-id map.
    """
    row: dict[str, Any] = dict.fromkeys(CANONICAL_COLUMNS)
    row["season"] = season
    for src_key, canon in SOURCE_TO_CANONICAL.items():
        if src_key not in src:
            continue
        raw = src.get(src_key)
        if canon in FLOAT_FIELDS:
            row[canon] = _coerce_float(raw)
        elif canon in INT_FIELDS:
            row[canon] = _coerce_int(raw)
        else:
            row[canon] = None if raw is None else str(raw)
    team_id = row.get("team_id")
    if team_id is not None:
        row["team_name"] = team_names.get(team_id)
    return row


def _parse_teams_csv(teams_csv_text: str) -> dict[int, str]:
    """Build a season's team-id -> team-name map from a vaastav teams.csv."""
    out: dict[int, str] = {}
    for rec in csv.DictReader(io.StringIO(teams_csv_text)):
        team_id = _coerce_int(rec.get("id"))
        if team_id is not None:
            out[team_id] = rec.get("name")
    return out


def parse_vaastav_players(players_csv_text: str, teams_csv_text: str, season: str) -> list[dict]:
    """Parse a vaastav players_raw.csv (+ teams.csv) into canonical rows."""
    team_names = _parse_teams_csv(teams_csv_text)
    return [
        _canonical_row(rec, season, team_names)
        for rec in csv.DictReader(io.StringIO(players_csv_text))
    ]


def parse_local_bootstrap(bootstrap: dict, season: str = LOCAL_SEASON) -> list[dict]:
    """Parse a local draft bootstrap-static.json into canonical rows.

    Tolerates both the plain-list shape (``elements: [...]``) and the wrapped
    shape (``elements: {"data": [...]}``) seen across FPL API variants.
    """
    elements = bootstrap.get("elements")
    if isinstance(elements, dict):
        elements = elements.get("data", [])
    teams = bootstrap.get("teams")
    if isinstance(teams, dict):
        teams = teams.get("data", [])
    team_names: dict[int, str] = {}
    for team in teams or []:
        team_id = _coerce_int(team.get("id"))
        if team_id is not None:
            team_names[team_id] = team.get("name")
    return [_canonical_row(el, season, team_names) for el in elements or []]


def _fetch_text(url: str, cache_path: Path, session: requests.Session) -> str:
    """Fetch a URL as text, caching to disk so re-runs do not re-download."""
    if cache_path.exists():
        logger.debug("cache hit %s", cache_path)
        return cache_path.read_text(encoding="utf-8")
    logger.info("fetching %s", url)
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(resp.text, encoding="utf-8")
    return resp.text


def load_vaastav_season(
    season: str,
    scratch_dir: Path,
    base_url: str = VAASTAV_BASE,
    session: requests.Session | None = None,
) -> list[dict]:
    """Download (cached) and parse one historical season from vaastav."""
    session = session or requests.Session()
    players = _fetch_text(
        f"{base_url}/{season}/players_raw.csv",
        Path(scratch_dir) / season / "players_raw.csv",
        session,
    )
    teams = _fetch_text(
        f"{base_url}/{season}/teams.csv",
        Path(scratch_dir) / season / "teams.csv",
        session,
    )
    return parse_vaastav_players(players, teams, season)


def load_local_season(bootstrap_path: Path, season: str = LOCAL_SEASON) -> list[dict]:
    """Parse the local bootstrap snapshot for the most recent season."""
    bootstrap = json.loads(Path(bootstrap_path).read_text(encoding="utf-8"))
    return parse_local_bootstrap(bootstrap, season)


def build_dataframe(rows: list[dict]) -> pd.DataFrame:
    """Assemble canonical rows into a typed, validated DataFrame.

    Drops rows lacking a ``code`` (cannot be joined across seasons), enforces
    nullable integer / float dtypes, and asserts ``(code, season)`` uniqueness.
    """
    frame = pd.DataFrame(rows, columns=CANONICAL_COLUMNS)
    frame = frame[frame["code"].notna()].reset_index(drop=True)
    for column in INT_FIELDS:
        frame[column] = frame[column].astype("Int64")
    for column in FLOAT_FIELDS:
        frame[column] = frame[column].astype("float64")
    duplicate_count = int(frame.duplicated(subset=["code", "season"]).sum())
    if duplicate_count:
        raise ValueError(f"{duplicate_count} duplicate (code, season) rows in ingest output")
    return frame


def summarize(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-season ingest summary: row count, distinct players, % null coverage."""
    grouped = frame.groupby("season")
    summary = pd.DataFrame(
        {
            "rows": grouped.size(),
            "distinct_players": grouped["code"].nunique(),
            "pct_xg_null": (grouped["expected_goals"].apply(lambda s: s.isna().mean() * 100).round(1)),
            "pct_now_cost_null": (grouped["now_cost"].apply(lambda s: s.isna().mean() * 100).round(1)),
        }
    )
    return summary.reset_index()


def ingest(
    historical_seasons: list[str] = DEFAULT_HISTORICAL_SEASONS,
    local_bootstrap_path: Path | None = None,
    scratch_dir: Path | None = None,
    base_url: str = VAASTAV_BASE,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """Ingest all configured seasons into the canonical player-season table."""
    if scratch_dir is None:
        raise ValueError("scratch_dir is required for vaastav download caching")
    session = session or requests.Session()
    rows: list[dict] = []
    for season in historical_seasons:
        season_rows = load_vaastav_season(season, scratch_dir, base_url, session)
        logger.info("season %s: %d players (vaastav)", season, len(season_rows))
        rows.extend(season_rows)
    if LOCAL_SEASON in historical_seasons:
        # Already ingested from vaastav — loading the local archive too would
        # duplicate every (code, season) row. Fresh machines pass 2025-26 in
        # --seasons precisely because they lack the local archive; ignore any
        # stray local file so both sources are never mixed.
        logger.info("season %s ingested from vaastav; skipping local bootstrap", LOCAL_SEASON)
    elif local_bootstrap_path and Path(local_bootstrap_path).exists():
        local_rows = load_local_season(Path(local_bootstrap_path))
        logger.info("season %s: %d players (local)", LOCAL_SEASON, len(local_rows))
        rows.extend(local_rows)
    else:
        logger.warning("local bootstrap not found at %s; skipping %s", local_bootstrap_path, LOCAL_SEASON)
    return build_dataframe(rows)


def write_parquet(frame: pd.DataFrame, out_path: Path) -> None:
    """Write the canonical table to parquet, creating parent dirs as needed."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out_path, index=False)


def _repo_root() -> Path:
    """Repo root resolved relative to this file (.../apps/backend/backend/ml)."""
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ingest all seasons, write parquet, print a summary."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    repo_root = _repo_root()
    parser = argparse.ArgumentParser(description="Phase A historical multi-season ingestion")
    parser.add_argument(
        "--out", type=Path,
        default=repo_root / "data/derived/ml/player_seasons.parquet",
        help="output parquet path",
    )
    parser.add_argument(
        "--bootstrap", type=Path,
        default=repo_root / "data/raw/bootstrap/bootstrap-static.json",
        help="local 2025-26 bootstrap-static.json",
    )
    parser.add_argument(
        "--scratch-dir", type=Path,
        default=repo_root / "data/derived/ml/_vaastav_cache",
        help="cache dir for downloaded vaastav CSVs",
    )
    parser.add_argument(
        "--seasons", nargs="*", default=DEFAULT_HISTORICAL_SEASONS,
        help="historical seasons to pull from vaastav",
    )
    args = parser.parse_args(argv)

    frame = ingest(
        historical_seasons=args.seasons,
        local_bootstrap_path=args.bootstrap,
        scratch_dir=args.scratch_dir,
    )
    write_parquet(frame, args.out)
    summary = summarize(frame)
    logger.info("wrote %d rows -> %s", len(frame), args.out)
    print(summary.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
