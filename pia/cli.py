"""Management CLI for registering workloads and DependencyTrack projects.

Subcommands
-----------
- add-workload: Register a CI/CD workload that is allowed to upload SBOMs for an
  Eclipse Foundation project.

- add-dt-project: Register a DependencyTrack project as the upload target for a
  given Eclipse Foundation project.

- sync: Reconcile all authorizations from a curated file (create/update/delete).

Usage Examples
--------------
Register GitHub Actions:

    PIA_DATABASE_URL=postgresql://user:secret@localhost:5432/pia \
        uv run pia add-workload eclipse-foo \
                https://github.com/eclipse-foo/repo

Register Jenkins Instance:

    PIA_DATABASE_URL=postgresql://user:secret@localhost:5432/pia \
        uv run pia add-workload eclipse-bar \
                https://ci.eclipse.org/eclipse-bar/oidc

Register DependencyTrack Project:

    PIA_DATABASE_URL=postgresql://user:secret@localhost:5432/pia \
    PIA_DEPENDENCY_TRACK_API_KEY=<API key with VIEW_PORTFOLIO permission> \
        uv run pia add-dt-project eclipse-baz \
                https://sbom.eclipse.org "Eclipse Baz" baz-server

Reconcile everything from a curated file:

    PIA_DATABASE_URL=postgresql://user:secret@localhost:5432/pia \
    PIA_DEPENDENCY_TRACK_API_KEY=<API key with VIEW_PORTFOLIO permission> \
    PIA_GITHUB_TOKEN=<optional token to lift the anonymous rate limit> \
        uv run pia sync projects.yaml --dt-url https://sbom.eclipse.org --dry-run

"""

import logging
import os

import click
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    DependencyTrackProject,
    EclipseFoundationProject,
    GitHubWorkload,
    JenkinsWorkload,
    Workload,
)
from .sync import (
    apply_plan,
    build_desired,
    classify_workload_url,
    compute_plan,
    fetch_github_owner_id,
    format_plan,
    load_projects_file,
    resolve_dt_child_uuid,
    validate_projects_file,
)

logger = logging.getLogger(__name__)


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """PIA management CLI."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Fail early if the DB URL is missing. `sync` is exempt because `--check`
    # runs fully offline; `sync` enforces the requirement itself when it needs
    # a database.
    if ctx.invoked_subcommand != "sync" and not os.environ.get("PIA_DATABASE_URL"):
        raise click.ClickException("PIA_DATABASE_URL is not set")


def _make_session() -> Session:
    engine = create_engine(os.environ["PIA_DATABASE_URL"])
    return sessionmaker(bind=engine)()


def _get_dt_api_key() -> str:
    key = os.environ.get("PIA_DEPENDENCY_TRACK_API_KEY")
    if not key:
        raise click.ClickException("PIA_DEPENDENCY_TRACK_API_KEY is not set")
    return key


def _create_ef_project_if_needed(session: Session, ef_project_id: str) -> None:
    if session.get(EclipseFoundationProject, ef_project_id) is None:
        logger.info(f"Creating EclipseFoundationProject {ef_project_id!r}")
        session.add(EclipseFoundationProject(id=ef_project_id))
    else:
        logger.info(f"Using existing EclipseFoundationProject {ef_project_id!r}")


@cli.command("add-workload")
@click.argument("ef_project_id")
@click.argument("url")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Look up data and prepare the row, but do not commit.",
)
def add_workload(ef_project_id: str, url: str, dry_run: bool) -> None:
    """Register a GitHub or Jenkins workload for an Eclipse Foundation project.

    URL type is determined by its value: github.com URLs create a GitHubWorkload,
    ci.eclipse.org URLs create a JenkinsWorkload with the URL as issuer. Both are
    matched on the URL's scheme and host. Any other URL is rejected.
    """
    kind, first, second = classify_workload_url(url)
    if kind == "github":
        owner, repo = first, second
        owner_id = fetch_github_owner_id(owner)
        workload: Workload = GitHubWorkload(
            ef_project_id=ef_project_id,
            repo_owner=owner,
            repo_name=repo,
            repo_owner_id=owner_id,
        )
        logger.info(
            f"Prepared GitHubWorkload(ef_project_id={ef_project_id!r}, "
            f"repo_owner={owner!r}, repo_name={repo!r}, repo_owner_id={owner_id})"
        )
    else:
        workload = JenkinsWorkload(ef_project_id=ef_project_id, issuer=first)
        logger.info(
            f"Prepared JenkinsWorkload(ef_project_id={ef_project_id!r}, "
            f"issuer={first!r})"
        )

    with _make_session() as session:
        _create_ef_project_if_needed(session, ef_project_id)
        session.add(workload)
        if dry_run:
            logger.info("Dry-run: rolling back transaction")
            session.rollback()
        else:
            session.commit()
            logger.info("Committed workload")


@cli.command("add-dt-project")
@click.argument("ef_project_id")
@click.argument("dt_url")
@click.argument("parent_name")
@click.argument("project_name")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Look up data and prepare the row, but do not commit.",
)
def add_dt_project(
    ef_project_id: str,
    dt_url: str,
    parent_name: str,
    project_name: str,
    dry_run: bool,
) -> None:
    """Register a DependencyTrack project for an Eclipse Foundation project.

    Fetches the root project matching PARENT_NAME (asserting exactly one), then
    finds PROJECT_NAME among its children (asserting exactly one), and stores
    that child's UUID.
    """
    api_key = _get_dt_api_key()

    child_uuid = resolve_dt_child_uuid(dt_url, parent_name, project_name, api_key)
    logger.info(f"Resolved child {project_name!r} -> uuid={child_uuid}")

    dt_project = DependencyTrackProject(
        ef_project_id=ef_project_id,
        name=project_name,
        parent_uuid=child_uuid,
    )
    logger.info(
        f"Prepared DependencyTrackProject(ef_project_id={ef_project_id!r}, "
        f"name={project_name!r}, parent_uuid={child_uuid})"
    )

    with _make_session() as session:
        _create_ef_project_if_needed(session, ef_project_id)
        session.add(dt_project)
        if dry_run:
            logger.info("Dry-run: rolling back transaction")
            session.rollback()
        else:
            session.commit()
            logger.info("Committed DependencyTrack project")


@cli.command("sync")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--dt-url",
    default=None,
    help="DependencyTrack base URL (required if the file has dependency_track "
    "entries). This is the base, not the /api/v1/bom upload URL.",
)
@click.option("--dry-run", is_flag=True, help="Show the plan without writing.")
@click.option(
    "--check",
    is_flag=True,
    help="Validate the file only; performs no database or network access.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Apply the plan even when it contains deletions.",
)
def sync(file: str, dt_url: str | None, dry_run: bool, check: bool, yes: bool) -> None:
    """Reconcile all authorizations from a curated FILE into the database.

    Computes the difference between the file (the source of truth) and the
    current database state, prints the plan, and — unless --dry-run — applies it,
    creating, updating and deleting rows to match the file.

    Requires PIA_DATABASE_URL. DependencyTrack mappings additionally require
    --dt-url and PIA_DEPENDENCY_TRACK_API_KEY. PIA_GITHUB_TOKEN is optional and
    only lifts the anonymous GitHub rate limit.
    """
    pf = load_projects_file(file)
    validate_projects_file(pf)
    if check:
        click.echo(f"OK: {file} is valid ({len(pf.projects)} project(s)).")
        return

    if not os.environ.get("PIA_DATABASE_URL"):
        raise click.ClickException("PIA_DATABASE_URL is not set")

    desired = build_desired(
        pf,
        dt_url=dt_url,
        dt_api_key=os.environ.get("PIA_DEPENDENCY_TRACK_API_KEY"),
        github_token=os.environ.get("PIA_GITHUB_TOKEN"),
    )

    with _make_session() as session:
        plan = compute_plan(session, desired)
        click.echo(format_plan(plan))

        if dry_run or plan.is_empty():
            session.rollback()
            return

        if (plan.ef_delete or plan.deletes) and not yes:
            session.rollback()
            raise click.ClickException(
                "Plan contains deletions; re-run with --yes to apply "
                "(or --dry-run to preview)."
            )

        apply_plan(session, plan)
        session.commit()
        click.echo("Applied.")


if __name__ == "__main__":
    cli()
