# Contributing

Thanks for helping improve FPL Draft Agent.

## Development Setup
- Go code lives in `apps/mcp-server/` (server, fetcher, TUI).
- Python ML pipeline lives in `apps/backend/` (managed by [uv](https://docs.astral.sh/uv/)
  via `pyproject.toml` + `uv.lock` — `uv sync` once, `uv add <pkg>` for dependencies).
- `apps/web/` is the deprecated legacy UI (kept for history; not developed).

## Local Checks
One command runs everything CI runs:

```bash
make preflight   # go vet/test/gofmt + uv sync + ruff + pytest
```

Individually: `go test ./...` / `gofmt -w .` / `go mod tidy` from
`apps/mcp-server/`; `uv run ruff check .` / `uv run pytest` from `apps/backend/`.
Note: CI enforces `go mod tidy` producing no diff.

## Pull Requests
- Keep PRs focused and include a short summary.
- Update docs when behavior or interfaces change.
- Add tests when feasible.

## Release Process
- Update the MCP server version string in `apps/mcp-server/fpl-server/main.go`.
- Update `CHANGELOG.md` with release notes.
- Tag a release as `vX.Y.Z` and push the tag to GitHub.
- GitHub Actions will generate the release notes.
