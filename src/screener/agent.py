"""Running the LLM assessment for one company.

Structured output: the JSON Schema of ``Assessment`` is sent as ``output_config.format``,
so the API constrains the model's final answer to that shape. We still validate the reply
with Pydantic, because (a) the API ignores some constraints such as maximum string length
and (b) code should never trust that an external service behaved as documented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anthropic
from pydantic import ValidationError

from .budget import BudgetExceeded, CompanyBudget
from .config import Settings
from .fetcher import FetchedPage, PageFetcher
from .llm import LLMCallError, call_llm, final_text, refusal_message
from .prompts import initial_user_message, system_prompt
from .schemas import Assessment, ResultStatus, ScreeningCriteria, Usage

# transform_schema (from the SDK) keeps what the API supports, forces
# additionalProperties: false, and moves unsupported limits (maxLength, ...) into the
# field descriptions. Pydantic enforces those limits after the reply arrives.
ASSESSMENT_SCHEMA: dict[str, Any] = anthropic.transform_schema(Assessment)


@dataclass
class AgentOutcome:
    status: ResultStatus
    assessment: Assessment | None = None
    error: str | None = None
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


async def assess_company(
    client: Any,
    settings: Settings,
    criteria: ScreeningCriteria,
    homepage: FetchedPage,
    fetcher: PageFetcher,
    *,
    budget: CompanyBudget,
    usage: Usage,
) -> AgentOutcome:
    """One LLM call with structured output, homepage text only."""
    outcome = AgentOutcome(status="ok", pages=[homepage])
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": initial_user_message(criteria, homepage)}
    ]

    def fail(status: ResultStatus, error: str) -> AgentOutcome:
        outcome.status, outcome.error = status, error
        return outcome

    try:
        response = await call_llm(
            client, build_request(settings, messages), budget=budget, usage=usage, settings=settings
        )
    except BudgetExceeded as exc:
        return fail("budget_exceeded", str(exc))
    except LLMCallError as exc:
        return fail("llm_error", exc.message)

    if response.stop_reason == "refusal":
        return fail("refused", refusal_message(response))
    if response.stop_reason == "max_tokens":
        return fail("invalid_output", "the answer was cut off at the output token limit")
    if response.stop_reason not in ("end_turn", "stop_sequence"):
        return fail("invalid_output", f"unexpected stop reason: {response.stop_reason}")

    text = final_text(response)
    if not text.strip():
        return fail("invalid_output", "the model returned no final answer")
    try:
        outcome.assessment = Assessment.model_validate_json(text)
    except ValidationError as exc:
        return fail(
            "invalid_output", f"the answer did not match the schema ({exc.error_count()} errors)"
        )
    return outcome
