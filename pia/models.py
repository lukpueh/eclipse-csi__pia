"""ORM models for projects/workloads/products and Pydantic request models."""

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

logger = logging.getLogger(__name__)


GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
"""OIDC issuer for GitHub Actions tokens. Constant across all GitHub workloads."""

JENKINS_ISSUER_PREFIX = "https://ci.eclipse.org"
"""Prefix used for early validation of Jenkins issuer URLs."""


class Base(DeclarativeBase):
    """Declarative base class for ORM models."""


class EclipseFoundationProject(Base):
    """Eclipse Foundation project. Groups workloads and DependencyTrack projects.

    https://www.eclipse.org/projects/handbook/#resources-identifiers
    """

    __tablename__ = "eclipse_foundation_projects"

    id: Mapped[str] = mapped_column(String, primary_key=True)


class Workload(Base):
    """CI/CD entity authorized to upload SBOMs.

    Polymorphic base — see GitHubWorkload and JenkinsWorkload.
    """

    __tablename__ = "workloads"

    id: Mapped[int] = mapped_column(primary_key=True)
    ef_project_id: Mapped[str] = mapped_column(
        ForeignKey(
            "eclipse_foundation_projects.id",
            onupdate="CASCADE",
        ),
    )
    type: Mapped[str] = mapped_column(String)

    __mapper_args__ = {
        "polymorphic_identity": "workload",
        "polymorphic_on": "type",
    }


class GitHubWorkload(Workload):
    """GitHub Actions workload. Issuer is always GITHUB_ISSUER."""

    __tablename__ = "github_workloads"

    id: Mapped[int] = mapped_column(ForeignKey("workloads.id"), primary_key=True)
    repo_name: Mapped[str] = mapped_column(String)
    repo_owner: Mapped[str] = mapped_column(String)
    repo_owner_id: Mapped[str] = mapped_column(String)

    __mapper_args__ = {
        "polymorphic_identity": "github",
    }


class JenkinsWorkload(Workload):
    """Jenkins workload. Each instance has a distinct issuer URL."""

    __tablename__ = "jenkins_workloads"

    id: Mapped[int] = mapped_column(ForeignKey("workloads.id"), primary_key=True)
    issuer: Mapped[str] = mapped_column(String)

    __mapper_args__ = {
        "polymorphic_identity": "jenkins",
    }


class DependencyTrackProject(Base):
    """DependencyTrack target for SBOM uploads."""

    __tablename__ = "dependency_track_projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    ef_project_id: Mapped[str] = mapped_column(
        ForeignKey(
            "eclipse_foundation_projects.id",
            onupdate="CASCADE",
        ),
    )
    name: Mapped[str] = mapped_column(String)
    parent_uuid: Mapped[str] = mapped_column(String)


def is_issuer_known(session: Session, issuer: str) -> bool:
    """Check if issuer is plausibly known to PIA.

    GitHub: issuer must equal GITHUB_ISSUER, and at least one GitHub workload
    must be registered.
    Jenkins: issuer must start with JENKINS_ISSUER_PREFIX, and a Jenkins
    workload with this exact issuer must be registered.
    """
    if issuer == GITHUB_ISSUER:
        stmt = select(GitHubWorkload.id).limit(1)
        return session.execute(stmt).first() is not None

    if issuer.startswith(JENKINS_ISSUER_PREFIX):
        stmt = select(JenkinsWorkload.id).where(JenkinsWorkload.issuer == issuer)
        return session.execute(stmt).first() is not None

    return False


def find_workload_by_claims(
    session: Session, token_claims: dict[str, Any]
) -> Workload | None:
    """Find Workload matching verified token claims.

    GitHub: match by repo_owner, repo_name, repo_owner_id.
    Jenkins: match by exact issuer.
    Returns None if no match.
    """
    issuer = token_claims["iss"]
    logger.info(f"Searching for workload matching issuer '{issuer}' and token claims")

    if issuer == GITHUB_ISSUER:
        repository = token_claims.get("repository", "")
        if "/" not in repository:
            logger.info(
                "GitHub token missing or malformed 'repository' claim: "
                f"{repository!r}"
            )
            return None
        repo_owner, repo_name = repository.split("/", 1)
        repo_owner_id = token_claims.get("repository_owner_id")
        gh_stmt = select(GitHubWorkload).where(
            GitHubWorkload.repo_owner == repo_owner,
            GitHubWorkload.repo_name == repo_name,
            GitHubWorkload.repo_owner_id == repo_owner_id,
        )
        return session.execute(gh_stmt).scalar_one_or_none()

    jk_stmt = select(JenkinsWorkload).where(JenkinsWorkload.issuer == issuer)
    return session.execute(jk_stmt).scalar_one_or_none()


def find_dt_project(
    session: Session, ef_project_id: str, name: str
) -> DependencyTrackProject | None:
    """Find DependencyTrackProject by name within an Eclipse Foundation project."""
    stmt = select(DependencyTrackProject).where(
        DependencyTrackProject.ef_project_id == ef_project_id,
        DependencyTrackProject.name == name,
    )
    return session.execute(stmt).scalar_one_or_none()


class PiaUploadPayload(BaseModel):
    """Payload for PIA SBOM upload."""

    product_name: str
    """
    Name of product for which the SBOM is produced. This field is required by
    DependencyTrack to aggregate SBOMs by product within a project.
    """

    product_version: str
    """
    Version of product for which the SBOM was produced
    """

    bom: str
    """
    Base64-encoded CycloneDX JSON SBOM
    """

    is_latest: bool = True
    """
    Whether this SBOM should be marked as the latest version in DependencyTrack
    """

    model_config = ConfigDict(use_attribute_docstrings=True)


class DependencyTrackUploadPayload(BaseModel):
    """Payload for DependencyTrack SBOM upload."""

    project_name: str = Field(serialization_alias="projectName")
    project_version: str = Field(serialization_alias="projectVersion")
    parent_uuid: str = Field(serialization_alias="parentUUID")
    auto_create: bool = Field(default=True, serialization_alias="autoCreate")
    is_latest: bool = Field(serialization_alias="isLatest")
    bom: str

    def to_dict(self):
        return self.model_dump(by_alias=True)
