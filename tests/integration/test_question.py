from __future__ import annotations

import json
import logging
from datetime import date

import pytest

from docqa.analyzer import analyze_question
from docqa.decision import validate_analysis
from docqa.evidence import EvidenceIndex
from docqa.errors import ConfigurationError, TransportExhausted
from docqa.llm_client import ModelReply, Usage
from docqa.models.analysis import ExtractedFact, Intent
from docqa.models.analysis import PlannedClaim, QuoteSelection
from docqa.models.common import BoolValue, DateValue, QuantityValue, Span
from docqa.models.contract import FieldDefinition, UnitDefinition
from docqa.models.output import InputRecord
from tests.fake_llm import FakeLLMClient
from tests.fixtures.builders import (
    analysis_reply,
    make_analysis,
    make_question_context,
    raw_reply,
)


def test_valid_analysis_produces_python_verified_answer() -> None:
    client = FakeLLMClient([analysis_reply()])

    result = analyze_question(**make_question_context(), client=client)

    assert result.decision == "answer"
    assert result.answer == "材料齐备时，办理期限为8个自然日。"
    assert result.reason_code is None
    assert [item.quote for item in result.evidence] == [
        "材料齐备时，2030年1月10日至19日申请的办理期限为8个自然日"
    ]
    assert len(client.calls) == 1


def test_schema_failure_can_be_repaired_once() -> None:
    client = FakeLLMClient([raw_reply("{bad"), analysis_reply()])

    result = analyze_question(**make_question_context(), client=client)

    assert result.decision == "answer"
    assert [call.purpose for call in client.calls] == ["analysis", "repair"]


def test_schema_failure_and_gate_failure_share_one_repair() -> None:
    client = FakeLLMClient([raw_reply("{bad"), analysis_reply(overclaim=True)])

    result = analyze_question(**make_question_context(), client=client)

    assert len(client.calls) == 2
    assert result.decision == "handoff"
    assert result.reason_code == "AMBIGUOUS"
    assert result.evidence == []


def test_business_handoff_does_not_request_unnecessary_repair() -> None:
    client = FakeLLMClient([analysis_reply(missing_date=True)])

    result = analyze_question(**make_question_context(), client=client)

    assert result.reason_code == "AMBIGUOUS"
    assert len(client.calls) == 1


def test_invalid_quote_uses_the_only_repair() -> None:
    client = FakeLLMClient([analysis_reply(bad_quote=True), analysis_reply()])

    result = analyze_question(**make_question_context(), client=client)

    assert result.decision == "answer"
    assert len(client.calls) == 2


def test_transport_exhaustion_isolated_as_ambiguous_handoff() -> None:
    client = FakeLLMClient([TransportExhausted("offline")])

    result = analyze_question(**make_question_context(), client=client)

    assert result.decision == "handoff"
    assert result.reason_code == "AMBIGUOUS"
    assert len(client.calls) == 1


def test_unplanned_final_promise_is_rejected_after_one_repair() -> None:
    client = FakeLLMClient(
        [analysis_reply(extra_promise=True), analysis_reply(extra_promise=True)]
    )

    result = analyze_question(**make_question_context(), client=client)

    assert result.decision == "handoff"
    assert result.reason_code == "AMBIGUOUS"
    assert len(client.calls) == 2


def test_every_in_scope_core_intent_must_have_a_planned_claim() -> None:
    analysis = make_analysis()
    first = analysis.intents[0]
    analysis.intents.append(
        Intent(
            intent_id="I_SECOND",
            source=first.source,
            description="第二个核心期限请求",
            scope="in_scope",
            mode="case_application",
            family_ids=["G_TERM"],
            claim_ids=["C_NEW"],
        )
    )
    partial = raw_reply(analysis.model_dump_json())
    client = FakeLLMClient([partial, partial])

    result = analyze_question(**make_question_context(), client=client)

    assert result.decision == "handoff"
    assert result.reason_code == "AMBIGUOUS"
    assert len(client.calls) == 1


def test_active_claim_can_bind_consistent_companion_from_overridden_base_rule() -> None:
    context = make_question_context()
    contract = context["contract"]
    new_claim = contract.policy_units[1].claims[0]
    old_claim = contract.policy_units[0].claims[0]
    old_claim.canonical_text = "材料齐备时，办理期限为8个自然日。"
    old_claim.normalized_literals = new_claim.normalized_literals.copy()
    old_claim.evidence_ids = ["E_NEW"]
    new_claim.required_companion_claim_ids = ["C_OLD"]
    analysis = make_analysis()
    analysis.intents[0].claim_ids.append("C_OLD")
    analysis.selected_unit_ids.append("U_OLD")
    analysis.claim_plan.append(
        PlannedClaim(
            intent_id="I_TERM",
            unit_id="U_OLD",
            claim_id="C_OLD",
            assertion_text=old_claim.canonical_text,
            evidence_ids=["E_NEW"],
            quotes=[
                QuoteSelection(
                    evidence_id="E_NEW",
                    fragment="材料齐备时，2030年1月10日至19日申请的办理期限为8个自然日",
                )
            ],
        )
    )
    analysis.answer_draft = "".join(
        plan.assertion_text for plan in analysis.claim_plan
    )
    reply = raw_reply(analysis.model_dump_json())
    client = FakeLLMClient([reply, reply])

    result = analyze_question(**context, client=client)

    assert result.decision == "answer"
    assert len(client.calls) == 1


def test_active_claim_cannot_bind_conflicting_overridden_companion() -> None:
    context = make_question_context()
    context["contract"].policy_units[1].claims[0].required_companion_claim_ids = ["C_OLD"]
    analysis = make_analysis()
    analysis.intents[0].claim_ids.append("C_OLD")
    analysis.selected_unit_ids.append("U_OLD")
    analysis.claim_plan.append(
        PlannedClaim(
            intent_id="I_TERM",
            unit_id="U_OLD",
            claim_id="C_OLD",
            assertion_text="材料齐备时，办理期限为5个自然日。",
            evidence_ids=["E_OLD"],
            quotes=[QuoteSelection(evidence_id="E_OLD", fragment="2030年1月10日前申请，材料齐备的，办理期限为5个自然日")],
        )
    )
    analysis.answer_draft = "".join(plan.assertion_text for plan in analysis.claim_plan)
    reply = raw_reply(analysis.model_dump_json())

    result = analyze_question(**context, client=FakeLLMClient([reply, reply]))

    assert result.decision == "handoff"


def test_wrong_source_position_is_rejected_even_when_text_occurs_elsewhere() -> None:
    analysis = make_analysis()
    fact = analysis.extracted_facts[0]
    source = fact.sources[0]
    fact.sources = [Span(start=source.start + 1, end=source.end + 1, text=source.text)]
    invalid = raw_reply(analysis.model_dump_json())
    client = FakeLLMClient([invalid, invalid])

    result = analyze_question(**make_question_context(), client=client)

    assert result.reason_code == "AMBIGUOUS"
    assert len(client.calls) == 2


def test_request_context_has_required_order_and_no_duplicate_full_context() -> None:
    client = FakeLLMClient([analysis_reply()])

    analyze_question(**make_question_context(), client=client)

    raw = client.calls[0].input_text
    payload = json.loads(raw)
    assert list(payload) == ["locale", "as_of", "contract", "evidence_blocks", "question"]
    assert "full_context" not in raw


def test_initial_request_states_exact_claim_and_answer_invariants() -> None:
    client = FakeLLMClient([analysis_reply()])

    analyze_question(**make_question_context(), client=client)

    instructions = client.calls[0].instructions
    assert "canonical_text" in instructions
    assert "不得改写" in instructions
    assert "按顺序拼接" in instructions
    assert "所属 Policy Unit 的 family_id" in instructions
    assert "in_scope_uncovered" in instructions


def test_repair_request_repeats_exact_claim_and_answer_invariants() -> None:
    client = FakeLLMClient([raw_reply("{bad"), analysis_reply()])

    analyze_question(**make_question_context(), client=client)

    instructions = client.calls[1].instructions
    assert "canonical_text" in instructions
    assert "不得改写" in instructions
    assert "按顺序拼接" in instructions
    assert "所属 Policy Unit 的 family_id" in instructions
    assert "in_scope_uncovered" in instructions


def test_analysis_and_repair_instruct_nonblank_handoff_draft() -> None:
    client = FakeLLMClient([raw_reply("{bad"), analysis_reply()])

    analyze_question(**make_question_context(), client=client)

    for request in client.calls:
        assert "proposed_decision 为 answer 时" in request.instructions
        assert "proposed_decision 为 handoff 时" in request.instructions
        assert "claim_plan 必须为空" in request.instructions
        assert "answer_draft 固定为“已转人工处理。”" in request.instructions


def test_analysis_and_repair_define_companion_ambiguity_and_scope_boundaries() -> None:
    client = FakeLLMClient([raw_reply("{bad"), analysis_reply()])

    analyze_question(**make_question_context(), client=client)

    for request in client.calls:
        assert "递归展开 required_companion_claim_ids" in request.instructions
        assert "没有说明要办理、判断或查询的具体事项" in request.instructions
        assert "handoff + AMBIGUOUS" in request.instructions
        assert "scope 表示用户实际请求的结论范围" in request.instructions
        assert "直接要求生成专业意见、判断或预测" in request.instructions


def test_analysis_and_repair_require_minimal_parseable_fact_sources() -> None:
    client = FakeLLMClient([raw_reply("{bad"), analysis_reply()])

    analyze_question(**make_question_context(), client=client)

    for request in client.calls:
        assert "最小原文片段" in request.instructions
        assert "日期表达本身" in request.instructions


def test_repair_keeps_source_supported_claims_when_fixing_literal_mismatch() -> None:
    analysis = make_analysis()
    analysis.claim_plan[0].assertion_text = "改写后的断言。"
    analysis.answer_draft = analysis.claim_plan[0].assertion_text
    client = FakeLLMClient([raw_reply(analysis.model_dump_json()), analysis_reply()])

    analyze_question(**make_question_context(), client=client)

    instructions = client.calls[1].instructions
    assert "不得仅为清除错误而删除" in instructions
    assert "替换为该 Claim 的完整 canonical_text" in instructions


def test_repair_requires_each_quote_to_be_exact_text_from_its_evidence_block() -> None:
    client = FakeLLMClient([analysis_reply(bad_quote=True), analysis_reply()])

    analyze_question(**make_question_context(), client=client)

    instructions = client.calls[1].instructions
    assert "逐字连续子串" in instructions
    assert "quote.evidence_id" in instructions


def test_analysis_selects_only_families_needed_for_the_requested_conclusion() -> None:
    client = FakeLLMClient([analysis_reply()])

    analyze_question(**make_question_context(), client=client)

    instructions = client.calls[0].instructions
    assert "只包含直接回答该意图所必需的规则族" in instructions
    assert "背景时段" in instructions


def test_repair_requires_literal_complete_quotes_for_every_claim() -> None:
    client = FakeLLMClient([analysis_reply(bad_quote=True), analysis_reply()])

    analyze_question(**make_question_context(), client=client)

    instructions = client.calls[1].instructions
    assert "每个所选 Claim" in instructions
    assert "完整覆盖该 Claim 的全部数值和单位" in instructions
    assert "不得包含该 Claim 未登记的其他数值和单位" in instructions


def test_analysis_and_repair_budgets_cover_observed_reasoning_exhaustion() -> None:
    client = FakeLLMClient([raw_reply("{bad"), analysis_reply()])

    analyze_question(**make_question_context(), client=client)

    assert client.calls[0].max_output_tokens == 24_000
    assert client.calls[1].max_output_tokens == 24_000


def test_configuration_error_propagates_to_batch_boundary() -> None:
    client = FakeLLMClient([ConfigurationError("bad key")])

    with pytest.raises(ConfigurationError, match="bad key"):
        analyze_question(**make_question_context(), client=client)


def test_claim_plan_must_be_named_by_its_own_intent() -> None:
    analysis = make_analysis()
    analysis.intents[0].claim_ids = []
    invalid = raw_reply(analysis.model_dump_json())
    client = FakeLLMClient([invalid, invalid])

    result = analyze_question(**make_question_context(), client=client)

    assert result.decision == "handoff"
    assert result.reason_code == "AMBIGUOUS"
    assert len(client.calls) == 2


def test_boolean_fact_must_match_its_exact_source_meaning() -> None:
    context = make_question_context()
    analysis = make_analysis()
    question = analysis.intents[0].source.text.replace("材料齐备", "材料不齐备")
    analysis.intents[0].source = Span(start=0, end=len(question), text=question)
    ready = next(item for item in analysis.extracted_facts if item.field_id == "F_READY")
    start = question.index("材料不齐备")
    ready.sources = [Span(start=start, end=start + len("材料不齐备"), text="材料不齐备")]
    ready.value = BoolValue(kind="boolean", value=True)
    invalid = raw_reply(analysis.model_dump_json())
    client = FakeLLMClient([invalid, invalid])
    context["record"] = InputRecord(id="Q-001", session_id="S-001", question=question)

    result = analyze_question(**context, client=client)

    assert result.reason_code == "AMBIGUOUS"
    assert len(client.calls) == 2


def test_false_boolean_fact_accepts_explicit_medial_negation() -> None:
    context = make_question_context()
    analysis = make_analysis()
    question = analysis.intents[0].source.text.replace("材料齐备", "材料未齐备")
    analysis.intents[0].source = Span(start=0, end=len(question), text=question)
    ready = next(item for item in analysis.extracted_facts if item.field_id == "F_READY")
    source_text = "材料未齐备"
    start = question.index(source_text)
    ready.sources = [
        Span(start=start, end=start + len(source_text), text=source_text)
    ]
    ready.value = BoolValue(kind="boolean", value=False)
    context["record"] = InputRecord(
        id="Q-001", session_id="S-001", question=question
    )

    report = validate_analysis(
        context["record"],
        analysis,
        context["contract"],
        EvidenceIndex(context["documents"].blocks),
        context["config"].reference_date,
    )

    assert "FACT_SOURCE_MISMATCH" not in {issue.code for issue in report.issues}


def test_negation_cannot_be_hidden_inside_planned_assertion() -> None:
    analysis = make_analysis()
    analysis.claim_plan[0].assertion_text = "并非材料齐备时，办理期限为8个自然日。"
    analysis.answer_draft = analysis.claim_plan[0].assertion_text
    invalid = raw_reply(analysis.model_dump_json())
    client = FakeLLMClient([invalid, invalid])

    result = analyze_question(**make_question_context(), client=client)

    assert result.reason_code == "AMBIGUOUS"
    assert len(client.calls) == 2


def test_rule_description_requires_claim_coverage_for_every_intent() -> None:
    context = make_question_context()
    analysis = make_analysis(mode="rule_description")
    first = analysis.intents[0]
    analysis.intents.append(
        Intent(
            intent_id="I_SECOND",
            source=first.source,
            description="另一个核心规则请求",
            scope="in_scope",
            mode="rule_description",
            family_ids=["G_TERM"],
            claim_ids=["C_NEW"],
        )
    )
    partial = raw_reply(analysis.model_dump_json())
    client = FakeLLMClient([partial])
    context["record"] = InputRecord(
        id="Q-001", session_id="S-001", question=first.source.text
    )

    result = analyze_question(**context, client=client)

    assert result.decision == "handoff"
    assert result.reason_code == "INSUFFICIENT_EVIDENCE"
    assert len(client.calls) == 1


def test_exhausted_gate_records_only_safe_technical_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeLLMClient([raw_reply("{bad"), raw_reply("{still-bad")])

    with caplog.at_level(logging.WARNING, logger="docqa.analyzer"):
        result = analyze_question(**make_question_context(), client=client)

    assert result.reason_code == "AMBIGUOUS"
    assert "technical_failure" in caplog.text
    assert "AnalysisFailed" in caplog.text
    assert "{still-bad" not in caplog.text


def test_each_failed_semantic_attempt_emits_content_free_issue_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    usage = Usage(
        input_tokens=101,
        output_tokens=202,
        cached_tokens=33,
        reasoning_tokens=44,
    )
    replies = [
        ModelReply(
            text='{\"Authorization\":\"Bearer secret\"}',
            status="completed",
            response_id="response-secret",
            model="deepseek-flash",
            usage=usage,
            elapsed_seconds=0.1,
        ),
        ModelReply(
            text="not-json secret question text",
            status="provider-controlled-status",
            response_id="second-secret",
            model="deepseek-flash",
            usage=usage,
            elapsed_seconds=0.2,
        ),
    ]
    failures = []

    with caplog.at_level(logging.WARNING, logger="docqa.analyzer"):
        result = analyze_question(
            **make_question_context(),
            client=FakeLLMClient(replies),
            on_attempt_failure=failures.append,
        )

    assert result.reason_code == "AMBIGUOUS"
    assert [event.model_dump(mode="json") for event in failures] == [
        {
            "attempt": 1,
            "response_status": "completed",
            "issue_codes": ["INVALID_ANALYSIS_SCHEMA"],
            "contract_object_ids": [],
            "usage": usage.model_dump(mode="json"),
        },
        {
            "attempt": 2,
            "response_status": "other",
            "issue_codes": ["INVALID_MODEL_REPLY"],
            "contract_object_ids": [],
            "usage": usage.model_dump(mode="json"),
        },
    ]
    assert "INVALID_ANALYSIS_SCHEMA" in caplog.text
    for forbidden in (
        "Authorization",
        "Bearer",
        "secret",
        "question text",
        "response-secret",
        "second-secret",
        "provider-controlled-status",
    ):
        assert forbidden not in caplog.text


def test_gate_failure_telemetry_identifies_allowlisted_contract_field() -> None:
    analysis = make_analysis()
    date_fact = next(
        fact for fact in analysis.extracted_facts if fact.field_id == "F_DATE"
    )
    date_fact.value = DateValue(kind="date", value=date(2030, 1, 11))
    invalid = raw_reply(analysis.model_dump_json())
    failures = []

    result = analyze_question(
        **make_question_context(),
        client=FakeLLMClient([invalid, invalid]),
        on_attempt_failure=failures.append,
    )

    assert result.reason_code == "AMBIGUOUS"
    assert [event.contract_object_ids for event in failures] == [
        ("F_DATE",),
        ("F_DATE",),
    ]


@pytest.mark.parametrize(
    ("canonical", "assertion", "qualifiers"),
    [
        (
            "材料齐备时，办理期限为8个自然日。",
            "材料齐备时，办理期限为8个自然日，并保证当天完成。",
            ["材料齐备"],
        ),
        (
            "材料齐备时，办理期限最多为8个自然日。",
            "材料齐备时，办理期限为8个自然日。",
            ["材料齐备", "最多"],
        ),
        (
            "材料齐备时，首次响应期限为8个自然日。",
            "材料齐备时，最终解决期限为8个自然日。",
            ["材料齐备", "首次响应"],
        ),
    ],
)
def test_assertion_cannot_hide_extra_or_changed_semantics(
    canonical: str, assertion: str, qualifiers: list[str]
) -> None:
    context = make_question_context()
    claim = context["contract"].policy_units[1].claims[0]
    claim.canonical_text = canonical
    claim.qualifiers = qualifiers
    analysis = make_analysis()
    analysis.claim_plan[0].assertion_text = assertion
    analysis.answer_draft = assertion
    invalid = raw_reply(analysis.model_dump_json())
    client = FakeLLMClient([invalid, invalid])

    result = analyze_question(**context, client=client)

    assert result.reason_code == "AMBIGUOUS"
    assert len(client.calls) == 2


def test_200_gb_cannot_satisfy_a_200_mb_claim() -> None:
    context = make_question_context()
    contract = context["contract"]
    contract.units.extend(
        [
            UnitDefinition(unit_code="MB", display_name="MB", aliases=[], dimension="quantity", evidence_ids=["E_NEW"]),
            UnitDefinition(unit_code="GB", display_name="GB", aliases=[], dimension="quantity", evidence_ids=["E_NEW"]),
        ]
    )
    claim = contract.policy_units[1].claims[0]
    claim.canonical_text = "材料齐备时，容量上限为200MB。"
    claim.normalized_literals = [
        QuantityValue(kind="quantity", value="200", unit_code="MB")
    ]
    claim.qualifiers = ["材料齐备", "上限"]
    analysis = make_analysis()
    analysis.claim_plan[0].assertion_text = "材料齐备时，容量上限为200GB。"
    analysis.answer_draft = analysis.claim_plan[0].assertion_text
    invalid = raw_reply(analysis.model_dump_json())
    client = FakeLLMClient([invalid, invalid])

    result = analyze_question(**context, client=client)

    assert result.reason_code == "AMBIGUOUS"
    assert len(client.calls) == 2


def test_python_finds_impactful_missing_field_even_if_model_claims_answer() -> None:
    analysis = make_analysis()
    analysis.extracted_facts = [
        fact for fact in analysis.extracted_facts if fact.field_id != "F_DATE"
    ]
    analysis.missing_field_ids = []
    client = FakeLLMClient([raw_reply(analysis.model_dump_json())])

    result = analyze_question(**make_question_context(), client=client)

    assert result.decision == "handoff"
    assert result.reason_code == "AMBIGUOUS"
    assert len(client.calls) == 1


def test_real_quote_from_same_block_cannot_support_a_conflicting_literal() -> None:
    analysis = make_analysis()
    analysis.claim_plan[0].quotes[0].fragment = "自2030年1月20日起为12个自然日"
    invalid = raw_reply(analysis.model_dump_json())
    client = FakeLLMClient([invalid, invalid])

    result = analyze_question(**make_question_context(), client=client)

    assert result.reason_code == "AMBIGUOUS"
    assert len(client.calls) == 2


def test_unknown_boolean_wording_cannot_be_promoted_to_true() -> None:
    context = make_question_context()
    analysis = make_analysis()
    question = analysis.intents[0].source.text.replace("材料齐备", "材料齐备情况未知")
    analysis.intents[0].source = Span(start=0, end=len(question), text=question)
    ready = next(item for item in analysis.extracted_facts if item.field_id == "F_READY")
    start = question.index("材料齐备情况未知")
    ready.sources = [
        Span(start=start, end=start + len("材料齐备情况未知"), text="材料齐备情况未知")
    ]
    invalid = raw_reply(analysis.model_dump_json())
    client = FakeLLMClient([invalid, invalid])
    context["record"] = InputRecord(id="Q-001", session_id="S-001", question=question)

    result = analyze_question(**context, client=client)

    assert result.reason_code == "AMBIGUOUS"
    assert len(client.calls) == 2


def test_technical_failure_log_does_not_include_user_controlled_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = make_question_context()
    context["record"] = InputRecord(
        id="Q\nAuthorization: Bearer secret",
        session_id="S-001",
        question=context["record"].question,
    )
    client = FakeLLMClient([raw_reply("{bad"), raw_reply("{still-bad")])

    with caplog.at_level(logging.WARNING, logger="docqa.analyzer"):
        analyze_question(**context, client=client)

    assert "technical_failure" in caplog.text
    assert "AnalysisFailed" in caplog.text
    assert "Authorization" not in caplog.text
    assert "secret" not in caplog.text


def test_negated_quantity_source_cannot_support_a_positive_fact() -> None:
    context = make_question_context()
    question = context["record"].question + " 不是8个自然日。"
    context["record"] = InputRecord(id="Q-001", session_id="S-001", question=question)
    context["contract"].fields.append(
        FieldDefinition(
            field_id="F_DURATION",
            display_name="办理期限",
            description="期限事实",
            aliases=[],
            value_kind="duration",
            allowed_values=[],
            evidence_ids=["E_NEW"],
        )
    )
    analysis = make_analysis()
    source_text = "不是8个自然日"
    start = question.index(source_text)
    analysis.extracted_facts.append(
        ExtractedFact(
            field_id="F_DURATION",
            value=QuantityValue(kind="duration", value="8", unit_code="NATURAL_DAY"),
            sources=[Span(start=start, end=start + len(source_text), text=source_text)],
            intent_ids=["I_TERM"],
        )
    )
    invalid = raw_reply(analysis.model_dump_json())
    client = FakeLLMClient([invalid, invalid])

    result = analyze_question(**context, client=client)

    assert result.reason_code == "AMBIGUOUS"
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    ("canonical", "qualifiers", "contradictory_quote"),
    [
        (
            "材料齐备时，办理期限最多为8个自然日。",
            ["材料齐备", "最多"],
            "材料齐备时，办理期限至少为8个自然日",
        ),
        (
            "材料齐备时，办理期限为8个自然日。",
            ["材料齐备"],
            "并非材料齐备时，办理期限为8个自然日",
        ),
        (
            "材料齐备时，首次响应期限为8个自然日。",
            ["材料齐备", "首次响应"],
            "材料齐备时，最终解决期限为8个自然日",
        ),
    ],
)
def test_true_quote_must_preserve_claim_qualifiers_and_polarity(
    canonical: str, qualifiers: list[str], contradictory_quote: str
) -> None:
    context = make_question_context()
    claim = context["contract"].policy_units[1].claims[0]
    claim.canonical_text = canonical
    claim.qualifiers = qualifiers
    block = next(
        item for item in context["documents"].blocks if item.block_id == "E_NEW"
    )
    block.text = contradictory_quote + "。"
    analysis = make_analysis()
    analysis.claim_plan[0].assertion_text = canonical
    analysis.claim_plan[0].quotes[0].fragment = contradictory_quote
    analysis.answer_draft = canonical
    invalid = raw_reply(analysis.model_dump_json())
    client = FakeLLMClient([invalid, invalid])

    result = analyze_question(**context, client=client)

    assert result.reason_code == "AMBIGUOUS"
    assert len(client.calls) == 2
