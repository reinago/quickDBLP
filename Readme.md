# quickDBLP

Utilities for local checking of author conflicts.

## Description

- `refresh_rdf_cache.pl` checks whether `dblp.rdf.gz` exists locally and is not more than 14 days older than the online version. If not, the current file is downloaded. Files are put in `./`.
- `ponder_dblp.pl` reads the local `dblp.rdf.gz` and extracts all InProceedings and Articles along with their authors, putting a concise version of the data in local csv files. This takes about an hour and reduces the raw data size from 33+GB to ~1.5GB. The CSVs are put in `./`. You have to overwrite the snapshot manually and **intentionally** for now (see below).
- `query_dblp.ipynb` Jupyter notebook for searching co-authors within a given threshold in years. **Reads the csv files from the `./snapshot/` folder!**

## Local files
- `dblp.rdf.gz` the database fetched from DBLP
- `dblp_authors.csv` tab-separated file containing a `NumericID` (primary key), `DBLP` (a link to the DBLP profile of the author), `Name` (readable name), and `ORCID` (link or empty)
- `dblp_papers.csv` tab-separated file containing a `NumericID` (primary key), `DBLP` (a link to the DBLP entry of a paper), `Title` (readable paper title), and `Year` (the publicatoin year)
- `dblp_papers_authors.csv` tab-separated file containing the relations `paper 1--* authors`: `PaperID` and `AuthorID` (foreign keys referencing the other two tables)

## TODOs

- [ ] check whether parsing can be made faster
- [ ] support incremental updates