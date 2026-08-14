#!/usr/bin/env python3
"""Unit-Tests für profiles.py (Nutzer, Invites, Pairing-Codes, Migration).

Laufen ohne Netzwerk, jede Testklasse bekommt eine frische Temp-DB:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import profiles  # noqa: E402


class ProfilesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        profiles.DB_PATH = os.path.join(self._tmp.name, "app.db")
        profiles.init_db()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ── User-CRUD + Filter ────────────────────────────────────────────────
    def test_upsert_deep_merge_filters(self) -> None:
        profiles.upsert_user("u1", email="a@b.de", config={"filters": {"max_warm_miete": 1600}})
        profiles.upsert_user("u1", config={"filters": {"min_groesse": 55}})
        p = profiles.get_user("u1")
        self.assertEqual(p.filters, {"max_warm_miete": 1600, "min_groesse": 55})
        self.assertEqual(p.email, "a@b.de")

    def test_update_clear_filters(self) -> None:
        profiles.upsert_user("u1", config={})
        profiles.update_filters("u1", {"max_warm_miete": 1800})
        profiles.update_filters("u1", {"min_zimmer": 2})
        self.assertEqual(profiles.get_user("u1").filters,
                         {"max_warm_miete": 1800, "min_zimmer": 2})
        profiles.clear_filters("u1")
        self.assertEqual(profiles.get_user("u1").filters, {})

    def test_active_and_delete(self) -> None:
        profiles.upsert_user("u1")
        profiles.upsert_user("u2")
        profiles.link_chat("111", "u1")
        profiles.set_active("u1", False)
        self.assertEqual([p.uid for p in profiles.load_active_profiles()], ["u2"])
        profiles.delete_user("u1")
        self.assertIsNone(profiles.get_user("u1"))
        self.assertIsNone(profiles.chat_uid("111"))

    # ── Einladungscodes ───────────────────────────────────────────────────
    def test_invite_single_use_atomic(self) -> None:
        self.assertTrue(profiles.add_invite("INV-1"))
        self.assertFalse(profiles.add_invite("INV-1"))          # Duplikat
        self.assertTrue(profiles.invite_valid("INV-1"))
        self.assertTrue(profiles.use_invite("INV-1", "a@b.de"))  # atomar
        self.assertFalse(profiles.use_invite("INV-1", "x@y.de")) # schon verbraucht
        self.assertFalse(profiles.invite_valid("INV-1"))
        profiles.release_invite("INV-1")                         # freigeben
        self.assertTrue(profiles.invite_valid("INV-1"))

    # ── Telegram-Pairing-Codes ────────────────────────────────────────────
    def test_claim_code_links_and_burns(self) -> None:
        profiles.upsert_user("u1", config={"filters": {"min_groesse": 60}})
        code = profiles.new_pairing_code("u1")
        self.assertEqual(profiles.claim_code(code, "777"), "u1")
        self.assertEqual(profiles.chat_uid("777"), "u1")
        self.assertIsNone(profiles.claim_code(code, "888"))     # verbrannt
        p = profiles.get_user("u1")
        self.assertEqual(p.chat_ids, ["777"])

    def test_claim_code_unknown_or_expired(self) -> None:
        profiles.upsert_user("u1")
        self.assertIsNone(profiles.claim_code("gibtsnicht", "555"))
        code = profiles.new_pairing_code("u1")
        old_ttl = profiles.CODE_TTL_SECONDS
        try:
            profiles.CODE_TTL_SECONDS = 1
            code2 = profiles.new_pairing_code("u1")
            import time
            time.sleep(1.2)
            self.assertIsNone(profiles.claim_code(code2, "999"))  # abgelaufen
        finally:
            profiles.CODE_TTL_SECONDS = old_ttl

    # ── Legacy-Migration / Fallback ───────────────────────────────────────
    def test_recipients_legacy_then_db_wins(self) -> None:
        recv, filt = profiles.recipients_and_filters(
            ["111", "222"], {"111": {"max_warm_miete": 1600}})
        self.assertEqual(sorted(recv), ["111", "222"])
        self.assertEqual(filt["111"], {"max_warm_miete": 1600})
        self.assertEqual(filt["222"], {})
        # DB ist jetzt Quelle: zweiter Aufruf identisch, ohne Env-Abhängigkeit
        recv2, filt2 = profiles.recipients_and_filters(["111", "222"], {})
        self.assertEqual(sorted(recv2), ["111", "222"])
        self.assertEqual(filt2["111"], {"max_warm_miete": 1600})
        # Web-Nutzer mit Telegram-Link läuft dazu
        profiles.upsert_user("user_x", config={"filters": {"min_groesse": 60}})
        profiles.link_chat("333", "user_x")
        recv3, _ = profiles.recipients_and_filters([], {})
        self.assertIn("333", recv3)


if __name__ == "__main__":
    unittest.main()
