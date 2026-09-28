from __future__ import annotations

import json

import pytest

import app
from docqa.contract_store import ContractStore
from docqa.models.contract import EnumOption, FieldDefinition
from tests.fake_llm import FakeLLMClient
from tests.fixtures.builders import contract_reply, make_builder, make_contract, make_documents


def test_explicit_alias_approval_creates_a_separate_manual_contract(tmp_path) -> None:
    from docqa.alias_approval import apply_alias_approvals

    documents = make_documents()
    body = make_contract()
    body.fields.append(
        FieldDefinition(
            field_id="F_STATUS",
            display_name="办理状态",
            description="已办理或未办理",
            aliases=[],
            value_kind="enum",
            allowed_values=[
                EnumOption(code="DONE", display_name="已办理", aliases=[]),
                EnumOption(code="NOT_DONE", display_name="未办理", aliases=[]),
            ],
            evidence_ids=["E_NEW"],
        )
    )
    base = make_builder(FakeLLMClient([contract_reply(body)]), tmp_path).get_or_build(documents)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        json.dumps(
            {
                "base_cache_key": base.manifest.cache_key,
                "base_body_sha256": base.manifest.body_sha256,
                "approval_note": "业务负责人确认此表达与已办理等价",
                "aliases": [
                    {"field_id": "F_STATUS", "value_code": "DONE", "alias": "手续已经办妥"}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = ContractStore(tmp_path)

    reviewed = apply_alias_approvals(store, documents, approval_path)

    assert reviewed.manifest.cache_key != base.manifest.cache_key
    assert store.review_status(documents.document_set_hash, reviewed.manifest.cache_key) == "manual_approved"
    assert "手续已经办妥" in store.load(documents.document_set_hash, reviewed.manifest.cache_key, documents=documents).body.fields[-1].allowed_values[0].aliases
    assert "手续已经办妥" not in store.load(documents.document_set_hash, base.manifest.cache_key, documents=documents).body.fields[-1].allowed_values[0].aliases
    assert apply_alias_approvals(store, documents, approval_path).manifest.cache_key == reviewed.manifest.cache_key

    payload = json.loads(approval_path.read_text(encoding="utf-8"))
    payload["base_body_sha256"] = "0" * 64
    approval_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="base body hash"):
        apply_alias_approvals(store, documents, approval_path)

    payload["base_body_sha256"] = base.manifest.body_sha256
    payload["aliases"][0]["alias"] = "未办理"
    approval_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="conflicts with another enum value"):
        apply_alias_approvals(store, documents, approval_path)


def test_cli_can_apply_alias_approval_without_api_key(tmp_path, monkeypatch, capsys) -> None:
    documents = make_documents()
    body = make_contract()
    body.fields.append(
        FieldDefinition(
            field_id="F_STATUS",
            display_name="办理状态",
            description="已办理或未办理",
            aliases=[],
            value_kind="enum",
            allowed_values=[EnumOption(code="DONE", display_name="已办理", aliases=[])],
            evidence_ids=["E_NEW"],
        )
    )
    base = make_builder(FakeLLMClient([contract_reply(body)]), tmp_path).get_or_build(documents)
    path = tmp_path / "approval.json"
    path.write_text(
        json.dumps(
            {
                "base_cache_key": base.manifest.cache_key,
                "base_body_sha256": base.manifest.body_sha256,
                "approval_note": "负责人确认同义关系",
                "aliases": [{"field_id": "F_STATUS", "value_code": "DONE", "alias": "手续已经办妥"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app, "_documents", lambda paths: documents)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result = app.main(
        ["approve-aliases", "--docs", "manual.md", "--cache-dir", str(tmp_path), "--approval", str(path)]
    )

    assert result == 0
    assert "review_status=manual_approved" in capsys.readouterr().out


def test_run_cli_passes_explicit_contract_key_to_pipeline(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from docqa import pipeline

    questions = tmp_path / "questions.jsonl"
    questions.write_text('{"id":"Q1","session_id":"S1","question":"办理期限？"}\n', encoding="utf-8")
    output = tmp_path / "predictions.jsonl"
    seen = {}

    def fake_run_batch(*args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(processed=1, answered=1, handed_off=0, item_failures=0)

    monkeypatch.setattr(pipeline, "run_batch", fake_run_batch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    monkeypatch.setattr("docqa.llm_client.DeepSeekClient", lambda config: object())

    result = app.main(
        [
            "run", "--docs", str(tmp_path / "manual.md"), "--questions", str(questions),
            "--output", str(output), "--contract-key", "a" * 64,
        ]
    )

    assert result == 0
    assert seen["pinned_contract_key"] == "a" * 64
