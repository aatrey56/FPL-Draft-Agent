# Deadline checklist agent

A compact, fresh checklist 30–60 minutes before each FPL Draft deadline
(trades close, waivers due, lineup lock) — delivered only while you are awake,
after a just-in-time research + derive run.

```bash
make checklist-plan                    # next deadlines + computed research/delivery times
make checklist-preview KIND=waivers    # render a checklist to stdout, send nothing
make autopilot                         # installs the 5-minute tick (com.fplcopilot.deadline)
```

Module: `apps/backend/backend/ml/deadline_agent.py`
(`python -m backend.ml.deadline_agent {tick|plan|preview}`, run from `apps/backend`).

## Deadlines

Per gameweek the draft calendar gives three instants, 24 h apart
(`events[].trades_time`, `waivers_time`, `deadline_time`; see `deadlines.go`).
Free agency is open between waivers processed and the lineup lock, so the
lineup checklist also lists free agents worth adding.

## Delivery-time algorithm

`compute_delivery(deadline, cfg)` is a pure function. Let
`target = deadline − CHECKLIST_LEAD_MIN` and let the **deliverable window** of
a local day be `[AWAKE_START, AWAKE_END − MIN_ACTION_MIN]` (it crosses midnight
when `AWAKE_END <= AWAKE_START`). The buffer at the end is deliberate: a
checklist that lands in the last minutes before bedtime leaves no time to act.
Equal times (`00:00`/`00:00`) mean **always awake**: there is no bedtime, so no
buffer — the whole day is deliverable and only the `MIN_ACTION_MIN`-before-
deadline rule applies.

Because `tick` only runs every 5 minutes and refuses to send outside a window
(see below), slots are **scheduled** with one tick of slack: each window is cut
at `deadline − MIN_ACTION_MIN` and then shortened by 5 min, so a tick is
guaranteed to land inside it.

1. `target` inside a (scheduling) window → deliver at `target`.
2. Otherwise, if the next window start after `target` still leaves
   `MIN_ACTION_MIN` + 5 min before the deadline → deliver at that window start.
3. Otherwise → the latest window end before the deadline (the night before).
4. If there is no window at all (degenerate config) or the slot is more than
   24 h before the deadline → deliver anyway and mark the checklist **early**.

Defaults (`America/New_York`, 07:00–02:00, lead 45, min action 15), local times:

| Deadline | target | delivered | why |
|---|---|---|---|
| 20:00 | 19:15 | **19:15** | inside the window |
| 07:20 | 06:35 | **07:00** | asleep at target; 07:00 leaves 20 ≥ 15 min |
| 07:10 | 06:25 | **01:40** (night before) | 07:00 would leave only 10 min |
| 04:30 | 03:45 | **01:40** | window ends 02:00, minus the 15-min buffer, minus one tick |
| 02:30 | 01:45 | **01:40** | target is past the scheduling end (01:40) |

Wall-clock times are resolved with `zoneinfo`, so DST changes are handled by
the zone rules: on 2026-11-01 (fall back) that night's window is an hour longer
in real time; on 2027-03-14 the nonexistent 02:00 resolves with the pre-change
offset. Research starts `RESEARCH_LEAD_MIN` before delivery (it ignores awake
hours — it is the machine working, not you).

## `tick`

Run every 300 s by launchd (`scripts/deadline_tick.sh`), independent of the
15-minute refresh, so timing is accurate to about five minutes. It is
idempotent; state lives in `~/.fplcopilot/deadline_agent.json`, keyed by
`<season>:<gw>:<kind>` (gw ids restart every season, so last season's GW9 never
suppresses this season's). Pre-namespacing `<gw>:<kind>` entries are migrated
under the current season only when their sent/missed time is within 11 days
before / 48 h after this season's matching deadline; anything else is dropped
as earlier-season history. A tick holds an exclusive `flock` on
`~/.fplcopilot/deadline_agent.lock` from loading the state to its final save:
an overlapping tick (launchd + a manual run) logs *another tick holds the lock;
skipped* and exits 0, so research and delivery never run twice. State and
checklist files are written via a uniquely named temp file + rename.

1. `now ≥ research_start` and research not done → run
   `uv run python -m backend.ml.research run --phase <waivers|lineup|trades> --gw N`
   if that module exists (exit 0 ok, exit 3 = no API key → skipped silently,
   anything else → the checklist says *research failed*), then `make derive`
   (under `autorefresh.sh`'s lock so two derives never overlap; if the lock is
   busy for 5 min the derive is skipped and the artifacts from the last refresh
   are used). Both commands run in their own process group with a 15-min
   timeout; on timeout the whole group gets SIGTERM, then SIGKILL after 10 s,
   and is reaped before the derive lock is released — `make`'s children never
   keep writing behind a released lock.
2. `now ≥ deliver_at` and not sent → re-read the clock and send only if that
   instant is inside a deliverable window **and** at least `MIN_ACTION_MIN`
   before the deadline (`delivery_slot`). The clock is injected (`Env.clock`)
   and re-read after research/derive (which, with the lock wait, can take
   ~35 min) and immediately before sending; `sent` records that real instant
   and the checklist countdown is computed from it.
3. A catch-up tick (the Mac was asleep at `deliver_at`) outside the window
   never wakes you: if a later in-window slot still leaves `MIN_ACTION_MIN`
   before the deadline (e.g. 07:00 for a 07:20 deadline) it waits for it;
   otherwise the deadline is logged as **missed** with a `missed_reason`.
4. `now ≥ deadline` and not sent → logged as **missed**; a checklist is never
   delivered after its deadline. A missed deadline is not researched.

## Checklist content

Rendered from local JSON only (`waiver_plan.json`, `my_week.json`,
`player_values.json`, `role_overrides.json`, `league/<id>/details.json`); a
missing artifact becomes a one-line pointer rather than an error. ≤ ~25 lines.

- **Trades:** deadline + countdown, "any to propose/accept?", up to 3 notes
  (squad players with a weak 3-GW outlook, departed players, availability
  flags), and an `ask Claude: trade_check give=… get=…` line.
- **Waivers:** up to 5 ranked claims (add → drop, 3-GW gain, label, risk news;
  one claim per drop), backup claims, players changed by research since the
  last checklist, and your waiver order (`waiver_pick`).
- **Lineup lock:** XI with formation, bench in auto-sub order (FPL auto-subs a
  0-minute starter from that order, so only the order needs checking),
  doubtful starters with their latest note and `if_out` fallback, free agents
  worth adding.
- **Footer:** `panel_max_gw`, `player_values` `generated_at`, artifact ages,
  scorer and any fallback flags.

"Changed by research" means `role_overrides.json` entries tagged as research
(`origin: "research"`, a `source` starting with `research` or a URL) whose
`as_of` is newer than the previous checklist.

## Delivery channels

- File: `~/.fplcopilot/checklists/<season>/gw<N>_<kind>.md`.
- macOS notification (`osascript`): title + one-line summary. A nonzero
  `osascript` exit (e.g. notifications not permitted) is logged with its stderr
  and recorded as `delivery_failed: ["macos"]` on the state entry; the file and
  ntfy are still delivered and the checklist is not re-sent.
- ntfy (optional): set `NTFY_TOPIC`; the markdown is POSTed with
  `Markdown: yes`, priority high for the lineup lock. Network errors are
  logged and never crash the tick.

**Privacy:** ntfy topics are public on ntfy.sh — anyone who knows the name can
read it. Use an unguessable topic (`openssl rand -hex 12`), keep league/entry
ids and team names out of it, or self-host / protect it (`NTFY_SERVER`). The
checklist itself contains player names and your squad.

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `USER_TZ` | `America/New_York` | IANA zone for awake hours and displayed times |
| `AWAKE_START` | `07:00` | window start |
| `AWAKE_END` | `02:00` | window end; at/before start = next day |
| `CHECKLIST_LEAD_MIN` | `45` | ideal lead, 30–60 |
| `MIN_ACTION_MIN` | `15` | minimum time left to act (≤ lead); also the pre-bedtime buffer |
| `RESEARCH_LEAD_MIN` | `25` | research + derive start before delivery |
| `NTFY_TOPIC` | – | enables the push |
| `NTFY_SERVER` | `https://ntfy.sh` | ntfy base URL |

Invalid values make the command exit with a config error rather than guess.
`notify_state.py` keeps the 24 h heads-up, GW finished and waivers processed
notices; its 3 h / 2 h reminders were removed in favour of this agent.
