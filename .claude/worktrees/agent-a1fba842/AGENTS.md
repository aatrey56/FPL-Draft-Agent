# AGENTS

## Project Summary
- FPL Draft Agent: Go MCP server, Python backend, and a lightweight web UI.
- Local-only data lives in `data/` and generated output in `reports/` (both git-ignored).

## Repo Layout
- `apps/mcp-server/`: Go MCP tools + HTTP server.
- `apps/backend/`: Python API, scheduler, CLI, and agent logic.
- `apps/web/`: Web UI.
- `data/`, `reports/`: local only, do not commit.

## Setup Notes
- Go 1.23+ and Python 3.11+.
- Copy `.env.example` to `.env` for local runs (never commit secrets).
- `FPL_MCP_API_KEY` is required for MCP server + backend; `OPENAI_API_KEY` enables LLM features.

## Safeguards (for Codex)
- Never modify or commit `.env`, secrets, or anything under `data/` or `reports/`.
- Keep changes minimal and scoped to the issue; avoid sweeping refactors unless requested.
- Preserve Go MCP tool names/args and response shapes used by the Python backend.
- If requirements are unclear or repro steps are missing, ask for clarification before changing code.
- Update README or other docs if user-facing behavior or setup changes.

## Required Tests (before any PR)
- Run `scripts/preflight.sh`.
- If any step fails, fix the issue or report failure; do not open a PR with failing tests.
- If the change affects areas not covered by `preflight`, add or run relevant tests and list them in the PR summary.

## Common Commands (run from repo root)
- Preflight:
  `scripts/preflight.sh`
- Go checks:
  `cd apps/mcp-server && go vet ./...`
  `cd apps/mcp-server && go test ./...`
- Python checks:
  `python -m compileall apps/backend/backend`
- Backend dev server:
  `PYTHONPATH=apps/backend uvicorn backend.server:app --reload --port 8000`

## Issue Handling Guidelines
- Start with a brief plan and risk assessment.
- Reproduce or validate the issue using available steps; request missing details if blocked.
- Implement the smallest correct fix, add tests where practical, and avoid unrelated changes.
- Run required tests and summarize results in the final response.

## Review Guidelines (for Codex)
- Do not commit secrets, `.env`, or any files under `data/` or `reports/`.
- For Go MCP changes, keep tool names/args backward-compatible with the Python backend.
- For Python backend changes, ensure MCP tool calls and response shapes match server outputs.
- For UI changes, keep routes aligned with backend endpoints and update README if startup steps change.
- If changes are non-trivial, run the checks listed above or explain why they were skipped.
