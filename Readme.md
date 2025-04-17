# quickDBLP

Utilities for local checking of author conflicts. Should work out-of-the-box with Strawberry Perl (v5.40 and up). The Jupyter notebook depends on pandas and itables at the very least.

## Description

- `refresh_rdf_cache.pl` checks whether `dblp.rdf.gz` exists locally and is not more than 14 days older than the online version. If not, the current file is downloaded. Files are put in `./`.
- `ponder_dblp.pl` reads the local `dblp.rdf.gz` and extracts all InProceedings and Articles along with their authors, putting a concise version of the data in local csv files. This takes about an hour and reduces the raw data size from 33+GB to ~1.5GB. The CSVs are put in `./`. You have to overwrite the snapshot manually and **intentionally** for now (see below).
- alternatively, the eponymous folder contains a multi-threaded C++ version. It still needs more than 7 mins to do its thing and can possibly be further optimized.
- `query_dblp.ipynb` Jupyter notebook for searching co-authors within a given threshold in years. **Reads the csv files from the `./snapshot/` folder!**

## Local files
- `dblp.rdf.gz` the database fetched from DBLP
- `dblp_authors.csv` tab-separated file containing a `NumericID` (primary key), `DBLP` (a link to the DBLP profile of the author), `Name` (readable name), and `ORCID` (link or empty)
- `dblp_papers.csv` tab-separated file containing a `NumericID` (primary key), `DBLP` (a link to the DBLP entry of a paper), `Title` (readable paper title), and `Year` (the publicatoin year)
- `dblp_papers_authors.csv` tab-separated file containing the relations `paper 1--* authors`: `PaperID` and `AuthorID` (foreign keys referencing the other two tables)

## TODOs

- [ ] check whether parsing can be made faster (nearly 10 times, see the C++ thing above.)
- [ ] support incremental updates
- [ ] check for multiple authors at once (comma separated)
- [ ] paste pieces of PCS or other conference software and try to guess authors lists from those