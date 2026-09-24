"""Generate, validate, repair, and cache evidence-bound document contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from docqa.config import AppConfig
from docqa.contract_store import (
    ContractManifest,
    ContractStore,
    DocumentContract,
    ReviewRecord,
    cache_key,
    canonical_body_sha256,
)
from docqa.contract_validator import validate_contract
from docqa.errors import ContractBlocked
from docqa.llm_client import ModelReply, ModelRequest, model_request_payload
from docqa.models.common import Issue
from docqa.models.contract import ContractBody
from docqa.models.documents import DocumentSet


_SCHEMA_VERSION = "contract-body-v1"
_PARSER_VERSION = "documents-v1"
_NORMALIZATION_VERSION = "zh-normalization-v1"
_VALIDATOR_VERSION = "contract-validator-v8"
_DEFAULT_MAX_REQUEST_BYTES = 2_000_000
_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


class LLMClient(Protocol):
    def request(self, request: ModelRequest) -> ModelReply: ...


def _read_prompt(name: str) -> str:
    try:
        text = (_PROMPT_DIR / name).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ContractBlocked(f"cannot read contract prompt: {name}") from error
    if not text.strip():
        raise ContractBlocked(f"contract prompt is blank: {name}")
    return text


def _prompt_hash(version: str, build_prompt: str, repair_prompt: str) -> str:
    payload = "\0".join((version, build_prompt, repair_prompt)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _stable_context(documents: DocumentSet) -> str:
    source_documents = sorted(
        (document.model_dump(mode="json") for document in documents.documents),
        key=lambda item: item["document_id"],
    )
    blocks = sorted(
        (block.model_dump(mode="json") for block in documents.blocks),
        key=lambda item: (
            item["document_id"],
            item["page"],
            item["ordinal"],
            item["block_id"],
        ),
    )
    payload = {
        "document_set_hash": documents.document_set_hash,
        "documents": source_documents,
        "evidence_blocks": blocks,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ContractBuilder:
    """Build one contract with one shared semantic repair opportunity."""

    def __init__(
        self,
        *,
        client: LLMClient,
        store: ContractStore,
        config: AppConfig,
        prompt_version: str = "contract-builder-v1",
        max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not prompt_version.strip():
            raise ValueError("prompt_version cannot be blank")
        if max_request_bytes <= 0:
            raise ValueError("max_request_bytes must be positive")
        self.client = client
        self.store = store
        self.config = config
        self.prompt_version = prompt_version
        self.max_request_bytes = max_request_bytes
        self._now = now
        self._build_prompt = _read_prompt("contract_builder.md")
        self._repair_prompt = _read_prompt("contract_repair.md")
        self._schema = ContractBody.model_json_schema()
        self._prompt_sha256 = _prompt_hash(
            prompt_version, self._build_prompt, self._repair_prompt
        )

    def _cache_key(self, documents: DocumentSet) -> str:
        pairing = [
            {
                "document_id": document.document_id,
                "formats": sorted(source.format for source in document.source_files),
                "citation_basis": document.citation_basis,
            }
            for document in sorted(documents.documents, key=lambda item: item.document_id)
        ]
        versions = {
            "schema": _SCHEMA_VERSION,
            "parser": _PARSER_VERSION,
            "normalization": _NORMALIZATION_VERSION,
            "validator": _VALIDATOR_VERSION,
            "prompt": self.prompt_version,
        }
        return cache_key(
            documents,
            pairing,
            versions,
            self._prompt_sha256,
            self.config.model,
            self.config.reasoning_effort,
        )

    def _check_budget(self, request: ModelRequest) -> None:
        wire_payload = json.dumps(
            model_request_payload(self.config, request),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        byte_count = len(wire_payload.encode("utf-8"))
        if byte_count > self.max_request_bytes:
            raise ContractBlocked(
                "contract input and schema exceed the UTF-8 request byte limit; "
                "the first release supports only document sets that fit in one context"
            )

    def _request(
        self,
        *,
        purpose: str,
        instructions: str,
        input_text: str,
        max_output_tokens: int,
    ) -> ModelReply:
        request = ModelRequest(
            purpose=purpose,
            instructions=instructions,
            input_text=input_text,
            schema_name="document_contract_body",
            schema=self._schema,
            max_output_tokens=max_output_tokens,
        )
        self._check_budget(request)
        return self.client.request(request)

    @staticmethod
    def _parse_and_validate(
        reply: ModelReply, documents: DocumentSet
    ) -> tuple[ContractBody | None, list[Issue], str]:
        if reply.status != "completed":
            code = (
                "MODEL_INCOMPLETE"
                if reply.status == "incomplete"
                else "INVALID_MODEL_REPLY"
            )
            return None, [Issue(code=code, message=code, object_ids=[])], reply.text
        if not reply.text.strip():
            return None, [Issue(code="EMPTY_MODEL_REPLY", message="EMPTY_MODEL_REPLY", object_ids=[])], reply.text
        text = reply.text
        try:
            body = ContractBody.model_validate_json(text)
        except ValidationError as error:
            safe_errors = []
            for item in error.errors(include_url=False, include_input=False):
                location = ".".join(str(part) for part in item["loc"]) or "<root>"
                safe_errors.append(f"{item['type']} at {location}")
            return None, [Issue(code="INVALID_CONTRACT_SCHEMA", message="; ".join(safe_errors), object_ids=[])], text
        report = validate_contract(body, documents)
        return body, report.issues, text

    def get_or_build(
        self, documents: DocumentSet, force: bool = False
    ) -> DocumentContract:
        """Return a valid cached contract or build it with at most one repair."""

        key = self._cache_key(documents)
        if not force:
            cached = self.store.load(
                documents.document_set_hash, key, documents=documents
            )
            if cached is not None:
                return cached

        stable_context = _stable_context(documents)
        reply = self._request(
            purpose="contract",
            instructions=self._build_prompt,
            input_text=stable_context,
            max_output_tokens=self.config.contract_max_output_tokens,
        )
        body, issues, previous_text = self._parse_and_validate(reply, documents)

        if issues:
            allowed_ids = sorted(block.block_id for block in documents.blocks)
            repair_input = json.dumps(
                {
                    "source_documents": json.loads(stable_context),
                    "allowed_evidence_ids": allowed_ids,
                    "validation_errors": [
                        issue.model_dump(mode="json") for issue in issues
                    ],
                    "previous_final_json": previous_text,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            reply = self._request(
                purpose="repair",
                instructions=self._repair_prompt,
                input_text=repair_input,
                max_output_tokens=self.config.contract_max_output_tokens,
            )
            body, issues, _ = self._parse_and_validate(reply, documents)

        if body is None or issues:
            codes = ", ".join(sorted({issue.code for issue in issues})) or "UNKNOWN"
            raise ContractBlocked(
                f"contract invalid after two semantic attempts: {codes}"
            )

        digest = canonical_body_sha256(body)
        generated_at = self._now()
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ValueError("builder clock must return a timezone-aware datetime")
        manifest = ContractManifest(
            schema_version=_SCHEMA_VERSION,
            parser_version=_PARSER_VERSION,
            normalization_version=_NORMALIZATION_VERSION,
            validator_version=_VALIDATOR_VERSION,
            prompt_sha256=self._prompt_sha256,
            model=self.config.model,
            effort=self.config.reasoning_effort,
            document_set_hash=documents.document_set_hash,
            cache_key=key,
            body_sha256=digest,
            generated_at=generated_at,
        )
        review = ReviewRecord(
            body_sha256=digest,
            status="auto_approved",
            reviewed_at=generated_at,
            issues=[],
            manual_change_notes=[],
        )
        contract = DocumentContract(manifest=manifest, body=body)
        self.store.save(contract, review)
        return contract
