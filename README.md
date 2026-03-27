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
# Run MCP server (stdio transport)
python -m pr_context.server

# Docker
docker compose build
docker compose run --rm -i pr-context

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

## Claude Desktop / CLI Configuration

```json
{
  "mcpServers": {
    "pr-context": {
      "command": "docker",
      "args": ["compose", "-f", "/path/to/pr-context/docker-compose.yml", "run", "--rm", "-i", "pr-context"]
    }
  }
}
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
- **`summarize_my_work_context`** — Full snapshot of your current work: authored PRs, reviews, action items, unread events
