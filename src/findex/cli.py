"""findex command line: `findex index | search | stats`.

Data (tables, --json) goes to stdout; diagnostics (logs, progress bar, errors) go to stderr.
Logging is configured once, here, in the root callback.
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import time
from enum import StrEnum
from itertools import islice
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from findex import __version__
from findex.corpus import iter_documents, list_sources
from findex.parallel import build_parallel
from findex.query import QueryError
from findex.search import match, search
from findex.stats import compute_stats, compute_stats_eager, measure
from findex.store import open_index, save

app = typer.Typer(help="findex — a small full-text search engine.", no_args_is_help=True)
out = Console()  # stdout: data
err = Console(stderr=True)  # stderr: diagnostics
log = logging.getLogger("findex.cli")


class ExecutorChoice(StrEnum):
    serial = "serial"
    threads = "threads"
    processes = "processes"


class ScorerName(StrEnum):  # typer needs an Enum for choices; the library uses Literal
    bm25 = "bm25"
    tfidf = "tfidf"


def fail(message: str, code: int = 1) -> NoReturn:
    """Expected error: one clear line on stderr, non-zero exit, no traceback."""
    err.print(f"[bold red]error:[/] {message}", highlight=False)
    raise typer.Exit(code)


def _version(value: bool) -> None:
    if value:
        out.print(f"findex {__version__}")
        raise typer.Exit


@app.callback()
def root(
    verbose: Annotated[
        int, typer.Option("--verbose", "-v", count=True, help="-v info, -vv debug")
    ] = 0,
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version.")
    ] = False,
) -> None:
    level = logging.WARNING if verbose == 0 else logging.INFO if verbose == 1 else logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(name)s: %(message)s",
        handlers=[RichHandler(console=err, show_time=False, show_path=False)],
        force=True,
    )


@app.command("index")
def index_cmd(
    corpus: Annotated[Path, typer.Argument(help="Corpus directory or .jsonl file.")],
    out_path: Annotated[Path, typer.Option("--out", "-o", help="index.bin (pickle) or *.json")],
    positions: Annotated[
        bool, typer.Option(help="Store token positions (phrase queries).")
    ] = False,
    limit: Annotated[int | None, typer.Option(help="Only the first N files.")] = None,
    workers: Annotated[int, typer.Option("--workers", "-w", min=1, help="Parallel workers.")] = (
        os.cpu_count() or 1
    ),
    executor: Annotated[
        ExecutorChoice, typer.Option(help="serial loop, thread pool or process pool.")
    ] = ExecutorChoice.processes,
) -> None:
    """Build the inverted index (map: build_partial per chunk, reduce: merge) and save it."""
    if not corpus.exists():
        fail(f"corpus not found: {corpus}")
    paths = list_sources(corpus)[:limit]

    t0 = time.perf_counter()
    with Progress(
        TextColumn("indexing"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=err,
        transient=True,
    ) as progress:
        task = progress.add_task("files", total=len(paths))
        index, stats = build_parallel(
            paths,
            workers=workers,
            executor=executor.value,
            positions=positions,
            on_chunk_done=lambda n: progress.advance(task, n),
        )
    t_build = time.perf_counter() - t0
    t1 = time.perf_counter()
    save(index, out_path)
    t_save = time.perf_counter() - t1
    n_postings = sum(len(p) for p in index.postings.values())
    log.info(
        "build %.2f s (%s x%d, %d chunks, merge %.2f s), save %.2f s",
        t_build,
        executor.value,
        workers,
        stats.chunks,
        stats.merge_seconds,
        t_save,
    )
    out.print(
        f"{index!r}: {n_postings:,} postings -> {out_path} "
        f"({out_path.stat().st_size / 2**20:.1f} MiB) in {t_build + t_save:.1f} s"
    )


@app.command("search")
def search_cmd(
    index_path: Annotated[Path, typer.Argument(metavar="INDEX", help="Index file.")],
    query: Annotated[str, typer.Argument(help='e.g. python AND (async OR await) "event loop"')],
    k: Annotated[int, typer.Option("--k", "-k", min=1, help="How many results.")] = 10,
    scorer: Annotated[ScorerName, typer.Option(help="Ranking function.")] = ScorerName.bm25,
    as_json: Annotated[bool, typer.Option("--json", help="JSON lines on stdout.")] = False,
) -> None:
    """Ranked search with highlighted snippets."""
    if not index_path.is_file():
        fail(f"index file not found: {index_path} (build it with `findex index`)")
    try:
        with open_index(index_path) as index:
            results = search(index, query, scorer.value, k)
            total = len(match(index, query))
    except QueryError as e:
        fail(f"bad query: {e}", code=2)
    except (pickle.UnpicklingError, json.JSONDecodeError, TypeError, EOFError) as e:
        fail(f"cannot read index {index_path}: {e}")

    if as_json:
        for r in results:
            print(json.dumps(r.to_dict(), ensure_ascii=False))  # plain stdout: pipe-friendly
        return
    table = Table(title=f"{total:,} documents match {query!r}", show_lines=True)
    table.add_column("#", justify="right")
    table.add_column("score", justify="right")
    table.add_column("document")
    for rank, r in enumerate(results, 1):
        snip = escape(r.snippet).replace("**", "\x00")
        parts = snip.split("\x00")  # every odd part was between ** **
        snip = "".join(f"[bold yellow]{p}[/]" if i % 2 else p for i, p in enumerate(parts))
        table.add_row(
            str(rank),
            f"{r.score:.2f}",
            f"[bold]{escape(r.title)}[/]\n[dim]{escape(r.path)}[/]\n{snip}",
        )
    out.print(table)


@app.command("stats")
def stats_cmd(
    corpus: Annotated[Path, typer.Argument(help="Corpus directory or .jsonl file.")],
    limit: Annotated[int | None, typer.Option(help="Only the first N documents.")] = None,
    top: Annotated[int, typer.Option(help="How many top terms.")] = 50,
    eager: Annotated[bool, typer.Option(help="List-based version (Lab 1 comparison).")] = False,
) -> None:
    """Corpus statistics in one pass: documents, tokens, vocabulary, top terms."""
    if not corpus.exists():
        fail(f"corpus not found: {corpus}")
    compute = compute_stats_eager if eager else compute_stats
    stats, elapsed, peak = measure(lambda: compute(islice(iter_documents(corpus), limit)))
    table = Table(
        title=f"{'eager' if eager else 'lazy'}: {elapsed:.2f} s, peak {peak / 2**20:.1f} MiB"
    )
    table.add_column("metric")
    table.add_column("value", justify="right")
    for name, value in (
        ("documents", stats.documents),
        ("tokens", stats.tokens),
        ("vocabulary", stats.vocabulary),
    ):
        table.add_row(name, f"{value:,}")
    out.print(table)
    terms = Table(title=f"top-{top} terms")
    terms.add_column("#", justify="right")
    terms.add_column("term")
    terms.add_column("count", justify="right")
    for rank, (term, n) in enumerate(stats.counts.most_common(top), 1):
        terms.add_row(str(rank), term, f"{n:,}")
    out.print(terms)
