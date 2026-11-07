import json
from pathlib import Path

from findex.corpus import count_documents, iter_documents


def test_iter_documents_is_lazy_and_reads_jsonl(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text(".. directive::\nTitle A\n=====\nbody", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text(
        json.dumps({"url": "u1", "title": "T1", "text": "x"}) + "\nnot json\n", encoding="utf-8"
    )
    (tmp_path / "bad.txt").write_bytes(b"ok \xff\xfe bytes")
    docs = iter_documents(tmp_path)
    first = next(docs)
    assert (first.doc_id, first.title) == (0, "Title A")
    rest = list(docs)
    assert [d.path for d in rest] == ["u1", str(tmp_path / "bad.txt")]
    assert [d.doc_id for d in rest] == [1, 3]  # b.jsonl reserves 2 ids (2 lines), 1 skipped
    assert "�" in rest[1].text  # undecodable bytes replaced, not a crash
    assert count_documents(tmp_path) == 4  # upper bound: the broken jsonl line counts too
