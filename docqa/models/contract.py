"""Strict document-contract models shared by building and evaluation."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, field_validator, model_validator

from docqa.models.common import NormalizedValue, StrictModel


ValueKind = Literal["string", "enum", "boolean", "integer", "decimal", "date", "datetime", "duration", "money", "quantity"]
PredicateOperator = Literal["exists", "not_exists", "eq", "neq", "in", "not_in", "before", "on_or_after", "between"]


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text fields cannot be blank")
    return value


def _nonblank_ids(values: list[str]) -> list[str]:
    if any(not value.strip() for value in values):
        raise ValueError("ID lists cannot contain blank values")
    return values


class LocaleProfile(StrictModel):
    language: Literal["zh-CN"]
    timezone: Literal["Asia/Shanghai"]
    output_encoding: Literal["utf-8"]
    normalization_version: str

    _required_text = field_validator("normalization_version")(_nonblank)


class EnumOption(StrictModel):
    code: str
    display_name: str
    aliases: list[str] = Field(default_factory=list)

    _required_text = field_validator("code", "display_name")(_nonblank)


class FieldDefinition(StrictModel):
    field_id: str
    display_name: str
    description: str
    aliases: list[str] = Field(default_factory=list)
    value_kind: ValueKind
    allowed_values: list[EnumOption] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    _required_text = field_validator("field_id", "display_name", "description")(_nonblank)
    _required_ids = field_validator("evidence_ids")(_nonblank_ids)


class UnitDefinition(StrictModel):
    unit_code: str
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    dimension: str
    evidence_ids: list[str] = Field(default_factory=list)

    _required_text = field_validator("unit_code", "display_name", "dimension")(_nonblank)
    _required_ids = field_validator("evidence_ids")(_nonblank_ids)


class Predicate(StrictModel):
    field_id: str
    operator: PredicateOperator
    values: list[NormalizedValue] = Field(default_factory=list)

    _required_id = field_validator("field_id")(_nonblank)

    @model_validator(mode="after")
    def validate_operator_values(self) -> "Predicate":
        size = len(self.values)
        if self.operator in {"exists", "not_exists"} and size != 0:
            raise ValueError(f"{self.operator} requires no values")
        if self.operator in {"eq", "neq", "before", "on_or_after"} and size != 1:
            raise ValueError(f"{self.operator} requires exactly one value")
        if self.operator in {"in", "not_in"} and size < 1:
            raise ValueError(f"{self.operator} requires at least one value")
        if self.operator == "between" and size != 2:
            raise ValueError("between requires exactly two values")
        if size > 1 and len({value.kind for value in self.values}) != 1:
            raise ValueError("predicate values must have the same kind")
        if self.operator in {"before", "on_or_after", "between"} and any(
            value.kind not in {"date", "datetime"} for value in self.values
        ):
            raise ValueError("temporal operators require date or datetime values")
        return self


class ConditionGroup(StrictModel):
    all: list[Predicate] = Field(min_length=1)


class Topic(StrictModel):
    topic_id: str
    description: str
    aliases: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    disposition: Literal["covered", "in_scope_uncovered", "out_of_scope"]

    _required_text = field_validator("topic_id", "description")(_nonblank)
    _required_ids = field_validator("evidence_ids")(_nonblank_ids)


class ContractDocument(StrictModel):
    document_id: str
    title: str
    version_label: str | None
    published_on: date | None
    effective_from: date | None
    effective_to: date | None
    supersedes_document_ids: list[str] = Field(default_factory=list)

    _required_text = field_validator("document_id", "title")(_nonblank)
    _required_ids = field_validator("supersedes_document_ids")(_nonblank_ids)


class TimeBasis(StrictModel):
    kind: Literal["none", "reference_date", "field"]
    field_id: str | None

    @model_validator(mode="after")
    def validate_field_reference(self) -> "TimeBasis":
        if self.kind == "field":
            if self.field_id is None or not self.field_id.strip():
                raise ValueError("field time basis requires a nonblank field_id")
        elif self.field_id is not None:
            raise ValueError("only field time basis may set field_id")
        return self


class Claim(StrictModel):
    claim_id: str
    canonical_text: str
    normalized_literals: list[NormalizedValue] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)
    qualifiers: list[str] = Field(default_factory=list)
    required_companion_claim_ids: list[str] = Field(default_factory=list)

    _required_text = field_validator("claim_id", "canonical_text")(_nonblank)
    _required_ids = field_validator("evidence_ids", "required_companion_claim_ids")(_nonblank_ids)


class PolicyUnit(StrictModel):
    unit_id: str
    family_id: str
    topic_ids: list[str] = Field(default_factory=list)
    kind: Literal["fact", "policy", "procedure", "exception", "contact"]
    applicability_any: list[ConditionGroup] = Field(default_factory=list)
    required_field_ids: list[str] = Field(default_factory=list)
    time_basis: TimeBasis
    effective_from: date | None
    effective_to: date | None
    claims: list[Claim] = Field(min_length=1)
    overrides_unit_ids: list[str] = Field(default_factory=list)

    _required_text = field_validator("unit_id", "family_id")(_nonblank)
    _required_ids = field_validator("topic_ids", "required_field_ids", "overrides_unit_ids")(_nonblank_ids)


class ContractWarning(StrictModel):
    code: str
    severity: Literal["warning", "blocking"]
    message: str
    evidence_ids: list[str] = Field(default_factory=list)

    _required_text = field_validator("code", "message")(_nonblank)
    _required_ids = field_validator("evidence_ids")(_nonblank_ids)


class ContractBody(StrictModel):
    locale: LocaleProfile
    documents: list[ContractDocument] = Field(default_factory=list)
    scope: list[Topic] = Field(default_factory=list)
    fields: list[FieldDefinition] = Field(default_factory=list)
    units: list[UnitDefinition] = Field(default_factory=list)
    policy_units: list[PolicyUnit] = Field(default_factory=list)
    warnings: list[ContractWarning] = Field(default_factory=list)
