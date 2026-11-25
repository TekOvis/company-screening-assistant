"""The claude_code backend, tested with a fake query() (Claude Code is never started)."""

import asyncio
import json

import pytest

pytest.importorskip("claude_agent_sdk")  # the optional "claude-code" extra

from claude_agent_sdk import AssistantMessage, RateLimitEvent, RateLimitInfo  # noqa: E402

from screener import evaluate  # noqa: E402
from screener.agent import ASSESSMENT_SCHEMA, AgentOutcome  # noqa: E402
from screener.budget import CompanyBudget, RunBudget  # noqa: E402
from screener.claude_code import (  # noqa: E402
    ALLOWED_TOOL,
    VERIFIER_MAX_TURNS,
    ClaudeCodeClient,
    make_fetch_handler,
)
from screener.eval_dataset import load_cases  # noqa: E402
from screener.fixtures import FixtureFetcher, FixturePage  # noqa: E402
from screener.llm import BackendError  # noqa: E402
from screener.pipeline import screen_company  # noqa: E402
from screener.prompts import system_prompt  # noqa: E402
from screener.schemas import Usage  # noqa: E402
from screener.verify import VERIFIER_SCHEMA  # noqa: E402

from .claude_code_fakes import ASSESSMENT_01, HOME, make_query, result_message  # noqa: E402
from .conftest import make_settings  # noqa: E402

CASE_01 = next(c for c in load_cases() if c.id == "01-valves-fit-homepage")


def _client(query, **settings_overrides):
    settings = make_settings(llm_backend="claude_code", **settings_overrides)
    return ClaudeCodeClient(settings, query_fn=query, environ={}), settings


async def _agent(client, settings):
    fetcher = FixtureFetcher(CASE_01.fixture_pages(), settings)
    homepage = await fetcher.fetch(CASE_01.start_url)
    budget = CompanyBudget.from_settings(RunBudget.from_settings(settings), settings)
    usage = Usage()
    outcome = await client.run_agent(
        CASE_01.criteria, homepage, fetcher, budget=budget, usage=usage
    )
    return outcome, usage, budget


def test_refuses_to_start_when_an_api_key_would_take_over():
    settings = make_settings(llm_backend="claude_code")
    with pytest.raises(BackendError) as info:
        ClaudeCodeClient(settings, query_fn=make_query(), environ={"ANTHROPIC_API_KEY": "x"})
    assert info.value.kind == "setup"
    assert "ANTHROPIC_API_KEY" in info.value.message and "subscription" in info.value.message


@pytest.mark.anyio
async def test_claude_code_is_locked_down_to_our_tool():
    query = make_query(ASSESSMENT_01)
    client, settings = _client(query)
    await _agent(client, settings)
    options = query.calls[0].options
    assert options.tools == []  # no built-in tools: no shell, files or web
    assert options.allowed_tools == [ALLOWED_TOOL] == ["mcp__screener__fetch_company_page"]
    assert list(options.mcp_servers) == ["screener"]
    assert options.strict_mcp_config is True  # the user's own MCP servers are ignored
    assert options.setting_sources == []  # no user/project settings, hooks or CLAUDE.md
    assert options.permission_mode == "dontAsk"
    assert "no-session-persistence" in options.extra_args
    assert "bare" not in options.extra_args  # --bare would ignore the subscription login
    assert options.system_prompt == system_prompt(settings.max_tool_calls)
    assert options.output_format == {"type": "json_schema", "schema": ASSESSMENT_SCHEMA}
    assert options.model == settings.llm_model and options.effort == settings.llm_effort
    assert options.max_turns == settings.claude_code_max_turns
    assert str(options.cwd).endswith("screener-claude-code")


@pytest.mark.anyio
async def test_structured_answer_is_validated_and_usage_counted():
    client, settings = _client(make_query(ASSESSMENT_01, num_turns=3))
    outcome, usage, budget = await _agent(client, settings)
    assert outcome.status == "ok" and outcome.assessment.fit == "fit"
    assert (usage.llm_requests, usage.llm_calls) == (3, 3)  # every turn is a model request
    assert (usage.input_tokens, usage.output_tokens) == (1200, 300)
    assert budget.requests == 3 and budget.reserved_tokens == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("message", "status"),
    [
        (result_message(subtype="error_max_turns", is_error=True), "invalid_output"),
        (
            result_message(subtype="error_max_structured_output_retries", is_error=True),
            "invalid_output",
        ),
        (result_message(structured_output={**ASSESSMENT_01, "fit": "maybe"}), "invalid_output"),
        (result_message(subtype="error_during_execution", is_error=True), "llm_error"),
    ],
)
async def test_turn_limit_and_bad_answers_are_reported(message, status):
    client, settings = _client(make_query(agent_messages=[message]))
    outcome, _, _ = await _agent(client, settings)
    assert outcome.status == status and outcome.assessment is None


def _limit_event(rate_limit_type="five_hour", status="rejected"):
    info = RateLimitInfo(status=status, rate_limit_type=rate_limit_type, resets_at=1795600000)
    return RateLimitEvent(rate_limit_info=info, uuid="u1", session_id="s1")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "messages",
    [
        [_limit_event(), result_message(structured_output=ASSESSMENT_01)],
        [_limit_event("overage", "allowed"), result_message(structured_output=ASSESSMENT_01)],
        [
            AssistantMessage(content=[], model="claude-sonnet-5-5", error="rate_limit"),
            result_message(is_error=True, subtype="error_during_execution", api_error_status=429),
        ],
    ],
    ids=["limit-rejected", "paid-overage", "rate-limit-error"],
)
async def test_subscription_limit_stops_the_run_without_switching_to_the_api(messages):
    client, settings = _client(make_query(agent_messages=messages))
    outcome, usage, budget = await _agent(client, settings)
    assert outcome.status == "llm_error"
    assert "subscription usage limit" in outcome.error
    assert "nothing was sent to the paid API" in outcome.error
    assert budget.run.stop_reason == outcome.error  # every other company is skipped now
    assert usage.failed_requests == 1


def test_quota_message_names_the_window_and_reset_time():
    from screener.claude_code import quota_message

    text = quota_message(_limit_event().rate_limit_info)
    assert "five hour window" in text and "resets at 2026-11-25 09:46 UTC" in text


@pytest.mark.anyio
async def test_not_logged_in_gives_a_clear_error():
    messages = [
        AssistantMessage(content=[], model="claude-sonnet-5-5", error="authentication_failed"),
        result_message(is_error=True, subtype="error_during_execution", api_error_status=401),
    ]
    client, settings = _client(make_query(agent_messages=messages))
    outcome, _, _ = await _agent(client, settings)
    assert outcome.status == "llm_error" and "not logged in" in outcome.error


@pytest.mark.anyio
async def test_a_slow_run_is_cut_off_by_the_timeout():
    async def slow_query(*, prompt, options):
        await asyncio.sleep(5)
        yield result_message(structured_output=ASSESSMENT_01)

    client, settings = _client(slow_query, claude_code_timeout_s=0.05)
    outcome, _, _ = await _agent(client, settings)
    assert outcome.status == "llm_error" and "did not finish within" in outcome.error


@pytest.mark.anyio
async def test_the_tool_uses_our_validation_same_site_rule_and_limit():
    settings = make_settings(max_tool_calls=3)
    fetcher = FixtureFetcher(
        {
            HOME: CASE_01.pages[HOME].to_fixture().body,
            HOME + "about": "<p>About Brightwater.</p>",
            HOME + "contact": FixturePage(status_code=302, location="https://elsewhere.example/"),
            "https://elsewhere.example/": "<p>Lure</p>",
        },
        settings,
    )
    homepage = await fetcher.fetch(HOME)
    outcome = AgentOutcome(status="ok", pages={homepage.url: homepage})
    handler = make_fetch_handler(settings, HOME, fetcher, outcome)

    ok = await handler({"path": "/about"})
    assert ok["is_error"] is False and f'<page url="{HOME}about">' in ok["content"][0]["text"]
    rejected = await handler({"path": "//evil.example/"})
    assert rejected["is_error"] and "Path rejected" in rejected["content"][0]["text"]
    offsite = await handler({"path": "/contact"})
    assert offsite["is_error"] and "another site" in offsite["content"][0]["text"]
    over = await handler({"path": "/team"})
    assert over["is_error"] and "limit reached" in over["content"][0]["text"]
    assert outcome.tool_calls == 4 and list(outcome.pages) == [HOME, HOME + "about"]


@pytest.mark.anyio
async def test_pipeline_runs_agent_and_evidence_check_through_claude_code():
    query = make_query(ASSESSMENT_01)
    client, settings = _client(query)
    fetcher = FixtureFetcher(CASE_01.fixture_pages(), settings)
    result = await screen_company(
        0,
        CASE_01.start_url,
        CASE_01.criteria,
        settings=settings,
        client=client,
        fetcher=fetcher,
        run_budget=RunBudget.from_settings(settings),
    )
    assert result.status == "ok" and result.fit == "fit"
    assert result.company_locations == ["Dortmund", "Germany"]
    assert {e.check for e in result.evidence} == {"supported"}
    agent_call, check_call = query.calls
    assert check_call.options.mcp_servers == {} and check_call.options.tools == []
    assert check_call.options.output_format["schema"] == VERIFIER_SCHEMA
    assert check_call.options.max_turns == VERIFIER_MAX_TURNS
    assert result.usage.llm_requests == 3  # 2 agent turns + 1 evidence check


@pytest.mark.anyio
async def test_limit_during_the_evidence_check_stops_the_run():
    query = make_query(ASSESSMENT_01, verifier_messages=[_limit_event()])
    client, settings = _client(query)
    run_budget = RunBudget.from_settings(settings)
    fetcher = FixtureFetcher(CASE_01.fixture_pages(), settings)
    result = await screen_company(
        0,
        CASE_01.start_url,
        CASE_01.criteria,
        settings=settings,
        client=client,
        fetcher=fetcher,
        run_budget=run_budget,
    )
    assert result.status == "verification_error" and result.fit is None
    assert "subscription usage limit" in result.error
    assert run_budget.stop_reason is not None


def test_eval_claude_code_mode_refuses_when_an_api_key_is_set(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    assert evaluate.main(["--mode", "claude-code", "--only", CASE_01.id]) == 1
    assert "instead of your" in capsys.readouterr().out


def test_eval_claude_code_mode_needs_yes(monkeypatch, capsys):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    def refuse(settings):
        raise AssertionError("Claude Code must not start without --yes")

    monkeypatch.setattr(evaluate, "create_client", refuse)
    assert evaluate.main(["--mode", "claude-code", "--only", CASE_01.id]) == 2
    output = capsys.readouterr().out
    assert "subscription" in output and "--yes" in output


def test_eval_report_never_presents_the_estimate_as_a_charge(monkeypatch, tmp_path):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(
        evaluate,
        "create_client",
        lambda settings: ClaudeCodeClient(settings, query_fn=make_query(ASSESSMENT_01), environ={}),
    )
    args = ["--mode", "claude-code", "--only", CASE_01.id, "--yes", "--out", str(tmp_path)]
    assert evaluate.main(args) == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["mode"] == "claude-code" and report["backend"] == "claude_code"
    assert report["model"] == "claude-sonnet-5-5 via Claude Code (subscription)"
    assert report["cost"]["usd"] is None  # no per-token charge
    assert report["cost"]["api_equivalent_usd"] > 0
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "no per-token charge" in markdown and "not an actual charge" in markdown
