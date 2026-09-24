from __future__ import annotations

from datetime import date

import pytest

from docqa.models.common import BoolValue, DateValue, QuantityValue
from docqa.models.contract import (
    Claim,
    ConditionGroup,
    FieldDefinition,
    PolicyUnit,
    Predicate,
    TimeBasis,
)
from docqa.rule_engine import (
    Truth,
    and_values,
    evaluate_predicate,
    evaluate_rules,
    or_values,
)
from tests.fixtures.builders import make_analysis, make_contract


def term_result(contract, analysis, as_of=date(2026, 9, 20)):
    return evaluate_rules(contract, analysis, as_of).intents["I_TERM"]


def test_false_branch_short_circuits_missing_fields() -> None:
    assert and_values([Truth.FALSE, Truth.UNKNOWN]) == Truth.FALSE
    assert and_values([Truth.TRUE, Truth.UNKNOWN]) == Truth.UNKNOWN
    assert or_values([Truth.TRUE, Truth.UNKNOWN]) == Truth.TRUE


def test_empty_condition_list_is_unconditional() -> None:
    assert and_values([]) == Truth.TRUE


@pytest.mark.parametrize(
    ("event_date", "expected"),
    [
        (date(2030, 1, 9), {"U_OLD"}),
        (date(2030, 1, 10), {"U_NEW"}),
        (date(2030, 1, 11), {"U_NEW"}),
    ],
)
def test_event_date_selects_policy_boundary(event_date, expected) -> None:
    result = term_result(make_contract(), make_analysis(event_date=event_date))
    assert result.active_unit_ids == expected


def test_event_date_not_reference_date_selects_new_rule() -> None:
    result = term_result(
        make_contract(),
        make_analysis(event_date=date(2030, 1, 10)),
        as_of=date(2026, 9, 20),
    )
    assert result.active_unit_ids == {"U_NEW"}


def test_missing_event_date_keeps_both_versions_unknown() -> None:
    result = term_result(make_contract(), make_analysis(event_date=None))
    assert result.active_unit_ids == set()
    assert result.unknown_unit_ids == {"U_OLD", "U_NEW"}
    assert result.missing_field_ids == {"F_DATE"}


def test_irrelevant_family_does_not_add_missing_fields() -> None:
    result = term_result(
        make_contract(include_irrelevant_family=True),
        make_analysis(event_date=date(2030, 1, 10)),
    )
    assert result.active_unit_ids == {"U_NEW"}
    assert "F_REFUND_DATE" not in result.missing_field_ids


def test_unselected_unit_in_same_family_does_not_add_missing_fields() -> None:
    """Catch family-wide candidate expansion that poisons a selected claim."""

    contract = make_contract()
    contract.fields.append(
        FieldDefinition(
            field_id="F_OPTION",
            display_name="另一互斥条件",
            description="只决定同一规则族中的另一个独立结论",
            aliases=[],
            value_kind="boolean",
            allowed_values=[],
            evidence_ids=["E_NEW"],
        )
    )
    contract.policy_units.append(
        PolicyUnit(
            unit_id="U_OPTION",
            family_id="G_TERM",
            topic_ids=["T_LOAN"],
            kind="policy",
            applicability_any=[
                ConditionGroup(
                    all=[
                        Predicate(
                            field_id="F_OPTION",
                            operator="eq",
                            values=[BoolValue(kind="boolean", value=True)],
                        )
                    ]
                )
            ],
            required_field_ids=["F_OPTION"],
            time_basis=TimeBasis(kind="none", field_id=None),
            effective_from=None,
            effective_to=None,
            claims=[
                Claim(
                    claim_id="C_OPTION",
                    canonical_text="另一互斥条件满足时适用独立规则。",
                    normalized_literals=[],
                    evidence_ids=["E_NEW"],
                    qualifiers=[],
                    required_companion_claim_ids=[],
                )
            ],
            overrides_unit_ids=[],
        )
    )

    result = term_result(
        contract, make_analysis(event_date=date(2030, 1, 10))
    )

    assert result.active_unit_ids == {"U_NEW"}
    assert result.unknown_unit_ids == set()
    assert "F_OPTION" not in result.missing_field_ids


def test_incoming_override_of_selected_claim_remains_a_candidate() -> None:
    """Catch claim filtering that would let a caller cherry-pick an old rule."""

    analysis = make_analysis(event_date=date(2030, 1, 10))
    analysis.intents[0].claim_ids = ["C_OLD"]

    result = term_result(make_contract(), analysis)

    assert result.active_unit_ids == {"U_NEW"}


def test_rule_description_does_not_require_personal_event_date() -> None:
    result = term_result(
        make_contract(),
        make_analysis(mode="rule_description", event_date=None),
    )
    assert result.missing_field_ids == set()
    assert result.conditioned_claim_ids == {"C_OLD", "C_NEW"}


def test_case_application_requires_conclusion_changing_date() -> None:
    result = term_result(
        make_contract(),
        make_analysis(mode="case_application", event_date=None),
    )
    assert result.missing_field_ids == {"F_DATE"}


def test_three_version_override_chain_keeps_only_newest_true_unit() -> None:
    result = term_result(
        make_contract(three_version_chain=True),
        make_analysis(event_date=date(2030, 1, 20)),
    )
    assert result.active_unit_ids == {"U_NEW"}


def test_predicate_comparisons_preserve_types_and_units() -> None:
    facts = {
        "F_DATE": DateValue(kind="date", value=date(2030, 1, 10)),
        "F_READY": BoolValue(kind="boolean", value=True),
        "F_TERM": QuantityValue(kind="duration", value="8", unit_code="NATURAL_DAY"),
    }
    assert evaluate_predicate(
        Predicate(
            field_id="F_DATE",
            operator="on_or_after",
            values=[DateValue(kind="date", value=date(2030, 1, 10))],
        ),
        facts,
    ) == Truth.TRUE
    assert evaluate_predicate(
        Predicate(
            field_id="F_TERM",
            operator="eq",
            values=[QuantityValue(kind="duration", value="8", unit_code="NATURAL_DAY")],
        ),
        facts,
    ) == Truth.TRUE
    assert evaluate_predicate(
        Predicate(
            field_id="F_TERM",
            operator="eq",
            values=[QuantityValue(kind="duration", value="8", unit_code="WORKING_DAY")],
        ),
        facts,
    ) == Truth.UNKNOWN


def test_decimal_quantity_text_uses_numeric_equality() -> None:
    facts = {
        "F_TERM": QuantityValue(
            kind="duration", value="8.0", unit_code="NATURAL_DAY"
        )
    }

    result = evaluate_predicate(
        Predicate(
            field_id="F_TERM",
            operator="eq",
            values=[
                QuantityValue(
                    kind="duration", value="8", unit_code="NATURAL_DAY"
                )
            ],
        ),
        facts,
    )

    assert result == Truth.TRUE


def test_missing_exists_is_false_but_not_exists_stays_unknown() -> None:
    assert evaluate_predicate(
        Predicate(field_id="F_DATE", operator="exists", values=[]), {}
    ) == Truth.FALSE
    assert evaluate_predicate(
        Predicate(field_id="F_DATE", operator="not_exists", values=[]), {}
    ) == Truth.UNKNOWN


def test_false_condition_makes_other_missing_predicate_irrelevant() -> None:
    contract = make_contract()
    contract.fields.append(
        FieldDefinition(
            field_id="F_OPTION",
            display_name="附加条件",
            description="仅旧规则使用的附加条件",
            aliases=[],
            value_kind="boolean",
            allowed_values=[],
            evidence_ids=["E_OLD"],
        )
    )
    contract.policy_units[0].applicability_any[0].all.append(
        Predicate(
            field_id="F_OPTION",
            operator="eq",
            values=[BoolValue(kind="boolean", value=True)],
        )
    )

    result = term_result(
        contract, make_analysis(event_date=date(2030, 1, 10))
    )

    assert result.active_unit_ids == {"U_NEW"}
    assert "F_OPTION" not in result.missing_field_ids


def test_unknown_new_version_prevents_fallback_to_true_old_version() -> None:
    contract = make_contract()
    old, new = contract.policy_units
    old.applicability_any = []
    old.required_field_ids = []
    old.time_basis = TimeBasis(kind="none", field_id=None)
    old.effective_to = None
    new.applicability_any = [
        ConditionGroup(
            all=[
                Predicate(
                    field_id="F_DATE",
                    operator="on_or_after",
                    values=[DateValue(kind="date", value=date(2030, 1, 10))],
                )
            ]
        )
    ]

    result = term_result(contract, make_analysis(event_date=None))

    assert result.active_unit_ids == set()
    assert result.unknown_unit_ids == {"U_OLD", "U_NEW"}
    assert result.missing_field_ids == {"F_DATE"}


def test_reference_date_time_basis_uses_as_of_not_event_fact() -> None:
    contract = make_contract()
    unit = contract.policy_units[1]
    contract.policy_units = [unit]
    unit.applicability_any = []
    unit.required_field_ids = []
    unit.time_basis = TimeBasis(kind="reference_date", field_id=None)

    before = term_result(
        contract,
        make_analysis(event_date=date(2040, 1, 1)),
        as_of=date(2030, 1, 9),
    )
    active = term_result(
        contract,
        make_analysis(event_date=date(2020, 1, 1)),
        as_of=date(2030, 1, 10),
    )

    assert before.active_unit_ids == set()
    assert active.active_unit_ids == {"U_NEW"}


def test_none_time_basis_does_not_apply_effective_window() -> None:
    contract = make_contract()
    unit = contract.policy_units[1]
    contract.policy_units = [unit]
    unit.applicability_any = []
    unit.required_field_ids = []
    unit.time_basis = TimeBasis(kind="none", field_id=None)

    result = term_result(
        contract, make_analysis(event_date=None), as_of=date(2020, 1, 1)
    )

    assert result.active_unit_ids == {"U_NEW"}


def test_type_mismatch_is_reported_as_conflict() -> None:
    analysis = make_analysis(event_date=date(2030, 1, 10))
    analysis.extracted_facts[0].value = BoolValue(kind="boolean", value=True)

    result = term_result(make_contract(), analysis)

    assert "TYPE_OR_UNIT_MISMATCH:F_DATE" in result.conflicts
    assert result.active_unit_ids == set()


def test_required_companion_claim_adds_its_unit_to_candidate_closure() -> None:
    contract = make_contract()
    contract.policy_units[1].claims[0].required_companion_claim_ids = ["C_COMPANION"]
    contract.policy_units.append(
        PolicyUnit(
            unit_id="U_COMPANION",
            family_id="G_COMPANION",
            topic_ids=["T_LOAN"],
            kind="fact",
            applicability_any=[],
            required_field_ids=[],
            time_basis=TimeBasis(kind="none", field_id=None),
            effective_from=None,
            effective_to=None,
            claims=[
                Claim(
                    claim_id="C_COMPANION",
                    canonical_text="该期限以材料齐备为前提。",
                    normalized_literals=[],
                    evidence_ids=["E_NEW"],
                    qualifiers=["材料齐备"],
                    required_companion_claim_ids=[],
                )
            ],
            overrides_unit_ids=[],
        )
    )

    result = term_result(
        contract, make_analysis(event_date=date(2030, 1, 10))
    )

    assert result.active_unit_ids == {"U_NEW", "U_COMPANION"}
    assert result.conditioned_claim_ids == {"C_NEW", "C_COMPANION"}
