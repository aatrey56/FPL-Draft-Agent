---
description: Plan -> execute -> review loop with a Fable architect and Opus reviewer
---
Goal: $ARGUMENTS

Follow this protocol strictly:

1. PRE-FLIGHT: `git status` must be clean — commit or stash first, never loop on a
   dirty tree. Read PLAN.md and STATE.md if they exist; read
   `docs/RESHAPE_PLAN.md` and `apps/backend/backend/ml/*_SPEC.md` for context.
2. PLAN: spawn the `architect` subagent with the goal + current state. It
   reconciles against the existing specs and writes/updates PLAN.md (numbered
   sections, each with a testable acceptance criterion). Show me the plan.
   STOP for my approval.
3. For each section in order:
   a. Branch `feat/<section-slug>`. Implement it yourself (the main loop).
   b. Run the tests. Fix until green.
   c. Spawn the `reviewer` subagent with the diff + the section's acceptance
      criterion. Apply required fixes; re-review if any finding was high severity.
   d. Spawn the `architect` for a drift check: completed vs remaining plan —
      anything invalidated? It updates PLAN.md. STOP only if scope materially
      changed.
   e. Commit; update STATE.md.
4. END: spawn the `architect` for a final review of the whole delta vs the
   original goal. Put remaining known issues in ISSUES.md. Summarize: what
   shipped, what changed from the plan, what is left.

Cost shape (by design): Sonnet runs every turn, Opus once per section, Fable only
at the boundaries (plan, drift checks, final review).
