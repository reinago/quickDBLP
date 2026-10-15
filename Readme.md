# quickDBLP

Local checking of author conflicts (co-authorship) against a snapshot of [DBLP](https://dblp.org/), with a small web page.

1. **Authors** – find the DBLP profiles of a submission's authors (names, DBLP URLs, or extracted from a PCS/EasyChair page) and tick the right ones.
2. **Conflicts** – their co-authors within the last N years, grouped, with the shared papers expandable. Filter by years, paper types, free text.
3. **Check reviewers** – paste the reviewers you have in mind; each one is reported as *conflict*, *no conflict found*, *several profiles match* (pick one) or *not found*.

## TL;DR

Prepare the python env using `uv` (recommended)
```
pip install uv
uv venv
uv sync
```
OR, if you do not know/care about uv
```
python -m venv .venv
.venv\Scripts\Activate.(bat|ps1|sh) # depending on your shell!
pip install -r requirements.txt
```

Either fetch the current parser binaries from [here](https://github.com/reinago/quickDBLP/releases) or compile the parser yourself (you need CMake and a 64-bit development environment, e.g. a Visual Studio Developer Command Prompt)
```
cd ponder_dblp
cmake --preset=x64-release .
cmake --build --preset=x64-release --target install
cd ..
```
Get/refresh the DBLP dump
```
python refresh_rdf_cache.py
```
Parse it and move the snapshot into place
```
ponder_dblp\install\x64-release\bin\ponder_dblp.exe
move *.csv snapshot
```
Start the web page (builds `snapshot/dblp.duckdb` first if it is missing or outdated, about a minute) and open <http://127.0.0.1:8000>
```
uv run quickdblp serve
```
(without uv, in the activated venv: `python -m quickdblp.cli serve`)

## Web server

```
uv run quickdblp serve [--host 0.0.0.0] [--port 8000] [--threads N] [--memory-limit 4GB]
                       [--max-concurrent 8] [--timeout 30] [--snapshot-dir snapshot] [--db snapshot/dblp.duckdb]
```

- **Sharing:** `--host 0.0.0.0` makes it reachable for others on your network. There is **no authentication**, so only do this in a network you trust.
- **One copy of the data:** the server is a single process with one read-only DuckDB database; requests share it. At most `--max-concurrent` queries run at the same time (others wait) and every query is cancelled after `--timeout` seconds. Requests are capped (50 authors, 200 reviewers, 5000 result rows, 2 MB of pasted HTML).
- **Updating the snapshot:** stop the server, put new CSVs into `snapshot/`, start it again (it notices and rebuilds). To rebuild without starting the server, or to force it: `uv run quickdblp build-snapshot [--force]`. On Windows the database file cannot be replaced while a server is using it.
- **Fuzzy / accent-insensitive search:** the *Matching* dropdown offers prefix, accent-insensitive prefix, fuzzy (Jaro-Winkler) and `LIKE` patterns.
- **Keyboard:** Enter in the author, reviewer and page-source boxes presses their button; Shift+Enter inserts a line break.

## Description

- `quickdblp/` the application
  - `db.py` builds the persisted DuckDB database from the snapshot CSVs (with a fingerprint to detect outdated databases) and runs the author/co-author/reviewer queries
  - `app.py` FastAPI app (JSON API under `/api/`, interactive docs at `/docs`) serving the static page
  - `guess.py` extracts author lists from PCS / EasyChair page sources
  - `static/` the web page (plain HTML/CSS/JS, no libraries, no build step)
  - `cli.py` the `quickdblp` command
- `refresh_rdf_cache.py` cache fetcher in python: checks whether `dblp.rdf.gz` exists locally and is not more than 14 days older than the online version. If not, the current file is downloaded. File is put in `./`.
- `ponder_dblp/` contains a multi-threaded C++ parser that turns the RDF dump into the CSV files below. You need CMake or a modern Visual Studio (with CMake support) to build it. It needs to be run in the same folder as the downloaded rdf, e.g. by calling `ponder_dblp\out\build\x64-release\ponder_dblp.exe` if you used VS out of the box. It still needs more than 7 mins to do its thing and can possibly be further optimized.
- `query_dblp.ipynb` / `query_dblp.py` the previous Jupyter notebook version (kept for now). It reads the csv files from the `./snapshot/` folder; select the venv created above as the kernel.

## Local files
- `dblp.rdf.gz` the database fetched from DBLP
- `snapshot/dblp_authors.csv` tab-separated file containing a `NumericID` (primary key), `DBLP` (a link to the DBLP profile of the author), `Name` (readable name), and `ORCID` (link or empty)
- `snapshot/dblp_papers.csv` tab-separated file containing a `NumericID` (primary key), `DBLP` (a link to the DBLP entry of a paper), `Title` (readable paper title), `Type` (1 Inproceedings, 2 Incollection, 3 Article, 4 Book, 5 Part, 6 Informal, 7 Data) and `Year` (the publication year)
- `snapshot/dblp_papers_authors.csv` tab-separated file containing the relations `paper 1--* authors`: `PaperID` and `AuthorID` (foreign keys referencing the other two tables)
- `snapshot/dblp_metadata.json` source timestamp and row counts; the counts are checked when the database is built
- `snapshot/dblp.duckdb` generated by `quickdblp build-snapshot` / `serve`, about 1.7 GB; safe to delete, it is rebuilt when needed

Parsers before this version wrote line breaks and tabs from titles (e.g. arXiv titles with math) into the CSV unescaped. The loader repairs such records automatically (and logs a warning); the current `ponder_dblp` no longer produces them.

## Tests

```
uv run pytest
```
The tests use a small fixed fixture and never depend on the current DBLP data or date. The browser tests (`tests/test_ui.py`) are optional and skipped unless Playwright and a browser are available:
```
uv sync --group ui
uv run --group ui pytest tests/test_ui.py
```
They use an installed Edge or Chrome; otherwise run `uv run playwright install chromium` once.

## TODOs

- [x] check whether parsing can be made faster (nearly 10 times, see the C++ thing above.)
- [ ] support incremental updates
- [x] check for multiple authors at once (semicolon separated)
- [x] paste pieces of PCS or other conference software and try to guess author lists from those
- [x] web page with a python backend
- [ ] remove the notebook once the web page has proven itself
- see `TODO.md` for the rest
