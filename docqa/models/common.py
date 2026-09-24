"""Common strict values that preserve source meaning and locations."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    computed_field,
)


_DECIMAL_PATTERN = re.compile(r"^[+-]?(?:0|[1-9]\d*)(?:\.\d+)?$")


class StrictModel(BaseModel):
    """Shared validation policy for internal structured values."""

    model_config = ConfigDict(extra="forbid", strict=True)


class Span(StrictModel):
    """An exact half-open Unicode code-point range in source text."""

    start: StrictInt = Field(ge=0)
    end: StrictInt
    text: str = Field(min_length=1)

    @field_validator("end")
    @classmethod
    def end_must_follow_start(cls, end: int, info: object) -> int:
        start = getattr(info, "data", {}).get("start")
        if start is not None and end <= start:
            raise ValueError("span end must be greater than start")
        return end


class StringValue(StrictModel):
    kind: Literal["string"]
    value: str


class EnumValue(StrictModel):
    kind: Literal["enum"]
    value: str


class BoolValue(StrictModel):
    kind: Literal["boolean"]
    value: StrictBool


class IntValue(StrictModel):
    kind: Literal["integer"]
    value: StrictInt


class DecimalValue(StrictModel):
    kind: Literal["decimal"]
    value: str

    @field_validator("value")
    @classmethod
    def decimal_text_is_canonical(cls, value: str) -> str:
        if not _DECIMAL_PATTERN.fullmatch(value):
            raise ValueError("value must be a decimal string")
        return value


class DateValue(StrictModel):
    kind: Literal["date"]
    value: date


class DateTimeValue(StrictModel):
    kind: Literal["datetime"]
    value: datetime

    @field_validator("value")
    @classmethod
    def datetime_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime values require timezone information")
        return value


class QuantityValue(StrictModel):
    kind: Literal["duration", "money", "quantity"]
    value: str
    unit_code: str = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def quantity_text_is_decimal(cls, value: str) -> str:
        if not _DECIMAL_PATTERN.fullmatch(value):
            raise ValueError("value must be a decimal string")
        return value


NormalizedValue = Annotated[
    StringValue
    | EnumValue
    | BoolValue
    | IntValue
    | DecimalValue
    | DateValue
    | DateTimeValue
    | QuantityValue,
    Field(discriminator="kind"),
]


class Issue(StrictModel):
    """One deterministic validation problem with affected object IDs."""

    code: str
    message: str
    object_ids: list[str] = Field(default_factory=list)

    @field_validator("code", "message")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("issue text cannot be blank")
        return value


class ValidationReport(StrictModel):
    """Validation result whose truth is derived rather than model supplied."""

    issues: list[Issue] = Field(default_factory=list)

    @computed_field
    @property
    def valid(self) -> bool:
        return not self.issues
