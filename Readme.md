# quickDBLP

Utilities for local checking of author conflicts.

## TL;DR
prepare python env
```
python -m venv .venv
.venv\Scripts\Activate.(bat|ps1|sh) # depending on your shell!
pip install -r requirements.txt
```
compile parser (you need to have CMake installed and a 64-bit dev environment active, e.g. using a Visual Studio Developer Command Prompt)
```
cd ponder_dblp
mkdir build
cmake --preset=x64-release .
cmake --build --preset=x64-release --target install
cd ..
```
get/refresh DBLP entries
```
python refresh_rdf_cache.py
```
parse and move snapshot into place
```
ponder_dblp\install\x64-release\bin\ponder_dblp.exe
move *.csv snapshot
```
open and run `query_dblp.ipynb`

## Description

- `refresh_rdf_cache.py` cache fetcher in python: checks whether `dblp.rdf.gz` exists locally and is not more than 14 days older than the online version. If not, the current file is downloaded. File is put in `./`.
- `ponder_dblp/` contains a multi-threaded C++ variant of the parser that is a bit faster. You need CMake or a modern Visual Studio (with CMake support) to build it. It needs to be run in the same folder as the downloaded rdf, e.g. by calling `ponder_dblp\out\build\x64-release\ponder_dblp.exe` if you used VS out of the box without customization of the build process. It still needs more than 7 mins to do its thing and can possibly be further optimized.
- `query_dblp.ipynb` Jupyter notebook for searching co-authors within a given threshold in years. **Reads the csv files from the `./snapshot/` folder!**


## Local files
- `dblp.rdf.gz` the database fetched from DBLP
- `dblp_authors.csv` tab-separated file containing a `NumericID` (primary key), `DBLP` (a link to the DBLP profile of the author), `Name` (readable name), and `ORCID` (link or empty)
- `dblp_papers.csv` tab-separated file containing a `NumericID` (primary key), `DBLP` (a link to the DBLP entry of a paper), `Title` (readable paper title), and `Year` (the publicatoin year)
- `dblp_papers_authors.csv` tab-separated file containing the relations `paper 1--* authors`: `PaperID` and `AuthorID` (foreign keys referencing the other two tables)

## TODOs

- [x] check whether parsing can be made faster (nearly 10 times, see the C++ thing above.)
- [ ] support incremental updates
- [x] check for multiple authors at once (semicolon separated)
- [x] paste pieces of PCS or other conference software and try to guess author lists from those
