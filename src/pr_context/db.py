from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

SCHEMA_VERSION = "7"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pull_requests (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    title TEXT,
    state TEXT,
    url TEXT,
    author TEXT,
    user_roles TEXT DEFAULT '[]',
    ci_status TEXT,
    review_decision TEXT,
    mergeable TEXT,
    merge_state_status TEXT,
    unresolved_thread_count INTEGER DEFAULT 0,
    pending_reviewers TEXT DEFAULT '[]',
    draft INTEGER DEFAULT 0,
    head_branch TEXT,
    base_branch TEXT,
    latest_commit_date TEXT,
    created_at TEXT,
    updated_at TEXT,
    snapshot_hash TEXT,
    last_synced_at TEXT,
    UNIQUE(repo, number)
);

CREATE TABLE IF NOT EXISTS pr_snapshots (
    pr_id TEXT PRIMARY KEY REFERENCES pull_requests(id),
    comments_json TEXT DEFAULT '[]',
    reviews_json TEXT DEFAULT '[]',
    checks_json TEXT DEFAULT '[]',
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS pr_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_id TEXT REFERENCES pull_requests(id),
    event_type TEXT NOT NULL,
    actor TEXT,
    summary TEXT,
    priority INTEGER DEFAULT 1,
    data_json TEXT,
    acknowledged INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row

        # Check schema version — if outdated, drop and recreate.
        # All data is a cache from GitHub, safe to rebuild.
        needs_reset = False
        try:
            cursor = await self._conn.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            )
            row = await cursor.fetchone()
            if row is None or row["value"] != SCHEMA_VERSION:
                needs_reset = True
        except Exception:
            needs_reset = True

        if needs_reset:
            await self._conn.executescript(
                "DROP TABLE IF EXISTS pr_events;"
                "DROP TABLE IF EXISTS pr_snapshots;"
                "DROP TABLE IF EXISTS pull_requests;"
                "DROP TABLE IF EXISTS metadata;"
            )

        await self._conn.executescript(SCHEMA)
        await self._conn.execute(
            "INSERT INTO metadata (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    # -- Pull Requests --

    async def upsert_pr(
        self,
        *,
        id: str,
        repo: str,
        number: int,
        title: str,
        state: str,
        url: str,
        author: str,
        user_roles: list[str],
        ci_status: str | None,
        review_decision: str | None,
        mergeable: str | None = None,
        merge_state_status: str | None = None,
        unresolved_thread_count: int = 0,
        pending_reviewers: list[str] | None = None,
        draft: bool,
        head_branch: str | None = None,
        base_branch: str | None = None,
        latest_commit_date: str | None = None,
        created_at: str,
        updated_at: str,
        snapshot_hash: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            """
            INSERT INTO pull_requests
                (id, repo, number, title, state, url, author, user_roles,
                 ci_status, review_decision, mergeable, merge_state_status,
                 unresolved_thread_count, pending_reviewers,
                 draft, head_branch, base_branch, latest_commit_date,
                 created_at, updated_at, snapshot_hash, last_synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, state=excluded.state, url=excluded.url,
                author=excluded.author, user_roles=excluded.user_roles,
                ci_status=excluded.ci_status, review_decision=excluded.review_decision,
                mergeable=excluded.mergeable, merge_state_status=excluded.merge_state_status,
                unresolved_thread_count=excluded.unresolved_thread_count,
                pending_reviewers=excluded.pending_reviewers,
                draft=excluded.draft, head_branch=excluded.head_branch,
                base_branch=excluded.base_branch, latest_commit_date=excluded.latest_commit_date,
                updated_at=excluded.updated_at,
                snapshot_hash=excluded.snapshot_hash, last_synced_at=excluded.last_synced_at
            """,
            (
                id, repo, number, title, state, url, author,
                json.dumps(user_roles), ci_status, review_decision,
                mergeable, merge_state_status, unresolved_thread_count,
                json.dumps(pending_reviewers or []),
                int(draft), head_branch, base_branch, latest_commit_date,
                created_at, updated_at, snapshot_hash, now,
            ),
        )
        await self.conn.commit()

    async def get_pr(self, pr_id: str) -> dict | None:
        cursor = await self.conn.execute(
            "SELECT * FROM pull_requests WHERE id = ?", (pr_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    async def get_all_prs(self, state: str | None = None) -> list[dict]:
        if state:
            cursor = await self.conn.execute(
                "SELECT * FROM pull_requests WHERE state = ? ORDER BY updated_at DESC",
                (state,),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT * FROM pull_requests ORDER BY updated_at DESC"
            )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_pr_snapshot_hash(self, pr_id: str) -> str | None:
        cursor = await self.conn.execute(
            "SELECT snapshot_hash FROM pull_requests WHERE id = ?", (pr_id,)
        )
        row = await cursor.fetchone()
        return row["snapshot_hash"] if row else None

    async def get_all_pr_ids(self) -> set[str]:
        cursor = await self.conn.execute("SELECT id FROM pull_requests")
        rows = await cursor.fetchall()
        return {r["id"] for r in rows}

    # -- Snapshots --

    async def upsert_snapshot(
        self,
        pr_id: str,
        *,
        comments: list[dict],
        reviews: list[dict],
        checks: list[dict],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            """
            INSERT INTO pr_snapshots (pr_id, comments_json, reviews_json, checks_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(pr_id) DO UPDATE SET
                comments_json=excluded.comments_json,
                reviews_json=excluded.reviews_json,
                checks_json=excluded.checks_json,
                updated_at=excluded.updated_at
            """,
            (pr_id, json.dumps(comments), json.dumps(reviews), json.dumps(checks), now),
        )
        await self.conn.commit()

    async def get_snapshot(self, pr_id: str) -> dict | None:
        cursor = await self.conn.execute(
            "SELECT * FROM pr_snapshots WHERE pr_id = ?", (pr_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "pr_id": row["pr_id"],
            "comments": json.loads(row["comments_json"]),
            "reviews": json.loads(row["reviews_json"]),
            "checks": json.loads(row["checks_json"]),
            "updated_at": row["updated_at"],
        }

    # -- Events --

    async def add_event(
        self,
        *,
        pr_id: str,
        event_type: str,
        actor: str | None,
        summary: str,
        priority: int = 1,
        data: dict | None = None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO pr_events (pr_id, event_type, actor, summary, priority, data_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (pr_id, event_type, actor, summary, priority, json.dumps(data) if data else None),
        )
        await self.conn.commit()

    async def get_unacknowledged_events(self) -> list[dict]:
        cursor = await self.conn.execute(
            """
            SELECT e.*, p.repo, p.number as pr_number
            FROM pr_events e
            JOIN pull_requests p ON e.pr_id = p.id
            WHERE e.acknowledged = 0
            ORDER BY e.priority DESC, e.created_at DESC
            """
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def acknowledge_events(self) -> int:
        cursor = await self.conn.execute(
            "UPDATE pr_events SET acknowledged = 1 WHERE acknowledged = 0"
        )
        await self.conn.commit()
        return cursor.rowcount

    # -- Metadata --

    async def set_metadata(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self.conn.commit()

    async def get_metadata(self, key: str) -> str | None:
        cursor = await self.conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row["value"] if row else None

    # -- Helpers --

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict:
        return dict(row)
