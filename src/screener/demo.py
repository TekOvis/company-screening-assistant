"""Protection for a public demo: an access token, per-IP rate limits and a daily budget.

Strangers must not be able to spend the owner's API budget. The layers, from the outside in:
1. an access token (login form for people, a Bearer header for the JSON API);
2. a limit of screenings per IP address per hour (and of login attempts);
3. a daily budget of companies and tokens for the whole server, reserved before each run;
4. the per-run and per-company budgets in budget.py.
All counters live in memory: they reset when the process restarts.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

SESSION_COOKIE = "screener_session"
Clock = Callable[[], float]


def session_value(token: str) -> str:
    """Cookie value derived from the token: changing the token logs everyone out."""
    return hmac.new(token.encode(), b"screener-session-v1", hashlib.sha256).hexdigest()


def same_secret(given: str | None, expected: str) -> bool:
    """Constant-time comparison, so response timing does not reveal the token."""
    return given is not None and hmac.compare_digest(given.encode(), expected.encode())


class RateLimiter:
    """At most ``limit`` events per ``window_s`` seconds per key (sliding window)."""

    def __init__(self, limit: int, window_s: float, clock: Clock = time.monotonic) -> None:
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._events: dict[str, deque[float]] = {}

    def _recent(self, key: str) -> deque[float]:
        now = self._clock()
        events = self._events.setdefault(key, deque())
        while events and now - events[0] >= self._window_s:
            events.popleft()
        return events

    def allow(self, key: str) -> bool:
        """Record an event if it fits, and say whether it did."""
        events = self._recent(key)
        if len(events) >= self._limit:
            return False
        events.append(self._clock())
        return True

    def retry_after_s(self, key: str) -> int:
        events = self._recent(key)
        if len(events) < self._limit:
            return 0
        return max(1, int(self._window_s - (self._clock() - events[0])) + 1)


def _utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


@dataclass
class DailyBudget:
    """Companies and tokens the whole server may use per UTC day.

    A run reserves its token cap before it starts; afterwards the reservation is replaced
    by the tokens it really used. So even runs that start at the same time cannot spend
    more than what is left for the day.
    """

    max_companies: int
    max_tokens: int
    today: Callable[[], str] = _utc_day
    day: str = ""
    companies: int = 0
    used_tokens: int = 0
    reserved_tokens: int = 0

    def _roll(self) -> None:
        current = self.today()
        if current != self.day:
            self.day, self.companies, self.used_tokens = current, 0, 0
            # Reservations of runs still in progress carry over to the new day.

    def remaining_tokens(self) -> int:
        self._roll()
        return max(0, self.max_tokens - self.used_tokens - self.reserved_tokens)

    def reserve(self, companies: int, run_token_cap: int) -> tuple[int, str | None]:
        """Reserve a run. Returns (token cap for the run, refusal message or None)."""
        self._roll()
        if self.companies + companies > self.max_companies:
            left = max(0, self.max_companies - self.companies)
            return 0, f"The demo's daily limit is reached ({left} more companies allowed today)."
        cap = min(run_token_cap, self.remaining_tokens())
        if cap <= 0:
            return 0, "The demo's daily token budget is used up. Please try again tomorrow."
        self.companies += companies
        self.reserved_tokens += cap
        return cap, None

    def settle(self, reserved: int, used: int) -> None:
        self._roll()
        self.reserved_tokens = max(0, self.reserved_tokens - reserved)
        self.used_tokens += used
