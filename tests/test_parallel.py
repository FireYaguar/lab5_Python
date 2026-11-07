import json
from pathlib import Path

import pytest

from findex.corpus import iter_documents, list_sources
from findex.index import build_index, build_partial, merge
from findex.parallel import ExecutorName, build_parallel, make_chunks


@pytest.fixture
def mixed_corpus(corpus_dir: Path) -> Path:
    lines = [json.dumps({"url": f"u{i}", "text": f"python page {i}"}) for i in range(3)]
    (corpus_dir / "z.jsonl").write_text("\n".join([*lines, "broken"]) + "\n", encoding="utf-8")
    return corpus_dir


def test_serial_partial_equals_build_index(mixed_corpus: Path) -> None:
    paths = list_sources(mixed_corpus)
    assert merge([build_partial(paths, 0)]) == build_index(iter_documents(mixed_corpus))


def test_chunks_cover_all_paths_with_disjoint_id_ranges(mixed_corpus: Path) -> None:
    paths = list_sources(mixed_corpus)
    chunks = make_chunks(paths, 3)
    assert [p for c in chunks for p in c.paths] == paths
    assert [c.start_id for c in chunks] == sorted({c.start_id for c in chunks})
    assert make_chunks(paths, 100)[-1].start_id == 4  # after 4 text files (ids 0-3)
    assert make_chunks([], 4) == []


@pytest.mark.parametrize("executor", ["serial", "threads", "processes"])
@pytest.mark.parametrize("positions", [False, True])
def test_executors_build_identical_index(
    mixed_corpus: Path, executor: ExecutorName, positions: bool
) -> None:
    paths = list_sources(mixed_corpus)
    expected = build_index(iter_documents(mixed_corpus), positions=positions)
    index, stats = build_parallel(paths, workers=2, executor=executor, positions=positions)
    assert index == expected
    assert stats.chunks == 1 if executor == "serial" else 1 <= stats.chunks <= len(paths)


@pytest.mark.parametrize("executor", ["threads", "processes"])
def test_worker_error_propagates(corpus_dir: Path, executor: ExecutorName) -> None:
    (corpus_dir / "broken.txt").mkdir()  # stat() works in the parent, reading fails in a worker
    paths = [*list_sources(corpus_dir), corpus_dir / "broken.txt"]
    with pytest.raises(IsADirectoryError) as exc:
        build_parallel(paths, workers=2, executor=executor)
    if executor == "processes":  # the worker's own traceback is attached
        assert "broken.txt" in str(exc.value.__cause__)


@pytest.mark.slow
def test_full_corpus_processes_equal_serial() -> None:
    data = Path("data")
    if not data.is_dir():
        pytest.skip("corpus not downloaded")
    paths = list_sources(data)
    serial, _ = build_parallel(paths, executor="serial")
    parallel, _ = build_parallel(paths, workers=4, executor="processes")
    assert parallel == serial == build_index(iter_documents(data))
