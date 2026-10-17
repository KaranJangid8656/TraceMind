from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class LogRecord(Base):
    __tablename__ = "logs"
    __table_args__ = (
        Index("ix_logs_timestamp_id", "timestamp", "id"),
        Index("ix_logs_service_timestamp", "service", "timestamp"),
        Index("ix_logs_severity_timestamp", "severity", "timestamp"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    service: Mapped[str] = mapped_column(String(100))
    severity: Mapped[str] = mapped_column(String(10))
    message: Mapped[str] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    exception_type: Mapped[str | None] = mapped_column(String(200), nullable=True)


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'investigating', 'resolved')"),
        Index("ix_incidents_last_seen", "last_seen", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(250))
    severity: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="open")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    occurrence_count: Mapped[int] = mapped_column(default=0)
    detection_rules: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    evidence: Mapped[list[IncidentLog]] = relationship(back_populates="incident")


class IncidentLog(Base):
    __tablename__ = "incident_logs"

    # One assignment per original log makes detection reruns idempotent.
    log_id: Mapped[int] = mapped_column(ForeignKey("logs.id"), primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    reason: Mapped[str] = mapped_column(String(50))
    strength: Mapped[str] = mapped_column(String(20))
    related_log_id: Mapped[int | None] = mapped_column(ForeignKey("logs.id"), nullable=True)
    incident: Mapped[Incident] = relationship(back_populates="evidence")
    log: Mapped[LogRecord] = relationship(foreign_keys=[log_id])


class IncidentLink(Base):
    __tablename__ = "incident_links"

    upstream_log_id: Mapped[int] = mapped_column(ForeignKey("logs.id"), primary_key=True)
    downstream_log_id: Mapped[int] = mapped_column(ForeignKey("logs.id"), primary_key=True)
    upstream_incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    downstream_incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    reason: Mapped[str] = mapped_column(String(100))
    strength: Mapped[str] = mapped_column(String(20), default="weak")
