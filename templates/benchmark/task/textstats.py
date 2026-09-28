"""Word statistics for the benchmark fixture.

Frozen interface, owned by worker-a:

    word_count(text) -> int
        Number of whitespace-separated words in text.

    top_words(text, n) -> list[tuple[str, int]]
        Lowercased words ordered by count descending, then alphabetically.
        At most n entries; empty input returns [].
"""


def word_count(text):
    """Return the number of whitespace-separated words in text."""
    raise NotImplementedError("worker-a owns word_count")


def top_words(text, n):
    """Return up to n (word, count) pairs ordered by count then alphabet."""
    raise NotImplementedError("worker-a owns top_words")
