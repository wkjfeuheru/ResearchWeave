from researchx.services.message_chunks import split_message


def test_split_message_prefers_word_boundaries() -> None:
    chunks = split_message("hello world again", 8)

    assert chunks == ["hello", "world", "again"]
    assert all(len(chunk) <= 8 for chunk in chunks)


def test_split_message_hard_splits_long_unbroken_text() -> None:
    chunks = split_message("abcdef", 2)

    assert chunks == ["ab", "cd", "ef"]


def test_split_message_empty_text_returns_no_chunks() -> None:
    assert split_message("", 10) == []
