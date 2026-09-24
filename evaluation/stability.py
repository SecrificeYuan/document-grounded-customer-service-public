"""Compare independently generated prediction batches against a frozen oracle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Sequence

from pydantic import ConfigDict

from docqa.errors import InputBatchError
from docqa.models.common import StrictModel
from docqa.models.output import OutputRecord
from evaluation.evaluate import ReferenceRecord


Component = Literal["decision", "reason", "facts", "evidence"]


class RunStability(StrictModel):
    path: str
    strict_output_valid: bool
    ids_match: bool
    reference_passed: int


class ItemStability(StrictModel):
    id: str
    stable_across_runs: bool
    meets_reference_every_run: bool
    changed_components: tuple[Component, ...]


class StabilityReport(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    run_count: int
    item_count: int
    stable: bool
    runs: tuple[RunStability, ...]
    items: tuple[ItemStability, ...]


def _read_records(path: Path, *, reference: bool) -> list[ReferenceRecord] | list[OutputRecord]:
    records: list[ReferenceRecord] | list[OutputRecord] = []
    seen: set[str] = set()
    label = "reference" if reference else "prediction"
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError as error:
        raise InputBatchError(f"invalid {label} UTF-8") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            record = (
                ReferenceRecord.model_validate(payload)
                if reference
                else OutputRecord.model_validate(payload)
            )
        except (json.JSONDecodeError, ValueError, TypeError) as error:
            raise InputBatchError(
                f"invalid {label} record at line {line_number}"
            ) from error
        if record.id in seen:
            raise InputBatchError(f"duplicate {label} id: {record.id}")
        seen.add(record.id)
        records.append(record)
    if not records:
        raise InputBatchError(f"{label} contains no records")
    return records


def semantic_signature(
    actual: OutputRecord, expected: ReferenceRecord
) -> tuple[object, ...]:
    """Return the plan-defined semantic signature, ignoring evidence order."""

    return (
        actual.decision,
        actual.reason_code,
        tuple(fact in actual.answer for fact in expected.required_facts),
        tuple(
            sorted(
                {
                    (item.document, item.page, item.section)
                    for item in actual.evidence
                }
            )
        ),
    )


def _meets_reference(actual: OutputRecord, expected: ReferenceRecord) -> bool:
    signature = semantic_signature(actual, expected)
    positions = signature[3]
    allowed = {
        (item.document, item.page, item.section)
        for item in expected.acceptable_evidence
    }
    evidence_ok = (
        bool(positions) and set(positions) <= allowed
        if expected.expected_decision == "answer"
        else not positions
    )
    return bool(
        actual.decision == expected.expected_decision
        and actual.reason_code == expected.reason_code
        and all(signature[2])
        and evidence_ok
    )


def compare_runs(paths: Sequence[Path], reference: Path) -> StabilityReport:
    """Validate complete runs and compare decisions, facts, and evidence sets."""

    if not paths:
        raise InputBatchError("no prediction runs supplied")
    expected_rows = _read_records(Path(reference), reference=True)
    expected = {item.id: item for item in expected_rows}
    ordered_ids = tuple(item.id for item in expected_rows)
    parsed_runs: list[dict[str, OutputRecord]] = []
    run_audits: list[RunStability] = []
    for path in paths:
        actual_rows = _read_records(Path(path), reference=False)
        actual = {item.id: item for item in actual_rows}
        if tuple(actual) != ordered_ids:
            missing = sorted(expected.keys() - actual.keys())
            extra = sorted(actual.keys() - expected.keys())
            raise InputBatchError(
                "prediction ids do not match reference ids: "
                f"missing={missing!r} extra={extra!r}"
            )
        parsed_runs.append(actual)
        run_audits.append(
            RunStability(
                path=str(Path(path)),
                strict_output_valid=True,
                ids_match=True,
                reference_passed=sum(
                    _meets_reference(actual[item_id], expected[item_id])
                    for item_id in ordered_ids
                ),
            )
        )

    component_names: tuple[Component, ...] = (
        "decision",
        "reason",
        "facts",
        "evidence",
    )
    items: list[ItemStability] = []
    for item_id in ordered_ids:
        signatures = [
            semantic_signature(run[item_id], expected[item_id])
            for run in parsed_runs
        ]
        changed = tuple(
            name
            for index, name in enumerate(component_names)
            if any(signature[index] != signatures[0][index] for signature in signatures[1:])
        )
        meets_reference = all(
            _meets_reference(run[item_id], expected[item_id]) for run in parsed_runs
        )
        items.append(
            ItemStability(
                id=item_id,
                stable_across_runs=not changed,
                meets_reference_every_run=meets_reference,
                changed_components=changed,
            )
        )

    return StabilityReport(
        run_count=len(parsed_runs),
        item_count=len(ordered_ids),
        stable=all(
            item.stable_across_runs and item.meets_reference_every_run
            for item in items
        ),
        runs=tuple(run_audits),
        items=tuple(items),
    )
