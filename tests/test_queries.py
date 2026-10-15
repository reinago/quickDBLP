import pytest


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
    assert db.search_authors("Cruz", matcher="prefix")["authors"] == []


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
