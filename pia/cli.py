"""Management CLI for registering workloads and DependencyTrack projects."""

import logging
import os
from urllib.parse import urlparse

import click
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    DependencyTrackProject,
    EclipseFoundationProject,
    GitHubWorkload,
    JenkinsWorkload,
)

logger = logging.getLogger(__name__)


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def cli(verbose: bool) -> None:
    """PIA management CLI."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _make_session() -> Session:
    db_url = os.environ.get("PIA_DATABASE_URL")
    if not db_url:
        raise click.ClickException("PIA_DATABASE_URL is not set")
    engine = create_engine(db_url)
    return sessionmaker(bind=engine)()


def _dt_api_key() -> str:
    key = os.environ.get("PIA_DEPENDENCY_TRACK_API_KEY")
    if not key:
        raise click.ClickException("PIA_DEPENDENCY_TRACK_API_KEY is not set")
    return key


def _get_or_create_ef_project(session: Session, ef_project_id: str) -> None:
    project = session.get(EclipseFoundationProject, ef_project_id)
    if project is None:
        logger.info(f"Creating EclipseFoundationProject {ef_project_id!r}")
        session.add(EclipseFoundationProject(id=ef_project_id))
    else:
        logger.info(f"Using existing EclipseFoundationProject {ef_project_id!r}")


def _fetch_github_owner_id(owner: str) -> str:
    url = f"https://api.github.com/users/{owner}"
    logger.info(f"Fetching GitHub owner id from {url}")
    response = requests.get(url, headers={"Accept": "application/vnd.github+json"})
    response.raise_for_status()
    owner_id = str(response.json()["id"])
    logger.info(f"GitHub owner {owner!r} has id {owner_id}")
    return owner_id


def _dt_find_project_by_name(dt_url: str, name: str, api_key: str) -> dict:
    """Look up a DependencyTrack project by name, asserting exactly one match."""
    url = f"{dt_url.rstrip('/')}/api/v1/project"
    logger.info(f"Querying DependencyTrack project list at {url} for name={name!r}")
    response = requests.get(
        url,
        params={"name": name},
        headers={"X-Api-Key": api_key, "Accept": "application/json"},
    )
    response.raise_for_status()
    projects = response.json()
    matches = [p for p in projects if p.get("name") == name]
    if len(matches) != 1:
        raise click.ClickException(
            f"Expected exactly one DependencyTrack project named {name!r}, "
            f"found {len(matches)}"
        )
    return matches[0]


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

    URL type is determined by host: github.com URLs create a GitHubWorkload,
    anything else creates a JenkinsWorkload with the URL as issuer.
    """
    parsed = urlparse(url)
    if "github.com" in parsed.netloc:
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) < 2 or not all(path_parts[:2]):
            raise click.ClickException(f"GitHub URL must include owner/repo: {url}")
        owner, repo = path_parts[0], path_parts[1]
        owner_id = _fetch_github_owner_id(owner)
        workload: GitHubWorkload | JenkinsWorkload = GitHubWorkload(
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
        workload = JenkinsWorkload(ef_project_id=ef_project_id, issuer=url)
        logger.info(
            f"Prepared JenkinsWorkload(ef_project_id={ef_project_id!r}, issuer={url!r})"
        )

    session = _make_session()
    try:
        _get_or_create_ef_project(session, ef_project_id)
        session.add(workload)
        if dry_run:
            logger.info("Dry-run: rolling back transaction")
            session.rollback()
        else:
            session.commit()
            logger.info("Committed workload")
    finally:
        session.close()


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

    Looks up parent and child by name on DependencyTrack (asserting exactly one
    match each), verifies the child's parent UUID matches, then stores the
    child UUID as parent_uuid.
    """
    api_key = _dt_api_key()

    parent = _dt_find_project_by_name(dt_url, parent_name, api_key)
    parent_uuid_remote = parent["uuid"]
    logger.info(f"Resolved parent {parent_name!r} -> uuid={parent_uuid_remote}")

    child = _dt_find_project_by_name(dt_url, project_name, api_key)
    child_parent_uuid = (child.get("parent") or {}).get("uuid")
    if child_parent_uuid != parent_uuid_remote:
        raise click.ClickException(
            f"DependencyTrack project {project_name!r} has parent UUID "
            f"{child_parent_uuid!r}, expected {parent_uuid_remote!r}"
        )
    child_uuid = child["uuid"]
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

    session = _make_session()
    try:
        _get_or_create_ef_project(session, ef_project_id)
        session.add(dt_project)
        if dry_run:
            logger.info("Dry-run: rolling back transaction")
            session.rollback()
        else:
            session.commit()
            logger.info("Committed DependencyTrack project")
    finally:
        session.close()


if __name__ == "__main__":
    cli()
