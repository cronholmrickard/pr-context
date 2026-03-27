from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_context.change_detector import (
    _diff_pr,
    _filter_own_events,
    _make_event,
    _truncate,
    compute_snapshot_hash,
    sync_and_detect,
)
from pr_context.db import Database
from pr_context.models import CICheck, Comment, PRDetails, PRSummary, Review


def _make_pr_summary(**overrides) -> PRSummary:
    defaults = {
        "id": "owner/repo#1",
        "repo": "owner/repo",
        "number": 1,
        "title": "Test PR",
        "state": "OPEN",
        "url": "https://github.com/owner/repo/pull/1",
        "author": "alice",
        "user_roles": ["author"],
        "ci_status": "SUCCESS",
        "review_decision": None,
        "mergeable": "MERGEABLE",
        "unresolved_thread_count": 0,
        "draft": False,
        "updated_at": datetime(2026, 3, 27, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return PRSummary(**defaults)


def _make_pr_details(**overrides) -> PRDetails:
    defaults = {
        "id": "owner/repo#1",
        "repo": "owner/repo",
        "number": 1,
        "title": "Test PR",
        "state": "OPEN",
        "url": "https://github.com/owner/repo/pull/1",
        "author": "alice",
        "body": "Test body",
        "comments": [],
        "reviews": [],
        "review_threads": [],
        "ci_checks": [],
        "review_decision": None,
        "mergeable": "MERGEABLE",
        "unresolved_thread_count": 0,
        "draft": False,
        "created_at": datetime(2026, 3, 26, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 3, 27, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return PRDetails(**defaults)


class TestComputeSnapshotHash:
    def test_same_pr_same_hash(self):
        pr = _make_pr_summary()
        assert compute_snapshot_hash(pr) == compute_snapshot_hash(pr)

    def test_different_state_different_hash(self):
        pr1 = _make_pr_summary(state="OPEN")
        pr2 = _make_pr_summary(state="CLOSED")
        assert compute_snapshot_hash(pr1) != compute_snapshot_hash(pr2)

    def test_different_ci_different_hash(self):
        pr1 = _make_pr_summary(ci_status="SUCCESS")
        pr2 = _make_pr_summary(ci_status="FAILURE")
        assert compute_snapshot_hash(pr1) != compute_snapshot_hash(pr2)


class TestFilterOwnEvents:
    def test_filters_own_events(self):
        events = [
            _make_event(pr_id="x", event_type="new_comment", actor="alice", summary="s", priority=1),
            _make_event(pr_id="x", event_type="new_comment", actor="bob", summary="s", priority=1),
        ]
        result = _filter_own_events(events, "alice")
        assert len(result) == 1
        assert result[0]["actor"] == "bob"

    def test_keeps_events_with_no_actor(self):
        events = [
            _make_event(pr_id="x", event_type="ci_failed", actor=None, summary="s", priority=3),
        ]
        result = _filter_own_events(events, "alice")
        assert len(result) == 1

    def test_case_insensitive(self):
        events = [
            _make_event(pr_id="x", event_type="new_comment", actor="Alice", summary="s", priority=1),
        ]
        result = _filter_own_events(events, "alice")
        assert len(result) == 0


class TestTruncate:
    def test_short_text(self):
        assert _truncate("hello") == "hello"

    def test_long_text(self):
        text = "a" * 100
        result = _truncate(text, 80)
        assert len(result) == 83  # 80 + "..."
        assert result.endswith("...")


class TestDiffPr:
    @pytest.fixture
    async def test_db(self, tmp_path: Path):
        d = Database(tmp_path / "test.db")
        await d.connect()
        yield d
        await d.close()

    async def test_ci_failure_on_authored_pr(self, test_db: Database):
        pr = _make_pr_summary(ci_status="FAILURE", author="alice")
        details = _make_pr_details(ci_checks=[])

        # Store old state with SUCCESS
        await test_db.upsert_pr(
            id=pr.id, repo=pr.repo, number=pr.number, title=pr.title,
            state=pr.state, url=pr.url, author=pr.author, user_roles=pr.user_roles,
            ci_status="SUCCESS", review_decision=None, draft=False,
            created_at=pr.updated_at.isoformat(), updated_at=pr.updated_at.isoformat(),
            snapshot_hash="old",
        )
        await test_db.upsert_snapshot(pr.id, comments=[], reviews=[], checks=[])

        events = await _diff_pr(test_db, pr, details, "alice")
        assert any(e["event_type"] == "ci_failed" and e["priority"] == 3 for e in events)

    async def test_ci_recovery(self, test_db: Database):
        pr = _make_pr_summary(ci_status="SUCCESS", author="alice")
        details = _make_pr_details()

        await test_db.upsert_pr(
            id=pr.id, repo=pr.repo, number=pr.number, title=pr.title,
            state=pr.state, url=pr.url, author=pr.author, user_roles=pr.user_roles,
            ci_status="FAILURE", review_decision=None, draft=False,
            created_at=pr.updated_at.isoformat(), updated_at=pr.updated_at.isoformat(),
            snapshot_hash="old",
        )
        await test_db.upsert_snapshot(pr.id, comments=[], reviews=[], checks=[])

        events = await _diff_pr(test_db, pr, details, "alice")
        assert any(e["event_type"] == "ci_recovered" and e["priority"] == 0 for e in events)

    async def test_new_comment_from_other_user(self, test_db: Database):
        pr = _make_pr_summary()
        comment = Comment(author="bob", body="looks good", created_at=datetime(2026, 3, 27, tzinfo=timezone.utc))
        details = _make_pr_details(comments=[comment])

        await test_db.upsert_pr(
            id=pr.id, repo=pr.repo, number=pr.number, title=pr.title,
            state=pr.state, url=pr.url, author=pr.author, user_roles=pr.user_roles,
            ci_status=pr.ci_status, review_decision=None, draft=False,
            created_at=pr.updated_at.isoformat(), updated_at=pr.updated_at.isoformat(),
            snapshot_hash="old",
        )
        await test_db.upsert_snapshot(pr.id, comments=[], reviews=[], checks=[])

        events = await _diff_pr(test_db, pr, details, "alice")
        assert any(e["event_type"] == "new_comment" and e["actor"] == "bob" for e in events)

    async def test_own_comment_filtered_out(self, test_db: Database):
        pr = _make_pr_summary()
        comment = Comment(author="alice", body="my own comment", created_at=datetime(2026, 3, 27, tzinfo=timezone.utc))
        details = _make_pr_details(comments=[comment])

        await test_db.upsert_pr(
            id=pr.id, repo=pr.repo, number=pr.number, title=pr.title,
            state=pr.state, url=pr.url, author=pr.author, user_roles=pr.user_roles,
            ci_status=pr.ci_status, review_decision=None, draft=False,
            created_at=pr.updated_at.isoformat(), updated_at=pr.updated_at.isoformat(),
            snapshot_hash="old",
        )
        await test_db.upsert_snapshot(pr.id, comments=[], reviews=[], checks=[])

        events = await _diff_pr(test_db, pr, details, "alice")
        assert not any(e["event_type"] == "new_comment" for e in events)

    async def test_changes_requested_on_authored_pr(self, test_db: Database):
        pr = _make_pr_summary(review_decision="CHANGES_REQUESTED", author="alice")
        details = _make_pr_details()

        await test_db.upsert_pr(
            id=pr.id, repo=pr.repo, number=pr.number, title=pr.title,
            state=pr.state, url=pr.url, author=pr.author, user_roles=pr.user_roles,
            ci_status=pr.ci_status, review_decision=None, draft=False,
            created_at=pr.updated_at.isoformat(), updated_at=pr.updated_at.isoformat(),
            snapshot_hash="old",
        )
        await test_db.upsert_snapshot(pr.id, comments=[], reviews=[], checks=[])

        events = await _diff_pr(test_db, pr, details, "alice")
        assert any(e["event_type"] == "changes_requested" and e["priority"] == 3 for e in events)
