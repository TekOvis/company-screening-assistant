"""Stand-ins for the Agent SDK's query() so the claude_code backend is tested without
starting Claude Code (no subscription quota is used). Messages are real SDK classes."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from claude_agent_sdk import ResultMessage

HOME = "https://brightwater-valves.example/"
DESCRIPTION_QUOTE = (
    "Brightwater Valve Works designs and manufactures butterfly and ball valves for chemical "
    "plants and refineries."
)

# A correct answer for eval case 01 (quotes are verbatim from the saved page).
ASSESSMENT_01: dict[str, Any] = {
    "company_name": "Brightwater Valve Works",
    "description": "Designs and manufactures butterfly and ball valves for chemical plants.",
    "industry": "Industrial valves",
    "company_locations": ["Dortmund", "Germany"],
    "markets_served": [],
    "industry_match": "met",
    "geography_match": "met",
    "business_match": "met",
    "exclusion_triggered": False,
    "fit": "fit",
    "rationale": "Valve manufacturer with headquarters and production in Dortmund, Germany.",
    "evidence": [
        {
            "supports": "company_location",
            "claim": "The company is headquartered in Dortmund, Germany.",
            "url": HOME,
            "quote": "Our headquarters and production site are in Dortmund, Germany.",
        },
        {
            "supports": "description",
            "claim": "Makes valves.",
            "url": HOME,
            "quote": DESCRIPTION_QUOTE,
        },
        {"supports": "industry", "claim": "Valve maker.", "url": HOME, "quote": DESCRIPTION_QUOTE},
        {
            "supports": "company_name",
            "claim": "The company is Brightwater Valve Works.",
            "url": HOME,
            "quote": "Brightwater Valve Works designs and manufactures",
        },
    ],
}


def result_message(
    *,
    structured_output: Any = None,
    subtype: str = "success",
    is_error: bool = False,
    num_turns: int = 1,
    api_error_status: int | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=10,
        duration_api_ms=8,
        is_error=is_error,
        num_turns=num_turns,
        session_id="test-session",
        structured_output=structured_output,
        usage={"input_tokens": 1200, "output_tokens": 300},
        api_error_status=api_error_status,
    )


def claims_in(prompt: str) -> list[dict[str, Any]]:
    """The items of a verifier prompt (<claims_to_check> JSON)."""
    return json.loads(prompt.split("\n", 1)[1].rsplit("\n</claims_to_check>", 1)[0])


def make_query(
    agent_payload: Any = None,
    *,
    agent_messages: list[Any] | None = None,
    verifier_messages: list[Any] | None = None,
    num_turns: int = 2,
):
    """A fake query(): agent runs (with our MCP server) and evidence checks (without)."""
    calls: list[SimpleNamespace] = []

    async def query(*, prompt: str, options: Any):
        calls.append(SimpleNamespace(prompt=prompt, options=options))
        if options.mcp_servers:  # the agent run
            if agent_messages is not None:
                for message in agent_messages:
                    yield message
                return
            yield result_message(structured_output=agent_payload, num_turns=num_turns)
        else:  # the evidence check
            if verifier_messages is not None:
                for message in verifier_messages:
                    yield message
                return
            verdicts = [
                {"id": item["id"], "verdict": "supported", "reason": "fake check"}
                for item in claims_in(prompt)
            ]
            yield result_message(structured_output={"verdicts": verdicts})

    query.calls = calls  # type: ignore[attr-defined]
    return query
