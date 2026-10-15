"""Browser tests of the web page (optional: skipped when Playwright or a browser is not available).

Setup:  uv sync --group ui       (installs Playwright; uses an installed Edge or Chrome)
        uv run playwright install chromium     (only if neither Edge nor Chrome is installed)
Run:    uv run --group ui pytest tests/test_ui.py
"""

import re
import socket
import threading
import time

import pytest
import uvicorn

from conftest import write_snapshot
from quickdblp.app import create_app

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

XSS = "<img src=x onerror=window.pwned=1>"
ROWS = "#conflict-table tbody tr:not(.detail)"


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ui")
    snap = tmp / "snapshot"
    snap.mkdir()
    # author 8 has a hostile name and shares paper 1 with author 1
    write_snapshot(snap, extra_authors=[(8, "https://dblp.org/pid/8", XSS, "")], extra_links=[(1, 8)])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(tmp / "dblp.duckdb", snapshot_dir=snap), port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        for options in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
            try:
                b = p.chromium.launch(**options)
                break
            except Exception:
                continue
        else:
            pytest.skip("no browser available (install Edge/Chrome or run: playwright install chromium)")
        yield b
        b.close()


@pytest.fixture
def page(browser, base_url):
    page = browser.new_page(viewport={"width": 1200, "height": 1000})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.goto(base_url)
    yield page
    page.close()
    assert errors == [], "console/page errors"


def select_alice_and_bob(page):
    """Search both authors, tick Alice's first profile, show all years."""
    page.fill("#author-input", "Alice Anders; Bob Brandt")
    page.press("#author-input", "Enter")
    page.check("#cand-1")
    page.check("#any-year")
    expect(page.locator("#conflict-summary")).to_contain_text("(all years)")


def test_search_select_and_layout(page):
    expect(page.locator("#snapshot-info")).to_contain_text("DBLP data from 2026-06-18")
    expect(page.locator("#type-boxes input")).to_have_count(7)
    expect(page.locator("#tabs")).to_be_hidden()
    expect(page.locator("#right-empty")).to_be_visible()

    page.fill("#author-input", "Alice Anders; Bob Brandt; Nobody Here")
    page.press("#author-input", "Enter")  # Enter = Search
    expect(page.locator(".group")).to_have_count(3)
    expect(page.locator("#cand-2")).to_be_checked()  # unique match is preselected
    expect(page.locator("#cand-1")).not_to_be_checked()  # homonyms are not
    expect(page.locator(".group").nth(2)).to_contain_text("No match")
    page.check("#cand-1")
    expect(page.locator("#selected-summary")).to_contain_text("Bob Brandt")
    expect(page.locator(ROWS).first).to_be_visible()
    expect(page.locator("#tab-conflicts")).to_contain_text(re.compile(r"Conflicts \(\d+\)"))

    left, right = page.locator("#step-authors").bounding_box(), page.locator("#right-pane").bounding_box()
    assert right["x"] >= left["x"] + left["width"] - 1, "results are to the right of the authors"
    assert left["width"] < right["width"]

    # clearing the selection hides the results again
    page.fill("#author-input", "Zzz")
    page.click("#author-search")
    expect(page.locator("#tabs")).to_be_hidden()
    expect(page.locator("#right-empty")).to_be_visible()


def test_conflict_list_filters_and_escaping(page):
    select_alice_and_bob(page)
    expect(page.locator("#years")).to_be_disabled()
    expect(page.locator(ROWS).first).to_contain_text("with ")  # several authors -> "with ..." line

    # hostile author name is shown as text and never executed
    expect(page.locator("#conflict-table")).to_contain_text(XSS)
    assert page.evaluate("window.pwned") is None

    page.locator(ROWS).first.locator(".toggle").click()
    expect(page.locator("#conflict-table tr.detail").first).to_be_visible()

    page.fill("#conflict-filter", "Carol")
    expect(page.locator(ROWS)).to_have_count(1)
    page.fill("#conflict-filter", "")

    names = "#conflict-table tbody tr:not(.detail) td:nth-child(2)"
    page.check("#exclude-self")
    expect(page.locator(names, has_text=re.compile(r"^Bob Brandt"))).to_have_count(0)
    expect(page.locator(names, has_text="Carol Cruz")).to_have_count(1)
    page.uncheck("#exclude-self")
    expect(page.locator(names, has_text=re.compile(r"^Bob Brandt"))).to_have_count(1)

    page.uncheck("#type-boxes input[value='1']")  # no Inproceedings: only Carol's 2020 Article is left
    expect(page.locator(ROWS)).to_have_count(2)  # Carol and Alice (paper 2)
    page.check("#type-boxes input[value='1']")

    page.click("#conflict-table th >> text=Papers")
    expect(page.locator("#conflict-table th[aria-sort='ascending']")).to_have_count(1)
    body = page.inner_text("body")
    assert "ORCID 0000-0000-0000-0001" in body and ", http" not in body


def test_reviewer_check_with_tabs(page):
    select_alice_and_bob(page)

    page.focus("#tab-conflicts")
    page.keyboard.press("ArrowRight")  # keyboard switch to tab 3
    expect(page.locator("#tab-reviewers")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#step-conflicts")).to_be_hidden()
    expect(page.locator("#step-reviewers")).to_be_visible()
    expect(page.locator("#shared-filters")).to_be_visible()  # filters are shared by both tabs

    page.click("#reviewer-input")
    for i, line in enumerate(["Carol Cruz", "Dave O'Brien", "Alice Anders", "Nobody Known"]):
        if i:
            page.keyboard.press("Shift+Enter")  # Shift+Enter = new line
        page.keyboard.type(line)
    assert page.input_value("#reviewer-input").splitlines() == ["Carol Cruz", "Dave O'Brien", "Alice Anders", "Nobody Known"]
    page.press("#reviewer-input", "Enter")  # Enter = Check

    expect(page.locator("#reviewer-summary")).to_contain_text("1 conflict, 1 no conflict found, 1 ambiguous, 1 not found")
    rows = page.locator("#reviewer-results tbody tr")
    expect(rows).to_have_count(4)
    for row, text in zip(range(4), ["Conflict", "No conflict found", "Several profiles match", "Not found in DBLP"]):
        expect(rows.nth(row)).to_contain_text(text)
    expect(page.locator("#tab-reviewers")).to_contain_text("in conflict")

    # the "shared papers" line expands to the papers
    rows.nth(0).locator("summary").click()
    expect(rows.nth(0).locator("details li").first).to_be_visible()

    # resolve the ambiguous reviewer by picking a profile
    rows.nth(2).locator("text=use this profile").first.click()
    expect(page.locator("#reviewer-input")).to_have_value("Carol Cruz\nDave O'Brien\nhttps://dblp.org/pid/1\nNobody Known")
    expect(page.locator("#reviewer-summary")).to_contain_text("2 conflict, 1 no conflict found, 0 ambiguous, 1 not found")

    # a changed (shared) filter re-runs the check
    page.uncheck("#type-boxes input[value='1']")
    page.uncheck("#type-boxes input[value='3']")
    expect(page.locator("#reviewer-summary")).to_contain_text("0 conflict")
