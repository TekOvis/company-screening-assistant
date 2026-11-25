"""Data models (Pydantic) for user input, the LLM's structured output and final results.

Three groups:
1. Input      — what the user submits (criteria + URLs).
2. Assessment — the JSON object the LLM must return. Its JSON Schema is sent to the API
                (structured output) and the reply is validated again here with Pydantic.
3. Results    — what the app shows and exports, including errors, usage and warnings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

DISCLAIMER = (
    "Educational prototype. Not financial or investment advice. Results are generated "
    "automatically from public website text and may be incomplete or wrong."
)

FitLabel = Literal["fit", "non_fit", "insufficient_information"]
CriterionStatus = Literal["met", "not_met", "unknown"]
EvidenceTarget = Literal[
    "company_name", "description", "industry", "geography", "business", "exclusion"
]


# --- 1. Input ------------------------------------------------------------------------


class ScreeningCriteria(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    industry: str = Field(min_length=1, max_length=200)
    geography: str = Field(min_length=1, max_length=200)
    target_business: str = Field(min_length=1, max_length=1000)
    exclusions: str | None = Field(default=None, max_length=1000)

    @field_validator("exclusions")
    @classmethod
    def _empty_to_none(cls, value: str | None) -> str | None:
        return value or None


class ScreeningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The upper limit (10 by default) is enforced by the app from settings.
    urls: list[str] = Field(min_length=1)
    criteria: ScreeningCriteria


# --- 2. Structured output expected from the LLM ---------------------------------------


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supports: EvidenceTarget = Field(
        description="Which output field or criterion this quote supports."
    )
    claim: str = Field(
        max_length=500,
        description="The specific factual statement that the quote supports.",
    )
    url: str = Field(
        max_length=2000,
        description="URL of the page the quote was copied from, exactly as given.",
    )
    quote: str = Field(
        max_length=500,
        description="Verbatim passage copied from that page's text (at most 300 characters).",
    )


class Assessment(BaseModel):
    """The JSON object the model must return as its final answer."""

    model_config = ConfigDict(extra="forbid")

    company_name: str | None = Field(
        max_length=200, description="Company name as stated in the text, or null."
    )
    description: str | None = Field(
        max_length=1000,
        description="One or two sentences on what the company does, based only on the text.",
    )
    industry: str | None = Field(
        max_length=200, description="Short industry label grounded in the text, or null."
    )
    geography: list[str] = Field(
        max_length=20,
        description=(
            "Places the text explicitly says the company is based in, has offices in, or "
            "operates in, written as they appear in the text. Empty if not stated."
        ),
    )
    industry_match: CriterionStatus
    geography_match: CriterionStatus
    business_match: CriterionStatus
    exclusion_triggered: bool
    fit: FitLabel
    rationale: str = Field(max_length=2000, description="1-3 sentences grounded in evidence.")
    evidence: list[EvidenceItem] = Field(max_length=30)


# --- 3. Results ----------------------------------------------------------------------

ResultStatus = Literal[
    "ok",
    "invalid_url",
    "fetch_error",
    "llm_error",
    "invalid_output",
    "refused",
    "budget_exceeded",
    "internal_error",
]


class Usage(BaseModel):
    """API usage for one company (or a whole run)."""

    llm_requests: int = 0  # every attempt, including retries and failed ones
    failed_requests: int = 0  # attempts that ended in an error (no usage is reported)
    llm_calls: int = 0  # successful responses; their usage is counted below
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def add(self, other: Usage) -> None:
        for name in type(self).model_fields:
            setattr(self, name, getattr(self, name) + getattr(other, name))


class CompanyResult(BaseModel):
    position: int = 0  # order in the user's input list
    input_url: str
    status: ResultStatus
    error: str | None = None

    company_name: str | None = None
    description: str | None = None
    industry: str | None = None
    geography: list[str] = Field(default_factory=list)
    industry_match: CriterionStatus | None = None
    geography_match: CriterionStatus | None = None
    business_match: CriterionStatus | None = None
    exclusion_triggered: bool | None = None
    fit: FitLabel | None = None
    rationale: str | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)

    warnings: list[str] = Field(default_factory=list)
    pages_fetched: list[str] = Field(default_factory=list)
    tool_calls: int = 0
    usage: Usage = Field(default_factory=Usage)
    duration_s: float = 0.0


def utc_now() -> datetime:
    return datetime.now(UTC)


class ScreeningRun(BaseModel):
    run_id: str
    status: Literal["running", "done", "failed"] = "running"
    backend: str
    model: str
    created_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    criteria: ScreeningCriteria
    urls: list[str]
    results: list[CompanyResult] = Field(default_factory=list)
    error: str | None = None
    stop_reason: str | None = None  # e.g. the API spend limit was reached
    disclaimer: str = DISCLAIMER

    @computed_field  # included in JSON exports
    @property
    def total_usage(self) -> Usage:
        total = Usage()
        for result in self.results:
            total.add(result.usage)
        return total
