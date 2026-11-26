"""The geography rule agreed on 2026-11-26, along the whole path.

- met:     a confirmed own site in the target region (any one is enough);
- not_met: the text explicitly confirms that there is no own site in the region;
- unknown: only sites elsewhere are named, and it is not known whether that is all.

Checked at three levels: the instructions (agent and verifier prompts), the evidence
check with its label rules, and the mock end to end (agent -> checks -> label). The
companies and texts here are made up for these tests and are not evaluation cases.
"""

import json

import pytest

from screener.budget import CompanyBudget, RunBudget
from screener.fake_llm import FakeAnthropicClient
from screener.fixtures import FixtureFetcher
from screener.pipeline import screen_company
from screener.prompts import VERIFIER_PROMPT, system_prompt
from screener.schemas import (
    GEOGRAPHY_NO_SITE_CLAIM,
    GEOGRAPHY_SITE_CLAIM,
    Assessment,
    ScreeningCriteria,
    Usage,
)
from screener.verify import verify_assessment

from .conftest import make_settings

HOME = "https://alpen-pumps.example/"
DESCRIPTION_QUOTE = "Alpen Pumps designs and manufactures industrial pumps for water utilities."
HQ_ELSEWHERE = "Our headquarters are in Brno, Czechia."
SITE_IN_REGION = "We also run a service office in Vienna, Austria."
ONLY_SITE_ELSEWHERE = "Our only office is in Brno, Czechia."
NO_SITE_IN_REGION = "We have no office in Austria."


def _html(*sentences: str) -> str:
    body = "".join(f"<p>{s}</p>" for s in (DESCRIPTION_QUOTE, *sentences))
    return f"<html><head><title>Alpen Pumps | Pumps</title></head><body>{body}</body></html>"


def _ev(supports, quote, claim="claim"):
    return {"supports": supports, "claim": claim, "url": HOME, "quote": quote}


def _answer(geography_match, locations, location_quotes, geography_quotes=()) -> Assessment:
    return Assessment.model_validate(
        {
            "company_name": "Alpen Pumps",
            "description": "Designs and manufactures industrial pumps for water utilities.",
            "industry": "industrial pumps",
            "company_locations": locations,
            "markets_served": [],
            "industry_match": "met",
            "geography_match": geography_match,
            "business_match": "met",
            "exclusion_triggered": False,
            "fit": {"met": "fit", "not_met": "non_fit"}.get(
                geography_match, "insufficient_information"
            ),
            "rationale": "test",
            "evidence": [
                _ev("description", DESCRIPTION_QUOTE),
                _ev("industry", DESCRIPTION_QUOTE),
                *(_ev("company_location", q) for q in location_quotes),
                *(_ev("geography", q) for q in geography_quotes),
            ],
        }
    )


async def _verify(answer, html, region="Austria"):
    settings = make_settings()
    page = await FixtureFetcher({HOME: html}, settings).fetch(HOME)
    client = FakeAnthropicClient()  # mock verifier: keyword rules
    verified = await verify_assessment(
        answer,
        {page.url: page},
        target_geography=region,
        client=client,
        settings=settings,
        budget=CompanyBudget.from_settings(RunBudget.from_settings(settings), settings),
        usage=Usage(),
    )
    return verified, client


def _sent(client, field):
    content = client.requests[0]["messages"][0]["content"]
    items = json.loads(content.split("\n", 1)[1].rsplit("\n</", 1)[0])
    return next((item for item in items if item["field"] == field), None)


# 1. The instructions state the rule.


def test_the_agent_prompt_states_the_three_cases():
    prompt = system_prompt(max_tool_calls=3)
    assert "Any one is enough" in prompt
    assert '"not_met" only if the text explicitly confirms that the company has no own site' in (
        " ".join(prompt.split())
    )
    assert 'do NOT make it "not_met" by themselves' in " ".join(prompt.split())
    assert 'supports "geography"' in prompt


def test_the_verifier_prompt_does_not_accept_a_site_elsewhere_as_no_site():
    text = " ".join(VERIFIER_PROMPT.split())
    assert "A target-region claim" in text and "A site elsewhere does not support it" in text
    assert "A no-site claim" in text
    assert "only names a headquarters or an office elsewhere does NOT support it" in text


# 2. The evidence check and the label rules.


@pytest.mark.anyio
async def test_met_a_confirmed_own_site_in_the_region_is_enough():
    answer = _answer(
        "met", ["Brno", "Czechia", "Vienna", "Austria"], [HQ_ELSEWHERE, SITE_IN_REGION]
    )
    verified, client = await _verify(answer, _html(HQ_ELSEWHERE, SITE_IN_REGION))
    assert verified.geography_match == "met"
    assert verified.fit == "fit"
    claim = _sent(client, GEOGRAPHY_SITE_CLAIM)
    assert claim["value"] == "Austria"  # the user's region, written into the claim by code
    assert SITE_IN_REGION in claim["quotes"]


@pytest.mark.anyio
async def test_met_needs_the_site_to_be_in_the_region_not_just_somewhere():
    """A model that wrongly calls a site elsewhere "met" is caught by the region claim."""
    answer = _answer("met", ["Brno", "Czechia"], [HQ_ELSEWHERE])
    verified, _ = await _verify(answer, _html(HQ_ELSEWHERE))
    assert verified.company_locations == ["Brno", "Czechia"]  # the site itself is real
    assert verified.geography_match == "unknown"
    assert verified.fit == "insufficient_information"
    assert any("no verified own site in the target region" in w for w in verified.warnings)


@pytest.mark.anyio
@pytest.mark.parametrize("statement", [NO_SITE_IN_REGION, ONLY_SITE_ELSEWHERE])
async def test_not_met_an_explicit_statement_of_no_site_in_the_region(statement):
    answer = _answer("not_met", ["Brno", "Czechia"], [HQ_ELSEWHERE], [statement])
    verified, client = await _verify(answer, _html(HQ_ELSEWHERE, statement))
    assert verified.geography_match == "not_met"
    assert verified.fit == "non_fit"
    claim = _sent(client, GEOGRAPHY_NO_SITE_CLAIM)
    assert claim["value"] == "Austria" and claim["quotes"] == [statement]


@pytest.mark.anyio
async def test_unknown_a_site_elsewhere_filed_as_no_site_evidence_is_not_enough():
    answer = _answer("not_met", ["Brno", "Czechia"], [HQ_ELSEWHERE], [HQ_ELSEWHERE])
    verified, _ = await _verify(answer, _html(HQ_ELSEWHERE))
    assert verified.geography_match == "unknown"
    assert verified.fit == "insufficient_information"


@pytest.mark.anyio
async def test_unknown_not_met_without_any_no_site_quote():
    answer = _answer("not_met", ["Brno", "Czechia"], [HQ_ELSEWHERE])
    verified, client = await _verify(answer, _html(HQ_ELSEWHERE))
    assert verified.geography_match == "unknown"
    assert _sent(client, GEOGRAPHY_NO_SITE_CLAIM) is None  # no quote, nothing to check
    assert any("Sites named elsewhere are not enough" in w for w in verified.warnings)


# 3. The mock end to end: agent, evidence check and label rules together.

CRITERIA = ScreeningCriteria(
    industry="industrial pumps",
    geography="DACH (Germany, Austria, Switzerland)",
    target_business="Manufacturer of industrial pumps",
)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("sentences", "geography", "fit"),
    [
        ((HQ_ELSEWHERE, SITE_IN_REGION), "met", "fit"),
        ((HQ_ELSEWHERE,), "unknown", "insufficient_information"),
        (("Our team is based in Brno, Czechia.",), "unknown", "insufficient_information"),
        ((ONLY_SITE_ELSEWHERE,), "not_met", "non_fit"),
        ((HQ_ELSEWHERE, "We have no office in Germany."), "not_met", "non_fit"),
    ],
)
async def test_mock_pipeline_follows_the_rule(sentences, geography, fit):
    settings = make_settings()
    fetcher = FixtureFetcher({HOME: _html(*sentences)}, settings)
    result = await screen_company(
        0,
        HOME,
        CRITERIA,
        settings=settings,
        client=FakeAnthropicClient(),
        fetcher=fetcher,
        run_budget=RunBudget.from_settings(settings),
    )
    assert result.status == "ok", result.error
    assert (result.geography_match, result.fit) == (geography, fit)


@pytest.mark.anyio
async def test_mock_does_not_read_a_denied_site_as_an_own_location():
    settings = make_settings()
    fetcher = FixtureFetcher({HOME: _html("We have no office in Austria.")}, settings)
    result = await screen_company(
        0,
        HOME,
        CRITERIA,
        settings=settings,
        client=FakeAnthropicClient(),
        fetcher=fetcher,
        run_budget=RunBudget.from_settings(settings),
    )
    assert result.company_locations == []
    assert (result.geography_match, result.fit) == ("not_met", "non_fit")
