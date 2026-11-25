"""The claude_code backend: the real model through the local Claude Code login.

Purpose: lets the author use a Claude subscription for local development and evaluation
without a separate API key. **Personal, local use only.** Anthropic does not allow
offering claude.ai login or subscription rate limits in products for other people, so a
public demo must use the API-key backend (or mock).

Requirements: Claude Code installed and logged in with the subscription, and the optional
extra (``uv sync --extra claude-code``), which brings the Agent SDK and its bundled CLI.
This module never reads credential files: authentication is left to the Claude Code CLI.

Everything this agent does not need is switched off:
* ``tools=[]``: no built-in tools (no shell, files or web access). The only tool is our
  ``fetch_company_page``, served in-process, so the same path validation, SSRF protection,
  same-site rule and call limit apply as in the API backend;
* ``strict_mcp_config`` and ``setting_sources=[]``: none of the user's MCP servers,
  settings, hooks, plugins or CLAUDE.md files are loaded;
* ``permission_mode="dontAsk"``: anything not pre-approved is denied, nothing prompts;
* ``--no-session-persistence``, ``--disable-slash-commands``, our own system prompt and a
  separate empty working directory. ``--bare`` is NOT used: it ignores the subscription.

If an API key or similar variable is set, Claude Code would use it instead of the
subscription (and bill the API), so this backend refuses to start. When the subscription
usage limit is reached, the run stops with a clear message; nothing falls back to the API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from anthropic.types import Message
from pydantic import ValidationError

from .agent import ASSESSMENT_SCHEMA, AgentOutcome
from .budget import BudgetExceeded, CompanyBudget, estimate_request_tokens
from .config import Settings
from .fetcher import FetchedPage, PageFetcher
from .llm import BackendError, ConfigurationError
from .prompts import initial_user_message, system_prompt
from .schemas import Assessment, ResultStatus, ScreeningCriteria, Usage
from .tools import FETCH_TOOL, FETCH_TOOL_NAME, run_fetch_tool, site_url_for, tool_result

logger = logging.getLogger(__name__)

MCP_SERVER_NAME = "screener"
ALLOWED_TOOL = f"mcp__{MCP_SERVER_NAME}__{FETCH_TOOL_NAME}"
# Variables that make Claude Code authenticate with something other than the user's
# subscription login: an API key or token, another endpoint, or a cloud provider.
AUTH_OVERRIDE_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
VERIFIER_MAX_TURNS = 3  # one answer; structured output may take an extra turn
_QUOTA_ERRORS = {"rate_limit", "billing_error"}
_AUTH_ERRORS = {"authentication_failed"}

QueryFn = Callable[..., AsyncIterator[Any]]
ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def check_auth_environment(environ: Mapping[str, str]) -> None:
    """Refuse to start if Claude Code would use something other than the subscription."""
    present = [name for name in AUTH_OVERRIDE_VARS if environ.get(name)]
    if present:
        raise BackendError(
            "setup",
            f"{', '.join(present)} is set, so Claude Code would use it instead of your "
            "subscription (for example, bill the API). Unset it to use LLM_BACKEND="
            "claude_code, or use LLM_BACKEND=anthropic for the API on purpose.",
        )


def sandbox_dir() -> Path:
    """An empty working directory for the CLI (no project files, no CLAUDE.md)."""
    path = Path(tempfile.gettempdir()) / "screener-claude-code"
    path.mkdir(exist_ok=True)
    return path


def build_options(
    settings: Settings,
    *,
    system: str,
    output_schema: dict[str, Any],
    max_turns: int,
    effort: str | None,
    mcp_servers: dict[str, Any] | None = None,
    allowed_tools: list[str] | None = None,
) -> Any:
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        system_prompt=system,  # replaces Claude Code's own system prompt
        model=settings.llm_model,
        effort=effort,
        tools=[],  # no built-in tools at all
        allowed_tools=allowed_tools or [],
        mcp_servers=mcp_servers or {},
        strict_mcp_config=True,  # ignore the user's own MCP servers
        setting_sources=[],  # no user/project/local settings, hooks, plugins, CLAUDE.md
        permission_mode="dontAsk",  # never prompt; deny what is not pre-approved
        max_turns=max_turns,
        output_format={"type": "json_schema", "schema": output_schema},
        cwd=str(sandbox_dir()),
        cli_path=settings.claude_code_cli_path,
        extra_args={"no-session-persistence": None, "disable-slash-commands": None},
    )


def make_fetch_handler(
    settings: Settings, site_url: str, fetcher: PageFetcher, outcome: AgentOutcome
) -> ToolHandler:
    """The MCP tool body: the same validation, fetching and limits as the API backend."""

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        outcome.tool_calls += 1
        if outcome.tool_calls > settings.max_tool_calls:
            result = tool_result(
                "mcp", "Tool call limit reached. Give your final answer.", is_error=True
            )
        else:
            block = SimpleNamespace(id="mcp", name=FETCH_TOOL_NAME, input=args)
            result = await run_fetch_tool(
                block, site_url=site_url, fetcher=fetcher, pages=outcome.pages
            )
        return {
            "content": [{"type": "text", "text": result["content"]}],
            "is_error": bool(result.get("is_error")),
        }

    return handler


@dataclass
class _Run:
    result: Any = None  # the ResultMessage
    rate_limit: Any = None  # RateLimitInfo that made us stop
    assistant_errors: set[str] = field(default_factory=set)


def quota_message(info: Any) -> str:
    text = "Your Claude subscription usage limit was reached"
    window = getattr(info, "rate_limit_type", None)
    if window:
        text += f" ({str(window).replace('_', ' ')} window)"
    resets_at = getattr(info, "resets_at", None)
    if resets_at:
        text += f"; it resets at {datetime.fromtimestamp(resets_at, UTC):%Y-%m-%d %H:%M} UTC"
    return text + ". The run was stopped; nothing was sent to the paid API."


class ClaudeCodeClient:
    """Client for the claude_code backend.

    * ``run_agent()`` runs the agent step inside Claude Code with our tool.
    * ``messages.create()`` is a one-turn structured call without tools, shaped like the
      Messages API, so the evidence check (verify.py) works unchanged.
    """

    backend = "claude_code"

    def __init__(
        self,
        settings: Settings,
        *,
        query_fn: QueryFn | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        check_auth_environment(self._environ)
        if query_fn is None:
            try:
                from claude_agent_sdk import query as sdk_query
            except ImportError as exc:
                raise ConfigurationError(
                    "LLM_BACKEND=claude_code needs the optional extra: uv sync --extra claude-code"
                ) from exc
            query_fn = sdk_query
        self._settings = settings
        self._query = query_fn
        self._slots = asyncio.Semaphore(settings.claude_code_concurrency)
        self.messages = _MessagesAdapter(self)

    async def run_agent(
        self,
        criteria: ScreeningCriteria,
        homepage: FetchedPage,
        fetcher: PageFetcher,
        *,
        budget: CompanyBudget,
        usage: Usage,
    ) -> AgentOutcome:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        settings = self._settings
        outcome = AgentOutcome(status="ok", pages={homepage.url: homepage})

        def fail(status: ResultStatus, error: str) -> AgentOutcome:
            outcome.status, outcome.error = status, error
            return outcome

        handler = make_fetch_handler(settings, site_url_for(homepage), fetcher, outcome)
        fetch_tool = tool(FETCH_TOOL_NAME, FETCH_TOOL["description"], FETCH_TOOL["input_schema"])(
            handler
        )
        server = create_sdk_mcp_server(name=MCP_SERVER_NAME, version="1.0.0", tools=[fetch_tool])
        system = system_prompt(settings.max_tool_calls)
        prompt = initial_user_message(criteria, homepage)
        options = build_options(
            settings,
            system=system,
            output_schema=ASSESSMENT_SCHEMA,
            max_turns=settings.claude_code_max_turns,
            effort=settings.llm_effort,
            mcp_servers={MCP_SERVER_NAME: server},
            allowed_tools=[ALLOWED_TOOL],
        )
        # The token budget is checked before the run (one turn's worst case) and charged
        # with the real usage after it; inside the run, max_turns and the tool limit bound it.
        reserved = estimate_request_tokens(
            {
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
                "tools": [FETCH_TOOL],
                "output_config": {"format": {"schema": ASSESSMENT_SCHEMA}},
                "max_tokens": settings.llm_max_output_tokens,
            }
        )
        try:
            budget.reserve(reserved)
        except BudgetExceeded as exc:
            return fail("budget_exceeded", str(exc))
        usage.llm_requests += 1
        try:
            run = await self._run(prompt, options)
        except BackendError as exc:
            budget.settle(reserved, 0)
            usage.failed_requests += 1
            if exc.quota:
                budget.run.stop(exc.message)
            return fail("llm_error", exc.message)

        result = run.result
        _account(result, budget=budget, reserved=reserved, usage=usage)
        if result.subtype == "error_max_turns":
            return fail(
                "invalid_output",
                f"Claude Code reached its turn limit ({settings.claude_code_max_turns}) "
                "without a final answer",
            )
        if result.subtype == "error_max_structured_output_retries":
            return fail("invalid_output", "Claude Code produced no answer that matched the schema")
        if result.is_error or result.structured_output is None:
            return fail("llm_error", f"Claude Code did not finish the task ({result.subtype})")
        try:
            outcome.assessment = Assessment.model_validate(result.structured_output)
        except ValidationError as exc:
            return fail(
                "invalid_output",
                f"the answer did not match the schema ({exc.error_count()} errors)",
            )
        return outcome

    async def _run(self, prompt: str, options: Any) -> _Run:
        """Run one Claude Code session; raise BackendError for quota, auth, setup, timeout."""
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeSDKError,
            CLINotFoundError,
            RateLimitEvent,
            ResultMessage,
            SystemMessage,
        )

        check_auth_environment(self._environ)  # again: the environment may have changed
        run = _Run()
        timeout_s = self._settings.claude_code_timeout_s
        async with self._slots:
            stream = self._query(prompt=prompt, options=options)
            try:
                async with asyncio.timeout(timeout_s):
                    async for message in stream:
                        if isinstance(message, SystemMessage) and message.subtype == "init":
                            # Only the name of the credential source is logged, never a secret.
                            logger.info(
                                "Claude Code session: model=%s, apiKeySource=%s, tools=%s",
                                message.data.get("model"),
                                message.data.get("apiKeySource"),
                                message.data.get("tools"),
                            )
                        elif isinstance(message, RateLimitEvent):
                            info = message.rate_limit_info
                            # Stop at the limit, also before paid "overage" usage starts.
                            if info.status == "rejected" or info.rate_limit_type == "overage":
                                run.rate_limit = info
                                break
                        elif isinstance(message, AssistantMessage) and message.error:
                            run.assistant_errors.add(str(message.error))
                        elif isinstance(message, ResultMessage):
                            run.result = message
            except TimeoutError as exc:
                raise BackendError(
                    "timeout", f"Claude Code did not finish within {timeout_s:.0f} s"
                ) from exc
            except CLINotFoundError as exc:
                raise BackendError(
                    "setup",
                    "Claude Code CLI not found: run 'uv sync --extra claude-code' or set "
                    "CLAUDE_CODE_CLI_PATH",
                ) from exc
            except ClaudeSDKError as exc:
                # query() raises after yielding an error result; keep what we collected.
                if run.result is None and run.rate_limit is None and not run.assistant_errors:
                    raise BackendError("failed", f"Claude Code failed: {str(exc)[:200]}") from exc
            finally:
                await _close(stream)  # stops the CLI process when we leave early

        status = getattr(run.result, "api_error_status", None)
        if run.rate_limit is not None or run.assistant_errors & _QUOTA_ERRORS or status == 429:
            raise BackendError("quota", quota_message(run.rate_limit))
        if run.assistant_errors & _AUTH_ERRORS or status in (401, 403):
            raise BackendError(
                "auth",
                "Claude Code is not logged in or the login expired. Open Claude Code and sign "
                "in with your subscription; nothing was sent to the paid API.",
            )
        if run.result is None:
            raise BackendError("failed", "Claude Code ended without a result")
        return run


class _MessagesAdapter:
    """``messages.create(**params)`` for one-turn structured calls (the evidence check)."""

    def __init__(self, client: ClaudeCodeClient) -> None:
        self._client = client

    async def create(self, **params: Any) -> Message:
        output_format = (params.get("output_config") or {}).get("format")
        if params.get("tools") or not output_format:
            raise ValueError("claude_code messages adapter: one-turn structured calls only")
        options = build_options(
            self._client._settings,
            system=params["system"],
            output_schema=output_format["schema"],
            max_turns=VERIFIER_MAX_TURNS,
            effort=(params.get("output_config") or {}).get("effort"),
        )
        run = await self._client._run(_user_text(params["messages"]), options)
        result = run.result
        if result.is_error or result.structured_output is None:
            raise BackendError(
                "failed", f"Claude Code returned no structured answer ({result.subtype})"
            )
        tokens = _usage_dict(result)
        return Message.model_validate(
            {
                "id": f"msg_claude_code_{result.session_id}",
                "type": "message",
                "role": "assistant",
                "model": params["model"],
                "content": [{"type": "text", "text": json.dumps(result.structured_output)}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": tokens,
            }
        )


async def _close(stream: Any) -> None:
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:  # noqa: S110 - closing must never hide the real outcome
        pass


def _user_text(messages: list[dict[str, Any]]) -> str:
    if len(messages) != 1 or messages[0].get("role") != "user":
        raise ValueError("claude_code messages adapter: exactly one user message expected")
    content = messages[0]["content"]
    if not isinstance(content, str):
        raise ValueError("claude_code messages adapter: text content expected")
    return content


def _usage_dict(result: Any) -> dict[str, int]:
    usage = result.usage or {}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
    }


def _account(result: Any, *, budget: CompanyBudget, reserved: int, usage: Usage) -> None:
    """Record what one Claude Code run used: every turn counts as a model request."""
    turns = max(1, int(result.num_turns or 1))
    tokens = _usage_dict(result)
    call = Usage(llm_calls=turns, **tokens)
    usage.add(call)
    usage.llm_requests += turns - 1  # the first request was counted before the run
    if result.is_error:
        usage.failed_requests += 1
    budget.settle(reserved, call.total_tokens)
    budget.add_requests(turns - 1)
