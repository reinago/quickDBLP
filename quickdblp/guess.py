"""Extract author names from the HTML source of submission systems."""

from __future__ import annotations

import re

from lxml import etree, html

GUESSERS = ("pcs", "easychair")


class GuessError(ValueError):
    pass


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse(source: str):
    if not source.strip():
        raise GuessError("no HTML given")
    try:
        return html.fromstring(source)
    except (etree.ParserError, etree.XMLSyntaxError) as e:
        raise GuessError(f"could not parse the HTML: {e}") from e


def guess_pcs(source: str) -> list[str]:
    tree = _parse(source)
    author_lists = tree.find_class("authorList")
    if not author_lists:
        raise GuessError("no author list found (expected an element with class 'authorList'); is this a PCS page?")
    names = []
    for li in author_lists[0].xpath(".//li"):
        span = li.find(".//span")
        if span is not None and _clean(span.text_content()):
            names.append(_clean(span.text_content()))
    return names


def guess_easychair(source: str) -> list[str]:
    tree = _parse(source)
    for table in tree.find_class("ct_table"):
        first_row = table.find(".//tr")
        first_cell = first_row.find(".//td") if first_row is not None else None
        if first_cell is None or _clean(first_cell.text_content()) != "Authors":
            continue
        names = []
        for row in table.xpath(".//tr")[2:]:
            cells = row.xpath(".//td")
            if len(cells) > 1:
                name = _clean(cells[0].text_content() + " " + cells[1].text_content())
                if name:
                    names.append(name)
        return names
    raise GuessError("no authors table found (expected a 'ct_table' headed 'Authors'); is this an EasyChair page?")


def guess_authors(source_kind: str, source: str) -> list[str]:
    if source_kind == "pcs":
        return guess_pcs(source)
    if source_kind == "easychair":
        return guess_easychair(source)
    raise GuessError(f"unknown source {source_kind!r}")
