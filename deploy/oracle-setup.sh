#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
# oracle-setup.sh — einmaliges Bootstrap für den Wohnungsmonitor auf einer
# Oracle-Cloud-Always-Free-VM (funktioniert auf jedem Ubuntu/Debian- oder
# Oracle-Linux/RHEL-System). Idempotent: sicher, zum Update erneut auszuführen.
#
# Installation (nach SSH auf die VM):
#   sudo apt update && sudo apt install -y git
#   git clone <dein-repo-url> ~/wohnungsmonitor
#   cd ~/wohnungsmonitor && bash deploy/oracle-setup.sh
#
# Dann .env hochladen (siehe Ausgabe unten) und starten:
#   sudo systemctl start wohnungsmonitor webapp
# ─────────────────────────────────────────────────────────────────────────
set -euo pipefail

SERVICE_ENGINE="wohnungsmonitor"
SERVICE_WEBAPP="webapp"
REPO_URL="${REPO_URL:-https://github.com/<DEIN_USERNAME>/wohnungsmonitor.git}"
# Aus dem Repo laufen, falls wir drin sind, sonst ~/wohnungsmonitor klonen.
APP_DIR="$(git rev-parse --show-toplevel 2>/dev/null || echo "$HOME/wohnungsmonitor")"

say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠️  %s\033[0m\n' "$*"; }

# 1. System-Pakete (Paketmanager erkennen) --------------------------------
say "Systempakete installieren (python, git, Fonts)…"
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    # Chromium kommt von Playwright selbst (./venv playwright install chromium) —
    # das Ubuntu-Snap-Paket ist riesig und für headless Playwright unnötig.
    sudo apt-get install -y python3 python3-venv python3-pip git fonts-liberation
    # Ubuntu braucht das venv-Paket der GENUTZTEN Python-Version (z.B. python3.9-venv)
    sudo apt-get install -y python3.9-venv python3.10-venv python3.11-venv 2>/dev/null || true
    sudo apt-get install -y xvfb || true   # nur nötig, falls IS24 headful erzwingt
elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-pip git || \
        sudo dnf install -y python3.11 python3-pip git
elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y python3 python3-pip git
else
    warn "Kein apt/dnf/yum gefunden — python3, git und Chromium manuell installieren."
fi

# 2. Python-Versions-Guard (braucht 3.9+) ---------------------------------
PY="$(command -v python3.11 || command -v python3.10 || command -v python3.9 || command -v python3)"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)'; then
    warn "Python 3.9+ nötig (gefunden: $("$PY" -V)). Auf Oracle Linux: sudo dnf install python3.11, dann erneut ausführen."
    exit 1
fi

# 3. Repo klonen oder updaten ---------------------------------------------
if [ -d "$APP_DIR/.git" ]; then
    say "Vorhandenes Checkout unter $APP_DIR wird aktualisiert"
    git -C "$APP_DIR" pull --ff-only || warn "git pull übersprungen (lokale Änderungen?) — Code nicht aktualisiert."
else
    say "Klonen $REPO_URL -> $APP_DIR"
    git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

# 4. Live-State von git abkoppeln (skip-worktree) --------------------------
# Diese Dateien schreibt die Engine selbst; skip-worktree verhindert, dass
# git pull sie überschreibt oder Konflikte entstehen.
say "Live-State-Dateien von git-Tracking abkoppeln (skip-worktree)…"
for f in seen.json matches.json fahrtzeit_cache.json geocode_cache.json \
         user_filters.json telegram_offset.json; do
    git ls-files --error-unmatch "$f" >/dev/null 2>&1 && git update-index --skip-worktree "$f" || true
done
# data/ (SQLite) ist bereits in .gitignore.

# 5. Virtualenv + Python-Dependencies -------------------------------------
say "Virtualenv anlegen und requirements installieren…"
"$PY" -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
say "Playwright-Browser installieren…"
./.venv/bin/python -m playwright install --with-deps chromium 2>/dev/null || \
    ./.venv/bin/python -m playwright install chromium || true

# 6. systemd-Services installieren ----------------------------------------
say "systemd-Services '$SERVICE_ENGINE' und '$SERVICE_WEBAPP' installieren…"
for unit in $SERVICE_ENGINE $SERVICE_WEBAPP; do
    sed -e "s#__USER__#$USER#g" \
        -e "s#__DIR__#$APP_DIR#g" \
        -e "s#__PY__#$APP_DIR/.venv/bin/python#g" \
        "deploy/$unit.service" | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_ENGINE" >/dev/null 2>&1 || true
sudo systemctl enable "$SERVICE_WEBAPP" >/dev/null 2>&1 || true

# 7. Pre-Flight: .env vorhanden? -------------------------------------------
MISSING=0
if [ ! -f "$APP_DIR/.env" ]; then
    warn "FEHLT $APP_DIR/.env — von deinem Rechner hochladen:"
    echo "     scp .env $USER@<vm-ip>:$APP_DIR/.env"
    MISSING=1
fi
if [ ! -f /etc/caddy/Caddyfile ]; then
    warn "Caddy noch nicht konfiguriert — Hostname eintragen und aktivieren:"
    echo "     cp deploy/Caddyfile /tmp/Caddyfile && nano /tmp/Caddyfile   # __DOMAIN__ ersetzen"
    echo "     sudo apt install -y caddy && sudo cp /tmp/Caddyfile /etc/caddy/Caddyfile"
    echo "     sudo systemctl restart caddy"
    echo "     (Ports 80+443 in der Oracle-Security-List öffnen!)"
    MISSING=1
fi

say "Setup abgeschlossen."
if [ "$MISSING" -eq 1 ]; then
    echo "→ Fehlende Dateien oben ablegen, dann starten:"
else
    echo "→ Alles bereit. Starten:"
fi
echo "     sudo systemctl start $SERVICE_ENGINE $SERVICE_WEBAPP"
echo "     journalctl -u $SERVICE_ENGINE -f    # Engine-Logs"
echo "     journalctl -u $SERVICE_WEBAPP -f    # Webapp-Logs"
echo
echo "Hinweis: GitHub-Actions-Cron deaktivieren, damit nichts doppelt läuft"
echo "(siehe .github/workflows — auf manuellen Fallback umgestellt)."
