#!/bin/bash
# Einmaliges Setup für den Wohnungsmonitor

set -e

echo "=== München Wohnungsmonitor – Setup ==="
echo ""

# Virtual environment
if [ ! -d ".venv" ]; then
    echo "1. Erstelle Virtual Environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "2. Installiere Pakete..."
pip install -q -r requirements.txt

echo "3. Installiere Chromium für Playwright..."
playwright install chromium

echo ""
echo "✅ Setup abgeschlossen!"
echo ""
echo "Nächste Schritte:"
echo "  1. Öffne monitor.py und trage TELEGRAM_TOKEN und TELEGRAM_CHAT_ID ein"
echo "     → Telegram-Bot erstellen: schreib @BotFather, /newbot"
echo "     → Chat-ID: schreib dem Bot eine Nachricht, dann öffne:"
echo "       https://api.telegram.org/bot<TOKEN>/getUpdates"
echo ""
echo "  2. Test:"
echo "     source .venv/bin/activate && python monitor.py --test"
echo ""
echo "  3. Monitor starten:"
echo "     source .venv/bin/activate && python monitor.py"
