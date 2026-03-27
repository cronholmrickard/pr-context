from __future__ import annotations

import asyncio
import json
import sys

import click

from pr_context.config import get_settings
from pr_context.db import Database
from pr_context.github_client import GitHubClient
from pr_context.change_detector import sync_and_detect


def _run(coro):
    return asyncio.run(coro)


@click.group()
def cli():
    """PR Context Engine — debug CLI."""
    pass


@cli.command()
def check():
    """Sync with GitHub and show new events."""

    async def _check():
        settings = get_settings()
        db = Database(settings.db_path)
        await db.connect()
        github = GitHubClient(settings.github_token)

        try:
            username = await github.get_viewer_login()
            click.echo(f"Authenticated as: {username}")

            events = await sync_and_detect(db, github, username)
            for event in events:
                await db.add_event(**event)

            if events:
                click.echo(f"\n{len(events)} new event(s):")
                for e in events:
                    marker = "!" * e["priority"] if e["priority"] > 0 else " "
                    click.echo(f"  [{marker}] {e['summary']}")
            else:
                click.echo("\nNo new events.")
        finally:
            await github.close()
            await db.close()

    _run(_check())


@cli.command("list")
@click.option(
    "--state", default="open", help="Filter by state: open, closed, merged, all"
)
def list_prs(state: str):
    """List tracked PRs from local DB."""

    async def _list():
        settings = get_settings()
        db = Database(settings.db_path)
        await db.connect()

        try:
            state_filter = state.upper() if state != "all" else None
            rows = await db.get_all_prs(state=state_filter)

            if not rows:
                click.echo("No PRs found. Run 'check' first to sync.")
                return

            click.echo(f"{len(rows)} PR(s):\n")
            for row in rows:
                roles = row["user_roles"]
                if isinstance(roles, str):
                    roles = json.loads(roles)
                ci = row["ci_status"] or "—"
                review = row["review_decision"] or "—"
                draft = " [DRAFT]" if row["draft"] else ""
                click.echo(f"  {row['id']}: {row['title']}{draft}")
                click.echo(f"    CI: {ci}  Review: {review}  Roles: {', '.join(roles)}")
                click.echo(f"    {row['url']}")
                click.echo()
        finally:
            await db.close()

    _run(_list())


@cli.command()
@click.confirmation_option(prompt="This will delete all local data. Continue?")
def reset():
    """Delete the local database."""
    settings = get_settings()
    db_path = settings.db_path
    if db_path.exists():
        db_path.unlink()
        click.echo(f"Deleted {db_path}")
    else:
        click.echo("No database found.")


@cli.command()
def events():
    """Show unacknowledged events."""

    async def _events():
        settings = get_settings()
        db = Database(settings.db_path)
        await db.connect()

        try:
            unacked = await db.get_unacknowledged_events()
            if not unacked:
                click.echo("No unacknowledged events.")
                return

            click.echo(f"{len(unacked)} unacknowledged event(s):\n")
            for e in unacked:
                priority_label = {0: "low", 1: "normal", 2: "high", 3: "urgent"}.get(
                    e["priority"], "?"
                )
                click.echo(f"  [{priority_label}] {e['summary']}")
                click.echo(f"    PR: {e['pr_id']}  Type: {e['event_type']}")
                click.echo()
        finally:
            await db.close()

    _run(_events())


if __name__ == "__main__":
    cli()
