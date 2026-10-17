from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import Base, engine, get_session
from app.models import LogRecord
from app.schemas import (
    LogBatch,
    LogPage,
    LogRead,
    Severity,
    normalize_service,
    normalize_severity,
    utc_timestamp,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Phase 1 bootstrap. Use migrations before evolving a deployed schema.
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


app = FastAPI(title="TraceMind", version="0.1.0", lifespan=lifespan)
DB = Annotated[Session, Depends(get_session)]


@app.get("/health")
def health(session: DB):
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    return {"status": "ok"}


@app.post("/logs", response_model=list[LogRead], status_code=201)
def ingest_logs(batch: LogBatch, session: DB):
    records = [
        LogRecord(**log.model_dump(exclude={"severity"}), severity=log.severity.value)
        for log in batch.logs
    ]
    try:
        session.add_all(records)
        session.flush()
        saved = [LogRead.model_validate(record) for record in records]
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=503, detail="Unable to persist logs") from None
    return saved


@app.get("/logs", response_model=LogPage)
def list_logs(
    session: DB,
    service: str | None = None,
    severity: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    try:
        service = normalize_service(service) if service is not None else None
        severity = Severity(normalize_severity(severity)) if severity is not None else None
        start = utc_timestamp(start) if start is not None else None
        end = utc_timestamp(end) if end is not None else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="start must be before or equal to end")

    filters = []
    if service is not None:
        filters.append(LogRecord.service == service)
    if severity is not None:
        filters.append(LogRecord.severity == severity.value)
    if start is not None:
        filters.append(LogRecord.timestamp >= start)
    if end is not None:
        filters.append(LogRecord.timestamp <= end)
    try:
        total = session.scalar(select(func.count()).select_from(LogRecord).where(*filters))
        items = session.scalars(
            select(LogRecord)
            .where(*filters)
            .order_by(LogRecord.timestamp, LogRecord.id)
            .offset(offset)
            .limit(limit)
        ).all()
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="Unable to retrieve logs") from None
    return {"items": items, "total": total, "limit": limit, "offset": offset}
