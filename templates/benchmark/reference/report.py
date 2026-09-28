"""Reference implementation of the frozen report interface."""

import textstats


def render(text, limit=3):
    """Return one "word: count" line per top word."""
    entries = textstats.top_words(text, limit)
    return "\n".join(f"{word}: {count}" for word, count in entries)
