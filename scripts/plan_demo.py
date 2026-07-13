#!/usr/bin/env python3
"""Print an exemplary sync plan to review the diff UX.

Seeds an in-memory SQLite DB with a "current" state, builds a "desired" state by
hand (no network / DependencyTrack calls needed), then runs the real
compute_plan + format_plan so the output is exactly what `pia sync` would print.
Exercises every entry type across Eclipse Foundation projects, GitHub, Jenkins
and DependencyTrack. A modification is expressed as a delete of the old row plus
a create of the new one (their diff_keys differ); the demo also covers a workload
moving between projects and the empty plan.

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
    DB,
    Plan,
    _diff_key,
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


def desired_target() -> DB:
    """The state the curated file wants (normally produced by build_desired)."""

    def by_key(objs):
        return {_diff_key(o): o for o in objs}

    return DB(
        # technology.legacy dropped -> delete; technology.bar added -> create.
        ef=by_key(
            [
                EclipseFoundationProject(id="technology.foo"),
                EclipseFoundationProject(id="technology.bar"),
            ]
        ),
        github=by_key(
            [
                # owner_id 111 -> 222 => diff_key differs => delete + create
                GitHubWorkload(
                    ef_project_id="technology.foo",
                    repo_owner="eclipse-foo",
                    repo_name="website",
                    repo_owner_id="222",
                ),
                # brand new => create
                GitHubWorkload(
                    ef_project_id="technology.bar",
                    repo_owner="eclipse-bar",
                    repo_name="app",
                    repo_owner_id="333",
                ),
            ]
        ),
        jenkins=by_key(
            [
                # same issuer, project foo -> bar => delete + create (moves projects)
                JenkinsWorkload(
                    ef_project_id="technology.bar",
                    issuer="https://ci.eclipse.org/foo/oidc",
                ),
                # brand new => create
                JenkinsWorkload(
                    ef_project_id="technology.bar",
                    issuer="https://ci.eclipse.org/bar/oidc",
                ),
            ]
        ),
        dt=by_key(
            [
                # parent_uuid uuid-old -> uuid-new => delete + create
                DependencyTrackProject(
                    ef_project_id="technology.foo",
                    name="scanner",
                    parent_uuid="uuid-new",
                ),
                # brand new, resolved uuid => create
                DependencyTrackProject(
                    ef_project_id="technology.bar",
                    name="dashboard",
                    parent_uuid="uuid-bar-dashboard",
                ),
            ]
        ),
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
