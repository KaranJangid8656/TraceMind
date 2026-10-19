import json
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.database import get_session
from app.main import app

SAMPLE = json.loads((Path(__file__).parents[1] / "data" / "sample_logs.json").read_text())


def test_ingestion_round_trip_and_normalization(client):
    payload = {
        "logs": [
            {
                "timestamp": "2026-10-17T15:30:01+05:30",
                "service": "  DATABASE_Service  ",
                "severity": "fatal",
                "message": " Connection pool exhausted ",
                "request_id": " req-1 ",
            }
        ]
    }
    response = client.post("/logs", json=payload)
    assert response.status_code == 201
    saved = response.json()[0]
    assert saved["id"] > 0
    assert saved["timestamp"] == "2026-10-17T10:00:01Z"
    assert saved["service"] == "database-service"
    assert saved["severity"] == "CRITICAL"
    assert saved["message"] == "Connection pool exhausted"
    assert saved["request_id"] == "req-1"
    assert client.get("/logs").json()["items"] == [saved]


def test_filters_and_pagination(client):
    assert client.post("/logs", json=SAMPLE).status_code == 201
    page = client.get(
        "/logs", params={"service": "Payment Service", "severity": "error", "limit": 1, "offset": 1}
    ).json()
    assert page["total"] == 2
    assert len(page["items"]) == 1
    assert page["items"][0]["message"] == "Payment processing failed"
    result = client.get(
        "/logs", params={"start": "2026-10-17T15:30:02+05:30", "end": "2026-10-17T10:00:03Z"}
    ).json()
    assert result["total"] == 2
    assert client.get("/logs", params={"severity": "warn"}).json()["total"] == 1
    assert client.get("/logs", params={"offset": 100}).json()["items"] == []


@pytest.mark.parametrize(
    "change",
    [
        {"timestamp": "2026-10-17T10:00:01"},
        {"timestamp": "not-a-date"},
        {"severity": "unknown"},
        {"severity": 12},
        {"severity": None},
        {"service": "  "},
        {"service": "db/service"},
        {"message": " "},
        {"message": "x" * 32001},
        {"trace_id": ""},
        {"unexpected": "field"},
    ],
)
def test_invalid_batch_is_atomic(client, change):
    invalid = {**SAMPLE["logs"][0], **change}
    response = client.post("/logs", json={"logs": [SAMPLE["logs"][1], invalid]})
    assert response.status_code == 422
    assert client.get("/logs").json()["total"] == 0


@pytest.mark.parametrize("logs", [[], SAMPLE["logs"] * 251])
def test_batch_bounds(client, logs):
    assert client.post("/logs", json={"logs": logs}).status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"start": "2026-10-18T00:00:00Z", "end": "2026-10-17T00:00:00Z"},
        {"start": "2026-10-17T00:00:00"},
        {"severity": "invalid"},
        {"service": "?"},
        {"limit": 0},
        {"limit": 1001},
        {"offset": -1},
    ],
)
def test_invalid_filters(client, params):
    assert client.get("/logs", params=params).status_code == 422


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_health_returns_controlled_response_when_database_is_unavailable(client):
    original_session = app.dependency_overrides[get_session]

    class UnavailableSession:
        def execute(self, statement):
            raise SQLAlchemyError("private database connection details")

    def unavailable_session():
        yield UnavailableSession()

    app.dependency_overrides[get_session] = unavailable_session
    try:
        response = client.get("/health")
    finally:
        app.dependency_overrides[get_session] = original_session

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
    assert "private database connection details" not in response.text
