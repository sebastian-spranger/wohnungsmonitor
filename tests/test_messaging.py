#!/usr/bin/env python3
"""Tests für die Telegram-Nachrichtenformatierung (HTML parse_mode)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import monitor_lite as ml  # noqa: E402


class MessagingTestCase(unittest.TestCase):
    def test_esc_html(self) -> None:
        self.assertEqual(ml._esc_html("A & B <b>"), "A &amp; B &lt;b&gt;")
        self.assertEqual(ml._esc_html('Titel "x"'), "Titel &quot;x&quot;")

    def test_als_nachricht_html_escaped(self) -> None:
        w = ml.Wohnung(id="t", titel="*Superior* Suite _mit_ [Klammern] & <b>Tags</b>",
                       preis_warm=1500, groesse=50, zimmer=2,
                       url="https://x.de/a&b?c=d", quelle="SZ-Test",
                       adresse="Lehel, München")
        msg = w.als_nachricht(0)
        # Titel ist HTML-escaped in <b>-Tags, keine rohen HTML-Tags aus dem Titel
        self.assertIn("<b>*Superior* Suite _mit_ [Klammern] &amp; &lt;b&gt;Tags&lt;/b&gt;</b>", msg)
        self.assertNotIn("<b>Tags</b>", msg)
        # URL im href ist escaped
        self.assertIn('href="https://x.de/a&amp;b?c=d"', msg)
        # Keine Legacy-Markdown-Steuerzeichen mehr
        self.assertNotIn("](https://", msg)


if __name__ == "__main__":
    unittest.main()
