"""Inverted index: term -> postings sorted by doc_id. Built in ONE pass over the lazy corpus."""

from __future__ import annotations

import functools
import logging
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from findex.corpus import Document, iter_paths
from findex.timing import timed
from findex.tokenize import tokenize

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Posting:
    doc_id: int
    tf: int  # term frequency in this document
    positions: tuple[int, ...] = ()  # token ordinals (0, 1, 2, ...) — only with --positions


@dataclass(frozen=True, slots=True)
class DocMeta:
    doc_id: int
    path: str
    title: str


class Index(Mapping[str, list[Posting]]):
    """The index behaves like a read-only dict: len(ix), "term" in ix, ix[term], for t in ix.

    Inheriting collections.abc.Mapping gives keys()/items()/get()/== for free from
    __getitem__, __len__ and __iter__.
    """

    def __init__(
        self,
        postings: dict[str, list[Posting]] | None = None,
        doc_lengths: dict[int, int] | None = None,
        doc_meta: dict[int, DocMeta] | None = None,
        has_positions: bool = False,
    ) -> None:
        self.postings: dict[str, list[Posting]] = postings if postings is not None else {}
        self.doc_lengths: dict[int, int] = doc_lengths if doc_lengths is not None else {}
        self.doc_meta: dict[int, DocMeta] = doc_meta if doc_meta is not None else {}
        self.has_positions = has_positions
        self._init_cache()

    # --- the data model: built-ins dispatch here ---
    def __getitem__(self, term: str) -> list[Posting]:
        return self.postings[term]  # KeyError if the term is absent

    def __contains__(self, term: object) -> bool:
        return term in self.postings  # faster than Mapping's default (try/except __getitem__)

    def __iter__(self) -> Iterator[str]:
        return iter(self.postings)

    def __len__(self) -> int:
        return len(self.postings)  # number of terms

    def __repr__(self) -> str:
        return f"Index(terms={len(self):_}, docs={self.num_docs:_}, positions={self.has_positions})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Index):
            return NotImplemented
        return (
            self.postings == other.postings
            and self.doc_lengths == other.doc_lengths
            and self.doc_meta == other.doc_meta
            and self.has_positions == other.has_positions
        )

    __hash__ = None  # type: ignore[assignment]  # mutable container with value equality

    # --- computed attributes ---
    @property
    def num_docs(self) -> int:
        return len(self.doc_lengths)

    @functools.cached_property
    def avg_doc_length(self) -> float:  # needed for every BM25 score: compute once
        return sum(self.doc_lengths.values()) / self.num_docs if self.num_docs else 0.0

    @functools.cached_property
    def all_doc_ids(self) -> list[int]:
        return sorted(self.doc_lengths)

    def doc_length(self, doc_id: int) -> int:
        return self.doc_lengths[doc_id]

    def df(self, term: str) -> int:
        return len(self.postings.get(term, ()))

    def doc_ids(self, term: str) -> list[int]:
        return [p.doc_id for p in self.postings.get(term, ())]

    # --- query cache: query string -> matching doc ids (see search.match) ---
    def _init_cache(self) -> None:
        from findex.query import parse  # local import: query.py imports Index

        @functools.lru_cache(maxsize=256)
        @timed  # inside the cache: logged only on a miss
        def match(query: str) -> tuple[int, ...]:
            return tuple(parse(query).evaluate(self))

        self.match = match  # per-instance lru_cache, freed together with the index

    def close(self) -> None:
        """Release what the index holds (caches now; an mmap later)."""
        self.match.cache_clear()

    # pickle: store only the data, not the cache closure or cached properties
    def __getstate__(self) -> dict[str, Any]:
        return {
            "postings": self.postings,
            "doc_lengths": self.doc_lengths,
            "doc_meta": self.doc_meta,
            "has_positions": self.has_positions,
        }

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__init__(**state)


type RawPosting = tuple[int, int, tuple[int, ...]]  # (doc_id, tf, positions)


@dataclass(slots=True)
class PartialIndex:
    """What one worker returns: postings for its own doc_id range (Lab 5).

    Postings are plain tuples (doc_id, tf, positions), not Posting objects: tuples pickle
    ~10x faster between processes; merge() turns them into Postings in the parent."""

    start_id: int
    postings: dict[str, list[RawPosting]]
    doc_lengths: dict[int, int]
    doc_meta: dict[int, DocMeta]
    has_positions: bool


def _accumulate(docs: Iterable[Document], positions: bool, start_id: int = 0) -> PartialIndex:
    """One streaming pass. Per document we keep a Counter (or term -> positions),
    flush it into the postings and drop it. doc_ids arrive increasing, so every
    postings list is sorted by doc_id without an explicit sort."""
    postings: defaultdict[str, list[RawPosting]] = defaultdict(list)
    doc_lengths: dict[int, int] = {}
    doc_meta: dict[int, DocMeta] = {}
    for doc in docs:
        if positions:
            pos: defaultdict[str, list[int]] = defaultdict(list)
            n = 0
            for n, tok in enumerate(tokenize(doc.text), 1):
                pos[tok].append(n - 1)
            for term, ps in pos.items():
                postings[term].append((doc.doc_id, len(ps), tuple(ps)))
            length = n
        else:
            counts = Counter(tokenize(doc.text))
            for term, tf in counts.items():
                postings[term].append((doc.doc_id, tf, ()))
            length = counts.total()
        doc_lengths[doc.doc_id] = length
        doc_meta[doc.doc_id] = DocMeta(doc.doc_id, doc.path, doc.title)
    return PartialIndex(start_id, dict(postings), doc_lengths, doc_meta, positions)


@timed
def build_index(docs: Iterable[Document], *, positions: bool = False) -> Index:
    """Single-process build from any stream of documents."""
    return merge([_accumulate(docs, positions)])


def build_partial(paths: Sequence[Path], start_id: int, positions: bool = False) -> PartialIndex:
    """Index the files `paths`; their doc_ids start at start_id (ranges are assigned up
    front, so ids are unique across chunks). Module-level => picklable for processes.
    Gets *paths*, not documents: each worker reads its own files."""
    return _accumulate(iter_paths(paths, start_id), positions, start_id)


@timed
def merge(partials: Iterable[PartialIndex]) -> Index:
    """Reduce step: concatenate postings per term in doc_id order. Partials cover
    disjoint, increasing id ranges, so concatenation keeps every list sorted.
    Sequential — the Amdahl part of a parallel build."""
    parts = sorted(partials, key=lambda p: p.start_id)
    postings: dict[str, list[Posting]] = {}
    doc_lengths: dict[int, int] = {}
    doc_meta: dict[int, DocMeta] = {}
    for part in parts:
        for term, raw in part.postings.items():
            objs = [Posting(d, tf, pos) for d, tf, pos in raw]
            existing = postings.get(term)
            if existing is None:
                postings[term] = objs
            else:
                existing.extend(objs)
        doc_lengths.update(part.doc_lengths)
        doc_meta.update(part.doc_meta)
    positions = parts[0].has_positions if parts else False
    return Index(postings, doc_lengths, doc_meta, positions)
