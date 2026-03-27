from pathlib import Path

import pytest

from pr_context.db import Database


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


async def test_schema_creation(db: Database):
    cursor = await db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row["name"] for row in await cursor.fetchall()}
    assert tables >= {"pull_requests", "pr_snapshots", "pr_events", "metadata"}


async def test_upsert_and_get_pr(db: Database):
    await db.upsert_pr(
        id="org/repo#1",
        repo="org/repo",
        number=1,
        title="Test PR",
        state="OPEN",
        url="https://github.com/org/repo/pull/1",
        author="alice",
        user_roles=["author"],
        ci_status="SUCCESS",
        review_decision=None,
        draft=False,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
        snapshot_hash="abc123",
    )

    pr = await db.get_pr("org/repo#1")
    assert pr is not None
    assert pr["title"] == "Test PR"
    assert pr["state"] == "OPEN"
    assert pr["author"] == "alice"
    assert pr["snapshot_hash"] == "abc123"


async def test_upsert_updates_existing(db: Database):
    kwargs = dict(
        id="org/repo#1",
        repo="org/repo",
        number=1,
        title="Original",
        state="OPEN",
        url="https://github.com/org/repo/pull/1",
        author="alice",
        user_roles=["author"],
        ci_status=None,
        review_decision=None,
        draft=False,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        snapshot_hash="hash1",
    )
    await db.upsert_pr(**kwargs)
    await db.upsert_pr(**{**kwargs, "title": "Updated", "snapshot_hash": "hash2"})

    pr = await db.get_pr("org/repo#1")
    assert pr["title"] == "Updated"
    assert pr["snapshot_hash"] == "hash2"


async def test_get_all_prs_with_state_filter(db: Database):
    for i, state in enumerate(["OPEN", "OPEN", "MERGED"], 1):
        await db.upsert_pr(
            id=f"org/repo#{i}",
            repo="org/repo",
            number=i,
            title=f"PR {i}",
            state=state,
            url=f"https://github.com/org/repo/pull/{i}",
            author="alice",
            user_roles=["author"],
            ci_status=None,
            review_decision=None,
            draft=False,
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            snapshot_hash=f"h{i}",
        )

    all_prs = await db.get_all_prs()
    assert len(all_prs) == 3

    open_prs = await db.get_all_prs(state="OPEN")
    assert len(open_prs) == 2


async def test_snapshot_round_trip(db: Database):
    # Need a PR first for the FK
    await db.upsert_pr(
        id="org/repo#1", repo="org/repo", number=1, title="T", state="OPEN",
        url="u", author="a", user_roles=[], ci_status=None, review_decision=None,
        draft=False, created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z", snapshot_hash="h",
    )

    comments = [{"author": "bob", "body": "LGTM"}]
    reviews = [{"author": "bob", "state": "APPROVED"}]
    checks = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]

    await db.upsert_snapshot("org/repo#1", comments=comments, reviews=reviews, checks=checks)

    snap = await db.get_snapshot("org/repo#1")
    assert snap is not None
    assert snap["comments"] == comments
    assert snap["reviews"] == reviews
    assert snap["checks"] == checks


async def test_events_lifecycle(db: Database):
    await db.upsert_pr(
        id="org/repo#1", repo="org/repo", number=1, title="T", state="OPEN",
        url="u", author="a", user_roles=[], ci_status=None, review_decision=None,
        draft=False, created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z", snapshot_hash="h",
    )

    await db.add_event(
        pr_id="org/repo#1",
        event_type="new_comment",
        actor="bob",
        summary="bob commented on PR #1",
        priority=1,
    )
    await db.add_event(
        pr_id="org/repo#1",
        event_type="ci_failed",
        actor=None,
        summary="CI failed on PR #1",
        priority=3,
    )

    events = await db.get_unacknowledged_events()
    assert len(events) == 2
    # Ordered by priority DESC
    assert events[0]["priority"] == 3
    assert events[1]["priority"] == 1

    count = await db.acknowledge_events()
    assert count == 2

    events = await db.get_unacknowledged_events()
    assert len(events) == 0


async def test_metadata(db: Database):
    assert await db.get_metadata("missing") is None

    await db.set_metadata("last_sync", "2024-01-01T00:00:00Z")
    assert await db.get_metadata("last_sync") == "2024-01-01T00:00:00Z"

    await db.set_metadata("last_sync", "2024-01-02T00:00:00Z")
    assert await db.get_metadata("last_sync") == "2024-01-02T00:00:00Z"
