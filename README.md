# München Wohnungsmonitor

Echtzeit-Scraper für Münchner Mietwohnungen (5+ Portale) mit **Web-Onboarding**:
Einladungscode → Google-Login (Clerk) → eigene Suchfilter → Telegram-Connect
per 1-Klick-Button. Passende Wohnungen kommen sofort als Telegram-Push.

```
monitor_lite.py        – Engine: Scraper + Filter + Telegram-Bot (--serve für always-on)
profiles.py            – SQLite-Store: Nutzer, Einladungscodes, Telegram-Verknüpfungen, Filter
webapp/                – FastAPI-Web-App (Clerk-Login, Dashboard, Einladung)
scripts/invites.py     – Admin-CLI: Einladungscodes erzeugen/listen
deploy/                – systemd-Units + Caddy + oracle-setup.sh (Oracle-VM)
.github/workflows/     – manueller Fallback-Lauf (Hauptbetrieb läuft auf der VM)
```

## Betrieb

| Modus | Wie | Intervall |
|-------|-----|-----------|
| **Oracle-VM (empfohlen)** | systemd: `monitor_lite.py --serve` + `webapp` | 90 s |
| GitHub Actions | nur manuell (`workflow_dispatch`), Fallback | – |
| Lokal | `python monitor_lite.py` | einmalig / Loop |

**Wichtig:** Der GitHub-Actions-Cron wurde entfernt, damit sich CI und VM-Engine
nicht doppelt schedelen. Falls du den Cron doch wieder willst, `schedule:` in
`.github/workflows/check.yml` ergänzen — und nur, wenn die VM aus ist.

---

## Einmalige Einrichtung

### 1. Clerk (Login mit Google)

1. [dashboard.clerk.com](https://dashboard.clerk.com) → *New application* → Name
   z.B. `wohnungsmonitor`, Anmelde-Methoden: **Google** aktivieren (bei den
   Providern). Für Produktion braucht Google-OAuth eigene Credentials im
   Google Cloud Console (Clerk führt dich durch); zum Testen reichen die
   Entwicklungs-Keys von Clerk.
2. **Domains/Origins** → deine Web-URL erlauben:
   `https://<deine-domain>` (und `http://localhost:8000` für lokale Tests).
3. **Sessions → Customize session token** → einfügen:
   ```json
   {"email": "{{user.primary_email_address}}"}
   ```
   Damit liest die App die E-Mail aus dem Session-Token — **kein Secret-Key
   auf dem Server nötig**. (Optional trotzdem `CLERK_SECRET_KEY` setzen.)
4. Publishable Key (`pk_test_…` / `pk_live_…`) kopieren.

### 2. Deployment auf der Oracle-VM

```bash
# Repo auf die VM klonen (oder von GitHub pullen), dann:
cd ~/wohnungsmonitor && bash deploy/oracle-setup.sh
```

- Öffnet nichts von selbst: Ports **80 + 443** in der Oracle-Security-List
  freigeben (Ingress TCP 80/443 von 0.0.0.0/0), Hostname (DuckDNS oder Domain)
  als A-Record auf die VM-IP.
- `deploy/Caddyfile` nach `/etc/caddy/Caddyfile` kopieren, `__DOMAIN__`
  ersetzen, `sudo systemctl restart caddy`.
- `.env` hochladen (Checkliste unten), dann:
  ```bash
  sudo systemctl start wohnungsmonitor webapp
  journalctl -u wohnungsmonitor -f
  curl -s https://<deine-domain>/healthz   # → "clerk_login": true
  ```

### 3. `.env` auf der VM

```bash
# Engines
TELEGRAM_TOKEN=123456:AA…            # Bot von @BotFather
TELEGRAM_CHAT_ID=12345678            # Fallback-Empfänger (nur solange keine DB-Nutzer)
DEEPSEEK_API_KEY=sk-…                # optional: Natursprachen-Filter per Telegram

# Web-App
CLERK_PUBLISHABLE_KEY=pk_test_…      # aus Schritt 1
BASE_URL=https://<deine-domain>
SESSION_SECRET=<32+ zufällige Zeichen>
BOT_USERNAME=Noapartmentsbot          # Handle ohne @, für den /start-Link
# CLERK_SECRET_KEY=sk_test_…          # optional, nur falls kein Session-email-Claim
# APP_DB=data/app.db                  # Standard; SQLite-Pfad
# SERVE_INTERVAL=90                   # Sekunden zwischen Engine-Läufen
```

> `ALLOW_DEV_LOGIN=1` funktioniert **nur** bei `BASE_URL` mit `localhost` —
> gegen die öffentliche URL wird der Dev-Login ignoriert.

### 4. Einladungscodes erzeugen

```bash
.venv/bin/python scripts/invites.py new --count 3     # 3 neue Codes
.venv/bin/python scripts/invites.py list              # Codes + Nutzer ansehen
```

Codes an Interessenten geben → die registrieren sich unter
`https://<deine-domain>` mit Google, geben den Code ein, stellen ihre Filter
ein und klicken „Telegram verknüpfen“ (öffnet den Bot mit `/start <code>` —
der Chat ist sofort verbunden).

### 5. Lokal testen (ohne Clerk)

```bash
ALLOW_DEV_LOGIN=1 SESSION_SECRET=dev \
  .venv/bin/uvicorn webapp.main:app --port 8000
# → http://localhost:8000 , Dev-Login mit beliebiger E-Mail
```

---

## Filter

- **Web-Dashboard:** max. Miete warm/kalt, min. Größe, min. Zimmer,
  max. Umkreis (km, Innenstadt), nur bestimmte Stadtteile.
- **Telegram (optional):** dem Bot frei schreiben („max 1600 warm, ab 2 Zimmer,
  min 55qm, Schwabing oder Maxvorstadt“) — DeepSeek übersetzt das in dieselben
  Felder. `/status` und `/reset` wie gehabt.
- Nutzer ohne eigene Filter bekommen den Standard-Filter
  (max 2200 € warm / 1950 € kalt, min 45 qm, Umkreis 4 km).
- Ohne DB-Nutzer greift der alte Weg (`TELEGRAM_CHAT_IDS` + `user_filters.json`)
  und migriert beim ersten Lauf automatisch in die DB.

## Portale

IS24, ImmoWelt, Kleinanzeigen, WG-Gesucht, Wohnungsboerse, Mr. Lodge,
Wunderflats, HousingAnywhere (IS24/HA brauchen Playwright/Chromium — auf der
VM via `deploy/oracle-setup.sh` installiert).

## Troubleshooting

- **Telegram-Matches kommen nicht:** `journalctl -u wohnungsmonitor -f` prüfen;
  `python monitor_lite.py` einmalig von Hand laufen lassen.
- **IS24 zeigt Captcha:** Engine-Unit auf `xvfb-run -a … monitor_lite.py --serve`
  umstellen (Chromium headful) — siehe Kommentar in `deploy/wohnungsmonitor.service`.
- **Speicher wächst:** `seen.json` löschen → aktuelle Inserate werden einmalig
  neu gemeldet; `matches.json` löschen löscht nur die Historie.
