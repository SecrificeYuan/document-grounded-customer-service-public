from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import json

import pytest

import app
from app import main
from tests.fake_llm import FakeLLMClient
from docqa.errors import ConfigurationError
from tests.fixtures.builders import analysis_reply, contract_reply, make_builder, make_config, make_documents


def test_help_requires_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(SystemExit) as caught:
        main(["--help"])

    assert caught.value.code == 0


@pytest.mark.parametrize("command", ["run", "build-contract", "inspect-contract", "evaluate"])
def test_help_exposes_all_four_commands(command: str) -> None:
    with pytest.raises(SystemExit) as caught:
        main([command, "--help"])

    assert caught.value.code == 0


def test_inspect_contract_is_offline_and_reports_verified_review_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "   ")
    documents = make_documents()
    make_builder(FakeLLMClient([contract_reply()]), tmp_path).get_or_build(documents)
    monkeypatch.setattr(app, "_documents", lambda paths: documents)

    result = main(
        [
            "inspect-contract",
            "--docs",
            str(tmp_path / "manual.md"),
            "--cache-dir",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "review_status=auto_approved" in captured.out
    assert "warnings=0" in captured.out
    assert captured.err == ""


def test_build_contract_output_accepts_the_documented_contracts_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    documents = make_documents()
    contracts = tmp_path / "artifacts" / "contracts"
    client = FakeLLMClient([contract_reply()])
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    monkeypatch.setattr(app, "_documents", lambda paths: documents)
    monkeypatch.setattr("docqa.llm_client.DeepSeekClient", lambda config: client)

    result = main(
        [
            "build-contract",
            "--docs",
            str(tmp_path / "manual.md"),
            "--output",
            str(contracts),
        ]
    )

    assert result == 0
    assert len(list(contracts.glob("*/*/manifest.json"))) == 1
    assert not (contracts / "artifacts" / "contracts").exists()
    assert "cache_key=" in capsys.readouterr().out


def test_run_prevalidates_duplicate_ids_before_requiring_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    questions = tmp_path / "questions.jsonl"
    questions.write_text(
        '{"id":"Q","session_id":"S","question":"一"}\n'
        '{"id":"Q","session_id":"S","question":"二"}\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result = main(
        ["run", "--docs", str(tmp_path / "manual.md"), "--questions", str(questions), "--output", str(tmp_path / "out.jsonl")]
    )

    assert result == 2
    assert "duplicate output id" in capsys.readouterr().err


def test_inspect_prefers_current_model_cache_over_hash_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    documents = make_documents()
    old_config = replace(make_config(), model="old-model")
    make_builder(FakeLLMClient([contract_reply()]), tmp_path).get_or_build(documents)
    from docqa.contract_builder import ContractBuilder
    from docqa.contract_store import ContractStore

    ContractBuilder(
        client=FakeLLMClient([contract_reply()]),
        store=ContractStore(tmp_path),
        config=old_config,
    ).get_or_build(documents)
    monkeypatch.setattr(app, "_documents", lambda paths: documents)
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")

    result = main(["inspect-contract", "--docs", "manual.md", "--cache-dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert result == 0
    assert "model=deepseek-flash" in output
    assert "stale=false" in output


def test_second_item_401_makes_cli_nonzero_and_preserves_old_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    questions = tmp_path / "questions.jsonl"
    normal = "2030年1月10日申请，材料齐备，可以办理吗？"
    questions.write_text(
        "".join(
            json.dumps({"id": item, "session_id": "S", "question": normal}, ensure_ascii=False) + "\n"
            for item in ("Q1", "Q2")
        ),
        encoding="utf-8",
    )
    output = tmp_path / "predictions.jsonl"
    output.write_text("OLD\n", encoding="utf-8")
    client = FakeLLMClient([contract_reply(), analysis_reply(), ConfigurationError("HTTP 401")])
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    monkeypatch.setattr("docqa.pipeline.load_documents", lambda paths, max_source_bytes: make_documents())
    monkeypatch.setattr("docqa.llm_client.DeepSeekClient", lambda config: client)

    result = main(["run", "--docs", str(tmp_path / "manual.md"), "--questions", str(questions), "--output", str(output), "--cache-dir", str(tmp_path)])

    assert result == 2
    assert output.read_text(encoding="utf-8") == "OLD\n"
    assert "ConfigurationError" in capsys.readouterr().err
