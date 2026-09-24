"""Crash-safe replacement of complete UTF-8 text files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, text: str) -> None:
    """Flush a temporary sibling and atomically replace ``path``.

    If writing, flushing, or replacement fails, any prior destination remains
    untouched and the temporary file is removed.
    """

    if not isinstance(path, Path):
        raise TypeError("path must be a pathlib.Path")
    if not isinstance(text, str):
        raise TypeError("text must be str")

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
