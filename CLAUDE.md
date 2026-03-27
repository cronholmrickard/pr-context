# PR Context Engine

Local-first MCP server that tracks GitHub PRs and exposes high-signal developer context to Claude.

## Decisions

### Stack
- **Python 3.12** with async throughout
- **FastMCP** (official MCP Python SDK) with stdio transport
- **SQLite** via `aiosqlite` for local state persistence
- **httpx** for async GitHub GraphQL API calls
- **pydantic-settings** for configuration
- **click** for debug CLI
- Runs in **Docker** with docker-compose (single container + named volume for DB)

### Architecture
- Single Python package: `src/pr_context/`
- MCP server is the primary interface (stdio transport for Claude Desktop/CLI)
- GitHub GraphQL API only (no REST) — batches 3 search queries into one request using aliases
- No Linear integration
- Username auto-detected from GitHub token via `viewer { login }` query

### State & Change Detection
- SQLite stores PR state, snapshots, and an append-only event log
- Two-tier change detection: SHA-256 hash for quick skip, then deep diff on comments/reviews/CI
- Events have an `acknowledged` flag — `get_pr_updates` returns only unacknowledged events
- User's own actions are always filtered out

### Priority System
- **3 (urgent):** CI failure on authored PR, changes-requested on authored PR
- **2 (high):** Review requested from user, new review on authored PR
- **1 (normal):** New comments, PR status change
- **0 (low):** CI recovered to green, draft status change

### MCP Tools (MVP)
- `get_my_prs` — all relevant PRs with minimal metadata
- `get_pr_updates` — new changes since last check, filtered and prioritized
- `get_pr_details` — full PR details (description, comments, reviews, CI)
- `get_my_action_items` — PRs needing review, blocked/failing, items needing attention

### Configuration
- `GITHUB_TOKEN` env var (required) — GitHub Personal Access Token
- `DB_PATH` env var (optional) — defaults to `./data/pr_context.db`
- `LOG_LEVEL` env var (optional) — defaults to `INFO`
- `.env` file supported via python-dotenv

### Docker
- Single container, `python:3.12-slim` base
- `docker-compose.yml` with named volume `pr-data` for SQLite DB
- `stdin_open: true` for stdio MCP transport
- Invoke via: `docker compose run --rm -i pr-context`

## Commands

```bash
# Install dependencies (local dev)
pip install -e ".[dev]"

# Run MCP server directly
python -m pr_context.server

# Run tests
pytest tests/

# Docker
docker compose build
docker compose run --rm -i pr-context

# CLI (debug)
python -m pr_context.cli check
python -m pr_context.cli list
python -m pr_context.cli reset
```
