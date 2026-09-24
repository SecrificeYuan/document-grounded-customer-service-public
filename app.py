"""Command-line entry point for reproducible document-grounded QA."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from docqa.config import AppConfig
from docqa.errors import ConfigurationError, ContractBlocked, DocumentError, InputBatchError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docqa")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run a question batch")
    run.add_argument("--docs", nargs="+", required=True, type=Path)
    run.add_argument("--questions", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--cache-dir", default=Path("."), type=Path)
    run.add_argument("--as-of", type=str)

    build = commands.add_parser("build-contract", help="build or refresh a contract")
    build.add_argument("--docs", nargs="+", required=True, type=Path)
    build.add_argument("--cache-dir", "--output", dest="cache_dir", default=Path("."), type=Path)
    build.add_argument("--as-of", type=str)
    build.add_argument("--force", action="store_true")

    inspect = commands.add_parser("inspect-contract", help="inspect cached contracts offline")
    inspect.add_argument("--docs", nargs="+", required=True, type=Path)
    inspect.add_argument("--cache-dir", default=Path("."), type=Path)
    inspect.add_argument("--as-of", type=str)

    evaluate = commands.add_parser("evaluate", help="evaluate predictions offline")
    evaluate.add_argument("--predictions", required=True, type=Path)
    evaluate.add_argument("--reference", required=True, type=Path)
    return parser


def _date(value: str | None):
    if value is None:
        return None
    from datetime import date

    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ConfigurationError("--as-of must be YYYY-MM-DD") from error


def _documents(paths: Sequence[Path]):
    from docqa.documents import load_documents

    return load_documents(paths, max_source_bytes=20_000_000)


def _contract_store_root(path: Path) -> Path:
    """Accept either a project/cache root or its artifacts/contracts directory."""

    path = Path(path)
    if (
        path.name.casefold() == "contracts"
        and path.parent.name.casefold() == "artifacts"
    ):
        return path.parent.parent
    return path


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            from docqa.llm_client import DeepSeekClient
            from docqa.pipeline import preflight_batch, run_batch

            preflight_batch(args.docs, args.questions, args.output)
            config = AppConfig.from_env(require_key=True, as_of=_date(args.as_of))
            report = run_batch(
                args.docs, args.questions, args.output, args.cache_dir, config, DeepSeekClient(config)
            )
            print(
                f"processed={report.processed} answered={report.answered} "
                f"handed_off={report.handed_off} item_failures={report.item_failures}"
            )
            return 0
        if args.command == "build-contract":
            from docqa.contract_builder import ContractBuilder
            from docqa.contract_store import ContractStore
            from docqa.llm_client import DeepSeekClient

            config = AppConfig.from_env(require_key=True, as_of=_date(args.as_of))
            documents = _documents(args.docs)
            result = ContractBuilder(
                client=DeepSeekClient(config),
                store=ContractStore(_contract_store_root(args.cache_dir)),
                config=config,
            ).get_or_build(documents, force=args.force)
            print(f"cache_key={result.manifest.cache_key} body_sha256={result.manifest.body_sha256}")
            return 0
        if args.command == "inspect-contract":
            from docqa.contract_store import ContractStore, ReviewRecord

            documents = _documents(args.docs)
            current_model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip()
            if not current_model:
                raise ConfigurationError("DEEPSEEK_MODEL cannot be blank")
            current_effort = "high"
            store_root = _contract_store_root(args.cache_dir)
            root = store_root / "artifacts" / "contracts" / documents.document_set_hash
            manifests = sorted(root.glob("*/manifest.json")) if root.is_dir() else []
            store = ContractStore(store_root)
            candidates = []
            for manifest_path in manifests:
                key = manifest_path.parent.name
                candidate = store.load(
                    documents.document_set_hash, key, documents=documents
                )
                if candidate is not None:
                    review_path = store.cache_dir(documents.document_set_hash, key) / "review.json"
                    review = ReviewRecord.model_validate_json(
                        review_path.read_text(encoding="utf-8")
                    )
                    candidates.append((candidate, review))
            if not candidates:
                raise ContractBlocked("no valid cached contract for selected documents")
            selected, review = max(
                candidates,
                key=lambda item: (
                    item[0].manifest.model == current_model
                    and item[0].manifest.effort == current_effort,
                    item[0].manifest.generated_at,
                ),
            )
            stale = (
                selected.manifest.model != current_model
                or selected.manifest.effort != current_effort
            )
            print(
                f"cache_key={selected.manifest.cache_key} "
                f"body_sha256={selected.manifest.body_sha256} "
                f"review_status={review.status} warnings={len(selected.body.warnings)} "
                f"stale={str(stale).lower()} model={selected.manifest.model} "
                f"effort={selected.manifest.effort}"
            )
            return 0
        from evaluation.evaluate import evaluate

        metrics = evaluate(args.predictions, args.reference)
        print(json.dumps(metrics.model_dump(mode="json"), ensure_ascii=False))
        return 0
    except (ConfigurationError, ContractBlocked, DocumentError, InputBatchError, OSError, ValueError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
