"""ONE measured build in a fresh process (so peak RSS belongs to this run only).

python scripts/lab05_run.py <corpus> <executor> <workers>   -> prints one JSON line
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time
from pathlib import Path

from findex.corpus import list_sources
from findex.parallel import build_parallel


def main() -> None:
    root, executor, workers = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    paths = list_sources(root)
    c0 = os.times()
    t0 = time.perf_counter()
    _, stats = build_parallel(paths, workers=workers, executor=executor)  # type: ignore[arg-type]
    wall = time.perf_counter() - t0
    c1 = os.times()
    # CPU of this process + of finished (joined) worker processes
    cpu = sum(c1[:4]) - sum(c0[:4])
    rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # Linux: KiB
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    print(
        json.dumps(
            {
                "wall": wall,
                "cpu": cpu,
                "rss": rss_mib,
                "merge": stats.merge_seconds,
                "gil": gil,
                "python": sys.version.split()[0],
            }
        )
    )


if __name__ == "__main__":
    main()
