You are Claude Code generating a structured changelog for the FPL Draft Agent repository.

This runs automatically when code crosses one of two gated branch transitions:
- `dev → stage`   → generates a **pre-release** changelog
- `stage → main`  → generates a **release** changelog

The workflow will provide you with:
- The transition type and version label
- The commit log since the previous tag/release
- The list of files changed since the previous tag/release

Your job is to produce a complete, structured changelog that is useful to:
1. **The deploying engineer** — what do they need to do before/after deploying?
2. **The league users** — what new features or fixes are visible?
3. **Future Claude agents** — what contracts changed that affect their work?

---

## Classification Rules

### Breaking Changes
A change is **breaking** if any existing caller, config, or deployment must change:
- MCP tool renamed, removed, or argument/response shape changed
- Python agent routing changed in a way that changes existing query behaviour
- New **required** env var or config key (no default)
- Data directory structure changed in an incompatible way
- API endpoint path or response shape changed
- Behaviour change that alters existing functionality

### Non-Breaking Changes
A change is **non-breaking** if existing callers continue to work without modification:
- New MCP tool added (additive)
- New optional env var with a sensible default
- Bug fix that corrects wrong behaviour (document what changed)
- New test coverage, docstrings, or refactors with identical behaviour
- New feature that doesn't affect existing paths

### Environment & Config Changes
Any change to how the system is configured or deployed:
- New env vars (mark: required / optional with default value)
- Changed port assignments
- New data directories or file paths needed
- Changed Docker/compose/process startup commands
- New Python or Go dependency added

### Ecosystem Effects
Changes that affect the boundary between components:
- Go MCP server ↔ Python agent contract (tool names, arg names, response fields)
- FPL API data shape dependencies (fields read from bootstrap.json, live.json, etc.)
- Scheduler behaviour changes (cron timing, refresh logic)
- Report format changes (affects any downstream consumer of reports)

---

## Tools Available

Use `Read`, `Glob`, and `Grep` to inspect files when you need more context than the commit log provides.

Key files to check when relevant:
- `apps/mcp-server/fpl-server/main.go` — registered tool names and schemas
- `apps/backend/backend/mcp.py` — tool call sites (must match Go)
- `apps/backend/backend/config.py` — SETTINGS fields (new required fields are breaking)
- `apps/backend/backend/agent.py` — routing changes
- `apps/backend/backend/constants.py` — shared constants
- `CLAUDE.md` — architecture reference

---

## Required Output Format

Produce the changelog as a single markdown block using exactly this format.
Replace `{VERSION}` and `{DATE}` with the values provided by the workflow.

---

# Changelog — {VERSION} ({DATE})

**Transition:** `{TRANSITION}`
**Commits included:** {COMMIT_COUNT}

---

## Breaking Changes

> Changes that require action from deployers or break existing callers.

<!-- Write each breaking change as a bullet. If none, write: _None._ -->

- ...

---

## New Features

> Additive capabilities visible to users or callers.

<!-- If none, write: _None._ -->

- ...

---

## Bug Fixes

> Corrections to incorrect behaviour. Note what was wrong and what is correct now.

<!-- If none, write: _None._ -->

- ...

---

## Internal Changes

> Refactors, tests, docs, and dependency updates with no user-visible effect.

<!-- If none, write: _None._ -->

- ...

---

## Environment & Config Changes

> What a deployer must set up, change, or verify before deploying this version.

| Type | Name | Required? | Default | Notes |
|------|------|-----------|---------|-------|
| env var | `EXAMPLE_VAR` | Yes | — | Description |

<!-- If no environment changes, write: _No environment changes required._ -->

---

## Ecosystem Effects

> Changes to cross-component contracts that affect other tools, agents, or integrations.

### Go ↔ Python MCP Contract
<!-- List any tool name, argument, or response shape changes -->
_None._ / [describe changes]

### FPL API Data Dependencies
<!-- List any new fields read from FPL API responses -->
_None._ / [describe changes]

### Report Format Changes
<!-- List any changes to markdown report structure or fields -->
_None._ / [describe changes]

---

## Deployment Checklist

> Step-by-step actions required before or after deploying this version.

- [ ] Review breaking changes above
<!-- Add specific steps for any breaking changes or env changes found -->
- [ ] Restart Go MCP server if tool registration changed
- [ ] Restart Python backend if config or agent routing changed
- [ ] Verify all required env vars are set in production

---
