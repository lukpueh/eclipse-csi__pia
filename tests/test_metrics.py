"""Tests for the Prometheus metrics endpoint and definitions."""

import logging
from typing import get_args

import pytest
from prometheus_client import CONTENT_TYPE_PLAIN_0_0_4

import pia
from pia.models import DependencyTrackProject, EclipseFoundationProject


@pytest.mark.usefixtures("setup_env")
class TestMetricsEndpoint:
    """Tests for the /metrics scrape endpoint."""

    def test_serves_prometheus_text_format(self, client):
        response = client.get("/metrics")

        assert response.status_code == 200
        assert response.headers["content-type"] == CONTENT_TYPE_PLAIN_0_0_4
        assert b"pia_http_requests_total" in response.content

    def test_reports_build_version(self, client, metric_value):
        """Info('pia_build') must expose `pia_build_info`, not `..._info_info`."""
        client.get("/metrics")

        assert metric_value("pia_build_info", version=pia.__version__) == 1.0


@pytest.mark.usefixtures("setup_env")
class TestSeriesInitialization:
    """Series are pre-created at zero so the first event after a restart counts."""

    @staticmethod
    def _exists(name: str, **labels: str) -> bool:
        from prometheus_client import REGISTRY

        return REGISTRY.get_sample_value(name, labels) is not None

    def test_startup_creates_db_independent_series(self, client):
        from pia.main import UPLOAD_PATH, UPLOAD_STATUSES
        from pia.metrics import RejectionReason

        for reason in get_args(RejectionReason):
            assert self._exists("pia_upload_rejections_total", reason=reason)
        for status in UPLOAD_STATUSES:
            assert self._exists(
                "pia_http_requests_total",
                method="POST",
                path=UPLOAD_PATH,
                status=status,
            )
        assert self._exists(
            "pia_http_request_duration_seconds_count", method="POST", path=UPLOAD_PATH
        )

    def test_creates_per_project_series_at_zero(self, session, session_factory):
        from prometheus_client import REGISTRY

        from pia.main import initialize_metrics
        from pia.metrics import UploadOutcome

        # Labels no other test increments, so the value must still be zero.
        session.add(EclipseFoundationProject(id="eclipse-init"))
        session.flush()
        session.add(
            DependencyTrackProject(
                ef_project_id="eclipse-init", name="init-product", parent_uuid="u"
            )
        )
        session.commit()

        initialize_metrics(session_factory)

        for outcome in get_args(UploadOutcome):
            if outcome == "no_dt_project":
                labels = {"product_name": "_unregistered"}
            else:
                labels = {"product_name": "init-product"}
            value = REGISTRY.get_sample_value(
                "pia_sbom_uploads_total",
                {"ef_project_id": "eclipse-init", "outcome": outcome, **labels},
            )
            assert value == 0.0, outcome
        # The sentinel is only paired with `no_dt_project`.
        assert not self._exists(
            "pia_sbom_uploads_total",
            ef_project_id="eclipse-init",
            product_name="_unregistered",
            outcome="success",
        )

    def test_database_failure_is_logged_not_raised(self, caplog):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from pia.main import initialize_metrics

        # No tables: the per-project query fails.
        empty = sessionmaker(bind=create_engine("sqlite://"))
        with caplog.at_level(logging.WARNING):
            initialize_metrics(empty)

        [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert "Could not pre-create per-project metric series" in warning.getMessage()
        # The database-independent series were still created.
        assert self._exists("pia_upload_rejections_total", reason="no_workload")
