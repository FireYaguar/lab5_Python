"""Lazy corpus reader: yields one document at a time, never the whole corpus."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

TEXT_SUFFIXES = frozenset({".txt", ".rst", ".md"})


@dataclass(frozen=True, slots=True)
class Document:
    doc_id: int
    path: str  # file path, or "file.jsonl#line" / URL for JSON-lines corpora
    title: str
    text: str


def _title_from(text: str, fallback: str) -> str:
    """First non-empty line that is not an rst directive/field or a decoration line."""
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line.startswith(("..", ":")):
            continue
        if line and not set(line) <= set("=-~*^#.:`'\"+_ "):
            return line[:120]
    return fallback


def _iter_jsonl(path: Path, start_id: int) -> Iterator[Document]:
    """doc_id = start_id + line index, so ids depend only on the file, not on what was
    skipped before (bad/blank lines leave gaps). That makes ids stable when the corpus
    is split into chunks for parallel indexing (Lab 5)."""
    with path.open(encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):  # the file is an iterator of lines
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                text = str(rec["text"])
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                log.warning("skip %s:%d: %s", path, i + 1, e)
                continue
            src = str(rec.get("url") or f"{path}#{i + 1}")
            title = str(rec.get("title") or _title_from(text, src))
            yield Document(start_id + i, src, title, text)


def list_sources(root: Path) -> list[Path]:
    """All corpus files in a stable (sorted) order. Only paths are materialized, not text."""
    paths = [root] if root.is_file() else sorted(root.rglob("*"))
    return [p for p in paths if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES | {".jsonl"}]


def source_size(path: Path) -> int:
    """How many doc_ids a source reserves: 1 for a text file, one per line for .jsonl."""
    if path.suffix.lower() == ".jsonl":
        with path.open("rb") as f:
            return sum(1 for _ in f)
    return 1


def count_documents(root: Path) -> int:
    """Upper bound for a progress bar (= the doc_id range of the whole corpus)."""
    return sum(source_size(p) for p in list_sources(root))


def iter_source(path: Path, start_id: int) -> Iterator[Document]:
    """Documents of one file; ids start at start_id."""
    if path.suffix.lower() == ".jsonl":
        yield from _iter_jsonl(path, start_id)
        return
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        log.warning("bad UTF-8 in %s, replacing undecodable bytes", path)
        text = raw.decode("utf-8", errors="replace")
    yield Document(start_id, str(path), _title_from(text, path.stem), text)


def iter_paths(paths: Iterable[Path], start_id: int = 0) -> Iterator[Document]:
    """Documents of several sources; each source gets the next source_size() ids."""
    for path in paths:
        yield from iter_source(path, start_id)
        start_id += source_size(path)


def iter_documents(root: Path) -> Iterator[Document]:
    """Yield documents from a directory tree (.txt/.rst/.md) and/or .jsonl files.

    Generator: opens one file at a time. Files that are not valid UTF-8 are read
    with errors="replace" and logged, so one bad file never stops a build.
    """
    yield from iter_paths(list_sources(root))
