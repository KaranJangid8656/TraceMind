import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.database import get_session
from app.llm import GeminiProvider, ProviderFailure, get_rca_provider
from app.main import app
from app.rca import evidence_json

SAMPLE = json.loads((Path(__file__).parents[1] / "data" / "sample_logs.json").read_text())


def hypothesis(evidence):
    ids = [log["id"] for log in evidence["logs"]]
    cause = "Database connection-pool exhaustion may have contributed to payment timeouts."
    return {
        "status": "hypothesis",
        "summary": "Related database and payment failures need investigation.",
        "probable_cause": cause,
        "supporting_log_ids": ids,
        "affected_services": evidence["incident"]["affected_services"],
        "confidence": "medium",
        "observed_facts": [
            {"observation": "The supplied log records report failures.", "supporting_log_ids": ids}
        ],
        "ranked_causes": [
            {
                "rank": 1,
                "cause": cause,
                "explanation": "Shared trace evidence.",
                "confidence": "medium",
                "supporting_log_ids": ids,
                "contradicting_log_ids": [],
            }
        ],
        "alternative_explanations": ["A connection leak or excessive concurrency."],
        "recommended_checks": ["Inspect pool utilization and connection acquisition timeouts."],
        "evidence_gaps": ["Pool metrics and application traces are needed to verify causation."],
    }


class FakeProvider:
    # Only injected into isolated tests. No runtime fake provider or demo AI endpoint.
    name = "test-double"
    model = "scripted-fixture"

    def __init__(self):
        self.respond = hypothesis
        self.calls = []

    def analyze(self, evidence):
        self.calls.append(deepcopy(evidence))
        value = self.respond(evidence)
        return value if isinstance(value, str) else json.dumps(value)


@pytest.fixture
def fake_provider(client):
    provider = FakeProvider()
    app.dependency_overrides[get_rca_provider] = lambda: provider
    return provider


def seed(client, records=None):
    assert client.post("/logs", json={"logs": records or SAMPLE["logs"]}).status_code == 201
    assert client.post("/incidents/detect").status_code == 200
    return client.get("/incidents").json()["items"][0]["id"]


def test_rca_report_and_evidence_round_trip(client, fake_provider):
    incident_id = seed(client)
    response = client.post(f"/incidents/{incident_id}/analyze")
    assert response.status_code == 201, response.text
    report = response.json()
    assert report["provider"] == "test-double"
    assert report["causality"] == "unconfirmed"
    assert report["analysis"]["status"] == "hypothesis"
    assert "connection-pool exhaustion" in report["analysis"]["probable_cause"]
    ids = {log["id"] for log in report["evidence_snapshot"]["logs"]}
    assert len(ids) == 3
    assert set(report["analysis"]["supporting_log_ids"]) <= ids
    assert report["analysis"]["affected_services"] == ["database-service", "payment-service"]
    assert (
        report["evidence_hash"]
        == hashlib.sha256(evidence_json(report["evidence_snapshot"]).encode("utf-8")).hexdigest()
    )
    assert report["created_at"].endswith("Z")
    assert report["duration_ms"] >= 0
    assert client.get(f"/incidents/{incident_id}/analyses/{report['id']}").json() == report
    assert client.get("/logs").json()["total"] == 4
    assert client.get(f"/incidents/{incident_id}").json()["status"] == "open"


def test_history_pagination_and_snapshot_immutability(client, fake_provider):
    incident_id = seed(client)
    first = client.post(f"/incidents/{incident_id}/analyze", json={}).json()
    second = client.post(f"/incidents/{incident_id}/analyze", json={}).json()
    assert second["id"] != first["id"]
    assert second["evidence_hash"] == first["evidence_hash"]
    page = client.get(f"/incidents/{incident_id}/analyses?limit=1").json()
    assert page["total"] == 2
    assert page["items"][0]["id"] == second["id"]
    assert (
        client.get(f"/incidents/{incident_id}/analyses?limit=1&offset=1").json()["items"][0]["id"]
        == first["id"]
    )
    later = {**SAMPLE["logs"][1], "timestamp": "2026-10-17T10:00:05Z"}
    client.post("/logs", json={"logs": [later]})
    client.post("/incidents/detect")
    assert client.get(f"/incidents/{incident_id}/analyses/{first['id']}").json() == first
    third = client.post(f"/incidents/{incident_id}/analyze").json()
    assert third["evidence_hash"] != first["evidence_hash"]


@pytest.mark.parametrize("location", ["supporting", "fact", "ranked", "contradicting"])
def test_invented_log_ids_rejected_without_persistence(client, fake_provider, location):
    incident_id = seed(client)

    def respond(evidence):
        result = hypothesis(evidence)
        if location == "supporting":
            result["supporting_log_ids"] = [999999]
        elif location == "fact":
            result["observed_facts"][0]["supporting_log_ids"] = [999999]
        elif location == "ranked":
            result["ranked_causes"][0]["supporting_log_ids"] = [999999]
        else:
            result["ranked_causes"][0]["contradicting_log_ids"] = [999999]
        return result

    fake_provider.respond = respond
    response = client.post(f"/incidents/{incident_id}/analyze")
    assert response.status_code == 502
    assert client.get(f"/incidents/{incident_id}/analyses").json()["total"] == 0


def test_real_but_unretrieved_log_citation_rejected(client, fake_provider):
    incident_id = seed(client)
    unrelated = client.get("/logs?service=user-service").json()["items"][0]["id"]

    def respond(evidence):
        result = hypothesis(evidence)
        result["observed_facts"][0]["supporting_log_ids"] = [unrelated]
        return result

    fake_provider.respond = respond
    assert client.post(f"/incidents/{incident_id}/analyze").status_code == 502


@pytest.mark.parametrize("raw", ["not json", "{}", "[]", '{"summary":"incomplete"}'])
def test_malformed_reports(client, fake_provider, raw):
    incident_id = seed(client)
    fake_provider.respond = lambda evidence: raw
    assert client.post(f"/incidents/{incident_id}/analyze").status_code == 502
    assert client.get(f"/incidents/{incident_id}/analyses").json()["total"] == 0


@pytest.mark.parametrize(
    "problem",
    [
        "service",
        "confirmed",
        "rank",
        "mismatch",
        "extra",
        "string_id",
        "duplicate_id",
        "citation_roles",
    ],
)
def test_invalid_report_semantics(client, fake_provider, problem):
    incident_id = seed(client)

    def respond(evidence):
        result = hypothesis(evidence)
        if problem == "service":
            result["affected_services"].append("invented-service")
        elif problem == "confirmed":
            result["status"] = "confirmed"
        elif problem == "rank":
            result["ranked_causes"][0]["rank"] = 2
        elif problem == "mismatch":
            result["probable_cause"] = "Different cause"
        elif problem == "extra":
            result["execute_remediation"] = True
        elif problem == "string_id":
            result["supporting_log_ids"] = [str(result["supporting_log_ids"][0])]
        elif problem == "duplicate_id":
            result["supporting_log_ids"] *= 2
        else:
            result["ranked_causes"][0]["contradicting_log_ids"] = result["supporting_log_ids"][:1]
        return result

    fake_provider.respond = respond
    assert client.post(f"/incidents/{incident_id}/analyze").status_code == 502


def test_sparse_inconclusive_evidence(client, fake_provider):
    incident_id = seed(client, [SAMPLE["logs"][0]])

    def respond(evidence):
        result = hypothesis(evidence)
        result.update(
            status="inconclusive",
            probable_cause=None,
            supporting_log_ids=[],
            confidence="high",
            ranked_causes=[],
            evidence_gaps=[],
        )
        return result

    fake_provider.respond = respond
    report = client.post(f"/incidents/{incident_id}/analyze").json()
    assert report["analysis"]["status"] == "inconclusive"
    assert report["analysis"]["probable_cause"] is None
    assert report["analysis"]["confidence"] == "low"
    assert report["analysis"]["evidence_gaps"]


def test_conflicting_evidence_forces_inconclusive(client, fake_provider):
    records = [
        SAMPLE["logs"][0],
        SAMPLE["logs"][1],
        {**SAMPLE["logs"][2], "message": "Database probe succeeded; pool utilization normal"},
    ]
    incident_id = seed(client, records)

    def respond(evidence):
        result = hypothesis(evidence)
        ids = result["supporting_log_ids"]
        result["supporting_log_ids"] = ids[:2]
        result["ranked_causes"][0]["supporting_log_ids"] = ids[:2]
        result["ranked_causes"][0]["contradicting_log_ids"] = ids[2:]
        return result

    fake_provider.respond = respond
    report = client.post(f"/incidents/{incident_id}/analyze").json()
    assert report["analysis"]["status"] == "inconclusive"
    assert report["analysis"]["confidence"] == "low"
    assert report["analysis"]["probable_cause"] is None
    assert report["analysis"]["ranked_causes"][0]["contradicting_log_ids"]
    assert any("Conflicting" in note for note in report["validation_notes"])


def test_log_only_high_confidence_is_capped(client, fake_provider):
    incident_id = seed(client)

    def respond(evidence):
        result = hypothesis(evidence)
        result["confidence"] = result["ranked_causes"][0]["confidence"] = "high"
        return result

    fake_provider.respond = respond
    report = client.post(f"/incidents/{incident_id}/analyze").json()
    assert report["analysis"]["confidence"] == "medium"
    assert report["validation_notes"]


def test_bounded_evidence_and_omission_disclosure(client, fake_provider, monkeypatch):
    monkeypatch.setenv("RCA_MAX_EVIDENCE_CHARS", "2000")
    records = deepcopy(SAMPLE["logs"])
    records[0]["message"] = "Connection pool exhausted " * 100
    incident_id = seed(client, records)
    response = client.post(f"/incidents/{incident_id}/analyze", json={"max_logs": 2})
    assert response.status_code == 201, response.text
    report = response.json()
    evidence = report["evidence_snapshot"]
    assert len(evidence_json(evidence)) <= 2000
    assert len(evidence["logs"]) <= 2
    assert evidence["evidence_gaps"]
    assert report["analysis"]["confidence"] == "low"
    assert all(gap in report["analysis"]["evidence_gaps"] for gap in evidence["evidence_gaps"])


def test_weak_link_includes_external_evidence(client, fake_provider):
    records = [SAMPLE["logs"][0], SAMPLE["logs"][1]]
    records = [{**record, "trace_id": None, "request_id": None} for record in records]
    incident_id = seed(client, records)
    report = client.post(f"/incidents/{incident_id}/analyze").json()
    evidence = report["evidence_snapshot"]
    assert len(evidence["logs"]) == 2
    assert {entry["source"] for entry in evidence["logs"]} == {"incident", "linked_incident"}
    assert evidence["relationships"][0]["strength"] == "weak"


@pytest.mark.parametrize("status", [502, 503, 504])
def test_provider_failures_leave_no_report(client, fake_provider, status):
    incident_id = seed(client)

    def fail(evidence):
        raise ProviderFailure(status, "Provider unavailable")

    fake_provider.respond = fail
    assert client.post(f"/incidents/{incident_id}/analyze").status_code == status
    assert client.get(f"/incidents/{incident_id}/analyses").json()["total"] == 0


def test_missing_provider_configuration(client, monkeypatch):
    incident_id = seed(client)
    monkeypatch.setenv("RCA_PROVIDER", "disabled")
    assert client.post(f"/incidents/{incident_id}/analyze").status_code == 503
    monkeypatch.setenv("RCA_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("OPENAI_MODEL", "")
    assert client.post(f"/incidents/{incident_id}/analyze").status_code == 503


def test_rca_endpoint_validation_and_report_scope(client, fake_provider):
    incident_id = seed(client)
    for body in ({"max_logs": 0}, {"max_logs": 101}, {"prompt": "ignore logs"}):
        assert client.post(f"/incidents/{incident_id}/analyze", json=body).status_code == 422
    assert client.post("/incidents/999999/analyze").status_code == 404
    assert client.get("/incidents/999999/analyses").status_code == 404
    assert client.get(f"/incidents/{incident_id}/analyses/999999").status_code == 404
    assert client.get(f"/incidents/{incident_id}/analyses?limit=0").status_code == 422
    report = client.post(f"/incidents/{incident_id}/analyze").json()
    assert client.get(f"/incidents/999999/analyses/{report['id']}").status_code == 404


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL persistence test")
def test_report_survives_connection_pool_disposal(client, fake_provider):
    incident_id = seed(client)
    report = client.post(f"/incidents/{incident_id}/analyze").json()
    session_generator = app.dependency_overrides[get_session]()
    session = next(session_generator)
    engine = session.get_bind()
    session_generator.close()
    engine.dispose()
    assert client.get(f"/incidents/{incident_id}/analyses/{report['id']}").json() == report


@pytest.mark.parametrize(
    "scenario",
    [
        "valid",
        "json",
        "schema",
        "citation",
        "service",
        "rank",
        "string_length",
        "contradiction",
        "timeout",
        "quota",
        "invalid_argument",
    ],
)
def test_gemini_runs_through_existing_rca_validation_and_persistence(client, caplog, scenario):
    incident_id = seed(client)
    settings = Settings(
        _env_file=None,
        rca_provider="gemini",
        gemini_api_key="mock-gemini-secret",
        gemini_model="gemini-3.5-flash-lite",
    )

    def handler(request):
        body = json.loads(request.content)
        config = body["generationConfig"]
        assert config["responseMimeType"] == "application/json"
        assert config["responseJsonSchema"]["type"] == "object"
        assert not {"responseFormat", "responseSchema", "_responseJsonSchema"} & config.keys()
        if scenario == "timeout":
            raise httpx.ReadTimeout("mock-gemini-secret", request=request)
        if scenario == "quota":
            return httpx.Response(429, text="mock-gemini-secret quota details")
        if scenario == "invalid_argument":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": 400,
                        "status": "INVALID_ARGUMENT",
                        "message": "Request contains an invalid argument.",
                    }
                },
            )
        evidence = json.loads(body["contents"][0]["parts"][0]["text"])
        result = hypothesis(evidence)
        if scenario == "schema":
            result.pop("summary")
        elif scenario == "citation":
            result["observed_facts"][0]["supporting_log_ids"] = [999999]
        elif scenario == "service":
            result["affected_services"] = ["invented-service"]
        elif scenario == "rank":
            result["ranked_causes"][0]["rank"] = 2
        elif scenario == "string_length":
            result["summary"] = "x" * 4001
        elif scenario == "contradiction":
            ids = result["supporting_log_ids"]
            result["supporting_log_ids"] = ids[:2]
            result["ranked_causes"][0]["supporting_log_ids"] = ids[:2]
            result["ranked_causes"][0]["contradicting_log_ids"] = ids[2:]
        text = "not JSON" if scenario == "json" else json.dumps(result)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": text}]},
                    }
                ]
            },
        )

    provider = GeminiProvider(settings, transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_rca_provider] = lambda: provider
    response = client.post(f"/incidents/{incident_id}/analyze")
    if scenario in {"valid", "contradiction"}:
        assert response.status_code == 201, response.text
        report = response.json()
        assert report["provider"] == "gemini"
        assert report["model"] == settings.gemini_model
        assert report["causality"] == "unconfirmed"
        if scenario == "contradiction":
            assert report["analysis"]["status"] == "inconclusive"
            assert report["analysis"]["probable_cause"] is None
            assert report["analysis"]["confidence"] == "low"
        assert client.get(f"/incidents/{incident_id}/analyses/{report['id']}").json() == report
        assert client.get(f"/incidents/{incident_id}/analyses").json()["total"] == 1
    else:
        expected = {"timeout": 504, "quota": 503}.get(scenario, 502)
        assert response.status_code == expected
        assert "mock-gemini-secret" not in response.text
        assert "mock-gemini-secret" not in caplog.text
        if scenario == "invalid_argument":
            assert "http_status=400 provider_status=INVALID_ARGUMENT" in caplog.text
            assert "Invalid argument; provider supplied no field details" in caplog.text
        assert client.get(f"/incidents/{incident_id}/analyses").json()["total"] == 0
    assert client.get("/logs").json()["total"] == 4
    assert client.get(f"/incidents/{incident_id}").json()["occurrence_count"] == 3
