import argparse
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

    args = parser.parse_args(argv)
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
