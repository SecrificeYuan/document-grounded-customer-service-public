from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from docqa.models.analysis import AnalysisResult
from docqa.models.common import BoolValue, DateValue, NormalizedValue
from docqa.models.contract import ContractBody, Predicate, TimeBasis
from tests.fixtures.builders import make_analysis, make_contract, make_documents


def test_chinese_payload_roundtrips() -> None:
    contract = make_contract()
    assert ContractBody.model_validate_json(contract.model_dump_json()) == contract
    analysis = make_analysis()
    assert AnalysisResult.model_validate_json(analysis.model_dump_json()) == analysis


def test_extra_fields_are_rejected() -> None:
    payload = json.loads(make_contract().model_dump_json())
    payload["approve_me"] = True
    with pytest.raises(ValueError):
        ContractBody.model_validate_json(json.dumps(payload, ensure_ascii=False))


def test_bool_is_not_an_integer() -> None:
    with pytest.raises(ValueError):
        TypeAdapter(NormalizedValue).validate_json('{"kind":"integer","value":true}')


@pytest.mark.parametrize(
    ("operator", "values"),
    [
        ("exists", [BoolValue(kind="boolean", value=True)]),
        ("eq", []),
        ("in", []),
        ("between", [DateValue(kind="date", value=date(2030, 1, 10))]),
        ("before", [BoolValue(kind="boolean", value=True)]),
        ("between", [DateValue(kind="date", value=date(2030, 1, 10)), BoolValue(kind="boolean", value=True)]),
    ],
)
def test_predicate_rejects_wrong_value_count_or_kind(operator: str, values: list[object]) -> None:
    with pytest.raises(ValueError):
        Predicate(field_id="F_DATE", operator=operator, values=values)


def test_predicate_accepts_closed_date_interval() -> None:
    predicate = Predicate(field_id="F_DATE", operator="between", values=[DateValue(kind="date", value=date(2030, 1, 10)), DateValue(kind="date", value=date(2030, 1, 19))])
    assert [value.value for value in predicate.values] == [date(2030, 1, 10), date(2030, 1, 19)]


def test_time_basis_requires_field_only_for_field_kind() -> None:
    assert TimeBasis(kind="field", field_id="F_DATE").field_id == "F_DATE"
    with pytest.raises(ValueError):
        TimeBasis(kind="field", field_id=None)
    with pytest.raises(ValueError):
        TimeBasis(kind="reference_date", field_id="F_DATE")


def test_blank_ids_and_unknown_kinds_are_rejected() -> None:
    with pytest.raises(ValueError):
        Predicate(field_id="  ", operator="exists", values=[])
    with pytest.raises(ValueError):
        TypeAdapter(NormalizedValue).validate_json('{"kind":"mystery","value":"x"}')


def test_blank_ids_inside_lists_are_rejected() -> None:
    contract = json.loads(make_contract().model_dump_json())
    contract["policy_units"][0]["topic_ids"] = ["  "]
    with pytest.raises(ValueError):
        ContractBody.model_validate_json(json.dumps(contract, ensure_ascii=False))

    analysis = json.loads(make_analysis().model_dump_json())
    analysis["intents"][0]["family_ids"] = [""]
    with pytest.raises(ValueError):
        AnalysisResult.model_validate_json(json.dumps(analysis, ensure_ascii=False))


def test_fixture_variants_are_fresh_and_consistent() -> None:
    default = make_contract()
    assert {unit.unit_id for unit in default.policy_units} == {"U_OLD", "U_NEW"}
    assert {claim.claim_id for unit in default.policy_units for claim in unit.claims} == {"C_OLD", "C_NEW"}
    irrelevant = make_contract(include_irrelevant_family=True)
    assert "G_REFUND" in {unit.family_id for unit in irrelevant.policy_units}
    assert "F_REFUND_DATE" in {field.field_id for field in irrelevant.fields}
    assert all("G_REFUND" not in intent.family_ids for intent in make_analysis().intents)
    chain = make_contract(three_version_chain=True)
    assert [unit.unit_id for unit in chain.policy_units] == ["U_OLD", "U_MID", "U_NEW"]
    assert [unit.effective_from for unit in chain.policy_units] == [None, date(2030, 1, 10), date(2030, 1, 20)]
    assert chain.policy_units[2].overrides_unit_ids == ["U_MID"]
    evidence_text = next(block.text for block in make_documents().blocks if block.block_id == "E_NEW")
    assert "8个自然日" in evidence_text
    assert "12个自然日" in evidence_text
    no_date = make_analysis(event_date=None)
    assert "2030" not in no_date.intents[0].source.text
    assert "F_DATE" not in {fact.field_id for fact in no_date.extracted_facts}
    assert no_date.missing_field_ids == ["F_DATE"]
    rule = make_analysis(mode="rule_description")
    assert rule.intents[0].mode == "rule_description"
    assert rule.extracted_facts == []
    first = make_documents()
    second = make_documents()
    first.blocks[0].text = "被测试修改"
    assert second.blocks[0].text != first.blocks[0].text


@pytest.mark.parametrize("builder", [make_contract, make_analysis])
def test_factory_json_roundtrip(builder: object) -> None:
    value = builder()
    assert type(value).model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize(
    ("model", "snapshot_name"),
    [(ContractBody, "contract_body.schema.json"), (AnalysisResult, "analysis_result.schema.json")],
)
def test_json_schema_matches_snapshot(model: object, snapshot_name: str) -> None:
    path = Path(__file__).parents[1] / "snapshots" / snapshot_name
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert model.model_json_schema() == expected
    serialized = json.dumps(expected, ensure_ascii=False)
    assert "参考答案" not in serialized
    assert "E:\\" not in serialized
