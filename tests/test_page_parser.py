"""The page parser's child process: same results, failures, and stopping a stuck parse."""

import asyncio
import os
import time

import pytest

from screener.html_text import extract_page
from screener.page_parser import PageParser, ParserError

from . import parse_targets
from .conftest import ACME_HOME

OPTIONS = {
    "base_url": "https://acme.com/",
    "mime_type": "text/html",
    "charset": "utf-8",
    "max_chars": 15_000,
    "max_links": 40,
}


@pytest.fixture
def parser():
    parser = PageParser(target=parse_targets.extract_for_tests)
    yield parser
    parser.stop()


@pytest.mark.anyio
async def test_parses_in_one_child_process_with_the_same_result(parser):
    page = await parser.extract(ACME_HOME.encode(), **OPTIONS)
    assert page == extract_page(ACME_HOME.encode(), **OPTIONS)
    reported = await parser.extract(parse_targets.REPORT_PID, **OPTIONS)
    assert int(reported.title) == parser.pid != os.getpid()
    assert parser.started == 1  # the same child for every page


@pytest.mark.anyio
async def test_a_time_limit_kills_a_stuck_parse_and_the_loop_keeps_running(parser):
    await parser.extract(b"<p>warm up</p>", **OPTIONS)  # the child is running
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(tick())
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(1):
            await parser.extract(parse_targets.HANG, **OPTIONS)
    elapsed = time.monotonic() - start
    ticker.cancel()

    assert elapsed < 3  # the limit ended the wait
    assert ticks > 30  # the event loop kept running meanwhile (about 60-100 ticks)
    assert parser.pid is None  # the stuck child was killed
    page = await parser.extract(ACME_HOME.encode(), **OPTIONS)
    assert page.title == "Acme Valves | Industrial valves"
    assert parser.started == 2


@pytest.mark.anyio
async def test_a_page_that_raises_is_an_error_and_the_child_keeps_working(parser):
    with pytest.raises(ParserError, match="ValueError"):
        await parser.extract(parse_targets.RAISE, **OPTIONS)
    page = await parser.extract(ACME_HOME.encode(), **OPTIONS)
    assert page.title and parser.started == 1


@pytest.mark.anyio
async def test_a_crashed_child_is_replaced_on_the_next_page(parser):
    with pytest.raises(ParserError, match="stopped unexpectedly"):
        await parser.extract(parse_targets.CRASH, **OPTIONS)
    assert parser.pid is None
    page = await parser.extract(ACME_HOME.encode(), **OPTIONS)
    assert page.title and parser.started == 2
