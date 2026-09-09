"""Phase A.2 — per-gameweek panel ingestion.

Builds the match-level table that the weekly model needs and the season model
uses for form / finishing-skill features: **one row per ``(code, season, gw)``**,
written to ``data/derived/ml/player_gameweeks.parquet``.

Contract: see ``GAMEWEEK_INGEST_SPEC.md`` in this directory. Key facts:

* Source is the project's own ``data/raw/gw/{gw}/live.json`` (all 38 GWs of
  2025-26) — no live FPL API calls, no network at all.
* ``live.json`` keys players by the per-season ``id``; the panel stores the
  permanent ``code`` (mapped via that season's ``bootstrap-static.json``).
* A player's ``explain`` is a list of ``[stat_breakdown, fixture_id]`` pairs —
  one per fixture that GW. ``len(explain)`` gives ``num_fixtures``: 0 = blank
  GW, 1 = normal, 2 = double GW. On a DGW the summed stats cannot be attributed
  to a single opponent, so ``opponent_team`` / ``was_home`` are null.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

LOCAL_SEASON = "2025-26"
DEFAULT_MAX_GW = 38

# Stat fields lifted from each element's `stats` block into the panel.
INT_STATS = [
    "minutes", "total_points", "goals_scored", "assists", "clean_sheets",
    "goals_conceded", "saves", "bonus", "bps", "defensive_contribution",
    "tackles", "recoveries", "clearances_blocks_interceptions",
    "yellow_cards", "red_cards",
]
FLOAT_STATS = [
    "expected_goals", "expected_assists", "expected_goal_involvements",
    "expected_goals_conceded", "threat", "creativity", "influence", "ict_index",
]
CANONICAL_COLUMNS = [
    "code", "season", "gw", "season_element_id", "element_type", "team_id",
    "opponent_team", "opponent_name", "was_home", "num_fixtures",
    "played", "started", *INT_STATS, *FLOAT_STATS,
]


def _coerce_int(value: Any) -> int | None:
    """Coerce a raw stat cell to int; None for empty/garbage/missing."""
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
        return int(float(text))
    except ValueError:
        return None


def _coerce_float(value: Any) -> float | None:
    """Coerce a raw stat cell to float; None for empty/garbage/missing."""
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


def build_id_maps(bootstrap: dict) -> dict[str, dict]:
    """Build per-season lookup maps from a bootstrap-static.json.

    Returns id->code, id->element_type, id->team_id, and team_id->name. The
    season ``id`` is the key used inside ``live.json``; ``code`` is permanent.
    """
    elements = bootstrap.get("elements")
    if isinstance(elements, dict):
        elements = elements.get("data", [])
    teams = bootstrap.get("teams")
    if isinstance(teams, dict):
        teams = teams.get("data", [])
    id_to_code: dict[int, int] = {}
    id_to_type: dict[int, int] = {}
    id_to_team: dict[int, int] = {}
    for el in elements or []:
        eid = _coerce_int(el.get("id"))
        if eid is None:
            continue
        id_to_code[eid] = _coerce_int(el.get("code"))
        id_to_type[eid] = _coerce_int(el.get("element_type"))
        id_to_team[eid] = _coerce_int(el.get("team"))
    team_names: dict[int, str] = {}
    for team in teams or []:
        tid = _coerce_int(team.get("id"))
        if tid is not None:
            team_names[tid] = team.get("name")
    return {
        "id_to_code": id_to_code,
        "id_to_type": id_to_type,
        "id_to_team": id_to_team,
        "team_names": team_names,
    }


def _derive_opponent(
    team_id: int | None,
    fixture_ids: list[int],
    fixtures_by_id: dict[int, dict],
    team_names: dict[int, str],
) -> tuple[int | None, str | None, bool | None]:
    """Resolve (opponent_team, opponent_name, was_home) for a single fixture.

    Returns all-null when the player has 0 or 2 fixtures (blank / double GW) or
    the fixture / team cannot be resolved — the summed stats of a DGW cannot be
    attributed to one opponent.
    """
    if team_id is None or len(fixture_ids) != 1:
        return None, None, None
    fixture = fixtures_by_id.get(fixture_ids[0])
    if fixture is None:
        return None, None, None
    home, away = _coerce_int(fixture.get("team_h")), _coerce_int(fixture.get("team_a"))
    if team_id == home:
        return away, team_names.get(away), True
    if team_id == away:
        return home, team_names.get(home), False
    return None, None, None


def parse_gw_live(live: dict, gw: int, season: str, id_maps: dict[str, dict]) -> list[dict]:
    """Parse one gameweek's live.json into canonical per-player rows."""
    fixtures_by_id = {
        _coerce_int(f.get("id")): f for f in live.get("fixtures", []) or []
    }
    id_to_code = id_maps["id_to_code"]
    id_to_type = id_maps["id_to_type"]
    id_to_team = id_maps["id_to_team"]
    team_names = id_maps["team_names"]

    rows: list[dict] = []
    for raw_id, payload in (live.get("elements") or {}).items():
        eid = _coerce_int(raw_id)
        code = id_to_code.get(eid)
        if code is None:
            continue  # unmappable element — cannot join across seasons
        stats = payload.get("stats", {}) or {}
        explain = payload.get("explain", []) or []
        fixture_ids = [
            _coerce_int(entry[1])
            for entry in explain
            if isinstance(entry, list) and len(entry) == 2
        ]
        team_id = id_to_team.get(eid)
        opponent_team, opponent_name, was_home = _derive_opponent(
            team_id, fixture_ids, fixtures_by_id, team_names
        )
        minutes = _coerce_int(stats.get("minutes")) or 0

        row: dict[str, Any] = dict.fromkeys(CANONICAL_COLUMNS)
        row.update(
            code=code, season=season, gw=gw, season_element_id=eid,
            element_type=id_to_type.get(eid), team_id=team_id,
            opponent_team=opponent_team, opponent_name=opponent_name,
            was_home=was_home, num_fixtures=len(fixture_ids),
            played=minutes > 0, started=_coerce_int(stats.get("starts")) == 1,
        )
        for stat in INT_STATS:
            row[stat] = _coerce_int(stats.get(stat))
        for stat in FLOAT_STATS:
            row[stat] = _coerce_float(stats.get(stat))
        rows.append(row)
    return rows


def build_dataframe(rows: list[dict]) -> pd.DataFrame:
    """Assemble canonical rows into a typed frame; assert (code, season, gw) unique."""
    frame = pd.DataFrame(rows, columns=CANONICAL_COLUMNS)
    frame = frame[frame["code"].notna()].reset_index(drop=True)
    id_cols = ["code", "gw", "season_element_id", "element_type", "team_id",
               "opponent_team", "num_fixtures", *INT_STATS]
    for column in id_cols:
        frame[column] = frame[column].astype("Int64")
    for column in FLOAT_STATS:
        frame[column] = frame[column].astype("float64")
    for column in ("played", "started"):
        frame[column] = frame[column].astype("boolean")
    frame["was_home"] = frame["was_home"].astype("boolean")
    dup = int(frame.duplicated(subset=["code", "season", "gw"]).sum())
    if dup:
        raise ValueError(f"{dup} duplicate (code, season, gw) rows in panel output")
    return frame


def ingest_local(
    gw_root: Path,
    bootstrap_path: Path,
    season: str = LOCAL_SEASON,
    max_gw: int = DEFAULT_MAX_GW,
) -> pd.DataFrame:
    """Ingest all local per-GW live.json files into the panel frame."""
    bootstrap = json.loads(Path(bootstrap_path).read_text(encoding="utf-8"))
    id_maps = build_id_maps(bootstrap)
    rows: list[dict] = []
    for gw in range(1, max_gw + 1):
        path = Path(gw_root) / str(gw) / "live.json"
        if not path.exists():
            logger.warning("missing %s; skipping GW%d", path, gw)
            continue
        live = json.loads(path.read_text(encoding="utf-8"))
        gw_rows = parse_gw_live(live, gw, season, id_maps)
        logger.info("GW%d: %d player rows", gw, len(gw_rows))
        rows.extend(gw_rows)
    return build_dataframe(rows)


def summarize(frame: pd.DataFrame) -> dict:
    """Coverage summary: totals, GWs, played share, DGW / blank counts."""
    return {
        "rows": len(frame),
        "distinct_players": int(frame["code"].nunique()),
        "gws": int(frame["gw"].nunique()),
        "pct_played": round(frame["played"].mean() * 100, 1),
        "dgw_rows": int((frame["num_fixtures"] == 2).sum()),
        "blank_rows": int((frame["num_fixtures"] == 0).sum()),
    }


def write_parquet(frame: pd.DataFrame, out_path: Path) -> None:
    """Write the panel to parquet, creating parent dirs as needed."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out_path, index=False)


def _repo_root() -> Path:
    """Repo root resolved relative to this file (.../apps/backend/backend/ml)."""
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ingest all local GWs, write parquet, print a summary."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    repo_root = _repo_root()
    parser = argparse.ArgumentParser(description="Phase A.2 per-gameweek panel ingestion")
    parser.add_argument("--out", type=Path,
                        default=repo_root / "data/derived/ml/player_gameweeks.parquet")
    parser.add_argument("--gw-root", type=Path, default=repo_root / "data/raw/gw")
    parser.add_argument("--bootstrap", type=Path,
                        default=repo_root / "data/raw/bootstrap/bootstrap-static.json")
    parser.add_argument("--max-gw", type=int, default=DEFAULT_MAX_GW)
    parser.add_argument("--season", type=str, default=LOCAL_SEASON,
                        help="season label written into the panel, e.g. 2026-27")
    args = parser.parse_args(argv)

    frame = ingest_local(args.gw_root, args.bootstrap, season=args.season,
                         max_gw=args.max_gw)
    write_parquet(frame, args.out)
    summary = summarize(frame)
    logger.info("wrote %d rows -> %s", len(frame), args.out)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
