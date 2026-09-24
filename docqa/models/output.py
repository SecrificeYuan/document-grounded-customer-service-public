"""Official input and five-field output records."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


Decision = Literal["answer", "handoff"]
ReasonCode = Literal["OUT_OF_SCOPE", "INSUFFICIENT_EVIDENCE", "AMBIGUOUS"]


class StrictModel(BaseModel):
    """Base model that rejects coercion and undeclared fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


def _require_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text fields cannot be blank")
    return value


class InputRecord(StrictModel):
    """One valid question from the external JSONL input."""

    id: str
    session_id: str
    question: str

    _nonblank = field_validator("id", "session_id", "question")(_require_nonblank)


class OutputEvidence(StrictModel):
    """One exact, human-checkable citation in an official answer."""

    document: str
    page: StrictInt = Field(ge=1)
    section: str
    quote: str

    _nonblank = field_validator("document", "section", "quote")(_require_nonblank)


class OutputRecord(StrictModel):
    """Official output record with cross-field decision invariants."""

    id: str
    decision: Decision
    answer: str
    reason_code: ReasonCode | None
    evidence: list[OutputEvidence] = Field(default_factory=list)

    _nonblank = field_validator("id", "answer")(_require_nonblank)

    @model_validator(mode="after")
    def validate_decision_fields(self) -> "OutputRecord":
        if self.decision == "answer":
            if self.reason_code is not None:
                raise ValueError("answer records require reason_code=null")
            if not self.evidence:
                raise ValueError("answer records require non-empty evidence")
        else:
            if self.reason_code is None:
                raise ValueError("handoff records require a reason_code")
            if self.evidence:
                raise ValueError("handoff records require evidence=[]")
        return self


def handoff(id: str, reason: str) -> OutputRecord:
    """Create the only valid shape for a transferred question."""

    return OutputRecord(
        id=id,
        decision="handoff",
        answer="已转人工处理。",
        reason_code=reason,
        evidence=[],
    )
