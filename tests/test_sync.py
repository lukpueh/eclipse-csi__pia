"""Tests for the declarative `pia sync` command and reconcile logic."""

import textwrap

import click
import pytest
from click.testing import CliRunner

from pia import cli as cli_module
from pia import sync as sync_module
from pia.models import (
    DependencyTrackProject,
    EclipseFoundationProject,
    GitHubWorkload,
    JenkinsWorkload,
)
from pia.sync import (
    DB,
    DT_PENDING_UUID,
    DtProjectSpec,
    ProjectsFile,
    ProjectSpec,
    apply_plan,
    build_desired,
    classify_workload_url,
    compute_plan,
    load_projects_file,
    resolve_dt_child_uuid,
    validate_projects_file,
)


def _write(tmp_path, text: str) -> str:
    p = tmp_path / "projects.yaml"
    p.write_text(textwrap.dedent(text))
    return str(p)


def _resp(json_data):
    from unittest.mock import MagicMock

    r = MagicMock()
    r.json.return_value = json_data
    r.raise_for_status.return_value = None
    return r


# --------------------------------------------------------------------------- #
# File model + validation
# --------------------------------------------------------------------------- #


def test_classify_workload_url():
    assert classify_workload_url("https://github.com/owner/repo") == (
        "github",
        "owner",
        "repo",
    )
    kind, issuer, _ = classify_workload_url("https://ci.eclipse.org/foo/oidc")
    assert kind == "jenkins" and issuer == "https://ci.eclipse.org/foo/oidc"

    with pytest.raises(click.ClickException, match="owner/repo"):
        classify_workload_url("https://github.com/only-owner")
    with pytest.raises(click.ClickException, match="URL must be"):
        classify_workload_url("https://evil.example/owner/repo")


def test_load_and_validate_ok(tmp_path):
    f = _write(
        tmp_path,
        """
        projects:
          - id: eclipse-foo
            workloads:
              - https://github.com/eclipse-foo/repo
              - https://ci.eclipse.org/foo/oidc
            dependency_track:
              - parent: "Eclipse Foo"
                project: foo-server
        """,
    )
    pf = load_projects_file(f)
    validate_projects_file(pf)  # no raise
    assert [p.id for p in pf.projects] == ["eclipse-foo"]


def test_validate_rejects_duplicate_project_id(tmp_path):
    pf = load_projects_file(
        _write(
            tmp_path,
            """
            projects:
              - id: dup
              - id: dup
            """,
        )
    )
    with pytest.raises(click.ClickException, match="Duplicate project id"):
        validate_projects_file(pf)


def test_validate_rejects_duplicate_workload_url(tmp_path):
    pf = load_projects_file(
        _write(
            tmp_path,
            """
            projects:
              - id: a
                workloads: ["https://github.com/o/r"]
              - id: b
                workloads: ["https://github.com/o/r"]
            """,
        )
    )
    with pytest.raises(click.ClickException, match="Duplicate workload URL"):
        validate_projects_file(pf)


def test_validate_rejects_unknown_field(tmp_path):
    pf_path = _write(
        tmp_path,
        """
        projects:
          - id: a
            bogus: true
        """,
    )
    with pytest.raises(click.ClickException, match="Invalid projects file"):
        load_projects_file(pf_path)


# --------------------------------------------------------------------------- #
# Diff (compute_plan)
# --------------------------------------------------------------------------- #


def test_compute_plan_creates_on_empty_db(session):
    desired = DB(
        ef={"eclipse-foo": EclipseFoundationProject(id="eclipse-foo")},
        github={
            ("eclipse-foo", "repo"): GitHubWorkload(
                ef_project_id="eclipse-foo",
                repo_owner="eclipse-foo",
                repo_name="repo",
                repo_owner_id="7",
            )
        },
        jenkins={
            "https://ci.eclipse.org/foo/oidc": JenkinsWorkload(
                ef_project_id="eclipse-foo", issuer="https://ci.eclipse.org/foo/oidc"
            )
        },
        dt={
            ("eclipse-foo", "prod"): DependencyTrackProject(
                ef_project_id="eclipse-foo", name="prod", parent_uuid="uuidX"
            )
        },
    )
    plan = compute_plan(session, desired)
    assert [p.id for p in plan.ef_create] == ["eclipse-foo"]
    assert len(plan.creates) == 3
    assert not plan.deletes
    assert not plan.ef_delete


def test_compute_plan_noop_when_matching(seed_db):
    session = seed_db
    desired = _desired_matching_seed()
    plan = compute_plan(session, desired)
    assert plan.is_empty()


def test_compute_plan_update_is_delete_plus_create(seed_db):
    session = seed_db
    desired = _desired_matching_seed()
    # Change the resolved owner id for the existing GitHub workload.
    desired.github[("eclipse-test", "repo")] = GitHubWorkload(
        ef_project_id="eclipse-test",
        repo_owner="eclipse-test",
        repo_name="repo",
        repo_owner_id="99",
    )
    plan = compute_plan(session, desired)
    # A modification is expressed as delete of the old row + create of the new.
    assert not plan.ef_create and not plan.ef_delete
    assert len(plan.deletes) == 1 and len(plan.creates) == 1
    assert isinstance(plan.deletes[0], GitHubWorkload)
    assert plan.deletes[0].repo_owner_id == "42"
    assert isinstance(plan.creates[0], GitHubWorkload)
    assert plan.creates[0].repo_owner_id == "99"


def test_compute_plan_deletes_removed(seed_db):
    session = seed_db
    # Keep only eclipse-test's GitHub workload + DT project; drop everything else.
    desired = DB(
        ef={"eclipse-test": EclipseFoundationProject(id="eclipse-test")},
        github={
            ("eclipse-test", "repo"): GitHubWorkload(
                ef_project_id="eclipse-test",
                repo_owner="eclipse-test",
                repo_name="repo",
                repo_owner_id="42",
            )
        },
        dt={
            ("eclipse-test", "test-product"): DependencyTrackProject(
                ef_project_id="eclipse-test", name="test-product", parent_uuid="uuid-1"
            )
        },
    )
    plan = compute_plan(session, desired)
    assert [p.id for p in plan.ef_delete] == ["eclipse-other"]
    deleted_kinds = sorted(type(o).__name__ for o in plan.deletes)
    assert deleted_kinds == ["DependencyTrackProject", "JenkinsWorkload"]
    assert not plan.creates


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #


def test_apply_creates_then_idempotent(session):
    desired = DB(
        ef={"p": EclipseFoundationProject(id="p")},
        github={
            ("o", "r"): GitHubWorkload(
                ef_project_id="p", repo_owner="o", repo_name="r", repo_owner_id="1"
            )
        },
    )
    apply_plan(session, compute_plan(session, desired))
    session.commit()

    assert session.query(GitHubWorkload).count() == 1
    assert session.query(EclipseFoundationProject).count() == 1
    # Re-running against the now-populated DB is a no-op.
    assert compute_plan(session, desired).is_empty()


def test_apply_update_replaces_row(seed_db):
    # An update is delete+create of the same business key; applying it must not
    # collide with the old row's unique key and must leave exactly one new row.
    session = seed_db
    desired = _desired_matching_seed()
    desired.github[("eclipse-test", "repo")] = GitHubWorkload(
        ef_project_id="eclipse-test",
        repo_owner="eclipse-test",
        repo_name="repo",
        repo_owner_id="99",
    )
    apply_plan(session, compute_plan(session, desired))
    session.commit()

    rows = session.query(GitHubWorkload).all()
    assert len(rows) == 1
    assert rows[0].repo_owner_id == "99"


def test_apply_deletes_children_before_project(seed_db):
    session = seed_db
    # Reconcile to a file that no longer contains eclipse-other at all.
    desired = DB(
        ef={"eclipse-test": EclipseFoundationProject(id="eclipse-test")},
        github={
            ("eclipse-test", "repo"): GitHubWorkload(
                ef_project_id="eclipse-test",
                repo_owner="eclipse-test",
                repo_name="repo",
                repo_owner_id="42",
            )
        },
        dt={
            ("eclipse-test", "test-product"): DependencyTrackProject(
                ef_project_id="eclipse-test", name="test-product", parent_uuid="uuid-1"
            )
        },
    )
    apply_plan(session, compute_plan(session, desired))
    session.commit()

    assert session.query(JenkinsWorkload).count() == 0
    assert (
        session.query(EclipseFoundationProject).filter_by(id="eclipse-other").count()
        == 0
    )
    assert session.query(DependencyTrackProject).count() == 1
    assert session.query(GitHubWorkload).count() == 1


def _desired_matching_seed() -> DB:
    """A DB that exactly mirrors the `seed_db` fixture contents."""
    return DB(
        ef={
            i: EclipseFoundationProject(id=i)
            for i in ("eclipse-test", "eclipse-other")
        },
        github={
            ("eclipse-test", "repo"): GitHubWorkload(
                ef_project_id="eclipse-test",
                repo_owner="eclipse-test",
                repo_name="repo",
                repo_owner_id="42",
            )
        },
        jenkins={
            "https://ci.eclipse.org/eclipse-other/oidc": JenkinsWorkload(
                ef_project_id="eclipse-other",
                issuer="https://ci.eclipse.org/eclipse-other/oidc",
            )
        },
        dt={
            ("eclipse-test", "test-product"): DependencyTrackProject(
                ef_project_id="eclipse-test", name="test-product", parent_uuid="uuid-1"
            ),
            ("eclipse-other", "other-product"): DependencyTrackProject(
                ef_project_id="eclipse-other", name="other-product", parent_uuid="uuid-2"
            ),
        },
    )


# --------------------------------------------------------------------------- #
# CLI-level
# --------------------------------------------------------------------------- #


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def patch_cli(session_factory, monkeypatch):
    """Point the CLI at the in-memory DB and stub the network resolvers."""
    monkeypatch.setenv("PIA_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("PIA_DEPENDENCY_TRACK_API_KEY", "test-key")
    monkeypatch.setattr(cli_module, "_make_session", session_factory)
    monkeypatch.setattr(
        sync_module, "fetch_github_owner_id", lambda owner, token=None: "42"
    )
    monkeypatch.setattr(
        sync_module,
        "resolve_dt_child_uuid",
        lambda dt_url,
        parent,
        project,
        api_key,
        root_cache=None,
        create=False,
        dry_run=False: "uuid-1",
    )


def test_sync_check_is_offline(runner, tmp_path, monkeypatch):
    # No DB URL and no network are needed for --check.
    monkeypatch.delenv("PIA_DATABASE_URL", raising=False)

    def no_network(*a, **kw):
        raise AssertionError("network must not be touched for --check")

    monkeypatch.setattr(sync_module.requests, "get", no_network)

    f = _write(
        tmp_path,
        """
        projects:
          - id: eclipse-foo
            workloads: ["https://github.com/eclipse-foo/repo"]
        """,
    )
    result = runner.invoke(cli_module.cli, ["sync", f, "--check"])
    assert result.exit_code == 0, result.output
    assert "valid" in result.output


def test_sync_requires_dt_config_even_without_dt_entries(
    runner, tmp_path, session_factory, patch_cli
):
    # DT config is required unconditionally, even for a file with no
    # dependency_track entries. Here --dt-url is omitted, so sync must refuse.
    f = _write(
        tmp_path,
        """
        projects:
          - id: eclipse-foo
            workloads: ["https://github.com/eclipse-foo/repo"]
        """,
    )
    result = runner.invoke(cli_module.cli, ["sync", f])
    assert result.exit_code != 0
    assert "--dt-url and PIA_DEPENDENCY_TRACK_API_KEY are required" in result.output


def test_sync_dry_run_writes_nothing(runner, tmp_path, session_factory, patch_cli):
    f = _write(
        tmp_path,
        """
        projects:
          - id: eclipse-foo
            workloads: ["https://github.com/eclipse-foo/repo"]
            dependency_track:
              - parent: "Eclipse Foo"
                project: foo-server
        """,
    )
    result = runner.invoke(
        cli_module.cli, ["sync", f, "--dt-url", "https://dt", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Plan:" in result.output
    with session_factory() as s:
        assert s.query(GitHubWorkload).count() == 0
        assert s.query(EclipseFoundationProject).count() == 0


def test_sync_apply_creates_rows(runner, tmp_path, session_factory, patch_cli):
    f = _write(
        tmp_path,
        """
        projects:
          - id: eclipse-foo
            workloads: ["https://github.com/eclipse-foo/repo"]
            dependency_track:
              - parent: "Eclipse Foo"
                project: foo-server
        """,
    )
    result = runner.invoke(cli_module.cli, ["sync", f, "--dt-url", "https://dt"])
    assert result.exit_code == 0, result.output
    with session_factory() as s:
        assert s.query(GitHubWorkload).count() == 1
        assert s.query(DependencyTrackProject).count() == 1
        assert s.query(EclipseFoundationProject).count() == 1


def test_sync_deletions_require_allow_flag(
    runner, tmp_path, session_factory, patch_cli
):
    # Seed rows that the file below will no longer contain.
    with session_factory() as s:
        s.add_all(
            [
                EclipseFoundationProject(id="eclipse-foo"),
                EclipseFoundationProject(id="eclipse-stale"),
                JenkinsWorkload(
                    ef_project_id="eclipse-stale",
                    issuer="https://ci.eclipse.org/stale/oidc",
                ),
            ]
        )
        s.commit()

    f = _write(
        tmp_path,
        """
        projects:
          - id: eclipse-foo
            workloads: ["https://github.com/eclipse-foo/repo"]
        """,
    )

    # Without --allow-db-deletions, a plan with deletions is refused and
    # nothing changes.
    result = runner.invoke(cli_module.cli, ["sync", f, "--dt-url", "https://dt"])
    assert result.exit_code != 0
    assert "deletions" in result.output
    with session_factory() as s:
        assert s.query(JenkinsWorkload).count() == 1
        assert (
            s.query(EclipseFoundationProject).filter_by(id="eclipse-stale").count() == 1
        )

    # With --allow-db-deletions, the stale workload and now-empty project are removed.
    result = runner.invoke(
        cli_module.cli, ["sync", f, "--dt-url", "https://dt", "--allow-db-deletions"]
    )
    assert result.exit_code == 0, result.output
    with session_factory() as s:
        assert s.query(JenkinsWorkload).count() == 0
        assert (
            s.query(EclipseFoundationProject).filter_by(id="eclipse-stale").count() == 0
        )
        assert s.query(GitHubWorkload).count() == 1


# --------------------------------------------------------------------------- #
# DependencyTrack project creation (--create-dt-projects)
# --------------------------------------------------------------------------- #


def test_resolve_dt_missing_without_create_raises(monkeypatch):
    monkeypatch.setattr(sync_module.requests, "get", lambda *a, **k: _resp([]))
    with pytest.raises(click.ClickException, match="Expected exactly one root"):
        resolve_dt_child_uuid("http://dt", "Root", "Child", "key", create=False)


def test_resolve_dt_creates_missing_root_and_child(monkeypatch):
    puts = []

    def fake_get(*a, **k):
        return _resp([])  # no root exists

    def fake_put(url, json=None, **k):
        puts.append((json["name"], json.get("parent")))
        return _resp({"uuid": f"uuid-{json['name']}", "name": json["name"]})

    monkeypatch.setattr(sync_module.requests, "get", fake_get)
    monkeypatch.setattr(sync_module.requests, "put", fake_put)

    uuid = resolve_dt_child_uuid("http://dt", "Root", "Child", "key", {}, create=True)

    assert uuid == "uuid-Child"
    # Root created first (no parent), then child under the new root.
    assert puts[0] == ("Root", None)
    assert puts[1] == ("Child", {"uuid": "uuid-Root"})


def test_resolve_dt_creates_only_missing_child(monkeypatch):
    puts = []
    monkeypatch.setattr(
        sync_module.requests,
        "get",
        lambda *a, **k: _resp([{"name": "Root", "uuid": "root-uuid", "children": []}]),
    )

    def fake_put(url, json=None, **k):
        puts.append(json)
        return _resp({"uuid": "child-uuid", "name": json["name"]})

    monkeypatch.setattr(sync_module.requests, "put", fake_put)

    uuid = resolve_dt_child_uuid("http://dt", "Root", "Child", "key", {}, create=True)

    assert uuid == "child-uuid"
    assert len(puts) == 1
    assert puts[0]["parent"] == {"uuid": "root-uuid"}


def test_resolve_dt_ambiguous_root_errors_even_with_create(monkeypatch):
    monkeypatch.setattr(
        sync_module.requests,
        "get",
        lambda *a, **k: _resp(
            [
                {"name": "Root", "uuid": "1", "children": []},
                {"name": "Root", "uuid": "2", "children": []},
            ]
        ),
    )
    with pytest.raises(click.ClickException, match="Expected exactly one root"):
        resolve_dt_child_uuid("http://dt", "Root", "Child", "key", create=True)


def test_resolve_dt_dry_run_reports_pending_without_creating(monkeypatch):
    monkeypatch.setattr(sync_module.requests, "get", lambda *a, **k: _resp([]))

    def fail_put(*a, **k):
        raise AssertionError("must not create projects under --dry-run")

    monkeypatch.setattr(sync_module.requests, "put", fail_put)

    uuid = resolve_dt_child_uuid(
        "http://dt", "Root", "Child", "key", {}, create=True, dry_run=True
    )
    assert uuid == DT_PENDING_UUID


def test_build_desired_creates_dt_projects(monkeypatch):
    monkeypatch.setattr(
        sync_module, "fetch_github_owner_id", lambda owner, token=None: "1"
    )
    puts = []
    monkeypatch.setattr(sync_module.requests, "get", lambda *a, **k: _resp([]))

    def fake_put(url, json=None, **k):
        puts.append(json["name"])
        return _resp({"uuid": f"uuid-{json['name']}", "name": json["name"]})

    monkeypatch.setattr(sync_module.requests, "put", fake_put)

    pf = ProjectsFile(
        projects=[
            ProjectSpec(
                id="p",
                dependency_track=[DtProjectSpec(parent="Root", project="Child")],
            )
        ]
    )
    desired = build_desired(pf, "http://dt", "key", create_dt_projects=True)

    assert desired.dt[("p", "Child")].parent_uuid == "uuid-Child"
    assert puts == ["Root", "Child"]


def test_build_desired_dry_run_marks_dt_pending(monkeypatch):
    monkeypatch.setattr(sync_module.requests, "get", lambda *a, **k: _resp([]))

    def fail_put(*a, **k):
        raise AssertionError("must not create projects under --dry-run")

    monkeypatch.setattr(sync_module.requests, "put", fail_put)

    pf = ProjectsFile(
        projects=[
            ProjectSpec(
                id="p",
                dependency_track=[DtProjectSpec(parent="Root", project="Child")],
            )
        ]
    )
    desired = build_desired(
        pf, "http://dt", "key", create_dt_projects=True, dry_run=True
    )
    assert desired.dt[("p", "Child")].parent_uuid == DT_PENDING_UUID
