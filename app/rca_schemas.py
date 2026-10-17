from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

ReportText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Confidence = Literal["low", "medium", "high"]
LogIds = Annotated[list[StrictInt], Field(max_length=100)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RCARequest(StrictModel):
    max_logs: int = Field(default=40, ge=1, le=100)


class EvidenceClaim(StrictModel):
    observation: ReportText
    supporting_log_ids: Annotated[list[StrictInt], Field(min_length=1, max_length=100)]


class RankedCause(StrictModel):
    rank: int = Field(ge=1, le=5)
    cause: ReportText
    explanation: ReportText
    confidence: Confidence
    supporting_log_ids: Annotated[list[StrictInt], Field(min_length=1, max_length=100)]
    contradicting_log_ids: LogIds


class RCAOutput(StrictModel):
    # All fields are required for the provider's strict JSON Schema.
    status: Literal["hypothesis", "inconclusive"]
    summary: ReportText
    probable_cause: ReportText | None
    supporting_log_ids: LogIds
    affected_services: Annotated[list[ReportText], Field(max_length=100)]
    confidence: Confidence
    observed_facts: Annotated[list[EvidenceClaim], Field(max_length=30)]
    ranked_causes: Annotated[list[RankedCause], Field(max_length=5)]
    alternative_explanations: Annotated[list[ReportText], Field(max_length=10)]
    recommended_checks: Annotated[list[ReportText], Field(min_length=1, max_length=15)]
    evidence_gaps: Annotated[list[ReportText], Field(max_length=20)]


class RCAReportRead(BaseModel):
    id: int
    incident_id: int
    provider: str
    model: str | None
    prompt_version: str
    evidence_hash: str
    created_at: datetime
    duration_ms: int
    analysis: RCAOutput
    validation_notes: list[str]
    evidence_snapshot: dict
    causality: Literal["unconfirmed"] = "unconfirmed"


class RCAReportPage(BaseModel):
    items: list[RCAReportRead]
    total: int
    limit: int
    offset: int
