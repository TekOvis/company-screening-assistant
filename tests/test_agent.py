"""Tests of the LLM call logic with a scripted fake client (no API, no network)."""

import json

import anthropic
import httpx2
import pytest

from screener.agent import ASSESSMENT_SCHEMA, assess_company, describe_api_error
from screener.fake_llm import FakeAnthropicClient, make_message, text_message
from screener.schemas import ScreeningCriteria

CRITERIA = ScreeningCriteria(
    industry="industrial valves",
    geography="Germany",
    target_business="Manufacturer of industrial valves",
)

VALID_ANSWER = {
    "company_name": "Acme Valves",
    "description": "Designs and manufactures industrial valves for process industries.",
    "industry": "Industrial valves",
    "geography": ["Stuttgart, Germany"],
    "industry_match": "met",
    "geography_match": "met",
    "business_match": "met",
    "exclusion_triggered": False,
    "fit": "fit",
    "rationale": "Valve manufacturer headquartered in Germany.",
    "evidence": [
        {
            "supports": "geography",
            "claim": "The company is headquartered in Stuttgart, Germany.",
            "url": "https://acme-valves.example/",
            "quote": "Acme Valves GmbH is headquartered in Stuttgart, Germany.",
        }
    ],
}


async def _run(client, settings, acme_fetcher):
    homepage = await acme_fetcher.fetch("https://acme-valves.example/")
    return await assess_company(client, settings, CRITERIA, homepage, acme_fetcher)


@pytest.mark.anyio
async def test_valid_structured_answer(settings, acme_fetcher):
    client = FakeAnthropicClient(
        [text_message(json.dumps(VALID_ANSWER), input_tokens=1200, output_tokens=300)]
    )
    outcome = await _run(client, settings, acme_fetcher)
    assert outcome.status == "ok"
    assert outcome.assessment.fit == "fit"
    assert outcome.usage.llm_calls == 1
    assert (outcome.usage.input_tokens, outcome.usage.output_tokens) == (1200, 300)


@pytest.mark.anyio
async def test_request_uses_structured_output_and_marks_page_text(settings, acme_fetcher):
    client = FakeAnthropicClient([text_message(json.dumps(VALID_ANSWER))])
    await _run(client, settings, acme_fetcher)
    request = client.requests[0]
    assert request["model"] == settings.llm_model
    assert request["output_config"]["effort"] == settings.llm_effort
    assert request["output_config"]["format"] == {
        "type": "json_schema",
        "schema": ASSESSMENT_SCHEMA,
    }
    assert "tools" not in request  # stage 2: no tool calling yet
    content = request["messages"][0]["content"]
    assert "<screening_criteria>" in content
    assert '<page url="https://acme-valves.example/">' in content
    assert "untrusted" in request["system"]


@pytest.mark.anyio
async def test_reads_text_blocks_and_skips_thinking_blocks(settings, acme_fetcher):
    reply = make_message(
        [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": json.dumps(VALID_ANSWER)},
        ]
    )
    outcome = await _run(FakeAnthropicClient([reply]), settings, acme_fetcher)
    assert outcome.status == "ok"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad_text",
    [
        "not json at all",
        json.dumps({**VALID_ANSWER, "fit": "maybe"}),  # not an allowed label
        json.dumps({k: v for k, v in VALID_ANSWER.items() if k != "evidence"}),  # missing field
        json.dumps({**VALID_ANSWER, "rationale": "x" * 2001}),  # too long: API can't enforce it
    ],
)
async def test_answers_that_fail_schema_validation(bad_text, settings, acme_fetcher):
    outcome = await _run(FakeAnthropicClient([text_message(bad_text)]), settings, acme_fetcher)
    assert outcome.status == "invalid_output"
    assert outcome.assessment is None
    assert outcome.usage.llm_calls == 1  # the tokens were still spent and are counted


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("stop_reason", "status"), [("refusal", "refused"), ("max_tokens", "invalid_output")]
)
async def test_stop_reasons(stop_reason, status, settings, acme_fetcher):
    reply = text_message("{", stop_reason=stop_reason)
    outcome = await _run(FakeAnthropicClient([reply]), settings, acme_fetcher)
    assert outcome.status == status


@pytest.mark.anyio
async def test_api_errors_become_llm_errors(settings, acme_fetcher):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    client = FakeAnthropicClient([anthropic.APIConnectionError(request=request)])
    outcome = await _run(client, settings, acme_fetcher)
    assert outcome.status == "llm_error"
    assert outcome.error == "could not connect to the API"


def test_describe_api_error_never_leaks_request_details():
    request = httpx2.Request(
        "POST", "https://api.anthropic.com/v1/messages", headers={"x-api-key": "secret"}
    )
    message = describe_api_error(anthropic.APITimeoutError(request=request))
    assert message == "the API request timed out"
    assert "secret" not in message


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_schema_sent_to_api_uses_only_supported_keywords():
    for node in _walk(ASSESSMENT_SCHEMA):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False
        for unsupported in ("maxLength", "minLength", "maxItems", "maximum", "minimum", "oneOf"):
            assert unsupported not in node
    assert set(ASSESSMENT_SCHEMA["required"]) == set(ASSESSMENT_SCHEMA["properties"])
