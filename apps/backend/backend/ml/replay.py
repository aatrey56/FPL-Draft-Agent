"""Waiver replay harness v0 — what would waiver_plan have done at each deadline?

For each waiver deadline of GW N (default GW2-5 of 2026-27) the harness
rebuilds the inputs as they stood at ``T_N = events[N].waivers_time``, asks
``waiver.plan()`` for its rank-1 add/drop, scores it against realized GW
points, and compares with four other rows: ``no_change``, ``std_points``,
``form3`` and ``me`` (my real accepted moves from transactions.json).

As-of contract (nothing after T_N may influence a recommendation):

* Ownership: the last ``element_status_history`` snapshot with ts <= T_N.
  Free pool = no owner and status "a" ("l" = locked new signing, excluded).
  Elements absent from that snapshot do not exist. Warns if the snapshot is
  more than 24h older than T_N.
* Fixtures: schedule for GW N..N+2 from ``gw/<k>/live.json["fixtures"]`` (or
  the bootstrap fixtures for events it holds, which are 6-8 only). The
  schedule is known in advance, so this is not leakage.
* Completed gameweeks: only gameweeks fully completed by T_N feed the
  as-of data (model panel, baseline history, departed-rule minutes). GW k
  counts as completed when its last fixture's ``kickoff_time`` + 2.5h <= T_N
  (``RESULT_LAG``: 90 min + stoppage + the results/bonus feed), with the
  fixtures read from ``gw/<k>/live.json`` or else the bootstrap fixtures.
  Usually that is every GW < N, but in a congested midweek GW N's waivers
  can close before GW N-1's last match, and then N-1 is excluded. A GW with
  no kickoff times at all falls back to the old ``k < N`` rule with a
  WARNING.
* Player stats: ``gw/<k>/live.json`` for completed k only, and only by the
  baselines. Realized GW N..N+2 points are read after the picks are made.
* Projections / ``player_seasons.parquet`` are the pre-GW1 artifacts; the
  harness refuses to run if the season table has 2026-27 rows.
* Bootstrap supplies identity only (id, code, web_name, element_type, team).
  Current status/news/chance_of_playing exist only for "now", so replay
  neutralises them (status "a", availability 1.0), recorded as
  ``availability_mode: "neutral+departed"``. One exception: a player whose
  CURRENT bootstrap status is "u" (departed) AND who had 0 minutes in every
  GW < N stays unavailable, as in the live tool. That uses today's status, a
  small deliberate leak, far smaller than recommending departed players
  (a "u" player with minutes before N is kept eligible: he left later).
  ``team`` is a tiny accepted leak. ``form``/``total_points``/``ep_*`` are
  never read.
* transactions.json is used for scoring only (contested claims, my moves).

Scoring: each strategy yields one (add, drop) pair per GW, same position.
``gw_gain = pts(add, N) - pts(drop, N)``; ``gw3_gain`` is the same summed over
N..N+2 when all three live files exist, else null. Missing/DNP = 0 points.
Raw player points: no lineup, bench or auto-sub simulation.

Contested claims: a candidate is lost if another entry has an accepted
waiver claim (kind "w", result "a") for it in event N; the next ranked
candidate (max 10) is used instead (``won`` = fallback), none left = no
change (``won`` = n, ``exhausted``). Pessimistic by design: ignores that I
might have out-ranked the winner. The ``me`` row is not subject to this: it
sums my accepted ``w`` claims and ``f`` free-agent moves in event N (``f``
moves happen after the waiver run and before the next deadline) and reports
the ``moves`` count.

Caveats: n=4 deadlines, one league, no waiver-order simulation. This proves
the harness runs; it is not evidence about the model.

Scorers (``--scorer``): ``heuristic`` ranks on the ros/38 next-GW value;
``model`` builds the match xP for deadline N from ``archive panel UNION season
panel[gw completed by T_N]`` (Stage 1 sees the same neutral availability as the
heuristic) and passes it to ``waiver.plan`` as ``gw_xp``. A leak guard raises if
the as-of season panel holds any gameweek not completed by T_N. The other four strategies do not
depend on the scorer, so their rows are identical between scorers.

Role signals: ``model`` also passes the as-of season panel (the gameweeks
completed by T_N) to
``waiver.plan`` so club-movers are valued on the minutes they had played by
the deadline (``club_moved`` itself reads today's club — the same accepted
``team`` leak as above). ``heuristic`` deliberately does NOT: it is the
pre-xP baseline the model is compared against, so it runs without a season
panel (every role factor 1.0). It is not strictly frozen, though: the
departed rule above (status "u" today + 0 minutes before N) leaves such a
player status "u" in the neutral bootstrap, and ``waiver``'s drop pick
always drops a status-"u" squad player first at his position — in BOTH
scorers. On 2026-27 GW2-5 the heuristic rank-1 rows and totals are
unchanged by it (waiver_plan -15.0). ``role_overrides.json`` is never
applied in replay — it is hand-written knowledge as of today, not as of the
deadline.

CLI (reads only; writes ``derived/<season>/ml/waiver_replay_<scorer>.json``):

    uv run python -m backend.ml.replay --season 2026-27 --gws 2-5 \\
        [--scorer {heuristic,model}] [--league ID --entry ID] [--data-root PATH]

League/entry fall back to LEAGUE_ID / ENTRY_ID (repo .env, then the .env next
to ``--data-root``); since load_dotenv does not override values already set,
the repo .env takes precedence over ``<data-root>/../.env``. Output rows carry web names and "me"/"other" only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

from backend.ml import jsonutil
from backend.ml import matchmodel
from backend.ml import waiver as wv

logger = logging.getLogger(__name__)

STRATEGIES = ("no_change", "std_points", "form3", "waiver_plan", "me")
MAX_CANDIDATES = 10
STALE_SNAPSHOT_HOURS = 24
# A gameweek's outcomes count as known this long after its last kickoff.
RESULT_LAG = timedelta(hours=2.5)
AVAILABILITY_MODE = "neutral+departed"
CAVEAT = ("Caveat: n=4 deadlines, one league, availability neutral+departed (today's "
          "status 'u' with no prior minutes stays out), raw player points. Harness "
          "check only - not model evidence.")
_IDENTITY_FIELDS = ("id", "code", "web_name", "element_type", "team")
_STEM_FMT = "%Y%m%dT%H%M"


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# As-of inputs
# ---------------------------------------------------------------------------

def asof_snapshot(history_dir: Path, cutoff_iso: str) -> tuple[str, dict]:
    """Load the last element-status snapshot with ts <= cutoff (stem compare).

    Only that one file is read. Raises if no snapshot exists at or before the
    cutoff; warns if it is more than 24h older than the cutoff.
    """
    cutoff = _parse_iso(cutoff_iso)
    key = cutoff.strftime(_STEM_FMT)
    stems = sorted(p.stem for p in Path(history_dir).glob("*.json") if p.stem[:13] <= key)
    if not stems:
        raise ValueError(f"no element-status snapshot at or before {cutoff_iso}")
    stem = stems[-1]
    age = cutoff - datetime.strptime(stem[:13], _STEM_FMT).replace(tzinfo=timezone.utc)
    if age.total_seconds() > STALE_SNAPSHOT_HOURS * 3600:
        logger.warning("snapshot %s is %.0fh older than %s", stem, age.total_seconds() / 3600, cutoff_iso)
    data = json.loads((Path(history_dir) / f"{stem}.json").read_text(encoding="utf-8"))
    return stem, data


def read_live(raw_root: Path, event: int) -> dict | None:
    """gw/<event>/live.json, or None if that gameweek has not been fetched."""
    path = Path(raw_root) / "gw" / str(event) / "live.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def asof_fixtures(bootstrap: dict, raw_root: Path, event: int,
                  n_events: int = wv.NEXT_GWS) -> dict[str, list[dict]]:
    """Schedule for events event..event+n-1 as ``{"<k>": [{team_h, team_a}]}``.

    live.json fixtures when that gameweek file exists, else the bootstrap
    fixtures for that event (it holds only later events); else omitted. Only
    team ids are copied — no scores or stats.
    """
    out: dict[str, list[dict]] = {}
    for k in range(event, event + n_events):
        live = read_live(raw_root, k)
        fixtures = live.get("fixtures") if live else None
        if fixtures is None:
            fixtures = (bootstrap.get("fixtures") or {}).get(str(k))
        if fixtures is not None:
            out[str(k)] = [{"team_h": f.get("team_h"), "team_a": f.get("team_a")} for f in fixtures]
    return out


def last_kickoff(bootstrap: dict, raw_root: Path, event: int) -> datetime | None:
    """Latest fixture kickoff of ``event``, or None when no time is known.

    Fixtures come from ``gw/<event>/live.json`` when fetched, else from the
    bootstrap fixtures for that event. Fixtures without a ``kickoff_time``
    (unscheduled) are ignored.
    """
    live = read_live(raw_root, event)
    fixtures = (live or {}).get("fixtures") or (bootstrap.get("fixtures") or {}).get(str(event)) or []
    kickoffs = [datetime.fromisoformat(f["kickoff_time"].replace("Z", "+00:00"))
                for f in fixtures if f.get("kickoff_time")]
    return max(kickoffs, default=None)


def completed_gameweeks(bootstrap: dict, raw_root: Path, event: int) -> set[int]:
    """Gameweeks before ``event`` fully completed by its waiver cutoff.

    GW k < event is completed when its last kickoff + ``RESULT_LAG`` <=
    ``events[event].waivers_time``. A GW with no known kickoff time is
    assumed completed (the pre-cutoff ``k < event`` rule) with a WARNING.
    """
    events = {e["id"]: e for e in bootstrap["events"]["data"]}
    cutoff = _parse_iso(events[event]["waivers_time"])
    completed: set[int] = set()
    for k in range(1, event):
        last = last_kickoff(bootstrap, raw_root, k)
        if last is None:
            logger.warning("GW%d has no fixture kickoff times; assumed completed before "
                           "the GW%d waiver cutoff", k, event)
            completed.add(k)
        elif last + RESULT_LAG <= cutoff:
            completed.add(k)
        else:
            logger.warning("GW%d's last kickoff %s + %s is after the GW%d waiver cutoff %s: "
                           "excluded from the as-of data", k, last.isoformat(), RESULT_LAG,
                           event, cutoff.isoformat())
    return completed


def asof_panel(archive: pd.DataFrame, season_panel: pd.DataFrame,
               completed: set[int]) -> pd.DataFrame:
    """Training panel as of a deadline: archive + season rows of the
    ``completed`` gameweeks (``completed_gameweeks``)."""
    return pd.concat([archive, season_panel[season_panel["gw"].isin(completed)]],
                     ignore_index=True)


def asof_season_panel(season_panel: pd.DataFrame, season: str,
                      completed: set[int]) -> pd.DataFrame:
    """``season`` rows of the gameweeks completed by a deadline
    (``completed_gameweeks``): the minutes the role signals may see as of it."""
    return season_panel[(season_panel["season"] == season)
                        & season_panel["gw"].isin(completed)]


def assert_no_panel_leak(panel: pd.DataFrame, season: str, event: int,
                         completed: set[int]) -> None:
    """Raise if the panel holds any ``season`` gameweek not completed by the
    deadline of ``event`` (future data)."""
    in_season = set(int(g) for g in panel.loc[panel["season"] == season, "gw"].unique())
    leaked = sorted(in_season - set(completed))
    if leaked:
        raise ValueError(f"as-of panel for {season} holds gw {leaked} not completed by "
                         f"the gw {event} waiver cutoff (leak)")


def model_gw_xp(archive: pd.DataFrame, season_panel: pd.DataFrame, neutral: dict,
                fixtures: dict[str, list[dict]], season: str, event: int,
                completed: set[int]) -> pd.DataFrame:
    """Match xP for the deadline of ``event`` using only data before it.

    ``neutral`` is the neutralised bootstrap (so Stage 1 sees neutral
    availability exactly like the heuristic); ``fixtures`` is the as-of
    schedule from ``asof_fixtures``, of which only ``event`` is scored;
    ``completed`` is ``completed_gameweeks`` for ``event``.
    """
    panel = asof_panel(archive, season_panel, completed)
    assert_no_panel_leak(panel, season, event, completed)
    bootstrap = {**neutral, "fixtures": {str(event): fixtures[str(event)]}}
    return matchmodel.build_gw_xp(panel, bootstrap, event, season)


def departed_before(bootstrap: dict, raw_root: Path, event: int,
                    completed: set[int] | None = None) -> set[int]:
    """Elements with current status "u" and 0 minutes in every GW < event.

    Uses today's status (deliberate small leak, see module docstring); the
    minutes come from live.json for the ``completed`` gameweeks (default
    GW1..event-1). A "u" player with any prior minutes left later and stays
    eligible.
    """
    played: set[int] = set()
    for k in sorted(completed) if completed is not None else range(1, event):
        minutes = gw_points(raw_root, k, "minutes") or {}
        played.update(e for e, m in minutes.items() if m > 0)
    return {el["id"] for el in bootstrap.get("elements", [])
            if el.get("status") == "u" and el["id"] not in played}


def neutralize_bootstrap(bootstrap: dict, departed: set[int] | frozenset[int] = frozenset()) -> dict:
    """Identity-only copy of the bootstrap with availability neutralised.

    Every element becomes status "a", no news, no chance_of_playing, except
    ``departed`` ids which stay status "u" (never recommended). Fields such as
    form/total_points/ep_* are dropped; the (future) fixtures key is omitted
    so it cannot be used by accident.
    """
    elements = []
    for el in bootstrap.get("elements", []):
        slim = {k: el.get(k) for k in _IDENTITY_FIELDS}
        slim.update({"status": "u" if el["id"] in departed else "a", "news": "",
                     "chance_of_playing_next_round": None,
                     "chance_of_playing_this_round": None})
        elements.append(slim)
    return {"teams": bootstrap.get("teams", []), "elements": elements}


def gw_points(raw_root: Path, event: int, stat: str = "total_points") -> dict[int, int] | None:
    """element id -> ``stat`` for one gameweek, or None if not fetched."""
    live = read_live(raw_root, event)
    if live is None:
        return None
    return {int(e): int((v.get("stats") or {}).get(stat) or 0)
            for e, v in (live.get("elements") or {}).items()}


def _pts(points: dict[int, int] | None, element: int) -> int:
    """Points for an element; missing gameweek / DNP / unknown element = 0."""
    return int((points or {}).get(element, 0))


# ---------------------------------------------------------------------------
# Picks and scoring
# ---------------------------------------------------------------------------

def baseline_pick(strategy: str, event: int, free: list[int], squad: list[int],
                  elements: dict[int, dict], history: dict[int, dict[int, int]]
                  ) -> list[tuple[int, int]]:
    """Ranked (add, drop) pairs for ``std_points`` / ``form3``.

    ``history`` maps gameweek -> {element: points} and must only contain
    gameweeks before ``event``. Metric: summed points over GW1..N-1
    (``std_points``) or mean points over the last min(3, N-1) GWs
    (``form3``). Each add is paired with the weakest same-position squad
    player by the same metric; ties go to the lower element id.
    """
    if any(k >= event for k in history):
        raise ValueError("baseline history includes a gameweek >= the deadline (leak)")
    if strategy == "std_points":
        window = sorted(history)
        divisor = 1
    elif strategy == "form3":
        window = sorted(history)[-min(3, event - 1):]
        divisor = max(len(window), 1)
    else:
        raise ValueError(f"unknown baseline {strategy!r}")

    def metric(element: int) -> float:
        return sum(_pts(history[k], element) for k in window) / divisor

    ranked = sorted(free, key=lambda e: (-metric(e), e))
    pairs = []
    for add in ranked:
        same_pos = [s for s in squad if elements[s]["element_type"] == elements[add]["element_type"]]
        if not same_pos:
            continue
        pairs.append((add, min(same_pos, key=lambda s: (metric(s), s))))
        if len(pairs) == MAX_CANDIDATES:
            break
    return pairs


def is_contested(transactions: list[dict], event: int, element: int, me: int) -> bool:
    """True if another entry has an accepted waiver claim for element in event."""
    return any(t.get("kind") == "w" and t.get("result") == "a" and t.get("event") == event
               and t.get("element_in") == element and t.get("entry") != me
               for t in transactions)


def select_pick(candidates: list[tuple[int, int]], lost: set[int]
                ) -> tuple[tuple[int, int] | None, str, list[int]]:
    """First candidate not lost -> (pair, won, skipped add ids).

    ``won`` is y (rank 1 kept) / fallback (a lower rank used) / n (all lost).
    No candidates at all gives (None, "-", []): there was nothing to contest.
    """
    if not candidates:
        return None, "-", []
    skipped: list[int] = []
    for pair in candidates:
        if pair[0] not in lost:
            return pair, "fallback" if skipped else "y", skipped
        skipped.append(pair[0])
    return None, "n", skipped


def score_pick(add: int | None, drop: int | None, event: int,
               live_points: dict[int, dict[int, int] | None]) -> dict[str, Any]:
    """gw_gain / gw3_gain for one (add, drop) pair; no pair = zero gain.

    ``live_points`` maps GW -> {element: points} (None = file missing). The
    3-GW gain needs all of N..N+2 present, otherwise it is null.
    """
    horizon = range(event, event + 3)
    full = all(live_points.get(k) is not None for k in horizon)
    if add is None or drop is None:
        return {"add_pts": None, "drop_pts": None, "gw_gain": 0.0,
                "gw3_gain": 0.0 if full else None}
    gw_gain = _pts(live_points.get(event), add) - _pts(live_points.get(event), drop)
    gw3_gain = float(sum(_pts(live_points.get(k), add) - _pts(live_points.get(k), drop)
                         for k in horizon)) if full else None
    return {"add_pts": _pts(live_points.get(event), add),
            "drop_pts": _pts(live_points.get(event), drop),
            "gw_gain": float(gw_gain), "gw3_gain": gw3_gain}


def _row(event: int, strategy: str, elements: dict[int, dict], pair: tuple[int, int] | None,
         won: str, scored: dict, minutes: dict[int, int] | None, lost_names: list[str]) -> dict:
    add = elements[pair[0]] if pair else None
    drop = elements[pair[1]] if pair else None
    return {
        "gw": event, "strategy": strategy,
        "add": add["web_name"] if add else "-",
        "position": wv.POSITIONS[add["element_type"]] if add else "-",
        "drop": drop["web_name"] if drop else "-",
        "won": won, "exhausted": won == "n",
        "lost_to": [{"add": name, "by": "other"} for name in lost_names],
        "add_minutes": _pts(minutes, pair[0]) if pair else None,
        "moves": 1 if pair else 0,
        **scored,
    }


def my_moves(transactions: list[dict], event: int, me: int) -> list[tuple[int, int]]:
    """My accepted (in, out) moves in event: waiver claims and free-agent adds."""
    return [(t["element_in"], t["element_out"]) for t in transactions
            if t.get("entry") == me and t.get("event") == event
            and t.get("kind") in ("w", "f") and t.get("result") == "a"]


def score_actual(moves: list[tuple[int, int]], event: int, elements: dict[int, dict],
                 live_points: dict[int, dict[int, int] | None],
                 minutes: dict[int, int] | None) -> dict:
    """Row for what I really did: per-move gains summed; none = 0, add "-"."""
    scored = [score_pick(a, d, event, live_points) for a, d in moves]
    gw3 = [s["gw3_gain"] for s in scored] or [score_pick(None, None, event, live_points)["gw3_gain"]]
    name = lambda e: elements.get(e, {}).get("web_name", "?")  # noqa: E731
    positions = [wv.POSITIONS.get(elements.get(a, {}).get("element_type"), "?") for a, _ in moves]
    return {
        "gw": event, "strategy": "me",
        "add": "+".join(name(a) for a, _ in moves) or "-",
        "position": "/".join(positions) or "-",
        "drop": "+".join(name(d) for _, d in moves) or "-",
        "won": "y" if moves else "-", "exhausted": False, "lost_to": [],
        "add_minutes": sum(_pts(minutes, a) for a, _ in moves) if moves else None,
        "moves": len(moves),
        "add_pts": sum(s["add_pts"] for s in scored) if moves else None,
        "drop_pts": sum(s["drop_pts"] for s in scored) if moves else None,
        "gw_gain": float(sum(s["gw_gain"] for s in scored)),
        "gw3_gain": float(sum(gw3)) if all(g is not None for g in gw3) else None,
    }


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def _load_inputs(data_root: Path, season: str, league: int) -> dict:
    raw = Path(data_root) / "raw" / season
    league_dir = raw / "league" / str(league)
    seasons = pd.read_parquet(Path(data_root) / "derived/ml/player_seasons.parquet")
    if (seasons["season"] == season).any():
        raise ValueError(f"player_seasons already contains {season}: not a pre-season table")
    return {
        "raw": raw,
        "bootstrap": json.loads((raw / "bootstrap/bootstrap-static.json").read_text(encoding="utf-8")),
        "history_dir": league_dir / "element_status_history",
        "transactions": json.loads(
            (league_dir / "transactions.json").read_text(encoding="utf-8")).get("transactions", []),
        "seasons": seasons,
        "projections": Path(data_root) / "derived/ml/projections_2627.json",
    }


def run(data_root: Path, season: str, league: int, entry: int, gws: list[int],
        scorer: str = "heuristic") -> dict:
    """Replay each deadline in gws and return the full result document.

    ``scorer`` selects how ``waiver_plan`` values the next GW: ``heuristic``
    (ros/38 x fixtures) or ``model`` (match xP built as of each deadline).
    """
    if scorer not in wv.SCORERS:
        raise ValueError(f"unknown scorer {scorer!r}")
    inputs = _load_inputs(data_root, season, league)
    if scorer == "model":
        archive = pd.read_parquet(Path(data_root) / "derived/ml/player_gameweeks.parquet")
        season_panel = pd.read_parquet(
            Path(data_root) / "derived" / season / "ml/player_gameweeks.parquet")
    raw, bootstrap = inputs["raw"], inputs["bootstrap"]
    events = {e["id"]: e for e in bootstrap["events"]["data"]}
    elements = {el["id"]: el for el in neutralize_bootstrap(bootstrap)["elements"]}
    transactions = inputs["transactions"]
    rows: list[dict] = []
    for event in gws:
        if event < 2:
            raise ValueError("replay needs GW >= 2 (GW1 has no prior gameweek)")
        completed = completed_gameweeks(bootstrap, raw, event)
        departed = departed_before(bootstrap, raw, event, completed)
        neutral = neutralize_bootstrap(bootstrap, departed)
        _, snap = asof_snapshot(inputs["history_dir"], events[event]["waivers_time"])
        status_rows = [r for r in snap.get("element_status", []) if int(r["element"]) in elements]
        # Locked (status "l") and other non-"a" unowned elements are not claimable.
        status_rows = [r for r in status_rows if r.get("owner") is not None or r.get("status") == "a"]
        free = sorted(int(r["element"]) for r in status_rows
                      if r.get("owner") is None and int(r["element"]) not in departed)
        squad = sorted(int(r["element"]) for r in status_rows if r.get("owner") == entry)

        fixtures = asof_fixtures(bootstrap, raw, event)
        gw_xp, minutes_panel = None, None
        if scorer == "model":
            gw_xp = model_gw_xp(archive, season_panel, neutral, fixtures, season, event,
                                completed)
            minutes_panel = asof_season_panel(season_panel, season, completed)
            assert_no_panel_leak(minutes_panel, season, event, completed)
        result = wv.plan(neutral, {"element_status": status_rows}, inputs["seasons"],
                         inputs["projections"], entry, MAX_CANDIDATES,
                         fixtures_by_event=fixtures,
                         neutral_availability=True, gw_xp=gw_xp,
                         season_panel=minutes_panel)
        candidates = {
            "no_change": [],
            "waiver_plan": [(r["add_element"], r["drop_element"]) for r in result["recommendations"]],
        }
        history = {k: pts for k in sorted(completed) if (pts := gw_points(raw, k)) is not None}
        for name in ("std_points", "form3"):
            candidates[name] = baseline_pick(name, event, free, squad, elements, history)

        lost = {add for pairs in candidates.values() for add, _ in pairs
                if is_contested(transactions, event, add, entry)}
        live_points = {k: gw_points(raw, k) for k in range(event, event + 3)}
        minutes = gw_points(raw, event, "minutes")
        for name in STRATEGIES[:-1]:
            pair, won, skipped = select_pick(candidates[name], lost)
            scored = score_pick(pair[0] if pair else None, pair[1] if pair else None,
                                event, live_points)
            rows.append(_row(event, name, elements, pair, won, scored, minutes,
                             [elements[a]["web_name"] for a in skipped]))
        rows.append(score_actual(my_moves(transactions, event, entry), event, elements,
                                 live_points, minutes))
    return {"season": season, "gws": gws, "scorer": scorer,
            "availability_mode": AVAILABILITY_MODE, "caveat": CAVEAT, "totals": _totals(rows), "rows": rows}


def _totals(rows: list[dict]) -> dict[str, dict[str, float]]:
    """Per-strategy sums; gw3 only over rows where it exists."""
    return {s: {"gw_gain": sum(r["gw_gain"] for r in rows if r["strategy"] == s),
                "gw3_gain": sum(r["gw3_gain"] for r in rows
                                if r["strategy"] == s and r["gw3_gain"] is not None)}
            for s in STRATEGIES}


# ---------------------------------------------------------------------------
# Output / CLI
# ---------------------------------------------------------------------------

def _fmt_gain(value: float | None) -> str:
    return "-" if value is None else f"{value:+.1f}"


def format_table(doc: dict) -> str:
    """Fixed-width table + totals + caveat for stdout."""
    lines = [f"{'GW':<3}{'strategy':<12}{'add (pos)':<28}{'drop':<22}{'won':<9}"
             f"{'moves':<6}{'add_pts':<8}{'gw_gain':<8}gw3_gain"]
    for r in doc["rows"]:
        add, drop = r["add"], r["drop"]
        if r["moves"] > 1:  # names stay in the JSON; the table stays aligned
            add, drop = f"({r['moves']} moves)", "-"
        elif add != "-":
            add = f"{add} ({r['position']})"
        add_pts = "-" if r["add_pts"] is None else str(r["add_pts"])
        lines.append(f"{r['gw']:<3}{r['strategy']:<12}{add:<28}{drop:<22}{r['won']:<9}"
                     f"{r['moves']:<6}{add_pts:<8}{_fmt_gain(r['gw_gain']):<8}{_fmt_gain(r['gw3_gain'])}")
    totals = doc["totals"]
    lines.append(f"scorer: {doc['scorer']}")
    lines.append("TOTAL gw_gain: " + " | ".join(
        f"{s} {totals[s]['gw_gain']:+.1f}" for s in STRATEGIES))
    lines.append("TOTAL gw3_gain (rows with all 3 GWs only): " + " | ".join(
        f"{s} {totals[s]['gw3_gain']:+.1f}" for s in STRATEGIES))
    lines.append("Note: form3 == std_points at GW2 (only one prior gameweek).")
    lines.append(doc["caveat"])
    return "\n".join(lines)


def _parse_gws(text: str) -> list[int]:
    start, _, end = text.partition("-")
    return list(range(int(start), int(end or start) + 1))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(_repo_root() / ".env")
    parser = argparse.ArgumentParser(description="Replay waiver_plan at past waiver deadlines")
    parser.add_argument("--season", default="2026-27")
    parser.add_argument("--gws", default="2-5", help="GW or range, e.g. 2-5")
    parser.add_argument("--league", type=int, default=None)
    parser.add_argument("--entry", type=int, default=None)
    parser.add_argument("--scorer", choices=wv.SCORERS, default=wv.DEFAULT_SCORER)
    parser.add_argument("--data-root", type=Path, default=_repo_root() / "data")
    args = parser.parse_args(argv)
    load_dotenv(args.data_root.parent / ".env")
    league = args.league or int(os.getenv("LEAGUE_ID", "0") or "0")
    entry = args.entry or int(os.getenv("ENTRY_ID", "0") or "0")
    if not league or not entry:
        parser.error("--league and --entry required (or set LEAGUE_ID / ENTRY_ID)")

    doc = run(args.data_root, args.season, league, entry, _parse_gws(args.gws), args.scorer)
    print(format_table(doc))
    out = args.data_root / "derived" / args.season / "ml" / f"waiver_replay_{args.scorer}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    try:
        tmp.write_text(jsonutil.dumps_strict(
            {k: doc[k] for k in ("season", "gws", "scorer", "availability_mode", "caveat", "rows")}, indent=1))
        os.replace(tmp, out)  # atomic: readers never see a partial file
    finally:
        tmp.unlink(missing_ok=True)  # no-op after a successful replace
    logger.info("wrote %s", out.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
