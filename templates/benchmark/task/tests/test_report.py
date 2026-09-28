"""Tests for the frozen report interface (worker-b's deliverable)."""

import unittest

import report


class RenderTests(unittest.TestCase):
    def test_renders_top_words(self):
        self.assertEqual("the: 3\nfox: 2", report.render("the fox the dog the fox", 2))

    def test_default_limit_is_three(self):
        self.assertEqual(3, len(report.render("a b c d").splitlines()))

    def test_empty_text_renders_nothing(self):
        self.assertEqual("", report.render("   "))


if __name__ == "__main__":
    unittest.main()
