from __future__ import annotations

import logging
from datetime import datetime

import httpx

from pr_context.models import CICheck, Comment, PRDetails, PRSummary, Review
from pr_context.queries import PR_DETAIL, SEARCH_MY_PRS, VIEWER_LOGIN

logger = logging.getLogger(__name__)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"


class GitHubClientError(Exception):
    pass


class GitHubClient:
    def __init__(self, token: str) -> None:
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        self._login: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables

        resp = await self._client.post(GITHUB_GRAPHQL_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()

        if "errors" in data:
            raise GitHubClientError(f"GraphQL errors: {data['errors']}")

        return data["data"]

    async def get_viewer_login(self) -> str:
        if self._login:
            return self._login
        data = await self._graphql(VIEWER_LOGIN)
        self._login = data["viewer"]["login"]
        return self._login

    async def fetch_my_prs(self) -> list[PRSummary]:
        login = await self.get_viewer_login()
        variables = {
            "author_q": f"is:pr author:{login} is:open",
            "reviewer_q": f"is:pr reviewed-by:{login} is:open",
            "assignee_q": f"is:pr assignee:{login} is:open",
        }
        data = await self._graphql(SEARCH_MY_PRS, variables)

        seen: dict[str, PRSummary] = {}
        role_map: dict[str, set[str]] = {}

        for category, role in [
            ("authored", "author"),
            ("reviewing", "reviewer"),
            ("assigned", "assignee"),
        ]:
            for node in data[category]["nodes"]:
                pr_id = _make_pr_id(node)
                role_map.setdefault(pr_id, set()).add(role)

                if pr_id not in seen:
                    seen[pr_id] = _parse_pr_summary(node, pr_id, [])

        # Assign collected roles
        for pr_id, pr in seen.items():
            pr.user_roles = sorted(role_map.get(pr_id, []))

        return list(seen.values())

    async def fetch_pr_details(self, owner: str, repo: str, number: int) -> PRDetails:
        data = await self._graphql(
            PR_DETAIL,
            {"owner": owner, "repo": repo, "number": number},
        )
        pr = data["repository"]["pullRequest"]
        pr_id = f"{owner}/{repo}#{number}"

        comments = [
            Comment(
                author=c["author"]["login"] if c["author"] else "ghost",
                body=c["body"],
                created_at=c["createdAt"],
            )
            for c in pr["comments"]["nodes"]
        ]

        reviews = [
            Review(
                author=r["author"]["login"] if r["author"] else "ghost",
                state=r["state"],
                body=r["body"] or "",
                submitted_at=r["submittedAt"],
            )
            for r in pr["reviews"]["nodes"]
        ]

        ci_checks = _parse_ci_checks(pr)

        return PRDetails(
            id=pr_id,
            repo=f"{owner}/{repo}",
            number=number,
            title=pr["title"],
            state=pr["state"],
            url=pr["url"],
            author=pr["author"]["login"] if pr["author"] else "ghost",
            body=pr["body"] or "",
            comments=comments,
            reviews=reviews,
            ci_checks=ci_checks,
            review_decision=pr.get("reviewDecision"),
            mergeable=pr.get("mergeable"),
            unresolved_thread_count=_count_unresolved_threads(pr),
            draft=pr["isDraft"],
            created_at=pr["createdAt"],
            updated_at=pr["updatedAt"],
        )


def _make_pr_id(node: dict) -> str:
    repo = node["repository"]["nameWithOwner"]
    return f"{repo}#{node['number']}"


def _parse_pr_summary(node: dict, pr_id: str, roles: list[str]) -> PRSummary:
    ci_status = _extract_ci_status(node)
    return PRSummary(
        id=pr_id,
        repo=node["repository"]["nameWithOwner"],
        number=node["number"],
        title=node["title"],
        state=node["state"],
        url=node["url"],
        author=node["author"]["login"] if node["author"] else "ghost",
        user_roles=roles,
        ci_status=ci_status,
        review_decision=node.get("reviewDecision"),
        mergeable=node.get("mergeable"),
        unresolved_thread_count=_count_unresolved_threads(node),
        draft=node["isDraft"],
        updated_at=node["updatedAt"],
    )


def _count_unresolved_threads(node: dict) -> int:
    threads = node.get("reviewThreads", {})
    nodes = threads.get("nodes", [])
    return sum(1 for t in nodes if not t.get("isResolved", True))


def _extract_ci_status(node: dict) -> str | None:
    commits = node.get("commits", {}).get("nodes", [])
    if not commits:
        return None
    rollup = commits[0].get("commit", {}).get("statusCheckRollup")
    if not rollup:
        return None
    return rollup.get("state")


def _parse_ci_checks(pr: dict) -> list[CICheck]:
    commits = pr.get("commits", {}).get("nodes", [])
    if not commits:
        return []
    rollup = commits[0].get("commit", {}).get("statusCheckRollup")
    if not rollup:
        return []
    contexts = rollup.get("contexts", {}).get("nodes", [])
    checks = []
    for ctx in contexts:
        if "name" in ctx:
            checks.append(CICheck(
                name=ctx["name"],
                status=ctx.get("status", "UNKNOWN"),
                conclusion=ctx.get("conclusion"),
            ))
        elif "context" in ctx:
            checks.append(CICheck(
                name=ctx["context"],
                status=ctx.get("state", "UNKNOWN"),
                conclusion=None,
            ))
    return checks
