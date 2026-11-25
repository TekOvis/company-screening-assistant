"""call_llm: every attempt is counted, only temporary errors are retried, a spend limit
stops the whole run. Uses a scripted fake client and a fake sleep (no waiting)."""

import anthropic
import httpx2
import pytest

from screener.budget import BudgetExceeded, CompanyBudget, RunBudget, estimate_request_tokens
from screener.fake_llm import FakeAnthropicClient, text_message
from screener.llm import (
    LLMCallError,
    call_llm,
    create_client,
    describe_api_error,
    is_retryable,
    retry_delay,
)
from screener.schemas import Usage

from .conftest import make_settings

PARAMS = {
    "model": "claude-sonnet-5-5",
    "max_tokens": 1_000,
    "system": "system prompt",
    "messages": [{"role": "user", "content": "hello " * 50}],
}
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, status, *, headers=None, error_code=None, message="error"):
    error = {"type": "error", "message": message}
    if error_code:
        error["details"] = {"error_code": error_code}
    body = {"type": "error", "error": error}
    response = httpx2.Response(status, request=REQUEST, headers=headers or {}, json=body)
    return cls(message, response=response, body=body)


async def _call(client, settings, *, budget=None, sleeps=None):
    usage = Usage()
    budget = budget or CompanyBudget.from_settings(RunBudget.from_settings(settings), settings)

    async def fake_sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)

    response = await call_llm(
        client, PARAMS, budget=budget, usage=usage, settings=settings, sleep=fake_sleep
    )
    return response, usage, budget


@pytest.mark.anyio
async def test_temporary_errors_are_retried_and_every_attempt_counts():
    settings = make_settings(llm_max_retries=2)
    client = FakeAnthropicClient(
        [
            anthropic.APIConnectionError(request=REQUEST),
            _status_error(anthropic.InternalServerError, 529, message="overloaded"),
            text_message("{}", input_tokens=200, output_tokens=20),
        ]
    )
    sleeps = []
    _, usage, budget = await _call(client, settings, sleeps=sleeps)
    assert (usage.llm_requests, usage.failed_requests, usage.llm_calls) == (3, 2, 1)
    assert (usage.input_tokens, usage.output_tokens) == (200, 20)
    assert budget.requests == 3 and budget.run.requests == 3
    assert budget.used_tokens == 220 and budget.reserved_tokens == 0  # reservation settled
    assert len(sleeps) == 2


@pytest.mark.anyio
async def test_gives_up_after_max_retries():
    settings = make_settings(llm_max_retries=1)
    client = FakeAnthropicClient([anthropic.APIConnectionError(request=REQUEST)] * 2)
    with pytest.raises(LLMCallError) as info:
        await _call(client, settings)
    assert info.value.message == "could not connect to the API"
    assert len(client.requests) == 2


@pytest.mark.anyio
async def test_permanent_errors_are_not_retried():
    settings = make_settings(llm_max_retries=2)
    client = FakeAnthropicClient(
        [_status_error(anthropic.BadRequestError, 400, message="bad schema")]
    )
    with pytest.raises(LLMCallError) as info:
        await _call(client, settings)
    assert "bad schema" in info.value.message
    assert len(client.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        _status_error(
            anthropic.EnforcedSpendLimitReachedError, 429, error_code="enforced_spend_limit_reached"
        ),
        _status_error(
            anthropic.BadRequestError,
            400,
            message="You have reached your specified API usage limits.",
        ),
    ],
)
async def test_spend_limit_is_never_retried_and_stops_the_run(error):
    settings = make_settings(llm_max_retries=2)
    run = RunBudget.from_settings(settings)
    budget = CompanyBudget.from_settings(run, settings)
    client = FakeAnthropicClient([error])
    with pytest.raises(LLMCallError) as info:
        await _call(client, settings, budget=budget)
    assert info.value.spend_limit
    assert len(client.requests) == 1
    assert run.stop_reason == "the API spend limit was reached"
    # Any other company in the same run is now refused before sending anything.
    other = CompanyBudget.from_settings(run, settings)
    with pytest.raises(BudgetExceeded):
        other.reserve(10)


@pytest.mark.anyio
async def test_request_limit_includes_retries():
    settings = make_settings(llm_max_retries=5, max_llm_requests_per_company=2)
    client = FakeAnthropicClient([anthropic.APIConnectionError(request=REQUEST)] * 3)
    with pytest.raises(BudgetExceeded):
        await _call(client, settings)
    assert len(client.requests) == 2  # the third attempt was refused by the budget


@pytest.mark.anyio
async def test_token_reservation_refuses_calls_that_might_not_fit():
    settings = make_settings()
    run = RunBudget(max_tokens=100_000, max_requests=10)
    budget = CompanyBudget(run=run, max_tokens=estimate_request_tokens(PARAMS) - 1, max_requests=5)
    with pytest.raises(BudgetExceeded):
        await _call(FakeAnthropicClient([]), settings, budget=budget)
    assert budget.requests == 0


def test_run_budget_is_shared_between_companies():
    run = RunBudget(max_tokens=1_000, max_requests=3)
    first = CompanyBudget(run=run, max_tokens=1_000, max_requests=3)
    second = CompanyBudget(run=run, max_tokens=1_000, max_requests=3)
    first.reserve(600)
    first.settle(600, 500)
    with pytest.raises(BudgetExceeded):  # 500 used + 600 reserved > 1000 for the run
        second.reserve(600)
    second.reserve(400)
    assert run.requests == 2


def test_retry_delay_respects_retry_after_and_is_bounded():
    limited = _status_error(anthropic.RateLimitError, 429, headers={"retry-after": "7"})
    assert retry_delay(limited, 1) == 7.0
    huge = _status_error(anthropic.RateLimitError, 429, headers={"retry-after": "3600"})
    assert retry_delay(huge, 1) == 30.0
    assert 0 < retry_delay(anthropic.APIConnectionError(request=REQUEST), 1) <= 2.0


def test_retryable_classification():
    assert is_retryable(anthropic.APIConnectionError(request=REQUEST))
    assert is_retryable(_status_error(anthropic.RateLimitError, 429))
    assert is_retryable(_status_error(anthropic.InternalServerError, 503))
    assert not is_retryable(_status_error(anthropic.BadRequestError, 400))
    assert not is_retryable(_status_error(anthropic.AuthenticationError, 401))


def test_error_descriptions_never_leak_request_details():
    request = httpx2.Request(
        "POST", "https://api.anthropic.com/v1/messages", headers={"x-api-key": "secret"}
    )
    message = describe_api_error(anthropic.APITimeoutError(request=request))
    assert message == "the API request timed out"
    assert "secret" not in describe_api_error(_status_error(anthropic.AuthenticationError, 401))


def test_live_backend_requires_a_key():
    with pytest.raises(RuntimeError):
        create_client(make_settings(llm_backend="anthropic", anthropic_api_key=None))
