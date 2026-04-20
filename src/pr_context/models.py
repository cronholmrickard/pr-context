from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Comment(BaseModel):
    id: str | None = None
    author: str
    body: str
    created_at: datetime


class Review(BaseModel):
    id: str | None = None
    author: str
    state: str  # APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED, PENDING
    body: str
    submitted_at: datetime | None = None


class ReviewThread(BaseModel):
    is_resolved: bool
    is_outdated: bool
    path: str | None
    line: int | None
    comments: list[Comment]


class CICheck(BaseModel):
    name: str
    status: str  # COMPLETED, IN_PROGRESS, QUEUED, etc.
    conclusion: str | None  # SUCCESS, FAILURE, NEUTRAL, etc.
    url: str | None  # Link to CI job details
    started_at: str | None
    completed_at: str | None


class PRSummary(BaseModel):
    id: str  # "owner/repo#number"
    repo: str
    number: int
    title: str
    state: str  # OPEN, CLOSED, MERGED
    url: str
    author: str
    user_roles: list[str]  # ["author", "reviewer", "assignee"]
    ci_status: str | None
    review_decision: str | None
    mergeable: str | None  # MERGEABLE, CONFLICTING, UNKNOWN
    merge_state_status: str | None = (
        None  # BEHIND, BLOCKED, CLEAN, DIRTY, DRAFT, HAS_HOOKS, UNKNOWN, UNSTABLE
    )
    unresolved_thread_count: int
    pending_reviewers: list[str] = []
    draft: bool
    head_branch: str | None = None
    base_branch: str | None = None
    latest_commit_date: datetime | None = None
    updated_at: datetime


class PRDetails(BaseModel):
    id: str
    repo: str
    number: int
    title: str
    state: str
    url: str
    author: str
    body: str
    comments: list[Comment]
    reviews: list[Review]
    review_threads: list[ReviewThread]
    ci_checks: list[CICheck]
    review_decision: str | None
    mergeable: str | None  # MERGEABLE, CONFLICTING, UNKNOWN
    merge_state_status: str | None = (
        None  # BEHIND, BLOCKED, CLEAN, DIRTY, DRAFT, HAS_HOOKS, UNKNOWN, UNSTABLE
    )
    unresolved_thread_count: int
    draft: bool
    head_branch: str | None = None
    base_branch: str | None = None
    created_at: datetime
    updated_at: datetime


class PREvent(BaseModel):
    event_type: str
    pr_id: str
    pr_number: int
    repo: str
    actor: str | None
    summary: str
    priority: int  # 0=low, 1=normal, 2=high, 3=urgent


class ActionItem(BaseModel):
    action_type: str  # needs_review, ci_failing, changes_requested
    pr_id: str
    pr_number: int
    repo: str
    title: str
    reason: str
    priority: int
