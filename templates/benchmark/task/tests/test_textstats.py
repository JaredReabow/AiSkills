"""Tests for the frozen textstats interface (worker-a's deliverable)."""

import unittest

import textstats


class WordCountTests(unittest.TestCase):
    def test_counts_whitespace_separated_words(self):
        self.assertEqual(3, textstats.word_count("one two three"))

    def test_blank_text_counts_zero(self):
        self.assertEqual(0, textstats.word_count("   "))

    def test_newlines_and_tabs_separate_words(self):
        self.assertEqual(5, textstats.word_count("a a b\n c\td"))


class TopWordsTests(unittest.TestCase):
    def test_lowercases_and_orders_by_count(self):
        self.assertEqual([("b", 3), ("a", 1)], textstats.top_words("B b A b", 2))

    def test_ties_break_alphabetically(self):
        self.assertEqual(
            [("alpha", 1), ("beta", 1)], textstats.top_words("beta alpha", 5)
        )

    def test_limit_is_respected(self):
        self.assertEqual(1, len(textstats.top_words("a b c", 1)))

    def test_empty_text_returns_empty_list(self):
        self.assertEqual([], textstats.top_words("   ", 3))


if __name__ == "__main__":
    unittest.main()
