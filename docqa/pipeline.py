"""Order-preserving batch orchestration with atomic final publication."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from pydantic import Field, StrictInt

from docqa.analyzer import LLMClient, analyze_question
from docqa.config import AppConfig
from docqa.contract_builder import ContractBuilder
from docqa.contract_store import ContractStore
from docqa.documents import load_documents
from docqa.errors import ConfigurationError, ContractBlocked
from docqa.jsonl_io import InputBatch, read_input, write_predictions
from docqa.llm_client import Usage
from docqa.models.output import handoff
from docqa.safe_logging import AnalysisAttemptFailure


_MAX_SOURCE_BYTES = 20_000_000


class ItemAttemptFailure(AnalysisAttemptFailure):
    """Content-free failed-attempt telemetry keyed only by batch position."""

    item_number: StrictInt = Field(ge=1)


@dataclass(frozen=True, slots=True)
class BatchReport:
    processed: int
    answered: int
    handed_off: int
    item_failures: int
    attempt_failures: tuple[ItemAttemptFailure, ...]
    output_path: Path
    usage: Usage
    elapsed_seconds: float


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def preflight_batch(
    docs: Sequence[Path], questions: Path, output: Path
) -> tuple[InputBatch, list[Path]]:
    """Validate the whole input identity and every protected source path."""

    questions = Path(questions)
    batch = read_input(questions)
    protected = [questions]
    for supplied in docs:
        path = Path(supplied)
        protected.append(path)
        if path.is_dir():
            protected.extend(item for item in path.iterdir() if item.is_file())
    if _path_key(Path(output)) in {_path_key(path) for path in protected}:
        raise ValueError("output path matches a protected input path")
    return batch, protected


def run_batch(
    docs: Sequence[Path],
    questions: Path,
    output: Path,
    cache_dir: Path,
    config: AppConfig,
    client: LLMClient,
    *,
    pinned_contract_key: str | None = None,
) -> BatchReport:
    """Run a complete batch, isolating only expected per-item failures."""

    started = time.monotonic()
    questions = Path(questions)
    output = Path(output)
    docs = [Path(path) for path in docs]
    batch, protected = preflight_batch(docs, questions, output)
    if config.api_key is None:
        raise ConfigurationError("DEEPSEEK_API_KEY is required for run")

    documents = load_documents(docs, max_source_bytes=_MAX_SOURCE_BYTES)
    store = ContractStore(Path(cache_dir))
    if pinned_contract_key is None:
        contract = ContractBuilder(
            client=client,
            store=store,
            config=config,
        ).get_or_build(documents)
    else:
        contract = store.load(
            documents.document_set_hash,
            pinned_contract_key,
            documents=documents,
        )
        if contract is None:
            raise ContractBlocked("pinned experiment contract is unavailable or invalid")

    outputs = []
    failures = 0
    attempt_failures: list[ItemAttemptFailure] = []
    for item_number, line in enumerate(batch.lines, start=1):
        if line.record is None:
            outputs.append(handoff(line.output_id, "AMBIGUOUS"))
            failures += 1
            continue
        item_failures: list[str] = []
        item_attempt_failures: list[AnalysisAttemptFailure] = []
        outputs.append(
            analyze_question(
                line.record,
                documents,
                contract.body,
                client,
                config,
                on_technical_failure=item_failures.append,
                on_attempt_failure=item_attempt_failures.append,
            )
        )
        failures += bool(item_failures)
        attempt_failures.extend(
            ItemAttemptFailure(
                item_number=item_number,
                **event.model_dump(),
            )
            for event in item_attempt_failures
        )

    write_predictions(output, outputs, protected_paths=protected)
    return BatchReport(
        processed=len(outputs),
        answered=sum(item.decision == "answer" for item in outputs),
        handed_off=sum(item.decision == "handoff" for item in outputs),
        item_failures=failures,
        attempt_failures=tuple(attempt_failures),
        output_path=output.resolve(strict=False),
        usage=Usage(
            input_tokens=None,
            output_tokens=None,
            cached_tokens=None,
            reasoning_tokens=None,
        ),
        elapsed_seconds=max(0.0, time.monotonic() - started),
    )
