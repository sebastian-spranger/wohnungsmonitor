# München Wohnungsmonitor

Echtzeit-Scraper für Münchner Mietwohnungen. Durchsucht 5 Portale alle 90 Sekunden
und schickt bei einem Match sofort eine Telegram-Push-Nachricht + macOS-Notification.

## 🧠 AIOS — Central Brain
The **AIOS** central brain (personal AI operating system) lives at `../aios/` — home of
cross-project knowledge, the dashboard and the brain. Use it if your task reaches
beyond this project; it's optional, not required. If you do, `../aios/CLAUDE.md` is the starting point.
<!-- AIOS_POINTER -->

## Was der Monitor macht

- Portale: ImmobilienScout24, ImmoWelt, Kleinanzeigen.de, WG-Gesucht (nur Wohnungen), Wohnungsboerse.net
- Filtert nach: max 2.300€ warm, min 45qm, schließt Außenbezirke aus
- Neue Matches → Telegram-Bot `@Noapartmentsbot` + macOS-Ton-Notification
- Alle Matches werden in `matches.json` gespeichert (Backup)
- Bereits gesehene Inserate werden in `seen.json` gemerkt und übersprungen
- **Mehrere Empfänger, eigene Filter**: Jeder Kontakt des Bots kann ihm einfach
  schreiben, wonach er sucht (z.B. „max 1600 warm, ab 2 Zimmer, min 55qm, Schwabing
  oder Maxvorstadt") – DeepSeek übersetzt das in Filter, die nur für diesen Kontakt
  gelten (`user_filters.json`, siehe unten). `/status` zeigt die eigenen Filter,
  `/reset` setzt sie zurück auf den Standard.

## Dateien

```
monitor.py        – Hauptskript (Scraper + Filter + Notifier)
requirements.txt  – Python-Abhängigkeiten
setup.sh          – Einmaliges Setup-Skript
seen.json           – Automatisch generiert: gesehene Inserate-IDs
matches.json        – Automatisch generiert: alle bisherigen Matches
user_filters.json   – Automatisch generiert: eigene Filter pro Telegram-Empfänger
telegram_offset.json – Automatisch generiert: zuletzt verarbeitete Telegram-Nachricht
monitor.log         – Automatisch generiert: Laufzeit-Log
```

## Konfiguration (oben in monitor.py)

```python
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")     # via env / GitHub Actions secret
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")   # via env / GitHub Actions secret

MAX_WARM_MIETE = 2300   # € warm
MIN_GROESSE    = 45     # qm
CHECK_INTERVAL = 90     # Sekunden
```

## Betrieb – zwei Modi

| Modus | Wie | Portale | Intervall | Kosten |
|-------|-----|---------|-----------|--------|
| **GitHub Actions** (empfohlen) | Cloud, PC aus | Alle 5 | alle 5 Min | kostenlos |
| **Lokal** | Mac läuft | Alle 5 + Playwright | 90 Sek | kostenlos |

---

## Cloud-Betrieb via GitHub Actions (kein PC nötig)

### Einmaliges Setup (~10 Minuten)

**1. GitHub-Repo erstellen**
- github.com → „New repository" → Name z.B. `wohnungsmonitor`
- **Public** (dann unbegrenzt kostenlose Minuten) oder Private (2000 Min/Monat gratis)
- Ohne README erstellen

**2. Code hochladen**
```bash
cd ~/Documents/Projects/wohnungsmonitor
git init
git add monitor_lite.py .github/ .gitignore requirements.txt AGENTS.md
git commit -m "init"
git remote add origin https://github.com/DEIN_USERNAME/wohnungsmonitor.git
git push -u origin main
```

**3. GitHub Secrets setzen**
- Repo-Seite → Settings → Secrets and variables → Actions → „New repository secret"
- Secret 1: `TELEGRAM_TOKEN` = `<dein Bot-Token von @BotFather>`
- Secret 2: `TELEGRAM_CHAT_ID` = `<deine Telegram Chat-ID>` (mehrere: kommagetrennt)
- Secret 3 (optional): `DEEPSEEK_API_KEY` = `<API-Key von platform.deepseek.com>`
  – nur nötig, wenn Empfänger ihre Filter per Telegram-Nachricht selbst setzen sollen

**4. Actions aktivieren**
- Repo → Tab „Actions" → „I understand my workflows, enable them"

**Fertig.** Ab sofort läuft der Monitor alle 5 Minuten automatisch.

**Manuell starten** (z.B. zum Testen):
- Repo → Actions → „Wohnungsmonitor" → „Run workflow"

**Logs ansehen:**
- Repo → Actions → letzter Run → Job „check" → Schritt „Monitor ausführen"

---

## Erstmalige Einrichtung (Lokal)

### 1. Dependencies installieren
```bash
cd ~/Documents/Projects/wohnungsmonitor
bash setup.sh
```
Installiert: playwright, beautifulsoup4, httpx + Chromium-Browser (headless).

### 2. Telegram Chat-ID herausfinden (einmalig)

1. Schreib dem Bot `/start` in Telegram: [@Noapartmentsbot](https://t.me/Noapartmentsbot)
2. Öffne diese URL im Browser:
   ```
   https://api.telegram.org/bot<TELEGRAM_TOKEN>/getUpdates
   ```
3. In der JSON-Antwort steht `"chat":{"id": 123456789}` – diese Zahl in `monitor.py` eintragen

### 3. Testen
```bash
source .venv/bin/activate
python monitor.py --test
```
Schickt eine Test-Wohnung per Telegram und macOS-Notification.

### 4. Monitor starten
```bash
source .venv/bin/activate
python monitor.py
```

### Im Hintergrund laufen lassen (Terminal bleibt frei)
```bash
source .venv/bin/activate
nohup python monitor.py > monitor.log 2>&1 &
echo "Monitor läuft im Hintergrund. PID: $!"
```

Stoppen:
```bash
pkill -f "python monitor.py"
```

## Filter anpassen

**Ausgeschlossene Bezirke** – in `monitor.py` in der Menge `BLOCKED_KEYWORDS`:
- Aktuell ausgeschlossen: Pasing, Riem, Neuperlach, Solln, Dachau, Garching, etc.
- Zum Hinzufügen: neuen Bezirksnamen (kleingeschrieben) in die Menge eintragen

**Erlaubte Bezirke** – werden nicht explizit gefiltert; der Suchumkreis ergibt sich
durch die Portal-URLs (Stadt München) + ausgeschlossene Bezirke.

## Troubleshooting

**"0 Inserate" von IS24 oder ImmoWelt**
→ Die Seiten sind JS-schwer; beim ersten Start lädt Chromium einmalig länger.
→ IS24 schaltet manchmal CAPTCHA vor. Log prüfen (`monitor.log`), bei Bedarf
  `CHECK_INTERVAL` auf 120–180 erhöhen.

**Telegram-Nachrichten kommen nicht an**
→ `python monitor.py --test` ausführen und Ausgabe prüfen
→ Chat-ID nochmal verifizieren (muss numerisch sein, z.B. `123456789`)
→ Bot muss von dir kontaktiert worden sein (Privacymodus)

**Speicher wächst zu groß**
→ `seen.json` löschen – dann werden alle aktuellen Inserate einmalig neu gemeldet
→ `matches.json` löschen – löscht die Match-Historie (schadet dem Monitor nicht)

## Portale direkt aufrufen

Links für manuelle Kontrolle (bereits mit Filtern):

- [IS24 – Wohnungen mieten München](https://www.immobilienscout24.de/Suche/de/bayern/muenchen/wohnung-mieten?price=-2300.0&livingspace=45.0-&sorting=2)
- [ImmoWelt – München](https://www.immowelt.de/suche/muenchen/wohnungen/mieten?ma=2300&mia=45&sort=createdate)
- [Kleinanzeigen – Wohnungen München](https://www.kleinanzeigen.de/s-wohnung-mieten/muenchen/c203l3207)
- [WG-Gesucht – Wohnungen München](https://www.wg-gesucht.de/wohnungen-in-Muenchen.90.2.1.0.html)
- [Wohnungsboerse – München](https://www.wohnungsboerse.net/search/index?estateType=1&marketingType=rent&city=M%C3%BCnchen&priceTo=2300&areaFrom=45)
