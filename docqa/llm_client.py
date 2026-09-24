"""Bounded OpenAI-compatible client for DeepSeek Responses API calls."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import openai
from pydantic import Field, StrictFloat, StrictInt, field_validator

from docqa.config import AppConfig
from docqa.errors import ConfigurationError, InvalidModelReply, TransportExhausted
from docqa.models.common import StrictModel


_SCHEMA_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class Usage(StrictModel):
    input_tokens: StrictInt | None
    output_tokens: StrictInt | None
    cached_tokens: StrictInt | None
    reasoning_tokens: StrictInt | None


class ModelRequest(StrictModel):
    purpose: Literal["contract", "analysis", "repair"]
    instructions: str
    input_text: str
    schema_name: str
    schema_payload: dict[str, object] = Field(alias="schema")
    max_output_tokens: StrictInt = Field(gt=0)

    @property
    def schema(self) -> dict[str, object]:
        return self.schema_payload

    @field_validator("instructions", "input_text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request text cannot be blank")
        return value

    @field_validator("schema_name")
    @classmethod
    def schema_name_is_safe(cls, value: str) -> str:
        if not _SCHEMA_NAME.fullmatch(value):
            raise ValueError("schema_name must match ^[A-Za-z0-9_-]{1,64}$")
        return value


class ModelReply(StrictModel):
    text: str
    status: str
    response_id: str | None
    model: str
    usage: Usage
    elapsed_seconds: StrictFloat = Field(ge=0)

    def require_completed_text(self) -> str:
        if self.status != "completed":
            raise InvalidModelReply(f"model response status is {self.status!r}")
        if not self.text.strip():
            raise InvalidModelReply("completed model response has empty text")
        return self.text


def model_request_payload(config: AppConfig, request: ModelRequest) -> dict[str, Any]:
    """Return the exact JSON-compatible payload passed to the SDK."""

    return {
        "model": config.model,
        "instructions": request.instructions,
        "input": request.input_text,
        "reasoning": {"effort": config.reasoning_effort},
        "text": {
            "format": {
                "type": "json_schema",
                "name": request.schema_name,
                "schema": request.schema,
            }
        },
        "max_output_tokens": request.max_output_tokens,
        "stream": False,
    }


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage(raw: object) -> Usage:
    if raw is None:
        return Usage(input_tokens=None, output_tokens=None, cached_tokens=None, reasoning_tokens=None)
    input_details = getattr(raw, "input_tokens_details", None)
    output_details = getattr(raw, "output_tokens_details", None)
    return Usage(
        input_tokens=_optional_int(getattr(raw, "input_tokens", None)),
        output_tokens=_optional_int(getattr(raw, "output_tokens", None)),
        cached_tokens=_optional_int(getattr(input_details, "cached_tokens", None)),
        reasoning_tokens=_optional_int(getattr(output_details, "reasoning_tokens", None)),
    )


def _status_code(error: Exception) -> int | None:
    value = getattr(error, "status_code", None)
    return value if isinstance(value, int) else None


def _retry_after(error: Exception, now: Callable[[], datetime]) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {})
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(str(raw))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - now()).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class DeepSeekClient:
    def __init__(
        self,
        config: AppConfig,
        *,
        sdk: object | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = config
        self._sleep = sleep
        self._monotonic = monotonic
        self._now = now
        if sdk is None:
            if config.api_key is None:
                raise ConfigurationError("API key is required to create DeepSeekClient")
            timeout = openai.Timeout(
                connect=config.connect_timeout_seconds,
                read=config.read_timeout_seconds,
                write=config.read_timeout_seconds,
                pool=config.connect_timeout_seconds,
            )
            sdk = openai.OpenAI(
                api_key=config.api_key.get_secret_value(),
                base_url=config.base_url,
                max_retries=0,
                timeout=timeout,
            )
        self._sdk = sdk

    def _create_response(self, request: ModelRequest) -> object:
        return self._sdk.responses.create(**model_request_payload(self.config, request))

    def request(self, request: ModelRequest) -> ModelReply:
        started = self._monotonic()
        deadline = started + self.config.question_deadline_seconds
        for attempt in range(self.config.transport_attempts):
            try:
                response = self._create_response(request)
                return ModelReply(
                    text=str(getattr(response, "output_text", "") or ""),
                    status=str(getattr(response, "status", "unknown") or "unknown"),
                    response_id=getattr(response, "id", None),
                    model=str(getattr(response, "model", self.config.model) or self.config.model),
                    usage=_usage(getattr(response, "usage", None)),
                    elapsed_seconds=float(max(0.0, self._monotonic() - started)),
                )
            except Exception as error:
                status = _status_code(error)
                is_connection = isinstance(error, (openai.APIConnectionError, openai.APITimeoutError, TimeoutError))
                retryable = is_connection or status in {408, 429} or (status is not None and 500 <= status <= 599)
                if status in {400, 401, 403}:
                    raise ConfigurationError(f"model API rejected request with HTTP {status}") from None
                if not retryable:
                    raise
                if attempt + 1 >= self.config.transport_attempts:
                    raise TransportExhausted("model transport attempts exhausted") from None
                delay = _retry_after(error, self._now)
                if delay is None:
                    delay = float(2**attempt)
                if self._monotonic() + delay > deadline:
                    raise TransportExhausted("retry delay would exceed question deadline") from None
                self._sleep(delay)
        raise TransportExhausted("model transport attempts exhausted")
