from __future__ import annotations

import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from pr_context.config import get_settings
from pr_context.db import Database
from pr_context.github_client import GitHubClient
from pr_context.models import PRDetails, PRSummary

logger = logging.getLogger(__name__)

# Module-level state populated by lifespan
db: Database | None = None
github: GitHubClient | None = None


@asynccontextmanager
async def lifespan(server: FastMCP):
    global db, github
    settings = get_settings()

    logging.basicConfig(level=settings.log_level)

    db = Database(settings.db_path)
    await db.connect()

    github = GitHubClient(settings.github_token)
    login = await github.get_viewer_login()
    logger.info("Authenticated as %s", login)

    try:
        yield
    finally:
        await github.close()
        await db.close()
        db = None
        github = None


mcp = FastMCP("pr-context", lifespan=lifespan)


def _compute_snapshot_hash(pr: PRSummary) -> str:
    blob = f"{pr.state}|{pr.title}|{pr.ci_status}|{pr.review_decision}|{pr.updated_at.isoformat()}"
    return hashlib.sha256(blob.encode()).hexdigest()


async def _sync_prs() -> list[PRSummary]:
    """Fetch PRs from GitHub and sync to local DB."""
    assert github is not None and db is not None
    prs = await github.fetch_my_prs()

    for pr in prs:
        snapshot_hash = _compute_snapshot_hash(pr)
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
    return prs


async def _should_sync() -> bool:
    """Return True if last sync was >5 minutes ago or never."""
    assert db is not None
    last_sync = await db.get_metadata("last_full_sync")
    if not last_sync:
        return True
    last = datetime.fromisoformat(last_sync)
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    return elapsed > 300


@mcp.tool()
async def get_my_prs(state: str = "open") -> list[dict]:
    """Get all PRs relevant to you (authored, reviewing, or assigned).

    Args:
        state: Filter by PR state. Use "open" (default), "closed", "merged", or "all".
    """
    assert db is not None

    if await _should_sync():
        await _sync_prs()

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
