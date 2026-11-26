"""Explicit API tariffs used to turn reported token counts into a cost estimate.

Prices are copied by hand from the provider's pricing page; check them before relying on
a figure. Only tokens the API reported for successful responses are counted: failed
requests report no usage, and a request that timed out on our side may still have been
billed — such tokens are unknown to the app.
"""

from __future__ import annotations

from dataclasses import dataclass

from .schemas import Usage

TARIFFS_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-11-25)"


@dataclass(frozen=True)
class Tariff:
    """US dollars per million tokens."""

    input: float
    output: float
    cache_write: float  # 5-minute cache writes
    cache_read: float


TARIFFS: dict[str, Tariff] = {
    "claude-sonnet-5-5": Tariff(input=2.00, output=10.00, cache_write=2.50, cache_read=0.10),
    "claude-opus-5-5": Tariff(input=4.00, output=20.00, cache_write=5.00, cache_read=0.20),
    # Haiku 5.5 rates for prompts up to 100K tokens (this app's prompts are far smaller).
    "claude-haiku-5-5": Tariff(input=0.10, output=0.50, cache_write=0.125, cache_read=0.01),
}


def tariff_for(model: str) -> Tariff | None:
    return TARIFFS.get(model)


def cost_usd(usage: Usage, tariff: Tariff) -> float:
    total = (
        usage.input_tokens * tariff.input
        + usage.output_tokens * tariff.output
        + usage.cache_creation_input_tokens * tariff.cache_write
        + usage.cache_read_input_tokens * tariff.cache_read
    )
    return total / 1_000_000
