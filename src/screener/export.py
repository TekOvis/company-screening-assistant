"""JSON and CSV export of a screening run."""

from __future__ import annotations

import csv
import io

from .schemas import CompanyResult, ScreeningRun

CSV_COLUMNS = [
    "position",
    "input_url",
    "status",
    "error",
    "company_name",
    "fit",
    "industry",
    "geography",
    "description",
    "industry_match",
    "geography_match",
    "business_match",
    "exclusion_triggered",
    "rationale",
    "evidence",
    "warnings",
    "pages_fetched",
    "tool_calls",
    "llm_calls",
    "input_tokens",
    "output_tokens",
    "duration_s",
]

# A cell starting with one of these is treated as a formula by spreadsheet apps.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def run_to_json(run: ScreeningRun) -> str:
    return run.model_dump_json(indent=2)


def run_to_csv(run: ScreeningRun) -> str:
    """CSV with one row per company. Written with a BOM so Excel detects UTF-8."""
    buffer = io.StringIO()
    buffer.write("﻿")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for result in sorted(run.results, key=lambda r: r.position):
        writer.writerow({key: safe_cell(value) for key, value in _row(result).items()})
    return buffer.getvalue()


def _row(result: CompanyResult) -> dict[str, object]:
    evidence = " || ".join(
        f"[{item.supports}] {item.quote} ({item.url})" for item in result.evidence
    )
    return {
        "position": result.position + 1,
        "input_url": result.input_url,
        "status": result.status,
        "error": result.error or "",
        "company_name": result.company_name or "",
        "fit": result.fit or "",
        "industry": result.industry or "",
        "geography": "; ".join(result.geography),
        "description": result.description or "",
        "industry_match": result.industry_match or "",
        "geography_match": result.geography_match or "",
        "business_match": result.business_match or "",
        "exclusion_triggered": ""
        if result.exclusion_triggered is None
        else str(result.exclusion_triggered).lower(),
        "rationale": result.rationale or "",
        "evidence": evidence,
        "warnings": " || ".join(result.warnings),
        "pages_fetched": " ".join(result.pages_fetched),
        "tool_calls": result.tool_calls,
        "llm_calls": result.usage.llm_calls,
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "duration_s": result.duration_s,
    }


def safe_cell(value: object) -> object:
    """Prefix text that a spreadsheet would run as a formula (CSV injection)."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value
