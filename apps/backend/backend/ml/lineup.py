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
  that probability ``1 - prod(p_play)`` over those starters. It deliberately
  ignores the sub's slot in the queue (an earlier sub may use up the miss):
  simple and monotone, which is what ordering four players needs.
* ``p_play`` — the chance a starter plays *any* minutes, since even a cameo
  blocks his auto-sub: 0 when availability is 0; else the model's
  ``p_appear`` (start or cameo; a role override sets it to its ``p_start``,
  see ``waiver.apply_role_overrides``); else ``p_start`` (an xP file from
  before ``p_appear`` was carried); else, for a heuristic row, the
  ``availability`` factor (the bootstrap ``chance_of_playing_next_round``/100
  when flagged, see ``waiver.availability_factor``); else 1.
* ``simulate_autosubs`` — what FPL does when starters play 0 minutes: walk
  the bench in order, skip bench players assumed not to play, and bring on
  the first one who keeps the formation legal (``_can_replace``).
* ``if_out`` — for each doubtful starter (status ``d``, or p_start below
  ``DOUBT_P_START`` with an availability flag or a role override) what
  *automatically* happens if he plays 0 minutes (``simulate_autosubs`` on the
  bench order — the user does nothing). Only when re-optimising the XI by
  hand beats that by more than ``MANUAL_GAIN_MIN`` xP is a
  ``manual_if_ruled_out_before_lock`` suggestion added: the one case where
  the user should act, and only if he is ruled out before the lock.

Inputs are a squad frame with ``element``, ``web_name``, ``position``,
``gw_xp``, ``p_start``, ``status`` (``p_appear``, ``availability`` and
``role_override`` optional) plus the
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
# A hand-made swap must beat the automatic result by more than this (xP) to
# be worth telling the user to act.
MANUAL_GAIN_MIN = 0.5


class LineupError(ValueError):
    """The squad cannot field any legal XI (e.g. fewer than 3 DEF in it)."""


@dataclass
class Lineup:
    """An optimised XI: the starters, formation label, total and bench."""

    xi: pd.DataFrame
    formation: str | None
    total: float
    bench: pd.DataFrame = field(default_factory=pd.DataFrame)


def _p_play(row: pd.Series) -> float:
    """Chance a starter plays any minutes (blocking his auto-sub); see the
    module docstring for the fallback order."""
    if row.get("availability") == 0:
        return 0.0
    for col in ("p_appear", "p_start", "availability"):
        value = row.get(col)
        if value is not None and pd.notna(value):
            return float(value)
    return 1.0


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
            all_play *= _p_play(starter)
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


def _formation_label(xi: pd.DataFrame) -> str:
    counts = xi["position"].value_counts()
    return "-".join(str(int(counts.get(pos, 0))) for pos in ("DEF", "MID", "FWD"))


def _total_xp(frame: pd.DataFrame) -> float:
    return round(sum(_xp(row) for _, row in frame.iterrows()), 1)


def simulate_autosubs(xi: pd.DataFrame, bench: pd.DataFrame,
                      out_elements: set[int]) -> dict[str, Any]:
    """FPL's automatic substitutions for starters who play 0 minutes.

    ``bench`` is in auto-sub order. Each ``out_elements`` starter (XI order)
    is replaced by the first unused bench player who is assumed to play
    (``_p_play`` above 0) and whose arrival keeps the XI legal — the reserve
    GKP only for the GKP. A starter nobody can legally replace stays in the
    XI on 0 points (``unreplaced``).

    Returns ``xi`` (players who will play, outs removed), ``formation``
    (shape of the XI the outs occupied, so an unreplaced starter still counts
    toward it), ``total`` (gw_xp of ``xi``), ``subs`` ({"out", "in",
    "bench_slot"} per swap, slot 1-based in ``bench``) and ``unreplaced``.
    """
    lineup = xi.copy()
    available = [(slot, bench.iloc[[slot - 1]]) for slot in range(1, len(bench) + 1)
                 if _p_play(bench.iloc[slot - 1]) > 0]
    subs: list[dict[str, Any]] = []
    unreplaced: list[str] = []
    for _, starter in xi.iterrows():
        if starter["element"] not in out_elements:
            continue
        counts = lineup["position"].value_counts().to_dict()
        for pos in POSITION_LIMITS:
            counts.setdefault(pos, 0)
        pick = next(((slot, sub) for slot, sub in available
                     if _can_replace(sub.iloc[0]["position"], starter["position"], counts)),
                    None)
        if pick is None:
            unreplaced.append(starter["web_name"])
            continue
        slot, sub = pick
        available = [(s, r) for s, r in available if s != slot]
        lineup = pd.concat([lineup[lineup["element"] != starter["element"]], sub])
        subs.append({"out": starter["web_name"], "in": sub.iloc[0]["web_name"],
                     "bench_slot": slot})
    played = lineup[~lineup["element"].isin(out_elements)]
    return {"xi": played, "formation": _formation_label(lineup), "total": _total_xp(played),
            "subs": subs, "unreplaced": unreplaced}


def if_out(squad: pd.DataFrame, lineup: Lineup,
           value_col: str = "gw_xp") -> list[dict[str, Any]]:
    """What happens for each doubtful starter who plays 0 minutes, XI order.

    The headline (``auto_sub``, ``automatic: true``, and the legacy
    ``formation`` / ``in`` / ``xi_gw_xp`` / ``text`` keys) is the FPL
    auto-sub result given the bench order — nothing for the user to do. If a
    manual re-optimisation (``value_col`` selection, as ``optimal_xi``)
    beats it by more than ``MANUAL_GAIN_MIN`` xP, ``manual_if_ruled_out_before_lock``
    says what to swap by hand if he is ruled out before the lock.
    """
    out = []
    for _, player in lineup.xi.iterrows():
        if not is_doubtful(player):
            continue
        name = player["web_name"]
        auto = simulate_autosubs(lineup.xi, lineup.bench, {player["element"]})
        sub = auto["subs"][0] if auto["subs"] else None
        if sub:
            text = (f"If {name} plays 0 minutes, FPL auto-subs {sub['in']} in "
                    f"(bench {sub['bench_slot']}) → {auto['formation']} "
                    f"(xP {auto['total']}). Nothing to do.")
        else:
            text = (f"If {name} plays 0 minutes, no bench player can legally replace "
                    f"him, so he stays in the XI on 0 → {auto['formation']} "
                    f"(xP {auto['total']}).")
        entry = {
            "web_name": name, "position": player["position"],
            "p_start": None if pd.isna(player.get("p_start")) else float(player["p_start"]),
            "automatic": True,
            "auto_sub": {"in": sub["in"] if sub else None, "formation": auto["formation"],
                         "xi_gw_xp": auto["total"],
                         "bench_slot": sub["bench_slot"] if sub else None},
            "formation": auto["formation"], "xi_gw_xp": auto["total"],
            "in": [sub["in"]] if sub else [], "benched": [], "text": text,
        }
        try:
            alt = optimal_xi(squad[squad["element"] != player["element"]], value_col)
        except LineupError:
            alt = None  # no legal manual XI either; the auto result stands
        if alt is not None and alt.total - auto["total"] > MANUAL_GAIN_MIN:
            gain = round(alt.total - auto["total"], 1)
            names_in = alt.xi[~alt.xi["element"].isin(lineup.xi["element"])]["web_name"].tolist()
            entry["manual_if_ruled_out_before_lock"] = {
                "in": names_in, "formation": alt.formation,
                "xi_gw_xp": alt.total, "gain": gain,
                "text": (f"If he's ruled out before lock, swapping manually gains +{gain} "
                         f"({alt.formation}, {', '.join(names_in)} in; xP {alt.total})"),
            }
            entry["text"] += f" {entry['manual_if_ruled_out_before_lock']['text']}."
        out.append(entry)
    return out
