"""Lab 5 benchmark: executors x workers, >= 3 runs each (median), table + speedup plot.

uv run python scripts/lab05_bench.py data/ [--ft .venv-ft/bin/python] [--runs 3]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

RUN = Path(__file__).with_name("lab05_run.py")


def run(python: str, root: Path, executor: str, workers: int, runs: int) -> dict[str, float]:
    rows = []
    for _ in range(runs):
        out = subprocess.run(
            [python, str(RUN), str(root), executor, str(workers)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        rows.append(json.loads(out.strip().splitlines()[-1]))
    med = {k: statistics.median(r[k] for r in rows) for k in ("wall", "cpu", "rss", "merge")}
    med["spread"] = max(r["wall"] for r in rows) - min(r["wall"] for r in rows)
    med["gil"] = rows[0]["gil"]
    return med


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--ft", help="python of a free-threaded (3.13t) venv")
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    counts = sorted({1, 2, 4, 8, os.cpu_count() or 1})
    configs: list[tuple[str, str, str, int]] = [("GIL", sys.executable, "serial", 1)]
    configs += [("GIL", sys.executable, "threads", n) for n in counts]
    configs += [("GIL", sys.executable, "processes", n) for n in counts]
    if args.ft:
        configs += [("3.13t", args.ft, "serial", 1)]
        configs += [("3.13t", args.ft, "threads", n) for n in counts]

    results = []
    for label, py, ex, n in configs:
        r = run(py, args.root, ex, n, args.runs)
        results.append((label, ex, n, r))
        print(f"{label} {ex} x{n}: {r}", file=sys.stderr)

    base = {lbl: r["wall"] for lbl, ex, _, r in results if ex == "serial"}
    print(
        "| Python | Executor | Workers | Wall (s, median) | ± | CPU (s) | Peak RSS parent "
        "(MiB) | Merge (s) | Speedup |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for label, ex, n, r in results:
        py = f"{label} (GIL {'on' if r['gil'] else 'off'})"
        print(
            f"| {py} | {ex} | {n} | {r['wall']:.2f} | {r['spread']:.2f} | {r['cpu']:.2f} "
            f"| {r['rss']:.0f} | {r['merge']:.2f} | {base['GIL'] / r['wall']:.2f}× |"
        )

    fig, ax = plt.subplots(figsize=(7, 4.5))
    for label, ex, style in (
        ("GIL", "threads", "o-"),
        ("GIL", "processes", "s-"),
        ("3.13t", "threads", "^--"),
    ):
        pts = [(n, base["GIL"] / r["wall"]) for lb, e, n, r in results if lb == label and e == ex]
        if pts:
            ax.plot(*zip(*pts), style, label=f"{ex} ({label})")
    ax.plot(counts, counts, ":", color="grey", label="ideal (linear)")
    ax.axvline(os.cpu_count() or 1, color="grey", lw=0.8, ls="--")
    ax.text((os.cpu_count() or 1) + 0.1, 0.3, f"cpu_count={os.cpu_count()}", color="grey")
    ax.set_xlabel("workers")
    ax.set_ylabel("speedup vs serial (GIL build)")
    ax.set_xticks(counts)
    ax.set_ylim(0, max(counts) + 0.5)
    ax.set_title("findex index: speedup vs workers")
    ax.legend()
    fig.tight_layout()
    fig.savefig("docs/lab05_speedup.png", dpi=120)


if __name__ == "__main__":
    main()
