"""Drop-radar — turn league element-status snapshots into ownership intel.

The fetcher archives timestamped snapshots of ``league/{id}/element-status``
(every player's ``owner``: an entry id, or null = free agent). This module
diffs consecutive snapshots into **ownership events** and ranks the current
**free agents** by projected value — the two feeds a 12-team league lives on:

* "who just got dropped" — an ``owner -> null`` transition, timestamped;
* "which quality players went unclaimed after waivers" — free agents sorted
  by next-season projection (from ``projections_2627``), so the post-waiver
  sweep is a ranked list instead of a scroll through 400 names.

Reads local files only (no live API calls). CLI:

    python -m backend.ml.ownership --season 2026-27 --league 999999
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def load_snapshots(history_dir: Path) -> list[tuple[str, dict[int, int | None]]]:
    """Load timestamped snapshots as (ts, {element_id: owner}) sorted by ts.

    Snapshot filenames are ``<UTC ts>.json`` (e.g. ``20260820T0025.json``);
    lexicographic order == chronological order.
    """
    snapshots = []
    for path in sorted(Path(history_dir).glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        owners = {
            int(row["element"]): row.get("owner")
            for row in data.get("element_status", [])
        }
        snapshots.append((path.stem, owners))
    return snapshots


def diff_snapshots(
    prev: dict[int, int | None], curr: dict[int, int | None], ts: str
) -> list[dict[str, Any]]:
    """Ownership events between two snapshots.

    Event kinds: ``drop`` (owner -> free agent), ``add`` (free agent -> owner),
    ``move`` (owner -> different owner, e.g. a trade). New elements appearing
    mid-season with an owner also register as ``add``.
    """
    events: list[dict[str, Any]] = []
    for element in sorted(set(prev) | set(curr)):
        before = prev.get(element)
        after = curr.get(element)
        if before == after:
            continue
        kind = "move"
        if before is not None and after is None:
            kind = "drop"
        elif before is None and after is not None:
            kind = "add"
        events.append({
            "ts": ts, "element": element, "kind": kind,
            "from_owner": before, "to_owner": after,
        })
    return events


def build_events(snapshots: list[tuple[str, dict[int, int | None]]]) -> list[dict[str, Any]]:
    """All ownership events across the full snapshot history (idempotent)."""
    events: list[dict[str, Any]] = []
    for (_, prev), (ts, curr) in zip(snapshots, snapshots[1:]):
        events.extend(diff_snapshots(prev, curr, ts))
    return events


def _player_index(bootstrap: dict) -> dict[int, dict[str, Any]]:
    """element id -> identity/context from that season's bootstrap."""
    teams = {t["id"]: t.get("short_name") or t.get("name") for t in bootstrap.get("teams", [])}
    index = {}
    for el in bootstrap.get("elements", []):
        index[el["id"]] = {
            "code": el.get("code"),
            "web_name": el.get("web_name"),
            "position": POSITIONS.get(el.get("element_type")),
            "team": teams.get(el.get("team")),
            "status": el.get("status"),           # a=available, i=injured, ...
            "news": el.get("news") or "",
        }
    return index


def _projection_by_code(projections_path: Path) -> dict[int, dict[str, Any]]:
    """code -> projection fields, if the projections file exists (else empty)."""
    if not Path(projections_path).exists():
        logger.warning("projections not found at %s; FA report will be unranked", projections_path)
        return {}
    rows = json.loads(Path(projections_path).read_text(encoding="utf-8"))
    return {int(r["code"]): r for r in rows}


def free_agent_report(
    latest: dict[int, int | None],
    bootstrap: dict,
    projections_path: Path,
    top_n: int = 15,
) -> list[dict[str, Any]]:
    """Free agents ranked by projected next-season points (per position mixed).

    Players without a projection (e.g. promoted-team players) rank last but are
    kept — availability flags and names still matter for the human scan.
    """
    players = _player_index(bootstrap)
    projections = _projection_by_code(projections_path)
    report = []
    for element, owner in latest.items():
        if owner is not None:
            continue
        info = players.get(element)
        if info is None:
            continue
        if info["status"] == "u":
            continue  # unavailable: left the league (transfer out) — not a real FA
        projection = projections.get(info["code"], {})
        report.append({
            "element": element,
            "web_name": info["web_name"],
            "position": info["position"],
            "team": info["team"],
            "availability": info["status"],
            "news": info["news"],
            "projected_points": projection.get("projected_points"),
            "tier": projection.get("tier"),
            "confidence": projection.get("confidence"),
        })
    report.sort(key=lambda r: (r["projected_points"] is None,
                               -(r["projected_points"] or 0)))
    return report[:top_n]


def annotate_events(events: list[dict[str, Any]], bootstrap: dict) -> list[dict[str, Any]]:
    """Attach player identity to raw events for human-readable output."""
    players = _player_index(bootstrap)
    out = []
    for event in events:
        info = players.get(event["element"], {})
        out.append({**event,
                    "web_name": info.get("web_name"),
                    "position": info.get("position"),
                    "team": info.get("team")})
    return out


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(_repo_root() / ".env")
    parser = argparse.ArgumentParser(description="Drop-radar: ownership events + free-agent report")
    parser.add_argument("--season", default="2026-27")
    parser.add_argument("--league", type=int, default=int(os.getenv("LEAGUE_ID", "0") or "0"))
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--raw-root", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if not args.league:
        parser.error("--league required (or set LEAGUE_ID)")

    raw_root = args.raw_root or _repo_root() / "data/raw" / args.season
    history_dir = raw_root / f"league/{args.league}/element_status_history"
    bootstrap = json.loads((raw_root / "bootstrap/bootstrap-static.json").read_text(encoding="utf-8"))
    projections_path = _repo_root() / "data/derived/ml/projections_2627.json"

    snapshots = load_snapshots(history_dir)
    if not snapshots:
        logger.error("no snapshots in %s — run the fetcher first", history_dir)
        return 1

    events = annotate_events(build_events(snapshots), bootstrap)
    out_path = args.out or _repo_root() / "data/derived" / args.season / "ml/ownership_events.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(events, indent=1))

    print(f"\nsnapshots: {len(snapshots)} (latest {snapshots[-1][0]}) | events: {len(events)}")
    recent = [e for e in events if e["ts"] == snapshots[-1][0]]
    if recent:
        print("\n== ownership changes in the latest snapshot ==")
        for e in recent:
            print(f"  {e['kind']:<5} {e['web_name']} ({e['position']}, {e['team']}) "
                  f"{e['from_owner']} -> {e['to_owner']}")

    print(f"\n== top {args.top} FREE AGENTS by projection ==")
    for r in free_agent_report(snapshots[-1][1], bootstrap, projections_path, args.top):
        pts = f"{r['projected_points']:.0f} pts" if r["projected_points"] is not None else "no projection"
        flag = f"  [{r['availability']}] {r['news']}" if r["availability"] != "a" else ""
        print(f"  {r['position']:<4} {r['web_name']:<20} {r['team']:<4} {pts:<14} "
              f"tier={r['tier'] or '-'} {r['confidence'] or ''}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
