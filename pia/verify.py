"""Independent verifier that the PIA database matches the curated file.

This is a deliberate *cross-check* of ``pia sync``: it answers "does the database
actually reflect the file?" without trusting the sync implementation. To that end
it shares **no** logic with ``sync.py``'s reconcile machinery — it does not import
``build_desired``, ``compute_plan``, ``apply_plan`` or ``_diff_key``. It re-derives
the file → row mapping here (``_expected``), from scratch, and reads the database
with plain ``select`` statements (``_actual``). A bug can only slip past both sync
and this verifier if *both* implementations share it; keeping the two independent
is the whole point. The golden fixtures (see ``tests/fixtures/golden``) pin this
verifier's projection against human-authored expected rows, so the verifier itself
is anchored to a trusted oracle rather than to sync.

Two classes of invariant, with two different oracles:

* **Structural** (default, offline, no network): the set of ``(project, workload)``
  and ``(project, dt-name)`` rows in the database must equal the set derived from
  the file — checked in *both* directions, so a stale/orphaned row (a deletion the
  sync missed, or an out-of-band DB edit) is a discrepancy just as much as a
  missing one. The two externally-resolved fields (``repo_owner_id``,
  ``parent_uuid``) are projected away here and only sanity-checked for
  well-formedness (a GitHub owner id is always digits; a DT parent uuid is
  non-empty) — a wrong-but-plausible value is out of scope for the offline check.

* **Resolution** (opt-in, ``check_resolution=True``, hits GitHub + DependencyTrack):
  re-resolve each externally-derived field from its source of truth and compare to
  the stored value. ``repo_owner_id`` is re-fetched from GitHub; ``parent_uuid`` is
  re-resolved from DependencyTrack using the parent *name* recovered from the file
  (the name is not stored in the DB — only the resolved uuid is — so the file is
  the only place to get it back). These lookups are read-only: unlike sync, the
  verifier never creates anything.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests
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
from .sync import ProjectsFile

logger = logging.getLogger(__name__)

# A GitHub numeric account id is always a string of digits; anything else in
# repo_owner_id is corruption (e.g. a field-swap) detectable without the network.
_DIGITS = re.compile(r"^\d+$")


# --------------------------------------------------------------------------- #
# Report model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Discrepancy:
    """One way the database fails to match the file.

    ``kind`` is one of:
      * ``missing``    — the file expects a row that is not in the database
      * ``orphan``     — the database has a row the file does not expect
      * ``malformed``  — a matched row's externally-resolved field is ill-formed
      * ``resolution`` — a matched row's resolved field disagrees with its source
    """

    entity: str
    kind: str
    key: str
    detail: str = ""


@dataclass
class VerifyReport:
    """The outcome of a verification: an empty report means the DB matches."""

    discrepancies: list[Discrepancy] = field(default_factory=list)
    resolution_checked: bool = False

    def ok(self) -> bool:
        return not self.discrepancies

    def add(self, entity: str, kind: str, key: str, detail: str = "") -> None:
        self.discrepancies.append(Discrepancy(entity, kind, key, detail))

    def format(self) -> str:
        scope = (
            "structural + resolution"
            if self.resolution_checked
            else "structural (resolution not checked)"
        )
        if self.ok():
            return f"OK: database matches the file ({scope})."
        out = [f"MISMATCH: {len(self.discrepancies)} discrepancy(ies) [{scope}]", ""]
        for d in sorted(
            self.discrepancies, key=lambda d: (d.entity, d.kind, d.key)
        ):
            line = f"  [{d.kind}] {d.entity}: {d.key}"
            if d.detail:
                line += f" — {d.detail}"
            out.append(line)
        return "\n".join(out)


# --------------------------------------------------------------------------- #
# Independent file → expected-rows projection (no sync.py logic)
# --------------------------------------------------------------------------- #


def _classify(url: str) -> tuple[str, str, str]:
    """Classify a workload URL. Re-derived here, independent of sync.py.

    Returns ``("github", owner, repo)`` or ``("jenkins", issuer, "")``; raises
    ``ValueError`` on anything else. Kept separate from ``sync.classify_workload_url``
    on purpose so a bug in one cannot hide in the other.
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.hostname}"
    if base == GITHUB_BASE_URL:
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError(f"GitHub URL must include owner/repo: {url}")
        return ("github", parts[0], parts[1])
    if base == JENKINS_ISSUER_BASE_URL:
        return ("jenkins", url, "")
    raise ValueError(f"unrecognised workload URL: {url}")


@dataclass
class _Expected:
    """The file's rows, projected to structural keys plus resolution hints."""

    ef: set[str] = field(default_factory=set)
    # github/jenkins/dt map the structural key to the data needed to re-resolve:
    #   github[(ef, owner, repo)] = owner  (owner is what GitHub resolves to an id)
    #   dt[(ef, name)]            = parent_name  (DT resolves (parent, name) -> uuid)
    github: dict[tuple[str, str, str], str] = field(default_factory=dict)
    jenkins: set[tuple[str, str]] = field(default_factory=set)
    dt: dict[tuple[str, str], str] = field(default_factory=dict)


def _expected(pf: ProjectsFile) -> _Expected:
    exp = _Expected()
    for p in pf.projects:
        exp.ef.add(p.id)
        for url in p.workloads:
            kind, a, b = _classify(url)
            if kind == "github":
                exp.github[(p.id, a, b)] = a
            else:
                exp.jenkins.add((p.id, a))
        for d in p.dependency_track:
            exp.dt[(p.id, d.project)] = d.parent
    return exp


@dataclass
class _Actual:
    """The database's rows, keyed the same way, with resolved fields retained."""

    ef: set[str] = field(default_factory=set)
    github: dict[tuple[str, str, str], str] = field(default_factory=dict)
    jenkins: set[tuple[str, str]] = field(default_factory=set)
    dt: dict[tuple[str, str], str] = field(default_factory=dict)


def _actual(session: Session) -> _Actual:
    act = _Actual()
    act.ef = {
        r.id for r in session.execute(select(EclipseFoundationProject)).scalars()
    }
    for w in session.execute(select(GitHubWorkload)).scalars():
        act.github[(w.ef_project_id, w.repo_owner, w.repo_name)] = w.repo_owner_id
    act.jenkins = {
        (w.ef_project_id, w.issuer)
        for w in session.execute(select(JenkinsWorkload)).scalars()
    }
    for d in session.execute(select(DependencyTrackProject)).scalars():
        act.dt[(d.ef_project_id, d.name)] = d.parent_uuid
    return act


# --------------------------------------------------------------------------- #
# Independent, read-only resolvers (no sync.py logic, never creates)
# --------------------------------------------------------------------------- #


def _resolve_github_owner_id(owner: str, token: str | None) -> str:
    url = f"https://api.github.com/users/{owner}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return str(resp.json()["id"])


def _resolve_dt_uuid(
    dt_url: str, parent_name: str, project_name: str, api_key: str
) -> str:
    """Resolve the child uuid for ``(parent_name, project_name)``, read-only.

    Raises ``ValueError`` if the root or child is missing or ambiguous — the
    verifier surfaces that as a discrepancy rather than provisioning anything.
    """
    resp = requests.get(
        f"{dt_url.rstrip('/')}/api/v1/project",
        params={"name": parent_name, "onlyRoot": "true"},
        headers={"X-Api-Key": api_key, "Accept": "application/json"},
    )
    resp.raise_for_status()
    roots = resp.json()
    if len(roots) != 1:
        raise ValueError(
            f"expected exactly one root {parent_name!r}, found {len(roots)}"
        )
    children = [
        c for c in roots[0].get("children", []) if c.get("name") == project_name
    ]
    if len(children) != 1:
        raise ValueError(
            f"expected exactly one child {project_name!r} under "
            f"{parent_name!r}, found {len(children)}"
        )
    return children[0]["uuid"]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def verify_db(
    session: Session,
    pf: ProjectsFile,
    *,
    dt_url: str | None = None,
    dt_api_key: str | None = None,
    github_token: str | None = None,
    check_resolution: bool = False,
) -> VerifyReport:
    """Verify the database matches ``pf``. Never writes. See module docstring.

    With ``check_resolution`` set, ``dt_url`` and ``dt_api_key`` are required and
    the externally-resolved fields are re-checked against GitHub/DependencyTrack.
    """
    if check_resolution and not (dt_url and dt_api_key):
        raise ValueError("check_resolution requires dt_url and dt_api_key")

    exp = _expected(pf)
    act = _actual(session)
    report = VerifyReport(resolution_checked=check_resolution)

    # -- Structural: symmetric set difference on each entity's key. ------------
    # Spelled out per entity (rather than a loop over the three) because the keys
    # have different arities and a shared loop would union their types.
    def _diff(entity: str, expected_keys: set[Any], actual_keys: set[Any]) -> None:
        for key in sorted(expected_keys - actual_keys):
            report.add(entity, "missing", str(key))
        for key in sorted(actual_keys - expected_keys):
            report.add(entity, "orphan", str(key))

    _diff("eclipse_foundation_project", exp.ef, act.ef)
    _diff("github_workload", set(exp.github), set(act.github))
    _diff("jenkins_workload", exp.jenkins, act.jenkins)
    _diff("dependency_track_project", set(exp.dt), set(act.dt))

    # -- Well-formedness of resolved fields on rows present in both. -----------
    for gh_key in sorted(set(exp.github) & set(act.github)):
        owner_id = act.github[gh_key]
        if not owner_id or not _DIGITS.match(owner_id):
            report.add(
                "github_workload", "malformed", str(gh_key),
                f"repo_owner_id is not a numeric id: {owner_id!r}",
            )
    for dt_key in sorted(set(exp.dt) & set(act.dt)):
        if not act.dt[dt_key]:
            report.add(
                "dependency_track_project", "malformed", str(dt_key),
                "parent_uuid is empty",
            )

    if not check_resolution:
        return report

    # -- Resolution: re-resolve from the source of truth and compare. ----------
    # dt_url/dt_api_key are guaranteed non-None by the guard above.
    assert dt_url is not None and dt_api_key is not None
    owner_id_cache: dict[str, str] = {}
    for gh_key in sorted(set(exp.github) & set(act.github)):
        owner = exp.github[gh_key]
        try:
            if owner not in owner_id_cache:
                owner_id_cache[owner] = _resolve_github_owner_id(owner, github_token)
            resolved = owner_id_cache[owner]
        except requests.RequestException as e:
            report.add(
                "github_workload", "resolution", str(gh_key), f"lookup failed: {e}"
            )
            continue
        if resolved != act.github[gh_key]:
            report.add(
                "github_workload", "resolution", str(gh_key),
                f"stored repo_owner_id {act.github[gh_key]!r} != GitHub {resolved!r}",
            )

    for dt_key in sorted(set(exp.dt) & set(act.dt)):
        parent_name, project_name = exp.dt[dt_key], dt_key[1]
        try:
            resolved = _resolve_dt_uuid(
                dt_url, parent_name, project_name, dt_api_key
            )
        except (requests.RequestException, ValueError) as e:
            report.add(
                "dependency_track_project", "resolution", str(dt_key),
                f"lookup failed: {e}",
            )
            continue
        if resolved != act.dt[dt_key]:
            report.add(
                "dependency_track_project", "resolution", str(dt_key),
                f"stored parent_uuid {act.dt[dt_key]!r} != DT {resolved!r}",
            )

    return report
