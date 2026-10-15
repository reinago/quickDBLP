import argparse
import json
import sys

from .db import SnapshotError, build_snapshot

DEFAULT_SNAPSHOT_DIR = "snapshot"
DEFAULT_DB = "snapshot/dblp.duckdb"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quickdblp")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-snapshot", help="build the DuckDB database from the snapshot CSVs")
    build.add_argument("--snapshot-dir", default=DEFAULT_SNAPSHOT_DIR)
    build.add_argument("--db", default=DEFAULT_DB)
    build.add_argument("--force", action="store_true", help="rebuild even if the database is up to date")

    serve = sub.add_parser("serve", help="start the web server (builds the database first if it is missing/outdated)")
    serve.add_argument("--snapshot-dir", default=DEFAULT_SNAPSHOT_DIR)
    serve.add_argument("--db", default=DEFAULT_DB)
    serve.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to share with others")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--threads", type=int, help="DuckDB threads (default: all cores)")
    serve.add_argument("--memory-limit", help="DuckDB memory limit, e.g. 4GB")
    serve.add_argument("--max-concurrent", type=int, default=8, help="max simultaneously running queries")
    serve.add_argument("--timeout", type=float, default=30.0, help="per-query timeout in seconds")

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

    args = parser.parse_args(argv)
    if args.command == "audit":
        from pathlib import Path

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
    if args.command == "serve":
        import logging

        import uvicorn

        from .app import create_app

        logging.basicConfig(level=logging.INFO)
        app = create_app(args.db, snapshot_dir=args.snapshot_dir, threads=args.threads, memory_limit=args.memory_limit,
                         max_concurrent=args.max_concurrent, timeout=args.timeout)
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
