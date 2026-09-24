from __future__ import annotations

import json
from pathlib import Path

import pytest

from docqa.errors import ContractBlocked, TransportExhausted
from docqa.models.contract import ContractWarning, UnitDefinition
from tests.fake_llm import FakeLLMClient
from tests.fixtures.builders import (
    contract_reply,
    make_builder,
    make_contract,
    make_documents,
    raw_reply,
)


def _wire_size(request, *, model: str = "deepseek-flash", effort: str = "high") -> int:
    payload = {
        "model": model,
        "instructions": request.instructions,
        "input": request.input_text,
        "reasoning": {"effort": effort},
        "text": {
            "format": {
                "type": "json_schema",
                "name": request.schema_name,
                "schema": request.schema,
            }
        },
        "max_output_tokens": request.max_output_tokens,
        "stream": False,
    }
    return len(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )


def test_invalid_json_can_be_repaired_once(tmp_path) -> None:
    client = FakeLLMClient([raw_reply("{bad"), contract_reply()])
    result = make_builder(client, tmp_path).get_or_build(make_documents())

    assert result.body.policy_units
    assert [call.purpose for call in client.calls] == ["contract", "repair"]
    assert client.calls[1].max_output_tokens == 64_000
    assert "{bad" in client.calls[1].input_text
    repair_payload = json.loads(client.calls[1].input_text)
    assert repair_payload["validation_errors"][0]["code"] == "INVALID_CONTRACT_SCHEMA"


def test_second_invalid_result_blocks_startup(tmp_path) -> None:
    client = FakeLLMClient([raw_reply(""), raw_reply("{bad")])

    with pytest.raises(ContractBlocked, match="two semantic attempts") as caught:
        make_builder(client, tmp_path).get_or_build(make_documents())

    assert len(client.calls) == 2
    assert "{bad" not in str(caught.value)


def test_incomplete_repair_reports_safe_failure_category(tmp_path) -> None:
    client = FakeLLMClient(
        [raw_reply("{bad"), raw_reply("private partial output", status="incomplete")]
    )

    with pytest.raises(ContractBlocked, match="MODEL_INCOMPLETE") as caught:
        make_builder(client, tmp_path).get_or_build(make_documents())

    assert "private partial output" not in str(caught.value)


def test_ambiguous_unit_alias_uses_the_contract_repair_attempt(tmp_path) -> None:
    invalid = make_contract()
    invalid.units.append(
        UnitDefinition(
            unit_code="BUSINESS_DAY",
            display_name="工作日",
            aliases=["天"],
            dimension="duration",
            evidence_ids=["E_NEW"],
        )
    )
    client = FakeLLMClient([contract_reply(invalid), contract_reply()])

    result = make_builder(client, tmp_path).get_or_build(make_documents())

    assert result.body == make_contract()
    assert [call.purpose for call in client.calls] == ["contract", "repair"]
    assert "AMBIGUOUS_UNIT_ALIAS" in client.calls[1].input_text


def test_cache_hit_uses_no_additional_model_call(tmp_path) -> None:
    client = FakeLLMClient([contract_reply()])
    builder = make_builder(client, tmp_path)
    documents = make_documents()

    first = builder.get_or_build(documents)
    second = builder.get_or_build(documents)

    assert second == first
    assert len(client.calls) == 1


def test_contract_manifest_invalidates_v4_cache_for_companion_semantics(
    tmp_path, monkeypatch
) -> None:
    import docqa.contract_builder as builder_module

    client = FakeLLMClient([contract_reply(), contract_reply()])
    documents = make_documents()
    monkeypatch.setattr(builder_module, "_VALIDATOR_VERSION", "contract-validator-v4")
    previous = make_builder(client, tmp_path).get_or_build(documents)

    monkeypatch.setattr(builder_module, "_VALIDATOR_VERSION", "contract-validator-v5")
    current = make_builder(client, tmp_path).get_or_build(documents)

    assert previous.manifest.validator_version == "contract-validator-v4"
    assert current.manifest.validator_version == "contract-validator-v5"
    assert current.manifest.cache_key != previous.manifest.cache_key
    assert len(client.calls) == 2


def test_contract_manifest_invalidates_v5_cache_for_offline_faq(
    tmp_path, monkeypatch
) -> None:
    import docqa.contract_builder as builder_module

    client = FakeLLMClient([contract_reply(), contract_reply()])
    documents = make_documents()
    monkeypatch.setattr(builder_module, "_VALIDATOR_VERSION", "contract-validator-v5")
    previous = make_builder(client, tmp_path).get_or_build(documents)

    monkeypatch.setattr(builder_module, "_VALIDATOR_VERSION", "contract-validator-v6")
    current = make_builder(client, tmp_path).get_or_build(documents)

    assert previous.manifest.validator_version == "contract-validator-v5"
    assert current.manifest.validator_version == "contract-validator-v6"
    assert current.manifest.cache_key != previous.manifest.cache_key
    assert len(client.calls) == 2


def test_contract_manifest_invalidates_v6_cache_for_query_aliases(
    tmp_path, monkeypatch
) -> None:
    import docqa.contract_builder as builder_module

    client = FakeLLMClient([contract_reply(), contract_reply()])
    documents = make_documents()
    monkeypatch.setattr(builder_module, "_VALIDATOR_VERSION", "contract-validator-v6")
    previous = make_builder(client, tmp_path).get_or_build(documents)

    monkeypatch.setattr(builder_module, "_VALIDATOR_VERSION", "contract-validator-v7")
    current = make_builder(client, tmp_path).get_or_build(documents)

    assert previous.manifest.validator_version == "contract-validator-v6"
    assert current.manifest.validator_version == "contract-validator-v7"
    assert current.manifest.cache_key != previous.manifest.cache_key
    assert len(client.calls) == 2


def test_force_rebuild_bypasses_a_valid_cache_entry(tmp_path) -> None:
    client = FakeLLMClient([contract_reply(), contract_reply()])
    builder = make_builder(client, tmp_path)
    documents = make_documents()

    builder.get_or_build(documents)
    builder.get_or_build(documents, force=True)

    assert len(client.calls) == 2


def test_source_or_prompt_change_rebuilds_contract(tmp_path) -> None:
    documents = make_documents()
    first_client = FakeLLMClient([contract_reply()])
    make_builder(first_client, tmp_path, prompt_version="prompt-v1").get_or_build(documents)

    changed_documents = make_documents()
    changed_documents.document_set_hash = "e" * 64
    source_client = FakeLLMClient([contract_reply()])
    make_builder(source_client, tmp_path, prompt_version="prompt-v1").get_or_build(changed_documents)

    prompt_client = FakeLLMClient([contract_reply()])
    make_builder(prompt_client, tmp_path, prompt_version="prompt-v2").get_or_build(documents)

    assert len(source_client.calls) == 1
    assert len(prompt_client.calls) == 1


def test_model_cannot_approve_its_own_contract(tmp_path) -> None:
    fake_approval = make_contract().model_dump(mode="json")
    fake_approval["review_status"] = "auto_approved"
    client = FakeLLMClient([raw_reply(json.dumps(fake_approval, ensure_ascii=False)), contract_reply()])

    result = make_builder(client, tmp_path).get_or_build(make_documents())

    assert result.body == make_contract()
    assert len(client.calls) == 2


def test_repair_that_keeps_blocking_warning_blocks_startup(tmp_path) -> None:
    blocked = make_contract()
    blocked.warnings.append(
        ContractWarning(
            code="UNRESOLVED_VERSION",
            severity="blocking",
            message="版本关系无法由原文确定。",
            evidence_ids=["E_NEW"],
        )
    )
    client = FakeLLMClient([contract_reply(blocked), contract_reply(blocked)])

    with pytest.raises(ContractBlocked, match="BLOCKING_WARNING"):
        make_builder(client, tmp_path).get_or_build(make_documents())

    assert len(client.calls) == 2


@pytest.mark.parametrize("invalid_kind", ["warning", "evidence", "extra_key"])
def test_final_error_never_contains_model_controlled_text(tmp_path, invalid_kind) -> None:
    marker = f"PRIVATE_TEST_MARKER_{invalid_kind}"
    payload = make_contract().model_dump(mode="json")
    if invalid_kind == "warning":
        payload["warnings"].append(
            {
                "code": "UNRESOLVED_VERSION",
                "severity": "blocking",
                "message": marker,
                "evidence_ids": ["E_NEW"],
            }
        )
    elif invalid_kind == "evidence":
        payload["policy_units"][0]["claims"][0]["evidence_ids"] = [marker]
    else:
        payload[marker] = "not allowed"
    reply = raw_reply(json.dumps(payload, ensure_ascii=False))

    with pytest.raises(ContractBlocked) as caught:
        make_builder(FakeLLMClient([reply, reply]), tmp_path).get_or_build(
            make_documents()
        )

    assert marker not in str(caught.value)


def test_transport_failure_does_not_spend_semantic_repair(tmp_path) -> None:
    client = FakeLLMClient([TransportExhausted("offline")])

    with pytest.raises(TransportExhausted, match="offline"):
        make_builder(client, tmp_path).get_or_build(make_documents())

    assert len(client.calls) == 1


def test_complete_utf8_request_budget_is_checked_before_call(tmp_path) -> None:
    client = FakeLLMClient([])

    with pytest.raises(ContractBlocked, match="UTF-8 request byte limit"):
        make_builder(client, tmp_path, max_request_bytes=100).get_or_build(make_documents())

    assert client.calls == []


def test_initial_budget_counts_wire_envelope_and_json_escaping(tmp_path: Path) -> None:
    documents = make_documents()
    documents.blocks[0].text += ('"quoted"\\path\n中文' * 100)
    probe = FakeLLMClient([contract_reply()])
    make_builder(probe, tmp_path / "probe").get_or_build(documents)
    wire_size = _wire_size(probe.calls[0])

    blocked = FakeLLMClient([])
    with pytest.raises(ContractBlocked, match="UTF-8 request byte limit"):
        make_builder(
            blocked, tmp_path / "blocked", max_request_bytes=wire_size - 1
        ).get_or_build(documents)

    assert blocked.calls == []


def test_repair_budget_counts_its_larger_wire_payload(tmp_path: Path) -> None:
    invalid = raw_reply(json.dumps({"unexpected": "x" * 20_000}))
    probe = FakeLLMClient([invalid, contract_reply()])
    make_builder(probe, tmp_path / "probe").get_or_build(make_documents())
    initial_size = _wire_size(probe.calls[0])
    repair_size = _wire_size(probe.calls[1])
    assert repair_size > initial_size

    client = FakeLLMClient([invalid])
    with pytest.raises(ContractBlocked, match="UTF-8 request byte limit"):
        make_builder(
            client, tmp_path / "blocked", max_request_bytes=repair_size - 1
        ).get_or_build(make_documents())

    assert len(client.calls) == 1


def test_contract_request_contains_stable_raw_evidence_and_schema(tmp_path) -> None:
    client = FakeLLMClient([contract_reply()])

    make_builder(client, tmp_path).get_or_build(make_documents())

    request = client.calls[0]
    assert request.purpose == "contract"
    assert request.schema_name == "document_contract_body"
    assert request.schema["additionalProperties"] is False
    assert "E_OLD" in request.input_text
    assert "2030年1月10日前申请" in request.input_text
    assert "family_id 只表示同一规则的版本链" in request.instructions
    assert "overrides_unit_ids 中每个目标必须与当前单元具有完全相同的 family_id" in request.instructions
    assert "同时有效的并列规则必须使用不同 family_id" in request.instructions
