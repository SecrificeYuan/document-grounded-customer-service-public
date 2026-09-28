"""Deterministic graph and evidence validation for generated contracts."""

from __future__ import annotations

import unicodedata
import re
from collections import Counter
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Iterable

from docqa.models.common import Issue, ValidationReport
from docqa.models.contract import ContractBody, PolicyUnit
from docqa.models.documents import DocumentSet
from docqa.zh_normalization import extract_literals, normalize_surface


def _issue(code: str, message: str, *object_ids: str) -> Issue:
    return Issue(code=code, message=message, object_ids=list(object_ids))


def _duplicates(values: Iterable[str]) -> set[str]:
    counts = Counter(values)
    return {value for value, count in counts.items() if count > 1}


def _overlap(left: PolicyUnit, right: PolicyUnit) -> bool:
    left_start = left.effective_from or date.min
    left_end = left.effective_to or date.max
    right_start = right.effective_from or date.min
    right_end = right.effective_to or date.max
    return max(left_start, right_start) <= min(left_end, right_end)


def validate_contract(body: ContractBody, documents: DocumentSet) -> ValidationReport:
    """Return every deterministic contract defect that can be proven locally."""

    issues: list[Issue] = []
    evidence = {block.block_id: block for block in documents.blocks}
    source_documents = {document.document_id for document in documents.documents}

    identity_groups = {
        "document": [item.document_id for item in body.documents],
        "topic": [item.topic_id for item in body.scope],
        "field": [item.field_id for item in body.fields],
        "unit_definition": [item.unit_code for item in body.units],
        "policy_unit": [item.unit_id for item in body.policy_units],
        "claim": [claim.claim_id for unit in body.policy_units for claim in unit.claims],
    }
    for kind, values in identity_groups.items():
        for duplicate in sorted(_duplicates(values)):
            issues.append(_issue("DUPLICATE_ID", f"duplicate {kind} ID", duplicate))

    topics = {item.topic_id: item for item in body.scope}
    fields = {item.field_id: item for item in body.fields}
    unit_definitions = {item.unit_code: item for item in body.units}
    literal_units = [
        SimpleNamespace(
            unit_code=unit.unit_code,
            aliases=[unit.display_name, f"个{unit.display_name}", *unit.aliases],
            dimension=unit.dimension,
        )
        for unit in body.units
    ]
    alias_owners: dict[str, set[tuple[int, str]]] = {}
    for index, unit in enumerate(body.units):
        for raw_alias in (unit.display_name, f"个{unit.display_name}", *unit.aliases):
            alias = unicodedata.normalize("NFKC", raw_alias)
            alias_owners.setdefault(alias, set()).add((index, unit.unit_code))
    ambiguous_aliases = {
        alias: owners for alias, owners in alias_owners.items() if len(owners) > 1
    }
    for alias, owners in sorted(ambiguous_aliases.items()):
        issues.append(
            _issue(
                "AMBIGUOUS_UNIT_ALIAS",
                "unit alias belongs to more than one unit definition",
                alias,
                *(unit_code for _, unit_code in sorted(owners)),
            )
        )
    policy_units = {item.unit_id: item for item in body.policy_units}
    claims = {claim.claim_id: claim for unit in body.policy_units for claim in unit.claims}
    topics_with_units = {
        topic_id for unit in body.policy_units for topic_id in unit.topic_ids
    }
    for topic in body.scope:
        if topic.disposition == "covered" and topic.topic_id not in topics_with_units:
            issues.append(
                _issue(
                    "COVERED_TOPIC_WITHOUT_UNIT",
                    "covered topic has no policy unit or claim",
                    topic.topic_id,
                )
            )

    for document in body.documents:
        if document.document_id not in source_documents:
            issues.append(_issue("UNKNOWN_DOCUMENT", "contract document is not in the source set", document.document_id))
        if document.effective_from and document.effective_to and document.effective_from > document.effective_to:
            issues.append(_issue("INVALID_DATE_RANGE", "contract document date range is reversed", document.document_id))

    evidence_owners = [
        *(('topic', item.topic_id, item.evidence_ids) for item in body.scope),
        *(('field', item.field_id, item.evidence_ids) for item in body.fields),
        *(('unit', item.unit_code, item.evidence_ids) for item in body.units),
        *(('claim', claim.claim_id, claim.evidence_ids) for unit in body.policy_units for claim in unit.claims),
        *(('warning', warning.code, warning.evidence_ids) for warning in body.warnings),
    ]
    for kind, owner_id, evidence_ids in evidence_owners:
        if kind in {"topic", "field", "unit", "claim"} and not evidence_ids:
            issues.append(_issue("UNBOUND_EVIDENCE", f"{kind} has no evidence", owner_id))
        for evidence_id in evidence_ids:
            if evidence_id not in evidence:
                issues.append(_issue("UNKNOWN_EVIDENCE", f"{kind} references unknown evidence", owner_id, evidence_id))

    if not ambiguous_aliases:
        claims_by_evidence: dict[str, list] = {}
        for policy_unit in body.policy_units:
            for claim in policy_unit.claims:
                for evidence_id in claim.evidence_ids:
                    claims_by_evidence.setdefault(evidence_id, []).append(claim)
        for block in documents.blocks:
            source_literals = {
                (value.kind, Decimal(value.value), value.unit_code)
                for value in extract_literals(block.text, literal_units)
            }
            if not source_literals:
                continue
            matching_claim = any(
                source_literals
                & {
                    (value.kind, Decimal(value.value), value.unit_code)
                    for value in extract_literals(claim.canonical_text, literal_units)
                }
                for claim in claims_by_evidence.get(block.block_id, [])
            )
            if not matching_claim:
                issues.append(
                    _issue(
                        "NUMERIC_EVIDENCE_WITHOUT_CLAIM",
                        "source block has no claim for its numeric fact",
                        block.block_id,
                    )
                )

    predicate_field_ids = {
        predicate.field_id
        for unit in body.policy_units
        for group in unit.applicability_any
        for predicate in group.all
    }
    for field in body.fields:
        if (
            field.value_kind == "boolean"
            and field.field_id in predicate_field_ids
            and any("、" in label or "或" in label for label in (field.display_name, *field.aliases))
        ):
            issues.append(_issue("COMPOSITE_BOOLEAN_REASONS", "alternative causes need independently parseable values and OR branches", field.field_id))
        if field.value_kind == "enum":
            codes = [option.code for option in field.allowed_values]
            if not codes or _duplicates(codes):
                issues.append(_issue("INVALID_ENUM_DEFINITION", "enum field requires unique allowed values", field.field_id))
            for option in field.allowed_values:
                if "未经授权" in normalize_surface(option.display_name) and any(
                    "未经授权" not in normalize_surface(alias) for alias in option.aliases
                ):
                    issues.append(
                        _issue(
                            "UNSAFE_ENUM_QUALIFIER_ALIAS",
                            "alias drops the unauthorized qualifier from an exclusion reason",
                            field.field_id,
                            option.code,
                        )
                    )
        elif field.allowed_values:
            issues.append(_issue("INVALID_ENUM_DEFINITION", "non-enum field cannot declare allowed values", field.field_id))

    for unit in body.policy_units:
        for claim in unit.claims:
            deadline = re.search(r"次日起\s*(\d+)\s*个自然日内.*?申请", normalize_surface(claim.canonical_text))
            if deadline and (
                not unit.applicability_any
                or any(
                    not any(
                        predicate.operator == "within_natural_days_from_next_day"
                        and Decimal(predicate.values[0].value) == Decimal(deadline.group(1))
                        for predicate in group.all
                    )
                    for group in unit.applicability_any
                )
            ):
                issues.append(_issue("UNGUARDED_RELATIVE_DEADLINE", "case eligibility claim lacks an evaluable next-day natural-day window", unit.unit_id, claim.claim_id))
        if unit.effective_from and unit.effective_to and unit.effective_from > unit.effective_to:
            issues.append(_issue("INVALID_DATE_RANGE", "policy unit date range is reversed", unit.unit_id))
        for topic_id in unit.topic_ids:
            if topic_id not in topics:
                issues.append(_issue("UNKNOWN_TOPIC", "policy unit references unknown topic", unit.unit_id, topic_id))
        for field_id in unit.required_field_ids:
            if field_id not in fields:
                issues.append(_issue("UNKNOWN_FIELD", "policy unit requires unknown field", unit.unit_id, field_id))
        if unit.time_basis.kind == "field":
            field = fields.get(unit.time_basis.field_id or "")
            if field is None or not field.evidence_ids:
                issues.append(_issue("INVALID_TIME_BASIS", "time basis field is missing or lacks evidence", unit.unit_id, unit.time_basis.field_id or ""))
            if unit.effective_from is not None:
                for group in unit.applicability_any:
                    for predicate in group.all:
                        if (
                            predicate.field_id == unit.time_basis.field_id
                            and predicate.operator == "before"
                            and predicate.values[0].kind == "date"
                            and predicate.values[0].value <= unit.effective_from
                        ):
                            issues.append(_issue("UNSATISFIABLE_TIME_BASIS", "event date must be before the effective start but is also the time basis", unit.unit_id, predicate.field_id))

        optional_guard_fields = {
            predicate.field_id
            for group in unit.applicability_any
            for predicate in group.all
            if predicate.operator == "not_before_if_present"
        }
        for field_id in sorted(optional_guard_fields.intersection(unit.required_field_ids)):
            issues.append(
                _issue(
                    "OPTIONAL_GUARD_REQUIRED",
                    "optional old-status guard is also listed as a required field",
                    unit.unit_id,
                    field_id,
                )
            )

        for group in unit.applicability_any:
            for predicate in group.all:
                field = fields.get(predicate.field_id)
                if field is None:
                    issues.append(_issue("UNKNOWN_FIELD", "predicate references unknown field", unit.unit_id, predicate.field_id))
                    continue
                if predicate.operator == "within_natural_days_from_next_day":
                    related = fields.get(predicate.related_field_id or "")
                    value = predicate.values[0]
                    duration_unit = unit_definitions.get(value.unit_code)
                    if (
                        field.value_kind != "date"
                        or related is None
                        or related.value_kind != "date"
                        or not related.evidence_ids
                        or duration_unit is None
                        or duration_unit.dimension != "duration"
                        or "自然日" not in {
                            normalize_surface(alias)
                            for alias in (duration_unit.display_name, *duration_unit.aliases)
                        }
                    ):
                        issues.append(_issue("INVALID_RELATIVE_DATE_PREDICATE", "relative date window requires two evidenced date fields and a natural-day duration unit", unit.unit_id, predicate.field_id, predicate.related_field_id or ""))
                    continue
                for value in predicate.values:
                    if value.kind != field.value_kind:
                        issues.append(_issue("FIELD_KIND_CONFLICT", "predicate value kind differs from field definition", unit.unit_id, predicate.field_id))
                    if value.kind == "enum":
                        allowed = {option.code for option in field.allowed_values}
                        if value.value not in allowed:
                            issues.append(_issue("UNKNOWN_ENUM_VALUE", "predicate enum value is not allowed", unit.unit_id, predicate.field_id))
                if predicate.operator == "between" and len(predicate.values) == 2:
                    if predicate.values[0].value > predicate.values[1].value:
                        issues.append(_issue("INVALID_DATE_RANGE", "between bounds are reversed", unit.unit_id, predicate.field_id))

        for overridden_id in unit.overrides_unit_ids:
            overridden = policy_units.get(overridden_id)
            if overridden is None:
                issues.append(_issue("UNKNOWN_OVERRIDE", "override target does not exist", unit.unit_id, overridden_id))
            elif overridden.family_id != unit.family_id:
                issues.append(_issue("CROSS_FAMILY_OVERRIDE", "override target belongs to another family", unit.unit_id, overridden_id))
        for claim in unit.claims:
            canonical_surface = normalize_surface(claim.canonical_text)
            expected_literals = {
                (literal.kind, Decimal(literal.value), literal.unit_code)
                for literal in claim.normalized_literals
                if literal.kind in {"duration", "money", "quantity"}
            }
            if not ambiguous_aliases:
                canonical_literals = {
                    (literal.kind, Decimal(literal.value), literal.unit_code)
                    for literal in extract_literals(claim.canonical_text, literal_units)
                }
                if canonical_literals != expected_literals:
                    issues.append(
                        _issue(
                            "CLAIM_LITERAL_MISMATCH",
                            "claim canonical literals differ from normalized literals",
                            claim.claim_id,
                        )
                    )
            evidence_surfaces = [
                normalize_surface(evidence[evidence_id].text)
                for evidence_id in claim.evidence_ids
                if evidence_id in evidence
            ]
            for source in evidence_surfaces:
                for causal_rule in re.finditer(
                    r"因(?P<cause>[^，。；;\n]{2,12}?)造成(?P<effects>[^，。；;\n]+)",
                    source,
                ):
                    cause = causal_rule.group("cause")
                    effects = re.split(r"[、或]", causal_rule.group("effects"))
                    for effect in effects:
                        effect = re.split(r"[的，。；;\n]", effect.strip(), maxsplit=1)[0]
                        if len(effect) >= 2 and effect in canonical_surface and cause not in canonical_surface:
                            issues.append(
                                _issue(
                                    "LOST_CAUSAL_QUALIFIER",
                                    "claim drops a shared causal condition from an alternative effect",
                                    claim.claim_id,
                                )
                            )
                            break
            for qualifier in claim.qualifiers:
                qualifier_surface = normalize_surface(qualifier)
                if not qualifier_surface or qualifier_surface not in canonical_surface:
                    issues.append(
                        _issue(
                            "CLAIM_QUALIFIER_NOT_CANONICAL",
                            "claim qualifier is not contained in canonical text",
                            claim.claim_id,
                        )
                    )
                if not qualifier_surface or not any(
                    qualifier_surface in source for source in evidence_surfaces
                ):
                    issues.append(
                        _issue(
                            "CLAIM_QUALIFIER_NOT_EVIDENCED",
                            "claim qualifier is not supported by claim evidence",
                            claim.claim_id,
                        )
                    )
            for companion_id in claim.required_companion_claim_ids:
                if companion_id not in claims:
                    issues.append(_issue("UNKNOWN_COMPANION", "required companion claim does not exist", claim.claim_id, companion_id))
            for literal in claim.normalized_literals:
                if literal.kind in {"duration", "money", "quantity"}:
                    definition = unit_definitions.get(literal.unit_code)
                    if definition is None:
                        issues.append(_issue("UNKNOWN_UNIT", "claim references unknown unit code", claim.claim_id, literal.unit_code))
                    elif definition.dimension != literal.kind:
                        issues.append(_issue("UNIT_KIND_CONFLICT", "unit dimension conflicts with normalized value", claim.claim_id, literal.unit_code))

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(unit_id: str, trail: list[str]) -> None:
        if unit_id in visiting:
            start = trail.index(unit_id) if unit_id in trail else 0
            cycle = trail[start:] + [unit_id]
            issues.append(_issue("OVERRIDE_CYCLE", "override graph contains a cycle", *cycle))
            return
        if unit_id in visited:
            return
        visiting.add(unit_id)
        trail.append(unit_id)
        for target in policy_units[unit_id].overrides_unit_ids:
            if target in policy_units:
                visit(target, trail)
        trail.pop()
        visiting.remove(unit_id)
        visited.add(unit_id)

    for unit_id in policy_units:
        visit(unit_id, [])

    by_family: dict[str, list[PolicyUnit]] = {}
    for unit in body.policy_units:
        by_family.setdefault(unit.family_id, []).append(unit)
    for family_units in by_family.values():
        for index, left in enumerate(family_units):
            for right in family_units[index + 1 :]:
                linked = right.unit_id in left.overrides_unit_ids or left.unit_id in right.overrides_unit_ids
                if _overlap(left, right) and not linked:
                    issues.append(_issue("OVERLAPPING_VERSIONS", "overlapping versions lack an explicit override", left.unit_id, right.unit_id))

    for warning in body.warnings:
        if warning.code.startswith("UNSUPPORTED_"):
            issues.append(_issue("UNSUPPORTED_CONDITION", warning.message, warning.code))
        if warning.severity == "blocking":
            issues.append(_issue("BLOCKING_WARNING", warning.message, warning.code))

    unique: list[Issue] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for item in issues:
        key = (item.code, tuple(item.object_ids))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return ValidationReport(issues=unique)
