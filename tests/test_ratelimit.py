#!/usr/bin/env python3
"""Unit-Tests für den In-Memory-Rate-Limiter der Web-App."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import ratelimit  # noqa: E402


class RatelimitTestCase(unittest.TestCase):
    def test_window_blocks_third(self) -> None:
        ok1, _ = ratelimit.hit("k", [(2, 60)])
        ok2, _ = ratelimit.hit("k", [(2, 60)])
        ok3, retry = ratelimit.hit("k", [(2, 60)])
        self.assertTrue(ok1 and ok2)
        self.assertFalse(ok3)
        self.assertGreater(retry, 0)

    def test_keys_independent(self) -> None:
        ratelimit.hit("a", [(1, 60)])
        ok, _ = ratelimit.hit("b", [(1, 60)])
        self.assertTrue(ok)
        blocked, _ = ratelimit.hit("a", [(1, 60)])
        self.assertFalse(blocked)

    def test_multi_limits(self) -> None:
        # (3, 60) und (1, 5): die 5s-Grenze schlägt schon beim 2. Request zu
        ok1, _ = ratelimit.hit("m", [(3, 60), (1, 5)])
        ok2, _ = ratelimit.hit("m", [(3, 60), (1, 5)])
        self.assertTrue(ok1)
        self.assertFalse(ok2)


if __name__ == "__main__":
    unittest.main()
