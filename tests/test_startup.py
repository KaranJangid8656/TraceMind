import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.migrations import MigrationStateError

PROJECT_ROOT = Path(__file__).parents[1]


def test_malformed_database_url_fails_during_engine_creation():
    environment = os.environ.copy()
    environment["DATABASE_URL"] = "not-a-sqlalchemy-url"

    result = subprocess.run(
        [sys.executable, "-c", "import app.database"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "sqlalchemy.exc.ArgumentError" in result.stderr
    assert "Could not parse SQLAlchemy URL" in result.stderr


def test_database_unavailable_during_startup_fails_lifespan(monkeypatch, caplog):
    runtime_engine = SimpleNamespace(dispose=lambda: None)

    def unavailable(bind):
        raise MigrationStateError("Database migration state could not be verified")

    monkeypatch.setattr(main, "engine", runtime_engine)
    monkeypatch.setattr(main, "verify_database_revision", unavailable)
    caplog.set_level("ERROR", logger="app.main")

    with (
        pytest.raises(
            RuntimeError,
            match="Application startup blocked: Database migration state could not be verified",
        ),
        TestClient(main.app),
    ):
        pass

    assert "event=startup_failed category=migration_state" in caplog.text
    assert "credentials" not in caplog.text


def test_provider_configuration_is_validated_lazily_on_rca_request(monkeypatch):
    runtime_engine = SimpleNamespace(dispose=lambda: None)
    monkeypatch.setattr(main, "engine", runtime_engine)
    monkeypatch.setattr(main, "verify_database_revision", lambda bind: None)
    monkeypatch.setenv("RCA_PROVIDER", "disabled")

    with TestClient(main.app) as client:
        response = client.post("/incidents/1/analyze")

    assert response.status_code == 503
    assert response.json() == {"detail": "RCA is disabled; configure RCA_PROVIDER=openai or gemini"}
