"""Race demo: threads updating ONE shared Counter (the naive way to parallelize stats).

python scripts/lab05_race.py data/     # run under the GIL build and under 3.13t
"""

from __future__ import annotations

import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from findex.corpus import iter_documents
from findex.tokenize import tokenize

THREADS = 4


def run(texts: list[str], worker: Callable[[list[str]], None]) -> float:
    parts = [texts[i::THREADS] for i in range(THREADS)]
    threads = [threading.Thread(target=worker, args=(p,)) for p in parts]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return time.perf_counter() - t0


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "data")
    tokens = [list(tokenize(d.text)) for d in iter_documents(root)]  # pre-tokenized: only the race
    texts = [" ".join(t) for t in tokens]
    expected = sum(len(t) for t in tokens)
    print(f"Python {sys.version.split()[0]}, GIL enabled: {sys._is_gil_enabled()}")

    for attempt in range(3):
        shared: Counter[str] = Counter()

        def naive(part: list[str], shared: Counter[str] = shared) -> None:
            for text in part:
                for tok in text.split():
                    shared[tok] += 1  # read, add, write: 3 steps, not atomic

        t = run(texts, naive)
        got = shared.total()
        print(
            f"naive shared Counter #{attempt + 1}: {got:,} / {expected:,} "
            f"(lost {expected - got:,}), {t:.2f} s"
        )

    lock = threading.Lock()
    locked: Counter[str] = Counter()

    def with_lock(part: list[str]) -> None:
        for text in part:
            for tok in text.split():
                with lock:
                    locked[tok] += 1

    t = run(texts, with_lock)
    print(
        f"fix 1, Lock around +=:       {locked.total():,} (lost {expected - locked.total()}), {t:.2f} s"
    )

    partials: list[Counter[str]] = []

    def private(part: list[str]) -> None:
        c: Counter[str] = Counter()
        for text in part:
            c.update(text.split())
        partials.append(c)  # one append per thread

    t = run(texts, private)
    merged = sum(partials, Counter[str]())
    print(
        f"fix 2, per-thread Counter:   {merged.total():,} (lost {expected - merged.total()}), {t:.2f} s"
    )


if __name__ == "__main__":
    main()
