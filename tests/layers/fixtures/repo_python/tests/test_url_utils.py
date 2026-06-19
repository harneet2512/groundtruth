"""Tests for url_utils — used by L3 hook tests to surface the TESTS family.

Uses unittest-style assertions because gt_hook's TestAssertionMiner only
recognises ``self.assertX`` calls, not bare ``assert`` statements.
"""
from __future__ import annotations

import unittest

from url_utils import parse_url, normalize_url, is_https


class TestUrlUtils(unittest.TestCase):
    def test_parse_url_returns_parsed_object(self) -> None:
        parsed = parse_url("https://example.com/foo")
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "example.com")

    def test_parse_url_rejects_empty(self) -> None:
        self.assertRaises(ValueError, parse_url, "")

    def test_normalize_url_lowercases_host(self) -> None:
        self.assertEqual(normalize_url("HTTPS://EXAMPLE.COM/p"), "https://example.com/p")

    def test_is_https_true(self) -> None:
        self.assertTrue(is_https("https://example.com"))
