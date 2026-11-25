"""Talking to the Claude API: creating the client and making one call.

Why our own retry loop instead of the SDK's? With SDK retries the extra attempts are
invisible to the app. Here every attempt is counted against the request and token budgets,
waiting between attempts is bounded, and a reached spend limit is never retried — it stops
the whole run instead.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any

import anthropic

from .budget import CompanyBudget, estimate_request_tokens
from .config import Settings
from .fake_llm import FakeAnthropicClient
from .schemas import Usage

Sleep = Callable[[float], Awaitable[None]]
MAX_RETRY_WAIT_S = 30.0

# Errors that mean "no more money": never retry them, stop the run.
_SPEND_LIMIT_ERRORS: tuple[type[Exception], ...] = tuple(
    getattr(anthropic, name)
    for name in (
        "EnforcedSpendLimitReachedError",
        "ContractSpendLimitReachedError",
        "LinkedOrganizationSpendLimitReachedError",
    )
    if hasattr(anthropic, name)
)


class ConfigurationError(RuntimeError):
    """The app is configured in a way that cannot work (e.g. missing API key)."""


class LLMCallError(Exception):
    """An API call failed for good (after any retries)."""

    def __init__(self, message: str, *, spend_limit: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.spend_limit = spend_limit


def create_client(settings: Settings) -> Any:
    """Return the real async Anthropic client, or the fake one in mock mode."""
    if settings.llm_backend == "mock":
        return FakeAnthropicClient()
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
    if not key.strip():
        raise ConfigurationError("LLM_BACKEND=anthropic requires the ANTHROPIC_API_KEY variable")
    # max_retries=0: retries are done in call_llm so that every attempt is counted.
    return anthropic.AsyncAnthropic(
        api_key=key, max_retries=0, timeout=settings.llm_request_timeout_s
    )


def model_label(settings: Settings) -> str:
    return "mock-heuristic (no LLM)" if settings.llm_backend == "mock" else settings.llm_model


async def call_llm(
    client: Any,
    params: dict[str, Any],
    *,
    budget: CompanyBudget,
    usage: Usage,
    settings: Settings,
    sleep: Sleep = asyncio.sleep,
) -> Any:
    """One logical call with retries. Raises BudgetExceeded or LLMCallError."""
    attempt = 0
    while True:
        attempt += 1
        reserved = estimate_request_tokens(params)
        budget.reserve(reserved)  # raises BudgetExceeded before anything is sent
        usage.llm_requests += 1
        try:
            response = await client.messages.create(**params)
        except anthropic.APIError as exc:
            budget.settle(reserved, 0)
            usage.failed_requests += 1
            if is_spend_limit_error(exc):
                budget.run.stop("the API spend limit was reached")
                raise LLMCallError(describe_api_error(exc), spend_limit=True) from exc
            if is_retryable(exc) and attempt <= settings.llm_max_retries:
                await sleep(retry_delay(exc, attempt))
                continue
            raise LLMCallError(describe_api_error(exc)) from exc
        call_usage = usage_from_response(response)
        budget.settle(reserved, call_usage.total_tokens)
        usage.add(call_usage)
        return response


def is_spend_limit_error(exc: Exception) -> bool:
    if isinstance(exc, _SPEND_LIMIT_ERRORS):
        return True
    # A custom (organization or workspace) spend limit arrives as a 400 with this text.
    return isinstance(exc, anthropic.BadRequestError) and "usage limits" in exc.message


def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, anthropic.APIConnectionError):  # includes timeouts
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in (408, 409, 429) or exc.status_code >= 500
    return False


def retry_delay(exc: Exception, attempt: int) -> float:
    response = getattr(exc, "response", None)
    header = response.headers.get("retry-after") if response is not None else None
    if header:
        try:
            return min(max(float(header), 0.0), MAX_RETRY_WAIT_S)
        except ValueError:
            pass
    # Exponential backoff; the jitter only spreads retries out (not security relevant).
    return min(2.0**attempt, MAX_RETRY_WAIT_S) * (0.5 + random.random() / 2)  # noqa: S311


def usage_from_response(response: Any) -> Usage:
    usage = response.usage
    return Usage(
        llm_calls=1,
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
    )


def final_text(response: Any) -> str:
    """The text of a reply. Read blocks by type: a reply may start with thinking blocks."""
    return "".join(block.text for block in response.content if block.type == "text")


def refusal_message(response: Any) -> str:
    details = getattr(response, "stop_details", None)
    category = getattr(details, "category", None) if details else None
    if category:
        return f"the model declined to answer (category: {category})"
    return "the model declined to answer"


def describe_api_error(exc: Exception) -> str:
    """A short, user-safe description of an API error (never includes credentials)."""
    if is_spend_limit_error(exc):
        return "the API spend limit was reached; no further requests are sent"
    if isinstance(exc, anthropic.AuthenticationError):
        return "the API key was rejected"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "the API key is not allowed to use this model"
    if isinstance(exc, anthropic.NotFoundError):
        return "the model or endpoint was not found (check LLM_MODEL)"
    if isinstance(exc, anthropic.RateLimitError):
        return "rate limited by the API; try again later"
    if isinstance(exc, anthropic.BadRequestError):
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
