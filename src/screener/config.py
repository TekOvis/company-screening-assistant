"""Application settings.

All settings come from environment variables (or a local, git-ignored ``.env`` file).
Secrets such as the API key are never hard-coded and are stored as ``SecretStr`` so
they do not show up in logs or error messages by accident.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM -------------------------------------------------------------------------
    # "mock" (default): keyword rules, no model, free.
    # "claude_code": the real model through the local Claude Code login (subscription);
    #                for personal local use only, not for a public demo.
    # "anthropic": the real model through the Claude API with an API key (pay per token).
    llm_backend: Literal["mock", "claude_code", "anthropic"] = "mock"
    anthropic_api_key: SecretStr | None = None
    # One model for the agent and the evidence verifier (simpler to reproduce and cost).
    llm_model: str = "claude-sonnet-5-5"
    # Anthropic's guidance for extraction/classification work is to start at "low".
    llm_effort: Literal["low", "medium", "high"] = "low"
    llm_max_output_tokens: int = Field(default=8_000, ge=1_000, le=32_000)
    verifier_max_output_tokens: int = Field(default=4_000, ge=500, le=16_000)
    llm_request_timeout_s: float = Field(default=120.0, gt=0)
    # Retries of temporary API errors, done by the app (not the SDK) so each one is counted.
    llm_max_retries: int = Field(default=2, ge=0, le=5)

    # --- Agent limits, per company ---------------------------------------------------
    max_tool_calls: int = Field(default=3, ge=0, le=10)
    max_llm_calls: int = Field(default=4, ge=1, le=10)  # agent turns
    max_claims_to_verify: int = Field(default=25, ge=1, le=60)  # in one verifier request

    # --- claude_code backend (Claude Code CLI via the Agent SDK) ---------------------
    claude_code_cli_path: str | None = None  # default: the CLI bundled with the SDK
    # Agent turns inside Claude Code; the structured answer itself takes a turn.
    claude_code_max_turns: int = Field(default=6, ge=2, le=20)
    claude_code_timeout_s: float = Field(default=300.0, gt=0)  # one Claude Code run
    claude_code_concurrency: int = Field(default=1, ge=1, le=4)  # Claude Code processes at once

    # --- Budgets: every API attempt counts (agent turns, verifier, retries) -----------
    max_llm_requests_per_company: int = Field(default=8, ge=2, le=40)
    max_tokens_per_company: int = Field(default=80_000, ge=5_000)
    max_llm_requests_per_run: int = Field(default=60, ge=2)
    max_tokens_per_run: int = Field(default=600_000, ge=5_000)

    # --- Request limits --------------------------------------------------------------
    max_urls_per_request: int = Field(default=10, ge=1, le=10)
    concurrency: int = Field(default=3, ge=1, le=10)

    # --- In-memory job store (no database by design) ---------------------------------
    max_stored_runs: int = Field(default=50, ge=2, le=1000)
    run_ttl_s: int = Field(default=2 * 3600, ge=60)  # finished runs are dropped after this
    max_active_runs: int = Field(default=2, ge=1, le=20)

    # --- Public demo protection (see demo.py and docs/deployment.md) -----------------
    # PUBLIC_DEMO=true requires an access token and refuses the claude_code backend.
    public_demo: bool = False
    demo_access_token: SecretStr | None = None  # when set, every page needs it
    cookie_secure: bool = False  # set true behind HTTPS
    demo_examples: bool = False  # /examples: synthetic cases on saved pages (screener.examples)
    # Wall-clock limit for one screening. On expiry, unfinished companies are skipped,
    # finished results are kept and the run's slot is freed.
    run_timeout_s: float = Field(default=300, ge=0.1, le=3600)
    runs_per_ip_per_hour: int = Field(default=10, ge=1, le=1000)
    login_attempts_per_ip_per_15_min: int = Field(default=10, ge=1, le=100)
    daily_max_companies: int = Field(default=200, ge=1)
    daily_max_tokens: int = Field(default=2_000_000, ge=10_000)

    # --- Page fetching ---------------------------------------------------------------
    fetch_connect_timeout_s: float = Field(default=5.0, gt=0)
    fetch_read_timeout_s: float = Field(default=10.0, gt=0)
    fetch_total_timeout_s: float = Field(default=25.0, gt=0)  # one page, including retries
    fetch_max_retries: int = Field(default=2, ge=0, le=5)
    fetch_max_bytes: int = Field(default=2_000_000, ge=10_000)
    fetch_max_redirects: int = Field(default=4, ge=0, le=10)
    page_text_max_chars: int = Field(default=15_000, ge=1_000)
    max_links_per_page: int = Field(default=40, ge=0, le=200)

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.max_active_runs >= self.max_stored_runs:
            raise ValueError("MAX_ACTIVE_RUNS must be smaller than MAX_STORED_RUNS")
        if self.public_demo:
            token = self.demo_access_token.get_secret_value() if self.demo_access_token else ""
            if len(token.strip()) < 16:
                raise ValueError("PUBLIC_DEMO=true requires DEMO_ACCESS_TOKEN (16+ characters)")
            if self.llm_backend == "claude_code":
                raise ValueError(
                    "PUBLIC_DEMO=true cannot use LLM_BACKEND=claude_code: a personal Claude "
                    "subscription must not serve other people; use anthropic or mock"
                )
        return self

    @property
    def access_token(self) -> str | None:
        token = self.demo_access_token.get_secret_value() if self.demo_access_token else ""
        return token.strip() or None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
