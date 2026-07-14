"""Tests for the independent `pia verify` cross-check.

Two layers here:

* **Golden fixtures** (``fixtures/golden/*.yaml`` + ``*.expected.json``) pin the
  verifier's file → rows projection against *human-authored* expected rows. This is
  the trust anchor: it checks the verifier reads the file the way a person reading
  the spec would, independently of both `pia sync` and the verifier's own DB reads.
* **Discrepancy-detection tests** seed a DB from the golden expected rows, mutate
  it, and assert the verifier reports exactly the right discrepancy class.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from pia import cli as cli_module
from pia.models import (
    DependencyTrackProject,
    EclipseFoundationProject,
    GitHubWorkload,
    JenkinsWorkload,
)
from pia.sync import load_projects_file
from pia.verify import _expected, verify_db

GOLDEN = Path(__file__).parent / "fixtures" / "golden"
FIXTURES = sorted(p.stem for p in GOLDEN.glob("*.yaml"))


def _load(name: str):
    pf = load_projects_file(str(GOLDEN / f"{name}.yaml"))
    expected = json.loads((GOLDEN / f"{name}.expected.json").read_text())
    return pf, expected


def _seed(session, expected) -> None:
    """Seed the DB from the human-authored expected rows (FK-safe order)."""
    for pid in expected["eclipse_foundation_projects"]:
        session.add(EclipseFoundationProject(id=pid))
    session.flush()
    for w in expected["github_workloads"]:
        session.add(
            GitHubWorkload(
                ef_project_id=w["ef_project_id"],
                repo_owner=w["repo_owner"],
                repo_name=w["repo_name"],
                repo_owner_id=w["repo_owner_id"],
            )
        )
    for w in expected["jenkins_workloads"]:
        session.add(
            JenkinsWorkload(ef_project_id=w["ef_project_id"], issuer=w["issuer"])
        )
    for d in expected["dependency_track_projects"]:
        session.add(
            DependencyTrackProject(
                ef_project_id=d["ef_project_id"],
                name=d["name"],
                parent_uuid=d["parent_uuid"],
            )
        )
    session.commit()


def _resp(json_data):
    r = MagicMock()
    r.json.return_value = json_data
    r.raise_for_status.return_value = None
    return r


# --------------------------------------------------------------------------- #
# Golden: the verifier's projection matches the human-authored oracle
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", FIXTURES)
def test_projection_matches_golden(name):
    """`_expected` (independent of sync) must equal the hand-written rows."""
    pf, expected = _load(name)
    exp = _expected(pf)

    assert exp.ef == set(expected["eclipse_foundation_projects"])
    assert set(exp.github) == {
        (g["ef_project_id"], g["repo_owner"], g["repo_name"])
        for g in expected["github_workloads"]
    }
    assert exp.jenkins == {
        (j["ef_project_id"], j["issuer"]) for j in expected["jenkins_workloads"]
    }
    assert set(exp.dt) == {
        (d["ef_project_id"], d["name"])
        for d in expected["dependency_track_projects"]
    }
    # Resolution hints (owner login / parent name) recovered for --check-resolution.
    for g in expected["github_workloads"]:
        key = (g["ef_project_id"], g["repo_owner"], g["repo_name"])
        assert exp.github[key] == g["resolves"]["owner"]
    for d in expected["dependency_track_projects"]:
        assert exp.dt[(d["ef_project_id"], d["name"])] == d["resolves"]["parent"]


@pytest.mark.parametrize("name", FIXTURES)
def test_verify_ok_when_db_matches_golden(name, session):
    pf, expected = _load(name)
    _seed(session, expected)
    report = verify_db(session, pf)
    assert report.ok(), report.format()


# --------------------------------------------------------------------------- #
# Structural discrepancy detection (offline)
# --------------------------------------------------------------------------- #


def test_detects_missing_row(session):
    pf, expected = _load("simple")
    _seed(session, expected)
    # Drop the github workload the file expects.
    session.query(GitHubWorkload).delete()
    session.commit()

    report = verify_db(session, pf)
    assert not report.ok()
    kinds = {(d.entity, d.kind) for d in report.discrepancies}
    assert ("github_workload", "missing") in kinds


def test_detects_orphan_row(session):
    pf, expected = _load("simple")
    _seed(session, expected)
    # A stale project + workload the file no longer contains (a deletion sync missed).
    session.add(EclipseFoundationProject(id="technology.stale"))
    session.flush()
    session.add(
        JenkinsWorkload(
            ef_project_id="technology.stale",
            issuer="https://ci.eclipse.org/stale/oidc",
        )
    )
    session.commit()

    report = verify_db(session, pf)
    kinds = {(d.entity, d.kind) for d in report.discrepancies}
    assert ("eclipse_foundation_project", "orphan") in kinds
    assert ("jenkins_workload", "orphan") in kinds


def test_detects_wrong_project_grouping(session):
    """A workload attached to the wrong EF project is a missing+orphan pair."""
    pf, expected = _load("multi")
    _seed(session, expected)
    # Re-home one github workload under the wrong (but existing) project.
    w = (
        session.query(GitHubWorkload)
        .filter_by(repo_name="one", ef_project_id="technology.alpha")
        .one()
    )
    w.ef_project_id = "iot.beta"
    session.commit()

    report = verify_db(session, pf)
    kinds = {(d.entity, d.kind) for d in report.discrepancies}
    assert ("github_workload", "missing") in kinds  # expected under alpha, absent
    assert ("github_workload", "orphan") in kinds  # present under beta, unexpected


def test_detects_malformed_owner_id_offline(session):
    pf, expected = _load("simple")
    _seed(session, expected)
    # A non-numeric owner id (e.g. a field-swap) is caught without the network.
    w = session.query(GitHubWorkload).one()
    w.repo_owner_id = "eclipse-foo"
    session.commit()

    report = verify_db(session, pf)
    malformed = [d for d in report.discrepancies if d.kind == "malformed"]
    assert malformed and malformed[0].entity == "github_workload"


# --------------------------------------------------------------------------- #
# Resolution check (mocked GitHub + DependencyTrack)
# --------------------------------------------------------------------------- #


def _resolution_get(expected):
    """A fake requests.get dispatching GitHub owner + DT project lookups.

    Built from the golden expected rows so a matching DB verifies clean.
    """
    owner_ids = {
        g["resolves"]["owner"]: g["repo_owner_id"] for g in expected["github_workloads"]
    }
    dt_tree: dict[str, dict] = {}
    for d in expected["dependency_track_projects"]:
        parent = d["resolves"]["parent"]
        root = dt_tree.setdefault(
            parent, {"name": parent, "uuid": f"root-{parent}", "children": []}
        )
        root["children"].append({"name": d["name"], "uuid": d["parent_uuid"]})

    def fake_get(url, params=None, headers=None, **kw):
        if "api.github.com/users/" in url:
            return _resp({"id": int(owner_ids[url.rsplit('/', 1)[1]])})
        if url.endswith("/api/v1/project"):
            root = dt_tree.get(params["name"])
            return _resp([root] if root else [])
        raise AssertionError(f"unexpected GET {url}")

    return fake_get


def test_resolution_ok_when_sources_agree(session, monkeypatch):
    pf, expected = _load("multi")
    _seed(session, expected)
    monkeypatch.setattr(
        "pia.verify.requests.get", _resolution_get(expected)
    )
    report = verify_db(
        session, pf, dt_url="https://dt", dt_api_key="key", check_resolution=True
    )
    assert report.ok(), report.format()
    assert report.resolution_checked


def test_resolution_detects_wrong_owner_id(session, monkeypatch):
    pf, expected = _load("simple")
    _seed(session, expected)
    # DB has repo_owner_id "111"; make GitHub report a different id. DT agrees,
    # so github is the only resolution discrepancy.
    def fake_get(url, params=None, headers=None, **kw):
        if "api.github.com/users/" in url:
            return _resp({"id": 999})
        if url.endswith("/api/v1/project"):
            return _resp(
                [{"name": params["name"], "uuid": "root",
                  "children": [{"name": "foo-server",
                                "uuid": "11111111-1111-1111-1111-111111111111"}]}]
            )
        raise AssertionError(url)

    monkeypatch.setattr("pia.verify.requests.get", fake_get)
    report = verify_db(
        session, pf, dt_url="https://dt", dt_api_key="key", check_resolution=True
    )
    res = [d for d in report.discrepancies if d.kind == "resolution"]
    assert len(res) == 1 and res[0].entity == "github_workload"


def test_resolution_detects_wrong_parent_uuid(session, monkeypatch):
    pf, expected = _load("simple")
    _seed(session, expected)
    # GitHub agrees, but DT resolves the child to a different uuid than stored.
    def fake_get(url, params=None, headers=None, **kw):
        if "api.github.com/users/" in url:
            return _resp({"id": 111})
        if url.endswith("/api/v1/project"):
            return _resp(
                [{"name": params["name"], "uuid": "root",
                  "children": [{"name": "foo-server", "uuid": "DIFFERENT"}]}]
            )
        raise AssertionError(url)

    monkeypatch.setattr("pia.verify.requests.get", fake_get)
    report = verify_db(
        session, pf, dt_url="https://dt", dt_api_key="key", check_resolution=True
    )
    res = [d for d in report.discrepancies if d.kind == "resolution"]
    assert res and res[0].entity == "dependency_track_project"


def test_check_resolution_requires_dt_config(session):
    pf, _ = _load("simple")
    with pytest.raises(ValueError, match="requires dt_url"):
        verify_db(session, pf, check_resolution=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


@pytest.fixture
def patch_verify_cli(session_factory, monkeypatch):
    monkeypatch.setenv("PIA_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setattr(cli_module, "_make_session", session_factory)


def test_cli_verify_ok(session_factory, patch_verify_cli):
    with session_factory() as s:
        _seed(s, _load("simple")[1])

    result = CliRunner().invoke(cli_module.cli, ["verify", str(GOLDEN / "simple.yaml")])
    assert result.exit_code == 0, result.output
    assert "OK:" in result.output


def test_cli_verify_reports_and_exits_nonzero(session_factory, patch_verify_cli):
    # Seed a DB missing everything the file expects.
    result = CliRunner().invoke(cli_module.cli, ["verify", str(GOLDEN / "simple.yaml")])
    assert result.exit_code == 1
    assert "MISMATCH" in result.output
    assert "missing" in result.output
