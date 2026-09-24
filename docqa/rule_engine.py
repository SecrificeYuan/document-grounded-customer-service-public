"""Intent-scoped three-valued evaluation of contract policy rules."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from docqa.models.analysis import AnalysisResult, Intent
from docqa.models.common import NormalizedValue
from docqa.models.contract import ContractBody, PolicyUnit, Predicate


class Truth(Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


def and_values(values: Iterable[Truth]) -> Truth:
    materialized = list(values)
    if Truth.FALSE in materialized:
        return Truth.FALSE
    if Truth.UNKNOWN in materialized:
        return Truth.UNKNOWN
    return Truth.TRUE


def or_values(values: Iterable[Truth]) -> Truth:
    materialized = list(values)
    if Truth.TRUE in materialized:
        return Truth.TRUE
    if Truth.UNKNOWN in materialized:
        return Truth.UNKNOWN
    return Truth.FALSE


def _same_comparison_domain(left: NormalizedValue, right: NormalizedValue) -> bool:
    if left.kind != right.kind:
        return False
    if left.kind in {"duration", "money", "quantity"}:
        return left.unit_code == right.unit_code
    return True


def _semantic_equal(left: NormalizedValue, right: NormalizedValue) -> bool:
    if not _same_comparison_domain(left, right):
        return False
    if left.kind in {"decimal", "duration", "money", "quantity"}:
        return Decimal(left.value) == Decimal(right.value)
    return left == right


def evaluate_predicate(
    predicate: Predicate, facts: Mapping[str, NormalizedValue]
) -> Truth:
    """Evaluate one predicate without coercing types, units, or absence."""

    fact = facts.get(predicate.field_id)
    if predicate.operator == "exists":
        return Truth.TRUE if fact is not None else Truth.FALSE
    if predicate.operator == "not_exists":
        return Truth.FALSE if fact is not None else Truth.UNKNOWN
    if fact is None:
        return Truth.UNKNOWN
    if any(not _same_comparison_domain(fact, value) for value in predicate.values):
        return Truth.UNKNOWN

    values = predicate.values
    if predicate.operator == "eq":
        result = _semantic_equal(fact, values[0])
    elif predicate.operator == "neq":
        result = not _semantic_equal(fact, values[0])
    elif predicate.operator == "in":
        result = any(_semantic_equal(fact, value) for value in values)
    elif predicate.operator == "not_in":
        result = not any(_semantic_equal(fact, value) for value in values)
    elif predicate.operator == "before":
        result = fact.value < values[0].value
    elif predicate.operator == "on_or_after":
        result = fact.value >= values[0].value
    elif predicate.operator == "between":
        result = values[0].value <= fact.value <= values[1].value
    else:  # Models reject unknown operators; retain fail-closed behavior.
        return Truth.UNKNOWN
    return Truth.TRUE if result else Truth.FALSE


@dataclass(slots=True)
class IntentEvaluation:
    intent_id: str
    active_unit_ids: set[str] = field(default_factory=set)
    unknown_unit_ids: set[str] = field(default_factory=set)
    missing_field_ids: set[str] = field(default_factory=set)
    conflicts: list[str] = field(default_factory=list)
    conditioned_claim_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class RuleEvaluation:
    intents: dict[str, IntentEvaluation]


@dataclass(slots=True)
class _DetailedTruth:
    truth: Truth
    missing: set[str] = field(default_factory=set)
    conflicts: set[str] = field(default_factory=set)


def _predicate_detail(
    predicate: Predicate, facts: Mapping[str, NormalizedValue]
) -> _DetailedTruth:
    fact = facts.get(predicate.field_id)
    truth = evaluate_predicate(predicate, facts)
    missing: set[str] = set()
    conflicts: set[str] = set()
    if truth is Truth.UNKNOWN:
        if fact is None:
            missing.add(predicate.field_id)
        elif any(
            not _same_comparison_domain(fact, value)
            for value in predicate.values
        ):
            conflicts.add(f"TYPE_OR_UNIT_MISMATCH:{predicate.field_id}")
    return _DetailedTruth(truth, missing, conflicts)


def _and_details(values: Iterable[_DetailedTruth]) -> _DetailedTruth:
    items = list(values)
    truth = and_values(item.truth for item in items)
    if truth is Truth.FALSE:
        relevant = [item for item in items if item.truth is Truth.FALSE]
    else:
        relevant = [item for item in items if item.truth is not Truth.TRUE]
    return _DetailedTruth(
        truth,
        set().union(*(item.missing for item in relevant)) if relevant else set(),
        set().union(*(item.conflicts for item in relevant)) if relevant else set(),
    )


def _or_details(values: Iterable[_DetailedTruth]) -> _DetailedTruth:
    items = list(values)
    truth = or_values(item.truth for item in items)
    if truth is Truth.TRUE:
        relevant = [item for item in items if item.truth is Truth.TRUE]
    elif truth is Truth.UNKNOWN:
        relevant = [item for item in items if item.truth is Truth.UNKNOWN]
    else:
        relevant = items
    return _DetailedTruth(
        truth,
        set().union(*(item.missing for item in relevant)) if relevant else set(),
        set().union(*(item.conflicts for item in relevant)) if relevant else set(),
    )


def _time_window(
    unit: PolicyUnit,
    facts: Mapping[str, NormalizedValue],
    as_of: date,
) -> _DetailedTruth:
    if unit.time_basis.kind == "none":
        return _DetailedTruth(Truth.TRUE)
    if unit.time_basis.kind == "reference_date":
        value = as_of
    else:
        field_id = unit.time_basis.field_id or ""
        fact = facts.get(field_id)
        if fact is None:
            return _DetailedTruth(Truth.UNKNOWN, {field_id})
        if fact.kind not in {"date", "datetime"}:
            return _DetailedTruth(
                Truth.UNKNOWN, conflicts={f"INVALID_TIME_BASIS_VALUE:{field_id}"}
            )
        value = fact.value.date() if isinstance(fact.value, datetime) else fact.value
    if unit.effective_from is not None and value < unit.effective_from:
        return _DetailedTruth(Truth.FALSE)
    if unit.effective_to is not None and value > unit.effective_to:
        return _DetailedTruth(Truth.FALSE)
    return _DetailedTruth(Truth.TRUE)


def _unit_detail(
    unit: PolicyUnit,
    facts: Mapping[str, NormalizedValue],
    as_of: date,
) -> _DetailedTruth:
    if unit.applicability_any:
        conditions = _or_details(
            _and_details(
                _predicate_detail(predicate, facts) for predicate in group.all
            )
            for group in unit.applicability_any
        )
    else:
        conditions = _DetailedTruth(Truth.TRUE)
    required = _and_details(
        _DetailedTruth(Truth.TRUE)
        if field_id in facts
        else _DetailedTruth(Truth.UNKNOWN, {field_id})
        for field_id in unit.required_field_ids
    )
    return _and_details([conditions, required, _time_window(unit, facts, as_of)])


def _candidate_units(contract: ContractBody, intent: Intent) -> list[PolicyUnit]:
    units_by_id = {unit.unit_id: unit for unit in contract.policy_units}
    claim_owner = {
        claim.claim_id: unit
        for unit in contract.policy_units
        for claim in unit.claims
    }
    selected_claim_ids = {
        claim_id for claim_id in intent.claim_ids if claim_id in claim_owner
    }
    declared_companions = {
        companion_id
        for claim_id in selected_claim_ids
        for claim in claim_owner[claim_id].claims
        if claim.claim_id == claim_id
        for companion_id in claim.required_companion_claim_ids
    }
    primary_claim_ids = selected_claim_ids - declared_companions
    if not primary_claim_ids:
        primary_claim_ids = selected_claim_ids
    selected_roots = {
        claim_owner[claim_id].unit_id
        for claim_id in primary_claim_ids
    }
    if not selected_roots:
        selected_roots = {
            unit.unit_id
            for unit in contract.policy_units
            if unit.family_id in set(intent.family_ids)
        }
    roots = set(selected_roots)

    companion_claims = list(primary_claim_ids)
    if not companion_claims:
        companion_claims = [
            claim.claim_id
            for unit_id in roots
            for claim in units_by_id[unit_id].claims
        ]
    seen_claims = set(companion_claims)
    while companion_claims:
        claim_id = companion_claims.pop()
        claim = next(
            claim
            for claim in claim_owner[claim_id].claims
            if claim.claim_id == claim_id
        )
        for companion_id in claim.required_companion_claim_ids:
            if companion_id not in claim_owner or companion_id in seen_claims:
                continue
            seen_claims.add(companion_id)
            companion_claims.append(companion_id)
            roots.add(claim_owner[companion_id].unit_id)

    selected = set(roots)
    newer_frontier = list(selected_roots)
    while newer_frontier:
        unit_id = newer_frontier.pop()
        for candidate in contract.policy_units:
            if (
                unit_id in candidate.overrides_unit_ids
                and candidate.unit_id not in selected
            ):
                selected.add(candidate.unit_id)
                newer_frontier.append(candidate.unit_id)

    older_frontier = list(selected)
    while older_frontier:
        unit_id = older_frontier.pop()
        for target in units_by_id[unit_id].overrides_unit_ids:
            if target in units_by_id and target not in selected:
                selected.add(target)
                older_frontier.append(target)
    return [unit for unit in contract.policy_units if unit.unit_id in selected]


def _facts_for_intent(
    analysis: AnalysisResult, intent_id: str
) -> tuple[dict[str, NormalizedValue], list[str]]:
    facts: dict[str, NormalizedValue] = {}
    conflicts: list[str] = []
    for item in analysis.extracted_facts:
        if item.intent_ids and intent_id not in item.intent_ids:
            continue
        previous = facts.get(item.field_id)
        if previous is not None and previous != item.value:
            conflicts.append(f"CONFLICTING_FACT:{item.field_id}")
            continue
        facts[item.field_id] = item.value
    return facts, conflicts


def evaluate_rules(
    contract: ContractBody, analysis: AnalysisResult, as_of: date
) -> RuleEvaluation:
    """Evaluate only rule families named by each intent, independent of model picks."""

    evaluations: dict[str, IntentEvaluation] = {}
    for intent in analysis.intents:
        result = IntentEvaluation(intent_id=intent.intent_id)
        candidates = _candidate_units(contract, intent)
        if intent.mode == "rule_description":
            result.conditioned_claim_ids = {
                claim.claim_id for unit in candidates for claim in unit.claims
            }
            evaluations[intent.intent_id] = result
            continue

        facts, fact_conflicts = _facts_for_intent(analysis, intent.intent_id)
        result.conflicts.extend(fact_conflicts)
        details = {
            unit.unit_id: _unit_detail(unit, facts, as_of) for unit in candidates
        }
        result.active_unit_ids = {
            unit_id for unit_id, detail in details.items() if detail.truth is Truth.TRUE
        }
        result.unknown_unit_ids = {
            unit_id
            for unit_id, detail in details.items()
            if detail.truth is Truth.UNKNOWN
        }

        units_by_id = {unit.unit_id: unit for unit in candidates}
        for unit_id in list(result.active_unit_ids):
            for target in units_by_id[unit_id].overrides_unit_ids:
                result.active_unit_ids.discard(target)
                result.unknown_unit_ids.discard(target)
        for unit_id in list(result.unknown_unit_ids):
            for target in units_by_id[unit_id].overrides_unit_ids:
                if target in result.active_unit_ids:
                    result.active_unit_ids.discard(target)
                    result.unknown_unit_ids.add(target)

        result.missing_field_ids = set().union(
            *(details[unit_id].missing for unit_id in result.unknown_unit_ids)
        ) if result.unknown_unit_ids else set()
        detail_conflicts = set().union(
            *(detail.conflicts for detail in details.values())
        ) if details else set()
        result.conflicts.extend(sorted(detail_conflicts))
        result.conditioned_claim_ids = {
            claim.claim_id
            for unit in candidates
            if unit.unit_id in result.active_unit_ids
            for claim in unit.claims
        }
        evaluations[intent.intent_id] = result
    return RuleEvaluation(intents=evaluations)
