"""Strict per-question semantic analysis returned by the model."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from docqa.models.common import NormalizedValue, Span, StrictModel
from docqa.models.output import Decision, ReasonCode


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text fields cannot be blank")
    return value


def _nonblank_ids(values: list[str]) -> list[str]:
    if any(not value.strip() for value in values):
        raise ValueError("ID lists cannot contain blank values")
    return values


class Intent(StrictModel):
    intent_id: str
    source: Span
    description: str
    scope: Literal["in_scope", "out_of_scope", "uncertain"]
    mode: Literal["rule_description", "case_application"]
    family_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)

    _required_text = field_validator("intent_id", "description")(_nonblank)
    _required_ids = field_validator("family_ids", "claim_ids")(_nonblank_ids)


class ExtractedFact(StrictModel):
    field_id: str
    value: NormalizedValue
    sources: list[Span] = Field(min_length=1)
    intent_ids: list[str] = Field(default_factory=list)

    _required_id = field_validator("field_id")(_nonblank)
    _required_ids = field_validator("intent_ids")(_nonblank_ids)


class UnresolvedExpression(StrictModel):
    source: Span
    category: Literal["time", "reference", "missing_subject", "other"]
    intent_ids: list[str] = Field(default_factory=list)
    field_ids: list[str] = Field(default_factory=list)

    _required_ids = field_validator("intent_ids", "field_ids")(_nonblank_ids)


class QuoteSelection(StrictModel):
    evidence_id: str
    fragment: str

    _required_text = field_validator("evidence_id", "fragment")(_nonblank)


class PlannedClaim(StrictModel):
    intent_id: str
    unit_id: str
    claim_id: str
    assertion_text: str
    evidence_ids: list[str] = Field(min_length=1)
    quotes: list[QuoteSelection] = Field(min_length=1)

    _required_text = field_validator("intent_id", "unit_id", "claim_id", "assertion_text")(_nonblank)
    _required_ids = field_validator("evidence_ids")(_nonblank_ids)

    @model_validator(mode="after")
    def every_evidence_has_a_quote(self) -> "PlannedClaim":
        quoted = {quote.evidence_id for quote in self.quotes}
        missing = [evidence_id for evidence_id in self.evidence_ids if evidence_id not in quoted]
        if missing:
            raise ValueError(f"planned evidence lacks a quote: {missing}")
        return self


class AnalysisResult(StrictModel):
    language: Literal["zh-CN"]
    intents: list[Intent] = Field(min_length=1)
    extracted_facts: list[ExtractedFact] = Field(default_factory=list)
    unresolved_expressions: list[UnresolvedExpression] = Field(default_factory=list)
    missing_field_ids: list[str] = Field(default_factory=list)
    selected_unit_ids: list[str] = Field(default_factory=list)
    claim_plan: list[PlannedClaim] = Field(default_factory=list)
    conflicting_unit_ids: list[str] = Field(default_factory=list)
    proposed_decision: Decision
    reason_code: ReasonCode | None
    answer_draft: str

    _required_answer = field_validator("answer_draft")(_nonblank)
    _required_ids = field_validator("missing_field_ids", "selected_unit_ids", "conflicting_unit_ids")(_nonblank_ids)

    @model_validator(mode="after")
    def validate_decision_reason(self) -> "AnalysisResult":
        if self.proposed_decision == "answer" and self.reason_code is not None:
            raise ValueError("answer analysis requires reason_code=null")
        if self.proposed_decision == "handoff" and self.reason_code is None:
            raise ValueError("handoff analysis requires a reason_code")
        return self
