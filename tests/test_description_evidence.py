"""Which quotes may confirm the description (decision of 2026-11-25, option a).

The description may rest on quotes the model filed under "business", not only under
"description", but only if the second check finds that the quotes state the whole
description. The category alone never confirms anything.

The verifier here is a scripted stand-in for the model. These tests check what the code
guarantees:
- which quotes are sent with the description claim;
- that only a "supported" verdict keeps a fact;
- that a partly supported description is not kept in part;
- that the verifier's instructions state the rules.

Whether the real model follows the instructions is a question for the evaluation.
"""

import json

import pytest

from screener.budget import CompanyBudget, RunBudget
from screener.fake_llm import FakeAnthropicClient, text_message
from screener.fixtures import FixtureFetcher
from screener.prompts import VERIFIER_PROMPT
from screener.schemas import Assessment, Usage
from screener.verify import verify_assessment

from .conftest import make_settings

HOME = "https://nordwerk-armaturen.example/"
HOME_HTML = """<html><head><title>Nordwerk Armaturen GmbH</title></head><body>
<p>Welcome to Nordwerk Armaturen GmbH.</p>
<p>Nordwerk Armaturen manufactures stainless-steel ball valves for breweries and dairies.</p>
<p>Our plant is in Kassel, Germany.</p>
<p>We are Europe's leading valve maker and guarantee delivery within 24 hours.</p>
</body></html>"""
NAME_QUOTE = "Welcome to Nordwerk Armaturen GmbH."
BUSINESS_QUOTE = (
    "Nordwerk Armaturen manufactures stainless-steel ball valves for breweries and dairies."
)
PLANT_QUOTE = "Our plant is in Kassel, Germany."
PROMO_QUOTE = "We are Europe's leading valve maker and guarantee delivery within 24 hours."
DESCRIPTION = "Manufactures stainless-steel ball valves for breweries and dairies."
SUPPORTED = {"verdict": "supported", "reason": "stated"}


def _ev(supports, quote, claim="claim"):
    return {"supports": supports, "claim": claim, "url": HOME, "quote": quote}


def _answer(**overrides) -> Assessment:
    base = {
        "company_name": "Nordwerk Armaturen GmbH",
        "description": DESCRIPTION,
        "industry": None,
        "company_locations": ["Kassel", "Germany"],
        "markets_served": [],
        "industry_match": "met",
        "geography_match": "met",
        "business_match": "met",
        "exclusion_triggered": False,
        "fit": "fit",
        "rationale": "Valve maker with a plant in Germany.",
        "evidence": [
            _ev("company_name", NAME_QUOTE),
            _ev("business", BUSINESS_QUOTE, "The company manufactures ball valves."),
            _ev("company_location", PLANT_QUOTE),
        ],
    }
    base.update(overrides)
    return Assessment.model_validate(base)


def _scripted_verifier(judge):
    """A verifier reply built item by item by `judge` (stands in for the model)."""

    def reply(params):
        verdicts = [{"id": item["id"], **judge(item)} for item in _items(params)]
        return text_message(json.dumps({"verdicts": verdicts}))

    return reply


def _items(params) -> list[dict]:
    content = params["messages"][0]["content"]
    return json.loads(content.split("\n", 1)[1].rsplit("\n</", 1)[0])


def _sent(client, field):
    """The item for `field` in the single verifier request, or None if it was not sent."""
    if not client.requests:
        return None
    return next((i for i in _items(client.requests[0]) if i["field"] == field), None)


async def _verify(answer, judge):
    settings = make_settings()
    page = await FixtureFetcher({HOME: HOME_HTML}, settings).fetch(HOME)
    client = FakeAnthropicClient([_scripted_verifier(judge)])
    budget = CompanyBudget.from_settings(RunBudget.from_settings(settings), settings)
    verified = await verify_assessment(
        answer,
        {page.url: page},
        target_geography="Germany",
        client=client,
        settings=settings,
        budget=budget,
        usage=Usage(),
    )
    return verified, client


# 1. A quote filed under "business" can confirm the description.


@pytest.mark.anyio
async def test_a_business_quote_can_support_the_description():
    def judge(item):
        if item["field"] == "description":
            if BUSINESS_QUOTE in item["quotes"]:
                return {"verdict": "supported", "reason": "the quote says what it makes"}
            return {"verdict": "not_supported", "reason": "no quote about the business"}
        return SUPPORTED

    verified, client = await _verify(_answer(), judge)

    item = _sent(client, "description")
    assert item is not None and item["quotes"] == [BUSINESS_QUOTE]
    assert verified.description == DESCRIPTION
    assert verified.industry_match == "met"  # the industry criterion rests on the description
    assert verified.fit == "fit"


@pytest.mark.anyio
async def test_the_business_category_alone_does_not_confirm_the_description():
    """The business statement itself is confirmed, but the description says something
    else; it is judged on its own and removed."""

    def judge(item):
        if item["field"] == "description":
            return {"verdict": "not_supported", "reason": "the quote is about valves"}
        return SUPPORTED

    answer = _answer(description="Develops control software for breweries.")
    verified, client = await _verify(answer, judge)

    assert _sent(client, "description")["quotes"] == [BUSINESS_QUOTE]
    assert verified.description is None
    assert any(
        w.startswith("Description removed: the quote is about valves") for w in verified.warnings
    )
    assert verified.business_match == "met"  # its own statement was confirmed
    assert verified.industry_match == "unknown"  # no verified industry or description
    assert verified.fit == "insufficient_information"


# 2. A quote that only names the company does not confirm the description.


@pytest.mark.anyio
async def test_a_name_quote_is_never_used_for_the_description():
    """Even a verifier that agrees with everything cannot confirm the description from a
    quote filed under the company name: the code does not send it."""
    answer = _answer(
        evidence=[_ev("company_name", NAME_QUOTE), _ev("company_location", PLANT_QUOTE)],
        business_match="unknown",
        fit="insufficient_information",
    )
    verified, client = await _verify(answer, lambda item: SUPPORTED)

    assert _sent(client, "description") is None
    assert verified.description is None
    assert verified.company_name == "Nordwerk Armaturen GmbH"


@pytest.mark.anyio
async def test_a_name_only_quote_filed_as_description_is_judged_and_rejected():
    def judge(item):
        if item["field"] == "description" and item["quotes"] == [NAME_QUOTE]:
            return {"verdict": "not_supported", "reason": "the quote only names the company"}
        return SUPPORTED

    answer = _answer(
        evidence=[
            _ev("company_name", NAME_QUOTE),
            _ev("description", NAME_QUOTE),
            _ev("company_location", PLANT_QUOTE),
        ],
        business_match="unknown",
        fit="insufficient_information",
    )
    verified, client = await _verify(answer, judge)

    assert _sent(client, "description")["quotes"] == [NAME_QUOTE]
    assert verified.description is None
    assert verified.company_name == "Nordwerk Armaturen GmbH"


# 3. A description that is only partly stated is rejected as a whole.


@pytest.mark.anyio
@pytest.mark.parametrize("verdict", ["not_supported", "unclear"])
async def test_a_partly_supported_description_is_rejected_as_a_whole(verdict):
    description = DESCRIPTION.rstrip(".") + " and exports them to Japan."

    def judge(item):
        if item["field"] == "description":
            return {"verdict": verdict, "reason": "nothing about exports to Japan"}
        return SUPPORTED

    verified, client = await _verify(_answer(description=description), judge)

    item = _sent(client, "description")
    assert description in item["claim"] and "every statement" in item["claim"]
    assert verified.description is None  # not shortened to the supported part
    assert verified.markets_served == []  # the extra statement does not become a fact
    assert any("nothing about exports to Japan" in w for w in verified.warnings)


# 4. Promotional wording is not turned into established facts.


@pytest.mark.anyio
async def test_promotional_wording_does_not_become_an_established_fact():
    def judge(item):
        if item["quotes"] == [PROMO_QUOTE]:
            return {"verdict": "not_supported", "reason": "self-promotion, not a fact"}
        return SUPPORTED

    answer = _answer(
        description="Europe's leading valve maker with guaranteed delivery within 24 hours.",
        evidence=[
            _ev("company_name", NAME_QUOTE),
            _ev("description", PROMO_QUOTE),
            _ev("business", PROMO_QUOTE, "The company is Europe's leading valve maker."),
            _ev("company_location", PLANT_QUOTE),
        ],
    )
    verified, client = await _verify(answer, judge)

    assert _sent(client, "description")["quotes"] == [PROMO_QUOTE]
    assert verified.description is None
    assert verified.business_match == "unknown"  # its only statement was promotional
    assert verified.fit == "insufficient_information"  # not fit
    assert all(e.check != "supported" for e in verified.evidence if e.quote == PROMO_QUOTE)


def test_the_verifier_is_told_these_rules():
    prompt = " ".join(VERIFIER_PROMPT.split())
    assert "every statement in the description" in prompt
    assert "only partly stated" in prompt
    assert "only names the company does not support a description" in prompt
    assert "Promotional or self-assessing wording" in prompt
    assert "not an established fact" in prompt
