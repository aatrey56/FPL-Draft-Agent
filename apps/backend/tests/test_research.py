"""research — triage, validation, spend guard, outputs. The Anthropic client
is always a fake: no test touches the network."""
import _thread
import argparse
import json
import os
import re
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from backend.ml import jsonutil
from backend.ml import research as rs
from backend.ml import waiver as wv

SEASON = "2026-27"
LEAGUE, ENTRY = 7, 42
GW = 6
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
SOURCE = "https://www.wolves.co.uk/news/injury-update"           # tier 1 (official club)
# A verbatim quote per status claim; every fake page carries all three.
QUOTES = {"expected_start": "{name} trained fully on Thursday and is available",
          "ruled_out": "{name} has been ruled out for three weeks",
          "doubtful": "{name} faces a late fitness test"}
PAGE_AGE = "October 7, 2026"                                      # search result date


def _quote(name="Doubt", claim="expected_start"):
    return QUOTES.get(claim, QUOTES["expected_start"]).format(name=name)


def _page(name="Doubt"):
    return (f"Team news. {_quote(name)} for Saturday, the head coach said. "
            f"{_quote(name, 'ruled_out')} with a hamstring problem. "
            f"{_quote(name, 'doubtful')} before the trip.")


QUOTE, PAGE = _quote(), _page()

TEAMS = [{"id": 1, "name": "Arsenal", "short_name": "ARS"},
         {"id": 2, "name": "Wolves", "short_name": "WOL"}]
ELEMENTS = [
    # squad
    {"id": 1, "code": 101, "web_name": "Fit", "element_type": 4, "team": 2, "status": "a"},
    {"id": 2, "code": 102, "web_name": "Doubt", "element_type": 3, "team": 2, "status": "d",
     "news": "Hamstring - 50% chance of playing", "chance_of_playing_next_round": 50},
    {"id": 3, "code": 103, "web_name": "Bencher", "element_type": 2, "team": 2, "status": "a"},
    {"id": 4, "code": 104, "web_name": "Starter", "element_type": 3, "team": 2, "status": "a"},
    # free agents
    {"id": 10, "code": 110, "web_name": "RecAdd", "element_type": 4, "team": 1, "status": "a"},
    {"id": 11, "code": 111, "web_name": "QuietFA", "element_type": 2, "team": 1, "status": "a"},
    {"id": 12, "code": 112, "web_name": "Mover", "element_type": 3, "team": 1, "status": "a"},
]
SQUAD = {1, 2, 3, 4}
BOOTSTRAP = {
    "teams": TEAMS, "elements": ELEMENTS, "fixtures": {"6": [{"team_h": 1, "team_a": 2}]},
    "events": {"current": 5, "next": 6, "data": [
        {"id": 5, "finished": True, "deadline_time": "2026-09-26T10:00:00Z"},
        {"id": 6, "finished": False, "deadline_time": "2099-10-10T10:00:00Z",
         "waivers_time": "2099-10-09T10:00:00Z", "trades_time": "2099-10-08T10:00:00Z"}]},
}
WAIVER_PLAN = {
    "recommendations": [{"add": "RecAdd", "add_element": 10, "add_p_start": 0.8,
                         "club_moved": False, "add_expected_minutes": 85.0}],
    "best_by_position": {"GKP": [], "DEF": [{"add": "QuietFA", "add_element": 11,
                                             "club_moved": False, "add_expected_minutes": 90.0}],
                         "MID": [{"add": "Mover", "add_element": 12, "club_moved": True,
                                  "add_expected_minutes": 70.0}], "FWD": []},
    "drop_candidates": [],
}
MY_WEEK = {
    "xi": [{"web_name": "Fit", "team": "WOL", "p_start": 0.9, "expected_minutes": 88.0, "club_moved": False},
           {"web_name": "Doubt", "team": "WOL", "p_start": 0.4, "expected_minutes": 80.0, "club_moved": False},
           {"web_name": "Starter", "team": "WOL", "p_start": 0.7, "expected_minutes": 75.0, "club_moved": False}],
    "bench": [{"web_name": "Bencher", "team": "WOL", "p_start": 0.3, "expected_minutes": 20.0, "club_moved": False}],
    "if_out": [{"web_name": "Starter", "text": "if Starter out: ..."}],
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "RESEARCH_EFFORT", "RESEARCH_MAX_USD", "RESEARCH_MONTHLY_USD",
                 "RESEARCH_MAX_PLAYERS", "RESEARCH_CONCURRENCY", "RESEARCH_BILLING_DAY",
                 "LEAGUE_ID", "ENTRY_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(rs, "load_dotenv", lambda *a, **k: None)


def _triage(phase="waivers", **overrides):
    kwargs = {"squad": SQUAD, "waiver_plan": WAIVER_PLAN, "my_week": MY_WEEK, "overrides": [],
              "gw": GW, "deadlines": wv.event_deadlines(BOOTSTRAP)}
    kwargs.update(overrides)
    return rs.triage(BOOTSTRAP, phase, **kwargs)


def _cand(element=2, **fields):
    base = {"element": element, "code": 100 + element, "web_name": "Doubt", "team": "WOL",
            "team_name": "Wolves", "position": "MID", "status": "d", "news": "", "chance": 50,
            "group": "squad", "reasons": ["status:d"]}
    base.update(fields)
    return rs.Candidate(**base)


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------

def test_triage_selects_by_trigger_squad_first_and_logs_the_rest():
    selected, skipped = _triage()
    assert [c.web_name for c in selected] == ["Starter", "Doubt", "Bencher", "RecAdd", "Mover"]
    reasons = {c.web_name: c.reasons for c in selected}
    assert reasons["Doubt"] == ["status:d", "news", "chance:50"]
    assert reasons["Starter"] == ["doubtful_starter"]
    assert reasons["Bencher"] == ["low_minutes:20"]
    assert reasons["RecAdd"] == ["recommended_add"]
    assert reasons["Mover"] == ["club_moved"]
    assert {(s["web_name"], s["reason"]) for s in skipped} == {("Fit", "no_trigger"),
                                                               ("QuietFA", "no_trigger")}
    doubt = next(c for c in selected if c.web_name == "Doubt")
    assert doubt.model_p_start == 0.4 and doubt.chance == 50 and doubt.team_name == "Wolves"


def test_triage_lineup_phase_researches_the_squad_only():
    selected, skipped = _triage("lineup")
    assert {c.group for c in selected} == {"squad"}
    assert all(s["group"] == "squad" for s in skipped)


def test_triage_caps_players_and_reports_the_overflow():
    selected, skipped = _triage(max_players=2)
    assert [c.web_name for c in selected] == ["Starter", "Doubt"]
    assert [s["web_name"] for s in skipped if s["reason"] == "over_cap"] == ["Bencher", "RecAdd", "Mover"]


def test_triage_with_empty_waiver_plan_researches_flagged_squad_only():
    for plan in (None, {}, {"recommendations": [{"add": "no element"}], "best_by_position": None}):
        selected, _ = _triage(waiver_plan=plan)
        assert [c.web_name for c in selected] == ["Starter", "Doubt", "Bencher"]


def test_triage_without_my_week_keeps_bootstrap_triggers_only():
    selected, skipped = _triage(my_week=None)
    assert [c.web_name for c in selected] == ["Doubt", "RecAdd", "Mover"]
    assert ("Starter", "no_trigger") in {(s["web_name"], s["reason"]) for s in skipped}


def test_triage_flags_a_live_override_but_not_a_stale_one():
    live = {"player": "Fit", "team": "WOL", "p_start": 0.5, "valid_through_gw": GW, "fact": "rotation"}
    stale = {"code": 111, "p_start": 0.5, "valid_through_gw": GW - 1}
    selected, _ = _triage(overrides=[live, stale])
    fit = next(c for c in selected if c.web_name == "Fit")
    assert fit.reasons == ["active_override"] and fit.active_override == "rotation"
    assert "QuietFA" not in [c.web_name for c in selected]


def test_triage_rejects_an_unknown_phase():
    with pytest.raises(ValueError):
        _triage("draft")


def test_resolve_squad_prefers_element_status_then_my_week():
    status = {"element_status": [{"element": 2, "owner": ENTRY}, {"element": 10, "owner": None}]}
    assert rs.resolve_squad(BOOTSTRAP, element_status=status, entry_id=ENTRY, my_week=MY_WEEK) \
        == ({2}, "element-status")
    assert rs.resolve_squad(BOOTSTRAP, element_status=None, entry_id=None, my_week=MY_WEEK) \
        == (SQUAD, "my_week")
    assert rs.resolve_squad(BOOTSTRAP, element_status=None, entry_id=None, my_week=None) == (set(), "none")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _evidence(url=SOURCE, quote=QUOTE, published_at="2026-10-07"):
    return {"claim": "Trained fully on Thursday", "source_url": url,
            "published_at": published_at, "quote": quote}


def _finding(**fields):
    base = {"element": 2, "web_name": "Doubt", "p_start": 0.6, "return_gw": None,
            "valid_through_gw": GW, "status_claim": "expected_start", "conflict": False,
            "confidence": "high", "evidence": [_evidence()],
            "summary": "Trained fully; manager expects him to be available."}
    base.update(fields)
    return base


def test_valid_finding_passes_and_is_normalized():
    finding, error = rs.validate_finding(
        _finding(evidence=[{"claim": " Trained ", "source_url": SOURCE + "/",
                            "published_at": "2026-10-07T18:00:00Z", "quote": f" {QUOTE} "}]),
        _cand(), GW, allowed_urls={SOURCE})
    assert error is None
    assert finding["evidence"] == [{"claim": "Trained", "source_url": SOURCE + "/",
                                    "llm_published_at": "2026-10-07", "quote": QUOTE}]


def test_model_publication_date_is_audit_only_and_never_required():
    finding, error = rs.validate_finding(_finding(evidence=[_evidence(published_at="last week")]),
                                         _cand(), GW, allowed_urls={SOURCE})
    assert error is None and finding["evidence"][0]["llm_published_at"] is None
    assert "published_at" not in finding["evidence"][0]


def test_return_gw_is_nulled_unless_after_the_gameweek_and_an_absence():
    for fields in ({"return_gw": GW, "status_claim": "ruled_out"},       # already back
                   {"return_gw": GW + 2, "status_claim": "doubtful"}):    # not an absence
        finding, error = rs.validate_finding(_finding(**fields), _cand(), GW, allowed_urls={SOURCE})
        assert error is None and finding["return_gw"] is None
    finding, _ = rs.validate_finding(_finding(return_gw=GW + 2, status_claim="ruled_out"),
                                     _cand(), GW, allowed_urls={SOURCE})
    assert finding["return_gw"] == GW + 2


@pytest.mark.parametrize("raw, reason", [
    ("not json", "not a JSON object"),
    ({k: v for k, v in _finding().items() if k != "summary"}, "missing fields: summary"),
    (_finding(extra=1), "unexpected fields: extra"),
    (_finding(element=3), "is not the researched 2"),
    (_finding(element=True), "is not the researched"),
    (_finding(p_start=1.5), "outside [0, 1]"),
    (_finding(p_start="0.5"), "outside [0, 1]"),
    (_finding(valid_through_gw=GW - 1), "valid_through_gw"),
    (_finding(return_gw=40), "is not a gameweek"),
    (_finding(confidence="medium"), "confidence"),
    (_finding(status_claim="injured"), "status_claim"),
    (_finding(conflict="no"), "conflict is not a boolean"),
    (_finding(evidence=[_evidence(quote="  ")]), "no valid evidence"),
    (_finding(summary="x" * 201), "201 chars"),
    (_finding(summary="  "), "summary is empty"),
    (_finding(evidence=[]), "no valid evidence"),
    (_finding(evidence=[_evidence(url="https://elsewhere.example/a")]), "no valid evidence"),
])
def test_invalid_findings_are_dropped_with_a_reason(raw, reason):
    finding, error = rs.validate_finding(raw, _cand(), GW, allowed_urls={SOURCE})
    assert finding is None and reason in error


# ---------------------------------------------------------------------------
# Credibility gate
# ---------------------------------------------------------------------------

BBC = "https://www.bbc.co.uk/sport/football/1"
BBC_COM = "https://www.bbc.com/sport/football/2"
SKY = "https://www.skysports.com/news/3"
RUMOUR = "https://transfer-gossip.example/4"
SEEN = {url: PAGE for url in (SOURCE, BBC, BBC_COM, SKY, RUMOUR)}
AGES = {url: [PAGE_AGE] for url in SEEN}


def _decide(evidence, seen=SEEN, cutoff="2026-09-26", ages=AGES, verdict="supports", cand=None,
            **fields):
    """Run the whole gate as ``run`` does: validate, deterministic checks, a
    stub verifier, decide."""
    finding, error = rs.validate_finding(_finding(evidence=evidence, **fields), cand or _cand(), GW,
                                         allowed_urls=set(SEEN) | set(seen))
    assert error is None, error
    rs.annotate_evidence(finding, seen, ages, names=rs.player_names(cand or _cand()), now=NOW,
                         cutoff=cutoff)
    for item in rs.verification_queue(finding):
        item["verifier"] = verdict
    return rs.decide(finding, cutoff), finding


def test_classify_source_by_parsed_host_on_label_boundaries():
    assert rs.classify_source("https://www.wolves.co.uk/news/x") == (1, "wolves")
    assert rs.classify_source("https://news.bbc.co.uk/a") == (2, "bbc")
    assert rs.classify_source("https://www.bbc.com/a") == (2, "bbc")
    assert rs.classify_source("https://notbbc.co.uk/a") == (3, "notbbc.co.uk")
    assert rs.classify_source("https://bbc.co.uk.evil.example/a") == (3, "bbc.co.uk.evil.example")
    assert rs.classify_source("https://Transfer-Gossip.example:443/a") == (3, "transfer-gossip.example")


@pytest.mark.parametrize("url", [
    "https://wolves.co.uk:443@rumour.example/news",     # userinfo disguising the real host
    "https://user:pw@www.bbc.co.uk/sport",               # any userinfo is untrusted
    "https://www.bbc.co.uk:notaport/sport",               # malformed port
    "https://www.bbc.co.uk\\@evil.example/x",             # backslash authority trick
    "ftp://www.bbc.co.uk/sport",
    "https:///no-host",
    "not a url",
])
def test_malformed_or_userinfo_urls_are_untrusted(url):
    assert rs.source_host(url) is None
    assert rs.classify_source(url) == (3, "invalid-url")


def test_verified_tier_one_source_is_applied_and_evidence_annotated():
    (decision, reasons), finding = _decide([_evidence()])
    assert decision == "applied" and reasons == ["verified tier-1 source"]
    item = finding["evidence"][0]
    assert {k: item[k] for k in ("tier", "outlet", "verified", "names_player", "status_language",
                                 "page_age", "source_published_at", "date_problem", "fresh",
                                 "verifier", "counts", "checks_failed", "llm_published_at")} == {
        "tier": 1, "outlet": "wolves", "verified": True, "names_player": True,
        "status_language": "supports", "page_age": PAGE_AGE, "source_published_at": "2026-10-07",
        "date_problem": None, "fresh": True, "verifier": "supports", "counts": True,
        "checks_failed": [], "llm_published_at": "2026-10-07"}


def test_two_independent_tier_two_outlets_are_applied_but_one_outlet_twice_is_not():
    (decision, reasons), _ = _decide([_evidence(BBC), _evidence(SKY)])
    assert decision == "applied" and reasons == ["verified 2 tier-2 outlets"]
    (decision, reasons), _ = _decide([_evidence(BBC), _evidence(BBC_COM)])
    assert decision == "proposed" and "one tier-2 outlet only" in reasons[0]


def test_unverified_quotes_and_rumours_are_watch_only():
    (decision, _), finding = _decide([_evidence(quote="He will definitely start")])
    assert decision == "watch" and finding["evidence"][0]["verified"] is False
    (decision, _), _ = _decide([_evidence(RUMOUR)])
    assert decision == "watch"
    (decision, _), _ = _decide([_evidence()], seen={})          # page never retrieved
    assert decision == "watch"


@pytest.mark.parametrize("fields, reason", [
    ({"conflict": True}, "credible reports conflict"),
    ({"confidence": "low"}, "low confidence"),
])
def test_conflict_or_low_confidence_is_proposed(fields, reason):
    (decision, reasons), _ = _decide([_evidence()], **fields)
    assert decision == "proposed" and reason in reasons


def test_quote_about_another_player_is_never_applied():
    # A trusted page, a verbatim quote — about somebody else.
    page = "Team news. DifferentPlayer has been ruled out for three weeks. Doubt is in the squad."
    quote = "DifferentPlayer has been ruled out for three weeks"
    (decision, reasons), finding = _decide([_evidence(quote=quote)], seen={SOURCE: page},
                                           status_claim="ruled_out")
    item = finding["evidence"][0]
    assert item["verified"] and not item["names_player"] and item["verifier"] is None
    assert decision == "proposed" and "quote does not name the player" in item["checks_failed"]
    assert any("subject, status and verifier" in r for r in reasons)


def test_pronoun_quote_counts_when_its_sentence_names_the_player():
    page = "Team news. Doubt, the head coach confirmed, has been ruled out for three weeks."
    (decision, _), finding = _decide([_evidence(quote="has been ruled out for three weeks")],
                                     seen={SOURCE: page}, status_claim="ruled_out")
    item = finding["evidence"][0]
    assert item["names_player"] and decision == "applied"
    assert item["check_text"] == "Doubt, the head coach confirmed, has been ruled out for three weeks."


def test_quote_contradicting_the_claimed_status_is_never_applied():
    # The model claims ruled_out, but the verified quote says he is available.
    (decision, reasons), finding = _decide([_evidence()], status_claim="ruled_out")
    item = finding["evidence"][0]
    assert item["status_language"] == "contradicts" and item["verifier"] is None
    assert decision == "proposed"
    assert "1 credible quote(s) about him contradict ruled_out" in reasons


def test_quote_without_status_language_is_never_applied():
    page = "Team news. Doubt spoke to the media on Thursday."
    (decision, _), finding = _decide([_evidence(quote="Doubt spoke to the media on Thursday")],
                                     seen={SOURCE: page}, status_claim="ruled_out")
    assert finding["evidence"][0]["status_language"] == "none" and decision == "proposed"


@pytest.mark.parametrize("verdict", ["unrelated", "contradicts", "error:overloaded", "not_run:run_cap"])
def test_only_a_supporting_verifier_verdict_lets_evidence_count(verdict):
    (decision, _), finding = _decide([_evidence()], verdict=verdict)
    assert decision == "proposed" and f"verifier: {verdict}" in finding["evidence"][0]["checks_failed"]


def test_matching_quote_with_supporting_verifier_is_applied():
    (decision, _), finding = _decide([_evidence(quote=_quote(claim="ruled_out"))], status_claim="ruled_out")
    assert decision == "applied" and finding["evidence"][0]["counts"] is True


def test_freshness_comes_from_the_source_not_the_model():
    # The model says yesterday; the search result says September: not fresh.
    (decision, reasons), finding = _decide([_evidence(published_at="2026-10-07")],
                                           ages={SOURCE: ["September 1, 2026"]})
    assert finding["evidence"][0]["fresh"] is False
    assert decision == "proposed" and reasons == [
        "no supporting source with a source-backed date on/after 2026-09-26"]
    (decision, _), _ = _decide([_evidence()], ages={SOURCE: ["September 1, 2026"]}, cutoff=None)
    assert decision == "applied"                                # no finished GW yet


def test_unknown_source_date_is_at_most_proposed():
    for ages in ({}, {SOURCE: ["sometime"]}):
        (decision, reasons), finding = _decide([_evidence()], ages=ages)
        assert finding["evidence"][0]["date_problem"] == "unknown"
        assert decision == "proposed" and "source-backed date" in reasons[-1]
    (decision, reasons), _ = _decide([_evidence()], ages={}, cutoff=None)
    assert decision == "proposed" and reasons == ["no supporting source with a source-backed publication date"]


def test_future_source_date_rejects_the_evidence():
    (decision, _), finding = _decide([_evidence()], ages={SOURCE: ["2099-01-01"]})
    item = finding["evidence"][0]
    assert item["date_problem"] == "future" and item["verifier"] is None and not item["counts"]
    assert decision == "proposed" and "source date is in the future" in item["checks_failed"]
    # within the 1h tolerance is fine
    near = (NOW + timedelta(minutes=30)).isoformat()
    (decision, _), _ = _decide([_evidence()], ages={SOURCE: [near]})
    assert decision == "applied"


def test_no_new_info_changes_nothing():
    (decision, _), finding = _decide([_evidence()], status_claim="no_new_info")
    assert decision == "no_change" and finding["evidence"][0]["verifier"] is None


def test_verifier_queue_is_capped_and_prefers_tier_one():
    evidence = [_evidence(BBC), _evidence(SKY), _evidence(BBC_COM), _evidence(BBC), _evidence()]
    finding = _finding(evidence=evidence)
    rs.annotate_evidence(finding, SEEN, AGES, names=["doubt"], now=NOW, cutoff="2026-09-26")
    queue = rs.verification_queue(finding)
    assert len(queue) == rs.MAX_VERIFY_PER_FINDING and queue[0]["tier"] == 1
    assert sum(item["verifier"] == "not_run:per_finding_cap" for item in evidence) == 1


def test_quote_matching_ignores_case_whitespace_and_typography():
    seen = {SOURCE: "He said: “I’m  fit” — ready."}
    assert rs.quote_verified('"i\'m fit" - ready', seen, SOURCE + "#top")
    assert not rs.quote_verified("I'm unfit", seen, SOURCE)


def test_player_names_cover_short_full_and_accented_forms():
    cand = _cand(web_name="B.Fernandes", first_name="Bruno", second_name="Borges Fernandes")
    names = rs.player_names(cand)
    assert set(names) == {"b fernandes", "fernandes", "borges fernandes", "bruno borges fernandes"}
    assert rs.names_player("FERNANDES will miss the game", names)
    assert not rs.names_player("Fernandesinho will miss the game", names)
    odegaard = rs.player_names(_cand(web_name="Ødegaard", first_name="Martin", second_name="Ødegaard"))
    assert rs.names_player("Odegaard is a doubt", odegaard)
    assert rs.names_player("Martin Ødegaard is a doubt", odegaard)
    assert rs.names_player("Raúl Jiménez returns", rs.player_names(_cand(web_name="Raúl")))


@pytest.mark.parametrize("quote, claim, expected", [
    ("Doubt has been ruled out for three weeks", "ruled_out", "supports"),
    ("Doubt has not been ruled out", "ruled_out", "contradicts"),
    ("Doubt will not be available on Saturday", "ruled_out", "supports"),
    ("Doubt is available for selection", "ruled_out", "contradicts"),
    ("Doubt faces a late fitness test", "doubtful", "supports"),
    ("Doubt is a 50-50 call for Saturday", "doubtful", "supports"),
    ("Doubt will start on Saturday", "expected_start", "supports"),
    ("Doubt won't start, he drops to the bench", "expected_start", "contradicts"),
    ("Doubt spoke to the media", "expected_start", "none"),
    ("Doubt is a doubt", "available", "none"),            # the player's own name is masked
])
def test_status_language(quote, claim, expected):
    assert rs.status_language(quote, claim, ["doubt"]) == expected


@pytest.mark.parametrize("value, expected", [
    ("October 7, 2026", datetime(2026, 10, 7, tzinfo=timezone.utc)),
    ("Oct 7, 2026", datetime(2026, 10, 7, tzinfo=timezone.utc)),
    ("7 October 2026", datetime(2026, 10, 7, tzinfo=timezone.utc)),
    ("2026-10-07", datetime(2026, 10, 7, tzinfo=timezone.utc)),
    ("2026-10-07T18:30:00Z", datetime(2026, 10, 7, 18, 30, tzinfo=timezone.utc)),
    ("3 days ago", NOW - timedelta(days=3)),
    ("an hour ago", NOW - timedelta(hours=1)),
    ("yesterday", NOW - timedelta(days=1)),
    ("last season", None),
    ("", None),
    (None, None),
])
def test_parse_page_age(value, expected):
    assert rs.parse_page_age(value, NOW) == expected


def test_source_date_takes_the_oldest_and_flags_future():
    assert rs.source_date(["October 7, 2026", "October 1, 2026"], NOW) == (
        datetime(2026, 10, 1, tzinfo=timezone.utc), None)
    assert rs.source_date(["October 7, 2026", "2099-01-01"], NOW) == (None, "future")
    assert rs.source_date([], NOW) == (None, "unknown")


# ---------------------------------------------------------------------------
# Cost, worst case + spend guard
# ---------------------------------------------------------------------------

def _usage(input_tokens=1000, output_tokens=500, searches=0, **extra):
    base = {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "cache_creation": None,
            "server_tool_use": SimpleNamespace(web_search_requests=searches, web_fetch_requests=0),
            "iterations": None}
    base.update(extra)
    return SimpleNamespace(**base)


def test_response_cost_prices_tokens_cache_and_searches():
    response = SimpleNamespace(model=rs.MODEL, usage=_usage(
        1_000_000, 100_000, searches=3, cache_read_input_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
        cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=200_000)))
    # 4 input + 2 output + 0.2 read + 0.8*5 + 0.2*8 writes + 3 searches * $0.01
    assert rs.response_cost(response) == pytest.approx(4 + 2 + 0.2 + 4.0 + 1.6 + 0.03)


def test_response_cost_sums_every_attempt_when_a_fallback_served():
    iterations = [SimpleNamespace(type="message", model=rs.MODEL, input_tokens=1_000_000,
                                  output_tokens=0, cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0, cache_creation=None),
                  SimpleNamespace(type="fallback_message", model="claude-opus-5",
                                  input_tokens=1_000_000, output_tokens=0, cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0, cache_creation=None)]
    response = SimpleNamespace(model="claude-opus-5", usage=_usage(1_000_000, 0, iterations=iterations))
    assert rs.response_cost(response) == pytest.approx(4.0 + 5.0)


def test_unknown_model_is_priced_at_the_highest_rates(caplog):
    response = SimpleNamespace(model="claude-mystery", usage=_usage(1_000_000, 0))
    with caplog.at_level("WARNING"):
        assert rs.response_cost(response) == pytest.approx(5.0)
    assert "no price" in caplog.text


def test_verifier_model_has_long_prompt_pricing():
    short = SimpleNamespace(model=rs.VERIFY_MODEL, usage=_usage(100_000, 1_000_000))
    long = SimpleNamespace(model=rs.VERIFY_MODEL, usage=_usage(100_001, 1_000_000))
    assert rs.response_cost(short) == pytest.approx(0.01 + 0.5)
    assert rs.response_cost(long) == pytest.approx(100_001 * 0.5 / 1e6 + 2.5)


def test_estimated_player_cost_matches_the_constants():
    assert rs.estimated_player_cost() == pytest.approx(
        (60_000 * 4 + 6_000 * 20) / 1e6 + 5 * 0.01 + (4_000 * 4 + 1_500 * 20) / 1e6
        + 2 * (400 * 0.10 + 300 * 0.50) / 1e6)


def test_worst_case_without_tools_is_prompt_plus_max_output():
    params = {"model": rs.MODEL, "max_tokens": 1_000, "system": "s",
              "messages": [{"role": "user", "content": "x" * 990}]}
    prompt = rs.approx_tokens({"system": "s", "messages": params["messages"], "output_config": None}) \
        + rs.REQUEST_OVERHEAD_TOKENS
    expected = (prompt * 4 + prompt * (5 - 4) + 1_000 * 20) / 1e6
    assert rs.worst_case_usd(params, fallback=False) == pytest.approx(expected)
    # a fallback may bill the declined attempt too: + the dearest fallback attempt
    fallback = (prompt * 5 + prompt * (6.25 - 5) + 1_000 * 25) / 1e6
    assert rs.worst_case_usd(params, fallback=True) == pytest.approx(expected + fallback)


def test_worst_case_with_server_tools_counts_every_sampling_step():
    params = {"model": rs.MODEL, "max_tokens": 100, "messages": [],
              "tools": [{"type": "web_search_20260318", "name": "web_search", "max_uses": 1},
                        {"type": "web_fetch_20260318", "name": "web_fetch", "max_uses": 1,
                         "max_content_tokens": 8_000}]}
    prompt = (rs.approx_tokens({"system": None, "messages": [], "output_config": None})
              + rs.REQUEST_OVERHEAD_TOKENS + 2 * rs.SERVER_TOOL_OVERHEAD_TOKENS)
    # 3 steps read the prompt; the fetch (8k, first) is re-read twice, the search once;
    # all output as if from step 1 is re-read twice.
    read = 3 * prompt + 8_000 * 2 + rs.WEB_SEARCH_RESULT_TOKENS * 1 + 100 * 2
    expected = (read * 4 + prompt * 1 + 100 * 20) / 1e6 + 1 * rs.WEB_SEARCH_USD_PER_REQUEST
    assert rs.worst_case_usd(params, fallback=False) == pytest.approx(expected)


def test_worst_case_grows_with_accumulated_context_and_covers_the_estimate():
    first = {"model": rs.MODEL, "max_tokens": rs.RESEARCH_MAX_TOKENS, "tools": rs.TOOLS,
             "system": rs.RESEARCH_SYSTEM, "messages": [{"role": "user", "content": "Player: X"}]}
    resumed = {**first, "messages": first["messages"] + [
        {"role": "assistant", "content": [SimpleNamespace(type="text", text="y" * 40_000)]}]}
    assert rs.worst_case_usd(resumed, fallback=True) > rs.worst_case_usd(first, fallback=True)
    assert rs.worst_case_usd(first, fallback=True) > rs.estimated_player_cost()


def test_spend_guard_reserves_worst_case_and_settles_actuals():
    spent = []
    guard = rs.SpendGuard(cap_usd=2.5, on_spend=lambda res, usd, est: spent.append((res.call, usd, est)))
    first = guard.reserve(2.0, key=1, call="research")
    assert first is not None and guard.reserved_usd == pytest.approx(2.0)
    guard.settle(first, 0.4)
    assert guard.spent_usd == pytest.approx(0.4) and guard.reserved_usd == 0
    second = guard.reserve(2.0, key=1, call="extract")           # 0.4 + 2.0 <= 2.5
    guard.settle(second, 0.1)
    assert guard.reserve(2.1, key=2, call="research") is None    # 0.5 + 2.1 > 2.5, nothing in flight
    assert guard.exhausted and guard.cost_for(1) == pytest.approx(0.5)
    assert spent == [("research", 0.4, False), ("extract", 0.1, False)]


def test_spend_guard_waits_for_in_flight_requests_instead_of_refusing():
    guard = rs.SpendGuard(cap_usd=1.0)
    held = guard.reserve(0.8, key=1, call="research")
    got = []
    waiter = threading.Thread(target=lambda: got.append(guard.reserve(0.8, key=2, call="research")))
    waiter.start()
    time.sleep(0.1)
    assert waiter.is_alive() and not got                          # 0.8 + 0.8 > 1.0: waits
    guard.settle(held, 0.1)
    waiter.join(timeout=5)
    assert got and got[0] is not None and not guard.exhausted      # 0.1 + 0.8 <= 1.0


def test_spend_guard_stop_and_abandon_count_unsettled_at_their_reservation(caplog):
    lines = []
    guard = rs.SpendGuard(cap_usd=5.0, on_spend=lambda res, usd, est: lines.append((usd, est)))
    held = guard.reserve(1.5, key=1, call="research")
    guard.stop()
    assert guard.reserve(0.01, key=2, call="verify") is None and not guard.exhausted
    assert guard.abandon_inflight() == pytest.approx(1.5)
    guard.settle(held, 0.2)                                       # late response: already counted
    assert guard.spent_usd == pytest.approx(1.5) and lines == [(1.5, True)]
    over = rs.SpendGuard(cap_usd=5.0)
    with caplog.at_level("WARNING"):
        over.settle(over.reserve(0.1, key=3, call="research"), 0.3)
    assert over.overruns == 1 and "over its $0.1000 reservation" in caplog.text


# ---------------------------------------------------------------------------
# Monthly ledger
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("now, day, start, end", [
    (NOW, 1, datetime(2026, 10, 1), datetime(2026, 11, 1)),
    (NOW, 15, datetime(2026, 9, 15), datetime(2026, 10, 15)),
    (datetime(2026, 10, 15, tzinfo=timezone.utc), 15, datetime(2026, 10, 15), datetime(2026, 11, 15)),
    (datetime(2026, 12, 20, tzinfo=timezone.utc), 15, datetime(2026, 12, 15), datetime(2027, 1, 15)),
    (datetime(2027, 1, 3, tzinfo=timezone.utc), 28, datetime(2026, 12, 28), datetime(2027, 1, 28)),
])
def test_billing_period(now, day, start, end):
    assert rs.billing_period(now, day) == (start.replace(tzinfo=timezone.utc),
                                           end.replace(tzinfo=timezone.utc))


def test_period_spent_sums_the_window_and_skips_bad_lines(tmp_path, caplog):
    ledger = tmp_path / "spend.jsonl"
    start, end = rs.billing_period(NOW, 1)
    assert rs.period_spent(ledger, start, end) == 0.0
    rs.append_ledger(ledger, {"ts": "2026-10-02T10:00:00+00:00", "usd": 2.5})
    rs.append_ledger(ledger, {"ts": "2026-09-30T23:59:59+00:00", "usd": 50})
    ledger.write_text(ledger.read_text() + "{broken")            # torn last line, no newline
    rs.append_ledger(ledger, {"ts": "2026-10-08T09:00:00Z", "usd": 1.0})
    with caplog.at_level("WARNING"):
        assert rs.period_spent(ledger, start, end) == pytest.approx(3.5)
    assert "line 3 unreadable" in caplog.text
    assert len(ledger.read_text().splitlines()) == 4               # the torn line was closed off


def test_append_ledger_never_loses_concurrent_lines(tmp_path):
    ledger = tmp_path / "spend.jsonl"

    def writer(worker):
        for index in range(50):
            rs.append_ledger(ledger, {"ts": NOW.isoformat(), "usd": 0.01, "worker": worker, "i": index})

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 400 and len({(r["worker"], r["i"]) for r in rows}) == 400


def test_ledger_lock_is_exclusive_and_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "LEDGER_LOCK_POLL_S", 0.01)
    ledger = tmp_path / "research" / "spend.jsonl"
    with rs.ledger_lock(ledger):
        assert (tmp_path / "research" / "spend.jsonl.lock").exists()
        errors = []

        def contender():
            try:
                with rs.ledger_lock(ledger, wait_s=0.1):
                    pass
            except rs.LedgerBusy as exc:
                errors.append(exc)

        thread = threading.Thread(target=contender)
        thread.start()
        thread.join()
        assert len(errors) == 1
    with rs.ledger_lock(ledger, wait_s=0.1):                     # released on exit
        pass


# ---------------------------------------------------------------------------
# Fake Anthropic client
# ---------------------------------------------------------------------------

NAMES = {el["id"]: el["web_name"] for el in ELEMENTS}


def _research_response(stop_reason="end_turn", input_tokens=10_000, searches=2, url=SOURCE,
                       name="Doubt", page_age=PAGE_AGE):
    page = _page(name)
    fetched = SimpleNamespace(
        type="web_fetch_result", url=url, retrieved_at="2026-10-08T08:00:00Z",
        content=SimpleNamespace(type="document", title="Injury update",
                                source=SimpleNamespace(type="text", data=page)))
    content = [
        SimpleNamespace(type="web_search_tool_result",
                        content=[SimpleNamespace(type="web_search_result", url=url,
                                                 title="Injury update", page_age=page_age)]),
        SimpleNamespace(type="web_fetch_tool_result", content=fetched),
        SimpleNamespace(type="text", text=f"{name} trained fully on Thursday.",
                        citations=[SimpleNamespace(url=url, title="Injury update",
                                                   cited_text=_quote(name))]),
        SimpleNamespace(type="text", text="\nASSESSMENT: p_start 0.6, confidence high.", citations=None),
    ]
    return SimpleNamespace(content=content, stop_reason=stop_reason, stop_details=None,
                           model=rs.MODEL, usage=_usage(input_tokens, 1_000, searches))


def _extraction_response(payload, stop_reason="end_turn"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason,
                           stop_details=None, model=rs.MODEL, usage=_usage(2_000, 500))


def _verify_response(verdict="supports", stop_reason="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""),
                                    SimpleNamespace(type="text", text=json.dumps({"verdict": verdict}))],
                           stop_reason=stop_reason, stop_details=None, model=rs.VERIFY_MODEL,
                           usage=_usage(300, 50))


def _finding_for(element, claim="expected_start", **fields):
    """A finding whose evidence quotes the fake page for ``claim``."""
    name = NAMES.get(element, "Doubt")
    return _finding(element=element, web_name=name, status_claim=claim,
                    evidence=[_evidence(quote=_quote(name, claim))], **fields)


class FakeClient:
    """``client.messages`` / ``client.beta.messages`` stand-in: research calls
    (with tools) go to ``research``, verifier calls (``VERIFY_MODEL``) to
    ``verify``, extraction calls to ``extract`` (element parsed from the
    prompt)."""

    def __init__(self, research=None, extract=None, verify=None):
        self.calls: list[dict] = []
        self.research = research or (lambda call: _research_response(name=_prompt_name(call)))
        self.extract = extract or (lambda element, call: _extraction_response(_finding_for(element)))
        self.verify = verify or (lambda call: _verify_response())
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._beta_create))
        self.messages = SimpleNamespace(create=self._create)

    def _beta_create(self, **params):
        return self._create(_endpoint="beta", **params)

    def _create(self, _endpoint="messages", **params):
        self.calls.append({**params, "_endpoint": _endpoint})
        if params["model"] == rs.VERIFY_MODEL:
            return self.verify(params)
        if "tools" in params:
            return self.research(params)
        element = int(re.search(r"element (\d+)", params["messages"][0]["content"]).group(1))
        return self.extract(element, params)

    def count(self, kind):
        if kind == "verify":
            return sum(c["model"] == rs.VERIFY_MODEL for c in self.calls)
        if kind == "research":
            return sum("tools" in c for c in self.calls)
        return sum("tools" not in c and c["model"] != rs.VERIFY_MODEL for c in self.calls)


def _prompt_name(call):
    return re.search(r"Player: (.+?) —", call["messages"][0]["content"]).group(1)


CTX = rs.RunContext(gw=GW, phase="waivers", today="2026-10-08", cutoff="2026-09-26",
                    last_finished_gw=5, event=BOOTSTRAP["events"]["data"][1], now=NOW)
RESEARCH_USD = (10_000 * 4 + 1_000 * 20) / 1e6 + 0.02
EXTRACT_USD = (2_000 * 4 + 500 * 20) / 1e6
VERIFY_USD = (300 * 0.10 + 50 * 0.50) / 1e6


def _guard(cap=100.0):
    return rs.SpendGuard(cap_usd=cap)


def test_research_player_uses_documented_request_shape_and_validates():
    client, guard = FakeClient(), _guard()
    result = rs.research_player(client, _cand(), CTX, rs.Config(), guard)
    assert result.status == "ok" and result.finding["p_start"] == 0.6
    assert result.decision == "applied" and PAGE in result.seen_text[SOURCE]
    assert result.page_ages == {SOURCE: [PAGE_AGE]}
    research, extraction, verify = client.calls
    assert research["model"] == extraction["model"] == "claude-opus-5-5"
    assert research["_endpoint"] == extraction["_endpoint"] == "beta"
    assert research["fallbacks"] == "default" and research["betas"] == [rs.FALLBACK_BETA]
    assert research["thinking"] == {"type": "adaptive"}
    assert research["output_config"] == {"effort": "high"}
    assert [t["type"] for t in research["tools"]] == ["web_search_20260318", "web_fetch_20260318"]
    assert "Outdated before: 2026-09-26" in research["messages"][0]["content"]
    assert extraction["output_config"]["format"]["type"] == "json_schema"
    assert "tools" not in extraction and SOURCE in extraction["messages"][0]["content"]
    assert f"listed {PAGE_AGE}" in extraction["messages"][0]["content"]
    # the verifier sees only the player, the claim and the passage
    assert verify["model"] == "claude-haiku-5-5" and verify["_endpoint"] == "messages"
    assert "fallbacks" not in verify and "tools" not in verify
    assert verify["output_config"]["format"]["schema"] == rs.VERIFY_SCHEMA
    assert verify["messages"][0]["content"] == (
        "Player: Doubt, Wolves.\nClaimed status: expected_start — expected to start the next "
        f"match.\n\n<passage>\n{QUOTE}\n</passage>")
    assert result.cost_usd == pytest.approx(RESEARCH_USD + EXTRACT_USD + VERIFY_USD)
    assert guard.spent_usd == pytest.approx(result.cost_usd) and result.api_calls == 3


def test_research_player_verifier_unrelated_keeps_it_proposed():
    client = FakeClient(verify=lambda call: _verify_response("unrelated"))
    result = rs.research_player(client, _cand(), CTX, rs.Config(), _guard())
    assert result.status == "ok" and result.decision == "proposed"
    assert result.finding["evidence"][0]["verifier"] == "unrelated"


@pytest.mark.parametrize("verify, verdict", [
    (lambda call: _verify_response("supports", stop_reason="max_tokens"), "error:max_tokens"),
    (lambda call: _extraction_response("{not json"), "error:not_json"),
    (lambda call: _extraction_response({"verdict": "maybe"}), "error:bad_verdict"),
])
def test_verifier_failures_never_count_as_support(verify, verdict):
    result = rs.research_player(FakeClient(verify=verify), _cand(), CTX, rs.Config(), _guard())
    assert result.decision == "proposed" and result.finding["evidence"][0]["verifier"] == verdict


def test_research_player_resumes_pause_turn():
    responses = iter([_research_response("pause_turn"), _research_response()])
    client = FakeClient(research=lambda call: next(responses))
    result = rs.research_player(client, _cand(), CTX, rs.Config(), _guard())
    assert result.status == "ok" and len(client.calls) == 4
    resumed = client.calls[1]["messages"]
    assert resumed[1]["role"] == "assistant" and len(resumed[1]["content"]) == 4


def test_research_player_gives_up_after_max_continuations():
    client = FakeClient(research=lambda call: _research_response("pause_turn"))
    result = rs.research_player(client, _cand(), CTX, rs.Config(), _guard())
    assert result.status == "failed" and "pause_turn" in result.reason
    assert len(client.calls) == rs.MAX_CONTINUATIONS + 1


def test_research_player_handles_refusal_and_max_tokens():
    refused = _research_response("refusal")
    refused.stop_details = SimpleNamespace(category="cyber")
    result = rs.research_player(FakeClient(research=lambda c: refused), _cand(), CTX, rs.Config(), _guard())
    assert result.status == "refused" and "cyber" in result.reason
    result = rs.research_player(FakeClient(research=lambda c: _research_response("max_tokens")),
                                _cand(), CTX, rs.Config(), _guard())
    assert result.status == "failed" and "max_tokens" in result.reason


@pytest.mark.parametrize("extract, reason", [
    (lambda element, call: _extraction_response("{not json"), "not JSON"),
    (lambda element, call: _extraction_response(_finding(element=99)), "not the researched"),
    (lambda element, call: _extraction_response({}, stop_reason="max_tokens"), "max_tokens"),
])
def test_bad_extraction_is_invalid(extract, reason):
    result = rs.research_player(FakeClient(extract=extract), _cand(), CTX, rs.Config(), _guard())
    assert result.status == "invalid" and reason in result.reason


def _raiser(cls, status):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def handler(*args):
        raise cls("boom", response=httpx.Response(status, request=request), body=None)
    return handler


def test_rate_limit_fails_one_player_but_auth_error_aborts():
    guard = _guard()
    result = rs.research_player(FakeClient(research=_raiser(anthropic.RateLimitError, 429)),
                                _cand(), CTX, rs.Config(), guard)
    assert result.status == "failed" and "RateLimitError" in result.reason
    assert guard.spent_usd == 0 and guard.reserved_usd == 0
    with pytest.raises(rs.ResearchAbort):
        rs.research_player(FakeClient(research=_raiser(anthropic.AuthenticationError, 401)),
                           _cand(), CTX, rs.Config(), guard)
    assert guard.reserved_usd == 0


def test_unanswered_request_is_counted_at_its_reservation():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def timeout(call):
        raise anthropic.APITimeoutError(request=request)

    guard = _guard()
    result = rs.research_player(FakeClient(research=timeout), _cand(), CTX, rs.Config(), guard)
    assert result.status == "failed" and "APITimeoutError" in result.reason
    assert result.cost_usd == pytest.approx(guard.spent_usd) and guard.spent_usd > 1.0


def test_extraction_crash_keeps_the_research_cost():
    def crash(element, call):
        raise RuntimeError("SDK shape surprise")

    guard = _guard()
    result = rs.research_player(FakeClient(extract=crash), _cand(), CTX, rs.Config(), guard)
    assert result.status == "failed" and "RuntimeError" in result.reason
    assert result.cost_usd == pytest.approx(RESEARCH_USD) == pytest.approx(guard.spent_usd)
    assert guard.cost_for(2) == pytest.approx(RESEARCH_USD) and guard.reserved_usd == 0


def test_expensive_request_is_never_started_over_the_cap():
    client = FakeClient(research=lambda call: _research_response(input_tokens=5_000_000))
    guard = _guard(cap=0.50)
    result = rs.research_player(client, _cand(), CTX, rs.Config(), guard)
    assert result.status == "budget" and "over the run cap" in result.reason
    assert client.calls == [] and guard.spent_usd == 0 and guard.exhausted


def test_continuation_and_extraction_are_each_admitted(monkeypatch):
    monkeypatch.setattr(rs, "worst_case_usd", lambda params, fallback: 0.5)
    # each request reserves $0.5 and research settles at ~$0.08: a $0.55 cap
    # admits the first request but not a second ($0.08 spent + $0.5 > $0.55)
    pause = FakeClient(research=lambda call: _research_response("pause_turn"))
    result = rs.research_player(pause, _cand(), CTX, rs.Config(), _guard(cap=0.55))
    assert result.status == "budget" and "research not started" in result.reason
    assert len(pause.calls) == 1 and result.cost_usd == pytest.approx(RESEARCH_USD)
    done = FakeClient()
    result = rs.research_player(done, _cand(), CTX, rs.Config(), _guard(cap=0.55))
    assert result.status == "budget" and "extract not started" in result.reason
    assert len(done.calls) == 1 and result.cost_usd == pytest.approx(RESEARCH_USD)


def test_verifier_refused_by_the_cap_keeps_the_finding_proposed(monkeypatch):
    monkeypatch.setattr(rs, "worst_case_usd",
                        lambda params, fallback: 0.2 if params["model"] == rs.VERIFY_MODEL else 0.1)
    client = FakeClient()
    result = rs.research_player(client, _cand(), CTX, rs.Config(), _guard(cap=RESEARCH_USD + EXTRACT_USD + 0.15))
    assert result.status == "ok" and result.decision == "proposed"
    assert result.finding["evidence"][0]["verifier"] == "not_run:run_cap"
    assert client.count("verify") == 0


# ---------------------------------------------------------------------------
# research_all
# ---------------------------------------------------------------------------

def test_unexpected_error_fails_one_player_and_keeps_its_recorded_cost():
    guard = rs.SpendGuard(cap_usd=10.0)

    def research_one(cand):
        guard.settle(guard.reserve(1.0, key=cand.element, call="research"), 0.5)
        if cand.element == 2:
            raise KeyError("shape")
        return rs.PlayerResult(element=cand.element, web_name=cand.web_name, status="ok",
                               cost_usd=0.5, api_calls=1)

    outcome = rs.research_all([_cand(2), _cand(3, web_name="Other")], research_one, guard, concurrency=2)
    assert [(r.element, r.status) for r in outcome.results] == [(2, "failed"), (3, "ok")]
    assert "KeyError" in outcome.results[0].reason and outcome.results[0].cost_usd == pytest.approx(0.5)
    assert outcome.budget_skipped == [] and outcome.abort is None and not outcome.interrupted
    assert guard.reserved_usd == pytest.approx(0.0) and guard.spent_usd == pytest.approx(1.0)


def test_research_all_skips_everyone_once_the_cap_is_exhausted():
    guard = rs.SpendGuard(cap_usd=1.0)

    def research_one(cand):
        reservation = guard.reserve(0.9, key=cand.element, call="research")
        if reservation is None:
            return rs.PlayerResult(element=cand.element, web_name=cand.web_name, status="budget")
        guard.settle(reservation, 0.5)                           # then 0.5 + 0.9 > 1.0
        return rs.PlayerResult(element=cand.element, web_name=cand.web_name, status="ok", api_calls=1)

    cands = [_cand(2), _cand(3, web_name="B"), _cand(4, web_name="C")]
    outcome = rs.research_all(cands, research_one, guard, concurrency=1)
    assert [r.element for r in outcome.results] == [2]
    assert outcome.budget_skipped == [{"element": 3, "web_name": "B", "reason": "run_cap"},
                                      {"element": 4, "web_name": "C", "reason": "run_cap"}]


# ---------------------------------------------------------------------------
# run() end to end (fake client, tmp data root)
# ---------------------------------------------------------------------------

def _data_root(tmp_path: Path, waiver_plan=WAIVER_PLAN, my_week=MY_WEEK) -> Path:
    raw = tmp_path / "raw" / SEASON
    (raw / "bootstrap").mkdir(parents=True)
    (raw / "bootstrap/bootstrap-static.json").write_text(json.dumps(BOOTSTRAP))
    league = raw / "league" / str(LEAGUE)
    league.mkdir(parents=True)
    (league / "element-status.json").write_text(json.dumps({"element_status": [
        {"element": e, "owner": ENTRY} for e in sorted(SQUAD)]}))
    ml = tmp_path / "derived" / SEASON / "ml"
    ml.mkdir(parents=True)
    if waiver_plan is not None:
        (ml / "waiver_plan.json").write_text(json.dumps(waiver_plan))
    if my_week is not None:
        (ml / "my_week.json").write_text(json.dumps(my_week))
    return tmp_path


def _args(root: Path, **fields) -> argparse.Namespace:
    base = {"season": SEASON, "data_root": root, "phase": "waivers", "gw": None, "dry_run": False,
            "max_usd": None, "max_players": None, "league": LEAGUE, "entry": ENTRY}
    base.update(fields)
    return argparse.Namespace(**base)


def _ml(root: Path) -> Path:
    return root / "derived" / SEASON / "ml"


def _ledger_rows(root: Path) -> list[dict]:
    return [json.loads(line) for line in (_ml(root) / "research/spend.jsonl").read_text().splitlines()]


def _run(root, monkeypatch, client=None, now=NOW, **fields):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("RESEARCH_CONCURRENCY", "1")
    client = client or FakeClient()
    code = rs.run(_args(root, **fields), client_factory=lambda key: client, now=now)
    return code, client


def _run_doc(root):
    return json.loads(next((_ml(root) / "research").glob("gw6_*.json")).read_text())


def test_run_writes_findings_tagged_overrides_and_ledger(tmp_path, monkeypatch, capsys):
    root = _data_root(tmp_path)

    def extract(element, call):
        if element == 4:                                    # Starter: out until GW8
            return _extraction_response(_finding_for(4, "ruled_out", p_start=0.1, return_gw=8))
        if element == 3:                                    # Bencher: too unsure to apply
            return _extraction_response(_finding_for(3, confidence="low"))
        if element == 12:                                   # Mover: nothing new
            return _extraction_response(_finding_for(12, "no_new_info"))
        return _extraction_response(_finding_for(element, "doubtful"))

    code, client = _run(root, monkeypatch, FakeClient(extract=extract))
    assert code == rs.EXIT_OK
    runs = list((_ml(root) / "research").glob("gw6_waivers_*.json"))
    assert [p.name for p in runs] == ["gw6_waivers_20261008T090000Z.json"]
    doc = json.loads(runs[0].read_text())
    assert [f["status"] for f in doc["findings"]] == ["ok"] * 5
    assert [f["decision"] for f in doc["findings"]] == ["applied", "applied", "proposed",
                                                        "applied", "no_change"]
    assert "seen_text" not in doc["findings"][0] and "page_ages" not in doc["findings"][0]
    assert doc["overrides_written"] == 3 and doc["budget_skipped"] == [] and doc["interrupted"] is False
    proposed = doc["decisions"]["proposed"]
    assert [(e["player"], e["model_p_start"], e["p_start"]) for e in proposed] == [("Bencher", 0.3, 0.9)]
    assert client.count("verify") == 4                      # every claim but no_new_info
    assert doc["cost"]["run_usd"] == pytest.approx(5 * RESEARCH_USD + 5 * EXTRACT_USD + 4 * VERIFY_USD,
                                                   abs=1e-4)
    assert doc["cost"]["period_start"] == "2026-10-01" and doc["cost"]["unsettled_usd"] == 0
    overrides = json.loads((_ml(root) / wv.RESEARCH_OVERRIDES_FILE).read_text())["overrides"]
    by_name = {o["player"]: o for o in overrides}
    assert set(by_name) == {"Starter", "Doubt", "RecAdd"}
    assert by_name["Starter"]["return_gw"] == 8 and "valid_through_gw" not in by_name["Starter"]
    assert by_name["Starter"]["p_start"] == 0.0 and by_name["Starter"]["llm_p_start"] == 0.1
    assert by_name["Doubt"]["valid_through_gw"] == GW and "return_gw" not in by_name["Doubt"]
    assert by_name["Doubt"]["p_start"] == rs.CLAIM_P_START["doubtful"]
    assert by_name["Doubt"]["decision"] == "applied"
    assert by_name["Doubt"]["evidence"][0]["source_published_at"] == "2026-10-07"
    assert all(o["source"] == "research" and o["fact"].startswith("research:") and o["evidence"]
               for o in overrides)
    assert by_name["Doubt"]["research_run"] == doc["run_id"] and by_name["Doubt"]["code"] == 102
    rows = _ledger_rows(root)
    assert len(rows) == len(client.calls) == 14             # one line per settled request
    assert {r["call"] for r in rows} == {"research", "extract", "verify"}
    assert sum(r["usd"] for r in rows) == pytest.approx(doc["cost"]["run_usd"], abs=1e-4)
    assert all(r["run_id"] == doc["run_id"] and r["estimated"] is False for r in rows)
    assert "== RESEARCH GW6 waivers" in capsys.readouterr().out
    # the derived artifacts read it back, tagged, below any manual entry
    entries = wv.load_all_role_overrides(_ml(root))
    assert all(e["override_origin"] == "research" for e in entries)


def test_run_cap_below_one_request_starts_nothing(tmp_path, monkeypatch):
    root = _data_root(tmp_path)
    client = FakeClient(research=lambda call: _research_response(input_tokens=5_000_000))
    code, client = _run(root, monkeypatch, client, max_usd=0.5)
    assert code == rs.EXIT_OK and client.calls == []
    doc = _run_doc(root)
    assert doc["findings"] == [] and doc["cost"]["run_usd"] == 0
    assert [s["reason"] for s in doc["budget_skipped"]] == ["run_cap"] * 5


def test_run_cap_stops_starting_players_mid_run(tmp_path, monkeypatch):
    root = _data_root(tmp_path)
    monkeypatch.setattr(rs, "worst_case_usd", lambda params, fallback: 0.9)
    # each research call costs 200k input = $0.80 (+ searches) ; extraction ~$0.018
    client = FakeClient(research=lambda call: _research_response(input_tokens=200_000, name=_prompt_name(call)))
    code, client = _run(root, monkeypatch, client, max_usd=1.5)
    assert code == rs.EXIT_OK
    doc = _run_doc(root)
    # Starter's research ($0.84) fits; its extraction would need 0.84 + 0.9 > 1.5
    assert [(f["web_name"], f["status"]) for f in doc["findings"]] == [("Starter", "budget")]
    assert [s["web_name"] for s in doc["budget_skipped"]] == ["Doubt", "Bencher", "RecAdd", "Mover"]
    assert doc["cost"]["run_usd"] <= 1.5 and client.count("research") == 1


def test_monthly_cap_refuses_to_run(tmp_path, monkeypatch, capsys):
    root = _data_root(tmp_path)
    rs.append_ledger(_ml(root) / "research/spend.jsonl", {"ts": "2026-10-01T00:00:00+00:00", "usd": 90.0})
    code, client = _run(root, monkeypatch)
    assert code == rs.EXIT_MONTHLY_CAP and client.calls == []
    assert "monthly cap reached" in capsys.readouterr().err
    assert not (_ml(root) / wv.RESEARCH_OVERRIDES_FILE).exists()


def test_monthly_window_follows_the_billing_day(tmp_path, monkeypatch):
    root = _data_root(tmp_path)
    rs.append_ledger(_ml(root) / "research/spend.jsonl", {"ts": "2026-10-01T00:00:00+00:00", "usd": 90.0})
    monkeypatch.setenv("RESEARCH_BILLING_DAY", "2")        # that spend is in the previous cycle
    code, _ = _run(root, monkeypatch)
    assert code == rs.EXIT_OK and _run_doc(root)["cost"]["period_start"] == "2026-10-02"
    monkeypatch.setenv("RESEARCH_BILLING_DAY", "15")       # Sep 15 - Oct 15: it counts
    code, _ = _run(root, monkeypatch, now=NOW + timedelta(seconds=1))
    assert code == rs.EXIT_MONTHLY_CAP


def test_monthly_remainder_caps_the_run(tmp_path, monkeypatch):
    root = _data_root(tmp_path)
    rs.append_ledger(_ml(root) / "research/spend.jsonl", {"ts": "2026-10-01T00:00:00+00:00", "usd": 89.0})
    code, _ = _run(root, monkeypatch)
    doc = _run_doc(root)
    assert code == rs.EXIT_OK and doc["cost"]["run_cap_usd"] == pytest.approx(1.0)
    assert doc["cost"]["run_usd"] <= 1.0


def test_overlapping_runs_near_the_cap_stay_within_it(tmp_path, monkeypatch):
    root = _data_root(tmp_path)
    ledger = _ml(root) / "research/spend.jsonl"
    rs.append_ledger(ledger, {"ts": "2026-10-01T00:00:00+00:00", "usd": 89.0})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("RESEARCH_CONCURRENCY", "1")
    monkeypatch.setattr(rs, "worst_case_usd", lambda params, fallback: 0.3)
    monkeypatch.setattr(rs, "LEDGER_LOCK_POLL_S", 0.01)

    def slow_research(call):
        time.sleep(0.02)
        return _research_response(input_tokens=50_000, name=_prompt_name(call))   # ~$0.24

    clients = [FakeClient(research=slow_research), FakeClient(research=slow_research)]
    codes = {}

    def go(index):
        codes[index] = rs.run(_args(root), client_factory=lambda key: clients[index],
                              now=NOW + timedelta(seconds=index))

    threads = [threading.Thread(target=go, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert codes == {0: rs.EXIT_OK, 1: rs.EXIT_OK}
    rows = _ledger_rows(root)
    assert len(rows) == 1 + len(clients[0].calls) + len(clients[1].calls)     # no lost lines
    assert sum(r["usd"] for r in rows) <= 90.0 + 1e-6
    assert len(clients[0].calls) + len(clients[1].calls) > 0


def test_interrupt_after_one_billed_response_keeps_it_in_the_ledger(tmp_path, monkeypatch, capsys):
    root = _data_root(tmp_path)

    def research(call):
        _thread.interrupt_main()            # Ctrl-C while this request is in flight
        time.sleep(1.0)
        return _research_response(name=_prompt_name(call))

    code, client = _run(root, monkeypatch, FakeClient(research=research))
    assert code == rs.EXIT_INTERRUPTED and len(client.calls) == 1
    rows = _ledger_rows(root)
    assert [(r["call"], r["estimated"]) for r in rows] == [("research", False)]
    assert rows[0]["usd"] == pytest.approx(RESEARCH_USD, abs=1e-6)
    doc = _run_doc(root)
    assert doc["interrupted"] is True and doc["overrides_written"] == 0
    assert [(f["web_name"], f["status"]) for f in doc["findings"]] == [("Starter", "interrupted")]
    assert {s["reason"] for s in doc["budget_skipped"]} == {"interrupted"}
    assert not (_ml(root) / wv.RESEARCH_OVERRIDES_FILE).exists()
    assert "interrupted" in capsys.readouterr().err


@pytest.mark.skipif(threading.current_thread() is not threading.main_thread(),
                    reason="signal handlers need the main thread")
def test_sigterm_is_handled_like_ctrl_c(tmp_path, monkeypatch):
    root = _data_root(tmp_path)
    previous = signal.getsignal(signal.SIGTERM)

    def research(call):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1.0)
        return _research_response(name=_prompt_name(call))

    code, _ = _run(root, monkeypatch, FakeClient(research=research))
    assert code == rs.EXIT_INTERRUPTED and [r["call"] for r in _ledger_rows(root)] == ["research"]
    assert signal.getsignal(signal.SIGTERM) is previous


def test_missing_api_key_exits_3_without_a_client(tmp_path, monkeypatch, capsys):
    root = _data_root(tmp_path)

    def no_client(key):
        raise AssertionError("client must not be built without a key")

    assert rs.run(_args(root), client_factory=no_client, now=NOW) == rs.EXIT_NO_API_KEY
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not (_ml(root) / "research").exists()


def test_main_exit_codes(tmp_path, monkeypatch):
    root = _data_root(tmp_path)
    assert rs.main(["run", "--phase", "lineup", "--data-root", str(root)]) == rs.EXIT_NO_API_KEY
    with pytest.raises(SystemExit) as exc:
        rs.main(["run", "--phase", "bogus", "--data-root", str(root)])
    assert exc.value.code == rs.EXIT_USAGE
    monkeypatch.setenv("RESEARCH_EFFORT", "extreme")
    assert rs.main(["run", "--phase", "lineup", "--data-root", str(root)]) == rs.EXIT_USAGE


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "-1", "0"])
def test_non_finite_or_non_positive_caps_exit_2(tmp_path, monkeypatch, value):
    root = _data_root(tmp_path)
    base = ["run", "--phase", "lineup", "--data-root", str(root)]
    # "--max-usd=-inf": argparse would read a bare "-inf" as an option flag
    assert rs.main(base + [f"--max-usd={value}"]) == rs.EXIT_USAGE
    monkeypatch.setenv("RESEARCH_MAX_USD", value)
    assert rs.main(base) == rs.EXIT_USAGE
    monkeypatch.delenv("RESEARCH_MAX_USD")
    monkeypatch.setenv("RESEARCH_MONTHLY_USD", value)
    assert rs.main(base) == rs.EXIT_USAGE


@pytest.mark.parametrize("value", ["0", "29", "x"])
def test_bad_billing_day_exits_2(tmp_path, monkeypatch, value):
    monkeypatch.setenv("RESEARCH_BILLING_DAY", value)
    root = _data_root(tmp_path)
    assert rs.main(["run", "--phase", "lineup", "--data-root", str(root)]) == rs.EXIT_USAGE


def test_config_defaults():
    config = rs.resolve_config()
    assert (config.max_players, config.max_usd, config.billing_day) == (25, 12.0, 1)


def test_dry_run_needs_no_key_and_writes_nothing(tmp_path, capsys):
    root = _data_root(tmp_path, waiver_plan=None, my_week=None)
    assert rs.run(_args(root, dry_run=True), now=NOW) == rs.EXIT_OK
    out = capsys.readouterr().out
    assert "DRY RUN GW6 waivers: 1 player(s)" in out and "Doubt" in out
    assert "each research request reserves up to $" in out
    assert not (_ml(root) / "research").exists()


def test_auth_error_aborts_the_run_and_still_records_spend(tmp_path, monkeypatch):
    root = _data_root(tmp_path)
    code, client = _run(root, monkeypatch, FakeClient(research=_raiser(anthropic.AuthenticationError, 401)))
    assert code == rs.EXIT_ERROR and len(client.calls) == 1
    doc = _run_doc(root)
    assert "AuthenticationError" in doc["aborted"] and doc["findings"] == []
    assert doc["cost"]["run_usd"] == 0 and doc["cost"]["unsettled_usd"] == 0


def test_another_run_holding_the_ledger_lock_exits_1(tmp_path, monkeypatch, capsys):
    root = _data_root(tmp_path)
    monkeypatch.setattr(rs, "LEDGER_LOCK_WAIT_S", 0.05)
    monkeypatch.setattr(rs, "LEDGER_LOCK_POLL_S", 0.01)
    with rs.ledger_lock(_ml(root) / "research/spend.jsonl"):
        done = []
        thread = threading.Thread(target=lambda: done.append(_run(root, monkeypatch)))
        thread.start()
        thread.join()
    code, client = done[0]
    assert code == rs.EXIT_ERROR and client.calls == []
    assert "held by another research run" in capsys.readouterr().err


def test_rerun_keeps_live_entries_for_players_not_rechecked(tmp_path, monkeypatch):
    root = _data_root(tmp_path)
    (_ml(root) / wv.RESEARCH_OVERRIDES_FILE).write_text(json.dumps({"overrides": [
        {"player": "QuietFA", "team": "ARS", "code": 111, "p_start": 0.2, "valid_through_gw": GW,
         "fact": "research: old", "source": "research"},
        {"player": "Fit", "team": "WOL", "code": 101, "p_start": 0.2, "valid_through_gw": GW - 1,
         "fact": "research: stale", "source": "research"},
        {"player": "Doubt", "team": "WOL", "code": 102, "p_start": 0.0, "valid_through_gw": GW,
         "fact": "research: replaced", "source": "research"}]}))
    code, _ = _run(root, monkeypatch, phase="lineup")
    assert code == rs.EXIT_OK
    overrides = json.loads((_ml(root) / wv.RESEARCH_OVERRIDES_FILE).read_text())["overrides"]
    by_code = {o["code"]: o for o in overrides}
    assert by_code[111]["fact"] == "research: old"          # live, not re-researched: kept
    assert 101 not in by_code                               # stale: dropped
    assert by_code[102]["p_start"] == rs.CLAIM_P_START["expected_start"]   # replaced


def test_clear_removes_the_research_file(tmp_path, capsys):
    root = _data_root(tmp_path)
    path = _ml(root) / wv.RESEARCH_OVERRIDES_FILE
    path.write_text("{}")
    assert rs.main(["clear", "--data-root", str(root)]) == rs.EXIT_OK
    assert not path.exists()
    assert rs.main(["clear", "--data-root", str(root)]) == rs.EXIT_OK
    assert "nothing to clear" in capsys.readouterr().out


def test_write_atomic_never_leaves_a_half_written_file(tmp_path, monkeypatch):
    path = tmp_path / "out" / "role_overrides.research.json"
    jsonutil.write_atomic(path, '{"v": 1}')
    assert json.loads(path.read_text()) == {"v": 1}

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(jsonutil.os, "replace", failing_replace)
    with pytest.raises(OSError):
        jsonutil.write_atomic(path, '{"v": 2}')
    assert json.loads(path.read_text()) == {"v": 1}
    assert [p.name for p in path.parent.iterdir()] == [path.name]
