# quickDBLP

Local checking of author conflicts (co-authorship) against a snapshot of [DBLP](https://dblp.org/), with a small web page.

1. **Authors** – find the DBLP profiles of a submission's authors (names, DBLP URLs, or extracted from a PCS/EasyChair page) and tick the right ones.
2. **Conflicts** – their co-authors within the last N years, grouped, with the shared papers expandable. Filter by years, paper types, free text. *Reference* (entries of encyclopedias/handbooks), *Editorship* (edited volumes; the people are editors) and *Withdrawn* records are included in the data but unticked by default.
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

Either fetch the current parser binaries from [here](https://github.com/reinago/quickDBLP/releases) (Windows: `ponder_dblp.exe` with its two DLLs; Linux: `ponder_dblp-linux-x64.tar.gz`, which needs glibc 2.39 or newer, e.g. Ubuntu 24.04) or compile the parser yourself. Windows and Linux have separate build and install trees (`ponder_dblp/build/<windows|linux>/...`, `ponder_dblp/install/<windows|linux>/...`), so one checkout can be built from Windows and from WSL/Linux without the two interfering.

Windows (you need CMake, Ninja and a 64-bit Visual Studio development environment, e.g. a Developer Command Prompt):
```
cd ponder_dblp
cmake --preset=x64-release
cmake --build --preset=x64-release --target install
cd ..
```
Linux / WSL (you need cmake, ninja, g++ 13 or newer, git, curl, zip, unzip, tar and pkg-config, e.g. `sudo apt install cmake ninja-build g++ git curl zip unzip tar pkg-config`; the dependencies are fetched by vcpkg. Under WSL make sure `pkg-config` is the Linux one: WSL appends the Windows `PATH`, which may contain a Windows `pkg-config` that vcpkg cannot run):
```
cd ponder_dblp
cmake --preset=linux-x64-release
cmake --build --preset=linux-x64-release --target install
cd ..
```
The other presets are `x64-debug`, `x64-relwithdeb` and `linux-x64-debug`, `linux-x64-relwithdeb`.

Get/refresh the DBLP dump
```
python refresh_rdf_cache.py
```
Parse it and move the snapshot into place
```
ponder_dblp\install\windows\x64-release\bin\ponder_dblp.exe      # Linux: ponder_dblp/install/linux/x64-release/bin/ponder_dblp
move *.csv snapshot
```
Start the web page and open <http://127.0.0.1:8000>. The server keeps its data in the directory `data/`: on the first start it adopts an existing `snapshot/dblp.duckdb` (instantly), or, if there is none, imports the current dump from dblp.org by itself (a few minutes, progress is shown in the page). From then on the page header has an update button, and the data is refreshed in the background (see [Self-updating server](#self-updating-server-data-directory)).
```
uv run quickdblp serve
```
(without uv, in the activated venv: `python -m quickdblp.cli serve`)

## Web server

```
uv run quickdblp serve [--host 0.0.0.0] [--port 8000] [--threads N] [--memory-limit 4GB]
                       [--max-concurrent 8] [--timeout 30]
                       [--data-dir data] [--interval-days 7] [--parser path/to/ponder_dblp]
                       [--db snapshot/dblp.duckdb] [--snapshot-dir snapshot]    # fixed-database mode, see below
```

- **Sharing:** `--host 0.0.0.0` makes it reachable for others on your network. There is **no authentication**, so only do this in a network you trust.
- **One copy of the data:** the server is a single process with one read-only DuckDB database; requests share it. At most `--max-concurrent` queries run at the same time (others wait) and every query is cancelled after `--timeout` seconds. Requests are capped (50 authors, 200 reviewers, 5000 result rows, 2 MB of pasted HTML).
- **Fixed-database mode:** giving `--db` and/or `--snapshot-dir` serves exactly that database, without the update button and without background updates. To update it by hand: stop the server, put new CSVs into `snapshot/`, start it again (it notices and rebuilds). To rebuild without starting the server, or to force it: `uv run quickdblp build-snapshot [--force]`. On Windows the database file cannot be replaced while a server is using it. (The self-updating mode below avoids all this.)
- **Fuzzy / accent-insensitive search:** the *Matching* dropdown offers prefix, accent-insensitive prefix, fuzzy (Jaro-Winkler) and `LIKE` patterns.
- **Keyboard:** Enter in the author, reviewer and page-source boxes presses their button; Shift+Enter inserts a line break.

## Self-updating server (data directory)

```
uv run quickdblp serve [--data-dir data] [--interval-days 7] [--parser path/to/ponder_dblp]     # this is what plain `serve` does
uv run quickdblp refresh --data-dir data [--force]     # update now, from the command line (e.g. for cron)
uv run quickdblp adopt --data-dir data --db snapshot/dblp.duckdb [--move]   # start from an existing database
```
By default (without `--db`/`--snapshot-dir`) the server manages its data itself (environment: `QUICKDBLP_DATA_DIR`, `QUICKDBLP_INTERVAL_DAYS`, `QUICKDBLP_PARSER`):
- **First start without data:** the server comes up at once and imports the current dump from dblp.org (download, parse, build; a few minutes). The page shows the progress; searching says that the import is still running. If there is already a `snapshot/dblp.duckdb`, it is adopted instead (hard link, no copy) and counts as just checked: starting the server never triggers an update by itself, the first automatic check comes one interval later. `quickdblp adopt` does the same by hand for another database.
- **Update:** every `--interval-days` (7; `0` = never automatically, only by button) the server asks dblp.org (a HEAD request) whether the dump changed, and only then downloads it. Anybody can also press **Check for updates / Update now** in the page header. While an update runs, searching continues on the old data; then the new database replaces it without restart.
- **Safe by construction:** only one update runs at a time, however many people click (they all see the same progress), and an OS file lock also covers a second process on the same directory (no stale lock files). The download is resumable and checked against dblp.org's MD5. The new database only replaces the old one if the parser finished without skipped records, the integrity checks pass, and it is not much smaller than the current one (90%). Otherwise the old data stays active, the error (with a log) is shown in the page, and the next try is allowed after 5 minutes (manual) or 30 minutes (automatic). The full `quickdblp audit` is not part of an update; run it by hand when you want the ground-truth check.
- **Requirements:** the `ponder_dblp` binary (found in `ponder_dblp/build|install`, on the `PATH`, or via `--parser`), about 6 GB of RAM while parsing, and free disk space of about five times the dump (14 GB).
- **Files in the data directory:** `current.json` (which database is active), `state.json`, `refresh.lock`, `downloads/` (the dump), `versions/<run>/dblp.duckdb` (the last two are kept), `logs/` (the last ten runs).
- **API:** `GET /api/refresh` (status), `POST /api/refresh` (`{"force": false}`; answers `started`, `already_running`, `up_to_date`, `recent_failure` or `check_failed`).

## Description

- `quickdblp/` the application
  - `db.py` builds the persisted DuckDB database from the snapshot CSVs (with a fingerprint to detect outdated databases) and runs the author/co-author/reviewer queries
  - `app.py` FastAPI app (JSON API under `/api/`, interactive docs at `/docs`) serving the static page
  - `refresh.py` the background update (check, download, parse, build, validate, switch)
  - `guess.py` extracts author lists from PCS / EasyChair page sources
  - `static/` the web page (plain HTML/CSS/JS, no libraries, no build step)
  - `cli.py` the `quickdblp` command
- `refresh_rdf_cache.py` cache fetcher in python: checks whether `dblp.rdf.gz` exists locally and is not more than 14 days older than the online version. If not, the current file is downloaded. File is put in `./`.
- `ponder_dblp/` contains a multi-threaded C++ parser that turns the RDF dump into the CSV files below. You need CMake or a modern Visual Studio (with CMake support) to build it. It reads `dblp.rdf.gz` from the current directory (or the file given as first argument, `ponder_dblp path\to\dump.rdf.gz`) and writes the CSV files into the current directory. On a 16-core machine it needs about a minute for the full dump on Windows and under half a minute on Linux, and about 5.5 GB of RAM. It is deterministic: two runs give identical files except for the numeric IDs. Malformed records are counted instead of aborting the run (`records_failed` in `dblp_metadata.json`, exit code 2). Each author is stored with one name variant, the lexicographically smallest of the ones used on their papers, and an ORCID if any signature has one.
- `query_dblp.ipynb` / `query_dblp.py` the previous Jupyter notebook version (kept for now). It reads the csv files from the `./snapshot/` folder; select the venv created above as the kernel.

## Local files
- `dblp.rdf.gz` the database fetched from DBLP
- `snapshot/dblp_authors.csv` tab-separated file containing a `NumericID` (primary key), `DBLP` (a link to the DBLP profile of the author), `Name` (readable name), and `ORCID` (link or empty)
- `snapshot/dblp_papers.csv` tab-separated file containing a `NumericID` (primary key), `DBLP` (a link to the DBLP entry of a paper), `Title` (readable paper title), `Type` (1 Inproceedings, 2 Incollection, 3 Article, 4 Book, 5 Part, 6 Informal, 7 Data, 8 Reference, 9 Editorship, 10 Withdrawn) and `Year` (the publication year)
- `snapshot/dblp_papers_authors.csv` tab-separated file containing the relations `paper 1--* authors`: `PaperID` and `AuthorID` (foreign keys referencing the other two tables)
- `snapshot/dblp_metadata.json` source timestamp and row counts; the counts are checked when the database is built
- `snapshot/dblp.duckdb` generated by `quickdblp build-snapshot` / `serve`, about 1.7 GB; safe to delete, it is rebuilt when needed

Parsers before this version wrote line breaks and tabs from titles (e.g. arXiv titles with math) into the CSV unescaped. The loader repairs such records automatically (and logs a warning); the current `ponder_dblp` no longer produces them.

## Verifying the data (ground truth)

Conflicts are only as good as the pipeline DBLP dump -> `ponder_dblp` -> CSV -> database. To be sure nothing got lost on the way, `audit` re-reads the original dump with a separate, deliberately simple reader (no shared code with the C++ parser) and compares everything with the database: every publication and why it is missing (or not), every year and type, every (paper, author) link, every author, dangling links.
```
uv run quickdblp audit --rdf dblp.rdf.gz --db snapshot/dblp.duckdb
```
It streams the whole dump (about 7 minutes for the 2.7 GB gz, ~740M lines; uses the system `gzip`, falls back to Python's) and keeps a ~1.5 GB intermediate file in `audit/` (use `--reuse-truth` to compare again without re-reading the dump). `FAIL` means database and dump disagree; the `WARNINGS` section lists publications with authors that the parser leaves out by its rules (their co-authorships cannot be found). Exit code 1 on failure. Run it after every snapshot update.

The same reader is used in the tests: the C++ parser is run on `mini.rdf.gz` and on a synthetic dump with every record type and awkward case (editors, doubled authors, missing title/year, ...) and its output must match the dump exactly (needs a `ponder_dblp` binary newer than `ponder_dblp.cpp`; skipped otherwise).

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
