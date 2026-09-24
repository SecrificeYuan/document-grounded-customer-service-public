"""Deterministic ingestion of explicitly selected Markdown and text PDFs."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pdfplumber

from docqa.errors import DocumentError
from docqa.models.documents import (
    DocumentSet,
    EvidenceBlock,
    SourceDocument,
    SourceFile,
)
from docqa.zh_normalization import normalize_surface


_PARSER_VERSION = "documents-v1"
_PAGE_BREAK = re.compile(r"\s*<!--\s*PAGE_BREAK\s*-->\s*|\f")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_LIST_ITEM = re.compile(r"^[ \t]*[-*+][ \t]+(.+)$")


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    page: int
    section_path: tuple[str, ...]
    text: str


@dataclass(frozen=True, slots=True)
class ParsedMarkdown:
    pages: tuple[str, ...]
    blocks: tuple[ParsedBlock, ...]


@dataclass(frozen=True, slots=True)
class ParsedPDF:
    pages: tuple[str, ...]


def _flush_paragraph(
    buffer: list[str],
    *,
    page: int,
    section_path: list[str],
    blocks: list[ParsedBlock],
) -> None:
    if not buffer:
        return
    text = "".join(buffer).strip()
    buffer.clear()
    if text:
        blocks.append(ParsedBlock(page, tuple(section_path), text))


def parse_markdown(path: Path) -> ParsedMarkdown:
    """Parse page markers and headings while retaining contiguous source text."""

    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise DocumentError(f"cannot read markdown document: {path.name}") from error
    pages = tuple(_PAGE_BREAK.split(source))
    if not pages or not any(page.strip() for page in pages):
        raise DocumentError(f"markdown document has no content: {path.name}")

    blocks: list[ParsedBlock] = []
    section_path: list[str] = []
    for page_number, page_text in enumerate(pages, start=1):
        paragraph: list[str] = []
        for line in page_text.splitlines(keepends=True):
            plain = line.rstrip("\r\n")
            heading = _HEADING.match(plain)
            if heading:
                _flush_paragraph(
                    paragraph,
                    page=page_number,
                    section_path=section_path,
                    blocks=blocks,
                )
                level = len(heading.group(1))
                section_path = section_path[: level - 1]
                section_path.append(heading.group(2).strip())
                continue
            list_item = _LIST_ITEM.match(plain)
            if list_item:
                _flush_paragraph(
                    paragraph,
                    page=page_number,
                    section_path=section_path,
                    blocks=blocks,
                )
                item = list_item.group(1).strip()
                if item:
                    blocks.append(
                        ParsedBlock(page_number, tuple(section_path), item)
                    )
                continue
            if not plain.strip():
                _flush_paragraph(
                    paragraph,
                    page=page_number,
                    section_path=section_path,
                    blocks=blocks,
                )
                continue
            paragraph.append(line)
        _flush_paragraph(
            paragraph,
            page=page_number,
            section_path=section_path,
            blocks=blocks,
        )

    if not blocks:
        raise DocumentError(f"markdown document has no evidence text: {path.name}")
    return ParsedMarkdown(pages=pages, blocks=tuple(blocks))


def parse_pdf(path: Path) -> ParsedPDF:
    """Extract complete page text or fail closed on blank/image-only pages."""

    try:
        with pdfplumber.open(path) as document:
            if not document.pages:
                raise DocumentError(f"pdf document has no pages: {path.name}")
            pages: list[str] = []
            for page_number, page in enumerate(document.pages, start=1):
                text = page.extract_text()
                if text is None or not text.strip():
                    raise DocumentError(
                        f"pdf has no extractable text on page {page_number}: {path.name}"
                    )
                pages.append(text)
    except DocumentError:
        raise
    except Exception as error:
        raise DocumentError(f"cannot parse pdf document: {path.name}") from error
    return ParsedPDF(pages=tuple(pages))


def _normalize_with_offsets(text: str) -> tuple[str, list[tuple[int, int]]]:
    characters: list[str] = []
    offsets: list[tuple[int, int]] = []
    for index, original in enumerate(text):
        for character in unicodedata.normalize("NFKC", original):
            if character.isspace():
                if characters and characters[-1] != " ":
                    characters.append(" ")
                    offsets.append((index, index + 1))
            else:
                characters.append(character)
                offsets.append((index, index + 1))
    while characters and characters[0] == " ":
        characters.pop(0)
        offsets.pop(0)
    while characters and characters[-1] == " ":
        characters.pop()
        offsets.pop()
    return "".join(characters), offsets


def _pairing_with_offsets(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Build a layout-insensitive key while retaining original PDF offsets."""

    normalized, offsets = _normalize_with_offsets(text)
    kept = [
        (character, offset)
        for character, offset in zip(normalized, offsets, strict=True)
        if character != " "
    ]
    return "".join(character for character, _ in kept), [
        offset for _, offset in kept
    ]


def _map_markdown_to_pdf(
    markdown: ParsedMarkdown,
    pdf: ParsedPDF,
    *,
    stem: str,
) -> list[ParsedBlock]:
    if len(markdown.pages) != len(pdf.pages):
        raise DocumentError(f"markdown/pdf page count mismatch: {stem}")

    mapped: list[ParsedBlock] = []
    page_cursors = {page: 0 for page in range(1, len(pdf.pages) + 1)}
    normalized_pages: dict[int, tuple[str, list[tuple[int, int]]]] = {
        number: _pairing_with_offsets(text)
        for number, text in enumerate(pdf.pages, start=1)
    }
    for block in markdown.blocks:
        needle = normalize_surface(block.text).replace(" ", "")
        haystack, offsets = normalized_pages[block.page]
        start = haystack.find(needle, page_cursors[block.page])
        if start < 0:
            raise DocumentError(
                f"markdown/pdf content mismatch on page {block.page}: {stem}"
            )
        end = start + len(needle)
        source_start = offsets[start][0]
        source_end = offsets[end - 1][1]
        fragment = pdf.pages[block.page - 1][source_start:source_end]
        if normalize_surface(fragment).replace(" ", "") != needle:
            raise DocumentError(
                f"markdown/pdf source mapping failed on page {block.page}: {stem}"
            )
        mapped.append(ParsedBlock(block.page, block.section_path, fragment))
        page_cursors[block.page] = end
    return mapped


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise DocumentError(f"cannot read document bytes: {path.name}") from error
    return digest.hexdigest()


def _expand_paths(paths: Sequence[Path]) -> list[Path]:
    if not paths:
        raise DocumentError("at least one document path is required")
    expanded: list[Path] = []
    for supplied in paths:
        path = Path(supplied)
        if path.is_dir():
            entries = sorted(path.iterdir(), key=lambda item: item.name.casefold())
            if any(
                not item.is_file() or item.suffix.lower() not in {".md", ".pdf"}
                for item in entries
            ):
                raise DocumentError(
                    "document directory must contain only explicit md/pdf files"
                )
            expanded.extend(entries)
        else:
            expanded.append(path)

    unique: list[Path] = []
    seen_resolved: set[str] = set()
    for path in expanded:
        if path.suffix.lower() not in {".md", ".pdf"}:
            raise DocumentError(f"unsupported document format: {path.name}")
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise DocumentError(f"document does not exist: {path}") from error
        key = str(resolved).casefold()
        if key not in seen_resolved:
            seen_resolved.add(key)
            unique.append(resolved)
    if not unique:
        raise DocumentError("no documents were selected")
    return unique


def load_documents(paths: Sequence[Path], max_source_bytes: int) -> DocumentSet:
    """Load an explicit, deterministic document set and evidence index."""

    if max_source_bytes <= 0:
        raise DocumentError("source byte limit must be positive")
    selected = _expand_paths(paths)
    total_bytes = sum(path.stat().st_size for path in selected)
    if total_bytes > max_source_bytes:
        raise DocumentError("selected documents exceed the source byte limit")

    hashes = {path: _sha256(path) for path in selected}
    by_name: dict[str, str] = {}
    deduplicated: list[Path] = []
    for path in selected:
        key = path.name.casefold()
        if key in by_name and by_name[key] != hashes[path]:
            raise DocumentError(
                f"same file name has different content: {path.name}"
            )
        if key not in by_name:
            by_name[key] = hashes[path]
            deduplicated.append(path)
    selected = deduplicated

    groups: dict[str, dict[str, Path]] = {}
    for path in selected:
        format_name = path.suffix.lower().lstrip(".")
        group = groups.setdefault(path.stem.casefold(), {})
        if format_name in group:
            raise DocumentError(f"duplicate document format for stem: {path.stem}")
        group[format_name] = path

    source_manifest = [
        {
            "name": path.name,
            "sha256": hashes[path],
            "format": path.suffix.lower().lstrip("."),
        }
        for path in sorted(selected, key=lambda item: item.name.casefold())
    ]
    pairing = [
        {"stem": stem, "formats": sorted(group)}
        for stem, group in sorted(groups.items())
    ]
    canonical = json.dumps(
        {"parser": _PARSER_VERSION, "sources": source_manifest, "pairing": pairing},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    document_set_hash = hashlib.sha256(canonical).hexdigest()

    documents: list[SourceDocument] = []
    evidence: list[EvidenceBlock] = []
    for document_number, (stem, group) in enumerate(sorted(groups.items()), start=1):
        document_id = f"DOC-{document_number:02d}"
        markdown = parse_markdown(group["md"]) if "md" in group else None
        pdf = parse_pdf(group["pdf"]) if "pdf" in group else None
        if markdown is not None and pdf is not None:
            parsed_blocks = _map_markdown_to_pdf(markdown, pdf, stem=stem)
            page_count = len(pdf.pages)
            citation_basis = "pdf"
            display_name = group["pdf"].name
            source_hash = hashes[group["pdf"]]
        elif markdown is not None:
            parsed_blocks = list(markdown.blocks)
            page_count = len(markdown.pages)
            citation_basis = "markdown"
            display_name = group["md"].name
            source_hash = hashes[group["md"]]
        elif pdf is not None:
            parsed_blocks = [
                ParsedBlock(page, (), text)
                for page, text in enumerate(pdf.pages, start=1)
            ]
            page_count = len(pdf.pages)
            citation_basis = "pdf"
            display_name = group["pdf"].name
            source_hash = hashes[group["pdf"]]
        else:
            raise DocumentError(f"document group has no usable source: {stem}")

        source_files = [
            SourceFile(
                name=path.name,
                relative_path=path.name,
                sha256=hashes[path],
                format=path.suffix.lower().lstrip("."),
            )
            for path in sorted(group.values(), key=lambda item: item.name.casefold())
        ]
        documents.append(
            SourceDocument(
                document_id=document_id,
                display_name=display_name,
                source_files=source_files,
                page_count=page_count,
                citation_basis=citation_basis,
            )
        )
        for ordinal, block in enumerate(parsed_blocks, start=1):
            evidence.append(
                EvidenceBlock(
                    block_id=f"{document_id}-P{block.page:03d}-B{ordinal:03d}",
                    document_id=document_id,
                    document_name=display_name,
                    page=block.page,
                    section_path=list(block.section_path),
                    ordinal=ordinal,
                    text=block.text,
                    source_sha256=source_hash,
                )
            )

    full_context = "\n\n".join(
        f"[{block.block_id}] {block.document_name} 第{block.page}页\n{block.text}"
        for block in evidence
    )
    return DocumentSet(
        document_set_hash=document_set_hash,
        documents=documents,
        blocks=evidence,
        full_context=full_context,
    )
