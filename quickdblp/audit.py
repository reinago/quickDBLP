"""Ground-truth audit of a snapshot database against the original RDF dump.

The RDF dump is read once with a deliberately simple parser of its own (it shares no code or assumptions
with `ponder_dblp`) and reduced to one line per publication: record key, entity type, years, the list of
authors and editors (`dblp:authoredBy` / `dblp:editedBy`) and a few flags. That "truth" file is then compared with the snapshot database:

* every publication that is missing from the database must be explained by one of the parser's documented
  rules (no authors, unhandled entity type, author/signature mismatch, no title, no year); anything else is
  an *unexplained loss* and fails the audit,
* every (paper, author) link, every author, and the year and type of every paper must match exactly.

The dump is huge (tens of GB uncompressed), so it is only ever streamed, never loaded.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Iterator

import duckdb

from .db import apply_resource_limits

# entity name in the dump -> `Type` column of papers.csv
ENTITY_TYPES = {"Inproceedings": 1, "Incollection": 2, "Article": 3, "Book": 4, "Part": 5, "Informal": 6, "Data": 7,
                "Reference": 8, "Editorship": 9, "Withdrawn": 10}

TRUTH_COLUMNS = "key\tentity\tis_pub\tyear_pub\tyear_event\tn_authors\tn_signatures\thas_title\tauthors\n"

_START = re.compile(rb'<dblp:(\w+) rdf:about="([^"]*)">')
_YEAR = re.compile(rb">\s*(\d{1,4})\s*</dblp:yearOf")
_RESOURCE = re.compile(rb'rdf:resource="([^"]*)"')
_TYPE_RESOURCE = re.compile(rb'rdf:resource="([^"]*)"')


def open_lines(path: str | Path) -> tuple[Iterator[bytes], "subprocess.Popen | None"]:
    """Iterate over the lines of a (gzipped) text file. Uses the system `gzip` when available (much faster)."""
    path = Path(path)
    if path.suffix == ".gz":
        gzip_exe = shutil.which("gzip")
        if gzip_exe:
            proc = subprocess.Popen([gzip_exe, "-dc", str(path)], stdout=subprocess.PIPE, bufsize=1 << 24)
            return iter(proc.stdout), proc
        import gzip
        import io

        return iter(io.BufferedReader(gzip.open(path, "rb"), 1 << 24)), None
    return iter(open(path, "rb")), None


def extract_truth(rdf_path: str | Path, out_path: str | Path, max_lines: int | None = None, progress=None) -> dict:
    """Stream the RDF dump into a tab-separated truth file (one line per publication). Returns statistics."""
    lines, proc = open_lines(rdf_path)
    stats: Counter = Counter()
    entities: Counter = Counter()
    publications: Counter = Counter()
    type_values: Counter = Counter()
    started = time.time()

    name = key = None
    end_tag = b""
    is_pub = has_title = False
    year_pub = year_event = ""
    n_signatures = 0
    authors: list[str] = []
    type_seen = False
    n = 0

    with open(out_path, "w", encoding="utf-8", newline="\n") as out:
        out.write(TRUTH_COLUMNS)
        for n, raw in enumerate(lines, 1):
            if max_lines and n > max_lines:
                break
            if progress and n % 50_000_000 == 0:
                progress(n, time.time() - started)
            if name is None:
                if raw.startswith(b"<dblp:") and b' rdf:about="' in raw:
                    m = _START.match(raw)
                    if m:
                        name, key = m.group(1), m.group(2)
                        end_tag = b"</dblp:" + name + b">"
                        is_pub = has_title = type_seen = False
                        year_pub = year_event = ""
                        n_signatures = 0
                        authors = []
                continue
            if raw.startswith(b"</dblp:"):
                if raw.startswith(end_tag):
                    entity = name.decode()
                    entities[entity] += 1
                    if is_pub:
                        publications[entity] += 1
                        out.write(
                            f"{key.decode()}\t{entity}\t1\t{year_pub}\t{year_event}\t{len(authors)}\t{n_signatures}\t"
                            f"{int(has_title)}\t{' '.join(authors)}\n"
                        )
                    name = None
                continue
            if raw.startswith(b"<dblp:authoredBy ") or raw.startswith(b"<dblp:editedBy "):  # editors count as people
                m = _RESOURCE.search(raw)
                if m:
                    authors.append(m.group(1).decode())
            elif raw.startswith(b"<dblp:signatureCreator "):
                n_signatures += 1
            elif raw.startswith(b"<dblp:title>"):
                has_title = True
            elif raw.startswith(b"<dblp:yearOfPublication"):
                m = _YEAR.search(raw)
                if m:
                    year_pub = m.group(1).decode()
            elif raw.startswith(b"<dblp:yearOfEvent"):
                m = _YEAR.search(raw)
                if m:
                    year_event = m.group(1).decode()
            elif not type_seen and raw.startswith(b"<rdf:type "):
                type_seen = True  # the record's own type is the first rdf:type line
                m = _TYPE_RESOURCE.search(raw)
                value = m.group(1).decode() if m else ""
                type_values[value] += 1
                is_pub = value.endswith("#Publication")

    if proc is not None:
        proc.terminate() if max_lines else proc.wait()
    stats.update(
        lines=n,
        seconds=round(time.time() - started, 1),
        records=sum(entities.values()),
        publications=sum(publications.values()),
        unterminated_record_at_eof=int(name is not None),
    )
    result = dict(stats)
    result["entities"] = dict(entities)
    result["publications_by_entity"] = dict(publications)
    result["first_rdf_type_values"] = dict(type_values)
    return result


# ---------------------------------------------------------------------------------------------------------------
# comparison

_TRUTH_TYPES = (
    "{'key': 'VARCHAR', 'entity': 'VARCHAR', 'is_pub': 'INTEGER', 'year_pub': 'INTEGER', 'year_event': 'INTEGER',"
    " 'n_authors': 'INTEGER', 'n_signatures': 'INTEGER', 'has_title': 'INTEGER', 'authors': 'VARCHAR'}"
)

_TYPE_CASE = "case entity " + " ".join(f"when '{e}' then {t}" for e, t in ENTITY_TYPES.items()) + " end"


def compare_truth(
    truth_path: str | Path,
    db_path: str | Path,
    work_dir: str | Path,
    memory_limit: str | None = None,
    examples: int = 10,
) -> dict:
    """Compare the truth file with the snapshot database. Returns a report (see `format_report`)."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    scratch = work_dir / "audit_scratch.duckdb"
    scratch.unlink(missing_ok=True)
    con = duckdb.connect(str(scratch))
    try:
        con.execute(f"set temp_directory = '{(work_dir / 'duckdb_tmp').as_posix()}'")
        apply_resource_limits(con, memory_limit=memory_limit)
        db_literal = Path(db_path).resolve().as_posix().replace("'", "''")
        con.execute(f"attach '{db_literal}' as snap (read_only)")
        q = lambda sql, *p: con.execute(sql, list(p)).fetchall()  # noqa: E731
        one = lambda sql, *p: q(sql, *p)[0][0]  # noqa: E731

        con.execute(
            f"create table pubs as select *, coalesce(year_pub, year_event) as year, {_TYPE_CASE} as type_id "
            f"from read_csv(?, delim='\\t', header=true, quote='', escape='', types={_TRUTH_TYPES}) where is_pub = 1",
            [str(truth_path)],
        )
        con.execute(
            """create table status as
               select p.*, s.NumericID as db_id, s.NumericID is not null as in_db, s.Year as db_year, s.Type as db_type,
                      case when p.n_authors = 0 then 'no_authors'
                           when p.type_id is null then 'unhandled_entity'
                           when p.n_authors <> p.n_signatures then 'author_signature_mismatch'
                           when p.has_title = 0 then 'no_title'
                           when p.year is null then 'no_year'
                           else 'none' end as reason
               from pubs p left join snap.papers s on s.DBLP = p.key"""
        )
        report: dict = {"checks": [], "info": {}}

        def check(name: str, description: str, count: int, sample_sql: str | None = None):
            report["checks"].append({
                "name": name, "description": description, "count": count, "ok": count == 0,
                "examples": [list(r) for r in q(sample_sql + f" limit {examples}")] if sample_sql and count else [],
            })

        # what the dump contains and what the database contains
        report["info"]["publications_in_dump"] = one("select count(*) from pubs")
        report["info"]["papers_in_db"] = one("select count(*) from snap.papers")
        report["info"]["authors_in_db"] = one("select count(*) from snap.authors")
        report["info"]["links_in_db"] = one("select count(*) from snap.papers_authors")
        report["info"]["dump_vs_db"] = {
            f"{'in_db' if in_db else 'not_in_db'}/{reason}": n
            for in_db, reason, n in q("select in_db, reason, count(*) from status group by 1, 2 order by 1 desc, 2")
        }

        report["info"]["unexplained_by_entity"] = {
            entity: n for entity, n in q(
                "select entity, count(*) from status where not in_db and reason = 'none' group by 1 order by 2 desc")
        }

        # 1. nothing may be lost without a documented reason
        check("unexplained_missing_papers",
              "publications with authors, title, year and a handled type that are missing from the database",
              one("select count(*) from status where not in_db and reason = 'none'"),
              "select key, entity, year, n_authors from status where not in_db and reason = 'none'")
        check("papers_not_in_dump",
              "papers in the database that are not publications of the dump",
              one("select count(*) from snap.papers p where not exists (select 1 from pubs where pubs.key = p.DBLP)"),
              "select DBLP, Year from snap.papers p where not exists (select 1 from pubs where pubs.key = p.DBLP)")
        check("kept_despite_rule",
              "papers in the database that the documented rules say should have been dropped",
              one("select count(*) from status where in_db and reason <> 'none'"),
              "select key, entity, reason from status where in_db and reason <> 'none'")
        check("year_mismatch", "papers whose year differs from the dump",
              one("select count(*) from status where in_db and db_year is distinct from year"),
              "select key, year, db_year from status where in_db and db_year is distinct from year")
        check("type_mismatch", "papers whose type differs from the dump",
              one("select count(*) from status where in_db and db_type is distinct from type_id"),
              "select key, entity, db_type from status where in_db and db_type is distinct from type_id")

        # 2. every (paper, author) link, compared on the database's integer IDs (light on memory)
        con.execute(
            """create table truth_links as
               select t.db_id as paper_id, a.NumericID as author_id, t.author as pid
               from (select db_id, unnest(string_split(authors, ' ')) as author
                     from status where in_db and n_authors > 0) t
               left join snap.authors a on a.DBLP = t.author"""
        )
        missing = ("from truth_links t where not exists "
                   "(select 1 from snap.papers_authors l where l.PaperID = t.paper_id and l.AuthorID = t.author_id)")
        extra = ("from snap.papers_authors l where not exists "
                 "(select 1 from truth_links t where t.paper_id = l.PaperID and t.author_id = l.AuthorID)")
        check("missing_links", "(paper, author) pairs of the dump that are missing in the database",
              one(f"select count(*) from (select distinct paper_id, pid {missing})"),
              f"select distinct p.DBLP, t.pid from (select t.paper_id, t.pid {missing}) t "
              "join snap.papers p on p.NumericID = t.paper_id")
        check("extra_links", "(paper, author) pairs in the database that are not in the dump",
              one(f"select count(*) {extra}"),
              f"select p.DBLP, a.DBLP from (select l.PaperID, l.AuthorID {extra}) l "
              "left join snap.papers p on p.NumericID = l.PaperID left join snap.authors a on a.NumericID = l.AuthorID")
        dangling = ("from snap.papers_authors l where not exists (select 1 from snap.papers p where p.NumericID = l.PaperID) "
                    "or not exists (select 1 from snap.authors a where a.NumericID = l.AuthorID)")
        check("dangling_links", "links in the database that point to a paper or author that does not exist",
              one(f"select count(*) {dangling}"), f"select l.PaperID, l.AuthorID {dangling}")

        # 3. authors
        check("missing_authors", "authors of the dump (on kept papers) that are missing in the database",
              one("select count(distinct pid) from truth_links where author_id is null"),
              "select distinct pid from truth_links where author_id is null")
        check("extra_authors", "authors in the database that are on no kept paper of the dump",
              one("select count(*) from snap.authors a where not exists (select 1 from truth_links t where t.author_id = a.NumericID)"),
              "select a.DBLP, a.Name from snap.authors a "
              "where not exists (select 1 from truth_links t where t.author_id = a.NumericID)")

        # not bugs, but design decisions of the parser that drop publications WITH authors: their co-authorships
        # are not in the database, so conflicts through them cannot be found
        report["warnings"] = []
        for reason, entity, papers, slots in q(
            """select reason, entity, count(*), sum(n_authors) from status
               where not in_db and n_authors > 0 and reason <> 'none'
               group by 1, 2 order by 3 desc"""
        ):
            report["warnings"].append({
                "reason": reason, "entity": entity, "papers": papers, "author_slots": slots,
                "examples": [r[0] for r in q(
                    "select key from status where not in_db and n_authors > 0 and reason = ? and entity = ? limit 3",
                    reason, entity)],
            })

        report["ok"] = all(c["ok"] for c in report["checks"])
        return report
    finally:
        con.close()
        scratch.unlink(missing_ok=True)


def format_report(report: dict, stats: dict | None = None) -> str:
    lines = []
    if stats:
        lines.append(f"Dump: {stats['lines']:,} lines in {stats['seconds']}s, {stats['records']:,} records, "
                     f"{stats['publications']:,} publications")
        lines.append("  publications by entity: " + ", ".join(f"{k} {v:,}" for k, v in sorted(stats["publications_by_entity"].items())))
        others = {k: v for k, v in stats["entities"].items() if k not in stats["publications_by_entity"]}
        lines.append("  other records: " + (", ".join(f"{k} {v:,}" for k, v in sorted(others.items())) or "none"))
        if stats.get("unterminated_record_at_eof"):
            lines.append("  WARNING: the dump ends inside a record (truncated download?)")
    info = report["info"]
    lines.append(f"Database: {info['papers_in_db']:,} papers, {info['authors_in_db']:,} authors, {info['links_in_db']:,} links")
    lines.append("Publications of the dump vs. database (documented parser rules explain every 'not_in_db' except /none):")
    for k, v in info["dump_vs_db"].items():
        lines.append(f"  {k:<42}{v:>12,}")
    unexplained = info.get("unexplained_by_entity")
    if unexplained:
        lines.append("Missing without a documented reason, by record type: " + ", ".join(f"{k} {v:,}" for k, v in unexplained.items()))
    lines.append("")
    for c in report["checks"]:
        lines.append(f"[{'PASS' if c['ok'] else 'FAIL'}] {c['description']}: {c['count']:,}")
        for ex in c["examples"]:
            lines.append("       e.g. " + " | ".join(str(x) for x in ex))
    lines.append("")
    warnings = report.get("warnings", [])
    if warnings:
        lines.append("WARNINGS - publications WITH authors that the parser leaves out by design (their co-authorships are not")
        lines.append("in the database, so conflicts that exist only through them cannot be found):")
        total = sum(w["papers"] for w in warnings)
        for w in warnings:
            lines.append(f"  {w['reason']:<28}{w['entity']:<14}{w['papers']:>10,} publications, {w['author_slots']:>11,} author slots"
                         f"   e.g. {', '.join(w['examples'][:2])}")
        lines.append(f"  total: {total:,} publications")
        lines.append("")
    lines.append("RESULT: " + ("no discrepancies between database and dump" if report["ok"] else
                               "FAILED - the snapshot does not match the dump")
                 + (f"; {sum(w['papers'] for w in warnings):,} publications with authors are excluded by the parser's rules" if warnings else ""))
    return "\n".join(lines)


def run_audit(rdf_path, db_path, work_dir, reuse_truth=False, memory_limit=None, progress=None) -> tuple[dict, dict]:
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    truth, stats_file = work_dir / "truth.tsv", work_dir / "truth_stats.json"
    if reuse_truth and truth.exists() and stats_file.exists():
        stats = json.loads(stats_file.read_text())
    else:
        stats = extract_truth(rdf_path, truth, progress=progress)
        stats_file.write_text(json.dumps(stats, indent=1))
    return compare_truth(truth, db_path, work_dir, memory_limit=memory_limit), stats
