"""DuckDB-backed data layer: snapshot -> persisted database, author search, co-author queries."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import duckdb

log = logging.getLogger("quickdblp")

SCHEMA_VERSION = 2

SNAPSHOT_FILES = ("dblp_authors.csv", "dblp_papers.csv", "dblp_papers_authors.csv")
METADATA_FILE = "dblp_metadata.json"

PAPER_TYPES = {
    1: "Inproceedings",
    2: "Incollection",
    3: "Article",
    4: "Book",
    5: "Part",
    6: "Informal",
    7: "Data",
    8: "Reference",
    9: "Editorship",
    10: "Withdrawn",
}

# Types that are not counted as conflicts unless the user asks for them (the page unticks them initially).
DEFAULT_EXCLUDED_TYPES = [8, 9, 10]

PAPER_TYPE_HELP = {
    8: "Entries of reference works such as encyclopedias and handbooks",
    9: "Edited volumes and proceedings: the people listed are editors, not authors",
    10: "Papers that were withdrawn after publication",
}

MATCHERS = ("prefix", "like", "accent", "fuzzy")

# DuckDB's strip_accents leaves e.g. "ß" or "ø" alone; good enough for name matching.
_NORM = "lower(strip_accents({}))"


class SnapshotError(RuntimeError):
    pass


def _csv(path: Path, types: str) -> str:
    return f"read_csv('{path.as_posix()}', delim='\\t', header=true, types={types})"


def fingerprint(snapshot_dir: str | Path) -> dict:
    """Describes the snapshot a database was (or would be) built from."""
    snapshot_dir = Path(snapshot_dir)
    meta_path = snapshot_dir / METADATA_FILE
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise SnapshotError(f"{meta_path} not found; run the parser and move its output into {snapshot_dir}") from e
    files = {}
    for name in SNAPSHOT_FILES:
        try:
            st = (snapshot_dir / name).stat()
        except FileNotFoundError as e:
            raise SnapshotError(f"{snapshot_dir / name} not found") from e
        files[name] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    return {"schema_version": SCHEMA_VERSION, "metadata": metadata, "files": files}


def _stored_fingerprint(db_path: Path) -> dict | None:
    if not db_path.exists():
        return None
    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except duckdb.Error:
        return None
    try:
        row = con.execute("select value from meta where key = 'fingerprint'").fetchone()
        return json.loads(row[0]) if row else None
    except duckdb.Error:
        return None
    finally:
        con.close()


def is_up_to_date(snapshot_dir: str | Path, db_path: str | Path) -> bool:
    return _stored_fingerprint(Path(db_path)) == fingerprint(snapshot_dir)


def build_snapshot(snapshot_dir: str | Path, db_path: str | Path, force: bool = False) -> bool:
    """Build the persisted database from the snapshot CSVs.

    Returns False if an up-to-date database already exists (and force is not set), True if it was (re)built.
    The database is built in a temporary file and moved into place only when complete and verified.
    """
    snapshot_dir, db_path = Path(snapshot_dir), Path(db_path)
    fp = fingerprint(snapshot_dir)
    if not force and _stored_fingerprint(db_path) == fp:
        return False

    tmp = db_path.with_name(db_path.name + ".tmp")
    for leftover in (tmp, tmp.with_name(tmp.name + ".wal")):
        leftover.unlink(missing_ok=True)

    con = duckdb.connect(str(tmp))
    try:
        authors, papers, links = (snapshot_dir / n for n in SNAPSHOT_FILES)
        work_dir = db_path.parent
        _load(con, "create table authors as select *, " + _NORM.format("Name") + " as NameNorm from {src}",
              authors, "{'NumericID': 'BIGINT', 'DBLP': 'VARCHAR', 'Name': 'VARCHAR', 'ORCID': 'VARCHAR'}", 4, work_dir)
        _load(con, "create table papers as select * from {src}",
              papers, "{'NumericID': 'BIGINT', 'DBLP': 'VARCHAR', 'Title': 'VARCHAR', 'Type': 'TINYINT', 'Year': 'SMALLINT'}",
              5, work_dir)
        _load(con, "create table papers_authors as select * from {src}",
              links, "{'PaperID': 'BIGINT', 'AuthorID': 'BIGINT'}", 2, work_dir)
        _verify_counts(con, fp["metadata"])
        integrity = _clean_and_check(con)
        # newer parsers count the malformed records they had to skip (older ones did not write the field)
        integrity["parser_records_failed"] = int(fp["metadata"].get("records_failed", 0))
        if integrity["parser_records_failed"]:
            log.warning("the parser skipped %d malformed records; the snapshot is incomplete", integrity["parser_records_failed"])
        con.execute("create index idx_pa_author on papers_authors(AuthorID)")
        con.execute("create index idx_pa_paper on papers_authors(PaperID)")
        con.execute("create table meta(key varchar primary key, value varchar)")
        con.execute(
            "insert into meta values ('fingerprint', ?), ('built_at', ?), ('integrity', ?)",
            [json.dumps(fp), datetime.now().isoformat(), json.dumps(integrity)],
        )
        con.execute("checkpoint")
    except BaseException:
        con.close()
        for leftover in (tmp, tmp.with_name(tmp.name + ".wal")):
            leftover.unlink(missing_ok=True)
        raise
    con.close()
    try:
        os.replace(tmp, db_path)
    except PermissionError as e:  # Windows: the old file is still open, e.g. by a running server
        tmp.unlink(missing_ok=True)
        raise SnapshotError(f"{db_path} is in use by another process (running server?); stop it and try again") from e
    return True


def _load(con: duckdb.DuckDBPyConnection, sql: str, path: Path, types: str, ncols: int, work_dir: Path) -> None:
    """Run `sql` (with `{src}` standing for the CSV) and, if the CSV is malformed, retry on a repaired copy."""
    try:
        con.execute(sql.format(src=_csv(path, types)))
        return
    except duckdb.InvalidInputException as first_error:
        error = first_error
    repaired = work_dir / (path.name + ".repaired.tmp")
    try:
        n = repair_tsv(path, repaired, ncols)
        if not n:
            raise SnapshotError(f"{path.name}: unreadable and nothing to repair:\n{error}") from error
        log.warning("%s contained %d record(s) with line breaks or tabs inside a field; loaded a repaired copy", path.name, n)
        con.execute("drop table if exists " + sql.split()[2])
        try:
            con.execute(sql.format(src=_csv(repaired, types)))
        except duckdb.InvalidInputException as e:
            raise SnapshotError(f"{path.name}: could not be loaded even after repair:\n{e}") from e
    finally:
        repaired.unlink(missing_ok=True)


def repair_tsv(src: Path, dst: Path, ncols: int) -> int:
    """Rewrite a tab-separated file so that every record is on one line with `ncols` fields.

    Old versions of the parser wrote tabs/line breaks inside titles and names unescaped. A record that is
    split over several lines is joined with single spaces; surplus tabs are folded into the third field
    (title/name, the only free-text column). Returns the number of records that needed repair.
    """
    repaired = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        pending: list[bytes] = []
        tabs = 0
        for raw in fin:
            line = raw.rstrip(b"\r\n")
            if not line:
                continue  # blank lines are never records (inside a split record they are just gaps)
            pending.append(line)
            tabs += line.count(b"\t")
            if tabs < ncols - 1:
                continue  # record continues on the next line
            record = pending[0] if len(pending) == 1 else b" ".join(p for p in pending if p)
            fields = record.split(b"\t")
            if len(pending) > 1 or len(fields) != ncols:
                repaired += 1
                extra = len(fields) - ncols
                if extra > 0:
                    fields[2:3 + extra] = [b" ".join(fields[2:3 + extra])]
                record = b"\t".join(fields)
            fout.write(record + b"\n")
            pending, tabs = [], 0
        if pending:
            raise SnapshotError(f"{src.name}: file ends in the middle of a record")
    return repaired


def _clean_and_check(con: duckdb.DuckDBPyConnection) -> dict:
    """Drop duplicate links and report inconsistencies of the loaded snapshot.

    Duplicate (paper, author) links occur when DBLP lists the same person twice on a paper; kept, they
    would inflate paper counts. Dangling links / papers without authors cannot be repaired here: they
    mean the parser lost records (see `quickdblp audit`), so they are reported, not hidden.
    """
    (before,) = con.execute("select count(*) from papers_authors").fetchone()
    con.execute("create table pa_unique as select distinct PaperID, AuthorID from papers_authors")
    con.execute("drop table papers_authors")
    con.execute("alter table pa_unique rename to papers_authors")
    (after,) = con.execute("select count(*) from papers_authors").fetchone()
    one = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    integrity = {
        "duplicate_links_removed": before - after,
        "links_to_missing_papers": one(
            "select count(*) from papers_authors l where not exists (select 1 from papers p where p.NumericID = l.PaperID)"),
        "links_to_missing_authors": one(
            "select count(*) from papers_authors l where not exists (select 1 from authors a where a.NumericID = l.AuthorID)"),
        "papers_without_authors": one(
            "select count(*) from papers p where not exists (select 1 from papers_authors l where l.PaperID = p.NumericID)"),
    }
    if integrity["duplicate_links_removed"]:
        log.info("removed %d duplicate (paper, author) links", integrity["duplicate_links_removed"])
    problems = {k: v for k, v in integrity.items() if v and k != "duplicate_links_removed"}
    if problems:
        log.warning("snapshot is inconsistent (the parser lost records, rebuild it with the current ponder_dblp): %s", problems)
    return integrity


def _verify_counts(con: duckdb.DuckDBPyConnection, metadata: dict) -> None:
    expected = {"authors": "total_authors", "papers": "total_papers", "papers_authors": "total_links"}
    for table, key in expected.items():
        if key not in metadata:
            continue
        (actual,) = con.execute(f"select count(*) from {table}").fetchone()
        if actual != metadata[key]:
            raise SnapshotError(f"{table}: {actual} rows loaded but metadata says {metadata[key]}; snapshot incomplete?")


_MEMORY_LIMIT = re.compile(r"^\d+(\.\d+)?\s*(KB|MB|GB|TB|KiB|MiB|GiB|TiB)$", re.IGNORECASE)


def apply_resource_limits(con: duckdb.DuckDBPyConnection, threads: int | None = None, memory_limit: str | None = None) -> None:
    """SET threads / memory_limit (DuckDB cannot bind parameters in SET, so the values are validated)."""
    if threads:
        con.execute(f"set threads = {int(threads)}")
    if memory_limit:
        if not _MEMORY_LIMIT.match(memory_limit.strip()):
            raise ValueError(f"invalid memory limit {memory_limit!r} (examples: 4GB, 512MB)")
        con.execute(f"set memory_limit = '{memory_limit.strip()}'")


class QueryTimeout(RuntimeError):
    pass


class Database:
    """Read-only access to the persisted database. Safe to share between threads (one cursor per call).

    At most `max_concurrent` queries run at once (others wait), and each query is interrupted after
    `timeout` seconds.
    """

    def __init__(
        self,
        db_path: str | Path,
        threads: int | None = None,
        memory_limit: str | None = None,
        max_concurrent: int = 8,
        timeout: float = 30.0,
    ):
        self._con = duckdb.connect(str(db_path), read_only=True)
        apply_resource_limits(self._con, threads, memory_limit)
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._timeout = timeout

    def close(self) -> None:
        self._con.close()

    def warm_up(self) -> None:
        """Touch the main tables so the first user request does not pay for paging them in."""
        self._query("select count(*) from authors where starts_with(NameNorm, 'zz')")
        self._query("select count(*) from papers_authors where AuthorID = 1")
        self._query("select count(*) from papers where NumericID = 1")

    def _query(self, sql: str, params: list | None = None, timeout: float | None = None) -> list[tuple]:
        with self._slots:
            cur = self._con.cursor()
            timer = threading.Timer(timeout or self._timeout, cur.interrupt)
            timer.start()
            try:
                return cur.execute(sql, params or []).fetchall()
            except duckdb.InterruptException as e:
                raise QueryTimeout("query took too long") from e
            finally:
                timer.cancel()
                cur.close()

    def meta(self) -> dict:
        (raw,) = self._query("select value from meta where key = 'fingerprint'")[0]
        fp = json.loads(raw)
        metadata = fp["metadata"]
        snapshot_time = metadata.get("source_file_last_write_time")
        age_days = None
        if snapshot_time:
            # the parser writes the (UTC) modification time of the dump
            snapshot = datetime.fromisoformat(snapshot_time[:26]).replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - snapshot).days
        return {
            "snapshot_time": snapshot_time,
            "age_days": age_days,
            "total_papers": metadata.get("total_papers"),
            "total_authors": metadata.get("total_authors"),
            "total_links": metadata.get("total_links"),
            "integrity": self._integrity(),
        }

    def _integrity(self) -> dict:
        rows = self._query("select value from meta where key = 'integrity'")
        return json.loads(rows[0][0]) if rows else {}

    def search_authors(self, query: str, matcher: str = "prefix", limit: int = 100) -> dict:
        """Find author candidates. Returns {"authors": [...], "truncated": bool}.

        `query` may be a DBLP profile URL, "First Last", or "Last, First".
        """
        if matcher not in MATCHERS:
            raise ValueError(f"unknown matcher {matcher!r}")
        query = query.strip()
        if not query:
            return {"authors": [], "truncated": False}

        if query.startswith("https://dblp.org"):
            where, order, params = "DBLP = ?", "Name", [query]
        else:
            if "," in query:
                last, first = query.split(",", 1)
                query = f"{first.strip()} {last.strip()}"
            if matcher == "prefix":
                where, order, params = "starts_with(upper(Name), upper(?))", "Name", [query]
            elif matcher == "like":
                where, order, params = "Name ilike ?", "Name", [query]
            elif matcher == "accent":
                where, order, params = f"starts_with(NameNorm, {_NORM.format('?')})", "Name", [query]
            else:  # fuzzy
                sim = f"jaro_winkler_similarity(NameNorm, {_NORM.format('?')})"
                where, order, params = f"{sim} >= 0.9", f"{sim} desc, Name", [query, query]

        rows = self._query(
            f"select NumericID, Name, DBLP, ORCID from authors where {where} order by {order} limit ?",
            params + [limit + 1],
        )
        truncated = len(rows) > limit
        rows = rows[:limit]
        stats = self._author_stats([r[0] for r in rows])
        authors = [
            {
                "id": r[0],
                "name": r[1],
                "dblp": r[2],
                "orcid": r[3],
                "paper_count": stats.get(r[0], (0, None))[0],
                "latest_year": stats.get(r[0], (0, None))[1],
            }
            for r in rows
        ]
        return {"authors": authors, "truncated": truncated}

    def _author_stats(self, ids: list[int]) -> dict[int, tuple[int, int | None]]:
        if not ids:
            return {}
        rows = self._query(
            """select pa.AuthorID, count(*), max(p.Year) from papers_authors pa
               join papers p on p.NumericID = pa.PaperID
               where pa.AuthorID in (select unnest(?)) group by pa.AuthorID""",
            [ids],
        )
        return {r[0]: (r[1], r[2]) for r in rows}

    def recent_papers(self, author_id: int, limit: int = 3) -> list[dict]:
        """Latest papers of an author; helps telling homonyms apart."""
        rows = self._query(
            """select p.NumericID, p.DBLP, p.Title, p.Type, p.Year from papers_authors pa
               join papers p on p.NumericID = pa.PaperID
               where pa.AuthorID = ? order by p.Year desc, p.NumericID desc limit ?""",
            [author_id, limit],
        )
        return [_paper(r) for r in rows]

    def coauthors(
        self,
        author_ids: list[int],
        since_year: int | None = None,
        exclude_self: bool = False,
        exclude_types: list[int] | None = None,
    ) -> list[dict]:
        """Co-authors of the given authors on papers since `since_year` (None = any year).

        One entry per co-author with the shared papers; `via` lists which of the given authors
        the co-author shares papers with. Sorted by name.
        """
        if not author_ids:
            return []
        rows = self._query(
            """with hits as (
                   select PaperID, list(AuthorID) as via from papers_authors
                   where AuthorID in (select unnest(?)) group by PaperID
               )
               select a.NumericID, a.Name, a.DBLP, a.ORCID,
                      p.NumericID, p.DBLP, p.Title, p.Type, p.Year, h.via
               from hits h
               join papers p on p.NumericID = h.PaperID
               join papers_authors pa on pa.PaperID = h.PaperID
               join authors a on a.NumericID = pa.AuthorID
               where (? or a.NumericID not in (select unnest(?)))
                 and (? is null or p.Year >= ?)
                 and p.Type not in (select unnest(?))
               order by a.Name, a.NumericID, p.Year desc, p.NumericID""",
            [author_ids, not exclude_self, author_ids, since_year, since_year, list(exclude_types or []) or [-1]],
        )
        result: dict[int, dict] = {}
        for aid, name, dblp, orcid, pid, pdblp, title, ptype, year, via in rows:
            entry = result.get(aid)
            if entry is None:
                entry = result[aid] = {
                    "id": aid, "name": name, "dblp": dblp, "orcid": orcid,
                    "paper_count": 0, "latest_year": year, "via": set(), "papers": [],
                }
            entry["paper_count"] += 1
            entry["latest_year"] = max(entry["latest_year"], year)
            entry["via"].update(v for v in via if v != aid)
            entry["papers"].append(_paper((pid, pdblp, title, ptype, year)))
        for entry in result.values():
            entry["via"] = sorted(entry["via"])
        return list(result.values())

    def check_reviewers(
        self,
        author_ids: list[int],
        reviewers: list[str],
        matcher: str = "prefix",
        since_year: int | None = None,
        exclude_types: list[int] | None = None,
        candidate_limit: int = 20,
    ) -> list[dict]:
        """Check each reviewer (name or DBLP URL) against the conflicts of the given authors.

        status is "conflict", "no_conflict", "not_found" or "ambiguous" (several matching DBLP
        profiles; every candidate carries its own conflict entry, or None).
        A reviewer who is one of the authors is reported as a conflict.
        """
        conflicts = {e["id"]: e for e in self.coauthors(author_ids, since_year, False, exclude_types)}
        out = []
        for query in reviewers:
            found = self.search_authors(query, matcher, limit=candidate_limit)
            candidates = [{**a, "conflict": conflicts.get(a["id"])} for a in found["authors"]]
            if not candidates:
                status = "not_found"
            elif len(candidates) > 1 or found["truncated"]:
                status = "ambiguous"
            else:
                status = "conflict" if candidates[0]["conflict"] else "no_conflict"
            out.append({"query": query, "status": status, "candidates": candidates, "truncated": found["truncated"]})
        return out


def _paper(row: tuple) -> dict:
    pid, dblp, title, ptype, year = row
    return {"id": pid, "dblp": dblp, "title": title, "type": PAPER_TYPES.get(ptype, "Invalid"), "type_id": ptype, "year": year}
