"""Generator of small RDF dumps in the layout of the real one (one element per line)."""

PUB = "https://dblp.org/rdf/schema#Publication"


def record(entity, key, authors=(), title=True, year=None, event_year=None, signatures=None, rdf_type=PUB, editors=False):
    """One RDF record, laid out like the real dump (one element per line)."""
    sig = len(authors) if signatures is None else signatures
    lines = [f'<dblp:{entity} rdf:about="https://dblp.org/rec/{key}">', f'<rdf:type rdf:resource="{rdf_type}" />',
             '<datacite:hasIdentifier>', '<rdf:type rdf:resource="http://purl.org/spar/datacite/Identifier" />',
             '</datacite:hasIdentifier>']
    if title:
        lines.append("<dblp:title>A title that continues" if key == "multiline" else "<dblp:title>A title</dblp:title>")
        if key == "multiline":
            lines += ["", "on the next lines.</dblp:title>"]
    person, signature = ("editedBy", "EditorSignature") if editors else ("authoredBy", "AuthorSignature")
    lines += [f'<dblp:{person} rdf:resource="https://dblp.org/pid/{a}" />' for a in authors]
    for a in list(authors)[:sig] + ["x"] * max(0, sig - len(authors)):
        lines += ["<dblp:hasSignature>", f"<dblp:{signature}>", f"<dblp:signatureDblpName>Name {a}</dblp:signatureDblpName>",
                  f'<dblp:signatureCreator rdf:resource="https://dblp.org/pid/{a}" />', f"</dblp:{signature}>",
                  "</dblp:hasSignature>"]
    if event_year:
        lines.append(f'<dblp:yearOfEvent rdf:datatype="http://www.w3.org/2001/XMLSchema#gYear">{event_year}</dblp:yearOfEvent>')
    if year:
        lines.append(f'<dblp:yearOfPublication rdf:datatype="http://www.w3.org/2001/XMLSchema#gYear">{year}</dblp:yearOfPublication>')
    lines.append(f"</dblp:{entity}>")
    return "\n".join(lines) + "\n\n"


RDF = "".join([
    record("Inproceedings", "conf/a/1", ["a/A", "b/B"], year=2024),
    record("Article", "journals/x/2", ["a/A"], year=2023, event_year=2019),          # publication year wins
    record("Book", "books/3", ["b/B"], event_year=2015),                             # only the event year
    record("Inproceedings", "conf/a/4", ["a/A", "a/A"], year=2022),                  # the same person twice
    record("Informal", "journals/corr/5", ["a/A", "b/B"], year=2021, signatures=1),  # author/signature mismatch
    record("Editorship", "conf/e/6", ["a/A"], year=2020, editors=True),              # editors, not authors
    record("Reference", "reference/r/10", ["c/C", "a/A"], year=2014),                # entry of a reference work
    record("Withdrawn", "journals/w/11", ["b/B"], year=2018),                        # withdrawn paper
    record("Mystery", "conf/m/12", ["a/A"], year=2019),                              # entity type nobody handles
    record("Article", "journals/x/7", ["c/C"], title=False, year=2020),              # no title
    record("Article", "journals/x/8", ["c/C"], year=None),                           # no year at all
    record("Proceedings", "conf/p/9", [], year=2020),                                # no authors
    record("Data", "multiline", ["c/C"], year=2025),                                 # title spans lines
    record("Person", "pid/a/A", [], title=False, rdf_type="https://dblp.org/rdf/schema#Creator"),  # not a publication
])

