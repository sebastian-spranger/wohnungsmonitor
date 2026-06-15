#!/usr/bin/env python3
"""
München Wohnungsmonitor
=======================
Überwacht ImmobilienScout24, ImmoWelt, Kleinanzeigen, WG-Gesucht und Wohnungsboerse
alle 90 Sekunden. Sobald eine passende Wohnung erscheint → Telegram + macOS-Notification.

Schnellstart:
  1. pip install -r requirements.txt && playwright install chromium
  2. Telegram-Bot: @BotFather → /newbot → Token kopieren
  3. Chat-ID: Dem Bot schreiben, dann:
     https://api.telegram.org/bot<TOKEN>/getUpdates  → "chat":{"id":...}
  4. TELEGRAM_TOKEN und TELEGRAM_CHAT_ID unten eintragen
  5. python monitor.py
  6. python monitor.py --test   (schickt Test-Nachricht)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
#  K O N F I G U R A T I O N  – hier anpassen
# ═══════════════════════════════════════════════════════════════════════════════

TELEGRAM_TOKEN   = "8887219904:AAE6WGlD-b7qmWnuVGZGBNUMZvPh-yz8ciY"
TELEGRAM_CHAT_ID = "7647141150"

MAX_WARM_MIETE = 2000   # € Gesamtmiete warm (inkl. Nebenkosten)
# Sicherheitsabschlag: zeigt ein Inserat nur die Kaltmiete (oder unklar),
# liegt die echte Warmmiete meist 15-20% höher → niedrigeres Limit anwenden.
MAX_KALT_MIETE = 1750   # € Limit für Kaltmiete-/unklare Inserate
MIN_GROESSE    = 45     # qm Mindestfläche
MIN_ZIMMER     = 1.5    # Mindestzimmer (1.5 = 1-Zimmer mit Wohnküche)

CHECK_INTERVAL = 90     # Sekunden zwischen Checks (nicht zu niedrig!)

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

# NUR diese Stadtteile sind erlaubt – alles andere wird verworfen.
# Substrings genügen: "giesing" deckt Ober-/Untergiesing ab,
# "isarvorstadt"/"ludwigsvorstadt" auch "Ludwigsvorstadt-Isarvorstadt".
ALLOWED_KEYWORDS = {
    "lehel", "maxvorstadt", "maximilianvorstadt", "giesing",
    "ludwigsvorstadt", "isarvorstadt", "theresienwiese",
}

# Stadtteile die definitiv zu weit sind → Listings werden ignoriert
BLOCKED_KEYWORDS = {
    "feldmoching", "hasenbergl", "am hart", "moosach", "pasing", "aubing",
    "lochhausen", "langwied", "riem", "trudering", "neuperlach",
    "solln", "forstenried", "fürstenried", "allach", "untermenzing",
    "obermenzing", "daglfing", "johanneskirchen", "haar", "unterhaching",
    "oberhaching", "pullach", "taufkirchen", "grünwald", "garching",
    "unterschleißheim", "dachau", "olching", "germering", "planegg",
    "gräfelfing", "gauting", "aschheim", "kirchheim", "ismaning",
}

SEEN_FILE    = Path("seen.json")
MATCHES_FILE = Path("matches.json")

# ═══════════════════════════════════════════════════════════════════════════════

# Dependencies prüfen
def _check_deps():
    missing = []
    for pkg, import_name in [("httpx", "httpx"), ("playwright", "playwright.async_api"),
                              ("beautifulsoup4", "bs4")]:
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"\n❌ Fehlende Pakete: {', '.join(missing)}")
        print(f"   pip install {' '.join(missing)}")
        if "playwright" in missing:
            print("   playwright install chromium")
        sys.exit(1)

_check_deps()

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Browser, Page, async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(), logging.FileHandler("monitor.log")],
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  D A T E N M O D E L L
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Wohnung:
    id: str
    titel: str
    preis_warm: float       # 0 = unbekannt
    groesse: float          # 0 = unbekannt
    zimmer: float           # 0 = unbekannt
    url: str
    quelle: str
    adresse: str = ""
    verfuegbar: str = ""
    moebliert: bool = False
    mit_kueche: bool = False
    gefunden_um: str = ""
    preis_ist_warm: bool = False   # True = Preis ist Warmmiete, False = kalt/unklar

    def passt(self) -> tuple[bool, list[str]]:
        fails = []
        if self.preis_warm == 0 and self.groesse == 0:
            fails.append("Kein Preis und keine Größe – kein echtes Inserat")
        if self.preis_warm > 0:
            limit = MAX_WARM_MIETE if self.preis_ist_warm else MAX_KALT_MIETE
            if self.preis_warm > limit:
                label = "warm" if self.preis_ist_warm else "kalt/unklar"
                fails.append(f"Miete {self.preis_warm:.0f}€ ({label}) > {limit}€")
        if self.groesse > 0 and self.groesse < MIN_GROESSE:
            fails.append(f"Größe {self.groesse:.0f}qm < {MIN_GROESSE}qm")
        combined = (self.titel + " " + self.adresse).lower()
        if not any(d in combined for d in ALLOWED_KEYWORDS):
            fails.append("Nicht in Wunsch-Stadtteil (oder Stadtteil unbekannt)")
        ausland = next((a for a in AUSLAND_KEYWORDS if a in combined), None)
        if ausland:
            fails.append(f"Nicht München ('{ausland}')")
        titel_lower = self.titel.lower()
        blocked_titel = next((k for k in BLOCKED_TITLE_KEYWORDS if k in titel_lower), None)
        if blocked_titel:
            fails.append(f"Kein Angebot ('{blocked_titel}')")
        if re.match(r'^suche\b', titel_lower):
            fails.append("Kein Angebot (Gesuche)")
        if not self.titel.strip():
            fails.append("Kein Titel – kein echtes Inserat")
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
            miet_label = "warm" if self.preis_ist_warm else "kalt"
            zeilen.append(f"💰 {self.preis_warm:.0f}€ {miet_label}{pqm_str}")
        groesse_str = f"📐 {self.groesse:.0f} qm" if self.groesse > 0 else ""
        if self.zimmer > 0:
            groesse_str += f" · {self.zimmer:.0f} Zi."
        if groesse_str:
            zeilen.append(groesse_str)
        if self.adresse:
            q = self.adresse.replace(' ', '+').replace(',', '%2C')
            zeilen.append(f"📍 [{self.adresse}](https://maps.google.com/?q={q})")
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


# ═══════════════════════════════════════════════════════════════════════════════
#  N O T I F I C A T I O N E N
# ═══════════════════════════════════════════════════════════════════════════════

async def telegram_senden(text: str) -> bool:
    if TELEGRAM_TOKEN.startswith("HIER"):
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as c:
        try:
            r = await c.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            }, timeout=10)
            return r.status_code == 200
        except Exception as e:
            log.error(f"Telegram-Fehler: {e}")
            return False


def macos_notification(titel: str, text: str):
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{text}" with title "{titel}" sound name "Glass"'
        ], timeout=5, capture_output=True)
    except Exception:
        pass


async def benachrichtigen(w: Wohnung, score: int = 0):
    ok = await telegram_senden(w.als_nachricht(score))
    log.info(f"     Telegram: {'✓' if ok else '⚠ nicht konfiguriert'}")
    preis_str = f"{w.preis_warm:.0f}€" if w.preis_warm > 0 else "Preis unbekannt"
    macos_notification(f"🏠 Neue Wohnung! ({w.quelle})", f"{w.titel} – {preis_str}")

    # Backup in matches.json
    matches = []
    if MATCHES_FILE.exists():
        try:
            matches = json.loads(MATCHES_FILE.read_text())
        except Exception:
            pass
    matches.append(w.to_dict())
    MATCHES_FILE.write_text(json.dumps(matches, ensure_ascii=False, indent=2))


# ═══════════════════════════════════════════════════════════════════════════════
#  D E D U P L I K A T I O N
# ═══════════════════════════════════════════════════════════════════════════════

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text())
            return set(data.get("ids", []))
        except Exception:
            return set()
    return set()


def save_seen(seen: set[str]):
    ids = list(seen)
    if len(ids) > 50_000:
        ids = ids[-50_000:]  # verhindert unbegrenztes Wachstum
    SEEN_FILE.write_text(json.dumps({"ids": ids}))


def listing_id(url: str, titel: str) -> str:
    return hashlib.md5((url + titel).encode()).hexdigest()[:14]


# ═══════════════════════════════════════════════════════════════════════════════
#  P A R S E R   H E L F E R
# ═══════════════════════════════════════════════════════════════════════════════

def parse_preis(text: str) -> float:
    if not text:
        return 0.0
    # "1.850,00 €" → "1850.00"
    text = re.sub(r"[€EUReur\s]", "", text)
    text = text.replace(".", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", text)
    return float(m.group()) if m else 0.0


def parse_groesse(text: str) -> float:
    if not text:
        return 0.0
    text = text.replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:m²|qm|m2)", text, re.IGNORECASE)
    return float(m.group(1)) if m else 0.0


def parse_zimmer(text: str) -> float:
    if not text:
        return 0.0
    text = text.replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else 0.0


def kueche_moebliert(text: str) -> tuple[bool, bool]:
    t = text.lower()
    kueche = any(w in t for w in ["küche", "kueche", "einbauküche", " ekü", "kitchen"])
    moebliert = any(w in t for w in ["möbliert", "moebliert", "furnished", "möbel"])
    return kueche, moebliert


def ist_warmmiete(text: str) -> bool:
    """True, wenn der angezeigte Preis erkennbar die Warmmiete ist (inkl. NK)."""
    t = text.lower().replace("warmwasser", "")  # 'Warmwasser' nicht als 'warm' werten
    schluessel = ["warmmiete", "gesamtmiete", "inkl. nk", "inkl. nebenkosten",
                  "inklusive nebenkosten", "brutto", "all-in", "warmmieten"]
    return any(k in t for k in schluessel) or bool(re.search(r"\bwarm\b", t))


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


# ═══════════════════════════════════════════════════════════════════════════════
#  S C R A P E R
# ═══════════════════════════════════════════════════════════════════════════════

async def _dismiss_cookies(page: Page):
    """Versucht Cookie-Banner wegzuklicken (ignoriert Fehler)."""
    selectors = [
        "[data-testid='uc-accept-all-button']",
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Akzeptieren')",
        "#onetrust-accept-btn-handler",
        ".accept-all",
    ]
    for sel in selectors:
        try:
            await page.click(sel, timeout=2000)
            await page.wait_for_timeout(500)
            return
        except Exception:
            continue


async def scrape_immoscout(page: Page) -> list[Wohnung]:
    url = (
        "https://www.immobilienscout24.de/Suche/de/bayern/muenchen/wohnung-mieten"
        f"?price=-{MAX_WARM_MIETE}.0&livingspace={MIN_GROESSE}.0-&sorting=2"
    )
    wohnungen = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)
        await _dismiss_cookies(page)
        await page.wait_for_timeout(1000)

        # Try to extract listing data from embedded JS/JSON
        try:
            raw = await page.evaluate("""() => {
                const scripts = [...document.querySelectorAll('script:not([src])')];
                for (const s of scripts) {
                    const t = s.textContent || '';
                    if (t.length > 1000 && (t.includes('"exposeId"') || t.includes('"resultListItems"') || t.includes('"realEstateId"'))) {
                        return t;
                    }
                }
                return document.getElementById('result-list-section')?.innerHTML || null;
            }""")
            if raw and len(raw) > 500:
                # Parse JSON if it looks like JSON
                import json as _json
                for pattern in [
                    r'"resultListItems"\s*:\s*(\[.*?\])\s*,\s*"[a-z]',
                    r'"exposees"\s*:\s*(\[.*?\])',
                ]:
                    m = re.search(pattern, raw, re.DOTALL)
                    if m:
                        try:
                            items_data = _json.loads(m.group(1))
                            for item in items_data[:20]:
                                try:
                                    expose_id = (item.get("id") or item.get("exposeId") or
                                                item.get("realEstateId") or "")
                                    if not expose_id:
                                        continue
                                    href = f"https://www.immobilienscout24.de/expose/{expose_id}"
                                    titel = (item.get("title") or
                                            item.get("realEstate", {}).get("title") or "")
                                    price_data = (item.get("realEstate", {}).get("price") or
                                                 item.get("price") or {})
                                    preis = float(price_data.get("value", 0) or 0)
                                    groesse = float((item.get("realEstate", {}) or item).get("livingSpace", 0) or 0)
                                    zimmer = float((item.get("realEstate", {}) or item).get("numberOfRooms", 0) or 0)
                                    addr = (item.get("realEstate", {}) or item).get("address", {})
                                    adresse = f"{addr.get('street', '')} {addr.get('houseNumber', '')}, {addr.get('city', 'München')}".strip(", ")
                                    kueche, moebliert = kueche_moebliert(titel)
                                    uid = listing_id(href, titel)
                                    wohnungen.append(Wohnung(
                                        id=uid, titel=titel, preis_warm=preis, groesse=groesse,
                                        zimmer=zimmer, url=href, quelle="ImmobilienScout24",
                                        adresse=adresse, mit_kueche=kueche, moebliert=moebliert,
                                        gefunden_um=datetime.now().strftime("%d.%m. %H:%M"),
                                        preis_ist_warm=ist_warmmiete(titel),
                                    ))
                                except Exception:
                                    continue
                            if wohnungen:
                                log.debug(f"IS24 (JSON): {len(wohnungen)} Einträge")
                                return wohnungen
                        except Exception:
                            pass
        except Exception as e:
            log.debug(f"IS24 page.evaluate() Fehler: {e}")

        content = await page.content()
        soup = BeautifulSoup(content, "html.parser")

        # IS24 speichert Ergebnisse auch als JSON im DOM
        script = soup.find("script", string=re.compile(r"resultList"))
        if script:
            try:
                m = re.search(r'"resultList"\s*:\s*(\{.*?\})\s*,\s*"[a-z]', script.string, re.DOTALL)
                # Fallback: parse direkt aus HTML-Karten
            except Exception:
                pass

        items = (
            soup.select("li[class*='result-list__listing']") or
            soup.select("article[class*='result-list']") or
            soup.select("[data-testid='result-list-entry']") or
            soup.select(".result-list-entry")
        )
        log.debug(f"IS24: {len(items)} Karten")

        for item in items[:25]:
            try:
                link = item.select_one("a[href*='/expose/']")
                if not link:
                    continue
                href = link["href"]
                if not href.startswith("http"):
                    href = "https://www.immobilienscout24.de" + href

                titel_el = item.select_one("h2, h3, [class*='title']")
                titel = titel_el.get_text(strip=True) if titel_el else link.get_text(strip=True)

                text = item.get_text(" ", strip=True)
                preis_m = re.search(r"([\d.]+(?:,\d+)?)\s*€", text)
                preis = parse_preis(preis_m.group() if preis_m else "")
                groesse = parse_groesse(text)
                zimmer_m = re.search(r"(\d[\d,.]?)\s*Zi(?:mmer)?", text, re.IGNORECASE)
                zimmer = parse_zimmer(zimmer_m.group(1) if zimmer_m else "")

                addr_el = item.select_one("[class*='address'], [data-testid*='address']")
                adresse = addr_el.get_text(strip=True) if addr_el else ""

                kueche, moebliert = kueche_moebliert(text)
                uid = listing_id(href, titel)
                wohnungen.append(Wohnung(
                    id=uid, titel=titel, preis_warm=preis, groesse=groesse,
                    zimmer=zimmer, url=href, quelle="ImmobilienScout24",
                    adresse=adresse, mit_kueche=kueche, moebliert=moebliert,
                    gefunden_um=datetime.now().strftime("%d.%m. %H:%M"),
                    preis_ist_warm=ist_warmmiete(text),
                ))
            except Exception as e:
                log.debug(f"IS24 item-Fehler: {e}")

    except Exception as e:
        log.warning(f"IS24 Fehler: {e}")
    return wohnungen


async def scrape_immowelt(page: Page) -> list[Wohnung]:
    url = (
        "https://www.immowelt.de/suche/muenchen/wohnungen/mieten"
        f"?ma={MAX_WARM_MIETE}&mia={MIN_GROESSE}&sort=createdate"
    )
    wohnungen = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)
        await _dismiss_cookies(page)
        await page.wait_for_timeout(1000)

        # Try __NEXT_DATA__ extraction via evaluate
        try:
            next_data_str = await page.evaluate("""() => {
                const el = document.querySelector('script#__NEXT_DATA__');
                return el ? el.textContent : null;
            }""")
            if next_data_str:
                import json as _json
                data = _json.loads(next_data_str)
                props = data.get("props", {}).get("pageProps", {})
                listings = (props.get("listings", []) or
                            props.get("searchResult", {}).get("listings", []) or [])
                if listings:
                    log.debug(f"ImmoWelt (JSON via evaluate): {len(listings)} Einträge")
                    for item in listings[:20]:
                        try:
                            expose_id = item.get("globalObjectKey", "") or item.get("id", "")
                            titel = item.get("title", "")
                            preis = float(item.get("prices", {}).get("rent", {}).get("value", 0) or 0)
                            groesse = float(item.get("areas", {}).get("living", {}).get("value", 0) or 0)
                            zimmer = float(item.get("rooms", 0) or 0)
                            href = f"https://www.immowelt.de/expose/{expose_id}"
                            loc = item.get("locationAddress", {})
                            adresse = f"{loc.get('street', '')} {loc.get('houseNumber', '')}, München".strip(", ")
                            kueche, moebliert = kueche_moebliert(titel)
                            uid = listing_id(href, titel)
                            wohnungen.append(Wohnung(
                                id=uid, titel=titel, preis_warm=preis, groesse=groesse,
                                zimmer=zimmer, url=href, quelle="ImmoWelt",
                                adresse=adresse, mit_kueche=kueche, moebliert=moebliert,
                                gefunden_um=datetime.now().strftime("%d.%m. %H:%M"),
                                preis_ist_warm=ist_warmmiete(titel),
                            ))
                        except Exception:
                            continue
                    if wohnungen:
                        return wohnungen
        except Exception as e:
            log.debug(f"ImmoWelt page.evaluate() Fehler: {e}")

        content = await page.content()
        soup = BeautifulSoup(content, "html.parser")

        items = (
            soup.select("[class*='EstateItem']") or
            soup.select("[data-testid='estate-item']") or
            soup.select("article[class*='estate']") or
            soup.select(".SearchList-module__listItem")
        )
        log.debug(f"ImmoWelt: {len(items)} Karten")

        for item in items[:25]:
            try:
                link = item.select_one("a[href*='/expose/'], a[href*='immowelt.de/expose']")
                if not link:
                    link = item.select_one("a[href]")
                if not link:
                    continue
                href = link["href"]
                if not href.startswith("http"):
                    href = "https://www.immowelt.de" + href
                if "immowelt.de" not in href:
                    continue

                titel_el = item.select_one("h2, h3, [class*='title'], [class*='Title']")
                titel = titel_el.get_text(strip=True) if titel_el else item.get_text(strip=True)[:80]

                text = item.get_text(" ", strip=True)
                preis_m = re.search(r"([\d.]+(?:,\d+)?)\s*€", text)
                preis = parse_preis(preis_m.group() if preis_m else "")
                groesse = parse_groesse(text)
                zimmer_m = re.search(r"(\d[\d,.]?)\s*Zimmer", text, re.IGNORECASE)
                zimmer = parse_zimmer(zimmer_m.group(1) if zimmer_m else "")

                addr_el = item.select_one("[class*='address'], [class*='Address'], [class*='location']")
                adresse = addr_el.get_text(strip=True) if addr_el else ""

                kueche, moebliert = kueche_moebliert(text)
                uid = listing_id(href, titel)
                wohnungen.append(Wohnung(
                    id=uid, titel=titel, preis_warm=preis, groesse=groesse,
                    zimmer=zimmer, url=href, quelle="ImmoWelt",
                    adresse=adresse, mit_kueche=kueche, moebliert=moebliert,
                    gefunden_um=datetime.now().strftime("%d.%m. %H:%M"),
                    preis_ist_warm=ist_warmmiete(text),
                ))
            except Exception as e:
                log.debug(f"ImmoWelt item-Fehler: {e}")

    except Exception as e:
        log.warning(f"ImmoWelt Fehler: {e}")
    return wohnungen


async def scrape_kleinanzeigen(client: httpx.AsyncClient) -> list[Wohnung]:
    # l3207 = München, c203 = Wohnungen zur Miete
    url = (
        f"https://www.kleinanzeigen.de/s-wohnung-mieten/muenchen/c203l6411"
        f"?maxPrice={MAX_WARM_MIETE}&minSize={MIN_GROESSE}"
        f"&sortingField=INSERTION_TIME&pageNum=1"
    )
    wohnungen = []
    try:
        r = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")

        items = soup.select("article.aditem") or soup.select("[class*='aditem']")
        log.debug(f"Kleinanzeigen: {len(items)} Karten")

        for item in items[:25]:
            try:
                link = item.select_one("a[href*='/s-anzeige/']")
                if not link:
                    continue
                href = "https://www.kleinanzeigen.de" + link["href"]

                titel_el = item.select_one("h2, .ellipsis, [class*='title']")
                titel = titel_el.get_text(strip=True) if titel_el else link.get_text(strip=True)

                preis_el = item.select_one(".aditem-details strong, .price-small, [class*='price']")
                preis = parse_preis(preis_el.get_text(strip=True) if preis_el else "")

                detail = item.select_one(".aditem-details, [class*='detail']")
                detail_text = detail.get_text(" ") if detail else ""
                groesse = parse_groesse(detail_text)
                zimmer_m = re.search(r"(\d[\d,.]?)\s*Zimmer", detail_text, re.IGNORECASE)
                zimmer = parse_zimmer(zimmer_m.group(1) if zimmer_m else "")

                addr_el = item.select_one(".aditem-main--top--left, [class*='location']")
                adresse = addr_el.get_text(strip=True) if addr_el else ""

                # Nur München – PLZ 8xxxx = München, oder Name enthält München
                plz_m = re.search(r'\b8[01]\d{3}\b', adresse)
                if adresse and "münchen" not in adresse.lower() and "munich" not in adresse.lower() and not plz_m:
                    log.debug(f"Kleinanzeigen: übersprungen ({adresse} – nicht München)")
                    continue

                text = (titel + detail_text).lower()
                kueche, moebliert = kueche_moebliert(text)
                uid = listing_id(href, titel)
                wohnungen.append(Wohnung(
                    id=uid, titel=titel, preis_warm=preis, groesse=groesse,
                    zimmer=zimmer, url=href, quelle="Kleinanzeigen",
                    adresse=adresse or "München", mit_kueche=kueche, moebliert=moebliert,
                    gefunden_um=datetime.now().strftime("%d.%m. %H:%M"),
                    preis_ist_warm=ist_warmmiete(text),
                ))
            except Exception as e:
                log.debug(f"Kleinanzeigen item-Fehler: {e}")

    except Exception as e:
        log.warning(f"Kleinanzeigen Fehler: {e}")
    return wohnungen


async def scrape_wggesucht(client: httpx.AsyncClient) -> list[Wohnung]:
    # Kategorie 2 = Wohnungen (nicht WG-Zimmer)
    url = (
        "https://www.wg-gesucht.de/wohnungen-in-Muenchen.90.2.1.0.html"
        f"?offer_filter=1&noDeact=1&city_id=90&category=2"
        f"&rent_type=0&sMin={MIN_GROESSE}&rMax={MAX_WARM_MIETE}"
        "&sort_column=created_at&sort_order=1"
    )
    wohnungen = []
    try:
        r = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")

        items = (
            soup.select("[class*='wgg_card']") or
            soup.select("[class*='offer_list_item']") or
            soup.select("tr.list-body-row")
        )
        log.debug(f"WG-Gesucht: {len(items)} Karten")

        for item in items[:25]:
            try:
                link = item.select_one("a[href*='/wohnung'], a[href*='/wohnungen']")
                if not link:
                    link = item.select_one("a[href]")
                if not link:
                    continue
                href = link["href"]
                if not href.startswith("http"):
                    href = "https://www.wg-gesucht.de" + href

                titel_el = (item.select_one("h3, h4, [class*='headline']") or
                            item.select_one("[class*='title']") or
                            item.select_one("strong"))
                titel = (titel_el.get_text(strip=True) if titel_el else link.get_text(strip=True)[:80]).strip()

                text = item.get_text(" ", strip=True)
                preis_m = re.search(r"([\d.]+(?:,\d+)?)\s*€", text)
                preis = parse_preis(preis_m.group() if preis_m else "")
                groesse = parse_groesse(text)
                zimmer_m = re.search(r"(\d[\d,.]?)\s*Zimmer", text, re.IGNORECASE)
                zimmer = parse_zimmer(zimmer_m.group(1) if zimmer_m else "")

                adresse = wggesucht_adresse(item)

                kueche, moebliert = kueche_moebliert(text)
                uid = listing_id(href, titel)
                wohnungen.append(Wohnung(
                    id=uid, titel=titel, preis_warm=preis, groesse=groesse,
                    zimmer=zimmer, url=href, quelle="WG-Gesucht",
                    adresse=adresse, mit_kueche=kueche, moebliert=moebliert,
                    gefunden_um=datetime.now().strftime("%d.%m. %H:%M"),
                    preis_ist_warm=ist_warmmiete(text),
                ))
            except Exception as e:
                log.debug(f"WG-Gesucht item-Fehler: {e}")

    except Exception as e:
        log.warning(f"WG-Gesucht Fehler: {e}")
    return wohnungen


async def scrape_wohnungsboerse(client: httpx.AsyncClient) -> list[Wohnung]:
    url = "https://www.wohnungsboerse.net/Muenchen/mieten/wohnungen"
    wohnungen = []
    try:
        r = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")

        # Each listing is a full <a> tag pointing to immodetail
        items = soup.select("a[href*='/immodetail/']")
        log.debug(f"Wohnungsboerse: {len(items)} Karten")

        for item in items[:25]:
            try:
                href = item["href"]
                if not href.startswith("http"):
                    href = "https://www.wohnungsboerse.net" + href

                text = item.get_text(" ", strip=True)
                if not text:
                    continue
                titel = re.sub(r"\s+", " ", text.split("Kaltmiete")[0]).strip()[:120]
                if not titel:
                    continue

                preis_m = re.search(r"([\d.]+(?:,\d+)?)\s*€", text)
                preis = parse_preis(preis_m.group() if preis_m else "")
                groesse = parse_groesse(text)
                zimmer_m = re.search(r"(\d[\d,.]?)\s*Zimmer", text, re.IGNORECASE)
                zimmer = parse_zimmer(zimmer_m.group(1) if zimmer_m else "")

                adresse_m = re.search(r"München\s*[-–]\s*([^\n€\d]+)", text)
                adresse = "München" + (" - " + adresse_m.group(1).strip() if adresse_m else "")

                kueche, moebliert = kueche_moebliert(text)
                uid = listing_id(href, titel)
                wohnungen.append(Wohnung(
                    id=uid, titel=titel, preis_warm=preis, groesse=groesse,
                    zimmer=zimmer, url=href, quelle="Wohnungsboerse",
                    adresse=adresse, mit_kueche=kueche, moebliert=moebliert,
                    gefunden_um=datetime.now().strftime("%d.%m. %H:%M"),
                    preis_ist_warm=ist_warmmiete(text),
                ))
            except Exception as e:
                log.debug(f"Wohnungsboerse item-Fehler: {e}")

    except Exception as e:
        log.warning(f"Wohnungsboerse Fehler: {e}")
    return wohnungen


# ═══════════════════════════════════════════════════════════════════════════════
#  S C O R E   &   C R O S S - P O R T A L - D E D U P
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
#  H A U P T S C H L E I F E
# ═══════════════════════════════════════════════════════════════════════════════

async def check_runde(browser: Browser, client: httpx.AsyncClient, seen: set[str]) -> int:
    alle: list[Wohnung] = []

    # Playwright-Seiten (JS-schwer)
    page = await browser.new_page()
    await page.set_extra_http_headers({"Accept-Language": "de-DE,de;q=0.9"})
    try:
        is24 = await scrape_immoscout(page)
        alle.extend(is24)
        log.info(f"IS24:         {len(is24):3d} Inserate")

        await page.wait_for_timeout(1500)

        iw = await scrape_immowelt(page)
        alle.extend(iw)
        log.info(f"ImmoWelt:     {len(iw):3d} Inserate")
    finally:
        await page.close()

    # httpx-Seiten (direktes HTML)
    ka = await scrape_kleinanzeigen(client)
    alle.extend(ka)
    log.info(f"Kleinanz.:    {len(ka):3d} Inserate")

    wg = await scrape_wggesucht(client)
    alle.extend(wg)
    log.info(f"WG-Gesucht:   {len(wg):3d} Inserate")

    wb = await scrape_wohnungsboerse(client)
    alle.extend(wb)
    log.info(f"Wohnungsbö.:  {len(wb):3d} Inserate")

    # Cross-portal deduplication
    alle = dedup(alle)

    # Precompute scores
    scores = {w.id: berechne_score(w) for w in alle}

    # Collect new matches, sort by score descending before sending
    neue_wohnungen = []
    for w in alle:
        if w.id in seen:
            continue
        seen.add(w.id)
        passt, gruende = w.passt()
        if passt:
            neue_wohnungen.append(w)
        else:
            log.debug(f"  ✗ {w.titel[:60]} → {', '.join(gruende)}")

    neue_wohnungen.sort(key=lambda w: scores.get(w.id, 0), reverse=True)

    neue_matches = 0
    for w in neue_wohnungen:
        sc = scores.get(w.id, 0)
        neue_matches += 1
        log.info(f"  ✅ MATCH [score={sc:+d}]: {w.titel} | {w.preis_warm:.0f}€ | {w.groesse:.0f}qm | {w.quelle}")
        await benachrichtigen(w, sc)

    save_seen(seen)
    return neue_matches


async def run_test():
    """Sendet eine Test-Nachricht um Telegram zu prüfen."""
    print("Sende Test-Nachricht...")
    w = Wohnung(
        id="test", titel="Testinserat – bitte ignorieren",
        preis_warm=1850.0, groesse=52.0, zimmer=2.0,
        url="https://www.immobilienscout24.de",
        quelle="Test", adresse="Maxvorstadt, München",
        verfuegbar="sofort", moebliert=True, mit_kueche=True,
        gefunden_um=datetime.now().strftime("%d.%m. %H:%M"),
        preis_ist_warm=True,
    )
    ok = await telegram_senden(w.als_nachricht(berechne_score(w)))
    macos_notification("🏠 Wohnungsmonitor Test", "Telegram-Test-Nachricht gesendet")
    if ok:
        print("✅ Telegram-Nachricht erfolgreich gesendet!")
    else:
        print("❌ Telegram nicht konfiguriert oder Fehler.")
        print("   Trage TELEGRAM_TOKEN und TELEGRAM_CHAT_ID in monitor.py ein.")
    print("✅ macOS-Notification gesendet (wenn Berechtigungen OK)")


async def main():
    if "--test" in sys.argv:
        await run_test()
        return

    log.info("═" * 55)
    log.info("  München Wohnungsmonitor")
    log.info(f"  Filter: max {MAX_WARM_MIETE}€ warm · min {MIN_GROESSE}qm")
    log.info(f"  Quellen: IS24, ImmoWelt, Kleinanzeigen, WG-Gesucht, Wohnungsboerse")
    log.info(f"  Check alle {CHECK_INTERVAL}s")
    log.info("═" * 55)

    if TELEGRAM_TOKEN.startswith("HIER"):
        log.warning("⚠  Telegram nicht konfiguriert – nur macOS-Notifications aktiv")

    seen = load_seen()
    log.info(f"Bereits bekannte Inserate: {len(seen)} (werden übersprungen)")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        async with httpx.AsyncClient() as client:
            runde = 0
            while True:
                runde += 1
                ts = datetime.now().strftime("%H:%M:%S")
                log.info(f"\n{'─'*40}")
                log.info(f"Runde {runde} · {ts}")
                try:
                    matches = await check_runde(browser, client, seen)
                    if matches:
                        log.info(f"🎉 {matches} neue Match{'es' if matches > 1 else ''}!")
                    else:
                        log.info(f"Keine neuen Matches. Nächster Check in {CHECK_INTERVAL}s.")
                except Exception as e:
                    log.error(f"Fehler in Runde {runde}: {e}", exc_info=True)

                await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("\n👋 Monitor gestoppt.")
