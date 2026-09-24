"""Controlled live experiments with reproducible, non-secret reports."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, StrictFloat, StrictInt, field_validator

from docqa.config import AppConfig
from docqa.errors import InvalidModelReply
from docqa.llm_client import DeepSeekClient, ModelReply, ModelRequest, Usage
from docqa.models.common import StrictModel
from docqa.pipeline import ItemAttemptFailure, run_batch


class RequestClient(Protocol):
    def request(self, request: ModelRequest) -> ModelReply: ...


class ProbePayload(StrictModel):
    normalized: StrictInt = Field(ge=14, le=14)


class ProbeReport(StrictModel):
    status: str
    normalized: StrictInt
    model: str
    response_id: str | None
    usage: Usage
    elapsed_seconds: StrictFloat = Field(ge=0)


class ExperimentSpec(StrictModel):
    """Reproducible inputs for one bounded model experiment."""

    docs: tuple[Path, ...]
    questions: Path
    output: Path
    cache_dir: Path
    as_of: date
    model: str
    effort: Literal["high", "max"]
    run_id: str
    pinned_contract_key: str | None = None

    @field_validator("docs")
    @classmethod
    def docs_must_not_be_empty(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        if not value:
            raise ValueError("docs cannot be empty")
        return value

    @field_validator("model", "run_id")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("experiment text fields cannot be blank")
        return value

    @field_validator("pinned_contract_key")
    @classmethod
    def pinned_key_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("pinned_contract_key must be a lowercase SHA-256 digest")
        return value


class ExperimentResult(StrictModel):
    """Safe aggregate metadata for a completed experiment run."""

    run_id: str
    model: str
    effort: Literal["high", "max"]
    as_of: date
    processed: StrictInt = Field(ge=0)
    answered: StrictInt = Field(ge=0)
    handed_off: StrictInt = Field(ge=0)
    item_failures: StrictInt = Field(ge=0)
    attempt_failures: tuple[ItemAttemptFailure, ...]
    output_path: Path
    logical_requests: StrictInt = Field(ge=0)
    transport_attempts: StrictInt | None
    usage: Usage
    elapsed_seconds: StrictFloat = Field(ge=0)


def _sum_optional(values: list[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


class _RecordingClient:
    """Record logical requests and response usage without storing content."""

    def __init__(self, client: RequestClient) -> None:
        self._client = client
        self.logical_requests = 0
        self._usage: list[Usage] = []

    def request(self, request: ModelRequest) -> ModelReply:
        self.logical_requests += 1
        reply = self._client.request(request)
        self._usage.append(reply.usage)
        return reply

    @property
    def usage(self) -> Usage:
        return Usage(
            input_tokens=_sum_optional([item.input_tokens for item in self._usage]),
            output_tokens=_sum_optional([item.output_tokens for item in self._usage]),
            cached_tokens=_sum_optional([item.cached_tokens for item in self._usage]),
            reasoning_tokens=_sum_optional(
                [item.reasoning_tokens for item in self._usage]
            ),
        )


def probe(client: RequestClient) -> ProbeReport:
    """Run one bounded structured-output connectivity probe."""

    request = ModelRequest(
        purpose="analysis",
        instructions="只返回符合所给 JSON Schema 的 JSON，不要添加解释。",
        input_text="把１４转换为整数。",
        schema_name="deepseek_connectivity_probe",
        schema={
            "type": "object",
            "properties": {"normalized": {"type": "integer", "const": 14}},
            "required": ["normalized"],
            "additionalProperties": False,
        },
        max_output_tokens=2_000,
    )
    reply = client.request(request)
    try:
        payload = ProbePayload.model_validate(json.loads(reply.require_completed_text()))
    except (json.JSONDecodeError, ValueError, TypeError) as error:
        raise InvalidModelReply("probe response failed strict validation") from error
    return ProbeReport(
        status=reply.status,
        normalized=payload.normalized,
        model=reply.model,
        response_id=reply.response_id,
        usage=reply.usage,
        elapsed_seconds=reply.elapsed_seconds,
    )


def run_experiment(spec: ExperimentSpec) -> ExperimentResult:
    """Run one configured batch and return content-free aggregate telemetry."""

    base_config = AppConfig.from_env(require_key=True, as_of=spec.as_of)
    config = replace(
        base_config,
        model=spec.model,
        reasoning_effort=spec.effort,
    )
    client = _RecordingClient(DeepSeekClient(config))
    report = run_batch(
        list(spec.docs),
        spec.questions,
        spec.output,
        spec.cache_dir,
        config,
        client,
        pinned_contract_key=spec.pinned_contract_key,
    )
    return ExperimentResult(
        run_id=spec.run_id,
        model=spec.model,
        effort=spec.effort,
        as_of=spec.as_of,
        processed=report.processed,
        answered=report.answered,
        handed_off=report.handed_off,
        item_failures=report.item_failures,
        attempt_failures=report.attempt_failures,
        output_path=report.output_path,
        logical_requests=client.logical_requests,
        transport_attempts=None,
        usage=client.usage,
        elapsed_seconds=report.elapsed_seconds,
    )
