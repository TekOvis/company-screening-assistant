"""FastAPI web app: HTML form, results table, JSON/CSV export and a small JSON API.

Run locally (mock mode, no API key needed):
    uv run uvicorn screener.web:create_app --factory --reload
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from . import __version__
from .budget import RunBudget
from .config import Settings, get_settings
from .demo import SESSION_COOKIE, DailyBudget, RateLimiter, same_secret, session_value
from .examples import load_examples
from .export import run_to_csv, run_to_json
from .fetcher import HttpFetcher, PageFetcher
from .fixtures import FixtureFetcher
from .llm import create_client, model_label
from .netguard import UnsafeURLError
from .pipeline import screen_companies
from .schemas import CompanyResult, ScreeningCriteria, ScreeningRequest, ScreeningRun, utc_now

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
BUSY_MESSAGE = "Too many screenings are running right now. Please try again in a minute."

# How each evidence check result is shown (text, not just colour).
CHECK_LABELS = {
    "supported": "quote found · second check agreed",
    "not_supported": "second check: does not support",
    "unclear": "second check: unclear",
    "page_mismatch": "cited page was not fetched",
    "quote_not_found": "quote not found on the page",
    "value_not_in_quote": "value not in the quote",
    "not_checked": "not checked",
}


class StoreFullError(RuntimeError):
    """No free slot: every stored run is still in progress."""


class RunStore:
    """Recent runs, kept in memory only (no database by design).

    Bounded in two ways: finished runs expire ``ttl_s`` seconds after they were created,
    and at most ``max_runs`` runs are kept (the oldest finished ones are dropped first).
    Runs in progress are never dropped; how many may run at once is limited separately.
    """

    def __init__(
        self, max_runs: int, ttl_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._runs: OrderedDict[str, tuple[float, ScreeningRun]] = OrderedDict()
        self._max_runs = max_runs
        self._ttl_s = ttl_s
        self._clock = clock

    def __len__(self) -> int:
        return len(self._runs)

    def _purge_expired(self) -> None:
        now = self._clock()
        for run_id, (created, run) in list(self._runs.items()):
            if run.status != "running" and now - created > self._ttl_s:
                del self._runs[run_id]

    def active_count(self) -> int:
        return sum(1 for _, run in self._runs.values() if run.status == "running")

    def add(self, run: ScreeningRun) -> None:
        self._purge_expired()
        while len(self._runs) >= self._max_runs:
            oldest_finished = next(
                (run_id for run_id, (_, r) in self._runs.items() if r.status != "running"), None
            )
            if oldest_finished is None:
                raise StoreFullError("too many runs in progress")
            del self._runs[oldest_finished]
        self._runs[run.run_id] = (self._clock(), run)

    def get(self, run_id: str) -> ScreeningRun | None:
        self._purge_expired()
        entry = self._runs.get(run_id)
        return entry[1] if entry else None


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
    runs = RunStore(max_runs=settings.max_stored_runs, ttl_s=settings.run_ttl_s)
    tasks: set[asyncio.Task[None]] = set()
    # Demo protection (demo.py): access token, per-IP limits, daily budget.
    access_token = settings.access_token
    run_limiter = RateLimiter(settings.runs_per_ip_per_hour, 3600)
    login_limiter = RateLimiter(settings.login_attempts_per_ip_per_15_min, 900)
    daily = DailyBudget(settings.daily_max_companies, settings.daily_max_tokens)
    examples = load_examples() if settings.demo_examples else {}
    if settings.demo_examples and not examples:
        logger.warning("DEMO_EXAMPLES is on, but no example cases were found")

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.filters["safe_href"] = safe_href
    templates.env.globals["CHECK_LABELS"] = CHECK_LABELS

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
            "auth_enabled": access_token is not None,
            "examples_enabled": bool(examples),
            **extra,
        }

    def client_ip(request: Request) -> str:
        # Behind a reverse proxy, uvicorn's --proxy-headers sets this from X-Forwarded-For.
        return request.client.host if request.client else "unknown"

    def is_authenticated(request: Request) -> bool:
        if access_token is None:
            return True
        if same_secret(request.cookies.get(SESSION_COOKIE), session_value(access_token)):
            return True
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and same_secret(value.strip(), access_token)

    @app.middleware("http")
    async def require_access(request: Request, call_next: Any) -> Response:
        path = request.url.path
        if (
            access_token is None
            or path in ("/healthz", "/login")
            or path.startswith("/static/")
            or is_authenticated(request)
        ):
            return await call_next(request)
        if path.startswith(("/api/", "/openapi")) or path.endswith((".json", ".csv")):
            return JSONResponse(
                {"detail": "authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return RedirectResponse(url="/login", status_code=303)

    def admit_run(ip: str, companies: int) -> tuple[int, str | None]:
        """Per-IP rate limit, then the daily budget. Returns (token cap, refusal)."""
        wait_s = run_limiter.retry_after_s(ip)
        if wait_s:
            minutes = max(1, wait_s // 60)
            limit = settings.runs_per_ip_per_hour
            return 0, f"Limit of {limit} screenings per hour reached; try again in {minutes} min."
        cap, refusal = daily.reserve(companies, settings.max_tokens_per_run)
        if refusal is None:
            run_limiter.allow(ip)  # count the run only once it is admitted
        return cap, refusal

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

    async def execute_run(run: ScreeningRun, fetcher: PageFetcher, token_cap: int) -> None:
        run_budget = RunBudget(max_tokens=token_cap, max_requests=settings.max_llm_requests_per_run)
        try:
            try:
                async with asyncio.timeout(settings.run_timeout_s):
                    await screen_companies(
                        run.urls,
                        run.criteria,
                        settings=settings,
                        client=llm_client,
                        fetcher=fetcher,
                        on_result=run.results.append,
                        run_budget=run_budget,
                    )
                run.stop_reason = run_budget.stop_reason
            except TimeoutError:
                logger.warning("run %s hit the time limit", run.run_id)
                run.stop_reason = time_limit_reason()
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
            daily.settle(token_cap, run_budget.used_tokens)
            await fetcher.aclose()

    def time_limit_reason() -> str:
        return f"the time limit of {settings.run_timeout_s:g} s per screening was reached"

    def new_run(criteria: ScreeningCriteria, urls: list[str]) -> ScreeningRun:
        return ScreeningRun(
            run_id=secrets.token_urlsafe(12),
            backend=settings.llm_backend,
            model=model_label(settings),
            criteria=criteria,
            urls=urls,
        )

    async def launch(run: ScreeningRun, fetcher: PageFetcher, token_cap: int) -> bool:
        """Store the run and start it in the background; False if the store is full."""
        try:
            runs.add(run)
        except StoreFullError:
            daily.settle(token_cap, 0)
            await fetcher.aclose()
            return False
        task = asyncio.create_task(execute_run(run, fetcher, token_cap))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True

    def get_run_or_404(run_id: str) -> ScreeningRun:
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found (runs are kept in memory)")
        return run

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        return templates.TemplateResponse(request, "index.html", context(form={}, errors=[]))

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        if access_token is None:
            return RedirectResponse(url="/", status_code=303)
        return templates.TemplateResponse(request, "login.html", context(error=None))

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request, token: str = Form("")) -> Response:
        if access_token is None:
            return RedirectResponse(url="/", status_code=303)
        ip = client_ip(request)
        if not login_limiter.allow(ip):
            wait = login_limiter.retry_after_s(ip)
            return templates.TemplateResponse(
                request,
                "login.html",
                context(error="Too many attempts. Please try again later."),
                status_code=429,
                headers={"Retry-After": str(wait)},
            )
        if not same_secret(token.strip(), access_token):
            return templates.TemplateResponse(
                request, "login.html", context(error="Wrong access token."), status_code=401
            )
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session_value(access_token),
            max_age=12 * 3600,
            httponly=True,
            samesite="lax",  # not sent on cross-site form posts (CSRF protection)
            secure=settings.cookie_secure,
        )
        return response

    @app.post("/logout")
    async def logout() -> Response:
        response = RedirectResponse(url="/login" if access_token else "/", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

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
        if runs.active_count() >= settings.max_active_runs:
            return templates.TemplateResponse(
                request, "index.html", context(form=form, errors=[BUSY_MESSAGE]), status_code=429
            )
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

        token_cap, refusal = admit_run(client_ip(request), len(url_list))
        if refusal:
            await fetcher.aclose()
            return templates.TemplateResponse(
                request, "index.html", context(form=form, errors=[refusal]), status_code=429
            )
        run = new_run(criteria, url_list)
        if not await launch(run, fetcher, token_cap):
            return templates.TemplateResponse(
                request, "index.html", context(form=form, errors=[BUSY_MESSAGE]), status_code=429
            )
        return RedirectResponse(url=f"/runs/{run.run_id}", status_code=303)

    @app.get("/examples", response_class=HTMLResponse)
    async def list_examples(request: Request) -> Response:
        if not examples:
            raise HTTPException(status_code=404, detail="examples are not enabled")
        return templates.TemplateResponse(
            request, "examples.html", context(examples=examples, errors=[])
        )

    @app.post("/examples/{case_id}", response_class=HTMLResponse)
    async def run_example(request: Request, case_id: str) -> Response:
        """Run one synthetic example on its saved pages, under the same limits as /screen."""
        example = examples.get(case_id)
        if example is None:
            raise HTTPException(status_code=404, detail="unknown example")

        def refuse(message: str) -> Response:
            return templates.TemplateResponse(
                request, "examples.html", context(examples=examples, errors=[message]), 429
            )

        if runs.active_count() >= settings.max_active_runs:
            return refuse(BUSY_MESSAGE)
        token_cap, refusal = admit_run(client_ip(request), 1)
        if refusal:
            return refuse(refusal)
        case = example.case
        fetcher = FixtureFetcher(case.fixture_pages(), settings)
        run = new_run(case.criteria, [fetcher.normalize(case.start_url)])
        run.example_id, run.example_expected = case.id, case.expected.fit
        if not await launch(run, fetcher, token_cap):
            return refuse(BUSY_MESSAGE)
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
    async def api_screen(request: Request, payload: ScreeningRequest) -> ScreeningRun:
        """Synchronous JSON API: waits until all companies are processed."""
        if runs.active_count() >= settings.max_active_runs:
            raise HTTPException(status_code=429, detail=BUSY_MESSAGE)
        fetcher = make_fetcher()
        run: ScreeningRun | None = None
        token_cap = 0
        run_budget: RunBudget | None = None
        try:
            url_list, errors = validate_urls(fetcher, payload.urls)
            if errors:
                raise HTTPException(status_code=422, detail=errors)
            token_cap, refusal = admit_run(client_ip(request), len(url_list))
            if refusal:
                raise HTTPException(status_code=429, detail=refusal)
            run = ScreeningRun(
                run_id=secrets.token_urlsafe(12),
                backend=settings.llm_backend,
                model=model_label(settings),
                criteria=payload.criteria,
                urls=url_list,
            )
            try:
                runs.add(run)  # counts as active while it runs
            except StoreFullError as exc:
                raise HTTPException(status_code=429, detail=BUSY_MESSAGE) from exc
            run_budget = RunBudget(
                max_tokens=token_cap, max_requests=settings.max_llm_requests_per_run
            )
            collected: list[CompanyResult] = []
            try:
                async with asyncio.timeout(settings.run_timeout_s):
                    await screen_companies(
                        url_list,
                        payload.criteria,
                        settings=settings,
                        client=llm_client,
                        fetcher=fetcher,
                        on_result=collected.append,
                        run_budget=run_budget,
                    )
                run.stop_reason = run_budget.stop_reason
            except TimeoutError:
                logger.warning("API run %s hit the time limit", run.run_id)
                run.stop_reason = time_limit_reason()
            run.results = sorted(collected, key=lambda result: result.position)
            run.status = "done"
            return run
        finally:
            if run is not None and run.status == "running":
                run.status, run.error = "failed", "the request ended before the run finished"
            if run is not None:
                run.finished_at = utc_now()
            if token_cap:
                daily.settle(token_cap, run_budget.used_tokens if run_budget else 0)
            await fetcher.aclose()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "backend": settings.llm_backend, "model": model_label(settings)}

    return app
