"""Strict document and immutable evidence data models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictInt, field_validator

from docqa.models.common import StrictModel


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text fields cannot be blank")
    return value


class SourceFile(StrictModel):
    name: str
    relative_path: str
    sha256: str
    format: Literal["md", "pdf"]

    _required_text = field_validator("name", "relative_path", "sha256")(_nonblank)


class SourceDocument(StrictModel):
    document_id: str
    display_name: str
    source_files: list[SourceFile] = Field(min_length=1)
    page_count: StrictInt = Field(ge=1)
    citation_basis: Literal["pdf", "markdown"]

    _required_text = field_validator("document_id", "display_name")(_nonblank)


class EvidenceBlock(StrictModel):
    block_id: str
    document_id: str
    document_name: str
    page: StrictInt = Field(ge=1)
    section_path: list[str] = Field(default_factory=list)
    ordinal: StrictInt = Field(ge=1)
    text: str
    source_sha256: str

    _required_text = field_validator(
        "block_id", "document_id", "document_name", "text", "source_sha256"
    )(_nonblank)


class DocumentSet(StrictModel):
    document_set_hash: str
    documents: list[SourceDocument] = Field(min_length=1)
    blocks: list[EvidenceBlock] = Field(min_length=1)
    full_context: str

    _required_text = field_validator("document_set_hash", "full_context")(_nonblank)
