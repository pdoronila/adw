from search import search


def test_finds_match():
    assert "cat" in search(["cat", "dog"], "cat")


def test_ignores_trailing_whitespace():
    assert "cat" in search(["cat", "dog"], "cat ")
