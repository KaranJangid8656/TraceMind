"""Bounded evidence snapshots and structural/reference validation for RCA reports."""

import hashlib
import json
from copy import deepcopy
from time import perf_counter

from pydantic import ValidationError
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.correlation import as_utc
from app.llm import PROMPT_VERSION, ProviderFailure, RCAProvider
from app.models import Incident, IncidentLink, IncidentLog, LogRecord, RCAReport
from app.rca_schemas import RCAOutput, RCAReportRead, RCARequest
from app.schemas import LogRead


def evidence_json(evidence: dict) -> str:
    return json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def prepare_evidence(
    session: Session, incident: Incident, request: RCARequest, settings: Settings
) -> dict:
    limit = min(request.max_logs, settings.rca_max_logs)
    priority = case((LogRecord.severity.in_(["ERROR", "CRITICAL"]), 0), else_=1)
    memberships = session.scalars(
        select(IncidentLog)
        .join(LogRecord, IncidentLog.log_id == LogRecord.id)
        .where(IncidentLog.incident_id == incident.id)
        .order_by(priority, LogRecord.timestamp, LogRecord.id)
        .limit(limit)
    ).all()
    primary_count = session.scalar(
        select(func.count()).select_from(IncidentLog).where(IncidentLog.incident_id == incident.id)
    )
    related_filter = or_(
        IncidentLink.upstream_incident_id == incident.id,
        IncidentLink.downstream_incident_id == incident.id,
    )
    link_count = session.scalar(
        select(func.count()).select_from(IncidentLink).where(related_filter)
    )
    links = session.scalars(
        select(IncidentLink)
        .where(related_filter)
        .order_by(IncidentLink.upstream_log_id, IncidentLink.downstream_log_id)
        .limit(limit)
    ).all()
    candidates = []
    primary_ids = {item.log_id for item in memberships}
    for item in memberships:
        entry = LogRead.model_validate(item.log).model_dump(mode="json")
        entry.update(
            source="incident",
            incident_id=incident.id,
            correlation={
                "reason": item.reason,
                "strength": item.strength,
                "related_log_id": item.related_log_id,
            },
        )
        candidates.append(entry)
    external_ids = {}
    for link in links:
        for log_id, owner in (
            (link.upstream_log_id, link.upstream_incident_id),
            (link.downstream_log_id, link.downstream_incident_id),
        ):
            if owner != incident.id:
                external_ids[log_id] = owner
    if external_ids and len(candidates) < limit:
        external = session.scalars(
            select(LogRecord)
            .where(LogRecord.id.in_(external_ids))
            .order_by(LogRecord.timestamp, LogRecord.id)
            .limit(limit - len(candidates))
        ).all()
        for log in external:
            entry = LogRead.model_validate(log).model_dump(mode="json")
            entry.update(
                source="linked_incident", incident_id=external_ids[log.id], correlation=None
            )
            candidates.append(entry)

    def package(entries: list[dict]) -> dict:
        ids = {entry["id"] for entry in entries}
        visible_links = [
            {
                name: getattr(link, name)
                for name in (
                    "upstream_incident_id",
                    "downstream_incident_id",
                    "upstream_log_id",
                    "downstream_log_id",
                    "reason",
                    "strength",
                )
            }
            for link in links
            if link.upstream_log_id in ids and link.downstream_log_id in ids
        ]
        gaps = []
        omitted = primary_count - len(ids & primary_ids)
        if omitted:
            gaps.append(f"{omitted} incident log records omitted by evidence limits.")
        if link_count > len(visible_links):
            gaps.append(
                f"{link_count - len(visible_links)} relationships omitted by evidence limits."
            )
        if any(entry.get("message_truncated") for entry in entries):
            gaps.append("Some log messages were truncated; consult the original logs.")
        return {
            "incident": {
                "id": incident.id,
                "title": incident.title,
                "severity": incident.severity,
                "status": incident.status,
                "first_seen": as_utc(incident.first_seen).isoformat(),
                "last_seen": as_utc(incident.last_seen).isoformat(),
                "occurrence_count": incident.occurrence_count,
                "detection_rules": incident.detection_rules,
                "affected_services": sorted(
                    {entry["service"] for entry in entries if entry["source"] == "incident"}
                ),
            },
            "logs": entries,
            "relationships": visible_links,
            "evidence_gaps": gaps,
        }

    selected = []
    for entry in candidates:
        if len(entry["message"]) > 2000:
            entry["message"] = entry["message"][:2000]
            entry["message_truncated"] = True
        trial = package([*selected, entry])
        if len(evidence_json(trial)) > settings.rca_max_evidence_chars:
            entry["message"] = entry["message"][:200]
            entry["message_truncated"] = True
            trial = package([*selected, entry])
        if len(evidence_json(trial)) <= settings.rca_max_evidence_chars:
            selected.append(entry)
    selected.sort(key=lambda entry: (entry["timestamp"], entry["id"]))
    ids = {entry["id"] for entry in selected}
    for entry in selected:
        if entry["correlation"] and entry["correlation"]["related_log_id"] not in ids:
            entry["correlation"]["related_log_id"] = None
    return package(selected)


def validate_analysis(raw: str, evidence: dict) -> tuple[RCAOutput, list[str]]:
    try:
        result = RCAOutput.model_validate_json(raw)
    except (ValidationError, ValueError, TypeError):
        raise ProviderFailure(502, "RCA provider returned an invalid report schema") from None
    ids = {entry["id"] for entry in evidence["logs"]}
    lists = [result.supporting_log_ids]
    lists.extend(fact.supporting_log_ids for fact in result.observed_facts)
    for cause in result.ranked_causes:
        lists.extend([cause.supporting_log_ids, cause.contradicting_log_ids])
        if set(cause.supporting_log_ids) & set(cause.contradicting_log_ids):
            raise ProviderFailure(502, "RCA report contains contradictory citation roles")
    if any(not set(cited) <= ids or len(cited) != len(set(cited)) for cited in lists):
        raise ProviderFailure(
            502, "RCA report cited logs outside its evidence or duplicated citations"
        )
    if not set(result.affected_services) <= set(evidence["incident"]["affected_services"]):
        raise ProviderFailure(502, "RCA report invented affected services")
    if [cause.rank for cause in result.ranked_causes] != list(
        range(1, len(result.ranked_causes) + 1)
    ):
        raise ProviderFailure(502, "RCA report returned invalid cause rankings")
    if result.status == "hypothesis":
        if not result.ranked_causes or result.probable_cause != result.ranked_causes[0].cause:
            raise ProviderFailure(
                502, "RCA report probable cause does not match its first hypothesis"
            )
        if set(result.supporting_log_ids) != set(result.ranked_causes[0].supporting_log_ids):
            raise ProviderFailure(502, "RCA report probable cause has inconsistent citations")
    elif result.probable_cause is not None or result.supporting_log_ids:
        raise ProviderFailure(502, "Inconclusive RCA report must not assert a probable cause")

    notes = []
    # Confidence is a qualitative assessment, never a calibrated probability or confirmation.
    if result.confidence == "high" or any(
        cause.confidence == "high" for cause in result.ranked_causes
    ):
        notes.append(
            "High confidence capped at medium because the evidence consists of logs alone."
        )
        if result.confidence == "high":
            result.confidence = "medium"
        for cause in result.ranked_causes:
            if cause.confidence == "high":
                cause.confidence = "medium"
    if result.ranked_causes and result.ranked_causes[0].contradicting_log_ids:
        result.status, result.probable_cause, result.supporting_log_ids = "inconclusive", None, []
        result.evidence_gaps = list(
            dict.fromkeys(
                [
                    *result.evidence_gaps,
                    "The highest-ranked hypothesis has unresolved contradictory evidence.",
                ]
            )
        )[:20]
        notes.append("Conflicting evidence prevents selection of a probable cause.")
    if result.status == "inconclusive" or len(evidence["logs"]) < 2 or evidence["evidence_gaps"]:
        result.confidence = "low"
        for cause in result.ranked_causes:
            cause.confidence = "low"
        notes.append("Confidence limited to low for inconclusive, sparse, or truncated evidence.")
    result.affected_services = evidence["incident"]["affected_services"]
    if result.status == "inconclusive" and not result.evidence_gaps:
        result.evidence_gaps = ["Available logs do not distinguish a likely initiating cause."]
    result.evidence_gaps = list(dict.fromkeys([*evidence["evidence_gaps"], *result.evidence_gaps]))[
        :20
    ]
    return result, notes


def create_report(
    session: Session,
    incident: Incident,
    request: RCARequest,
    settings: Settings,
    provider: RCAProvider,
) -> RCAReportRead:
    started = perf_counter()
    incident_id = incident.id
    evidence = prepare_evidence(session, incident, request, settings)
    # Release the read transaction/connection while waiting on a remote provider.
    session.rollback()
    if not evidence["logs"]:
        raw = json.dumps(
            {
                "status": "inconclusive",
                "summary": "No usable logs fit the evidence package.",
                "probable_cause": None,
                "supporting_log_ids": [],
                "affected_services": [],
                "confidence": "low",
                "observed_facts": [],
                "ranked_causes": [],
                "alternative_explanations": [],
                "recommended_checks": ["Review the original incident logs."],
                "evidence_gaps": ["No usable log evidence is available."],
            }
        )
        provider_name, model = "evidence_guard", None
    else:
        # An adapter must not be able to mutate the evidence that validates its own output.
        raw = provider.analyze(deepcopy(evidence))
        provider_name, model = provider.name, provider.model
    result, notes = validate_analysis(raw, evidence)
    report = RCAReport(
        incident_id=incident_id,
        provider=provider_name,
        model=model,
        prompt_version=PROMPT_VERSION,
        evidence_hash=hashlib.sha256(evidence_json(evidence).encode("utf-8")).hexdigest(),
        evidence_snapshot=evidence,
        analysis=result.model_dump(mode="json"),
        validation_notes=notes,
        duration_ms=round((perf_counter() - started) * 1000),
    )
    session.add(report)
    session.flush()
    saved = report_data(report)
    session.commit()
    return saved


def report_data(report: RCAReport) -> RCAReportRead:
    fields = {
        name: getattr(report, name)
        for name in (
            "id",
            "incident_id",
            "provider",
            "model",
            "prompt_version",
            "evidence_hash",
            "duration_ms",
            "analysis",
            "validation_notes",
            "evidence_snapshot",
        )
    }
    return RCAReportRead(**fields, created_at=as_utc(report.created_at))
