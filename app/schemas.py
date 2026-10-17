import re
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class Severity(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


def normalize_severity(value: str) -> str:
    if not isinstance(value, str):
        # Pydantic treats TypeError as a programming error, not input validation.
        raise ValueError("severity must be a string")  # noqa: TRY004
    value = value.strip().upper()
    return {"WARN": "WARNING", "FATAL": "CRITICAL"}.get(value, value)


def normalize_service(value: str) -> str:
    value = re.sub(r"[\s_]+", "-", value.strip().lower())
    if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", value) or len(value) > 100:
        raise ValueError("service must be a name of 1–100 letters/digits with dots or hyphens")
    return value


def utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone, e.g. 2026-10-17T10:00:00Z")
    return value.astimezone(UTC)


class LogCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    service: str
    severity: Severity
    message: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32000)
    ]
    request_id: ShortText | None = None
    trace_id: ShortText | None = None
    exception_type: ShortText | None = None

    _timestamp = field_validator("timestamp")(utc_timestamp)
    _service = field_validator("service")(normalize_service)
    _severity = field_validator("severity", mode="before")(normalize_severity)


class LogBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logs: list[LogCreate] = Field(min_length=1, max_length=1000)


class LogRead(LogCreate):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int

    @field_validator("timestamp", mode="before")
    @classmethod
    def restore_sqlite_timezone(cls, value: datetime) -> datetime:
        # SQLite drops timezone metadata; PostgreSQL preserves it. All writes are UTC.
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class LogPage(BaseModel):
    items: list[LogRead]
    total: int
    limit: int
    offset: int
