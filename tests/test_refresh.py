"""The background update: check dblp.org, download, parse, build, validate, switch. All against a local HTTP server
that imitates dblp.org and a stub parser, so every path can be tested quickly and deterministically."""

import hashlib
import http.server
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quickdblp.app import create_app
from quickdblp.db import Database
from quickdblp.refresh import ProcessLock, RefreshManager

STUB = str(Path(__file__).parent / "stub_parser.py")


class DumpServer:
    """Serves /rdf/dblp.rdf.gz (HEAD, GET, Range/If-Range) and its .md5 like dblp.org does."""

    def __init__(self):
        self.content = bytes(range(256)) * 12000  # 3 MB: more than two download chunks
        self.etag = '"v1"'
        self.last_modified = "Fri, 16 Oct 2026 01:28:09 GMT"
        self.md5_mode = "ok"      # ok | bad | missing
        self.drop_after = None    # close the next full download after this many bytes
        self.requests = []        # (method, path, range header)
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _headers(self, length, status=200, extra=None):
                self.send_response(status)
                self.send_header("ETag", owner.etag)
                self.send_header("Last-Modified", owner.last_modified)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()

            def do_HEAD(self):
                owner.requests.append(("HEAD", self.path, None))
                self._headers(len(owner.content))

            def do_GET(self):
                owner.requests.append(("GET", self.path, self.headers.get("Range")))
                if self.path.endswith(".md5"):
                    if owner.md5_mode == "missing":
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    digest = hashlib.md5(owner.content).hexdigest() if owner.md5_mode == "ok" else "0" * 32
                    body = f"{digest}  dblp.rdf.gz\n".encode()
                    self._headers(len(body))
                    self.wfile.write(body)
                    return
                data, status, extra = owner.content, 200, None
                wanted = self.headers.get("Range")
                if wanted and self.headers.get("If-Range", owner.etag) == owner.etag:
                    start = int(wanted.split("=")[1].split("-")[0])
                    data = owner.content[start:]
                    status, extra = 206, {"Content-Range": f"bytes {start}-{len(owner.content) - 1}/{len(owner.content)}"}
                self._headers(len(data), status, extra)
                if owner.drop_after is not None and status == 200:
                    cut, owner.drop_after = owner.drop_after, None
                    self.wfile.write(data[:cut])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/rdf/dblp.rdf.gz"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def new_version(self, tag):
        self.etag = f'"{tag}"'
        self.last_modified = "Sat, 17 Oct 2026 02:00:00 GMT"
        self.content = bytes(range(256)) * 12100

    def count(self, method):
        """Requests of this kind for the dump itself (not its checksum)."""
        return sum(1 for m, path, _ in self.requests if m == method and not path.endswith(".md5"))

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def server():
    s = DumpServer()
    yield s
    s.close()


@pytest.fixture
def activated():
    return []


def make_manager(tmp_path, server, activated, **options):
    options.setdefault("retry_delay", 0)
    return RefreshManager(tmp_path / "data", on_activate=activated.append, dump_url=server.url,
                          parser_command=[sys.executable, STUB], **options)


def wait(manager, timeout=60):
    end = time.time() + timeout
    while manager.running():
        assert time.time() < end, "refresh did not finish"
        time.sleep(0.02)
    return manager.status()["job"]


def import_first(manager):
    answer = manager.request("initial")
    assert answer["started"], answer
    job = wait(manager)
    assert job["state"] == "succeeded", job
    return job


# ----------------------------------------------------------------------------------------------- the lock

def test_process_lock_is_exclusive_and_released(tmp_path):
    first, second = ProcessLock(tmp_path / "x.lock"), ProcessLock(tmp_path / "x.lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


# ----------------------------------------------------------------------------------------------- newer or not

def test_is_newer_by_etag_and_by_time():
    remote = {"etag": '"b"', "last_modified": "Sat, 17 Oct 2026 02:00:00 GMT"}
    assert RefreshManager.is_newer(remote, None)
    assert RefreshManager.is_newer(remote, {"dump": {"etag": '"a"'}, "source_time": "2099-01-01 00:00:00"})  # etag wins
    assert not RefreshManager.is_newer(remote, {"dump": {"etag": '"b"'}, "source_time": "2000-01-01 00:00:00"})
    # no etag stored (data from an older setup): compare the dump's time with the data's source time (UTC)
    assert RefreshManager.is_newer(remote, {"source_time": "2026-10-15 13:34:21.4565769"})
    assert not RefreshManager.is_newer(remote, {"source_time": "2026-10-17 02:00:01.000000"})


# ----------------------------------------------------------------------------------------------- the pipeline

def test_first_import_runs_the_whole_pipeline(tmp_path, server, activated):
    manager = make_manager(tmp_path, server, activated)
    assert manager.current() is None and manager.status()["ready"] is False
    job = import_first(manager)
    data = tmp_path / "data"
    assert job["progress"] == 1.0 and job["phase"] == "activate"
    assert len(activated) == 1 and activated[0].is_file()
    assert (data / "downloads" / "dblp.rdf.gz").read_bytes() == server.content
    assert not (data / "work" / job["id"]).exists()                    # the CSVs are cleaned up
    assert (data / "logs" / f"{job['id']}.log").is_file()
    status = manager.status()
    assert status["ready"] and status["current"]["papers"] == 6 and status["last_error"] is None
    assert status["remote"]["update_available"] is False
    db = Database(manager.current_database_path())
    try:
        assert [a["name"] for a in db.search_authors("Bob")["authors"]] == ["Bob Brandt"]
    finally:
        db.close()


def test_nothing_to_do_when_up_to_date(tmp_path, server, activated):
    manager = make_manager(tmp_path, server, activated)
    import_first(manager)
    downloads = server.count("GET")
    answer = manager.request("manual")
    assert not answer["started"] and answer["reason"] == "up_to_date"
    assert server.count("GET") == downloads and len(activated) == 1


def test_a_newer_dump_replaces_the_data_and_old_versions_are_pruned(tmp_path, server, activated):
    manager = make_manager(tmp_path, server, activated, keep_versions=2)
    import_first(manager)
    versions = tmp_path / "data" / "versions"
    for tag in ("v2", "v3"):
        server.new_version(tag)
        time.sleep(1.1)  # run ids have a resolution of one second
        answer = manager.request("manual")
        assert answer["started"], answer
        assert wait(manager)["state"] == "succeeded"
    assert len(activated) == 3
    assert manager.current_database_path() == activated[-1]
    assert sorted(p.name for p in versions.iterdir()) == sorted(p.parent.name for p in activated[-2:])
    assert manager.status()["remote"]["update_available"] is False


def test_forcing_refreshes_even_if_up_to_date(tmp_path, server, activated):
    manager = make_manager(tmp_path, server, activated)
    import_first(manager)
    time.sleep(1.1)
    assert manager.request("manual", force=True)["started"]
    assert wait(manager)["state"] == "succeeded" and len(activated) == 2


def test_simultaneous_requests_start_exactly_one_refresh(tmp_path, server, activated, monkeypatch):
    monkeypatch.setenv("STUB_DELAY", "1.5")
    manager = make_manager(tmp_path, server, activated)
    answers = []
    threads = [threading.Thread(target=lambda: answers.append(manager.request("manual"))) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(a["started"] for a in answers) == 1
    assert sorted(a["reason"] for a in answers if not a["started"]) == ["already_running"] * 9
    assert wait(manager)["state"] == "succeeded"
    assert server.count("GET") == 1 and len(activated) == 1  # downloaded and activated once, not ten times


# ----------------------------------------------------------------------------------------------- downloads

def test_an_interrupted_download_is_resumed(tmp_path, server, activated):
    server.drop_after = len(server.content) // 2
    manager = make_manager(tmp_path, server, activated)
    job = import_first(manager)
    assert any("resuming" in line for line in job["log"])
    assert any(r[0] == "GET" and r[2] and r[2].startswith("bytes=") for r in server.requests)
    assert (tmp_path / "data" / "downloads" / "dblp.rdf.gz").read_bytes() == server.content


def test_a_corrupt_download_is_rejected_and_deleted(tmp_path, server, activated):
    server.md5_mode = "bad"
    manager = make_manager(tmp_path, server, activated)
    assert manager.request("initial")["started"]
    job = wait(manager)
    assert job["state"] == "failed" and "corrupt" in job["error"]
    assert not (tmp_path / "data" / "downloads" / "dblp.rdf.gz").exists()
    assert manager.current() is None and activated == []
    assert manager.status()["last_error"]["message"] == job["error"]


def test_a_missing_checksum_is_tolerated(tmp_path, server, activated):
    server.md5_mode = "missing"
    import_first(make_manager(tmp_path, server, activated))


def test_a_downloaded_dump_is_reused_after_a_failed_parse(tmp_path, server, activated, monkeypatch):
    manager = make_manager(tmp_path, server, activated)
    monkeypatch.setenv("STUB_MODE", "crash")
    assert manager.request("initial")["started"]
    assert wait(manager)["state"] == "failed"
    gets = server.count("GET")
    monkeypatch.setenv("STUB_MODE", "ok")
    assert manager.request("initial", force=True)["started"]
    job = wait(manager)
    assert job["state"] == "succeeded" and server.count("GET") == gets  # not downloaded again
    assert any("already downloaded" in line for line in job["log"])


# ----------------------------------------------------------------------------------------------- failures keep the old data

@pytest.mark.parametrize("mode, message", [
    ("crash", "parser failed"),
    ("failed_records", "malformed records"),
    ("dangling", "inconsistent"),
])
def test_a_bad_new_version_never_replaces_the_good_one(tmp_path, server, activated, monkeypatch, mode, message):
    manager = make_manager(tmp_path, server, activated)
    import_first(manager)
    good = manager.current_database_path()
    server.new_version("v2")
    monkeypatch.setenv("STUB_MODE", mode)
    assert manager.request("manual")["started"]
    job = wait(manager)
    assert job["state"] == "failed" and message in job["error"]
    assert manager.current_database_path() == good and len(activated) == 1
    assert good.is_file()
    status = manager.status()
    assert message in status["last_error"]["message"] and status["last_error"]["log_tail"]
    assert status["ready"] and status["remote"]["update_available"] is True


def test_a_much_smaller_new_database_is_rejected(tmp_path, server, activated):
    manager = make_manager(tmp_path, server, activated, min_size_ratio=1.5)  # i.e. the data must grow by half
    import_first(manager)
    server.new_version("v2")
    time.sleep(1.1)
    assert manager.request("manual")["started"]
    job = wait(manager)
    assert job["state"] == "failed" and "much fewer" in job["error"]


def test_after_a_failure_the_next_try_has_to_wait(tmp_path, server, activated, monkeypatch):
    manager = make_manager(tmp_path, server, activated)
    monkeypatch.setenv("STUB_MODE", "crash")
    manager.request("initial")
    wait(manager)
    answer = manager.request("manual")
    assert not answer["started"] and answer["reason"] == "recent_failure" and answer["retry_at"] > time.time()
    assert manager.request("manual", force=True)["started"]  # forcing is allowed
    wait(manager)


def test_a_stale_parser_lock_file_does_not_block(tmp_path, server, activated):
    downloads = tmp_path / "data" / "downloads"
    downloads.mkdir(parents=True)
    (downloads / "dblp.rdf.gz.lock").write_text("left behind by a killed parser")
    import_first(make_manager(tmp_path, server, activated))
    assert not (downloads / "dblp.rdf.gz.lock").exists()


def test_a_refresh_in_another_process_blocks_this_one(tmp_path, server, activated):
    manager = make_manager(tmp_path, server, activated)
    other = ProcessLock(tmp_path / "data" / "refresh.lock")
    assert other.acquire()
    try:
        assert manager.request("initial")["started"]
        job = wait(manager)
        assert job["state"] == "failed" and "another process" in job["error"]
    finally:
        other.release()
    assert server.count("GET") == 0


def test_the_parser_must_exist(tmp_path, server, activated):
    manager = make_manager(tmp_path, server, activated)
    manager.parser_command = None
    manager.request("initial")
    assert "parser" in wait(manager)["error"]


# ----------------------------------------------------------------------------------------------- the schedule

def test_scheduler_decisions(tmp_path, server, activated):
    now = [1_000_000.0]
    manager = make_manager(tmp_path, server, activated, interval_days=7, clock=lambda: now[0])
    manager._tick()                                  # no data at all: the initial import starts
    assert wait(manager)["state"] == "succeeded" and manager.current() is not None
    heads = server.count("HEAD")
    manager._tick()                                  # just checked: nothing to do
    assert server.count("HEAD") == heads
    now[0] += 6 * 86400
    manager._tick()
    assert server.count("HEAD") == heads
    now[0] += 2 * 86400                              # a week has passed: ask dblp.org ...
    manager._tick()
    assert server.count("HEAD") == heads + 1 and not manager.running()  # ... nothing newer
    assert manager.status()["next_check_at"] == now[0] + 7 * 86400
    server.new_version("v2")
    time.sleep(1.1)
    now[0] += 8 * 86400                              # next week there is a new dump: it is fetched
    manager._tick()
    assert manager.running()
    assert wait(manager)["state"] == "succeeded" and len(activated) == 2


# ----------------------------------------------------------------------------------------------- through the web app

def test_the_app_imports_on_its_own_and_switches_over(tmp_path, server, monkeypatch):
    monkeypatch.setenv("STUB_DELAY", "1.5")
    app = create_app(data_dir=tmp_path / "data", warm_up=False, refresh_options={
        "dump_url": server.url, "parser_command": [sys.executable, STUB], "retry_delay": 0})
    with TestClient(app) as client:
        # no data yet: the first import has started by itself and the data endpoints say so
        status = client.get("/api/refresh").json()
        assert status["enabled"] and status["ready"] is False
        assert client.get("/api/meta").json() == {"ready": False}
        r = client.post("/api/authors/search", json={"queries": ["Bob"]})
        assert r.status_code == 503 and "first import" in r.json()["detail"]
        # everybody who asks meanwhile joins the running import
        answers = [client.post("/api/refresh", json={}).json() for _ in range(3)]
        assert all(not a["started"] and a["reason"] == "already_running" for a in answers)
        end = time.time() + 60
        while client.get("/api/refresh").json()["job"]["state"] == "running":
            assert time.time() < end
            time.sleep(0.1)
        # the new database is live without a restart
        assert client.get("/api/refresh").json()["ready"] is True
        assert client.get("/api/meta").json()["ready"] is True
        found = client.post("/api/authors/search", json={"queries": ["Bob"]}).json()
        assert found["results"][0]["authors"][0]["name"] == "Bob Brandt"
        again = client.post("/api/refresh", json={}).json()
        assert not again["started"] and again["reason"] == "up_to_date"
        # a newer dump: the update runs in the background while searching keeps working
        server.new_version("v2")
        time.sleep(1.1)
        assert client.post("/api/refresh", json={}).json()["started"]
        assert client.post("/api/authors/search", json={"queries": ["Bob"]}).status_code == 200
        end = time.time() + 60
        while client.get("/api/refresh").json()["job"]["state"] == "running":
            assert time.time() < end
            time.sleep(0.1)
        assert client.get("/api/refresh").json()["job"]["state"] == "succeeded"
        assert client.post("/api/authors/search", json={"queries": ["Bob"]}).status_code == 200


def test_a_fixed_database_cannot_be_updated(snapshot_dir, tmp_path):
    app = create_app(tmp_path / "dblp.duckdb", snapshot_dir=snapshot_dir)
    with TestClient(app) as client:
        assert client.get("/api/refresh").json() == {"enabled": False, "ready": True}
        assert client.post("/api/refresh", json={}).status_code == 409


# ----------------------------------------------------------------------------------------------- adopting an existing database

def test_an_existing_database_can_be_adopted(tmp_path, server, activated, snapshot_dir):
    from quickdblp.cli import main
    from quickdblp.db import build_snapshot

    old_db = tmp_path / "snapshot.duckdb"
    build_snapshot(snapshot_dir, old_db)          # a database made from data of 2026-06-18
    data = tmp_path / "data"
    assert main(["adopt", "--data-dir", str(data), "--db", str(old_db)]) == 0
    assert old_db.exists()                         # copied, not moved
    manager = make_manager(tmp_path, server, activated)
    status = manager.status()
    assert status["ready"] and status["current"]["papers"] == 6
    assert manager.is_newer({"etag": '"v1"', "last_modified": server.last_modified}, manager.current())  # dump is newer
    assert main(["adopt", "--data-dir", str(data), "--db", str(old_db)]) == 1  # no second adoption
    assert manager.request("manual")["started"]   # so the first check replaces it
    assert wait(manager)["state"] == "succeeded" and len(activated) == 1


def test_adopting_can_move_the_file(tmp_path, snapshot_dir):
    from quickdblp.cli import main
    from quickdblp.db import build_snapshot

    old_db = tmp_path / "snapshot.duckdb"
    build_snapshot(snapshot_dir, old_db)
    assert main(["adopt", "--data-dir", str(tmp_path / "data"), "--db", str(old_db), "--move"]) == 0
    assert not old_db.exists()


def test_a_relative_data_directory_works(tmp_path, server, activated, monkeypatch):
    # the parser runs in a work directory of its own: it must be given an absolute path to the dump
    monkeypatch.chdir(tmp_path)
    manager = RefreshManager("data", on_activate=activated.append, dump_url=server.url,
                             parser_command=[sys.executable, STUB], retry_delay=0)
    import_first(manager)
    assert manager.current_database_path().is_absolute() and manager.current_database_path().is_file()
