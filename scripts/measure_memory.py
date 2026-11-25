"""Peak memory and CPU time of the web app on the largest allowed job. No network, no model.

    uv run python scripts/measure_memory.py --runs 1 --concurrency 2
    uv run python scripts/measure_memory.py --runs 2 --concurrency 2

How the job is built:
- The app runs in mock mode, in this process, behind an in-process HTTP client.
- MAX_URLS_PER_REQUEST synthetic sites (10 by default). For each site the fetcher serves
  a homepage and its subpages, each just under FETCH_MAX_BYTES of HTML, which is the
  largest page the real HTTP fetcher accepts.
- The pages are generated when fetched and go through the same build_page() code as
  downloaded pages, so only a few exist in memory at a time, as in production.

It submits N screenings at once, waits for all of them, and prints:
- the peak memory of the whole process (Windows: peak working set; Linux: ru_maxrss);
- the CPU time and wall time.

The numbers include the interpreter and all imports. They are measurements on this
machine, not guarantees for another OS or allocator.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import httpx

from screener.config import Settings
from screener.fake_llm import FakeAnthropicClient
from screener.fetcher import FetchedPage, build_page
from screener.fixtures import FixtureFetcher
from screener.web import create_app

SUBPAGES = ("about", "company", "contact", "locations")


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
        normalized = self.normalize(url)
        site = int(normalized.split("//", 1)[1].split(".", 1)[0].removeprefix("site"))
        path = normalized.split("/", 3)[3] if normalized.count("/") >= 3 else ""
        body = big_html(site, path, self._settings.fetch_max_bytes - 1000).encode()
        LargePageFetcher.served += 1
        return build_page(
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
    baseline = peak_memory_mb()
    cpu, wall = time.process_time(), time.perf_counter()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(*(client.post("/screen", data=form) for _ in range(runs)))
        locations = [r.headers["location"] for r in responses if r.status_code == 303]
        if len(locations) != runs:
            raise SystemExit(f"only {len(locations)} of {runs} runs started")
        while True:
            pages = [(await client.get(loc)).text for loc in locations]
            if not any("Processing" in page for page in pages):
                break
            await asyncio.sleep(0.1)
        done = [(await client.get(loc + "/export.json")).json() for loc in locations]
    statuses = [r["status"] for run in done for r in run["results"]]
    print(
        f"runs at once: {runs}, URLs per run: {n}, companies in parallel per run: {concurrency}, "
        f"page size: {settings.fetch_max_bytes - 1000:,} bytes"
    )
    print(f"pages processed: {LargePageFetcher.served}; result statuses: {sorted(set(statuses))}")
    print(f"peak memory before the runs (imports, app): {baseline:.0f} MB")
    print(f"peak memory during the runs: {peak_memory_mb():.0f} MB")
    print(
        f"CPU time: {time.process_time() - cpu:.1f} s; "
        f"wall time: {time.perf_counter() - wall:.1f} s"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=1, help="screenings submitted at once")
    parser.add_argument("--concurrency", type=int, default=2, help="CONCURRENCY setting")
    parser.add_argument("--max-bytes", type=int, default=2_000_000, help="FETCH_MAX_BYTES setting")
    args = parser.parse_args()
    asyncio.run(main(args.runs, args.concurrency, args.max_bytes))
