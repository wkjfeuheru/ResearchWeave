from researchx.workspace.paths import safe_filename


def test_safe_filename_strips_path_and_unsafe_characters() -> None:
    assert safe_filename("../bad name;$(rm).txt") == "bad_name_rm_.txt"


def test_safe_filename_rejects_empty_or_parent_segments() -> None:
    assert safe_filename("../") == ""
    assert safe_filename("...") == ""
