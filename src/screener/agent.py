"""The agent loop for one company: model turns plus fetch_company_page tool calls.

Structured output: the JSON Schema of ``Assessment`` is sent as ``output_config.format``,
so the API constrains the model's final answer to that shape. We still validate the reply
with Pydantic, because (a) the API ignores some constraints such as maximum string length
and (b) code should never trust that an external service behaved as documented.

Limits are enforced in code, never left to the model:
* at most MAX_LLM_CALLS model turns. On the last turn tools are switched off with
  tool_choice "none" — that forbids tool calls but does not guarantee a usable answer, so
  every way a turn can end is still handled explicitly below;
* at most MAX_TOOL_CALLS tool calls in total (invalid calls count too);
* token and request budgets before every API attempt (budget.py).
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
from .tools import FETCH_TOOL, run_fetch_tool, site_url_for, tool_result

# transform_schema (from the SDK) keeps what the API supports, forces
# additionalProperties: false, and moves unsupported limits (maxLength, ...) into the
# field descriptions. Pydantic enforces those limits after the reply arrives.
ASSESSMENT_SCHEMA: dict[str, Any] = anthropic.transform_schema(Assessment)


@dataclass
class AgentOutcome:
    status: ResultStatus
    assessment: Assessment | None = None
    error: str | None = None
    pages: dict[str, FetchedPage] = field(default_factory=dict)  # final URL -> page
    tool_calls: int = 0


def build_agent_request(
    settings: Settings, messages: list[dict[str, Any]], *, tools_allowed: bool
) -> dict[str, Any]:
    """Parameters for ``client.messages.create``."""
    params: dict[str, Any] = {
        "model": settings.llm_model,
        "max_tokens": settings.llm_max_output_tokens,
        "system": system_prompt(settings.max_tool_calls),
        "messages": messages,
        "output_config": {
            "effort": settings.llm_effort,
            "format": {"type": "json_schema", "schema": ASSESSMENT_SCHEMA},
        },
    }
    if settings.max_tool_calls > 0:
        # The tool stays defined on every turn (earlier turns contain tool calls);
        # tool_choice "none" switches tool use off for the final turn.
        params["tools"] = [FETCH_TOOL]
        params["tool_choice"] = {"type": "auto" if tools_allowed else "none"}
    return params


async def run_agent(
    client: Any,
    settings: Settings,
    criteria: ScreeningCriteria,
    homepage: FetchedPage,
    fetcher: PageFetcher,
    *,
    budget: CompanyBudget,
    usage: Usage,
) -> AgentOutcome:
    outcome = AgentOutcome(status="ok", pages={homepage.url: homepage})
    site_url = site_url_for(homepage)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": initial_user_message(criteria, homepage)}
    ]

    def fail(status: ResultStatus, error: str) -> AgentOutcome:
        outcome.status, outcome.error = status, error
        return outcome

    for turn in range(1, settings.max_llm_calls + 1):
        tools_allowed = (
            turn < settings.max_llm_calls and outcome.tool_calls < settings.max_tool_calls
        )
        params = build_agent_request(settings, messages, tools_allowed=tools_allowed)
        try:
            response = await call_llm(client, params, budget=budget, usage=usage, settings=settings)
        except BudgetExceeded as exc:
            return fail("budget_exceeded", str(exc))
        except LLMCallError as exc:
            return fail("llm_error", exc.message)

        if response.stop_reason == "refusal":
            return fail("refused", refusal_message(response))
        if response.stop_reason == "max_tokens":
            return fail("invalid_output", "the answer was cut off at the output token limit")

        if response.stop_reason == "tool_use":
            tool_blocks = [block for block in response.content if block.type == "tool_use"]
            if not tools_allowed:
                return fail(
                    "invalid_output", "the model requested a tool after tools were disabled"
                )
            if not tool_blocks:
                return fail("invalid_output", "tool_use stop without a tool call")
            # Pass the assistant turn back unchanged (including thinking blocks).
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in tool_blocks:
                outcome.tool_calls += 1
                if outcome.tool_calls > settings.max_tool_calls:
                    results.append(
                        tool_result(
                            block.id,
                            "Tool call limit reached. Give your final answer.",
                            is_error=True,
                        )
                    )
                    continue
                results.append(
                    await run_fetch_tool(
                        block, site_url=site_url, fetcher=fetcher, pages=outcome.pages
                    )
                )
            # All results of one turn go back together in a single user message.
            messages.append({"role": "user", "content": results})
            continue

        if response.stop_reason not in ("end_turn", "stop_sequence"):
            return fail("invalid_output", f"unexpected stop reason: {response.stop_reason}")
        text = final_text(response)
        if not text.strip():
            return fail("invalid_output", "the model returned no final answer")
        try:
            outcome.assessment = Assessment.model_validate_json(text)
        except ValidationError as exc:
            return fail(
                "invalid_output",
                f"the answer did not match the schema ({exc.error_count()} errors)",
            )
        return outcome

    return fail("invalid_output", "no final answer within the model call limit")
