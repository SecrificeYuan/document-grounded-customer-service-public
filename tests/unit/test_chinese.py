from __future__ import annotations

from datetime import date, datetime, timezone
from dataclasses import dataclass
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from docqa.models.common import (
    BoolValue,
    DateTimeValue,
    IntValue,
    NormalizedValue,
    Span,
)
from docqa.zh_normalization import (
    normalize_surface,
    extract_literals,
    parse_date,
    parse_number,
    span_matches,
)


@dataclass(frozen=True)
class UnitSpec:
    unit_code: str
    aliases: list[str]
    dimension: str


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("十四", Decimal(14)),
        ("两", Decimal(2)),
        ("１２.５", Decimal("12.5")),
        ("二〇二六", Decimal(2026)),
        ("一百零二", Decimal(102)),
        ("两千零六", Decimal(2006)),
        ("-3.50", Decimal("-3.50")),
    ],
)
def test_chinese_numbers_are_parsed_as_complete_tokens(
    source: str, expected: Decimal
) -> None:
    assert parse_number(source) == expected


@pytest.mark.parametrize(
    "source",
    ["", "十十", "一二三天", "统一", "一旦", "周一", "12.3.4", "三百百"],
)
def test_invalid_or_embedded_numbers_are_not_rewritten(source: str) -> None:
    assert parse_number(source) is None


def test_normalization_does_not_damage_chinese_words() -> None:
    assert normalize_surface("统一标准，周一开放，一旦激活") == "统一标准,周一开放,一旦激活"


def test_normalization_only_standardizes_width_and_search_whitespace() -> None:
    assert normalize_surface("  版本Ａ\t１２．５  GB \r\n") == "版本A 12.5 GB"


def test_dates_are_reproducible_and_conservative() -> None:
    reference = date(2026, 9, 20)
    assert parse_date("今天", reference) == reference
    assert parse_date("昨天", reference) == date(2026, 9, 19)
    assert parse_date("明天", reference) == date(2026, 9, 21)
    assert parse_date("2026-09-20", reference) == reference
    assert parse_date("二〇二六年九月二十日", reference) == reference
    assert parse_date("9月20日", reference) is None
    assert parse_date("9月20日", reference, explicit_year=2026) == reference
    assert parse_date("2026年2月30日", reference) is None
    assert parse_date("最近", reference) is None


def test_invalid_explicit_year_does_not_make_a_date() -> None:
    assert parse_date("2月28日", date(2026, 1, 1), explicit_year=0) is None
    assert parse_date("2026-09-20之后", date(2026, 1, 1)) is None


def test_span_matches_only_the_exact_unicode_position() -> None:
    text = "昨天😀，昨天\r\n１２V"
    second = text.index("昨天", 1)

    assert span_matches(text, Span(start=second, end=second + 2, text="昨天"))
    assert span_matches(text, Span(start=0, end=2, text="昨天"))
    assert span_matches(text, Span(start=text.index("😀"), end=text.index("😀") + 1, text="😀"))
    assert span_matches(text, Span(start=text.index("１２V"), end=len(text), text="１２V"))
    assert not span_matches(text, Span(start=second, end=second + 2, text="明天"))


@pytest.mark.parametrize(
    "payload",
    [
        {"start": -1, "end": 1, "text": "昨"},
        {"start": 1, "end": 1, "text": "昨"},
        {"start": 0, "end": 1, "text": ""},
    ],
)
def test_invalid_spans_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Span.model_validate(payload)


def test_normalized_value_union_is_strict_and_discriminated() -> None:
    adapter = TypeAdapter(NormalizedValue)

    assert adapter.validate_python({"kind": "integer", "value": 3}) == IntValue(
        kind="integer", value=3
    )
    assert adapter.validate_python({"kind": "boolean", "value": False}) == BoolValue(
        kind="boolean", value=False
    )
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "integer", "value": True})
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "boolean", "value": "false"})


def test_datetime_values_require_timezone_information() -> None:
    with pytest.raises(ValidationError):
        DateTimeValue(kind="datetime", value=datetime(2026, 9, 20, 12, 0))

    aware = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    assert DateTimeValue(kind="datetime", value=aware).value == aware


def test_literal_extraction_keeps_units_distinct_without_conversion() -> None:
    units = [
        UnitSpec("natural_day", ["个自然日", "自然日"], "duration"),
        UnitSpec("work_day", ["个工作日", "工作日"], "duration"),
        UnitSpec("month", ["个月", "月"], "duration"),
        UnitSpec("gigabyte", ["GB"], "storage"),
        UnitSpec("volt", ["V"], "voltage"),
        UnitSpec("ampere", ["A"], "current"),
    ]

    values = extract_literals(
        "十四个自然日、2个工作日、200GB、5V/3A、6个月。", units
    )

    assert [(value.kind, value.value, value.unit_code) for value in values] == [
        ("duration", "14", "natural_day"),
        ("duration", "2", "work_day"),
        ("quantity", "200", "gigabyte"),
        ("quantity", "5", "volt"),
        ("quantity", "3", "ampere"),
        ("duration", "6", "month"),
    ]


def test_calendar_month_is_not_misread_as_a_duration_literal() -> None:
    units = [UnitSpec("month", ["月"], "duration")]

    assert extract_literals("2026年9月20日生效", units) == []


def test_relative_ordinal_day_is_extracted_without_misreading_calendar_date() -> None:
    units = [UnitSpec("day", ["日"], "duration")]

    values = extract_literals("官方发货日期后第7日作为保修起算日。", units)

    assert [(value.kind, value.value, value.unit_code) for value in values] == [
        ("duration", "7", "day")
    ]
    assert extract_literals("2026年9月20日生效", units) == []
