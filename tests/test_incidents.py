import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.correlation import fingerprint
from app.models import LogRecord

SAMPLE = json.loads((Path(__file__).parents[1] / "data" / "sample_logs.json").read_text())


def log(seconds=0, **changes):
    return {
        "timestamp": (
            datetime(2026, 10, 17, 10, tzinfo=UTC) + timedelta(seconds=seconds)
        ).isoformat(),
        "service": "payment-service",
        "severity": "ERROR",
        "message": "Payment failed",
        **changes,
    }


def ingest_detect(client, records):
    response = client.post("/logs", json={"logs": records})
    assert response.status_code == 201
    detection = client.post("/incidents/detect")
    assert detection.status_code == 200, detection.text
    return client.get("/incidents").json()["items"]


def test_sample_trace_evidence_and_idempotency(client):
    items = ingest_detect(client, SAMPLE["logs"])
    assert len(items) == 1
    incident = items[0]
    assert incident["occurrence_count"] == 3
    assert incident["affected_services"] == ["database-service", "payment-service"]
    assert "multi_service_trace_failure" in incident["detection_rules"]
    detail = client.get(f"/incidents/{incident['id']}").json()
    assert detail["causality"] == "unconfirmed"
    assert [item["reason"] for item in detail["evidence"]] == [
        "isolated_error",
        "trace_id",
        "trace_id",
    ]
    assert detail["evidence"][1]["related_log_id"] == detail["evidence"][0]["log"]["id"]
    assert client.get("/logs").json()["total"] == 4
    assert client.post("/incidents/detect").json() == {
        "incidents_created": 0,
        "logs_assigned": 0,
        "links_created": 0,
    }
    assert client.get("/incidents").json()["items"] == items


@pytest.mark.parametrize("identifier", ["trace_id", "request_id"])
def test_identifiers_work_across_batches(client, identifier):
    items = ingest_detect(client, [log(**{identifier: "shared"})])
    first_id = items[0]["id"]
    items = ingest_detect(client, [log(1, service="user-service", **{identifier: "shared"})])
    assert len(items) == 1
    assert items[0]["id"] == first_id
    assert items[0]["occurrence_count"] == 2


def test_trace_precedes_request_and_conflicting_traces_stay_separate(client):
    items = ingest_detect(
        client,
        [
            log(trace_id="a", request_id="shared"),
            log(1, trace_id="b", request_id="shared"),
            log(2, trace_id="a", request_id="another"),
        ],
    )
    assert sorted(item["occurrence_count"] for item in items) == [1, 2]


def test_fixed_window_prevents_unbounded_chaining(client, monkeypatch):
    monkeypatch.setenv("CORRELATION_WINDOW_SECONDS", "10")
    items = ingest_detect(
        client, [log(0, trace_id="a"), log(10, trace_id="a"), log(20, trace_id="a")]
    )
    assert sorted(item["occurrence_count"] for item in items) == [1, 2]


def test_repeated_errors_normalize_dynamic_values(client):
    items = ingest_detect(
        client,
        [
            log(message="Payment 123 failed", exception_type="PaymentError"),
            log(1, message="Payment 456 failed", exception_type="PaymentError"),
            log(2, message="Payment 789 failed", exception_type="DifferentError"),
        ],
    )
    assert len(items) == 2
    grouped = next(item for item in items if item["occurrence_count"] == 2)
    assert "repeated_error" in grouped["detection_rules"]
    detail = client.get(f"/incidents/{grouped['id']}").json()
    assert detail["evidence"][1]["strength"] == "moderate"


def test_unrelated_nearby_errors_are_not_merged(client):
    items = ingest_detect(
        client,
        [
            log(message="Invalid state"),
            log(1, message="Missing configuration"),
            log(2, service="user-service", message="Invalid state"),
        ],
    )
    assert len(items) == 3


def test_error_burst_is_signal_not_merge(client, monkeypatch):
    monkeypatch.setenv("ERROR_BURST_THRESHOLD", "3")
    items = ingest_detect(
        client,
        [
            log(message="HTTP 500 response"),
            log(1, message="Bad configuration"),
            log(2, message="Handler failed"),
        ],
    )
    assert len(items) == 3
    assert all("service_error_burst" in item["detection_rules"] for item in items)


def test_dependency_timeout_links_remain_weak(client):
    items = ingest_detect(
        client,
        [
            log(service="database-service", message="Connection pool exhausted"),
            log(1, message="Database request timed out"),
        ],
    )
    assert len(items) == 2
    detail = client.get(f"/incidents/{items[0]['id']}").json()
    link = detail["relationships"][0]
    assert link["strength"] == "weak"
    assert link["reason"] == "configured_dependency_and_temporal_proximity"
    assert link["upstream_incident_id"] != link["downstream_incident_id"]
    assert client.post("/incidents/detect").json()["links_created"] == 0


def test_network_failure_trace_and_context(client):
    items = ingest_detect(
        client,
        [
            log(2, trace_id="network", message="Network timeout"),
            log(3, service="user-service", trace_id="network", message="Network timeout"),
            log(0, trace_id="network", severity="INFO", message="Request started"),
            log(4, severity="INFO", message="Unrelated request"),
        ],
    )
    assert len(items) == 1
    assert items[0]["occurrence_count"] == 2
    assert len(items[0]["related_log_ids"]) == 3
    assert items[0]["first_seen"] == "2026-10-17T10:00:00Z"


def test_late_arrival_and_severity_escalation(client):
    first = ingest_detect(client, [log(10, trace_id="late")])[0]
    item = ingest_detect(client, [log(0, trace_id="late", severity="CRITICAL")])[0]
    assert item["id"] == first["id"]
    assert item["first_seen"] == "2026-10-17T10:00:00Z"
    assert item["severity"] == "CRITICAL"


def test_status_filters_pagination_and_resolved_recurrence(client):
    items = ingest_detect(client, [log(trace_id="a"), log(1, service="user-service")])
    incident_id = next(
        item["id"] for item in items if "payment-service" in item["affected_services"]
    )
    assert (
        client.patch(f"/incidents/{incident_id}", json={"status": "investigating"}).status_code
        == 200
    )
    assert client.get("/incidents?status=investigating").json()["total"] == 1
    assert client.get("/incidents?service=User%20Service").json()["total"] == 1
    first_page = client.get("/incidents?limit=1").json()
    second_page = client.get("/incidents?limit=1&offset=1").json()
    assert first_page["total"] == second_page["total"] == 2
    assert first_page["items"][0]["id"] != second_page["items"][0]["id"]
    assert client.patch(f"/incidents/{incident_id}", json={"status": "resolved"}).status_code == 200
    items = ingest_detect(client, [log(2, trace_id="a")])
    assert len(items) == 3
    assert client.get("/incidents?status=resolved").json()["total"] == 1


def test_failed_detection_rolls_back(client, monkeypatch):
    import app.incidents as api

    client.post("/logs", json={"logs": [log()]})
    original = api.detect_incidents

    def fail_after_work(session, settings):
        original(session, settings)
        raise SQLAlchemyError("simulated failure")

    monkeypatch.setattr(api, "detect_incidents", fail_after_work)
    assert client.post("/incidents/detect").status_code == 503
    assert client.get("/incidents").json()["total"] == 0
    monkeypatch.setattr(api, "detect_incidents", original)
    assert client.post("/incidents/detect").json()["incidents_created"] == 1


@pytest.mark.parametrize(
    "path",
    ["/incidents/999", "/incidents?status=invalid", "/incidents?limit=0", "/incidents?service=?"],
)
def test_incident_api_errors(client, path):
    assert client.get(path).status_code in {404, 422}


def test_invalid_status_and_missing_incident(client):
    incident_id = ingest_detect(client, [log()])[0]["id"]
    assert client.patch(f"/incidents/{incident_id}", json={"status": "wrong"}).status_code == 422
    assert client.patch("/incidents/999", json={"status": "resolved"}).status_code == 404


def test_fingerprint_preserves_service_and_exception_context():
    first = LogRecord(service="payment", exception_type="Error", message="Failed id 123")
    second = LogRecord(service="payment", exception_type="Error", message="Failed id 456")
    assert fingerprint(first) == fingerprint(second)
    second.service = "user"
    assert fingerprint(first) != fingerprint(second)


def test_duplicate_raw_logs_preserved_but_detection_not_duplicated(client):
    items = ingest_detect(client, [log(trace_id="same"), log(trace_id="same")])
    assert len(items) == 1
    assert items[0]["occurrence_count"] == 2
    assert len(set(items[0]["related_log_ids"])) == 2
    assert client.post("/incidents/detect").json()["logs_assigned"] == 0
    assert client.get("/logs").json()["total"] == 2


@pytest.mark.parametrize("identifier", ["trace_id", "request_id"])
def test_conflicting_identifiers_prevent_weak_dependency_link(client, identifier):
    items = ingest_detect(
        client,
        [
            log(service="database-service", message="Pool exhausted", **{identifier: "a"}),
            log(1, message="Database timed out", **{identifier: "b"}),
        ],
    )
    assert len(items) == 2
    assert all(not client.get(f"/incidents/{item['id']}").json()["relationships"] for item in items)


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL locking test")
def test_concurrent_detection_assigns_each_log_once(client):
    assert client.post("/logs", json={"logs": [log(trace_id="concurrent")]}).status_code == 201
    with ThreadPoolExecutor(max_workers=3) as executor:
        responses = list(executor.map(lambda _: client.post("/incidents/detect"), range(3)))
    assert all(response.status_code == 200 for response in responses)
    assert sum(response.json()["incidents_created"] for response in responses) == 1
    assert sum(response.json()["logs_assigned"] for response in responses) == 1
    assert client.get("/incidents").json()["total"] == 1
