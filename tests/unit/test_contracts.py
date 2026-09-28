from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from docqa.contract_store import (
    ContractManifest,
    ContractStore,
    DocumentContract,
    ReviewRecord,
    cache_key,
    canonical_body_sha256,
)
from docqa.contract_validator import validate_contract
from docqa.models.common import DateValue, Issue, QuantityValue
from docqa.models.contract import ConditionGroup, ContractWarning, EnumOption, FieldDefinition, Predicate, Topic, UnitDefinition
from tests.fixtures.builders import make_contract, make_documents


def issue_codes(body=None) -> set[str]:
    return {issue.code for issue in validate_contract(body or make_contract(), make_documents()).issues}


def test_unknown_evidence_and_cycle_block_contract() -> None:
    body = make_contract()
    body.policy_units[0].claims[0].evidence_ids = ["UNKNOWN"]
    assert "UNKNOWN_EVIDENCE" in issue_codes(body)

    body = make_contract()
    body.policy_units[0].overrides_unit_ids = [body.policy_units[1].unit_id]
    assert "OVERRIDE_CYCLE" in issue_codes(body)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda body: body.fields.append(body.fields[0].model_copy(deep=True)), "DUPLICATE_ID"),
        (lambda body: setattr(body.policy_units[0], "effective_from", body.policy_units[0].effective_to.replace(day=10)), "INVALID_DATE_RANGE"),
        (lambda body: body.policy_units[1].overrides_unit_ids.__setitem__(0, "MISSING"), "UNKNOWN_OVERRIDE"),
        (lambda body: setattr(body.policy_units[1], "family_id", "G_OTHER"), "CROSS_FAMILY_OVERRIDE"),
        (lambda body: setattr(body.policy_units[0].time_basis, "field_id", "F_UNKNOWN"), "INVALID_TIME_BASIS"),
        (lambda body: body.policy_units[0].claims[0].required_companion_claim_ids.append("C_MISSING"), "UNKNOWN_COMPANION"),
        (lambda body: body.scope[0].evidence_ids.clear(), "UNBOUND_EVIDENCE"),
        (lambda body: body.fields[0].evidence_ids.clear(), "UNBOUND_EVIDENCE"),
        (lambda body: body.units[0].evidence_ids.clear(), "UNBOUND_EVIDENCE"),
        (lambda body: setattr(body.units[0], "dimension", "money"), "UNIT_KIND_CONFLICT"),
    ],
)
def test_contract_graph_failures_are_reported(mutate, expected: str) -> None:
    body = make_contract()
    mutate(body)
    assert expected in issue_codes(body)


def test_overlap_without_override_is_blocking() -> None:
    body = make_contract()
    body.policy_units[1].overrides_unit_ids = []
    body.policy_units[0].effective_to = body.policy_units[1].effective_from
    assert "OVERLAPPING_VERSIONS" in issue_codes(body)


def test_nonblocking_contract_warning_does_not_invalidate_contract() -> None:
    body = make_contract()
    body.warnings.append(ContractWarning(code="LIMIT", severity="warning", message="需要人工关注措辞。", evidence_ids=["E_NEW"]))
    report = validate_contract(body, make_documents())
    assert report.valid


def test_unsupported_policy_condition_cannot_be_only_a_nonblocking_warning() -> None:
    body = make_contract()
    body.warnings.append(
        ContractWarning(
            code="UNSUPPORTED_GT_PREDICATE",
            severity="warning",
            message="A policy threshold cannot be evaluated.",
            evidence_ids=["E_NEW"],
        )
    )
    assert "UNSUPPORTED_CONDITION" in issue_codes(body)


def test_next_day_window_validates_related_date_and_natural_day_unit() -> None:
    body = make_contract()
    body.fields.append(FieldDefinition(field_id="F_RECEIPT", display_name="签收日期", description="签收日期", value_kind="date", evidence_ids=["E_NEW"]))
    predicate = Predicate(field_id="F_DATE", operator="within_natural_days_from_next_day", related_field_id="F_RECEIPT", values=[QuantityValue(kind="duration", value="7", unit_code="NATURAL_DAY")])
    body.policy_units[1].applicability_any[0].all.append(predicate)
    assert "FIELD_KIND_CONFLICT" not in issue_codes(body)
    assert "INVALID_RELATIVE_DATE_PREDICATE" not in issue_codes(body)
    body.fields[-1].value_kind = "boolean"
    assert "INVALID_RELATIVE_DATE_PREDICATE" in issue_codes(body)
    body.fields[-1].value_kind = "date"
    predicate.values[0].unit_code = "MISSING"
    assert "INVALID_RELATIVE_DATE_PREDICATE" in issue_codes(body)


def test_case_eligibility_deadline_requires_an_evaluable_date_window() -> None:
    body = make_contract()
    body.policy_units[0].claims[0].canonical_text = "签收次日起7个自然日内可以申请退货。"
    assert "UNGUARDED_RELATIVE_DEADLINE" in issue_codes(body)
    body.fields.append(FieldDefinition(field_id="F_RECEIPT", display_name="签收日期", description="签收日期", value_kind="date", evidence_ids=["E_OLD"]))
    body.policy_units[0].applicability_any[0].all.append(Predicate(field_id="F_DATE", operator="within_natural_days_from_next_day", related_field_id="F_RECEIPT", values=[QuantityValue(kind="duration", value="7", unit_code="NATURAL_DAY")]))
    assert "UNGUARDED_RELATIVE_DEADLINE" not in issue_codes(body)


def test_event_before_cutoff_cannot_also_be_time_basis_from_cutoff() -> None:
    body = make_contract()
    old = body.policy_units[0]
    old.effective_from = date(2030, 1, 10)
    old.effective_to = None
    assert "UNSATISFIABLE_TIME_BASIS" in issue_codes(body)


def test_alternative_causes_cannot_be_one_boolean_fact() -> None:
    body = make_contract()
    body.fields.append(FieldDefinition(field_id="F_DAMAGE", display_name="是否进水或受潮", description="是否进水或受潮", aliases=["进水、受潮"], value_kind="boolean", evidence_ids=["E_NEW"]))
    body.policy_units[0].applicability_any[0].all.append(Predicate(field_id="F_DAMAGE", operator="eq", values=[{"kind": "boolean", "value": True}]))
    assert "COMPOSITE_BOOLEAN_REASONS" in issue_codes(body)


@pytest.mark.parametrize("action", ["拆机", "改装", "维修"])
def test_unauthorized_action_enum_alias_must_preserve_qualifier(action: str) -> None:
    body = make_contract()
    body.fields.append(
        FieldDefinition(
            field_id="F_UNAUTHORIZED_ACTION",
            display_name="免费保修排除原因",
            description="未经授权的操作",
            value_kind="enum",
            allowed_values=[EnumOption(code="unauthorized", display_name=f"未经授权{action}", aliases=[action])],
            evidence_ids=["E_NEW"],
        )
    )
    assert "UNSAFE_ENUM_QUALIFIER_ALIAS" in issue_codes(body)
    body.fields[-1].allowed_values[0].aliases = [f"未经授权{action}"]
    assert "UNSAFE_ENUM_QUALIFIER_ALIAS" not in issue_codes(body)


def test_claim_cannot_drop_shared_user_cause_from_alternative_effect() -> None:
    body = make_contract()
    documents = make_documents()
    documents.blocks[0].text = "因用户原因造成明显损坏或配件缺失的商品不适用无理由退货。"
    claim = body.policy_units[0].claims[0]
    claim.canonical_text = "配件缺失的商品不适用无理由退货。"
    claim.normalized_literals = []
    assert "LOST_CAUSAL_QUALIFIER" in {
        issue.code for issue in validate_contract(body, documents).issues
    }
    claim.canonical_text = "因用户原因造成配件缺失的商品不适用无理由退货。"
    assert "LOST_CAUSAL_QUALIFIER" not in {
        issue.code for issue in validate_contract(body, documents).issues
    }


def test_blocking_contract_warning_is_an_issue() -> None:
    body = make_contract()
    body.warnings.append(ContractWarning(code="RISK", severity="blocking", message="存在未解决冲突。", evidence_ids=["E_NEW"]))
    assert "BLOCKING_WARNING" in issue_codes(body)


def test_covered_topic_without_any_policy_unit_is_blocked() -> None:
    body = make_contract()
    body.policy_units.clear()

    assert "COVERED_TOPIC_WITHOUT_UNIT" in issue_codes(body)


def test_in_scope_uncovered_topic_does_not_require_a_policy_unit() -> None:
    body = make_contract()
    body.scope.append(
        Topic(
            topic_id="T_UNCOVERED",
            description="业务内未公布的办理日期",
            aliases=[],
            evidence_ids=["E_SCOPE"],
            disposition="in_scope_uncovered",
        )
    )

    assert "COVERED_TOPIC_WITHOUT_UNIT" not in issue_codes(body)


def test_optional_old_status_guard_cannot_also_be_a_required_field() -> None:
    body = make_contract()
    unit = body.policy_units[1]
    body.fields.append(
        FieldDefinition(
            field_id="F_OLD_STATUS_DATE",
            display_name="旧状态日期",
            description="仅在已知时排除新规则",
            aliases=[],
            value_kind="date",
            allowed_values=[],
            evidence_ids=["E_NEW"],
        )
    )
    unit.applicability_any = [
        ConditionGroup(
            all=[
                Predicate(
                    field_id="F_OLD_STATUS_DATE",
                    operator="not_before_if_present",
                    values=[DateValue(kind="date", value=date(2030, 1, 10))],
                )
            ]
        )
    ]
    assert "OPTIONAL_GUARD_REQUIRED" not in issue_codes(body)

    unit.required_field_ids.append("F_OLD_STATUS_DATE")
    assert "OPTIONAL_GUARD_REQUIRED" in issue_codes(body)


def test_numeric_source_fact_cannot_disappear_from_all_claims() -> None:
    from docqa.models.documents import EvidenceBlock

    body = make_contract()
    documents = make_documents()
    documents.blocks.append(
        EvidenceBlock(
            block_id="E_CAPACITY",
            document_id="D_TERM",
            document_name="期限规则.md",
            page=3,
            section_path=["容量"],
            ordinal=4,
            text="服务提供128个自然日的资料保留。",
            source_sha256="a" * 64,
        )
    )

    report = validate_contract(body, documents)

    assert any(
        issue.code == "NUMERIC_EVIDENCE_WITHOUT_CLAIM"
        and issue.object_ids == ["E_CAPACITY"]
        for issue in report.issues
    )

    body.policy_units[0].claims[0].evidence_ids.append("E_CAPACITY")
    report = validate_contract(body, documents)
    assert any(
        issue.code == "NUMERIC_EVIDENCE_WITHOUT_CLAIM"
        and issue.object_ids == ["E_CAPACITY"]
        for issue in report.issues
    )


def test_cache_key_is_path_independent_and_configuration_sensitive() -> None:
    first = make_documents()
    moved = make_documents()
    moved.documents[0].source_files[0].relative_path = "moved/期限规则.md"
    base = dict(pairing="paired", versions={"parser": "1", "validator": "1"}, prompt_hash="p1", model="deepseek-flash")
    assert cache_key(first, effort="high", **base) == cache_key(moved, effort="high", **base)
    assert cache_key(first, effort="high", **base) != cache_key(first, effort="max", **base)
    assert cache_key(first, effort="high", **base) != cache_key(first, effort="high", **{**base, "prompt_hash": "p2"})
    assert cache_key(first, effort="high", **base) != cache_key(first, effort="high", **{**base, "versions": {"parser": "1", "validator": "2"}})


def make_cached_contract(*, key: str, model: str = "deepseek-flash") -> tuple[DocumentContract, ReviewRecord]:
    body = make_contract()
    digest = canonical_body_sha256(body)
    manifest = ContractManifest(
        schema_version="1",
        parser_version="1",
        normalization_version="1",
        validator_version="1",
        prompt_sha256="p" * 64,
        model=model,
        effort="high",
        document_set_hash=make_documents().document_set_hash,
        cache_key=key,
        body_sha256=digest,
        generated_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    review = ReviewRecord(body_sha256=digest, status="auto_approved", reviewed_at=datetime(2030, 1, 1, tzinfo=timezone.utc), issues=[], manual_change_notes=[])
    return DocumentContract(manifest=manifest, body=body), review


def test_store_roundtrip_and_two_versions_coexist(tmp_path: Path) -> None:
    store = ContractStore(tmp_path)
    first, first_review = make_cached_contract(key="1" * 64)
    second, second_review = make_cached_contract(key="2" * 64, model="deepseek-flash-v2")
    store.save(first, first_review)
    store.save(second, second_review)
    assert store.load(first.manifest.document_set_hash, first.manifest.cache_key, documents=make_documents()) == first
    assert store.load(second.manifest.document_set_hash, second.manifest.cache_key, documents=make_documents()) == second


def test_tampered_body_or_review_is_not_reused(tmp_path: Path) -> None:
    store = ContractStore(tmp_path)
    contract, review = make_cached_contract(key="3" * 64)
    store.save(contract, review)
    cache_dir = store.cache_dir(contract.manifest.document_set_hash, contract.manifest.cache_key)
    body = json.loads((cache_dir / "body.json").read_text(encoding="utf-8"))
    body["locale"]["normalization_version"] = "tampered"
    (cache_dir / "body.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    assert store.load(contract.manifest.document_set_hash, contract.manifest.cache_key, documents=make_documents()) is None


def test_partial_save_never_has_a_ready_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ContractStore(tmp_path)
    contract, review = make_cached_contract(key="4" * 64)
    real_write = __import__("docqa.contract_store", fromlist=["atomic_write"]).atomic_write
    calls = 0

    def fail_second(path: Path, text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated write failure")
        real_write(path, text)

    monkeypatch.setattr("docqa.contract_store.atomic_write", fail_second)
    with pytest.raises(OSError, match="simulated"):
        store.save(contract, review)
    cache_dir = store.cache_dir(contract.manifest.document_set_hash, contract.manifest.cache_key)
    assert not (cache_dir / "ready.json").exists()
    assert store.load(contract.manifest.document_set_hash, contract.manifest.cache_key, documents=make_documents()) is None


def test_auto_approval_cannot_claim_unresolved_issues(tmp_path: Path) -> None:
    store = ContractStore(tmp_path)
    contract, review = make_cached_contract(key="5" * 64)
    review.issues = [Issue(code="RISK", message="仍需审核", object_ids=["U_NEW"])]
    with pytest.raises(ValueError, match="auto_approved"):
        store.save(contract, review)


def test_conflicting_unit_definition_is_detected() -> None:
    body = make_contract()
    body.units.append(UnitDefinition(unit_code="NATURAL_DAY", display_name="自然日", aliases=[], dimension="money", evidence_ids=["E_NEW"]))
    assert "DUPLICATE_ID" in issue_codes(body)


def test_ambiguous_unit_alias_is_reported_without_crashing_validation() -> None:
    body = make_contract()
    body.units.append(
        UnitDefinition(
            unit_code="BUSINESS_DAY",
            display_name="工作日",
            aliases=["天"],
            dimension="duration",
            evidence_ids=["E_NEW"],
        )
    )

    assert "AMBIGUOUS_UNIT_ALIAS" in issue_codes(body)


def test_claim_qualifier_must_be_part_of_canonical_text() -> None:
    body = make_contract()
    body.policy_units[1].claims[0].qualifiers = ["仅在材料齐备时适用"]

    assert "CLAIM_QUALIFIER_NOT_CANONICAL" in issue_codes(body)


def test_claim_qualifier_must_be_supported_by_claim_evidence() -> None:
    body = make_contract()
    documents = make_documents()
    next(block for block in documents.blocks if block.block_id == "E_NEW").text = (
        "2030年1月10日至19日申请的办理期限为8个自然日；"
        "自2030年1月20日起为12个自然日。"
    )

    codes = {
        issue.code for issue in validate_contract(body, documents).issues
    }

    assert "CLAIM_QUALIFIER_NOT_EVIDENCED" in codes


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_claim_canonical_literals_must_match_normalized_literals(
    mutation: str,
) -> None:
    body = make_contract()
    claim = body.policy_units[1].claims[0]
    if mutation == "missing":
        claim.normalized_literals = []
    else:
        claim.canonical_text = "材料齐备时，可以办理。"

    assert "CLAIM_LITERAL_MISMATCH" in issue_codes(body)
