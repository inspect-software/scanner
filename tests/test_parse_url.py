import pytest

from scanner.github import parse_repo_url


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/pallets/flask",
        "https://github.com/pallets/flask/",
        "https://github.com/pallets/flask.git",
        "https://www.github.com/pallets/flask/issues",
        "git@github.com:pallets/flask.git",
        "pallets/flask",
    ],
)
def test_parse_valid(url):
    assert parse_repo_url(url) == ("pallets", "flask")


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/owner/name",
        "https://github.com/onlyowner",
        "not a url",
        "",
    ],
)
def test_parse_invalid(url):
    with pytest.raises(ValueError):
        parse_repo_url(url)
