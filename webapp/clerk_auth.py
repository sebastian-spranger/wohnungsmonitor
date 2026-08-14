#!/usr/bin/env python3
"""
webapp/clerk_auth.py — Clerk-Login für die server-rendered Web-App
==================================================================

Clerk ist für JS-Frontends gebaut, daher das pragmatische Muster:
ClerkJS (geladen mit dem Publishable-Key) rendert die Sign-in-UI und setzt ein
First-Party-`__session`-Cookie (ein kurzlebiges JWT). Unser Backend verifiziert
das JWT gegen Clerk's PUBLIC JWKS (kein Secret-Key nötig) und liest die E-Mail
des Nutzers.

E-Mail-Auflösung, in dieser Reihenfolge:
  1. ein `email`-Claim auf dem Session-Token — einmalig im Clerk-Dashboard setzen
     (Sessions → Customize session token → `{"email": "{{user.primary_email_address}}"}`).
     Empfohlener Weg: kein Secret auf dem Server.
  2. sonst, wenn CLERK_SECRET_KEY gesetzt ist, über die Clerk Backend API.

Nur der Publishable-Key ist Pflicht; Instanz-Domain (Frontend API / JWKS /
Issuer) wird daraus abgeleitet.
"""

from __future__ import annotations

import base64
import os
from typing import Dict, Optional, Set

import httpx

_jwks_client = None  # gecachter PyJWKClient


def publishable_key() -> str:
    return os.environ.get("CLERK_PUBLISHABLE_KEY", "").strip()


def configured() -> bool:
    return bool(publishable_key())


def frontend_api() -> str:
    """Clerk-Frontend-API-Host, dekodiert aus dem Publishable-Key
    (`pk_test_<base64(host + "$")>`)."""
    pk = publishable_key()
    if not pk:
        return ""
    b64 = pk.split("_", 2)[-1]
    try:
        dec = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode()
    except Exception:
        return ""
    return dec.rstrip("$")


def issuer() -> str:
    api = frontend_api()
    return f"https://{api}" if api else ""


def jwks_url() -> str:
    api = frontend_api()
    return f"https://{api}/.well-known/jwks.json" if api else ""


def clerk_js_url() -> str:
    api = frontend_api()
    return f"https://{api}/npm/@clerk/clerk-js@5/dist/clerk.browser.js" if api else ""


def _client():
    global _jwks_client
    if _jwks_client is None:
        from jwt import PyJWKClient
        _jwks_client = PyJWKClient(jwks_url())
    return _jwks_client


def _allowed_origins() -> Set[str]:
    """Unsere eigenen Web-Origins — der `azp` (authorized party) eines Clerk
    `__session`-Tokens muss einer davon sein, damit ein für EINE ANDERE Seite
    derselben Clerk-Instanz ausgestelltes Token hier abgelehnt wird."""
    origins: Set[str] = set()
    base = os.environ.get("BASE_URL", "").strip().rstrip("/")
    if base:
        origins.add(base)
    for extra in os.environ.get("CLERK_ALLOWED_ORIGINS", "").split(","):
        extra = extra.strip().rstrip("/")
        if extra:
            origins.add(extra)
    return origins


def verify_session_token(token: str) -> Optional[Dict]:
    """Clerk-`__session`-JWT über die public JWKS verifizieren. Liefert die
    Claims oder None bei ungültig/abgelaufen/für andere Origin. Kein Secret
    nötig."""
    if not token or not configured():
        return None
    try:
        import jwt
        signing_key = _client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, signing_key, algorithms=["RS256"],
                            issuer=issuer(), leeway=10,
                            options={"verify_aud": False})
    except Exception:
        return None
    # Origin-Bindung: trägt das Token einen authorized-party, muss er uns gehören.
    azp = (claims.get("azp") or "").strip().rstrip("/")
    allowed = _allowed_origins()
    if azp and allowed and azp not in allowed:
        return None
    return claims


def resolve_email(claims: Dict) -> Optional[str]:
    """E-Mail aus dem Session-Token-`email`-Claim (bevorzugt) oder, als
    Fallback, über die Clerk Backend API (braucht CLERK_SECRET_KEY). Ein
    vorhandenes, aber falsches `email_verified` wird abgelehnt (nie einer
    unverifizierten Adresse trauen)."""
    ev = claims.get("email_verified")
    if ev is False or (isinstance(ev, str) and ev.strip().lower() in ("false", "0", "no")):
        return None
    email = (claims.get("email") or "").strip().lower()
    if email:
        return email
    sub = claims.get("sub")
    secret = os.environ.get("CLERK_SECRET_KEY", "").strip()
    if sub and secret:
        try:
            r = httpx.get(f"https://api.clerk.com/v1/users/{sub}",
                          headers={"Authorization": f"Bearer {secret}"}, timeout=15)
            if r.status_code == 200:
                for e in r.json().get("email_addresses", []):
                    addr = (e.get("email_address") or "").strip().lower()
                    if addr:
                        return addr
        except Exception:
            pass
    return None
