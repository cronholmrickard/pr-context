# PR Context Engine

Local-first MCP server that tracks GitHub PRs and exposes high-signal developer context to Claude.

## Decisions

### Stack
- **Python 3.12** with async throughout
- **FastMCP** (official MCP Python SDK) with SSE transport (stdio for local dev)
- **SQLite** via `aiosqlite` for local state persistence
- **httpx** for async GitHub GraphQL API calls
- **pydantic-settings** for configuration
- **click** for debug CLI
- **black** for code formatting (pre-commit hook)
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
- Draft PRs always have priority 0 regardless of other signals
- As author: **3** CI failed / changes requested, **2** new review / approved but blocked, **1** new comments / CI passed, **0** CI recovered / draft
- As reviewer: **2** review requested / re-review requested / new comments by others, **0** already approved / CI change / your comment is last / draft
- "Who commented last" determines reviewer action items — if your comment/review is most recent, no action item is generated

### MCP Tools
- `get_my_prs` — authored/assigned PRs with CI, review state, merge status, branches, last comment
- `get_my_reviews` — PRs to review (from others), with new-commits detection and reviewer context
- `get_pr_details` — full PR details (description, comments, reviews, CI, branches)
- `get_pr_threads` — review threads with file paths and resolution status
- `get_pr_comments` — top-level comments and review bodies
- `get_pr_ci` — individual CI check details with URLs and timing
- `get_pr_updates` — updates on authored/assigned PRs (reviews, CI, comments)
- `get_review_updates` — updates on PRs you're reviewing (new commits, CI, comments)
- `get_my_action_items` — actionable items separated by as_author/as_reviewer
- `summarize_my_work_context` — full work context snapshot

### Configuration
- `GITHUB_TOKEN` env var (required) — GitHub Personal Access Token
- `DB_PATH` env var (optional) — defaults to `./data/pr_context.db`
- `LOG_LEVEL` env var (optional) — defaults to `INFO`
- `.env` file supported via python-dotenv

### Docker
- Single container, `python:3.12-slim` base
- `docker-compose.yml` with named volume `pr-data` for SQLite DB
- SSE transport on port 8321, restart unless-stopped
- Invoke via: `docker compose up -d`

## Commands

```bash
# Install dependencies (local dev)
pip install -e ".[dev]"

# Run MCP server directly
python -m pr_context

# Format code
black src/ tests/

# Run tests
pytest tests/

# Docker
docker compose build
docker compose up -d

# CLI (debug)
python -m pr_context.cli check
python -m pr_context.cli list
python -m pr_context.cli reset
```
