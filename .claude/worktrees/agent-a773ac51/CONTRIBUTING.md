# Contributing

Thanks for helping improve FPL Draft Agent.

## Development Setup
- Go code lives in `apps/mcp-server/`.
- Python backend lives in `apps/backend/`.
- Web UI lives in `apps/web/`.

## Local Checks
- Go vet: `go vet ./...` from `apps/mcp-server/`.
- Go test: `go test ./...` from `apps/mcp-server/`.
- Go format: `gofmt -w .` from `apps/mcp-server/`.
- Go mod tidy: `go mod tidy` from `apps/mcp-server/` (ensure no diffs in go.mod/go.sum).
- Python compile: `python -m compileall apps/backend/backend` from repo root.
- Python tooling: `pip install ruff pytest` (in your active venv).
- Python lint: `ruff check` from repo root.
- Python tests: `PYTHONPATH=apps/backend pytest` from repo root.

## Pull Requests
- Keep PRs focused and include a short summary.
- Update docs when behavior or interfaces change.
- Add tests when feasible.

## Release Process
- Update the MCP server version string in `apps/mcp-server/fpl-server/main.go`.
- Update `CHANGELOG.md` with release notes.
- Tag a release as `vX.Y.Z` and push the tag to GitHub.
- GitHub Actions will generate the release notes.
