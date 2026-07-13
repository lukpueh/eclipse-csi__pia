"""Declarative sync of project authorizations from a curated file into the DB.

Reconciles the whole authorization state against a human-curated file (the single
source of truth): it creates new entries, updates changed ones, and deletes entries
that are no longer in the file.

The file is a list of Eclipse Foundation projects, each with a flat list of workload
URLs (GitHub repo or Jenkins issuer — the type is inferred from the host) and a list
of DependencyTrack (parent, project) mappings:

    projects:
      - id: technology.foo
        workloads:
          - https://github.com/eclipse-foo/repo
          - https://ci.eclipse.org/foo/oidc
        dependency_track:
          - parent: "Eclipse Foo"
            project: foo-server
"""

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import click
import requests
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    GITHUB_BASE_URL,
    JENKINS_ISSUER_BASE_URL,
    DependencyTrackProject,
    EclipseFoundationProject,
    GitHubWorkload,
    JenkinsWorkload,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Curated-file model
# --------------------------------------------------------------------------- #


class DtProjectSpec(BaseModel):
    """A DependencyTrack (root project, child project) mapping."""

    model_config = ConfigDict(extra="forbid")

    parent: str
    project: str


class ProjectSpec(BaseModel):
    """One Eclipse Foundation project and everything authorized for it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    workloads: list[str] = Field(default_factory=list)
    dependency_track: list[DtProjectSpec] = Field(default_factory=list)


class ProjectsFile(BaseModel):
    """Top-level curated file."""

    model_config = ConfigDict(extra="forbid")

    projects: list[ProjectSpec] = Field(default_factory=list)


def load_projects_file(path: str) -> ProjectsFile:
    """Parse and structurally validate the curated file at ``path``."""
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    if raw is None:
        raw = {}
    try:
        return ProjectsFile.model_validate(raw)
    except ValidationError as e:
        raise click.ClickException(f"Invalid projects file {path!r}:\n{e}") from e


def classify_workload_url(url: str) -> tuple[str, str, str]:
    """Classify a workload URL by host.

    Returns ``("github", repo_owner, repo_name)`` for a github.com repo URL or
    ``("jenkins", issuer, "")`` for a ci.eclipse.org issuer URL. Raises
    ``click.ClickException`` for anything else. No network access.
    """
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.hostname}"
    if base_url == GITHUB_BASE_URL:
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) != 2 or not all(path_parts):
            raise click.ClickException(f"GitHub URL must include owner/repo: {url}")
        return ("github", path_parts[0], path_parts[1])
    if base_url == JENKINS_ISSUER_BASE_URL:
        return ("jenkins", url, "")
    raise click.ClickException(
        f"URL must be a {GITHUB_BASE_URL} repo URL or a "
        f"{JENKINS_ISSUER_BASE_URL} issuer URL: {url}"
    )


def validate_projects_file(pf: ProjectsFile) -> None:
    """Semantic validation beyond structural parsing (no network access).

    Enforces: unique project ids; every workload URL is a valid GitHub/Jenkins URL
    and globally unique; DependencyTrack project names are unique within a project
    (the runtime resolves DT projects by ``(ef_project_id, name)``).
    """
    ids = [p.id for p in pf.projects]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise click.ClickException(f"Duplicate project id(s): {', '.join(dupes)}")

    seen_urls: set[str] = set()
    for p in pf.projects:
        for url in p.workloads:
            classify_workload_url(url)  # raises on invalid
            if url in seen_urls:
                raise click.ClickException(f"Duplicate workload URL: {url}")
            seen_urls.add(url)

        dt_names = [d.project for d in p.dependency_track]
        dt_dupes = sorted({n for n in dt_names if dt_names.count(n) > 1})
        if dt_dupes:
            raise click.ClickException(
                f"Duplicate DependencyTrack project name(s) in {p.id!r}: "
                f"{', '.join(dt_dupes)}"
            )


# --------------------------------------------------------------------------- #
# Resolution (external lookups)
# --------------------------------------------------------------------------- #


def fetch_github_owner_id(owner: str, token: str | None = None) -> str:
    """Resolve a GitHub owner login to its numeric id.

    An optional token is sent as a Bearer credential to lift the anonymous rate
    limit (only public read is needed for ``GET /users/{owner}``).
    """
    url = f"https://api.github.com/users/{owner}"
    logger.info(f"Fetching GitHub owner id from {url}")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    owner_id = str(response.json()["id"])
    logger.info(f"GitHub owner {owner!r} has id {owner_id}")
    return owner_id


def _dt_search_root_projects(
    dt_url: str, name: str, api_key: str
) -> list[dict[str, Any]]:
    """Return all root DependencyTrack projects with the given name."""
    url = f"{dt_url.rstrip('/')}/api/v1/project"
    logger.info(f"Querying DependencyTrack root projects at {url} for name={name!r}")
    response = requests.get(
        url,
        params={"name": name, "onlyRoot": "true"},
        headers={"X-Api-Key": api_key, "Accept": "application/json"},
    )
    response.raise_for_status()
    return response.json()


def _dt_create_project(
    dt_url: str, name: str, api_key: str, parent_uuid: str | None = None
) -> dict[str, Any]:
    """Create a DependencyTrack project (root if ``parent_uuid`` is None)."""
    where = f"under parent {parent_uuid}" if parent_uuid else "(root)"
    logger.info(f"Creating DependencyTrack project {name!r} {where}")
    body: dict[str, Any] = {"name": name}
    if parent_uuid:
        body["parent"] = {"uuid": parent_uuid}
    response = requests.put(
        f"{dt_url.rstrip('/')}/api/v1/project",
        json=body,
        headers={
            "X-Api-Key": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    response.raise_for_status()
    return response.json()


def _dt_pick_one(
    matches: list[dict[str, Any]], describe: str, create: bool
) -> dict[str, Any] | None:
    """Return the single match, or ``None`` when there are none and ``create`` is set.

    Raises on ambiguity (>1) always, and on zero matches unless ``create`` allows
    the caller to create the missing project. ``describe`` names the thing being
    resolved, e.g. ``"root DependencyTrack project named 'Foo'"``.
    """
    if len(matches) != 1 and not (create and not matches):
        raise click.ClickException(
            f"Expected exactly one {describe}, found {len(matches)}"
        )
    return matches[0] if matches else None


def resolve_dt_child_uuid(
    dt_url: str,
    parent_name: str,
    project_name: str,
    api_key: str,
    root_cache: dict[str, dict[str, Any]] | None = None,
    create: bool = False,
) -> str:
    """Resolve the UUID of child ``project_name`` under root ``parent_name``.

    ``root_cache`` (optional) memoises root-project lookups by name so a sync that
    reuses the same DT root across many mappings issues one request per root.

    When ``create`` is set, a missing root or child project is created rather than
    raising; this happens even under ``pia sync --dry-run``, which scopes to the
    PIA database only (DependencyTrack projects are a prerequisite the sync
    provisions eagerly). An *ambiguous* match (more than one) is always an error,
    even with ``create``.
    """
    # Resolve (or, with --create, provision) the root project, caching it so other
    # mappings that reuse this root neither re-query nor re-create it.
    parent = root_cache.get(parent_name) if root_cache is not None else None
    if parent is None:
        roots = _dt_search_root_projects(dt_url, parent_name, api_key)
        match = _dt_pick_one(
            roots, f"root DependencyTrack project named {parent_name!r}", create
        )
        parent = (
            match
            if match is not None
            else _dt_create_project(dt_url, parent_name, api_key)
        )
        parent.setdefault("children", [])
        if root_cache is not None:
            root_cache[parent_name] = parent

    # Resolve (or provision) the child under the resolved root.
    children = [c for c in parent["children"] if c.get("name") == project_name]
    match = _dt_pick_one(
        children, f"child named {project_name!r} under {parent_name!r}", create
    )
    if match is not None:
        return match["uuid"]

    child = _dt_create_project(
        dt_url, project_name, api_key, parent_uuid=parent["uuid"]
    )
    # Keep the cache consistent for other mappings that reuse this root.
    parent["children"].append({"name": project_name, "uuid": child["uuid"]})
    return child["uuid"]


# --------------------------------------------------------------------------- #
# DB state
# --------------------------------------------------------------------------- #

# Both the desired and the current state are expressed as ORM instances from
# models.py, held in a DB snapshot. The desired instances are transient
# (session-less): they carry only the resolved business fields from the curated
# file and DT/GH lookups, and their autoincrement PKs and polymorphic `type`
# discriminator are populated by SQLAlchemy on flush, once the objects that
# survive the diff are added to a session in apply_plan. The current instances
# are the session-attached rows loaded from the DB.
#
# Each dict is keyed by the row's `diff_key` — the tuple of all its business
# columns. Keying on the full column set (rather than a bare business key plus a
# field-by-field comparison) means the diff is a plain set difference over keys:
# an unchanged row has the same key on both sides and cancels, while a modified
# row has a different key on each side and so shows up as a delete of the old key
# plus a create of the new one (see _diff). ORM identity is never used, so the
# transient objects need no PK.


@dataclass
class DB:
    """A snapshot of the DB entities, keyed by ``diff_key`` for set-diffing.

    Used for both the desired state (built from the curated file by
    build_desired) and the current state (loaded from the DB by _load_current).
    """

    ef: dict[tuple[str, ...], EclipseFoundationProject] = field(default_factory=dict)
    github: dict[tuple[str, ...], GitHubWorkload] = field(default_factory=dict)
    jenkins: dict[tuple[str, ...], JenkinsWorkload] = field(default_factory=dict)
    dt: dict[tuple[str, ...], DependencyTrackProject] = field(default_factory=dict)


def build_desired(
    pf: ProjectsFile,
    dt_url: str,
    dt_api_key: str,
    github_token: str | None = None,
    create_dt_projects: bool = False,
) -> DB:
    """Resolve the curated file into a fully-populated desired state.

    Performs the external lookups (GitHub owner ids, DependencyTrack child UUIDs).
    ``dt_url`` and ``dt_api_key`` are required (the CLI validates their presence).
    When ``create_dt_projects`` is set, missing DependencyTrack root/child projects
    are created; this is independent of ``pia sync --dry-run``, which scopes only
    to the PIA database.
    """
    desired = DB()
    owner_id_cache: dict[str, str] = {}
    dt_root_cache: dict[str, dict[str, Any]] = {}

    for project in pf.projects:
        ef = EclipseFoundationProject(id=project.id)
        desired.ef[ef.diff_key] = ef

        for url in project.workloads:
            kind, a, b = classify_workload_url(url)
            if kind == "github":
                owner, repo = a, b
                if owner not in owner_id_cache:
                    owner_id_cache[owner] = fetch_github_owner_id(owner, github_token)
                gh = GitHubWorkload(
                    ef_project_id=project.id,
                    repo_owner=owner,
                    repo_name=repo,
                    repo_owner_id=owner_id_cache[owner],
                )
                desired.github[gh.diff_key] = gh
            else:
                issuer = a
                jk = JenkinsWorkload(ef_project_id=project.id, issuer=issuer)
                desired.jenkins[jk.diff_key] = jk

        for dt in project.dependency_track:
            child_uuid = resolve_dt_child_uuid(
                dt_url,
                dt.parent,
                dt.project,
                dt_api_key,
                dt_root_cache,
                create=create_dt_projects,
            )
            dtp = DependencyTrackProject(
                ef_project_id=project.id,
                name=dt.project,
                parent_uuid=child_uuid,
            )
            desired.dt[dtp.diff_key] = dtp

    return desired


# --------------------------------------------------------------------------- #
# Diff / plan
# --------------------------------------------------------------------------- #


@dataclass
class Plan:
    """A reconciliation plan: what to create and delete.

    Modifying a workload or DT project falls out of the diff as a delete of the
    current row plus a create of the desired one (their diff_keys differ), so
    there is no separate update bucket. apply_plan performs all deletes before
    all creates, which frees the old row's unique key before its replacement is
    inserted.
    """

    # Eclipse Foundation projects are kept in their own ef_create/ef_delete
    # lists, separate from the creates/deletes lists of child dt projects and
    # workloads, to assure foreign-key ordering: child rows reference the parent
    # via ef_project_id, so they must be deleted before their parents, and
    # vice-versa parents must be created before their children.

    ef_create: list[EclipseFoundationProject] = field(default_factory=list)
    ef_delete: list[EclipseFoundationProject] = field(default_factory=list)
    creates: list[Any] = field(default_factory=list)
    deletes: list[Any] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.ef_create or self.ef_delete or self.creates or self.deletes)


def _load_current(session: Session) -> DB:
    return DB(
        ef={
            p.diff_key: p
            for p in session.execute(select(EclipseFoundationProject)).scalars()
        },
        github={
            w.diff_key: w for w in session.execute(select(GitHubWorkload)).scalars()
        },
        jenkins={
            w.diff_key: w for w in session.execute(select(JenkinsWorkload)).scalars()
        },
        dt={
            d.diff_key: d
            for d in session.execute(select(DependencyTrackProject)).scalars()
        },
    )


def compute_plan(session: Session, desired: DB) -> Plan:
    """Diff the desired state against the current DB state. No writes.

    The diff is a set difference over each entity's diff_key: a key only in
    desired is a create, a key only in current is a delete, and a row whose
    business columns changed shows up as both (its diff_key differs on each
    side), i.e. a delete of the old row plus a create of the new one.
    """
    current = _load_current(session)
    plan = Plan()

    # Eclipse Foundation projects go in their own buckets so apply_plan can
    # honour foreign-key ordering (see Plan). Creates carry the transient desired
    # row, deletes the attached current one, so apply_plan neither reconstructs
    # nor re-fetches them.
    plan.ef_create = [
        desired.ef[k] for k in sorted(desired.ef.keys() - current.ef.keys())
    ]
    plan.ef_delete = [
        current.ef[k] for k in sorted(current.ef.keys() - desired.ef.keys())
    ]

    for cur, des in (
        (current.github, desired.github),
        (current.jenkins, desired.jenkins),
        (current.dt, desired.dt),
    ):
        plan.creates += [des[k] for k in sorted(des.keys() - cur.keys())]
        plan.deletes += [cur[k] for k in sorted(cur.keys() - des.keys())]

    return plan


def format_plan(plan: Plan) -> str:
    """Render a plan as a human-readable, reviewable block.

    Shows a create block then a delete block, each rendered from the rows'
    ``__repr__``. Within a block, Eclipse Foundation projects lead on create and
    trail on delete, mirroring apply_plan's foreign-key-safe ordering.
    """
    if plan.is_empty():
        return "Plan: no changes — database already matches the file."
    creates = [*plan.ef_create, *plan.creates]
    deletes = [*plan.deletes, *plan.ef_delete]
    out = [f"Plan: {len(creates)} to create, {len(deletes)} to delete"]
    if creates:
        out += ["", "Create:", *(f"  + {obj!r}" for obj in creates)]
    if deletes:
        out += ["", "Delete:", *(f"  - {obj!r}" for obj in deletes)]
    return "\n".join(out)


def apply_plan(session: Session, plan: Plan) -> None:
    """Apply a plan within the given session's transaction (no commit).

    Order matters against the foreign keys (which have no ON DELETE CASCADE):
    delete child rows, then empty projects; create projects before their children.
    Deleting (and flushing) before creating also frees unique keys, so a
    modification expressed as delete+create of the same business key does not
    collide with its own old row.
    """
    # 1. delete workload / DT child rows (ORM delete clears the joined base row too)
    for obj in plan.deletes:
        session.delete(obj)
    # 2. delete now-unreferenced Eclipse Foundation projects (attached current rows)
    for obj in plan.ef_delete:
        session.delete(obj)
    session.flush()
    # 3. create Eclipse Foundation projects before any child references them
    for obj in plan.ef_create:
        session.add(obj)
    session.flush()
    # 4. create child rows (old rows already deleted above, so keys are free)
    for obj in plan.creates:
        session.add(obj)
    session.flush()
