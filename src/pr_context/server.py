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

# Index mapping: index number -> pr_id (rebuilt on each get_my_prs call)
_pr_index: dict[int, str] = {}


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
            mergeable=pr.mergeable,
            merge_state_status=pr.merge_state_status,
            unresolved_thread_count=pr.unresolved_thread_count,
            pending_reviewers=pr.pending_reviewers,
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


async def _resolve_pr_ref(pr_ref: str) -> tuple[str, str, int] | None:
    """Resolve a PR reference to (pr_id, owner/repo, number).

    Accepts:
    - Index number from get_my_prs (e.g. "5" or "#5")
    - Full PR ID (e.g. "Link-Labs/l2s-frontend#1315")
    - Repo + number (e.g. "l2s-frontend#1315" — matches partial repo name)
    """
    assert db is not None
    ref = pr_ref.strip().lstrip("#")

    # Try as index number
    if ref.isdigit():
        idx = int(ref)
        pr_id = _pr_index.get(idx)
        if pr_id:
            parts = pr_id.split("#")
            return pr_id, parts[0], int(parts[1])

    # Try as full PR ID (owner/repo#number)
    if "#" in pr_ref:
        repo_part, num_part = pr_ref.rsplit("#", 1)
        if num_part.isdigit():
            number = int(num_part)
            # Try exact match first
            pr = await db.get_pr(pr_ref)
            if pr:
                return pr_ref, repo_part, number
            # Try partial repo name match
            all_ids = await db.get_all_pr_ids()
            for pid in all_ids:
                if pid.endswith(f"{repo_part}#{num_part}") or repo_part in pid:
                    parts = pid.split("#")
                    return pid, parts[0], int(parts[1])

    return None


@mcp.tool()
async def get_my_prs(state: str = "open") -> list[dict]:
    """Get all PRs relevant to you (authored, reviewing, or assigned).

    Each PR includes a short index number (e.g. #1, #2) that you can use
    to reference it in other tools like get_pr_details or get_pr_threads.

    Args:
        state: Filter by PR state. Use "open" (default), "closed", "merged", or "all".
    """
    global _pr_index
    assert db is not None
    await _ensure_synced()

    state_filter = state.upper() if state != "all" else None
    rows = await db.get_all_prs(state=state_filter)

    # Rebuild index
    _pr_index = {}
    results = []
    for i, row in enumerate(rows, 1):
        _pr_index[i] = row["id"]
        user_roles = row["user_roles"]
        if isinstance(user_roles, str):
            user_roles = json.loads(user_roles)
        pending_reviewers = row.get("pending_reviewers", "[]")
        if isinstance(pending_reviewers, str):
            pending_reviewers = json.loads(pending_reviewers)
        review_decision = row["review_decision"]
        results.append({
            "index": i,
            "id": row["id"],
            "repo": row["repo"],
            "number": row["number"],
            "title": row["title"],
            "state": row["state"],
            "url": row["url"],
            "author": row["author"],
            "user_roles": user_roles,
            "ci_status": row["ci_status"],
            "review_decision": review_decision,
            "effective_review_state": _effective_review_state(review_decision, pending_reviewers),
            "pending_reviewers": pending_reviewers,
            "mergeable": row.get("mergeable"),
            "merge_state_status": row.get("merge_state_status"),
            "unresolved_threads": row.get("unresolved_thread_count", 0),
            "draft": bool(row["draft"]),
            "updated_at": row["updated_at"],
            "last_comment": await _get_last_comment(row["id"]),
        })

    # Store index in metadata for persistence
    await db.set_metadata("pr_index", json.dumps({str(k): v for k, v in _pr_index.items()}))

    return results


@mcp.tool()
async def get_pr_details(pr_ref: str) -> dict:
    """Get full details for a specific PR including description, comments, reviews, and CI checks.

    Args:
        pr_ref: PR reference — use the index number from get_my_prs (e.g. "5"),
                or full ID like "owner/repo#123".
    """
    assert github is not None and db is not None
    await _load_index()

    resolved = await _resolve_pr_ref(pr_ref)
    if not resolved:
        return {"error": f"Could not resolve PR reference '{pr_ref}'. Run get_my_prs first to see available PRs."}

    pr_id, repo, number = resolved
    parts = repo.split("/")
    if len(parts) != 2:
        return {"error": f"Invalid repo format '{repo}'"}

    owner, repo_name = parts
    details: PRDetails = await github.fetch_pr_details(owner, repo_name, number)

    # Store snapshot in DB
    await db.upsert_snapshot(
        details.id,
        comments=[c.model_dump(mode="json") for c in details.comments],
        reviews=[r.model_dump(mode="json") for r in details.reviews],
        checks=[c.model_dump(mode="json") for c in details.ci_checks],
    )

    return details.model_dump(mode="json")


@mcp.tool()
async def get_pr_threads(pr_ref: str, show_resolved: bool = False) -> dict:
    """Get review threads for a PR, showing file path, comments, and resolution status.

    Args:
        pr_ref: PR reference — use the index number from get_my_prs (e.g. "5"),
                or full ID like "owner/repo#123".
        show_resolved: If True, include resolved threads. Default shows only unresolved.
    """
    assert github is not None and db is not None
    await _load_index()

    resolved = await _resolve_pr_ref(pr_ref)
    if not resolved:
        return {"error": f"Could not resolve PR reference '{pr_ref}'. Run get_my_prs first to see available PRs."}

    pr_id, repo, number = resolved
    parts = repo.split("/")
    if len(parts) != 2:
        return {"error": f"Invalid repo format '{repo}'"}

    owner, repo_name = parts
    details: PRDetails = await github.fetch_pr_details(owner, repo_name, number)

    threads = []
    for t in details.review_threads:
        if not show_resolved and t.is_resolved:
            continue
        threads.append({
            "is_resolved": t.is_resolved,
            "is_outdated": t.is_outdated,
            "path": t.path,
            "line": t.line,
            "comments": [
                {
                    "author": c.author,
                    "body": c.body,
                    "created_at": c.created_at.isoformat(),
                }
                for c in t.comments
            ],
        })

    total = len(details.review_threads)
    unresolved = sum(1 for t in details.review_threads if not t.is_resolved)

    return {
        "pr_id": pr_id,
        "url": details.url,
        "title": details.title,
        "threads": threads,
        "total_threads": total,
        "unresolved_count": unresolved,
        "resolved_count": total - unresolved,
    }


@mcp.tool()
async def get_pr_comments(pr_ref: str) -> dict:
    """Get all comments on a PR (top-level comments, not inline review threads).

    Args:
        pr_ref: PR reference — use the index number from get_my_prs (e.g. "5"),
                or full ID like "owner/repo#123".
    """
    assert github is not None and db is not None
    await _load_index()

    resolved = await _resolve_pr_ref(pr_ref)
    if not resolved:
        return {"error": f"Could not resolve PR reference '{pr_ref}'. Run get_my_prs first to see available PRs."}

    pr_id, repo, number = resolved
    parts = repo.split("/")
    if len(parts) != 2:
        return {"error": f"Invalid repo format '{repo}'"}

    owner, repo_name = parts
    details: PRDetails = await github.fetch_pr_details(owner, repo_name, number)

    return {
        "pr_id": pr_id,
        "url": details.url,
        "title": details.title,
        "comments": [
            {
                "author": c.author,
                "body": c.body,
                "created_at": c.created_at.isoformat(),
            }
            for c in details.comments
        ],
        "reviews": [
            {
                "author": r.author,
                "state": r.state,
                "body": r.body,
                "submitted_at": r.submitted_at.isoformat(),
            }
            for r in details.reviews
            if r.body  # skip empty review bodies
        ],
        "total_comments": len(details.comments),
        "total_reviews": len(details.reviews),
    }


@mcp.tool()
async def get_pr_ci(pr_ref: str) -> dict:
    """Get detailed CI/check status for a PR — individual job names, statuses, conclusions, and links.

    Args:
        pr_ref: PR reference — use the index number from get_my_prs (e.g. "5"),
                or full ID like "owner/repo#123".
    """
    assert github is not None and db is not None
    await _load_index()

    resolved = await _resolve_pr_ref(pr_ref)
    if not resolved:
        return {"error": f"Could not resolve PR reference '{pr_ref}'. Run get_my_prs first to see available PRs."}

    pr_id, repo, number = resolved
    parts = repo.split("/")
    if len(parts) != 2:
        return {"error": f"Invalid repo format '{repo}'"}

    owner, repo_name = parts
    details: PRDetails = await github.fetch_pr_details(owner, repo_name, number)

    checks = []
    for c in details.ci_checks:
        check = {
            "name": c.name,
            "status": c.status,
            "conclusion": c.conclusion,
        }
        if c.url:
            check["url"] = c.url
        if c.started_at:
            check["started_at"] = c.started_at
        if c.completed_at:
            check["completed_at"] = c.completed_at
        checks.append(check)

    # Summarize
    total = len(checks)
    passed = sum(1 for c in checks if c.get("conclusion") in ("SUCCESS", "NEUTRAL", "SKIPPED"))
    failed = sum(1 for c in checks if c.get("conclusion") == "FAILURE")
    pending = sum(1 for c in checks if c.get("status") in ("IN_PROGRESS", "QUEUED", "PENDING"))

    return {
        "pr_id": pr_id,
        "url": details.url,
        "title": details.title,
        "overall_status": details.ci_checks[0].status if details.ci_checks else None,
        "checks": checks,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "pending": pending,
        },
    }


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
    - PRs with merge conflicts
    - PRs with unresolved review threads
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
        pending_reviewers = row.get("pending_reviewers", "[]")
        if isinstance(pending_reviewers, str):
            pending_reviewers = json.loads(pending_reviewers)
        effective_state = _effective_review_state(row.get("review_decision"), pending_reviewers)

        # Authored PR with failing CI
        if is_author and row.get("ci_status") == "FAILURE":
            items.append({
                "action_type": "ci_failing",
                "pr_id": row["id"],
                "pr_number": row["number"],
                "repo": row["repo"],
                "title": row["title"],
                "url": row["url"],
                "reason": "CI is failing on your PR",
                "priority": 3,
            })

        # Authored PR with changes requested (but NOT if re-review was requested)
        if is_author and effective_state == "CHANGES_REQUESTED":
            items.append({
                "action_type": "changes_requested",
                "pr_id": row["id"],
                "pr_number": row["number"],
                "repo": row["repo"],
                "title": row["title"],
                "url": row["url"],
                "reason": "Changes were requested on your PR",
                "priority": 3,
            })

        # Authored PR where re-review was requested (lower priority — waiting on reviewer)
        if is_author and effective_state == "RE_REVIEW_REQUESTED":
            items.append({
                "action_type": "re_review_requested",
                "pr_id": row["id"],
                "pr_number": row["number"],
                "repo": row["repo"],
                "title": row["title"],
                "url": row["url"],
                "reason": f"Waiting for re-review from {', '.join(pending_reviewers)}",
                "priority": 1,
            })

        # Reviewer with pending review (initial or re-review)
        if is_reviewer and username and username.lower() in [r.lower() for r in pending_reviewers]:
            reason = "Re-review requested" if effective_state == "RE_REVIEW_REQUESTED" else "Your review is requested"
            items.append({
                "action_type": "needs_review",
                "pr_id": row["id"],
                "pr_number": row["number"],
                "repo": row["repo"],
                "title": row["title"],
                "url": row["url"],
                "reason": reason,
                "priority": 2,
            })
        elif is_reviewer and row.get("review_decision") == "REVIEW_REQUIRED":
            items.append({
                "action_type": "needs_review",
                "pr_id": row["id"],
                "pr_number": row["number"],
                "repo": row["repo"],
                "title": row["title"],
                "url": row["url"],
                "reason": "Your review is requested",
                "priority": 2,
            })

        # Merge conflicts
        if is_author and row.get("mergeable") == "CONFLICTING":
            items.append({
                "action_type": "merge_conflict",
                "pr_id": row["id"],
                "pr_number": row["number"],
                "repo": row["repo"],
                "title": row["title"],
                "url": row["url"],
                "reason": "PR has merge conflicts",
                "priority": 2,
            })

        # Branch behind base
        if is_author and row.get("merge_state_status") == "BEHIND":
            items.append({
                "action_type": "branch_behind",
                "pr_id": row["id"],
                "pr_number": row["number"],
                "repo": row["repo"],
                "title": row["title"],
                "url": row["url"],
                "reason": "Branch is behind base — needs update",
                "priority": 1,
            })

        # Unresolved review threads
        unresolved = row.get("unresolved_thread_count", 0)
        if is_author and unresolved > 0:
            items.append({
                "action_type": "unresolved_threads",
                "pr_id": row["id"],
                "pr_number": row["number"],
                "repo": row["repo"],
                "title": row["title"],
                "url": row["url"],
                "reason": f"{unresolved} unresolved review thread{'s' if unresolved != 1 else ''}",
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


@mcp.tool()
async def summarize_my_work_context() -> dict:
    """Get a structured summary of all your PRs, updates, and priorities.

    Returns a complete picture of your current work context: authored PRs,
    PRs you're reviewing, pending action items, and recent unacknowledged events.
    Useful for starting your day or context-switching back to PR work.
    """
    assert db is not None and username is not None
    await _ensure_synced()

    rows = await db.get_all_prs(state="OPEN")
    unacked = await db.get_unacknowledged_events()

    authored = []
    reviewing = []
    other = []

    for row in rows:
        user_roles = row["user_roles"]
        if isinstance(user_roles, str):
            user_roles = json.loads(user_roles)

        entry = {
            "id": row["id"],
            "number": row["number"],
            "repo": row["repo"],
            "title": row["title"],
            "url": row["url"],
            "ci_status": row["ci_status"],
            "review_decision": row["review_decision"],
            "mergeable": row.get("mergeable"),
            "merge_state_status": row.get("merge_state_status"),
            "unresolved_threads": row.get("unresolved_thread_count", 0),
            "draft": bool(row["draft"]),
            "updated_at": row["updated_at"],
        }

        if "author" in user_roles:
            authored.append(entry)
        elif "reviewer" in user_roles:
            reviewing.append(entry)
        else:
            other.append(entry)

    action_items = await get_my_action_items()

    return {
        "user": username,
        "authored_prs": authored,
        "reviewing_prs": reviewing,
        "other_prs": other,
        "action_items": action_items,
        "unread_events": [
            {
                "event_type": e["event_type"],
                "pr_id": e["pr_id"],
                "summary": e["summary"],
                "priority": e["priority"],
            }
            for e in unacked
        ],
        "counts": {
            "authored": len(authored),
            "reviewing": len(reviewing),
            "action_items": len(action_items),
            "unread_events": len(unacked),
        },
    }


def _effective_review_state(review_decision: str | None, pending_reviewers: list[str]) -> str:
    """Compute a more accurate review state from GitHub's reviewDecision + pending requests.

    - CHANGES_REQUESTED with pending reviewers = re-review requested (author addressed feedback)
    - CHANGES_REQUESTED with no pending reviewers = truly changes requested
    - REVIEW_REQUIRED = waiting for initial review
    - APPROVED = approved
    """
    if review_decision == "CHANGES_REQUESTED" and pending_reviewers:
        return "RE_REVIEW_REQUESTED"
    return review_decision or "NONE"


async def _get_last_comment(pr_id: str) -> dict | None:
    """Derive last comment from stored snapshot data."""
    assert db is not None
    snapshot = await db.get_snapshot(pr_id)
    if not snapshot:
        return None
    comments = snapshot.get("comments", [])
    if not comments:
        return None
    c = comments[-1]
    body = c.get("body", "")
    preview = body[:100] + "..." if len(body) > 100 else body
    preview = preview.replace("\n", " ").strip()
    return {
        "author": c.get("author"),
        "at": c.get("created_at"),
        "preview": preview,
    }


async def _load_index() -> None:
    """Load PR index from DB if not already in memory."""
    global _pr_index
    if _pr_index:
        return
    assert db is not None
    raw = await db.get_metadata("pr_index")
    if raw:
        data = json.loads(raw)
        _pr_index = {int(k): v for k, v in data.items()}
