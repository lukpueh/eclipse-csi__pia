"""Declarative sync of project authorizations from a curated file into the DB.

The imperative `add-workload` / `add-dt-project` commands append single rows. This
module reconciles the whole authorization state against a human-curated file (the
single source of truth): it creates new entries, updates changed ones, and deletes
entries that are no longer in the file.

The file is a list of Eclipse Foundation projects, each with a flat list of workload
URLs (GitHub repo or Jenkins issuer — the type is inferred from the host, exactly as
`add-workload` does) and a list of DependencyTrack (parent, project) mappings:

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
    """Classify a workload URL by host, mirroring ``add-workload``.

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
# Resolution (external lookups) — shared with the imperative CLI commands
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


def _dt_find_root_project_by_name(
    dt_url: str, name: str, api_key: str
) -> dict[str, Any]:
    """Look up a root DependencyTrack project by name, asserting exactly one match."""
    url = f"{dt_url.rstrip('/')}/api/v1/project"
    logger.info(f"Querying DependencyTrack root projects at {url} for name={name!r}")
    response = requests.get(
        url,
        params={"name": name, "onlyRoot": "true"},
        headers={"X-Api-Key": api_key, "Accept": "application/json"},
    )
    response.raise_for_status()
    projects = response.json()
    if len(projects) != 1:
        raise click.ClickException(
            f"Expected exactly one root DependencyTrack project named {name!r}, "
            f"found {len(projects)}"
        )
    return projects[0]


def resolve_dt_child_uuid(
    dt_url: str,
    parent_name: str,
    project_name: str,
    api_key: str,
    root_cache: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Resolve the UUID of child ``project_name`` under root ``parent_name``.

    ``root_cache`` (optional) memoises root-project lookups by name so a sync that
    reuses the same DT root across many mappings issues one request per root.
    """
    if root_cache is not None and parent_name in root_cache:
        parent = root_cache[parent_name]
    else:
        parent = _dt_find_root_project_by_name(dt_url, parent_name, api_key)
        if root_cache is not None:
            root_cache[parent_name] = parent

    children = [c for c in parent.get("children", []) if c.get("name") == project_name]
    if len(children) != 1:
        raise click.ClickException(
            f"Expected exactly one child named {project_name!r} under "
            f"{parent_name!r}, found {len(children)}"
        )
    return children[0]["uuid"]


# --------------------------------------------------------------------------- #
# Desired state
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DesiredGitHub:
    ef_project_id: str
    repo_owner: str
    repo_name: str
    repo_owner_id: str


@dataclass(frozen=True)
class DesiredJenkins:
    ef_project_id: str
    issuer: str


@dataclass(frozen=True)
class DesiredDt:
    ef_project_id: str
    name: str
    parent_uuid: str


@dataclass
class Desired:
    """Fully resolved target state, keyed for diffing."""

    ef_ids: set[str] = field(default_factory=set)
    # (repo_owner, repo_name) -> DesiredGitHub
    github: dict[tuple[str, str], DesiredGitHub] = field(default_factory=dict)
    # issuer -> DesiredJenkins
    jenkins: dict[str, DesiredJenkins] = field(default_factory=dict)
    # (ef_project_id, name) -> DesiredDt
    dt: dict[tuple[str, str], DesiredDt] = field(default_factory=dict)


def build_desired(
    pf: ProjectsFile,
    dt_url: str | None,
    dt_api_key: str | None,
    github_token: str | None = None,
) -> Desired:
    """Resolve the curated file into a fully-populated desired state.

    Performs the external lookups (GitHub owner ids, DependencyTrack child UUIDs).
    ``dt_url``/``dt_api_key`` are required only if any DependencyTrack mappings exist.
    """
    desired = Desired()
    owner_id_cache: dict[str, str] = {}
    dt_root_cache: dict[str, dict[str, Any]] = {}

    for project in pf.projects:
        desired.ef_ids.add(project.id)

        for url in project.workloads:
            kind, a, b = classify_workload_url(url)
            if kind == "github":
                owner, repo = a, b
                if owner not in owner_id_cache:
                    owner_id_cache[owner] = fetch_github_owner_id(owner, github_token)
                desired.github[(owner, repo)] = DesiredGitHub(
                    ef_project_id=project.id,
                    repo_owner=owner,
                    repo_name=repo,
                    repo_owner_id=owner_id_cache[owner],
                )
            else:
                issuer = a
                desired.jenkins[issuer] = DesiredJenkins(
                    ef_project_id=project.id, issuer=issuer
                )

        for dt in project.dependency_track:
            if not dt_url or not dt_api_key:
                raise click.ClickException(
                    "DependencyTrack mappings present but --dt-url / "
                    "PIA_DEPENDENCY_TRACK_API_KEY not provided"
                )
            child_uuid = resolve_dt_child_uuid(
                dt_url, dt.parent, dt.project, dt_api_key, dt_root_cache
            )
            desired.dt[(project.id, dt.project)] = DesiredDt(
                ef_project_id=project.id,
                name=dt.project,
                parent_uuid=child_uuid,
            )

    return desired


# --------------------------------------------------------------------------- #
# Diff / plan
# --------------------------------------------------------------------------- #


@dataclass
class Plan:
    """A reconciliation plan: what to create, update, and delete."""

    ef_create: list[str] = field(default_factory=list)
    ef_delete: list[str] = field(default_factory=list)
    creates: list[Any] = field(default_factory=list)  # ORM instances to add
    # (orm_obj, {field: new_value})
    updates: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)
    deletes: list[Any] = field(default_factory=list)  # ORM instances to delete
    lines: list[str] = field(default_factory=list)  # human-readable, in plan order

    def is_empty(self) -> bool:
        return not (
            self.ef_create
            or self.ef_delete
            or self.creates
            or self.updates
            or self.deletes
        )


def _load_current(session: Session):
    ef = {p.id for p in session.execute(select(EclipseFoundationProject)).scalars()}
    gh = {
        (w.repo_owner, w.repo_name): w
        for w in session.execute(select(GitHubWorkload)).scalars()
    }
    jk = {w.issuer: w for w in session.execute(select(JenkinsWorkload)).scalars()}
    dt = {
        (d.ef_project_id, d.name): d
        for d in session.execute(select(DependencyTrackProject)).scalars()
    }
    return ef, gh, jk, dt


def compute_plan(session: Session, desired: Desired) -> Plan:
    """Diff the desired state against the current DB state. No writes."""
    ef_cur, gh_cur, jk_cur, dt_cur = _load_current(session)
    plan = Plan()

    # Eclipse Foundation projects
    for ef_id in sorted(desired.ef_ids - ef_cur):
        plan.ef_create.append(ef_id)
        plan.lines.append(f"+ project {ef_id}")
    for ef_id in sorted(ef_cur - desired.ef_ids):
        plan.ef_delete.append(ef_id)
        plan.lines.append(f"- project {ef_id}")

    # GitHub workloads (keyed by repo identity)
    for gh_key in sorted(desired.github):
        dg = desired.github[gh_key]
        gh_row = gh_cur.get(gh_key)
        gh_label = f"github {dg.repo_owner}/{dg.repo_name}"
        if gh_row is None:
            plan.creates.append(
                GitHubWorkload(
                    ef_project_id=dg.ef_project_id,
                    repo_owner=dg.repo_owner,
                    repo_name=dg.repo_name,
                    repo_owner_id=dg.repo_owner_id,
                )
            )
            plan.lines.append(f"+ {gh_label} (project {dg.ef_project_id})")
        else:
            gh_changes: dict[str, Any] = {}
            if gh_row.ef_project_id != dg.ef_project_id:
                gh_changes["ef_project_id"] = dg.ef_project_id
            if gh_row.repo_owner_id != dg.repo_owner_id:
                gh_changes["repo_owner_id"] = dg.repo_owner_id
            if gh_changes:
                plan.updates.append((gh_row, gh_changes))
                plan.lines.append(f"~ {gh_label} ({_fmt_changes(gh_row, gh_changes)})")
    for gh_key in sorted(gh_cur):
        if gh_key not in desired.github:
            gh_row = gh_cur[gh_key]
            plan.deletes.append(gh_row)
            plan.lines.append(
                f"- github {gh_row.repo_owner}/{gh_row.repo_name} "
                f"(project {gh_row.ef_project_id})"
            )

    # Jenkins workloads (keyed by issuer)
    for issuer in sorted(desired.jenkins):
        dj = desired.jenkins[issuer]
        jk_row = jk_cur.get(issuer)
        if jk_row is None:
            plan.creates.append(
                JenkinsWorkload(ef_project_id=dj.ef_project_id, issuer=dj.issuer)
            )
            plan.lines.append(f"+ jenkins {issuer} (project {dj.ef_project_id})")
        elif jk_row.ef_project_id != dj.ef_project_id:
            jk_changes: dict[str, Any] = {"ef_project_id": dj.ef_project_id}
            plan.updates.append((jk_row, jk_changes))
            plan.lines.append(
                f"~ jenkins {issuer} ({_fmt_changes(jk_row, jk_changes)})"
            )
    for issuer in sorted(jk_cur):
        if issuer not in desired.jenkins:
            jk_row = jk_cur[issuer]
            plan.deletes.append(jk_row)
            plan.lines.append(f"- jenkins {issuer} (project {jk_row.ef_project_id})")

    # DependencyTrack projects (keyed by (ef_project_id, name))
    for dt_key in sorted(desired.dt):
        dd = desired.dt[dt_key]
        dt_row = dt_cur.get(dt_key)
        dt_label = f"dt {dd.ef_project_id}/{dd.name}"
        if dt_row is None:
            plan.creates.append(
                DependencyTrackProject(
                    ef_project_id=dd.ef_project_id,
                    name=dd.name,
                    parent_uuid=dd.parent_uuid,
                )
            )
            plan.lines.append(f"+ {dt_label} -> {dd.parent_uuid}")
        else:
            dt_changes: dict[str, Any] = {}
            if dt_row.parent_uuid != dd.parent_uuid:
                dt_changes["parent_uuid"] = dd.parent_uuid
            if dt_changes:
                plan.updates.append((dt_row, dt_changes))
                plan.lines.append(f"~ {dt_label} ({_fmt_changes(dt_row, dt_changes)})")
    for dt_key in sorted(dt_cur):
        if dt_key not in desired.dt:
            dt_row = dt_cur[dt_key]
            plan.deletes.append(dt_row)
            plan.lines.append(f"- dt {dt_row.ef_project_id}/{dt_row.name}")

    return plan


def _fmt_changes(obj: Any, changes: dict[str, Any]) -> str:
    return ", ".join(f"{k}: {getattr(obj, k)!r} -> {v!r}" for k, v in changes.items())


def format_plan(plan: Plan) -> str:
    """Render a plan as a human-readable, reviewable block."""
    if plan.is_empty():
        return "Plan: no changes — database already matches the file."
    n_create = len(plan.ef_create) + len(plan.creates)
    n_update = len(plan.updates)
    n_delete = len(plan.ef_delete) + len(plan.deletes)
    header = f"Plan: {n_create} to create, {n_update} to update, {n_delete} to delete"
    return "\n".join([header, *plan.lines])


def apply_plan(session: Session, plan: Plan) -> None:
    """Apply a plan within the given session's transaction (no commit).

    Order matters against the foreign keys (which have no ON DELETE CASCADE):
    delete child rows, then empty projects; create projects before their children.
    Deleting before creating also frees unique keys when an entry moves.
    """
    # 1. delete workload / DT child rows (ORM delete clears the joined base row too)
    for obj in plan.deletes:
        session.delete(obj)
    # 2. delete now-unreferenced Eclipse Foundation projects
    for ef_id in plan.ef_delete:
        obj = session.get(EclipseFoundationProject, ef_id)
        if obj is not None:
            session.delete(obj)
    session.flush()
    # 3. create Eclipse Foundation projects before any child references them
    for ef_id in plan.ef_create:
        session.add(EclipseFoundationProject(id=ef_id))
    session.flush()
    # 4. apply updates
    for obj, changes in plan.updates:
        for attr, value in changes.items():
            setattr(obj, attr, value)
    # 5. create child rows
    for obj in plan.creates:
        session.add(obj)
    session.flush()
