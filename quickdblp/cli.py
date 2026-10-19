import argparse
import json
import os
import sys
from pathlib import Path

from .db import SnapshotError, build_snapshot

DEFAULT_SNAPSHOT_DIR = "snapshot"
DEFAULT_DB = "snapshot/dblp.duckdb"
DEFAULT_DATA_DIR = "data"


def adopt_existing_database(data_dir) -> None:
    """First start with an empty data directory: use snapshot/dblp.duckdb if there is one, instead of importing the
    dump again. The data counts as just checked, so starting the server never triggers an update by itself."""
    import logging

    from .refresh import RefreshError, RefreshManager

    manager = RefreshManager(data_dir, on_activate=lambda path: None)
    if manager.current() is None and Path(DEFAULT_DB).is_file():
        try:
            target = manager.adopt(DEFAULT_DB)
            logging.getLogger("quickdblp").info("adopted %s as the starting point of %s (%s)", DEFAULT_DB, data_dir, target)
        except (RefreshError, SnapshotError, OSError) as e:
            logging.getLogger("quickdblp").warning("could not adopt %s: %s", DEFAULT_DB, e)


def app_from_args(args):
    """The web app for the `serve` options: a self-updating data directory, or (with --db / --snapshot-dir) a fixed database."""
    from .app import create_app

    common = dict(threads=args.threads, memory_limit=args.memory_limit, max_concurrent=args.max_concurrent, timeout=args.timeout)
    if args.db is not None or args.snapshot_dir is not None:
        return create_app(args.db or DEFAULT_DB, snapshot_dir=args.snapshot_dir or DEFAULT_SNAPSHOT_DIR, **common)
    adopt_existing_database(args.data_dir)
    return create_app(data_dir=args.data_dir, refresh_options={
        "interval_days": args.interval_days, "parser_command": [args.parser] if args.parser else None}, **common)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quickdblp")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-snapshot", help="build the DuckDB database from the snapshot CSVs")
    build.add_argument("--snapshot-dir", default=DEFAULT_SNAPSHOT_DIR)
    build.add_argument("--db", default=DEFAULT_DB)
    build.add_argument("--force", action="store_true", help="rebuild even if the database is up to date")

    serve = sub.add_parser("serve", help="start the web server (builds the database first if it is missing/outdated)")
    serve.add_argument("--data-dir", default=os.environ.get("QUICKDBLP_DATA_DIR", DEFAULT_DATA_DIR),
                       help="the server manages its data in this directory (default: %(default)s, environment: QUICKDBLP_DATA_DIR): "
                            "it imports the DBLP dump itself when there is no data yet (a snapshot/dblp.duckdb is adopted instead) "
                            "and updates the data in the background (page button, every --interval-days)")
    serve.add_argument("--interval-days", type=float, default=float(os.environ.get("QUICKDBLP_INTERVAL_DAYS", 7)),
                       help="check dblp.org for a newer dump this often and update if there is one (default 7; 0 = only when "
                            "the button is pressed)")
    serve.add_argument("--parser", default=os.environ.get("QUICKDBLP_PARSER"),
                       help="path of the ponder_dblp executable (default: found automatically)")
    serve.add_argument("--snapshot-dir", default=None,
                       help="fixed-database mode (no updates): build --db from the CSVs in this directory when it is outdated")
    serve.add_argument("--db", default=None,
                       help="fixed-database mode (no updates): serve exactly this database (default snapshot/dblp.duckdb)")
    serve.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to share with others")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--threads", type=int, help="DuckDB threads (default: all cores)")
    serve.add_argument("--memory-limit", help="DuckDB memory limit, e.g. 4GB")
    serve.add_argument("--max-concurrent", type=int, default=8, help="max simultaneously running queries")
    serve.add_argument("--timeout", type=float, default=30.0, help="per-query timeout in seconds")

    refresh = sub.add_parser("refresh", help="update the data in a data directory now (like the page's update button)")
    refresh.add_argument("--data-dir", default=os.environ.get("QUICKDBLP_DATA_DIR"), required=not os.environ.get("QUICKDBLP_DATA_DIR"))
    refresh.add_argument("--parser", default=os.environ.get("QUICKDBLP_PARSER"))
    refresh.add_argument("--force", action="store_true", help="refresh even if dblp.org has nothing newer")

    adopt = sub.add_parser("adopt", help="use an existing database as the starting point of a data directory (no re-import)")
    adopt.add_argument("--data-dir", default=os.environ.get("QUICKDBLP_DATA_DIR"), required=not os.environ.get("QUICKDBLP_DATA_DIR"))
    adopt.add_argument("--db", default=DEFAULT_DB, help="the database to adopt (default: %(default)s)")
    adopt.add_argument("--move", action="store_true", help="move the file instead of copying it (saves disk space)")

    audit = sub.add_parser(
        "audit",
        help="compare the database with the original RDF dump (ground truth; streams the whole dump, takes a while)",
    )
    audit.add_argument("--rdf", default="dblp.rdf.gz", help="the original dump (default: dblp.rdf.gz)")
    audit.add_argument("--db", default=DEFAULT_DB)
    audit.add_argument("--work-dir", default="audit", help="where the (large) intermediate files go")
    audit.add_argument("--reuse-truth", action="store_true", help="skip reading the dump if work-dir/truth.tsv exists")
    audit.add_argument("--memory-limit", help="DuckDB memory limit for the comparison, e.g. 8GB")
    audit.add_argument("--extract-only", action="store_true", help="only read the dump into work-dir/truth.tsv")
    audit.add_argument("--max-lines", type=int, help="with --extract-only: stop after this many lines (benchmarking)")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "audit":
        from .audit import extract_truth, format_report, run_audit

        def progress(lines, seconds):
            print(f"  {lines / 1e6:,.0f}M lines, {seconds:,.0f}s ({lines / seconds / 1e6:.2f}M lines/s)", flush=True)

        if args.extract_only:
            work = Path(args.work_dir)
            work.mkdir(parents=True, exist_ok=True)
            stats = extract_truth(args.rdf, work / "truth.tsv", max_lines=args.max_lines, progress=progress)
            print(json.dumps(stats, indent=1))
            return 0
        report, stats = run_audit(args.rdf, args.db, args.work_dir, args.reuse_truth, args.memory_limit, progress)
        print(format_report(report, stats))
        return 0 if report["ok"] else 1
    if args.command == "adopt":
        from .refresh import RefreshError, RefreshManager

        try:
            target = RefreshManager(args.data_dir, on_activate=lambda path: None).adopt(args.db, move=args.move)
        except (RefreshError, SnapshotError, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"{args.db} is now the active database of {args.data_dir} ({target})")
        return 0
    if args.command == "refresh":
        import logging
        import time

        from .refresh import RefreshManager

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        manager = RefreshManager(args.data_dir, on_activate=lambda path: None,
                                 parser_command=[args.parser] if args.parser else None)
        answer = manager.request("manual", force=args.force)
        if not answer["started"]:
            print(f"nothing started: {answer['reason']} {answer.get('message', '')}".strip())
            return 0 if answer["reason"] in ("up_to_date", "already_running") else 1
        while manager.running():
            time.sleep(1)
        job = manager.status()["job"]
        print(f"{job['state']}" + (f": {job['error']}" if job["error"] else ""))
        return 0 if job["state"] in ("succeeded", "up_to_date") else 1
    if args.command == "serve":
        import logging

        import uvicorn

        logging.basicConfig(level=logging.INFO)
        app = app_from_args(args)
        # One process only: every worker process would hold its own copy of the data in memory.
        try:
            uvicorn.run(app, host=args.host, port=args.port, workers=1)
        except SnapshotError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    elif args.command == "build-snapshot":
        try:
            built = build_snapshot(args.snapshot_dir, args.db, force=args.force)
        except SnapshotError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"built {args.db}" if built else f"{args.db} is up to date (use --force to rebuild)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
