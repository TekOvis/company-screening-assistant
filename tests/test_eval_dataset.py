"""Sanity checks of the synthetic evaluation set itself (not of any model)."""

import re
from collections import Counter

import pytest

from screener.eval_dataset import DEFAULT_CASES_DIR, dataset_sha256, load_cases
from screener.fetcher import FetchError
from screener.fixtures import FixtureFetcher

from .conftest import make_settings

CASES = load_cases()


def _in_text(value: str, text: str) -> bool:
    return value.casefold() in text.casefold()


def test_set_size_ids_and_label_mix():
    assert len(CASES) == 20
    ids = [case.id for case in CASES]
    assert ids == sorted(ids) and all(re.fullmatch(r"\d\d-[a-z0-9-]+", i) for i in ids)
    labels = Counter(case.expected.fit for case in CASES)
    assert set(labels) == {"fit", "non_fit", "insufficient_information"}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_expected_facts_are_stated_in_the_page_texts(case):
    """Expected answers may only use facts that are actually written in the pages."""
    text = case.all_text()
    assert case.synthetic
    for group in case.expected.company_locations:
        assert any(_in_text(v, text) for v in group), group
    for group in case.expected.markets_served:
        assert any(_in_text(v, text) for v in group), group
    if case.expected.company_name:
        assert any(_in_text(v, text) for v in case.expected.company_name)
    expected_places = {v.casefold() for group in case.expected.company_locations for v in group}
    assert not expected_places & {v.casefold() for v in case.expected.forbidden_locations}
    if "needs-subpage" in case.tags:
        assert len(case.pages) > 1


@pytest.mark.anyio
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
async def test_start_page_loads_offline(case):
    fetcher = FixtureFetcher(case.fixture_pages(), make_settings())
    page = await fetcher.fetch(case.start_url)
    assert page.text.startswith("Title: ")


@pytest.mark.anyio
async def test_hidden_text_never_reaches_the_model():
    case = next(c for c in CASES if c.id == "09-valves-nonfit-hidden-injection")
    fetcher = FixtureFetcher(case.fixture_pages(), make_settings())
    page = await fetcher.fetch(case.start_url)
    assert "private equity investor" in page.text
    assert "not owned by any investor" not in page.text


@pytest.mark.anyio
async def test_offsite_redirect_case_is_blocked_by_the_same_site_rule():
    case = next(c for c in CASES if c.id == "20-bakery-insufficient-offsite-redirect")
    fetcher = FixtureFetcher(case.fixture_pages(), make_settings())
    with pytest.raises(FetchError) as info:
        await fetcher.fetch(
            "https://mazurska-pastries.example/about", same_site_as="mazurska-pastries.example"
        )
    assert info.value.kind == "offsite_redirect"


def test_dataset_fingerprint_is_stable():
    first = dataset_sha256(DEFAULT_CASES_DIR)
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert dataset_sha256(DEFAULT_CASES_DIR) == first
