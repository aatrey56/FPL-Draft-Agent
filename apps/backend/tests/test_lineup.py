"""Lineup optimiser: formation search, bench order, if-out fallbacks."""

import pandas as pd
import pytest

from backend.ml import lineup as lu
from backend.ml import waiver as wv


def _squad(spec):
    """spec: list of (position, gw_xp[, p_start, status, role_override])."""
    rows = []
    for i, item in enumerate(spec):
        pos, xp, *rest = item
        p_start, status, override = (list(rest) + [1.0, "a", None][len(rest):])[:3]
        rows.append({"element": i + 1, "web_name": f"{pos}{i + 1}", "position": pos,
                     "gw_xp": xp, "p_start": p_start, "status": status,
                     "role_override": override, "availability": 1.0})
    return pd.DataFrame(rows)


def _legal(xi):
    counts = xi["position"].value_counts().to_dict()
    return (len(xi) == 11 and counts.get("GKP") == 1
            and 3 <= counts.get("DEF", 0) <= 5 and 2 <= counts.get("MID", 0) <= 5
            and 1 <= counts.get("FWD", 0) <= 3)


# 2 GKP, 5 DEF, 5 MID, 3 FWD with only three mids worth playing.
THIN_MIDS = [("GKP", 3.0), ("GKP", 1.9),
             ("DEF", 3.7), ("DEF", 3.1), ("DEF", 1.6), ("DEF", 1.5), ("DEF", 1.4),
             ("MID", 4.3), ("MID", 3.8), ("MID", 0.9), ("MID", 0.8), ("MID", 0.0),
             ("FWD", 1.5), ("FWD", 1.2), ("FWD", 0.0)]


def test_formations_are_every_legal_shape():
    assert len(lu.FORMATIONS) == 8 and (5, 2, 3) in lu.FORMATIONS
    assert all(d + m + f == 10 for d, m, f in lu.FORMATIONS)


def test_picks_the_max_xp_formation():
    lineup = lu.optimal_xi(_squad(THIN_MIDS))
    assert lineup.formation == "5-3-2"         # fifth DEF (1.5) beats fourth MID (0.8)
    assert lineup.total == 26.0
    assert _legal(lineup.xi)


@pytest.mark.parametrize("seed", range(20))
def test_never_illegal_and_matches_brute_force(seed):
    rng = pd.Series(range(15)).sample(frac=1, random_state=seed).tolist()
    spec = [(pos, float(rng[i])) for i, (pos, _) in enumerate(THIN_MIDS)]
    squad = _squad(spec)
    lineup = lu.optimal_xi(squad)
    assert _legal(lineup.xi)
    brute = max(squad[squad.position == "GKP"].gw_xp.max()
                + sum(squad[squad.position == p].gw_xp.nlargest(n).sum()
                      for p, n in zip(("DEF", "MID", "FWD"), f))
                for f in lu.FORMATIONS)
    assert lineup.total == round(brute, 1)


def test_short_of_fit_defenders_still_fields_a_legal_xi():
    spec = list(THIN_MIDS)
    spec[4:7] = [("DEF", -1.0), ("DEF", -1.0), ("DEF", -1.0)]   # only 2 fit DEF
    lineup = lu.optimal_xi(_squad(spec))
    assert _legal(lineup.xi) and lineup.formation.startswith("3-")


def test_squad_missing_a_position_is_a_clear_error():
    squad = _squad([p for p in THIN_MIDS if p[0] != "DEF"] + [("DEF", 2.0), ("DEF", 2.0)])
    with pytest.raises(lu.LineupError, match="no legal formation"):
        lu.optimal_xi(squad)


def test_blank_gw_players_with_zero_xp_are_benched():
    spec = list(THIN_MIDS)
    spec[2] = ("DEF", 0.0)          # best DEF blanks
    spec[7] = ("MID", 0.0)          # best MID blanks
    lineup = lu.optimal_xi(_squad(spec))
    assert not {"DEF3", "MID8"} & set(lineup.xi["web_name"])


def test_same_xi_as_waiver_best_xi_when_that_was_optimal():
    squad = _squad(THIN_MIDS)
    old, old_total = wv.best_xi(squad, value_col="gw_xp")
    new = lu.optimal_xi(squad)
    assert set(new.xi["element"]) == set(old["element"]) and new.total == old_total


def test_bench_puts_gk_first_then_most_likely_useful_outfielder():
    spec = list(THIN_MIDS)
    spec[12] = ("FWD", 1.5, 0.35, "d", "hamstring")   # doubtful striker
    lineup = lu.optimal_xi(_squad(spec))
    bench = lineup.bench
    assert bench.iloc[0]["position"] == "GKP"
    assert list(bench["web_name"])[1] == "MID11"      # 0.8 covers the doubtful FWD/DEF/MID
    assert bench.iloc[-1]["sub_value"] == 0.0


def test_if_out_gives_the_fallback_for_a_doubtful_starter():
    spec = list(THIN_MIDS)
    spec[12] = ("FWD", 1.5, 0.35, "a", "hamstring")
    spec[10] = ("MID", 0.9)
    squad = _squad(spec)
    lineup = lu.optimal_xi(squad)
    alts = lu.if_out(squad, lineup)
    assert [a["web_name"] for a in alts] == ["FWD13"]
    assert alts[0]["formation"] == "5-4-1" and alts[0]["in"] == ["MID11"]
    assert alts[0]["text"].startswith("if FWD13 out: 5-4-1, MID11 in")


def test_low_p_start_without_a_flag_is_not_doubtful():
    row = pd.Series({"status": "a", "p_start": 0.3, "role_override": None})
    assert not lu.is_doubtful(row)
    assert lu.is_doubtful(row.copy().replace({"a": "d"}))


def _cover_case(**starter):
    """A one-FWD-short squad: FWD13 starts (fields in ``starter``), FWD14 is
    the only bench player who can cover him; returns FWD14's sub_value."""
    spec = [("GKP", 3.0), ("GKP", 1.0)] + [("DEF", 3.0)] * 5 + [("MID", 3.0)] * 5 \
        + [("FWD", 5.0), ("FWD", 2.0), ("FWD", 0.1)]
    squad = _squad(spec)
    squad.loc[squad["web_name"] == "FWD13", list(starter)] = list(starter.values())
    xi = squad[squad["web_name"].isin(
        ["GKP1"] + [f"DEF{i}" for i in range(3, 6)] + [f"MID{i}" for i in range(8, 13)]
        + ["FWD13", "FWD15"])]
    sub = squad[squad["web_name"] == "FWD14"].iloc[0]
    return lu.substitute_value(sub, xi)


def test_a_likely_cameo_blocks_the_auto_sub():
    # p_start 0.4 but p_appear 0.9: he plays in 9 of 10 worlds, so cover is
    # worth 2.0 x 0.1 — not 2.0 x 0.6 as 1 - p_start would say.
    assert _cover_case(p_start=0.4, p_appear=0.9) == pytest.approx(2.0 * 0.1)


def test_heuristic_flagged_starter_gives_cover_real_value():
    # heuristic row: no p_start/p_appear, a 25% chance-of-playing flag
    value = _cover_case(p_start=float("nan"), p_appear=float("nan"),
                        availability=0.25, status="d")
    assert value == pytest.approx(2.0 * 0.75)


def test_model_row_from_an_older_file_falls_back_to_p_start():
    assert _cover_case(p_start=0.4, p_appear=float("nan")) == pytest.approx(2.0 * 0.6)


def test_unavailable_starter_is_certain_to_miss_whatever_p_appear_says():
    assert _cover_case(p_appear=0.9, availability=0.0) == pytest.approx(2.0)
