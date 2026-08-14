# Zweiter Mac als 24/7-Wohnungsmonitor (inkl. IS24)

Dieser Mac übernimmt den Dauerbetrieb der Engine — **mit IS24**, weil er von
einer Heim-IP läuft (die Oracle-VM wird von IS24 per Captcha blockiert).

> ⚠️ **Wichtig:** Es läuft immer nur EINE Engine zur Zeit (sonst doppelte
> Telegram-Nachrichten und zerstrittene Zustell-Historien). Der Mac übernimmt,
> die VM wird gestoppt (bleibt als Fallback).
> `monitor.py` (der alte lokale Scraper) wird NICHT genutzt — der Mac fährt
> dieselbe Multi-User-Engine wie die VM (`monitor_lite.py --serve`) mit der
> von der VM übernommenen Datenbank (Sebastian + Finn + Filter + Zustellungen).

Voraussetzungen: ein Mac (auch älter, macOS 12+), Netzwerk, läuft am Strom.

---

## 1. SSH-Zugriff einrichten

**Auf dem neuen Mac** (einmalig, im Terminal des neuen Macs):

```bash
# Remote Login aktivieren (Systemeinstellungen → Allgemein → Freigaben → Remote Login)
sudo systemsetup -setremotelogin on

# IP des Macs anzeigen (für Zugriffe aus dem LAN):
ipconfig getifaddr en0        # z.B. 192.168.1.50
```

**Vom Haupt-Rechner** passwortlosen SSH-Zugang einrichten (einmalig):

```bash
# Schlüssel kopieren (ersetzt das Passwort-Login)
ssh-copy-id <MAC-BENUTZER>@<MAC-IP>
# Test:
ssh <MAC-BENUTZER>@<MAC-IP> 'echo hallo'
```

**Von außerhalb (optional, empfohlen): Tailscale** statt Port-Forwarding:
Auf beiden Geräten [Tailscale](https://tailscale.com) installieren und anmelden,
dann funktioniert `ssh <MAC-BENUTZER>@<MAC-TAILSCALE-IP>` von überall.

---

## 2. Projekt + Abhängigkeiten (auf dem neuen Mac)

```bash
cd ~
git clone https://github.com/sebastian-spranger/wohnungsmonitor.git
cd wohnungsmonitor

# Python-Umgebung
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Chromium für die Browser-Portale (IS24, HousingAnywhere) — auf dem Mac nativ, kein xvfb nötig
python -m playwright install chromium

chmod +x deploy/run_mac.sh
```

> Falls `python3` fehlt: `xcode-select --install` oder Homebrew (`brew install python@3.12`).

---

## 3. Zustand von der VM übernehmen (einmalig)

Damit keine Nachrichten doppelt/verloren gehen und Finn weiter versorgt wird,
werden Datenbank + State von der VM geholt. Dafür braucht der Mac SSH zur VM
(Alias `oracle` = 152.70.4.186, Schlüssel siehe `~/.ssh/config`):

```bash
# SSH-Schlüssel des Macs zur VM hinterlegen (einmalig, auf dem Mac):
ssh-copy-id oracle

# State holen (auf dem Mac, im Projektordner):
cd ~/wohnungsmonitor
mkdir -p data
scp oracle:~/wohnungsmonitor/.env .                     # Secrets — Datei lokal lassen!
scp oracle:~/wohnungsmonitor/data/app.db data/app.db
scp oracle:~/wohnungsmonitor/seen.json \
    oracle:~/wohnungsmonitor/delivered.json \
    oracle:~/wohnungsmonitor/matches.json \
    oracle:~/wohnungsmonitor/user_filters.json \
    oracle:~/wohnungsmonitor/telegram_offset.json \
    oracle:~/wohnungsmonitor/geocode_cache.json \
    oracle:~/wohnungsmonitor/fahrtzeit_cache.json .

chmod 600 .env
```

`.env` enthält Secrets (Bot-Token, Clerk-Keys) — sie ist in `.gitignore` und
wird nie committet. Prüfen: `cat .env` → `TELEGRAM_TOKEN` und
`CLERK_PUBLISHABLE_KEY` gesetzt, `BASE_URL=https://nohouses.duckdns.org`.

---

## 4. VM-Engine stoppen (sonst doppelte Nachrichten!)

```bash
ssh oracle 'sudo systemctl stop wohnungsmonitor && sudo systemctl disable wohnungsmonitor'
```

Web-App (`webapp`) und Caddy auf der VM bleiben **an** (Login-Seite, Admin-Panel
laufen weiter auf der VM — nur die Scrape-Engine zieht auf den Mac um).

---

## 5. Always-on per LaunchAgent (startet bei Login, Neustart bei Absturz)

`~/Library/LaunchAgents/de.wohnungsmonitor.engine.plist` anlegen:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>de.wohnungsmonitor.engine</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/<MAC-BENUTZER>/wohnungsmonitor/deploy/run_mac.sh</string>
    </array>
    <key>WorkingDirectory</key><string>/Users/<MAC-BENUTZER>/wohnungsmonitor</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/Users/<MAC-BENUTZER>/wohnungsmonitor/monitor.log</string>
    <key>StandardErrorPath</key><string>/Users/<MAC-BENUTZER>/wohnungsmonitor/monitor.log</string>
    <key>EnvironmentVariables</key>
    <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
</dict>
</plist>
```

Aktivieren:

```bash
plutil -lint ~/Library/LaunchAgents/de.wohnungsmonitor.engine.plist   # muss "OK" sagen
launchctl load ~/Library/LaunchAgents/de.wohnungsmonitor.engine.plist
tail -f ~/wohnungsmonitor/monitor.log
```

Stoppen/Neustarten:

```bash
launchctl unload ~/Library/LaunchAgents/de.wohnungsmonitor.engine.plist
launchctl load   ~/Library/LaunchAgents/de.wohnungsmonitor.engine.plist
```

---

## 6. Mac nie schlafen lassen (läuft 24/7)

```bash
sudo pmset -a sleep 0 disksleep 0 displaysleep 10   # System nie schlafen, Bildschirm nach 10 min aus
sudo pmset -a womp 1                                # optional: Wake on LAN
```

(App-Energieeinstellungen: „Computerschlaf verhindern, wenn das Display aus
ist" ggf. ebenfalls aktivieren.)

---

## 7. Testen

```bash
# Einmaliger Probelauf im Vordergrund:
cd ~/wohnungsmonitor && source .venv/bin/activate
python monitor_lite.py

# Erwartet im Log:
#   IS24: N Einträge   (vorher auf der VM: Captcha → 0!)
#   Gesamt: X Inserate
#   ✅ MATCH ... → 1/2 Empfänger ...   (Sebastian + Finn, sobald etwas passt)
```

IS24 sollte jetzt von der Heim-IP **ohne Captcha** liefern. Die Zustellung
bleibt pro Nutzer (delivered.json) — genau wie auf der VM.

---

## 8. Zurück zur VM (falls der Mac ausfällt)

```bash
# 1) Mac-Engine stoppen:
launchctl unload ~/Library/LaunchAgents/de.wohnungsmonitor.engine.plist

# 2) Aktuellen State zurück zur VM schieben (sonst Fehl-/Doppel-Zustellungen):
cd ~/wohnungsmonitor
scp data/app.db seen.json delivered.json matches.json telegram_offset.json \
    geocode_cache.json fahrtzeit_cache.json oracle:~/wohnungsmonitor/

# 3) VM-Engine wieder an:
ssh oracle 'sudo systemctl enable wohnungsmonitor && sudo systemctl start wohnungsmonitor'
```

---

## Sicherheit

- `.env` (Bot-Token, Clerk-Secret) niemals committen — ist in `.gitignore`.
- SSH nur mit Schlüsseln (oben eingerichtet); kein Passwort-Login nötig.
- Für Fernzugriff Tailscale statt offenem Port 22 im Router.
- Der Mac sollte für andere Benutzer gesperrt sein (Bildschirmschoner mit
  Passwort), falls er irgendwo öffentlich steht.
