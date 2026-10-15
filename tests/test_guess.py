import pytest

from quickdblp.guess import GuessError, guess_authors

PCS = """
<html><body><div class="authorList"><ul>
  <li><span> Alice   Anders </span><span>Uni A</span></li>
  <li><span>Bob Brandt</span><span>Uni B</span></li>
  <li><span></span></li>
</ul></div></body></html>
"""

EASYCHAIR = """
<html><body>
<table class="ct_table"><tr><td>Title</td><td>Foo</td></tr></table>
<table class="ct_table">
  <tr><td>Authors</td><td></td><td></td></tr>
  <tr><td>first name</td><td>last name</td><td>email</td></tr>
  <tr><td>Alice</td><td>Anders</td><td>a@x.org</td></tr>
  <tr><td>José</td><td>Müller</td><td>j@x.org</td></tr>
</table></body></html>
"""


def test_pcs():
    assert guess_authors("pcs", PCS) == ["Alice Anders", "Bob Brandt"]


def test_easychair():
    assert guess_authors("easychair", EASYCHAIR) == ["Alice Anders", "José Müller"]


@pytest.mark.parametrize("kind", ["pcs", "easychair"])
def test_wrong_page(kind):
    with pytest.raises(GuessError):
        guess_authors(kind, "<html><body><p>nothing here</p></body></html>")


@pytest.mark.parametrize("kind", ["pcs", "easychair"])
def test_empty(kind):
    with pytest.raises(GuessError):
        guess_authors(kind, "   ")


def test_unknown_source():
    with pytest.raises(GuessError):
        guess_authors("nope", PCS)
