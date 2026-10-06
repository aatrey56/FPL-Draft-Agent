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
* **The short-vs-long balance is explicit**: every candidate shows
  ``next1_gain`` (next-GW xP vs the drop candidate), ``next3_gain`` (heuristic
  3-GW points) AND ``season_gain`` (rest-of-season vs the same drop). Ranking
  is by ``next1_gain``. Labels: ``upgrade`` (next1 and season positive — add
  and hold), ``stream`` (helps next GW only — plan to re-drop), ``hold``
  (no next-GW gain, better over the season — patience play); holds are listed
  after every positive-next1 recommendation. A free agent with no ROS
  projection but a model ``xp_next`` (e.g. a promoted club's starter) is still
  ranked, with ``season_gain`` and ``add_ros`` null and ``season_unknown``
  true; his season gain counts as 0 for the label and ordering, so he is at
  most a ``stream``. His heuristic 3-GW value does not exist either, so
  ``add_next3_xp`` and ``next3_gain`` are null too (unknown, never "0 minus
  the drop"). With no match xP supplied (the
  heuristic scorer) ``recommend`` keeps its original ranking (``rank_by=
  "legacy"``: labels from ``next3_gain``, ordered by the larger gain) so the
  heuristic output is unchanged.
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
* **Departed squad players** (status ``u``) have ``ros_adj`` 0 and are
  always the drop at their position — sort key ``(status != "u", ros_adj,
  xp_next)`` — with or without a projection. ``drop_candidates`` lists the
  top ``DROP_CANDIDATES_PER_POSITION`` per position so the pick is auditable.
* **Role overrides** — ``data/derived/<season>/ml/role_overrides.json``
  (hand-maintained team news, optional) is applied to the next GW only: see
  ``apply_role_overrides``. An entry with neither ``return_gw`` nor
  ``valid_through_gw`` goes stale after the first deadline following its
  ``as_of`` (file ``updated`` / mtime fallback). An override never lifts the availability gate: a
  player the live feed rules out (availability 0) is ``overrides_blocked``.
  ``overrides_applied`` / ``overrides_unmatched`` / ``overrides_expired`` /
  ``overrides_stale`` / ``overrides_blocked`` in the output say what happened to every entry.

CLI: python -m backend.ml.waiver --league <id> --entry <id> [--scorer {heuristic,model}]
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

from backend.ml import jsonutil
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
# The season the projections and team strengths were measured on.
PRIOR_SEASON = "2025-26"
# Role signals for club-movers (see module docstring). A player averaging
# ROLE_MINUTES_FULL minutes over the last ROLE_MINUTES_WINDOW finished GWs
# keeps his full projection; below that it scales down linearly, never under
# ROLE_FLOOR (a 0-minute signing still has some chance of winning the role).
ROLE_MINUTES_WINDOW = 5
ROLE_MINUTES_FULL = 60.0
ROLE_FLOOR = 0.15
DROP_CANDIDATES_PER_POSITION = 3
# What ``apply_role_overrides`` did with each role_overrides.json entry, in
# the order the output JSON and CLI list them.
OVERRIDE_REPORT_KEYS = ("overrides_applied", "overrides_unmatched", "overrides_expired",
                        "overrides_stale", "overrides_blocked")


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

def team_strengths(seasons: pd.DataFrame, teams: list[dict]) -> dict[int, float]:
    """26/27 team id -> strength score (sum of that club's player points in
    25/26). Promoted/unseen clubs get a weak prior: 90% of the minimum."""
    last = seasons[seasons["season"] == PRIOR_SEASON]
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


def read_gw_xp(path: Path, expected_gw: int | None,
               last_finished: int | None = None) -> tuple[pd.DataFrame | None, str | None]:
    """Read the match model's ``xp_gw{N}.parquet``: ``(frame, None)`` when it
    is usable for ``expected_gw``, else ``(None, reason)`` — the reason is
    written to the artifact so a fallback is never silent.

    ``last_finished`` is the last finished gameweek in the bootstrap (0 before
    GW1 finishes); the file must have been trained on a panel through exactly
    that GW. None means "assume ``expected_gw - 1``" (the between-gameweeks
    case). Mid-gameweek — GW N-1 in play, planning for N — the freshest panel
    ends at N-2, which is what ``last_finished`` is then, so the file is
    served from the deadline onward instead of being rejected until GW N-1
    finishes.
    """
    path = Path(path)
    if not path.exists():
        return None, f"match xP file {path.name} missing"
    try:
        frame = pd.read_parquet(path)
    except (OSError, ValueError) as exc:   # pyarrow's ArrowInvalid is a ValueError
        return None, (f"match xP file {path.name} is unreadable "
                      f"({type(exc).__name__}: {exc}); rebuild it")
    gws = sorted(int(g) for g in frame["gw"].unique()) if "gw" in frame else []
    if expected_gw is None or gws != [expected_gw]:
        return None, f"match xP file {path.name} is for gw {gws}, expected {expected_gw} (stale)"
    if "panel_max_gw" not in frame:
        return None, (f"match xP file {path.name} has no panel_max_gw "
                      "(built before the panel check; rebuild it)")
    required = expected_gw - 1 if last_finished is None else last_finished
    panel_gws = sorted(int(g) for g in frame["panel_max_gw"].unique())
    if panel_gws != [required]:
        return None, (f"match xP file {path.name} was trained on a panel through gw "
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


def build_player_table(bootstrap: dict, seasons: pd.DataFrame,
                       projections_path: Path, *,
                       fixtures_by_event: dict | None = None,
                       neutral_availability: bool = False,
                       gw_xp: pd.DataFrame | None = None,
                       season_panel: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per 26/27 element: identity, availability, xP, ROS value.

    ``fixtures_by_event`` overrides the bootstrap schedule (default:
    ``upcoming_fixtures``, i.e. from ``next_event`` onward) and
    ``neutral_availability`` forces availability to 1.0, except 0.0 for
    departed (status "u") players (both used by the
    replay harness, where only the current status/news snapshot exists).

    ``gw_xp`` is the match model's one-row-per-code next-GW frame (see
    ``matchmodel.build_gw_xp``). Joined on the permanent ``code``, it supplies
    ``xp_next, p_start, xp_floor, xp_ceiling, drivers, opponents`` with
    ``xp_source == "model"``. A player it does not cover — no row, or a row
    without a prediction (``usable_gw_xp``) — gets the heuristic
    1-GW value (``ros/38 x next-event fixture load x availability``,
    source ``heuristic``), or ``none`` when there is no projection either.
    ``next3_xp`` stays heuristic (already availability-gated).

    Availability reconciliation: the model row's Stage-1 gate used the
    bootstrap from when the xP file was built. When the CURRENT availability
    is 0 (status u/i/s without a chance, or chance 0) a model row's
    ``xp_next``, ``p_start``, ``xp_floor`` and ``xp_ceiling`` are set to 0
    and ``xp_reconciled`` is True (False on every other row). Partial
    availability changes (e.g. a new 50% doubt) are not reconciled.

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
    strengths = team_strengths(seasons, bootstrap.get("teams", []))
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
    projections = {}
    if Path(projections_path).exists():
        projections = {int(r["code"]): r for r in
                       json.loads(Path(projections_path).read_text(encoding="utf-8"))}
    else:
        logger.warning("projections missing at %s — values will be null", projections_path)

    team_names = {t["id"]: t.get("short_name") or t.get("name")
                  for t in bootstrap.get("teams", [])}
    moves = club_moves(bootstrap, seasons)
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
        xp_started = full_load1
        if model is not None and pd.notna(model.get("xp_started")):
            xp_started = float(model["xp_started"])
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
        else:
            xp_extra = {"p_start": None, "xp_floor": None, "xp_ceiling": None,
                        "drivers": None, "opponents": None}
            if full_load1 is not None:
                xp_next = full_load1 * avail
                xp_source = "heuristic"
            else:
                xp_next, xp_source = None, "none"
        rows.append({
            "element": el["id"], "code": el.get("code"),
            "web_name": el.get("web_name"),
            "position": POSITIONS.get(el.get("element_type")),
            "team": team_names.get(el.get("team")),
            "availability": avail,
            "status": el.get("status"), "news": el.get("news") or "",
            "next3_xp": round(next3, 1) if next3 is not None else None,
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


def apply_role_overrides(players: pd.DataFrame, overrides: list[dict],
                         target_gw: int | None,
                         deadlines: dict[int, datetime] | None = None,
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
      null ``xp_next``. ``next3_xp`` and ROS are not touched.
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
        if code is not None:
            matched = out.index[pd.to_numeric(out["code"], errors="coerce") == code]
        else:
            matched = out.index[(out["web_name"] == entry.get("player"))
                                & (out["team"] == entry.get("team"))]
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
            out.at[row, "role_override_p_start"] = p_override
            out.at[row, "p_start"] = p_override
            out.at[row, "xp_floor"] = out.at[row, "xp_ceiling"] = float("nan")
            if pd.notna(out.at[row, "xp_started"]):
                out.at[row, "xp_next"] = round(p_override * float(out.at[row, "xp_started"]), 2)
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


def _drop_tiebreak(rank_by: str) -> str:
    """Third drop-order key: the short-horizon value the ranking is built on."""
    return "next3_xp" if rank_by == "legacy" else "xp_next"


def drop_order(squad: pd.DataFrame, tiebreak: str = "xp_next") -> pd.DataFrame:
    """Squad players who may be dropped, most droppable first.

    Sort key ``(status != "u", ros_adj, tiebreak)``: a departed player is
    always first, then the lowest role-adjusted season value. Only players
    with a ROS projection are droppable — an unprojected teammate is unknown,
    not worthless — except departed ones, who are droppable regardless.
    """
    droppable = squad[squad["ros_points"].notna() | (squad["status"] == "u")]
    return (droppable.assign(_kept=droppable["status"] != "u")
            .sort_values(["_kept", "ros_adj", tiebreak])
            .drop(columns="_kept"))


def drop_candidates(squad: pd.DataFrame, tiebreak: str = "xp_next",
                    per_position: int = DROP_CANDIDATES_PER_POSITION) -> list[dict[str, Any]]:
    """The first ``per_position`` of ``drop_order`` at each position, so the
    drop pick behind every recommendation can be audited (and overruled)."""
    rows = []
    for position in POSITIONS.values():
        ordered = drop_order(squad[squad["position"] == position], tiebreak)
        for _, p in ordered.head(per_position).iterrows():
            rows.append({
                "web_name": p["web_name"], "position": position, "team": p["team"],
                "status": p["status"],
                "ros_points": _none_if_nan(p["ros_points"]), "ros_adj": _none_if_nan(p["ros_adj"]),
                "xp_next": _none_if_nan(p["xp_next"]),
                "club_moved": _flag_or_none(p["club_moved"]),
                "expected_minutes": _none_if_nan(p["expected_minutes"]),
            })
    return rows


def recommend(players: pd.DataFrame, squad: pd.DataFrame,
              top_n: int = 10, *, rank_by: str = "next1") -> list[dict[str, Any]]:
    """Ranked add/drop pairs with the short-vs-long balance made explicit.

    For each free agent, pair with the first same-position player of
    ``drop_order`` (a departed squad player, else the lowest ``ros_adj``).
    ``next1_gain`` = next-GW xP of the add minus the drop, ``season_gain`` =
    ``ros_adj`` difference (role-adjusted ROS; a departed drop counts as 0).
    Each rec carries the add's role signals (``club_moved``,
    ``add_expected_minutes``, ``add_role_factor``, ``add_ros_adj``) next to
    the raw ``add_ros``. Ranking is by ``next1_gain`` descending;
    ``hold`` recs (no next-GW gain, better over the season) come after every
    positive-next1 rec, ordered by ``season_gain``. The label encodes the
    balance so a streamer is never confused with a season upgrade. A free
    agent without a ROS projection is ranked only when the match model
    values him (``xp_source == "model"``); his ``season_gain`` is emitted as
    None with ``season_unknown`` True (every other rec carries False) and
    counts as 0 for the label and the ordering; his ``next3_gain`` is None
    as well, because the 3-GW value is built from the missing projection.

    ``rank_by="legacy"`` keeps the pre-xP behaviour for the heuristic scorer
    (labels from ``next3_gain``, ranked by the larger of ``next3_gain`` and
    ``season_gain``); ``plan`` selects it whenever no match xP is supplied.
    """
    # Free agents by definition have no owner, so my squad is already excluded.
    pool = players[players["is_free_agent"] & (players["status"] != "u")]
    # Drop candidates come only from players the model can value. An
    # unprojected teammate (under the minutes floor last season, promoted,
    # or newly signed) is NOT worth zero — it is unknown, and auto-dropping
    # a returning star on missing data is the one unrecoverable mistake.
    # Those players go in the plan's unprojected_squad section instead.
    # (A departed teammate is the exception: see drop_order.)
    droppable = {position: drop_order(squad[squad["position"] == position],
                                      _drop_tiebreak(rank_by))
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
                      and fa["xp_source"] == "model")
        if mine.empty or (pd.isna(fa["ros_points"]) and not model_only):
            continue  # cannot recommend a player we cannot value
        drop = mine.iloc[0]

        def _v(x):
            return 0.0 if pd.isna(x) else float(x)

        next1_gain = _v(fa["xp_next"]) - _v(drop["xp_next"])
        # next3_xp is heuristic (ros/38 based), so a model-only add has none:
        # the gain is unknown, not "0 minus the drop".
        next3_gain = None if model_only else _v(fa["next3_xp"]) - _v(drop["next3_xp"])
        season_gain = 0.0 if model_only else _v(fa["ros_adj"]) - _v(drop["ros_adj"])
        short_gain = next3_gain if rank_by == "legacy" else next1_gain
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
            "next3_gain": None if next3_gain is None else round(next3_gain, 1),
            "season_gain": None if model_only else round(season_gain, 1),
            "season_unknown": model_only,
            "label": label,
            "add_xp_next": fa["xp_next"], "drop_xp_next": drop["xp_next"],
            "add_p_start": fa["p_start"], "add_xp_source": fa["xp_source"],
            "drivers": fa["drivers"], "opponents": fa["opponents"],
            "add_next3_xp": fa["next3_xp"], "add_ros": fa["ros_points"],
            "drop_next3_xp": drop["next3_xp"], "drop_ros": drop["ros_points"],
            "add_ros_adj": _none_if_nan(fa["ros_adj"]),
            "drop_ros_adj": _none_if_nan(drop["ros_adj"]),
            "drop_status": drop["status"],
            "club_moved": _flag_or_none(fa["club_moved"]),
            "add_expected_minutes": _none_if_nan(fa["expected_minutes"]),
            "add_minutes_season": (None if pd.isna(fa["minutes_season"])
                                   else int(fa["minutes_season"])),
            "add_role_factor": float(fa["role_factor"]),
            "add_role_override": fa.get("role_override"),
            "availability": fa["status"], "news": fa["news"],
            "confidence": fa["confidence"],
        })
    if rank_by == "legacy":
        recs.sort(key=lambda r: -max(r["next3_gain"], r["season_gain"]))
    else:
        recs.sort(key=lambda r: (r["label"] == "hold", -r["next1_gain"],
                                 -(r["season_gain"] or 0.0)))
    return recs[:top_n]


def unprojected_squad(squad: pd.DataFrame) -> list[dict[str, Any]]:
    """Squad players with no value at all — no ROS projection AND no match
    xP (``xp_source == "none"``) — surfaced for human judgment instead of
    being silently treated as droppable zeros. Same definition as my_week's.

    A player with model xP but no ROS projection is valued for the next GW
    and so is not listed here; he is still never the drop candidate
    (``recommend`` only drops players with a ROS projection)."""
    rows = squad[squad["xp_source"] == "none"]
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
         role_overrides: list[dict] | None = None) -> dict[str, Any]:
    """Pure waiver plan: players table, my squad, best-XI xP, ranked recs.

    ``gw_xp`` (optional) is the match model's next-GW frame; see
    ``build_player_table``. Without it the heuristic scorer applies and
    ``recommend`` keeps its pre-xP ranking (``rank_by="legacy"``).
    ``season_panel`` (optional) switches on the club-move role signals.
    ``role_overrides`` (optional, entries of ``role_overrides.json``) are
    applied for the bootstrap's next event; see ``apply_role_overrides``.

    Free agents are the element-status rows with no owner. Returns
    ``players``, ``squad`` (DataFrames), ``xi_next3_xp``, ``recommendations``,
    ``drop_candidates``, ``unprojected_squad``, the ``OVERRIDE_REPORT_KEYS``
    name lists and ``xp_reconciled`` (how many model rows were zeroed because
    the player is now ruled out; see ``build_player_table``). No I/O beyond
    reading ``projections_path``.
    """
    players = build_player_table(
        bootstrap, seasons, projections_path,
        fixtures_by_event=fixtures_by_event, neutral_availability=neutral_availability,
        gw_xp=gw_xp, season_panel=season_panel)
    players, override_report = apply_role_overrides(
        players, role_overrides or [], next_event(bootstrap), event_deadlines(bootstrap))
    free = {row["element"] for row in element_status.get("element_status", [])
            if row.get("owner") is None}
    players["is_free_agent"] = players["element"].isin(free)
    squad = my_squad(players, element_status, entry_id)
    _, xi_total = best_xi(squad)
    rank_by = "next1" if gw_xp is not None else "legacy"
    return {
        "players": players, "squad": squad, "xi_next3_xp": xi_total,
        "recommendations": recommend(players, squad, top_n, rank_by=rank_by),
        "drop_candidates": drop_candidates(squad, _drop_tiebreak(rank_by)),
        "unprojected_squad": unprojected_squad(squad),
        **override_report,
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
    parser.add_argument("--data-root", type=Path, default=_repo_root() / "data")
    parser.add_argument("--out", type=Path, default=None,
                        help="output JSON (default <data-root>/derived/<season>/ml/waiver_plan.json)")
    args = parser.parse_args(argv)
    load_dotenv(args.data_root.parent / ".env")
    league = args.league or int(os.getenv("LEAGUE_ID", "0") or "0")
    entry = args.entry or int(os.getenv("ENTRY_ID", "0") or "0")
    if not league or not entry:
        parser.error("--league and --entry required (or set LEAGUE_ID / ENTRY_ID)")

    raw_root = args.data_root / "raw" / args.season
    bootstrap = json.loads((raw_root / "bootstrap/bootstrap-static.json").read_text(encoding="utf-8"))
    element_status = json.loads(
        (raw_root / f"league/{league}/element-status.json").read_text(encoding="utf-8"))
    seasons = pd.read_parquet(args.data_root / "derived/ml/player_seasons.parquet")
    ml_dir = args.data_root / "derived" / args.season / "ml"

    gw_xp, scorer_meta = resolve_scorer(args.scorer, bootstrap, ml_dir)
    season_panel = load_season_panel(ml_dir / "player_gameweeks.parquet", args.season)
    minutes_through_gw = (int(season_panel["gw"].max())
                          if season_panel is not None and not season_panel.empty else None)
    result = plan(bootstrap, element_status, seasons,
                  args.data_root / "derived/ml/projections_2627.json",
                  entry, args.top, gw_xp=gw_xp, season_panel=season_panel,
                  role_overrides=load_role_overrides(ml_dir / "role_overrides.json"))
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
    print("\n== ROLE OVERRIDES (role_overrides.json, next GW only) ==")
    for key, names in overrides.items():
        print(f"  {key}: {', '.join(names) or '-'}")

    def _num(x, width=6):
        return f"{'?':>{width}}" if x is None else f"{x:>{width}}"

    def _moved(x):
        return "?" if x is None else ("yes" if x else "no")

    print(f"\n== DROP CANDIDATES (drop_candidates: top {DROP_CANDIDATES_PER_POSITION} per "
          f"position; minutes through GW{minutes_through_gw}) ==")
    print("   pos  player               team st  ros_adj    ROS xp_next moved exp_min")
    for c in result["drop_candidates"]:
        print(f"  {c['position']:<4} {c['web_name']:<20} {c['team']:<4} {c['status']:<2} "
              f"{_num(c['ros_adj'], 8)} {_num(c['ros_points'])} {_num(c['xp_next'], 7)} "
              f"{_moved(c['club_moved']):<5} {_num(c['expected_minutes'], 7)}")

    recs = result["recommendations"]
    fallback = (f" — FALLBACK: {scorer_meta['xp_fallback_reason']}"
                if scorer_meta["xp_fallback"] else "")
    print(f"\n== WAIVER RECOMMENDATIONS (top {args.top}, scorer {scorer_meta['scorer']}{fallback}) ==")
    print("   label    add                    ->  drop                next1  next3   season"
          "  src        exp_min moved")
    for r in recs:
        flag = f"  [{r['availability']}] {r['news']}" if r["availability"] != "a" else ""
        if r["add_role_override"] is not None:
            flag += f"  (override: {r['add_role_override']})"
        season = "?" if r["season_unknown"] else f"{r['season_gain']:+.1f}"
        next3 = "?" if r["next3_gain"] is None else f"{r['next3_gain']:+.1f}"
        print(f"  {r['label']:<8} {r['add']:<18}({r['position']}) -> {r['drop']:<18} "
              f"{r['next1_gain']:>+6.2f} {next3:>6} {season:>7}"
              f"  {r['add_xp_source']:<9} {_num(r['add_expected_minutes'], 8)} "
              f"{_moved(r['club_moved'])}{flag}")

    out = args.out or ml_dir / "waiver_plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(jsonutil.dumps_strict(
        {**scorer_meta, "xi_next3_xp": xi_total, "recommendations": recs,
         "drop_candidates": result["drop_candidates"],
         "minutes_through_gw": minutes_through_gw, **overrides,
         "unprojected_squad": unknown, "xp_reconciled": result["xp_reconciled"]}, indent=1))
    logger.info("wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
