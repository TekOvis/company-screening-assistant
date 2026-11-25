"""Evaluation runner on the synthetic set (pages always come from saved files).

    uv run python -m screener.evaluate --mode mock
    uv run python -m screener.evaluate --mode live --only 01-valves-fit-homepage --yes
    uv run python -m screener.evaluate --mode live --yes

mock  The whole application with the deterministic stand-in client. It shows that the
      pipeline works; it says nothing about the quality of a model.
live  The real model (LLM_MODEL), still on the saved synthetic pages, never the internet.
      It does not start without --yes and first prints the hard spending cap.

Results go to eval/runs/<timestamp>-<mode>/ (git-ignored): report.md, report.json and
results.jsonl with one line per case.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__, metrics
from .agent import ASSESSMENT_SCHEMA
from .budget import RunBudget
from .config import Settings
from .eval_dataset import DEFAULT_CASES_DIR, EvalCase, dataset_sha256, load_cases
from .fake_llm import FakeAnthropicClient
from .fixtures import FixtureFetcher
from .llm import create_client
from .pipeline import screen_company
from .pricing import TARIFFS_SOURCE, cost_usd, tariff_for
from .prompts import VERIFIER_PROMPT, system_prompt
from .schemas import CompanyResult, Usage, utc_now
from .tools import FETCH_TOOL
from .verify import VERIFIER_SCHEMA

PROJECT_ROOT = DEFAULT_CASES_DIR.parents[1]
DEFAULT_RUNS_DIR = PROJECT_ROOT / "eval" / "runs"
MOCK_WARNING = (
    "MOCK MODE: answers come from keyword rules, not from an LLM. This run shows that the "
    "pipeline works; it says nothing about model quality."
)
LIVE_NOTE = (
    "LIVE MODE on a small synthetic set of invented companies. The numbers describe this "
    "set only; they are not evidence of accuracy on real company websites."
)
LIMIT_SETTINGS = (
    "max_tool_calls",
    "max_llm_calls",
    "max_llm_requests_per_company",
    "max_tokens_per_company",
    "max_tokens_per_run",
    "llm_max_output_tokens",
    "verifier_max_output_tokens",
    "llm_max_retries",
    "page_text_max_chars",
)


@dataclass
class CaseOutcome:
    case: EvalCase
    result: CompanyResult


async def run_cases(cases: list[EvalCase], settings: Settings, client: Any) -> list[CaseOutcome]:
    """Run the cases one by one (simple timing, gentle on rate limits)."""
    run_budget = RunBudget.from_settings(settings)
    outcomes = []
    try:
        for position, case in enumerate(cases):
            fetcher = FixtureFetcher(case.fixture_pages(), settings)
            result = await screen_company(
                position,
                case.start_url,
                case.criteria,
                settings=settings,
                client=client,
                fetcher=fetcher,
                run_budget=run_budget,
            )
            outcomes.append(CaseOutcome(case, result))
            mark = "ok  " if result.fit == case.expected.fit else "MISS"
            print(
                f"[{position + 1:>2}/{len(cases)}] {mark} {case.id}: expected "
                f"{case.expected.fit}, got {result.fit or result.status} "
                f"({result.duration_s:.1f} s, {result.usage.llm_requests} API requests)"
            )
    finally:
        close = getattr(client, "close", None)
        if close is not None:  # the real client keeps an HTTP connection pool
            await close()
    return outcomes


def prompts_sha256(settings: Settings) -> str:
    """Fingerprint of everything sent as instructions: prompts, schemas and the tool."""
    parts = [
        system_prompt(settings.max_tool_calls),
        VERIFIER_PROMPT,
        json.dumps(ASSESSMENT_SCHEMA, sort_keys=True),
        json.dumps(VERIFIER_SCHEMA, sort_keys=True),
        json.dumps(FETCH_TOOL, sort_keys=True),
    ]
    return hashlib.sha256("\n\x1e".join(parts).encode()).hexdigest()


def _classification(expected: list[str], predicted: list[str | None]) -> dict[str, Any]:
    return {
        "accuracy": metrics.accuracy(expected, predicted),
        "correct": sum(e == p for e, p in zip(expected, predicted, strict=True)),
        "macro_f1": metrics.macro_f1(expected, predicted),
        "per_class": metrics.per_class(expected, predicted),
        "confusion_matrix": metrics.confusion_matrix(expected, predicted),
    }


def _ratio(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def build_report(
    outcomes: list[CaseOutcome],
    *,
    mode: str,
    settings: Settings,
    cases_dir: Path,
    started: datetime,
    wall_time_s: float,
) -> dict[str, Any]:
    expected = [o.case.expected.fit for o in outcomes]
    usage = Usage()
    for outcome in outcomes:
        usage.add(outcome.result.usage)
    statuses = Counter(o.result.status for o in outcomes)
    answered = sum(o.result.raw_assessment is not None for o in outcomes)
    invalid = statuses.get("invalid_output", 0)
    checks = Counter(e.check for o in outcomes for e in o.result.evidence)
    evidence_total = sum(checks.values())
    verbatim = evidence_total - checks["page_mismatch"] - checks["quote_not_found"]

    labelled = [o for o in outcomes if o.result.status == "ok"]
    names = [
        metrics.name_correct(o.case.expected.company_name, o.result.company_name) for o in labelled
    ]
    names = [n for n in names if n is not None]

    def invented(o: CaseOutcome, places: list[str]) -> bool:
        exp = o.case.expected
        return metrics.invented_location(exp.company_locations, exp.forbidden_locations, places)

    raw = [o for o in outcomes if o.result.raw_assessment is not None]
    durations = [o.result.duration_s for o in outcomes]
    if mode == "mock":
        cost: dict[str, Any] = {
            "usd": 0.0,
            "note": "mock mode: nothing is billed; token counts are rough estimates",
        }
    elif (tariff := tariff_for(settings.llm_model)) is not None:
        cost = {
            "usd": round(cost_usd(usage, tariff), 4),
            "tariff_usd_per_million_tokens": asdict(tariff),
            "source": TARIFFS_SOURCE,
            "note": "successful responses only; failed requests report no usage",
        }
    else:
        cost = {"usd": None, "note": f"no tariff listed for {settings.llm_model}"}

    try:
        dataset_path = str(cases_dir.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        dataset_path = cases_dir.name
    return {
        "mode": mode,
        "notice": MOCK_WARNING if mode == "mock" else LIVE_NOTE,
        "app_version": __version__,
        "started_at": started.isoformat(timespec="seconds"),
        "wall_time_s": round(wall_time_s, 2),
        "model": "mock-heuristic (no LLM)" if mode == "mock" else settings.llm_model,
        "effort": None if mode == "mock" else settings.llm_effort,
        "limits": {name: getattr(settings, name) for name in LIMIT_SETTINGS},
        "dataset": {
            "path": dataset_path,
            "cases": len(outcomes),
            "synthetic": True,
            "sha256": dataset_sha256(cases_dir),
        },
        "prompts_sha256": prompts_sha256(settings),
        "classification": {
            "final": _classification(expected, [o.result.fit for o in outcomes]),
            "model_before_checks": _classification(
                expected, [o.result.model_fit for o in outcomes]
            ),
        },
        "structured_output": {
            "final_answers_valid": answered,
            "final_answers_invalid": invalid,
            "valid_rate": _ratio(answered, answered + invalid),
        },
        "statuses": dict(statuses),
        "evidence": {
            "items": evidence_total,
            "checks": dict(checks),
            "verbatim_rate": _ratio(verbatim, evidence_total),
            "supported_rate_of_verbatim": _ratio(checks["supported"], verbatim),
        },
        "facts": {
            "labelled_cases": len(labelled),
            "name_correct": [sum(names), len(names)],
            "locations_correct": [
                sum(
                    metrics.locations_correct(
                        o.case.expected.company_locations,
                        o.case.expected.forbidden_locations,
                        o.result.company_locations,
                    )
                    for o in labelled
                ),
                len(labelled),
            ],
            "invented_location_after_checks": [
                o.case.id for o in labelled if invented(o, o.result.company_locations)
            ],
            "invented_location_before_checks": [
                o.case.id
                for o in raw
                if o.result.raw_assessment
                and invented(o, o.result.raw_assessment.company_locations)
            ],
            "markets_found": [
                sum(
                    metrics.markets_found(o.case.expected.markets_served, o.result.markets_served)
                    for o in labelled
                ),
                sum(len(o.case.expected.markets_served) for o in labelled),
            ],
        },
        "tools": {
            "tool_calls_total": sum(o.result.tool_calls for o in outcomes),
            "cases_with_tool_calls": sum(o.result.tool_calls > 0 for o in outcomes),
        },
        "time": {
            "per_case_median_s": round(statistics.median(durations), 2) if durations else None,
            "per_case_max_s": round(max(durations), 2) if durations else None,
        },
        "usage": usage.model_dump(),
        "cost": cost,
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def render_markdown(report: dict[str, Any], outcomes: list[CaseOutcome]) -> str:
    final = report["classification"]["final"]
    before = report["classification"]["model_before_checks"]
    n = report["dataset"]["cases"]
    usage = report["usage"]
    facts = report["facts"]
    lines = [
        f"# Evaluation report: {report['mode']} mode",
        "",
        f"> {report['notice']}",
        "",
        f"- Started {report['started_at']}, wall time {report['wall_time_s']} s, "
        f"app version {report['app_version']}",
        f"- Model: {report['model']}"
        + (f", effort {report['effort']}" if report["effort"] else ""),
        f"- Cases: {n} synthetic (`{report['dataset']['path']}`, sha256 "
        f"`{report['dataset']['sha256'][:12]}`); prompts sha256 `{report['prompts_sha256'][:12]}`",
        "",
        "## Classification",
        "",
        "Final label = after evidence checks and the explicit label rules. "
        "A case without a label (an error) counts as wrong.",
        "",
        "| | Accuracy | Macro-F1 |",
        "|---|---|---|",
        f"| Final label | {final['accuracy']:.2f} ({final['correct']}/{n}) "
        f"| {final['macro_f1']:.2f} |",
        f"| Model's own label, before checks | {before['accuracy']:.2f} ({before['correct']}/{n}) "
        f"| {before['macro_f1']:.2f} |",
        "",
        "| Label (final) | Precision | Recall | F1 | Support |",
        "|---|---|---|---|---|",
    ]
    for label in metrics.LABELS:
        s = final["per_class"][label]
        lines.append(
            f"| {label} | {s['precision']:.2f} | {s['recall']:.2f} | {s['f1']:.2f} "
            f"| {s['support']} |"
        )
    columns = [*metrics.LABELS, metrics.NO_LABEL]
    lines += [
        "",
        "Confusion matrix of final labels (rows: expected, columns: predicted):",
        "",
        "| expected \\ predicted | " + " | ".join(columns) + " |",
        "|---" * (len(columns) + 1) + "|",
    ]
    for label in metrics.LABELS:
        row = final["confusion_matrix"][label]
        lines.append(f"| {label} | " + " | ".join(str(row[c]) for c in columns) + " |")
    structured = report["structured_output"]
    evidence = report["evidence"]
    statuses = ", ".join(f"{k} {v}" for k, v in sorted(report["statuses"].items()))
    lines += [
        "",
        "## Structured output, evidence and facts",
        "",
        f"- Final answers that passed schema validation: {structured['final_answers_valid']} "
        f"of {structured['final_answers_valid'] + structured['final_answers_invalid']} "
        f"({_pct(structured['valid_rate'])})",
        f"- Result statuses: {statuses}",
        f"- Evidence items: {evidence['items']}; quote verbatim on the cited page: "
        f"{_pct(evidence['verbatim_rate'])}; of those, supported by the second check: "
        f"{_pct(evidence['supported_rate_of_verbatim'])}",
        f"- Company name correct: {facts['name_correct'][0]}/{facts['name_correct'][1]}; "
        f"own locations exactly right: {facts['locations_correct'][0]}/"
        f"{facts['locations_correct'][1]}; markets found: {facts['markets_found'][0]}/"
        f"{facts['markets_found'][1]} (labelled cases only)",
        f"- Invented or trap locations before checks: "
        f"{', '.join(facts['invented_location_before_checks']) or 'none'}; "
        f"after checks: {', '.join(facts['invented_location_after_checks']) or 'none'}",
        f"- Tool calls: {report['tools']['tool_calls_total']} in "
        f"{report['tools']['cases_with_tool_calls']} cases",
        "",
        "## Time, tokens and cost",
        "",
        f"- Time per case: median {report['time']['per_case_median_s']} s, "
        f"max {report['time']['per_case_max_s']} s",
        f"- API requests: {usage['llm_requests']} ({usage['failed_requests']} failed); "
        f"tokens: {usage['input_tokens']:,} input, {usage['output_tokens']:,} output, "
        f"{usage['cache_creation_input_tokens']:,} cache write, "
        f"{usage['cache_read_input_tokens']:,} cache read",
    ]
    cost = report["cost"]
    if cost.get("usd") is None:
        lines.append(f"- Cost: not computed ({cost['note']})")
    else:
        source = f" Tariffs: {cost['source']}." if "source" in cost else ""
        lines.append(f"- Cost: ${cost['usd']:.4f} ({cost['note']}).{source}")
    lines += [
        "",
        "## Per case",
        "",
        "| Case | Expected | Final | Model | Status | Own locations | Tools | Requests | Time, s |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for o in outcomes:
        r = o.result
        lines.append(
            f"| {o.case.id} | {o.case.expected.fit} | {r.fit or '-'} | {r.model_fit or '-'} | "
            f"{r.status} | {'; '.join(r.company_locations) or '-'} | {r.tool_calls} | "
            f"{r.usage.llm_requests} | {r.duration_s:.1f} |"
        )
    return "\n".join(lines) + "\n"


def write_outputs(out_dir: Path, report: dict[str, Any], outcomes: list[CaseOutcome]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "report.md").write_text(render_markdown(report, outcomes), encoding="utf-8")
    with (out_dir / "results.jsonl").open("w", encoding="utf-8") as handle:
        for o in outcomes:
            line = {
                "id": o.case.id,
                "tags": o.case.tags,
                "expected": o.case.expected.model_dump(),
                "result": o.result.model_dump(mode="json"),
            }
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def print_spending_plan(cases: list[EvalCase], settings: Settings) -> None:
    n = len(cases)
    token_cap = min(n * settings.max_tokens_per_company, settings.max_tokens_per_run)
    request_cap = min(n * settings.max_llm_requests_per_company, settings.max_llm_requests_per_run)
    print(
        f"Live evaluation: {n} case(s), model {settings.llm_model}, effort {settings.llm_effort}."
    )
    print(
        f"Hard caps for this run: {token_cap:,} tokens and {request_cap} API requests "
        f"({settings.max_tokens_per_company:,} tokens and "
        f"{settings.max_llm_requests_per_company} requests per case)."
    )
    tariff = tariff_for(settings.llm_model)
    if tariff is None:
        print(f"No tariff is listed for {settings.llm_model}; the cost cannot be estimated.")
    else:
        ceiling = token_cap * tariff.output / 1_000_000
        print(
            f"At the listed tariffs the caps can never cost more than ${ceiling:.2f} (every "
            f"capped token billed at the output price); real runs use far fewer tokens."
        )
        print(f"Tariffs: {TARIFFS_SOURCE}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m screener.evaluate", description="Run the synthetic evaluation set."
    )
    parser.add_argument(
        "--mode",
        choices=("mock", "live"),
        required=True,
        help="mock: free pipeline test; live: real model, costs money",
    )
    parser.add_argument("--only", nargs="+", metavar="CASE_ID", help="run only these cases")
    parser.add_argument("--limit", type=int, help="run only the first N cases")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_DIR, help=argparse.SUPPRESS)
    parser.add_argument("--out", type=Path, help="output folder (default: eval/runs/...)")
    parser.add_argument("--yes", action="store_true", help="confirm a live run (costs money)")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if args.only:
        unknown = sorted(set(args.only) - {case.id for case in cases})
        if unknown:
            print(f"Unknown case ids: {', '.join(unknown)}")
            return 1
        cases = [case for case in cases if case.id in args.only]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("No cases selected.")
        return 1

    base = Settings()
    client: Any
    if args.mode == "mock":
        settings = base.model_copy(update={"llm_backend": "mock"})
        client = FakeAnthropicClient()
        print(MOCK_WARNING)
    else:
        settings = base.model_copy(update={"llm_backend": "anthropic"})
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
        if not key.strip():
            print("Live mode needs ANTHROPIC_API_KEY (environment or .env). Nothing was sent.")
            return 1
        print_spending_plan(cases, settings)
        if not args.yes:
            print("Nothing was sent. Re-run with --yes to start the live evaluation.")
            return 2
        client = create_client(settings)
        print(LIVE_NOTE)

    started = utc_now()
    timer = time.perf_counter()
    outcomes = asyncio.run(run_cases(cases, settings, client))
    report = build_report(
        outcomes,
        mode=args.mode,
        settings=settings,
        cases_dir=args.cases,
        started=started,
        wall_time_s=time.perf_counter() - timer,
    )
    out_dir = args.out or DEFAULT_RUNS_DIR / f"{started:%Y%m%dT%H%M%SZ}-{args.mode}"
    write_outputs(out_dir, report, outcomes)

    final = report["classification"]["final"]
    print(
        f"Final labels: accuracy {final['accuracy']:.2f} ({final['correct']}/{len(outcomes)}), "
        f"macro-F1 {final['macro_f1']:.2f}. Report: {out_dir / 'report.md'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
