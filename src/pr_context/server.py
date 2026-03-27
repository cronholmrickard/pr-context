from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from pr_context.change_detector import compute_snapshot_hash, sync_and_detect
from pr_context.config import get_settings
from pr_context.db import Database
from pr_context.github_client import GitHubClient
from pr_context.models import PRDetails

logger = logging.getLogger(__name__)

# Module-level state populated by lifespan
db: Database | None = None
github: GitHubClient | None = None
username: str | None = None


@asynccontextmanager
async def lifespan(server: FastMCP):
    global db, github, username
    settings = get_settings()

    logging.basicConfig(level=settings.log_level)

    db = Database(settings.db_path)
    await db.connect()

    github = GitHubClient(settings.github_token)
    username = await github.get_viewer_login()
    logger.info("Authenticated as %s", username)

    try:
        yield
    finally:
        await github.close()
        await db.close()
        db = None
        github = None
        username = None


mcp = FastMCP("pr-context", lifespan=lifespan)


async def _sync_prs() -> None:
    """Fetch PRs from GitHub, detect changes, store events."""
    assert github is not None and db is not None and username is not None
    prs = await github.fetch_my_prs()

    for pr in prs:
        snapshot_hash = compute_snapshot_hash(pr)
        await db.upsert_pr(
            id=pr.id,
            repo=pr.repo,
            number=pr.number,
            title=pr.title,
            state=pr.state,
            url=pr.url,
            author=pr.author,
            user_roles=pr.user_roles,
            ci_status=pr.ci_status,
            review_decision=pr.review_decision,
            draft=pr.draft,
            created_at=pr.updated_at.isoformat(),
            updated_at=pr.updated_at.isoformat(),
            snapshot_hash=snapshot_hash,
        )

    now = datetime.now(timezone.utc).isoformat()
    await db.set_metadata("last_full_sync", now)
    logger.info("Synced %d PRs", len(prs))


async def _sync_with_detection() -> None:
    """Full sync with change detection and event generation."""
    assert github is not None and db is not None and username is not None
    events = await sync_and_detect(db, github, username)
    now = datetime.now(timezone.utc).isoformat()
    await db.set_metadata("last_full_sync", now)

    for event in events:
        await db.add_event(**event)

    logger.info("Sync complete: %d new events", len(events))


async def _should_sync() -> bool:
    """Return True if last sync was >5 minutes ago or never."""
    assert db is not None
    last_sync = await db.get_metadata("last_full_sync")
    if not last_sync:
        return True
    last = datetime.fromisoformat(last_sync)
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    return elapsed > 300


async def _ensure_synced() -> None:
    if await _should_sync():
        await _sync_with_detection()


@mcp.tool()
async def get_my_prs(state: str = "open") -> list[dict]:
    """Get all PRs relevant to you (authored, reviewing, or assigned).

    Args:
        state: Filter by PR state. Use "open" (default), "closed", "merged", or "all".
    """
    assert db is not None
    await _ensure_synced()

    state_filter = state.upper() if state != "all" else None
    rows = await db.get_all_prs(state=state_filter)

    results = []
    for row in rows:
        user_roles = row["user_roles"]
        if isinstance(user_roles, str):
            user_roles = json.loads(user_roles)
        results.append({
            "id": row["id"],
            "repo": row["repo"],
            "number": row["number"],
            "title": row["title"],
            "state": row["state"],
            "url": row["url"],
            "author": row["author"],
            "user_roles": user_roles,
            "ci_status": row["ci_status"],
            "review_decision": row["review_decision"],
            "draft": bool(row["draft"]),
            "updated_at": row["updated_at"],
        })

    return results


@mcp.tool()
async def get_pr_details(repo: str, pr_number: int) -> dict:
    """Get full details for a specific PR including description, comments, reviews, and CI checks.

    Args:
        repo: Repository in "owner/repo" format (e.g. "anthropics/claude-code").
        pr_number: The PR number.
    """
    assert github is not None and db is not None

    parts = repo.split("/")
    if len(parts) != 2:
        return {"error": f"Invalid repo format '{repo}', expected 'owner/repo'"}

    owner, repo_name = parts
    details: PRDetails = await github.fetch_pr_details(owner, repo_name, pr_number)

    # Store snapshot in DB
    await db.upsert_snapshot(
        details.id,
        comments=[c.model_dump(mode="json") for c in details.comments],
        reviews=[r.model_dump(mode="json") for r in details.reviews],
        checks=[c.model_dump(mode="json") for c in details.ci_checks],
    )

    return details.model_dump(mode="json")


@mcp.tool()
async def get_pr_updates(since: str | None = None) -> dict:
    """Get new changes since last check, filtered and prioritized.

    Returns unacknowledged events (new comments, reviews, CI changes, etc.)
    sorted by priority. Events caused by you are excluded. After returning,
    events are marked as acknowledged.

    Args:
        since: Not yet used. Reserved for future "since ISO datetime" filtering.
    """
    assert db is not None
    await _ensure_synced()

    events = await db.get_unacknowledged_events()
    count = await db.acknowledge_events()

    return {
        "events": [
            {
                "event_type": e["event_type"],
                "pr_id": e["pr_id"],
                "pr_number": e["pr_number"],
                "repo": e["repo"],
                "actor": e["actor"],
                "summary": e["summary"],
                "priority": e["priority"],
            }
            for e in events
        ],
        "total": len(events),
        "acknowledged": count,
    }


@mcp.tool()
async def get_my_action_items() -> list[dict]:
    """Get PRs that need your attention — reviews to do, CI failures, requested changes.

    Computed from current state:
    - PRs where you are a reviewer and review is required
    - Your PRs with failing CI
    - Your PRs with changes requested
    - PRs with unacknowledged high-priority events
    """
    assert db is not None and username is not None
    await _ensure_synced()

    rows = await db.get_all_prs(state="OPEN")
    unacked = await db.get_unacknowledged_events()

    items: list[dict] = []

    for row in rows:
        user_roles = row["user_roles"]
        if isinstance(user_roles, str):
            user_roles = json.loads(user_roles)

        is_author = "author" in user_roles
        is_reviewer = "reviewer" in user_roles

        # Authored PR with failing CI
        if is_author and row.get("ci_status") == "FAILURE":
            items.append({
                "action_type": "ci_failing",
                "pr_id": row["id"],
                "pr_number": row["number"],
                "repo": row["repo"],
                "title": row["title"],
                "reason": "CI is failing on your PR",
                "priority": 3,
            })

        # Authored PR with changes requested
        if is_author and row.get("review_decision") == "CHANGES_REQUESTED":
            items.append({
                "action_type": "changes_requested",
                "pr_id": row["id"],
                "pr_number": row["number"],
                "repo": row["repo"],
                "title": row["title"],
                "reason": "Changes were requested on your PR",
                "priority": 3,
            })

        # Reviewer with pending review
        if is_reviewer and row.get("review_decision") == "REVIEW_REQUIRED":
            items.append({
                "action_type": "needs_review",
                "pr_id": row["id"],
                "pr_number": row["number"],
                "repo": row["repo"],
                "title": row["title"],
                "reason": "Your review is requested",
                "priority": 2,
            })

    # Add high-priority unacknowledged events
    for e in unacked:
        if e["priority"] >= 2:
            items.append({
                "action_type": "unread_event",
                "pr_id": e["pr_id"],
                "pr_number": e["pr_number"],
                "repo": e["repo"],
                "title": e["summary"],
                "reason": e["summary"],
                "priority": e["priority"],
            })

    # Sort by priority descending
    items.sort(key=lambda x: x["priority"], reverse=True)
    return items
