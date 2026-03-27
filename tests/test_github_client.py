import json

import httpx
import pytest

from pr_context.github_client import GitHubClient, _make_pr_id, _parse_pr_summary


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
        "author": {"login": author},
        "repository": {"nameWithOwner": repo},
        "updatedAt": "2024-01-15T10:00:00Z",
        "createdAt": "2024-01-10T10:00:00Z",
        "reviewDecision": review_decision,
        "commits": {
            "nodes": [
                {
                    "commit": {
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
