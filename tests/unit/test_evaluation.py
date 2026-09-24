from __future__ import annotations

import json
from pathlib import Path

import pytest

from docqa.errors import InputBatchError
from evaluation.evaluate import evaluate


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _reference_row(record_id: str) -> dict[str, object]:
    return {
        "id": record_id,
        "expected_decision": "answer",
        "reason_code": None,
        "reference_answer": "规则要求八天。",
        "required_facts": ["八天"],
        "acceptable_evidence": [
            {"document": "manual.md", "page": 1, "section": "期限"}
        ],
    }


def _prediction_row(record_id: str) -> dict[str, object]:
    return {
        "id": record_id,
        "decision": "answer",
        "answer": "规则要求八天。",
        "reason_code": None,
        "evidence": [
            {
                "document": "manual.md",
                "page": 1,
                "section": "期限",
                "quote": "规则要求八天。",
            }
        ],
    }


def test_official_metrics_keep_literal_facts_separate_from_evidence(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.jsonl"
    predictions = tmp_path / "predictions.jsonl"
    _write_jsonl(
        reference,
        [
            {
                "id": "A",
                "expected_decision": "answer",
                "reason_code": None,
                "reference_answer": "规则要求八天。",
                "required_facts": ["八天"],
                "acceptable_evidence": [
                    {"document": "manual.md", "page": 1, "section": "期限"}
                ],
            },
            {
                "id": "B",
                "expected_decision": "handoff",
                "reason_code": "AMBIGUOUS",
                "reference_answer": "条件不完整。",
                "required_facts": [],
                "acceptable_evidence": [],
            },
        ],
    )
    _write_jsonl(
        predictions,
        [
            {
                "id": "A",
                "decision": "answer",
                "answer": "规则要求八日。",
                "reason_code": None,
                "evidence": [
                    {
                        "document": "manual.md",
                        "page": 1,
                        "section": "期限",
                        "quote": "规则要求八日。",
                    }
                ],
            },
            {
                "id": "B",
                "decision": "handoff",
                "answer": "已转人工处理。",
                "reason_code": "AMBIGUOUS",
                "evidence": [],
            },
        ],
    )

    metrics = evaluate(predictions, reference)

    assert metrics.total == 2
    assert metrics.decision_correct == 2
    assert metrics.reason_correct == 2
    assert metrics.fact_hits == 0
    assert metrics.fact_total == 1
    assert metrics.evidence_position_correct == 2
    assert metrics.strict_output_valid is True
    assert [item.id for item in metrics.audit] == ["A", "B"]


def test_audit_rejects_extra_unapproved_evidence_positions(tmp_path: Path) -> None:
    reference = tmp_path / "reference.jsonl"
    predictions = tmp_path / "predictions.jsonl"
    actual = _prediction_row("A")
    evidence = actual["evidence"]
    assert isinstance(evidence, list)
    evidence.append(
        {
            "document": "manual.md",
            "page": 9,
            "section": "无关章节",
            "quote": "无关内容。",
        }
    )
    _write_jsonl(reference, [_reference_row("A")])
    _write_jsonl(predictions, [actual])

    metrics = evaluate(predictions, reference)

    assert metrics.evidence_position_correct == 1
    assert metrics.audit[0].strict_evidence_positions_valid is False


def test_audit_checks_section_while_official_metric_checks_document_page(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.jsonl"
    predictions = tmp_path / "predictions.jsonl"
    actual = _prediction_row("A")
    evidence = actual["evidence"]
    assert isinstance(evidence, list)
    assert isinstance(evidence[0], dict)
    evidence[0]["section"] = "错误章节"
    _write_jsonl(reference, [_reference_row("A")])
    _write_jsonl(predictions, [actual])

    metrics = evaluate(predictions, reference)

    assert metrics.evidence_position_correct == 1
    assert metrics.audit[0].strict_evidence_positions_valid is False


@pytest.mark.parametrize(
    "prediction_ids",
    [
        ["A"],
        ["A", "B", "C"],
    ],
)
def test_prediction_ids_must_match_reference_exactly(
    tmp_path: Path, prediction_ids: list[str]
) -> None:
    reference = tmp_path / "reference.jsonl"
    predictions = tmp_path / "predictions.jsonl"
    _write_jsonl(reference, [_reference_row("A"), _reference_row("B")])
    _write_jsonl(predictions, [_prediction_row(item) for item in prediction_ids])

    with pytest.raises(InputBatchError, match="prediction ids do not match reference ids"):
        evaluate(predictions, reference)


@pytest.mark.parametrize("kind", ["predictions", "reference"])
def test_duplicate_ids_are_rejected(tmp_path: Path, kind: str) -> None:
    reference = tmp_path / "reference.jsonl"
    predictions = tmp_path / "predictions.jsonl"
    reference_rows = [_reference_row("A")]
    prediction_rows = [_prediction_row("A")]
    if kind == "predictions":
        prediction_rows.append(_prediction_row("A"))
    else:
        reference_rows.append(_reference_row("A"))
    _write_jsonl(reference, reference_rows)
    _write_jsonl(predictions, prediction_rows)

    with pytest.raises(InputBatchError, match=f"duplicate {kind} id: A"):
        evaluate(predictions, reference)


def test_invalid_prediction_is_rejected_by_strict_output_schema(tmp_path: Path) -> None:
    reference = tmp_path / "reference.jsonl"
    predictions = tmp_path / "predictions.jsonl"
    invalid = _prediction_row("A")
    invalid["extra"] = True
    _write_jsonl(reference, [_reference_row("A")])
    _write_jsonl(predictions, [invalid])

    with pytest.raises(InputBatchError, match="invalid prediction record at line 1"):
        evaluate(predictions, reference)


def test_reference_requires_expected_decision_field(tmp_path: Path) -> None:
    reference = tmp_path / "reference.jsonl"
    predictions = tmp_path / "predictions.jsonl"
    invalid = _reference_row("A")
    invalid["decision"] = invalid.pop("expected_decision")
    _write_jsonl(reference, [invalid])
    _write_jsonl(predictions, [_prediction_row("A")])

    with pytest.raises(InputBatchError, match="invalid reference record at line 1"):
        evaluate(predictions, reference)
