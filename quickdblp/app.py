"""FastAPI app: JSON API plus the static frontend."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import DEFAULT_EXCLUDED_TYPES, MATCHERS, PAPER_TYPE_HELP, PAPER_TYPES, Database, QueryTimeout, build_snapshot, is_up_to_date
from .guess import GuessError, guess_authors
from .refresh import RefreshManager

log = logging.getLogger("quickdblp")

STATIC_DIR = Path(__file__).parent / "static"

MAX_QUERIES = 50          # author names per search request
MAX_AUTHOR_IDS = 50       # authors whose conflicts are computed
MAX_REVIEWERS = 200
MAX_COAUTHORS = 5000      # rows returned; the rest is cut off and flagged
MAX_HTML_CHARS = 2_000_000

Matcher = Literal["prefix", "like", "accent", "fuzzy"]
assert set(MATCHERS) == set(Matcher.__args__)


class RevalidatingStaticFiles(StaticFiles):
    """Static files that browsers re-check on every load (cheap 304s), so UI updates never mix old and new files."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


class SearchRequest(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=MAX_QUERIES)
    matcher: Matcher = "prefix"
    limit: int = Field(100, ge=1, le=500)


class ConflictRequest(BaseModel):
    author_ids: list[int] = Field(min_length=1, max_length=MAX_AUTHOR_IDS)
    years_back: int | None = Field(5, ge=0, le=200, description="null = any year")
    exclude_self: bool = False
    exclude_types: list[int] = Field(default_factory=list, max_length=20)


class ReviewerRequest(ConflictRequest):
    reviewers: list[str] = Field(min_length=1, max_length=MAX_REVIEWERS)
    matcher: Matcher = "prefix"


class RefreshRequest(BaseModel):
    force: bool = Field(False, description="refresh even if dblp.org has nothing newer")


class GuessRequest(BaseModel):
    source: Literal["pcs", "easychair"]
    html: str = Field(max_length=MAX_HTML_CHARS)


def _since_year(years_back: int | None) -> int | None:
    return None if years_back is None else datetime.now().year - years_back


def _clean_queries(queries: list[str]) -> list[str]:
    cleaned = [q.strip() for q in queries if q.strip()]
    if any(len(q) > 300 for q in cleaned):
        raise HTTPException(422, "query too long")
    return cleaned


def get_db(request: Request) -> Database:
    """The active database; while there is none (first import still running) the data endpoints say so."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(503, "There is no data yet: the first import from dblp.org is still running "
                                 "(see the progress at the top of the page).")
    return db


def create_app(
    db_path: str | Path | None = None,
    snapshot_dir: str | Path | None = None,
    threads: int | None = None,
    memory_limit: str | None = None,
    max_concurrent: int = 8,
    timeout: float = 30.0,
    warm_up: bool = True,
    data_dir: str | Path | None = None,
    refresh_options: dict | None = None,
) -> FastAPI:
    """Two ways to run:

    * `data_dir`: the server manages its data itself. It starts with the active database of that directory (or
      without data: the first import then starts at once) and keeps it up to date in the background (see
      refresh.py). `refresh_options` are passed to the RefreshManager.
    * `db_path` (and optionally `snapshot_dir`, from which the database is built when it is missing or
      outdated): a fixed database, no updates from the page.
    """
    if data_dir is None and db_path is None:
        raise ValueError("either data_dir or db_path is required")

    def open_database(path: Path) -> Database:
        db = Database(path, threads=threads, memory_limit=memory_limit, max_concurrent=max_concurrent, timeout=timeout)
        if warm_up:
            db.warm_up()
        return db

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = None
        app.state.refresh = None

        def activate(path: Path) -> None:
            """Switch to a new database. Requests that already run keep using the old one, which is closed after the
            longest possible query (the timeout) has passed."""
            new, old = open_database(path), app.state.db
            app.state.db = new
            if old is not None:
                closer = threading.Timer(timeout + 30, old.close)
                closer.daemon = True
                closer.start()

        if data_dir is not None:
            manager = RefreshManager(data_dir, on_activate=activate, **(refresh_options or {}))
            current = manager.current_database_path()
            if current is not None:
                app.state.db = open_database(current)
            app.state.refresh = manager
            manager.start()
        else:
            if snapshot_dir is not None and not is_up_to_date(snapshot_dir, db_path):
                log.info("database missing or outdated, building it from %s ...", snapshot_dir)
                build_snapshot(snapshot_dir, db_path)
            app.state.db = open_database(Path(db_path))
        yield
        if app.state.refresh is not None:
            app.state.refresh.stop()
        if app.state.db is not None:
            app.state.db.close()

    app = FastAPI(title="quickDBLP", lifespan=lifespan)

    @app.exception_handler(QueryTimeout)
    async def _timeout(request: Request, exc: QueryTimeout):
        return JSONResponse({"detail": "The query took too long; try a more specific search."}, status_code=504)

    # Plain `def` routes run in the threadpool, so slow queries never block the event loop.

    @app.get("/api/meta")
    def meta(request: Request):
        db = request.app.state.db
        if db is None:
            return {"ready": False}
        return {
            "ready": True,
            **db.meta(),
            "paper_types": PAPER_TYPES,
            "paper_type_help": PAPER_TYPE_HELP,
            "default_excluded_types": DEFAULT_EXCLUDED_TYPES,
        }

    @app.post("/api/authors/search")
    def search(req: SearchRequest, request: Request):
        db = get_db(request)
        results = []
        for q in _clean_queries(req.queries):
            found = db.search_authors(q, req.matcher, req.limit)
            results.append({"query": q, **found})
        return {"results": results}

    @app.get("/api/authors/{author_id}/recent")
    def recent(author_id: int, request: Request, limit: int = 3):
        return {"papers": get_db(request).recent_papers(author_id, max(1, min(limit, 20)))}

    @app.post("/api/conflicts")
    def conflicts(req: ConflictRequest, request: Request):
        since = _since_year(req.years_back)
        entries = get_db(request).coauthors(req.author_ids, since, req.exclude_self, req.exclude_types)
        return {
            "since_year": since,
            "coauthors": entries[:MAX_COAUTHORS],
            "truncated": len(entries) > MAX_COAUTHORS,
        }

    @app.post("/api/reviewers/check")
    def reviewers(req: ReviewerRequest, request: Request):
        since = _since_year(req.years_back)
        results = get_db(request).check_reviewers(
            req.author_ids, _clean_queries(req.reviewers), req.matcher, since, req.exclude_types
        )
        return {"since_year": since, "reviewers": results}

    @app.get("/api/refresh")
    def refresh_status(request: Request):
        manager: RefreshManager | None = request.app.state.refresh
        if manager is None:
            return {"enabled": False, "ready": request.app.state.db is not None}
        return manager.status()

    @app.post("/api/refresh")
    def refresh_start(req: RefreshRequest, request: Request):
        """Start an update. If one is already running the caller just joins it; if dblp.org has nothing newer, nothing
        happens ("up_to_date")."""
        manager: RefreshManager | None = request.app.state.refresh
        if manager is None:
            raise HTTPException(409, "Updates are not enabled (the server was not started with a data directory).")
        return manager.request("manual", force=req.force)

    @app.post("/api/guess")
    def guess(req: GuessRequest):
        try:
            return {"authors": guess_authors(req.source, req.html)}
        except GuessError as e:
            raise HTTPException(422, str(e)) from e

    if STATIC_DIR.is_dir():
        app.mount("/", RevalidatingStaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
