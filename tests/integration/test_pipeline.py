from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from docqa.errors import ConfigurationError
from docqa.models.analysis import AnalysisResult, Intent
from docqa.models.common import Span
from docqa.pipeline import run_batch
from tests.fake_llm import FakeLLMClient
from tests.fixtures.builders import (
    analysis_reply,
    contract_reply,
    make_config,
    make_documents,
    raw_reply,
    make_builder,
)


def _questions(path: Path) -> None:
    normal = "2030年1月10日申请，材料齐备，可以办理吗？"
    rows = [
        {"id": "Q1", "session_id": "S", "question": normal},
        {"id": "Q2", "session_id": "S", "question": normal},
        {"id": "Q3", "session_id": "S", "question": "明天天气如何？"},
    ]
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _oos_reply():
    question = "明天天气如何？"
    analysis = AnalysisResult(
        language="zh-CN",
        intents=[
            Intent(
                intent_id="I_WEATHER",
                source=Span(start=0, end=len(question), text=question),
                description="天气请求",
                scope="out_of_scope",
                mode="case_application",
                family_ids=[],
                claim_ids=[],
            )
        ],
        extracted_facts=[],
        unresolved_expressions=[],
        missing_field_ids=[],
        selected_unit_ids=[],
        claim_plan=[],
        conflicting_unit_ids=[],
        proposed_decision="handoff",
        reason_code="OUT_OF_SCOPE",
        answer_draft="该问题不属于业务文档范围。",
    )
    return raw_reply(analysis.model_dump_json())


def test_batch_preserves_order_and_isolates_one_technical_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    questions = tmp_path / "questions.jsonl"
    output = tmp_path / "predictions.jsonl"
    _questions(questions)
    monkeypatch.setattr("docqa.pipeline.load_documents", lambda paths, max_source_bytes: make_documents())
    client = FakeLLMClient(
        [contract_reply(), raw_reply(""), raw_reply(""), analysis_reply(), _oos_reply()]
    )

    report = run_batch(
        [tmp_path / "manual.md"], questions, output, tmp_path, make_config(), client
    )

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [row["id"] for row in rows] == ["Q1", "Q2", "Q3"]
    assert [row["decision"] for row in rows] == ["handoff", "answer", "handoff"]
    assert report.processed == 3
    assert report.answered == 1
    assert report.handed_off == 2
    assert report.item_failures == 1
    assert [item.model_dump(mode="json") for item in report.attempt_failures] == [
        {
            "item_number": 1,
            "attempt": 1,
            "response_status": "completed",
            "issue_codes": ["INVALID_MODEL_REPLY"],
            "contract_object_ids": [],
            "usage": {
                "input_tokens": None,
                "output_tokens": None,
                "cached_tokens": None,
                "reasoning_tokens": None,
            },
        },
        {
            "item_number": 1,
            "attempt": 2,
            "response_status": "completed",
            "issue_codes": ["INVALID_MODEL_REPLY"],
            "contract_object_ids": [],
            "usage": {
                "input_tokens": None,
                "output_tokens": None,
                "cached_tokens": None,
                "reasoning_tokens": None,
            },
        },
    ]


def test_global_configuration_error_preserves_old_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    questions = tmp_path / "questions.jsonl"
    output = tmp_path / "predictions.jsonl"
    _questions(questions)
    output.write_text("OLD\n", encoding="utf-8")
    monkeypatch.setattr("docqa.pipeline.load_documents", lambda paths, max_source_bytes: make_documents())
    client = FakeLLMClient([contract_reply(), ConfigurationError("HTTP 401")])

    with pytest.raises(ConfigurationError, match="401"):
        run_batch(
            [tmp_path / "manual.md"], questions, output, tmp_path, make_config(), client
        )

    assert output.read_text(encoding="utf-8") == "OLD\n"


def test_output_cannot_replace_a_source_inside_document_directory(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "manual.md"
    source.write_text("# 规则\n原始资料", encoding="utf-8")
    questions = tmp_path / "questions.jsonl"
    questions.write_text(
        '{"id":"Q","session_id":"S","question":"问题"}\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="protected input"):
        run_batch([corpus], questions, source, tmp_path, make_config(), FakeLLMClient([]))

    assert source.read_text(encoding="utf-8") == "# 规则\n原始资料"


def test_pinned_contract_is_reused_across_analysis_efforts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents = make_documents()
    contract = make_builder(
        FakeLLMClient([contract_reply()]), tmp_path
    ).get_or_build(documents)
    questions = tmp_path / "questions.jsonl"
    output = tmp_path / "predictions.jsonl"
    question = "2030年1月10日申请，材料齐备，可以办理吗？"
    questions.write_text(
        json.dumps(
            {"id": "Q1", "session_id": "S1", "question": question},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "docqa.pipeline.load_documents", lambda paths, max_source_bytes: documents
    )
    client = FakeLLMClient([analysis_reply()])

    report = run_batch(
        [tmp_path / "manual.md"],
        questions,
        output,
        tmp_path,
        replace(make_config(), reasoning_effort="max"),
        client,
        pinned_contract_key=contract.manifest.cache_key,
    )

    assert report.answered == 1
    assert [call.purpose for call in client.calls] == ["analysis"]
