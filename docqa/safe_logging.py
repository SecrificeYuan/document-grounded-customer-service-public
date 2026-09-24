"""Allowlisted diagnostics that never serialize prompts, keys, or SDK bodies."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from pydantic import Field, StrictInt

from docqa.llm_client import ModelReply, Usage
from docqa.models.common import Issue, StrictModel


_SAFE_ISSUE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SAFE_RESPONSE_STATUSES = {"completed", "incomplete", "failed", "cancelled"}


class AnalysisAttemptFailure(StrictModel):
    """Content-free diagnostics for one failed semantic response."""

    attempt: StrictInt = Field(ge=1)
    response_status: Literal["completed", "incomplete", "failed", "cancelled", "other"]
    issue_codes: tuple[str, ...] = Field(min_length=1)
    contract_object_ids: tuple[str, ...] = ()
    usage: Usage


def analysis_failure_event(
    reply: ModelReply,
    *,
    issues: Iterable[Issue],
    allowed_object_ids: Iterable[str],
    attempt: int,
) -> AnalysisAttemptFailure:
    """Reduce a failed response to allowlisted contract IDs and fixed metadata."""

    issue_list = list(issues)
    safe_codes = sorted(
        {
            code if _SAFE_ISSUE_CODE.fullmatch(code) else "UNSAFE_ISSUE_CODE"
            for code in (issue.code for issue in issue_list)
        }
    )
    allowed = set(allowed_object_ids)
    safe_object_ids = sorted(
        {
            object_id
            for issue in issue_list
            for object_id in issue.object_ids
            if object_id in allowed
        }
    )
    status = reply.status if reply.status in _SAFE_RESPONSE_STATUSES else "other"
    return AnalysisAttemptFailure(
        attempt=attempt,
        response_status=status,
        issue_codes=tuple(safe_codes),
        contract_object_ids=tuple(safe_object_ids),
        usage=reply.usage,
    )


def request_event(reply: ModelReply, *, attempt: int) -> dict[str, object]:
    return {
        "id": reply.response_id,
        "status": reply.status,
        "model": reply.model,
        "input_tokens": reply.usage.input_tokens,
        "output_tokens": reply.usage.output_tokens,
        "cached_tokens": reply.usage.cached_tokens,
        "reasoning_tokens": reply.usage.reasoning_tokens,
        "elapsed_seconds": reply.elapsed_seconds,
        "attempt": attempt,
    }


def error_event(error: Exception, *, attempt: int) -> dict[str, object]:
    status = getattr(error, "status_code", None)
    code = f"HTTP_{status}" if isinstance(status, int) else type(error).__name__
    return {"attempt": attempt, "error_code": code}
