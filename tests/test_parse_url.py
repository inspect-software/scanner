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
        # Unexpanded build placeholders. A Maven pom whose <scm> still reads
        # ${github.org}/${github.name} is structurally a valid URL, and one
        # such row reached the production catalogue before this check.
        "https://github.com/${github.org}/${github.name}",
        "${github.org}/${github.name}",
        "https://github.com/@{owner}/name",
        # An owner cannot start or end with a hyphen, or run two together.
        "https://github.com/-acme/name",
        "https://github.com/ac--me/name",
        "https://github.com/" + "a" * 40 + "/name",
    ],
)
def test_parse_invalid(url):
    with pytest.raises(ValueError):
        parse_repo_url(url)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/dotnet/runtime", ("dotnet", "runtime")),
        ("https://github.com/a/b.c_d-e", ("a", "b.c_d-e")),
        ("https://github.com/ninenines/cowboy", ("ninenines", "cowboy")),
    ],
)
def test_parse_keeps_legitimate_names(url, expected):
    assert parse_repo_url(url) == expected
