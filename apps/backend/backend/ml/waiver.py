"""waiver_plan — roster-aware add/drop recommendations.

Answers the weekly question: *given MY squad and MY league's free agents, who
do I add, who do I drop, and is the move a short-term stream or a season-long
upgrade?* Reads local files only (bootstrap, element-status snapshot,
projections, the 25/26 season table) — no live API calls.

Design (v1, pre-GW1-honest):

* **Per-GW baseline** = season projection / 38 (the measured-vs-baseline model
  from ``projection.py``). Early season there is no current form to use; as the
  match xP model (MATCH_MODEL_SPEC) lands, it replaces this baseline.
* **Fixture adjustment** — opponent strength from last season's per-team player
  points totals (promoted teams get a weak prior: 90% of the minimum), mapped
  to a multiplier in [0.85, 1.15], with a small home/away nudge. Summing per
  event handles blanks (0 fixtures) and doubles (2) naturally.
* **Availability gate** — live status/news from bootstrap: available=1.0,
  doubtful=chance/100, injured/suspended=chance if stated else 0, departed=0.
  A cached model xP row is reconciled against it: a player whose CURRENT
  availability is 0 gets ``xp_next``/``p_start`` 0 and ``xp_reconciled``
  True, even if the file was built (or a stale file served) before the news.
* **Next-GW xP** (``xp_next``) comes from the match model's
  ``xp_gw{N}.parquet`` when ``--scorer model`` and the file is fresh; players
  the model does not cover (blank GW, no row, or a position the panel was too
  thin to fit — a row with a NaN ``p_start``) fall back to the heuristic
  ``ros/38 x fixture load x availability``. ``xp_source`` (model / heuristic /
  none) is carried on every player and recommendation. Scale caveat: model xP
  and the heuristic fallback share one ranking; the heuristic is on a lower
  scale (``ros/38`` averages below the model's per-start expectation), so a
  heuristic-valued drop makes model-valued adds look better than they are.
* **Three horizons, always shown together**: every candidate carries
  ``gains = {gw1, gw3, ros}`` — next-GW xP, 3-GW xP and rest-of-season
  (``ros_adj``), each the add minus the drop candidate — and the same values
  as the flat ``next1_gain`` / ``next3_gain`` / ``season_gain``. With the
  model scorer the 3-GW value is the match model's
  ``xp_horizon_gw{N}.parquet`` sum (``matchmodel.build_horizon_xp``: GW
  N..N+2, features frozen at N — a schedule view) for every player it covers
  and the heuristic ``ros/38 x 3-event fixture load x availability`` for the
  rest; ``add_next3_source`` (model / heuristic / none) says which.
* **``--horizon {1,3,ros}`` picks the ranking key** (default ``3``) under
  the model scorer. Labels read the short horizon against the season:
  ``upgrade`` (short and season gain positive — add and hold), ``stream``
  (short only — plan to re-drop), ``hold`` (season only — patience play).
  ``1`` ranks by ``gw1`` with ``gw1`` as the short horizon; ``3`` ranks by
  ``gw3`` with ``gw3`` as the short horizon; in both, holds come after every
  positive-short rec. ``ros`` ranks by the season gain (labels as ``3``,
  holds not demoted). ``rank_by`` in the output records the ranking used
  (``next1`` / ``next3`` / ``ros`` / ``legacy``). If the horizon file is
  missing, unreadable, stale or disagrees with ``xp_gw{N}``
  (``horizon_fallback`` + ``horizon_fallback_reason``), 3-GW values are
  heuristic and ``--horizon 3`` ranks on the next GW instead (``rank_key``).
  A free agent with no ROS projection but a model value (e.g. a promoted
  club's starter) is still ranked, with ``season_gain`` and ``add_ros`` null
  and ``season_unknown`` true; his season gain counts as 0 for the label and
  ordering, so he is at most a ``stream``. A gain whose add has no value at
  that horizon is null (unknown, never "0 minus the drop"). With no match xP
  supplied (the heuristic scorer, requested or after a fallback)
  ``recommend`` keeps its original ranking whatever ``--horizon`` says
  (``rank_by="legacy"``: every value heuristic, labels from ``next3_gain``,
  ordered by the larger gain) so the heuristic output is unchanged.
* **Role signals** — a season projection only knows last season's role at
  last season's club, so a transfer silently breaks it. ``club_moved`` =
  bootstrap club != the prior-season club in ``player_seasons`` (None when
  there is no prior row: new to the league). ``expected_minutes`` = mean
  minutes over the last ``ROLE_MINUTES_WINDOW`` finished gameweeks of the
  season panel (a missing row is 0 minutes; None before any gameweek is
  finished), ``minutes_season`` = their sum. For club-movers ONLY,
  ``ros_adj = ros_points x role_factor`` with ``role_factor =
  clip(expected_minutes / ROLE_MINUTES_FULL, ROLE_FLOOR, 1.0)``; everyone
  else — and any club-mover whose status is not "a" (injured, doubtful,
  suspended: the absence explains the minutes) — has factor 1.0. ``season_gain``, the drop pick and the heuristic
  per-GW baseline (``ros_adj / 38``) use ``ros_adj``; ``ros_points`` stays in
  the output. A general "benched at his old club too" factor is Phase B.
* **The drop pick** (``drop_order``) follows the ranking horizon under the
  model scorer: sort key ``(status != "u", value, ros_adj)`` where value is
  ``xp_next`` (``--horizon 1``), ``next3_xp`` (``3``) or ``ros_adj``
  (``ros``, tie-break ``next3_xp``). A missing value sorts last, so ROS only
  breaks ties or orders players the horizon cannot value. Any squad player
  with a model value (next-GW or 3-GW) OR a ROS projection is droppable — an injured 0-xP
  player is the obvious drop, not an unknown. A player with neither stays in
  ``unprojected_squad`` and is never auto-dropped. A drop without a ROS
  projection makes the rec's season gain unknown (``season_unknown``).
  The heuristic scorer (``legacy``) keeps its frozen pick: ROS-projected
  players only, key ``(status != "u", ros_adj, next3_xp)``.
* **Departed squad players** (status ``u``) have ``ros_adj`` 0 and are
  always the drop at their position, with or without a projection.
  ``drop_candidates`` lists the top ``DROP_CANDIDATES_PER_POSITION`` per
  position so the pick is auditable.
* **Diversified top N** (model scorer): ``recommendations`` is the overall
  ranking with at most ``MAX_RECS_PER_DROP`` recs per drop player — a rec
  past the cap is held back and only used to backfill (in rank order, after
  the capped picks) when fewer than ``top_n`` recs remain. Rank 1 never
  changes. The heuristic scorer is not diversified (frozen baseline).
  ``best_by_position`` lists the best ``BEST_BY_POSITION_N`` swaps per
  position (each against that position's drop pick), undiversified, for
  every scorer, so a run of MID swaps cannot hide the best DEF/FWD move.
* **Role overrides** — ``data/derived/<season>/ml/role_overrides.json``
  (hand-maintained team news, optional) is applied to the next GW, and to
  the model's 3-GW value event by event: an undated entry touches GW N only,
  ``valid_through_gw`` / ``return_gw`` extend it over the later events they
  cover (see ``apply_role_overrides``). An entry with neither ``return_gw`` nor
  ``valid_through_gw`` goes stale after the first deadline following its
  ``as_of`` (file ``updated`` / mtime fallback). An override never lifts the availability gate: a
  player the live feed rules out (availability 0) is ``overrides_blocked``.
  ``overrides_applied`` / ``overrides_unmatched`` / ``overrides_expired`` /
  ``overrides_stale`` / ``overrides_blocked`` in the output say what happened to every entry.
* **Never-drop list** — ``data/derived/<season>/ml/squad_prefs.json``
  (``{"never_drop": [{"player", "team"?, "code"?, "until_gw"?, "note"?}]}``,
  hand-maintained, gitignored) names squad players the user has decided to
  keep. Matched like a role override (``code``, else ``player`` + ``team``).
  A protected player is never a drop pick or a ``drop_candidates`` row: the
  drop at his position falls to the next eligible player, and a position
  whose every player is protected gets no swaps (``best_by_position`` empty).
  ``until_gw`` protects through that gameweek, then the entry is expired.
  ``never_drop_applied`` / ``never_drop_unmatched`` / ``never_drop_expired``
  in the output say what happened to every entry.

CLI: python -m backend.ml.waiver --league <id> --entry <id> [--scorer {heuristic,model}]
         [--horizon {1,3,ros}]
(env fallback: LEAGUE_ID / ENTRY_ID; ``--data-root`` and ``--out`` override the
default data dir / output path)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

from backend.ml import jsonutil, paths
from backend.ml.gameweeks import finished_gameweeks

logger = logging.getLogger(__name__)

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
NEXT_GWS = 3
TOTAL_GWS = 38
# Fixture multiplier bounds and home nudge (documented, deliberately mild —
# fixtures modulate quality, they don't replace it).
FIXTURE_MULT_MIN, FIXTURE_MULT_MAX = 0.85, 1.15
HOME_NUDGE = 1.03
# Valid draft formations: (DEF, MID, FWD) with exactly 1 GKP, 10 outfielders.
FORMATIONS = [(3, 4, 3), (3, 5, 2), (4, 3, 3), (4, 4, 2), (4, 5, 1), (5, 3, 2), (5, 4, 1)]
# Default prior season (for 2026-27): the season team strengths and club
# moves are measured on. Callers pass ``paths.prior_season(season)``.
PRIOR_SEASON = "2025-26"
# Role signals for club-movers (see module docstring). A player averaging
# ROLE_MINUTES_FULL minutes over the last ROLE_MINUTES_WINDOW finished GWs
# keeps his full projection; below that it scales down linearly, never under
# ROLE_FLOOR (a 0-minute signing still has some chance of winning the role).
ROLE_MINUTES_WINDOW = 5
ROLE_MINUTES_FULL = 60.0
ROLE_FLOOR = 0.15
DROP_CANDIDATES_PER_POSITION = 3
# Diversification of the overall top N (see module docstring): at most this
# many recs may share one drop player before the rest are held for backfill.
MAX_RECS_PER_DROP = 3
# Swaps listed per position in best_by_position.
BEST_BY_POSITION_N = 3
# What ``apply_role_overrides`` did with each role_overrides.json entry, in
# the order the output JSON and CLI list them.
OVERRIDE_REPORT_KEYS = ("overrides_applied", "overrides_unmatched", "overrides_expired",
                        "overrides_stale", "overrides_blocked")
# What ``apply_never_drop`` did with each squad_prefs.json ``never_drop`` entry.
NEVER_DROP_REPORT_KEYS = ("never_drop_applied", "never_drop_unmatched", "never_drop_expired")


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

def team_strengths(seasons: pd.DataFrame, teams: list[dict],
                   prior_season: str = PRIOR_SEASON) -> dict[int, float]:
    """Current team id -> strength score (sum of that club's player points in
    ``prior_season``). Promoted/unseen clubs get a weak prior: 90% of the
    minimum."""
    last = seasons[seasons["season"] == prior_season]
    by_name = last.groupby("team_name")["total_points"].sum().to_dict()
    floor = 0.9 * min(by_name.values()) if by_name else 0.0
    return {t["id"]: float(by_name.get(t["name"], floor)) for t in teams}


def fixture_multiplier(opponent_strength: float, strengths: dict[int, float],
                       is_home: bool) -> float:
    """Mild multiplier: weakest opponent -> ~1.15, strongest -> ~0.85."""
    values = sorted(strengths.values())
    lo, hi = values[0], values[-1]
    span = (hi - lo) or 1.0
    # opponent strong -> harder fixture -> below 1
    centered = 0.5 - (opponent_strength - lo) / span  # +0.5 weakest .. -0.5 strongest
    mult = 1.0 + centered * (FIXTURE_MULT_MAX - FIXTURE_MULT_MIN)
    mult *= HOME_NUDGE if is_home else (2 - HOME_NUDGE)
    return max(FIXTURE_MULT_MIN, min(FIXTURE_MULT_MAX, mult))


def next_fixture_load(bootstrap: dict, strengths: dict[int, float],
                      n_events: int = NEXT_GWS, *,
                      fixtures_by_event: dict | None = None) -> dict[int, float]:
    """team id -> summed fixture multiplier over the next n events.

    ``bootstrap['fixtures']`` is a dict keyed by event number holding that
    event's fixtures; a team appearing 0/1/2 times in an event is a blank /
    normal / double gameweek and the sum reflects it automatically.
    ``fixtures_by_event`` (same shape) overrides the bootstrap schedule — the
    replay harness uses it to supply the as-of schedule.
    """
    load: dict[int, float] = {t["id"]: 0.0 for t in bootstrap.get("teams", [])}
    if fixtures_by_event is None:
        fixtures_by_event = bootstrap.get("fixtures", {}) or {}
    events = sorted(int(e) for e in fixtures_by_event)[:n_events]
    for event in events:
        for fx in fixtures_by_event[str(event)]:
            home, away = fx.get("team_h"), fx.get("team_a")
            if home in load:
                load[home] += fixture_multiplier(strengths.get(away, 0.0), strengths, True)
            if away in load:
                load[away] += fixture_multiplier(strengths.get(home, 0.0), strengths, False)
    return load


def availability_factor(element: dict) -> float:
    """Live availability gate from bootstrap status/news flags."""
    status = element.get("status", "a")
    chance = element.get("chance_of_playing_next_round")
    if status == "a":
        return 1.0
    if status == "u":
        return 0.0
    if chance is not None:
        return max(0.0, min(1.0, float(chance) / 100.0))
    return 0.75 if status == "d" else 0.0


def club_moves(bootstrap: dict, seasons: pd.DataFrame,
               prior_season: str = PRIOR_SEASON) -> dict[int, bool]:
    """code -> True when the player's current club differs from his club in
    ``prior_season`` (bootstrap team name vs ``player_seasons.team_name``).

    Codes without a prior-season row are absent — callers read that as None
    (new to the league, nothing to compare). A seasons table without a
    ``code`` column yields no flags at all; rows with a null code are skipped.
    """
    if "code" not in seasons.columns:
        return {}
    last = seasons[(seasons["season"] == prior_season) & seasons["code"].notna()]
    prior_club = dict(zip(last["code"].astype(int), last["team_name"]))
    club_names = {t["id"]: t.get("name") for t in bootstrap.get("teams", [])}
    return {int(el["code"]): prior_club[el["code"]] != club_names.get(el.get("team"))
            for el in bootstrap.get("elements", []) if el.get("code") in prior_club}


def minutes_profile(season_panel: pd.DataFrame | None,
                    window: int = ROLE_MINUTES_WINDOW) -> dict[int, tuple[float, int]] | None:
    """code -> ``(expected_minutes, minutes_season)`` from one season's panel.

    ``expected_minutes`` is the mean over the last ``window`` gameweeks present
    in the panel (fewer early in the season); a gameweek with no row for the
    player counts as 0 minutes. Returns None when there is no panel or it has
    no rows yet (pre-GW1) — "unknown", which is not the same as 0 minutes.
    Players with no row at all are absent; callers treat them as ``(0.0, 0)``.
    """
    if season_panel is None or season_panel.empty:
        return None
    minutes = pd.to_numeric(season_panel["minutes"], errors="coerce").fillna(0.0)
    recent_gws = sorted(int(gw) for gw in season_panel["gw"].unique())[-window:]
    in_window = season_panel["gw"].isin(recent_gws)
    season_total = minutes.groupby(season_panel["code"]).sum()
    recent_total = minutes[in_window].groupby(season_panel.loc[in_window, "code"]).sum()
    return {int(code): (float(recent_total.get(code, 0.0)) / len(recent_gws), int(total))
            for code, total in season_total.items()}


def role_factor(club_moved: bool | None, expected_minutes: float | None,
                status: str | None = "a") -> float:
    """Multiplier on a season projection for a player whose role is unproven.

    1.0 unless the player changed clubs, his minutes are known AND he is
    available (status "a"); then
    ``clip(expected_minutes / ROLE_MINUTES_FULL, ROLE_FLOOR, 1.0)``. An
    injured, doubtful or suspended club-mover's low minutes are explained by
    the absence, not by a lost role, so his projection is not scaled down
    (the availability gate already handles the next gameweek).
    """
    if (club_moved is not True or status != "a"
            or expected_minutes is None or pd.isna(expected_minutes)):
        return 1.0
    return max(ROLE_FLOOR, min(1.0, float(expected_minutes) / ROLE_MINUTES_FULL))


def next_event(bootstrap: dict, now: datetime | None = None) -> int | None:
    """The gameweek the weekly tools plan for, or None (season over / no calendar).

    Delegates to ``matchmodel.next_gameweek`` so waiver_plan, my_week and the
    ``xp_gw{N}`` build agree on N. The bootstrap's fixture map can still hold
    the in-play gameweek mid-GW (its smallest key = current), which is why
    the smallest key is not used: it would plan for a locked gameweek.
    """
    # Local import: matchmodel imports availability_factor from this module.
    from backend.ml import matchmodel
    try:
        return matchmodel.next_gameweek(bootstrap, now)
    except ValueError:
        return None


def upcoming_fixtures(bootstrap: dict) -> dict:
    """The bootstrap fixture map from ``next_event`` onward.

    Drops an in-play gameweek still present mid-GW so the heuristic's fixture
    loads describe the same gameweek as the match xP. Without an event
    calendar (``next_event`` None) the map is returned as given.
    """
    fixtures = bootstrap.get("fixtures") or {}
    event = next_event(bootstrap)
    if event is None:
        return fixtures
    return {k: v for k, v in fixtures.items() if int(k) >= event}


def read_gw_xp(path: Path, expected_gw: int | None, last_finished: int | None = None,
               kind: str = "match xP") -> tuple[pd.DataFrame | None, str | None]:
    """Read the match model's ``xp_gw{N}.parquet``: ``(frame, None)`` when it
    is usable for ``expected_gw``, else ``(None, reason)`` — the reason is
    written to the artifact so a fallback is never silent.

    ``last_finished`` is the last finished gameweek in the bootstrap (0 before
    GW1 finishes); the file must have been trained on a panel through exactly
    that GW. None means "assume ``expected_gw - 1``" (the between-gameweeks
    case). Mid-gameweek — GW N-1 in play, planning for N — the freshest panel
    ends at N-2, which is what ``last_finished`` is then, so the file is
    served from the deadline onward instead of being rejected until GW N-1
    finishes. ``kind`` names the file in the reason (the horizon file shares
    this rule: see ``read_horizon_xp``).
    """
    path = Path(path)
    if not path.exists():
        return None, f"{kind} file {path.name} missing"
    try:
        frame = pd.read_parquet(path)
    except (OSError, ValueError) as exc:   # pyarrow's ArrowInvalid is a ValueError
        return None, (f"{kind} file {path.name} is unreadable "
                      f"({type(exc).__name__}: {exc}); rebuild it")
    gws = sorted(int(g) for g in frame["gw"].unique()) if "gw" in frame else []
    if expected_gw is None or gws != [expected_gw]:
        return None, f"{kind} file {path.name} is for gw {gws}, expected {expected_gw} (stale)"
    if "panel_max_gw" not in frame:
        return None, (f"{kind} file {path.name} has no panel_max_gw "
                      "(built before the panel check; rebuild it)")
    required = expected_gw - 1 if last_finished is None else last_finished
    panel_gws = sorted(int(g) for g in frame["panel_max_gw"].unique())
    if panel_gws != [required]:
        return None, (f"{kind} file {path.name} was trained on a panel through gw "
                      f"{panel_gws}, but the last finished gw is {required} "
                      "(panel not refreshed; stale)")
    return frame, None


def load_gw_xp(path: Path, expected_gw: int | None,
               last_finished: int | None = None) -> pd.DataFrame | None:
    """Read the match model's ``xp_gw{N}.parquet`` for the upcoming gameweek.

    Returns None (with a WARNING, so the caller falls back to the heuristic
    loudly) when the file is missing or unreadable, was built for a different
    gameweek than ``expected_gw``, or was trained on a panel whose last GW is
    not the last finished one (stale). See ``read_gw_xp`` for the rule and
    the reason string.
    """
    frame, reason = read_gw_xp(path, expected_gw, last_finished)
    if reason:
        logger.warning("%s — falling back to heuristic", reason)
    return frame


def usable_gw_xp(gw_xp: pd.DataFrame) -> pd.DataFrame:
    """The rows of a match xP frame that carry a real prediction.

    ``matchmodel.score_fixtures`` emits a row for every fixture stub, including
    players in a position the panel was too thin to fit: their NaN per-fixture
    xP sums to ``xp`` 0 with a NaN ``p_start``. That is "no opinion", not a
    predicted blank, so those rows are dropped here and the player is valued by
    the heuristic like anyone else the model does not cover.
    """
    return gw_xp[gw_xp["xp"].notna() & gw_xp["p_start"].notna()]


SCORERS = ("heuristic", "model")
DEFAULT_SCORER = "model"
# ``--horizon``: which gain the model scorer ranks on (see ``rank_key``).
HORIZONS = ("1", "3", "ros")
DEFAULT_HORIZON = "3"
_RANK_BY_HORIZON = {"1": "next1", "3": "next3", "ros": "ros"}
# Largest |xp_h1 - xp_gw.xp| tolerated between the two model files. They are
# equal bit for bit when built from the same inputs; anything visible means
# one of them is from an older bootstrap or panel.
HORIZON_XP_TOLERANCE = 1e-6


def resolve_scorer(scorer: str, bootstrap: dict,
                   ml_dir: Path) -> tuple[pd.DataFrame | None, dict[str, Any]]:
    """The next-GW xP frame for the requested scorer, plus run metadata.

    ``model`` reads ``<ml_dir>/xp_gw{N}.parquet`` for the bootstrap's next
    event and requires its panel to end at the bootstrap's last finished
    gameweek (see ``read_gw_xp``); a missing, unreadable or stale file falls
    back to the heuristic for everyone.
    The metadata (merged into the waiver_plan / my_week JSON) records what
    actually ran, never just what was asked for:

    * ``scorer`` — the scorer used (``heuristic`` after a fallback)
    * ``scorer_requested`` — the ``--scorer`` value
    * ``xp_fallback`` — True when ``model`` was requested but not used
    * ``xp_fallback_reason`` — why (None when there was no fallback)
    """
    meta: dict[str, Any] = {"scorer": scorer, "scorer_requested": scorer,
                            "xp_fallback": False, "xp_fallback_reason": None}
    if scorer != "model":
        return None, meta
    event = next_event(bootstrap)
    if event is None:
        frame, reason = None, "no upcoming gameweek in the bootstrap"
    else:
        last_finished = max(finished_gameweeks(bootstrap), default=0)
        frame, reason = read_gw_xp(Path(ml_dir) / f"xp_gw{event}.parquet", event,
                                   last_finished)
    if reason:
        logger.warning("%s — falling back to heuristic", reason)
        meta.update(scorer="heuristic", xp_fallback=True, xp_fallback_reason=reason)
    return frame, meta


def read_horizon_xp(path: Path, expected_gw: int | None, last_finished: int | None,
                    gw_xp: pd.DataFrame) -> tuple[pd.DataFrame | None, str | None]:
    """Read ``xp_horizon_gw{N}.parquet``: ``(frame, None)`` or ``(None, reason)``.

    The file passes the same freshness rule as ``xp_gw{N}`` (``read_gw_xp``:
    built for ``expected_gw`` on a panel through ``last_finished``) and must
    also agree with ``gw_xp``: its first event is the same forecast, so a
    visible difference (> ``HORIZON_XP_TOLERANCE``) means the two files were
    built from different inputs and the 3-GW values cannot be trusted next to
    the next-GW ones.
    """
    frame, reason = read_gw_xp(path, expected_gw, last_finished, kind="horizon xP")
    if reason:
        return None, reason
    required = {"code", "event", "xp", "xp_started", "xp_h1", "xp_h3", "fitted"}
    if not required <= set(frame.columns):
        return None, (f"horizon xP file {Path(path).name} lacks "
                      f"{sorted(required - set(frame.columns))}; rebuild it")
    first = frame.drop_duplicates("code").set_index("code")["xp_h1"]
    paired = gw_xp.drop_duplicates("code").set_index("code")["xp"].to_frame().join(
        first, how="inner").dropna()
    if not paired.empty and (paired["xp"] - paired["xp_h1"]).abs().max() > HORIZON_XP_TOLERANCE:
        return None, (f"horizon xP file {Path(path).name} disagrees with xp_gw{expected_gw} "
                      "on the next gameweek (built from different inputs; rebuild both)")
    return frame, None


def resolve_horizon(horizon: str, gw_xp: pd.DataFrame | None, bootstrap: dict,
                    ml_dir: Path) -> tuple[pd.DataFrame | None, dict[str, Any]]:
    """The 3-GW horizon frame for the model scorer, plus run metadata.

    Read only when the match xP is in use (``gw_xp`` not None): the heuristic
    scorer never mixes model values in. The metadata is merged into the
    waiver_plan JSON:

    * ``horizon_requested`` — the ``--horizon`` value
    * ``horizon_fallback`` — True when the model scorer ran without a usable
      ``xp_horizon_gw{N}.parquet``: every 3-GW value is then heuristic, and a
      requested 3-GW ranking is replaced by the next-GW one (``rank_key``)
    * ``horizon_fallback_reason`` — why (None when there was no fallback)
    * ``horizon_events`` — the events summed into the model's 3-GW value
      (None without the file); fewer than 3 means the bootstrap did not hold
      the whole horizon
    """
    meta: dict[str, Any] = {"horizon_requested": horizon, "horizon_fallback": False,
                            "horizon_fallback_reason": None, "horizon_events": None}
    if gw_xp is None:
        return None, meta
    event = next_event(bootstrap)
    last_finished = max(finished_gameweeks(bootstrap), default=0)
    frame, reason = read_horizon_xp(Path(ml_dir) / f"xp_horizon_gw{event}.parquet", event,
                                    last_finished, gw_xp)
    if reason:
        logger.warning("%s — 3-GW values fall back to the heuristic", reason)
        meta.update(horizon_fallback=True, horizon_fallback_reason=reason)
        return None, meta
    meta["horizon_events"] = sorted(int(e) for e in frame["event"].unique())
    return frame, meta


def rank_key(horizon: str, gw_xp: pd.DataFrame | None,
             horizon_xp: pd.DataFrame | None) -> str:
    """``recommend``'s ``rank_by`` for a requested ``--horizon``.

    * no match xP (heuristic scorer, or a fallback to it) -> ``legacy``,
      whatever was asked: the heuristic ranking is the frozen pre-xP baseline.
    * ``"1"`` -> ``next1``; ``"ros"`` -> ``ros``.
    * ``"3"`` -> ``next3`` when the horizon frame is there, else ``next1``:
      without it the 3-GW values are heuristic and a free agent the model
      values but the projection does not (a promoted club's starter) would
      drop out of the ranking.
    """
    if horizon not in HORIZONS:
        raise ValueError(f"unknown horizon {horizon!r}; expected one of {HORIZONS}")
    if gw_xp is None:
        return "legacy"
    if horizon == "3" and horizon_xp is None:
        return "next1"
    return _RANK_BY_HORIZON[horizon]


def usable_horizon_xp(horizon_xp: pd.DataFrame | None) -> pd.DataFrame:
    """The rows of a horizon frame that carry a real prediction: positions the
    model fitted. (A blank — ``num_fixtures`` 0, ``xp`` 0.0 — is a prediction;
    an unfitted position's 0.0 is not.) Empty for None."""
    if horizon_xp is None:
        return pd.DataFrame(columns=["code", "event", "xp", "xp_started", "xp_h3"])
    return horizon_xp[horizon_xp["fitted"].astype(bool)]


def build_player_table(bootstrap: dict, seasons: pd.DataFrame,
                       projections_path: Path, *,
                       fixtures_by_event: dict | None = None,
                       neutral_availability: bool = False,
                       gw_xp: pd.DataFrame | None = None,
                       season_panel: pd.DataFrame | None = None,
                       horizon_xp: pd.DataFrame | None = None,
                       prior_season: str = PRIOR_SEASON) -> pd.DataFrame:
    """One row per 26/27 element: identity, availability, xP, ROS value.

    ``fixtures_by_event`` overrides the bootstrap schedule (default:
    ``upcoming_fixtures``, i.e. from ``next_event`` onward) and
    ``neutral_availability`` forces availability to 1.0, except 0.0 for
    departed (status "u") players (both used by the
    replay harness, where only the current status/news snapshot exists).
    ``prior_season`` is the season team strengths and club moves are read
    from (``paths.prior_season`` of the season being scored).

    ``gw_xp`` is the match model's one-row-per-code next-GW frame (see
    ``matchmodel.build_gw_xp``). Joined on the permanent ``code``, it supplies
    ``xp_next, p_start, p_appear, xp_floor, xp_ceiling, drivers, opponents`` with
    ``xp_source == "model"``. A player it does not cover — no row, or a row
    without a prediction (``usable_gw_xp``) — gets the heuristic
    1-GW value (``ros/38 x next-event fixture load x availability``,
    source ``heuristic``), or ``none`` when there is no projection either.

    ``horizon_xp`` is the match model's (code, event) frame for the next 3
    events (``matchmodel.build_horizon_xp``). A player it covers
    (``usable_horizon_xp``) takes ``next3_xp = xp_h3`` (2 decimals) with
    ``next3_source == "model"``; everyone else, and everyone when it is not
    supplied, keeps the heuristic ``ros/38 x 3-event fixture load x
    availability`` (1 decimal, ``heuristic``; ``none`` without a projection).

    Availability reconciliation: the model rows' Stage-1 gate used the
    bootstrap from when the xP files were built. When the CURRENT availability
    is 0 (status u/i/s without a chance, or chance 0) a model row's
    ``xp_next``, ``p_start``, ``xp_floor`` and ``xp_ceiling`` — and a
    model-based ``next3_xp`` — are set to 0 and ``xp_reconciled`` is True
    (False on every other row). Partial availability changes (e.g. a new 50%
    doubt) are not reconciled.

    ``season_panel`` is the current season's ``player_gameweeks`` panel
    (finished gameweeks only). It feeds ``expected_minutes`` /
    ``minutes_season`` and, for club-movers, ``role_factor`` and ``ros_adj``
    (see the module docstring). Without it every factor is 1.0 and
    ``ros_adj == ros_points``, except departed (status "u") players, whose
    ``ros_adj`` is always 0. ``xp_started`` is the next-GW value if the player
    starts every fixture: the model's Stage-2 conditional summed over the
    gameweek's fixtures when the xP frame carries it, else the heuristic at
    full availability.
    """
    strengths = team_strengths(seasons, bootstrap.get("teams", []), prior_season)
    if fixtures_by_event is None:
        fixtures_by_event = upcoming_fixtures(bootstrap)
    load = next_fixture_load(bootstrap, strengths, fixtures_by_event=fixtures_by_event)
    load1 = next_fixture_load(bootstrap, strengths, n_events=1,
                              fixtures_by_event=fixtures_by_event)
    model_by_code: dict[int, dict] = {}
    if gw_xp is not None and not gw_xp.empty:
        usable = usable_gw_xp(gw_xp)
        if len(usable) < len(gw_xp):
            logger.warning("match xP has no prediction for %d of %d players (position not "
                           "fitted) — heuristic for those", len(gw_xp) - len(usable), len(gw_xp))
        model_by_code = usable.drop_duplicates("code").set_index("code").to_dict("index")
    horizon_by_code = (usable_horizon_xp(horizon_xp).drop_duplicates("code")
                       .set_index("code")["xp_h3"].to_dict())
    projections = {}
    if Path(projections_path).exists():
        projections = {int(r["code"]): r for r in
                       json.loads(Path(projections_path).read_text(encoding="utf-8"))}
    else:
        logger.warning("projections missing at %s — values will be null", projections_path)

    team_names = {t["id"]: t.get("short_name") or t.get("name")
                  for t in bootstrap.get("teams", [])}
    moves = club_moves(bootstrap, seasons, prior_season)
    profile = minutes_profile(season_panel)
    rows = []
    for el in bootstrap.get("elements", []):
        projection = projections.get(el.get("code"), {})
        ros = projection.get("projected_points")
        club_moved = moves.get(el.get("code"))
        expected_minutes, minutes_season = (
            (None, None) if profile is None else profile.get(el.get("code"), (0.0, 0)))
        factor = role_factor(club_moved, expected_minutes, el.get("status"))
        ros_role = ros * factor if ros is not None else None
        # A departed player scores nothing from here on, projection or not.
        ros_adj = 0.0 if el.get("status") == "u" else ros_role
        if neutral_availability:
            # replay mode: everyone available except departed ("u") players
            avail = 0.0 if el.get("status") == "u" else 1.0
        else:
            avail = availability_factor(el)
        per_gw = (ros_role / TOTAL_GWS) if ros_role is not None else None
        next3 = (per_gw * load.get(el.get("team"), 0.0) * avail) if per_gw is not None else None
        full_load1 = per_gw * load1.get(el.get("team"), 0.0) if per_gw is not None else None
        model = model_by_code.get(el.get("code"))
        reconciled = False
        # The conditional "if he starts" value: the model's own when the xP file
        # has it; for a model row from an older file without it, left missing
        # so an override rescales the model value rather than switching to the
        # heuristic; for a heuristic row, the heuristic at full availability.
        if model is None:
            xp_started = full_load1
        elif pd.notna(model.get("xp_started")):
            xp_started = float(model["xp_started"])
        else:
            xp_started = None
        if model is not None:
            xp_next, xp_source = float(model["xp"]), "model"
            xp_extra = {"p_start": float(model["p_start"]), "xp_floor": float(model["xp_floor"]),
                        "xp_ceiling": float(model["xp_ceiling"]),
                        "drivers": model["drivers"], "opponents": model["opponents"]}
            if avail == 0.0 and (xp_next > 0.0 or xp_extra["p_start"] > 0.0):
                # The xP file's Stage-1 gate saw the bootstrap of its build
                # time; a player ruled out since (or served from an older
                # file after a failed rebuild) must not keep a positive xP.
                xp_next, reconciled = 0.0, True
                xp_extra.update(p_start=0.0, xp_floor=0.0, xp_ceiling=0.0)
            # Appearance chance (start or cameo) for auto-sub odds; NaN from an
            # xP file written before it was carried.
            p_appear = model.get("p_appear")
            xp_extra["p_appear"] = (0.0 if reconciled else
                                    float(p_appear) if pd.notna(p_appear) else float("nan"))
        else:
            xp_extra = {"p_start": None, "xp_floor": None, "xp_ceiling": None,
                        "drivers": None, "opponents": None, "p_appear": None}
            if full_load1 is not None:
                xp_next = full_load1 * avail
                xp_source = "heuristic"
            else:
                xp_next, xp_source = None, "none"
        if el.get("code") in horizon_by_code:
            next3_xp, next3_source = round(float(horizon_by_code[el["code"]]), 2), "model"
            if avail == 0.0 and next3_xp > 0.0:
                # same reconciliation as xp_next: the horizon file's gate is stale
                next3_xp, reconciled = 0.0, True
        elif next3 is not None:
            next3_xp, next3_source = round(next3, 1), "heuristic"
        else:
            next3_xp, next3_source = None, "none"
        rows.append({
            "element": el["id"], "code": el.get("code"),
            "web_name": el.get("web_name"),
            "position": POSITIONS.get(el.get("element_type")),
            "team": team_names.get(el.get("team")),
            "availability": avail,
            "status": el.get("status"), "news": el.get("news") or "",
            "next3_xp": next3_xp, "next3_source": next3_source,
            "xp_next": round(xp_next, 2) if xp_next is not None else None,
            "xp_source": xp_source, "xp_reconciled": reconciled, **xp_extra,
            "xp_started": round(xp_started, 2) if xp_started is not None else None,
            # ROS is deliberately NOT availability-gated: an injury gates the
            # next-3 horizon, not the season (status "u" = departed is the
            # exception: ros_adj 0, never recommended, always the drop).
            "ros_points": round(ros, 1) if ros is not None else None,
            "ros_adj": round(ros_adj, 1) if ros_adj is not None else None,
            "club_moved": club_moved,
            "expected_minutes": (round(expected_minutes, 1)
                                 if expected_minutes is not None else None),
            "minutes_season": minutes_season,
            "role_factor": round(factor, 3),
            "tier": projection.get("tier"), "confidence": projection.get("confidence"),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Role overrides (hand-maintained team news)
# ---------------------------------------------------------------------------

def load_season_panel(path: Path, season: str) -> pd.DataFrame | None:
    """The season's ``player_gameweeks`` panel for the role signals, or None
    (with a WARNING — club-move factors are then off) when it is missing or
    unreadable. Rows of other seasons are dropped."""
    path = Path(path)
    if not path.exists():
        logger.warning("season panel %s missing — club-move role signals off", path.name)
        return None
    try:
        panel = pd.read_parquet(path)
    except (OSError, ValueError) as exc:   # pyarrow's ArrowInvalid is a ValueError
        logger.warning("season panel %s unreadable (%s) — club-move role signals off",
                       path.name, type(exc).__name__)
        return None
    return panel[panel["season"] == season] if "season" in panel.columns else panel


def load_role_overrides(path: Path) -> list[dict]:
    """Entries of ``role_overrides.json`` (``{"overrides": [...]}``).

    The file is optional: absent -> ``[]`` silently; unparseable or the wrong
    shape -> ``[]`` with a WARNING, so a typo never blocks the weekly run.

    Every returned entry carries an ``as_of`` (when the fact was written, for
    the staleness rule in ``apply_role_overrides``): its own if it has one,
    else the file's top-level ``as_of`` or ``updated``, else the file's
    modification time (UTC, ISO). Entries are copies; the file is not touched.
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        entries = doc.get("overrides", [])
        file_as_of = (doc.get("as_of") or doc.get("updated")
                      or datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat())
    except (OSError, ValueError, AttributeError) as exc:
        logger.warning("role overrides %s unreadable (%s) — ignored", path.name, exc)
        return []
    if not isinstance(entries, list):
        logger.warning("role overrides %s: 'overrides' is not a list — ignored", path.name)
        return []
    return [{**entry, "as_of": entry.get("as_of") or file_as_of}
            for entry in entries if isinstance(entry, dict)]


def match_player_rows(players: pd.DataFrame, entry: dict, code: int | None) -> pd.Index:
    """Index labels of the ``players`` rows a hand-written entry names: by the
    permanent ``code`` when ``code`` is given (an int, already parsed by the
    caller), else by ``entry["player"]`` (web name) + ``entry["team"]`` (short
    name). Shared by ``role_overrides.json`` and ``squad_prefs.json``; callers
    treat anything but exactly one row as unmatched."""
    if code is not None:
        return players.index[pd.to_numeric(players["code"], errors="coerce") == code]
    return players.index[(players["web_name"] == entry.get("player"))
                         & (players["team"] == entry.get("team"))]


def load_squad_prefs(path: Path) -> list[dict]:
    """The ``never_drop`` entries of ``squad_prefs.json``
    (``{"never_drop": [{"player", "team"?, "code"?, "until_gw"?, "note"?}]}``).

    The file is optional and hand-maintained, like ``role_overrides.json``:
    absent -> ``[]`` silently; unparseable or the wrong shape -> ``[]`` with a
    WARNING, so a typo never blocks the weekly run (but then nobody is
    protected — the warning is the only signal). Entries are copies.
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8")).get("never_drop", [])
    except (OSError, ValueError, AttributeError) as exc:
        logger.warning("squad prefs %s unreadable (%s) — never_drop ignored", path.name, exc)
        return []
    if not isinstance(entries, list):
        logger.warning("squad prefs %s: 'never_drop' is not a list — ignored", path.name)
        return []
    return [dict(entry) for entry in entries if isinstance(entry, dict)]


def apply_never_drop(players: pd.DataFrame, entries: list[dict],
                     target_gw: int | None) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    """Mark the players the user never wants dropped.

    Each entry names one player the way a role override does
    (``match_player_rows``: ``code`` first — int or numeric string — else
    ``player`` + ``team``). An entry with ``until_gw`` protects him through
    that gameweek and is ``expired`` once ``target_gw`` is past it (a missing
    ``target_gw`` leaves it live). Returns ``(players copy, report)``: the
    copy gains a ``never_drop`` bool column; the report lists entry names
    under ``never_drop_applied`` / ``never_drop_unmatched`` (no single match,
    or a non-numeric ``code`` / ``until_gw``) / ``never_drop_expired``.
    Nothing raises on a bad entry. ``drop_order`` never returns a protected
    player, so he is neither a drop pick nor a drop candidate.
    """
    out = players.copy()
    out["never_drop"] = False
    report: dict[str, list[str]] = {key: [] for key in NEVER_DROP_REPORT_KEYS}
    for entry in entries:
        name = str(entry.get("player") or entry.get("code") or "?")
        try:
            code = int(entry["code"]) if entry.get("code") is not None else None
            until = int(entry["until_gw"]) if entry.get("until_gw") is not None else None
        except (TypeError, ValueError):
            logger.warning("never_drop entry for %s has a non-numeric code/until_gw — skipped", name)
            report["never_drop_unmatched"].append(name)
            continue
        if until is not None and target_gw is not None and target_gw > until:
            report["never_drop_expired"].append(name)
            continue
        matched = match_player_rows(out, entry, code)
        if len(matched) != 1:
            logger.warning("never_drop entry for %s (%s) matches %d players — skipped",
                           name, entry.get("team"), len(matched))
            report["never_drop_unmatched"].append(name)
            continue
        out.loc[matched[0], "never_drop"] = True
        report["never_drop_applied"].append(name)
    return out, report


def event_deadlines(bootstrap: dict) -> dict[int, datetime]:
    """Gameweek -> deadline (UTC) from the bootstrap ``events`` (draft
    ``{"data": [...]}`` shape or a plain list); events without a parseable
    ``deadline_time`` are left out."""
    from backend.ml import matchmodel   # local import: matchmodel imports this module
    events = bootstrap.get("events") or []
    if isinstance(events, dict):
        events = events.get("data") or []
    deadlines = {int(e["id"]): matchmodel._parse_deadline(e.get("deadline_time"))
                 for e in events if "id" in e}
    return {gw: deadline for gw, deadline in deadlines.items() if deadline is not None}


def _parse_as_of(value: Any) -> datetime | None:
    """``as_of`` as an aware UTC datetime: an ISO date (read as 00:00 UTC that
    day, so a fact written on a deadline day counts for that gameweek) or an
    ISO datetime (naive = UTC). None when absent or unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def override_window_end(as_of: Any, deadlines: dict[int, datetime]) -> int | None:
    """The last gameweek an undated override entry may describe: the first
    gameweek whose deadline falls after ``as_of``. None when ``as_of`` is
    unknown or no deadline in ``deadlines`` follows it."""
    written = _parse_as_of(as_of)
    if written is None:
        return None
    after = [gw for gw, deadline in deadlines.items() if deadline > written]
    return min(after) if after else None


def _override_lifetime(entry: dict, target_gw: int | None,
                       deadlines: dict[int, datetime] | None) -> str:
    """``"live"``, ``"expired"`` or ``"stale"`` for ``entry`` at ``target_gw``.

    * ``valid_through_gw`` (explicit): stale once ``target_gw`` is past it.
    * ``return_gw``: expired once ``return_gw <= target_gw`` (the absence it
      described is over).
    * Neither: the fact describes the gameweek it was written for and no
      other — live only while ``target_gw <= override_window_end(as_of)``.
      Unknown ``as_of``, no calendar, or no ``target_gw`` -> stale (an entry
      that cannot be shown to be current is not applied).

    Raises ValueError/TypeError on a non-numeric ``valid_through_gw`` /
    ``return_gw``.
    """
    valid_through = entry.get("valid_through_gw")
    return_gw = entry.get("return_gw")
    if valid_through is not None:
        if target_gw is None or target_gw > int(valid_through):
            return "stale"
    if return_gw is not None:
        return "expired" if target_gw is not None and int(return_gw) <= target_gw else "live"
    if valid_through is not None:
        return "live"
    window_end = override_window_end(entry.get("as_of"), deadlines or {})
    if target_gw is None or window_end is None or target_gw > window_end:
        return "stale"
    return "live"


def _override_p_start(entry: dict, target_gw: int | None) -> float | None:
    """The start probability an override entry asserts for ``target_gw``.

    A ``return_gw`` after the target means "out until then": 0.0 whatever
    ``p_start`` says. Otherwise ``p_start`` clipped to [0, 1], or None for a
    fact-only entry. Raises ValueError/TypeError on a non-numeric value.
    """
    return_gw = entry.get("return_gw")
    if return_gw is not None and target_gw is not None and int(return_gw) > target_gw:
        return 0.0
    if entry.get("p_start") is None:
        return None
    return max(0.0, min(1.0, float(entry["p_start"])))


def _overridden_xp_next(p_override: float, xp_started, xp_next, model_p_start,
                        name: str):
    """Next-GW xP once ``p_start`` is overridden to ``p_override``.

    A player with no value at all (no ``xp_next``, no ``xp_started``) stays
    unvalued. Otherwise a ruled-out player (``p_override == 0``) is worth 0
    whatever else is known, and the conditional ``xp_started`` gives
    ``p × xp_started``. An xP file written before S3 has no ``xp_started``:
    the model's own value is rescaled by ``p_override / model_p_start``,
    which keeps its cameo term, so it is approximate. With no usable
    ``p_start`` either, the value is left as it was, with a warning.
    """
    if pd.isna(xp_next) and pd.isna(xp_started):
        return xp_next
    if p_override == 0:
        return 0.0
    if pd.notna(xp_started):
        return round(p_override * float(xp_started), 2)
    if pd.notna(xp_next) and pd.notna(model_p_start) and float(model_p_start) > 0:
        return round(float(xp_next) * p_override / float(model_p_start), 2)
    logger.warning("role override for %s: no xp_started or model p_start to rescale "
                   "by — xp_next left unchanged", name)
    return xp_next


def _override_horizon_xp(entry: dict, events: pd.DataFrame,
                         deadlines: dict[int, datetime] | None) -> float:
    """A player's horizon xP with ``entry`` applied to each event it covers.

    ``events`` are his ``usable_horizon_xp`` rows. For every event the entry
    is live at (``_override_lifetime``) and asserts a start probability for
    (``_override_p_start``), the event is worth ``p_start x xp_started``;
    every other event keeps the model's ``xp``.
    """
    total = 0.0
    for event, xp, xp_started in zip(events["event"], events["xp"], events["xp_started"]):
        live = _override_lifetime(entry, int(event), deadlines) == "live"
        p_start = _override_p_start(entry, int(event)) if live else None
        total += float(xp) if p_start is None else p_start * float(xp_started)
    return total


def apply_role_overrides(players: pd.DataFrame, overrides: list[dict],
                         target_gw: int | None,
                         deadlines: dict[int, datetime] | None = None,
                         horizon_xp: pd.DataFrame | None = None,
                         ) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    """Apply ``role_overrides.json`` entries to the next gameweek's values.

    Entry: ``{"player": web_name, "team": short name, "fact": str,
    "p_start": 0..1, "return_gw": int, "valid_through_gw": int,
    "as_of": ISO date/datetime, "code": int}`` — only ``player`` +
    ``team`` (or ``code``, which wins when present, for the rare same-name
    team-mates; an int or a numeric string) are needed to match; an entry must match exactly one player.

    * ``p_start`` replaces the model's: ``xp_next = p_start x xp_started``
      (``xp_started`` = value if he starts every fixture; the substitute-cameo
      term is dropped, a deliberate simplification — an override says "this
      is his start chance", not how often he comes off the bench). ``p_start``
      is set to the override and the now-stale ``xp_floor``/``xp_ceiling`` are
      cleared. A player with no value at all (``xp_started`` null) keeps a
      null ``xp_next``. ROS is not touched.
    * The 3-GW value follows the same per-event rule when ``horizon_xp`` (the
      model's (code, event) frame) is supplied and values the player
      (``next3_source == "model"``): ``next3_xp`` is re-summed with the
      override applied to exactly the events it is live for. An undated entry
      therefore changes event ``target_gw`` only (it is stale for the next
      one); ``valid_through_gw`` extends a ``p_start`` through that gameweek;
      ``return_gw`` zeroes every event before it and leaves the ones from it
      on at the model's value. A heuristic ``next3_xp`` (no horizon frame, or
      a player it does not cover) is NOT touched: it has no per-event parts.
    * ``return_gw`` after ``target_gw`` forces ``p_start`` 0 (out until then).
      Once ``return_gw <= target_gw`` the entry has EXPIRED and is ignored —
      its ``p_start`` described the absence, not the player after it.
    * Staleness (team news goes off): an entry with ``valid_through_gw``
      applies through that gameweek. An entry with neither ``return_gw`` nor
      ``valid_through_gw`` applies only up to the first gameweek whose
      deadline (``deadlines``, see ``event_deadlines``) falls after its
      ``as_of`` (``load_role_overrides`` fills that from the file's
      ``as_of``/``updated`` or mtime). Past that it is STALE and ignored — a
      "rotation risk" note written for GW2 says nothing about GW6. Without
      ``deadlines`` every such entry is stale.
    * The availability gate wins: a matched player whose ``availability`` is
      0 (status u/i/s, or a stated 0% chance) is BLOCKED — his row is left
      untouched. A start chance written weeks ago must not resurrect a player
      the live feed says cannot play.

    Returns ``(players copy, report)``. The copy gains ``role_override`` (the
    entry's ``fact``, None when not overridden) and ``role_override_p_start``.
    The report lists entry names under ``overrides_applied``,
    ``overrides_unmatched`` (no single match, or an invalid ``p_start`` /
    gameweek), ``overrides_expired``, ``overrides_stale`` and
    ``overrides_blocked``; nothing raises on a bad entry.
    """
    out = players.copy()
    out["role_override"] = None
    out["role_override_p_start"] = float("nan")
    report: dict[str, list[str]] = {key: [] for key in OVERRIDE_REPORT_KEYS}
    horizon_rows = usable_horizon_xp(horizon_xp).sort_values("event")
    for entry in overrides:
        name = str(entry.get("player") or entry.get("code") or "?")
        try:
            lifetime = _override_lifetime(entry, target_gw, deadlines)
            p_override = _override_p_start(entry, target_gw) if lifetime == "live" else None
            # A hand-edited JSON may quote the code ("123"): match on the integer.
            code = int(entry["code"]) if entry.get("code") is not None else None
        except (TypeError, ValueError):
            logger.warning("role override for %s has a non-numeric p_start/return_gw/"
                           "valid_through_gw/code — skipped", name)
            report["overrides_unmatched"].append(name)
            continue
        if lifetime != "live":
            report[f"overrides_{lifetime}"].append(name)
            continue
        matched = match_player_rows(out, entry, code)
        if len(matched) != 1:
            logger.warning("role override for %s (%s) matches %d players — skipped",
                           name, entry.get("team"), len(matched))
            report["overrides_unmatched"].append(name)
            continue
        row = matched[0]
        if out.at[row, "availability"] == 0:
            report["overrides_blocked"].append(name)
            continue
        out.at[row, "role_override"] = str(entry.get("fact") or "")
        if p_override is not None:
            model_p_start = out.at[row, "p_start"]
            out.at[row, "role_override_p_start"] = p_override
            out.at[row, "p_start"] = p_override
            if "p_appear" in out.columns:
                # The override states a start chance and xp_next ignores the
                # cameo, so the appearance chance follows it exactly.
                out.at[row, "p_appear"] = p_override
            out.at[row, "xp_floor"] = out.at[row, "xp_ceiling"] = float("nan")
            out.at[row, "xp_next"] = _overridden_xp_next(
                p_override, out.at[row, "xp_started"], out.at[row, "xp_next"],
                model_p_start, name)
            events = horizon_rows[horizon_rows["code"] == out.at[row, "code"]]
            if not events.empty and out.at[row, "next3_source"] == "model":
                out.at[row, "next3_xp"] = round(
                    _override_horizon_xp(entry, events, deadlines), 2)
        report["overrides_applied"].append(name)
    if report["overrides_stale"]:
        logger.warning("role overrides: %d stale entr%s ignored for GW%s (no return_gw/"
                       "valid_through_gw and written before an earlier deadline) — "
                       "prune or re-date them: %s", len(report["overrides_stale"]),
                       "y" if len(report["overrides_stale"]) == 1 else "ies", target_gw,
                       ", ".join(report["overrides_stale"]))
    return out, report


# ---------------------------------------------------------------------------
# Squad evaluation
# ---------------------------------------------------------------------------

def my_squad(players: pd.DataFrame, element_status: dict, entry_id: int) -> pd.DataFrame:
    """My current 15, from the latest element-status snapshot."""
    mine = {row["element"] for row in element_status.get("element_status", [])
            if row.get("owner") == entry_id}
    return players[players["element"].isin(mine)].copy()


def best_xi(squad: pd.DataFrame, value_col: str = "next3_xp") -> tuple[pd.DataFrame, float]:
    """Best legal XI (1 GKP + a valid DEF/MID/FWD formation) by value_col."""
    ranked = {pos: squad[squad["position"] == pos]
              .sort_values(value_col, ascending=False, na_position="last")
              for pos in ("GKP", "DEF", "MID", "FWD")}
    best_total, best_frame = -1.0, None
    for d, m, f in FORMATIONS:
        if len(ranked["GKP"]) < 1 or len(ranked["DEF"]) < d \
                or len(ranked["MID"]) < m or len(ranked["FWD"]) < f:
            continue
        xi = pd.concat([ranked["GKP"].head(1), ranked["DEF"].head(d),
                        ranked["MID"].head(m), ranked["FWD"].head(f)])
        total = float(xi[value_col].fillna(0).sum())
        if total > best_total:
            best_total, best_frame = total, xi
    if best_frame is None:  # squad too broken for any formation — take best 11
        best_frame = squad.sort_values(value_col, ascending=False).head(11)
        best_total = float(best_frame[value_col].fillna(0).sum())
    return best_frame, round(best_total, 1)


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

def _none_if_nan(value: Any) -> Any:
    """None for a missing table cell (NaN / None), else the value unchanged."""
    return None if pd.isna(value) else value


def _flag_or_none(value: Any) -> bool | None:
    """A plain bool (JSON-safe, never a numpy scalar), or None when unknown."""
    return None if pd.isna(value) else bool(value)


# Sort keys after "departed first" for each ranking (see ``drop_order``).
_DROP_KEYS = {"legacy": ["ros_adj", "next3_xp"],
              "next1": ["xp_next", "ros_adj"],
              "next3": ["next3_xp", "ros_adj"],
              "ros": ["ros_adj", "next3_xp"]}


def valued_mask(squad: pd.DataFrame, rank_by: str = "legacy") -> pd.Series:
    """Squad players the ``rank_by`` ranking can value: a ROS projection
    under ``legacy``; a model value or a ROS projection (``xp_next``,
    ``next3_xp`` or ``ros_points`` present) otherwise. ``drop_order`` draws
    from these and ``unprojected_squad`` lists the rest, so no player is
    both a drop and "no value"."""
    if rank_by not in _DROP_KEYS:
        raise ValueError(f"unknown rank_by {rank_by!r}")
    if rank_by == "legacy":
        return squad["ros_points"].notna()
    return (squad["ros_points"].notna() | squad["xp_next"].notna()
            | squad["next3_xp"].notna())


def drop_order(squad: pd.DataFrame, rank_by: str = "legacy") -> pd.DataFrame:
    """Squad players who may be dropped, most droppable first.

    A departed (status "u") player is always first, valued or not. Then:

    * ``legacy`` (heuristic scorer, frozen): only ROS-projected players, by
      ``(ros_adj, next3_xp)``.
    * ``next1`` / ``next3`` / ``ros``: every player with a model value or a
      ROS projection (``xp_next``, ``next3_xp`` or ``ros_points`` present),
      by the ranking horizon's value — ``xp_next`` / ``next3_xp`` /
      ``ros_adj`` — then ``ros_adj`` (``next3_xp`` under ``ros``). Missing
      values sort last: a player the horizon cannot value is dropped only
      after every player it can.

    A player with no value at all is unknown, not worthless, and is never
    returned (see ``unprojected_squad``). Neither is a protected player
    (``never_drop`` True, see ``apply_never_drop``): the drop falls to the next
    eligible player at the position, and to nobody when all are protected.
    """
    departed = squad["status"] == "u"
    protected = (squad["never_drop"].astype(bool) if "never_drop" in squad.columns
                 else pd.Series(False, index=squad.index))
    droppable = squad[(valued_mask(squad, rank_by) | departed) & ~protected]
    return (droppable.assign(_kept=droppable["status"] != "u")
            .sort_values(["_kept", *_DROP_KEYS[rank_by]], na_position="last")
            .drop(columns="_kept"))


def drop_candidates(squad: pd.DataFrame, rank_by: str = "legacy",
                    per_position: int = DROP_CANDIDATES_PER_POSITION) -> list[dict[str, Any]]:
    """The first ``per_position`` of ``drop_order`` at each position, so the
    drop pick behind every recommendation can be audited (and overruled)."""
    rows = []
    for position in POSITIONS.values():
        ordered = drop_order(squad[squad["position"] == position], rank_by)
        for _, p in ordered.head(per_position).iterrows():
            rows.append({
                "web_name": p["web_name"], "position": position, "team": p["team"],
                "status": p["status"],
                "ros_points": _none_if_nan(p["ros_points"]), "ros_adj": _none_if_nan(p["ros_adj"]),
                "xp_next": _none_if_nan(p["xp_next"]),
                "next3_xp": _none_if_nan(p["next3_xp"]),
                "club_moved": _flag_or_none(p["club_moved"]),
                "expected_minutes": _none_if_nan(p["expected_minutes"]),
            })
    return rows


def ranked_recs(players: pd.DataFrame, squad: pd.DataFrame,
                *, rank_by: str = "next1") -> list[dict[str, Any]]:
    """Every positive add/drop pair, ranked, with the short-vs-long balance
    made explicit (``recommend`` caps and diversifies this list).

    For each free agent, pair with the first same-position player of
    ``drop_order`` (a departed squad player, else the most droppable on the
    ranking horizon; see ``drop_order``).
    Three gains, each the add minus the drop, are carried on every rec as
    ``gains = {"gw1", "gw3", "ros"}`` and as the flat ``next1_gain`` /
    ``next3_gain`` / ``season_gain`` (same values, kept for older readers):

    * ``gw1`` — next-GW xP (``xp_next``).
    * ``gw3`` — 3-GW xP (``next3_xp``: the match model's horizon sum when
      ``add_next3_source`` is ``model``, else the heuristic). None when the
      add or the drop has no 3-GW value at all — unknown, never "0 minus the
      drop" or "the add minus 0" (a departed drop counts as 0). An unknown
      gain counts as 0 for the label and sorts after every known gain.
    * ``ros`` — ``ros_adj`` difference (role-adjusted rest-of-season; a
      departed drop counts as 0). None, with ``season_unknown`` True, for a
      free agent without a ROS projection — such a player is ranked only
      when the match model values him (next GW or horizon) — or for a drop
      without one (``drop_ros_unknown``); the unknown season gain counts as
      0 for the label and the ordering. ``add_ros_unknown`` /
      ``drop_ros_unknown`` say which side lacks the projection.

    Each rec also carries the add's role signals (``club_moved``,
    ``add_expected_minutes``, ``add_role_factor``, ``add_ros_adj``) next to
    the raw ``add_ros``.

    ``rank_by`` picks the gain that orders the list and the short horizon the
    label is read from (``upgrade`` = short and season gain both positive,
    ``stream`` = short only, ``hold`` = season only):

    * ``next1`` — short = ``gw1``; by ``gw1`` descending, holds after every
      positive-``gw1`` rec, ties by ``ros``.
    * ``next3`` — short = ``gw3``; by ``gw3`` descending, holds last, ties by
      ``ros``.
    * ``ros`` — short = ``gw3`` (labels as ``next3``); by ``ros`` descending,
      ties by ``gw3``. Holds are not demoted: on this key they are the point.
    * ``legacy`` — the pre-xP behaviour of the heuristic scorer (labels from
      ``gw3``, ordered by the larger of ``gw3`` and ``ros``); ``plan`` selects
      it whenever no match xP is supplied.
    """
    # Free agents by definition have no owner, so my squad is already excluded.
    pool = players[players["is_free_agent"] & (players["status"] != "u")]
    # Drop candidates come only from players we can value (a model value or
    # a ROS projection). A teammate with neither is NOT worth zero — it is
    # unknown, and auto-dropping a returning star on missing data is the one
    # unrecoverable mistake. Those players go in the plan's
    # unprojected_squad section instead. (A departed teammate is the
    # exception: see drop_order.)
    droppable = {position: drop_order(squad[squad["position"] == position], rank_by)
                 for position in POSITIONS.values()}
    recs: list[dict[str, Any]] = []
    for _, fa in pool.iterrows():
        mine = droppable.get(fa["position"], squad.iloc[0:0])
        # A free agent with no ROS projection (promoted club, new signing,
        # under last season's minutes floor) is still rankable on the next GW
        # when the match model values him: his season gain is unknown — it
        # counts as 0 here, so he can only ever label as a "stream", and is
        # emitted as null with season_unknown so no consumer reads it as 0.
        # The legacy ranking has no next-GW model value and still skips him.
        model_only = (rank_by != "legacy" and pd.isna(fa["ros_points"])
                      and "model" in (fa["xp_source"], fa["next3_source"]))
        if mine.empty or (pd.isna(fa["ros_points"]) and not model_only):
            continue  # cannot recommend a player we cannot value
        drop = mine.iloc[0]

        def _v(x):
            return 0.0 if pd.isna(x) else float(x)

        next1_gain = _v(fa["xp_next"]) - _v(drop["xp_next"])
        # A 3-GW value missing on either side (no projection and no model
        # horizon) makes the gain unknown, not "0 minus the drop" or "the add
        # minus 0". A departed drop is the exception: his value really is 0.
        drop_next3_unknown = pd.isna(drop["next3_xp"]) and drop["status"] != "u"
        next3_gain = (None if pd.isna(fa["next3_xp"]) or drop_next3_unknown
                      else _v(fa["next3_xp"]) - _v(drop["next3_xp"]))
        # A drop with no ROS projection (valued by the match model only) has
        # an unknown season value: the season gain is unknown, not "add - 0".
        drop_ros_unknown = bool(pd.isna(drop["ros_adj"]))
        add_ros_unknown = bool(pd.isna(fa["ros_points"]))  # == model_only here
        season_unknown = model_only or drop_ros_unknown
        season_gain = 0.0 if season_unknown else _v(fa["ros_adj"]) - _v(drop["ros_adj"])
        short_gain = next1_gain if rank_by == "next1" else (next3_gain or 0.0)
        if short_gain <= 0 and season_gain <= 0:
            continue
        if short_gain > 0 and season_gain > 0:
            label = "upgrade"       # better now AND over the season: add and hold
        elif short_gain > 0:
            label = "stream"        # helps the next GW only: plan to re-drop
        else:
            label = "hold"          # tough fixtures now, better player long-term
        recs.append({
            "add": fa["web_name"], "add_element": int(fa["element"]),
            "drop_element": int(drop["element"]),
            "add_team": fa["team"], "position": fa["position"],
            "drop": drop["web_name"],
            "next1_gain": round(next1_gain, 2),
            # 1 decimal under the legacy ranking (its order is built on the
            # rounded value and must not move), 2 once model values are in.
            "next3_gain": (None if next3_gain is None
                           else round(next3_gain, 1 if rank_by == "legacy" else 2)),
            "season_gain": None if season_unknown else round(season_gain, 1),
            "season_unknown": season_unknown,
            "add_ros_unknown": add_ros_unknown,
            "drop_ros_unknown": drop_ros_unknown,
            "label": label,
            "add_xp_next": fa["xp_next"], "drop_xp_next": drop["xp_next"],
            "add_p_start": fa["p_start"], "add_xp_source": fa["xp_source"],
            "drivers": fa["drivers"], "opponents": fa["opponents"],
            "add_next3_xp": fa["next3_xp"], "add_next3_source": fa["next3_source"],
            "add_ros": fa["ros_points"],
            "drop_next3_xp": drop["next3_xp"], "drop_ros": drop["ros_points"],
            "add_ros_adj": _none_if_nan(fa["ros_adj"]),
            "drop_ros_adj": _none_if_nan(drop["ros_adj"]),
            "drop_status": drop["status"], "drop_xp_source": drop["xp_source"],
            "club_moved": _flag_or_none(fa["club_moved"]),
            "add_expected_minutes": _none_if_nan(fa["expected_minutes"]),
            "add_minutes_season": (None if pd.isna(fa["minutes_season"])
                                   else int(fa["minutes_season"])),
            "add_role_factor": float(fa["role_factor"]),
            "add_role_override": fa.get("role_override"),
            "availability": fa["status"], "news": fa["news"],
            "confidence": fa["confidence"],
        })
    for rec in recs:
        rec["gains"] = {"gw1": rec["next1_gain"], "gw3": rec["next3_gain"],
                        "ros": rec["season_gain"]}
    if rank_by == "legacy":
        recs.sort(key=lambda r: -max(r["next3_gain"], r["season_gain"]))
    elif rank_by == "next1":
        recs.sort(key=lambda r: (r["label"] == "hold", -r["next1_gain"],
                                 -(r["season_gain"] or 0.0)))
    elif rank_by == "next3":
        recs.sort(key=lambda r: (r["label"] == "hold", r["next3_gain"] is None,
                                 -(r["next3_gain"] or 0.0), -(r["season_gain"] or 0.0)))
    elif rank_by == "ros":
        recs.sort(key=lambda r: (-(r["season_gain"] or 0.0), r["next3_gain"] is None,
                                 -(r["next3_gain"] or 0.0)))
    else:
        raise ValueError(f"unknown rank_by {rank_by!r}")
    return recs


def diversify(recs: list[dict[str, Any]], top_n: int,
              per_drop: int = MAX_RECS_PER_DROP) -> list[dict[str, Any]]:
    """The first ``top_n`` of ranked ``recs`` with at most ``per_drop`` per
    drop player: a rec past its drop's cap is held back, and held recs only
    backfill (in rank order, after the capped picks) when fewer than
    ``top_n`` recs would remain. Rank 1 is always ``recs[0]``."""
    picked: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    per_drop_count: dict[int, int] = {}
    for rec in recs:
        if len(picked) == top_n:
            break
        count = per_drop_count.get(rec["drop_element"], 0)
        if count < per_drop:
            picked.append(rec)
            per_drop_count[rec["drop_element"]] = count + 1
        else:
            held.append(rec)
    return (picked + held)[:top_n]


def recommend(players: pd.DataFrame, squad: pd.DataFrame,
              top_n: int = 10, *, rank_by: str = "next1") -> list[dict[str, Any]]:
    """The overall top ``top_n`` of ``ranked_recs``, diversified by drop
    (``diversify``) except under ``legacy``, the heuristic scorer's frozen
    baseline, which stays a plain top N."""
    recs = ranked_recs(players, squad, rank_by=rank_by)
    return recs[:top_n] if rank_by == "legacy" else diversify(recs, top_n)


def best_by_position(recs: list[dict[str, Any]],
                     per_position: int = BEST_BY_POSITION_N) -> dict[str, list[dict[str, Any]]]:
    """The best ``per_position`` of ranked ``recs`` at each position (every
    position present, an empty list when nothing beats the drop there)."""
    out: dict[str, list[dict[str, Any]]] = {position: [] for position in POSITIONS.values()}
    for rec in recs:
        bucket = out[rec["position"]]
        if len(bucket) < per_position:
            bucket.append(rec)
    return out


def unprojected_squad(squad: pd.DataFrame, rank_by: str = "next1") -> list[dict[str, Any]]:
    """Squad players the ``rank_by`` ranking cannot value at all — no ROS
    projection AND no model value on any horizon (``valued_mask``) —
    surfaced for human judgment instead of being silently treated as
    droppable zeros. Exactly the non-departed players ``drop_order`` never
    returns, so a listed player is never the drop.

    A player with no next-GW xP (``xp_source == "none"``, e.g. a blank GW)
    but a model 3-GW value is valued, not listed. my_week calls this with
    the default: its table carries no model horizon, so there it means
    ``xp_source == "none"`` — the same test as its "no value" warning."""
    rows = squad[~valued_mask(squad, rank_by)]
    return [{
        "web_name": p["web_name"], "position": p["position"], "team": p["team"],
        "availability": p["status"], "news": p["news"],
    } for _, p in rows.iterrows()]


def plan(bootstrap: dict, element_status: dict, seasons: pd.DataFrame,
         projections_path: Path, entry_id: int, top_n: int = 10, *,
         fixtures_by_event: dict | None = None,
         neutral_availability: bool = False,
         gw_xp: pd.DataFrame | None = None,
         season_panel: pd.DataFrame | None = None,
         role_overrides: list[dict] | None = None,
         never_drop: list[dict] | None = None,
         horizon_xp: pd.DataFrame | None = None,
         horizon: str = DEFAULT_HORIZON,
         prior_season: str = PRIOR_SEASON) -> dict[str, Any]:
    """Pure waiver plan: players table, my squad, best-XI xP, ranked recs.

    ``gw_xp`` (optional) is the match model's next-GW frame; see
    ``build_player_table``. Without it the heuristic scorer applies and
    ``recommend`` keeps its pre-xP ranking (``rank_by="legacy"``).
    ``horizon_xp`` (optional) is the model's 3-GW frame and ``horizon`` the
    requested ranking key (``HORIZONS``); ``rank_key`` turns the three into
    the ``rank_by`` actually used, returned under that name. Without
    ``gw_xp`` the horizon frame is ignored: the heuristic scorer never mixes
    model values in.
    ``season_panel`` (optional) switches on the club-move role signals.
    ``role_overrides`` (optional, entries of ``role_overrides.json``) are
    applied for the bootstrap's next event; see ``apply_role_overrides``.
    ``never_drop`` (optional, entries of ``squad_prefs.json``) protects squad
    players from being a drop pick or candidate; see ``apply_never_drop``.

    Free agents are the element-status rows with no owner. Returns
    ``players``, ``squad`` (DataFrames), ``xi_next3_xp``, ``rank_by``,
    ``recommendations``, ``best_by_position``, ``drop_candidates``,
    ``unprojected_squad``, the
    ``OVERRIDE_REPORT_KEYS`` and ``NEVER_DROP_REPORT_KEYS`` name lists and ``xp_reconciled`` (how many model
    rows were zeroed because the player is now ruled out; see
    ``build_player_table``). No I/O beyond reading ``projections_path``.
    """
    if gw_xp is None:
        horizon_xp = None
    players = build_player_table(
        bootstrap, seasons, projections_path,
        fixtures_by_event=fixtures_by_event, neutral_availability=neutral_availability,
        gw_xp=gw_xp, season_panel=season_panel, horizon_xp=horizon_xp,
        prior_season=prior_season)
    players, override_report = apply_role_overrides(
        players, role_overrides or [], next_event(bootstrap), event_deadlines(bootstrap),
        horizon_xp)
    players, never_drop_report = apply_never_drop(
        players, never_drop or [], next_event(bootstrap))
    free = {row["element"] for row in element_status.get("element_status", [])
            if row.get("owner") is None}
    players["is_free_agent"] = players["element"].isin(free)
    squad = my_squad(players, element_status, entry_id)
    _, xi_total = best_xi(squad)
    rank_by = rank_key(horizon, gw_xp, horizon_xp)
    ranked = ranked_recs(players, squad, rank_by=rank_by)
    return {
        "players": players, "squad": squad, "xi_next3_xp": xi_total, "rank_by": rank_by,
        "recommendations": (ranked[:top_n] if rank_by == "legacy"
                            else diversify(ranked, top_n)),
        "best_by_position": best_by_position(ranked),
        "drop_candidates": drop_candidates(squad, rank_by),
        "unprojected_squad": unprojected_squad(squad, rank_by),
        **override_report, **never_drop_report,
        "xp_reconciled": int(players["xp_reconciled"].sum()),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(_repo_root() / ".env")
    parser = argparse.ArgumentParser(description="waiver_plan: roster-aware add/drop recommendations")
    parser.add_argument("--season", default="2026-27")
    parser.add_argument("--league", type=int, default=int(os.getenv("LEAGUE_ID", "0") or "0"))
    parser.add_argument("--entry", type=int, default=int(os.getenv("ENTRY_ID", "0") or "0"))
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--scorer", choices=SCORERS, default=DEFAULT_SCORER,
                        help="next-GW xP source: match model parquet or the ros/38 heuristic")
    parser.add_argument("--horizon", choices=HORIZONS, default=DEFAULT_HORIZON,
                        help="gain the model scorer ranks on: next GW (1), next 3 GWs (3) "
                             "or rest of season (ros); ignored by the heuristic scorer")
    parser.add_argument("--data-root", type=Path, default=_repo_root() / "data")
    parser.add_argument("--out", type=Path, default=None,
                        help="output JSON (default <data-root>/derived/<season>/ml/waiver_plan.json)")
    args = parser.parse_args(argv)
    load_dotenv(args.data_root.parent / ".env")
    league = args.league or int(os.getenv("LEAGUE_ID", "0") or "0")
    entry = args.entry or int(os.getenv("ENTRY_ID", "0") or "0")
    if not league or not entry:
        parser.error("--league and --entry required (or set LEAGUE_ID / ENTRY_ID)")

    try:
        projections_path = paths.projections_path(args.season, args.data_root)
        prior_season = paths.prior_season(args.season)
    except (FileNotFoundError, ValueError) as exc:
        parser.exit(2, f"{parser.prog}: error: {exc}\n")  # one line, no usage dump
    raw_root = paths.raw_root(args.season, args.data_root)
    bootstrap = json.loads((raw_root / "bootstrap/bootstrap-static.json").read_text(encoding="utf-8"))
    element_status = json.loads(
        (raw_root / f"league/{league}/element-status.json").read_text(encoding="utf-8"))
    seasons = pd.read_parquet(paths.seasons_table_path(args.data_root))
    ml_dir = paths.derived_root(args.season, args.data_root) / "ml"

    gw_xp, scorer_meta = resolve_scorer(args.scorer, bootstrap, ml_dir)
    horizon_xp, horizon_meta = resolve_horizon(args.horizon, gw_xp, bootstrap, ml_dir)
    season_panel = load_season_panel(ml_dir / "player_gameweeks.parquet", args.season)
    minutes_through_gw = (int(season_panel["gw"].max())
                          if season_panel is not None and not season_panel.empty else None)
    result = plan(bootstrap, element_status, seasons, projections_path,
                  entry, args.top, gw_xp=gw_xp, season_panel=season_panel,
                  role_overrides=load_role_overrides(ml_dir / "role_overrides.json"),
                  never_drop=load_squad_prefs(ml_dir / "squad_prefs.json"),
                  horizon_xp=horizon_xp, horizon=args.horizon, prior_season=prior_season)
    squad = result["squad"]
    xi, xi_total = best_xi(squad)
    bench = squad[~squad["element"].isin(xi["element"])]

    def _fmt(x):
        return "    ?" if pd.isna(x) else f"{x:>5}"

    print(f"\n== MY SQUAD — best XI, next {NEXT_GWS} GWs (xP {xi_total}) ==")
    for _, p in xi.sort_values(["position", "next3_xp"], ascending=[True, False]).iterrows():
        flag = f"  [{p['status']}] {p['news']}" if p["status"] != "a" else ""
        print(f"  {p['position']:<4} {p['web_name']:<20} {p['team']:<4} "
              f"next3 {_fmt(p['next3_xp'])} | ROS {_fmt(p['ros_points'])}{flag}")
    print("-- bench --")
    for _, p in bench.sort_values("next3_xp", ascending=False).iterrows():
        flag = f"  [{p['status']}] {p['news']}" if p["status"] != "a" else ""
        print(f"  {p['position']:<4} {p['web_name']:<20} {p['team']:<4} "
              f"next3 {_fmt(p['next3_xp'])} | ROS {_fmt(p['ros_points'])}{flag}")

    unknown = result["unprojected_squad"]
    if unknown:
        print("\n== UNPROJECTED SQUAD PLAYERS (model has no value — judge manually) ==")
        for p in unknown:
            flag = f"  [{p['availability']}] {p['news']}" if p["availability"] != "a" else ""
            print(f"  {p['position']:<4} {p['web_name']:<20} {p['team']:<4}"
                  f" use player_card for their history{flag}")

    overrides = {key: result[key] for key in OVERRIDE_REPORT_KEYS}
    print("\n== ROLE OVERRIDES (role_overrides.json; next GW, and the model's 3-GW "
          "value for the events an entry covers) ==")
    for key, names in overrides.items():
        print(f"  {key}: {', '.join(names) or '-'}")

    never_drop = {key: result[key] for key in NEVER_DROP_REPORT_KEYS}
    print("\n== NEVER DROP (squad_prefs.json; never a drop pick or candidate) ==")
    for key, names in never_drop.items():
        print(f"  {key}: {', '.join(names) or '-'}")

    def _num(x, width=6):
        return f"{'?':>{width}}" if x is None else f"{x:>{width}}"

    def _moved(x):
        return "?" if x is None else ("yes" if x else "no")

    print(f"\n== DROP CANDIDATES (drop_candidates: top {DROP_CANDIDATES_PER_POSITION} per "
          f"position in drop order for {result['rank_by']}; minutes through GW{minutes_through_gw}) ==")
    print("   pos  player               team st  ros_adj    ROS xp_next  next3 moved exp_min")
    for c in result["drop_candidates"]:
        print(f"  {c['position']:<4} {c['web_name']:<20} {c['team']:<4} {c['status']:<2} "
              f"{_num(c['ros_adj'], 8)} {_num(c['ros_points'])} {_num(c['xp_next'], 7)} "
              f"{_num(c['next3_xp'])} "
              f"{_moved(c['club_moved']):<5} {_num(c['expected_minutes'], 7)}")

    recs = result["recommendations"]
    fallback = (f" — FALLBACK: {scorer_meta['xp_fallback_reason']}"
                if scorer_meta["xp_fallback"] else "")
    if horizon_meta["horizon_fallback"]:
        fallback += f" — HORIZON FALLBACK: {horizon_meta['horizon_fallback_reason']}"
    diversified = "" if result["rank_by"] == "legacy" else f", max {MAX_RECS_PER_DROP} per drop"
    print(f"\n== WAIVER RECOMMENDATIONS (top {args.top}{diversified}, scorer "
          f"{scorer_meta['scorer']}, ranked by {result['rank_by']}{fallback}) ==")
    rec_header = ("   label    add                    ->  drop                  gw1    gw3      ros"
                  "  src1/src3            exp_min moved")

    def _print_rec(r):
        flag = f"  [{r['availability']}] {r['news']}" if r["availability"] != "a" else ""
        if r["add_role_override"] is not None:
            flag += f"  (override: {r['add_role_override']})"
        season = "?" if r["season_unknown"] else f"{r['season_gain']:+.1f}"
        next3 = "?" if r["next3_gain"] is None else f"{r['next3_gain']:+.2f}"
        sources = f"{r['add_xp_source']}/{r['add_next3_source']}"
        print(f"  {r['label']:<8} {r['add']:<18}({r['position']}) -> {r['drop']:<18} "
              f"{r['next1_gain']:>+6.2f} {next3:>6} {season:>8}"
              f"  {sources:<19} {_num(r['add_expected_minutes'], 8)} "
              f"{_moved(r['club_moved'])}{flag}")

    print(rec_header)
    for r in recs:
        _print_rec(r)

    print(f"\n== BEST BY POSITION (best_by_position: top {BEST_BY_POSITION_N} per position, "
          f"each vs that position's drop pick) ==")
    print(rec_header)
    for position, position_recs in result["best_by_position"].items():
        if not position_recs:
            print(f"  {position}: no swap beats the drop pick")
        for r in position_recs:
            _print_rec(r)

    out = args.out or ml_dir / "waiver_plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(jsonutil.dumps_strict(
        {**scorer_meta, **horizon_meta, "rank_by": result["rank_by"],
         "xi_next3_xp": xi_total, "recommendations": recs,
         "max_recs_per_drop": None if result["rank_by"] == "legacy" else MAX_RECS_PER_DROP,
         "best_by_position": result["best_by_position"],
         "drop_candidates": result["drop_candidates"],
         "minutes_through_gw": minutes_through_gw, **overrides, **never_drop,
         "unprojected_squad": unknown, "xp_reconciled": result["xp_reconciled"]}, indent=1))
    logger.info("wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
