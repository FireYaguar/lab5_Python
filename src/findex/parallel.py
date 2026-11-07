"""Parallel index build (Lab 5): map = build_partial per chunk of files, reduce = merge.

The same build_partial/merge run under three executors:
    serial     a plain loop, one chunk (identical to build_index)
    threads    ThreadPoolExecutor  — one process, shares the GIL
    processes  ProcessPoolExecutor — one interpreter (and GIL) per worker, "spawn" start method
"""

from __future__ import annotations

import logging
import multiprocessing
import time
from collections.abc import Callable, Sequence
from concurrent.futures import (
    Executor,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from findex.corpus import source_size
from findex.index import Index, PartialIndex, build_partial, merge

log = logging.getLogger(__name__)

type ExecutorName = Literal["serial", "threads", "processes"]
CHUNKS_PER_WORKER = 4  # several chunks per worker so a slow chunk doesn't idle the others


@dataclass(frozen=True, slots=True)
class Chunk:
    paths: tuple[Path, ...]
    start_id: int


def make_chunks(paths: Sequence[Path], n_chunks: int) -> list[Chunk]:
    """Split the sorted file list into n contiguous chunks of ~equal *bytes*, and give each
    chunk its doc_id range up front (prefix sum of source_size)."""
    if not paths:
        return []
    n_chunks = max(1, min(n_chunks, len(paths)))
    sizes = [p.stat().st_size for p in paths]
    target = sum(sizes) / n_chunks
    chunks: list[Chunk] = []
    current: list[Path] = []
    acc = 0.0
    start = next_id = 0
    for path, size in zip(paths, sizes):
        current.append(path)
        acc += size
        next_id += source_size(path)
        if acc >= target * (len(chunks) + 1) and len(chunks) < n_chunks - 1:
            chunks.append(Chunk(tuple(current), start))
            current, start = [], next_id
    if current:
        chunks.append(Chunk(tuple(current), start))
    return chunks


def _make_executor(name: ExecutorName, workers: int) -> Executor:
    if name == "threads":
        return ThreadPoolExecutor(max_workers=workers)
    # spawn: a fresh interpreter per worker (default on macOS/Windows; safe with threads).
    # Our entry points are guarded by `if __name__ == "__main__"`, so re-import is harmless.
    return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))


@dataclass(frozen=True, slots=True)
class BuildStats:
    chunks: int
    map_seconds: float
    merge_seconds: float


def build_parallel(
    paths: Sequence[Path],
    *,
    workers: int = 1,
    executor: ExecutorName = "serial",
    positions: bool = False,
    on_chunk_done: Callable[[int], None] | None = None,
) -> tuple[Index, BuildStats]:
    """Build the index from `paths`. If a worker raises, the exception (with the worker's
    traceback attached as __cause__) is re-raised here and pending chunks are cancelled."""
    n_chunks = 1 if executor == "serial" else workers * CHUNKS_PER_WORKER
    chunks = make_chunks(paths, n_chunks)
    log.info("%d files -> %d chunks, %s x%d", len(paths), len(chunks), executor, workers)
    t0 = time.perf_counter()
    partials: list[PartialIndex] = []
    if executor == "serial":
        for c in chunks:
            partials.append(build_partial(c.paths, c.start_id, positions))
            if on_chunk_done:
                on_chunk_done(len(c.paths))
    else:
        with _make_executor(executor, workers) as pool:
            futures: dict[Future[PartialIndex], Chunk] = {
                pool.submit(build_partial, c.paths, c.start_id, positions): c for c in chunks
            }
            try:
                for fut in as_completed(futures):
                    partials.append(fut.result())  # re-raises a worker's exception
                    if on_chunk_done:
                        on_chunk_done(len(futures[fut].paths))
            except BaseException:
                pool.shutdown(wait=False, cancel_futures=True)  # fail fast, don't hang
                raise
    t_map = time.perf_counter() - t0
    t1 = time.perf_counter()
    index = merge(partials)
    t_merge = time.perf_counter() - t1
    log.info("map %.2f s, merge %.2f s", t_map, t_merge)
    return index, BuildStats(len(chunks), t_map, t_merge)
