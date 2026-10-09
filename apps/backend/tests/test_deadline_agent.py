"""Tests for the deadline checklist agent (no network, no real subprocesses)."""

from __future__ import annotations

import json
import shlex
import shutil
import signal
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

from backend.ml import deadline_agent as da

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")


def cfg(tz=NY, start="07:00", end="02:00", lead=45, action=15, research=25, **extra):
    return da.Config(tz=tz, awake_start=da.parse_clock(start), awake_end=da.parse_clock(end),
                     lead_min=lead, min_action_min=action, research_lead_min=research, **extra)


def local(tz, *args):
    return datetime(*args, tzinfo=tz)


def deliver(deadline, config=None):
    return da.compute_delivery(deadline, config or cfg()).deliver_at


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def test_config_defaults():
    config = da.Config.from_env({})
    assert config.tz.key == "America/New_York"
    assert (config.awake_start.hour, config.awake_end.hour) == (7, 2)
    assert (config.lead_min, config.min_action_min, config.research_lead_min) == (45, 15, 25)
    assert config.ntfy_topic is None and config.ntfy_server == "https://ntfy.sh"


def test_config_from_env_overrides():
    config = da.Config.from_env({"USER_TZ": "Europe/London", "AWAKE_START": "08:30",
                                 "AWAKE_END": "01:00", "CHECKLIST_LEAD_MIN": "60",
                                 "NTFY_TOPIC": " secret ", "NTFY_SERVER": "https://n.example/"})
    assert config.tz.key == "Europe/London" and config.lead_min == 60
    assert (config.awake_start.minute, config.awake_end.hour) == (30, 1)
    assert config.ntfy_topic == "secret" and config.ntfy_server == "https://n.example"


@pytest.mark.parametrize("env", [
    {"USER_TZ": "Mars/Olympus"}, {"AWAKE_START": "7am"}, {"AWAKE_END": "25:00"},
    {"CHECKLIST_LEAD_MIN": "29"}, {"CHECKLIST_LEAD_MIN": "61"}, {"CHECKLIST_LEAD_MIN": "x"},
    {"MIN_ACTION_MIN": "50"}, {"RESEARCH_LEAD_MIN": "-1"},
])
def test_config_rejects_bad_values(env):
    with pytest.raises(da.ConfigError):
        da.Config.from_env(env)


# ---------------------------------------------------------------------------
# Delivery-time algorithm
# ---------------------------------------------------------------------------
def test_target_inside_window_is_delivered_at_target():
    deadline = local(NY, 2026, 10, 14, 20, 0)
    slot = da.compute_delivery(deadline, cfg())
    assert slot.deliver_at == local(NY, 2026, 10, 14, 19, 15)
    assert slot.target == slot.deliver_at and not slot.early
    assert slot.research_start == local(NY, 2026, 10, 14, 18, 50)


def test_target_before_window_start_waits_for_start():
    # 07:20 - 45m = 06:35 (asleep); 07:00 still leaves 20 >= 15 minutes.
    assert deliver(local(NY, 2026, 10, 14, 7, 20)) == local(NY, 2026, 10, 14, 7, 0)


def test_window_start_too_close_falls_back_to_previous_night():
    # 07:00 would leave only 10 minutes -> the night before: window end (02:00 -
    # 15 min buffer = 01:45) minus one 5-min tick so a tick can land in the window.
    assert deliver(local(NY, 2026, 10, 14, 7, 10)) == local(NY, 2026, 10, 14, 1, 40)


def test_target_after_window_end_delivers_one_tick_before_effective_end():
    assert deliver(local(NY, 2026, 10, 14, 4, 30)) == local(NY, 2026, 10, 14, 1, 40)


def test_midnight_crossing_window():
    assert deliver(local(NY, 2026, 10, 15, 0, 30)) == local(NY, 2026, 10, 14, 23, 45)
    assert deliver(local(NY, 2026, 10, 15, 2, 0)) == local(NY, 2026, 10, 15, 1, 15)


@pytest.mark.parametrize("deadline, expected", [
    ((7, 45), (7, 0)),      # target exactly at window start -> inside
    ((7, 44), (7, 0)),      # target 06:59, one minute before start -> start
    ((2, 25), (1, 40)),     # target exactly at schedulable end (02:00 - 15m - 1 tick)
    ((2, 26), (1, 40)),     # target 01:41, one minute past -> clamped back
    ((7, 20), (7, 0)),      # 07:00 + 1 tick == latest_ok (07:05) -> still waits for 07:00
    ((7, 19), (1, 40)),     # one minute less -> the night before
])
def test_window_boundaries(deadline, expected):
    assert deliver(local(NY, 2026, 10, 14, *deadline)) == local(NY, 2026, 10, 14, *expected)


@pytest.mark.parametrize("lead, expected", [(30, (7, 0)), (60, (6, 0))])
def test_lead_extremes(lead, expected):
    # Lead 30: target 06:50 -> wait for 07:00. Lead 60 on a 20:00 deadline: 19:00.
    assert deliver(local(NY, 2026, 10, 14, 7, 20), cfg(lead=lead)) == local(NY, 2026, 10, 14, 7, 0)
    assert deliver(local(NY, 2026, 10, 14, 20, 0), cfg(lead=lead)) == local(
        NY, 2026, 10, 14, 20, 0) - timedelta(minutes=lead)


def test_dst_fall_back_nov_1_2026():
    # Clocks go back at 02:00 EDT -> 01:00 EST: the Oct-31 awake window is an
    # hour longer in real time and ends at 02:00 EST == 07:00 UTC.
    start, end = da.deliverable_windows(cfg(), local(NY, 2026, 10, 31).date(),
                                        local(NY, 2026, 10, 31).date())[0]
    assert end - start == timedelta(hours=19, minutes=45)
    slot = da.compute_delivery(local(NY, 2026, 11, 1, 4, 30), cfg())
    assert slot.deliver_at == datetime(2026, 11, 1, 6, 40, tzinfo=UTC)   # 01:40 EST
    # The day after is back to a normal 18h45 window.
    nov2 = local(NY, 2026, 11, 2).date()
    start, end = da.deliverable_windows(cfg(), nov2, nov2)[0]
    assert end - start == timedelta(hours=18, minutes=45)


def test_dst_spring_forward_mar_2027():
    # 2027-03-14 02:00 EST does not exist; the old offset applies (07:00 UTC).
    slot = da.compute_delivery(local(NY, 2027, 3, 14, 4, 30), cfg())
    assert slot.deliver_at == datetime(2027, 3, 14, 6, 40, tzinfo=UTC)
    # After the change, 07:00 EDT == 11:00 UTC.
    slot = da.compute_delivery(local(NY, 2027, 3, 14, 7, 20), cfg())
    assert slot.deliver_at == datetime(2027, 3, 14, 11, 0, tzinfo=UTC)
    # An ordinary in-window deadline the same day keeps its 45-minute lead.
    slot = da.compute_delivery(local(NY, 2027, 3, 14, 20, 0), cfg())
    assert slot.deliver_at == local(NY, 2027, 3, 14, 19, 15)


def test_non_default_timezone_london():
    london = cfg(tz=LONDON)
    # 07:20 BST == 06:20 UTC -> 07:00 BST == 06:00 UTC.
    assert deliver(datetime(2026, 10, 17, 6, 20, tzinfo=UTC), london) == datetime(
        2026, 10, 17, 6, 0, tzinfo=UTC)
    # Same wall-clock deadline in winter (GMT) -> 07:00 UTC.
    assert deliver(datetime(2026, 12, 5, 7, 20, tzinfo=UTC), london) == datetime(
        2026, 12, 5, 7, 0, tzinfo=UTC)
    # London deadline 17:30 BST = 12:30 EDT: both zones deliver 45 minutes ahead.
    assert deliver(datetime(2026, 10, 17, 16, 30, tzinfo=UTC), london) == datetime(
        2026, 10, 17, 15, 45, tzinfo=UTC)


def test_always_awake_window_delivers_at_target():
    slot = da.compute_delivery(local(NY, 2026, 10, 14, 4, 30), cfg(start="00:00", end="00:00"))
    assert slot.deliver_at == local(NY, 2026, 10, 14, 3, 45) and not slot.early


def test_always_awake_has_no_bedtime_buffer():
    # Regression: 00:00/00:00 used to subtract the buffer, inventing an
    # unavailable 23:45-00:00 every night (target 23:55 -> pushed to 00:00).
    always = cfg(start="00:00", end="00:00", lead=60)
    slot = da.compute_delivery(local(NY, 2026, 10, 15, 0, 55), always)
    assert slot.deliver_at == local(NY, 2026, 10, 14, 23, 55) and not slot.early
    day = local(NY, 2026, 10, 14).date()
    (start, end), = da.deliverable_windows(always, day, day)
    assert end - start == timedelta(hours=24)


def test_no_deliverable_window_is_marked_early():
    slot = da.compute_delivery(local(NY, 2026, 10, 14, 12, 0), cfg(start="07:00", end="07:10"))
    assert slot.early and slot.deliver_at == slot.target


def test_naive_deadline_rejected():
    with pytest.raises(ValueError):
        da.compute_delivery(datetime(2026, 10, 14, 12, 0), cfg())


# ---------------------------------------------------------------------------
# Fixtures for tick / rendering
# ---------------------------------------------------------------------------
GW = 9
LINEUP_AT = datetime(2026, 10, 17, 17, 30, tzinfo=UTC)       # 13:30 EDT
DELIVER_AT = LINEUP_AT - timedelta(minutes=45)                # 16:45 UTC
RESEARCH_AT = DELIVER_AT - timedelta(minutes=25)              # 16:20 UTC


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def rec(add, drop, position, gw3, label="upgrade", add_el=1, drop_el=2, **extra):
    return {"add": add, "add_element": add_el, "drop": drop, "drop_element": drop_el,
            "position": position, "add_team": "ARS", "label": label, "next1_gain": 1.0,
            "gains": {"gw1": 1.0, "gw3": gw3, "ros": 2.0}, **extra}


WAIVER_PLAN = {
    "scorer": "model", "xp_fallback": False, "xp_fallback_reason": None,
    "recommendations": [
        rec("Alpha", "Dropone", "MID", 3.4, add_el=1, drop_el=10),
        rec("Bravo", "Dropone", "MID", 3.0, "stream", add_el=2, drop_el=10, news="Doubtful"),
        rec("Charlie", "Droptwo", "DEF", 2.2, add_el=3, drop_el=11),
        rec("Delta", "Dropthree", "FWD", 1.5, "hold", add_el=4, drop_el=12),
        rec("Echo", "Dropfour", "GKP", 1.0, add_el=5, drop_el=13),
        rec("Foxtrot", "Dropfive", "DEF", 0.9, add_el=6, drop_el=14),
        rec("Golf", "Dropsix", "FWD", 0.8, add_el=7, drop_el=15),
    ],
    "best_by_position": {"GKP": [], "DEF": [], "MID": [], "FWD": []},
    "drop_candidates": [
        {"web_name": "Weakling", "position": "MID", "status": "a", "next3_xp": 1.2},
        {"web_name": "Solid", "position": "DEF", "status": "a", "next3_xp": 9.0},
        {"web_name": "Gone", "position": "FWD", "status": "u", "next3_xp": None},
    ],
}
MY_WEEK = {
    "gw": GW, "scorer": "model", "xp_fallback": True, "xp_fallback_reason": "xp file stale",
    "formation": "3-5-2", "xi_gw_xp": 55.5,
    "xi": [{"web_name": "Keeper", "p_start": 1.0, "warnings": []},
           {"web_name": "Shaky", "p_start": 0.4, "warnings": ["role override: p_start 0.4 — rotation"]},
           {"web_name": "Fine", "p_start": 0.95, "warnings": []}],
    "bench_order": ["Bench1", "Bench2", "Bench3", "Bench4"],
    "if_out": [{"web_name": "Shaky", "text": "if Shaky out: 3-5-2, Bench1 in"}],
    "attention": [{"web_name": "Shaky", "warnings": ["availability [d] — knock"]}],
}
VALUES = {"panel_max_gw": 8, "generated_at": "2026-10-17T16:30:00+00:00"}


def make_world(tmp_path, *, plan=True, week=True, values=True, kinds=("lineup",)):
    raw, ml = tmp_path / "raw", tmp_path / "ml"
    (raw / "bootstrap").mkdir(parents=True)
    ml.mkdir()
    event = {"id": GW, "name": f"Gameweek {GW}"}
    fields = {"trades": LINEUP_AT - timedelta(hours=48), "waivers": LINEUP_AT - timedelta(hours=24),
              "lineup": LINEUP_AT}
    for kind in kinds:
        event[da.KIND_FIELDS[kind]] = iso(fields[kind])
    (raw / "bootstrap" / "bootstrap-static.json").write_text(
        json.dumps({"events": {"data": [event]}}))
    for flag, name, doc in ((plan, "waiver_plan", WAIVER_PLAN), (week, "my_week", MY_WEEK),
                            (values, "player_values", VALUES)):
        if flag:
            (ml / f"{name}.json").write_text(json.dumps(doc))
    return da.Sources(ml_dir=ml, raw_dir=raw, league_id="L1", entry_id="E1")


class Recorder:
    """Fake runner / notifier / ntfy that record calls."""

    def __init__(self, research_code=0, derive_code=0):
        self.commands, self.notes, self.pushes = [], [], []
        self.research_code, self.derive_code = research_code, derive_code

    def runner(self, cmd, cwd, timeout):
        self.commands.append((cmd, cwd))
        return self.research_code if "backend.ml.research" in cmd else self.derive_code

    def notify(self, title, message):
        self.notes.append((title, message))
        return True

    def ntfy(self, config, deadline, markdown):
        self.pushes.append((deadline.key, markdown))
        return True


@contextmanager
def free_lock(repo):
    yield True


@contextmanager
def busy_lock(repo):
    yield False


def make_env(tmp_path, sources, rec_=None, research_ok=True, lock=free_lock, config=None):
    rec_ = rec_ or Recorder()
    env = da.Env(cfg=config or cfg(), repo=tmp_path / "repo", season="2026-27", sources=sources,
                 state_path=tmp_path / "state" / "deadline_agent.json",
                 checklist_dir=tmp_path / "state" / "checklists",
                 runner=rec_.runner, research_ok=lambda: research_ok, lock=lock,
                 notify=rec_.notify, ntfy=rec_.ntfy)
    return env, rec_


# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------
def test_tick_before_research_start_does_nothing(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path))
    assert da.tick(env, RESEARCH_AT - timedelta(minutes=1)) == []
    assert recorder.commands == [] and recorder.notes == []


def test_tick_runs_research_then_derive_then_delivers_once(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path))
    log = da.tick(env, RESEARCH_AT)
    assert len(recorder.commands) == 2 and recorder.notes == []
    research, derive = (c for c, _ in recorder.commands)
    assert research == ["uv", "run", "python", "-m", "backend.ml.research", "run",
                        "--phase", "lineup", "--gw", "9"]
    assert recorder.commands[0][1] == env.repo / "apps" / "backend"
    assert derive == ["make", "derive", "SEASON=2026-27"]
    assert any("research 2026-27:9:lineup" in line for line in log)

    da.tick(env, DELIVER_AT)
    assert len(recorder.notes) == 1 and len(recorder.pushes) == 1
    assert (env.checklist_dir / "2026-27" / "gw9_lineup.md").exists()
    # Idempotent: later ticks neither re-research nor re-send.
    for later in (DELIVER_AT, DELIVER_AT + timedelta(minutes=5), LINEUP_AT - timedelta(minutes=1)):
        assert da.tick(env, later) == []
    assert len(recorder.commands) == 2 and len(recorder.notes) == 1 and len(recorder.pushes) == 1


def test_tick_state_survives_new_process(tmp_path):
    sources = make_world(tmp_path)
    first, _ = make_env(tmp_path, sources)
    da.tick(first, DELIVER_AT)
    second, recorder = make_env(tmp_path, sources)
    assert da.tick(second, DELIVER_AT + timedelta(minutes=5)) == []
    assert recorder.notes == []


def test_late_tick_does_research_and_delivery_in_one_pass(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path))
    log = da.tick(env, DELIVER_AT + timedelta(minutes=3))
    assert len(recorder.commands) == 2 and len(recorder.notes) == 1
    assert log[-1].startswith("delivered 2026-27:9:lineup")


def test_research_module_absent_skips_research_only(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path), research_ok=False)
    da.tick(env, DELIVER_AT)
    assert [c[0][0] for c in recorder.commands] == ["make"]
    assert "research" not in recorder.pushes[0][1].lower().replace("researched", "")


def test_research_exit_3_is_silent(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path), Recorder(research_code=3))
    da.tick(env, DELIVER_AT)
    state = da.load_state(env.state_path)
    assert state["2026-27:9:lineup"]["research"]["research"] == "skipped"
    assert "research failed" not in recorder.pushes[0][1]
    assert [c[0][0] for c in recorder.commands] == ["uv", "make"]    # derive still ran


def test_research_failure_is_noted_in_checklist(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path), Recorder(research_code=1))
    da.tick(env, DELIVER_AT)
    assert "research failed" in recorder.pushes[0][1]
    assert "research failed" in (env.checklist_dir / "2026-27" / "gw9_lineup.md").read_text()


def test_derive_failure_and_busy_lock(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path), Recorder(derive_code=2))
    da.tick(env, DELIVER_AT)
    assert "derive failed" in recorder.pushes[0][1]

    env2, recorder2 = make_env(tmp_path / "b", make_world(tmp_path / "b"), lock=busy_lock)
    da.tick(env2, DELIVER_AT)
    assert da.load_state(env2.state_path)["2026-27:9:lineup"]["research"]["derive"] == "busy"
    assert [c[0][0] for c in recorder2.commands] == ["uv"]
    assert len(recorder2.notes) == 1


def test_missed_deadline_is_logged_and_never_delivered(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path))
    log = da.tick(env, LINEUP_AT + timedelta(minutes=1))
    assert log == ["MISSED 2026-27:9:lineup: deadline passed before delivery"]
    assert recorder.notes == [] and recorder.commands == []
    assert da.tick(env, LINEUP_AT + timedelta(minutes=6)) == []        # logged once only
    assert not (env.checklist_dir / "2026-27" / "gw9_lineup.md").exists()


def test_old_deadlines_are_history_not_missed(tmp_path):
    env, _ = make_env(tmp_path, make_world(tmp_path))
    assert da.tick(env, LINEUP_AT + timedelta(days=5)) == []


NIGHT_DEADLINE = datetime(2026, 10, 17, 8, 30, tzinfo=UTC)      # 04:30 EDT


def night_world(tmp_path, at=NIGHT_DEADLINE):
    world = make_world(tmp_path)
    (world.raw_dir / "bootstrap" / "bootstrap-static.json").write_text(
        json.dumps({"events": [{"id": GW, "deadline_time": iso(at)}]}))
    return world


def test_delivery_never_before_slot_and_asleep_deadline_uses_buffer(tmp_path):
    # Deadline 04:30 EDT: delivery at 01:40 EDT (05:40Z), not 03:45.
    env, recorder = make_env(tmp_path, night_world(tmp_path))
    da.tick(env, datetime(2026, 10, 17, 5, 39, tzinfo=UTC))
    assert recorder.notes == []
    da.tick(env, datetime(2026, 10, 17, 5, 40, tzinfo=UTC))
    assert len(recorder.notes) == 1


def test_any_tick_in_the_last_slack_before_window_end_still_delivers(tmp_path):
    # 01:44:59 EDT: inside the 01:45 window end -> sent, stamped with that instant.
    env, recorder = make_env(tmp_path, night_world(tmp_path))
    at = datetime(2026, 10, 17, 5, 44, 59, tzinfo=UTC)
    da.tick(env, at)
    assert len(recorder.notes) == 1
    assert da.load_state(env.state_path)["2026-27:9:lineup"]["sent"] == at.isoformat()


def test_catch_up_while_asleep_is_missed_not_sent(tmp_path):
    # Machine asleep through 01:40-01:45; the 03:00 EDT catch-up tick must not
    # wake the user, and no awake slot is left before 04:30 -> missed.
    env, recorder = make_env(tmp_path, night_world(tmp_path))
    log = da.tick(env, datetime(2026, 10, 17, 7, 0, tzinfo=UTC))
    assert recorder.notes == [] and recorder.pushes == [] and recorder.commands == []
    assert len(log) == 1 and log[0].startswith("MISSED 2026-27:9:lineup: no deliverable slot left")
    entry = da.load_state(env.state_path)["2026-27:9:lineup"]
    assert "outside awake hours" in entry["missed_reason"]
    assert da.tick(env, datetime(2026, 10, 17, 7, 30, tzinfo=UTC)) == []   # logged once only


def test_catch_up_inside_window_is_sent(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path))
    at = DELIVER_AT + timedelta(minutes=20)                   # 13:05 EDT, 25 min before lock
    da.tick(env, at)
    assert len(recorder.notes) == 1
    assert da.load_state(env.state_path)["2026-27:9:lineup"]["sent"] == at.isoformat()


def test_catch_up_with_less_than_min_action_left_is_missed(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path))
    log = da.tick(env, LINEUP_AT - timedelta(minutes=10))
    assert recorder.notes == [] and log[0].startswith("MISSED 2026-27:9:lineup: no deliverable")


class FakeClock:
    """A clock that ``advance`` moves (e.g. from inside a runner)."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


def clocked_env(tmp_path, sources, start, advance_on_derive):
    clock = FakeClock(start)
    recorder = Recorder()

    def runner(cmd, cwd, timeout):
        if cmd[0] == "make":
            clock.advance(**advance_on_derive)
        return recorder.runner(cmd, cwd, timeout)

    env, _ = make_env(tmp_path, sources, recorder)
    env.runner, env.clock = runner, clock
    return env, recorder, clock


def test_clock_passing_deadline_during_derive_is_missed_not_sent(tmp_path):
    env, recorder, clock = clocked_env(tmp_path, make_world(tmp_path), DELIVER_AT,
                                       {"minutes": 50})
    log = da.tick(env)
    assert len(recorder.commands) == 2 and recorder.notes == [] and recorder.pushes == []
    assert log[-1] == "MISSED 2026-27:9:lineup: deadline passed before delivery"
    entry = da.load_state(env.state_path)["2026-27:9:lineup"]
    assert entry["missed"] == clock.now.isoformat() and "sent" not in entry
    assert not (env.checklist_dir / "2026-27" / "gw9_lineup.md").exists()


def test_clock_leaving_window_during_derive_is_missed(tmp_path):
    # Research at 01:15 EDT for the 04:30 deadline; derive runs until 02:05.
    env, recorder, _ = clocked_env(tmp_path, night_world(tmp_path),
                                   datetime(2026, 10, 17, 5, 15, tzinfo=UTC), {"minutes": 50})
    log = da.tick(env)
    assert recorder.notes == [] and log[-1].startswith("MISSED 2026-27:9:lineup: no deliverable")


def test_sent_records_real_send_time_after_slow_derive(tmp_path):
    env, recorder, clock = clocked_env(tmp_path, make_world(tmp_path), DELIVER_AT, {"minutes": 7})
    da.tick(env)
    assert len(recorder.notes) == 1
    sent = da.load_state(env.state_path)["2026-27:9:lineup"]["sent"]
    assert sent == (DELIVER_AT + timedelta(minutes=7)).isoformat() == clock.now.isoformat()
    assert "(in 38m)" in recorder.pushes[0][1]               # countdown from the real time


def test_catch_up_outside_window_waits_for_later_slot(tmp_path, monkeypatch):
    # A slot that was due at 03:00 for a 07:30 deadline: 07:00 still leaves
    # >= MIN_ACTION_MIN, so the 03:00 tick waits instead of sending or missing.
    deadline = datetime(2026, 10, 17, 11, 30, tzinfo=UTC)    # 07:30 EDT
    env, recorder = make_env(tmp_path, night_world(tmp_path, deadline))
    early = da.Delivery(deadline, deadline, datetime(2026, 10, 17, 7, 0, tzinfo=UTC),
                        datetime(2026, 10, 17, 6, 35, tzinfo=UTC))
    monkeypatch.setattr(da, "compute_delivery", lambda at, config: early)
    da.tick(env, datetime(2026, 10, 17, 7, 0, tzinfo=UTC))   # 03:00 EDT
    assert recorder.notes == [] and "missed" not in da.load_state(env.state_path)["2026-27:9:lineup"]
    da.tick(env, datetime(2026, 10, 17, 11, 0, tzinfo=UTC))  # 07:00 EDT
    assert len(recorder.notes) == 1


@pytest.mark.parametrize("now, deadline, expected", [
    ((2026, 10, 14, 19, 0), (2026, 10, 14, 20, 0), (2026, 10, 14, 19, 0)),   # in window
    ((2026, 10, 14, 3, 0), (2026, 10, 14, 7, 30), (2026, 10, 14, 7, 0)),     # wait for start
    ((2026, 10, 14, 3, 0), (2026, 10, 14, 7, 20), (2026, 10, 14, 7, 0)),     # 07:00+5 <= 07:05
    ((2026, 10, 14, 3, 0), (2026, 10, 14, 7, 10), None),                     # 07:00 too late
    ((2026, 10, 14, 3, 0), (2026, 10, 14, 4, 30), None),                     # asleep till after
    ((2026, 10, 14, 1, 45), (2026, 10, 14, 4, 30), (2026, 10, 14, 1, 45)),   # window end inclusive
    ((2026, 10, 14, 19, 46), (2026, 10, 14, 20, 0), None),                   # < MIN_ACTION_MIN
])
def test_delivery_slot(now, deadline, expected):
    got = da.delivery_slot(local(NY, *now), local(NY, *deadline), cfg())
    assert got == (None if expected is None else local(NY, *expected))


def test_delivery_slot_always_awake_and_degenerate():
    always = cfg(start="00:00", end="00:00")
    assert da.delivery_slot(local(NY, 2026, 10, 14, 3, 0), local(NY, 2026, 10, 14, 4, 30),
                            always) == local(NY, 2026, 10, 14, 3, 0)
    tiny = cfg(start="07:00", end="07:10")                   # no window at all
    assert da.delivery_slot(local(NY, 2026, 10, 14, 3, 0), local(NY, 2026, 10, 14, 4, 30),
                            tiny) == local(NY, 2026, 10, 14, 3, 0)
    assert da.delivery_slot(local(NY, 2026, 10, 14, 4, 20), local(NY, 2026, 10, 14, 4, 30),
                            tiny) is None


def test_ntfy_failure_does_not_crash_tick(tmp_path):
    def boom(url, **kwargs):
        raise requests.ConnectionError("offline")

    config = cfg(ntfy_topic="topic")
    env, recorder = make_env(tmp_path, make_world(tmp_path), config=config)
    env.ntfy = lambda c, d, md: da.post_ntfy(c, d, md, post=boom)
    da.tick(env, DELIVER_AT)
    assert len(recorder.notes) == 1
    assert da.load_state(env.state_path)["2026-27:9:lineup"]["sent"]


def test_same_gw_and_kind_in_a_new_season_is_sent(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path))
    da.save_state(env.state_path, {"2025-26:9:lineup": {"sent": "2025-10-18T16:45:00+00:00"}})
    log = da.tick(env, DELIVER_AT)
    assert len(recorder.notes) == 1 and log[-1].startswith("delivered 2026-27:9:lineup")
    state = da.load_state(env.state_path)
    assert state["2025-26:9:lineup"] == {"sent": "2025-10-18T16:45:00+00:00"}   # history kept
    assert (env.checklist_dir / "2026-27" / "gw9_lineup.md").exists()


def test_legacy_key_from_an_earlier_season_is_dropped_not_obeyed(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path))
    da.save_state(env.state_path, {"9:lineup": {"sent": "2025-10-18T16:45:00+00:00"}})
    da.tick(env, DELIVER_AT)
    assert len(recorder.notes) == 1
    assert "9:lineup" not in da.load_state(env.state_path)


def test_legacy_key_for_this_seasons_deadline_is_migrated(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path))
    sent = iso(DELIVER_AT)
    da.save_state(env.state_path, {"9:lineup": {"sent": sent}, "last_checklist_at": sent})
    log = da.tick(env, DELIVER_AT + timedelta(minutes=5))
    assert recorder.notes == [] and recorder.commands == []           # not re-sent
    assert log == ["state: migrated 1 of 1 un-namespaced entries under 2026-27; "
                   "dropped the rest as earlier-season history"]
    state = da.load_state(env.state_path)
    assert state["2026-27:9:lineup"] == {"sent": sent} and "9:lineup" not in state


@pytest.mark.parametrize("break_calendar", [
    lambda path: path.unlink(),
    lambda path: path.write_text("{not json"),
    lambda path: path.write_text(json.dumps({"events": []})),
], ids=["missing", "corrupt", "empty"])
def test_legacy_state_is_untouched_without_a_calendar_then_migrated_not_resent(
        tmp_path, break_calendar):
    sources = make_world(tmp_path)
    env, recorder = make_env(tmp_path, sources)
    calendar = sources.raw_dir / "bootstrap" / "bootstrap-static.json"
    good_calendar = calendar.read_text()
    sent = iso(DELIVER_AT)
    legacy_state = {"9:lineup": {"sent": sent}, "last_checklist_at": sent}
    da.save_state(env.state_path, legacy_state)
    saved = env.state_path.read_text()

    break_calendar(calendar)
    log = da.tick(env, DELIVER_AT + timedelta(minutes=5))
    assert recorder.notes == [] and recorder.commands == [] and recorder.pushes == []
    assert env.state_path.read_text() == saved                      # not even rewritten
    assert log == ["state: 1 un-namespaced entries left untouched (no 2026-27 calendar loaded)"]
    assert not env.checklist_dir.exists()

    calendar.write_text(good_calendar)
    da.tick(env, DELIVER_AT + timedelta(minutes=10))
    assert recorder.notes == [] and recorder.commands == []         # already sent: no re-send
    state = da.load_state(env.state_path)
    assert state["2026-27:9:lineup"] == {"sent": sent} and "9:lineup" not in state


def test_derive_command_default_and_custom_root(tmp_path):
    assert da.derive_command("2026-27") == ["make", "derive", "SEASON=2026-27"]
    custom = tmp_path / "other" / "data"
    assert da.derive_command("2026-27", custom) == [
        "make", "derive", "SEASON=2026-27", f"DATA_DIR={custom.resolve()}"]


def test_tick_passes_a_custom_data_root_to_derive(tmp_path):
    custom = tmp_path / "custom-data"
    env, recorder = make_env(tmp_path, make_world(tmp_path))
    env.data_root = custom
    da.tick(env, DELIVER_AT)
    derive = [cmd for cmd, _ in recorder.commands if cmd[:2] == ["make", "derive"]]
    assert derive == [["make", "derive", "SEASON=2026-27", f"DATA_DIR={custom.resolve()}"]]


def test_tick_without_a_data_root_leaves_derive_unchanged(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path))
    da.tick(env, DELIVER_AT)
    derive = [cmd for cmd, _ in recorder.commands if cmd[:2] == ["make", "derive"]]
    assert derive == [["make", "derive", "SEASON=2026-27"]]


def test_build_env_records_only_an_explicit_data_root(tmp_path):
    assert da.build_env("2026-27", state_dir=tmp_path).data_root is None
    assert da.build_env("2026-27", tmp_path / "d", state_dir=tmp_path).data_root == tmp_path / "d"


def test_migrate_legacy_state_rules():
    deadline = da.Deadline(9, "lineup", LINEUP_AT)
    state = {"9:lineup": {"research": {"research": "ok"}},        # no timestamp -> dropped
             "10:waivers": {"sent": iso(LINEUP_AT)},              # not in this calendar
             "last_checklist_at": "x", "2025-26:9:lineup": {"sent": "y"}}
    assert da.migrate_legacy_state(state, "2026-27", [deadline]) == []
    assert state == {"last_checklist_at": "x", "2025-26:9:lineup": {"sent": "y"}}


def test_corrupt_state_file_is_empty_state(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json")
    assert da.load_state(path) == {}
    path.write_text("[1]")
    assert da.load_state(path) == {}


# ---------------------------------------------------------------------------
# Delivery channels
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, error=None):
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error


def test_post_ntfy_headers_and_priority():
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    config = cfg(ntfy_topic="abc123", ntfy_server="https://n.example")
    lineup = da.Deadline(9, "lineup", LINEUP_AT)
    waivers = da.Deadline(9, "waivers", LINEUP_AT)
    assert da.post_ntfy(config, lineup, "# hi", post=fake_post)
    assert da.post_ntfy(config, waivers, "# hi", post=fake_post)
    (url, kwargs), (_, kwargs2) = calls
    assert url == "https://n.example/abc123" and kwargs["data"] == b"# hi"
    assert kwargs["headers"]["Markdown"] == "yes" and kwargs["headers"]["Priority"] == "high"
    assert kwargs2["headers"]["Priority"] == "default"


def test_post_ntfy_without_topic_or_on_http_error():
    called = []
    lineup = da.Deadline(9, "lineup", LINEUP_AT)
    assert not da.post_ntfy(cfg(), lineup, "x", post=lambda *a, **k: called.append(1))
    assert called == []
    bad = lambda *a, **k: FakeResponse(requests.HTTPError("500"))  # noqa: E731
    assert not da.post_ntfy(cfg(ntfy_topic="t"), lineup, "x", post=bad)


def completed(code=0, stderr=""):
    return lambda cmd, **kw: subprocess.CompletedProcess(cmd, code, "", stderr)


def test_notify_macos_passes_text_as_argv_and_survives_errors():
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert da.notify_macos('FPL "Co-Pilot"', 'say "hi"; do shell script "x"', run=run)
    cmd = seen[0]
    assert cmd[0] == "osascript" and cmd[-2:] == ['say "hi"; do shell script "x"', 'FPL "Co-Pilot"']
    assert all('say "hi"' not in part for part in cmd[1:-2])    # never spliced into the script

    def missing(cmd, **kw):
        raise FileNotFoundError("osascript")

    assert da.notify_macos("t", "m", run=missing) is False


def test_notify_macos_nonzero_exit_is_a_failure(caplog):
    with caplog.at_level("WARNING"):
        assert da.notify_macos("t", "m", run=completed(1, "execution error: -1743")) is False
    assert "osascript exited 1" in caplog.text and "-1743" in caplog.text


def test_tick_records_failed_notification_and_still_pushes_ntfy(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path), config=cfg(ntfy_topic="t"))
    env.notify = lambda title, message: False
    log = da.tick(env, DELIVER_AT)
    assert len(recorder.pushes) == 1                          # ntfy still attempted
    entry = da.load_state(env.state_path)["2026-27:9:lineup"]
    assert entry["delivery_failed"] == ["macos"] and entry["sent"]
    assert "FAILED: macos" in log[-1]


def test_failed_channels_ignores_unconfigured_ntfy():
    assert da.failed_channels(cfg(), True, False) == []
    assert da.failed_channels(cfg(ntfy_topic="t"), True, False) == ["ntfy"]
    assert da.failed_channels(cfg(ntfy_topic="t"), False, False) == ["macos", "ntfy"]


def test_derive_lock_is_exclusive(tmp_path):
    with da.derive_lock(tmp_path, wait_s=0) as first:
        assert first
        with da.derive_lock(tmp_path, wait_s=0) as second:
            assert second is False
    with da.derive_lock(tmp_path, wait_s=0) as again:
        assert again                                  # released after the first block


def test_default_runner_reports_missing_binary(tmp_path):
    assert da.default_runner(["definitely-not-a-binary-xyz"], tmp_path, 5) == -1


class FakeProc:
    """Popen stand-in: ``wait`` times out ``timeouts`` times, then returns ``code``."""

    def __init__(self, pid=4242, code=0, timeouts=0):
        self.pid, self.code, self.timeouts = pid, code, timeouts
        self.waits, self.polls = [], 0

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.timeouts:
            self.timeouts -= 1
            raise subprocess.TimeoutExpired("make", timeout)
        return self.code

    def poll(self):
        self.polls += 1


class FakeGroup:
    """killpg stand-in: the group survives ``probes_alive`` liveness probes after SIGTERM."""

    def __init__(self, probes_alive=0):
        self.signals, self.probes_alive, self.dead = [], probes_alive, False

    def __call__(self, pgid, sig):
        self.signals.append((pgid, sig))
        if sig == 0:
            if self.probes_alive:
                self.probes_alive -= 1
                return
            self.dead = True
        if self.dead:
            raise ProcessLookupError


def test_default_runner_starts_own_session_and_returns_code(tmp_path):
    seen = {}

    def popen(cmd, **kw):
        seen.update(kw)
        return FakeProc(code=2)

    group = FakeGroup()
    assert da.default_runner(["make", "derive"], tmp_path, 5, popen=popen, killpg=group) == 2
    assert seen == {"cwd": tmp_path, "start_new_session": True}
    assert group.signals == []                                  # no timeout -> nothing killed


def test_default_runner_timeout_kills_whole_group_and_reaps(tmp_path):
    proc, group = FakeProc(timeouts=1), FakeGroup()
    code = da.default_runner(["make"], tmp_path, 5, popen=lambda *a, **k: proc, killpg=group)
    assert code == -1
    assert group.signals[0] == (4242, signal.SIGTERM)
    assert group.signals[-1] == (4242, signal.SIGKILL)          # stragglers swept up
    assert proc.waits == [5, None]                              # leader reaped before return


def test_terminate_group_escalates_when_sigterm_ignored():
    proc, group, naps = FakeProc(), FakeGroup(probes_alive=1000), []
    da.terminate_group(proc, group, grace_s=1, sleep=naps.append)
    sigs = [sig for _, sig in group.signals]
    assert sigs[0] == signal.SIGTERM and sigs[-1] == signal.SIGKILL
    assert len(naps) == int(1 / da.KILL_POLL_S)                 # waited the full grace
    assert proc.waits == [None] and proc.polls == len(naps)


def test_derive_lock_held_until_timed_out_group_is_gone(tmp_path):
    events = []

    class LoggedProc(FakeProc):
        def wait(self, timeout=None):
            events.append(("wait", timeout))
            return super().wait(timeout)

    def killpg(pgid, sig):
        events.append(("kill", sig))
        if sig != signal.SIGTERM:
            raise ProcessLookupError

    @contextmanager
    def lock(repo):
        events.append("lock")
        yield True
        events.append("unlock")

    proc = LoggedProc(timeouts=1)
    runner = lambda cmd, cwd, timeout: da.default_runner(  # noqa: E731
        cmd, cwd, timeout, popen=lambda *a, **k: proc, killpg=killpg)
    da.run_research_and_derive(da.Deadline(9, "lineup", LINEUP_AT), tmp_path, "2026-27",
                               runner=runner, research_ok=lambda: False, lock=lock)
    assert events[0] == "lock" and events[-1] == "unlock"
    assert ("kill", signal.SIGTERM) in events
    assert events[-2] == ("wait", None)                         # reaped before the unlock


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render(tmp_path, kind, sources=None, **kwargs):
    sources = sources or make_world(tmp_path, kinds=da.KINDS)
    fields = {"trades": LINEUP_AT - timedelta(hours=48), "waivers": LINEUP_AT - timedelta(hours=24),
              "lineup": LINEUP_AT}
    deadline = da.Deadline(GW, kind, fields[kind])
    delivery = da.compute_delivery(deadline.at, cfg())
    return da.render_checklist(deadline, delivery, cfg(), sources, DELIVER_AT, **kwargs)


def test_waivers_checklist_full(tmp_path):
    sources = make_world(tmp_path, kinds=da.KINDS)
    (sources.raw_dir / "league" / "L1").mkdir(parents=True)
    (sources.raw_dir / "league" / "L1" / "details.json").write_text(json.dumps(
        {"league_entries": [{"entry_id": "E1", "waiver_pick": 4}, {"entry_id": "E2", "waiver_pick": 1}]}))
    text = render(tmp_path, "waivers", sources, since=datetime(2026, 10, 10, tzinfo=UTC))
    lines = text.splitlines()
    assert len(lines) <= 25
    assert lines[0].startswith("## GW9 waivers due — Fri ")
    claims = [ln for ln in lines if ln[:2] in {"1.", "2.", "3.", "4.", "5.", "6."}]
    assert len(claims) == 5 and claims[0].startswith("1. Alpha (MID, ARS) → drop Dropone · 3-GW +3.4")
    # Bravo shares Alpha's drop -> a backup, never a primary claim.
    assert not any("Bravo" in c for c in claims)
    backups = lines[lines.index("Backups if a claim fails:") + 1:]
    assert backups[0].startswith("- Bravo") and "Doubtful" in backups[0]
    assert "Your waiver order: #4 of 2" in text
    assert "panel_max_gw 8" in text and "scorer model" in text
    assert "FALLBACK: xp file stale" in text


def _write_overrides(sources, plan_applied, week_rows=(), recs=(), research_file=None):
    """Rewrite the artifacts of ``make_world`` with override signals."""
    plan = {**WAIVER_PLAN, "overrides_applied": plan_applied,
            "recommendations": [*WAIVER_PLAN["recommendations"], *recs]}
    week = {**MY_WEEK, "overrides_applied": plan_applied, "xi": [*MY_WEEK["xi"], *week_rows]}
    (sources.ml_dir / "waiver_plan.json").write_text(json.dumps(plan))
    (sources.ml_dir / "my_week.json").write_text(json.dumps(week))
    if research_file is not None:
        (sources.ml_dir / "role_overrides.research.json").write_text(json.dumps(research_file))


def test_research_section_uses_the_artifacts_research_tag_not_source_urls(tmp_path):
    """Regression: a hand-written role_overrides.json entry with an http source
    (or origin/source "research") used to be reported as 'Changed by research'."""
    sources = make_world(tmp_path, kinds=da.KINDS)
    (sources.ml_dir / "role_overrides.json").write_text(json.dumps({"overrides": [
        {"player": "Handmade", "team": "CHE", "fact": "hand fact", "source": "https://e.org/a",
         "origin": "research", "as_of": "2026-10-17"}]}))
    _write_overrides(
        sources, ["research:Newsy", "Handmade"],
        week_rows=[{"web_name": "Newsy", "team": "ARS", "role_override": "research: back in training",
                    "warnings": []},
                   {"web_name": "Handmade", "team": "CHE", "role_override": "hand fact",
                    "warnings": []}])
    text = render(tmp_path, "waivers", sources)
    research, hand = text.split("Your overrides:")
    assert "Changed by research since last checklist:" in research
    assert "- Newsy (ARS): research: back in training" in research
    assert "Handmade" not in research
    assert "- Handmade (CHE): hand fact" in hand and "Newsy" not in hand


def test_same_web_name_research_and_hand_players_keep_their_own_fact(tmp_path):
    """Regression: facts were keyed by web_name alone, so a researched squad
    player and a hand-overridden free agent both called "Mid" swapped facts."""
    sources = make_world(tmp_path, kinds=da.KINDS)
    (sources.ml_dir / "role_overrides.json").write_text(json.dumps({"overrides": [
        {"player": "Mid", "team": "CHE", "fact": "hand fact"}]}))
    (sources.ml_dir / "role_overrides.research.json").write_text(json.dumps({"overrides": [
        {"player": "Mid", "team": "ARS", "fact": "research: starts"}]}))
    _write_overrides(
        sources, ["research:Mid", "Mid"],
        week_rows=[{"web_name": "Mid", "team": "CHE", "role_override": "hand fact", "warnings": []}],
        recs=[{**rec("Mid", "Dropone", "MID", 0.5, add_el=98, drop_el=10), "add_team": "ARS",
               "add_role_override": "research: starts"}])
    research, hand = render(tmp_path, "waivers", sources).split("Your overrides:")
    assert "- Mid (ARS): research: starts" in research and "hand fact" not in research
    assert "- Mid (CHE): hand fact" in hand and "starts" not in hand


def test_ambiguous_same_name_fact_is_omitted_not_misattributed(tmp_path):
    sources = make_world(tmp_path, kinds=da.KINDS)
    _write_overrides(
        sources, ["Mid"],
        week_rows=[{"web_name": "Mid", "team": "CHE", "role_override": "fact-alpha", "warnings": []},
                   {"web_name": "Mid", "team": "ARS", "role_override": "fact-beta", "warnings": []}])
    text = render(tmp_path, "waivers", sources)
    assert "- Mid\n" in text + "\n" and "fact-alpha" not in text and "fact-beta" not in text


def test_hand_overrides_alone_produce_no_research_section(tmp_path):
    sources = make_world(tmp_path, kinds=da.KINDS)
    (sources.ml_dir / "role_overrides.json").write_text(json.dumps({"overrides": [
        {"player": "Handmade", "team": "CHE", "fact": "x", "source": "research"}]}))
    _write_overrides(sources, ["Handmade"])
    text = render(tmp_path, "waivers", sources)
    assert "Changed by research" not in text
    assert "- Handmade" in text and "Your overrides:" in text


def test_research_section_is_filtered_by_since_via_the_research_file(tmp_path):
    sources = make_world(tmp_path, kinds=da.KINDS)
    _write_overrides(
        sources, ["research:Old", "research:New", "research:Undated"],
        research_file={"overrides": [{"player": "Old", "as_of": "2026-10-01"},
                                     {"player": "New", "as_of": "2026-10-16"},
                                     {"player": "Undated"}]})
    text = render(tmp_path, "waivers", sources, since=datetime(2026, 10, 10, tzinfo=UTC))
    assert "- New" in text and "- Undated" in text and "- Old" not in text
    everything = render(tmp_path, "waivers", sources)
    assert "- Old" in everything


def test_research_fact_comes_from_a_waiver_rec_too(tmp_path):
    sources = make_world(tmp_path, kinds=da.KINDS)
    recs = [{**rec("Zed", "Dropone", "MID", 0.5, add_el=99, drop_el=10), "add_team": "LIV",
             "add_role_override": "research: starts now"}]
    _write_overrides(sources, ["research:Zed"], recs=recs)
    assert "- Zed (LIV): research: starts now" in render(tmp_path, "waivers", sources)


def test_waivers_checklist_flags_dead_never_drop_entries(tmp_path):
    sources = make_world(tmp_path, kinds=da.KINDS)
    plan = {**WAIVER_PLAN, "never_drop_applied": ["Kept"], "never_drop_unmatched": ["Ghost"],
            "never_drop_expired": ["Back"],
            "never_drop_invalid": ["Bad: code 1.5 is not a finite whole number"]}
    (sources.ml_dir / "waiver_plan.json").write_text(json.dumps(plan))
    text = render(tmp_path, "waivers", sources)
    assert ("never_drop entries not protecting anyone — unmatched Ghost; expired Back; "
            "invalid Bad: code 1.5 is not a finite whole number") in text
    clean = {**WAIVER_PLAN, "never_drop_applied": ["Kept"], "never_drop_unmatched": [],
             "never_drop_expired": []}
    (sources.ml_dir / "waiver_plan.json").write_text(json.dumps(clean))
    assert "never_drop" not in render(tmp_path, "waivers", sources)


def test_lineup_checklist_full(tmp_path):
    text = render(tmp_path, "lineup")
    assert len(text.splitlines()) <= 25
    assert "XI (3-5-2): Keeper, Shaky, Fine" in text
    assert "Bench (auto-sub order): Bench1 > Bench2 > Bench3 > Bench4" in text
    assert "Shaky: role override: p_start 0.4 — rotation" in text
    assert "if Shaky out: 3-5-2, Bench1 in" in text
    assert "Free agency is open" in text and "Alpha" in text
    assert "Keeper:" not in text


def test_trades_checklist_notes_and_cap(tmp_path):
    text = render(tmp_path, "trades")
    assert text.splitlines()[0].startswith("## GW9 trades close — ")
    assert "any to propose or accept?" in text
    assert "Weakling (MID): weak 3-GW outlook (xP 1.2)" in text
    assert "Gone (FWD) has left the league" in text
    assert "Solid" not in text
    assert "trade_check give=… get=…" in text
    bullets = [ln for ln in text.splitlines() if ln.startswith("- ")]
    assert len(bullets) <= da.MAX_TRADE_NOTES + 2      # notes + the question and the ask line


@pytest.mark.parametrize("kind", da.KINDS)
def test_partial_artifacts_degrade_gracefully(tmp_path, kind):
    sources = make_world(tmp_path, plan=False, week=False, values=False, kinds=da.KINDS)
    text = render(tmp_path, kind, sources)
    assert text.startswith(f"## GW9 {da.KIND_LABELS[kind].lower()}")
    assert "panel_max_gw ?" in text and "missing" in text


def test_waivers_without_plan_but_with_week(tmp_path):
    sources = make_world(tmp_path, plan=False, kinds=da.KINDS)
    assert "waiver_plan.json missing" in render(tmp_path, "waivers", sources)
    assert "XI (3-5-2)" in render(tmp_path, "lineup", sources)
    assert "Free agency is open" not in render(tmp_path, "lineup", sources)


def test_unreadable_artifact_is_treated_as_missing(tmp_path):
    sources = make_world(tmp_path, kinds=da.KINDS)
    (sources.ml_dir / "waiver_plan.json").write_text("{broken")
    assert "waiver_plan.json missing" in render(tmp_path, "waivers", sources)


def test_early_and_research_note_are_shown(tmp_path):
    sources = make_world(tmp_path, kinds=da.KINDS)
    deadline = da.Deadline(GW, "lineup", LINEUP_AT)
    delivery = da.Delivery(LINEUP_AT, LINEUP_AT, LINEUP_AT - timedelta(hours=3),
                           LINEUP_AT - timedelta(hours=4), early=True)
    text = da.render_checklist(deadline, delivery, cfg(), sources, DELIVER_AT,
                               research_note="research failed — stale")
    assert "Early" in text and "⚠ research failed — stale" in text


def test_summary_line_per_kind(tmp_path):
    for kind, expected in (("trades", "any trades"), ("waivers", "Alpha"), ("lineup", "XI (3-5-2)")):
        text = render(tmp_path / kind, kind, make_world(tmp_path / kind, kinds=da.KINDS))
        deadline = da.Deadline(GW, kind, LINEUP_AT)
        assert expected in da.summary_line(deadline, cfg(), text)


# ---------------------------------------------------------------------------
# plan / preview
# ---------------------------------------------------------------------------
def test_plan_lists_deadlines_with_times(tmp_path):
    env, _ = make_env(tmp_path, make_world(tmp_path, kinds=da.KINDS))
    lines = da.plan_lines(env, LINEUP_AT - timedelta(days=3))
    assert lines[0].startswith("now ") and "America/New_York" in lines[0]
    assert len(lines) == 4
    assert [ln.split()[1] for ln in lines[1:]] == ["trades", "waivers", "lineup"]
    assert "research Sat 12:20 EDT" in lines[3]
    assert "deliver Sat 12:45 EDT (45m before)" in lines[3]


def test_plan_without_calendar(tmp_path):
    sources = da.Sources(ml_dir=tmp_path, raw_dir=tmp_path / "nothing")
    env, _ = make_env(tmp_path, sources)
    assert da.plan_lines(env, LINEUP_AT) == ["no upcoming deadlines in the season calendar"]


def test_preview_renders_next_deadline_of_kind_and_sends_nothing(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path, kinds=da.KINDS))
    text = da.preview(env, LINEUP_AT - timedelta(days=3), "waivers")
    assert text.startswith("## GW9 waivers due")
    assert da.preview(env, LINEUP_AT - timedelta(days=3)).startswith("## GW9 trades close")
    assert "no upcoming deadline of kind lineup" in da.preview(env, LINEUP_AT + timedelta(days=1), "lineup")
    assert recorder.notes == [] and recorder.commands == [] and not env.checklist_dir.exists()


def test_deadlines_from_events_skips_garbage():
    events = [{"id": 1, "trades_time": "nope", "waivers_time": None, "deadline_time": "2026-08-21T17:30:00Z"},
              {"id": "x", "deadline_time": "2026-08-22T17:30:00Z"}, {"deadline_time": "2026-08-23T17:30:00Z"}]
    found = da.deadlines_from_events(events)
    assert [(d.gw, d.kind) for d in found] == [(1, "lineup")]


def test_main_rejects_bad_config(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("USER_TZ", "Mars/Olympus")
    monkeypatch.setattr(da, "load_dotenv", lambda *a, **k: None)
    assert da.main(["plan", "--data-root", str(tmp_path)]) == 2
    assert "USER_TZ" in capsys.readouterr().err


def test_research_available_matches_find_spec():
    import importlib.util
    assert da.research_available() == (importlib.util.find_spec("backend.ml.research") is not None)


def test_checklists_written_atomically(tmp_path):
    deadline = da.Deadline(3, "waivers", LINEUP_AT)
    path = da.write_checklist(tmp_path / "out", deadline, "hello\n")
    assert path.name == "gw3_waivers.md" and path.read_text() == "hello\n"
    assert list(path.parent.iterdir()) == [path]                # no temp left behind


def test_atomic_write_uses_unique_temp_names(tmp_path, monkeypatch):
    seen = []
    real_replace = da.os.replace

    def spy(src, dst):
        seen.append(Path(src).name)
        real_replace(src, dst)

    monkeypatch.setattr(da.os, "replace", spy)
    target = tmp_path / "state.json"
    da.atomic_write_text(target, "a")
    da.save_state(target, {"b": 1})
    assert len(set(seen)) == 2 and all(n.startswith(".state.json.") for n in seen)
    assert json.loads(target.read_text()) == {"b": 1}


def test_atomic_write_failure_cleans_temp_and_keeps_old_file(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text("old")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(da.os, "replace", boom)
    with pytest.raises(OSError):
        da.atomic_write_text(target, "new")
    assert target.read_text() == "old" and list(tmp_path.iterdir()) == [target]


def test_second_tick_while_lock_held_does_nothing(tmp_path):
    env, recorder = make_env(tmp_path, make_world(tmp_path))
    with da.tick_lock(env.state_path.with_suffix(".lock")) as held:
        assert held
        assert da.tick(env, DELIVER_AT) == ["another tick holds the lock; skipped"]
    assert recorder.commands == [] and recorder.notes == [] and not env.state_path.exists()
    da.tick(env, DELIVER_AT)                                    # lock released -> normal run
    assert len(recorder.notes) == 1


def test_tick_lock_is_released_on_error(tmp_path):
    lock_path = tmp_path / "x.lock"
    with pytest.raises(RuntimeError):
        with da.tick_lock(lock_path):
            raise RuntimeError("tick crashed")
    with da.tick_lock(lock_path) as held:
        assert held


def test_derive_command_keeps_a_spaced_data_root_as_one_argv_item(tmp_path):
    root = tmp_path / "fpl review-data"
    assert da.derive_command("2026-27", root) == [
        "make", "derive", "SEASON=2026-27", f"DATA_DIR={root.resolve()}"]


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_make_derive_quotes_a_data_root_with_spaces(tmp_path):
    """Regression: unquoted $(DATA_DIR) split a spaced root into several arguments."""
    repo = Path(__file__).resolve().parents[3]
    root = str(tmp_path / "fpl review-data")
    out = subprocess.run(
        ["make", "-n", "derive", f"DATA_DIR={root}", "SEASON=2026-27"],
        cwd=repo, capture_output=True, text=True, check=True).stdout
    printed = [ln for ln in out.replace("\\\n", " ").splitlines() if "backend.ml" in ln]
    assert printed, out
    for line in printed:
        tokens = shlex.split(line)
        assert "review-data" not in tokens, line
        assert not any(t.startswith("review-data") for t in tokens), line
        assert any(root in t for t in tokens), line
