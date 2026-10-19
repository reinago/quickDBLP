# quickDBLP web rewrite – TODO

Goal: replace `query_dblp.ipynb` + `query_dblp.py` (ipywidgets/itables) with a Python backend and a web page for
searching co-authors (conflict-of-interest checks) with readable, filterable output.

## Current functionality to preserve (from `query_dblp.py`)
- Load `snapshot/*.csv` (authors, papers, paper–author links) into DuckDB; warn if snapshot is older than 10 days (`dblp_metadata.json`).
- Search authors by: name prefix (`startswith`), `ILIKE` pattern, or exact DBLP profile URL. Multiple queries separated by `;`. "Last, First" is flipped to "First Last".
- For all matched authors, find co-papers (papers where a matched author is listed) with `Year >= now - cutoff` (cutoff 1–10 years, or ignored).
- Filters: exclude the searched authors themselves, prefilter by DBLP author URLs, ignore paper types (Inproceedings, Incollection, Article, Book, Part, Informal, Data).
- Two views: *summary* (one row per co-author) and *explain* (one row per paper × co-author with paper title/link/type/year).
- Guess author list by pasting HTML source of PCS (`.authorList li span`) or EasyChair (`ct_table` with "Authors" header).
- Offline pipeline stays as is: `refresh_rdf_cache.py` -> `ponder_dblp` -> `snapshot/`.

## Known problems in the current code (fix in rewrite)
- SQL built via f-strings (`'{cleaned}'`) -> injection/quote bugs (e.g. names with `'`). Use bound parameters.
- Summary view takes `first` row per author, so the year shown is arbitrary; should be the most recent shared paper (plus count).
- Matching "ignore cutoff" is a separate checkbox; replace with a single "any year" option.
- Every start reads ~1.9 GB CSV into memory; consider a persisted `.duckdb` file built once per snapshot.
- Globals (`con`, `everything`, `authors_ids`) -> proper app state.
- `like` matcher has no wildcard guidance; ambiguous names produce many hits with no way to pick the right person.

## Decisions
- Shared deployment, **no auth**, concurrency matters.
- Backend: FastAPI + uvicorn.
- Frontend: vanilla JS, no libraries (revisit a table library only if needed; discuss first).
- Results: co-authors grouped (latest shared year, paper count), rows expandable to the shared papers.

## Concurrency / shared-use design
- Build a persisted `snapshot/dblp.duckdb` once (offline step), open it `read_only=True` -> no writers, safe to share.
- One DuckDB connection per process; each request uses its own `con.cursor()` (thread-safe), queries run in the threadpool (`def` routes / `run_in_threadpool`) so the event loop is never blocked. DuckDB releases the GIL.
- Indexes/sorted tables for name-prefix lookup and `papers_authors(AuthorID)` / `(PaperID)` so a query is ms, not a full scan of 29M links. Benchmark with parallel requests.
- Guard rails (no auth): max queries per request, max result rows (with "truncated" flag), query timeout, max size for pasted HTML in guess endpoint, no `LIKE` pattern of only wildcards, optional per-IP rate limit.
- Memory: DuckDB `memory_limit`/`threads` settings configurable; decide worker count (1 process by default; multiple workers share the read-only file via OS page cache).
- Snapshot refresh: swap in a new `.duckdb` file and reload without restart (or restart-only for v1).
- Frontend must HTML-escape all DB/user strings (titles come from DBLP; pasted HTML is only parsed server-side).

## Proposed architecture
- **Backend:** Python 3.13, DuckDB, FastAPI + uvicorn (JSON API, serves static frontend). Start with `uv run quickdblp` -> http://127.0.0.1:8000 (host/port configurable for sharing).
- **Frontend:** single static page, no build step.
- **API sketch**
  - `GET  /api/meta` – snapshot date/age, counts.
  - `POST /api/authors/search` – `{queries[], matcher}` -> candidate authors per query (id, name, DBLP, ORCID, paper count, latest year).
  - `POST /api/coauthors` – `{author_ids[], since_year|null, exclude_self, paper_types[], ...}` -> summary rows and per-paper rows.
  - `POST /api/guess` – `{source: "pcs"|"easychair", html}` -> author names.
- **Data layer:** `quickdblp/db.py` (load/index/prepared queries), `quickdblp/guess.py` (lxml parsers), `quickdblp/app.py` (routes).

## Ground truth / data audit (added after the first full audit, 2026-10-15)
- `quickdblp audit` (`quickdblp/audit.py`): independent streaming reader of the RDF dump -> truth file -> comparison with the database (papers, years, types, links, authors, dangling links) + warnings for publications with authors that parser rules drop. Full dump: ~7 min read (1.7M lines/s with system gzip), ~30 s comparison, 1.5 GB scratch in `audit/`.
- Findings on the snapshot made by the old parser (vs. the complete dump, 8,812,262 publications): all 8,675,746 papers' years/types and all links correct, except
  - **export off-by-one** (C++): last paper, last author and the first link were never written; metadata counts were wrong the same way, so the build check could not notice. Fixed in `ponder_dblp.cpp`.
  - **duplicate (paper, author) links** (516 in the old snapshot; DBLP lists some people twice on a paper) inflated counts. Fixed in the parser and removed by the loader (`duplicate_links_removed` in the database meta).
  - **Reference** records (16,758 with authors) were not parsed at all; **Editorship** (editors via `editedBy`/`EditorSignature`) and **Withdrawn** likewise. Now parsed as types 8, 9, 10; the page leaves them unticked by default (`default_excluded_types`).
- Not covered by the audit (needs other checks): the dump itself vs. dblp.org; people with several DBLP profiles; the name search.
- **Name variants:** 14% of the authors (592k of 4.2M) are signed with more than one spelling on their papers ("Ruben D. Vromans" / "Ruben Vromans"). The parser keeps one (smallest string; before: whichever thread came first, so also the ORCID was lost at random for ~0.007% of authors). Only that variant is searchable by prefix/LIKE; accent-insensitive prefix and fuzzy matching cover some of the rest. To find all variants, store all signature names as aliases (needs a table `author_names` in the parser output and a search on it).

## Parser performance (ponder_dblp, 2026-10-16)
Full dump (739M lines, 43.7 GB uncompressed) on 16 cores: **410 s -> 52 s**, CPU time 15 min -> 2 min. Output verified identical in content (papers, authors, links) to the previously audited snapshot and passing `quickdblp audit`.
- Profile of the old version: 8.6 s counting the uncompressed size (only for a progress bar), 266 s reading/parsing, 135 s writing CSV + exit; ~140 us CPU per record, one heap allocation per input line, 4 copies of every record, log messages built for every paper/author although logging was off, one global lock per paper/author/link, 32 threads on 16 cores, unbuffered CSV writing, destructors of 13M strings at exit.
- Done: block reader with `memchr` line splitting and a bounded queue (records moved, not copied); in-place XML parse; sharded ID tables with `string_view` lookup; lock-free chunked storage; links stored per paper; pre-sized tables; one worker per core; 8 MB write buffers, three CSVs written in parallel; `quick_exit` after everything is closed; progress from the compressed offset (no counting pass); `ponder_dblp [file]`.
- New behaviour: deterministic author name/ORCID (see above); malformed records are counted (`records_failed`, exit code 2) instead of crashing the run; the loader reports them as `parser_records_failed`.
- Linux (WSL2, 2026-10-19): the first Linux build was 5x slower than Windows (8 s vs 1.6 s on a 30M-line slice, 8.4 s of it system time): waking a worker thread for every single record is a system call and often an inter-processor interrupt, which is expensive in a VM. Records are now handed to the workers in batches of 64 (bounded queue of 8 batches per worker): slice 7.9 s -> 1.1 s on Linux, Windows 1.7 -> 1.5 s; the full dump takes **24 s on Linux** (52 s on Windows); output identical in content across OS and before/after (slice and full-dump counts).
- Build trees are per OS: `ponder_dblp/build|install/<windows|linux>/x64-release`, presets `x64-*` (Windows) and `linux-x64-*`; verified by building both from the same checkout.
- Not done: CSV writing still takes ~16 s (probably disk bound); the reader is one thread (decompression 5 GB/s is not the limit, but line handling is); the XML DOM per record could be replaced by direct field extraction (now ~36 s of the run).
- [ ] regenerate the real snapshot with the fixed parser, rebuild the database, run `quickdblp audit` until it is all PASS (a trial of the whole chain runs in `audit/new/`)
- [ ] optional: spot-check a few authors against the live dblp.org per-person XML (network)

## Testing rules
- DBLP changes constantly (new papers, new conflicts): tests must **never assert on live data**. Use the fixed fixture in `tests/conftest.py`; no dependence on the current date either (pass `since_year` explicitly).
- Any optional smoke test against the real `snapshot/` must only check invariants (e.g. results are non-empty, years >= cutoff, a searched author is never listed when `exclude_self` is set), never exact counts or names, and must skip when no snapshot exists.

## Tasks
- [x] M-1 Benchmark (done, snapshot of 2026-06-18, 4.1M authors / 8.5M papers / 29M links):
  - Build `.duckdb` from CSVs: ~20 s + ~15 s for indexes on `papers_authors`; file 1.57 GB (CSVs: 1.9 GB). The old notebook path re-reads the CSVs on every start.
  - Open read-only: 0.04 s, RSS 41 MB at open, ~270 MB after 10 parallel queries (data is paged in on demand, one shared buffer pool).
  - Queries: co-author join for a person ~0.05 s warm (0.17 s cold); 10 parallel co-author queries finish in 0.42 s wall; first-ever query cold ~3 s (page-in), prefix search 0.03 s.
  - Accent-insensitive scan over all names 0.04 s; **fuzzy jaro_winkler full scan 0.05 s** -> no blocking/prefilter needed.
  - Conclusion: persisted `.duckdb` is a clear win; fuzzy search is feasible as-is. Consider a warm-up query at startup.
- [x] M0 Project setup: `quickdblp/` package, `pyproject.toml` (fastapi/uvicorn added, pytest/httpx dev group, hatchling build, `quickdblp` entry point). Jupyter/itables deps stay until the notebook is removed (M6).
- [x] M1 Data layer (`quickdblp/db.py`, 23 tests in `tests/`, hand-made CSV fixture instead of `mini.rdf`)
  - [x] `meta` table in the `.duckdb`: full `dblp_metadata.json` content + size/mtime of the 3 CSVs + schema version
  - [x] `build-snapshot`: up-to-date check via fingerprint, build into `.tmp`, verify row counts, atomic rename; `--force`
  - [x] startup check in the server: fingerprint mismatch/missing -> rebuild before serving (in `create_app` lifespan)
  - Note: on Windows `os.replace` fails while the server holds the old file open -> live refresh needs a restart or a different swap (later).
  - Real snapshot: build 38 s, `.duckdb` 1.7 GB, counts verified against metadata JSON, repeat call 0.2 s.
- [x] M2 API (`quickdblp/app.py`, `quickdblp/guess.py`, `uv run quickdblp serve`; 42 tests, all on the fixture)
  - Endpoints: `GET /api/meta`, `POST /api/authors/search`, `GET /api/authors/{id}/recent`, `POST /api/conflicts`, `POST /api/reviewers/check` (status `conflict` / `no_conflict` / `not_found` / `ambiguous`, with per-candidate conflict entries), `POST /api/guess`. Static frontend is served from `quickdblp/static/` (M3).
  - Guard rails done: max 50 queries/author IDs, 200 reviewers, 5000 co-author rows (`truncated` flag), 2 MB pasted HTML, 300 chars/query, per-query timeout (default 30 s, 504), max 8 concurrent queries (`--max-concurrent`), single process, warm-up query at startup.
  - [ ] still open: optional per-IP rate limit; `GET /api/meta` could also report whether a newer DBLP dump exists
- [x] M3 Frontend (`quickdblp/static/`, vanilla JS, no libraries; checked end-to-end in Edge via Playwright against a fixture DB, incl. an XSS probe): search box (multi-author, `;`), candidate disambiguation step, filters (years, types, exclude self, text filter), summary + per-paper views, sortable columns, clickable DBLP/ORCID links
- [x] M4 Guessers (server side `guess.py` + paste box in step 1): PCS + EasyChair paste box (port from notebook), error messages for unrecognised HTML
- [-] M5 Output: export dropped for now (not wanted)
- [x] UI e2e tests in `tests/test_ui.py` (Playwright, optional `ui` dependency group; skipped when Playwright/browser missing)
- [x] Malformed CSV records (found with the 2026-10-15 snapshot): `ponder_dblp` wrote raw tabs/line breaks from arXiv titles into the TSV. Fixed in the parser (`sanitizeField`, takes effect after rebuilding `ponder_dblp`) and tolerated by the loader (`repair_tsv`, 4 records repaired in the current snapshot); failed rename because a server holds the DB now gives a clear error and no leftover `.tmp`.
- [x] M6 Docs: `Readme.md` updated. Notebook (`query_dblp.ipynb`, `query_dblp.py`) and Jupyter deps are **kept for now** at the user's request; remove later.
- [ ] Later: incremental snapshot updates (existing open item)

## More decisions (from Q&A)
- Load: ~10 concurrent users at peak. **Exactly one copy of the data in memory:** single uvicorn process (no multi-worker, since each process has its own buffer pool), one read-only DuckDB connection with per-request cursors, no pandas copies of the tables (stream/fetch only result rows).
- Persisted `.duckdb` snapshot is OK if it loads considerably faster than the CSVs -> **benchmark first** (CSV load vs. open `.duckdb`); keep the CSV -> duckdb conversion as an offline build step (`quickdblp build-snapshot`).
- Candidate disambiguation: search shows matched authors first; user ticks the right person(s) (with paper count, latest year, ORCID, a few recent titles to tell homonyms apart) before co-authors are computed.
- Cutoff: "years back from today" (keep slider 1–10 plus "any year").
- No export for now.
- Search matching: dropdown `Prefix` / `Pattern (LIKE)` / `Accent-insensitive` / `Fuzzy`. Accent-insensitive via DuckDB `strip_accents`; fuzzy via `jaro_winkler_similarity`/`levenshtein` on a pre-normalised name column (check speed on 4.1M names; may need a prefilter such as same first letter or token blocking).

## Workflow / UI (reviewer check)
Use case: take the author list of a submission, compute their conflicts, then check a reviewer list against it.
1. **Authors**: paste PCS/EasyChair HTML (guesser) or type names / DBLP URLs, `;` separated -> per name a candidate list -> tick the right DBLP IDs.
2. **Conflicts** (computed from the ticked authors): co-author table, grouped, expandable papers, filters (years back, exclude self, paper types, free-text filter).
3. **Reviewers to check** (optional input, names or DBLP URLs, one per line or `;`): the conflict table is filtered to these people; reviewers with no conflict are listed as "no conflict found" so nothing is silently dropped; unresolved/ambiguous reviewer names are flagged so the user can pick the right DBLP ID.
Make the three steps explicit in the layout (numbered sections, state kept client-side, shareable URL optional).

- Step 3 lists reviewers with "no conflict found". UI is English only, no branding.

## Open questions (remaining)
- none

## Auto-refresh (done, 2026-10-19) and Docker (still to do)
Decisions: anyone may start an update from the page (safeguards: one job at a time, no-op if dblp.org has nothing newer, wait after a failure); cheap checks only before switching (parser clean, integrity, size sanity; the full `audit` stays manual); weekly check, update only if the dump is newer; image built by GitHub Actions and pushed to a private ghcr.io (the server pulls with a read:packages token on the host; nothing secret inside the image).

Done (95 tests incl. browser tests pass):
- [x] `quickdblp/refresh.py`: RefreshManager (single job, OS file lock, HEAD check, resumable download with ETag/Range + MD5, parser run with progress, validation, versioned databases + `current.json` pointer, hot swap, pruning, weekly scheduler, initial import when there is no data, `adopt`)
- [x] CLI: `serve --data-dir` (env QUICKDBLP_DATA_DIR / _INTERVAL_DAYS / _PARSER), `refresh`, `adopt`; API `/api/refresh` GET/POST; 503 on data endpoints until the first import is done; the old `--db/--snapshot-dir` mode still works (no updates)
- [x] page: update status line, button, progress bar (download 2-40%, parse 40-75%, build 75-92%, ...), details log
- [x] tests: `tests/test_refresh.py` (fake dblp.org server with ETag/Range/MD5/interruptions, stub parser), browser test of the panel in `tests/test_ui.py`
- [x] Readme section "Self-updating server"
- [x] real run against dblp.org (scratch data dir `audit/data-test`): 2.76 GB download ~4 min, parse 52 s, build+validate ~1 min -> 8,757,662 papers, 4,220,031 authors (dump of 2026-10-19); the server started from that directory, reported "up to date", a repeated update request did nothing. The run found one bug the tests had missed (relative data dir: the parser got a relative dump path; fixed + regression test).

Later changes (2026-10-19): plain `quickdblp serve` is now the self-updating mode (data dir `data/`, adopts `snapshot/dblp.duckdb` by hard link on the first start without starting an update; `--db`/`--snapshot-dir` select the old fixed mode; `--interval-days 0` = no automatic checks). The release workflow builds Windows and Linux (ubuntu-24.04, GCC 13) binaries, smoke-tests both on `mini.rdf.gz` and creates one release from both; the Linux binary links libstdc++/libgcc statically (needs only glibc). Not run on GitHub yet (only the YAML syntax was checked locally).

Still to do (Docker, nothing tested because Docker is not installed on the dev machine; the image needs GCC >= 13 to build the parser, the Linux build itself is verified in WSL/Ubuntu 24.04):
- [ ] Dockerfile (multi-stage: build ponder_dblp with cmake/vcpkg on Linux - not yet compiled on Linux, may need small portability fixes - then python/uv image), `.dockerignore` (exclude snapshot/, audit/, data/, dblp.rdf.gz, .venv, build dirs), docker-compose.yml (volume /data, memory limit >= 12-16 GB, healthcheck on /api/meta), Readme section
- [ ] GitHub Actions workflow: build and push to ghcr.io (private); `docker login ghcr.io` with a read:packages token on the server
