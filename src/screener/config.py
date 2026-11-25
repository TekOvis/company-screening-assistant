"""Application settings.

All settings come from environment variables (or a local, git-ignored ``.env`` file).
Secrets such as the API key are never hard-coded and are stored as ``SecretStr`` so
they do not show up in logs or error messages by accident.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM -------------------------------------------------------------------------
    # "mock" (default) never calls a paid API; "anthropic" uses the real Claude API.
    llm_backend: Literal["mock", "anthropic"] = "mock"
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-sonnet-5-5"
    # Anthropic's guidance for extraction/classification work is to start at "low".
    llm_effort: Literal["low", "medium", "high"] = "low"
    llm_max_output_tokens: int = Field(default=16_000, ge=1_000, le=64_000)
    llm_request_timeout_s: float = Field(default=120.0, gt=0)
    llm_sdk_max_retries: int = Field(default=2, ge=0, le=5)

    # --- Agent limits, per company ---------------------------------------------------
    max_tool_calls: int = Field(default=3, ge=0, le=10)
    max_llm_calls: int = Field(default=4, ge=1, le=10)
    # Checked between LLM calls; a single call can overshoot by at most its own size.
    max_tokens_per_company: int = Field(default=60_000, ge=5_000)

    # --- Request limits --------------------------------------------------------------
    max_urls_per_request: int = Field(default=10, ge=1, le=10)
    concurrency: int = Field(default=3, ge=1, le=10)

    # --- Page fetching ---------------------------------------------------------------
    fetch_connect_timeout_s: float = Field(default=5.0, gt=0)
    fetch_read_timeout_s: float = Field(default=10.0, gt=0)
    fetch_total_timeout_s: float = Field(default=20.0, gt=0)
    fetch_max_bytes: int = Field(default=2_000_000, ge=10_000)
    fetch_max_redirects: int = Field(default=4, ge=0, le=10)
    page_text_max_chars: int = Field(default=15_000, ge=1_000)
    max_links_per_page: int = Field(default=40, ge=0, le=200)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
