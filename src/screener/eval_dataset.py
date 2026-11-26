"""Loading the synthetic evaluation set (eval/cases/*.json).

Every case is synthetic: invented companies on reserved ``*.example`` domains, with the
expected answers written down before any evaluation run (see eval/README.md). Pages are
served from memory by FixtureFetcher, so an evaluation never requests these domains from
the internet — not in mock mode and not in live mode.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .fixtures import FixturePage
from .schemas import FitLabel, ScreeningCriteria

DEFAULT_CASES_DIR = Path(__file__).resolve().parents[2] / "eval" / "cases"


class CasePage(BaseModel):
    """A saved page: HTML lines, or a status code (e.g. 404) / redirect without a body."""

    model_config = ConfigDict(extra="forbid")

    html: list[str] | None = None
    status: int = 200
    location: str | None = None

    def to_fixture(self) -> FixturePage:
        return FixturePage(
            body="\n".join(self.html or []), status_code=self.status, location=self.location
        )


class Expected(BaseModel):
    """Answers fixed before evaluation. Lists of alternatives allow equivalent spellings."""

    model_config = ConfigDict(extra="forbid")

    fit: FitLabel
    company_name: list[str] = Field(default_factory=list)  # accepted spellings; empty = any
    # One inner list per place the company itself is located in (alternatives inside).
    company_locations: list[list[str]] = Field(default_factory=list)
    # Places that must NOT be reported as the company's own location (traps).
    forbidden_locations: list[str] = Field(default_factory=list)
    # Markets the text states (optional, used for a recall figure only).
    markets_served: list[list[str]] = Field(default_factory=list)


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    synthetic: bool
    description: str
    tags: list[str]
    criteria: ScreeningCriteria
    start_url: str
    pages: dict[str, CasePage]
    expected: Expected
    label_rationale: str

    @model_validator(mode="after")
    def _check(self) -> EvalCase:
        if not self.synthetic:
            raise ValueError(f"{self.id}: only synthetic cases belong in this set")
        if self.start_url not in self.pages:
            raise ValueError(f"{self.id}: start_url must be one of the pages")
        for url in self.pages:
            host = urlsplit(url).hostname or ""
            if not host.endswith(".example"):
                raise ValueError(f"{self.id}: {url} is not on a reserved .example domain")
        return self

    def fixture_pages(self) -> dict[str, FixturePage]:
        return {url: page.to_fixture() for url, page in self.pages.items()}

    def all_text(self) -> str:
        return "\n".join(line for page in self.pages.values() for line in page.html or [])


def load_cases(directory: Path = DEFAULT_CASES_DIR) -> list[EvalCase]:
    cases = []
    for path in sorted(directory.glob("*.json")):
        case = EvalCase.model_validate_json(path.read_text(encoding="utf-8"))
        if case.id != path.stem:
            raise ValueError(f"{path.name}: id {case.id!r} does not match the file name")
        cases.append(case)
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("duplicate case ids")
    return cases


def dataset_sha256(directory: Path = DEFAULT_CASES_DIR) -> str:
    """Fingerprint of the case files, recorded in every report (which data was used)."""
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.json")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()
