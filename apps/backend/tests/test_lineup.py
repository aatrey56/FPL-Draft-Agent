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


def _xi_bench(starters, bench):
    """(xi, bench) frames from [(pos, xp, ...)] specs; bench is in the given order."""
    frame = _squad(list(starters) + list(bench))
    return frame.iloc[:len(starters)], frame.iloc[len(starters):]


# A 5-3-2 XI (element 1 = GKP, 2-6 DEF, 7-9 MID, 10-11 FWD).
FIVE_THREE_TWO = [("GKP", 3.0)] + [("DEF", 3.0)] * 5 + [("MID", 3.0)] * 3 + [("FWD", 3.0)] * 2


def test_autosub_gk_out_takes_the_reserve_gk_not_an_outfielder():
    xi, bench = _xi_bench(FIVE_THREE_TWO, [("MID", 5.0), ("GKP", 1.0)])
    res = lu.simulate_autosubs(xi, bench, {1})
    assert res["subs"] == [{"out": "GKP1", "in": "GKP13", "bench_slot": 2}]
    assert res["formation"] == "5-3-2" and res["unreplaced"] == []
    assert res["total"] == 31.0 and len(res["xi"]) == 11


def test_autosub_def_out_at_three_def_skips_the_earlier_mid():
    starters = [("GKP", 3.0)] + [("DEF", 3.0)] * 3 + [("MID", 3.0)] * 5 + [("FWD", 3.0)] * 2
    xi, bench = _xi_bench(starters, [("MID", 9.0), ("DEF", 1.0), ("GKP", 1.0)])
    res = lu.simulate_autosubs(xi, bench, {2})
    assert res["subs"] == [{"out": "DEF2", "in": "DEF13", "bench_slot": 2}]
    assert res["formation"] == "3-5-2"


def test_autosub_skips_a_bench_player_who_will_not_play():
    xi, bench = _xi_bench(FIVE_THREE_TWO, [("GKP", 1.0), ("MID", 5.0), ("MID", 2.0)])
    bench = bench.assign(availability=[1.0, 0.0, 1.0])
    res = lu.simulate_autosubs(xi, bench, {10})
    assert res["subs"] == [{"out": "FWD10", "in": "MID14", "bench_slot": 3}]
    assert res["formation"] == "5-4-1"
    assert res["total"] == 32.0


def test_autosub_leaves_a_starter_unreplaced_when_nobody_is_legal():
    xi, bench = _xi_bench(FIVE_THREE_TWO, [("GKP", 1.0), ("FWD", 4.0), ("FWD", 4.0)])
    # With a bench FWD available the MID is covered (5-2-3) ...
    assert lu.simulate_autosubs(xi, bench, {8})["unreplaced"] == []
    # ... but with only the reserve GKP left nobody can legally come on.
    res = lu.simulate_autosubs(xi, bench.iloc[:1], {8})
    assert res["subs"] == [] and res["unreplaced"] == ["MID8"]
    assert res["formation"] == "5-3-2" and res["total"] == 30.0


def test_if_out_reports_the_automatic_result():
    spec = list(THIN_MIDS)
    spec[12] = ("FWD", 1.5, 0.35, "a", "hamstring")
    spec[10] = ("MID", 0.9)
    squad = _squad(spec)
    lineup = lu.optimal_xi(squad)
    alts = lu.if_out(squad, lineup)
    assert [a["web_name"] for a in alts] == ["FWD13"]
    alt = alts[0]
    assert alt["automatic"] is True
    assert alt["auto_sub"]["in"] == "MID11" and alt["auto_sub"]["bench_slot"] == 2
    assert alt["formation"] == alt["auto_sub"]["formation"] == "5-4-1"
    assert alt["in"] == ["MID11"]
    assert alt["text"].startswith(
        "If FWD13 plays 0 minutes, FPL auto-subs MID11 in (bench 2) → 5-4-1")
    assert "Nothing to do." in alt["text"]


def test_if_out_havertz_shape_five_three_two_becomes_five_four_one():
    """Doubtful FWD in a 5-3-2: the first outfield bench player (a MID)
    comes on automatically — GK1 on the bench is skipped."""
    starters = [("GKP", 3.0)] + [("DEF", 3.0)] * 5 + [("MID", 3.0)] * 3 \
        + [("FWD", 5.0, 0.4, "d", None), ("FWD", 3.0)]
    xi, bench = _xi_bench(starters, [("GKP", 1.0), ("MID", 2.5), ("FWD", 2.0)])
    squad = pd.concat([xi, bench])
    lineup = lu.Lineup(xi=xi, formation="5-3-2", total=0.0, bench=bench)
    (alt,) = lu.if_out(squad, lineup)
    assert alt["auto_sub"] == {"in": "MID13", "formation": "5-4-1",
                               "xi_gw_xp": 32.5, "bench_slot": 2}
    assert alt["formation"] == "5-4-1" and alt["in"] == ["MID13"]
    assert "manual_if_ruled_out_before_lock" not in alt


def test_manual_suggestion_only_when_it_beats_the_auto_sub_by_more_than_threshold(
        monkeypatch):
    starters = [("GKP", 3.0)] + [("DEF", 3.0)] * 5 + [("MID", 3.0)] * 3 \
        + [("FWD", 5.0, 0.4, "d", None), ("FWD", 3.0)]
    # Bench order puts a weak MID ahead of a strong FWD: auto-sub is worse
    # than swapping by hand (bench order here is deliberately bad).
    xi, bench = _xi_bench(starters, [("GKP", 1.0), ("MID", 0.5), ("FWD", 6.0)])
    lineup = lu.Lineup(xi=xi, formation="5-3-2", total=0.0, bench=bench)
    (alt,) = lu.if_out(pd.concat([xi, bench]), lineup)
    manual = alt["manual_if_ruled_out_before_lock"]
    assert manual["in"] == ["FWD14"] and manual["gain"] == 5.5
    assert manual["text"].startswith("If he's ruled out before lock, swapping manually gains +5.5")

    # Sane bench order: the auto-sub already is the best XI -> no suggestion.
    xi, bench = _xi_bench(starters, [("GKP", 1.0), ("FWD", 6.0), ("MID", 0.5)])
    lineup = lu.Lineup(xi=xi, formation="5-3-2", total=0.0, bench=bench)
    (alt,) = lu.if_out(pd.concat([xi, bench]), lineup)
    assert "manual_if_ruled_out_before_lock" not in alt

    # A gain equal to the threshold is not enough; just above it is.
    xi, bench = _xi_bench(starters, [("GKP", 1.0), ("MID", 0.5), ("FWD", 6.0)])
    lineup = lu.Lineup(xi=xi, formation="5-3-2", total=0.0, bench=bench)
    squad = pd.concat([xi, bench])
    monkeypatch.setattr(lu, "MANUAL_GAIN_MIN", 5.5)
    assert "manual_if_ruled_out_before_lock" not in lu.if_out(squad, lineup)[0]
    monkeypatch.setattr(lu, "MANUAL_GAIN_MIN", 5.4)
    assert "manual_if_ruled_out_before_lock" in lu.if_out(squad, lineup)[0]


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
