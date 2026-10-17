"""Transparent incident grouping. Correlation evidence does not establish causality."""

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Incident, IncidentLink, IncidentLog, LogRecord

ERROR_LEVELS = {"ERROR", "CRITICAL"}


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def fingerprint(log: LogRecord) -> tuple[str, str, str]:
    message = log.message.lower()
    message = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<id>", message)
    message = re.sub(r"\b0x[0-9a-f]+\b", "<id>", message)
    message = re.sub(r"\b\d+(?:\.\d+)*\b", "<number>", message)
    return log.service, log.exception_type or "", " ".join(message.split())


def detect_incidents(session: Session, settings: Settings) -> dict[str, int]:
    # Serialize detector workers on PostgreSQL; assignments and summaries commit atomically.
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(784231905)"))
    logs = list(session.scalars(select(LogRecord).order_by(LogRecord.timestamp, LogRecord.id)))
    incidents = {item.id: item for item in session.scalars(select(Incident))}
    assignments = {item.log_id: item for item in session.scalars(select(IncidentLog))}
    members = defaultdict(list)
    by_id = {log.id: log for log in logs}
    for log_id, assignment in assignments.items():
        members[assignment.incident_id].append(by_id[log_id])

    created = assigned = links_created = 0
    window = settings.correlation_window_seconds
    # Errors seed incidents; lower severities only become context for an identifier match.
    pending = [log for log in logs if log.id not in assignments]
    pending.sort(key=lambda log: (log.severity not in ERROR_LEVELS, as_utc(log.timestamp), log.id))
    for log in pending:
        candidates = []
        for incident_id, previous in members.items():
            incident = incidents[incident_id]
            if incident.status == "resolved":
                continue
            times = [as_utc(item.timestamp) for item in previous] + [as_utc(log.timestamp)]
            if (max(times) - min(times)).total_seconds() > window:
                continue
            for other in previous:
                if log.trace_id and log.trace_id == other.trace_id:
                    candidates.append((0, incident_id, other.id, "trace_id", "strong"))
                elif (
                    log.request_id
                    and log.request_id == other.request_id
                    and not (log.trace_id and other.trace_id and log.trace_id != other.trace_id)
                ):
                    candidates.append((1, incident_id, other.id, "request_id", "strong"))
                elif (
                    log.severity in ERROR_LEVELS
                    and other.severity in ERROR_LEVELS
                    and not any((log.trace_id, log.request_id, other.trace_id, other.request_id))
                    and fingerprint(log) == fingerprint(other)
                ):
                    candidates.append((2, incident_id, other.id, "error_fingerprint", "moderate"))
        if candidates:
            _, incident_id, related_id, reason, strength = min(candidates)
            incident = incidents[incident_id]
        elif log.severity in ERROR_LEVELS:
            incident = Incident(
                title=f"{log.service}: {log.message}"[:250],
                severity=log.severity,
                first_seen=as_utc(log.timestamp),
                last_seen=as_utc(log.timestamp),
                occurrence_count=0,
                detection_rules=[],
            )
            session.add(incident)
            session.flush()
            incidents[incident.id] = incident
            reason, strength, related_id = "isolated_error", "observed", None
            created += 1
        else:
            continue
        assignment = IncidentLog(
            log_id=log.id,
            incident_id=incident.id,
            reason=reason,
            strength=strength,
            related_log_id=related_id,
        )
        session.add(assignment)
        assignments[log.id] = assignment
        members[incident.id].append(log)
        assigned += 1

    # A service burst is a detection signal, never sufficient to merge different errors.
    service_errors = defaultdict(list)
    for log in logs:
        if log.severity in ERROR_LEVELS:
            service_errors[log.service].append(log)
    burst_ids = set()
    for errors in service_errors.values():
        left = 0
        for right, log in enumerate(errors):
            while (as_utc(log.timestamp) - as_utc(errors[left].timestamp)).total_seconds() > window:
                left += 1
            if right - left + 1 >= settings.error_burst_threshold:
                burst_ids.update(item.id for item in errors[left : right + 1])

    for incident_id, records in members.items():
        incident = incidents[incident_id]
        errors = [log for log in records if log.severity in ERROR_LEVELS]
        patterns = Counter(fingerprint(log) for log in errors)
        trace_services = defaultdict(set)
        for log in errors:
            if log.trace_id:
                trace_services[log.trace_id].add(log.service)
        rules = ["error_observed"]
        if any(count >= 2 for count in patterns.values()):
            rules.append("repeated_error")
        if any(len(services) >= 2 for services in trace_services.values()):
            rules.append("multi_service_trace_failure")
        if any(log.id in burst_ids for log in errors):
            rules.append("service_error_burst")
        summary = (
            min(as_utc(log.timestamp) for log in records),
            max(as_utc(log.timestamp) for log in records),
            len(errors),
            "CRITICAL" if any(log.severity == "CRITICAL" for log in errors) else "ERROR",
            rules,
        )
        previous = (
            as_utc(incident.first_seen),
            as_utc(incident.last_seen),
            incident.occurrence_count,
            incident.severity,
            incident.detection_rules,
        )
        if summary != previous:
            (
                incident.first_seen,
                incident.last_seen,
                incident.occurrence_count,
                incident.severity,
                incident.detection_rules,
            ) = summary
            incident.updated_at = datetime.now(UTC)

    existing_links = {
        (link.upstream_log_id, link.downstream_log_id)
        for link in session.scalars(select(IncidentLink))
    }
    for downstream in logs:
        if downstream.id not in assignments or not re.search(
            r"timeout|timed out|latency", downstream.message, re.IGNORECASE
        ):
            continue
        for service in settings.service_dependencies.get(downstream.service, []):
            for upstream in service_errors[service]:
                key = upstream.id, downstream.id
                if key in existing_links:
                    continue
                upstream_id = assignments[upstream.id].incident_id
                downstream_id = assignments[downstream.id].incident_id
                delta = (as_utc(downstream.timestamp) - as_utc(upstream.timestamp)).total_seconds()
                if upstream_id == downstream_id or not 0 <= delta <= window:
                    continue
                if (
                    upstream.trace_id
                    and downstream.trace_id
                    and upstream.trace_id != downstream.trace_id
                ):
                    continue
                if (
                    upstream.request_id
                    and downstream.request_id
                    and upstream.request_id != downstream.request_id
                ):
                    continue
                session.add(
                    IncidentLink(
                        upstream_log_id=upstream.id,
                        downstream_log_id=downstream.id,
                        upstream_incident_id=upstream_id,
                        downstream_incident_id=downstream_id,
                        reason="configured_dependency_and_temporal_proximity",
                        strength="weak",
                    )
                )
                existing_links.add(key)
                links_created += 1
    session.flush()
    return {"incidents_created": created, "logs_assigned": assigned, "links_created": links_created}
