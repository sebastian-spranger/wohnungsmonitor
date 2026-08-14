#!/bin/bash
# run_mac.sh — Startskript für den Wohnungsmonitor auf einem Mac (LaunchAgent).
# Die Engine (monitor_lite.py) liest KEINE .env selbst → hier werden die
# Variablen geladen und die Engine im --serve-Modus gestartet.
# Auf dem Mac ist KEIN xvfb nötig (echter Display-Server vorhanden).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
. ./.env
set +a
exec ./.venv/bin/python monitor_lite.py --serve
