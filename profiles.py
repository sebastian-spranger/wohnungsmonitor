#!/usr/bin/env python3
"""
profiles.py — Nutzer-, Einladungs- und Telegram-Verknüpfungs-Store
==================================================================

Macht aus dem Single-Recipient-Wohnungsmonitor ein kleines Multi-User-System:

  * SQLite (`data/app.db`) speichert Accounts + einen JSON-`config`-Blob pro
    Nutzer (u.a. die Wohnungs-Filter), Telegram-Chat-Verknüpfungen sowie
    Einladungs- und Pairing-Codes.
  * Wer sich per Clerk (Google) anmeldet, wird über die uid = Clerk-`sub`
    identifiziert; die Telegram-Chat-IDs werden über `/start <code>`-Pairing
    (reg_codes) an den Account gehängt.
  * Backward-Kompatibilität: Solange KEIN Nutzer in der DB existiert, fällt der
    Monitor auf das alte Verhalten (TELEGRAM_CHAT_IDS-Env) zurück — ein frisches
    Deployment verhält sich exakt wie vorher, bis der erste Nutzer onboarded.

Dieses Modul ist PURELY ADDITIVE und importiert nichts aus monitor_lite — der
Monitor importiert uns.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

DB_PATH    = os.environ.get("APP_DB", "data/app.db")
# Telegram-Link-Codes verfallen schnell — das Dashboard münzt bei jedem Aufruf
# einen frischen Code, ein kurzes Fenster begrenzt den Schaden eines geleakten
# Codes. (LINK_CODE_TTL in Sekunden, Default 30 Min.)
CODE_TTL_SECONDS = int(os.environ.get("LINK_CODE_TTL", "1800"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Profile ────────────────────────────────────────────────────────────────

@dataclass
class Profile:
    """Ein Nutzer. `config` ist der JSON-Blob; `filters` darin trägt die
    Wohnungs-Suchfilter (siehe DEFAULT_FILTER in monitor_lite.py)."""
    uid: str
    config: Dict[str, Any] = field(default_factory=dict)
    active: bool = True
    chat_ids: Optional[List[str]] = None

    def __post_init__(self) -> None:
        if self.chat_ids is None:
            self.chat_ids = []

    # Identität
    @property
    def email(self) -> str:
        return self.config.get("email", "")

    @property
    def name(self) -> str:
        return self.config.get("name", "")

    # Wohnungs-Filter (Schlüssel wie DEFAULT_FILTER in monitor_lite.py)
    @property
    def filters(self) -> Dict[str, Any]:
        return self.config.get("filters", {})

    def filters_json(self) -> str:
        return json.dumps(self.filters, ensure_ascii=False)

    @property
    def created_at(self) -> str:
        return self.config.get("created_at", "")


# ── DB-Zugriff ─────────────────────────────────────────────────────────────

@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """Connection mit Transaktions-Handling (commit/rollback) — und wird IMMER
    geschlossen (kein Leck im 90s-Engine-Loop / Webapp-Betrieb)."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                uid        TEXT PRIMARY KEY,   -- Clerk sub (oder Legacy-Chat-ID)
                email      TEXT,
                name       TEXT,
                active     INTEGER NOT NULL DEFAULT 1,
                config     TEXT NOT NULL DEFAULT '{}',
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS invites (
                code     TEXT PRIMARY KEY,
                used     INTEGER NOT NULL DEFAULT 0,
                used_by  TEXT,
                ts       TEXT
            );
            CREATE TABLE IF NOT EXISTS reg_codes (
                code TEXT PRIMARY KEY,
                uid  TEXT,
                used INTEGER NOT NULL DEFAULT 0,
                ts   TEXT
            );
            CREATE TABLE IF NOT EXISTS telegram_links (
                chat_id TEXT PRIMARY KEY,
                uid     TEXT NOT NULL,
                ts      TEXT
            );
            """
        )


def db_exists() -> bool:
    return os.path.exists(DB_PATH)


def _row_to_profile(row: sqlite3.Row, links: List[str]) -> Profile:
    return Profile(
        uid=row["uid"],
        config=json.loads(row["config"] or "{}"),
        active=bool(row["active"]),
        chat_ids=links,
    )


def _links_for(c: sqlite3.Connection, uid: str) -> List[str]:
    rows = c.execute(
        "SELECT chat_id FROM telegram_links WHERE uid=?", (uid,)
    ).fetchall()
    return [r["chat_id"] for r in rows]


# ── User-CRUD ──────────────────────────────────────────────────────────────

def upsert_user(uid: str, email: str = "", name: str = "",
                config: Optional[Dict[str, Any]] = None,
                active: bool = True) -> None:
    """Nutzer anlegen oder aktualisieren. `config` wird gemerged (bestehende
    Schlüssel bleiben erhalten, z.B. filters), sofern nicht überschrieben."""
    init_db()
    now = _now()
    with _conn() as c:
        row = c.execute("SELECT config, created_at FROM users WHERE uid=?",
                        (uid,)).fetchone()
        if row:
            merged = json.loads(row["config"] or "{}")
            if config:
                # Tiefes Merge nur für den Filter-Blob (einzelne Felder
                # überschreiben, bestehende behalten) — alles andere flach.
                new_filters = config.get("filters") or {}
                old_filters = merged.get("filters") or {}
                if new_filters:
                    merged["filters"] = {**old_filters, **new_filters}
                for k, v in config.items():
                    if k != "filters":
                        merged[k] = v
            merged["updated_at"] = now
            c.execute(
                "UPDATE users SET email=?, name=?, active=?, config=?, updated_at=? WHERE uid=?",
                (email or merged.get("email", ""), name or merged.get("name", ""),
                 int(active), json.dumps(merged, ensure_ascii=False), now, uid),
            )
        else:
            cfg = dict(config or {})
            cfg.setdefault("email", email)
            cfg.setdefault("name", name)
            cfg["created_at"] = now
            cfg["updated_at"] = now
            c.execute(
                "INSERT INTO users (uid, email, name, active, config, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (uid, email, name, int(active), json.dumps(cfg, ensure_ascii=False), now, now),
            )


def get_user(uid: str) -> Optional[Profile]:
    if not db_exists():
        return None
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
        if not row:
            return None
        return _row_to_profile(row, _links_for(c, uid))


def set_active(uid: str, active: bool) -> None:
    init_db()
    with _conn() as c:
        c.execute("UPDATE users SET active=?, updated_at=? WHERE uid=?",
                  (int(active), _now(), uid))


def delete_user(uid: str) -> None:
    init_db()
    with _conn() as c:
        c.execute("DELETE FROM telegram_links WHERE uid=?", (uid,))
        c.execute("DELETE FROM reg_codes WHERE uid=?", (uid,))
        c.execute("DELETE FROM users WHERE uid=?", (uid,))


def load_active_profiles() -> List[Profile]:
    """Alle aktiven Nutzer inklusive ihrer Telegram-Chat-Verknüpfungen."""
    if not db_exists():
        return []
    with _conn() as c:
        rows = c.execute("SELECT * FROM users WHERE active=1").fetchall()
        return [_row_to_profile(r, _links_for(c, r["uid"])) for r in rows]


def list_users() -> List[Dict[str, Any]]:
    """Übersicht für die Admin-CLI (ohne den vollen config-Blob)."""
    if not db_exists():
        return []
    with _conn() as c:
        rows = c.execute("SELECT uid, email, name, active, created_at FROM users").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["chat_ids"] = _links_for(c, r["uid"])
            d["active"] = bool(d["active"])
            out.append(d)
        return out


# ── Telegram-Chat-Verknüpfung ─────────────────────────────────────────────

def link_chat(chat_id: str, uid: str) -> None:
    """Chat-ID an einen Nutzer hängen (idempotent)."""
    init_db()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO telegram_links (chat_id, uid, ts) VALUES (?,?,?)",
            (str(chat_id), uid, _now()),
        )


def unlink_chat(chat_id: str, uid: str) -> None:
    init_db()
    with _conn() as c:
        c.execute("DELETE FROM telegram_links WHERE chat_id=? AND uid=?",
                  (str(chat_id), uid))


def chat_uid(chat_id: str) -> Optional[str]:
    """Welchem Nutzer gehört diese Chat-ID? (None = Legacy/unbekannt)"""
    if not db_exists():
        return None
    with _conn() as c:
        row = c.execute("SELECT uid FROM telegram_links WHERE chat_id=?",
                        (str(chat_id),)).fetchone()
        return row["uid"] if row else None


def profile_for_chat(chat_id: str) -> Optional[Profile]:
    uid = chat_uid(chat_id)
    return get_user(uid) if uid else None


# ── Einladungscodes ────────────────────────────────────────────────────────

INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # ohne 0/O, 1/I/L


def generate_invite_code(prefix: str = "WOHN") -> str:
    """Lesbaren Einladungscode erzeugen (z.B. WOHN-X7K2MP). Wird von der
    Admin-Webseite UND der Admin-CLI (scripts/invites.py) genutzt."""
    body = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(6))
    return f"{prefix}-{body}"


def add_invite(code: str) -> bool:
    """Einen neuen Einladungscode anlegen. False, wenn er schon existiert."""
    init_db()
    with _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO invites (code, used, ts) VALUES (?,0,?)",
            (code, _now()),
        )
        return cur.rowcount > 0


def invite_valid(code: str) -> bool:
    if not db_exists():
        return False
    with _conn() as c:
        row = c.execute("SELECT used FROM invites WHERE code=?", (code,)).fetchone()
        return bool(row) and not row["used"]


def use_invite(code: str, email: str = "") -> bool:
    """Einladung ATOMAR konsumieren (single-use). False, wenn unbekannt oder
    schon benutzt — schützt vor Races, die aus einem Code mehrere Accounts
    erzeugen würden."""
    init_db()
    with _conn() as c:
        cur = c.execute(
            "UPDATE invites SET used=1, used_by=?, ts=? WHERE code=? AND used=0",
            (email, _now(), code),
        )
        return cur.rowcount > 0


def release_invite(code: str) -> None:
    """Konsumierten Code wieder freigeben (z.B. wenn das Onboarding danach
    fehlschlägt — der Einladungscode des Nutzers soll nicht verbrennen)."""
    init_db()
    with _conn() as c:
        c.execute("UPDATE invites SET used=0, used_by=NULL, ts=? WHERE code=?",
                  (_now(), code))


def list_invites() -> List[Dict[str, Any]]:
    if not db_exists():
        return []
    with _conn() as c:
        rows = c.execute("SELECT code, used, used_by, ts FROM invites "
                         "ORDER BY ts DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["used"] = bool(d["used"])
            out.append(d)
        return out


# ── Telegram-Pairing-Codes ────────────────────────────────────────────────

def add_code(code: str, uid: str) -> None:
    """Pairing-Code an einen Nutzer binden. Das Dashboard münzt pro Aufruf
    einen frischen Code (INSERT OR REPLACE lässt alte Codes desselben Nutzers
    verfallen)."""
    init_db()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO reg_codes (code, uid, used, ts) VALUES (?,?,0,?)",
            (code, uid, _now()),
        )


def new_pairing_code(uid: str) -> str:
    """Frischen Pairing-Code für den Dashboard-Button erzeugen."""
    code = secrets.token_urlsafe(8)
    add_code(code, uid)
    return code


def claim_code(code: str, chat_id: str) -> Optional[str]:
    """Redeem einen gültigen, unbenutzten, NICHT abgelaufenen Pairing-Code:
    verknüpft `chat_id` mit dem Nutzer des Codes, brennt den Code und liefert
    die uid. None bei unbekannt/benutzt/abgelaufen/ungebunden."""
    if not db_exists():
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT uid, used, ts FROM reg_codes WHERE code=?", (code,)
        ).fetchone()
        if not row or row["used"] or not row["uid"]:
            return None
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(row["ts"])).total_seconds()
            if age > CODE_TTL_SECONDS:
                return None
        except Exception:
            pass  # nicht parsebares ts -> nur beim Alter großzügig sein
        # Atomar brennen (AND used=0 + rowcount): zwei gleichzeitige Claims
        # desselben Codes können so NIE beide durchkommen.
        cur = c.execute(
            "UPDATE reg_codes SET used=1, ts=? WHERE code=? AND used=0",
            (_now(), code),
        )
        if cur.rowcount != 1:
            return None
        uid = row["uid"]
    link_chat(str(chat_id), uid)
    return uid


# ── Filter aktualisieren (aus Bot/Web-UI) ─────────────────────────────────

def update_filters(uid: str, filters: Dict[str, Any]) -> None:
    """Filter eines Nutzers mergen (einzelne Felder überschreiben, Rest bleibt)."""
    upsert_user(uid, config={"filters": dict(filters)})


def clear_filters(uid: str) -> None:
    """Alle eigenen Filter löschen -> zurück auf Standard (DEFAULT_FILTER)."""
    init_db()
    with _conn() as c:
        row = c.execute("SELECT config FROM users WHERE uid=?", (uid,)).fetchone()
        if not row:
            return
        cfg = json.loads(row["config"] or "{}")
        cfg.pop("filters", None)
        cfg["updated_at"] = _now()
        c.execute("UPDATE users SET config=?, updated_at=? WHERE uid=?",
                  (json.dumps(cfg, ensure_ascii=False), _now(), uid))


# ── Legacy-Fallback & Migration ───────────────────────────────────────────
# Solange die DB keine Nutzer kennt, verhält sich der Monitor EXAKT wie vorher
# (TELEGRAM_CHAT_IDS-Env + user_filters.json). Beim ersten DB-losen Lauf werden
# diese Empfänger einmalig als Legacy-Profile in die DB übernommen — danach ist
# die DB die einzige Wahrheit und Web-Onboardings laufen dazu.

def sync_legacy_recipients(env_chat_ids: List[str],
                           legacy_user_filters: Optional[Dict[str, Any]] = None) -> None:
    """Einmalige Migration von env-Chat-IDs + user_filters.json in die DB.
    Idempotent; tut nichts, sobald die DB bereits aktive Nutzer hat."""
    if load_active_profiles():
        return
    legacy = legacy_user_filters or {}
    ids = {str(c) for c in env_chat_ids} | {str(c) for c in legacy}
    for chat_id in sorted(ids):
        if chat_uid(chat_id) is None:
            uid = "legacy:" + chat_id
            filt = dict(legacy.get(chat_id) or {})
            upsert_user(uid, email="", name="Legacy " + chat_id,
                        config={"filters": filt})
            link_chat(chat_id, uid)


def recipients_and_filters(env_chat_ids: List[str],
                           legacy_user_filters: Optional[Dict[str, Any]] = None
                           ) -> "tuple[List[str], Dict[str, Dict[str, Any]]]":
    """Empfänger + Filter für den Monitorlauf.
    Gibt (recipients, chat_filters) zurück: Chat-IDs, die Benachrichtigungen
    bekommen, und ihre effektiven eigenen Filter (chat_id -> filter-dict).
    DB-Profile mit verknüpften Chats gewinnen; ohne DB-Nutzer greift der
    Legacy-Pfad (env + user_filters.json) mit einmaliger Migration."""
    pro = load_active_profiles()
    if not pro:
        sync_legacy_recipients(env_chat_ids, legacy_user_filters or {})
        pro = load_active_profiles()
    recipients: List[str] = []
    chat_filters: Dict[str, Dict[str, Any]] = {}
    for p in pro:
        for cid in p.chat_ids:
            recipients.append(cid)
            chat_filters.setdefault(cid, dict(p.filters))
    return recipients, chat_filters
