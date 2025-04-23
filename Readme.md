# quickDBLP

Utilities for local checking of author conflicts. The Jupyter notebook depends on `pandas`, `lxml`, and `itables`, the python cache fetcher on `requests`.

TL;DR: `pip install pandas lxml itables requests`.

## Description

- `refresh_rdf_cache.py` cache fetcher in python: checks whether `dblp.rdf.gz` exists locally and is not more than 14 days older than the online version. If not, the current file is downloaded. File is put in `./`.
- `refresh_rdf_cache.pl` (deprecated) checks whether `dblp.rdf.gz` exists locally and is not more than 14 days older than the online version. If not, the current file is downloaded. File is put in `./`. Should work out-of-the-box with Strawberry Perl (v5.40 and up).
- `ponder_dblp.pl` (deprecated) reads the local `dblp.rdf.gz` and extracts all InProceedings and Articles along with their authors, putting a concise version of the data in local csv files. This takes about an hour and reduces the raw data size from 33+GB to ~1.5GB. The CSVs are put in `./`. You have to overwrite the snapshot manually and **intentionally** for now (see below).
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
- [ ] check for multiple authors at once (comma separated)
- [ ] paste pieces of PCS or other conference software and try to guess authors lists from those