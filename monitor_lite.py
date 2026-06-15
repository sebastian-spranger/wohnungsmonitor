#!/usr/bin/env python3
"""
Wohnungsmonitor Lite – Single-Run Version für GitHub Actions.
Kein Playwright/Browser nötig. Läuft in <15 Sekunden.
Portale: Kleinanzeigen, WG-Gesucht, Wohnungsboerse, IS24 (Lite), ImmoWelt (Lite)
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

# ─── Konfiguration ────────────────────────────────────────────────────────────
# Werte kommen aus GitHub Secrets (nie im Code speichern!)
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_IDS = [cid.strip() for cid in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]

MAX_WARM_MIETE   = int(os.environ.get("MAX_WARM_MIETE",   "2200"))
# Sicherheitsabschlag: zeigt ein Inserat nur die Kaltmiete (oder unklar),
# liegt die echte Warmmiete meist 15-20% höher → niedrigeres Limit anwenden.
MAX_KALT_MIETE   = int(os.environ.get("MAX_KALT_MIETE",   "1950"))
MIN_GROESSE      = int(os.environ.get("MIN_GROESSE",      "45"))
MAX_FAHRTZEIT    = int(os.environ.get("MAX_FAHRTZEIT",    "20"))   # Minuten zur TUM
# Geografischer Filter: nur Wohnungen im Umkreis um die Innenstadt (Marienplatz)
MAX_RADIUS_KM    = float(os.environ.get("MAX_RADIUS_KM",  "3.0"))  # km Luftlinie
# Spätestes Einzugs-/Verfügbarkeitsdatum (ISO). Inserate mit erkennbar späterem
# "Verfügbar ab"-Datum werden verworfen. Unbekannte Verfügbarkeit bleibt drin.
VERFUEGBAR_BIS   = os.environ.get("VERFUEGBAR_BIS", "2026-07-15")

SEEN_FILE           = Path("seen.json")
MATCHES_FILE        = Path("matches.json")
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

    def passt(self) -> tuple[bool, list[str]]:
        fails = []
        if self.preis_warm == 0 and self.groesse == 0:
            fails.append("Kein Preis und keine Größe – kein echtes Inserat")
        if self.preis_warm > 0:
            # klar kalt → strenger (Warm ≈ +NK); warm oder unklar → großzügig (nichts verpassen)
            if self.preis_ist_kalt and not self.preis_ist_warm:
                limit, label = MAX_KALT_MIETE, "kalt"
            else:
                limit = MAX_WARM_MIETE
                label = "warm" if self.preis_ist_warm else "unklar"
            if self.preis_warm > limit:
                fails.append(f"Preis {self.preis_warm:.0f}€ ({label}) > {limit}€")
        if self.groesse > 0 and self.groesse < MIN_GROESSE:
            fails.append(f"Größe {self.groesse:.0f}qm < {MIN_GROESSE}qm")
        combined = (self.titel + " " + self.adresse).lower()
        if self.entfernung_km is not None and self.entfernung_km > MAX_RADIUS_KM:
            fails.append(f"{self.entfernung_km:.1f} km vom Zentrum > {MAX_RADIUS_KM:.0f} km")
        ausland = next((a for a in AUSLAND_KEYWORDS if a in combined), None)
        if ausland:
            fails.append(f"Nicht München ('{ausland}')")
        titel_lower = self.titel.lower()
        blocked_titel = next((k for k in BLOCKED_TITLE_KEYWORDS if k in titel_lower), None)
        if blocked_titel:
            fails.append(f"Kein Angebot ('{blocked_titel}')")
        if re.match(r'^suche\b', titel_lower):
            fails.append("Kein Angebot (Gesuche)")
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
        zeilen = [f"🏠 *{self.titel[:70]}*  {sterne}"]
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
            zeilen.append(f"📍 [{self.adresse}](https://maps.google.com/?q={q})")
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

async def telegram(text: str, client: httpx.AsyncClient) -> bool:
    if not TELEGRAM_TOKEN:
        print("⚠  TELEGRAM_TOKEN nicht gesetzt")
        return False
    success = True
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            r = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text,
                      "parse_mode": "Markdown", "disable_web_page_preview": False},
                timeout=10,
            )
            if r.status_code != 200:
                success = False
        except Exception as e:
            print(f"Telegram-Fehler ({chat_id}): {e}")
            success = False
    return success


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
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*Zimmer", text.replace(",", "."), re.IGNORECASE)
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
    return any(re.search(p, t) for p in _KURZZEIT_PATTERNS)


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
                preis_el = item.select_one(".aditem-details strong, .price-small, [class*='price']")
                preis = parse_preis(preis_el.get_text(strip=True) if preis_el else "")
                detail = item.select_one(".aditem-details")
                detail_text = detail.get_text(" ") if detail else ""
                groesse = parse_groesse(detail_text)
                zimmer = parse_zimmer(detail_text)
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


async def scrape_is24_lite(client: httpx.AsyncClient) -> list[Wohnung]:
    """IS24 ohne Browser – extrahiert eingebettetes JSON aus dem HTML-Source."""
    url = (
        "https://www.immobilienscout24.de/Suche/de/bayern/muenchen/wohnung-mieten"
        f"?price=-{MAX_WARM_MIETE}.0&livingspace={MIN_GROESSE}.0-&sorting=2"
    )
    out = []
    try:
        r = await client.get(url, headers=HEADERS, timeout=25, follow_redirects=True)
        text = r.text

        # IS24 bettet Listing-Daten als JSON in einen <script>-Tag ein
        # Versuche verschiedene bekannte Patterns
        json_data = None
        for pattern in [
            r'"resultList"\s*:\s*\{.*?"realEstateList"\s*:\s*(\[.*?\])\s*,\s*"[a-z]',
            r'__REACT_QUERY_STATE__\s*=\s*(\{.*?\})\s*;',
            r'window\.__reactQSD\s*=\s*(\{.*?\})\s*;',
        ]:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                try:
                    json_data = json.loads(m.group(1))
                    break
                except Exception:
                    continue

        if json_data:
            # Versuche Listings aus dem JSON zu extrahieren
            listings = json_data if isinstance(json_data, list) else []
            print(f"IS24 (JSON): {len(listings)} Einträge")
            for item in listings[:20]:
                try:
                    expose_id = item.get("id", "") or item.get("realEstateId", "")
                    titel = item.get("title", "") or item.get("address", {}).get("description", {}).get("text", "")
                    preis = float(item.get("price", {}).get("value", 0) or 0)
                    groesse = float(item.get("livingSpace", 0) or 0)
                    zimmer = float(item.get("numberOfRooms", 0) or 0)
                    href = f"https://www.immobilienscout24.de/expose/{expose_id}"
                    addr = item.get("address", {})
                    adresse = f"{addr.get('street', '')} {addr.get('houseNumber', '')}, {addr.get('city', 'München')}".strip(", ")
                    uid = listing_id(href, titel)
                    out.append(make_wohnung(uid, titel, preis, groesse, zimmer, href, "ImmobilienScout24", adresse, titel))
                except Exception:
                    continue
        else:
            # Fallback: HTML-Parsing
            soup = BeautifulSoup(text, "html.parser")
            items = (soup.select("li[class*='result-list__listing']") or
                     soup.select("article[class*='result-list']") or
                     soup.select(".result-list-entry"))
            print(f"IS24 (HTML-Fallback): {len(items)} Einträge")
            for item in items[:20]:
                try:
                    link = item.select_one("a[href*='/expose/']")
                    if not link:
                        continue
                    href = link["href"]
                    if not href.startswith("http"):
                        href = "https://www.immobilienscout24.de" + href
                    titel_el = item.select_one("h2, h3, [class*='title']")
                    titel = titel_el.get_text(strip=True) if titel_el else link.get_text(strip=True)
                    t = item.get_text(" ", strip=True)
                    preis_m = re.search(r"([\d.]+(?:,\d+)?)\s*€", t)
                    preis = parse_preis(preis_m.group() if preis_m else "")
                    groesse = parse_groesse(t)
                    zimmer = parse_zimmer(t)
                    addr_el = item.select_one("[class*='address']")
                    adresse = addr_el.get_text(strip=True) if addr_el else ""
                    uid = listing_id(href, titel)
                    out.append(make_wohnung(uid, titel, preis, groesse, zimmer, href, "ImmobilienScout24", adresse, t))
                except Exception:
                    continue

    except Exception as e:
        print(f"IS24 Fehler: {e}")
    return out


async def scrape_immowelt_lite(client: httpx.AsyncClient) -> list[Wohnung]:
    """ImmoWelt ohne Browser – HTML-Fallback."""
    url = (
        "https://www.immowelt.de/suche/muenchen/wohnungen/mieten"
        f"?ma={MAX_WARM_MIETE}&mia={MIN_GROESSE}&sort=createdate"
    )
    out = []
    try:
        r = await client.get(url, headers=HEADERS, timeout=25, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")

        # ImmoWelt bettet manchmal JSON in __NEXT_DATA__ ein
        next_data = soup.find("script", id="__NEXT_DATA__")
        if next_data:
            try:
                data = json.loads(next_data.string)
                # Pfad durch die Datenstruktur suchen
                props = data.get("props", {}).get("pageProps", {})
                listings = (props.get("listings", []) or
                            props.get("searchResult", {}).get("listings", []) or [])
                print(f"ImmoWelt (JSON): {len(listings)} Einträge")
                for item in listings[:20]:
                    try:
                        expose_id = item.get("globalObjectKey", "") or item.get("id", "")
                        titel = item.get("title", "")
                        preis = float(item.get("prices", {}).get("rent", {}).get("value", 0) or 0)
                        groesse = float(item.get("areas", {}).get("living", {}).get("value", 0) or 0)
                        zimmer = float(item.get("rooms", 0) or 0)
                        href = f"https://www.immowelt.de/expose/{expose_id}"
                        loc = item.get("locationAddress", {})
                        adresse = f"{loc.get('street', '')} {loc.get('houseNumber', '')}, {loc.get('city', 'München')}".strip(", ")
                        uid = listing_id(href, titel)
                        out.append(make_wohnung(uid, titel, preis, groesse, zimmer, href, "ImmoWelt", adresse, titel))
                    except Exception:
                        continue
                return out
            except Exception:
                pass

        # HTML-Fallback
        items = (soup.select("[class*='EstateItem']") or
                 soup.select("[data-testid='estate-item']") or
                 soup.select("article[class*='estate']"))
        print(f"ImmoWelt (HTML-Fallback): {len(items)} Einträge")
        for item in items[:20]:
            try:
                link = item.select_one("a[href*='/expose/']") or item.select_one("a[href]")
                if not link:
                    continue
                href = link["href"]
                if not href.startswith("http"):
                    href = "https://www.immowelt.de" + href
                if "immowelt.de" not in href:
                    continue
                titel_el = item.select_one("h2, h3, [class*='title']")
                titel = titel_el.get_text(strip=True) if titel_el else ""
                if not titel:
                    continue
                t = item.get_text(" ", strip=True)
                preis_m = re.search(r"([\d.]+(?:,\d+)?)\s*€", t)
                preis = parse_preis(preis_m.group() if preis_m else "")
                groesse = parse_groesse(t)
                zimmer = parse_zimmer(t)
                addr_el = item.select_one("[class*='address'], [class*='location']")
                adresse = addr_el.get_text(strip=True) if addr_el else ""
                uid = listing_id(href, titel)
                out.append(make_wohnung(uid, titel, preis, groesse, zimmer, href, "ImmoWelt", adresse, t))
            except Exception:
                continue

    except Exception as e:
        print(f"ImmoWelt Fehler: {e}")
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


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    print(f"=== Wohnungsmonitor Lite – {datetime.now().strftime('%d.%m.%Y %H:%M')} ===")
    print(f"Filter: max {MAX_WARM_MIETE}€ warm, min {MIN_GROESSE}qm")

    seen = load_seen()
    fahrtzeit_cache = load_fahrtzeit_cache()
    geocode_cache = load_geocode_cache()
    print(f"Bereits bekannte Inserate: {len(seen)}")

    async with httpx.AsyncClient(http2=True) as client:
        alle: list[Wohnung] = []
        # Alle Scraper parallel ausführen
        results = await asyncio.gather(
            scrape_kleinanzeigen(client),
            scrape_wggesucht(client),
            scrape_wohnungsboerse(client),
            scrape_is24_lite(client),
            scrape_immowelt_lite(client),
            scrape_mrlodge(client),
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
            neue_matches += 1
            print(f"  ✅ MATCH [score={sc:+d}]: {w.titel} | {w.preis_warm:.0f}€ | {w.groesse:.0f}qm | {w.quelle}")
            ok = await telegram(w.als_nachricht(sc), client)
            print(f"     Telegram: {'✓' if ok else '✗'}")
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
    asyncio.run(main())
