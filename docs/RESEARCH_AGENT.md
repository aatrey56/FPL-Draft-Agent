# Research agent — pre-deadline team news → sourced role overrides

`backend.ml.research` checks the news before each deadline and turns what it
finds into dated, sourced entries in
`data/derived/<season>/ml/role_overrides.research.json`. waiver_plan, my_week
and player_values read that file **below** the hand-maintained
`role_overrides.json`. It calls the Claude API (`claude-opus-5-5` with web
search and web fetch), billed to the Anthropic API credit that comes with the
Max plan.

## CLI contract

```bash
python -m backend.ml.research run --phase {waivers,lineup,trades} \
    [--gw N] [--dry-run] [--max-usd X] [--max-players N] \
    [--season 2026-27] [--data-root data] [--league ID] [--entry ID]
python -m backend.ml.research clear [--season 2026-27] [--data-root data]
make research PHASE=waivers [GW=n] [DRY_RUN=1]
make research-clear
```

| Exit | Meaning |
|---|---|
| 0 | Success. This includes a run that stopped early at the run cap, and a run with zero players to research. |
| 1 | Error: the bootstrap is missing or unreadable, there is no upcoming GW, or a fatal API error aborted the run (e.g. a rejected key). The spend so far is still recorded in the ledger. |
| 2 | Bad arguments, or a bad `RESEARCH_*` value |
| 3 | `ANTHROPIC_API_KEY` is not set, so research was skipped. A scheduler should treat this as "skip". |
| 4 | The monthly cap is already reached. Nothing ran and nothing was written. |

`--gw` defaults to the bootstrap's next gameweek. `--league` / `--entry`
default to `LEAGUE_ID` / `ENTRY_ID` from `.env`. `--dry-run` runs the triage,
prints the players it picked and a cost estimate, and exits 0. It needs no
key, makes no API calls and writes nothing.

## Inputs

All inputs are local files; only the research calls go out to the API.

- `raw/<season>/bootstrap/bootstrap-static.json`: status, news,
  `chance_of_playing_next_round`, and the event calendar (`waivers_time`,
  `trades_time`, `deadline_time`).
- `raw/<season>/league/<LEAGUE_ID>/element-status.json`: the squad. If it is
  missing, the squad comes from `my_week.json`'s XI and bench.
- `derived/<season>/ml/waiver_plan.json`: free-agent targets, plus
  `club_moved` / `expected_minutes`. Optional.
- `derived/<season>/ml/my_week.json`: squad role signals, and doubtful
  starters (`if_out`). Optional.
- Both override files, used to find players with a live override.

## Triage

Triage is deterministic and uses no LLM.

**Pool.** The pool always holds the squad. In the `waivers` and `trades`
phases it also holds up to 25 free agents from waiver_plan: the
`recommendations` adds in rank order, then the `best_by_position` adds. The
`lineup` phase researches the squad only.

**Triggers.** A pool player is researched if **any** of these is true:

- `status != "a"`
- the news is not empty
- `chance_of_playing` < 100
- he has a live role override
- `club_moved`
- `expected_minutes` < 60
- he is a recommended add
- he is a doubtful starter in my_week

**Cap.** At most `RESEARCH_MAX_PLAYERS` (default 30) are researched, in this
order:

1. squad before free agents
2. doubtful starters first
3. recommendation rank
4. more triggers
5. element id

Every skipped player is logged with a reason: `no_trigger`, `over_cap` or
`not_in_bootstrap`.

## Research and extraction

1. **Research call (one per player).** The model is `claude-opus-5-5` with
   adaptive thinking and `output_config.effort = RESEARCH_EFFORT` (default
   `high`; `xhigh` is allowed). The tools are `web_search_20260318`
   (`max_uses` 5) and `web_fetch_20260318` (`max_uses` 3, 8k tokens per
   page).
   - The prompt asks for injury and fitness news, suspensions, knocks from
     international duty, depth chart, manager quotes and predicted lineups.
   - Every source needs a publication date and a verbatim quote.
   - Anything published before the deadline of the last finished GW counts
     as outdated, unless a newer source confirms it still holds.
   - Up to 4 calls run in parallel (`RESEARCH_CONCURRENCY`).
   - `pause_turn` is resumed up to 3 times. A `refusal` or `max_tokens` stop
     fails that player only.
2. **Extraction call.** This uses the same model at effort `low` with no
   tools, and returns structured output (`output_config.format`,
   `json_schema`). A second call is needed because web-search answers always
   carry citations, and structured outputs reject citations, so the first
   call cannot return the JSON directly.
3. **Refusal fallback.** Every request sends `fallbacks: "default"` (beta
   `server-side-fallback-2026-07-01`). A safety decline is then re-run
   server-side on the model Anthropic recommends.

Each player produces this object:

`{element, web_name, p_start, return_gw|null, valid_through_gw, status_claim,
conflict, confidence (low/med/high), evidence: [{claim, source_url,
published_at, quote}], summary (≤200 chars)}`.

**Validation** drops a result, with a logged reason, when:

- the element does not match the player researched
- `p_start` is outside [0, 1]
- a gameweek is out of range
- an enum value is unknown
- the summary is longer than 200 characters
- no valid evidence is left. An evidence item is dropped if its URL was never
  seen in the research, or if its date or quote is missing.

`return_gw` is set to null unless it is after the GW and the claim is an
absence (`ruled_out` / `suspended`).

## Credibility gate: what may change picks

Code decides what gets applied, not the model.

- **Tier.** Each evidence URL gets a tier by its domain (`SOURCE_OUTLETS`):
  - **1:** official club sites and premierleague.com
  - **2:** established outlets and beat reporters (BBC, Sky, The Athletic,
    Guardian, regional Reach titles, …)
  - **3:** everything else
- **Verified.** A quote counts only if it appears verbatim in the page text
  the research retrieved: fetched page text, or the citation `cited_text`.
  Case, whitespace and curly quotes are ignored.

| Decision | Rule | Effect |
|---|---|---|
| `applied` | All of these: no reported conflict; confidence med or high; verified support from one tier-1 source or from tier-2 sources at two different outlets; and at least one supporting source published on or after the last finished GW's deadline. | Written to `role_overrides.research.json` |
| `proposed` | It has a verified tier-1 or tier-2 source but fails another rule. | Listed in the run file and on stdout as a decision diff (`model_p_start → p_start`), but **never applied**. To approve one, copy it into `role_overrides.json`. |
| `watch` | No verified source is tier 1 or tier 2: rumour, or quotes that could not be verified. | Logged only |
| `no_change` | `status_claim = no_new_info` | Nothing |

The override's `p_start` is set by code from the claim (`CLAIM_P_START`):

| Claim | p_start |
|---|---|
| `ruled_out`, `suspended` | 0 |
| `benched` | 0.15 |
| `doubtful` | 0.4 |
| `rotation_risk` | 0.5 |
| `available` | 0.7 |
| `expected_start` | 0.9 |

The model's own number is kept as `llm_p_start`, for audit only.

## Outputs

All outputs are written atomically (temp file, then rename).

- `derived/<season>/ml/research/gw<N>_<phase>_<YYYYMMDDTHHMMSSZ>.json` holds
  the triage (selected and skipped), every finding with its research text,
  sources, tiered evidence, decision and reasons, the `decisions` lists
  (applied / proposed / watch), the players skipped by the cap, and the cost.
- `derived/<season>/ml/role_overrides.research.json` holds
  `{updated, gw, source: "research", research_run, overrides: [...]}`. Each
  entry contains:
  - `player`, `team`, `code`, `element`
  - `p_start` and `llm_p_start`, `status_claim`
  - `fact` (starts with `research:`)
  - `as_of`, `source: "research"`, `research_run`
  - `confidence`, `decision`, `evidence`
  - `valid_through_gw` set to this GW, or `return_gw` when the player is out
  
  Entries from earlier runs are kept only while they are still live, and only
  for players this run did not re-apply.
- `derived/<season>/ml/research/spend.jsonl` is the spend ledger, with one
  line per run.

## Precedence in the derived artifacts

`waiver.load_all_role_overrides` reads the manual file first, then the
research file.

- **Manual wins.** If a manual entry was applied to a player, his research
  entry is `overrides_superseded`. A manual entry that is stale, expired or
  blocked does not shadow fresh research.
- **Tagging.** Research entries are named `research:<player>` in every
  `overrides_*` list. Their `role_override` / `add_role_override` text, which
  my_week also shows under `attention`, starts with `research:`.
- **Same rules otherwise.** Research entries follow the usual staleness and
  availability-gate rules.
- **Re-derive needed.** Research takes effect on the next `make derive`, or
  the next autopilot refresh.

## Cost model and caps

Prices live in `PRICES_PER_MTOK` (USD per million tokens, checked 2026-10-08
against platform.claude.com/docs/en/about-claude/pricing):

| Model | Input | Output | Cache read | Cache write (5 min / 1 h) |
|---|---|---|---|---|
| Opus 5.5 | $4 | $20 | $0.20 | $5 / $8 |

Opus 5 and Opus 4.8 are also priced, because a fallback can run on them. Web
search costs $10 per 1,000 searches (`WEB_SEARCH_USD_PER_REQUEST`). Web fetch
has no per-request fee.

**How a call is costed.** Each call's `usage` is priced. When a fallback
served the request, every attempt in `usage.iterations` is priced at its own
model's rate. A model missing from the table is priced at the highest rates
in it.

**Estimate.** `ESTIMATED_USAGE` assumes about 60k input and 6k output tokens
plus 5 searches for the research call, and 4k input plus 1.5k output for the
extraction call. That comes to **≈ $0.46 per player, or ≈ $13.7 for 30
players**. Treat it as a prior: check it against the `cost` block of real
runs.

**Run cap.** The run cap is `RESEARCH_MAX_USD` (default $12), or `--max-usd`.
It is also limited to what is left of the monthly cap. A new player starts
only if `spent + in-flight reservations + projected ≤ cap`, where
`projected` is the mean actual cost so far, or the estimate before any
player has finished. Once the cap refuses a player, no further player
starts.

**Monthly cap.** The monthly cap is `RESEARCH_MONTHLY_USD` (default $90). It
is summed per UTC calendar month from `spend.jsonl`. Once it is reached, the
run refuses to start (exit 4). The Anthropic billing cycle may not match the
calendar month, so leave headroom under the $100 credit.

## How to disable

- **Stop running it.** Leave `ANTHROPIC_API_KEY` unset. Research then exits
  3 and nothing changes.
- **Drop its effect.** `make research-clear` deletes
  `role_overrides.research.json`, and the next derive runs on manual
  overrides only.
- **One player.** A manual entry in `role_overrides.json` for that player
  always outranks research.
