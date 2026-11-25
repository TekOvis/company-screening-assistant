"""Peak memory and CPU time of the web app on the largest allowed job. No network, no model.

    uv run python scripts/measure_memory.py --runs 1 --concurrency 2
    uv run python scripts/measure_memory.py --runs 2 --concurrency 2

How the job is built:
- The app runs in mock mode, in this process, served by uvicorn on a free loopback port;
  the job is submitted and followed over HTTP.
- MAX_URLS_PER_REQUEST synthetic sites (10 by default). For each site the fetcher serves
  a homepage and its subpages, each just under FETCH_MAX_BYTES of HTML, which is the
  largest page the real HTTP fetcher accepts.
- The pages are generated when fetched and go through the same code as downloaded pages:
  they are parsed in the page parser's child process (screener.page_parser), so only a
  few exist in memory at a time, as in production.

It submits N screenings at once, waits for all of them, and prints:
- the peak memory of the app process and of the parser's child process (Windows: peak
  working set; Linux: VmHWM). Their sum is an upper bound: the two peaks need not happen
  at the same moment;
- the CPU time of both processes and the wall time;
- how long /healthz took to answer while the job ran, asked about once a second from a
  separate thread, as an external health check would.

The numbers include the interpreters and all imports. They are measurements on this
machine, not guarantees for another OS or allocator.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import threading
import time
import urllib.request

import httpx
import uvicorn

from screener.config import Settings
from screener.fake_llm import FakeAnthropicClient
from screener.fetcher import FetchedPage, build_page_in_parser
from screener.fixtures import FixtureFetcher
from screener.page_parser import PAGE_PARSER
from screener.web import create_app

SUBPAGES = ("about", "company", "contact", "locations")
NETWORK_DELAY_S = 0.05


def big_html(site: int, path: str, size: int) -> str:
    """Realistic-looking HTML just under `size` bytes: navigation, many blocks, scripts."""
    links = "".join(f'<a href="/{p}">{p.title()}</a>' for p in SUBPAGES)
    head = (
        f"<html><head><title>Site {site} {path}</title>"
        f"<style>{'.c{color:#123}' * 200}</style></head><body><nav>{links}</nav>"
        "<p>We design and manufacture industrial valves for process industries.</p>"
    )
    blocks = []
    total = len(head)
    i = 0
    while True:
        block = (
            f'<div class="card"><h3>Product line {i}</h3><p>Our valve series {i} covers '
            "pressure classes for water, steam and chemical media, with documentation, "
            "spare parts and service contracts for plant operators across many sectors."
            f'</p><a href="/p/{i}">Details</a><script>var x{i}=1;</script></div>'
        )
        if total + len(block) + 20 > size:
            break
        blocks.append(block)
        total += len(block)
        i += 1
    return head + "".join(blocks) + "</body></html>"


class LargePageFetcher(FixtureFetcher):
    """Serves generated large pages for every site and subpage; counts what it served."""

    served = 0

    def __init__(self, settings: Settings) -> None:
        super().__init__({}, settings)
        self._settings = settings

    async def fetch(self, url: str, *, same_site_as: str | None = None) -> FetchedPage:
        # A real fetch waits for DNS and the network, which hands the event loop to other
        # requests. Without any wait, a whole company would run in one go and block the loop
        # far longer than in production. 50 ms is a short, unfavourable network time.
        await asyncio.sleep(NETWORK_DELAY_S)
        normalized = self.normalize(url)
        site = int(normalized.split("//", 1)[1].split(".", 1)[0].removeprefix("site"))
        path = normalized.split("/", 3)[3] if normalized.count("/") >= 3 else ""
        body = big_html(site, path, self._settings.fetch_max_bytes - 1000).encode()
        LargePageFetcher.served += 1
        # Same as HttpFetcher: parsed in the parser's child process.
        return await build_page_in_parser(
            PAGE_PARSER,
            requested_url=normalized,
            final_url=normalized,
            status_code=200,
            content_type="text/html; charset=utf-8",
            body=body,
            settings=self._settings,
        )


def process_stats(pid: int) -> tuple[float, float]:
    """(peak memory in MB, CPU seconds) of a running process: this one or the parser."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        query_limited_information, vm_read = 0x1000, 0x0010
        handle = kernel32.OpenProcess(query_limited_information | vm_read, False, pid)
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
            times = [ctypes.c_ulonglong() for _ in range(4)]  # created, exited, kernel, user
            kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times))
        finally:
            kernel32.CloseHandle(handle)
        return counters.PeakWorkingSetSize / 2**20, (times[2].value + times[3].value) / 1e7
    with open(f"/proc/{pid}/status") as status:
        fields = dict(line.split(":", 1) for line in status)
    with open(f"/proc/{pid}/stat") as stat:
        ticks = stat.read().rsplit(")", 1)[1].split()  # utime and stime are fields 14 and 15
    cpu = (int(ticks[11]) + int(ticks[12])) / os.sysconf("SC_CLK_TCK")
    return int(fields["VmHWM"].split()[0]) / 1024, cpu


def probe_healthz(url: str, stop: threading.Event, latencies: list[float]) -> None:
    """Act like an external health check: from its own thread, ask /healthz over HTTP about
    once a second and record how long each answer took. The app's event loop does not
    schedule these requests, so a busy loop shows up as a slow answer."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while not stop.is_set():
        start = time.perf_counter()
        with opener.open(url, timeout=120) as response:
            response.read()
        latencies.append(time.perf_counter() - start)
        stop.wait(max(0.0, 1.0 - (time.perf_counter() - start)))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def main(runs: int, concurrency: int, max_bytes: int) -> None:
    settings = Settings(
        _env_file=None,
        llm_backend="mock",
        fetch_max_bytes=max_bytes,
        concurrency=concurrency,
        max_active_runs=runs,
        max_stored_runs=max(runs + 1, 2),
        runs_per_ip_per_hour=1000,
        daily_max_companies=10_000,
    )
    app = create_app(
        settings, client=FakeAnthropicClient(), fetcher_factory=lambda: LargePageFetcher(settings)
    )
    n = settings.max_urls_per_request
    form = {
        "urls": "\n".join(f"https://site{i}.example/" for i in range(n)),
        "industry": "industrial valves",
        "geography": "Germany",
        "target_business": "Manufacturer of industrial valves",
        "exclusions": "distributors",
    }
    base = f"http://127.0.0.1:{free_port()}"
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=int(base.rsplit(":", 1)[1]),
            limit_concurrency=32,
            log_level="warning",
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - uvicorn only exposes a flag, not an event
        await asyncio.sleep(0.05)
    baseline = process_stats(os.getpid())[0]
    cpu, wall = time.process_time(), time.perf_counter()
    stop_probe, latencies = threading.Event(), []
    prober = threading.Thread(
        target=probe_healthz, args=(base + "/healthz", stop_probe, latencies), daemon=True
    )
    prober.start()
    # No keep-alive: the server closes idle connections after 5 s, and reusing one that it
    # is closing fails with a read error.
    no_keep_alive = httpx.Limits(max_keepalive_connections=0)
    async with httpx.AsyncClient(
        base_url=base, timeout=120, trust_env=False, limits=no_keep_alive
    ) as client:
        responses = await asyncio.gather(*(client.post("/screen", data=form) for _ in range(runs)))
        locations = [r.headers["location"] for r in responses if r.status_code == 303]
        if len(locations) != runs:
            raise SystemExit(f"only {len(locations)} of {runs} runs started")
        while True:
            pages = [(await client.get(loc)).text for loc in locations]
            if not any("Processing" in page for page in pages):
                break
            await asyncio.sleep(0.1)
        cpu, wall = time.process_time() - cpu, time.perf_counter() - wall
        stop_probe.set()
        await asyncio.to_thread(prober.join)
        done = [(await client.get(loc + "/export.json")).json() for loc in locations]
    server.should_exit = True
    await serving
    # The parser's child started during the job, so all of its CPU time belongs to it.
    app_peak = process_stats(os.getpid())[0]
    parser_peak, parser_cpu = process_stats(PAGE_PARSER.pid) if PAGE_PARSER.pid else (0.0, 0.0)
    parsers_started = PAGE_PARSER.started
    PAGE_PARSER.stop()
    latencies.sort()
    statuses = [r["status"] for run in done for r in run["results"]]
    print(
        f"runs at once: {runs}, URLs per run: {n}, companies in parallel per run: {concurrency}, "
        f"page size: {settings.fetch_max_bytes - 1000:,} bytes"
    )
    print(f"pages processed: {LargePageFetcher.served}; result statuses: {sorted(set(statuses))}")
    print(f"peak memory of the app before the runs (imports, app): {baseline:.0f} MB")
    print(
        f"peak memory during the runs: app {app_peak:.0f} MB + parser {parser_peak:.0f} MB "
        f"= at most {app_peak + parser_peak:.0f} MB (parser processes started: {parsers_started})"
    )
    print(
        f"CPU time: app {cpu:.1f} s + parser {parser_cpu:.1f} s = {cpu + parser_cpu:.1f} s; "
        f"wall time: {wall:.1f} s"
    )
    print(
        f"/healthz during the job: {len(latencies)} checks; median "
        f"{latencies[len(latencies) // 2]:.2f} s, max {latencies[-1]:.2f} s; "
        f"over 1 s: {sum(t > 1 for t in latencies)}, over 5 s: {sum(t > 5 for t in latencies)}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=1, help="screenings submitted at once")
    parser.add_argument("--concurrency", type=int, default=2, help="CONCURRENCY setting")
    parser.add_argument("--max-bytes", type=int, default=2_000_000, help="FETCH_MAX_BYTES setting")
    args = parser.parse_args()
    # multiprocessing imports a script again in each child process it starts, and with this
    # one the whole web app. In production (python -m uvicorn) the parser's child does not
    # load it, so the measurement must not either. Without a file name, it is not imported.
    del sys.modules["__main__"].__file__
    asyncio.run(main(args.runs, args.concurrency, args.max_bytes))
