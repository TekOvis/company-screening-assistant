"""Ready-made synthetic examples for the demo: selected evaluation cases on saved pages.

A visitor can try the interface without pasting real URLs. The pages come from
eval/cases through FixtureFetcher; nothing is requested from the internet.

Examples are chosen for what they illustrate, not for how well a backend does on them.
Two of them are cases the mock's keyword rules get wrong: a distributor, and a company
headquartered abroad, which the mock (like the prompt) treats as not located in the
target region. The results page shows the expected label next to the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .eval_dataset import DEFAULT_CASES_DIR, EvalCase, load_cases

EXAMPLE_TITLES: dict[str, str] = {
    "01-valves-fit-homepage": "A clear fit: a valve maker located in Germany",
    "03-valves-nonfit-other-country": (
        "Missing information: headquartered in Spain, Germany only an export market; "
        "an own site in Germany is neither shown nor ruled out"
    ),
    "04-valves-insufficient-markets-only": (
        "Missing information: markets are stated, the company's own location is not"
    ),
    "09-valves-nonfit-hidden-injection": (
        "Hidden text that tries to instruct the AI, plus a private-equity owner"
    ),
    "06-valves-nonfit-distributor": (
        "A distributor, not a manufacturer: keyword rules (mock mode) get this one wrong"
    ),
}


@dataclass(frozen=True)
class Example:
    case: EvalCase
    title: str


def load_examples(directory: Path = DEFAULT_CASES_DIR) -> dict[str, Example]:
    """The configured examples found in the case directory, in display order."""
    if not directory.is_dir():
        return {}
    cases = {case.id: case for case in load_cases(directory)}
    return {
        case_id: Example(cases[case_id], title)
        for case_id, title in EXAMPLE_TITLES.items()
        if case_id in cases
    }
