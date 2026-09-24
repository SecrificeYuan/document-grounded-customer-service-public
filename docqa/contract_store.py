"""Integrity-bound storage for validated document contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from docqa.atomic_io import atomic_write
from docqa.contract_validator import validate_contract
from docqa.models.common import Issue, StrictModel
from docqa.models.contract import ContractBody
from docqa.models.documents import DocumentSet, SourceFile


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text fields cannot be blank")
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone information")
    return value


def _canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_body_sha256(body: ContractBody) -> str:
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def _source_identity(value: Any) -> Any:
    if isinstance(value, DocumentSet):
        return {"document_set_hash": value.document_set_hash}
    if isinstance(value, SourceFile):
        return {"name": value.name, "sha256": value.sha256, "format": value.format}
    if isinstance(value, Path):
        return {"name": value.name, "sha256": hashlib.sha256(value.read_bytes()).hexdigest()}
    if isinstance(value, BaseModel):
        return _source_identity(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _source_identity(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0])) if str(key) not in {"path", "relative_path", "absolute_path"}}
    if isinstance(value, (list, tuple)):
        return [_source_identity(item) for item in value]
    if isinstance(value, set):
        return sorted(_source_identity(item) for item in value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def cache_key(sources: Any, pairing: Any, versions: Any, prompt_hash: str, model: str, effort: str) -> str:
    """Hash every build input while excluding machine-specific source paths."""

    payload = {
        "sources": _source_identity(sources),
        "pairing": _source_identity(pairing),
        "versions": _source_identity(versions),
        "prompt_hash": _nonblank(prompt_hash),
        "model": _nonblank(model),
        "effort": _nonblank(effort),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class ContractManifest(StrictModel):
    schema_version: str
    parser_version: str
    normalization_version: str
    validator_version: str
    prompt_sha256: str
    model: str
    effort: Literal["high", "max"]
    document_set_hash: str
    cache_key: str
    body_sha256: str
    generated_at: datetime

    _required_text = field_validator("schema_version", "parser_version", "normalization_version", "validator_version", "prompt_sha256", "model", "document_set_hash", "cache_key", "body_sha256")(_nonblank)
    _aware_time = field_validator("generated_at")(_aware)


class DocumentContract(StrictModel):
    manifest: ContractManifest
    body: ContractBody


class ReviewRecord(StrictModel):
    body_sha256: str
    status: Literal["auto_approved", "manual_approved", "blocked"]
    reviewed_at: datetime
    issues: list[Issue] = Field(default_factory=list)
    manual_change_notes: list[str] = Field(default_factory=list)

    _required_hash = field_validator("body_sha256")(_nonblank)
    _aware_time = field_validator("reviewed_at")(_aware)

    @model_validator(mode="after")
    def auto_approval_requires_no_issues(self) -> "ReviewRecord":
        if self.status == "auto_approved" and self.issues:
            raise ValueError("auto_approved review cannot contain unresolved issues")
        return self


class ContractStore:
    """Store contracts under a ready-last multi-file transaction boundary."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def cache_dir(self, document_set_hash: str, key: str) -> Path:
        if not _DIGEST.fullmatch(document_set_hash) or not _DIGEST.fullmatch(key):
            raise ValueError("document and cache keys must be lowercase SHA-256 digests")
        path = (self.root / "artifacts" / "contracts" / document_set_hash / key).absolute()
        if os.name == "nt" and not str(path).startswith("\\\\?\\"):
            return Path("\\\\?\\" + str(path))
        return path

    def save(self, contract: DocumentContract, review: ReviewRecord) -> None:
        contract = DocumentContract.model_validate(contract.model_dump())
        review = ReviewRecord.model_validate(review.model_dump())
        digest = canonical_body_sha256(contract.body)
        if digest != contract.manifest.body_sha256 or digest != review.body_sha256:
            raise ValueError("body hash must match manifest and review")
        directory = self.cache_dir(contract.manifest.document_set_hash, contract.manifest.cache_key)
        ready_path = directory / "ready.json"
        ready_path.unlink(missing_ok=True)
        body_text = _canonical_json(contract.body)
        manifest_text = _canonical_json(contract.manifest)
        review_text = _canonical_json(review)
        files = {"body.json": body_text, "manifest.json": manifest_text, "review.json": review_text}
        for name, text in files.items():
            atomic_write(directory / name, text)
        ready = {
            "body_sha256": digest,
            "file_sha256": {name: hashlib.sha256(text.encode("utf-8")).hexdigest() for name, text in files.items()},
        }
        atomic_write(ready_path, _canonical_json(ready))

    def load(self, document_set_hash: str, key: str, *, documents: DocumentSet | None = None) -> DocumentContract | None:
        try:
            directory = self.cache_dir(document_set_hash, key)
            ready_path = directory / "ready.json"
            if not ready_path.is_file():
                return None
            ready = json.loads(ready_path.read_text(encoding="utf-8"))
            texts = {name: (directory / name).read_text(encoding="utf-8") for name in ("body.json", "manifest.json", "review.json")}
            expected_files = ready.get("file_sha256")
            if not isinstance(expected_files, dict):
                return None
            for name, text in texts.items():
                if expected_files.get(name) != hashlib.sha256(text.encode("utf-8")).hexdigest():
                    return None
            body = ContractBody.model_validate_json(texts["body.json"])
            manifest = ContractManifest.model_validate_json(texts["manifest.json"])
            review = ReviewRecord.model_validate_json(texts["review.json"])
            digest = canonical_body_sha256(body)
            if manifest.document_set_hash != document_set_hash or manifest.cache_key != key:
                return None
            if ready.get("body_sha256") != digest or manifest.body_sha256 != digest or review.body_sha256 != digest:
                return None
            if review.status == "blocked":
                return None
            if documents is not None:
                if documents.document_set_hash != document_set_hash or not validate_contract(body, documents).valid:
                    return None
            return DocumentContract(manifest=manifest, body=body)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
