"""Apply explicit, hash-bound business aliases without changing generated contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field, field_validator

from docqa.contract_store import (
    ContractStore,
    DocumentContract,
    ReviewRecord,
    canonical_body_sha256,
)
from docqa.contract_validator import validate_contract
from docqa.models.common import StrictModel
from docqa.models.documents import DocumentSet
from docqa.zh_normalization import normalize_surface


class AliasEntry(StrictModel):
    field_id: str
    value_code: str
    alias: str

    @field_validator("field_id", "value_code", "alias")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("alias approval fields cannot be blank")
        return value


class AliasApproval(StrictModel):
    base_cache_key: str
    base_body_sha256: str
    approval_note: str
    aliases: list[AliasEntry] = Field(min_length=1)

    @field_validator("base_cache_key", "base_body_sha256", "approval_note")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("alias approval fields cannot be blank")
        return value


def apply_alias_approvals(
    store: ContractStore, documents: DocumentSet, approval_path: Path
) -> DocumentContract:
    """Create or reuse a separate manual-review contract for approved enum aliases."""

    approval = AliasApproval.model_validate_json(Path(approval_path).read_text(encoding="utf-8"))
    base = store.load(
        documents.document_set_hash,
        approval.base_cache_key,
        documents=documents,
    )
    if base is None:
        raise ValueError("base contract is unavailable or blocked")
    if base.manifest.body_sha256 != approval.base_body_sha256:
        raise ValueError("base body hash does not match approval file")

    body = base.body.model_copy(deep=True)
    fields = {field.field_id: field for field in body.fields}
    for entry in approval.aliases:
        field = fields.get(entry.field_id)
        if field is None or field.value_kind != "enum":
            raise ValueError(f"alias target is not an enum field: {entry.field_id}")
        target = next(
            (option for option in field.allowed_values if option.code == entry.value_code),
            None,
        )
        if target is None:
            raise ValueError(f"alias target value does not exist: {entry.value_code}")
        normalized = normalize_surface(entry.alias)
        for option in field.allowed_values:
            surfaces = [normalize_surface(text) for text in [option.display_name, *option.aliases]]
            if normalized in surfaces and option.code != entry.value_code:
                raise ValueError(f"alias conflicts with another enum value: {entry.alias}")
        if normalized not in [normalize_surface(text) for text in [target.display_name, *target.aliases]]:
            target.aliases.append(entry.alias)

    if canonical_body_sha256(body) == base.manifest.body_sha256:
        raise ValueError("approval adds no new aliases")
    report = validate_contract(body, documents)
    if not report.valid:
        raise ValueError("approved aliases produce an invalid contract")

    approval_json = json.dumps(approval.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    key = hashlib.sha256(
        ("alias-approval-v1\0" + base.manifest.cache_key + "\0" + approval_json).encode("utf-8")
    ).hexdigest()
    if store.review_status(documents.document_set_hash, key) == "blocked":
        raise ValueError("approved-alias cache has been blocked")
    cached = store.load(documents.document_set_hash, key, documents=documents)
    if cached is not None:
        if cached.manifest.body_sha256 != canonical_body_sha256(body):
            raise ValueError("approved-alias cache key has conflicting body")
        return cached

    now = datetime.now(timezone.utc)
    manifest = base.manifest.model_copy(
        update={"cache_key": key, "body_sha256": canonical_body_sha256(body), "generated_at": now}
    )
    contract = DocumentContract(manifest=manifest, body=body)
    review = ReviewRecord(
        body_sha256=manifest.body_sha256,
        status="manual_approved",
        reviewed_at=now,
        issues=[],
        manual_change_notes=[approval.approval_note],
    )
    store.save(contract, review)
    return contract
