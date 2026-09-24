from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from docqa.atomic_io import atomic_write
from docqa.errors import InputBatchError
from docqa.jsonl_io import read_input, write_predictions
from docqa.models.output import OutputEvidence, OutputRecord, handoff


def answer_record(record_id: str = "A") -> OutputRecord:
    return OutputRecord(
        id=record_id,
        decision="answer",
        answer="可以办理。",
        reason_code=None,
        evidence=[
            OutputEvidence(
                document="手册.pdf",
                page=1,
                section="办理规则",
                quote="材料齐全时可以办理。",
            )
        ],
    )


def test_bad_question_keeps_id_and_physical_position(tmp_path: Path) -> None:
    source = tmp_path / "q.jsonl"
    source.write_text(
        '{"id":"A","session_id":"S"}\n'
        "{bad\n"
        '{"id":"C","session_id":"S","question":"你好"}\n',
        encoding="utf-8",
    )

    lines = read_input(source).lines

    assert [line.output_id for line in lines] == ["A", "__line_2__", "C"]
    assert [line.line_number for line in lines] == [1, 2, 3]
    assert lines[0].record is None
    assert lines[0].issue_code == "INVALID_RECORD"
    assert lines[1].record is None
    assert lines[1].issue_code == "INVALID_JSON"
    assert lines[2].record is not None
    assert lines[2].record.question == "你好"


def test_blank_lines_are_skipped_but_line_numbers_remain_physical(tmp_path: Path) -> None:
    source = tmp_path / "q.jsonl"
    source.write_text(
        '\n  \n{"id":"A","session_id":"S","question":"问题"}\n',
        encoding="utf-8",
    )

    batch = read_input(source)

    assert len(batch.lines) == 1
    assert batch.lines[0].line_number == 3


def test_input_accepts_utf8_bom(tmp_path: Path) -> None:
    source = tmp_path / "q.jsonl"
    source.write_text(
        '{"id":"中文一","session_id":"会话","question":"你好"}\n',
        encoding="utf-8-sig",
    )

    record = read_input(source).lines[0].record

    assert record is not None
    assert record.id == "中文一"


@pytest.mark.parametrize("contents", ["", "\n \t\n"])
def test_empty_or_blank_only_batch_is_rejected(tmp_path: Path, contents: str) -> None:
    source = tmp_path / "q.jsonl"
    source.write_text(contents, encoding="utf-8")

    with pytest.raises(InputBatchError, match="no question records"):
        read_input(source)


def test_duplicate_id_in_invalid_record_stops_the_batch(tmp_path: Path) -> None:
    source = tmp_path / "q.jsonl"
    source.write_text(
        '{"id":"A","session_id":"S"}\n'
        '{"id":"A","session_id":"S","question":"问题"}\n',
        encoding="utf-8",
    )

    with pytest.raises(InputBatchError, match="duplicate output id: A"):
        read_input(source)


def test_synthetic_id_collision_stops_the_batch(tmp_path: Path) -> None:
    source = tmp_path / "q.jsonl"
    source.write_text(
        '{"id":"__line_2__","session_id":"S","question":"问题"}\n'
        "{bad\n",
        encoding="utf-8",
    )

    with pytest.raises(InputBatchError, match="duplicate output id: __line_2__"):
        read_input(source)


@pytest.mark.parametrize(
    "payload",
    [
        '{"id":" ","session_id":"S","question":"问题"}',
        '{"id":"A","session_id":" ","question":"问题"}',
        '{"id":"A","session_id":"S","question":" "}',
        '["A", "S", "问题"]',
        "null",
        '{"id":1,"session_id":"S","question":"问题"}',
    ],
)
def test_invalid_input_shapes_become_positioned_issues(
    tmp_path: Path, payload: str
) -> None:
    source = tmp_path / "q.jsonl"
    source.write_text(payload + "\n", encoding="utf-8")

    line = read_input(source).lines[0]

    assert line.line_number == 1
    assert line.record is None
    assert line.issue_code == "INVALID_RECORD"


def test_invalid_answer_and_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        OutputRecord.model_validate_json(
            '{"id":"A","decision":"answer","answer":"可用",'
            '"reason_code":null,"evidence":[]}'
        )
    with pytest.raises(ValidationError):
        OutputRecord.model_validate_json(
            '{"id":"A","decision":"handoff","answer":"已转人工",'
            '"reason_code":"AMBIGUOUS","evidence":[],"extra":true}'
        )


def test_answer_and_handoff_relationships_are_strict() -> None:
    with pytest.raises(ValidationError):
        OutputRecord(
            id="A",
            decision="answer",
            answer="可以办理。",
            reason_code="AMBIGUOUS",
            evidence=answer_record().evidence,
        )
    with pytest.raises(ValidationError):
        OutputRecord(
            id="A",
            decision="handoff",
            answer="已转人工处理。",
            reason_code="AMBIGUOUS",
            evidence=answer_record().evidence,
        )
    assert handoff("A", "AMBIGUOUS").model_dump() == {
        "id": "A",
        "decision": "handoff",
        "answer": "已转人工处理。",
        "reason_code": "AMBIGUOUS",
        "evidence": [],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", " "),
        ("answer", "\t"),
    ],
)
def test_output_text_fields_cannot_be_blank(field: str, value: str) -> None:
    payload = answer_record().model_dump()
    payload[field] = value

    with pytest.raises(ValidationError):
        OutputRecord.model_validate(payload)


def test_invalid_output_does_not_replace_previous_file(tmp_path: Path) -> None:
    target = tmp_path / "predictions.jsonl"
    target.write_text("OLD\n", encoding="utf-8")

    with pytest.raises(TypeError):
        write_predictions(target, [object()])  # type: ignore[list-item]

    assert target.read_text(encoding="utf-8") == "OLD\n"


def test_duplicate_output_ids_do_not_replace_previous_file(tmp_path: Path) -> None:
    target = tmp_path / "predictions.jsonl"
    target.write_text("OLD\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate output id: A"):
        write_predictions(target, [answer_record(), answer_record()])

    assert target.read_text(encoding="utf-8") == "OLD\n"


def test_output_is_utf8_jsonl_with_literal_chinese(tmp_path: Path) -> None:
    target = tmp_path / "predictions.jsonl"

    write_predictions(target, [answer_record(), handoff("B", "OUT_OF_SCOPE")])

    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert "可以办理。" in raw.decode("utf-8")
    assert raw.endswith(b"\n")
    assert len(raw.decode("utf-8").splitlines()) == 2


def test_replace_permission_error_preserves_old_file_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "predictions.jsonl"
    target.write_text("OLD\n", encoding="utf-8")

    def deny_replace(source: str | os.PathLike[str], destination: Path) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(os, "replace", deny_replace)

    with pytest.raises(PermissionError, match="locked"):
        atomic_write(target, "NEW\n")

    assert target.read_text(encoding="utf-8") == "OLD\n"
    assert list(tmp_path.glob(".predictions.jsonl.*.tmp")) == []


def test_output_cannot_overwrite_a_protected_input_path(tmp_path: Path) -> None:
    source = tmp_path / "questions.jsonl"
    original = '{"id":"A","session_id":"S","question":"问题"}\n'
    source.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="protected input path"):
        write_predictions(source, [answer_record()], protected_paths=[source])

    assert source.read_text(encoding="utf-8") == original
