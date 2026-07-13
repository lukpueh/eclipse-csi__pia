"""Management CLI for PIA.

Subcommands
-----------
- sync: Reconcile all project authorizations from a curated file into the
  database (create/update/delete).

Usage Example
-------------
    PIA_DATABASE_URL=postgresql://user:secret@localhost:5432/pia \
    PIA_DEPENDENCY_TRACK_API_KEY=<API key with VIEW_PORTFOLIO permission> \
    PIA_GITHUB_TOKEN=<optional token with public read perission to lift the anonymous rate limit> \
        uv run pia sync projects.yaml --dt-url https://sbom.eclipse.org --db-dry-run

"""

import logging
import os

import click
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .sync import (
    apply_plan,
    build_desired,
    compute_plan,
    format_plan,
    load_projects_file,
    validate_projects_file,
)


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def cli(verbose: bool) -> None:
    """PIA management CLI."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _make_session() -> Session:
    engine = create_engine(os.environ["PIA_DATABASE_URL"])
    return sessionmaker(bind=engine)()


@cli.command("sync")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--dt-url",
    default=None,
    help="DependencyTrack base URL (required). This is the base, not the "
    "/api/v1/bom upload URL.",
)
@click.option(
    "--db-dry-run",
    is_flag=True,
    help="Show the plan without writing to the PIA database. Scoped to the "
    "database only: with --create-dt-projects, missing DependencyTrack projects "
    "are still created (they are a prerequisite, not part of the DB plan).",
)
@click.option(
    "--check",
    is_flag=True,
    help="Validate the file only; performs no database or network access.",
)
@click.option(
    "--allow-db-deletions",
    is_flag=True,
    help="Apply the plan even when it contains deletions in the PIA database.",
)
@click.option(
    "--create-dt-projects",
    is_flag=True,
    help="Create missing parent/child projects on DependencyTrack instead of "
    "failing when they do not exist; performs no deletion on DependencyTrack; "
    "requires a DT API key with PORTFOLIO_MANAGEMENT permission. Applies even "
    "under --db-dry-run.",
)
def sync(
    file: str,
    dt_url: str | None,
    db_dry_run: bool,
    check: bool,
    allow_db_deletions: bool,
    create_dt_projects: bool,
) -> None:
    """Reconcile all authorizations from a curated FILE into the database.

    Computes the difference between the file (the source of truth) and the
    current database state, prints the plan, and — unless --db-dry-run — applies
    it, creating, updating and deleting rows to match the file.

    Requires PIA_DATABASE_URL, --dt-url, and PIA_DEPENDENCY_TRACK_API_KEY (with
    VIEW_PORTFOLIO permission). PIA_GITHUB_TOKEN is optional and only lifts the
    anonymous GitHub rate limit.
    """
    pf = load_projects_file(file)
    validate_projects_file(pf)
    if check:
        click.echo(f"OK: {file} is valid ({len(pf.projects)} project(s)).")
        return

    if not os.environ.get("PIA_DATABASE_URL"):
        raise click.ClickException("PIA_DATABASE_URL is not set")

    dt_api_key = os.environ.get("PIA_DEPENDENCY_TRACK_API_KEY")
    if not dt_url or not dt_api_key:
        raise click.ClickException(
            "--dt-url and PIA_DEPENDENCY_TRACK_API_KEY are required"
        )

    desired = build_desired(
        pf,
        dt_url=dt_url,
        dt_api_key=dt_api_key,
        github_token=os.environ.get("PIA_GITHUB_TOKEN"),
        create_dt_projects=create_dt_projects,
    )

    with _make_session() as session:
        plan = compute_plan(session, desired)
        click.echo(format_plan(plan))

        if db_dry_run or plan.is_empty():
            session.rollback()
            return

        if (plan.ef_delete or plan.deletes) and not allow_db_deletions:
            session.rollback()
            raise click.ClickException(
                "Plan contains deletions; re-run with --allow-db-deletions to "
                "apply (or --db-dry-run to preview)."
            )

        apply_plan(session, plan)
        session.commit()
        click.echo("Applied.")


if __name__ == "__main__":
    cli()
