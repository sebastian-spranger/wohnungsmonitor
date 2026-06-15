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
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

# ─── Konfiguration ────────────────────────────────────────────────────────────
# Werte kommen aus GitHub Secrets (nie im Code speichern!)
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MAX_WARM_MIETE = int(os.environ.get("MAX_WARM_MIETE", "2300"))
MIN_GROESSE    = int(os.environ.get("MIN_GROESSE",    "45"))

SEEN_FILE    = Path("seen.json")
MATCHES_FILE = Path("matches.json")

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
    "tausch", "wohnungstausch", "tauschwohnung",
    "suche wohnung", "suche eine wohnung", "suche dringend wohnung",
    "suche 1-zimmer", "suche 2-zimmer", "suche 3-zimmer",
    "suche apartment", "wir suchen", "ich suche",
    "biete tausch", "biete wg-zimmer",
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

    def passt(self) -> tuple[bool, list[str]]:
        fails = []
        if self.preis_warm == 0 and self.groesse == 0:
            fails.append("Kein Preis und keine Größe – kein echtes Inserat")
        if self.preis_warm > 0 and self.preis_warm > MAX_WARM_MIETE:
            fails.append(f"Preis {self.preis_warm:.0f}€ > {MAX_WARM_MIETE}€")
        if self.groesse > 0 and self.groesse < MIN_GROESSE:
            fails.append(f"Größe {self.groesse:.0f}qm < {MIN_GROESSE}qm")
        combined = (self.titel + " " + self.adresse).lower()
        blocked_ort = next((b for b in BLOCKED_KEYWORDS if b in combined), None)
        if blocked_ort:
            fails.append(f"Bezirk '{blocked_ort}' zu weit")
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
            zeilen.append(f"💰 {self.preis_warm:.0f}€ warm{pqm_str}")
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


# ─── Notifier ────────────────────────────────────────────────────────────────

async def telegram(text: str, client: httpx.AsyncClient) -> bool:
    if not TELEGRAM_TOKEN:
        print("⚠  TELEGRAM_TOKEN nicht gesetzt")
        return False
    try:
        r = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "Markdown", "disable_web_page_preview": False},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"Telegram-Fehler: {e}")
        return False


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


def make_wohnung(uid, titel, preis, groesse, zimmer, url, quelle, adresse, text) -> Wohnung:
    k, m = kueche_moebliert(text)
    return Wohnung(
        id=uid, titel=titel, preis_warm=preis, groesse=groesse, zimmer=zimmer,
        url=url, quelle=quelle, adresse=adresse, mit_kueche=k, moebliert=m,
        gefunden_um=datetime.now().strftime("%d.%m. %H:%M"),
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
                addr_el = item.select_one("[class*='col-sm-5'], [class*='location']")
                adresse = addr_el.get_text(strip=True) if addr_el else "München"
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


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    print(f"=== Wohnungsmonitor Lite – {datetime.now().strftime('%d.%m.%Y %H:%M')} ===")
    print(f"Filter: max {MAX_WARM_MIETE}€ warm, min {MIN_GROESSE}qm")

    seen = load_seen()
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
