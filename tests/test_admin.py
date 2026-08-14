#!/usr/bin/env python3
"""Tests für das Admin-Panel (/admin, Codes erzeugen/widerrufen, Metriken)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Muss VOR dem Import von webapp.main gesetzt sein (wird beim Import gelesen)
os.environ["ALLOW_DEV_LOGIN"] = "1"
os.environ["BASE_URL"] = "http://localhost:8000"
os.environ["SESSION_SECRET"] = "dev-secret"
os.environ["CLERK_PUBLISHABLE_KEY"] = ""

import profiles  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from webapp.main import app, ADMIN_EMAILS  # noqa: E402


class AdminTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        profiles.DB_PATH = os.path.join(self._tmp.name, "app.db")
        profiles.init_db()
        # State-Dateien für die Metriken in den Test-CWD legen
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        Path("seen.json").write_text(json.dumps({"ids": ["a", "b", "c"]}))
        Path("delivered.json").write_text(json.dumps({"a": ["1"], "b": ["1", "2"]}))
        Path("matches.json").write_text(json.dumps([
            {"titel": "Wohnung 1", "preis_warm": 1500, "groesse": 55,
             "quelle": "ImmoWelt", "url": "https://x.de/1",
             "gefunden_um": "14.08. 10:00"},
            {"titel": "Wohnung 2", "preis_warm": 1800, "groesse": 70,
             "quelle": "Kleinanzeigen", "url": "https://x.de/2",
             "gefunden_um": "14.08. 11:00"},
        ]))
        self.client.cookies.clear()

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_admin_braucht_login(self) -> None:
        r = self.client.get("/admin", follow_redirects=False)
        self.assertEqual(r.status_code, 303)

    def test_nicht_admin_bekommt_403(self) -> None:
        self.client.post("/login/dev", data={"email": "finn@example.de"})
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 403)

    def test_admin_sieht_panel_und_metriken(self) -> None:
        self.client.post("/login/dev", data={"email": "sebastianspranger699@gmail.com"})
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Admin-Panel", r.text)
        self.assertIn("Matches gesamt", r.text)
        self.assertIn("2", r.text)                    # matches gesamt
        self.assertIn("ImmoWelt: 1", r.text)          # nach Quelle
        self.assertIn("Kleinanzeigen: 1", r.text)
        self.assertIn("Zustellungen", r.text)
        # Admin-Link in der Topbar
        self.assertIn('href="/admin"', r.text)

    def test_codes_erzeugen_und_widerrufen(self) -> None:
        self.client.post("/login/dev", data={"email": "sebastianspranger699@gmail.com"})
        r = self.client.post("/admin/codes", data={"count": "2", "prefix": "WOHN"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("2 Code(s) erzeugt", r.text)
        invites = profiles.list_invites()
        self.assertEqual(len(invites), 2)
        self.assertTrue(all(i["code"].startswith("WOHN-") and not i["used"] for i in invites))
        # widerrufen
        code = invites[0]["code"]
        r = self.client.post("/admin/revoke", data={"code": code.lower()})
        self.assertIn("widerrufen", r.text)
        self.assertIn(code, [i["code"] for i in profiles.list_invites()])
        self.assertTrue(profiles.list_invites()[0]["used"])


if __name__ == "__main__":
    unittest.main()
