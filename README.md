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

### Your PRs
- **`get_my_prs`** — PRs you authored or are assigned to, with CI status, review state, merge status, branch staleness, unresolved threads, and last comment preview. Use `role="all"` to include reviewer-only PRs.
- **`get_my_reviews`** — PRs where you are a reviewer (excludes your own). Shows new commits since your last review, other reviewers' states, and how long the PR has been waiting.

### PR Details
- **`get_pr_details`** — Full PR info: description, comments, reviews, CI checks, branch names, merge state.
- **`get_pr_threads`** — Review threads with file paths, comments, and resolution status.
- **`get_pr_comments`** — Top-level comments and review bodies.
- **`get_pr_ci`** — Individual CI check names, statuses, conclusions, URLs, and timing.

### Updates & Actions
- **`get_pr_updates`** — New changes since last check (comments, reviews, CI), filtered and prioritized. Includes `last_synced_at` timestamp.
- **`get_my_action_items`** — Actionable items separated by role (`as_author` / `as_reviewer`): CI failures, changes requested, pending reviews, merge conflicts, behind branches, unresolved threads.
- **`summarize_my_work_context`** — Full snapshot: authored PRs, reviewing PRs, action items, unread events.

### Key Features
- PRs are referenced by index number (e.g. `#1`, `#5`) across all tools
- Smart review state: distinguishes `CHANGES_REQUESTED` from `RE_REVIEW_REQUESTED` (author re-requested review)
- Branch staleness via `merge_state_status`: BEHIND, CLEAN, DIRTY, BLOCKED, etc.
- Full comment/review/CI snapshots stored locally — last comment shown on PR summaries
- Auto-sync with 5-minute cooldown, all data cached in SQLite
