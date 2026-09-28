"""Report rendering for the benchmark fixture.

Frozen interface, owned by worker-b:

    render(text, limit=3) -> str
        One "word: count" line per top word, newline separated, no trailing
        newline. Empty input returns "".
"""

import textstats


def render(text, limit=3):
    """Return one "word: count" line per top word."""
    raise NotImplementedError("worker-b owns render")
