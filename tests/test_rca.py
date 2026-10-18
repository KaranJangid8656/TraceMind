import hashlib
import json
import os
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app import rca
from app.config import Settings
from app.database import get_session
from app.llm import (
    PROMPT_VERSION,
    GeminiProvider,
    OpenAIProvider,
    ProviderFailure,
    get_rca_provider,
)
from app.main import app
from app.models import Incident, LogRecord, RCAReport
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


@pytest.mark.rca_benchmark
def test_rca_report_and_evidence_round_trip(client, fake_provider):
    incident_id = seed(client)
    response = client.post(f"/incidents/{incident_id}/analyze")
    assert response.status_code == 201, response.text
    report = response.json()
    assert report["provider"] == "test-double"
    assert report["prompt_version"] == PROMPT_VERSION
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


def test_report_commit_failure_rolls_back_insert(client, fake_provider, monkeypatch, caplog):
    incident_id = seed(client)
    caplog.set_level("WARNING", logger="app.rca")
    original_session = app.dependency_overrides[get_session]
    inserted_counts = []

    def failing_session():
        with contextmanager(original_session)() as session:

            def fail_commit():
                inserted_counts.append(session.scalar(select(func.count()).select_from(RCAReport)))
                raise SQLAlchemyError("PRIVATE persistence failure")

            with monkeypatch.context() as patch:
                patch.setattr(session, "commit", fail_commit)
                yield session

    app.dependency_overrides[get_session] = failing_session
    try:
        response = client.post(f"/incidents/{incident_id}/analyze")
    finally:
        app.dependency_overrides[get_session] = original_session
    assert inserted_counts == [1]  # The insert actually reached the transaction before failure.
    assert response.status_code == 503
    assert response.json() == {"detail": "Database operation unavailable"}
    assert client.get(f"/incidents/{incident_id}/analyses").json()["total"] == 0
    assert "event=rca_persistence_failed category=database_error" in caplog.text
    assert "PRIVATE persistence failure" not in caplog.text
    assert "event=rca_report_persisted" not in caplog.text
    with contextmanager(original_session)() as session:
        assert session.scalar(select(func.count()).select_from(RCAReport)) == 0
    assert client.post(f"/incidents/{incident_id}/analyze").status_code == 201


def test_successful_provider_call_logs_latency_and_report_metadata(client, fake_provider, caplog):
    incident_id = seed(client)
    caplog.set_level("INFO", logger="app.rca")

    response = client.post(f"/incidents/{incident_id}/analyze")

    assert response.status_code == 201, response.text
    assert (
        "event=rca_provider_completed provider=test-double model=scripted-fixture duration_ms="
        in caplog.text
    )
    assert (
        "event=rca_report_persisted provider=test-double model=scripted-fixture "
        f"prompt_version={PROMPT_VERSION}" in caplog.text
    )


def test_failed_provider_call_logs_latency_without_sensitive_details(client, fake_provider, caplog):
    incident_id = seed(client)
    caplog.set_level("WARNING", logger="app.rca")

    def fail(_evidence):
        raise ProviderFailure(504, "PRIVATE provider failure")

    fake_provider.respond = fail
    response = client.post(f"/incidents/{incident_id}/analyze")

    assert response.status_code == 504
    assert (
        "event=rca_provider_failed provider=test-double model=scripted-fixture "
        "category=provider_failure status_code=504 duration_ms=" in caplog.text
    )
    assert "PRIVATE provider failure" not in caplog.text
    assert client.get(f"/incidents/{incident_id}/analyses").json()["total"] == 0


def test_validation_failure_logs_category_without_provider_output(client, fake_provider, caplog):
    incident_id = seed(client)
    caplog.set_level("WARNING", logger="app.rca")

    def invalid(evidence):
        result = hypothesis(evidence)
        result["observed_facts"][0]["observation"] = "PRIVATE raw provider output"
        result["supporting_log_ids"] = [999999]
        return result

    fake_provider.respond = invalid
    response = client.post(f"/incidents/{incident_id}/analyze")

    assert response.status_code == 502
    assert (
        "event=rca_validation_failed provider=test-double model=scripted-fixture "
        "category=citation_invalid status_code=502" in caplog.text
    )
    assert "PRIVATE raw provider output" not in caplog.text
    assert "999999" not in caplog.text
    assert client.get(f"/incidents/{incident_id}/analyses").json()["total"] == 0


@pytest.mark.rca_benchmark
def test_no_usable_evidence_skips_provider(client, fake_provider):
    with contextmanager(app.dependency_overrides[get_session])() as session:
        now = datetime.now(UTC)
        incident = Incident(
            title="Incident without logs", severity="ERROR", first_seen=now, last_seen=now
        )
        session.add(incident)
        session.flush()
        incident_id = incident.id
        session.commit()

    response = client.post(f"/incidents/{incident_id}/analyze")
    assert response.status_code == 201, response.text
    assert len(fake_provider.calls) == 0
    report = response.json()
    assert report["provider"] == "evidence_guard"
    assert report["model"] is None
    assert report["evidence_snapshot"]["logs"] == []
    assert report["analysis"]["status"] == "inconclusive"
    assert report["analysis"]["probable_cause"] is None
    assert report["analysis"]["supporting_log_ids"] == []
    assert report["analysis"]["confidence"] == "low"
    assert "No usable log evidence is available." in report["analysis"]["evidence_gaps"]
    assert client.get(f"/incidents/{incident_id}/analyses/{report['id']}").json() == report
    assert len(fake_provider.calls) == 0


def test_stored_report_survives_log_edits_and_prompt_change(client, fake_provider, monkeypatch):
    incident_id = seed(client)
    response = client.post(f"/incidents/{incident_id}/analyze")
    assert response.status_code == 201
    first = response.json()
    log_id = first["evidence_snapshot"]["logs"][0]["id"]
    with contextmanager(app.dependency_overrides[get_session])() as session:
        session.get(LogRecord, log_id).message = "Updated source log after report creation"
        session.commit()
    monkeypatch.setattr(rca, "PROMPT_VERSION", "test-future-prompt")
    response = client.post(f"/incidents/{incident_id}/analyze")
    assert response.status_code == 201
    second = response.json()
    assert second["prompt_version"] == "test-future-prompt"
    assert second["evidence_hash"] != first["evidence_hash"]
    assert (
        next(log for log in second["evidence_snapshot"]["logs"] if log["id"] == log_id)["message"]
        == "Updated source log after report creation"
    )
    assert client.get(f"/incidents/{incident_id}/analyses/{first['id']}").json() == first
    with contextmanager(app.dependency_overrides[get_session])() as session:
        stored = session.get(RCAReport, first["id"])
        assert stored.prompt_version == first["prompt_version"] == PROMPT_VERSION
        assert stored.evidence_snapshot == first["evidence_snapshot"]


@pytest.mark.parametrize("failure,expected", [(429, 503), (500, 502), ("timeout", 504)])
def test_openai_failure_does_not_retry_or_fall_back(client, monkeypatch, failure, expected):
    incident_id = seed(client)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if failure == "timeout":
            raise httpx.ReadTimeout("private timeout details", request=request)
        return httpx.Response(failure, text="private provider details")

    def forbid_fallback(self, evidence):
        pytest.fail("OpenAI failure must not invoke Gemini")

    monkeypatch.setattr(GeminiProvider, "analyze", forbid_fallback)
    provider = OpenAIProvider(
        Settings(_env_file=None, openai_api_key="test-secret", openai_model="test-model"),
        transport=httpx.MockTransport(handler),
    )
    app.dependency_overrides[get_rca_provider] = lambda: provider
    response = client.post(f"/incidents/{incident_id}/analyze")
    assert response.status_code == expected
    assert calls == ["https://api.openai.com/v1/responses"]
    assert "private" not in response.text
    assert client.get(f"/incidents/{incident_id}/analyses").json()["total"] == 0


@pytest.mark.rca_benchmark
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


def test_probable_cause_requires_exact_verbatim_first_ranked_cause(client, fake_provider):
    incident_id = seed(client)

    def respond(evidence):
        result = hypothesis(evidence)
        result["probable_cause"] = "Payment timeouts may have followed database pool exhaustion."
        return result

    fake_provider.respond = respond
    response = client.post(f"/incidents/{incident_id}/analyze")
    assert response.status_code == 502
    assert response.json()["detail"] == (
        "RCA report probable cause does not match its first hypothesis"
    )
    assert client.get(f"/incidents/{incident_id}/analyses").json()["total"] == 0


def test_probable_cause_requires_exact_citation_order(client, fake_provider):
    incident_id = seed(client)

    def respond(evidence):
        result = hypothesis(evidence)
        assert len(result["supporting_log_ids"]) >= 2
        result["supporting_log_ids"] = list(reversed(result["supporting_log_ids"]))
        return result

    fake_provider.respond = respond
    response = client.post(f"/incidents/{incident_id}/analyze")
    assert response.status_code == 502
    assert response.json()["detail"] == ("RCA report probable cause has inconsistent citations")
    assert client.get(f"/incidents/{incident_id}/analyses").json()["total"] == 0


@pytest.mark.rca_benchmark
def test_hypothesis_requires_observed_fact(client, fake_provider):
    incident_id = seed(client)

    def respond(evidence):
        result = hypothesis(evidence)
        result["observed_facts"] = []
        return result

    fake_provider.respond = respond
    response = client.post(f"/incidents/{incident_id}/analyze")
    assert response.status_code == 502
    assert response.json()["detail"] == (
        "Hypothesis RCA report must contain at least one observed fact"
    )
    assert client.get(f"/incidents/{incident_id}/analyses").json()["total"] == 0


@pytest.mark.rca_benchmark
def test_hypothesis_rejects_duplicate_ranked_causes(client, fake_provider):
    incident_id = seed(client)

    def respond(evidence):
        result = hypothesis(evidence)
        duplicate = deepcopy(result["ranked_causes"][0])
        duplicate.update(rank=2, cause=duplicate["cause"].swapcase(), confidence="low")
        result["ranked_causes"].append(duplicate)
        return result

    fake_provider.respond = respond
    response = client.post(f"/incidents/{incident_id}/analyze")
    assert response.status_code == 502
    assert response.json()["detail"] == "RCA report contains duplicate ranked causes"
    assert client.get(f"/incidents/{incident_id}/analyses").json()["total"] == 0


@pytest.mark.rca_benchmark
def test_hypothesis_rejects_confidence_increase_at_lower_rank(client, fake_provider):
    incident_id = seed(client)

    def respond(evidence):
        result = hypothesis(evidence)
        result["confidence"] = "low"
        result["ranked_causes"][0]["confidence"] = "low"
        lower_ranked = deepcopy(result["ranked_causes"][0])
        lower_ranked.update(
            rank=2,
            cause="Excess application concurrency may have exhausted available workers.",
            confidence="medium",
        )
        result["ranked_causes"].append(lower_ranked)
        return result

    fake_provider.respond = respond
    response = client.post(f"/incidents/{incident_id}/analyze")
    assert response.status_code == 502
    assert response.json()["detail"] == "RCA report returned invalid cause confidence rankings"
    assert client.get(f"/incidents/{incident_id}/analyses").json()["total"] == 0


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


@pytest.mark.rca_benchmark
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


@pytest.mark.rca_benchmark
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


@pytest.mark.parametrize("status", [502, 503, pytest.param(504, marks=pytest.mark.rca_benchmark)])
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
        "fact_citation_limit",
        "cause_citation_limit",
        "contradicting_citation_limit",
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
        elif scenario == "fact_citation_limit":
            result["observed_facts"][0]["supporting_log_ids"] = [
                result["supporting_log_ids"][0]
            ] * 101
        elif scenario == "cause_citation_limit":
            result["ranked_causes"][0]["supporting_log_ids"] = [
                result["supporting_log_ids"][0]
            ] * 101
        elif scenario == "contradicting_citation_limit":
            result["ranked_causes"][0]["contradicting_log_ids"] = [
                result["supporting_log_ids"][0]
            ] * 101
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
        if scenario.endswith("citation_limit"):
            assert response.json()["detail"] == "RCA provider returned an invalid report schema"
        if scenario == "invalid_argument":
            assert "http_status=400 provider_status=INVALID_ARGUMENT" in caplog.text
            assert "Invalid argument; provider supplied no field details" in caplog.text
        assert client.get(f"/incidents/{incident_id}/analyses").json()["total"] == 0
    assert client.get("/logs").json()["total"] == 4
    assert client.get(f"/incidents/{incident_id}").json()["occurrence_count"] == 3
