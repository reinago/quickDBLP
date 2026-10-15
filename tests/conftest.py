import json

import pytest

from quickdblp.db import Database, build_snapshot

AUTHORS = [
    (1, "https://dblp.org/pid/1", "Alice Anders", "https://orcid.org/0000-0000-0000-0001"),
    (2, "https://dblp.org/pid/2", "Bob Brandt", ""),
    (3, "https://dblp.org/pid/3", "Carol Cruz", ""),
    (4, "https://dblp.org/pid/4", "José Müller", ""),
    (5, "https://dblp.org/pid/5", "Alice Anders", ""),  # homonym
    (6, "https://dblp.org/pid/6", "Dave O'Brien", ""),
]
# id, year, type
PAPERS = [(1, 2024, 1), (2, 2020, 3), (3, 2015, 1), (4, 2024, 6), (5, 2023, 1), (6, 2023, 1)]
# paper, author
LINKS = [(1, 1), (1, 2), (2, 1), (2, 3), (3, 1), (3, 4), (4, 5), (4, 6), (5, 2), (5, 3), (6, 1), (6, 5)]


def write_snapshot(directory, extra_author=False, extra_authors=(), extra_links=()):
    """Write the fixture snapshot; `extra_authors` / `extra_links` add rows (same layout as AUTHORS / LINKS)."""
    authors = AUTHORS + ([(7, "https://dblp.org/pid/7", "Erin Evans", "")] if extra_author else []) + list(extra_authors)
    links = LINKS + list(extra_links)
    (directory / "dblp_authors.csv").write_text(
        "NumericID\tDBLP\tName\tORCID\n" + "".join(f"{i}\t{d}\t{n}\t{o}\n" for i, d, n, o in authors), encoding="utf-8"
    )
    (directory / "dblp_papers.csv").write_text(
        "NumericID\tDBLP\tTitle\tType\tYear\n"
        + "".join(f"{i}\thttps://dblp.org/rec/p{i}\tPaper {i}\t{t}\t{y}\n" for i, y, t in PAPERS),
        encoding="utf-8",
    )
    (directory / "dblp_papers_authors.csv").write_text(
        "PaperID\tAuthorID\n" + "".join(f"{p}\t{a}\n" for p, a in links), encoding="utf-8"
    )
    (directory / "dblp_metadata.json").write_text(
        json.dumps({
            "source_file": "dblp.rdf.gz",
            "source_file_last_write_time": "2026-06-18 16:22:49.8340833",
            "total_papers": len(PAPERS),
            "total_authors": len(authors),
            "total_links": len(links),
        }),
        encoding="utf-8",
    )


@pytest.fixture
def snapshot_dir(tmp_path):
    d = tmp_path / "snapshot"
    d.mkdir()
    write_snapshot(d)
    return d


@pytest.fixture
def db(snapshot_dir, tmp_path):
    path = tmp_path / "dblp.duckdb"
    build_snapshot(snapshot_dir, path)
    database = Database(path)
    yield database
    database.close()
