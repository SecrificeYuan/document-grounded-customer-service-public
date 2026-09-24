"""Resolution of model-selected IDs to immutable source evidence."""

from __future__ import annotations

from collections.abc import Iterable

from docqa.models.documents import EvidenceBlock


class EvidenceIndex:
    """Unique evidence blocks with exact-substring quote enforcement."""

    def __init__(self, blocks: Iterable[EvidenceBlock]) -> None:
        self._blocks: dict[str, EvidenceBlock] = {}
        for block in blocks:
            if block.block_id in self._blocks:
                raise ValueError(f"duplicate evidence id: {block.block_id}")
            self._blocks[block.block_id] = block

    def resolve(self, block_id: str) -> EvidenceBlock:
        try:
            return self._blocks[block_id]
        except KeyError as error:
            raise KeyError(f"unknown evidence id: {block_id}") from error

    def quote(self, block_id: str, fragment: str) -> str:
        if not isinstance(fragment, str) or not fragment:
            raise ValueError("quote fragment cannot be empty")
        block = self.resolve(block_id)
        if fragment not in block.text:
            raise ValueError("quote is not a continuous substring of the evidence block")
        return fragment
