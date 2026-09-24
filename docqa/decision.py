"""Deterministic validation and decision gates for one model analysis."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from docqa.evidence import EvidenceIndex
from docqa.models.analysis import AnalysisResult, ExtractedFact
from docqa.models.common import Issue, ValidationReport
from docqa.models.contract import Claim, ContractBody, FieldDefinition
from docqa.models.output import OutputEvidence, OutputRecord, handoff
from docqa.rule_engine import RuleEvaluation
from docqa.zh_normalization import (
    extract_literals,
    normalize_surface,
    parse_date,
    parse_number,
    span_matches,
)


@dataclass(slots=True)
class GateResult:
    """Either a final output or repairable deterministic gate issues."""

    output: OutputRecord | None
    issues: list[Issue] = field(default_factory=list)


def _issue(code: str, message: str, *object_ids: str) -> Issue:
    return Issue(code=code, message=message, object_ids=list(object_ids))


def _surface_sentence(text: str) -> str:
    """Ignore only spacing and fixed punctuation when comparing answer coverage."""

    return re.sub(r"[\s，,。；;：:！？!?]+", "", text)


def _claim_literal_issues(
    assertion: str, claim: Claim, contract: ContractBody
) -> list[Issue]:
    actual = extract_literals(assertion, _unit_views(contract))
    expected_quantities = [
        value
        for value in claim.normalized_literals
        if value.kind in {"duration", "money", "quantity"}
    ]
    actual_keys = {
        (value.kind, Decimal(value.value), value.unit_code) for value in actual
    }
    expected_keys = {
        (value.kind, Decimal(value.value), value.unit_code)
        for value in expected_quantities
    }
    issues: list[Issue] = []
    if actual_keys != expected_keys:
        issues.append(
            _issue(
                "CLAIM_LITERAL_MISMATCH",
                "assertion numeric values or units do not match the selected claim",
                claim.claim_id,
            )
        )
    for qualifier in claim.qualifiers:
        if _surface_sentence(qualifier) not in _surface_sentence(assertion):
            issues.append(
                _issue(
                    "MISSING_CLAIM_QUALIFIER",
                    "assertion omits a required claim qualifier",
                    claim.claim_id,
                )
            )
    return issues


def _unit_views(contract: ContractBody) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            unit_code=unit.unit_code,
            aliases=[unit.display_name, f"个{unit.display_name}", *unit.aliases],
            dimension=unit.dimension,
        )
        for unit in contract.units
    ]


def _fact_source_matches(
    fact: ExtractedFact,
    definition: FieldDefinition,
    contract: ContractBody,
    as_of: date,
) -> bool:
    """Conservatively prove a typed value from every declared source span."""

    labels = [definition.display_name, *definition.aliases]
    for source in fact.sources:
        text = normalize_surface(source.text)
        kind = fact.value.kind
        uncertain = any(
            marker in text
            for marker in ("不是", "并非", "不", "未", "否", "非", "无", "未知", "不确定", "存疑", "尚待", "待确认", "无法判断")
        )
        if kind not in {"boolean", "enum"} and uncertain:
            return False
        if kind == "date":
            matched = parse_date(text, as_of) == fact.value.value
        elif kind == "datetime":
            matched = fact.value.value.isoformat() == text
        elif kind in {"integer", "decimal"}:
            parsed = parse_number(text)
            matched = parsed is not None and parsed == Decimal(str(fact.value.value))
        elif kind in {"duration", "money", "quantity"}:
            values = extract_literals(text, _unit_views(contract))
            matched = len(values) == 1 and any(
                value.kind == fact.value.kind
                and value.unit_code == fact.value.unit_code
                and Decimal(value.value) == Decimal(fact.value.value)
                for value in values
            )
        elif kind == "enum":
            option = next(
                (item for item in definition.allowed_values if item.code == fact.value.value),
                None,
            )
            matched = option is not None and any(
                normalize_surface(alias) == text
                for alias in [option.code, option.display_name, *option.aliases]
            )
        elif kind == "boolean":
            positives = {normalize_surface(label) for label in labels if label.strip()}
            for label in list(positives):
                if label.startswith("是否") and len(label) > 2:
                    positives.add(label[2:])
                if label.endswith("状态") and len(label) > 2:
                    positives.add(label[:-2])
            negatives = {
                form
                for label in positives
                for form in (
                    f"未{label}",
                    f"不{label}",
                    f"{label}为否",
                    f"{label}：否",
                    f"{label}:否",
                    f"{label[:-2]}不{label[-2:]}" if len(label) > 2 else f"未{label}",
                    f"{label[:-2]}未{label[-2:]}" if len(label) > 2 else f"未{label}",
                )
            }
            matched = text in (positives if fact.value.value else negatives)
        else:  # string
            matched = normalize_surface(str(fact.value.value)) == text
        if not matched:
            return False
    return True


def validate_analysis(
    record: object,
    analysis: AnalysisResult,
    contract: ContractBody,
    index: EvidenceIndex,
    as_of: date,
) -> ValidationReport:
    """Validate model-controlled IDs, spans, facts, claims, and exact quotes."""

    question = getattr(record, "question")
    issues: list[Issue] = []
    fields = {item.field_id: item for item in contract.fields}
    units = {item.unit_id: item for item in contract.policy_units}
    claims = {
        claim.claim_id: (unit, claim)
        for unit in contract.policy_units
        for claim in unit.claims
    }
    intent_ids = {intent.intent_id for intent in analysis.intents}
    family_ids = {unit.family_id for unit in contract.policy_units}

    if len(intent_ids) != len(analysis.intents):
        issues.append(_issue("DUPLICATE_INTENT_ID", "intent IDs must be unique"))
    for intent in analysis.intents:
        if not span_matches(question, intent.source):
            issues.append(_issue("INVALID_INTENT_SPAN", "intent span is not exact", intent.intent_id))
        for family_id in intent.family_ids:
            if family_id not in family_ids:
                issues.append(_issue("UNKNOWN_FAMILY_ID", "intent names an unknown family", family_id))
        for claim_id in intent.claim_ids:
            if claim_id not in claims:
                issues.append(_issue("UNKNOWN_CLAIM_ID", "intent names an unknown claim", claim_id))

    seen_facts: dict[tuple[str, tuple[str, ...]], object] = {}
    for fact in analysis.extracted_facts:
        definition = fields.get(fact.field_id)
        if definition is None:
            issues.append(_issue("UNKNOWN_FIELD_ID", "fact names an unknown field", fact.field_id))
        elif definition.value_kind != fact.value.kind:
            issues.append(_issue("FACT_KIND_MISMATCH", "fact kind does not match its field", fact.field_id))
        for intent_id in fact.intent_ids:
            if intent_id not in intent_ids:
                issues.append(_issue("UNKNOWN_INTENT_ID", "fact names an unknown intent", intent_id))
        for source in fact.sources:
            if not span_matches(question, source):
                issues.append(_issue("INVALID_FACT_SPAN", "fact span is not exact", fact.field_id))
        if definition is not None and not _fact_source_matches(
            fact, definition, contract, as_of
        ):
            issues.append(_issue("FACT_SOURCE_MISMATCH", "typed fact differs from its source", fact.field_id))
        key = (fact.field_id, tuple(sorted(fact.intent_ids)))
        previous = seen_facts.get(key)
        if previous is not None and previous != fact.value:
            issues.append(_issue("CONFLICTING_FACT", "duplicate facts disagree", fact.field_id))
        seen_facts[key] = fact.value

    for unresolved in analysis.unresolved_expressions:
        if not span_matches(question, unresolved.source):
            issues.append(_issue("INVALID_UNRESOLVED_SPAN", "unresolved span is not exact"))
        for intent_id in unresolved.intent_ids:
            if intent_id not in intent_ids:
                issues.append(_issue("UNKNOWN_INTENT_ID", "unresolved expression names an unknown intent", intent_id))
        for field_id in unresolved.field_ids:
            if field_id not in fields:
                issues.append(_issue("UNKNOWN_FIELD_ID", "unresolved expression names an unknown field", field_id))
    for field_id in analysis.missing_field_ids:
        if field_id not in fields:
            issues.append(_issue("UNKNOWN_FIELD_ID", "missing field is unknown", field_id))
    for unit_id in [*analysis.selected_unit_ids, *analysis.conflicting_unit_ids]:
        if unit_id not in units:
            issues.append(_issue("UNKNOWN_UNIT_ID", "analysis names an unknown policy unit", unit_id))

    for plan in analysis.claim_plan:
        owner_and_claim = claims.get(plan.claim_id)
        intent = next((item for item in analysis.intents if item.intent_id == plan.intent_id), None)
        unit = units.get(plan.unit_id)
        if intent is None:
            issues.append(_issue("UNKNOWN_INTENT_ID", "claim plan names an unknown intent", plan.intent_id))
        if unit is None:
            issues.append(_issue("UNKNOWN_UNIT_ID", "claim plan names an unknown unit", plan.unit_id))
        if owner_and_claim is None:
            issues.append(_issue("UNKNOWN_CLAIM_ID", "claim plan names an unknown claim", plan.claim_id))
            continue
        owner, claim = owner_and_claim
        if unit is None or owner.unit_id != plan.unit_id:
            issues.append(_issue("CLAIM_UNIT_MISMATCH", "claim does not belong to selected unit", plan.claim_id, plan.unit_id))
        if intent is not None and owner.family_id not in intent.family_ids:
            issues.append(_issue("INTENT_FAMILY_MISMATCH", "claim unit is outside the intent families", plan.intent_id, plan.unit_id))
        if intent is not None and plan.claim_id not in intent.claim_ids:
            issues.append(_issue("INTENT_CLAIM_MISMATCH", "claim was not selected for its intent", plan.intent_id, plan.claim_id))
        if plan.unit_id not in analysis.selected_unit_ids:
            issues.append(_issue("UNSELECTED_UNIT", "claim plan unit was not selected", plan.unit_id))
        if not set(plan.evidence_ids).issubset(set(claim.evidence_ids)):
            issues.append(_issue("CLAIM_EVIDENCE_MISMATCH", "evidence does not belong to selected claim", plan.claim_id))
        if any(quote.evidence_id not in plan.evidence_ids for quote in plan.quotes):
            issues.append(_issue("QUOTE_PLAN_MISMATCH", "quote is outside planned evidence", plan.claim_id))
        for quote in plan.quotes:
            try:
                index.quote(quote.evidence_id, quote.fragment)
            except (KeyError, ValueError):
                issues.append(_issue("INVALID_EXACT_QUOTE", "quote is not exact source text", quote.evidence_id))
        expected_quote_literals = {
            (value.kind, Decimal(value.value), value.unit_code)
            for value in claim.normalized_literals
            if value.kind in {"duration", "money", "quantity"}
        }
        if expected_quote_literals:
            quoted_literals = {
                (value.kind, Decimal(value.value), value.unit_code)
                for value in extract_literals(
                    " ".join(quote.fragment for quote in plan.quotes),
                    _unit_views(contract),
                )
            }
            if quoted_literals != expected_quote_literals:
                issues.append(
                    _issue(
                        "QUOTE_LITERAL_MISMATCH",
                        "quoted text does not support the selected claim literals",
                        claim.claim_id,
                    )
                )
        quote_surface = normalize_surface(" ".join(quote.fragment for quote in plan.quotes))
        missing_quote_qualifiers = [
            qualifier
            for qualifier in claim.qualifiers
            if normalize_surface(qualifier) not in quote_surface
        ]
        semantic_markers = (
            "不是",
            "并非",
            "最多",
            "至少",
            "上限",
            "下限",
            "首次响应",
            "最终解决",
        )
        canonical_markers = {
            marker for marker in semantic_markers if marker in claim.canonical_text
        }
        quote_markers = {marker for marker in semantic_markers if marker in quote_surface}
        if missing_quote_qualifiers or quote_markers != canonical_markers:
            issues.append(
                _issue(
                    "QUOTE_QUALIFIER_MISMATCH",
                    "quoted text does not preserve claim qualifiers and polarity",
                    claim.claim_id,
                )
            )
        issues.extend(_claim_literal_issues(plan.assertion_text, claim, contract))
        if _surface_sentence(plan.assertion_text) != _surface_sentence(claim.canonical_text):
            issues.append(
                _issue(
                    "ASSERTION_CLAIM_MISMATCH",
                    "assertion is not the contract's canonical claim text",
                    claim.claim_id,
                )
            )
        same_intent_claims = {
            item.claim_id for item in analysis.claim_plan if item.intent_id == plan.intent_id
        }
        missing_companions = set(claim.required_companion_claim_ids) - same_intent_claims
        if missing_companions:
            issues.append(_issue("MISSING_COMPANION_CLAIM", "required companion claims are absent", *sorted(missing_companions)))

    planned_answer = "".join(plan.assertion_text for plan in analysis.claim_plan)
    if analysis.claim_plan and _surface_sentence(planned_answer) != _surface_sentence(analysis.answer_draft):
        issues.append(_issue("UNPLANNED_ANSWER_TEXT", "answer draft contains text outside planned assertions"))
    return ValidationReport(issues=issues)


def _answer_output(
    record: object, analysis: AnalysisResult, index: EvidenceIndex
) -> OutputRecord:
    selected: dict[tuple[str, str, int, int, int, str], OutputEvidence] = {}
    for plan in analysis.claim_plan:
        for quote in plan.quotes:
            block = index.resolve(quote.evidence_id)
            key = (
                block.document_name,
                block.document_id,
                block.page,
                block.ordinal,
                block.text.find(quote.fragment),
                quote.fragment,
            )
            selected[key] = OutputEvidence(
                document=block.document_name,
                page=block.page,
                section="/".join(block.section_path) or "正文",
                quote=quote.fragment,
            )
    return OutputRecord(
        id=getattr(record, "id"),
        decision="answer",
        answer=analysis.answer_draft,
        reason_code=None,
        evidence=[selected[key] for key in sorted(selected)],
    )


def decide(
    record: object,
    analysis: AnalysisResult,
    rules: RuleEvaluation,
    contract: ContractBody,
    index: EvidenceIndex,
) -> GateResult:
    """Apply deterministic reason precedence and rule-to-claim binding."""

    claim_owners = {
        claim.claim_id: (unit, claim)
        for unit in contract.policy_units
        for claim in unit.claims
    }
    units = {unit.unit_id: unit for unit in contract.policy_units}
    scopes = {intent.scope for intent in analysis.intents}
    if scopes == {"out_of_scope"}:
        return GateResult(handoff(getattr(record, "id"), "OUT_OF_SCOPE"))
    if "out_of_scope" in scopes or "uncertain" in scopes:
        return GateResult(handoff(getattr(record, "id"), "AMBIGUOUS"))

    for intent in analysis.intents:
        evaluated = rules.intents[intent.intent_id]
        if evaluated.conflicts or evaluated.missing_field_ids or evaluated.unknown_unit_ids:
            return GateResult(handoff(getattr(record, "id"), "AMBIGUOUS"))

    if analysis.conflicting_unit_ids or analysis.missing_field_ids:
        return GateResult(handoff(getattr(record, "id"), "AMBIGUOUS"))
    planned_intents = {plan.intent_id for plan in analysis.claim_plan}
    if any(
        intent.scope == "in_scope" and intent.intent_id not in planned_intents
        for intent in analysis.intents
    ):
        return GateResult(handoff(getattr(record, "id"), "INSUFFICIENT_EVIDENCE"))
    if not analysis.claim_plan:
        return GateResult(handoff(getattr(record, "id"), "INSUFFICIENT_EVIDENCE"))

    def overridden_ancestor(active_unit_id: str, ancestor_id: str) -> bool:
        pending = list(units[active_unit_id].overrides_unit_ids)
        seen: set[str] = set()
        while pending:
            unit_id = pending.pop()
            if unit_id == ancestor_id:
                return True
            if unit_id not in seen and unit_id in units:
                seen.add(unit_id)
                pending.extend(units[unit_id].overrides_unit_ids)
        return False

    def numeric_literals(claim_id: str) -> set[tuple[str, Decimal, str]]:
        return {
            (item.kind, Decimal(item.value), item.unit_code)
            for item in claim_owners[claim_id][1].normalized_literals
            if item.kind in {"duration", "money", "quantity"}
        }

    inherited_companions: dict[str, set[str]] = {}
    for intent in analysis.intents:
        active = rules.intents[intent.intent_id].active_unit_ids
        permitted: set[str] = set()
        pending = [
            plan.claim_id
            for plan in analysis.claim_plan
            if plan.intent_id == intent.intent_id and plan.unit_id in active
        ]
        seen: set[str] = set()
        while pending:
            parent_id = pending.pop()
            if parent_id in seen:
                continue
            seen.add(parent_id)
            parent_unit, parent_claim = claim_owners[parent_id]
            for companion_id in parent_claim.required_companion_claim_ids:
                companion_unit, _ = claim_owners[companion_id]
                if (
                    companion_unit.unit_id in active
                    or (
                        overridden_ancestor(parent_unit.unit_id, companion_unit.unit_id)
                        and numeric_literals(parent_id) == numeric_literals(companion_id)
                    )
                ):
                    permitted.add(companion_id)
                    pending.append(companion_id)
        inherited_companions[intent.intent_id] = permitted

    issues: list[Issue] = []
    for plan in analysis.claim_plan:
        evaluated = rules.intents[plan.intent_id]
        allowed = (
            plan.unit_id in evaluated.active_unit_ids
            or plan.claim_id in inherited_companions[plan.intent_id]
            if next(item for item in analysis.intents if item.intent_id == plan.intent_id).mode == "case_application"
            else plan.claim_id in evaluated.conditioned_claim_ids
        )
        if not allowed:
            issues.append(_issue("INACTIVE_CLAIM", "planned claim is not active for its intent", plan.claim_id))
    if issues:
        return GateResult(None, issues)
    return GateResult(_answer_output(record, analysis, index))
