"""Runtime configuration loaded without exposing secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr

from docqa.errors import ConfigurationError


_CHINA_STANDARD_TIME = timezone(timedelta(hours=8))


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Validated configuration shared by the CLI and injected services."""

    api_key: SecretStr | None
    base_url: str
    model: str
    reasoning_effort: Literal["high", "max"]
    reference_date: date
    connect_timeout_seconds: int = 10
    read_timeout_seconds: int = 120
    question_deadline_seconds: int = 600
    transport_attempts: int = 3
    semantic_request_limit: int = 2
    contract_max_output_tokens: int = 64_000
    analysis_max_output_tokens: int = 24_000
    repair_max_output_tokens: int = 24_000

    @classmethod
    def from_env(cls, *, require_key: bool, as_of: date | None) -> "AppConfig":
        """Build configuration from approved environment variables.

        ``as_of`` is already parsed by the CLI. Keeping it explicit avoids a
        hidden environment switch in tests and makes relative dates
        reproducible.
        """

        if as_of is not None and not isinstance(as_of, date):
            raise ConfigurationError("as_of must be an ISO calendar date")

        raw_key = os.environ.get("DEEPSEEK_API_KEY")
        if raw_key is not None and not raw_key.strip():
            raise ConfigurationError("DEEPSEEK_API_KEY cannot be blank")
        if require_key and raw_key is None:
            raise ConfigurationError("DEEPSEEK_API_KEY is required for online commands")

        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
        parsed_url = urlparse(base_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise ConfigurationError("DEEPSEEK_BASE_URL must be an absolute HTTPS URL")

        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip()
        if not model:
            raise ConfigurationError("DEEPSEEK_MODEL cannot be blank")

        reference_date = as_of or datetime.now(_CHINA_STANDARD_TIME).date()
        return cls(
            api_key=SecretStr(raw_key.strip()) if raw_key is not None else None,
            base_url=base_url.rstrip("/"),
            model=model,
            reasoning_effort="high",
            reference_date=reference_date,
        )
