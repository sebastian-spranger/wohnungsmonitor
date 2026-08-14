#!/usr/bin/env python3
"""
scripts/invites.py — Admin-CLI für Einladungscodes & Nutzerübersicht
====================================================================

Der Betreiber erzeugt damit die Einladungscodes, die er persönlich weitergibt.
Codes sind single-use und werden beim Onboarding atomar verbraucht.

Beispiele (aus dem Repo-Wurzelverzeichnis, mit aktivem venv):
    python scripts/invites.py new --count 3
    python scripts/invites.py new --count 2 --prefix SOMMER
    python scripts/invites.py list
    python scripts/invites.py revoke WOHN-X7K2MP

Die SQLite-DB liegt unter data/app.db (überschreibbar via APP_DB-Env).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Damit `import profiles` vom Repo-Root aus funktioniert, egal wo die CLI
# aufgerufen wird.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import profiles  # noqa: E402

# Lesbares Alphabet ohne verwechselbare Zeichen (0/O, 1/I/l)
_DEFAULT_PREFIX = "WOHN"


def _code(prefix: str) -> str:
    return profiles.generate_invite_code(prefix)


def cmd_new(args: argparse.Namespace) -> int:
    codes: list[str] = []
    for _ in range(args.count):
        code = _code(args.prefix)
        if profiles.add_invite(code):
            codes.append(code)
    if not codes:
        print("❌ Keine Codes angelegt (Prefix evtl. kollidiert?).")
        return 1
    print(f"✅ {len(codes)} Einladungscode(s) angelegt:\n")
    for c in codes:
        print(f"   {c}")
    print("\nEinfach so weitergeben — jeder Code gilt genau einmal.")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    invites = profiles.list_invites()
    print(f"Einladungscodes ({len(invites)}):")
    if not invites:
        print("   (keine — erzeuge welche mit: python scripts/invites.py new)")
    for inv in invites:
        status = "benutzt" if inv["used"] else "offen"
        who = f" durch {inv['used_by']}" if inv.get("used_by") else ""
        print(f"   {inv['code']:<16} {status:<8}{who}")
    print()
    users = profiles.list_users()
    print(f"Nutzer ({len(users)}):")
    if not users:
        print("   (noch keine Registrierungen)")
    for u in users:
        status = "aktiv" if u["active"] else "pausiert"
        chats = ", ".join(u["chat_ids"]) or "–"
        print(f"   {u['uid']:<24} {u['email'] or '–':<32} {status:<8} Telegram: {chats}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    # Revoke = Code als verbraucht markieren (kann nie mehr eingelöst werden).
    if profiles.use_invite(args.code, email="(revoked)"):
        print(f"🔴 Code {args.code} widerrufen.")
        return 0
    print(f"⚠  Code {args.code} nicht gefunden oder bereits benutzt.")
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description="Wohnungsmonitor-Admin: Einladungscodes")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new", help="Neue Einladungscodes erzeugen")
    p_new.add_argument("--count", type=int, default=1, help="Anzahl (Default 1)")
    p_new.add_argument("--prefix", default=_DEFAULT_PREFIX, help="Code-Präfix (Default WOHN)")
    p_new.set_defaults(func=cmd_new)

    sub.add_parser("list", help="Codes + Nutzer anzeigen").set_defaults(func=cmd_list)

    p_rev = sub.add_parser("revoke", help="Code widerrufen")
    p_rev.add_argument("code")
    p_rev.set_defaults(func=cmd_revoke)

    args = p.parse_args()
    if args.cmd == "new" and args.count < 1:
        print("--count muss >= 1 sein.")
        return 1
    if not os.path.isdir(os.path.dirname(profiles.DB_PATH) or "."):
        print(f"⚠  DB-Verzeichnis existiert noch nicht: {os.path.dirname(profiles.DB_PATH)}")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
