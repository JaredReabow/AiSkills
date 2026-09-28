"""Reference implementation of the frozen textstats interface."""


def word_count(text):
    """Return the number of whitespace-separated words in text."""
    return len(text.split())


def top_words(text, n):
    """Return up to n (word, count) pairs ordered by count then alphabet."""
    counts = {}
    for word in text.split():
        key = word.lower()
        counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ordered[:n]
