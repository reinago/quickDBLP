import json
import sys

import pytest

from conftest import write_snapshot
from quickdblp.db import Database, SnapshotError, build_snapshot, is_up_to_date


def test_build_then_up_to_date(snapshot_dir, tmp_path):
    db_path = tmp_path / "dblp.duckdb"
    assert build_snapshot(snapshot_dir, db_path) is True
    assert is_up_to_date(snapshot_dir, db_path)
    assert build_snapshot(snapshot_dir, db_path) is False


def test_force_rebuilds(snapshot_dir, tmp_path):
    db_path = tmp_path / "dblp.duckdb"
    build_snapshot(snapshot_dir, db_path)
    assert build_snapshot(snapshot_dir, db_path, force=True) is True


def test_changed_snapshot_triggers_rebuild(snapshot_dir, tmp_path):
    db_path = tmp_path / "dblp.duckdb"
    build_snapshot(snapshot_dir, db_path)
    write_snapshot(snapshot_dir, extra_author=True)
    assert not is_up_to_date(snapshot_dir, db_path)
    assert build_snapshot(snapshot_dir, db_path) is True
    db = Database(db_path)
    try:
        assert db.search_authors("Erin")["authors"][0]["name"] == "Erin Evans"
    finally:
        db.close()


def test_count_mismatch_keeps_old_db(snapshot_dir, tmp_path):
    db_path = tmp_path / "dblp.duckdb"
    build_snapshot(snapshot_dir, db_path)
    before = db_path.read_bytes()
    meta_path = snapshot_dir / "dblp_metadata.json"
    meta = json.loads(meta_path.read_text())
    meta["total_links"] += 1
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(SnapshotError, match="papers_authors"):
        build_snapshot(snapshot_dir, db_path)
    assert db_path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp*"))


def test_missing_files(tmp_path):
    with pytest.raises(SnapshotError):
        build_snapshot(tmp_path, tmp_path / "x.duckdb")


def test_meta(db):
    meta = db.meta()
    assert meta["total_papers"] == 6 and meta["total_authors"] == 6
    assert meta["age_days"] >= 0


def _write_broken_papers(snapshot_dir):
    # title with line breaks (as written by older ponder_dblp for arXiv titles) and one with a stray tab
    (snapshot_dir / "dblp_papers.csv").write_text(
        "NumericID\tDBLP\tTitle\tType\tYear\n"
        "1\thttps://dblp.org/rec/p1\tPaper 1\t1\t2024\n"
        "2\thttps://dblp.org/rec/p2\tOver \U0001d53dq\n\nn.\t3\t2020\n"
        "3\thttps://dblp.org/rec/p3\tTab\tin title\t1\t2015\n"
        "4\thttps://dblp.org/rec/p4\tPaper 4\t6\t2024\n"
        "5\thttps://dblp.org/rec/p5\tPaper 5\t1\t2023\n"
        "6\thttps://dblp.org/rec/p6\tPaper 6\t1\t2023\n",
        encoding="utf-8",
    )


def test_malformed_records_are_repaired(snapshot_dir, tmp_path, caplog):
    _write_broken_papers(snapshot_dir)
    db_path = tmp_path / "dblp.duckdb"
    build_snapshot(snapshot_dir, db_path)
    assert not list(tmp_path.glob("*.tmp*"))
    db = Database(db_path)
    try:
        titles = {p["id"]: p["title"] for e in db.coauthors([1]) for p in e["papers"]}
    finally:
        db.close()
    assert titles[2] == "Over \U0001d53dq n."
    assert titles[3] == "Tab in title"
    assert titles[1] == "Paper 1"
    assert "repaired copy" in caplog.text


def test_repair_tsv_counts_and_leaves_clean_files_alone(tmp_path):
    from quickdblp.db import repair_tsv

    clean = tmp_path / "clean.tsv"
    clean.write_text("a\tb\tc\n1\t2\t3\n", encoding="utf-8")
    assert repair_tsv(clean, tmp_path / "out.tsv", 3) == 0
    assert (tmp_path / "out.tsv").read_text(encoding="utf-8") == "a\tb\tc\n1\t2\t3\n"
    truncated = tmp_path / "trunc.tsv"
    truncated.write_text("a\tb\tc\n1\t2\n", encoding="utf-8")
    with pytest.raises(SnapshotError, match="middle of a record"):
        repair_tsv(truncated, tmp_path / "out2.tsv", 3)


@pytest.mark.skipif(sys.platform != "win32", reason="other platforms can replace an open file")
def test_rebuild_while_db_is_open_fails_cleanly(snapshot_dir, tmp_path):
    db_path = tmp_path / "dblp.duckdb"
    build_snapshot(snapshot_dir, db_path)
    db = Database(db_path)
    try:
        with pytest.raises(SnapshotError, match="in use"):
            build_snapshot(snapshot_dir, db_path, force=True)
        assert not list(tmp_path.glob("*.tmp*"))
        assert db.meta()["total_authors"] == 6  # the old database is untouched
    finally:
        db.close()


def test_duplicate_links_are_removed_and_counted(snapshot_dir, tmp_path):
    # DBLP sometimes lists a person twice on a paper: the metadata count includes the duplicate
    links = (snapshot_dir / "dblp_papers_authors.csv")
    links.write_text(links.read_text() + "1\t2\n", encoding="utf-8")
    meta_path = snapshot_dir / "dblp_metadata.json"
    meta = json.loads(meta_path.read_text())
    meta["total_links"] += 1
    meta_path.write_text(json.dumps(meta))
    db_path = tmp_path / "dblp.duckdb"
    build_snapshot(snapshot_dir, db_path)
    db = Database(db_path)
    try:
        assert db.meta()["integrity"]["duplicate_links_removed"] == 1
        bob = next(e for e in db.coauthors([1]) if e["name"] == "Bob Brandt")
        assert bob["paper_count"] == 1 and len(bob["papers"]) == 1  # not counted twice
    finally:
        db.close()


def test_clean_snapshot_has_no_integrity_problems(db):
    assert db.meta()["integrity"] == {
        "duplicate_links_removed": 0, "links_to_missing_papers": 0,
        "links_to_missing_authors": 0, "papers_without_authors": 0,
    }


def test_lost_records_are_reported(snapshot_dir, tmp_path, caplog):
    # what the old exporter did: the last paper/author row is missing although links refer to it
    for name, drop in (("dblp_papers.csv", "6\t"), ("dblp_authors.csv", "6\t")):
        f = snapshot_dir / name
        f.write_text("".join(l for l in f.read_text(encoding="utf-8").splitlines(True) if not l.startswith(drop)), encoding="utf-8")
    meta_path = snapshot_dir / "dblp_metadata.json"
    meta = json.loads(meta_path.read_text())
    meta["total_papers"] -= 1
    meta["total_authors"] -= 1
    meta_path.write_text(json.dumps(meta))
    db_path = tmp_path / "dblp.duckdb"
    build_snapshot(snapshot_dir, db_path)
    db = Database(db_path)
    try:
        integrity = db.meta()["integrity"]
    finally:
        db.close()
    assert integrity["links_to_missing_papers"] == 2 and integrity["links_to_missing_authors"] == 1
    assert "inconsistent" in caplog.text
