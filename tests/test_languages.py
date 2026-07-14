from scanner.languages import MAX_LANGUAGES, significant_languages


def test_single_language_repo():
    assert significant_languages({"Python": 100_000}, "Python") == ["Python"]


def test_residue_languages_are_dropped():
    # Shell/Dockerfile glue under the 10% share never counts as a repo language.
    langs = {"Python": 90_000, "Shell": 6_000, "Dockerfile": 4_000}
    assert significant_languages(langs, "Python") == ["Python"]


def test_multi_language_repo_ordered_by_bytes():
    langs = {"TypeScript": 40_000, "Rust": 50_000, "Python": 45_000, "Shell": 5_000}
    assert significant_languages(langs, "Rust") == ["Rust", "Python", "TypeScript"]


def test_boundary_share_is_included():
    # Exactly 10% counts as significant.
    assert significant_languages({"Python": 90_000, "Go": 10_000}, "Python") == ["Python", "Go"]


def test_empty_map_falls_back_to_primary():
    assert significant_languages({}, "Python") == ["Python"]
    assert significant_languages({}, None) == []


def test_zero_byte_entries_ignored():
    assert significant_languages({"Python": 0}, None) == []
    assert significant_languages({"Python": 10, "Go": 0}, None) == ["Python"]


def test_never_empty_when_bytes_exist():
    # Pathological map where nothing reaches the share threshold still names
    # the largest language rather than returning nothing.
    langs = {f"Lang{i}": 100 for i in range(20)}
    result = significant_languages(langs, None)
    assert result == ["Lang0"]


def test_capped_at_max_languages():
    langs = {f"Lang{i}": 1000 for i in range(8)}  # each has 12.5% share
    assert len(significant_languages(langs, None)) == MAX_LANGUAGES


def test_markup_languages_are_not_filtered():
    # For a docs/design repo, HTML genuinely is the language.
    langs = {"HTML": 70_000, "CSS": 30_000}
    assert significant_languages(langs, "HTML") == ["HTML", "CSS"]
