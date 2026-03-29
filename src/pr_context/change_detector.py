from __future__ import annotations

import asyncio
import hashlib
import logging

from pr_context.db import Database
from pr_context.github_client import GitHubClient
from pr_context.models import PRDetails, PRSummary

logger = logging.getLogger(__name__)


def compute_snapshot_hash(pr: PRSummary) -> str:
    blob = (
        f"{pr.state}|{pr.title}|{pr.ci_status}|{pr.review_decision}"
        f"|{pr.mergeable}|{pr.unresolved_thread_count}|{pr.updated_at.isoformat()}"
    )
    return hashlib.sha256(blob.encode()).hexdigest()


async def sync_and_detect(
    db: Database,
    github: GitHubClient,
    username: str,
) -> list[dict]:
    """Fetch PRs, detect changes, generate events. Returns new events."""
    prs = await github.fetch_my_prs()
    current_ids = set()
    new_events: list[dict] = []

    # Compute hashes and find which PRs need detail fetches
    changed_prs: list[PRSummary] = []
    unchanged_prs: list[PRSummary] = []
    hashes: dict[str, tuple[str, str | None]] = {}  # pr_id -> (new_hash, old_hash)

    for pr in prs:
        current_ids.add(pr.id)
        new_hash = compute_snapshot_hash(pr)
        old_hash = await db.get_pr_snapshot_hash(pr.id)
        hashes[pr.id] = (new_hash, old_hash)

        if old_hash is None or new_hash != old_hash:
            changed_prs.append(pr)
        else:
            unchanged_prs.append(pr)

    # Fetch details only for new/changed PRs (concurrently)
    async def _fetch_details(pr: PRSummary) -> PRDetails:
        owner, rest = pr.repo.split("/", 1)
        return await github.fetch_pr_details(owner, rest, pr.number)

    details_list = await asyncio.gather(*[_fetch_details(pr) for pr in changed_prs])
    details_map = {pr.id: details for pr, details in zip(changed_prs, details_list)}

    logger.info(
        "Sync: %d PRs total, %d changed, %d skipped",
        len(prs),
        len(changed_prs),
        len(unchanged_prs),
    )

    # Update all PRs in DB (summary data always updated)
    for pr in prs:
        new_hash, old_hash = hashes[pr.id]

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
            head_branch=pr.head_branch,
            base_branch=pr.base_branch,
            latest_commit_date=(
                pr.latest_commit_date.isoformat() if pr.latest_commit_date else None
            ),
            created_at=pr.updated_at.isoformat(),
            updated_at=pr.updated_at.isoformat(),
            snapshot_hash=new_hash,
        )

        details = details_map.get(pr.id)
        if details:
            await db.upsert_snapshot(
                details.id,
                comments=[c.model_dump(mode="json") for c in details.comments],
                reviews=[r.model_dump(mode="json") for r in details.reviews],
                checks=[c.model_dump(mode="json") for c in details.ci_checks],
            )

        if old_hash is None:
            # New PR, no events needed
            continue

        if new_hash == old_hash:
            # Nothing changed
            continue

        # Something changed — diff for events
        events = await _diff_pr(db, pr, details, username)
        new_events.extend(events)

    # Detect removed PRs (closed/merged since last sync)
    known_ids = await db.get_all_pr_ids()
    removed = known_ids - current_ids
    for pr_id in removed:
        pr_data = await db.get_pr(pr_id)
        if pr_data:
            event = _make_event(
                pr_id=pr_id,
                event_type="pr_closed",
                actor=None,
                summary=f"PR {pr_id} is no longer open",
                priority=1,
            )
            await db.add_event(**event)
            new_events.append(event)

    return new_events


async def _diff_pr(
    db: Database,
    pr: PRSummary,
    details: PRDetails,
    username: str,
) -> list[dict]:
    """Compare current PR details against stored snapshot, generate events."""
    events: list[dict] = []
    old_snapshot = await db.get_snapshot(pr.id)
    old_pr = await db.get_pr(pr.id)

    is_author = pr.author.lower() == username.lower()

    # CI status change
    if old_pr:
        old_ci = old_pr.get("ci_status")
        new_ci = pr.ci_status
        if old_ci != new_ci and new_ci is not None:
            if new_ci == "FAILURE" and is_author:
                events.append(
                    _make_event(
                        pr_id=pr.id,
                        event_type="ci_failed",
                        actor=None,
                        summary=f"CI failed on your PR: {pr.title}",
                        priority=3,
                    )
                )
            elif new_ci == "SUCCESS" and old_ci == "FAILURE":
                events.append(
                    _make_event(
                        pr_id=pr.id,
                        event_type="ci_recovered",
                        actor=None,
                        summary=f"CI recovered to green: {pr.title}",
                        priority=0,
                    )
                )

    # Review decision change
    if old_pr:
        old_decision = old_pr.get("review_decision")
        new_decision = pr.review_decision
        if old_decision != new_decision and new_decision is not None:
            if new_decision == "CHANGES_REQUESTED" and is_author:
                events.append(
                    _make_event(
                        pr_id=pr.id,
                        event_type="changes_requested",
                        actor=None,
                        summary=f"Changes requested on your PR: {pr.title}",
                        priority=3,
                    )
                )
            elif new_decision == "APPROVED" and is_author:
                events.append(
                    _make_event(
                        pr_id=pr.id,
                        event_type="pr_approved",
                        actor=None,
                        summary=f"Your PR was approved: {pr.title}",
                        priority=2,
                    )
                )

    # New commits pushed (relevant for reviewers)
    is_reviewer = "reviewer" in pr.user_roles
    if old_pr and is_reviewer and not is_author:
        old_commit_date = old_pr.get("latest_commit_date")
        new_commit_date = (
            pr.latest_commit_date.isoformat() if pr.latest_commit_date else None
        )
        if old_commit_date and new_commit_date and new_commit_date != old_commit_date:
            events.append(
                _make_event(
                    pr_id=pr.id,
                    event_type="new_commits_pushed",
                    actor=pr.author,
                    summary=f"New commits pushed to {pr.title} — may need re-review",
                    priority=2,
                )
            )

    # Draft status change
    if old_pr and bool(old_pr.get("draft")) != pr.draft:
        events.append(
            _make_event(
                pr_id=pr.id,
                event_type="draft_changed",
                actor=None,
                summary=f"PR {'marked as draft' if pr.draft else 'marked ready for review'}: {pr.title}",
                priority=0,
            )
        )

    if not old_snapshot:
        return _filter_own_events(events, username)

    # New comments
    old_comments = old_snapshot.get("comments", [])
    old_comment_set = {
        (c.get("author"), c.get("body"), c.get("created_at")) for c in old_comments
    }
    for comment in details.comments:
        key = (comment.author, comment.body, comment.created_at.isoformat())
        if key not in old_comment_set:
            events.append(
                _make_event(
                    pr_id=pr.id,
                    event_type="new_comment",
                    actor=comment.author,
                    summary=f"{comment.author} commented on {pr.title}: {_truncate(comment.body)}",
                    priority=1,
                )
            )

    # New reviews
    old_reviews = old_snapshot.get("reviews", [])
    old_review_set = {
        (r.get("author"), r.get("state"), r.get("submitted_at")) for r in old_reviews
    }
    for review in details.reviews:
        key = (review.author, review.state, review.submitted_at.isoformat())
        if key not in old_review_set:
            priority = 2 if is_author else 1
            events.append(
                _make_event(
                    pr_id=pr.id,
                    event_type="new_review",
                    actor=review.author,
                    summary=f"{review.author} reviewed {pr.title}: {review.state}",
                    priority=priority,
                )
            )

    return _filter_own_events(events, username)


def _filter_own_events(events: list[dict], username: str) -> list[dict]:
    """Filter out events caused by the user themselves."""
    return [
        e
        for e in events
        if e.get("actor") is None or e["actor"].lower() != username.lower()
    ]


def _make_event(
    *,
    pr_id: str,
    event_type: str,
    actor: str | None,
    summary: str,
    priority: int,
) -> dict:
    return {
        "pr_id": pr_id,
        "event_type": event_type,
        "actor": actor,
        "summary": summary,
        "priority": priority,
    }


def _truncate(text: str, length: int = 80) -> str:
    if len(text) <= length:
        return text
    return text[:length] + "..."
