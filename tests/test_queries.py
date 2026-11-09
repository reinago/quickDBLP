import duckdb
import pytest

from conftest import write_snapshot
from quickdblp.db import Database, build_snapshot


def names(result):
    return [a["name"] for a in result["authors"]]


def by_name(entries):
    return {e["name"]: e for e in entries}


def test_prefix_case_insensitive_and_stats(db):
    res = db.search_authors("alice")
    assert names(res) == ["Alice Anders", "Alice Anders"]
    by_id = {a["id"]: a for a in res["authors"]}
    assert by_id[1]["paper_count"] == 4 and by_id[1]["latest_year"] == 2024
    assert by_id[1]["orcid"] == "https://orcid.org/0000-0000-0000-0001"
    assert by_id[5]["paper_count"] == 2


def test_last_first_form(db):
    assert names(db.search_authors("Brandt, Bob")) == ["Bob Brandt"]


def test_apostrophe_is_safe(db):
    assert names(db.search_authors("Dave O'Brien")) == ["Dave O'Brien"]
    assert db.search_authors("x' or '1'='1")["authors"] == []


def test_dblp_url(db):
    assert names(db.search_authors("https://dblp.org/pid/3")) == ["Carol Cruz"]


def test_like(db):
    assert names(db.search_authors("%Cruz", matcher="like")) == ["Carol Cruz"]
    assert db.search_authors("Cruz", matcher="like")["authors"] == []  # a pattern means the whole name


def test_accent_insensitive(db):
    assert db.search_authors("Jose Muller", matcher="prefix")["authors"] == []
    assert names(db.search_authors("Jose Muller", matcher="accent")) == ["José Müller"]


def test_fuzzy(db):
    assert names(db.search_authors("Alise Anders", matcher="fuzzy"))[:2] == ["Alice Anders", "Alice Anders"]
    assert db.search_authors("Zzzz Qqqq", matcher="fuzzy")["authors"] == []


def test_empty_query_and_limit(db):
    assert db.search_authors("   ") == {"authors": [], "truncated": False}
    res = db.search_authors("%", matcher="like", limit=2)
    assert len(res["authors"]) == 2 and res["truncated"] is True


def test_unknown_matcher(db):
    with pytest.raises(ValueError):
        db.search_authors("a", matcher="nope")


def test_coauthors_any_year(db):
    # author 1 is on papers 1, 2, 3, 6
    res = by_name(db.coauthors([1]))
    assert set(res) == {"Alice Anders", "Bob Brandt", "Carol Cruz", "José Müller"}
    assert res["Bob Brandt"]["paper_count"] == 1


def test_coauthors_since_year(db):
    res = db.coauthors([1], since_year=2021)  # papers 1 and 6 only
    assert {e["id"] for e in res} == {1, 2, 5}
    assert all(p["year"] >= 2021 for e in res for p in e["papers"])


def test_exclude_self(db):
    res = db.coauthors([1], exclude_self=True)
    assert 1 not in {e["id"] for e in res}
    assert 5 in {e["id"] for e in res}  # the other profile is a different person


def test_exclude_types(db):
    res = db.coauthors([5], exclude_types=[6])
    assert {e["id"] for e in res} == {1, 5}  # informal paper 4 (with Dave) dropped, paper 6 stays
    assert db.coauthors([5], exclude_types=[1, 6]) == []


def test_multiple_authors_dedupe_and_via(db):
    res = {e["id"]: e for e in db.coauthors([1, 5])}
    # paper 6 has both 1 and 5: must appear once per co-author
    assert [p["id"] for p in res[1]["papers"]].count(6) == 1
    assert res[1]["via"] == [5]
    assert res[6]["via"] == [5] and res[2]["via"] == [1]


def test_paper_fields(db):
    e = by_name(db.coauthors([2]))["Carol Cruz"]
    assert e["latest_year"] == 2023 and e["paper_count"] == 1
    assert e["papers"][0] == {
        "id": 5, "dblp": "https://dblp.org/rec/p5", "title": "Paper 5",
        "type": "Inproceedings", "type_id": 1, "year": 2023,
    }


def test_no_authors(db):
    assert db.coauthors([]) == []


def test_recent_papers(db):
    assert [p["id"] for p in db.recent_papers(1, limit=2)] == [1, 6]


# ----------------------------------------------------------------------------------------------- names by their words

NAMES = [
    (10, "https://dblp.org/pid/10", "Guido Reina", ""),
    (11, "https://dblp.org/pid/11", "Hans-Peter Seidel", ""),
    (12, "https://dblp.org/pid/12", "Wei Wang 0001", ""),
    (13, "https://dblp.org/pid/13", "Wei Wang 0002", ""),
    (14, "https://dblp.org/pid/14", "Zoë Lüdenscheid", ""),
    (15, "https://dblp.org/pid/15", "Reinald Bauer", ""),
    (16, "https://dblp.org/pid/16", "Percy_Underscore", ""),
]


@pytest.fixture
def names_db(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    write_snapshot(snapshot, extra_authors=NAMES)
    path = tmp_path / "dblp.duckdb"
    build_snapshot(snapshot, path)
    database = Database(path)
    yield database
    database.close()


@pytest.mark.parametrize("index", [True, False], ids=["name index", "without index"])
def test_prefix_matches_the_start_of_any_word(names_db, index):
    names_db._name_index = index  # an old database that could not be upgraded must give the same answers
    find = lambda q, m="prefix": names(names_db.search_authors(q, matcher=m))  # noqa: E731
    assert find("Reina") == ["Guido Reina", "Reinald Bauer"]       # last name only; any word, sorted by name
    assert find("Reinal") == ["Reinald Bauer"]
    assert find("Guido Rei") == ["Guido Reina"]                    # every query word must match ...
    assert find("Reina Guido") == ["Guido Reina"]                  # ... in any order
    assert find("Guido Bauer") == []
    assert find("Peter") == ["Hans-Peter Seidel"]                  # hyphens separate words
    assert find("Hans-Peter Sei") == ["Hans-Peter Seidel"]
    assert find("Wang") == ["Wei Wang 0001", "Wei Wang 0002"]
    assert find("Wang 0002") == ["Wei Wang 0002"]                  # the homonym number still narrows it down
    assert find("eina") == []                                     # the start of a word, not any part
    assert find("Ludenscheid") == []                              # plain prefix keeps the accents ...
    assert find("Lüdenscheid") == ["Zoë Lüdenscheid"]
    assert find("Ludenscheid", "accent") == ["Zoë Lüdenscheid"]   # ... the accent-insensitive one does not
    assert find("zoe lud", "accent") == ["Zoë Lüdenscheid"]
    assert find("%") == [] and find("_") == []                    # LIKE wildcards are plain characters here
    assert find("Percy_") == ["Percy_Underscore"]
    assert find("Brandt, Bob") == ["Bob Brandt"]


@pytest.mark.parametrize("index", [True, False], ids=["name index", "without index"])
def test_fuzzy_compares_word_by_word(names_db, index):
    names_db._name_index = index
    find = lambda q: names(names_db.search_authors(q, matcher="fuzzy"))  # noqa: E731
    assert find("Riena") == ["Guido Reina"]                       # a misspelt last name alone
    assert find("Guido Riena")[0] == "Guido Reina"
    assert find("Seidl") == ["Hans-Peter Seidel"]
    assert find("Luedenscheid") == ["Zoë Lüdenscheid"]
    assert set(find("Wang")) == {"Wei Wang 0001", "Wei Wang 0002"}
    assert find("Alise Anders")[:2] == ["Alice Anders", "Alice Anders"]
    assert find("Guido Bauer") == []                              # each word matches someone, but no one both


@pytest.mark.parametrize("matcher", ["prefix", "accent", "fuzzy"])
def test_the_name_index_changes_nothing_but_the_speed(names_db, matcher):
    """Same candidates, order, paper counts and latest years with and without the index."""
    for query in ("Alice", "Wang", "Reina", "Guido Reina", "Bob Brandt", "Zoe", "Anders Alice", "Wang 0001"):
        names_db._name_index = True
        indexed = names_db.search_authors(query, matcher=matcher)
        names_db._name_index = False
        assert names_db.search_authors(query, matcher=matcher) == indexed, query


def test_an_old_database_gets_the_name_index_when_opened(snapshot_dir, tmp_path):
    path = tmp_path / "dblp.duckdb"
    build_snapshot(snapshot_dir, path)
    con = duckdb.connect(str(path))  # as built before the name index existed
    for table in ("name_words", "name_word_list", "author_stats"):
        con.execute(f"drop table {table}")
    con.execute("delete from meta where key = 'name_index'")
    con.close()
    database = Database(path)
    try:
        assert database._name_index
        assert names(database.search_authors("Cruz")) == ["Carol Cruz"]
    finally:
        database.close()
    database = Database(path)
    assert database._name_index  # and it stays there
    database.close()
