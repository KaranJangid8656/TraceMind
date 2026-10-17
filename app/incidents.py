from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.correlation import as_utc, detect_incidents
from app.database import get_session
from app.models import Incident, IncidentLink, IncidentLog, LogRecord
from app.schemas import (
    DetectionResult,
    IncidentDetail,
    IncidentPage,
    IncidentRead,
    IncidentStatus,
    IncidentUpdate,
    Severity,
    normalize_service,
)

router = APIRouter(prefix="/incidents", tags=["incidents"])
DB = Annotated[Session, Depends(get_session)]


def incident_data(incident: Incident, session: Session, detail: bool = False) -> dict:
    evidence = session.scalars(
        select(IncidentLog)
        .join(LogRecord, IncidentLog.log_id == LogRecord.id)
        .where(IncidentLog.incident_id == incident.id)
        .order_by(LogRecord.timestamp, LogRecord.id)
    ).all()
    data = {
        name: getattr(incident, name)
        for name in ("id", "title", "severity", "status", "occurrence_count", "detection_rules")
    }
    for name in ("first_seen", "last_seen", "created_at", "updated_at"):
        data[name] = as_utc(getattr(incident, name))
    data["affected_services"] = sorted({item.log.service for item in evidence})
    data["related_log_ids"] = [item.log_id for item in evidence]
    if detail:
        data["evidence"] = [
            {
                "log": item.log,
                "reason": item.reason,
                "strength": item.strength,
                "related_log_id": item.related_log_id,
            }
            for item in evidence
        ]
        relationships = session.scalars(
            select(IncidentLink)
            .where(
                or_(
                    IncidentLink.upstream_incident_id == incident.id,
                    IncidentLink.downstream_incident_id == incident.id,
                )
            )
            .order_by(IncidentLink.upstream_log_id, IncidentLink.downstream_log_id)
        ).all()
        data["relationships"] = [
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
            for link in relationships
        ]
    return data


@router.post("/detect", response_model=DetectionResult)
def detect(session: DB):
    result = detect_incidents(session, Settings())
    session.commit()
    return result


@router.get("", response_model=IncidentPage)
def list_incidents(
    session: DB,
    status: IncidentStatus | None = None,
    severity: Severity | None = None,
    service: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    filters = []
    if status is not None:
        filters.append(Incident.status == status.value)
    if severity is not None:
        filters.append(Incident.severity == severity.value)
    if service is not None:
        try:
            service = normalize_service(service)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        filters.append(
            Incident.id.in_(
                select(IncidentLog.incident_id)
                .join(LogRecord, IncidentLog.log_id == LogRecord.id)
                .where(LogRecord.service == service)
            )
        )
    total = session.scalar(select(func.count()).select_from(Incident).where(*filters))
    incidents = session.scalars(
        select(Incident)
        .where(*filters)
        .order_by(Incident.last_seen.desc(), Incident.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return {
        "items": [incident_data(item, session) for item in incidents],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{incident_id}", response_model=IncidentDetail)
def get_incident(incident_id: int, session: DB):
    incident = session.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident_data(incident, session, detail=True)


@router.patch("/{incident_id}", response_model=IncidentRead)
def update_incident(incident_id: int, update: IncidentUpdate, session: DB):
    incident = session.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident.status = update.status.value
    incident.updated_at = datetime.now(UTC)
    session.flush()
    result = incident_data(incident, session)
    session.commit()
    return result
