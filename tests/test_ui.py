"""Browser tests of the web page (optional: skipped when Playwright or a browser is not available).

Setup:  uv sync --group ui       (installs Playwright; uses an installed Edge or Chrome)
        uv run playwright install chromium     (only if neither Edge nor Chrome is installed)
Run:    uv run --group ui pytest tests/test_ui.py
"""

import re
import socket
import sys
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
    # a 422 is the server rejecting a request (e.g. a page of the wrong kind); the browser logs it, the tests may provoke it
    page.on("console", lambda m: m.type == "error" and "status of 422" not in m.text and errors.append(m.text))
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
    expect(page.locator("#type-boxes input")).to_have_count(10)
    for type_id in (1, 2, 3, 4, 5, 6, 7):
        expect(page.locator(f"#type-boxes input[value='{type_id}']")).to_be_checked()
    for type_id in (8, 9, 10):  # Reference, Editorship, Withdrawn: off until the user asks for them
        expect(page.locator(f"#type-boxes input[value='{type_id}']")).not_to_be_checked()
    expect(page.locator("#type-boxes label", has_text="Editorship")).to_have_attribute("title", re.compile("editors"))
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


# ----------------------------------------------------------------------------------------------- data updates

@pytest.fixture
def updating_site(tmp_path, monkeypatch):
    """A server that manages its own data (data directory) and a fake dblp.org to update from."""
    from test_refresh import STUB, DumpServer

    monkeypatch.setenv("STUB_DELAY", "2")
    dump = DumpServer()
    app = create_app(data_dir=tmp_path / "data", warm_up=False, refresh_options={
        "dump_url": dump.url, "parser_command": [sys.executable, STUB], "retry_delay": 0})
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/", dump
    server.should_exit = True
    thread.join(timeout=10)
    dump.close()


def test_update_panel(browser, updating_site):
    url, dump = updating_site
    page = browser.new_page(viewport={"width": 1200, "height": 900})
    page_errors = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    status, button = page.locator("#update-status"), page.locator("#update-btn")
    page.goto(url)

    # no data yet: the first import has started by itself and everybody can watch it
    expect(page.locator("#update-panel")).to_be_visible()
    expect(status).to_contain_text("Importing the data for the first time")
    expect(page.locator("#update-progress")).to_be_visible()
    expect(button).to_be_disabled()
    page.fill("#author-input", "Bob")
    page.press("#author-input", "Enter")
    expect(page.locator("#error")).to_contain_text("first import")  # searching too early says why it does not work

    # when it is done the data is live without reloading the page
    expect(status).to_contain_text("The data was updated", timeout=60000)
    expect(page.locator("#snapshot-info")).to_contain_text("DBLP data from", timeout=10000)
    expect(page.locator("#update-progress")).to_be_hidden()
    page.press("#author-input", "Enter")
    expect(page.locator("#cand-2")).to_be_checked()
    # the log of the import opens in a floating window under the Log button, and Escape closes it again
    expect(page.locator("#update-log-btn")).to_be_visible()
    expect(page.locator("#update-log-window")).to_be_hidden()
    page.click("#update-log-btn")
    expect(page.locator("#update-log-window")).to_be_visible()
    expect(page.locator("#update-log")).to_contain_text("checksum ok")
    window, log_button = page.locator("#update-log-window").bounding_box(), page.locator("#update-log-btn").bounding_box()
    assert window["y"] >= log_button["y"] + log_button["height"]                      # below the button
    assert window["x"] >= 0 and window["x"] + window["width"] <= page.viewport_size["width"]  # inside the screen
    page.keyboard.press("Escape")
    expect(page.locator("#update-log-window")).to_be_hidden()

    # nothing newer on dblp.org: the button says so and does not start anything
    button.click()
    expect(status).to_contain_text("Already up to date")
    assert dump.count("GET") == 1

    # a newer dump: one click, progress is shown, searching keeps working meanwhile
    dump.new_version("v2")
    time.sleep(1.1)
    button.click()
    expect(status).to_contain_text("Updating the data")
    expect(page.locator("#update-progress")).to_be_visible()
    page.press("#author-input", "Enter")
    expect(page.locator("#cand-2")).to_be_checked()
    expect(status).to_contain_text("The data was updated", timeout=60000)
    assert dump.count("GET") == 2
    assert page_errors == []
    page.close()


# ----------------------------------------------------------------------------------------------- compact controls

PCS_PAGE = '<div class="authorList"><ul><li><span>Alice Anders</span></li><li><span>Bob Brandt</span></li></ul></div>'


def test_author_box_grows_and_the_extraction_tool_is_a_window(page):
    box = page.locator("#author-input")
    start = box.bounding_box()["height"]
    page.fill("#author-input", "\n".join(f"Name {i}" for i in range(6)))
    assert box.bounding_box()["height"] > 2 * start - 10        # grows with its content ...
    page.fill("#author-input", "")
    assert abs(box.bounding_box()["height"] - start) < 2         # ... and shrinks back
    page.fill("#author-input", "\n".join(f"Name {i}" for i in range(40)))
    assert box.bounding_box()["height"] < 400                    # but only up to a limit (then it scrolls)
    page.fill("#author-input", "")

    expect(page.locator("#guess-window")).to_be_hidden()
    page.click("#guess-open")
    expect(page.locator("#guess-window")).to_be_visible()
    expect(page.locator("#guess-html")).to_be_focused()
    # a page of the wrong kind: the message appears inside the window, which stays open
    page.fill("#guess-html", "<p>nothing here</p>")
    page.click("#guess-btn")
    expect(page.locator("#guess-error")).to_contain_text("authorList")
    expect(page.locator("#guess-window")).to_be_visible()
    # a good page: the window closes, the names are in the box (which grew) and are searched
    page.fill("#guess-html", PCS_PAGE)
    page.click("#guess-btn")
    expect(page.locator("#guess-window")).to_be_hidden()
    expect(box).to_have_value("Alice Anders\nBob Brandt")
    expect(page.locator(".group")).to_have_count(2)
    page.click("#guess-open")
    expect(page.locator("#guess-error")).to_be_hidden()         # the old message is gone
    page.keyboard.press("Escape")
    expect(page.locator("#guess-window")).to_be_hidden()


def test_conflict_toolbar_is_one_row(page):
    page.set_viewport_size({"width": 1800, "height": 1000})  # on a narrow window the toolbar wraps, which is fine
    page.fill("#author-input", "Alice Anders; Bob Brandt")
    page.press("#author-input", "Enter")
    page.check("#cand-1")
    expect(page.locator("#conflict-summary")).to_contain_text("co-author")
    boxes = {name: page.locator(sel).bounding_box() for name, sel in [
        ("filter", "#conflict-filter"), ("exclude", "#exclude-self"), ("expand", "#expand-all"),
        ("collapse", "#collapse-all"), ("summary", "#conflict-summary")]}
    centers = {name: b["y"] + b["height"] / 2 for name, b in boxes.items()}
    assert max(centers.values()) - min(centers.values()) < 14, centers   # all on the same line (wide window)
    # the buttons still work
    page.click("#expand-all")
    expect(page.locator("#conflict-table tr.detail").first).to_be_visible()
    page.click("#collapse-all")
    expect(page.locator("#conflict-table tr.detail").first).to_be_hidden()


def test_matching_selector_sits_beside_its_button(page):
    select, button = page.locator("#author-matcher").bounding_box(), page.locator("#author-search").bounding_box()
    assert abs((select["y"] + select["height"] / 2) - (button["y"] + button["height"] / 2)) < 10
    label = page.locator("label.inline-field", has_text="Matching").first.bounding_box()
    assert label["height"] < 50                                         # the label is not stacked above the select


# ----------------------------------------------------------------------------------------------- paper type tags

def style_of(locator, *properties):
    return locator.evaluate("(el, props) => Object.fromEntries(props.map(p => [p, getComputedStyle(el)[p]]))", list(properties))


def test_paper_types_are_colored_toggle_tags(page):
    page.fill("#author-input", "Alice Anders")       # the filters appear once authors are selected
    page.press("#author-input", "Enter")
    page.check("#cand-1")
    expect(page.locator("#shared-filters")).to_be_visible()
    pills = page.locator("#type-boxes label.type-pill")
    expect(pills).to_have_count(10)
    # every type has its own colour (the excluded ones are outlined, so compare the colour of the border)
    colors = {pills.nth(i).get_attribute("data-type"): style_of(pills.nth(i), "borderTopColor")["borderTopColor"] for i in range(10)}
    assert len(set(colors.values())) == 10, colors

    included, excluded = page.locator("label.type-pill[data-type='1']"), page.locator("label.type-pill[data-type='8']")
    assert "on" in included.get_attribute("class") and "on" not in excluded.get_attribute("class")  # Reference starts off
    inc, exc = style_of(included, "backgroundColor", "borderStyle"), style_of(excluded, "backgroundColor", "borderStyle")
    assert inc["backgroundColor"] != "rgba(0, 0, 0, 0)" and exc["backgroundColor"] == "rgba(0, 0, 0, 0)"
    assert inc["borderStyle"] == "solid" and exc["borderStyle"] == "dashed"   # not only the colour tells them apart
    assert included.evaluate("el => getComputedStyle(el, '::before').content") == '"\u2713"'

    # a click on the tag toggles it (the checkbox underneath is the real control)
    excluded.click()
    expect(page.locator("#type-boxes input[value='8']")).to_be_checked()
    assert "on" in excluded.get_attribute("class")
    excluded.click()
    expect(page.locator("#type-boxes input[value='8']")).not_to_be_checked()


def test_tags_in_the_paper_lists_have_the_color_of_their_filter_tag(page):
    page.fill("#author-input", "Alice Anders; Bob Brandt")
    page.press("#author-input", "Enter")
    page.check("#cand-1")
    page.check("#any-year")
    expect(page.locator("#conflict-summary")).to_contain_text("(all years)")
    page.click("#expand-all")
    for type_id in ("1", "3"):                     # Inproceedings and Article occur in the fixture
        tag = page.locator(f"#conflict-table .tag[data-type='{type_id}']").first
        pill = page.locator(f"label.type-pill[data-type='{type_id}']")
        assert style_of(tag, "backgroundColor", "color") == style_of(pill, "backgroundColor", "color")
    inproceedings = style_of(page.locator("#conflict-table .tag[data-type='1']").first, "backgroundColor")
    article = style_of(page.locator("#conflict-table .tag[data-type='3']").first, "backgroundColor")
    assert inproceedings != article

    # filtering still works through the tags: switch Article off, the Article papers disappear
    page.locator("label.type-pill[data-type='3']").click()
    expect(page.locator("#conflict-table .tag[data-type='3']")).to_have_count(0)
    expect(page.locator("#conflict-table .tag[data-type='1']").first).to_be_visible()
