"""Lineup optimiser — formation, XI, auto-sub bench order and if-out fallbacks.

FPL Draft rules: an XI is 1 GKP + 10 outfielders with DEF 3-5, MID 2-5 and
FWD 1-3 (eight formations). Auto-subs replace a starter who does not play
with the first bench outfielder whose arrival keeps the formation legal; the
reserve GKP only ever replaces the starting GKP.

* ``optimal_xi`` — for each legal formation take the top-n per position by
  ``value_col`` (optimal within that formation, since positions are
  independent) and keep the formation with the highest total. Formations are
  tried in ``waiver.FORMATIONS`` order first and a later one must be strictly
  better, so on ties the pick matches the old ``waiver.best_xi``.
* Bench order — the reserve GKP first (FPL's bench slot 1, which only covers
  the keeper), then outfielders by *substitute value*:
  ``gw_xp x P(at least one starter he could legally replace misses)``, with
  that probability ``1 - prod(p_start)`` over those starters. It deliberately
  ignores the sub's slot in the queue (an earlier sub may use up the miss):
  simple and monotone, which is what ordering four players needs.
* ``if_out`` — for each doubtful starter (status ``d``, or p_start below
  ``DOUBT_P_START`` with an availability flag or a role override) the best XI
  with him removed, and who comes in.

Inputs are a squad frame with ``element``, ``web_name``, ``position``,
``gw_xp``, ``p_start``, ``status`` (``role_override`` optional) plus the
selection column. Pure: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from backend.ml import waiver as wv

POSITION_LIMITS = {"DEF": (3, 5), "MID": (2, 5), "FWD": (1, 3)}
# Every legal (DEF, MID, FWD): the waiver order first so ties keep the old
# best_xi pick, then any it never listed (5-2-3).
FORMATIONS = list(wv.FORMATIONS) + sorted(
    {(d, 10 - d - f, f) for d in range(3, 6) for f in range(1, 4)
     if 2 <= 10 - d - f <= 5} - set(wv.FORMATIONS))
# A starter below this start chance is "doubtful" when something flags him.
DOUBT_P_START = 0.5


class LineupError(ValueError):
    """The squad cannot field any legal XI (e.g. fewer than 3 DEF in it)."""


@dataclass
class Lineup:
    """An optimised XI: the starters, formation label, total and bench."""

    xi: pd.DataFrame
    formation: str | None
    total: float
    bench: pd.DataFrame = field(default_factory=pd.DataFrame)


def _p_start(row: pd.Series) -> float:
    """Start chance used for auto-sub odds: 0 when unavailable, unknown = 1."""
    if row.get("availability") == 0:
        return 0.0
    value = row.get("p_start")
    return 1.0 if value is None or pd.isna(value) else float(value)


def _xp(row: pd.Series) -> float:
    value = row.get("gw_xp")
    return 0.0 if value is None or pd.isna(value) else max(float(value), 0.0)


def _can_replace(sub_pos: str, starter_pos: str, counts: dict[str, int]) -> bool:
    """Whether ``sub_pos`` coming on for ``starter_pos`` keeps the XI legal."""
    if sub_pos == starter_pos:
        return True
    if "GKP" in (sub_pos, starter_pos):
        return False
    return (counts[starter_pos] - 1 >= POSITION_LIMITS[starter_pos][0]
            and counts[sub_pos] + 1 <= POSITION_LIMITS[sub_pos][1])


def substitute_value(sub: pd.Series, xi: pd.DataFrame) -> float:
    """Expected contribution of one bench outfielder (see module docstring)."""
    counts = xi["position"].value_counts().to_dict()
    for pos in POSITION_LIMITS:
        counts.setdefault(pos, 0)
    all_play = 1.0
    for _, starter in xi.iterrows():
        if _can_replace(sub["position"], starter["position"], counts):
            all_play *= _p_start(starter)
    return _xp(sub) * (1.0 - all_play)


def bench_order(bench: pd.DataFrame, xi: pd.DataFrame) -> pd.DataFrame:
    """Bench in auto-sub order: reserve GKP, then outfielders by
    ``substitute_value`` (ties: higher gw_xp, then lower element id)."""
    scored = bench.assign(
        sub_value=[round(substitute_value(row, xi), 3) for _, row in bench.iterrows()],
        _gk=bench["position"] != "GKP",
        _xp=[_xp(row) for _, row in bench.iterrows()])
    ordered = scored.sort_values(["_gk", "sub_value", "_xp", "element"],
                                 ascending=[True, False, False, True], kind="stable")
    return ordered.drop(columns=["_gk", "_xp"])


def optimal_xi(squad: pd.DataFrame, value_col: str = "gw_xp") -> Lineup:
    """Best legal XI over every formation, maximising ``value_col``.

    Raises ``LineupError`` when no formation can be filled. ``total`` is the
    real expected points (``gw_xp``, clipped at 0), not the selection value.
    """
    ranked = {pos: squad[squad["position"] == pos]
              .sort_values([value_col, "element"], ascending=[False, True],
                           na_position="last", kind="stable")
              for pos in ("GKP", "DEF", "MID", "FWD")}
    best: tuple[float, pd.DataFrame, str] | None = None
    for d, m, f in FORMATIONS:
        if len(ranked["GKP"]) < 1 or len(ranked["DEF"]) < d \
                or len(ranked["MID"]) < m or len(ranked["FWD"]) < f:
            continue
        xi = pd.concat([ranked["GKP"].head(1), ranked["DEF"].head(d),
                        ranked["MID"].head(m), ranked["FWD"].head(f)])
        score = float(pd.to_numeric(xi[value_col], errors="coerce").fillna(0).sum())
        if best is None or score > best[0]:
            best = (score, xi, f"{d}-{m}-{f}")
    if best is None:
        have = {pos: len(frame) for pos, frame in ranked.items()}
        raise LineupError(f"no legal formation: squad has {have} "
                          "(need 1 GKP, 3+ DEF, 2+ MID, 1+ FWD)")
    _, xi, formation = best
    total = round(sum(_xp(row) for _, row in xi.iterrows()), 1)
    bench = bench_order(squad[~squad["element"].isin(xi["element"])], xi)
    return Lineup(xi=xi, formation=formation, total=total, bench=bench)


def is_doubtful(row: pd.Series) -> bool:
    """Status ``d``, or a sub-``DOUBT_P_START`` start chance that something
    (an availability flag or a role override) backs up."""
    if row.get("status") == "d":
        return True
    p_start = row.get("p_start")
    if p_start is None or pd.isna(p_start) or float(p_start) >= DOUBT_P_START:
        return False
    override = row.get("role_override")
    flagged = override is not None and not pd.isna(override)
    return flagged or row.get("status") not in ("a", None)


def if_out(squad: pd.DataFrame, lineup: Lineup,
           value_col: str = "gw_xp") -> list[dict[str, Any]]:
    """Fallback XI for each doubtful starter, in XI order."""
    base = set(lineup.xi["element"])
    out = []
    for _, player in lineup.xi.iterrows():
        if not is_doubtful(player):
            continue
        rest = squad[squad["element"] != player["element"]]
        try:
            alt = optimal_xi(rest, value_col)
        except LineupError as exc:
            out.append({"web_name": player["web_name"], "error": str(exc)})
            continue
        incoming = alt.xi[~alt.xi["element"].isin(base)]
        dropped = lineup.xi[~lineup.xi["element"].isin(alt.xi["element"])
                            & (lineup.xi["element"] != player["element"])]
        names_in = incoming["web_name"].tolist()
        text = f"if {player['web_name']} out: {alt.formation}"
        text += f", {', '.join(names_in)} in" if names_in else ""
        text += f", {', '.join(dropped['web_name'])} benched" if len(dropped) else ""
        out.append({
            "web_name": player["web_name"], "position": player["position"],
            "p_start": None if pd.isna(player.get("p_start")) else float(player["p_start"]),
            "formation": alt.formation, "xi_gw_xp": alt.total,
            "in": names_in, "benched": dropped["web_name"].tolist(),
            "text": f"{text} (xP {alt.total})",
        })
    return out
