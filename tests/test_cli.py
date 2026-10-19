"""`quickdblp serve` without options: the self-updating mode (with the update button), starting from the
snapshot that is already there. None of these tests may start an import: that would download from dblp.org."""

import time

from fastapi.testclient import TestClient

from quickdblp.cli import app_from_args, build_parser
from quickdblp.db import build_snapshot


def serve_args(*options):
    return build_parser().parse_args(["serve", *options])


def make_snapshot_db(snapshot_dir):
    """snapshot/dblp.duckdb next to the CSVs, as the old workflow leaves it."""
    db = snapshot_dir / "dblp.duckdb"
    build_snapshot(snapshot_dir, db)
    return db


def test_serve_defaults_to_the_updating_mode_and_adopts_the_snapshot(tmp_path, monkeypatch, snapshot_dir):
    monkeypatch.chdir(tmp_path)
    db = make_snapshot_db(snapshot_dir)  # tmp_path / "snapshot" / "dblp.duckdb"
    app = app_from_args(serve_args())
    with TestClient(app) as client:
        time.sleep(0.5)  # the scheduler has had its look
        status = client.get("/api/refresh").json()
        assert status["enabled"] and status["ready"]                 # the page gets its update button
        assert status["job"] is None and status["remote"] is None    # nothing was started, dblp.org was not asked
        assert status["next_check_at"] is not None
        assert client.get("/api/meta").json()["total_papers"] == 6
        found = client.post("/api/authors/search", json={"queries": ["Bob"]}).json()
        assert found["results"][0]["authors"][0]["name"] == "Bob Brandt"
    assert (tmp_path / "data" / "current.json").is_file()
    assert db.exists()  # the snapshot itself is left alone


def test_a_second_start_uses_the_data_directory_not_the_snapshot_again(tmp_path, monkeypatch, snapshot_dir):
    monkeypatch.chdir(tmp_path)
    make_snapshot_db(snapshot_dir)
    for _ in range(2):
        with TestClient(app_from_args(serve_args())) as client:
            assert client.get("/api/refresh").json()["ready"]
    assert len(list((tmp_path / "data" / "versions").iterdir())) == 1


def test_interval_zero_switches_the_automatic_checks_off(tmp_path, monkeypatch, snapshot_dir):
    monkeypatch.chdir(tmp_path)
    make_snapshot_db(snapshot_dir)
    with TestClient(app_from_args(serve_args("--interval-days", "0"))) as client:
        status = client.get("/api/refresh").json()
        assert status["enabled"] and status["next_check_at"] is None  # the button still works


def test_giving_a_database_selects_the_fixed_mode_without_updates(tmp_path, monkeypatch, snapshot_dir):
    monkeypatch.chdir(tmp_path)
    db = make_snapshot_db(snapshot_dir)
    with TestClient(app_from_args(serve_args("--db", str(db)))) as client:
        assert client.get("/api/refresh").json() == {"enabled": False, "ready": True}
        assert client.get("/api/meta").json()["total_papers"] == 6
    assert not (tmp_path / "data").exists()


def test_the_data_directory_can_be_chosen(tmp_path, monkeypatch, snapshot_dir):
    monkeypatch.chdir(tmp_path)
    make_snapshot_db(snapshot_dir)
    with TestClient(app_from_args(serve_args("--data-dir", str(tmp_path / "elsewhere")))) as client:
        assert client.get("/api/refresh").json()["ready"]
    assert (tmp_path / "elsewhere" / "current.json").is_file() and not (tmp_path / "data").exists()
