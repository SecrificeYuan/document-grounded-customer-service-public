"""The single source of reusable, neutral test objects."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import SecretStr

from docqa.models.analysis import (
    AnalysisResult,
    ExtractedFact,
    Intent,
    PlannedClaim,
    QuoteSelection,
    UnresolvedExpression,
)
from docqa.models.common import BoolValue, DateValue, QuantityValue, Span
from docqa.models.contract import (
    Claim,
    ConditionGroup,
    ContractBody,
    ContractDocument,
    FieldDefinition,
    LocaleProfile,
    PolicyUnit,
    Predicate,
    TimeBasis,
    Topic,
    UnitDefinition,
)
from docqa.models.documents import DocumentSet, EvidenceBlock, SourceDocument, SourceFile
from docqa.config import AppConfig
from docqa.llm_client import ModelReply, ModelRequest, Usage
from docqa.models.output import InputRecord


OLD_TEXT = "2030年1月10日前申请，材料齐备的，办理期限为5个自然日。"
NEW_TEXT = "材料齐备时，2030年1月10日至19日申请的办理期限为8个自然日；自2030年1月20日起为12个自然日。"
SCOPE_TEXT = "贷款期限业务属于服务范围；退款规则另行适用。"


def make_documents() -> DocumentSet:
    """Return a fresh evidence set whose IDs are shared across unit tests."""

    blocks = [
        EvidenceBlock(block_id="E_OLD", document_id="D_TERM", document_name="期限规则.md", page=1, section_path=["旧规则"], ordinal=1, text=OLD_TEXT, source_sha256="a" * 64),
        EvidenceBlock(block_id="E_NEW", document_id="D_TERM", document_name="期限规则.md", page=2, section_path=["新规则"], ordinal=2, text=NEW_TEXT, source_sha256="a" * 64),
        EvidenceBlock(block_id="E_SCOPE", document_id="D_TERM", document_name="期限规则.md", page=1, section_path=["范围"], ordinal=3, text=SCOPE_TEXT, source_sha256="a" * 64),
    ]
    return DocumentSet(
        document_set_hash="d" * 64,
        documents=[SourceDocument(document_id="D_TERM", display_name="期限规则", source_files=[SourceFile(name="期限规则.md", relative_path="期限规则.md", sha256="a" * 64, format="md")], page_count=2, citation_basis="markdown")],
        blocks=blocks,
        full_context="\n".join(block.text for block in blocks),
    )


def _date_predicate(operator: str, *values: date) -> Predicate:
    return Predicate(field_id="F_DATE", operator=operator, values=[DateValue(kind="date", value=value) for value in values])


def _ready_predicate() -> Predicate:
    return Predicate(field_id="F_READY", operator="eq", values=[BoolValue(kind="boolean", value=True)])


def _claim(claim_id: str, days: str, evidence_id: str) -> Claim:
    return Claim(
        claim_id=claim_id,
        canonical_text=f"材料齐备时，办理期限为{days}个自然日。",
        normalized_literals=[QuantityValue(kind="duration", value=days, unit_code="NATURAL_DAY")],
        evidence_ids=[evidence_id],
        qualifiers=["材料齐备"],
        required_companion_claim_ids=[],
    )


def make_contract(*, include_irrelevant_family: bool = False, three_version_chain: bool = False) -> ContractBody:
    """Return a fresh, internally validated neutral contract."""

    fields = [
        FieldDefinition(field_id="F_DATE", display_name="申请日", description="提交申请的日期", aliases=["申请日期"], value_kind="date", allowed_values=[], evidence_ids=["E_OLD", "E_NEW"]),
        FieldDefinition(field_id="F_READY", display_name="材料齐备", description="申请材料是否齐全", aliases=["材料齐全"], value_kind="boolean", allowed_values=[], evidence_ids=["E_OLD", "E_NEW"]),
    ]
    topics = [Topic(topic_id="T_LOAN", description="贷款办理期限", aliases=["办理时限"], evidence_ids=["E_SCOPE"], disposition="covered")]
    old = PolicyUnit(unit_id="U_OLD", family_id="G_TERM", topic_ids=["T_LOAN"], kind="policy", applicability_any=[ConditionGroup(all=[_date_predicate("before", date(2030, 1, 10)), _ready_predicate()])], required_field_ids=["F_DATE", "F_READY"], time_basis=TimeBasis(kind="field", field_id="F_DATE"), effective_from=None, effective_to=date(2030, 1, 9), claims=[_claim("C_OLD", "5", "E_OLD")], overrides_unit_ids=[])
    if three_version_chain:
        units = [
            old,
            PolicyUnit(unit_id="U_MID", family_id="G_TERM", topic_ids=["T_LOAN"], kind="policy", applicability_any=[ConditionGroup(all=[_date_predicate("between", date(2030, 1, 10), date(2030, 1, 19)), _ready_predicate()])], required_field_ids=["F_DATE", "F_READY"], time_basis=TimeBasis(kind="field", field_id="F_DATE"), effective_from=date(2030, 1, 10), effective_to=date(2030, 1, 19), claims=[_claim("C_MID", "8", "E_NEW")], overrides_unit_ids=["U_OLD"]),
            PolicyUnit(unit_id="U_NEW", family_id="G_TERM", topic_ids=["T_LOAN"], kind="policy", applicability_any=[ConditionGroup(all=[_date_predicate("on_or_after", date(2030, 1, 20)), _ready_predicate()])], required_field_ids=["F_DATE", "F_READY"], time_basis=TimeBasis(kind="field", field_id="F_DATE"), effective_from=date(2030, 1, 20), effective_to=None, claims=[_claim("C_NEW", "12", "E_NEW")], overrides_unit_ids=["U_MID"]),
        ]
    else:
        units = [
            old,
            PolicyUnit(unit_id="U_NEW", family_id="G_TERM", topic_ids=["T_LOAN"], kind="policy", applicability_any=[ConditionGroup(all=[_date_predicate("on_or_after", date(2030, 1, 10)), _ready_predicate()])], required_field_ids=["F_DATE", "F_READY"], time_basis=TimeBasis(kind="field", field_id="F_DATE"), effective_from=date(2030, 1, 10), effective_to=None, claims=[_claim("C_NEW", "8", "E_NEW")], overrides_unit_ids=["U_OLD"]),
        ]
    if include_irrelevant_family:
        fields.append(FieldDefinition(field_id="F_REFUND_DATE", display_name="退款申请日", description="退款申请日期", aliases=[], value_kind="date", allowed_values=[], evidence_ids=["E_SCOPE"]))
        topics.append(Topic(topic_id="T_REFUND", description="退款规则", aliases=[], evidence_ids=["E_SCOPE"], disposition="covered"))
        units.append(PolicyUnit(unit_id="U_REFUND", family_id="G_REFUND", topic_ids=["T_REFUND"], kind="policy", applicability_any=[], required_field_ids=["F_REFUND_DATE"], time_basis=TimeBasis(kind="field", field_id="F_REFUND_DATE"), effective_from=None, effective_to=None, claims=[Claim(claim_id="C_REFUND", canonical_text="退款规则另行适用。", normalized_literals=[], evidence_ids=["E_SCOPE"], qualifiers=[], required_companion_claim_ids=[])], overrides_unit_ids=[]))
    return ContractBody(
        locale=LocaleProfile(language="zh-CN", timezone="Asia/Shanghai", output_encoding="utf-8", normalization_version="1"),
        documents=[ContractDocument(document_id="D_TERM", title="期限规则", version_label="测试版", published_on=date(2029, 12, 1), effective_from=None, effective_to=None, supersedes_document_ids=[])],
        scope=topics,
        fields=fields,
        units=[UnitDefinition(unit_code="NATURAL_DAY", display_name="自然日", aliases=["天"], dimension="duration", evidence_ids=["E_OLD", "E_NEW"])],
        policy_units=units,
        warnings=[],
    )


def make_analysis(*, mode: str = "case_application", event_date: date | None = date(2030, 1, 10)) -> AnalysisResult:
    """Return a fresh analysis with exact source spans."""

    if mode == "rule_description":
        question = "贷款办理期限的规则是什么？"
    elif event_date is None:
        question = "材料齐备，可以办理吗？"
    else:
        question = f"{event_date.year}年{event_date.month}月{event_date.day}日申请，材料齐备，可以办理吗？"
    facts = []
    if event_date is not None and mode == "case_application":
        date_text = f"{event_date.year}年{event_date.month}月{event_date.day}日"
        start = question.index(date_text)
        facts.append(ExtractedFact(field_id="F_DATE", value=DateValue(kind="date", value=event_date), sources=[Span(start=start, end=start + len(date_text), text=date_text)], intent_ids=["I_TERM"]))
    if mode == "case_application":
        ready = "材料齐备"
        start = question.index(ready)
        facts.append(ExtractedFact(field_id="F_READY", value=BoolValue(kind="boolean", value=True), sources=[Span(start=start, end=start + len(ready), text=ready)], intent_ids=["I_TERM"]))
    selected = ["U_NEW"] if event_date is not None or mode == "rule_description" else []
    plan = [PlannedClaim(intent_id="I_TERM", unit_id="U_NEW", claim_id="C_NEW", assertion_text="材料齐备时，办理期限为8个自然日。", evidence_ids=["E_NEW"], quotes=[QuoteSelection(evidence_id="E_NEW", fragment="材料齐备时，2030年1月10日至19日申请的办理期限为8个自然日")])] if selected else []
    return AnalysisResult(
        language="zh-CN",
        intents=[Intent(intent_id="I_TERM", source=Span(start=0, end=len(question), text=question), description="询问贷款办理期限", scope="in_scope", mode=mode, family_ids=["G_TERM"], claim_ids=["C_NEW"])],
        extracted_facts=facts,
        unresolved_expressions=[],
        missing_field_ids=[] if selected else ["F_DATE"],
        selected_unit_ids=selected,
        claim_plan=plan,
        conflicting_unit_ids=[],
        proposed_decision="answer" if selected else "handoff",
        reason_code=None if selected else "INSUFFICIENT_EVIDENCE",
        answer_draft="材料齐备时，办理期限为8个自然日。" if selected else "申请日期缺失，需转人工确认。",
    )


def make_config() -> AppConfig:
    return AppConfig(api_key=SecretStr("unit-test-placeholder"), base_url="https://api.deepseek.com", model="deepseek-flash", reasoning_effort="high", reference_date=date(2030, 1, 10))


def make_request(schema_name: str = "unit_test_object") -> ModelRequest:
    return ModelRequest(purpose="analysis", instructions="只返回JSON。", input_text="测试输入", schema_name=schema_name, schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}, max_output_tokens=2000)


def raw_reply(text: str, status: str = "completed") -> ModelReply:
    return ModelReply(text=text, status=status, response_id="r-test", model="deepseek-flash", usage=Usage(input_tokens=None, output_tokens=None, cached_tokens=None, reasoning_tokens=None), elapsed_seconds=0.1)


def contract_reply(body: ContractBody | None = None) -> ModelReply:
    """Return one complete structured contract response."""

    return raw_reply((body or make_contract()).model_dump_json())


def make_builder(
    client: object,
    tmp_path: Path,
    *,
    prompt_version: str = "test-contract-prompt-v1",
    max_request_bytes: int = 1_000_000,
):
    """Create the production builder with deterministic test dependencies."""

    from docqa.contract_builder import ContractBuilder
    from docqa.contract_store import ContractStore

    return ContractBuilder(
        client=client,
        store=ContractStore(tmp_path),
        config=make_config(),
        prompt_version=prompt_version,
        max_request_bytes=max_request_bytes,
        now=lambda: datetime(2030, 1, 10, tzinfo=timezone.utc),
    )


def make_question_context() -> dict[str, object]:
    """Return the four production inputs for one deterministic question."""

    analysis = make_analysis()
    return {
        "record": InputRecord(
            id="Q-001",
            session_id="S-001",
            question=analysis.intents[0].source.text,
        ),
        "documents": make_documents(),
        "contract": make_contract(),
        "config": make_config(),
    }


def analysis_reply(
    *,
    overclaim: bool = False,
    missing_date: bool = False,
    bad_quote: bool = False,
    extra_promise: bool = False,
) -> ModelReply:
    """Return a Schema-valid analysis with one requested semantic mutation."""

    analysis = make_analysis()
    if overclaim:
        analysis.claim_plan[0].assertion_text = analysis.claim_plan[0].assertion_text.replace("8", "9")
        analysis.answer_draft = analysis.answer_draft.replace("8", "9")
    if bad_quote:
        analysis.claim_plan[0].quotes[0].fragment = "原文中不存在的引文"
    if extra_promise:
        analysis.answer_draft += "并保证当天完成。"
    if missing_date:
        date_fact = next(item for item in analysis.extracted_facts if item.field_id == "F_DATE")
        analysis.extracted_facts = [item for item in analysis.extracted_facts if item.field_id != "F_DATE"]
        analysis.unresolved_expressions = [
            UnresolvedExpression(
                source=date_fact.sources[0],
                category="time",
                intent_ids=["I_TERM"],
                field_ids=["F_DATE"],
            )
        ]
        analysis.missing_field_ids = ["F_DATE"]
        analysis.selected_unit_ids = []
        analysis.claim_plan = []
        analysis.proposed_decision = "handoff"
        analysis.reason_code = "AMBIGUOUS"
        analysis.answer_draft = "申请日期仍需人工确认。"
    checked = AnalysisResult.model_validate_json(analysis.model_dump_json())
    return raw_reply(checked.model_dump_json())
