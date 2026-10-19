import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app import main

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


def test_database_unavailable_during_startup_fails_lifespan(monkeypatch):
    def unavailable(bind):
        raise SQLAlchemyError("database unavailable during startup")

    monkeypatch.setattr(main.Base.metadata, "create_all", unavailable)

    with (
        pytest.raises(SQLAlchemyError, match="database unavailable during startup"),
        TestClient(main.app),
    ):
        pass


def test_provider_configuration_is_validated_lazily_on_rca_request(monkeypatch):
    runtime_engine = SimpleNamespace(dispose=lambda: None)
    monkeypatch.setattr(main, "engine", runtime_engine)
    monkeypatch.setattr(main.Base.metadata, "create_all", lambda bind: None)
    monkeypatch.setenv("RCA_PROVIDER", "disabled")

    with TestClient(main.app) as client:
        response = client.post("/incidents/1/analyze")

    assert response.status_code == 503
    assert response.json() == {"detail": "RCA is disabled; configure RCA_PROVIDER=openai or gemini"}
