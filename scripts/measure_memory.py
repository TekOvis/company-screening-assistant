"""Peak memory and CPU time of the web app on the largest allowed job. No network, no model.

    uv run python scripts/measure_memory.py --runs 1 --concurrency 2
    uv run python scripts/measure_memory.py --runs 2 --concurrency 2

How the job is built:
- The app runs in mock mode, in this process, served by uvicorn on a free loopback port;
  the job is submitted and followed over HTTP.
- MAX_URLS_PER_REQUEST synthetic sites (10 by default). For each site the fetcher serves
  a homepage and its subpages, each just under FETCH_MAX_BYTES of HTML, which is the
  largest page the real HTTP fetcher accepts.
- The pages are generated when fetched and go through the same build_page() code as
  downloaded pages, so only a few exist in memory at a time, as in production.

It submits N screenings at once, waits for all of them, and prints:
- the peak memory of the whole process (Windows: peak working set; Linux: ru_maxrss);
- the CPU time and wall time;
- how long /healthz took to answer while the job ran, asked about once a second from a
  separate thread. HTML parsing runs in the event loop, so this is how long an external
  health check would wait.

The numbers include the interpreter and all imports. They are measurements on this
machine, not guarantees for another OS or allocator.
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
import threading
import time
import urllib.request

import httpx
import uvicorn

from screener.config import Settings
from screener.fake_llm import FakeAnthropicClient
from screener.fetcher import FetchedPage, build_page_in_thread
from screener.fixtures import FixtureFetcher
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
        # Same as HttpFetcher: parsed in the parser thread.
        return await build_page_in_thread(
            requested_url=normalized,
            final_url=normalized,
            status_code=200,
            content_type="text/html; charset=utf-8",
            body=body,
            settings=self._settings,
        )


def peak_memory_mb() -> float:
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

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        return counters.PeakWorkingSetSize / 2**20
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # Linux: kilobytes


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
    baseline = peak_memory_mb()
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
    latencies.sort()
    statuses = [r["status"] for run in done for r in run["results"]]
    print(
        f"runs at once: {runs}, URLs per run: {n}, companies in parallel per run: {concurrency}, "
        f"page size: {settings.fetch_max_bytes - 1000:,} bytes"
    )
    print(f"pages processed: {LargePageFetcher.served}; result statuses: {sorted(set(statuses))}")
    print(f"peak memory before the runs (imports, app): {baseline:.0f} MB")
    print(f"peak memory during the runs: {peak_memory_mb():.0f} MB")
    print(f"CPU time: {cpu:.1f} s; wall time: {wall:.1f} s")
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
    asyncio.run(main(args.runs, args.concurrency, args.max_bytes))
