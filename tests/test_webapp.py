#!/usr/bin/env python3
"""Smoke-Test für die Web-App (FastAPI + Dev-Login).

Braucht die Webapp-Dependencies (fastapi, python-multipart, itsdangerous,
pyjwt) — unter .venv nach `pip install -r requirements.txt`.
Läuft komplett offline mit dem lokalen Dev-Login.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# MUSS vor dem Import von webapp.main gesetzt sein (wird beim Import gelesen)
os.environ["ALLOW_DEV_LOGIN"] = "1"
os.environ["BASE_URL"] = "http://localhost:8000"
os.environ["SESSION_SECRET"] = "dev-secret"
os.environ["CLERK_PUBLISHABLE_KEY"] = ""

import profiles  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from webapp.main import app  # noqa: E402


class WebappTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        profiles.DB_PATH = os.path.join(self._tmp.name, "app.db")
        profiles.init_db()
        # frische Session pro Test
        self.client.cookies.clear()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_login_required(self) -> None:
        r = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/")

    def test_full_onboarding_flow(self) -> None:
        # Dev-Login -> noch kein Profil -> /onboard
        r = self.client.post("/login/dev", data={"email": "anna@example.de"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.client.get("/dashboard", follow_redirects=False)
                         .headers["location"], "/onboard")
        # ungültiger Invite
        r = self.client.post("/onboard", data={"invite": "FALSCH"})
        self.assertIn("Ungültig", r.text)
        # gültiger Invite -> Profil + Dashboard mit Telegram-Button
        profiles.add_invite("WOHN-TEST1")
        r = self.client.post("/onboard", data={"invite": "WOHN-TEST1"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        r = self.client.get("/dashboard")
        self.assertIn("Telegram verknüpfen", r.text)
        self.assertIn("t.me/Noapartmentsbot?start=", r.text)
        # Code ist verbraucht
        self.assertFalse(profiles.use_invite("WOHN-TEST1", "x"))
        # Filter speichern (deutsche Zahlen + Bezirks-Normalisierung)
        self.client.post("/settings", data={
            "max_warm_miete": "1600", "max_kalt_miete": "",
            "min_groesse": "55", "min_zimmer": "2",
            "max_radius_km": "3,5",
            "bezirke_erlaubt": " Schwabing, maxvorstadt, schwabing ",
        })
        p = profiles.get_user("dev:anna@example.de")
        self.assertEqual(p.filters["max_warm_miete"], 1600.0)
        self.assertEqual(p.filters["max_radius_km"], 3.5)
        self.assertEqual(p.filters["bezirke_erlaubt"], ["schwabing", "maxvorstadt"])
        # vorausgefülltes Formular
        r = self.client.get("/dashboard")
        self.assertIn('value="1600"', r.text)
        # Pause / Resume
        self.client.post("/pause")
        self.assertFalse(profiles.get_user("dev:anna@example.de").active)
        self.client.post("/resume")
        self.assertTrue(profiles.get_user("dev:anna@example.de").active)

    def test_healthz(self) -> None:
        r = self.client.get("/healthz")
        self.assertTrue(r.json()["ok"])
        self.assertTrue(r.json()["dev_login"])


if __name__ == "__main__":
    unittest.main()
