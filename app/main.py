import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import engine, get_session
from app.incidents import router as incidents_router
from app.migrations import MigrationStateError, verify_database_revision
from app.models import LogRecord
from app.rca_routes import router as rca_router
from app.schemas import (
    LogBatch,
    LogPage,
    LogRead,
    Severity,
    normalize_service,
    normalize_severity,
    utc_timestamp,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        verify_database_revision(engine)
    except MigrationStateError as exc:
        logger.error("event=startup_failed category=migration_state")
        engine.dispose()
        raise RuntimeError(f"Application startup blocked: {exc}") from None
    try:
        yield
    finally:
        engine.dispose()


app = FastAPI(title="TraceMind", version="0.3.0", lifespan=lifespan)
app.include_router(incidents_router)
app.include_router(rca_router)
DB = Annotated[Session, Depends(get_session)]


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(request, exc):
    return JSONResponse(status_code=503, content={"detail": "Database operation unavailable"})


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
