"""One-question structured analysis with a shared one-repair budget."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from collections.abc import Callable
from typing import Protocol

from pydantic import ValidationError

from docqa.config import AppConfig
from docqa.decision import decide, validate_analysis
from docqa.evidence import EvidenceIndex
from docqa.errors import AnalysisFailed, InvalidModelReply, TransportExhausted
from docqa.llm_client import ModelReply, ModelRequest
from docqa.models.analysis import AnalysisResult
from docqa.models.common import Issue
from docqa.models.contract import ContractBody
from docqa.models.documents import DocumentSet
from docqa.models.output import InputRecord, OutputRecord, handoff
from docqa.rule_engine import evaluate_rules
from docqa.safe_logging import (
    AnalysisAttemptFailure,
    analysis_failure_event,
    error_event,
)


_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_LOGGER = logging.getLogger(__name__)


class LLMClient(Protocol):
    def request(self, request: ModelRequest) -> ModelReply: ...


def _read_prompt(name: str) -> str:
    text = (_PROMPT_DIR / name).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"blank question prompt: {name}")
    return text


def _source_context(
    record: InputRecord,
    documents: DocumentSet,
    contract: ContractBody,
    config: AppConfig,
) -> dict[str, object]:
    blocks = sorted(
        (block.model_dump(mode="json") for block in documents.blocks),
        key=lambda item: (item["document_id"], item["page"], item["ordinal"], item["block_id"]),
    )
    return {
        "locale": contract.locale.model_dump(mode="json"),
        "as_of": config.reference_date.isoformat(),
        "contract": contract.model_dump(mode="json"),
        "evidence_blocks": blocks,
        "question": record.question,
    }


def _safe_parse(reply: ModelReply) -> tuple[AnalysisResult | None, list[Issue], str]:
    try:
        text = reply.require_completed_text()
    except InvalidModelReply as error:
        return None, [Issue(code="INVALID_MODEL_REPLY", message=str(error), object_ids=[])], reply.text
    try:
        return AnalysisResult.model_validate_json(text), [], text
    except ValidationError as error:
        locations = [".".join(str(part) for part in item["loc"]) or "<root>" for item in error.errors(include_url=False, include_input=False)]
        return None, [Issue(code="INVALID_ANALYSIS_SCHEMA", message="invalid fields: " + ", ".join(locations), object_ids=[])], text


def repair_analysis(
    *,
    client: LLMClient,
    context: dict[str, object],
    previous_text: str,
    issues: list[Issue],
    schema: dict[str, object],
    config: AppConfig,
) -> ModelReply:
    """Spend the sole semantic repair request; called only by analyze_question."""

    payload = {
        **context,
        "validation_errors": [issue.model_dump(mode="json") for issue in issues],
        "previous_final_json": previous_text,
    }
    return client.request(
        ModelRequest(
            purpose="repair",
            instructions=_read_prompt("semantic_repair.md"),
            input_text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            schema_name="question_analysis_result",
            schema=schema,
            max_output_tokens=config.repair_max_output_tokens,
        )
    )


def analyze_question(
    record: InputRecord,
    documents: DocumentSet,
    contract: ContractBody,
    client: LLMClient,
    config: AppConfig,
    on_technical_failure: Callable[[str], None] | None = None,
    on_attempt_failure: Callable[[AnalysisAttemptFailure], None] | None = None,
) -> OutputRecord:
    """Analyze one item and return exactly one answer or safe handoff."""

    schema = AnalysisResult.model_json_schema()
    context = _source_context(record, documents, contract, config)
    index = EvidenceIndex(documents.blocks)
    allowed_contract_object_ids = {
        *(document.document_id for document in contract.documents),
        *(topic.topic_id for topic in contract.scope),
        *(field.field_id for field in contract.fields),
        *(unit.unit_code for unit in contract.units),
        *(unit.unit_id for unit in contract.policy_units),
        *(unit.family_id for unit in contract.policy_units),
        *(
            claim.claim_id
            for unit in contract.policy_units
            for claim in unit.claims
        ),
        *(block.block_id for block in documents.blocks),
    }
    request = ModelRequest(
        purpose="analysis",
        instructions=_read_prompt("question_analyzer.md"),
        input_text=json.dumps(context, ensure_ascii=False, separators=(",", ":")),
        schema_name="question_analysis_result",
        schema=schema,
        max_output_tokens=config.analysis_max_output_tokens,
    )
    previous_text = ""
    issues: list[Issue] = []
    request_limit = max(1, min(2, config.semantic_request_limit))
    try:
        for attempt in range(request_limit):
            reply = client.request(request) if attempt == 0 else repair_analysis(
                client=client,
                context=context,
                previous_text=previous_text,
                issues=issues,
                schema=schema,
                config=config,
            )
            analysis, issues, previous_text = _safe_parse(reply)
            if analysis is None:
                event = analysis_failure_event(
                    reply,
                    issues=issues,
                    allowed_object_ids=allowed_contract_object_ids,
                    attempt=attempt + 1,
                )
                _LOGGER.warning(
                    "analysis_attempt_failure %s", event.model_dump(mode="json")
                )
                if on_attempt_failure is not None:
                    on_attempt_failure(event)
                continue
            report = validate_analysis(record, analysis, contract, index, config.reference_date)
            if not report.valid:
                issues = report.issues
                event = analysis_failure_event(
                    reply,
                    issues=issues,
                    allowed_object_ids=allowed_contract_object_ids,
                    attempt=attempt + 1,
                )
                _LOGGER.warning(
                    "analysis_attempt_failure %s", event.model_dump(mode="json")
                )
                if on_attempt_failure is not None:
                    on_attempt_failure(event)
                continue
            rules = evaluate_rules(contract, analysis, config.reference_date)
            result = decide(record, analysis, rules, contract, index)
            if result.output is not None:
                return result.output
            issues = result.issues
            event = analysis_failure_event(
                reply,
                issues=issues,
                allowed_object_ids=allowed_contract_object_ids,
                attempt=attempt + 1,
            )
            _LOGGER.warning(
                "analysis_attempt_failure %s", event.model_dump(mode="json")
            )
            if on_attempt_failure is not None:
                on_attempt_failure(event)
    except TransportExhausted as error:
        _LOGGER.warning("technical_failure %s", error_event(error, attempt=1))
        if on_technical_failure is not None:
            on_technical_failure("TransportExhausted")
        return handoff(record.id, "AMBIGUOUS")
    _LOGGER.warning(
        "technical_failure %s",
        error_event(AnalysisFailed("analysis validation failed"), attempt=request_limit),
    )
    if on_technical_failure is not None:
        on_technical_failure("AnalysisFailed")
    return handoff(record.id, "AMBIGUOUS")
