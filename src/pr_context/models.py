from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Comment(BaseModel):
    author: str
    body: str
    created_at: datetime


class Review(BaseModel):
    author: str
    state: str  # APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED, PENDING
    body: str
    submitted_at: datetime


class CICheck(BaseModel):
    name: str
    status: str  # COMPLETED, IN_PROGRESS, QUEUED, etc.
    conclusion: str | None  # SUCCESS, FAILURE, NEUTRAL, etc.


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
    draft: bool
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
    ci_checks: list[CICheck]
    review_decision: str | None
    draft: bool
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
