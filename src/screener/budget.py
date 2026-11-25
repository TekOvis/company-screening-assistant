"""Token and request budgets for LLM calls.

Every API attempt counts — agent turns, the evidence check and retries — at two levels:
per company and per run (all companies of one screening). Before a call we *reserve* its
worst case (an estimate of the input plus the max_tokens limit for the output) and refuse
the call if the reservation does not fit. After the call the reservation is replaced by
the usage the API reported. A failed attempt still counts as a request, but its tokens are
released, because the API reports no usage for failed requests.

Known limit: a request that times out on our side may still be completed and billed by
the API; its tokens are unknown to us.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from .config import Settings

CHARS_PER_TOKEN_ESTIMATE = 3  # pessimistic on purpose (English text is ~4 chars/token)
REQUEST_OVERHEAD_TOKENS = 500  # tool-use system prompt, message framing, schema grammar


class BudgetExceeded(Exception):
    """A limit would be exceeded, so the call was not made."""


@dataclass
class RunBudget:
    max_tokens: int
    max_requests: int
    used_tokens: int = 0
    reserved_tokens: int = 0
    requests: int = 0
    stop_reason: str | None = None  # set when the whole run must stop (spend limit)

    @classmethod
    def from_settings(cls, settings: Settings) -> RunBudget:
        return cls(
            max_tokens=settings.max_tokens_per_run, max_requests=settings.max_llm_requests_per_run
        )

    def stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason


@dataclass
class CompanyBudget:
    run: RunBudget
    max_tokens: int
    max_requests: int
    used_tokens: int = 0
    reserved_tokens: int = 0
    requests: int = 0

    @classmethod
    def from_settings(cls, run: RunBudget, settings: Settings) -> CompanyBudget:
        return cls(
            run=run,
            max_tokens=settings.max_tokens_per_company,
            max_requests=settings.max_llm_requests_per_company,
        )

    def reserve(self, tokens: int) -> None:
        """Count one API attempt and reserve its worst-case tokens, or raise."""
        run = self.run
        if run.stop_reason:
            raise BudgetExceeded(f"the run was stopped: {run.stop_reason}")
        if self.requests >= self.max_requests:
            raise BudgetExceeded(f"request limit per company reached ({self.max_requests})")
        if run.requests >= run.max_requests:
            raise BudgetExceeded(f"request limit per run reached ({run.max_requests})")
        if self.used_tokens + self.reserved_tokens + tokens > self.max_tokens:
            raise BudgetExceeded(f"token limit per company would be exceeded ({self.max_tokens})")
        if run.used_tokens + run.reserved_tokens + tokens > run.max_tokens:
            raise BudgetExceeded(f"token limit per run would be exceeded ({run.max_tokens})")
        self.requests += 1
        run.requests += 1
        self.reserved_tokens += tokens
        run.reserved_tokens += tokens

    def settle(self, reserved: int, actual: int) -> None:
        """Replace a reservation with the tokens the API actually reported."""
        self.reserved_tokens -= reserved
        self.run.reserved_tokens -= reserved
        self.used_tokens += actual
        self.run.used_tokens += actual


def estimate_request_tokens(params: dict[str, Any]) -> int:
    """Pessimistic size of one request: everything we send, plus the output limit."""
    chars = len(str(params.get("system", "")))
    chars += len(json.dumps(params.get("tools", []), ensure_ascii=False))
    chars += len(json.dumps(params.get("output_config", {}), ensure_ascii=False))
    for message in params.get("messages", []):
        content = message.get("content") if isinstance(message, dict) else None
        chars += _content_chars(content)
    input_estimate = math.ceil(chars / CHARS_PER_TOKEN_ESTIMATE) + REQUEST_OVERHEAD_TOKENS
    return input_estimate + int(params.get("max_tokens", 0))


def _content_chars(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    total = 0
    for block in content:
        if hasattr(block, "model_dump"):  # SDK objects echoed back from earlier turns
            block = block.model_dump(exclude_none=True)
        total += len(json.dumps(block, ensure_ascii=False, default=str))
    return total
