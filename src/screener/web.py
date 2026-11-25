"""FastAPI web app: HTML form, results table, JSON/CSV export and a small JSON API.

Run locally (mock mode, no API key needed):
    uv run uvicorn screener.web:create_app --factory --reload
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections import OrderedDict
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from . import __version__
from .agent import create_client, model_label
from .config import Settings, get_settings
from .export import run_to_csv, run_to_json
from .fetcher import HttpFetcher, PageFetcher
from .netguard import UnsafeURLError
from .pipeline import screen_companies
from .schemas import ScreeningCriteria, ScreeningRequest, ScreeningRun, utc_now

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
_URL_SPLIT_RE = re.compile(r"[\s,]+")
_FIELD_LABELS = {
    "industry": "Industry",
    "geography": "Geography",
    "target_business": "Target business",
    "exclusions": "Exclusions",
}

FetcherFactory = Callable[[], PageFetcher]


class RunStore:
    """The most recent runs, kept in memory only (no database by design)."""

    def __init__(self, max_runs: int = 100) -> None:
        self._runs: OrderedDict[str, ScreeningRun] = OrderedDict()
        self._max_runs = max_runs

    def add(self, run: ScreeningRun) -> None:
        self._runs[run.run_id] = run
        while len(self._runs) > self._max_runs:
            self._runs.popitem(last=False)

    def get(self, run_id: str) -> ScreeningRun | None:
        return self._runs.get(run_id)


def parse_url_list(text: str) -> list[str]:
    """Split pasted text into URLs (newlines, spaces or commas), dropping duplicates."""
    return list(dict.fromkeys(item for item in _URL_SPLIT_RE.split(text) if item))


def safe_href(url: str | None) -> str | None:
    """Only http(s) URLs may become links; model output could contain javascript: URLs."""
    if url and url.lower().startswith(("http://", "https://")):
        return url
    return None


def create_app(
    settings: Settings | None = None,
    *,
    client: Any = None,
    fetcher_factory: FetcherFactory | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    llm_client = client if client is not None else create_client(settings)
    make_fetcher: FetcherFactory = fetcher_factory or (lambda: HttpFetcher(settings))
    runs = RunStore()
    tasks: set[asyncio.Task[None]] = set()

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.filters["safe_href"] = safe_href

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Company Screening Assistant", version=__version__, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def context(**extra: Any) -> dict[str, Any]:
        return {
            "backend": settings.llm_backend,
            "model": model_label(settings),
            "max_urls": settings.max_urls_per_request,
            **extra,
        }

    def validate_urls(fetcher: PageFetcher, raw_urls: list[str]) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        if not raw_urls:
            return [], ["Enter at least one company URL."]
        if len(raw_urls) > settings.max_urls_per_request:
            limit = settings.max_urls_per_request
            return [], [f"At most {limit} URLs per request (got {len(raw_urls)})."]
        normalized: list[str] = []
        for raw in raw_urls:
            try:
                normalized.append(fetcher.normalize(raw))
            except UnsafeURLError as exc:
                errors.append(f"{raw}: {exc}")
        return list(dict.fromkeys(normalized)), errors

    async def execute_run(run: ScreeningRun, fetcher: PageFetcher) -> None:
        try:
            await screen_companies(
                run.urls,
                run.criteria,
                settings=settings,
                client=llm_client,
                fetcher=fetcher,
                on_result=run.results.append,
            )
            run.results.sort(key=lambda result: result.position)
            run.status = "done"
        except asyncio.CancelledError:
            run.status, run.error = "failed", "cancelled because the server is shutting down"
            raise
        except Exception:
            logger.exception("run %s failed", run.run_id)
            run.status, run.error = "failed", "internal error while processing the run"
        finally:
            run.finished_at = utc_now()
            await fetcher.aclose()

    def get_run_or_404(run_id: str) -> ScreeningRun:
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found (runs are kept in memory)")
        return run

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        return templates.TemplateResponse(request, "index.html", context(form={}, errors=[]))

    @app.post("/screen", response_class=HTMLResponse)
    async def submit(
        request: Request,
        urls: str = Form(""),
        industry: str = Form(""),
        geography: str = Form(""),
        target_business: str = Form(""),
        exclusions: str = Form(""),
    ) -> Response:
        form = {
            "urls": urls,
            "industry": industry,
            "geography": geography,
            "target_business": target_business,
            "exclusions": exclusions,
        }
        fetcher = make_fetcher()
        url_list, errors = validate_urls(fetcher, parse_url_list(urls))
        criteria: ScreeningCriteria | None = None
        try:
            criteria = ScreeningCriteria(
                industry=industry,
                geography=geography,
                target_business=target_business,
                exclusions=exclusions or None,
            )
        except ValidationError as exc:
            for error in exc.errors():
                field = str(error["loc"][0]) if error["loc"] else "input"
                errors.append(f"{_FIELD_LABELS.get(field, field)}: {error['msg']}")
        if errors or criteria is None:
            await fetcher.aclose()
            return templates.TemplateResponse(
                request, "index.html", context(form=form, errors=errors), status_code=422
            )

        run = ScreeningRun(
            run_id=secrets.token_urlsafe(12),
            backend=settings.llm_backend,
            model=model_label(settings),
            criteria=criteria,
            urls=url_list,
        )
        runs.add(run)
        task = asyncio.create_task(execute_run(run, fetcher))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return RedirectResponse(url=f"/runs/{run.run_id}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def show_run(request: Request, run_id: str) -> Response:
        run = get_run_or_404(run_id)
        results = sorted(run.results, key=lambda result: result.position)
        return templates.TemplateResponse(
            request, "run.html", context(run=run, results=results, usage=run.total_usage)
        )

    @app.get("/runs/{run_id}/export.json")
    async def export_json(run_id: str) -> Response:
        run = get_run_or_404(run_id)
        return Response(
            run_to_json(run),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="screening-{run.run_id}.json"'},
        )

    @app.get("/runs/{run_id}/export.csv")
    async def export_csv(run_id: str) -> Response:
        run = get_run_or_404(run_id)
        return Response(
            run_to_csv(run),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="screening-{run.run_id}.csv"'},
        )

    @app.post("/api/screen", response_model=ScreeningRun)
    async def api_screen(payload: ScreeningRequest) -> ScreeningRun:
        """Synchronous JSON API: waits until all companies are processed."""
        fetcher = make_fetcher()
        try:
            url_list, errors = validate_urls(fetcher, payload.urls)
            if errors:
                raise HTTPException(status_code=422, detail=errors)
            run = ScreeningRun(
                run_id=secrets.token_urlsafe(12),
                backend=settings.llm_backend,
                model=model_label(settings),
                criteria=payload.criteria,
                urls=url_list,
            )
            run.results = await screen_companies(
                url_list, payload.criteria, settings=settings, client=llm_client, fetcher=fetcher
            )
            run.status, run.finished_at = "done", utc_now()
            runs.add(run)
            return run
        finally:
            await fetcher.aclose()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "backend": settings.llm_backend, "model": model_label(settings)}

    return app
