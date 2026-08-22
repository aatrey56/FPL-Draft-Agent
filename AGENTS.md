# AGENTS

**`CLAUDE.md` is the canonical agent contract for this repository** — read it
first and follow it exactly (engineering standards, architecture, workflow,
and the non-negotiable rules). This file is only the quick orientation.

## Project Summary
- FPL Draft Co-Pilot: Go MCP server (14 tools) + Python ML pipeline (uv),
  used from Claude Desktop/Code. See `README.md`.
- Local-only data lives in `data/` and `reports/` (git-ignored — never commit).

## Setup
- Go 1.25+ and [uv](https://docs.astral.sh/uv/) (no system Python needed).
- Copy `.env.example` to `.env` (three variables; never commit secrets).
- All operations: `Makefile` at the repo root (`make preflight` before any PR).

## Hard Rules (abridged — full list in CLAUDE.md §15)
- Never commit `.env`, secrets, league/entry ids, or anything under `data/`.
- Never push to `main`; feature branch → PR → all checks green → reviewed merge.
- No live FPL API calls in tests; fixtures only.
- Keep changes minimal and scoped; update docs when behavior changes.
