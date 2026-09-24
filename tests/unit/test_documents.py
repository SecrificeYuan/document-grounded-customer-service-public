from __future__ import annotations

from pathlib import Path

import pytest

from docqa.documents import load_documents
from docqa.evidence import EvidenceIndex
from docqa.errors import DocumentError


class FakePage:
    def __init__(self, text: str | None, *, images: list[object] | None = None) -> None:
        self._text = text
        self.images = images or []

    def extract_text(self) -> str | None:
        return self._text


class FakePDF:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages

    def __enter__(self) -> "FakePDF":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def patch_pdf(
    monkeypatch: pytest.MonkeyPatch,
    pages_by_name: dict[str, list[FakePage]],
) -> None:
    def fake_open(path: Path) -> FakePDF:
        return FakePDF(pages_by_name[Path(path).name])

    monkeypatch.setattr("docqa.documents.pdfplumber.open", fake_open)


def test_pages_stability_and_exact_quote(tmp_path: Path) -> None:
    markdown = tmp_path / "manual.md"
    markdown.write_text(
        "# 手册\n\n## 服务范围\n\n面向借阅服务。\n\n"
        "<!-- PAGE_BREAK -->\n\n## 开放时间\n\n每周一开放。\n",
        encoding="utf-8",
    )

    first = load_documents([markdown], max_source_bytes=10_000)
    second = load_documents([markdown], max_source_bytes=10_000)

    assert first.document_set_hash == second.document_set_hash
    assert [block.block_id for block in first.blocks] == [
        block.block_id for block in second.blocks
    ]
    block = next(block for block in first.blocks if "每周一开放" in block.text)
    assert block.page == 2
    assert block.section_path == ["手册", "开放时间"]
    index = EvidenceIndex(first.blocks)
    assert index.quote(block.block_id, "每周一开放。") == "每周一开放。"
    with pytest.raises(ValueError, match="not a continuous substring"):
        index.quote(block.block_id, "每周1开放。")


def test_mixed_directory_requires_explicit_files(tmp_path: Path) -> None:
    (tmp_path / "manual.md").write_text("中文业务说明。", encoding="utf-8")
    (tmp_path / "answers.jsonl").write_text(
        '{"sentinel":"DO_NOT_UPLOAD"}', encoding="utf-8"
    )

    with pytest.raises(DocumentError, match="explicit md/pdf files"):
        load_documents([tmp_path], max_source_bytes=10_000)


def test_document_hash_does_not_depend_on_parent_directory(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    text = "# 规则\n\n期限为十四个自然日。\n"
    first = first_dir / "manual.md"
    second = second_dir / "manual.md"
    first.write_text(text, encoding="utf-8")
    second.write_text(text, encoding="utf-8")

    assert load_documents([first], 10_000).document_set_hash == load_documents(
        [second], 10_000
    ).document_set_hash


def test_same_name_with_different_bytes_is_rejected(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "manual.md"
    second = second_dir / "manual.md"
    first.write_text("版本一。", encoding="utf-8")
    second.write_text("版本二。", encoding="utf-8")

    with pytest.raises(DocumentError, match="same file name has different content"):
        load_documents([first, second], 10_000)


def test_invalid_extension_and_size_limit_are_rejected(tmp_path: Path) -> None:
    text_file = tmp_path / "manual.txt"
    text_file.write_text("规则", encoding="utf-8")
    with pytest.raises(DocumentError, match="unsupported document format"):
        load_documents([text_file], 10_000)

    markdown = tmp_path / "manual.md"
    markdown.write_text("超过限制", encoding="utf-8")
    with pytest.raises(DocumentError, match="source byte limit"):
        load_documents([markdown], 2)


def test_markdown_pdf_pair_uses_pdf_page_text_and_markdown_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    markdown = tmp_path / "manual.md"
    pdf = tmp_path / "manual.pdf"
    markdown.write_text(
        "# 手册\n\n## 期限\n\n期限为１４个自然日。\n\n"
        "<!-- PAGE_BREAK -->\n\n## 客服\n\n周六 10:00—16:00 开放。\n",
        encoding="utf-8",
    )
    pdf.write_bytes(b"%PDF-fake")
    patch_pdf(
        monkeypatch,
        {
            "manual.pdf": [
                FakePage("手册\n期限\n期限为14个自然日。"),
                FakePage("客服\n周六 10:00—16:00 开放。"),
            ]
        },
    )

    result = load_documents([pdf, markdown], max_source_bytes=10_000)

    assert len(result.documents) == 1
    assert result.documents[0].citation_basis == "pdf"
    assert result.documents[0].page_count == 2
    deadline = next(block for block in result.blocks if "14个自然日" in block.text)
    assert deadline.document_name == "manual.pdf"
    assert deadline.text == "期限为14个自然日。"
    assert deadline.section_path == ["手册", "期限"]


def test_pairing_rejects_changed_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    markdown = tmp_path / "manual.md"
    pdf = tmp_path / "manual.pdf"
    markdown.write_text("# 手册\n\n期限为14个自然日。\n", encoding="utf-8")
    pdf.write_bytes(b"%PDF-fake")
    patch_pdf(monkeypatch, {"manual.pdf": [FakePage("手册\n期限为15个自然日。")]})

    with pytest.raises(DocumentError, match="markdown/pdf content mismatch"):
        load_documents([markdown, pdf], max_source_bytes=10_000)


def test_pairing_ignores_pdf_layout_wrap_but_keeps_pdf_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    markdown = tmp_path / "manual.md"
    pdf = tmp_path / "manual.pdf"
    markdown.write_text("# 手册\n\n期限为14个自然日。\n", encoding="utf-8")
    pdf.write_bytes(b"%PDF-fake")
    patch_pdf(monkeypatch, {"manual.pdf": [FakePage("手册\n期限为14个\n自然日。")]})

    result = load_documents([markdown, pdf], max_source_bytes=10_000)

    assert result.blocks[0].text == "期限为14个\n自然日。"


def test_pdf_with_blank_image_page_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(b"%PDF-fake")
    patch_pdf(
        monkeypatch,
        {"manual.pdf": [FakePage("正文"), FakePage(None, images=[object()])]},
    )

    with pytest.raises(DocumentError, match="no extractable text on page 2"):
        load_documents([pdf], max_source_bytes=10_000)


def test_pdf_page_count_must_match_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    markdown = tmp_path / "manual.md"
    pdf = tmp_path / "manual.pdf"
    markdown.write_text(
        "第一页。\n\n<!-- PAGE_BREAK -->\n\n第二页。\n", encoding="utf-8"
    )
    pdf.write_bytes(b"%PDF-fake")
    patch_pdf(monkeypatch, {"manual.pdf": [FakePage("第一页。")]})

    with pytest.raises(DocumentError, match="page count mismatch"):
        load_documents([markdown, pdf], max_source_bytes=10_000)
