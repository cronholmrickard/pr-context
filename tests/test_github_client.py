import json

import httpx
import pytest

from pr_context.github_client import (
    GitHubClient,
    _extract_latest_commit_date,
    _extract_pending_reviewers,
    _make_pr_id,
    _parse_ci_checks,
    _parse_pr_summary,
)


def _make_pr_node(
    number: int = 1,
    repo: str = "org/repo",
    title: str = "Test PR",
    state: str = "OPEN",
    author: str = "alice",
    ci_state: str | None = "SUCCESS",
    review_decision: str | None = None,
    draft: bool = False,
) -> dict:
    return {
        "id": f"PR_node_{number}",
        "number": number,
        "title": title,
        "state": state,
        "url": f"https://github.com/{repo}/pull/{number}",
        "isDraft": draft,
        "headRefName": f"feature-branch-{number}",
        "baseRefName": "main",
        "author": {"login": author},
        "repository": {"nameWithOwner": repo},
        "updatedAt": "2024-01-15T10:00:00Z",
        "createdAt": "2024-01-10T10:00:00Z",
        "reviewDecision": review_decision,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "committedDate": "2024-01-14T10:00:00Z",
                        "statusCheckRollup": {"state": ci_state} if ci_state else None
                    }
                }
            ]
        },
        "reviewRequests": {"nodes": []},
        "reviewThreads": {"totalCount": 0, "nodes": []},
        "mergeable": "MERGEABLE",
    }


def test_make_pr_id():
    node = _make_pr_node(number=42, repo="acme/widget")
    assert _make_pr_id(node) == "acme/widget#42"


def test_parse_pr_summary():
    node = _make_pr_node(number=5, repo="org/repo", ci_state="FAILURE")
    pr = _parse_pr_summary(node, "org/repo#5", ["author"])
    assert pr.id == "org/repo#5"
    assert pr.number == 5
    assert pr.ci_status == "FAILURE"
    assert pr.user_roles == ["author"]
    assert pr.draft is False


def test_parse_pr_summary_no_ci():
    node = _make_pr_node(ci_state=None)
    pr = _parse_pr_summary(node, "org/repo#1", [])
    assert pr.ci_status is None


def test_parse_pr_summary_ghost_author():
    node = _make_pr_node()
    node["author"] = None
    pr = _parse_pr_summary(node, "org/repo#1", [])
    assert pr.author == "ghost"


def test_parse_pr_summary_branch_info():
    node = _make_pr_node(number=5, repo="org/repo")
    pr = _parse_pr_summary(node, "org/repo#5", ["author"])
    assert pr.head_branch == "feature-branch-5"
    assert pr.base_branch == "main"
    assert pr.latest_commit_date is not None


class TestExtractLatestCommitDate:
    def test_with_commit(self):
        node = {"commits": {"nodes": [{"commit": {"committedDate": "2024-01-15T10:00:00Z"}}]}}
        assert _extract_latest_commit_date(node) == "2024-01-15T10:00:00Z"

    def test_no_commits(self):
        assert _extract_latest_commit_date({"commits": {"nodes": []}}) is None

    def test_missing_field(self):
        node = {"commits": {"nodes": [{"commit": {}}]}}
        assert _extract_latest_commit_date(node) is None


class TestExtractPendingReviewers:
    def test_no_requests(self):
        node = {"reviewRequests": {"nodes": []}}
        assert _extract_pending_reviewers(node) == []

    def test_user_reviewer(self):
        node = {"reviewRequests": {"nodes": [
            {"requestedReviewer": {"login": "bob"}},
        ]}}
        assert _extract_pending_reviewers(node) == ["bob"]

    def test_team_reviewer(self):
        node = {"reviewRequests": {"nodes": [
            {"requestedReviewer": {"name": "backend-team"}},
        ]}}
        assert _extract_pending_reviewers(node) == ["backend-team"]

    def test_mixed_reviewers(self):
        node = {"reviewRequests": {"nodes": [
            {"requestedReviewer": {"login": "alice"}},
            {"requestedReviewer": {"name": "frontend-team"}},
        ]}}
        assert _extract_pending_reviewers(node) == ["alice", "frontend-team"]

    def test_null_reviewer(self):
        node = {"reviewRequests": {"nodes": [
            {"requestedReviewer": None},
        ]}}
        assert _extract_pending_reviewers(node) == []


class TestParseCIChecks:
    def test_parse_check_run_with_details(self):
        pr = {
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {
                "state": "FAILURE",
                "contexts": {"nodes": [
                    {
                        "name": "build",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "detailsUrl": "https://github.com/org/repo/actions/runs/123",
                        "startedAt": "2024-01-15T10:00:00Z",
                        "completedAt": "2024-01-15T10:05:00Z",
                    },
                    {
                        "name": "test",
                        "status": "COMPLETED",
                        "conclusion": "FAILURE",
                        "detailsUrl": "https://github.com/org/repo/actions/runs/124",
                        "startedAt": "2024-01-15T10:00:00Z",
                        "completedAt": "2024-01-15T10:03:00Z",
                    },
                ]},
            }}}]},
        }
        checks = _parse_ci_checks(pr)
        assert len(checks) == 2
        assert checks[0].name == "build"
        assert checks[0].url == "https://github.com/org/repo/actions/runs/123"
        assert checks[0].started_at == "2024-01-15T10:00:00Z"
        assert checks[0].completed_at == "2024-01-15T10:05:00Z"
        assert checks[1].conclusion == "FAILURE"

    def test_parse_status_context_with_url(self):
        pr = {
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {
                "state": "SUCCESS",
                "contexts": {"nodes": [
                    {
                        "context": "ci/circleci",
                        "state": "SUCCESS",
                        "targetUrl": "https://circleci.com/build/123",
                    },
                ]},
            }}}]},
        }
        checks = _parse_ci_checks(pr)
        assert len(checks) == 1
        assert checks[0].name == "ci/circleci"
        assert checks[0].url == "https://circleci.com/build/123"
        assert checks[0].started_at is None

    def test_empty_commits(self):
        assert _parse_ci_checks({"commits": {"nodes": []}}) == []

    def test_no_rollup(self):
        pr = {"commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]}}
        assert _parse_ci_checks(pr) == []


class TestGitHubClientFetchMyPrs:
    """Tests for fetch_my_prs using mocked HTTP responses."""

    @pytest.fixture
    def mock_transport(self):
        return MockTransport()

    @pytest.fixture
    def client(self, mock_transport):
        gh = GitHubClient(token="fake-token")
        gh._client = httpx.AsyncClient(
            base_url="https://api.github.com/graphql",
            transport=mock_transport,
        )
        return gh

    async def test_fetch_deduplicates_and_assigns_roles(self, client, mock_transport):
        pr_node = _make_pr_node(number=10, repo="org/app", author="testuser")

        mock_transport.add_response({
            "data": {
                "viewer": {"login": "testuser"},
            }
        })
        mock_transport.add_response({
            "data": {
                "authored": {"nodes": [pr_node]},
                "reviewing": {"nodes": [pr_node]},
                "assigned": {"nodes": []},
            }
        })

        prs = await client.fetch_my_prs()
        assert len(prs) == 1
        assert set(prs[0].user_roles) == {"author", "reviewer"}

        await client.close()

    async def test_fetch_multiple_prs(self, client, mock_transport):
        mock_transport.add_response({
            "data": {"viewer": {"login": "testuser"}}
        })
        mock_transport.add_response({
            "data": {
                "authored": {"nodes": [_make_pr_node(number=1)]},
                "reviewing": {"nodes": [_make_pr_node(number=2, repo="other/repo")]},
                "assigned": {"nodes": [_make_pr_node(number=3)]},
            }
        })

        prs = await client.fetch_my_prs()
        assert len(prs) == 3

        await client.close()


class MockTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self._responses: list[dict] = []

    def add_response(self, data: dict):
        self._responses.append(data)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self._responses:
            raise RuntimeError("No mock responses left")
        body = self._responses.pop(0)
        return httpx.Response(200, json=body)
