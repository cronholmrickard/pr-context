# Plan: Local MCP-Powered PR Context Engine

## Context

Build a local-first developer tool that tracks GitHub PRs, detects meaningful changes over time, and exposes high-signal context via MCP tools for Claude. This is NOT an API wrapper — it's a stateful signal engine that adds temporal awareness and relevance filtering. Python + SQLite + Docker. No Linear integration.

---

## Project Structure

```
issue-tracker/
├── src/
│   └── pr_context/
│       ├── __init__.py
│       ├── server.py          # MCP server entry point (FastMCP + stdio)
│       ├── config.py          # pydantic-settings, env vars
│       ├── db.py              # SQLite via aiosqlite
│       ├── models.py          # Pydantic models for all tool return types
│       ├── github_client.py   # Async GraphQL client (httpx)
│       ├── queries.py         # GraphQL query strings
│       ├── change_detector.py # Stateful diff engine
│       └── cli.py             # Click-based debug CLI
├── tests/
│   ├── __init__.py
│   ├── test_change_detector.py
│   ├── test_db.py
│   └── test_github_client.py
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── .env.example
└── README.md
```

## Dependencies

- `mcp[cli]` — Official MCP Python SDK (FastMCP)
- `httpx` — Async HTTP for GitHub GraphQL
- `aiosqlite` — Async SQLite
- `pydantic-settings` — Config from env vars
- `click` — CLI
- `python-dotenv` — .env file loading

## Database Schema (SQLite)

```sql
CREATE TABLE pull_requests (
    id TEXT PRIMARY KEY,           -- "owner/repo#number"
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    title TEXT,
    state TEXT,                    -- open/closed/merged
    url TEXT,
    author TEXT,
    user_roles TEXT DEFAULT '[]',  -- JSON: ["author","reviewer","assignee"]
    ci_status TEXT,                -- success/failure/pending/null
    review_decision TEXT,          -- approved/changes_requested/review_required/null
    draft INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    snapshot_hash TEXT,            -- SHA-256 for quick change detection
    last_synced_at TEXT,
    UNIQUE(repo, number)
);

CREATE TABLE pr_snapshots (
    pr_id TEXT PRIMARY KEY REFERENCES pull_requests(id),
    comments_json TEXT DEFAULT '[]',
    reviews_json TEXT DEFAULT '[]',
    checks_json TEXT DEFAULT '[]',
    updated_at TEXT
);

CREATE TABLE pr_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_id TEXT REFERENCES pull_requests(id),
    event_type TEXT NOT NULL,      -- new_comment, review_requested, ci_failed, etc.
    actor TEXT,
    summary TEXT,
    priority INTEGER DEFAULT 1,   -- 0=low, 1=normal, 2=high, 3=urgent
    data_json TEXT,
    acknowledged INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

## MCP Tools

### 1. `get_my_prs`
- **Input:** `state` (optional, default "open")
- **Returns:** List of `PRSummary` (repo, number, title, state, user_roles, ci_status, review_decision, url, updated_at)
- **Logic:** Query `pull_requests` table filtered by state. If stale (>5min since last sync), trigger background sync first.

### 2. `get_pr_updates`
- **Input:** `since` (optional, "last_check" or ISO datetime)
- **Returns:** List of `PREvent` (type, pr_number, repo, actor, summary, priority)
- **Logic:** Read unacknowledged events from `pr_events`, mark them acknowledged, return sorted by priority desc.

### 3. `get_pr_details`
- **Input:** `repo`, `pr_number`
- **Returns:** `PRDetails` (description, comments, reviews, ci_checks, timeline)
- **Logic:** Fresh GraphQL fetch for single PR, update local state.

### 4. `get_my_action_items`
- **Input:** none
- **Returns:** List of `ActionItem` (action_type, pr, reason, priority)
- **Logic:** Computed from current state:
  - PRs where user is reviewer + review_decision is "review_required"
  - Authored PRs with failing CI
  - Authored PRs with "changes_requested"
  - PRs with unacknowledged high-priority events

### 5. `summarize_my_work_context` (Phase 4)
- **Input:** none
- **Returns:** Structured text summary of all PRs, updates, and priorities

## GitHub GraphQL Strategy

**Batched search query** — single request fetches 3 categories via aliases:

```graphql
query {
  authored: search(query: "is:pr author:USER is:open", type: ISSUE, first: 50) { ...PRFields }
  reviewing: search(query: "is:pr reviewed-by:USER is:open", type: ISSUE, first: 50) { ...PRFields }
  assigned: search(query: "is:pr assignee:USER is:open", type: ISSUE, first: 50) { ...PRFields }
}

fragment PRFields on SearchResultItemConnection {
  nodes {
    ... on PullRequest {
      id number title state url isDraft
      author { login }
      repository { nameWithOwner }
      updatedAt createdAt
      reviewDecision
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      reviewRequests(first: 10) { nodes { requestedReviewer { ... on User { login } } } }
    }
  }
}
```

**Detail query** — for `get_pr_details`, fetches comments, reviews, and full CI:

```graphql
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      body
      comments(first: 100) { nodes { author { login } body createdAt } }
      reviews(first: 50) { nodes { author { login } state body submittedAt } }
      commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 50) {
        nodes { ... on CheckRun { name conclusion status } }
      } } } } }
    }
  }
}
```

## Change Detection Algorithm

```
On sync:
  1. Fetch all PRs via batched GraphQL
  2. For each PR:
     a. Compute snapshot_hash = SHA-256(state + title + ci_status + review_decision + updated_at)
     b. Compare with stored snapshot_hash
     c. If unchanged -> skip
     d. If changed or new:
        - Fetch detail if needed
        - Compare comments/reviews/checks against stored snapshots
        - Generate events for each difference
        - Filter out events where actor == current user
        - Assign priority (3=urgent, 2=high, 1=normal, 0=low)
        - Store new snapshot
  3. Detect removed PRs (merged/closed since last sync)
  4. Update metadata.last_full_sync
```

**Priority rules:**
- **3 (urgent):** CI failure on authored PR, changes-requested on authored PR
- **2 (high):** Review requested from user, new review on authored PR
- **1 (normal):** New comments, PR status change
- **0 (low):** CI recovered to green, draft status change

## Docker Setup

**Dockerfile:**
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY src/ src/
ENV DB_PATH=/data/pr_context.db
VOLUME /data
ENTRYPOINT ["python", "-m", "pr_context.server"]
```

**docker-compose.yml:**
```yaml
services:
  pr-context:
    build: .
    env_file: .env
    volumes:
      - pr-data:/data
    stdin_open: true   # Required for stdio MCP transport
volumes:
  pr-data:
```

**Claude config** would point to:
```json
{
  "mcpServers": {
    "pr-context": {
      "command": "docker",
      "args": ["compose", "run", "--rm", "-i", "pr-context"]
    }
  }
}
```

## Configuration (.env)

```
GITHUB_TOKEN=ghp_...          # Required
DB_PATH=./data/pr_context.db  # Optional, default
LOG_LEVEL=INFO                 # Optional
```

Username is auto-detected via GitHub `viewer { login }` query.

## Implementation Phases

### Phase 1: Foundation
- `pyproject.toml` with dependencies
- `config.py` — settings from env
- `models.py` — all Pydantic models
- `db.py` — schema creation + CRUD
- `github_client.py` + `queries.py` — batched search + detail queries
- Basic tests

### Phase 2: Core MCP Server
- `server.py` — FastMCP with lifespan, register `get_my_prs` and `get_pr_details`
- Dockerfile + docker-compose.yml
- Test with Claude (stdio)

### Phase 3: Stateful Change Detection
- `change_detector.py` — snapshot hashing, diff engine, event generation
- `get_pr_updates` and `get_my_action_items` tools
- Priority filtering + signal logic

### Phase 4: Polish
- `summarize_my_work_context` tool
- `cli.py` — debug CLI (check, list, reset)
- Error handling, rate limiting, caching
- README with setup instructions

## Verification

1. **Unit tests:** `pytest tests/` — test change detector logic, DB operations, model serialization
2. **Local MCP test:** Run `python -m pr_context.server` and use MCP inspector or Claude CLI to call tools
3. **Docker test:** `docker compose build && docker compose run --rm -i pr-context` — verify stdio transport works
4. **End-to-end:** Configure in Claude Desktop/CLI, ask "What PRs need my attention?" and verify meaningful response
