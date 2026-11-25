"""Screening pipeline: fetch the homepage -> ask the LLM -> build a result row.

Each company is processed independently; one failure never stops the others. A
semaphore limits how many companies are processed at the same time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from .agent import assess_company
from .config import Settings
from .fetcher import FetchError, PageFetcher
from .netguard import UnsafeURLError
from .schemas import CompanyResult, ResultStatus, ScreeningCriteria

logger = logging.getLogger(__name__)


async def screen_company(
    position: int,
    raw_url: str,
    criteria: ScreeningCriteria,
    *,
    settings: Settings,
    client: Any,
    fetcher: PageFetcher,
) -> CompanyResult:
    started = time.perf_counter()
    result = CompanyResult(position=position, input_url=raw_url, status="ok")

    def finish(status: ResultStatus, error: str | None = None) -> CompanyResult:
        result.status = status
        result.error = error
        result.duration_s = round(time.perf_counter() - started, 3)
        return result

    try:
        url = fetcher.normalize(raw_url)
        homepage = await fetcher.fetch(url)
    except UnsafeURLError as exc:
        return finish("invalid_url", f"URL not accepted: {exc}")
    except FetchError as exc:
        return finish("fetch_error", f"could not fetch the page ({exc.kind}): {exc.message}")

    try:
        outcome = await assess_company(client, settings, criteria, homepage, fetcher)
    except Exception:  # last-resort guard: one company must not break the whole run
        logger.exception("unexpected error while assessing %s", raw_url)
        return finish("internal_error", "internal error while assessing this company")

    result.pages_fetched = [page.url for page in outcome.pages]
    result.tool_calls = outcome.tool_calls
    result.usage = outcome.usage
    result.warnings = outcome.warnings
    assessment = outcome.assessment
    if assessment is not None:
        result.company_name = assessment.company_name
        result.description = assessment.description
        result.industry = assessment.industry
        result.geography = assessment.geography
        result.industry_match = assessment.industry_match
        result.geography_match = assessment.geography_match
        result.business_match = assessment.business_match
        result.exclusion_triggered = assessment.exclusion_triggered
        result.fit = assessment.fit
        result.rationale = assessment.rationale
        result.evidence = assessment.evidence
    return finish(outcome.status, outcome.error)


async def screen_companies(
    urls: list[str],
    criteria: ScreeningCriteria,
    *,
    settings: Settings,
    client: Any,
    fetcher: PageFetcher,
    on_result: Callable[[CompanyResult], None] | None = None,
) -> list[CompanyResult]:
    """Screen all URLs concurrently (bounded); results are returned in input order."""
    semaphore = asyncio.Semaphore(settings.concurrency)

    async def run_one(position: int, url: str) -> CompanyResult:
        async with semaphore:
            result = await screen_company(
                position, url, criteria, settings=settings, client=client, fetcher=fetcher
            )
        if on_result is not None:
            on_result(result)
        return result

    return list(await asyncio.gather(*(run_one(i, url) for i, url in enumerate(urls))))
