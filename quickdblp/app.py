"""FastAPI app: JSON API plus the static frontend."""

from __future__ import annotations

import logging
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


def create_app(
    db_path: str | Path,
    snapshot_dir: str | Path | None = None,
    threads: int | None = None,
    memory_limit: str | None = None,
    max_concurrent: int = 8,
    timeout: float = 30.0,
    warm_up: bool = True,
) -> FastAPI:
    """If `snapshot_dir` is given, the database is (re)built on startup when it is missing or outdated."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if snapshot_dir is not None and not is_up_to_date(snapshot_dir, db_path):
            log.info("database missing or outdated, building it from %s ...", snapshot_dir)
            build_snapshot(snapshot_dir, db_path)
        app.state.db = Database(db_path, threads=threads, memory_limit=memory_limit,
                                max_concurrent=max_concurrent, timeout=timeout)
        if warm_up:
            app.state.db.warm_up()
        yield
        app.state.db.close()

    app = FastAPI(title="quickDBLP", lifespan=lifespan)

    @app.exception_handler(QueryTimeout)
    async def _timeout(request: Request, exc: QueryTimeout):
        return JSONResponse({"detail": "The query took too long; try a more specific search."}, status_code=504)

    # Plain `def` routes run in the threadpool, so slow queries never block the event loop.

    @app.get("/api/meta")
    def meta(request: Request):
        return {
            **request.app.state.db.meta(),
            "paper_types": PAPER_TYPES,
            "paper_type_help": PAPER_TYPE_HELP,
            "default_excluded_types": DEFAULT_EXCLUDED_TYPES,
        }

    @app.post("/api/authors/search")
    def search(req: SearchRequest, request: Request):
        db: Database = request.app.state.db
        results = []
        for q in _clean_queries(req.queries):
            found = db.search_authors(q, req.matcher, req.limit)
            results.append({"query": q, **found})
        return {"results": results}

    @app.get("/api/authors/{author_id}/recent")
    def recent(author_id: int, request: Request, limit: int = 3):
        return {"papers": request.app.state.db.recent_papers(author_id, max(1, min(limit, 20)))}

    @app.post("/api/conflicts")
    def conflicts(req: ConflictRequest, request: Request):
        since = _since_year(req.years_back)
        entries = request.app.state.db.coauthors(req.author_ids, since, req.exclude_self, req.exclude_types)
        return {
            "since_year": since,
            "coauthors": entries[:MAX_COAUTHORS],
            "truncated": len(entries) > MAX_COAUTHORS,
        }

    @app.post("/api/reviewers/check")
    def reviewers(req: ReviewerRequest, request: Request):
        since = _since_year(req.years_back)
        results = request.app.state.db.check_reviewers(
            req.author_ids, _clean_queries(req.reviewers), req.matcher, since, req.exclude_types
        )
        return {"since_year": since, "reviewers": results}

    @app.post("/api/guess")
    def guess(req: GuessRequest):
        try:
            return {"authors": guess_authors(req.source, req.html)}
        except GuessError as e:
            raise HTTPException(422, str(e)) from e

    if STATIC_DIR.is_dir():
        app.mount("/", RevalidatingStaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
