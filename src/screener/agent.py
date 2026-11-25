"""Running the LLM assessment for one company.

Structured output: the JSON Schema of ``Assessment`` is sent as ``output_config.format``,
so the API constrains the model's final answer to that shape. We still validate the reply
with Pydantic, because (a) the API ignores some constraints such as maximum string length
and (b) code should never trust that an external service behaved as documented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import anthropic
from pydantic import ValidationError

from .config import Settings
from .fake_llm import FakeAnthropicClient
from .fetcher import FetchedPage, PageFetcher
from .prompts import initial_user_message, system_prompt
from .schemas import Assessment, ResultStatus, ScreeningCriteria, Usage

logger = logging.getLogger(__name__)

# transform_schema (from the SDK) keeps what the API supports, forces
# additionalProperties: false, and moves unsupported limits (maxLength, ...) into the
# field descriptions. Pydantic enforces those limits after the reply arrives.
ASSESSMENT_SCHEMA: dict[str, Any] = anthropic.transform_schema(Assessment)


class ConfigurationError(RuntimeError):
    """The app is configured in a way that cannot work (e.g. missing API key)."""


def create_client(settings: Settings) -> Any:
    """Return the real async Anthropic client, or the fake one in mock mode."""
    if settings.llm_backend == "mock":
        return FakeAnthropicClient()
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
    if not key.strip():
        raise ConfigurationError("LLM_BACKEND=anthropic requires the ANTHROPIC_API_KEY variable")
    return anthropic.AsyncAnthropic(
        api_key=key,
        max_retries=settings.llm_sdk_max_retries,
        timeout=settings.llm_request_timeout_s,
    )


def model_label(settings: Settings) -> str:
    return "mock-heuristic (no LLM)" if settings.llm_backend == "mock" else settings.llm_model


@dataclass
class AgentOutcome:
    status: ResultStatus
    assessment: Assessment | None = None
    error: str | None = None
    usage: Usage = field(default_factory=Usage)
    pages: list[FetchedPage] = field(default_factory=list)
    tool_calls: int = 0
    warnings: list[str] = field(default_factory=list)


def build_request(settings: Settings, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Parameters for ``client.messages.create``."""
    return {
        "model": settings.llm_model,
        "max_tokens": settings.llm_max_output_tokens,
        "system": system_prompt(max_tool_calls=0),
        "messages": messages,
        "output_config": {
            "effort": settings.llm_effort,
            "format": {"type": "json_schema", "schema": ASSESSMENT_SCHEMA},
        },
    }


def usage_from_response(response: Any) -> Usage:
    usage = response.usage
    return Usage(
        llm_calls=1,
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
    )


async def assess_company(
    client: Any,
    settings: Settings,
    criteria: ScreeningCriteria,
    homepage: FetchedPage,
    fetcher: PageFetcher,
) -> AgentOutcome:
    """Stage-2 version: one LLM call with structured output, homepage text only."""
    outcome = AgentOutcome(status="ok", pages=[homepage])
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": initial_user_message(criteria, homepage)}
    ]
    try:
        response = await client.messages.create(**build_request(settings, messages))
    except anthropic.APIError as exc:
        outcome.status, outcome.error = "llm_error", describe_api_error(exc)
        return outcome
    outcome.usage.add(usage_from_response(response))

    if response.stop_reason == "refusal":
        outcome.status, outcome.error = "refused", "the model declined to answer"
        return outcome
    if response.stop_reason == "max_tokens":
        outcome.status = "invalid_output"
        outcome.error = "the answer was cut off at the output token limit"
        return outcome

    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        outcome.assessment = Assessment.model_validate_json(text)
    except ValidationError as exc:
        outcome.status = "invalid_output"
        outcome.error = f"the answer did not match the schema ({exc.error_count()} errors)"
    return outcome


def describe_api_error(exc: anthropic.APIError) -> str:
    """A short, user-safe description of an API error (never includes credentials)."""
    if isinstance(exc, anthropic.EnforcedSpendLimitReachedError):
        return "the API organization reached its spend limit; requests are paused"
    if isinstance(exc, anthropic.AuthenticationError):
        return "the API key was rejected"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "the API key is not allowed to use this model"
    if isinstance(exc, anthropic.RateLimitError):
        return "rate limited by the API; try again later"
    if isinstance(exc, anthropic.BadRequestError):
        if "usage limits" in exc.message:
            return "a custom API spend limit was reached"
        return f"the API rejected the request: {exc.message[:200]}"
    if isinstance(exc, anthropic.APITimeoutError):
        return "the API request timed out"
    if isinstance(exc, anthropic.APIConnectionError):
        return "could not connect to the API"
    if isinstance(exc, anthropic.InternalServerError):
        return "the API is temporarily unavailable"
    if isinstance(exc, anthropic.APIStatusError):
        return f"API error (HTTP {exc.status_code})"
    return "unexpected API error"
