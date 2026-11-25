"""Evidence verification in two stages, then explicit rules for the final label.

Stage 1 — code, deterministic:
  * the cited URL is one of the pages we actually fetched;
  * the quote appears verbatim on that page (ignoring case, whitespace and typographic
    quote/dash variants);
  * for names and places, the value itself appears in the quote.
Stage 2 — semantic: does the quote actually *state* the claim? "We serve customers in
Austria" contains "Austria" but does not say that the company is located there. All
remaining claims go to the verifier in ONE request. The verifier is an LLM (a rule-based
test double in mock mode) and can be wrong too: "supported" means "a second, independent
check agreed", not "proven".
Finally, facts without supported evidence are removed and the label is recomputed by the
explicit rules in decide_label(), so an unverified fact can never produce fit or non_fit.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any

import anthropic
from pydantic import ValidationError

from .budget import CompanyBudget
from .config import Settings
from .fetcher import FetchedPage
from .llm import LLMCallError, call_llm, final_text, refusal_message
from .prompts import VERIFIER_PROMPT, render_claims
from .schemas import (
    Assessment,
    CheckedEvidence,
    CheckStatus,
    CriterionStatus,
    EvidenceItem,
    ExclusionStatus,
    FitLabel,
    Usage,
    VerifierOutput,
    VerifierVerdict,
)

VERIFIER_SCHEMA: dict[str, Any] = anthropic.transform_schema(VerifierOutput)
MAX_QUOTES_PER_CLAIM = 3
MAX_STATEMENTS_PER_KIND = 3  # business / exclusion statements checked per company
_VALUE_FIELDS = ("company_name", "company_location", "market_served")
_CHAR_FIXES = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‐": "-",
        "‑": "-",
        "–": "-",
        "—": "-",
        "−": "-",
    }
)
_VERDICT_RANK = {"not_checked": 0, "not_supported": 1, "unclear": 2, "supported": 3}


class VerificationError(Exception):
    """The evidence check could not be completed (API error, refusal, bad answer)."""


def normalize_for_match(text: str) -> str:
    """Lower-case, NFKC, plain quotes/dashes and single spaces — for verbatim matching."""
    text = unicodedata.normalize("NFKC", text).translate(_CHAR_FIXES)
    return " ".join(text.casefold().split())


def decide_label(
    industry: CriterionStatus,
    geography: CriterionStatus,
    business: CriterionStatus,
    exclusion: ExclusionStatus,
) -> FitLabel:
    """The final label, from VERIFIED statuses only.

    1. A verified exclusion                          -> non_fit
    2. Any criterion verified as not met             -> non_fit
    3. All three criteria verified as met and no
       exclusion reported                            -> fit
    4. Anything else (missing or unverified facts)   -> insufficient_information
    """
    if exclusion == "triggered":
        return "non_fit"
    if "not_met" in (industry, geography, business):
        return "non_fit"
    if industry == geography == business == "met" and exclusion == "not_triggered":
        return "fit"
    return "insufficient_information"


@dataclass
class Claim:
    """One statement to check (a fact value or a criterion statement) and its quotes."""

    field: str
    value: str | None
    text: str
    evidence_ids: list[int]
    verdict: CheckStatus = "not_checked"
    reason: str | None = None

    @property
    def supported(self) -> bool:
        return self.verdict == "supported"


@dataclass
class VerifiedAssessment:
    company_name: str | None
    description: str | None
    industry: str | None
    company_locations: list[str]
    markets_served: list[str]
    industry_match: CriterionStatus
    geography_match: CriterionStatus
    business_match: CriterionStatus
    exclusion: ExclusionStatus
    fit: FitLabel
    decision_note: str
    evidence: list[CheckedEvidence]
    warnings: list[str] = field(default_factory=list)


async def verify_assessment(
    assessment: Assessment,
    pages: dict[str, FetchedPage],
    *,
    client: Any,
    settings: Settings,
    budget: CompanyBudget,
    usage: Usage,
) -> VerifiedAssessment:
    """Raises VerificationError, or BudgetExceeded if the check would not fit the budget."""
    evidence = check_quotes(assessment.evidence, pages)  # stage 1
    claims = build_claims(assessment, evidence)
    checkable = [claim for claim in claims if claim.evidence_ids]
    to_check = checkable[: settings.max_claims_to_verify]
    for claim in checkable[settings.max_claims_to_verify :]:
        claim.reason = "not checked: claim limit per company reached"
    for claim in claims:
        if not claim.evidence_ids:
            claim.reason = "no verbatim quote from a fetched page supports it"
    if to_check:  # stage 2, one request for all claims
        verdicts = await ask_verifier(
            to_check, evidence, client=client, settings=settings, budget=budget, usage=usage
        )
        for number, claim in enumerate(to_check, start=1):
            verdict = verdicts.get(number)
            if verdict is None:
                claim.reason = "the verifier returned no verdict for this claim"
            else:
                claim.verdict, claim.reason = verdict.verdict, verdict.reason
    mark_evidence(claims, evidence)
    return apply_rules(assessment, claims, evidence)


def check_quotes(items: list[EvidenceItem], pages: dict[str, FetchedPage]) -> list[CheckedEvidence]:
    """Stage 1: the cited page was fetched and the quote is verbatim on it."""
    texts: dict[str, str] = {}
    for page in pages.values():
        normalized = normalize_for_match(page.text)
        for url in (page.url, page.requested_url):
            texts[url] = normalized
            texts[_toggle_trailing_slash(url)] = normalized
    checked = []
    for item in items:
        evidence = CheckedEvidence.model_validate(item.model_dump())
        page_text = texts.get(item.url.strip())
        quote = normalize_for_match(item.quote)
        if page_text is None:
            evidence.check = "page_mismatch"
            evidence.check_note = "the cited URL is not one of the fetched pages"
        elif len(quote) < 3 or quote not in page_text:
            evidence.check = "quote_not_found"
            evidence.check_note = "the quote is not verbatim on the cited page"
        checked.append(evidence)
    return checked


def build_claims(assessment: Assessment, evidence: list[CheckedEvidence]) -> list[Claim]:
    """Turn every reported fact into a claim with the stage-1-valid quotes behind it.

    Claim texts are written by the code, not taken from the model: e.g. a location is
    always checked as "the company itself is located in X", whatever the model wrote.
    """

    def quotes_for(target: str, value: str | None = None) -> list[int]:
        ids = []
        for i, item in enumerate(evidence):
            if item.supports != target or item.check != "not_checked":
                continue
            if value is not None and normalize_for_match(value) not in normalize_for_match(
                item.quote
            ):
                continue
            ids.append(i)
        return ids[:MAX_QUOTES_PER_CLAIM]

    a = assessment
    claims: list[Claim] = []
    # Ordered by importance for the criteria, in case the claim limit is reached.
    for place in _unique(a.company_locations):
        claims.append(
            Claim(
                "company_location",
                place,
                f"The company itself is located in {place}: it has its headquarters, an "
                "office, a plant or another facility of its own there.",
                quotes_for("company_location", place),
            )
        )
    if a.industry:
        text = f"The company works in this industry: {a.industry}."
        claims.append(Claim("industry", a.industry, text, quotes_for("industry")))
    if a.description:
        text = f"Description of the company: {a.description}"
        claims.append(Claim("description", a.description, text, quotes_for("description")))
    for kind in ("business", "exclusion"):
        ids = [i for i, e in enumerate(evidence) if e.supports == kind and e.check == "not_checked"]
        for i in ids[:MAX_STATEMENTS_PER_KIND]:
            claims.append(Claim(kind, None, evidence[i].claim, [i]))
    if a.company_name:
        text = f"The company's name is {a.company_name}."
        claims.append(
            Claim("company_name", a.company_name, text, quotes_for("company_name", a.company_name))
        )
    for place in _unique(a.markets_served):
        text = f"The company sells to or serves customers in {place}."
        claims.append(Claim("market_served", place, text, quotes_for("market_served", place)))
    return claims


async def ask_verifier(
    claims: list[Claim],
    evidence: list[CheckedEvidence],
    *,
    client: Any,
    settings: Settings,
    budget: CompanyBudget,
    usage: Usage,
) -> dict[int, VerifierVerdict]:
    """Stage 2: one request with all claims; returns verdicts by item id."""
    items = [
        {
            "id": number,
            "field": claim.field,
            "value": claim.value,
            "claim": claim.text,
            "quotes": [evidence[i].quote for i in claim.evidence_ids],
        }
        for number, claim in enumerate(claims, start=1)
    ]
    params = {
        "model": settings.llm_model,
        "max_tokens": settings.verifier_max_output_tokens,
        "system": VERIFIER_PROMPT,
        "messages": [{"role": "user", "content": render_claims(items)}],
        "output_config": {
            "effort": settings.llm_effort,
            "format": {"type": "json_schema", "schema": VERIFIER_SCHEMA},
        },
    }
    try:
        response = await call_llm(client, params, budget=budget, usage=usage, settings=settings)
    except LLMCallError as exc:
        raise VerificationError(exc.message) from exc
    if response.stop_reason == "refusal":
        raise VerificationError(refusal_message(response))
    if response.stop_reason == "max_tokens":
        raise VerificationError("the verifier's answer was cut off at the output token limit")
    if response.stop_reason not in ("end_turn", "stop_sequence"):
        raise VerificationError(f"unexpected stop reason from the verifier: {response.stop_reason}")
    try:
        output = VerifierOutput.model_validate_json(final_text(response))
    except ValidationError as exc:
        raise VerificationError(
            f"the verifier's answer did not match the schema ({exc.error_count()} errors)"
        ) from exc
    return {verdict.id: verdict for verdict in output.verdicts}


def mark_evidence(claims: list[Claim], evidence: list[CheckedEvidence]) -> None:
    """Copy the best verdict of the claims that used each quote onto the quote."""
    used: set[int] = set()
    for claim in claims:
        for i in claim.evidence_ids:
            used.add(i)
            item = evidence[i]
            if item.check_note is None or _VERDICT_RANK[claim.verdict] > _VERDICT_RANK[item.check]:
                item.check, item.check_note = claim.verdict, claim.reason
    for i, item in enumerate(evidence):
        if i in used or item.check != "not_checked":
            continue
        if item.supports in _VALUE_FIELDS:
            item.check = "value_not_in_quote"
            item.check_note = "the quote does not contain the reported name or place"
        else:
            item.check_note = "not linked to a reported value or over the check limit"


def apply_rules(
    a: Assessment, claims: list[Claim], evidence: list[CheckedEvidence]
) -> VerifiedAssessment:
    """Drop unsupported facts, confirm criteria only with verified facts, decide the label."""
    warnings: list[str] = []

    def kept(target: str, value: str, label: str) -> bool:
        claim = next((c for c in claims if c.field == target and c.value == value), None)
        if claim is not None and claim.supported:
            return True
        reason = claim.reason if claim is not None and claim.reason else "no supporting evidence"
        warnings.append(f"{label} removed: {reason}")
        return False

    name = (
        a.company_name if a.company_name and kept("company_name", a.company_name, "Name") else None
    )
    description = (
        a.description
        if a.description and kept("description", a.description, "Description")
        else None
    )
    industry = a.industry if a.industry and kept("industry", a.industry, "Industry") else None
    locations = [
        p for p in _unique(a.company_locations) if kept("company_location", p, f"Location '{p}'")
    ]
    markets = [p for p in _unique(a.markets_served) if kept("market_served", p, f"Market '{p}'")]
    business_supported = description is not None or any(
        c.field == "business" and c.supported for c in claims
    )
    exclusion_supported = any(c.field == "exclusion" and c.supported for c in claims)

    industry_match = _confirm(
        a.industry_match, industry is not None or description is not None, "Industry", warnings
    )
    geography_match = _confirm(a.geography_match, bool(locations), "Geography", warnings)
    business_match = _confirm(a.business_match, business_supported, "Business", warnings)
    exclusion: ExclusionStatus
    if not a.exclusion_triggered:
        exclusion = "not_triggered"
    elif exclusion_supported:
        exclusion = "triggered"
    else:
        exclusion = "unknown"
        warnings.append("Exclusion reported by the model was not verified; treated as unknown.")

    fit = decide_label(industry_match, geography_match, business_match, exclusion)
    note = (
        f"Rules after evidence checks: industry {industry_match}, geography {geography_match}, "
        f"business {business_match}, exclusion {exclusion.replace('_', ' ')} -> {fit}."
    )
    if fit != a.fit:
        warnings.append(f"Label changed from {a.fit} (model) to {fit} after evidence checks.")
    return VerifiedAssessment(
        company_name=name,
        description=description,
        industry=industry,
        company_locations=locations,
        markets_served=markets,
        industry_match=industry_match,
        geography_match=geography_match,
        business_match=business_match,
        exclusion=exclusion,
        fit=fit,
        decision_note=note,
        evidence=evidence,
        warnings=warnings,
    )


def _confirm(
    status: CriterionStatus, has_verified_facts: bool, label: str, warnings: list[str]
) -> CriterionStatus:
    """A met/not_met judgment stands only if the facts behind it were verified."""
    if status == "unknown" or has_verified_facts:
        return status
    warnings.append(f"{label} criterion '{status}' set to unknown: its facts were not verified.")
    return "unknown"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _toggle_trailing_slash(url: str) -> str:
    if url.endswith("/") and url.count("/") > 3:
        return url.rstrip("/")
    return url if url.endswith("/") else url + "/"
