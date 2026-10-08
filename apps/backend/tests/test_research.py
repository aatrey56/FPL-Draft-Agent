"""research — triage, validation, spend guard, outputs. The Anthropic client
is always a fake: no test touches the network."""
import argparse
import json
import re
from datetime import datetime, timezone
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
QUOTE = "He trained fully on Thursday and is available"
PAGE = f"Team news. {QUOTE} for Saturday, the head coach said."

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
                 "RESEARCH_MAX_PLAYERS", "RESEARCH_CONCURRENCY",
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
                                    "published_at": "2026-10-07", "quote": QUOTE}]


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
    (_finding(evidence=[_evidence(published_at="last week")]), "no valid evidence"),
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


def _decide(evidence, seen=SEEN, cutoff="2026-09-26", **fields):
    finding = _finding(evidence=evidence, **fields)
    return rs.decide(finding, seen, cutoff), finding


def test_classify_source_by_domain():
    assert rs.classify_source("https://www.wolves.co.uk/news/x") == (1, "wolves")
    assert rs.classify_source("https://news.bbc.co.uk/a") == (2, "bbc")
    assert rs.classify_source("https://www.bbc.com/a") == (2, "bbc")
    assert rs.classify_source("https://notbbc.co.uk/a") == (3, "notbbc.co.uk")
    assert rs.classify_source("https://Transfer-Gossip.example:443/a") == (3, "transfer-gossip.example")


def test_verified_tier_one_source_is_applied_and_evidence_annotated():
    (decision, reasons), finding = _decide([_evidence()])
    assert decision == "applied" and reasons == ["verified tier-1 source"]
    assert finding["evidence"][0] == {**_evidence(), "tier": 1, "outlet": "wolves",
                                      "verified": True, "fresh": True}


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


def test_stale_credible_sources_are_proposed():
    (decision, reasons), _ = _decide([_evidence(published_at="2026-09-01")])
    assert decision == "proposed" and reasons == ["no credible source on/after 2026-09-26"]
    (decision, _), _ = _decide([_evidence(published_at="2026-09-01")], cutoff=None)
    assert decision == "applied"                                # no finished GW yet


def test_no_new_info_changes_nothing():
    (decision, _), _ = _decide([_evidence()], status_claim="no_new_info")
    assert decision == "no_change"


def test_quote_matching_ignores_case_whitespace_and_typography():
    seen = {SOURCE: "He said: \u201cI\u2019m  fit\u201d \u2014 ready."}
    assert rs.quote_verified('"i\'m fit" - ready', seen, SOURCE + "#top")
    assert not rs.quote_verified("I'm unfit", seen, SOURCE)


# ---------------------------------------------------------------------------
# Cost + spend guard
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


def test_estimated_player_cost_matches_the_constants():
    assert rs.estimated_player_cost() == pytest.approx(
        (60_000 * 4 + 6_000 * 20) / 1e6 + 5 * 0.01 + (4_000 * 4 + 1_500 * 20) / 1e6)


def test_spend_guard_projects_from_actual_costs_and_stays_stopped():
    guard = rs.SpendGuard(cap_usd=2.5, prior_usd=0.5)
    first = guard.try_start()
    assert first == 0.5
    guard.finish(first, 1.0)                     # projection becomes the mean actual: 1.0
    second = guard.try_start()                   # 1.0 spent + 1.0 projected <= 2.5
    assert second == 1.0
    assert guard.try_start() is None             # 1.0 + 1.0 reserved + 1.0 > 2.5
    guard.finish(second, 0.1)
    assert guard.try_start() is None             # once exhausted, never restarts
    assert guard.spent_usd == pytest.approx(1.1) and guard.reserved_usd == pytest.approx(0.0)


def test_month_spent_sums_the_month_and_skips_bad_lines(tmp_path, caplog):
    ledger = tmp_path / "spend.jsonl"
    assert rs.month_spent(ledger, "2026-10") == 0.0
    rs.append_ledger(ledger, {"month": "2026-10", "usd": 2.5})
    rs.append_ledger(ledger, {"month": "2026-09", "usd": 50})
    ledger.write_text(ledger.read_text() + "{broken\n")
    rs.append_ledger(ledger, {"month": "2026-10", "usd": 1.0})
    with caplog.at_level("WARNING"):
        assert rs.month_spent(ledger, "2026-10") == pytest.approx(3.5)
    assert "line 3 unreadable" in caplog.text


# ---------------------------------------------------------------------------
# Fake Anthropic client
# ---------------------------------------------------------------------------

def _research_response(stop_reason="end_turn", input_tokens=10_000, searches=2, url=SOURCE):
    fetched = SimpleNamespace(
        type="web_fetch_result", url=url, retrieved_at="2026-10-08T08:00:00Z",
        content=SimpleNamespace(type="document", title="Injury update",
                                source=SimpleNamespace(type="text", data=PAGE)))
    content = [
        SimpleNamespace(type="web_search_tool_result",
                        content=[SimpleNamespace(type="web_search_result", url=url,
                                                 title="Injury update", page_age="October 7, 2026")]),
        SimpleNamespace(type="web_fetch_tool_result", content=fetched),
        SimpleNamespace(type="text", text="He trained fully on Thursday.",
                        citations=[SimpleNamespace(url=url, title="Injury update",
                                                   cited_text=QUOTE)]),
        SimpleNamespace(type="text", text="\nASSESSMENT: p_start 0.6, confidence high.", citations=None),
    ]
    return SimpleNamespace(content=content, stop_reason=stop_reason, stop_details=None,
                           model=rs.MODEL, usage=_usage(input_tokens, 1_000, searches))


def _extraction_response(payload, stop_reason="end_turn"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason,
                           stop_details=None, model=rs.MODEL, usage=_usage(2_000, 500))


class FakeClient:
    """``client.beta.messages.create`` stand-in: research calls (with tools)
    go to ``research``, extraction calls to ``extract`` (element parsed from
    the prompt)."""

    def __init__(self, research=None, extract=None):
        self.calls: list[dict] = []
        self.research = research or (lambda call: _research_response())
        self.extract = extract or (lambda element, call: _extraction_response(
            _finding(element=element, web_name=f"P{element}")))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **params):
        self.calls.append(params)
        if "tools" in params:
            return self.research(params)
        element = int(re.search(r"element (\d+)", params["messages"][0]["content"]).group(1))
        return self.extract(element, params)


CTX = rs.RunContext(gw=GW, phase="waivers", today="2026-10-08", cutoff="2026-09-26",
                    last_finished_gw=5, event=BOOTSTRAP["events"]["data"][1])


def test_research_player_uses_documented_request_shape_and_validates():
    client = FakeClient()
    result = rs.research_player(client, _cand(), CTX, rs.Config())
    assert result.status == "ok" and result.finding["p_start"] == 0.6
    assert result.decision == "applied" and PAGE in result.seen_text[SOURCE]
    research, extraction = client.calls
    assert research["model"] == extraction["model"] == "claude-opus-5-5"
    assert research["fallbacks"] == "default" and research["betas"] == [rs.FALLBACK_BETA]
    assert research["thinking"] == {"type": "adaptive"}
    assert research["output_config"] == {"effort": "high"}
    assert [t["type"] for t in research["tools"]] == ["web_search_20260318", "web_fetch_20260318"]
    assert "Outdated before: 2026-09-26" in research["messages"][0]["content"]
    assert extraction["output_config"]["format"]["type"] == "json_schema"
    assert "tools" not in extraction and SOURCE in extraction["messages"][0]["content"]
    assert result.cost_usd == pytest.approx((10_000 * 4 + 1_000 * 20 + 2_000 * 4 + 500 * 20) / 1e6 + 0.02)


def test_research_player_resumes_pause_turn():
    responses = iter([_research_response("pause_turn"), _research_response()])
    client = FakeClient(research=lambda call: next(responses))
    result = rs.research_player(client, _cand(), CTX, rs.Config())
    assert result.status == "ok" and len(client.calls) == 3
    resumed = client.calls[1]["messages"]
    assert resumed[1]["role"] == "assistant" and len(resumed[1]["content"]) == 4


def test_research_player_gives_up_after_max_continuations():
    client = FakeClient(research=lambda call: _research_response("pause_turn"))
    result = rs.research_player(client, _cand(), CTX, rs.Config())
    assert result.status == "failed" and "pause_turn" in result.reason
    assert len(client.calls) == rs.MAX_CONTINUATIONS + 1


def test_research_player_handles_refusal_and_max_tokens():
    refused = _research_response("refusal")
    refused.stop_details = SimpleNamespace(category="cyber")
    result = rs.research_player(FakeClient(research=lambda c: refused), _cand(), CTX, rs.Config())
    assert result.status == "refused" and "cyber" in result.reason
    result = rs.research_player(FakeClient(research=lambda c: _research_response("max_tokens")),
                                _cand(), CTX, rs.Config())
    assert result.status == "failed" and "max_tokens" in result.reason


@pytest.mark.parametrize("extract, reason", [
    (lambda element, call: _extraction_response("{not json"), "not JSON"),
    (lambda element, call: _extraction_response(_finding(element=99)), "not the researched"),
    (lambda element, call: _extraction_response({}, stop_reason="max_tokens"), "max_tokens"),
])
def test_bad_extraction_is_invalid(extract, reason):
    result = rs.research_player(FakeClient(extract=extract), _cand(), CTX, rs.Config())
    assert result.status == "invalid" and reason in result.reason


def test_rate_limit_fails_one_player_but_auth_error_aborts():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def raise_(cls, status):
        def handler(call):
            raise cls("boom", response=httpx.Response(status, request=request), body=None)
        return handler

    result = rs.research_player(FakeClient(research=raise_(anthropic.RateLimitError, 429)),
                                _cand(), CTX, rs.Config())
    assert result.status == "failed" and "RateLimitError" in result.reason
    with pytest.raises(rs.ResearchAbort):
        rs.research_player(FakeClient(research=raise_(anthropic.AuthenticationError, 401)),
                           _cand(), CTX, rs.Config())


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


def _run(root, monkeypatch, client=None, **fields):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("RESEARCH_CONCURRENCY", "1")
    client = client or FakeClient()
    code = rs.run(_args(root, **fields), client_factory=lambda key: client, now=NOW)
    return code, client


def test_run_writes_findings_tagged_overrides_and_ledger(tmp_path, monkeypatch, capsys):
    root = _data_root(tmp_path)

    def extract(element, call):
        if element == 4:                                    # Starter: out until GW8
            return _extraction_response(_finding(element=4, p_start=0.1, return_gw=8,
                                                 status_claim="ruled_out"))
        if element == 3:                                    # Bencher: too unsure to apply
            return _extraction_response(_finding(element=3, confidence="low"))
        if element == 12:                                   # Mover: nothing new
            return _extraction_response(_finding(element=12, status_claim="no_new_info"))
        return _extraction_response(_finding(element=element, status_claim="doubtful"))

    code, client = _run(root, monkeypatch, FakeClient(extract=extract))
    assert code == rs.EXIT_OK
    runs = list((_ml(root) / "research").glob("gw6_waivers_*.json"))
    assert [p.name for p in runs] == ["gw6_waivers_20261008T090000Z.json"]
    doc = json.loads(runs[0].read_text())
    assert [f["status"] for f in doc["findings"]] == ["ok"] * 5
    assert [f["decision"] for f in doc["findings"]] == ["applied", "applied", "proposed",
                                                        "applied", "no_change"]
    assert "seen_text" not in doc["findings"][0]
    assert doc["overrides_written"] == 3 and doc["budget_skipped"] == []
    proposed = doc["decisions"]["proposed"]
    assert [(e["player"], e["model_p_start"], e["p_start"]) for e in proposed] == [("Bencher", 0.3, 0.9)]
    assert doc["cost"]["run_usd"] == pytest.approx(5 * rs.response_cost(_research_response())
                                                   + 5 * rs.response_cost(_extraction_response("")))
    overrides = json.loads((_ml(root) / wv.RESEARCH_OVERRIDES_FILE).read_text())["overrides"]
    by_name = {o["player"]: o for o in overrides}
    assert set(by_name) == {"Starter", "Doubt", "RecAdd"}
    assert by_name["Starter"]["return_gw"] == 8 and "valid_through_gw" not in by_name["Starter"]
    assert by_name["Starter"]["p_start"] == 0.0 and by_name["Starter"]["llm_p_start"] == 0.1
    assert by_name["Doubt"]["valid_through_gw"] == GW and "return_gw" not in by_name["Doubt"]
    assert by_name["Doubt"]["p_start"] == rs.CLAIM_P_START["doubtful"]
    assert by_name["Doubt"]["decision"] == "applied"
    assert all(o["source"] == "research" and o["fact"].startswith("research:") and o["evidence"]
               for o in overrides)
    assert by_name["Doubt"]["research_run"] == doc["run_id"] and by_name["Doubt"]["code"] == 102
    ledger = (_ml(root) / "research/spend.jsonl").read_text().splitlines()
    assert len(ledger) == 1 and json.loads(ledger[0])["usd"] == doc["cost"]["run_usd"]
    assert "== RESEARCH GW6 waivers" in capsys.readouterr().out
    # the derived artifacts read it back, tagged, below any manual entry
    entries = wv.load_all_role_overrides(_ml(root))
    assert all(e["override_origin"] == "research" for e in entries)


def test_run_cap_stops_starting_players_mid_run(tmp_path, monkeypatch):
    root = _data_root(tmp_path)
    # each research call costs 200k input = $0.80 (+ searches) ; extraction ~$0.018
    client = FakeClient(research=lambda call: _research_response(input_tokens=200_000))
    code, client = _run(root, monkeypatch, client, max_usd=1.5)
    assert code == rs.EXIT_OK
    doc = json.loads(next((_ml(root) / "research").glob("*.json")).read_text())
    assert [f["web_name"] for f in doc["findings"]] == ["Starter"]
    assert [s["web_name"] for s in doc["budget_skipped"]] == ["Doubt", "Bencher", "RecAdd", "Mover"]
    assert doc["cost"]["run_usd"] <= 1.5
    assert sum("tools" in c for c in client.calls) == 1


def test_monthly_cap_refuses_to_run(tmp_path, monkeypatch, capsys):
    root = _data_root(tmp_path)
    rs.append_ledger(_ml(root) / "research/spend.jsonl", {"month": "2026-10", "usd": 90.0})
    code, client = _run(root, monkeypatch)
    assert code == rs.EXIT_MONTHLY_CAP and client.calls == []
    assert "monthly cap reached" in capsys.readouterr().err
    assert not (_ml(root) / wv.RESEARCH_OVERRIDES_FILE).exists()


def test_monthly_remainder_caps_the_run(tmp_path, monkeypatch):
    root = _data_root(tmp_path)
    rs.append_ledger(_ml(root) / "research/spend.jsonl", {"month": "2026-10", "usd": 89.0})
    code, _ = _run(root, monkeypatch)
    doc = json.loads(next((_ml(root) / "research").glob("gw6_*.json")).read_text())
    assert code == rs.EXIT_OK and doc["cost"]["run_cap_usd"] == pytest.approx(1.0)


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


def test_dry_run_needs_no_key_and_writes_nothing(tmp_path, capsys):
    root = _data_root(tmp_path, waiver_plan=None, my_week=None)
    assert rs.run(_args(root, dry_run=True), now=NOW) == rs.EXIT_OK
    out = capsys.readouterr().out
    assert "DRY RUN GW6 waivers: 1 player(s)" in out and "Doubt" in out
    assert not (_ml(root) / "research").exists()


def test_auth_error_aborts_the_run_and_still_records_spend(tmp_path, monkeypatch):
    root = _data_root(tmp_path)
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def research(call):
        raise anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=request),
                                            body=None)

    code, client = _run(root, monkeypatch, FakeClient(research=research))
    assert code == rs.EXIT_ERROR and len(client.calls) == 1
    doc = json.loads(next((_ml(root) / "research").glob("gw6_*.json")).read_text())
    assert "AuthenticationError" in doc["aborted"] and doc["findings"] == []
    assert (_ml(root) / "research/spend.jsonl").exists()


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


def test_unexpected_error_fails_one_player_and_releases_its_budget():
    guard = rs.SpendGuard(cap_usd=10.0, prior_usd=1.0)

    def research_one(cand):
        if cand.element == 2:
            raise KeyError("shape")
        return rs.PlayerResult(element=cand.element, web_name=cand.web_name, status="ok", cost_usd=0.5)

    results, skipped, abort = rs.research_all([_cand(2), _cand(3, web_name="Other")], research_one,
                                              guard, concurrency=2)
    assert [(r.element, r.status) for r in results] == [(2, "failed"), (3, "ok")]
    assert "KeyError" in results[0].reason and skipped == [] and abort is None
    assert guard.reserved_usd == pytest.approx(0.0) and guard.spent_usd == pytest.approx(0.5)
