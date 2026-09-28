"""Integration check: both modules must agree on the frozen interface.

Owned by the integrator. Exits 0 only when the two modules compose correctly.
"""

import sys

import report
import textstats

SAMPLE = "the quick brown fox jumps over the lazy dog the fox"
EXPECTED_COUNT = 11
EXPECTED_TOP = [("the", 3), ("fox", 2)]
EXPECTED_RENDER = "the: 3\nfox: 2"


def main():
    """Run the cross-module check and return a process exit code."""
    problems = []
    count = textstats.word_count(SAMPLE)
    if count != EXPECTED_COUNT:
        problems.append(f"word_count returned {count!r}, expected {EXPECTED_COUNT}")

    top = textstats.top_words(SAMPLE, 2)
    if top != EXPECTED_TOP:
        problems.append(f"top_words returned {top!r}, expected {EXPECTED_TOP}")

    rendered = report.render(SAMPLE, 2)
    if rendered != EXPECTED_RENDER:
        problems.append(f"render returned {rendered!r}, expected {EXPECTED_RENDER!r}")

    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        return 1
    print(f"ok word_count={count} top={top}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
