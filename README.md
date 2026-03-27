# PR Context Engine

Local-first MCP server that tracks GitHub PRs and exposes high-signal developer context to Claude. Not an API wrapper — a stateful signal engine that adds temporal awareness and relevance filtering.

## Stack

- Python 3.12, async throughout
- FastMCP (official MCP Python SDK) with stdio transport
- SQLite via aiosqlite for local state
- GitHub GraphQL API (batched queries)
- Docker for deployment

## Setup

```bash
# Install dependencies
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env with your GitHub token
```

## Usage

```bash
# Run MCP server
python -m pr_context.server

# Run tests
pytest tests/

# Docker
docker compose build
docker compose run --rm -i pr-context
```

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `GITHUB_TOKEN` | Yes | — | GitHub Personal Access Token |
| `DB_PATH` | No | `./data/pr_context.db` | SQLite database path |
| `LOG_LEVEL` | No | `INFO` | Logging level |

Username is auto-detected from the GitHub token via `viewer { login }`.

## MCP Tools

- **`get_my_prs`** — List all relevant PRs with metadata
- **`get_pr_updates`** — Changes since last check, filtered and prioritized
- **`get_pr_details`** — Full PR details (description, comments, reviews, CI)
- **`get_my_action_items`** — PRs needing review, blocked/failing, items needing attention
