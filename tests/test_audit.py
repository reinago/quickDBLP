"""Tests of the ground-truth audit itself, on a small hand-written RDF with all the awkward cases."""

import json

import pytest

from quickdblp.audit import compare_truth, extract_truth, format_report
from quickdblp.db import build_snapshot

from rdfgen import PUB, RDF  # noqa: E402

# what a correct parser keeps (key, author pids...) and the exact CSVs for it
KEPT = {
    "conf/a/1": (1, 2024, ["a/A", "b/B"]), "journals/x/2": (3, 2023, ["a/A"]), "books/3": (4, 2015, ["b/B"]),
    "conf/a/4": (1, 2022, ["a/A"]), "multiline": (7, 2025, ["c/C"]), "conf/e/6": (9, 2020, ["a/A"]),
    "reference/r/10": (8, 2014, ["c/C", "a/A"]), "journals/w/11": (10, 2018, ["b/B"]),
}


def write_snapshot(directory, kept=KEPT, drop_link=None, wrong_year=None, extra_paper=False):
    authors = sorted({a for _, _, auth in kept.values() for a in auth})
    ids = {a: i for i, a in enumerate(authors, 1)}
    papers = list(kept.items()) + ([("conf/zzz/99", (1, 2000, ["a/A"]))] if extra_paper else [])
    rows, links = [], []
    for pid, (key, (type_id, year, auth)) in enumerate(papers, 1):
        year = year + 1 if key == wrong_year else year
        rows.append(f"{pid}\thttps://dblp.org/rec/{key}\tA title\t{type_id}\t{year}\n")
        links += [(pid, ids[a]) for a in auth if (key, a) != drop_link]
    (directory / "dblp_authors.csv").write_text(
        "NumericID\tDBLP\tName\tORCID\n" + "".join(f"{i}\thttps://dblp.org/pid/{a}\tName {a}\t\n" for a, i in ids.items()), encoding="utf-8")
    (directory / "dblp_papers.csv").write_text("NumericID\tDBLP\tTitle\tType\tYear\n" + "".join(rows), encoding="utf-8")
    (directory / "dblp_papers_authors.csv").write_text("PaperID\tAuthorID\n" + "".join(f"{p}\t{a}\n" for p, a in links), encoding="utf-8")
    (directory / "dblp_metadata.json").write_text(json.dumps({
        "source_file_last_write_time": "2026-01-01 00:00:00.0000000",
        "total_papers": len(rows), "total_authors": len(ids), "total_links": len(links)}), encoding="utf-8")


@pytest.fixture
def truth(tmp_path):
    rdf = tmp_path / "dump.rdf"
    rdf.write_text(RDF, encoding="utf-8")
    out = tmp_path / "truth.tsv"
    return out, extract_truth(rdf, out)


def audit(tmp_path, truth_path, **snapshot_options):
    snap = tmp_path / "snap"
    snap.mkdir(exist_ok=True)
    write_snapshot(snap, **snapshot_options)
    build_snapshot(snap, tmp_path / "db.duckdb", force=True)
    return compare_truth(truth_path, tmp_path / "db.duckdb", tmp_path / "work")


def failed(report):
    return {c["name"] for c in report["checks"] if not c["ok"]}


def test_extraction_reads_every_record_kind(truth):
    path, stats = truth
    assert stats["records"] == 14 and stats["publications"] == 13  # the Person is not a publication
    assert stats["unterminated_record_at_eof"] == 0
    assert stats["entities"]["Person"] == 1 and stats["publications_by_entity"]["Editorship"] == 1
    rows = {r.split("\t")[0].rsplit("rec/", 1)[1]: r.rstrip("\n").split("\t") for r in path.read_text().splitlines()[1:]}
    assert rows["conf/a/1"][2:] == ["1", "2024", "", "2", "2", "1", "https://dblp.org/pid/a/A https://dblp.org/pid/b/B"]
    assert rows["journals/x/2"][3:5] == ["2023", "2019"]
    assert rows["books/3"][3:5] == ["", "2015"]
    assert rows["conf/a/4"][5] == "2"                       # duplicate author is kept in the truth file
    assert rows["journals/corr/5"][5:7] == ["2", "1"]       # mismatch visible
    assert rows["journals/x/7"][7] == "0"                   # no title
    assert rows["multiline"][7] == "1"                      # title over several lines is still a title


def test_correct_snapshot_passes_and_every_drop_is_explained(truth, tmp_path):
    report = audit(tmp_path, truth[0])
    assert report["ok"], format_report(report, truth[1])
    assert report["info"]["dump_vs_db"] == {
        "in_db/none": 8, "not_in_db/author_signature_mismatch": 1, "not_in_db/no_authors": 1,
        "not_in_db/no_title": 1, "not_in_db/no_year": 1, "not_in_db/unhandled_entity": 1,
    }
    # publications that have authors but are left out by the parser's rules are surfaced as warnings
    warned = {(w["reason"], w["entity"]): w["papers"] for w in report["warnings"]}
    assert warned == {
        ("author_signature_mismatch", "Informal"): 1, ("unhandled_entity", "Mystery"): 1,
        ("no_title", "Article"): 1, ("no_year", "Article"): 1,
    }
    text = format_report(report, truth[1])
    assert "RESULT: no discrepancies" in text and "WARNINGS" in text and "4 publications with authors are excluded" in text


def test_lost_paper_is_detected(truth, tmp_path):
    kept = {k: v for k, v in KEPT.items() if k != "journals/x/2"}
    report = audit(tmp_path, truth[0], kept=kept)
    assert failed(report) == {"unexplained_missing_papers"}
    check = next(c for c in report["checks"] if c["name"] == "unexplained_missing_papers")
    assert check["count"] == 1 and "journals/x/2" in check["examples"][0][0]


def test_lost_link_is_detected(truth, tmp_path):
    report = audit(tmp_path, truth[0], drop_link=("conf/a/1", "b/B"))
    assert failed(report) == {"missing_links"}


def test_wrong_year_is_detected(truth, tmp_path):
    assert failed(audit(tmp_path, truth[0], wrong_year="books/3")) == {"year_mismatch"}


def test_extra_paper_is_detected(truth, tmp_path):
    report = audit(tmp_path, truth[0], extra_paper=True)
    assert {"papers_not_in_dump", "extra_links"} <= failed(report)


def test_truncated_dump_is_flagged(tmp_path):
    rdf = tmp_path / "cut.rdf"
    rdf.write_text(RDF[: RDF.index("conf/e/6") + 10], encoding="utf-8")
    assert extract_truth(rdf, tmp_path / "t.tsv")["unterminated_record_at_eof"] == 1


def test_gzipped_dump(tmp_path):
    import gzip

    rdf = tmp_path / "dump.rdf.gz"
    with gzip.open(rdf, "wb") as f:
        f.write(RDF.encode())
    assert extract_truth(rdf, tmp_path / "t.tsv")["publications"] == 13
