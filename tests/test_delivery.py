#!/usr/bin/env python3
"""Tests für die pro-Nutzer-Zustellung (delivered.json)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import monitor_lite as ml  # noqa: E402


class DeliveryTestCase(unittest.TestCase):
    def test_seed_delivered_nur_vorweb_empfaenger(self) -> None:
        known = {"id1", "id2"}
        recipients = ["7647141150", "8812614073"]  # Sebastian + Finn
        seed = ml._seed_delivered(known, recipients, {"7647141150"})
        self.assertEqual(seed, {"id1": ["7647141150"], "id2": ["7647141150"]})
        self.assertNotIn("8812614073", seed["id1"])  # Finn bekommt Backlog

    def test_zustelllogik_neuer_empfaenger(self) -> None:
        recipients = ["7647141150", "8812614073"]
        delivered = {"id1": ["7647141150"]}  # Sebastian hat es schon
        erhalten = set(delivered["id1"])
        neu_fuer = [c for c in recipients if c not in erhalten]
        self.assertEqual(neu_fuer, ["8812614073"])
        # nach Zustellung an Finn: keiner mehr neu
        delivered["id1"] = sorted(erhalten | {"8812614073"})
        self.assertEqual(delivered["id1"], ["7647141150", "8812614073"])
        self.assertEqual([c for c in recipients if c not in set(delivered["id1"])], [])

    def test_load_save_roundtrip(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ml.DELIVERED_FILE = Path(d) / "delivered.json"
            ml.save_delivered({"x": ["1"]})
            self.assertEqual(ml.load_delivered(), {"x": ["1"]})


if __name__ == "__main__":
    unittest.main()
