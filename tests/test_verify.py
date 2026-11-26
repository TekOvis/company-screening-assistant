"""Evidence verification: stage 1 (page + verbatim quote), stage 2 (one batched semantic
check) and the explicit label rules. The verifier is either the mock (keyword rules) or
a scripted fake that returns exactly what a test needs."""

import json

import pytest

from screener.budget import BudgetExceeded, CompanyBudget, RunBudget
from screener.fake_llm import FakeAnthropicClient, text_message
from screener.fetcher import build_page
from screener.fixtures import FixtureFetcher
from screener.schemas import Assessment, Usage
from screener.verify import (
    VerificationError,
    check_quotes,
    decide_label,
    normalize_for_match,
    verify_assessment,
)

from .conftest import make_settings

HOME = "https://alpen-pumps.example/"
HOME_HTML = """<html><head><title>Alpen Pumps | Industrial pumps</title></head><body>
<p>Alpen Pumps designs and manufactures industrial pumps for water utilities.</p>
<p>We serve customers in Austria and Switzerland.</p>
<p>Our headquarters are in Brno, Czechia.</p>
<p>The company is owned by Northbridge Capital, a private equity fund.</p>
</body></html>"""
DESCRIPTION_QUOTE = "Alpen Pumps designs and manufactures industrial pumps for water utilities."
MARKET_QUOTE = "We serve customers in Austria and Switzerland."
HQ_QUOTE = "Our headquarters are in Brno, Czechia."


def _ev(supports, quote, claim="claim", url=HOME):
    return {"supports": supports, "claim": claim, "url": url, "quote": quote}


def _answer(**overrides) -> Assessment:
    base = {
        "company_name": "Alpen Pumps",
        "description": "Designs and manufactures industrial pumps for water utilities.",
        "industry": "industrial pumps",
        "company_locations": ["Brno", "Czechia"],
        "markets_served": ["Austria"],
        "industry_match": "met",
        "geography_match": "not_met",
        "business_match": "met",
        "exclusion_triggered": False,
        "fit": "non_fit",
        "rationale": "Pump maker headquartered in Czechia, not Austria.",
        "evidence": [
            _ev("company_name", "Title: Alpen Pumps | Industrial pumps"),
            _ev("description", DESCRIPTION_QUOTE),
            _ev("industry", DESCRIPTION_QUOTE),
            _ev("business", DESCRIPTION_QUOTE, "The company manufactures industrial pumps."),
            _ev("company_location", HQ_QUOTE),
            _ev("market_served", MARKET_QUOTE),
        ],
    }
    base.update(overrides)
    return Assessment.model_validate(base)


async def _verify(answer, client=None, *, html=HOME_HTML, region="Austria", **settings_overrides):
    settings = make_settings(**settings_overrides)
    fetcher = FixtureFetcher({HOME: html}, settings)
    page = await fetcher.fetch(HOME)
    client = client or FakeAnthropicClient()  # mock verifier: keyword rules
    budget = CompanyBudget.from_settings(RunBudget.from_settings(settings), settings)
    usage = Usage()
    verified = await verify_assessment(
        answer,
        {page.url: page},
        target_geography=region,
        client=client,
        settings=settings,
        budget=budget,
        usage=usage,
    )
    return verified, client, usage


def test_normalize_for_match_ignores_case_spacing_and_typography():
    assert normalize_for_match("Héllo  “World” –\nNow") == normalize_for_match(
        'héllo "world" - now'
    )


def test_stage_one_checks_page_and_verbatim_quote():
    page_answer = _answer(
        evidence=[
            _ev("description", "  alpen pumps DESIGNS and manufactures industrial pumps "),
            _ev("description", "Alpen Pumps builds pumps."),  # paraphrase
            _ev("description", DESCRIPTION_QUOTE, url="https://other.example/"),
        ]
    )
    settings = make_settings()
    page = build_page(
        requested_url=HOME,
        final_url=HOME,
        status_code=200,
        content_type="text/html",
        body=HOME_HTML.encode(),
        settings=settings,
    )
    checked = check_quotes(page_answer.evidence, {HOME: page})
    assert [c.check for c in checked] == ["not_checked", "quote_not_found", "page_mismatch"]


@pytest.mark.anyio
async def test_verified_facts_are_kept_and_a_site_elsewhere_is_no_mismatch():
    """The model says "not_met" because the headquarters are elsewhere. Under the agreed
    geography rule that is not enough: the text may not list every site."""
    verified, client, usage = await _verify(_answer())
    assert verified.company_locations == ["Brno", "Czechia"]
    assert verified.markets_served == ["Austria"]
    assert verified.geography_match == "unknown"
    assert verified.fit == "insufficient_information"
    assert any("Sites named elsewhere are not enough" in w for w in verified.warnings)
    assert len(client.requests) == 1  # all claims checked in ONE request
    assert usage.llm_calls == 1


@pytest.mark.anyio
async def test_serving_customers_somewhere_is_not_a_location():
    """'We serve customers in Austria' contains 'Austria' but is not a location."""
    answer = _answer(
        company_locations=["Austria"],
        markets_served=[],
        geography_match="met",
        fit="fit",
        evidence=[
            _ev("description", DESCRIPTION_QUOTE),
            _ev("industry", DESCRIPTION_QUOTE),
            _ev("company_location", MARKET_QUOTE),  # passes stage 1: verbatim, has "Austria"
        ],
    )
    verified, _, _ = await _verify(answer)
    assert verified.company_locations == []
    location_check = next(e for e in verified.evidence if e.supports == "company_location")
    assert location_check.check == "not_supported"
    assert "not a location" in location_check.check_note
    # Missing facts mean insufficient information, not an automatic non-fit.
    assert verified.geography_match == "unknown"
    assert verified.fit == "insufficient_information"
    assert any("Label changed from fit" in w for w in verified.warnings)


@pytest.mark.anyio
async def test_value_must_appear_in_the_quote():
    answer = _answer(
        company_locations=["Vienna"],
        evidence=[_ev("company_location", HQ_QUOTE)],  # quote never mentions Vienna
    )
    verified, client, _ = await _verify(answer)
    assert verified.company_locations == []
    assert verified.evidence[0].check == "value_not_in_quote"
    sent = json.dumps(client.requests[0]["messages"]) if client.requests else ""
    assert "Vienna" not in sent  # failed before the semantic stage


@pytest.mark.anyio
async def test_unverified_exclusion_becomes_unknown():
    answer = _answer(
        company_locations=["Brno"],
        geography_match="met",
        exclusion_triggered=True,
        fit="non_fit",
        evidence=[
            _ev("description", DESCRIPTION_QUOTE),
            _ev("industry", DESCRIPTION_QUOTE),
            _ev("company_location", HQ_QUOTE),
            _ev("exclusion", MARKET_QUOTE, "The company is a reseller."),  # wrong quote
        ],
    )
    verified, _, _ = await _verify(answer)
    assert verified.exclusion == "unknown"
    assert verified.fit == "insufficient_information"


@pytest.mark.anyio
async def test_verified_exclusion_gives_non_fit():
    answer = _answer(
        exclusion_triggered=True,
        evidence=[
            _ev("description", DESCRIPTION_QUOTE),
            _ev(
                "exclusion",
                "The company is owned by Northbridge Capital, a private equity fund.",
                "The company is owned by a private equity fund.",
            ),
        ],
        company_locations=[],
        geography_match="unknown",
    )
    verified, _, _ = await _verify(answer)
    assert verified.exclusion == "triggered"
    assert verified.fit == "non_fit"


@pytest.mark.anyio
async def test_no_request_when_there_is_nothing_to_check():
    answer = _answer(
        company_name=None,
        description=None,
        industry=None,
        company_locations=[],
        markets_served=[],
        industry_match="unknown",
        geography_match="unknown",
        business_match="unknown",
        fit="insufficient_information",
        evidence=[],
    )
    verified, client, usage = await _verify(answer)
    assert client.requests == [] and usage.llm_requests == 0
    assert verified.fit == "insufficient_information"


@pytest.mark.anyio
async def test_a_wrong_verifier_cannot_rescue_a_quote_that_failed_stage_one():
    answer = _answer(
        evidence=[
            _ev("description", DESCRIPTION_QUOTE),
            _ev("company_location", "Our headquarters are in Brno, Czechia, EU."),  # not verbatim
        ]
    )

    def always_supported(params):  # a verifier that agrees with everything it is shown
        content = params["messages"][0]["content"]
        items = json.loads(content.split("\n", 1)[1].rsplit("\n</", 1)[0])
        verdicts = [{"id": i["id"], "verdict": "supported", "reason": "ok"} for i in items]
        return text_message(json.dumps({"verdicts": verdicts}))

    verified, client, _ = await _verify(answer, FakeAnthropicClient([always_supported]))
    assert len(client.requests) == 1
    assert "Brno" not in client.requests[0]["messages"][0]["content"]  # never sent
    assert verified.evidence[1].check == "quote_not_found"
    assert verified.company_locations == []
    assert verified.description is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (text_message("{", stop_reason="refusal"), "declined"),
        (text_message("{", stop_reason="max_tokens"), "cut off"),
        (text_message("not json"), "did not match the schema"),
    ],
)
async def test_verifier_failures_raise(reply, message):
    with pytest.raises(VerificationError, match=message):
        await _verify(_answer(), FakeAnthropicClient([reply]))


@pytest.mark.anyio
async def test_claims_without_a_verdict_are_not_counted_as_supported():
    reply = text_message(json.dumps({"verdicts": []}))
    verified, _, _ = await _verify(_answer(), FakeAnthropicClient([reply]))
    assert verified.company_locations == [] and verified.company_name is None
    assert verified.fit == "insufficient_information"


@pytest.mark.anyio
async def test_verification_respects_the_budget():
    with pytest.raises(BudgetExceeded):
        await _verify(_answer(), max_llm_requests_per_company=2, max_tokens_per_company=5_000)


@pytest.mark.parametrize(
    ("industry", "geography", "business", "exclusion", "expected"),
    [
        ("met", "met", "met", "not_triggered", "fit"),
        ("met", "met", "met", "unknown", "insufficient_information"),
        ("met", "met", "met", "triggered", "non_fit"),
        ("met", "not_met", "met", "not_triggered", "non_fit"),
        ("not_met", "unknown", "unknown", "not_triggered", "non_fit"),
        ("met", "unknown", "met", "not_triggered", "insufficient_information"),
        ("unknown", "unknown", "unknown", "not_triggered", "insufficient_information"),
    ],
)
def test_decide_label(industry, geography, business, exclusion, expected):
    assert decide_label(industry, geography, business, exclusion) == expected
