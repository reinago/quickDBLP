"""Ground truth for the C++ parser: run it on real-format dumps and audit its output against the RDF with the
independent reader of `quickdblp.audit`.

Inputs: `mini.rdf.gz` (a real extract) and a synthetic dump with every record type and awkward case.
Skipped when there is no parser binary, or when the binary is older than `ponder_dblp.cpp` (rebuild it first).
Set QUICKDBLP_PONDER_EXE to test a specific binary.
"""

import gzip
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from quickdblp.audit import compare_truth, extract_truth, format_report
from quickdblp.db import PAPER_TYPES, build_snapshot
from rdfgen import RDF

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "ponder_dblp" / "ponder_dblp.cpp"
MINI = ROOT / "mini.rdf.gz"


def find_parser():
    candidates = []
    if os.environ.get("QUICKDBLP_PONDER_EXE"):
        candidates.append(Path(os.environ["QUICKDBLP_PONDER_EXE"]))
    for sub in ("build", "install"):
        for preset in ("x64-release", "x64-relwithdeb"):
            for exe in ("ponder_dblp.exe", "ponder_dblp"):
                candidates.append(ROOT / "ponder_dblp" / sub / preset / ("bin" if sub == "install" else "") / exe)
    fresh = [c for c in candidates if c.is_file() and c.stat().st_mtime >= SOURCE.stat().st_mtime]
    return fresh[0] if fresh else None


def run_parser(tmp_path_factory, rdf_gz: Path, as_argument: bool = False) -> Path:
    exe = find_parser()
    if exe is None or not rdf_gz.exists():
        pytest.skip("no ponder_dblp binary newer than ponder_dblp.cpp (build it: see Readme)")
    work = tmp_path_factory.mktemp("parser")
    if not as_argument:
        shutil.copy(rdf_gz, work / "dblp.rdf.gz")
    env = dict(os.environ)
    # the vcpkg DLLs sit next to the binary in `install/`, and in vcpkg_installed/ in the build tree
    dll_dirs = [exe.parent, *exe.parent.glob("vcpkg_installed/*/bin")]
    env["PATH"] = os.pathsep.join(str(d) for d in dll_dirs) + os.pathsep + env.get("PATH", "")
    command = [str(exe), str(rdf_gz)] if as_argument else [str(exe)]
    result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    return work


@pytest.fixture(scope="module")
def mini_output(tmp_path_factory):
    return run_parser(tmp_path_factory, MINI), MINI


@pytest.fixture(scope="module")
def synthetic_output(tmp_path_factory):
    rdf = tmp_path_factory.mktemp("synthetic") / "synthetic.rdf.gz"
    with gzip.open(rdf, "wb") as f:
        f.write(RDF.encode())
    return run_parser(tmp_path_factory, rdf), rdf


@pytest.fixture(params=["mini_output", "synthetic_output"])
def parsed(request):
    return request.getfixturevalue(request.param)


def test_parser_output_matches_the_rdf(parsed, tmp_path):
    output, rdf = parsed
    build_snapshot(output, tmp_path / "db.duckdb", force=True)
    stats = extract_truth(rdf, tmp_path / "truth.tsv")
    report = compare_truth(tmp_path / "truth.tsv", tmp_path / "db.duckdb", tmp_path / "work")
    assert report["ok"], "\n" + format_report(report, stats)


def test_parser_metadata_counts_are_the_real_row_counts(parsed):
    output, _ = parsed
    meta = json.loads((output / "dblp_metadata.json").read_text())
    rows = lambda name: sum(1 for _ in open(output / name, encoding="utf-8")) - 1  # noqa: E731
    assert meta["total_papers"] == rows("dblp_papers.csv")
    assert meta["total_authors"] == rows("dblp_authors.csv")
    assert meta["total_links"] == rows("dblp_papers_authors.csv")
    links = (output / "dblp_papers_authors.csv").read_text().splitlines()[1:]
    assert len(links) == len(set(links)), "duplicate links"


def test_parser_handles_every_record_type_and_editors(synthetic_output, tmp_path):
    output, _ = synthetic_output
    build_snapshot(output, tmp_path / "db.duckdb", force=True)
    import duckdb

    con = duckdb.connect(str(tmp_path / "db.duckdb"), read_only=True)
    types = dict(con.execute("select replace(DBLP, 'https://dblp.org/rec/', ''), Type from papers").fetchall())
    assert types["reference/r/10"] == 8 and PAPER_TYPES[8] == "Reference"
    assert types["conf/e/6"] == 9 and PAPER_TYPES[9] == "Editorship"
    assert types["journals/w/11"] == 10 and PAPER_TYPES[10] == "Withdrawn"
    assert "conf/m/12" not in types and "conf/p/9" not in types  # unknown record type, no people
    # the editor of the Editorship record is linked like an author; the doubled author only once
    links = con.execute("""select p.DBLP, count(*) from papers_authors l join papers p on p.NumericID = l.PaperID
                           where p.DBLP like '%conf/e/6' or p.DBLP like '%conf/a/4' group by 1 order by 1""").fetchall()
    assert [n for _, n in links] == [1, 1]


def test_parser_accepts_the_dump_as_argument(tmp_path_factory):
    # the path ends up in the metadata: backslashes of Windows paths must not break the JSON
    work = run_parser(tmp_path_factory, MINI, as_argument=True)
    meta = json.loads((work / "dblp_metadata.json").read_text())
    assert Path(meta["source_file"]) == MINI
    assert meta["total_papers"] == 6 and meta["records_failed"] == 0
    assert not (MINI.parent / "mini.rdf.gz.lock").exists()
