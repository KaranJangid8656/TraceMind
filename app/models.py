from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

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
