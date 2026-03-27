# PR Context Engine

Local-first MCP server that tracks GitHub PRs and exposes high-signal developer context to Claude. Not an API wrapper — a stateful signal engine that adds temporal awareness and relevance filtering.

## Stack

- Python 3.12, async throughout
- FastMCP (official MCP Python SDK) with SSE transport
- SQLite via aiosqlite for local state
- GitHub GraphQL API (batched queries)
- Docker for deployment

## Setup

```bash
# Ensure gh has the required scopes
gh auth refresh -s read:org,repo

# Generate .env from gh token
echo "GITHUB_TOKEN=$(gh auth token)" > .env

# Build and start the server
docker compose build
docker compose up -d
```

The server runs on `http://localhost:8321` with SSE transport. Data is persisted in a Docker named volume (`pr-data`).

## Claude Code Configuration

Add the MCP server to Claude Code:

```bash
claude mcp add --transport sse --scope user pr-context http://localhost:8321/sse
```

Note: The MCP server will only be available in new Claude Code sessions. Already running sessions will not pick it up.

## Local Development

```bash
# Install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run MCP server locally (stdio transport)
python -m pr_context

# Run tests
pytest tests/
```

## Debug CLI

```bash
# Sync with GitHub and show new events
pr-context check

# List tracked PRs
pr-context list
pr-context list --state all

# Show unacknowledged events
pr-context events

# Delete local database
pr-context reset
```

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `GITHUB_TOKEN` | Yes | — | GitHub Personal Access Token |
| `DB_PATH` | No | `./data/pr_context.db` | SQLite database path |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `TRANSPORT` | No | `stdio` | Transport: `stdio` or `sse` |
| `PORT` | No | `8321` | Port for SSE transport |

Username is auto-detected from the GitHub token via `viewer { login }`.

## MCP Tools

- **`get_my_prs`** — List all relevant PRs with metadata
- **`get_pr_updates`** — Changes since last check, filtered and prioritized
- **`get_pr_details`** — Full PR details (description, comments, reviews, CI)
- **`get_my_action_items`** — PRs needing review, blocked/failing, items needing attention
- **`summarize_my_work_context`** — Full snapshot of your current work: authored PRs, reviews, action items, unread events
