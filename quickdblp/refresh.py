"""Keeping the data up to date: check dblp.org, download the dump, parse it, build the database, validate it,
and switch the server over to it - in the background, while the server keeps answering with the old data.

Layout of the data directory:

    current.json            which database is active (written last, atomically: the switch)
    state.json              what happened so far (last check, last error, ...)
    refresh.lock            OS-level lock held while a refresh runs (released automatically if the process dies)
    downloads/              the dump (dblp.rdf.gz, plus a .json with its ETag) and a partial download
    versions/<run>/         one database per successful refresh (the last two are kept)
    work/<run>/             the parser output while a refresh runs (deleted afterwards)
    logs/<run>.log          log of the last refreshes

Only one refresh runs at a time. Requests that arrive while one is running simply join it (they get the same
status), so many users clicking "update" at once cause exactly one refresh.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

import requests

from .db import Database, build_snapshot

log = logging.getLogger("quickdblp.refresh")

DUMP_URL = "https://dblp.org/rdf/dblp.rdf.gz"

# share of the overall progress bar that belongs to each phase
PHASE_SPAN = {
    "check": (0.0, 0.02), "download": (0.02, 0.40), "parse": (0.40, 0.75),
    "build": (0.75, 0.92), "validate": (0.92, 0.97), "activate": (0.97, 1.0),
}

DISK_FACTOR = 5  # free space needed, as a multiple of the dump size: dump + CSVs + new and old database + temp


class RefreshError(RuntimeError):
    pass


class ProcessLock:
    """Exclusive lock on a file. The operating system drops it when the process ends, however it ends, so
    unlike a lock *file* it can never be left behind."""

    def __init__(self, path: Path):
        self._path = path
        self._file = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self._path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                f.seek(0, os.SEEK_END)
                if f.tell() == 0:
                    f.write(b" ")
                    f.flush()
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._file = f
        return True

    def release(self) -> None:
        f, self._file = self._file, None
        if f is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


class RefreshJob:
    """What the page shows: phase, progress, a few log lines."""

    def __init__(self, trigger: str, force: bool):
        self.id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:4]
        self.trigger, self.force = trigger, force
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.state = "running"  # running | succeeded | up_to_date | failed
        self.phase, self.progress, self.detail = "check", 0.0, ""
        self.error: str | None = None
        self.log: deque[str] = deque(maxlen=300)

    def set(self, phase: str, fraction: float = 0.0, detail: str = "") -> None:
        low, high = PHASE_SPAN[phase]
        self.phase, self.detail = phase, detail
        self.progress = low + (high - low) * max(0.0, min(1.0, fraction))

    def say(self, message: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {message}")
        log.info("[%s] %s", self.id, message)

    def as_dict(self, log_lines: int = 40) -> dict:
        return {
            "id": self.id, "trigger": self.trigger, "state": self.state, "phase": self.phase,
            "progress": round(self.progress, 3), "detail": self.detail, "error": self.error,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "log": list(self.log)[-log_lines:],
        }


def find_parser() -> list[str] | None:
    """The ponder_dblp executable: $QUICKDBLP_PARSER, the PATH, or a build in this repository."""
    candidates: list[str] = []
    if os.environ.get("QUICKDBLP_PARSER"):
        candidates.append(os.environ["QUICKDBLP_PARSER"])
    found = shutil.which("ponder_dblp")
    if found:
        candidates.append(found)
    # The build trees of the two operating systems are separate (see ponder_dblp/CMakePresets.json), so a checkout
    # shared between Windows and WSL never picks up the other system's binary.
    root = Path(__file__).resolve().parent.parent / "ponder_dblp"
    system = "windows" if os.name == "nt" else "linux"
    exe = "ponder_dblp.exe" if os.name == "nt" else "ponder_dblp"
    for preset in ("x64-release", "x64-relwithdeb"):
        candidates.append(str(root / "install" / system / preset / "bin" / exe))
        candidates.append(str(root / "build" / system / preset / exe))
    for c in candidates:
        if Path(c).is_file():
            return [c]
    return None


def _parser_environment(command: list[str]) -> dict:
    """The parser's DLLs sit next to it, or (in a build tree) in vcpkg_installed/."""
    env = dict(os.environ)
    exe_dir = Path(command[0]).resolve().parent
    dirs = [exe_dir, *exe_dir.glob("vcpkg_installed/*/bin")]
    env["PATH"] = os.pathsep.join(str(d) for d in dirs) + os.pathsep + env.get("PATH", "")
    return env


def _utc(text: str | None) -> datetime | None:
    """A timestamp from an HTTP header or from the parser's metadata (UTC) as an aware datetime."""
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(text[:26])
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class RefreshManager:
    def __init__(
        self,
        data_dir: str | Path,
        on_activate: Callable[[Path], None],
        dump_url: str = DUMP_URL,
        parser_command: list[str] | None = None,
        interval_days: float = 7,
        retry_after_failure_minutes: float = 30,
        manual_retry_after_failure_minutes: float = 5,
        keep_versions: int = 2,
        min_size_ratio: float = 0.9,
        retry_delay: float = 5,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.data_dir = Path(data_dir).resolve()  # absolute: the parser runs in another directory
        self.on_activate = on_activate
        self.dump_url = dump_url
        self.parser_command = parser_command if parser_command is not None else find_parser()
        self.interval = interval_days * 86400
        self.retry_after_failure = retry_after_failure_minutes * 60
        self.manual_retry_after_failure = manual_retry_after_failure_minutes * 60
        self.keep_versions = keep_versions
        self.min_size_ratio = min_size_ratio
        self.retry_delay = retry_delay
        self._session = session or requests.Session()
        self._clock = clock

        self._request_lock = threading.Lock()   # one request at a time decides whether a job starts
        self._state_lock = threading.Lock()     # protects the state file and the job reference
        self._job: RefreshJob | None = None
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._state = self._read_json(self.data_dir / "state.json") or {}

    # ----------------------------------------------------------------- files

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save_state(self) -> None:
        with self._state_lock:
            _atomic_write(self.data_dir / "state.json", self._state)

    def current(self) -> dict | None:
        """The active version (current.json), if its database file exists."""
        pointer = self._read_json(self.data_dir / "current.json")
        if pointer and (self.data_dir / pointer.get("database", "")).is_file():
            return pointer
        return None

    def current_database_path(self) -> Path | None:
        pointer = self.current()
        return self.data_dir / pointer["database"] if pointer else None

    def adopt(self, db_path: str | Path, move: bool = False) -> Path:
        """Use an existing database (e.g. snapshot/dblp.duckdb) as the active one, without importing the dump again.

        Its data counts as "from the dump of its source time", so the next check updates it if dblp.org has a newer one."""
        if self.current() is not None:
            raise RefreshError("this data directory already has an active database")
        db = Database(db_path, max_concurrent=1)
        try:
            meta = db.meta()
        finally:
            db.close()
        run_id = "adopted-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        destination = self.data_dir / "versions" / run_id / "dblp.duckdb"
        destination.parent.mkdir(parents=True)
        if move:
            shutil.move(str(db_path), destination)
        else:
            try:
                os.link(db_path, destination)  # same volume: no second copy of the 2 GB file
            except OSError:
                shutil.copy2(db_path, destination)
        with self._state_lock:
            self._state["last_check_at"] = self._clock()  # a fresh start is not the moment for a surprise update
        self._save_state()
        _atomic_write(self.data_dir / "current.json", {
            "database": destination.relative_to(self.data_dir).as_posix(),
            "run_id": run_id,
            "activated_at": self._clock(),
            "dump": {},
            "source_time": meta["snapshot_time"],
            "papers": meta["total_papers"],
            "authors": meta["total_authors"],
        })
        return destination

    # ----------------------------------------------------------------- remote

    def check_remote(self) -> dict:
        """Ask dblp.org about the dump (HEAD request only) and remember the answer."""
        response = self._session.head(self.dump_url, allow_redirects=True, timeout=30)
        response.raise_for_status()
        remote = {
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
            "content_length": int(response.headers.get("Content-Length") or 0),
            "checked_at": self._clock(),
        }
        with self._state_lock:
            self._state["remote"] = remote
            self._state["last_check_at"] = remote["checked_at"]
        self._save_state()
        return remote

    @staticmethod
    def is_newer(remote: dict, current: dict | None) -> bool:
        """Is the online dump newer than the one the active database was made from?"""
        if current is None:
            return True
        dump = current.get("dump") or {}
        if dump.get("etag") and remote.get("etag"):
            return dump["etag"] != remote["etag"]
        remote_time, source_time = _utc(remote.get("last_modified")), _utc(current.get("source_time"))
        if remote_time is None or source_time is None:
            return True
        return remote_time > source_time

    # ----------------------------------------------------------------- requests and status

    def running(self) -> bool:
        with self._state_lock:
            return self._job is not None and self._job.state == "running"

    def request(self, trigger: str = "manual", force: bool = False) -> dict:
        """Start a refresh unless one is running, the data is current, or a recent attempt failed.

        Returns {"started": bool, "reason": ..., "status": ...}. Many callers at once start one refresh."""
        with self._request_lock:
            if self.running():
                return self._answer(False, "already_running")
            now = self._clock()
            failed_at = self._state.get("last_failure_at")
            wait = self.manual_retry_after_failure if trigger == "manual" else self.retry_after_failure
            if failed_at and now - failed_at < wait and not force:
                return self._answer(False, "recent_failure", retry_at=failed_at + wait)
            if self.current() is not None and not force:
                try:
                    remote = self.check_remote()
                except Exception as e:  # network down, dblp.org unreachable
                    return self._answer(False, "check_failed", message=str(e))
                if not self.is_newer(remote, self.current()):
                    return self._answer(False, "up_to_date")
            job = RefreshJob(trigger, force)
            with self._state_lock:
                self._job = job
            self._thread = threading.Thread(target=self._run, args=(job,), name="refresh", daemon=True)
            self._thread.start()
            return self._answer(True, "started")

    def _answer(self, started: bool, reason: str, **extra) -> dict:
        return {"started": started, "reason": reason, **extra, "status": self.status()}

    def status(self) -> dict:
        with self._state_lock:
            state = json.loads(json.dumps(self._state))  # a copy
            job = self._job
        current = self.current()
        remote = state.get("remote")
        last_check = state.get("last_check_at")
        return {
            "enabled": True,
            "ready": current is not None,
            "job": job.as_dict() if job else None,
            "current": None if current is None else {
                "source_time": current.get("source_time"), "activated_at": current.get("activated_at"),
                "papers": current.get("papers"), "authors": current.get("authors"),
            },
            "remote": None if not remote else {
                "last_modified": remote.get("last_modified"), "checked_at": remote.get("checked_at"),
                "content_length": remote.get("content_length"),
                "update_available": self.is_newer(remote, current),
            },
            "last_error": state.get("last_error"),
            "interval_days": self.interval / 86400,
            "next_check_at": None if last_check is None or self.interval <= 0 else last_check + self.interval,
            "parser_available": self.parser_command is not None,
        }

    # ----------------------------------------------------------------- scheduler

    def start(self) -> None:
        """Start the scheduler: an initial import if there is no data, and a check every interval."""
        if self._scheduler is None:
            self._scheduler = threading.Thread(target=self._scheduler_loop, name="refresh-scheduler", daemon=True)
            self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("scheduler error")
            self._stop.wait(30)

    def _tick(self) -> None:
        if self.running():
            return
        if self.current() is None:
            log.info("no data yet: starting the initial import")
            self.request("initial")
            return
        if self.interval <= 0:
            return  # automatic checks are off; the button still works
        last_check = self._state.get("last_check_at")
        if last_check is None or self._clock() - last_check >= self.interval:
            log.info("scheduled check")
            self.request("schedule")

    # ----------------------------------------------------------------- the refresh itself

    def _run(self, job: RefreshJob) -> None:
        lock = ProcessLock(self.data_dir / "refresh.lock")
        try:
            if not lock.acquire():
                raise RefreshError("another process is already refreshing this data directory")
            self._pipeline(job)
        except BaseException as e:  # noqa: BLE001 - whatever happens, the job must end up finished
            job.error = str(e) or type(e).__name__
            job.state = "failed"
            job.say(f"FAILED: {job.error}")
            if not isinstance(e, RefreshError):
                log.exception("refresh failed")
        finally:
            lock.release()
            job.finished_at = self._clock()
            self._record(job)
            shutil.rmtree(self.data_dir / "work" / job.id, ignore_errors=True)

    def _record(self, job: RefreshJob) -> None:
        with self._state_lock:
            if job.state == "failed":
                self._state["last_failure_at"] = job.finished_at
                self._state["last_error"] = {"at": job.finished_at, "message": job.error, "log_tail": list(job.log)[-30:]}
            else:
                self._state.pop("last_failure_at", None)
                self._state.pop("last_error", None)
                self._state["last_check_at"] = job.finished_at
            history = self._state.setdefault("history", [])
            history.append({"id": job.id, "trigger": job.trigger, "state": job.state, "finished_at": job.finished_at,
                            "error": job.error})
            del history[:-10]
        self._save_state()
        logs = self.data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / f"{job.id}.log").write_text("\n".join(job.log), encoding="utf-8")
        for old in sorted(logs.glob("*.log"))[:-10]:
            old.unlink(missing_ok=True)

    def _pipeline(self, job: RefreshJob) -> None:
        if not self.parser_command:
            raise RefreshError("the parser (ponder_dblp) was not found; build it or set QUICKDBLP_PARSER")
        job.set("check", 0, "asking dblp.org")
        remote = self.check_remote()
        current = self.current()
        if current is not None and not job.force and not self.is_newer(remote, current):
            job.state = "up_to_date"
            job.say("the data is already up to date")
            return
        job.say(f"dump on dblp.org: modified {remote['last_modified']}, {remote['content_length'] / 1e9:.2f} GB")

        self.data_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.data_dir).free
        if remote["content_length"] and free < DISK_FACTOR * remote["content_length"]:
            raise RefreshError(
                f"not enough free disk space: {free / 1e9:.1f} GB free, about {DISK_FACTOR * remote['content_length'] / 1e9:.0f} GB needed")

        shutil.rmtree(self.data_dir / "work", ignore_errors=True)  # leftovers of a crashed run
        dump = self._download(job, remote)
        work = self.data_dir / "work" / job.id
        work.mkdir(parents=True)
        metadata = self._parse(job, dump, work)
        database = self.data_dir / "versions" / job.id / "dblp.duckdb"
        database.parent.mkdir(parents=True)
        job.set("build", 0, "building the database")
        job.say("building the database")
        build_snapshot(work, database, force=True)
        meta = self._validate(job, database, current)
        self._activate(job, database, remote, meta, work / "dblp_metadata.json")
        job.set("activate", 1.0, "done")
        job.state = "succeeded"
        job.say(f"done: {meta['total_papers']:,} papers, {meta['total_authors']:,} authors")

    # -- download

    def _download(self, job: RefreshJob, remote: dict) -> Path:
        folder = self.data_dir / "downloads"
        folder.mkdir(parents=True, exist_ok=True)
        dest, part = folder / "dblp.rdf.gz", folder / "dblp.rdf.gz.part"
        info_file, part_info = folder / "dblp.rdf.gz.json", folder / "dblp.rdf.gz.part.json"
        total = remote["content_length"]

        done = self._read_json(info_file)
        if dest.is_file() and done and done.get("etag") == remote["etag"] and dest.stat().st_size == total:
            job.say("the dump was already downloaded, reusing it")
            return dest

        for attempt in range(1, 5):
            start = 0
            known = self._read_json(part_info)
            if part.is_file() and known and known.get("etag") == remote["etag"] and 0 < part.stat().st_size < total:
                start = part.stat().st_size
            headers = {"Range": f"bytes={start}-", "If-Range": remote["etag"]} if start and remote["etag"] else {}
            if not start:
                _atomic_write(part_info, {"etag": remote["etag"]})
            try:
                with self._session.get(self.dump_url, stream=True, headers=headers, timeout=(30, 120)) as response:
                    response.raise_for_status()
                    if response.status_code != 206:
                        start = 0
                    job.say(f"downloading (attempt {attempt}" + (f", resuming at {start / 1e6:.0f} MB)" if start else ")"))
                    with open(part, "ab" if start else "wb") as out:
                        received = start
                        for chunk in response.iter_content(1 << 20):
                            if self._stop.is_set():
                                raise RefreshError("stopped")
                            out.write(chunk)
                            received += len(chunk)
                            job.set("download", received / total if total else 0,
                                    f"{received / 1e9:.2f} of {total / 1e9:.2f} GB")
                if total and part.stat().st_size != total:
                    raise requests.ConnectionError(f"incomplete: {part.stat().st_size} of {total} bytes")
                break
            except (requests.RequestException, OSError) as e:
                job.say(f"download interrupted: {e}")
                if attempt == 4:
                    raise RefreshError(f"download failed: {e}") from e
                time.sleep(min(self.retry_delay * attempt, 30))

        job.set("download", 1.0, "verifying")
        self._verify_checksum(job, part)
        modified = _utc(remote.get("last_modified"))
        if modified:
            os.utime(part, (modified.timestamp(), modified.timestamp()))  # the parser records this time
        os.replace(part, dest)
        part_info.unlink(missing_ok=True)
        _atomic_write(info_file, {"etag": remote["etag"], "last_modified": remote["last_modified"], "content_length": total})
        return dest

    def _verify_checksum(self, job: RefreshJob, path: Path) -> None:
        try:
            response = self._session.get(self.dump_url + ".md5", timeout=30)
            response.raise_for_status()
            expected = response.text.split()[0].lower()
        except (requests.RequestException, IndexError):
            job.say("no checksum available, skipping the check")
            return
        digest = hashlib.md5()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 24), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            path.unlink(missing_ok=True)
            raise RefreshError("the downloaded dump is corrupt (MD5 mismatch); it was deleted")
        job.say("checksum ok")

    # -- parse

    def _parse(self, job: RefreshJob, dump: Path, work: Path) -> dict:
        command = [*self.parser_command, str(dump)]
        Path(str(dump) + ".lock").unlink(missing_ok=True)  # we hold the refresh lock, so this one is stale
        job.set("parse", 0, "starting the parser")
        job.say("parsing: " + " ".join(command))
        tail = bytearray()
        self._proc = proc = subprocess.Popen(
            command, cwd=work, env=_parser_environment(self.parser_command),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                tail += chunk
                del tail[:-8000]
                matches = re.findall(rb"(\d{1,3}) %", chunk)
                if matches:
                    percent = int(matches[-1])
                    job.set("parse", percent / 100, f"{percent} %")
            code = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
            self._proc = None
        output = [line for line in tail.decode("utf-8", "replace").replace("\r", "\n").splitlines()
                  if line.strip() and not re.match(r"^\[[= >]*\]\s*\d+ %", line)]
        for line in output[-8:]:
            job.say("parser: " + line)
        if code != 0:
            raise RefreshError(f"the parser failed (exit code {code}): {' | '.join(output[-3:])}")
        metadata = self._read_json(work / "dblp_metadata.json")
        if metadata is None:
            raise RefreshError("the parser did not write dblp_metadata.json")
        if metadata.get("records_failed"):
            raise RefreshError(f"the parser skipped {metadata['records_failed']} malformed records")
        return metadata

    # -- validate and switch

    def _validate(self, job: RefreshJob, database: Path, current: dict | None) -> dict:
        job.set("validate", 0, "checking the new database")
        job.say("validating")
        db = Database(database, max_concurrent=2)
        try:
            meta = db.meta()
            db.search_authors("a", limit=1)  # the query machinery works
        finally:
            db.close()
        problems = {k: v for k, v in meta["integrity"].items() if v and k != "duplicate_links_removed"}
        if problems:
            raise RefreshError(f"the new database is inconsistent: {problems}")
        if not meta["total_papers"] or not meta["total_authors"]:
            raise RefreshError("the new database is empty")
        if current:
            for key, label in (("papers", "papers"), ("authors", "authors")):
                before, after = current.get(key) or 0, meta[f"total_{key}"]
                if before and after < self.min_size_ratio * before:
                    raise RefreshError(f"the new data has much fewer {label} than the current data "
                                       f"({after:,} instead of {before:,}); not replacing it")
        return meta

    def _activate(self, job: RefreshJob, database: Path, remote: dict, meta: dict, metadata_file: Path) -> None:
        job.set("activate", 0, "switching to the new database")
        if metadata_file.is_file():
            shutil.copy(metadata_file, database.parent / "dblp_metadata.json")
        pointer = {
            "database": database.relative_to(self.data_dir).as_posix(),
            "run_id": job.id,
            "activated_at": self._clock(),
            "dump": {"etag": remote["etag"], "last_modified": remote["last_modified"], "content_length": remote["content_length"]},
            "source_time": meta["snapshot_time"],
            "papers": meta["total_papers"],
            "authors": meta["total_authors"],
        }
        _atomic_write(self.data_dir / "current.json", pointer)  # from here on a restart uses the new database
        self.on_activate(database)
        job.say("the new database is active")
        versions = sorted((self.data_dir / "versions").iterdir(), key=lambda p: p.name)
        for old in versions[:-self.keep_versions]:
            if old != database.parent:
                shutil.rmtree(old, ignore_errors=True)
