#!/usr/bin/env python3
"""
Wohnungsmonitor Lite – Single-Run Version für GitHub Actions.
Portale via httpx: Kleinanzeigen, WG-Gesucht, Wohnungsboerse, ImmoWelt, Mr. Lodge, Wunderflats.
Portale via Browser (Playwright, optional): ImmobilienScout24, HousingAnywhere.
In CI läuft Chromium headful über xvfb – nur so umgeht IS24 die Bot-Sperre.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

# Multi-User-Store: Nutzer, Einladungen, Telegram-Verknüpfungen (SQLite).
# Solange die DB keine Nutzer kennt, verhält sich alles wie vorher (Env-IDs +
# user_filters.json) – siehe profiles.recipients_and_filters().
import profiles

# Playwright ist optional: nur für die JS-/Bot-geschützten Portale (IS24, HousingAnywhere).
# Fehlt es (z.B. lokaler Schnell-Lauf ohne Browser), laufen einfach nur die httpx-Portale.
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

# ─── Konfiguration ────────────────────────────────────────────────────────────
# Werte kommen aus GitHub Secrets (nie im Code speichern!)
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_IDS = [cid.strip() for cid in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

MAX_WARM_MIETE   = int(os.environ.get("MAX_WARM_MIETE",   "2200"))
# Sicherheitsabschlag: zeigt ein Inserat nur die Kaltmiete (oder unklar),
# liegt die echte Warmmiete meist 15-20% höher → niedrigeres Limit anwenden.
MAX_KALT_MIETE   = int(os.environ.get("MAX_KALT_MIETE",   "1950"))
MIN_GROESSE      = int(os.environ.get("MIN_GROESSE",      "45"))
MAX_FAHRTZEIT    = int(os.environ.get("MAX_FAHRTZEIT",    "20"))   # Minuten zur TUM
# Geografischer Filter: nur Wohnungen im Umkreis um die Innenstadt (Marienplatz)
MAX_RADIUS_KM    = float(os.environ.get("MAX_RADIUS_KM",  "4.0"))  # km Luftlinie
# Spätestes Einzugs-/Verfügbarkeitsdatum (ISO). Inserate mit erkennbar späterem
# "Verfügbar ab"-Datum werden verworfen. Unbekannte Verfügbarkeit bleibt drin.
VERFUEGBAR_BIS   = os.environ.get("VERFUEGBAR_BIS", "2026-07-15")

# Always-on-Modus (--serve, z.B. auf der Oracle-VM): Pause zwischen Läufen
SERVE_INTERVAL   = int(os.environ.get("SERVE_INTERVAL", "90"))   # Sekunden

# Standard-Filter für Empfänger ohne eigene Einstellungen (Momentaufnahme der
# Basiswerte, BEVOR sie unten pro Lauf für den breiten Scrape-Filter aufgeweitet
# werden – siehe user_filters/DEFAULT_FILTER-Nutzung in main()).
DEFAULT_FILTER = {
    "max_warm_miete": MAX_WARM_MIETE,
    "max_kalt_miete": MAX_KALT_MIETE,
    "min_groesse": MIN_GROESSE,
    "min_zimmer": 0.0,
    "max_radius_km": MAX_RADIUS_KM,
    "bezirke_erlaubt": None,
}

SEEN_FILE           = Path("seen.json")
MATCHES_FILE        = Path("matches.json")
USER_FILTERS_FILE   = Path("user_filters.json")
TELEGRAM_OFFSET_FILE = Path("telegram_offset.json")
FAHRTZEIT_CACHE_FILE = Path("fahrtzeit_cache.json")
GEOCODE_CACHE_FILE   = Path("geocode_cache.json")

# TUM Hauptcampus Arcisstraße 21 (Länge:Breite:WGS84)
TUM_COORD = "11.568290:48.149640:WGS84"
# Innenstadt-Zentrum = Marienplatz (Breite, Länge)
ZENTRUM_LAT, ZENTRUM_LON = 48.137154, 11.575924

# Fallback, falls der Geocoder nicht erreichbar ist: zentrale Stadtteile
# (~2 km um den Marienplatz). Nur dann genutzt, wenn keine Koordinaten kommen.
ZENTRAL_KEYWORDS = {
    "altstadt", "lehel", "maxvorstadt", "maximilian", "ludwigsvorstadt",
    "isarvorstadt", "glockenbach", "gärtnerplatz", "theresienwiese",
    "schwanthalerhöhe", "westend", "au-haidhausen", "haidhausen",
    "angerviertel", "kreuzviertel", "hackenviertel", "graggenau",
}

BLOCKED_KEYWORDS = {
    "feldmoching", "hasenbergl", "am hart", "moosach", "pasing", "aubing",
    "lochhausen", "langwied", "riem", "trudering", "neuperlach",
    "solln", "forstenried", "fürstenried", "allach", "untermenzing",
    "obermenzing", "daglfing", "johanneskirchen", "haar", "unterhaching",
    "oberhaching", "pullach", "taufkirchen", "grünwald", "garching",
    "unterschleißheim", "dachau", "olching", "germering", "planegg",
    "gräfelfing", "gauting", "aschheim", "kirchheim", "ismaning",
}

# Tauschwohnungen und Gesuche (kein echtes Angebot) rausfiltern
BLOCKED_TITLE_KEYWORDS = {
    "tausch", "tausche", "wohnungstausch", "tauschwohnung",
    "swap", "wohnungsswap", "apartment swap",
    "suche wohnung", "suche eine wohnung", "suche dringend wohnung",
    "suche 1-zimmer", "suche 2-zimmer", "suche 3-zimmer",
    "suche apartment", "wir suchen", "ich suche",
    "biete tausch", "biete wg-zimmer",
}

# Inserate im Ausland / fremden Städten, die durch den Orts-Filter rutschen
AUSLAND_KEYWORDS = {
    "bulgarien", "varna", "sonnenstrand", "spanien", "mallorca", "italien",
    "kroatien", "türkei", "ungarn", "österreich", "portugal", "griechenland",
    "thailand", "ägypten", "dubai", "polen", "tschechien",
}

TOPLAGE = {
    "schwabing", "maxvorstadt", "glockenbachviertel", "glockenbach",
    "haidhausen", "au-haidhausen", "isarvorstadt", "lehel", "au ",
    "neuhausen", "nymphenburg", "bogenhausen", "westend", "ludwigsvorstadt",
}
GUTE_LAGE = {
    "obergiesing", "untergiesing", "giesing", "ramersdorf",
    "schwabing-west", "sendling", "schwanthalerhöhe", "maximilianvorstadt",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# ─── Datenmodell ─────────────────────────────────────────────────────────────

def _md_escape(s: str) -> str:
    """Telegram-Markdown (legacy) Sonderzeichen escapen. Titel/Adressen kommen
    von den Portal-Websites — ohne Escaping würde z.B. ein `_` im Titel die
    sendMessage mit HTTP 400 scheitern lassen (Match geht verloren)."""
    for ch in "\\_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, "\\" + ch)
    return s


@dataclass
class Wohnung:
    id: str
    titel: str
    preis_warm: float
    groesse: float
    zimmer: float
    url: str
    quelle: str
    adresse: str = ""
    moebliert: bool = False
    mit_kueche: bool = False
    gefunden_um: str = ""
    fahrtzeit_min: int | None = None
    preis_ist_warm: bool = False   # Preis erkennbar Warmmiete (inkl. NK)
    preis_ist_kalt: bool = False   # Preis erkennbar Kaltmiete (zzgl. NK)
    entfernung_km: float | None = None   # Luftlinie zum Marienplatz (None = noch nicht geprüft)
    verfuegbar_ab: str = ""               # ISO-Datum "YYYY-MM-DD" oder "" (unbekannt)

    def passt(self, filt: dict | None = None) -> tuple[bool, list[str]]:
        """Prüft gegen einen Filter. Ohne Argument gelten die aktuellen globalen
        Konstanten (ggf. für den breiten Scrape-Pass aufgeweitet); mit einem
        expliziten filt-dict (siehe DEFAULT_FILTER) gilt dieser statt der Globals –
        so bekommt jeder Telegram-Empfänger seine eigenen Grenzen geprüft."""
        f = filt if filt is not None else {}
        max_warm = f.get("max_warm_miete", MAX_WARM_MIETE)
        max_kalt = f.get("max_kalt_miete", MAX_KALT_MIETE)
        min_groesse = f.get("min_groesse", MIN_GROESSE)
        min_zimmer = f.get("min_zimmer") or 0
        max_radius = f.get("max_radius_km", MAX_RADIUS_KM)
        bezirke_erlaubt = f.get("bezirke_erlaubt") or None

        fails = []
        if self.preis_warm == 0 and self.groesse == 0:
            fails.append("Kein Preis und keine Größe – kein echtes Inserat")
        if self.preis_warm > 0:
            # klar kalt → strenger (Warm ≈ +NK); warm oder unklar → großzügig (nichts verpassen)
            if self.preis_ist_kalt and not self.preis_ist_warm:
                limit, label = max_kalt, "kalt"
            else:
                limit = max_warm
                label = "warm" if self.preis_ist_warm else "unklar"
            if self.preis_warm > limit:
                fails.append(f"Preis {self.preis_warm:.0f}€ ({label}) > {limit}€")
        if self.groesse > 0 and self.groesse < min_groesse:
            fails.append(f"Größe {self.groesse:.0f}qm < {min_groesse}qm")
        if self.zimmer > 0 and min_zimmer and self.zimmer < min_zimmer:
            fails.append(f"{self.zimmer:.1f} Zimmer < {min_zimmer} gewünscht")
        combined = (self.titel + " " + self.adresse).lower()
        if self.entfernung_km is not None and self.entfernung_km > max_radius:
            fails.append(f"{self.entfernung_km:.1f} km vom Zentrum > {max_radius:.0f} km")
        if bezirke_erlaubt and not any(b in combined for b in bezirke_erlaubt):
            fails.append(f"Nicht in Wunschlage ({', '.join(bezirke_erlaubt)})")
        ausland = next((a for a in AUSLAND_KEYWORDS if a in combined), None)
        if ausland:
            fails.append(f"Nicht München ('{ausland}')")
        titel_lower = self.titel.lower()
        blocked_titel = next((k for k in BLOCKED_TITLE_KEYWORDS if k in titel_lower), None)
        if blocked_titel:
            fails.append(f"Kein Angebot ('{blocked_titel}')")
        if ist_gesuch(self.titel):
            fails.append("Kein Angebot (Gesuch)")
        if ist_kurzzeit(self.titel):
            fails.append("Kurzzeit-/Wochenmiete (zu kurz)")
        if self.verfuegbar_ab and self.verfuegbar_ab > VERFUEGBAR_BIS:
            fails.append(f"Verfügbar erst {self.verfuegbar_ab} (> {VERFUEGBAR_BIS})")
        if not self.titel.strip():
            fails.append("Kein Titel – kein echtes Inserat")
        if self.fahrtzeit_min is not None and self.fahrtzeit_min > MAX_FAHRTZEIT:
            fails.append(f"Fahrtzeit {self.fahrtzeit_min} Min > {MAX_FAHRTZEIT} Min zur TUM")
        return len(fails) == 0, fails

    def als_nachricht(self, score: int = 0) -> str:
        sterne = "⭐" * max(1, min(5, 1 + (score + 5) // 10))
        zeilen = [f"🏠 *{_md_escape(self.titel[:70])}*  {sterne}"]
        quelle_zeit = f"🏷 {self.quelle}"
        if hasattr(self, 'gefunden_um') and self.gefunden_um:
            quelle_zeit += f" · {self.gefunden_um}"
        zeilen.append(quelle_zeit)
        zeilen.append("")
        if self.preis_warm > 0:
            pqm_str = f"  _({self.preis_warm/self.groesse:.0f}€/qm)_" if self.groesse > 0 else ""
            miet_label = "warm" if self.preis_ist_warm else ("kalt" if self.preis_ist_kalt else "ca.")
            zeilen.append(f"💰 {self.preis_warm:.0f}€ {miet_label}{pqm_str}")
        groesse_str = f"📐 {self.groesse:.0f} qm" if self.groesse > 0 else ""
        if self.zimmer > 0:
            groesse_str += f" · {self.zimmer:.0f} Zi."
        if groesse_str:
            zeilen.append(groesse_str)
        if self.adresse:
            q = self.adresse.replace(' ', '+').replace(',', '%2C')
            zeilen.append(f"📍 [{_md_escape(self.adresse)}](https://maps.google.com/?q={q})")
        if self.entfernung_km is not None and self.entfernung_km < 900:
            zeilen.append(f"📌 {self.entfernung_km:.1f} km zum Zentrum")
        if self.verfuegbar_ab:
            y, mo, d = self.verfuegbar_ab.split("-")
            zeilen.append(f"🗓 ab {d}.{mo}.{y}")
        if self.fahrtzeit_min is not None:
            zeilen.append(f"🚇 {self.fahrtzeit_min} Min zur TUM")
        flags = []
        if self.moebliert:
            flags.append("möbliert")
        if self.mit_kueche:
            flags.append("EBK")
        if flags:
            zeilen.append("✅ " + " · ".join(flags))
        zeilen.append(f"\n🔗 [Zum Inserat]({self.url})")
        return "\n".join(zeilen)

    def to_dict(self) -> dict:
        return self.__dict__


# ─── Notifier ────────────────────────────────────────────────────────────────

async def send_telegram(chat_id: str, text: str, client: httpx.AsyncClient) -> bool:
    if not TELEGRAM_TOKEN:
        print("⚠  TELEGRAM_TOKEN nicht gesetzt")
        return False
    try:
        r = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "parse_mode": "Markdown", "disable_web_page_preview": False},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"Telegram-Fehler ({chat_id}): HTTP {r.status_code} – {r.text[:150]}")
        return r.status_code == 200
    except Exception as e:
        print(f"Telegram-Fehler ({chat_id}): {e}")
        return False


async def telegram(text: str, client: httpx.AsyncClient) -> bool:
    """Broadcast an alle aktiven Empfänger (DB-Profile bzw. Legacy-Env), für
    generische Meldungen ohne Empfänger-spezifischen Filter (z.B. Fehler)."""
    recipients, _ = profiles.recipients_and_filters(
        TELEGRAM_CHAT_IDS, load_user_filters())
    if not recipients:
        print("⚠  Keine Telegram-Empfänger konfiguriert")
        return False
    ok = [await send_telegram(cid, text, client) for cid in recipients]
    return any(ok)


# ─── Eigene Filter pro Empfänger ─────────────────────────────────────────────
# Jeder Telegram-Kontakt kann dem Bot einfach schreiben, wonach er sucht
# ("max 1600 warm, ab 2 Zimmer, min 55qm, Schwabing oder Maxvorstadt") – DeepSeek
# übersetzt das in Filter-Felder, die hier persistiert und pro Empfänger beim
# Versand angewendet werden. Ohne eigene Nachricht gilt DEFAULT_FILTER.

def load_user_filters() -> dict:
    if USER_FILTERS_FILE.exists():
        try:
            return json.loads(USER_FILTERS_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_user_filters(filters: dict):
    USER_FILTERS_FILE.write_text(json.dumps(filters, ensure_ascii=False, indent=2))


def load_telegram_offset() -> int:
    if TELEGRAM_OFFSET_FILE.exists():
        try:
            return json.loads(TELEGRAM_OFFSET_FILE.read_text()).get("offset", 0)
        except Exception:
            return 0
    return 0


def save_telegram_offset(offset: int):
    TELEGRAM_OFFSET_FILE.write_text(json.dumps({"offset": offset}))


def format_filter_summary(f: dict) -> str:
    teile = []
    if f.get("max_warm_miete"):
        teile.append(f"max {f['max_warm_miete']:.0f}€ warm")
    if f.get("max_kalt_miete"):
        teile.append(f"max {f['max_kalt_miete']:.0f}€ kalt")
    if f.get("min_groesse"):
        teile.append(f"min {f['min_groesse']:.0f}qm")
    if f.get("min_zimmer"):
        teile.append(f"ab {f['min_zimmer']:.1f} Zi.")
    if f.get("max_radius_km"):
        teile.append(f"max {f['max_radius_km']:.1f}km vom Zentrum")
    if f.get("bezirke_erlaubt"):
        teile.append("nur: " + ", ".join(f["bezirke_erlaubt"]))
    return " · ".join(teile) if teile else "Standard-Filter (keine eigenen Einstellungen)"


WILLKOMMEN_TEXT = (
    "👋 Willkommen beim München-Wohnungsmonitor!\n\n"
    "Wenn du dich gerade über das Web registriert hast, klicke auf den "
    "„Telegram verknüpfen“-Button in deinem Dashboard — dann bekommst du "
    "passende Wohnungen automatisch hierher.\n\n"
    "Schreib mir auch in eigenen Worten, wonach du suchst, z.B.:\n"
    "_\"max 1600 warm, ab 2 Zimmer, min 55qm, am liebsten Schwabing oder Maxvorstadt\"_\n\n"
    "Befehle:\n"
    "/status – zeigt deine aktuellen Filter\n"
    "/reset – setzt auf Standard zurück"
)

DEEPSEEK_SYSTEM_PROMPT = """Du extrahierst Wohnungssuche-Filter aus einer deutschen Chat-Nachricht.
Gib AUSSCHLIESSLICH ein JSON-Objekt zurück mit genau diesen Feldern (nur setzen, was die Nachricht wirklich hergibt, sonst null):
{
  "max_warm_miete": number|null,    // € Gesamtmiete warm, inkl. Nebenkosten
  "max_kalt_miete": number|null,    // € Kaltmiete, nur falls explizit getrennt genannt
  "min_groesse": number|null,       // Quadratmeter
  "min_zimmer": number|null,        // Mindestanzahl Zimmer
  "max_radius_km": number|null,     // km Umkreis um die Münchner Innenstadt (Marienplatz)
  "bezirke_erlaubt": [string]|null, // gewünschte Stadtteile/Bezirke, kleingeschrieben, z.B. ["schwabing","maxvorstadt"]
  "verstanden": boolean             // false, wenn die Nachricht KEINE Filterangabe enthält (z.B. Smalltalk)
}
Nenne keine Erklärung, gib nur das JSON-Objekt zurück."""


async def deepseek_parse_filter(text: str, client: httpx.AsyncClient) -> dict | None:
    if not DEEPSEEK_API_KEY:
        return None
    try:
        r = await client.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": DEEPSEEK_SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=20,
        )
        if r.status_code != 200:
            print(f"DeepSeek-Fehler: HTTP {r.status_code} – {r.text[:200]}")
            return None
        content = r.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        if not data.get("verstanden", True):
            return None
        parsed = {k: v for k, v in data.items() if k != "verstanden" and v is not None}
        # Modell hält sich nicht immer an "kleingeschrieben" – für den späteren
        # Substring-Abgleich gegen die (lowercase) Adresse selbst normalisieren.
        if parsed.get("bezirke_erlaubt"):
            parsed["bezirke_erlaubt"] = [str(b).lower().strip() for b in parsed["bezirke_erlaubt"]]
        return parsed
    except Exception as e:
        print(f"DeepSeek-Fehler: {e}")
        return None


async def poll_telegram_commands(client: httpx.AsyncClient, user_filters: dict,
                                 recipients: list | None = None) -> list:
    """Holt neue Telegram-Nachrichten seit dem letzten Lauf und verarbeitet sie:
      - `/start <code>`   → Telegram-Chat mit Web-Account verknüpfen (Pairing)
      - `/start`          → Willkommensnachricht
      - `/status` `/reset`→ eigene Filter anzeigen / zurücksetzen
      - freier Text       → DeepSeek übersetzt ihn in Filter-Felder

    `user_filters` wird in-place aktualisiert (chat_id -> filter-dict). Chats,
    die per `/start <code>` NEU verknüpft wurden, werden an `recipients`
    angehängt (falls übergeben) — so fließen sie noch im selben Lauf ein.
    Rückgabe: Liste der neu verknüpften (chat_id, filter_dict)."""
    newly: list = []
    if not TELEGRAM_TOKEN:
        return newly
    offset = load_telegram_offset()
    try:
        r = await client.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": offset + 1, "timeout": 0},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"getUpdates-Fehler: HTTP {r.status_code}")
            return newly
        updates = r.json().get("result", [])
    except Exception as e:
        print(f"getUpdates-Fehler: {e}")
        return newly

    print(f"Telegram: {len(updates)} neue Nachricht(en) seit offset {offset}")
    for upd in updates:
        offset = max(offset, upd["update_id"])
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        cid = str(chat.get("id", ""))
        text = (msg.get("text") or "").strip()
        if not cid or not text:
            continue
        print(f"  [{cid}] {text[:60]!r}")

        if text.lower().startswith("/start"):
            token = text.split(None, 1)[1].strip() if " " in text else ""
            if token:
                # Pairing: Web-Dashboard hat einen Code gemünzt → Chat verknüpfen
                uid = profiles.claim_code(token, cid)
                if uid:
                    prof = profiles.get_user(uid)
                    filt = dict(prof.filters) if prof else {}
                    user_filters[cid] = filt
                    newly.append((cid, filt))
                    if recipients is not None and cid not in recipients:
                        recipients.append(cid)
                    ok = await send_telegram(
                        cid,
                        "✅ Telegram verbunden! Du bekommst ab jetzt passende "
                        "Wohnungen hierher geschickt.\n"
                        "Deine Filter kannst du jederzeit im Dashboard anpassen.",
                        client,
                    )
                    print(f"    → Pairing OK ({uid}): {'✓' if ok else '✗'}")
                else:
                    ok = await send_telegram(
                        cid,
                        "❌ Ungültiger oder abgelaufener Verbindungscode.\n"
                        "Öffne dein Dashboard für einen frischen Code.",
                        client,
                    )
                    print(f"    → Pairing fehlgeschlagen: {'✓' if ok else '✗'}")
                continue
            ok = await send_telegram(cid, WILLKOMMEN_TEXT, client)
            print(f"    → Willkommensnachricht: {'✓' if ok else '✗'}")
            continue
        if text.lower() == "/status":
            eff = {**DEFAULT_FILTER, **user_filters.get(cid, {})}
            await send_telegram(cid, f"📋 Deine aktuellen Filter:\n{format_filter_summary(eff)}", client)
            continue
        if text.lower() == "/reset":
            prof = profiles.profile_for_chat(cid)
            if prof:
                profiles.clear_filters(prof.uid)
                user_filters.pop(cid, None)
            else:
                user_filters.pop(cid, None)
                save_user_filters(user_filters)
            await send_telegram(cid, "🔄 Zurück auf Standard-Filter gesetzt.", client)
            continue

        parsed = await deepseek_parse_filter(text, client)
        if not parsed:
            ok = await send_telegram(
                cid,
                "🤔 Das konnte ich nicht als Filter verstehen. Beispiel:\n"
                "_\"max 1800 warm, ab 2 Zimmer, min 50qm, Glockenbachviertel oder Schwabing\"_",
                client,
            )
            print(f"    → DeepSeek: nicht verstanden, Hinweis gesendet: {'✓' if ok else '✗'}")
            continue
        current = user_filters.get(cid, {})
        current.update(parsed)
        current["updated_at"] = date.today().isoformat()
        user_filters[cid] = current
        prof = profiles.profile_for_chat(cid)
        if prof:
            # Web-Nutzer: Filter landen in der DB (die Quelle der Wahrheit)
            profiles.update_filters(prof.uid, {k: v for k, v in parsed.items()})
        else:
            save_user_filters(user_filters)
        print(f"    → Filter gesetzt: {parsed}")
        eff = {**DEFAULT_FILTER, **current}
        await send_telegram(cid, f"✅ Filter aktualisiert:\n{format_filter_summary(eff)}", client)

    save_telegram_offset(offset)
    print(f"Telegram-Offset gespeichert: {offset}")
    return newly


# ─── Deduplication ───────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()).get("ids", []))
        except Exception:
            return set()
    return set()


def save_seen(seen: set[str]):
    ids = list(seen)[-50_000:]
    SEEN_FILE.write_text(json.dumps({"ids": ids}))


def listing_id(url: str, titel: str) -> str:
    return hashlib.md5((url + titel).encode()).hexdigest()[:14]


# ─── Parser-Helfer ───────────────────────────────────────────────────────────

def parse_preis(text: str) -> float:
    if not text:
        return 0.0
    text = re.sub(r"[€EUReur\s]", "", text).replace(".", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", text)
    return float(m.group()) if m else 0.0


def parse_groesse(text: str) -> float:
    if not text:
        return 0.0
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:m²|qm|m2)", text.replace(",", "."), re.IGNORECASE)
    return float(m.group(1)) if m else 0.0


def parse_zimmer(text: str) -> float:
    t = text.replace(",", ".")
    # "3 Zimmer", "3-Zimmer", "3 Zi.", "3 Zi"
    m = (re.search(r"(\d+(?:\.\d+)?)[\s-]*zimmer", t, re.IGNORECASE) or
         re.search(r"(\d+(?:\.\d+)?)[\s-]*zi\.?(?![a-zäöü])", t, re.IGNORECASE))
    return float(m.group(1)) if m else 0.0


def kueche_moebliert(text: str) -> tuple[bool, bool]:
    t = text.lower()
    k = any(w in t for w in ["küche", "kueche", "einbauküche", " ekü", "kitchen"])
    m = any(w in t for w in ["möbliert", "moebliert", "furnished"])
    return k, m


def ist_warmmiete(text: str) -> bool:
    """True, wenn der angezeigte Preis erkennbar die Warmmiete ist (inkl. NK)."""
    t = text.lower().replace("warmwasser", "")  # 'Warmwasser' nicht als 'warm' werten
    schluessel = ["warmmiete", "gesamtmiete", "inkl. nk", "inkl. nebenkosten",
                  "inklusive nebenkosten", "brutto", "all-in", "warmmieten"]
    return any(k in t for k in schluessel) or bool(re.search(r"\bwarm\b", t))


def ist_kaltmiete(text: str) -> bool:
    """True, wenn der Preis erkennbar die Kaltmiete ist (Nebenkosten kommen obendrauf)."""
    t = text.lower()
    schluessel = ["kaltmiete", "kalt-miete", "nettokalt", "netto kalt", "nettomiete",
                  "zzgl. nk", "zzgl nk", "zzgl. nebenkosten", "+ nk", "ohne nk",
                  "ohne nebenkosten", "exkl. nebenkosten", "zzgl. heizung"]
    return any(k in t for k in schluessel) or bool(re.search(r"\bkalt\b", t))


# Sehr kurze Miet-/Zwischenmietzeiträume (Wochen/Tage) → unbrauchbar.
# Mehrmonatige Zwischenmieten werden NICHT geblockt.
_KURZZEIT_PATTERNS = [
    r"wochenweise", r"tageweise", r"tagesweise", r"\bkurzzeit", r"kurzfristig",
    r"kurze\s+zeit", r"monatsweise", r"\bübernachtung", r"pro\s+(woche|nacht|tag)",
    r"/\s*(woche|nacht|tag)\b", r"\bweekly\b", r"\bnightly\b", r"per\s+night",
    r"\b\d{1,2}\s*wochen?\b", r"\b(eine|ein paar|wenige|paar)\s+wochen?\b",
]
def ist_kurzzeit(text: str) -> bool:
    t = text.lower()
    if any(re.search(p, t) for p in _KURZZEIT_PATTERNS):
        return True
    # Datumsbereich "01.07. bis 31.08." / "04.-26.07." → Dauer < ~50 Tage = zu kurz
    m = re.search(r"(\d{1,2})\.(\d{1,2})?\.?\s*(?:bis|-|–|to)\s*(\d{1,2})\.(\d{1,2})\.", t)
    if m:
        d1, mo1, d2, mo2 = (int(m.group(1)), int(m.group(2) or 0),
                            int(m.group(3)), int(m.group(4)))
        if mo1 == 0:
            mo1 = mo2
        tage = (mo2 - mo1) * 30 + (d2 - d1)
        if 0 < tage < 50:
            return True
    return False


def ist_gesuch(titel: str) -> bool:
    """True für Wohnungs-Gesuche ('… sucht Wohnung', 'Wohnung gesucht').
    'Nachmieter/Zwischenmieter gesucht' ist ein echtes Angebot → False."""
    t = titel.lower()
    if "nachmieter" in t or "zwischenmieter" in t:
        return False
    if re.search(r"\bgesucht\b", t):
        return True
    if (re.search(r"\b(suche|suchen|sucht)\b", t) and
            re.search(r"\b(wohnung|zimmer|apartment|appartement|bleibe|unterkunft|zuhause)\b", t)):
        return True
    return False


_MONATE = {"januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
           "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10,
           "november": 11, "dezember": 12}

def parse_verfuegbar(text: str) -> str:
    """Extrahiert das Verfügbar-ab-Datum als ISO-String 'YYYY-MM-DD' oder '' (unbekannt)."""
    t = text.lower()
    # 1. explizit: "Verfügbar: 01.07.2026", "frei ab 1.8.26", "bezugsfrei ab ..."
    m = re.search(r"(?:verfügbar|frei|bezugsfrei|beziehbar)\s*(?:ab|:)?\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})", t)
    # 2. "ab 01.08.2026"
    if not m:
        m = re.search(r"\bab\s+(\d{1,2})\.(\d{1,2})\.(\d{2,4})", t)
    # 3. Zeitraum "01.07.2026 - 30.08.2026" → Startdatum
    if not m:
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})\s*[-–]\s*\d{1,2}\.\d{1,2}\.\d{2,4}", t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return ""
    # 4. sofort verfügbar
    if re.search(r"\b(ab sofort|sofort frei|sofort beziehbar|sofort verfügbar|ab heute|bezugsfrei)\b", t):
        return date.today().isoformat()
    # 5. Monatsname: "ab August", "ab Juli 2026"
    mm = re.search(r"\bab\s+(" + "|".join(_MONATE) + r")\s*(\d{4})?", t)
    if mm:
        mo = _MONATE[mm.group(1)]
        y = int(mm.group(2)) if mm.group(2) else date.today().year
        try:
            cand = date(y, mo, 1)
            if not mm.group(2) and cand < date.today():
                cand = date(y + 1, mo, 1)
            return cand.isoformat()
        except ValueError:
            return ""
    return ""


def wggesucht_adresse(card) -> str:
    """Extrahiert Stadtteil + Straße aus einer WG-Gesucht-Karte.
    Format der Zeile: '<Zimmertyp> | München <Stadtteil> | <Straße>'."""
    el = card.select_one("[class*='col-xs-11'][class*='hidable_content']")
    if not el:
        return "München"
    raw = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
    parts = [p.strip() for p in raw.split("|")]
    stadtteil = re.sub(r"(^München\s*|\s*München$)", "", parts[1]).strip() if len(parts) >= 2 else ""
    strasse = parts[2].strip() if len(parts) >= 3 else ""
    teile = [t for t in (strasse, stadtteil) if t]
    if len(teile) == 2 and teile[0].lower() == teile[1].lower():
        teile = teile[:1]
    return (", ".join(teile) + ", München") if teile else "München"


def make_wohnung(uid, titel, preis, groesse, zimmer, url, quelle, adresse, text) -> Wohnung:
    k, m = kueche_moebliert(text)
    return Wohnung(
        id=uid, titel=titel, preis_warm=preis, groesse=groesse, zimmer=zimmer,
        url=url, quelle=quelle, adresse=adresse, mit_kueche=k, moebliert=m,
        gefunden_um=datetime.now().strftime("%d.%m. %H:%M"),
        preis_ist_warm=ist_warmmiete(text),
        preis_ist_kalt=ist_kaltmiete(text),
        verfuegbar_ab=parse_verfuegbar(text),
    )


# ─── Score & Cross-Portal-Dedup ──────────────────────────────────────────────

def berechne_score(w) -> int:
    s = 0
    combined = (w.titel + " " + w.adresse).lower()
    if any(b in combined for b in TOPLAGE):
        s += 20
    elif any(b in combined for b in GUTE_LAGE):
        s += 10
    if w.mit_kueche:
        s += 10
    if "balkon" in combined or "terrasse" in combined:
        s += 8
    if any(x in combined for x in ["altbau", "gründerzeit", "stuckdecke"]):
        s += 5
    if any(x in combined for x in ["u-bahn", "ubahn", "u1 ", "u2 ", "u3 ", "u4 ", "u5 ", "u6 "]):
        s += 5
    if w.moebliert:
        s -= 5
    if "neubau" in combined:
        s -= 5
    if w.preis_warm > 0 and w.groesse > 0:
        pqm = w.preis_warm / w.groesse
        if pqm < 20:
            s += 15
        elif pqm < 25:
            s += 8
        elif pqm > 32:
            s -= 5
    return s


def dedup(listings: list) -> list:
    seen_sigs: dict = {}
    result = []
    for w in listings:
        if w.preis_warm > 0 and w.groesse > 0:
            pb = round(w.preis_warm / 50) * 50
            gb = round(w.groesse / 5) * 5
            sig = (pb, gb)
            words = frozenset(w.titel.lower().split()[:6])
            if sig in seen_sigs:
                overlap = len(words & seen_sigs[sig]) / max(len(words), 1)
                if overlap > 0.4:
                    continue
            seen_sigs[sig] = words
        result.append(w)
    return result


# ─── Fahrtzeit-Cache & MVV-API ───────────────────────────────────────────────

def load_fahrtzeit_cache() -> dict:
    if FAHRTZEIT_CACHE_FILE.exists():
        try:
            return json.loads(FAHRTZEIT_CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_fahrtzeit_cache(cache: dict):
    FAHRTZEIT_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


# ─── Geocoding & Radius zum Zentrum ──────────────────────────────────────────

import math

def load_geocode_cache() -> dict:
    if GEOCODE_CACHE_FILE.exists():
        try:
            return json.loads(GEOCODE_CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_geocode_cache(cache: dict):
    GEOCODE_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * r * math.asin(math.sqrt(a))

async def geocode(adresse: str, client: httpx.AsyncClient):
    """Liefert [lat, lon], None (nicht gefunden/zu vage) oder 'error' (Geocoder down)."""
    key = (adresse or "").lower().strip()
    # zu vage (nur 'München' o.ä.) → kein verifizierbarer Ort
    rest = re.sub(r"[,\s]+", " ", key.replace("münchen", "").replace("munich", "")).strip()
    if len(rest) < 3:
        return None
    query = adresse if "münchen" in key else f"{adresse}, München"
    try:
        r = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "de"},
            headers={"User-Agent": "wohnungsmonitor/1.0 (privat)"},
            timeout=15,
        )
        if r.status_code != 200:
            return "error"
        arr = r.json()
        if not arr:
            return None
        return [float(arr[0]["lat"]), float(arr[0]["lon"])]
    except Exception as e:
        print(f"Geocode Fehler ({adresse!r}): {e}")
        return "error"

async def entfernung_zum_zentrum(w, cache: dict, client: httpx.AsyncClient) -> float:
    """Setzt und liefert die Luftlinie (km) zum Marienplatz.
    Bei Geocoder-Ausfall: Fallback auf zentrale Stadtteil-Keywords."""
    key = (w.adresse or "").lower().strip()
    if key in cache:
        coord = cache[key]
    else:
        coord = await geocode(w.adresse, client)
        if coord != "error":
            cache[key] = coord            # nur stabile Ergebnisse cachen
            await asyncio.sleep(1.1)       # Nominatim: max 1 Anfrage/Sekunde
    if not isinstance(coord, list):
        # Geocoder down ("error") oder Adresse nicht auflösbar (None):
        # Fallback auf zentrale Stadtteil-Namen, damit zentrale Inserate nicht verloren gehen.
        combined = (w.titel + " " + w.adresse).lower()
        return 0.0 if any(z in combined for z in ZENTRAL_KEYWORDS) else 999.0
    return round(haversine_km(coord[0], coord[1], ZENTRUM_LAT, ZENTRUM_LON), 2)

async def mvv_fahrtzeit(adresse: str, client: httpx.AsyncClient) -> int | None:
    """Gibt Fahrtzeit in Minuten von der Adresse zur TUM zurück, oder None falls unbekannt."""
    if not adresse or len(adresse) < 5:
        return None
    try:
        r = await client.get(
            "https://efa.mvv-muenchen.de/mvv/XML_TRIP_REQUEST2",
            params={
                "outputFormat": "rapidJSON",
                "type_origin": "any",
                "name_origin": adresse,
                "anyObjFilter_origin": "2",
                "type_destination": "coord",
                "name_destination": TUM_COORD,
                "itdDate": "20260622",
                "itdTime": "0830",
                "calcNumberOfTrips": "1",
                "ptOptionsActive": "1",
                "useRealtime": "0",
            },
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        trips = data.get("trips", [])
        if not trips:
            return None
        return round(trips[0].get("duration", 0) / 60)
    except Exception as e:
        print(f"MVV Fehler ({adresse!r}): {e}")
        return None


# ─── Scraper ─────────────────────────────────────────────────────────────────

async def scrape_kleinanzeigen(client: httpx.AsyncClient) -> list[Wohnung]:
    # l3207 = München, c203 = Wohnungen zur Miete, Radius 0 = nur Stadt
    url = (
        f"https://www.kleinanzeigen.de/s-wohnung-mieten/muenchen/c203l6411"
        f"?maxPrice={MAX_WARM_MIETE}&minSize={MIN_GROESSE}"
        f"&sortingField=INSERTION_TIME&pageNum=1"
    )
    out = []
    try:
        r = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("article.aditem") or soup.select("[class*='aditem']")
        print(f"Kleinanzeigen: {len(items)} Einträge")
        for item in items[:30]:
            try:
                link = item.select_one("a[href*='/s-anzeige/']")
                if not link:
                    continue
                href = "https://www.kleinanzeigen.de" + link["href"]
                titel_el = item.select_one("h2, .ellipsis, [class*='title']")
                titel = titel_el.get_text(strip=True) if titel_el else ""
                if not titel:
                    continue
                preis_el = item.select_one("[class*='price']")
                preis = parse_preis(preis_el.get_text(strip=True) if preis_el else "")
                # qm + Zimmer stehen in den Tags ("Gesuch · 70 m² · 3 Zi.").
                # Fallback auf den ganzen Kartentext, falls sich das Markup ändert –
                # "m²/qm/m2" ist eindeutig, da kann nichts anderes reinrutschen.
                tags = item.select_one(".aditem-main--middle--tags") or item.select_one("[class*='tag']")
                karten_text = item.get_text(" ", strip=True)
                detail_text = tags.get_text(" ", strip=True) if tags else karten_text
                groesse = parse_groesse(detail_text) or parse_groesse(karten_text)
                zimmer = parse_zimmer(detail_text) or parse_zimmer(karten_text)
                addr_el = item.select_one(".aditem-main--top--left, [class*='location']")
                adresse = addr_el.get_text(strip=True) if addr_el else ""

                # Nur München-Listings – PLZ 8xxxx = München, oder Name enthält München
                plz_m = re.search(r'\b8[01]\d{3}\b', adresse)
                if adresse and "münchen" not in adresse.lower() and "munich" not in adresse.lower() and not plz_m:
                    print(f"  ✗ Kleinanzeigen: übersprungen ({adresse} – nicht München)")
                    continue

                uid = listing_id(href, titel)
                out.append(make_wohnung(uid, titel, preis, groesse, zimmer, href, "Kleinanzeigen", adresse or "München", titel + detail_text))
            except Exception:
                continue
    except Exception as e:
        print(f"Kleinanzeigen Fehler: {e}")
    return out


async def scrape_wggesucht(client: httpx.AsyncClient) -> list[Wohnung]:
    url = (
        f"https://www.wg-gesucht.de/wohnungen-in-Muenchen.90.2.1.0.html"
        f"?offer_filter=1&noDeact=1&city_id=90&category=2"
        f"&rent_type=0&sMin={MIN_GROESSE}&rMax={MAX_WARM_MIETE}"
        "&sort_column=created_at&sort_order=1"
    )
    out = []
    try:
        r = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        items = (soup.select("[class*='wgg_card']") or soup.select("[class*='offer_list_item']")
                 or soup.select("tr.list-body-row"))
        print(f"WG-Gesucht: {len(items)} Einträge")
        for item in items[:30]:
            try:
                link = item.select_one("a[href*='/wohnung'], a[href*='/wohnungen']") or item.select_one("a[href]")
                if not link:
                    continue
                href = link["href"]
                if not href.startswith("http"):
                    href = "https://www.wg-gesucht.de" + href
                titel_el = (item.select_one("h3, h4, [class*='headline']") or
                            item.select_one("[class*='title']") or
                            item.select_one("strong"))
                titel = titel_el.get_text(strip=True) if titel_el else link.get_text(strip=True)[:80]
                titel = titel.strip()
                if not titel:
                    continue
                text = item.get_text(" ", strip=True)
                preis_m = re.search(r"([\d.]+(?:,\d+)?)\s*€", text)
                preis = parse_preis(preis_m.group() if preis_m else "")
                groesse = parse_groesse(text)
                zimmer = parse_zimmer(text)
                adresse = wggesucht_adresse(item)
                uid = listing_id(href, titel)
                out.append(make_wohnung(uid, titel, preis, groesse, zimmer, href, "WG-Gesucht", adresse, text))
            except Exception:
                continue
    except Exception as e:
        print(f"WG-Gesucht Fehler: {e}")
    return out


async def scrape_wohnungsboerse(client: httpx.AsyncClient) -> list[Wohnung]:
    url = "https://www.wohnungsboerse.net/Muenchen/mieten/wohnungen"
    out = []
    try:
        r = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        # Each listing is a full <a> tag pointing to immodetail
        items = soup.select("a[href*='/immodetail/']")
        print(f"Wohnungsboerse: {len(items)} Einträge")
        for item in items[:30]:
            try:
                href = item["href"]
                if not href.startswith("http"):
                    href = "https://www.wohnungsboerse.net" + href
                text = item.get_text(" ", strip=True)
                if not text:
                    continue
                # Title is the first line / bold part before price info
                titel = re.sub(r"\s+", " ", text.split("Kaltmiete")[0]).strip()[:120]
                if not titel:
                    continue
                preis_m = re.search(r"([\d.]+(?:,\d+)?)\s*€", text)
                preis = parse_preis(preis_m.group() if preis_m else "")
                groesse = parse_groesse(text)
                zimmer = parse_zimmer(text)
                adresse_m = re.search(r"München\s*[-–]\s*([^\n€\d]+)", text)
                adresse = "München" + (" - " + adresse_m.group(1).strip() if adresse_m else "")
                uid = listing_id(href, titel)
                out.append(make_wohnung(uid, titel, preis, groesse, zimmer, href, "Wohnungsboerse", adresse, text))
            except Exception:
                continue
    except Exception as e:
        print(f"Wohnungsboerse Fehler: {e}")
    return out


async def scrape_immowelt_lite(client: httpx.AsyncClient) -> list[Wohnung]:
    """ImmoWelt ohne Browser. Die Suchergebnis-Seite (AVIV/React) rendert die
    Inserate serverseitig als Karten mit stabilen data-testid-Attributen."""
    url = (
        "https://www.immowelt.de/suche/muenchen/wohnungen/mieten"
        f"?ma={MAX_WARM_MIETE}&mia={MIN_GROESSE}&sort=createdate"
    )
    out = []
    try:
        r = await client.get(url, headers=HEADERS, timeout=25, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")

        cards = (soup.select('div[data-testid="serp-core-classified-card-testid"]') or
                 soup.select('div[id^="classified-card-"]'))
        print(f"ImmoWelt: {len(cards)} Einträge")
        for card in cards[:25]:
            try:
                link = card.select_one("a[href*='/expose/']")
                if not link:
                    continue
                href = link["href"].split("?")[0]
                if not href.startswith("http"):
                    href = "https://www.immowelt.de" + href

                def tid(name):
                    el = card.select_one(f'[data-testid="{name}"]')
                    return el.get_text(" ", strip=True) if el else ""

                preis_txt   = tid("cardmfe-price-testid")          # "1.450 € Kaltmiete"
                keyfacts    = tid("cardmfe-keyfacts-testid")        # "3,5 Zimmer · 103 m² · EG · frei ab sofort"
                adresse     = tid("cardmfe-description-box-address")
                box_txt     = tid("cardmfe-description-box-text-test-id")

                preis   = parse_preis(preis_txt)
                groesse = parse_groesse(keyfacts)
                zimmer  = parse_zimmer(keyfacts)

                # Titel = Objekttyp (Teil des Beschreibungstexts ohne Preis/Keyfacts)
                titel = box_txt.replace(preis_txt, "").strip()
                titel = re.split(r"\s+\d+(?:[.,]\d+)?\s*Zimmer|·", titel)[0].strip()
                if not titel:
                    titel = (adresse or "Wohnung").split(",")[0]

                # Volltext für Kaltmiete-/Verfügbar-/Möbliert-Erkennung
                text = " ".join([preis_txt, keyfacts, box_txt, adresse])
                uid = listing_id(href, titel)
                out.append(make_wohnung(uid, titel, preis, groesse, zimmer, href, "ImmoWelt", adresse, text))
            except Exception:
                continue

    except Exception as e:
        print(f"ImmoWelt Fehler: {e}")
    return out


async def scrape_wunderflats(client: httpx.AsyncClient) -> list[Wohnung]:
    """Wunderflats (möbliertes Wohnen auf Zeit). Daten aus dem eingebetteten
    'data-hydrant'-JSON. Preise sind All-in (warm) und in Cent angegeben."""
    url = "https://wunderflats.com/en/furnished-apartments/munich"
    out = []
    try:
        r = await client.get(url, headers=HEADERS, timeout=25, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        node = soup.find("script", id="data-hydrant")
        if not node:
            print("Wunderflats: kein data-hydrant gefunden")
            return out
        data = json.loads(node.string)
        items = (data.get("pageData", {}).get("listingResults", {}).get("items", []) or [])
        # Echte Detail-Links aus den <a>-Tags: Format /en/furnished-apartment/<slug>/<_id>
        href_by_id = {}
        for a in soup.select('a[href*="/furnished-apartment/"]'):
            clean = a["href"].split("?")[0]
            href_by_id[clean.rstrip("/").split("/")[-1]] = clean
        print(f"Wunderflats: {len(items)} Einträge")
        for it in items[:25]:
            try:
                titel = (it.get("title", {}) or {}).get("de") or (it.get("title", {}) or {}).get("en") or "Wohnung"
                preis = float(it.get("price", 0) or 0) / 100.0      # Cent → €, all-in (warm)
                groesse = float(it.get("area", 0) or 0)
                zimmer = float(it.get("rooms", 0) or 0)
                _id = it.get("_id", "")
                pfad = href_by_id.get(_id)
                if not pfad:
                    continue   # ohne gültigen Detail-Link überspringen (Link wäre tot)
                href = "https://wunderflats.com" + pfad
                addr = it.get("address", {}) or {}
                strasse = addr.get("street", "")
                adresse = (f"{strasse}, München" if strasse else "München")
                # All-in-Preis → als Warmmiete kennzeichnen
                text = f"{titel} möbliert inkl. Nebenkosten warmmiete {adresse}"
                uid = listing_id(href, titel)
                out.append(make_wohnung(uid, titel, preis, groesse, zimmer, href, "Wunderflats", adresse, text))
            except Exception:
                continue
    except Exception as e:
        print(f"Wunderflats Fehler: {e}")
    return out


async def scrape_mrlodge(client: httpx.AsyncClient) -> list[Wohnung]:
    """Mr. Lodge (möbliertes Wohnen auf Zeit). Daten aus JSON-LD (@type Apartment).
    Hinweis: Mr. Lodge nennt keine Preise öffentlich → preis_warm bleibt 0."""
    url = "https://www.mrlodge.de/wohnungen-muenchen"
    out = []
    try:
        r = await client.get(url, headers=HEADERS, timeout=25, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")

        # Preis steht nur in den sichtbaren Karten ("2.590 €/Monat"), nicht im JSON-LD.
        # Karte über den Listing-Link finden und Preis daraus ziehen.
        preis_pro_pfad = {}
        for a in soup.find_all("a", href=re.compile(r"/wohnen-auf-zeit/")):
            pfad = a["href"].split("?")[0].rstrip("/")
            node = a
            for _ in range(6):
                node = node.parent
                if node is None:
                    break
                pm = re.search(r"([\d.]+)\s*€\s*/\s*Monat", node.get_text(" "))
                if pm:
                    preis_pro_pfad.setdefault(pfad, parse_preis(pm.group(1)))
                    break

        apts = []
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(s.string or "")
            except Exception:
                continue
            for it in (d if isinstance(d, list) else [d]):
                if isinstance(it, dict) and it.get("@type") == "Apartment":
                    apts.append(it)
        print(f"Mr. Lodge: {len(apts)} Einträge")
        for it in apts[:30]:
            try:
                titel = (it.get("name") or "").replace(" | ", " · ").strip()
                href = it.get("url") or ""
                if not href or not titel:
                    continue
                groesse = float((it.get("floorSize") or {}).get("value", 0) or 0)
                zimmer = float(it.get("numberOfRooms", 0) or 0)
                pfad = href.split("mrlodge.de")[-1].split("?")[0].rstrip("/")
                preis = preis_pro_pfad.get(pfad, 0.0)
                addr = it.get("address") or {}
                strasse = addr.get("streetAddress", "")
                plz = addr.get("postalCode", "")
                # Stadtteil steht im Namen: "... | München-Schwabing | 9209"
                stadtteil_m = re.search(r"münchen[-\s]([\wäöüß-]+)", titel, re.IGNORECASE)
                stadtteil = stadtteil_m.group(1) if stadtteil_m else ""
                teile = [t for t in (strasse, stadtteil, f"{plz} München".strip()) if t]
                adresse = ", ".join(teile) if teile else "München"
                desc = it.get("description", "")
                w = make_wohnung(listing_id(href, titel), titel, preis, groesse, zimmer,
                                 href, "Mr. Lodge", adresse, titel + " " + desc)
                w.preis_ist_warm = True   # möblierte Monatspauschale = All-in
                w.moebliert = True
                out.append(w)
            except Exception:
                continue
    except Exception as e:
        print(f"Mr. Lodge Fehler: {e}")
    return out


# ─── Browser-Portale (Playwright) ────────────────────────────────────────────
# IS24 (Imperva) und HousingAnywhere (client-gerendert) gehen NUR mit echtem,
# nicht-headless Browser. In GitHub Actions läuft Chromium headful via xvfb.

_STEALTH_JS = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "Object.defineProperty(navigator,'languages',{get:()=>['de-DE','de','en']});"
    "window.chrome={runtime:{}};"
)


async def scrape_is24_browser(page) -> list[Wohnung]:
    """IS24 über headful Chromium. Daten aus dem eingebetteten searchResponseModel-JSON.
    Liefert die echte Warmmiete (calculatedTotalRent) und Koordinaten mit."""
    url = (
        "https://www.immobilienscout24.de/Suche/de/bayern/muenchen/wohnung-mieten"
        f"?price=-{MAX_WARM_MIETE}.0&livingspace={MIN_GROESSE}.0-&sorting=2"
    )
    out = []
    try:
        # Warmup: erst die Startseite besuchen (setzt Imperva-Cookies) – erhöht die
        # Erfolgsquote vor der eigentlichen Suche.
        try:
            await page.goto("https://www.immobilienscout24.de/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
        except Exception:
            pass

        # Bis zu 3 Versuche: Imperva zeigt sporadisch eine "Ich bin kein Roboter"-Seite.
        raw = ""
        for versuch in range(1, 4):
            await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(4000 + versuch * 1500)
            titel_seite = (await page.title()) or ""
            if "roboter" in titel_seite.lower() or "robot" in titel_seite.lower():
                print(f"IS24: Captcha bei Versuch {versuch}/3 – neuer Versuch")
                await page.wait_for_timeout(2500)
                continue
            raw = await page.evaluate(
                "() => { const s=[...document.querySelectorAll('script:not([src])')]"
                ".find(s=>(s.textContent||'').includes('searchResponseModel'));"
                "return s ? s.textContent : ''; }"
            )
            if raw:
                break
            print(f"IS24: kein searchResponseModel bei Versuch {versuch}/3")
            await page.wait_for_timeout(2000)

        if not raw:
            print("IS24: nach 3 Versuchen blockiert/leer – übersprungen")
            return out
        i = raw.find('"searchResponseModel"')
        j = raw.find("{", i)
        depth, end = 0, len(raw)
        for k in range(j, len(raw)):
            if raw[k] == "{":
                depth += 1
            elif raw[k] == "}":
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        data = json.loads(raw[j:end])
        groups = data["resultlist.resultlist"]["resultlistEntries"][0]["resultlistEntry"]
        print(f"IS24 (Browser): {len(groups)} Einträge")
        for g in groups[:25]:
            try:
                re_ = g.get("resultlist.realEstate") or {}
                eid = g.get("@id") or re_.get("@id") or ""
                titel = re_.get("title", "")
                warm = ((re_.get("calculatedTotalRent") or {}).get("totalRent") or {}).get("value", 0)
                kalt = (re_.get("price") or {}).get("value", 0)
                preis = float(warm or kalt or 0)
                ist_warm = bool(warm)
                groesse = float(re_.get("livingSpace", 0) or 0)
                zimmer = float(re_.get("numberOfRooms", 0) or 0)
                a = re_.get("address") or {}
                adresse = " ".join(x for x in [a.get("street", ""), a.get("houseNumber", "")] if x).strip()
                adresse = (adresse + ", " if adresse else "") + (a.get("quarter") or "") + ", München"
                href = f"https://www.immobilienscout24.de/expose/{eid}"
                text = f"{titel} {'warmmiete' if ist_warm else ''}"
                w = make_wohnung(listing_id(href, titel), titel, preis, groesse, zimmer,
                                 href, "ImmobilienScout24", re.sub(r"^,\s*", "", adresse), text)
                w.preis_ist_warm = ist_warm
                if re_.get("builtInKitchen"):
                    w.mit_kueche = True
                out.append(w)
            except Exception:
                continue
    except Exception as e:
        print(f"IS24 (Browser) Fehler: {e}")
    return out


async def scrape_ha_browser(page) -> list[Wohnung]:
    """HousingAnywhere (möbliert auf Zeit) über Browser. Best-effort: liest die
    gerenderten Karten; HA-München sind meist kleine Studios."""
    out = []
    try:
        await page.goto("https://housinganywhere.com/s/Munich--Germany",
                        wait_until="domcontentloaded", timeout=35000)
        await page.wait_for_timeout(5000)
        for _ in range(6):
            await page.mouse.wheel(0, 3500)
            await page.wait_for_timeout(800)
        cards = await page.evaluate(r"""() => {
            const out=[]; const seen=new Set();
            for(const a of document.querySelectorAll('a[href*="/room/"]')){
              const href=a.href.split('?')[0];
              if(seen.has(href))continue; seen.add(href);
              let n=a, best='';
              for(let i=0;i<5 && n;i++){ n=n.parentElement;
                if(n){const t=n.innerText||''; if(t.length>best.length)best=t; if(t.length>40)break;} }
              out.push({href, txt:best.replace(/\s+/g,' ').trim()});
            }
            return out;
        }""")
        print(f"HousingAnywhere (Browser): {len(cards)} Einträge")
        for c in cards[:25]:
            try:
                txt = c["txt"]
                if "m²" not in txt:
                    continue
                preis = parse_preis(txt[txt.find("€"):txt.find("€") + 12]) if "€" in txt else 0.0
                groesse = parse_groesse(txt)
                # Titel/Adresse aus "... in <Straße>, Munich"
                tm = re.search(r"((?:Studio|Private room|Apartment|Entire)[^|]*?in\s+([^,]+),\s*Munich)", txt)
                titel = (tm.group(1).strip() if tm else "Wohnung HousingAnywhere")
                strasse = tm.group(2).strip() if tm else ""
                adresse = (f"{strasse}, München" if strasse else "München")
                text = f"{titel} möbliert inkl. nebenkosten warmmiete"
                w = make_wohnung(listing_id(c["href"], titel), titel, preis, groesse, 0,
                                 c["href"], "HousingAnywhere", adresse, text)
                w.preis_ist_warm = True
                w.moebliert = True
                out.append(w)
            except Exception:
                continue
    except Exception as e:
        print(f"HousingAnywhere (Browser) Fehler: {e}")
    return out


async def scrape_browser_portale() -> list[Wohnung]:
    """Startet einen headful Chromium (xvfb in CI) und scrapt die Browser-Portale.
    Fehler hier dürfen den restlichen Lauf nie killen."""
    if not PLAYWRIGHT_OK:
        print("Playwright nicht installiert – überspringe IS24/HousingAnywhere.")
        return []
    out = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=False,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage"],
            )
            ctx = await browser.new_context(
                locale="de-DE", timezone_id="Europe/Berlin",
                viewport={"width": 1366, "height": 900},
                user_agent=HEADERS["User-Agent"],
            )
            await ctx.add_init_script(_STEALTH_JS)
            page = await ctx.new_page()
            out += await scrape_is24_browser(page)
            out += await scrape_ha_browser(page)
            await browser.close()
    except Exception as e:
        print(f"Browser-Portale Fehler: {e}")
    return out


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    print(f"=== Wohnungsmonitor Lite – {datetime.now().strftime('%d.%m.%Y %H:%M')} ===")

    seen = load_seen()
    fahrtzeit_cache = load_fahrtzeit_cache()
    geocode_cache = load_geocode_cache()
    # Empfänger + ihre eigenen Filter: DB-Profile (Web-Registrierung) gewinnen,
    # ohne DB-Nutzer greift der Legacy-Pfad (TELEGRAM_CHAT_IDS + user_filters.json)
    # mit einmaliger Migration – siehe profiles.recipients_and_filters().
    recipients, user_filters = profiles.recipients_and_filters(
        TELEGRAM_CHAT_IDS, load_user_filters())
    print(f"Bereits bekannte Inserate: {len(seen)}")
    print(f"Empfänger: {len(recipients)} · davon mit eigenen Filtern: {len(user_filters)}")

    async with httpx.AsyncClient(http2=True) as client:
        # Neue /start <code>-Verknüpfungen fließen noch im selben Lauf ein
        newly = await poll_telegram_commands(client, user_filters, recipients)
        for cid, filt in newly:
            user_filters.setdefault(cid, filt)
        if newly:
            print(f"→ {len(newly)} neue Telegram-Verknüpfung(en) in diesem Lauf")

        # Scrape-Grenzen einmal gemeinsam auf den lockersten aller Wünsche aufweiten
        # (die Portale werden nur einmal durchsucht) – die enge Auswahl pro Empfänger
        # passiert unten in passt(effektiver_filter).
        global MAX_WARM_MIETE, MAX_KALT_MIETE, MIN_GROESSE, MAX_RADIUS_KM
        for f in user_filters.values():
            if f.get("max_warm_miete"):
                MAX_WARM_MIETE = max(MAX_WARM_MIETE, f["max_warm_miete"])
            if f.get("max_kalt_miete"):
                MAX_KALT_MIETE = max(MAX_KALT_MIETE, f["max_kalt_miete"])
            if f.get("min_groesse") is not None:
                MIN_GROESSE = min(MIN_GROESSE, f["min_groesse"])
            if f.get("max_radius_km"):
                MAX_RADIUS_KM = max(MAX_RADIUS_KM, f["max_radius_km"])
        print(f"Filter (weitester Suchradius über alle Empfänger): "
              f"max {MAX_WARM_MIETE}€ warm, min {MIN_GROESSE}qm, max {MAX_RADIUS_KM:.1f}km")

        alle: list[Wohnung] = []
        # httpx-Portale parallel + Browser-Portale (IS24/HA) gemeinsam ausführen
        results = await asyncio.gather(
            scrape_kleinanzeigen(client),
            scrape_wggesucht(client),
            scrape_wohnungsboerse(client),
            scrape_immowelt_lite(client),
            scrape_mrlodge(client),
            scrape_wunderflats(client),
            scrape_browser_portale(),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, list):
                alle.extend(r)

        # Cross-portal deduplication
        alle = dedup(alle)

        print(f"\nGesamt: {len(alle)} Inserate geladen (nach Dedup)")

        # Precompute scores for all listings
        scores = {w.id: berechne_score(w) for w in alle}

        # Collect new matches, sort by score descending before sending
        neue_wohnungen = []
        for w in alle:
            if w.id in seen:
                continue
            seen.add(w.id)
            # Erstcheck ohne Geo (spart Geocoder-Calls für unpassende Inserate)
            passt, gruende = w.passt()
            if not passt:
                print(f"  ✗ {w.titel[:55]} → {', '.join(gruende)}")
                continue
            # Entfernung zum Zentrum (Marienplatz) prüfen
            w.entfernung_km = await entfernung_zum_zentrum(w, geocode_cache, client)
            passt, gruende = w.passt()
            if passt:
                neue_wohnungen.append(w)
            else:
                print(f"  ✗ {w.titel[:55]} → {', '.join(gruende)}")

        neue_wohnungen.sort(key=lambda w: scores.get(w.id, 0), reverse=True)

        neue_matches = 0
        for w in neue_wohnungen:
            sc = scores.get(w.id, 0)
            # Wer genau will DIESE Wohnung? Jeder Empfänger hat seinen eigenen
            # effektiven Filter (Standard + eigene Wünsche via Web-Dashboard oder
            # Telegram-Nachricht).
            treffer_fuer = [
                cid for cid in recipients
                if w.passt({**DEFAULT_FILTER, **user_filters.get(cid, {})})[0]
            ]
            if not treffer_fuer:
                continue
            neue_matches += 1
            gesendet = sum([await send_telegram(cid, w.als_nachricht(sc), client) for cid in treffer_fuer])
            print(f"  ✅ MATCH [score={sc:+d}] → {gesendet}/{len(treffer_fuer)} Empfänger: "
                  f"{w.titel} | {w.preis_warm:.0f}€ | {w.groesse:.0f}qm | {w.quelle}")
            # Backup
            matches = []
            if MATCHES_FILE.exists():
                try:
                    matches = json.loads(MATCHES_FILE.read_text())
                except Exception:
                    pass
            matches.append(w.to_dict())
            MATCHES_FILE.write_text(json.dumps(matches, ensure_ascii=False, indent=2))

        save_seen(seen)
        save_fahrtzeit_cache(fahrtzeit_cache)
        save_geocode_cache(geocode_cache)

    print(f"\n=== Fertig: {neue_matches} neue Match{'es' if neue_matches != 1 else ''} ===")


async def _serve_loop() -> None:
    """Always-on-Modus für die Oracle-VM: Lauf → Pause → nächster Lauf.
    Ein Lauf-Fehler darf den Service nie beenden (systemd-Restart wäre zwar
    ok, aber so bleibt der Rhythmus stabil)."""
    print(f"=== --serve Modus: Läufe alle {SERVE_INTERVAL}s ===")
    while True:
        try:
            await main()
        except Exception as e:
            print(f"⚠ Lauf fehlgeschlagen: {e}")
        print(f"… nächster Lauf in {SERVE_INTERVAL}s")
        await asyncio.sleep(SERVE_INTERVAL)


if __name__ == "__main__":
    # Abhängigkeiten prüfen
    missing = []
    try:
        import httpx
    except ImportError:
        missing.append("httpx")
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        missing.append("beautifulsoup4")
    if missing:
        print(f"pip install {' '.join(missing)}")
        sys.exit(1)
    try:
        import h2  # für http2=True
    except ImportError:
        pass  # optional, kein Fehler
    if "--serve" in sys.argv:
        asyncio.run(_serve_loop())
    else:
        asyncio.run(main())
