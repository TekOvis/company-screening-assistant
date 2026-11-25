"""HTML parsing in a child process that can be stopped.

Parsing is CPU-bound Python. A page that takes too long (a pathological page or a parser
bug) cannot be stopped inside the app, because a thread cannot be killed: with the earlier
parser thread, one such page held the only parser and every later page waited behind it.

So pages are parsed in one child process:

* When the caller stops waiting (the page's or the screening's time limit), the child is
  killed and the next page starts a fresh one. Nothing keeps running in the background,
  and no new thread or process is added per timeout.
* One child, one page at a time, as with the parser thread before: parsing more pages at
  once would not be faster on one CPU and would hold more pages in memory.
* The event loop never waits on the child: it checks the pipe every few milliseconds, so
  the app keeps answering other requests and the health check while a page is parsed.
* The child is started on first use, imports only what parsing needs, and lives until it
  is stopped or the app exits.

A ``PageParser`` serves one event loop at a time (the app has one).
"""

from __future__ import annotations

import asyncio
import multiprocessing
import signal
from collections.abc import Callable
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Any

from .html_text import ExtractedPage, extract_page

PROCESS_NAME = "html-parse"
START_TIMEOUT_S = 30.0  # imports in a fresh child on a slow, busy server
POLL_MIN_S = 0.001
POLL_MAX_S = 0.02

Target = Callable[..., ExtractedPage]


class ParserError(Exception):
    """The page could not be parsed: the parser failed to start, stopped, or raised."""


def _serve(conn: Connection, target: Target) -> None:
    """The child process: parse pages until the parent closes the pipe or kills us."""
    # Ctrl+C in a terminal reaches the whole process group; the parent decides when we stop.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    conn.send(("ready", None))
    while True:
        try:
            body, kwargs = conn.recv()
        except (EOFError, OSError):
            return  # the parent is gone
        try:
            reply = ("ok", target(body, **kwargs))
        except Exception as exc:  # a broken page must not end the child
            reply = ("error", type(exc).__name__)
        conn.send(reply)


class PageParser:
    def __init__(self, target: Target = extract_page) -> None:
        self._target = target  # must be importable by name (the child imports it)
        self._context = multiprocessing.get_context("spawn")  # no fork of a threaded app
        self._process: BaseProcess | None = None
        self._conn: Connection | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self.started = 0  # child processes started so far

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    async def extract(self, body: bytes, **kwargs: Any) -> ExtractedPage:
        """``target(body, **kwargs)`` in the child process."""
        async with self._loop_lock():
            try:
                conn = await self._ready_connection()
                conn.send((body, kwargs))
                status, value = await self._receive(conn)
            except BaseException:
                # Cancelled by a time limit, or the child failed. A busy child would answer
                # this page to the next caller and might never finish: stop it.
                self.stop()
                raise
        if status != "ok":
            raise ParserError(f"the page parser raised {value}")
        return value

    def stop(self) -> None:
        """Kill the child, if there is one. The next page starts a new one."""
        process, conn = self._process, self._conn
        self._process = self._conn = None
        if conn is not None:
            conn.close()
        if process is not None:
            process.kill()
            process.join(timeout=5)

    def _loop_lock(self) -> asyncio.Lock:
        # One page at a time. A lock belongs to one event loop; tests run several loops
        # one after another, so a new loop gets a new lock.
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    async def _ready_connection(self) -> Connection:
        if self._process is not None and self._conn is not None and self._process.is_alive():
            return self._conn
        self.stop()  # a child that ended by itself
        parent_conn, child_conn = self._context.Pipe()
        process = self._context.Process(
            target=_serve, args=(child_conn, self._target), name=PROCESS_NAME, daemon=True
        )
        process.start()
        child_conn.close()
        self._process, self._conn = process, parent_conn
        self.started += 1
        await self._receive(parent_conn, timeout_s=START_TIMEOUT_S)  # ("ready", None)
        return parent_conn

    async def _receive(self, conn: Connection, *, timeout_s: float | None = None) -> Any:
        loop = asyncio.get_running_loop()
        deadline = None if timeout_s is None else loop.time() + timeout_s
        delay = POLL_MIN_S
        try:
            while not conn.poll():
                if self._process is None or not self._process.is_alive():
                    raise ParserError("the page parser stopped unexpectedly")
                if deadline is not None and loop.time() > deadline:
                    raise ParserError("the page parser did not start in time")
                await asyncio.sleep(delay)
                delay = min(delay * 2, POLL_MAX_S)
            return conn.recv()
        except (EOFError, OSError) as exc:
            raise ParserError("the page parser stopped unexpectedly") from exc


# Shared by all fetchers of the app; no process exists until the first page is parsed.
PAGE_PARSER = PageParser()
