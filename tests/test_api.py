from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from conftest import write_snapshot
from quickdblp.app import create_app
from quickdblp.db import QueryTimeout


@pytest.fixture
def client(snapshot_dir, tmp_path):
    # snapshot_dir given -> the app builds the database on startup
    app = create_app(tmp_path / "dblp.duckdb", snapshot_dir=snapshot_dir)
    with TestClient(app) as c:
        yield c


def ids(authors):
    return {a["id"] for a in authors}


def test_meta(client):
    meta = client.get("/api/meta").json()
    assert meta["total_authors"] == 6
    assert meta["paper_types"]["8"] == "Reference" and meta["paper_types"]["9"] == "Editorship"
    assert meta["paper_types"]["10"] == "Withdrawn"
    assert meta["default_excluded_types"] == [8, 9, 10]  # the page starts with these unticked
    assert set(meta["paper_type_help"]) == {"8", "9", "10"}


def test_search_multiple_queries(client):
    r = client.post("/api/authors/search", json={"queries": ["Bob", " ", "Brandt, Bob", "Nobody"]})
    assert r.status_code == 200
    results = r.json()["results"]
    assert [x["query"] for x in results] == ["Bob", "Brandt, Bob", "Nobody"]  # blank dropped
    assert ids(results[0]["authors"]) == {2} and ids(results[1]["authors"]) == {2}
    assert results[2]["authors"] == []


def test_search_validation(client):
    assert client.post("/api/authors/search", json={"queries": []}).status_code == 422
    assert client.post("/api/authors/search", json={"queries": ["a"] * 51}).status_code == 422
    assert client.post("/api/authors/search", json={"queries": ["a"], "matcher": "nope"}).status_code == 422
    assert client.post("/api/authors/search", json={"queries": ["a" * 301]}).status_code == 422


def test_recent(client):
    papers = client.get("/api/authors/1/recent?limit=2").json()["papers"]
    assert [p["id"] for p in papers] == [1, 6]


def test_conflicts_years_back(client):
    # fixture papers are from 2015-2024; years_back relative to today so use None and a huge window
    r = client.post("/api/conflicts", json={"author_ids": [1], "years_back": None, "exclude_self": True}).json()
    assert r["since_year"] is None and ids(r["coauthors"]) == {2, 3, 4, 5}
    years = datetime.now().year - 2020  # keeps papers from 2020 on, whatever the current year is
    r = client.post("/api/conflicts", json={"author_ids": [1], "years_back": years}).json()
    assert r["since_year"] == 2020 and 4 not in ids(r["coauthors"])  # 4 only shares the 2015 paper
    assert r["truncated"] is False


def test_conflicts_filters_and_validation(client):
    r = client.post("/api/conflicts", json={"author_ids": [5], "years_back": None, "exclude_types": [6]}).json()
    assert ids(r["coauthors"]) == {1, 5}
    assert client.post("/api/conflicts", json={"author_ids": []}).status_code == 422
    assert client.post("/api/conflicts", json={"author_ids": [1], "years_back": -1}).status_code == 422


def test_reviewers(client):
    body = {
        "author_ids": [1], "years_back": None,
        "reviewers": ["Bob Brandt", "Dave O'Brien", "Alice Anders", "Nobody Known", "https://dblp.org/pid/3"],
    }
    r = client.post("/api/reviewers/check", json=body).json()["reviewers"]
    status = {x["query"]: x["status"] for x in r}
    assert status == {
        "Bob Brandt": "conflict",
        "Dave O'Brien": "no_conflict",
        "Alice Anders": "ambiguous",  # two DBLP profiles with this name
        "Nobody Known": "not_found",
        "https://dblp.org/pid/3": "conflict",
    }
    alice = next(x for x in r if x["query"] == "Alice Anders")
    assert {c["id"]: c["conflict"] is not None for c in alice["candidates"]} == {1: True, 5: True}
    bob = next(x for x in r if x["query"] == "Bob Brandt")
    assert [p["id"] for p in bob["candidates"][0]["conflict"]["papers"]] == [1]


def test_guess(client):
    html = '<div class="authorList"><ul><li><span>Alice Anders</span></li></ul></div>'
    assert client.post("/api/guess", json={"source": "pcs", "html": html}).json() == {"authors": ["Alice Anders"]}
    r = client.post("/api/guess", json={"source": "pcs", "html": "<p>nope</p>"})
    assert r.status_code == 422 and "authorList" in r.json()["detail"]
    assert client.post("/api/guess", json={"source": "other", "html": html}).status_code == 422
    assert client.post("/api/guess", json={"source": "pcs", "html": "x" * 2_000_001}).status_code == 422


def test_startup_rebuilds_outdated_db(snapshot_dir, tmp_path):
    db_path = tmp_path / "dblp.duckdb"
    with TestClient(create_app(db_path, snapshot_dir=snapshot_dir)) as c:
        assert c.get("/api/meta").json()["total_authors"] == 6
    write_snapshot(snapshot_dir, extra_author=True)
    with TestClient(create_app(db_path, snapshot_dir=snapshot_dir)) as c:
        assert c.get("/api/meta").json()["total_authors"] == 7


def test_timeout_maps_to_504(snapshot_dir, tmp_path):
    app = create_app(tmp_path / "dblp.duckdb", snapshot_dir=snapshot_dir)
    with TestClient(app) as c:
        def slow(*a, **k):
            raise QueryTimeout("slow")
        app.state.db.search_authors = slow
        assert c.post("/api/authors/search", json={"queries": ["a"]}).status_code == 504


def test_query_interrupted_after_timeout(db):
    with pytest.raises(QueryTimeout):
        db._query("select count(*) from range(100000000000)", timeout=0.2)


def test_parallel_requests_with_small_slot_limit(snapshot_dir, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    app = create_app(tmp_path / "dblp.duckdb", snapshot_dir=snapshot_dir, max_concurrent=2)
    with TestClient(app) as c:
        def call(_):
            return c.post("/api/conflicts", json={"author_ids": [1], "years_back": None}).json()
        with ThreadPoolExecutor(10) as ex:
            results = list(ex.map(call, range(20)))
    assert all(len(r["coauthors"]) == 5 for r in results)


def test_static_files_are_revalidated(client):
    for path in ("/", "/app.js", "/style.css"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_resource_limits_are_applied(snapshot_dir, tmp_path):
    app = create_app(tmp_path / "dblp.duckdb", snapshot_dir=snapshot_dir, threads=2, memory_limit="1GB")
    with TestClient(app) as c:
        assert c.get("/api/meta").status_code == 200
        con = app.state.db._con
        assert con.execute("select current_setting('threads')").fetchone()[0] == 2
        assert "953" in str(con.execute("select current_setting('memory_limit')").fetchone()[0])  # 1GB = 953.6 MiB


def test_invalid_memory_limit_is_rejected(snapshot_dir, tmp_path):
    from quickdblp.db import Database, build_snapshot

    build_snapshot(snapshot_dir, tmp_path / "dblp.duckdb")
    with pytest.raises(ValueError, match="memory limit"):
        Database(tmp_path / "dblp.duckdb", memory_limit="1GB'; drop table authors; --")
