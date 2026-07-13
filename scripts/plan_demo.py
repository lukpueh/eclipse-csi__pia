#!/usr/bin/env python3
"""Print an exemplary sync plan to review the diff UX.

Seeds an in-memory SQLite DB with a "current" state, builds a "desired" state by
hand (no network / DependencyTrack calls needed), then runs the real
compute_plan + format_plan so the output is exactly what `pia sync` would print.
Exercises every entry type across Eclipse Foundation projects, GitHub, Jenkins
and DependencyTrack. A modification is expressed as a delete of the old row plus
a create of the new one, so it shows as an adjacent -/+ pair; the demo also
covers a workload moving between projects, the dry-run "(to-be-created)"
sentinel, and the empty plan.

Usage:
    uv run python scripts/plan_demo.py

Tweak seed_current() (the DB state) and desired_target() (the file state) to
explore other diffs.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from pia.models import (
    Base,
    DependencyTrackProject,
    EclipseFoundationProject,
    GitHubWorkload,
    JenkinsWorkload,
)
from pia.sync import (
    DT_PENDING_UUID,
    Desired,
    DesiredDt,
    DesiredGitHub,
    DesiredJenkins,
    Plan,
    compute_plan,
    format_plan,
)


def seed_current(session) -> None:
    """The state currently in the database."""
    session.add_all(
        [
            EclipseFoundationProject(id="technology.foo"),
            EclipseFoundationProject(id="technology.legacy"),
            # GitHub: 'website' stays (owner_id will change), 'oldsite' is removed.
            GitHubWorkload(
                ef_project_id="technology.foo",
                repo_owner="eclipse-foo",
                repo_name="website",
                repo_owner_id="111",
            ),
            GitHubWorkload(
                ef_project_id="technology.legacy",
                repo_owner="eclipse-legacy",
                repo_name="oldsite",
                repo_owner_id="900",
            ),
            # Jenkins: 'foo' issuer stays (moves project), 'legacy' issuer removed.
            JenkinsWorkload(
                ef_project_id="technology.foo",
                issuer="https://ci.eclipse.org/foo/oidc",
            ),
            JenkinsWorkload(
                ef_project_id="technology.legacy",
                issuer="https://ci.eclipse.org/legacy/oidc",
            ),
            # DT: 'scanner' stays (parent_uuid changes), 'oldproduct' removed.
            DependencyTrackProject(
                ef_project_id="technology.foo",
                name="scanner",
                parent_uuid="uuid-old",
            ),
            DependencyTrackProject(
                ef_project_id="technology.legacy",
                name="oldproduct",
                parent_uuid="uuid-leg",
            ),
        ]
    )
    session.commit()


def desired_target() -> Desired:
    """The state the curated file wants (normally produced by build_desired)."""
    return Desired(
        # technology.legacy dropped -> delete; technology.bar added -> create.
        ef_ids={"technology.foo", "technology.bar"},
        github={
            # unchanged key, owner_id 111 -> 222  => delete + create
            ("eclipse-foo", "website"): DesiredGitHub(
                ef_project_id="technology.foo",
                repo_owner="eclipse-foo",
                repo_name="website",
                repo_owner_id="222",
            ),
            # brand new => create
            ("eclipse-bar", "app"): DesiredGitHub(
                ef_project_id="technology.bar",
                repo_owner="eclipse-bar",
                repo_name="app",
                repo_owner_id="333",
            ),
        },
        jenkins={
            # same issuer, project foo -> bar  => delete + create (moves projects)
            "https://ci.eclipse.org/foo/oidc": DesiredJenkins(
                ef_project_id="technology.bar",
                issuer="https://ci.eclipse.org/foo/oidc",
            ),
            # brand new => create
            "https://ci.eclipse.org/bar/oidc": DesiredJenkins(
                ef_project_id="technology.bar",
                issuer="https://ci.eclipse.org/bar/oidc",
            ),
        },
        dt={
            # parent_uuid uuid-old -> uuid-new  => delete + create
            ("technology.foo", "scanner"): DesiredDt(
                ef_project_id="technology.foo",
                name="scanner",
                parent_uuid="uuid-new",
            ),
            # brand new, resolved uuid => create
            ("technology.bar", "dashboard"): DesiredDt(
                ef_project_id="technology.bar",
                name="dashboard",
                parent_uuid="uuid-bar-dashboard",
            ),
            # brand new, dry-run "would create" sentinel => create shown as pending
            ("technology.bar", "pending-svc"): DesiredDt(
                ef_project_id="technology.bar",
                name="pending-svc",
                parent_uuid=DT_PENDING_UUID,
            ),
        },
    )


def main() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    seed_current(session)
    plan = compute_plan(session, desired_target())

    print("=" * 70)
    print("Exemplary plan (create / delete; a modification is delete + create)")
    print("=" * 70)
    print(format_plan(plan))

    print()
    print("=" * 70)
    print("Empty plan (database already matches the file)")
    print("=" * 70)
    print(format_plan(Plan()))


if __name__ == "__main__":
    main()
