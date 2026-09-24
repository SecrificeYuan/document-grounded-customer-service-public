from __future__ import annotations

import json

from docqa.llm_client import ModelReply, Usage
from docqa.models.common import Issue
from docqa.safe_logging import analysis_failure_event


def test_failure_telemetry_keeps_only_allowlisted_contract_object_ids() -> None:
    reply = ModelReply(
        text="private model response",
        status="completed",
        response_id="private-response-id",
        model="deepseek-flash",
        usage=Usage(
            input_tokens=10,
            output_tokens=20,
            cached_tokens=3,
            reasoning_tokens=15,
        ),
        elapsed_seconds=0.2,
    )
    issues = [
        Issue(
            code="FACT_SOURCE_MISMATCH",
            message="private diagnostic",
            object_ids=["payment_date", "model-invented-id"],
        ),
        Issue(
            code="QUOTE_QUALIFIER_MISMATCH",
            message="private quote",
            object_ids=["C-018-1"],
        ),
    ]

    event = analysis_failure_event(
        reply,
        issues=issues,
        allowed_object_ids={"payment_date", "C-018-1"},
        attempt=2,
    )

    assert event.issue_codes == (
        "FACT_SOURCE_MISMATCH",
        "QUOTE_QUALIFIER_MISMATCH",
    )
    assert event.contract_object_ids == ("C-018-1", "payment_date")
    serialized = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
    for forbidden in (
        "model-invented-id",
        "private model response",
        "private-response-id",
        "private diagnostic",
        "private quote",
    ):
        assert forbidden not in serialized
