"""Deadline checklist agent — a compact, fresh checklist before each FPL Draft deadline.

For every upcoming deadline (trades, waivers, lineup lock) the agent picks a
delivery instant that is both close to ``deadline - CHECKLIST_LEAD_MIN`` and
inside the hours the user is awake, runs fresh research + ``make derive`` just
before it, renders a short markdown checklist from the local JSON artifacts and
delivers it (file, macOS notification, optional ntfy push).

CLI (run from ``apps/backend``)::

    python -m backend.ml.deadline_agent plan                    # next deadlines + computed times
    python -m backend.ml.deadline_agent preview --kind waivers  # render, print, send nothing
    python -m backend.ml.deadline_agent tick                    # what launchd runs every 300 s

Configuration (``.env`` at the repo root, a real environment variable wins):
``USER_TZ``, ``AWAKE_START``, ``AWAKE_END``, ``CHECKLIST_LEAD_MIN``,
``MIN_ACTION_MIN``, ``RESEARCH_LEAD_MIN``, ``NTFY_TOPIC``, ``NTFY_SERVER`` —
see ``docs/DEADLINE_AGENT.md`` and ``.env.example``.

Delivery-time algorithm (:func:`compute_delivery`, pure, DST-correct):

* ``target = deadline - lead``. The *deliverable window* of each local day is
  ``[AWAKE_START, AWAKE_END - MIN_ACTION_MIN]``: a checklist that lands in the
  last ``MIN_ACTION_MIN`` before bedtime leaves no time to act on it. The
  window crosses midnight when ``AWAKE_END <= AWAKE_START`` (07:00-02:00).
* ``target`` inside a window -> deliver at ``target``.
* Otherwise take the nearest window start after ``target`` when it still
  leaves ``MIN_ACTION_MIN`` before the deadline (deadline 07:20, lead 45 ->
  07:00); else the latest window end before ``target`` (deadline 04:30 ->
  01:45 the night before).
* No window slot within 24 h before the deadline -> the latest earlier slot,
  flagged ``early``.

Everything here reads local files only; the only network call is the optional
ntfy POST, whose failure is logged and never raised.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv

from backend.ml import paths

logger = logging.getLogger(__name__)

UTC = timezone.utc
DEFAULT_SEASON = "2026-27"
STATE_DIR = Path.home() / ".fplcopilot"

# Deadline kind -> bootstrap event field. Order = chronological within a GW
# (trades close 24 h before waivers, waivers 24 h before the lineup lock).
KIND_FIELDS = {"trades": "trades_time", "waivers": "waivers_time", "lineup": "deadline_time"}
KINDS = tuple(KIND_FIELDS)
KIND_LABELS = {"trades": "Trades close", "waivers": "Waivers due", "lineup": "Lineup lock"}

LEAD_MIN_RANGE = (30, 60)
EARLY_SEARCH_DAYS = 10          # how far back to look for any deliverable slot
EARLY_HORIZON = timedelta(hours=24)
MISSED_LOOKBACK = timedelta(hours=48)   # older unsent deadlines are history, not "missed"
RESEARCH_TIMEOUT_S = 900
DERIVE_TIMEOUT_S = 900
DERIVE_LOCK_WAIT_S = 300
KILL_GRACE_S = 10               # SIGTERM -> SIGKILL grace for a timed-out process group
KILL_POLL_S = 0.2
RESEARCH_NO_KEY_EXIT = 3        # backend.ml.research: no API key -> skip silently
MAX_CLAIMS = 5
MAX_BACKUPS = 3
MAX_TRADE_NOTES = 3


class ConfigError(ValueError):
    """Invalid agent configuration (bad timezone, time or range)."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    """Agent knobs; all times are wall-clock in ``tz``."""

    tz: ZoneInfo
    awake_start: dtime
    awake_end: dtime
    lead_min: int = 45
    min_action_min: int = 15
    research_lead_min: int = 25
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"

    def __post_init__(self) -> None:
        if not LEAD_MIN_RANGE[0] <= self.lead_min <= LEAD_MIN_RANGE[1]:
            raise ConfigError(f"CHECKLIST_LEAD_MIN must be {LEAD_MIN_RANGE[0]}-"
                              f"{LEAD_MIN_RANGE[1]}, got {self.lead_min}")
        if self.min_action_min < 0 or self.min_action_min > self.lead_min:
            raise ConfigError("MIN_ACTION_MIN must be between 0 and CHECKLIST_LEAD_MIN, "
                              f"got {self.min_action_min}")
        if self.research_lead_min < 0:
            raise ConfigError("RESEARCH_LEAD_MIN must be >= 0")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        """Build from environment variables (defaults in the module docs)."""
        env = os.environ if env is None else env
        tz_name = env.get("USER_TZ") or "America/New_York"
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"USER_TZ {tz_name!r} is not an IANA timezone") from exc
        return cls(
            tz=tz,
            awake_start=parse_clock(env.get("AWAKE_START") or "07:00", "AWAKE_START"),
            awake_end=parse_clock(env.get("AWAKE_END") or "02:00", "AWAKE_END"),
            lead_min=_int(env, "CHECKLIST_LEAD_MIN", 45),
            min_action_min=_int(env, "MIN_ACTION_MIN", 15),
            research_lead_min=_int(env, "RESEARCH_LEAD_MIN", 25),
            ntfy_topic=(env.get("NTFY_TOPIC") or "").strip() or None,
            ntfy_server=(env.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/"),
        )


def parse_clock(text: str, name: str = "time") -> dtime:
    """``"07:00"`` -> ``time(7, 0)``; raises ConfigError on anything else."""
    try:
        hours, minutes = text.strip().split(":")
        return dtime(int(hours), int(minutes))
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"{name} must be HH:MM, got {text!r}") from exc


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


# ---------------------------------------------------------------------------
# Delivery-time algorithm (pure)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Delivery:
    """When to deliver one deadline's checklist (UTC instants)."""

    deadline: datetime
    target: datetime          # deadline - lead
    deliver_at: datetime
    research_start: datetime
    early: bool = False       # no awake slot within 24 h of the deadline


def _aware_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return moment.astimezone(UTC)


def deliverable_windows(cfg: Config, first_day: date, last_day: date) -> list[tuple[datetime, datetime]]:
    """UTC ``(start, end)`` deliverable windows for local days ``first_day..last_day``.

    Day ``d``'s window is ``[AWAKE_START, AWAKE_END - MIN_ACTION_MIN]`` where
    ``AWAKE_END`` falls on ``d + 1`` when it is not after ``AWAKE_START``
    (midnight-crossing). ``AWAKE_START == AWAKE_END`` means awake all day:
    there is no bedtime, so no buffer either, and consecutive windows touch —
    only the MIN_ACTION_MIN-before-deadline rule of :func:`compute_delivery`
    applies. Wall-clock times are resolved with the zone's rules, so a DST
    change makes that day's window 1 h shorter/longer in real time; a
    nonexistent wall time (spring forward) resolves with the old offset
    (``fold=0``). Windows too short to leave ``MIN_ACTION_MIN`` are dropped.
    """
    always_awake = cfg.awake_end == cfg.awake_start
    buffer = timedelta(0) if always_awake else timedelta(minutes=cfg.min_action_min)
    crosses = cfg.awake_end <= cfg.awake_start
    windows = []
    day = first_day
    while day <= last_day:
        start = datetime.combine(day, cfg.awake_start, tzinfo=cfg.tz)
        end_day = day + timedelta(days=1) if crosses else day
        end = datetime.combine(end_day, cfg.awake_end, tzinfo=cfg.tz)
        start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC) - buffer
        if start_utc <= end_utc:
            windows.append((start_utc, end_utc))
        day += timedelta(days=1)
    return windows


def compute_delivery(deadline: datetime, cfg: Config) -> Delivery:
    """Delivery instant for one deadline (see the module docstring)."""
    deadline = _aware_utc(deadline)
    target = deadline - timedelta(minutes=cfg.lead_min)
    latest_ok = deadline - timedelta(minutes=cfg.min_action_min)
    local = deadline.astimezone(cfg.tz).date()
    windows = deliverable_windows(
        cfg, local - timedelta(days=EARLY_SEARCH_DAYS), local + timedelta(days=1))

    def make(at: datetime, early: bool = False) -> Delivery:
        return Delivery(deadline, target, at, at - timedelta(minutes=cfg.research_lead_min), early)

    if any(start <= target <= end for start, end in windows):
        return make(target)
    after = [start for start, _ in windows if start > target and start <= latest_ok]
    if after:
        return make(min(after))
    before = [min(end, latest_ok) for start, end in windows if start <= latest_ok]
    if not before:      # no window at all (e.g. empty awake range): give up gracefully
        return make(target, early=True)
    slot = max(before)
    return make(slot, early=slot < deadline - EARLY_HORIZON)


# ---------------------------------------------------------------------------
# Deadlines from the season calendar
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Deadline:
    """One deadline of one gameweek."""

    gw: int
    kind: str
    at: datetime              # UTC

    @property
    def key(self) -> str:
        return f"{self.gw}:{self.kind}"


def parse_ts(value: Any) -> datetime | None:
    """ISO-8601 (``Z`` ok) -> aware UTC datetime, None when absent/garbled."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def load_events(raw_dir: Path) -> list[dict]:
    """Bootstrap ``events`` (draft ``{"data": [...]}`` or plain list); [] if unreadable."""
    path = raw_dir / "bootstrap" / "bootstrap-static.json"
    try:
        events = json.loads(path.read_text(encoding="utf-8")).get("events", [])
    except (OSError, ValueError, AttributeError) as exc:
        logger.warning("no season calendar at %s (%s)", path, exc)
        return []
    if isinstance(events, dict):
        events = events.get("data", [])
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def deadlines_from_events(events: list[dict], kinds: tuple[str, ...] = KINDS) -> list[Deadline]:
    """Every parseable deadline of ``events``, chronological."""
    found = []
    for event in events:
        gw = event.get("id")
        if not isinstance(gw, int):
            continue
        for kind in kinds:
            at = parse_ts(event.get(KIND_FIELDS[kind]))
            if at is not None:
                found.append(Deadline(gw, kind, at))
    return sorted(found, key=lambda d: (d.at, KINDS.index(d.kind)))


def next_deadlines(deadlines: list[Deadline], now: datetime, count: int = 6) -> list[Deadline]:
    """The first ``count`` deadlines strictly after ``now``."""
    return [d for d in deadlines if d.at > now][:count]


# ---------------------------------------------------------------------------
# State (idempotency)
# ---------------------------------------------------------------------------
def load_state(path: Path) -> dict[str, Any]:
    """The state file as a dict; a missing or corrupt file is an empty state."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic write (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Checklist rendering (local JSON artifacts only)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Sources:
    """Where the checklist reads from."""

    ml_dir: Path
    raw_dir: Path
    league_id: str | None = None
    entry_id: str | None = None


def _read_json(path: Path) -> dict | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _age(path: Path, now: datetime) -> str:
    try:
        seconds = (now - datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)).total_seconds()
    except OSError:
        return "missing"
    minutes = int(max(seconds, 0) // 60)
    return f"{minutes}m old" if minutes < 120 else f"{minutes // 60}h old"


def fmt_local(moment: datetime, tz: ZoneInfo, now: datetime | None = None) -> str:
    """``Sat 04:30 EDT`` plus ``(in 3h 45m)`` when ``now`` is given."""
    text = moment.astimezone(tz).strftime("%a %H:%M %Z")
    if now is None:
        return text
    return f"{text} ({fmt_delta(moment - now)})"


def fmt_delta(delta: timedelta) -> str:
    """``in 3h 45m`` / ``22m ago``."""
    minutes = int(abs(delta.total_seconds()) // 60)
    hours, rest = divmod(minutes, 60)
    span = f"{hours}h {rest:02d}m" if hours else f"{rest}m"
    return f"in {span}" if delta.total_seconds() >= 0 else f"{span} ago"


def _signed(value: Any) -> str:
    return "?" if not isinstance(value, (int, float)) else f"{value:+.1f}"


def _gain(rec: dict) -> str:
    gains = rec.get("gains") or {}
    gw3, gw1 = gains.get("gw3", rec.get("next3_gain")), gains.get("gw1", rec.get("next1_gain"))
    return f"3-GW {_signed(gw3)}" if gw3 is not None else f"GW {_signed(gw1)}"


def is_research_override(entry: dict) -> bool:
    """A role-override entry written by research (``origin == "research"``, or a
    ``source`` that is a URL / starts with ``research``)."""
    source = str(entry.get("source") or "").lower()
    return (entry.get("origin") == "research" or source.startswith("research")
            or source.startswith(("http://", "https://")))


def research_changes(ml_dir: Path, since: datetime | None) -> list[dict]:
    """Research-tagged role overrides written after ``since`` (all when None)."""
    doc = _read_json(ml_dir / "role_overrides.json") or {}
    entries = doc.get("overrides") if isinstance(doc.get("overrides"), list) else []
    file_as_of = parse_ts(doc.get("updated") or doc.get("as_of"))
    changed = []
    for entry in entries:
        if not isinstance(entry, dict) or not is_research_override(entry):
            continue
        as_of = parse_ts(entry.get("as_of")) or file_as_of
        if since is None or as_of is None or as_of > since:
            changed.append(entry)
    return changed


def _claims(plan: dict) -> tuple[list[dict], list[dict]]:
    """Ranked primary claims (one per drop, <= MAX_CLAIMS) and backups.

    Candidates are the overall ranking, then each position's best swaps, so a
    run of swaps at one position cannot hide the best move elsewhere. A claim
    whose drop (or add) is already used by a better claim is a backup.
    """
    ranked = list(plan.get("recommendations") or [])
    by_position = plan.get("best_by_position") or {}
    extras = [rec for recs in by_position.values() for rec in (recs or [])[:1]]
    seen, candidates = set(), []
    for rec in ranked + extras:
        ident = (rec.get("add_element"), rec.get("drop_element"))
        if ident not in seen:
            seen.add(ident)
            candidates.append(rec)
    primary, backups, used_drops, used_adds = [], [], set(), set()
    for rec in candidates:
        fresh = rec.get("drop_element") not in used_drops and rec.get("add_element") not in used_adds
        if fresh and len(primary) < MAX_CLAIMS:
            primary.append(rec)
            used_drops.add(rec.get("drop_element"))
            used_adds.add(rec.get("add_element"))
        elif rec.get("add_element") not in used_adds and len(backups) < MAX_BACKUPS:
            backups.append(rec)
    return primary, backups


def _claim_line(index: int, rec: dict, bullet: str | None = None) -> str:
    risk = f" · ⚠ {rec['news']}" if rec.get("news") else ""
    note = f" · {rec['add_role_override']}" if rec.get("add_role_override") else ""
    return (f"{bullet or str(index) + '.'} {rec.get('add')} ({rec.get('position')}, {rec.get('add_team')}) "
            f"→ drop {rec.get('drop')} · {_gain(rec)} · {rec.get('label')}{note}{risk}")


def _waiver_position(sources: Sources) -> str | None:
    if not (sources.league_id and sources.entry_id):
        return None
    doc = _read_json(sources.raw_dir / "league" / sources.league_id / "details.json")
    entries = (doc or {}).get("league_entries")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("entry_id")) == str(sources.entry_id):
            return f"#{entry['waiver_pick']} of {len(entries)}" if "waiver_pick" in entry else None
    return None


def _trades_section(plan: dict | None, week: dict | None) -> list[str]:
    lines = ["- Trades close — any to propose or accept?"]
    notes: list[str] = []
    for cand in (plan or {}).get("drop_candidates") or []:
        outlook = cand.get("next3_xp")
        if cand.get("status") == "u":
            notes.append(f"{cand['web_name']} ({cand['position']}) has left the league")
        elif isinstance(outlook, (int, float)) and outlook < 3.0:
            notes.append(f"{cand['web_name']} ({cand['position']}): weak 3-GW outlook "
                         f"(xP {outlook:.1f}) — trade-out candidate")
    for item in (week or {}).get("attention") or []:
        warnings = [w for w in item.get("warnings") or [] if not w.startswith("no model xP")]
        if warnings:
            notes.append(f"{item['web_name']}: {warnings[0]}")
    lines += [f"- {note}" for note in notes[:MAX_TRADE_NOTES]]
    lines.append("- Ask Claude: `trade_check give=… get=…`")
    return lines


def _waivers_section(plan: dict | None, since: datetime | None, sources: Sources) -> list[str]:
    if plan is None:
        return ["- waiver_plan.json missing — ask Claude for `waiver_plan`"]
    primary, backups = _claims(plan)
    lines = [_claim_line(i, rec) for i, rec in enumerate(primary, 1)]
    if not lines:
        lines.append("- No claim beats your current squad.")
    if backups:
        lines.append("Backups if a claim fails:")
        lines += [_claim_line(0, rec, "-") for rec in backups]
    changed = research_changes(sources.ml_dir, since)
    if changed:
        lines.append("Changed by research since last checklist:")
        lines += [f"- {e.get('player')} ({e.get('team')}): {e.get('fact')}" for e in changed[:5]]
    order = _waiver_position(sources)
    if order:
        lines.append(f"Your waiver order: {order}")
    return lines


def _lineup_section(plan: dict | None, week: dict | None) -> list[str]:
    if week is None:
        return ["- my_week.json missing — ask Claude for `my_week`"]
    xi = week.get("xi") or []
    names = ", ".join(p.get("web_name", "?") for p in xi)
    lines = [f"XI ({week.get('formation') or 'no legal formation'}): {names}",
             "Bench (auto-sub order): " + (" > ".join(week.get("bench_order") or []) or "—"),
             "- FPL auto-subs a 0-minute starter from this order; just check it is the order you want."]
    doubtful = [p for p in xi if p.get("warnings") or (p.get("p_start") is not None and p["p_start"] < 0.75)]
    if doubtful:
        lines.append("Doubtful starters:")
        for p in doubtful[:4]:
            note = p["warnings"][0] if p.get("warnings") else f"p_start {p['p_start']:.2f}"
            lines.append(f"- {p.get('web_name')}: {note}")
        lines += [f"- {alt['text']}" for alt in (week.get("if_out") or [])[:2] if alt.get("text")]
    if plan is not None:
        primary, _ = _claims(plan)
        if primary:
            lines.append("Free agency is open — worth adding before lock:")
            lines += [_claim_line(0, rec, "-") for rec in primary[:3]]
    return lines


def _footer(sources: Sources, week: dict | None, plan: dict | None, now: datetime) -> list[str]:
    values = _read_json(sources.ml_dir / "player_values.json") or {}
    parts = [f"panel_max_gw {values.get('panel_max_gw', '?')}",
             f"player_values generated {values.get('generated_at', '?')}"]
    for name in ("waiver_plan", "my_week"):
        parts.append(f"{name} {_age(sources.ml_dir / (name + '.json'), now)}")
    flags = []
    for label, doc in (("waiver_plan", plan), ("my_week", week)):
        if doc is None:
            continue
        flags.append(f"{label} scorer {doc.get('scorer', '?')}")
        if doc.get("xp_fallback"):
            flags.append(f"FALLBACK: {doc.get('xp_fallback_reason')}")
        if doc.get("horizon_fallback"):
            flags.append(f"HORIZON FALLBACK: {doc.get('horizon_fallback_reason')}")
    lines = ["_" + " · ".join(parts) + "_"]
    if flags:
        lines.append("_" + " · ".join(flags) + "_")
    return lines


def render_checklist(deadline: Deadline, delivery: Delivery, cfg: Config, sources: Sources,
                     now: datetime, *, since: datetime | None = None,
                     research_note: str | None = None) -> str:
    """The checklist markdown for ``deadline`` (<= ~25 lines); missing artifacts degrade
    to a one-line pointer instead of failing."""
    plan = _read_json(sources.ml_dir / "waiver_plan.json")
    week = _read_json(sources.ml_dir / "my_week.json")
    header = (f"## GW{deadline.gw} {KIND_LABELS[deadline.kind].lower()} — "
              f"{fmt_local(deadline.at, cfg.tz, now)}")
    lines = [header]
    if delivery.early:
        lines.append("_Early: no awake slot closer to the deadline._")
    if deadline.kind == "trades":
        lines += _trades_section(plan, week)
    elif deadline.kind == "waivers":
        lines += _waivers_section(plan, since, sources)
    else:
        lines += _lineup_section(plan, week)
    if research_note:
        lines.insert(1, f"⚠ {research_note}")
    lines += [""] + _footer(sources, week, plan, now)
    return "\n".join(lines) + "\n"


def summary_line(deadline: Deadline, cfg: Config, markdown: str) -> str:
    """One line for the notification: headline + the most actionable line."""
    lines = markdown.splitlines()
    if deadline.kind == "trades":
        detail = "any trades to propose or accept?"
    elif deadline.kind == "waivers":
        detail = next((ln[3:] for ln in lines if ln.startswith("1. ")), "see checklist")
    else:
        detail = next((ln for ln in lines if ln.startswith("XI (")), "see checklist")
    return f"{KIND_LABELS[deadline.kind]} {fmt_local(deadline.at, cfg.tz)} — {detail}"[:200]


# ---------------------------------------------------------------------------
# Delivery channels
# ---------------------------------------------------------------------------
def notify_macos(title: str, message: str, run: Callable[..., Any] = subprocess.run) -> bool:
    """macOS notification via osascript (arguments passed as argv — no quoting issues)."""
    script = ["on run argv", "display notification (item 1 of argv) with title (item 2 of argv)",
              "end run"]
    cmd = ["osascript"] + [part for line in script for part in ("-e", line)] + [message, title]
    try:
        result = run(cmd, check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("macOS notification failed: %s", exc)
        return False
    if result.returncode != 0:
        logger.warning("macOS notification failed: osascript exited %s: %s",
                       result.returncode, (result.stderr or "").strip()[:300])
        return False
    return True


def post_ntfy(cfg: Config, deadline: Deadline, markdown: str,
              post: Callable[..., Any] = requests.post) -> bool:
    """POST the markdown to ``NTFY_SERVER/NTFY_TOPIC``; never raises."""
    if not cfg.ntfy_topic:
        return False
    headers = {"Title": f"FPL GW{deadline.gw} {KIND_LABELS[deadline.kind]}",
               "Markdown": "yes", "Tags": "soccer",
               "Priority": "high" if deadline.kind == "lineup" else "default"}
    try:
        response = post(f"{cfg.ntfy_server}/{cfg.ntfy_topic}", data=markdown.encode("utf-8"),
                        headers=headers, timeout=10)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - any transport/HTTP failure must not crash tick
        logger.warning("ntfy delivery failed (%s): %s", type(exc).__name__, exc)
        return False
    return True


def failed_channels(cfg: Config, notified: bool, pushed: bool) -> list[str]:
    """Push channels that failed (ntfy only counts when ``NTFY_TOPIC`` is set)."""
    failed = [] if notified else ["macos"]
    if cfg.ntfy_topic and not pushed:
        failed.append("ntfy")
    return failed


def write_checklist(directory: Path, deadline: Deadline, markdown: str) -> Path:
    """``gw<N>_<kind>.md`` under ``directory`` (atomic)."""
    path = directory / f"gw{deadline.gw}_{deadline.kind}.md"
    directory.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(markdown, encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Research + derive
# ---------------------------------------------------------------------------
def default_runner(cmd: list[str], cwd: Path, timeout: int, *,
                   popen: Callable[..., Any] = subprocess.Popen,
                   killpg: Callable[[int, int], None] = os.killpg) -> int:
    """Run ``cmd`` in its own process group; its exit code, -1 on timeout / missing binary.

    ``make derive`` forks the real work, so killing only ``make`` on timeout
    would leave its children writing artifacts after the caller releases the
    derive lock. The command therefore starts a new session (its pid is the
    process-group id) and a timeout terminates the whole group, reaped,
    before this returns.
    """
    try:
        proc = popen(cmd, cwd=cwd, start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("%s failed to run: %s", cmd[0], exc)
        return -1
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("%s timed out after %ss; terminating its process group", cmd[0], timeout)
        terminate_group(proc, killpg)
        return -1


def terminate_group(proc: Any, killpg: Callable[[int, int], None] = os.killpg,
                    grace_s: float = KILL_GRACE_S,
                    sleep: Callable[[float], None] = time.sleep) -> None:
    """SIGTERM ``proc``'s process group, give it ``grace_s`` to exit, SIGKILL
    whatever is left, and reap ``proc``. ``killpg(pgid, 0)`` raising
    ProcessLookupError means the group is gone."""
    with contextlib.suppress(ProcessLookupError):
        killpg(proc.pid, signal.SIGTERM)
    for _ in range(max(int(grace_s / KILL_POLL_S), 1)):
        proc.poll()                     # reap the leader so it does not keep the group alive
        try:
            killpg(proc.pid, 0)
        except ProcessLookupError:
            break
        sleep(KILL_POLL_S)
    with contextlib.suppress(ProcessLookupError):
        killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def research_available() -> bool:
    """True when ``backend.ml.research`` exists (shipped by a separate change)."""
    try:
        return importlib.util.find_spec("backend.ml.research") is not None
    except (ImportError, ValueError):
        return False


@contextlib.contextmanager
def derive_lock(repo: Path, wait_s: int = DERIVE_LOCK_WAIT_S,
                sleep: Callable[[float], None] = time.sleep) -> Iterator[bool]:
    """Share autorefresh.sh's lock directory so two derives never overlap.

    Yields True when held; False when the lock stayed busy for ``wait_s``
    (the caller then skips its derive — autorefresh's own is at most 15 min old).
    """
    lock = repo / ".autorefresh.lock"
    held, waited = False, 0
    while True:
        try:
            lock.mkdir()
            held = True
            break
        except FileExistsError:
            if waited >= wait_s:
                break
            sleep(10)
            waited += 10
        except OSError as exc:
            logger.warning("cannot take %s: %s", lock, exc)
            break
    try:
        yield held
    finally:
        if held:
            with contextlib.suppress(OSError):
                lock.rmdir()


def run_research_and_derive(deadline: Deadline, repo: Path, season: str,
                            runner: Callable[[list[str], Path, int], int] = default_runner,
                            research_ok: Callable[[], bool] = research_available,
                            lock: Callable[[Path], Any] = derive_lock) -> dict[str, str]:
    """-> ``{"research": ok|skipped|failed|absent, "derive": ok|failed|busy}``."""
    outcome = {"research": "absent", "derive": "failed"}
    backend = repo / "apps" / "backend"
    if research_ok():
        code = runner(["uv", "run", "python", "-m", "backend.ml.research", "run",
                       "--phase", deadline.kind, "--gw", str(deadline.gw)],
                      backend, RESEARCH_TIMEOUT_S)
        outcome["research"] = ("ok" if code == 0 else
                               "skipped" if code == RESEARCH_NO_KEY_EXIT else "failed")
    with lock(repo) as held:
        if not held:
            outcome["derive"] = "busy"
        else:
            code = runner(["make", "derive", f"SEASON={season}"], repo, DERIVE_TIMEOUT_S)
            outcome["derive"] = "ok" if code == 0 else "failed"
    return outcome


def outcome_note(outcome: Mapping[str, str]) -> str | None:
    """The checklist warning for a research/derive outcome (None = nothing to say)."""
    problems = []
    if outcome.get("research") == "failed":
        problems.append("research failed — team news below is not freshly researched")
    if outcome.get("derive") == "failed":
        problems.append("derive failed — artifacts may be stale")
    return "; ".join(problems) or None


# ---------------------------------------------------------------------------
# tick / plan / preview
# ---------------------------------------------------------------------------
@dataclass
class Env:
    """Everything tick needs from the outside world (injectable for tests)."""

    cfg: Config
    repo: Path
    season: str
    sources: Sources
    state_path: Path
    checklist_dir: Path
    runner: Callable[[list[str], Path, int], int] = default_runner
    research_ok: Callable[[], bool] = research_available
    lock: Callable[[Path], Any] = derive_lock
    notify: Callable[[str, str], bool] = notify_macos
    ntfy: Callable[[Config, Deadline, str], bool] = post_ntfy


def tick(env: Env, now: datetime) -> list[str]:
    """One idempotent pass; returns human-readable log lines for what happened.

    Per ``gw:kind`` the state records ``research`` (outcome dict), ``sent``
    (ISO time) or ``missed``. Order: research once ``now >= research_start``,
    delivery once ``now >= deliver_at``; a deadline that has passed without a
    delivery is logged as missed and never delivered.
    """
    now = _aware_utc(now)
    state = load_state(env.state_path)
    log: list[str] = []
    for deadline in deadlines_from_events(load_events(env.sources.raw_dir)):
        if deadline.at < now - MISSED_LOOKBACK:
            continue
        entry = state.setdefault(deadline.key, {})
        if entry.get("sent") or entry.get("missed"):
            continue
        delivery = compute_delivery(deadline.at, env.cfg)
        if now >= deadline.at:
            entry["missed"] = now.isoformat(timespec="seconds")
            log.append(f"MISSED {deadline.key}: deadline passed before delivery")
            save_state(env.state_path, state)
            continue
        if now >= delivery.research_start and "research" not in entry:
            entry["research"] = run_research_and_derive(
                deadline, env.repo, env.season, env.runner, env.research_ok, env.lock)
            log.append(f"research {deadline.key}: {entry['research']}")
            save_state(env.state_path, state)
        if now >= delivery.deliver_at:
            since = parse_ts(state.get("last_checklist_at"))
            note = outcome_note(entry.get("research") or {})
            markdown = render_checklist(deadline, delivery, env.cfg, env.sources, now,
                                        since=since, research_note=note)
            path = write_checklist(env.checklist_dir, deadline, markdown)
            failed = failed_channels(
                env.cfg, env.notify("FPL Co-Pilot", summary_line(deadline, env.cfg, markdown)),
                env.ntfy(env.cfg, deadline, markdown))
            entry["sent"] = now.isoformat(timespec="seconds")
            if failed:
                entry["delivery_failed"] = failed
            state["last_checklist_at"] = entry["sent"]
            save_state(env.state_path, state)
            suffix = f" (FAILED: {', '.join(failed)}; the file was written)" if failed else ""
            log.append(f"delivered {deadline.key} -> {path}{suffix}")
    return log


def plan_lines(env: Env, now: datetime, count: int = 6) -> list[str]:
    """Next deadlines with their computed research and delivery times."""
    upcoming = next_deadlines(deadlines_from_events(load_events(env.sources.raw_dir)), now, count)
    if not upcoming:
        return ["no upcoming deadlines in the season calendar"]
    tz = env.cfg.tz
    lines = [f"now {fmt_local(now, tz)} · awake {env.cfg.awake_start:%H:%M}-{env.cfg.awake_end:%H:%M} "
             f"{tz.key} · lead {env.cfg.lead_min}m · min action {env.cfg.min_action_min}m"]
    for deadline in upcoming:
        slot = compute_delivery(deadline.at, env.cfg)
        flag = "  [early]" if slot.early else ""
        lines.append(f"GW{deadline.gw:<2} {deadline.kind:<8} deadline {fmt_local(deadline.at, tz)}"
                     f" | research {fmt_local(slot.research_start, tz)}"
                     f" | deliver {fmt_local(slot.deliver_at, tz)}"
                     f" ({int((deadline.at - slot.deliver_at).total_seconds() // 60)}m before){flag}")
    return lines


def preview(env: Env, now: datetime, kind: str | None = None) -> str:
    """Checklist for the next deadline (of ``kind``, if given); sends nothing."""
    upcoming = [d for d in deadlines_from_events(load_events(env.sources.raw_dir)) if d.at > now
                and (kind is None or d.kind == kind)]
    if not upcoming:
        return "no upcoming deadline" + (f" of kind {kind}" if kind else "") + "\n"
    deadline = upcoming[0]
    since = parse_ts(load_state(env.state_path).get("last_checklist_at"))
    return render_checklist(deadline, compute_delivery(deadline.at, env.cfg), env.cfg,
                            env.sources, now, since=since)


def build_env(season: str, data_root: Path | None = None, state_dir: Path = STATE_DIR) -> Env:
    """Env from ``.env`` / the environment and the repo layout."""
    repo = paths.default_data_root().parent
    load_dotenv(repo / ".env")
    data = data_root or paths.default_data_root()
    return Env(
        cfg=Config.from_env(), repo=repo, season=season,
        sources=Sources(ml_dir=paths.derived_root(season, data) / "ml",
                        raw_dir=paths.raw_root(season, data),
                        league_id=os.getenv("LEAGUE_ID") or None,
                        entry_id=os.getenv("ENTRY_ID") or None),
        state_path=state_dir / "deadline_agent.json",
        checklist_dir=state_dir / "checklists")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=["tick", "plan", "preview"])
    parser.add_argument("--season", default=os.getenv("SEASON") or DEFAULT_SEASON)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--kind", choices=KINDS, default=None,
                        help="preview: checklist kind (default: the next deadline)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        env = build_env(args.season, args.data_root)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    now = datetime.now(UTC)
    if args.command == "plan":
        print("\n".join(plan_lines(env, now)))
    elif args.command == "preview":
        print(preview(env, now, args.kind), end="")
    else:
        for line in tick(env, now):
            logger.info(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
