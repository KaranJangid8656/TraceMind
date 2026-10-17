from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import get_session
from app.llm import ProviderFailure, RCAProvider, get_rca_provider
from app.models import Incident, RCAReport
from app.rca import create_report, report_data
from app.rca_schemas import RCAReportPage, RCAReportRead, RCARequest

router = APIRouter(prefix="/incidents", tags=["RCA"])
DB = Annotated[Session, Depends(get_session)]
Provider = Annotated[RCAProvider, Depends(get_rca_provider)]


@router.post("/{incident_id}/analyze", response_model=RCAReportRead, status_code=201)
def analyze_incident(
    incident_id: int, session: DB, provider: Provider, request: RCARequest | None = None
):
    incident = session.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    try:
        return create_report(session, incident, request or RCARequest(), Settings(), provider)
    except ProviderFailure as exc:
        session.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None


@router.get("/{incident_id}/analyses", response_model=RCAReportPage)
def list_reports(
    incident_id: int,
    session: DB,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    if session.get(Incident, incident_id) is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    condition = RCAReport.incident_id == incident_id
    total = session.scalar(select(func.count()).select_from(RCAReport).where(condition))
    reports = session.scalars(
        select(RCAReport)
        .where(condition)
        .order_by(RCAReport.created_at.desc(), RCAReport.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return {
        "items": [report_data(report) for report in reports],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{incident_id}/analyses/{report_id}", response_model=RCAReportRead)
def get_report(incident_id: int, report_id: int, session: DB):
    report = session.scalar(
        select(RCAReport).where(
            RCAReport.id == report_id,
            RCAReport.incident_id == incident_id,
        )
    )
    if report is None:
        raise HTTPException(status_code=404, detail="RCA report not found")
    return report_data(report)
