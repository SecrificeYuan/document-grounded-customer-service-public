"""Order-preserving JSONL input parsing and validated output writing."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel, ConfigDict

from docqa.atomic_io import atomic_write
from docqa.errors import InputBatchError
from docqa.models.output import InputRecord, OutputRecord


class InputLine(BaseModel):
    """A nonblank physical input line and its eventual output identity."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    line_number: int
    output_id: str
    record: InputRecord | None
    issue_code: str | None


class InputBatch(BaseModel):
    """All retained input lines in original physical order."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    lines: tuple[InputLine, ...]


def _recognizable_id(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def read_input(path: Path) -> InputBatch:
    """Parse JSONL without losing invalid rows, IDs, or physical positions."""

    if not isinstance(path, Path):
        raise TypeError("path must be a pathlib.Path")

    retained: list[InputLine] = []
    seen_ids: set[str] = set()
    text = path.read_text(encoding="utf-8-sig")

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue

        output_id = f"__line_{line_number}__"
        record: InputRecord | None = None
        issue_code: str | None = None
        try:
            payload = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            issue_code = "INVALID_JSON"
        else:
            if isinstance(payload, dict):
                output_id = _recognizable_id(payload.get("id")) or output_id
            try:
                record = InputRecord.model_validate(payload)
            except ValueError:
                issue_code = "INVALID_RECORD"

        if output_id in seen_ids:
            raise InputBatchError(f"duplicate output id: {output_id}")
        seen_ids.add(output_id)
        retained.append(
            InputLine(
                line_number=line_number,
                output_id=output_id,
                record=record,
                issue_code=issue_code,
            )
        )

    if not retained:
        raise InputBatchError("input contains no question records")
    return InputBatch(lines=tuple(retained))


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def write_predictions(
    path: Path,
    records: Sequence[OutputRecord],
    *,
    protected_paths: Sequence[Path] = (),
) -> None:
    """Validate a complete prediction batch before replacing its output."""

    if not isinstance(path, Path):
        raise TypeError("path must be a pathlib.Path")
    target_key = _path_key(path)
    if target_key in {_path_key(item) for item in protected_paths}:
        raise ValueError("output path matches a protected input path")

    validated: list[OutputRecord] = []
    seen_ids: set[str] = set()
    for record in records:
        if not isinstance(record, OutputRecord):
            raise TypeError("every prediction must be an OutputRecord")
        serialized = record.model_dump_json()
        checked = OutputRecord.model_validate_json(serialized)
        if checked.id in seen_ids:
            raise ValueError(f"duplicate output id: {checked.id}")
        seen_ids.add(checked.id)
        validated.append(checked)

    text = "".join(
        json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
        for record in validated
    )
    atomic_write(path, text)
