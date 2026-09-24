"""Official-compatible metrics with separate strict audit details."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from docqa.errors import InputBatchError
from docqa.models.output import OutputRecord, ReasonCode


class AcceptableEvidence(BaseModel):
    """One document/page position accepted by the official evaluator."""

    model_config = ConfigDict(extra="allow", strict=True, frozen=True)

    document: str
    page: StrictInt = Field(ge=1)
    section: str

    @field_validator("document", "section")
    @classmethod
    def require_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text fields cannot be blank")
        return value


class ReferenceRecord(BaseModel):
    """Reference fields used by the supplied public evaluator."""

    model_config = ConfigDict(extra="allow", strict=True, frozen=True)

    id: str
    expected_decision: Literal["answer", "handoff"]
    reason_code: ReasonCode | None
    reference_answer: str
    required_facts: list[str]
    acceptable_evidence: list[AcceptableEvidence]

    @field_validator("id", "reference_answer")
    @classmethod
    def require_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text fields cannot be blank")
        return value

    @field_validator("required_facts")
    @classmethod
    def facts_must_be_nonblank(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("required facts cannot be blank")
        return values

    @model_validator(mode="after")
    def validate_reason(self) -> "ReferenceRecord":
        if self.expected_decision == "answer" and self.reason_code is not None:
            raise ValueError("answer references require reason_code=null")
        if self.expected_decision == "handoff" and self.reason_code is None:
            raise ValueError("handoff references require a reason_code")
        return self


class ItemAudit(BaseModel):
    """Per-item compatibility results without changing official denominators."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str
    decision_correct: bool
    reason_correct: bool
    fact_hits: int
    fact_total: int
    evidence_position_correct: bool
    strict_evidence_positions_valid: bool


class Metrics(BaseModel):
    """Aggregate official metrics plus strict validation and item audit."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    total: int
    decision_correct: int
    reason_correct: int
    fact_hits: int
    fact_total: int
    evidence_position_correct: int
    strict_output_valid: bool
    audit: tuple[ItemAudit, ...]


RecordT = TypeVar("RecordT", bound=BaseModel)


def _read_indexed(
    path: Path,
    label: str,
    validate: Callable[[Any], RecordT],
) -> tuple[list[RecordT], dict[str, RecordT]]:
    ordered: list[RecordT] = []
    indexed: dict[str, RecordT] = {}
    record_label = label[:-1] if label.endswith("s") else label
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError as error:
        raise InputBatchError(f"invalid {label} UTF-8") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            record = validate(payload)
        except (json.JSONDecodeError, ValueError, TypeError) as error:
            raise InputBatchError(
                f"invalid {record_label} record at line {line_number}"
            ) from error
        record_id = str(record.id)
        if record_id in indexed:
            raise InputBatchError(f"duplicate {label} id: {record_id}")
        ordered.append(record)
        indexed[record_id] = record
    if not ordered:
        raise InputBatchError(f"{label} contains no records")
    return ordered, indexed


def evaluate(predictions: Path, reference: Path) -> Metrics:
    """Score one complete prediction batch against the supplied reference."""

    expected_rows, expected_by_id = _read_indexed(
        reference, "reference", ReferenceRecord.model_validate
    )
    _, actual_by_id = _read_indexed(
        predictions, "predictions", OutputRecord.model_validate
    )
    if actual_by_id.keys() != expected_by_id.keys():
        missing = sorted(expected_by_id.keys() - actual_by_id.keys())
        extra = sorted(actual_by_id.keys() - expected_by_id.keys())
        raise InputBatchError(
            "prediction ids do not match reference ids: "
            f"missing={missing!r} extra={extra!r}"
        )
    audit: list[ItemAudit] = []
    for expected in expected_rows:
        actual = actual_by_id[expected.id]
        answer_case = expected.expected_decision == "answer"
        allowed = {
            (item.document, item.page) for item in expected.acceptable_evidence
        }
        cited = {(item.document, item.page) for item in actual.evidence}
        strictly_allowed = {
            (item.document, item.page, item.section)
            for item in expected.acceptable_evidence
        }
        strictly_cited = {
            (item.document, item.page, item.section) for item in actual.evidence
        }
        facts = expected.required_facts
        audit.append(
            ItemAudit(
                id=expected.id,
                decision_correct=actual.decision == expected.expected_decision,
                reason_correct=(
                    actual.reason_code is None
                    if answer_case
                    else actual.reason_code == expected.reason_code
                ),
                fact_hits=sum(fact in actual.answer for fact in facts),
                fact_total=len(facts),
                evidence_position_correct=(
                    bool(allowed & cited)
                    if answer_case
                    else not cited or bool(allowed & cited)
                ),
                strict_evidence_positions_valid=(
                    bool(strictly_cited) and strictly_cited <= strictly_allowed
                    if answer_case
                    else not strictly_cited
                ),
            )
        )

    return Metrics(
        total=len(audit),
        decision_correct=sum(item.decision_correct for item in audit),
        reason_correct=sum(item.reason_correct for item in audit),
        fact_hits=sum(item.fact_hits for item in audit),
        fact_total=sum(item.fact_total for item in audit),
        evidence_position_correct=sum(
            item.evidence_position_correct for item in audit
        ),
        strict_output_valid=True,
        audit=tuple(audit),
    )
