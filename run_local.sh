#!/bin/bash
# Lokaler, zuverlässiger Runner für den Wohnungsmonitor (via launchd, alle 5 Min).
# Teilt seen.json/Caches per Git mit der Cloud → keine Doppel-Benachrichtigungen.
# Secrets (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, Limits) kommen aus der launchd-plist,
# NICHT aus diesem (eingecheckten) Skript.
set -u
cd "$(dirname "$0")" || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"

echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="

# 1. neuesten Stand holen (seen.json der Cloud), damit nichts doppelt geschickt wird
git pull --rebase --autostash origin main 2>&1 || echo "WARN: git pull fehlgeschlagen"

# 2. Monitor ausführen
./.venv/bin/python monitor_lite.py 2>&1

# 3. aktualisierten Stand zurückschreiben (nur bei Änderungen)
git add seen.json matches.json fahrtzeit_cache.json geocode_cache.json 2>/dev/null
if ! git diff --staged --quiet; then
  git commit -m "chore: update seen [skip ci]" 2>&1
  for i in 1 2 3; do
    git pull --rebase --autostash origin main && git push origin main && break
    echo "WARN: push-Versuch $i fehlgeschlagen"; sleep 5
  done
fi
