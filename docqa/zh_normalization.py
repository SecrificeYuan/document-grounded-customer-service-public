"""Conservative Chinese surface normalization and literal parsing."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol, Sequence

from docqa.models.common import NormalizedValue, QuantityValue, Span


_ARABIC_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_CHINESE_DATE = re.compile(
    r"^(?:(?P<year>[0-9零〇一二两三四五六七八九十百千]+)年)?"
    r"(?P<month>[0-9零〇一二两三四五六七八九十]+)月"
    r"(?P<day>[0-9零〇一二两三四五六七八九十]+)日$"
)
_WHITESPACE = re.compile(r"\s+")
_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_UNITS = {"十": 10, "百": 100, "千": 1000}


class UnitDefinitionLike(Protocol):
    """Structural subset supplied later by contract UnitDefinition."""

    unit_code: str
    aliases: list[str]
    dimension: str


def normalize_surface(text: str) -> str:
    """Normalize width and whitespace for search, never for final quotes."""

    if not isinstance(text, str):
        raise TypeError("text must be str")
    normalized = unicodedata.normalize("NFKC", text)
    return _WHITESPACE.sub(" ", normalized).strip()


def parse_number(token: str) -> Decimal | None:
    """Parse a complete Arabic or conservative Chinese numeric token."""

    if not isinstance(token, str):
        raise TypeError("token must be str")
    token = unicodedata.normalize("NFKC", token).strip()
    if not token:
        return None
    if _ARABIC_NUMBER.fullmatch(token):
        try:
            return Decimal(token)
        except InvalidOperation:
            return None
    if any(character not in _DIGITS and character not in _UNITS for character in token):
        return None

    if not any(character in _UNITS for character in token):
        digits = "".join(str(_DIGITS[character]) for character in token)
        return Decimal(int(digits))

    total = 0
    pending_digit: int | None = None
    last_unit = 10_000
    saw_unit = False
    for character in token:
        if character in _DIGITS:
            digit = _DIGITS[character]
            if digit == 0 and saw_unit and pending_digit is None:
                continue
            if pending_digit is not None:
                return None
            pending_digit = digit
            continue

        unit = _UNITS[character]
        if unit >= last_unit:
            return None
        if pending_digit is None:
            if unit == 10 and not saw_unit:
                pending_digit = 1
            else:
                return None
        total += pending_digit * unit
        pending_digit = None
        last_unit = unit
        saw_unit = True

    if pending_digit is not None:
        total += pending_digit
    return Decimal(total)


def _decimal_to_int(value: Decimal | None) -> int | None:
    if value is None or value != value.to_integral_value():
        return None
    return int(value)


def parse_date(
    token: str,
    as_of: date,
    explicit_year: int | None = None,
) -> date | None:
    """Parse explicit dates and three reproducible relative-day words."""

    if not isinstance(token, str):
        raise TypeError("token must be str")
    normalized = unicodedata.normalize("NFKC", token).strip()
    relative = {"今天": 0, "昨天": -1, "明天": 1}
    if normalized in relative:
        return as_of + timedelta(days=relative[normalized])

    iso_match = _ISO_DATE.fullmatch(normalized)
    if iso_match:
        try:
            return date(*(int(part) for part in iso_match.groups()))
        except ValueError:
            return None

    chinese_match = _CHINESE_DATE.fullmatch(normalized)
    if not chinese_match:
        return None
    year_token = chinese_match.group("year")
    year = _decimal_to_int(parse_number(year_token)) if year_token else explicit_year
    month = _decimal_to_int(parse_number(chinese_match.group("month")))
    day = _decimal_to_int(parse_number(chinese_match.group("day")))
    if year is None or month is None or day is None:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def span_matches(text: str, span: Span) -> bool:
    """Return whether a span points to exactly its declared source text."""

    return 0 <= span.start < span.end <= len(text) and text[span.start : span.end] == span.text


def extract_literals(
    text: str,
    units: Sequence[UnitDefinitionLike],
) -> list[NormalizedValue]:
    """Extract explicit number-unit pairs without performing conversions."""

    normalized = unicodedata.normalize("NFKC", text)
    alias_to_unit: dict[str, UnitDefinitionLike] = {}
    for unit in units:
        for raw_alias in unit.aliases:
            alias = unicodedata.normalize("NFKC", raw_alias)
            existing = alias_to_unit.get(alias)
            if existing is not None and existing.unit_code != unit.unit_code:
                raise ValueError(f"unit alias is ambiguous: {alias}")
            alias_to_unit[alias] = unit
    if not alias_to_unit:
        return []

    aliases = "|".join(
        re.escape(alias) for alias in sorted(alias_to_unit, key=len, reverse=True)
    )
    number = r"[+-]?(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千]+)"
    pattern = re.compile(rf"(?P<number>{number})\s*(?P<unit>{aliases})")
    found: list[NormalizedValue] = []
    for match in pattern.finditer(normalized):
        alias = match.group("unit")
        if alias == "日" and normalized[: match.start()].endswith("月"):
            continue
        if alias == "月":
            previous = normalized[match.start() - 1] if match.start() else ""
            following = normalized[match.end() : match.end() + 1]
            if previous == "年" or following in "0123456789零〇一二两三四五六七八九十日":
                continue
        value = parse_number(match.group("number"))
        if value is None:
            continue
        unit = alias_to_unit[alias]
        kind = unit.dimension if unit.dimension in {"duration", "money"} else "quantity"
        found.append(
            QuantityValue(
                kind=kind,
                value=format(value, "f"),
                unit_code=unit.unit_code,
            )
        )
    return found
