"""Screening pipeline for each company:

    fetch the homepage -> agent loop (may fetch more pages) -> evidence check -> result row

Each company is processed independently; one failure never stops the others — except a
reached API spend limit, which stops the whole run (see budget.RunBudget). A semaphore
limits how many companies are processed at the same time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from .agent import run_agent
from .budget import BudgetExceeded, CompanyBudget, RunBudget
from .config import Settings
from .fetcher import FetchError, PageFetcher
from .netguard import UnsafeURLError
from .schemas import CompanyResult, ResultStatus, ScreeningCriteria
from .verify import VerificationError, verify_assessment

logger = logging.getLogger(__name__)


async def screen_company(
    position: int,
    raw_url: str,
    criteria: ScreeningCriteria,
    *,
    settings: Settings,
    client: Any,
    fetcher: PageFetcher,
    run_budget: RunBudget,
) -> CompanyResult:
    started = time.perf_counter()
    result = CompanyResult(position=position, input_url=raw_url, status="ok")

    def finish(status: ResultStatus, error: str | None = None) -> CompanyResult:
        result.status = status
        result.error = error
        result.duration_s = round(time.perf_counter() - started, 3)
        return result

    if run_budget.stop_reason:
        return finish("budget_exceeded", f"not processed: {run_budget.stop_reason}")
    try:
        url = fetcher.normalize(raw_url)
        homepage = await fetcher.fetch(url)
    except UnsafeURLError as exc:
        return finish("invalid_url", f"URL not accepted: {exc}")
    except FetchError as exc:
        return finish("fetch_error", f"could not fetch the page ({exc.kind}): {exc.message}")

    budget = CompanyBudget.from_settings(run_budget, settings)
    try:
        outcome = await run_agent(
            client, settings, criteria, homepage, fetcher, budget=budget, usage=result.usage
        )
        result.pages_fetched = list(outcome.pages)
        result.tool_calls = outcome.tool_calls
        if outcome.assessment is None:
            return finish(outcome.status, outcome.error)

        # Keep the model's own answer for transparency; only verified facts are shown.
        result.raw_assessment = outcome.assessment
        result.model_fit = outcome.assessment.fit
        result.rationale = outcome.assessment.rationale
        try:
            verified = await verify_assessment(
                outcome.assessment,
                outcome.pages,
                client=client,
                settings=settings,
                budget=budget,
                usage=result.usage,
            )
        except BudgetExceeded as exc:
            return finish("budget_exceeded", f"evidence check not run: {exc}")
        except VerificationError as exc:
            return finish("verification_error", f"evidence check failed: {exc}")
    except Exception:  # last-resort guard: one company must not break the whole run
        logger.exception("unexpected error while assessing %s", raw_url)
        return finish("internal_error", "internal error while assessing this company")

    result.company_name = verified.company_name
    result.description = verified.description
    result.industry = verified.industry
    result.company_locations = verified.company_locations
    result.markets_served = verified.markets_served
    result.industry_match = verified.industry_match
    result.geography_match = verified.geography_match
    result.business_match = verified.business_match
    result.exclusion = verified.exclusion
    result.fit = verified.fit
    result.decision_note = verified.decision_note
    result.evidence = verified.evidence
    result.warnings = verified.warnings
    return finish("ok")


async def screen_companies(
    urls: list[str],
    criteria: ScreeningCriteria,
    *,
    settings: Settings,
    client: Any,
    fetcher: PageFetcher,
    on_result: Callable[[CompanyResult], None] | None = None,
    run_budget: RunBudget | None = None,
) -> list[CompanyResult]:
    """Screen all URLs concurrently (bounded); results are returned in input order."""
    budget = run_budget or RunBudget.from_settings(settings)
    semaphore = asyncio.Semaphore(settings.concurrency)

    async def run_one(position: int, url: str) -> CompanyResult:
        async with semaphore:
            result = await screen_company(
                position,
                url,
                criteria,
                settings=settings,
                client=client,
                fetcher=fetcher,
                run_budget=budget,
            )
        if on_result is not None:
            on_result(result)
        return result

    return list(await asyncio.gather(*(run_one(i, url) for i, url in enumerate(urls))))
