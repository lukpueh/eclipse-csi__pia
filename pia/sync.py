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

# Placeholder UUID used in the plan for a DependencyTrack project that --dry-run
# reports as "would be created" (with --create-dt-projects) but does not create.
DT_PENDING_UUID = "(to-be-created)"


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
    dry_run: bool = False,
) -> str:
    """Resolve the UUID of child ``project_name`` under root ``parent_name``.

    ``root_cache`` (optional) memoises root-project lookups by name so a sync that
    reuses the same DT root across many mappings issues one request per root.

    When ``create`` is set, a missing root or child project is created rather than
    raising — except under ``dry_run``, where nothing is created and the pending
    creation is logged and reported via the ``DT_PENDING_UUID`` sentinel. An
    *ambiguous* match (more than one) is always an error, even with ``create``.
    """
    child_note = (
        f"[dry-run] would create DependencyTrack project {project_name!r} "
        f"under {parent_name!r}"
    )

    # Resolve (or, with --create, provision) the root project, caching it so other
    # mappings that reuse this root neither re-query nor re-create it.
    parent = root_cache.get(parent_name) if root_cache is not None else None
    if parent is None:
        roots = _dt_search_root_projects(dt_url, parent_name, api_key)
        match = _dt_pick_one(
            roots, f"root DependencyTrack project named {parent_name!r}", create
        )
        if match is not None:
            parent = match
        elif dry_run:
            logger.info(
                f"[dry-run] would create DependencyTrack root project {parent_name!r}"
            )
            parent = {"uuid": None, "_pending": True}
        else:
            parent = _dt_create_project(dt_url, parent_name, api_key)
        parent.setdefault("children", [])
        if root_cache is not None:
            root_cache[parent_name] = parent

    # A pending root (dry-run) has no real UUID to parent a child lookup under.
    if parent.get("_pending"):
        logger.info(child_note)
        return DT_PENDING_UUID

    # Resolve (or provision) the child under the resolved root.
    children = [c for c in parent["children"] if c.get("name") == project_name]
    match = _dt_pick_one(
        children, f"child named {project_name!r} under {parent_name!r}", create
    )
    if match is not None:
        return match["uuid"]
    if dry_run:
        logger.info(child_note)
        return DT_PENDING_UUID

    child = _dt_create_project(
        dt_url, project_name, api_key, parent_uuid=parent["uuid"]
    )
    # Keep the cache consistent for other mappings that reuse this root.
    parent["children"].append({"name": project_name, "uuid": child["uuid"]})
    return child["uuid"]


# --------------------------------------------------------------------------- #
# Desired state
# --------------------------------------------------------------------------- #

# Below classes mirror the ORM models in models.py but deliberately omit db
# concerns (sessions, autoincrement PKs, etc.). They carry only the resolved
# fields from the curated input file and subsequent DT and GH API lookups. They
# are used to compute a diff to the current state, and are converted into
# actual ORM instances eventually.


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
    dt_url: str,
    dt_api_key: str,
    github_token: str | None = None,
    create_dt_projects: bool = False,
    dry_run: bool = False,
) -> Desired:
    """Resolve the curated file into a fully-populated desired state.

    Performs the external lookups (GitHub owner ids, DependencyTrack child UUIDs).
    ``dt_url`` and ``dt_api_key`` are required (the CLI validates their presence).
    When ``create_dt_projects`` is set, missing DependencyTrack root/child projects
    are created (or, under ``dry_run``, reported as pending without being created).
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
            child_uuid = resolve_dt_child_uuid(
                dt_url,
                dt.parent,
                dt.project,
                dt_api_key,
                dt_root_cache,
                create=create_dt_projects,
                dry_run=dry_run,
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
class Change:
    """One rendered plan entry, tagged with its owning project for grouping.

    ``format_plan`` groups changes under a per-project header, so ``body`` omits
    the owning project (an update keeps it inline only when ef_project_id itself
    changed, since that is carried in the change detail).
    """

    project: str  # grouping key: the project this entry belongs to
    kind: str  # "github" | "jenkins" | "dt"
    op: str  # "+" create | "~" update | "-" delete
    body: str  # entry text without the op prefix


@dataclass
class Plan:
    """A reconciliation plan: what to create, update, and delete."""

    # Eclipse Foundation projects are kept in their own ef_create/ef_delete
    # lists, separate from the creates/updates/deletes lists of child dt
    # projects and workloads, to assure foreign-key ordering: child rows
    # reference the parent via ef_project_id, so they must be deleted before
    # their parents, and vice-versa parents must be created before their
    # children. Note: There is no ef_update row, because their id is their
    # whole identity, so they can only be created or deleted.

    ef_create: list[str] = field(default_factory=list)
    ef_delete: list[str] = field(default_factory=list)
    creates: list[Any] = field(default_factory=list)
    updates: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)
    deletes: list[Any] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)  # rendered by format_plan

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

    # Eclipse Foundation projects. These become the per-project group headers in
    # format_plan, so they need no Change entry of their own.
    for ef_id in sorted(desired.ef_ids - ef_cur):
        plan.ef_create.append(ef_id)
    for ef_id in sorted(ef_cur - desired.ef_ids):
        plan.ef_delete.append(ef_id)

    # A change is grouped under its desired project (create/update) or, for a
    # deletion, the project the row currently belongs to.
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
            plan.changes.append(Change(dg.ef_project_id, "github", "+", gh_label))
        else:
            gh_changes: dict[str, Any] = {}
            if gh_row.ef_project_id != dg.ef_project_id:
                gh_changes["ef_project_id"] = dg.ef_project_id
            if gh_row.repo_owner_id != dg.repo_owner_id:
                gh_changes["repo_owner_id"] = dg.repo_owner_id
            if gh_changes:
                plan.updates.append((gh_row, gh_changes))
                plan.changes.append(
                    Change(
                        dg.ef_project_id,
                        "github",
                        "~",
                        f"{gh_label} ({_fmt_changes(gh_row, gh_changes)})",
                    )
                )
    for gh_key in sorted(gh_cur):
        if gh_key not in desired.github:
            gh_row = gh_cur[gh_key]
            plan.deletes.append(gh_row)
            plan.changes.append(
                Change(
                    gh_row.ef_project_id,
                    "github",
                    "-",
                    f"github {gh_row.repo_owner}/{gh_row.repo_name}",
                )
            )

    # Jenkins workloads (keyed by issuer)
    for issuer in sorted(desired.jenkins):
        dj = desired.jenkins[issuer]
        jk_row = jk_cur.get(issuer)
        if jk_row is None:
            plan.creates.append(
                JenkinsWorkload(ef_project_id=dj.ef_project_id, issuer=dj.issuer)
            )
            plan.changes.append(
                Change(dj.ef_project_id, "jenkins", "+", f"jenkins {issuer}")
            )
        elif jk_row.ef_project_id != dj.ef_project_id:
            jk_changes: dict[str, Any] = {"ef_project_id": dj.ef_project_id}
            plan.updates.append((jk_row, jk_changes))
            plan.changes.append(
                Change(
                    dj.ef_project_id,
                    "jenkins",
                    "~",
                    f"jenkins {issuer} ({_fmt_changes(jk_row, jk_changes)})",
                )
            )
    for issuer in sorted(jk_cur):
        if issuer not in desired.jenkins:
            jk_row = jk_cur[issuer]
            plan.deletes.append(jk_row)
            plan.changes.append(
                Change(jk_row.ef_project_id, "jenkins", "-", f"jenkins {issuer}")
            )

    # DependencyTrack projects (keyed by (ef_project_id, name)). The project is
    # part of the key, so a DT project never moves; only parent_uuid can change.
    for dt_key in sorted(desired.dt):
        dd = desired.dt[dt_key]
        dt_row = dt_cur.get(dt_key)
        if dt_row is None:
            plan.creates.append(
                DependencyTrackProject(
                    ef_project_id=dd.ef_project_id,
                    name=dd.name,
                    parent_uuid=dd.parent_uuid,
                )
            )
            plan.changes.append(
                Change(dd.ef_project_id, "dt", "+", f"dt {dd.name} -> {dd.parent_uuid}")
            )
        else:
            dt_changes: dict[str, Any] = {}
            # Ignore the dry-run "pending creation" sentinel: it is not a real
            # UUID, so it must not be recorded as a parent_uuid change.
            if (
                dd.parent_uuid != DT_PENDING_UUID
                and dt_row.parent_uuid != dd.parent_uuid
            ):
                dt_changes["parent_uuid"] = dd.parent_uuid
            if dt_changes:
                plan.updates.append((dt_row, dt_changes))
                plan.changes.append(
                    Change(
                        dd.ef_project_id,
                        "dt",
                        "~",
                        f"dt {dd.name} ({_fmt_changes(dt_row, dt_changes)})",
                    )
                )
    for dt_key in sorted(dt_cur):
        if dt_key not in desired.dt:
            dt_row = dt_cur[dt_key]
            plan.deletes.append(dt_row)
            plan.changes.append(
                Change(dt_row.ef_project_id, "dt", "-", f"dt {dt_row.name}")
            )

    return plan


def _fmt_changes(obj: Any, changes: dict[str, Any]) -> str:
    return ", ".join(f"{k}: {getattr(obj, k)!r} -> {v!r}" for k, v in changes.items())


# Sort order for entries within a project group: creates, then updates, then
# deletes; ties broken by entity kind and then the rendered text.
_OP_RANK = {"+": 0, "~": 1, "-": 2}
_KIND_RANK = {"github": 0, "jenkins": 1, "dt": 2}


def format_plan(plan: Plan) -> str:
    """Render a plan as a human-readable, reviewable block, grouped by project.

    Each project is a group: a header line (``+``/``-`` if the project itself is
    created/deleted, otherwise unmarked) followed by its indented entries, with a
    blank line between groups. Entries omit their owning project since the header
    carries it — except an update that moves a workload between projects, which
    shows the change inline via its ``ef_project_id`` detail.
    """
    if plan.is_empty():
        return "Plan: no changes — database already matches the file."

    n_create = len(plan.ef_create) + len(plan.creates)
    n_update = len(plan.updates)
    n_delete = len(plan.ef_delete) + len(plan.deletes)
    header = f"Plan: {n_create} to create, {n_update} to update, {n_delete} to delete"

    created, deleted = set(plan.ef_create), set(plan.ef_delete)
    by_project: dict[str, list[Change]] = {}
    for change in plan.changes:
        by_project.setdefault(change.project, []).append(change)

    blocks: list[str] = []
    for project in sorted(created | deleted | by_project.keys()):
        marker = "+" if project in created else "-" if project in deleted else " "
        lines = [f"{marker} project {project}"]
        for change in sorted(
            by_project.get(project, []),
            key=lambda c: (_OP_RANK[c.op], _KIND_RANK[c.kind], c.body),
        ):
            lines.append(f"    {change.op} {change.body}")
        blocks.append("\n".join(lines))

    return "\n".join([header, "", "\n\n".join(blocks)])


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
